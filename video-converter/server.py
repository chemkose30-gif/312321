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
        # 진행 중인 작업은 지우지 않음. 하이라이트 분석처럼 원본을 오래 쓰는 작업은 ttl을 따로 둠
        expired = [j for j in jobs.values()
                   if j["status"] not in ("queued", "running")
                   and now - j["created"] > j.get("ttl", JOB_TTL_SECONDS)]
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


# ------------------------------------------------- 하이라이트 (LCK 등 중계 영상)

HIGHLIGHT_TTL = 3 * 60 * 60  # 분석 후 장면을 고르고 내려받을 시간
HL_STEP = 0.5                 # 소리 크기를 재는 단위 (초)


def _audio_levels(src: Path, duration: float, on_progress) -> "np.ndarray":
    """영상 소리를 0.5초 단위 음량(dB) 배열로 만든다. 저용량 모노 8kHz로 빠르게 디코딩."""
    import numpy as np

    rate = 8000
    win = int(rate * HL_STEP)
    proc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    levels, buf = [], b""
    while chunk := proc.stdout.read(win * 2 * 240):  # 2분 분량씩
        buf += chunk
        n = len(buf) // (win * 2)
        if n:
            x = np.frombuffer(buf[:n * win * 2], dtype=np.int16).astype(np.float32).reshape(n, win)
            buf = buf[n * win * 2:]
            levels.append(20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1.0))
            if duration:
                on_progress(min(1.0, sum(map(len, levels)) * HL_STEP / duration))
    proc.wait()
    if not levels:
        raise RuntimeError("소리를 찾을 수 없습니다. 오디오가 있는 영상인지 확인해 주세요")
    return np.concatenate(levels)


