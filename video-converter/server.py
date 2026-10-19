"""유튜브 다운로드 / 영상 → MP4 변환 웹앱.

실행: python server.py  →  http://localhost:8000
환경변수로 설정합니다 (README 참고): APP_PASSWORD, MAX_UPLOAD_MB, MAX_CONCURRENT,
WORK_DIR, HOST, PORT, DISABLE_GPU
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
WORK_DIR = Path(os.environ.get("WORK_DIR") or Path(tempfile.gettempdir()) / "video-converter")
WORK_DIR.mkdir(parents=True, exist_ok=True)
JOB_TTL_SECONDS = 60 * 60
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4096"))
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT", "2")))
# 유튜브 봇 차단 대응: 로그인 쿠키 파일 / 프록시 (README 참고)
COOKIES_FILE = Path(os.environ.get("YTDLP_COOKIES") or BASE_DIR / "cookies" / "cookies.txt")
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "")
# PO 토큰 제공 서버 (쿠키 없이 봇 차단을 줄이는 용도, docker-compose에 포함)
YTDLP_POT_URL = os.environ.get("YTDLP_POT_URL", "")

# 서버 재시작 시 이전에 남은 작업 폴더 정리
for _old in WORK_DIR.iterdir():
    if _old.is_dir() and _old.name != "bin":
        shutil.rmtree(_old, ignore_errors=True)


def _find_ffmpeg() -> str:
    """시스템 ffmpeg가 있으면 사용하고, 없으면 imageio-ffmpeg 내장 바이너리를 사용."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg

    bundled = imageio_ffmpeg.get_ffmpeg_exe()
    # yt-dlp가 인식할 수 있도록 'ffmpeg'라는 이름의 링크를 만든다
    bin_dir = WORK_DIR / "bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not link.exists():
        try:
            link.symlink_to(bundled)
        except OSError:
            shutil.copy2(bundled, link)
    return str(link)


FFMPEG = _find_ffmpeg()


def _detect_gpu_encoder() -> str | None:
    """NVIDIA GPU 인코더(NVENC)를 실제로 써볼 수 있는지 확인."""
    if os.environ.get("DISABLE_GPU") == "1":
        return None
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=s=256x256:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
        capture_output=True,
    )
    return "h264_nvenc" if r.returncode == 0 else None


GPU_ENCODER = _detect_gpu_encoder()

# 속도/화질 프리셋: fast(빠름, 파일 조금 큼) / balanced(기본) / quality(고화질, 느림)
PRESETS = {
    "fast": {"cpu": ["-preset", "ultrafast", "-crf", "26"], "gpu": ["-preset", "p1", "-cq", "26"]},
    "balanced": {"cpu": ["-preset", "veryfast", "-crf", "23"], "gpu": ["-preset", "p4", "-cq", "23"]},
    "quality": {"cpu": ["-preset", "slow", "-crf", "20"], "gpu": ["-preset", "p7", "-cq", "20"]},
}

app = FastAPI(title="Video Converter")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
# 동시에 돌아가는 변환/다운로드 수 제한 (나머지는 순서대로 대기)
work_slots = threading.Semaphore(MAX_CONCURRENT)


SESSION_COOKIE = "vc_session"
# 비밀번호에서 만든 세션 토큰: 비밀번호를 바꾸면 기존 로그인은 모두 풀린다
SESSION_TOKEN = hmac.new(APP_PASSWORD.encode(), b"video-converter-session",
                         hashlib.sha256).hexdigest()
PUBLIC_PATHS = {"/login.html", "/api/login"}


def _logged_in(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), SESSION_TOKEN)


