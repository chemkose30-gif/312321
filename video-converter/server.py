"""유튜브 다운로드 / 영상 → MP4 변환 웹앱.

실행: python server.py  →  http://localhost:8000
환경변수로 설정합니다 (README 참고): APP_PASSWORD, MAX_UPLOAD_MB, MAX_CONCURRENT,
WORK_DIR, HOST, PORT, DISABLE_GPU
"""
import asyncio
import base64
import hmac
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
from fastapi.responses import FileResponse, Response
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


@app.middleware("http")
async def password_guard(request: Request, call_next):
    """APP_PASSWORD가 설정되어 있으면 브라우저 기본 로그인 창으로 비밀번호를 묻는다."""
    if APP_PASSWORD:
        header = request.headers.get("authorization", "")
        password = ""
        if header.startswith("Basic "):
            try:
                password = base64.b64decode(header[6:]).decode().partition(":")[2]
            except Exception:  # noqa: BLE001
                password = ""
            if not hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
                await asyncio.sleep(1)  # 무차별 대입 완화
                password = None
        if not password:
            return Response(
                "비밀번호가 필요합니다", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="video-converter", charset="UTF-8"'},
            )
    return await call_next(request)


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
        if copy_video:
            cmd += ["-c:v", "copy"]
        elif GPU_ENCODER:
            cmd += ["-c:v", GPU_ENCODER, *PRESETS[preset]["gpu"], "-rc", "vbr", "-b:v", "0"]
        else:
            cmd += ["-c:v", "libx264", *PRESETS[preset]["cpu"]]
        if not copy_video:
            cmd += ["-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]

        if copy_video and copy_audio:
            mode = "컨테이너 변환 중 (재인코딩 없음)"
        else:
            mode = "인코딩 중" + (" (GPU)" if GPU_ENCODER and not copy_video else "")
        _update(job_id, message=mode)
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
        _update(job_id, status="done", progress=100, message="완료", file=str(out))
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", message=f"실패: {e}")
    finally:
        src.unlink(missing_ok=True)


@app.post("/api/convert")
async def start_convert(file: UploadFile = File(...), preset: str = Form("balanced")):
    if preset not in PRESETS:
        raise HTTPException(400, "잘못된 속도/화질 옵션입니다")
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("convert")
    original = _safe_name(Path(file.filename or "video").stem)
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
    out = job_dir / "output.mp4"
    _update(job_id, filename=f"{original}.mp4")
    _start_worker(job_id, _run_convert, src, out, preset)
    return {"job_id": job_id}


# ------------------------------------------------------------- Common


@app.get("/api/config")
def config():
    return {"max_upload_mb": MAX_UPLOAD_MB, "gpu": GPU_ENCODER is not None,
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
    media = "audio/mpeg" if job["filename"].endswith(".mp3") else "video/mp4"
    return FileResponse(job["file"], media_type=media, filename=job["filename"],
                        background=background)


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
