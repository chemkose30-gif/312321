"""유튜브 다운로드 / 영상 → MP4 변환 로컬 웹앱.

실행: python server.py  →  http://localhost:8000
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
WORK_DIR = Path(tempfile.gettempdir()) / "video-converter"
WORK_DIR.mkdir(exist_ok=True)
JOB_TTL_SECONDS = 60 * 60


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

app = FastAPI(title="Video Converter")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


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


class YoutubeRequest(BaseModel):
    url: str
    format: str = "mp4"  # mp4 | mp3
    quality: str = "best"  # best | 1080 | 720 | 480 | 360


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

    opts = {
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "restrictfilenames": False,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": FFMPEG,
    }
    if req.format == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        h = "" if req.quality == "best" else f"[height<={int(req.quality)}]"
        opts["format"] = (
            f"bv*{h}[ext=mp4]+ba[ext=m4a]/b{h}[ext=mp4]/bv*{h}+ba/b{h}/b"
        )
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
        msg = str(e).replace("ERROR: ", "")
        _update(job_id, status="error", message=f"실패: {msg[:300]}")


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
    threading.Thread(target=_run_youtube, args=(job_id, job_dir, req), daemon=True).start()
    return {"job_id": job_id}


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


def _run_convert(job_id: str, src: Path, out: Path):
    try:
        _update(job_id, status="running", message="파일 분석 중")
        duration, vcodec, acodec = _probe(src)
        if vcodec is None:
            raise RuntimeError("영상 스트림을 찾을 수 없습니다. 지원하지 않는 파일일 수 있습니다")

        # 이미 MP4 호환 코덱이면 재인코딩 없이 컨테이너만 바꿔 빠르게 처리
        copy_video = vcodec == "h264"
        copy_audio = acodec in (None, "aac", "mp3")
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c:v", "copy"] if copy_video else [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]

        mode = "컨테이너 변환 중" if copy_video and copy_audio else "인코딩 중"
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
async def start_convert(file: UploadFile = File(...)):
    _cleanup_old_jobs()
    job_id, job_dir = _new_job("convert")
    original = _safe_name(Path(file.filename or "video").stem)
    suffix = Path(file.filename or "").suffix.lower() or ".bin"
    src = job_dir / f"input{suffix}"
    with src.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    out = job_dir / "output.mp4"
    _update(job_id, filename=f"{original}.mp4")
    threading.Thread(target=_run_convert, args=(job_id, src, out), daemon=True).start()
    return {"job_id": job_id}


# ------------------------------------------------------------- Common


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

    uvicorn.run(app, host="127.0.0.1", port=8000)
