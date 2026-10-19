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
        info = ydl.extract_info(url, download=True)
    files = sorted(job_dir.glob("source.*"), key=lambda p: p.stat().st_size)
    if not files:
        raise RuntimeError("영상을 받지 못했습니다")
    _update(job_id, title=_safe_name(info.get("title") or "highlight"))
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
        levels = _audio_levels(
            src, duration,
            lambda f: _update(job_id, progress=round(base + f * (85 - base), 1)))
        events = _find_highlights(levels)
        if not events:
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
                result={"duration": duration, "events": events, "has_video": bool(vcodec)})
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
    _update(job_id, ttl=HIGHLIGHT_TTL)
    _start_worker(job_id, _run_highlights, job_dir, src, url, quality)
    return {"job_id": job_id}


def _highlight_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("kind") != "highlights" or not job.get("source"):
        raise HTTPException(404, "분석 결과가 없습니다. 만료됐을 수 있으니 다시 분석해 주세요")
    return job


@app.get("/api/highlights/{job_id}/thumb/{index}.jpg")
def highlight_thumb(job_id: str, index: int):
    path = Path(_highlight_job(job_id)["dir"]) / "thumbs" / f"{index}.jpg"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/highlights/{job_id}/video")
def highlight_video(job_id: str):
    """미리보기용 원본 영상 (구간 요청을 지원해 원하는 위치부터 재생 가능)."""
    return FileResponse(_highlight_job(job_id)["source"], media_type="video/mp4")


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