@app.middleware("http")
async def password_guard(request: Request, call_next):
    """APP_PASSWORD가 설정되어 있으면 로그인 페이지에서 비밀번호를 받는다."""
    if APP_PASSWORD and request.url.path not in PUBLIC_PATHS and not _logged_in(request):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
        return RedirectResponse("/login.html", status_code=302)
    return await call_next(request)


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    if not APP_PASSWORD:
        return {"ok": True}
    if not hmac.compare_digest(req.password.encode(), APP_PASSWORD.encode()):
        await asyncio.sleep(1)  # 무차별 대입 완화
        raise HTTPException(401, "비밀번호가 틀렸습니다")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, SESSION_TOKEN, max_age=60 * 60 * 24 * 30, httponly=True,
        samesite="lax", secure=request.headers.get("x-forwarded-proto") == "https",
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _start_worker(job_id: str, target, *args):
    def run():
        _update(job_id, message="대기 중 (다른 작업이 끝나면 시작)")
        with work_slots:
            target(job_id, *args)
    threading.Thread(target=run, daemon=True).start()


def _new_job(kind: str) -> tuple[str, Path]:
    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",  # queued | running | done | error
            "progress": 0.0,
            "message": "대기 중",
            "file": None,
            "filename": None,
            "dir": str(job_dir),
            "created": time.time(),
        }
    return job_id, job_dir


def _update(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _cleanup_old_jobs():
    now = time.time()
    with jobs_lock:
        expired = [j for j in jobs.values() if now - j["created"] > JOB_TTL_SECONDS]
        for j in expired:
            jobs.pop(j["id"], None)
    for j in expired:
        shutil.rmtree(j["dir"], ignore_errors=True)


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")
    return name[:150] or "video"


# ---------------------------------------------------------------- YouTube


def _ytdlp_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, **extra}
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    if YTDLP_POT_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [YTDLP_POT_URL]}}
    return opts


def _ytdlp_error(e: Exception) -> str:
    msg = str(e).replace("ERROR: ", "")
    if "not a bot" in msg or "Sign in to confirm" in msg:
        if COOKIES_FILE.is_file():
            return ("유튜브가 서버를 봇으로 차단했어요. 등록된 쿠키가 만료됐을 수 있으니 "
                    "화면 아래 '유튜브 쿠키 설정'에서 새 쿠키를 등록해 주세요.")
        return ("유튜브가 서버를 봇으로 차단했어요. 화면 아래 '유튜브 쿠키 설정'에서 "
                "쿠키를 등록하거나, 관리자가 프록시(YTDLP_PROXY)를 설정해야 해요.")
    return msg[:300]


class YoutubeRequest(BaseModel):
    url: str
    format: str = "mp4"  # mp4 | mp3
    quality: str = "best"  # best | 영상 높이(px) 예: 2160, 1080, 720


QUALITY_NAMES = {4320: "8K", 2160: "4K", 1440: "QHD", 1080: "Full HD", 720: "HD"}


def _video_selector(height: str) -> str:
    """선택한 화질을 정확히 받되, 같은 화질 안에서는 호환성 좋은 코덱을 우선.

    H.264(avc) > VP9 > 그 외(AV1 등) 순서. 유튜브는 H.264를 1080p까지만 주므로
    1440p/4K는 VP9로 받아진다. 정확한 화질이 없으면 그 이하 최고 화질로 대체.
    """
    if height == "best":
        return "bv*+ba[ext=m4a]/bv*+ba/b"
    eq, le = f"[height={int(height)}]", f"[height<={int(height)}]"
    return (f"bv*{eq}[vcodec^=avc]+ba[ext=m4a]/bv*{eq}[vcodec^=vp]+ba[ext=m4a]"
            f"/bv*{eq}+ba[ext=m4a]/bv*{eq}+ba"
            f"/bv*{le}+ba[ext=m4a]/bv*{le}+ba/b{le}/b")


def _size(f: dict) -> int:
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