def _find_highlights(levels, max_n: int = 60, min_gap: float = 25.0) -> list[dict]:
    """해설·함성이 평소보다 크게 올라가는 순간을 찾아 점수 순으로 돌려준다.

    평소 음량(앞뒤 90초 중앙값)과 비교하므로 방송 전체 음량이나 쉬는 시간 음악에 덜 휘둘린다.
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    n = len(levels)
    if n < 20:
        return []
    # 2초 평균으로 순간 잡음 제거
    k = 4
    smooth = np.convolve(np.pad(levels, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    # 평소 음량: 앞뒤 90초 구간의 중앙값
    half = int(90 / HL_STEP)
    padded = np.pad(smooth, half, mode="edge")
    baseline = np.median(sliding_window_view(padded, 2 * half + 1), axis=1)[:n]
    excite = smooth - baseline
    # 소리가 거의 없는 구간(무음)은 제외
    excite[smooth < np.percentile(smooth, 10)] = 0

    # 함성·해설 고조는 수십 초 안에 가라앉지만, 쉬는 시간 음악·광고는 계속 크다.
    # 20~45초 뒤에도 여전히 크면 그만큼 점수를 깎는다 (영상 끝이면 앞쪽 구간으로 판단).
    import warnings

    a, b = int(20 / HL_STEP), int(45 / HL_STEP)
    padded_ex = np.pad(excite, b, mode="constant", constant_values=np.nan)
    with warnings.catch_warnings():  # 영상 앞뒤 끝의 빈 구간은 NaN으로 두고 무시
        warnings.simplefilter("ignore", RuntimeWarning)
        after = np.nanmedian(sliding_window_view(padded_ex[b + a:], b - a)[:n], axis=1)
        before = np.nanmedian(sliding_window_view(padded_ex[:n + b - a], b - a)[:n], axis=1)
    sustained = np.where(np.isnan(after), before, after)
    excite = excite - np.clip(np.nan_to_num(sustained), 0, None)

    gap = int(min_gap / HL_STEP)
    order = np.argsort(excite)[::-1]
    picked: list[int] = []
    taken = np.zeros(n, bool)
    for i in order:
        if excite[i] < 2.0 or len(picked) >= max_n:  # 평소보다 2dB 이상 커진 순간만
            break
        if taken[i]:
            continue
        picked.append(int(i))
        taken[max(0, i - gap):i + gap] = True
    if not picked:
        return []
    top = float(excite[picked[0]])
    return [{"t": round(i * HL_STEP, 1), "score": round(float(excite[i]) / top * 100)}
            for i in picked]


def _download_for_highlights(job_id: str, job_dir: Path, url: str, quality: str) -> Path:
    import yt_dlp

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes") or 0) / total * 45 if total else 0
            _update(job_id, progress=round(pct, 1), message="영상 받는 중")

    opts = _ytdlp_opts(outtmpl=str(job_dir / "source.%(ext)s"), progress_hooks=[hook],
                       ffmpeg_location=FFMPEG, merge_output_format="mp4",
                       format=_video_selector(quality))
    with yt_dlp.YoutubeDL(opts) as ydl:
        _update(job_id, message="영상 정보 확인 중")
        info = ydl.extract_info(url, download=False)
        # 4K 장시간 영상은 수십 GB라 디스크가 모자랄 수 있으므로 받기 전에 확인
        # (영상·음성을 받은 뒤 합치는 동안 잠시 두 배 가까이 필요)
        parts = info.get("requested_formats") or [info]
        size = sum(_size(f) for f in parts)
        free = shutil.disk_usage(job_dir).free
        if size and size * 2 + 1024 ** 3 > free:
            raise RuntimeError(
                f"서버 디스크가 부족해요 (영상 약 {size / 1024 ** 3:.1f}GB, 남은 공간 "
                f"{free / 1024 ** 3:.1f}GB). 더 낮은 화질로 시도해 주세요")
        ydl.process_ie_result(info, download=True)
    files = sorted(job_dir.glob("source.*"), key=lambda p: p.stat().st_size)
    if not files:
        raise RuntimeError("영상을 받지 못했습니다")
    _update(job_id, title=_safe_name(info.get("title") or "highlight"), got_height=info.get("height"))
    return files[-1]


def _run_highlights(job_id: str, job_dir: Path, src: Path | None, url: str, quality: str):
    try:
        _update(job_id, status="running", message="준비 중")
        if src is None:
            try:
                src = _download_for_highlights(job_id, job_dir, url, quality)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(_ytdlp_error(e)) from e
        base = 45 if url else 0
        duration, vcodec, _ = _probe(src)
        _update(job_id, source=str(src), message="소리 분석 중", progress=base)
        try:
            levels = _audio_levels(
                src, duration,
                lambda f: _update(job_id, progress=round(base + f * (85 - base), 1)))
            events = _find_highlights(levels)
        except RuntimeError:
            if not vcodec:
                raise
            events = []  # 소리가 없어도 화면(킬 점수)으로는 찾을 수 있음
        if not events and not vcodec:
            raise RuntimeError("두드러지는 장면을 찾지 못했습니다")

        # 장면마다 썸네일 (소리가 터지기 2초 전 화면)
        thumbs = job_dir / "thumbs"
        thumbs.mkdir(exist_ok=True)
        if vcodec:
            for i, ev in enumerate(events):
                subprocess.run(
                    [FFMPEG, "-v", "error", "-y", "-ss", str(max(0, ev["t"] - 2)), "-i", str(src),
                     "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(thumbs / f"{i}.jpg")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ev["thumb"] = (thumbs / f"{i}.jpg").exists()
                _update(job_id, progress=round(85 + (i + 1) / len(events) * 15, 1),
                        message="썸네일 만드는 중")
        _update(job_id, status="done", progress=100, message="완료",
                result={"duration": duration, "events": events, "has_video": bool(vcodec),
                        "height": jobs.get(job_id, {}).get("got_height")})
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")


@app.post("/api/highlights")
async def start_highlights(file: UploadFile | None = File(None), url: str = Form(""),
                           quality: str = Form("1080")):
    url = url.strip()
    if not file and not re.match(r"^https?://", url):
        raise HTTPException(400, "영상 파일이나 유튜브 링크를 넣어주세요")
    if quality != "best" and not quality.isdigit():
        raise HTTPException(400, "잘못된 화질 값입니다")
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("highlights")
    src = None
    if file:
        src, original = await _save_upload(file, job_id, job_dir)
        _update(job_id, title=original)
        url = ""
    _update(job_id, ttl=HIGHLIGHT_TTL, url=url)
    _start_worker(job_id, _run_highlights, job_dir, src, url, quality)
    return {"job_id": job_id}


def _highlight_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("kind") != "highlights" or not job.get("source"):
        raise HTTPException(404, "분석 결과가 없습니다. 만료됐을 수 있으니 다시 분석해 주세요")
    return job


@app.get("/api/highlights/{job_id}/thumb/{name}.jpg")
def highlight_thumb(job_id: str, name: str):
    if not re.fullmatch(r"k?\d+", name):
        raise HTTPException(404)
    path = Path(_highlight_job(job_id)["dir"]) / "thumbs" / f"{name}.jpg"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/highlights/{job_id}/video")
def highlight_video(job_id: str):
    """미리보기용 원본 영상 (구간 요청을 지원해 원하는 위치부터 재생 가능)."""
    return FileResponse(_highlight_job(job_id)["source"], media_type="video/mp4")


# ---- 킬 스코어 변화로 킬 장면 찾기

KILL_W, KILL_H = 64, 32  # 킬 숫자 영역을 이 크기 흑백으로 줄여서 비교


def _kill_filter(boxes, sample_fps: float | None) -> str:
    """킬 숫자 영역들을 잘라 같은 크기 흑백으로 만든 뒤 가로로 이어 붙이는 필터."""
    n = len(boxes)
    head = f"[0:v]{'fps=' + str(sample_fps) + ',' if sample_fps else ''}split={n}" + "".join(f"[s{i}]" for i in range(n)) + ";"
    crops = "".join(f"[s{i}]crop={w}:{h}:{x}:{y},scale={KILL_W}:{KILL_H},format=gray[c{i}];"
                    for i, (x, y, w, h) in enumerate(boxes))
    tail = ("".join(f"[c{i}]" for i in range(n)) + f"hstack=inputs={n}," if n > 1 else "[c0]") + "showinfo[out]"
    return head + crops + tail


def _kill_frames(src: Path, boxes, duration: float, keyframes_only: bool, on_progress=None,
                 start: float = 0.0, length: float | None = None, sample_fps: float = 1):
    """킬 숫자 영역을 뽑아 (시각 배열, 프레임 배열)로 돌려준다. start/length로 일부 구간만도 가능."""
    import numpy as np

    cmd = [FFMPEG, "-v", "info", "-hide_banner"]
    if keyframes_only:  # 키프레임만 디코딩 → 수십 배 빠름 (보통 2~5초 간격)
        cmd += ["-skip_frame", "nokey"]
    if start:
        cmd += ["-ss", f"{start:.2f}"]
    if length:
        cmd += ["-t", f"{length:.2f}"]
    cmd += ["-i", str(src), "-filter_complex", _kill_filter(boxes, None if keyframes_only else sample_fps),
            "-map", "[out]", "-an", "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times: list[float] = []

    def read_times():
        for line in proc.stderr:
            m = re.search(rb"pts_time:\s*([\d.]+)", line)
            if m and b"showinfo" in line:
                times.append(float(m[1]) + start)
                if duration and on_progress:
                    on_progress(min(1.0, times[-1] / duration))
    t = threading.Thread(target=read_times, daemon=True)
    t.start()
    size = KILL_W * KILL_H * len(boxes)
    frames = []
    while chunk := proc.stdout.read(size):
        if len(chunk) < size:
            break
        frames.append(np.frombuffer(chunk, np.uint8))
    proc.wait()
    t.join(timeout=5)
    n = min(len(frames), len(times))
    if not n:
        raise RuntimeError("영상에서 화면을 읽지 못했습니다")
    return np.array(times[:n]), np.stack(frames[:n]).reshape(n, KILL_H, KILL_W * len(boxes)).astype(np.float32)


def _kill_reference(src: Path, boxes, ref_time: float):
    """사용자가 영역을 그린 장면(점수판이 보이는 화면)의 킬 숫자 영역."""
    import numpy as np

    r = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{ref_time:.2f}", "-i", str(src), "-filter_complex",
         _kill_filter(boxes, None).replace(",showinfo", "").replace("showinfo", "null"),
         "-map", "[out]", "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True)
    size = KILL_W * KILL_H * len(boxes)
    if len(r.stdout) < size:
        raise RuntimeError("기준 화면을 읽지 못했습니다")
    return np.frombuffer(r.stdout[:size], np.uint8).reshape(KILL_H, KILL_W * len(boxes)).astype(np.float32)


def _find_kill_changes(times, frames, ref, require_stable: bool = True) -> list[tuple[float, float]]:
    """킬 숫자가 바뀐 시각들.

    - 점수판이 보이는 화면인지: 기준 화면과 비슷한지로 판단 (숫자만 다르면 조금만 다름,
      다른 화면으로 넘어가면 크게 다름) → 장면 전환·리플레이·쉬는 시간은 무시
    - 바뀐 뒤 다음 프레임에서도 그대로면(새 숫자로 안정) 킬로 인정
    """
    import numpy as np

    out: list[float] = []
    n_boxes = frames.shape[2] // KILL_W
    for j in range(n_boxes):
        seg = frames[:, :, j * KILL_W:(j + 1) * KILL_W]
        r = ref[:, j * KILL_W:(j + 1) * KILL_W]
        d_ref = np.abs(seg - r).mean(axis=(1, 2))
        visible = d_ref < max(20.0, 2.5 * np.percentile(d_ref, 30))
        diff = np.r_[0, np.abs(seg[1:] - seg[:-1]).mean(axis=(1, 2))]
        both = visible & np.r_[False, visible[:-1]]
        noise = np.median(diff[both]) if both.any() else 2.0
        thr = max(6.0, noise * 4)
        nxt = np.r_[np.abs(seg[1:] - seg[:-1]).mean(axis=(1, 2)), 0]  # i → i+1 변화
        for i in np.nonzero(both & (diff > thr))[0]:
            # 키프레임처럼 간격이 넓으면 연속 킬로 숫자가 계속 바뀔 수 있어 안정 확인은 정밀 단계에서만
            stable = not require_stable or i + 1 >= len(seg) or (visible[i + 1] and nxt[i] < thr / 2)
            if stable:
                out.append((float(times[i - 1]), float(times[i])))  # 이 사이에 숫자가 바뀜
    return sorted(out)


def _refine_kill_changes(src: Path, boxes, ref, coarse: list[tuple[float, float]]) -> list[float]:
    """키프레임으로 찾은 대략의 구간만 0.5초 간격으로 다시 봐서 정확한 시각과 킬 수를 얻는다."""
    result: list[float] = []
    windows: list[list[float]] = []
    for a, b in coarse:  # 겹치는 구간은 합쳐서 한 번만 디코딩
        if windows and a - 1 <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], b + 1)
        else:
            windows.append([max(0.0, a - 1), b + 1])
    for wa, wb in windows:
        try:
            times, frames = _kill_frames(src, boxes, 0, False, start=wa, length=wb - wa, sample_fps=2)
            fine = _find_kill_changes(times, frames, ref)
        except RuntimeError:
            fine = []
        if fine:
            result += [(x + y) / 2 for x, y in fine]
        else:  # 정밀 확인이 실패하면 대략의 시각이라도 사용
            result += [(x + y) / 2 for x, y in coarse if wa <= x and y <= wb]
    return sorted(result)


def _group_kills(change_times: list[float], gap: float = 12.0) -> list[dict]:
    """몇 초 사이에 연달아 난 킬은 하나의 장면(더블킬·한타)으로 묶는다."""
    groups: list[dict] = []
    for t in change_times:
        if groups and t - groups[-1]["last"] <= gap:
            groups[-1]["kills"] += 1
            groups[-1]["last"] = t
        else:
            groups.append({"t": t, "last": t, "kills": 1})
    return [{"t": round(g["last"], 1), "first": round(g["t"], 1), "kills": g["kills"]} for g in groups]


def _keyframe_skip_works(src: Path, boxes) -> bool:
    """키프레임만 디코딩하는 방식이 이 영상에서 통하는지 (VP9·AV1 일부는 무시하고 전부 디코딩함)."""
    try:
        times, _ = _kill_frames(src, boxes, 0, True, length=30)
    except RuntimeError:
        return False
    return len(times) <= 30  # 30초에 30프레임 이하 = 키프레임만 나옴


def _download_analysis_proxy(source_job: dict) -> Path | None:
    """유튜브 영상이면 분석 전용 저화질 H.264 사본을 받는다 (4K VP9도 빠르게 분석하려고)."""
    import yt_dlp

    if source_job.get("proxy") and Path(source_job["proxy"]).exists():
        return Path(source_job["proxy"])
    url = source_job.get("url")
    if not url:
        return None
    job_dir = Path(source_job["dir"])
    opts = _ytdlp_opts(outtmpl=str(job_dir / "proxy.%(ext)s"),
                       format="bv*[vcodec^=avc][height<=480]/wv*[vcodec^=avc]")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception:  # noqa: BLE001
        return None
    found = sorted(job_dir.glob("proxy.*"))
    if not found:
        return None
    _update(source_job["id"], proxy=str(found[0]))
    return found[0]


def _run_kills(job_id: str, source_job: dict, boxes, ref_time: float):
    try:
        _update(job_id, status="running", message="킬 점수 변화 찾는 중")
        src = Path(source_job["source"])
        duration = (source_job.get("result") or {}).get("duration") or 0
        vw, vh = _video_display_size(src)
        boxes = [b for b in (_clamp_box(b, vw, vh) for b in boxes) if b]
        if not boxes:
            raise RuntimeError("지정한 영역이 화면 밖에 있습니다")
        prog = lambda f: _update(job_id, progress=round(f * 85, 1))  # noqa: E731
        slow_format = not _keyframe_skip_works(src, boxes)
        if slow_format:
            # 4K VP9 등: 원본을 다 디코딩하면 몇 시간 걸리므로 저화질 사본으로 분석
            _update(job_id, message="빠른 분석용 저화질 영상 받는 중")
            proxy = _download_analysis_proxy(source_job)
            if proxy:
                pw, ph = _video_display_size(proxy)
                sx, sy = pw / vw, ph / vh
                scaled = [_clamp_box((round(x * sx), round(y * sy), max(4, round(w * sx)), max(4, round(h * sy))), pw, ph)
                          for x, y, w, h in boxes]
                boxes = [b for b in scaled if b]
                src, slow_format = proxy, False
            _update(job_id, message="킬 점수 변화 찾는 중" + (" (이 영상 형식은 오래 걸려요)" if slow_format else ""))
        ref = _kill_reference(src, boxes, ref_time)
        times, frames = (_kill_frames(src, boxes, duration, False, prog) if slow_format
                         else _kill_frames(src, boxes, duration, True, prog))
        if slow_format:
            changes = [(a + b) / 2 for a, b in _find_kill_changes(times, frames, ref)]
        elif duration and len(times) < duration / 8:
            # 키프레임 간격이 너무 길면(8초 이상) 1초마다 화면을 보는 방식으로 다시 (느림)
            _update(job_id, message="킬 점수 변화 찾는 중 (정밀 모드)", progress=0)
            times, frames = _kill_frames(src, boxes, duration, False, prog)
            changes = [(a + b) / 2 for a, b in _find_kill_changes(times, frames, ref)]
        else:
            # 키프레임으로 대략 찾고 → 그 근처만 0.5초 간격으로 다시 확인 (빠르면서 정확)
            _update(job_id, message="킬 장면 정밀 확인 중", progress=80)
            changes = _refine_kill_changes(src, boxes, ref, _find_kill_changes(times, frames, ref, require_stable=False))
        kills = _group_kills(changes)
        thumbs = Path(source_job["dir"]) / "thumbs"
        thumbs.mkdir(exist_ok=True)
        for i, k in enumerate(kills):
            subprocess.run(
                [FFMPEG, "-v", "error", "-y", "-ss", str(max(0, k["first"] - 3)), "-i", str(src),
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(thumbs / f"k{i}.jpg")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            k["thumb"] = f"k{i}"
            _update(job_id, progress=round(85 + (i + 1) / max(1, len(kills)) * 15, 1), message="썸네일 만드는 중")
        _update(job_id, status="done", progress=100, message="완료", result={"kills": kills})
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")


class KillRequest(BaseModel):
    boxes: list[dict]
    ref_time: float = 0


@app.post("/api/highlights/{job_id}/kills")
def find_kills(job_id: str, req: KillRequest):
    source_job = _highlight_job(job_id)
    if not source_job.get("result", {}).get("has_video"):
        raise HTTPException(400, "영상 화면이 없는 파일입니다")
    boxes = _parse_boxes(json.dumps(req.boxes))
    if len(boxes) > 2:
        raise HTTPException(400, "킬 숫자 영역은 2개까지(양 팀) 지정할 수 있습니다")
    _cleanup_old_jobs()
    new_id, _ = _new_job("kills")
    _start_worker(new_id, _run_kills, source_job, [(max(0, x), max(0, y), w - w % 2, h - h % 2) for x, y, w, h in boxes], req.ref_time)
    return {"job_id": new_id}


class HighlightExport(BaseModel):
    segments: list[tuple[float, float]]
    mode: str = "merged"  # merged | clips


def _fmt_clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}시간{t % 3600 // 60:02d}분{t % 60:02d}초" if t >= 3600 else f"{t // 60}분{t % 60:02d}초"


def _run_highlight_export(job_id: str, job_dir: Path, src: Path, title: str,
                          segments: list[tuple[float, float]], mode: str):
    import zipfile

    try:
        _update(job_id, status="running", message="장면 자르는 중")
        parts = []
        for i, (start, end) in enumerate(segments):
            part = job_dir / f"part{i:03d}.mp4"
            # 재인코딩 없이 잘라서 빠르고 화질 손실이 없음 (시작점은 가까운 키프레임 기준)
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-y", "-ss", f"{start:.2f}", "-i", str(src),
                 "-t", f"{end - start:.2f}", "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                 "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(part)],
                capture_output=True, text=True, errors="replace")
            if r.returncode != 0 or not part.exists():
                raise RuntimeError("자르기 실패: " + r.stderr.strip()[-200:])
            parts.append((part, start))
            _update(job_id, progress=round((i + 1) / len(segments) * 85, 1))

        if mode == "clips":
            out = job_dir / "clips.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
                for n, (part, start) in enumerate(parts, 1):
                    z.write(part, f"{title}_{n:02d}_{_fmt_clock(start)}.mp4")
            filename = f"{title}_하이라이트_클립{len(parts)}개.zip"
        else:
            _update(job_id, message="하나로 합치는 중")
            listing = job_dir / "list.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p, _ in parts))
            out = job_dir / "highlights.mp4"
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-movflags", "+faststart", str(out)],
                capture_output=True, text=True, errors="replace", cwd=job_dir)
            if r.returncode != 0:
                raise RuntimeError("합치기 실패: " + r.stderr.strip()[-200:])
            filename = f"{title}_하이라이트.mp4"
        for part, _ in parts:
            if part != out:
                part.unlink(missing_ok=True)
        _update(job_id, status="done", progress=100, message="완료", file=str(out), filename=filename)
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")


@app.post("/api/highlights/{job_id}/export")
def export_highlights(job_id: str, req: HighlightExport):
    source_job = _highlight_job(job_id)
    if req.mode not in ("merged", "clips"):
        raise HTTPException(400, "잘못된 내보내기 방식입니다")
    duration = (source_job.get("result") or {}).get("duration") or 0
    segs = sorted((max(0.0, s), min(e, duration) if duration else e) for s, e in req.segments)
    segs = [(s, e) for s, e in segs if e - s >= 1]
    if not segs:
        raise HTTPException(400, "장면을 하나 이상 선택하세요")
    if len(segs) > 100:
        raise HTTPException(400, "장면은 100개까지 선택할 수 있습니다")
    # 겹치는 구간은 하나로 합침
    merged = [list(segs[0])]
    for s, e in segs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    _cleanup_old_jobs()
    new_id, new_dir = _new_job("highlight-export")
    _start_worker(new_id, _run_highlight_export, new_dir, Path(source_job["source"]),
                  source_job.get("title") or "highlight", [tuple(m) for m in merged], req.mode)
    return {"job_id": new_id}


# ------------------------------------------------------------ 쇼츠 만들기

SHORTS_W, SHORTS_H = 1080, 1920
SHORTS_VIDEO_TOP = 400
# 가운데 영상 영역의 높이: 원본 비율(16:9) / 확대(1:1) / 크게 확대(4:5)
SHORTS_LAYOUTS = {"fit": 608, "square": 1080, "tall": 1350}
FONTS = {
    "blackhan": BASE_DIR / "static" / "fonts" / "BlackHanSans-Regular.ttf",
    "nanum": BASE_DIR / "static" / "fonts" / "NanumGothic-ExtraBold.ttf",
}
CAPTION_FONT = BASE_DIR / "static" / "fonts" / "NanumGothic-ExtraBold.ttf"
INFO_FONT = BASE_DIR / "static" / "fonts" / "NanumGothic-Bold.ttf"


class ShortsSettings(BaseModel):
    title: str = ""
    subtitle: str = ""
    info: str = ""
    channel: str = ""
    captions: list[dict] = []        # [{"t": 초, "text": "..."}] 다음 자막 전까지 표시
    layout: str = "square"
    zoom: float = 1.0                 # 1.0 ~ 5.0 추가 확대
    offset_x: float = 0.0             # -1(왼쪽) ~ 1(오른쪽)
    offset_y: float = 0.0             # -1(위) ~ 1(아래)
    font: str = "blackhan"
    title_color: str = "#ffffff"


def _wrap_long(lines: list[str], font, measure, max_width: int) -> list[str]:
    """너무 긴 줄은 가운데에 가장 가까운 띄어쓰기에서 두 줄로 나눔 (자막용)."""
    out = []
    for ln in lines:
        spaces = [i for i, ch in enumerate(ln) if ch == " "]
        if measure(ln, font) <= max_width or not spaces:
            out.append(ln)
            continue
        cut = min(spaces, key=lambda i: abs(i - len(ln) / 2))
        out += [ln[:cut].strip(), ln[cut + 1:].strip()]
    return out


def _draw_lines(draw, text: str, font_path: Path, size: int, center_y: float, fill,
                max_width: int = 1000, stroke: int = 0, min_size: int = 24, anchor_mid: bool = True,
                wrap: bool = False):
    """여러 줄 텍스트를 가운데 정렬로 그림. wrap이면 긴 줄을 나누고, 그래도 넘치면 글자 크기를 줄임."""
    from PIL import ImageFont

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return
    if wrap:
        lines = _wrap_long(lines, ImageFont.truetype(str(font_path), size),
                           lambda t, f: draw.textlength(t, font=f) + stroke * 2, max_width)
    while True:
        font = ImageFont.truetype(str(font_path), size)
        widest = max(draw.textlength(ln, font=font) for ln in lines)
        if widest + stroke * 2 <= max_width or size <= min_size:
            break
        size -= 4
    line_h = int(size * 1.22)
    total = line_h * len(lines)
    top = center_y - total / 2 if anchor_mid else center_y
    for i, ln in enumerate(lines):
        draw.text((SHORTS_W / 2, top + i * line_h + line_h / 2), ln, font=font, fill=fill,
                  anchor="mm", stroke_width=stroke, stroke_fill=(0, 0, 0, 255))


def _shorts_video_top(cfg: ShortsSettings) -> int:
    """제목·부제가 있으면 그 아래(400px), 없으면 경기 화면을 세로 가운데에 둠."""
    if cfg.title.strip() or cfg.subtitle.strip():
        return SHORTS_VIDEO_TOP
    return (SHORTS_H - SHORTS_LAYOUTS[cfg.layout]) // 4 * 2


def _shorts_overlays(cfg: ShortsSettings, out_dir: Path, duration: float):
    """제목·정보·채널명이 있는 기본 레이어 1장 + 자막별 레이어 이미지들을 만든다."""
    from PIL import Image, ImageColor, ImageDraw

    vh = SHORTS_LAYOUTS[cfg.layout]
    vt = _shorts_video_top(cfg)
    title_font = FONTS.get(cfg.font, FONTS["blackhan"])
    base = Image.new("RGBA", (SHORTS_W, SHORTS_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    title_rgb = ImageColor.getrgb(cfg.title_color) if re.fullmatch(r"#[0-9a-fA-F]{6}", cfg.title_color) else (255, 255, 255)
    # 제목 영역: 영상 위쪽 (부제가 있으면 부제 아래에 제목)
    if cfg.subtitle.strip():
        _draw_lines(d, cfg.subtitle, INFO_FONT, 50, 205, (235, 235, 235, 255))
        _draw_lines(d, cfg.title, title_font, 96, 300, (*title_rgb, 255), min_size=40)
    else:
        _draw_lines(d, cfg.title, title_font, 100, 255, (*title_rgb, 255), min_size=40)
    _draw_lines(d, cfg.info, INFO_FONT, 36, vt + vh + 60, (200, 200, 200, 255))
    if cfg.channel.strip():  # 영상 가운데 흐릿하게
        _draw_lines(d, cfg.channel, INFO_FONT, 34, vt + vh * 0.5 + 40, (255, 255, 255, 80))
    base_path = out_dir / "base.png"
    base.save(base_path)

    # 빈 자막도 시간 계산에 넣음 → 빈 자막 = 그 시점에 자막 지우기
    caps = sorted(cfg.captions, key=lambda c: float(c.get("t", 0)))
    layers = []
    for i, c in enumerate(caps):
        start = max(0.0, float(c.get("t", 0)))
        end = float(caps[i + 1].get("t", 0)) if i + 1 < len(caps) else duration
        if not str(c.get("text", "")).strip() or end - start < 0.1 or start >= duration:
            continue
        img = Image.new("RGBA", (SHORTS_W, SHORTS_H), (0, 0, 0, 0))
        _draw_lines(ImageDraw.Draw(img), str(c["text"]), CAPTION_FONT, 58,
                    vt + vh * 0.74, (255, 255, 255, 255), stroke=6, wrap=True)
        path = out_dir / f"cap{i}.png"
        img.save(path)
        layers.append((path, start, end))
    return base_path, layers


def _shorts_crop(width: int, height: int, cfg: ShortsSettings) -> tuple[int, int, int, int]:
    """원본에서 가운데 영상 영역 비율에 맞게 잘라낼 부분 (확대·위치 반영)."""
    aspect = SHORTS_W / SHORTS_LAYOUTS[cfg.layout]
    cw, ch = (width, width / aspect) if width / height < aspect else (height * aspect, height)
    zoom = min(5.0, max(1.0, cfg.zoom))
    cw, ch = int(cw / zoom) // 2 * 2, int(ch / zoom) // 2 * 2
    ox, oy = (max(-1.0, min(1.0, v)) for v in (cfg.offset_x, cfg.offset_y))
    x = int((width - cw) / 2 * (1 + ox))
    y = int((height - ch) / 2 * (1 + oy))
    return cw, ch, x, y


def _run_shorts(job_id: str, job_dir: Path, src: Path, start: float, end: float,
                cfg: ShortsSettings, preset: str, cleanup_src: bool):
    try:
        _update(job_id, status="running", message="준비 중")
        duration = end - start
        w, h = _video_display_size(src)
        base, layers = _shorts_overlays(cfg, job_dir, duration)
        cw, ch, x, y = _shorts_crop(w, h, cfg)
        vh = SHORTS_LAYOUTS[cfg.layout]
        graph = (f"[0:v]crop={cw}:{ch}:{x}:{y},scale={SHORTS_W}:{vh},setsar=1,"
                 f"pad={SHORTS_W}:{SHORTS_H}:0:{_shorts_video_top(cfg)}:black[v0];[v0][1:v]overlay=0:0[v1]")
        inputs = ["-i", str(base)]
        for i, (path, a, b) in enumerate(layers):
            inputs += ["-i", str(path)]
            graph += f";[v{i + 1}][{i + 2}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{i + 2}]"
        graph += f";[v{len(layers) + 1}]format=yuv420p[out]"
        out = job_dir / "shorts.mp4"
        cmd = [FFMPEG, "-hide_banner", "-y", "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
               *inputs, "-filter_complex", graph, "-map", "[out]", "-map", "0:a:0?",
               *_video_encoder_args(preset), "-c:a", "aac", "-b:a", "160k",
               "-t", f"{duration:.2f}", "-movflags", "+faststart", str(out)]
        _update(job_id, message="쇼츠 만드는 중" + (" (GPU)" if GPU_ENCODER else ""))
        _run_ffmpeg(job_id, cmd, duration)
        if not out.exists() or _probe(out)[1] is None:
            raise RuntimeError("영상이 만들어지지 않았습니다. 구간이 영상 길이 안에 있는지 확인해 주세요")
        _update(job_id, status="done", progress=100, message="완료", file=str(out))
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")
    finally:
        if cleanup_src:
            src.unlink(missing_ok=True)


@app.post("/api/shorts")
async def start_shorts(settings: str = Form(...), file: UploadFile | None = File(None),
                       hl_job: str = Form(""), start: float = Form(0), end: float = Form(0),
                       preset: str = Form("balanced")):
    try:
        cfg = ShortsSettings(**json.loads(settings))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"설정이 올바르지 않습니다: {e}")
    if cfg.layout not in SHORTS_LAYOUTS:
        raise HTTPException(400, "잘못된 화면 배치입니다")
    if preset not in PRESETS:
        raise HTTPException(400, "잘못된 속도/화질 옵션입니다")
    if len(cfg.captions) > 60:
        raise HTTPException(400, "자막은 60개까지 넣을 수 있습니다")
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("shorts")
    if file:
        src, original = await _save_upload(file, job_id, job_dir)
        cleanup = True
    elif hl_job:
        source_job = _highlight_job(hl_job)
        src, original, cleanup = Path(source_job["source"]), source_job.get("title") or "shorts", False
    else:
        _remove_job(job_id)
        raise HTTPException(400, "영상 파일이나 하이라이트 장면을 선택하세요")
    total = _probe(src)[0]
    end = min(end, total) if end > 0 else total
    start = max(0.0, min(start, end))
    if end - start < 1:
        _remove_job(job_id)
        raise HTTPException(400, "영상 구간이 너무 짧습니다")
    if end - start > 180:
        _remove_job(job_id)
        raise HTTPException(400, "쇼츠는 3분(180초)까지 만들 수 있습니다")
    name = _safe_name(cfg.title.replace("\n", " ")) if cfg.title.strip() else original
    _update(job_id, filename=f"{name}_쇼츠.mp4")
    _start_worker(job_id, _run_shorts, job_dir, src, start, end, cfg, preset, cleanup)
    return {"job_id": job_id}


# ------------------------------------------------- AI 제목·썸네일 문구 (선택)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = "claude-opus-5-5"

AI_SYSTEM = """너는 한국 리그 오브 레전드(LCK) e스포츠 유튜브 쇼츠 채널의 편집자야.
영상 장면 캡처와 편집자가 적은 정보로, 클릭하고 싶어지는 유튜브 제목과 썸네일 문구를 만든다.