def _summarize_formats(info: dict) -> dict:
    """yt-dlp 영상 정보에서 화질별 선택지(예상 용량 포함)를 만든다."""
    formats = info.get("formats") or []
    audios = [f for f in formats
              if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    m4a = [f for f in audios if f.get("ext") == "m4a"] or audios
    best_audio = max(m4a, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = _size(best_audio) if best_audio else 0

    by_height: dict[int, list[dict]] = {}
    for f in formats:
        if f.get("height") and f.get("vcodec") not in (None, "none"):
            by_height.setdefault(int(f["height"]), []).append(f)

    qualities = []
    for height, fs in sorted(by_height.items(), reverse=True):
        # 다운로드 시 실제로 고를 포맷과 같은 우선순위로 대표 포맷 선택
        for pick in (lambda f: f["vcodec"].startswith("avc"),
                     lambda f: f["vcodec"].startswith("vp"),
                     lambda f: True):
            cands = [f for f in fs if pick(f)]
            if cands:
                break
        chosen = max(cands, key=lambda f: f.get("tbr") or 0)
        size = _size(chosen)
        if size and chosen.get("acodec") in (None, "none"):
            size += audio_size
        short_side = min(height, int(chosen.get("width") or height))
        fps = int(max((f.get("fps") or 0) for f in fs))
        label = f"{short_side}p" + (str(fps) if fps > 30 else "")
        qualities.append({
            "height": height,
            "label": label,
            "name": QUALITY_NAMES.get(short_side, ""),
            "size": size,
            # H.264가 아니면 일부 기기(아이폰 기본 앱, 구형 PC 등)에서 재생이 안 될 수 있음
            "compatible": chosen["vcodec"].startswith("avc"),
        })

    duration = info.get("duration") or 0
    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": duration,
        "qualities": qualities,
        "mp3_size": int(duration * 192_000 / 8),
    }


@app.get("/api/youtube/info")
def youtube_info(url: str):
    import yt_dlp

    if not re.match(r"^https?://", url.strip()):
        raise HTTPException(400, "올바른 URL을 입력하세요")
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "영상 정보를 가져오지 못했습니다: " + _ytdlp_error(e))
    if info.get("_type") == "playlist":
        raise HTTPException(400, "재생목록이 아닌 영상 하나의 링크를 넣어주세요")
    return _summarize_formats(info)


def _run_youtube(job_id: str, job_dir: Path, req: YoutubeRequest):
    import yt_dlp

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 90) if total else 0
            _update(job_id, progress=round(pct, 1), message="다운로드 중")
        elif d["status"] == "finished":
            _update(job_id, progress=90, message="변환 중")

    opts = _ytdlp_opts(
        outtmpl=str(job_dir / "%(title)s.%(ext)s"),
        progress_hooks=[hook],
        ffmpeg_location=FFMPEG,
    )
    if req.format == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = _video_selector(req.quality)
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]

    try:
        _update(job_id, status="running", message="영상 정보 확인 중")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
        ext = "mp3" if req.format == "mp3" else "mp4"
        outputs = sorted(job_dir.glob(f"*.{ext}"), key=lambda p: p.stat().st_mtime)
        if not outputs:
            raise RuntimeError("출력 파일을 찾을 수 없습니다")
        out = outputs[-1]
        title = _safe_name(info.get("title") or out.stem)
        _update(job_id, status="done", progress=100, message="완료",
                file=str(out), filename=f"{title}.{ext}")
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {_ytdlp_error(e)}")


@app.post("/api/youtube")
def start_youtube(req: YoutubeRequest):
    if not re.match(r"^https?://", req.url.strip()):
        raise HTTPException(400, "올바른 URL을 입력하세요")
    if req.format not in ("mp4", "mp3"):
        raise HTTPException(400, "지원하지 않는 포맷입니다")
    if req.quality != "best" and not req.quality.isdigit():
        raise HTTPException(400, "잘못된 화질 값입니다")
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("youtube")
    _start_worker(job_id, _run_youtube, job_dir, req)
    return {"job_id": job_id}


@app.get("/api/youtube/cookies")
def cookies_status():
    if not COOKIES_FILE.is_file():
        return {"set": False}
    return {"set": True, "updated": int(COOKIES_FILE.stat().st_mtime)}