원칙:
- 짧고 강렬하게. 감탄·놀람·궁금증을 일으키는 표현(ㄷㄷ, 미친, 역대급, 이게 된다고? 등)을 적극적으로 써도 좋다.
- 단, 사실을 지어내지 마라. 화면이나 주어진 정보로 확인되지 않는 선수 이름, 경기 결과, 기록(예: '최초', '세계 최고')은 쓰지 않는다.
  주어진 정보에 있는 내용은 써도 된다. 거짓 낚시는 채널 신뢰와 유튜브 정책에 해롭다.
- 특정 선수나 팀을 비하하거나 조롱하지 마라.
- 제목은 40자 이내가 좋다(앞 40자 정도만 화면에 보임). 썸네일 문구는 한 줄 8~12자 이내 두 줄.
- 한국어로 쓴다."""

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "description": "장면에서 일어난 일을 한두 문장으로 (확인된 사실만)"},
        "titles": {"type": "array", "items": {"type": "string"}, "description": "유튜브 제목 후보 5개"},
        "thumbnail_texts": {
            "type": "array",
            "description": "썸네일 문구 후보 3개",
            "items": {
                "type": "object",
                "properties": {
                    "line1": {"type": "string"},
                    "line2": {"type": "string"},
                    "highlight": {"type": "string", "description": "강조 색으로 칠할 단어 (line1 또는 line2에 들어있는 단어)"},
                },
                "required": ["line1", "line2", "highlight"],
                "additionalProperties": False,
            },
        },
        "description_hook": {"type": "string", "description": "설명란 첫 줄에 쓸 한두 문장"},
    },
    "required": ["scene", "titles", "thumbnail_texts", "description_hook"],
    "additionalProperties": False,
}


class AiSuggestRequest(BaseModel):
    frames: list[str] = []      # JPEG base64 (data URL 접두사 없이), 최대 4장
    title: str = ""
    subtitle: str = ""
    info: str = ""
    captions: list[str] = []
    kills: int = 0


@app.post("/api/ai/suggest")
def ai_suggest(req: AiSuggestRequest):
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise HTTPException(400, "AI 기능이 꺼져 있어요. 서버 .env에 ANTHROPIC_API_KEY를 넣으면 사용할 수 있어요")
    frames = [f for f in req.frames if f][:4]
    if not frames:
        raise HTTPException(400, "분석할 장면이 없습니다")
    multi = {2: "더블킬", 3: "트리플킬", 4: "쿼드라킬", 5: "펜타킬"}.get(min(req.kills, 5), "")
    facts = "\n".join(line for line in [
        f"- 편집자가 붙인 제목: {req.title}" if req.title.strip() else "",
        f"- 부제: {req.subtitle}" if req.subtitle.strip() else "",
        f"- 경기 정보: {req.info}" if req.info.strip() else "",
        f"- 점수판 분석 결과: 이 장면에서 연속 {req.kills}킬({multi})이 나왔음" if multi else "",
        ("- 영상 자막:\n" + "\n".join(f"  · {c}" for c in req.captions if c.strip())) if any(c.strip() for c in req.captions) else "",
    ] if line) or "- (추가 정보 없음)"
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}} for f in frames]
    content.append({"type": "text", "text": f"위 이미지는 쇼츠 영상에서 시간순으로 뽑은 장면 {len(frames)}장이야.\n\n"
                                            f"편집자가 준 정보:\n{facts}\n\n"
                                            "이 영상의 유튜브 제목 후보 5개와 썸네일 문구 후보 3개를 만들어줘."})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=16000,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
            # 안전 분류기가 거절하면 서버에서 다른 모델로 다시 시도
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise HTTPException(400, "API 키가 올바르지 않아요. .env의 ANTHROPIC_API_KEY를 확인해 주세요")
    except anthropic.RateLimitError:
        raise HTTPException(429, "AI 요청이 너무 많아요. 잠시 후 다시 시도해 주세요")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"AI 서버 오류 ({e.status_code}). 잠시 후 다시 시도해 주세요")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "AI 서버에 연결할 수 없어요")
    if resp.stop_reason == "refusal":
        raise HTTPException(400, "AI가 이 장면에 대한 요청을 처리하지 않았어요. 다른 장면으로 시도해 주세요")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        raise HTTPException(502, "AI 응답을 읽지 못했어요. 다시 시도해 주세요")
    return data


# ------------------------------------------------------------- Common


@app.get("/api/config")
def config():
    return {"max_upload_mb": MAX_UPLOAD_MB, "gpu": GPU_ENCODER is not None,
            "auth": bool(APP_PASSWORD), "ai": bool(ANTHROPIC_API_KEY),
            "max_concurrent": MAX_CONCURRENT}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "작업을 찾을 수 없습니다")
        data = {k: job[k] for k in ("id", "status", "progress", "message", "filename")}
        if job.get("result") is not None:
            data["result"] = job["result"]
        return data


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
    media = {".mp3": "audio/mpeg", ".png": "image/png", ".zip": "application/zip"}.get(
        Path(job["filename"]).suffix, "video/mp4")
    return FileResponse(job["file"], media_type=media, filename=job["filename"],
                        background=background)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