@app.post("/api/youtube/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    data = await file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise HTTPException(400, "쿠키 파일이 너무 큽니다")
    text = data.decode("utf-8", errors="replace")
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    if "HTTP Cookie File" not in first_line:
        raise HTTPException(400, "cookies.txt(Netscape 형식) 파일이 아닙니다")
    if "youtube.com" not in text:
        raise HTTPException(400, "유튜브 쿠키가 들어있지 않습니다. youtube.com에서 내보내 주세요")
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(text, encoding="utf-8")
    return {"set": True, "updated": int(COOKIES_FILE.stat().st_mtime)}


@app.delete("/api/youtube/cookies")
def delete_cookies():
    COOKIES_FILE.unlink(missing_ok=True)
    return {"set": False}


# ---------------------------------------------------------- File → MP4


def _probe(path: Path) -> tuple[float, str | None, str | None]:
    """ffmpeg -i 출력에서 길이(초), 비디오 코덱, 오디오 코덱을 추출."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True, errors="replace")
    err = r.stderr
    duration = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    v = re.search(r"Stream #.*?Video: (\w+).*", err)
    a = re.search(r"Stream #.*?Audio: (\w+)", err)
    vcodec = v[1] if v else None
    # yuv420p가 아닌 H.264(4:4:4 등)는 폰/브라우저에서 재생이 안 될 수 있어 재인코딩 대상
    if vcodec == "h264" and "yuv420p" not in v[0]:
        vcodec = "h264-other"
    return duration, vcodec, (a[1] if a else None)


def _run_ffmpeg(job_id: str, cmd: list[str], duration: float):
    """ffmpeg를 실행하며 -progress 출력으로 진행률을 갱신한다. cmd 끝에 출력 파일."""
    cmd = cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    # stderr가 가득 차서 멈추지 않도록 별도 스레드에서 비운다
    err_tail: list[str] = []
    def drain():
        for line in proc.stderr:
            err_tail.append(line)
            del err_tail[:-20]
    threading.Thread(target=drain, daemon=True).start()

    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            try:
                sec = int(line.split("=")[1]) / 1_000_000
                _update(job_id, progress=round(min(sec / duration * 100, 99), 1))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 오류: " + "".join(err_tail[-3:]).strip()[:300])


def _video_encoder_args(preset: str) -> list[str]:
    if GPU_ENCODER:
        return ["-c:v", GPU_ENCODER, *PRESETS[preset]["gpu"], "-rc", "vbr", "-b:v", "0"]
    return ["-c:v", "libx264", *PRESETS[preset]["cpu"]]


def _run_convert(job_id: str, src: Path, out: Path, preset: str):
    try:
        _update(job_id, status="running", message="파일 분석 중")
        duration, vcodec, acodec = _probe(src)
        if vcodec is None:
            raise RuntimeError("영상 스트림을 찾을 수 없습니다. 지원하지 않는 파일일 수 있습니다")

        # 이미 MP4 호환 코덱이면 재인코딩 없이 컨테이너만 바꿔 빠르게 처리
        copy_video = vcodec == "h264"
        copy_audio = acodec in (None, "aac", "mp3")
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c:v", "copy"] if copy_video else _video_encoder_args(preset)
        if not copy_video:
            cmd += ["-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(out)]

        if copy_video and copy_audio:
            mode = "컨테이너 변환 중 (재인코딩 없음)"
        else:
            mode = "인코딩 중" + (" (GPU)" if GPU_ENCODER and not copy_video else "")
        _update(job_id, message=mode)
        _run_ffmpeg(job_id, cmd, duration)
        _update(job_id, status="done", progress=100, message="완료", file=str(out))
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")
    finally:
        src.unlink(missing_ok=True)


async def _save_upload(file: UploadFile, job_id: str, job_dir: Path) -> tuple[Path, str]:
    """업로드를 작업 폴더에 저장하고 (저장 경로, 원본 파일 이름)을 돌려준다."""
    original = _safe_name(Path(file.filename or "file").stem)
    suffix = re.sub(r"[^.\w]", "", Path(file.filename or "").suffix.lower()) or ".bin"
    src = job_dir / f"input{suffix}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with src.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                break
            f.write(chunk)
    if written > limit:
        _remove_job(job_id)
        raise HTTPException(413, f"파일이 너무 큽니다 (최대 {MAX_UPLOAD_MB} MB)")
    return src, original


@app.post("/api/convert")
async def start_convert(file: UploadFile = File(...), preset: str = Form("balanced")):
    if preset not in PRESETS:
        raise HTTPException(400, "잘못된 속도/화질 옵션입니다")
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("convert")
    src, original = await _save_upload(file, job_id, job_dir)
    out = job_dir / "output.mp4"
    _update(job_id, filename=f"{original}.mp4")
    _start_worker(job_id, _run_convert, src, out, preset)
    return {"job_id": job_id}


# ------------------------------------------------------ Watermark 지우기

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _parse_boxes(raw: str) -> list[tuple[int, int, int, int]]:
    try:
        boxes = [tuple(int(round(float(v))) for v in (b["x"], b["y"], b["w"], b["h"]))
                 for b in json.loads(raw)]
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "지울 영역 정보가 올바르지 않습니다")
    boxes = [b for b in boxes if b[2] >= 2 and b[3] >= 2]
    if not boxes:
        raise HTTPException(400, "지울 영역을 하나 이상 지정하세요")
    if len(boxes) > 20:
        raise HTTPException(400, "영역은 20개까지 지정할 수 있습니다")
    return boxes


def _clamp_box(box, width: int, height: int, margin: int = 0):
    x, y, w, h = box
    x0, y0 = max(margin, x), max(margin, y)
    x1, y1 = min(width - margin, x + w), min(height - margin, y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 - x0 >= 2 and y1 - y0 >= 2 else None


def _harmonic(img, fill_mask):
    """fill_mask 영역을 주변 픽셀에서 매끄럽게 이어지도록 채움 (라플라스 방정식).

    큰 영역도 빨리 수렴하도록 작은 해상도에서 먼저 풀고 키워가며 다듬는다(coarse→fine).
    그라데이션·하늘·벽처럼 부드러운 배경에서는 자국 없이 채워진다.
    """
    import cv2

    h, w = img.shape[:2]
    if min(h, w) <= 24:
        out = img.copy()
        if (~fill_mask).any():
            out[fill_mask] = img[~fill_mask].mean(axis=0)
        iters = 300
    else:
        size = ((w + 1) // 2, (h + 1) // 2)
        small = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(fill_mask.astype("uint8"), size, interpolation=cv2.INTER_NEAREST) > 0
        up = cv2.resize(_harmonic(small, small_mask), (w, h), interpolation=cv2.INTER_LINEAR)
        out = img.copy()
        out[fill_mask] = up[fill_mask]
        iters = 40
    for _ in range(iters):
        out[fill_mask] = cv2.blur(out, (3, 3))[fill_mask]
    return out


def _fill_regions(img, mask):
    """mask가 있는 부분 주변만 잘라서 채움 (큰 이미지도 빠르게)."""
    import numpy as np

    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    m = max(y1 - y0, x1 - x0) // 2 + 8
    y0, y1 = max(0, y0 - m), min(img.shape[0], y1 + m)
    x0, x1 = max(0, x0 - m), min(img.shape[1], x1 + m)
    crop_mask = mask[y0:y1, x0:x1] > 0
    filled = _harmonic(img[y0:y1, x0:x1].astype(np.float32), crop_mask)
    out = img.copy()
    out[y0:y1, x0:x1][crop_mask] = np.clip(filled[crop_mask], 0, 255).astype(img.dtype)
    return out


def _run_watermark_image(job_id: str, src: Path, out: Path, boxes):
    import cv2
    import numpy as np

    try:
        _update(job_id, status="running", message="지우는 중", progress=30)
        data = np.fromfile(str(src), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if img is not None and (img.ndim == 2 or img.shape[2] != 4):
            # 투명도가 없으면 EXIF 회전을 반영해 읽음 (브라우저에 보이는 방향과 좌표를 맞춤)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("이미지를 읽을 수 없습니다")
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if img.dtype != np.uint8:  # 16비트 PNG 등
            img = (img / 257).astype(np.uint8)
        h, w = img.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        for box in boxes:
            b = _clamp_box(box, w, h)
            if b:
                x, y, bw, bh = b
                mask[y:y + bh, x:x + bw] = 255
        if not mask.any():
            raise RuntimeError("지정한 영역이 이미지 밖에 있습니다")
        # 글자 테두리(안티앨리어싱)까지 지우도록 영역을 살짝 넓힘
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        fixed = _fill_regions(img, mask)  # 투명(알파) 채널도 같이 채워짐
        ok, buf = cv2.imencode(".png", fixed)
        if not ok:
            raise RuntimeError("PNG 저장에 실패했습니다")
        out.write_bytes(buf.tobytes())
        _update(job_id, status="done", progress=100, message="완료", file=str(out))
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")
    finally:
        src.unlink(missing_ok=True)


def _video_display_size(path: Path) -> tuple[int, int]:
    """회전 정보를 반영한 영상 표시 크기 (ffmpeg 필터가 보는 크기와 같음)."""
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, errors="replace").stderr
    m = re.search(r"Stream #.*?Video: .*?(\d{2,5})x(\d{2,5})", err)
    if not m:
        raise RuntimeError("영상 크기를 확인할 수 없습니다")
    w, h = int(m[1]), int(m[2])
    if re.search(r"rotation of -?(90|270)\.|rotate\s*:\s*(90|270)", err):
        w, h = h, w
    return w, h


def _run_watermark_video(job_id: str, src: Path, out: Path, boxes, preset: str):
    try:
        _update(job_id, status="running", message="파일 분석 중")
        duration, vcodec, acodec = _probe(src)
        if vcodec is None:
            raise RuntimeError("영상 스트림을 찾을 수 없습니다")
        w, h = _video_display_size(src)
        # delogo는 영역이 화면 가장자리에 닿으면 오류가 나서 1px 안쪽으로 맞춤
        regions = [b for b in (_clamp_box(box, w, h, margin=1) for box in boxes) if b]
        if not regions:
            raise RuntimeError("지정한 영역이 영상 밖에 있습니다")
        vf = ",".join(f"delogo=x={x}:y={y}:w={bw}:h={bh}" for x, y, bw, bh in regions)
        vf += ",scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
               "-vf", vf, *_video_encoder_args(preset), "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "copy"] if acodec in (None, "aac", "mp3") else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", str(out)]
        _update(job_id, message="지우는 중" + (" (GPU)" if GPU_ENCODER else ""))
        _run_ffmpeg(job_id, cmd, duration)
        _update(job_id, status="done", progress=100, message="완료", file=str(out))
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")
    finally:
        src.unlink(missing_ok=True)


@app.post("/api/watermark")
async def start_watermark(file: UploadFile = File(...), boxes: str = Form(...),
                          preset: str = Form("balanced")):
    if preset not in PRESETS:
        raise HTTPException(400, "잘못된 속도/화질 옵션입니다")
    regions = _parse_boxes(boxes)
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("watermark")
    src, original = await _save_upload(file, job_id, job_dir)
    if src.suffix in IMAGE_EXTS or (file.content_type or "").startswith("image/"):
        out = job_dir / "output.png"
        _update(job_id, filename=f"{original}_워터마크제거.png")
        _start_worker(job_id, _run_watermark_image, src, out, regions)
    else:
        out = job_dir / "output.mp4"
        _update(job_id, filename=f"{original}_워터마크제거.mp4")
        _start_worker(job_id, _run_watermark_video, src, out, regions, preset)
    return {"job_id": job_id}


# ------------------------------------------------------------- Common


@app.get("/api/config")
def config():
    return {"max_upload_mb": MAX_UPLOAD_MB, "gpu": GPU_ENCODER is not None,
            "auth": bool(APP_PASSWORD),
            "max_concurrent": MAX_CONCURRENT}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "작업을 찾을 수 없습니다")
        return {k: job[k] for k in ("id", "status", "progress", "message", "filename")}


def _remove_job(job_id: str):
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, background: BackgroundTasks):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        raise HTTPException(404, "다운로드할 파일이 없습니다")
    background.add_task(_remove_job, job_id)
    media = {".mp3": "audio/mpeg", ".png": "image/png"}.get(Path(job["filename"]).suffix, "video/mp4")
    return FileResponse(job["file"], media_type=media, filename=job["filename"],
                        background=background)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
