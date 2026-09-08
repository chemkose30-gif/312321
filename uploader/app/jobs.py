"""업로드 작업 오케스트레이션 — 플랫폼별 게시를 동시에 실행."""
import asyncio
import random
import time
from pathlib import Path

from . import db
from .config import UPLOAD_DIR, demo_platform
from .platforms import facebook, instagram, tiktok, youtube
from .platforms.base import Progress, PublishError, PublishResult

PUBLISHERS = {
    "youtube": youtube.publish,
    "tiktok": tiktok.publish,
    "instagram": instagram.publish,
    "facebook": facebook.publish,
}

DEMO_URLS = {
    "youtube": "https://youtu.be/demo{n}",
    "tiktok": "https://www.tiktok.com/@demo/video/{n}",
    "instagram": "https://www.instagram.com/reel/demo{n}/",
    "facebook": "https://www.facebook.com/demo/videos/{n}",
}


def _progress_for(target_id: str) -> Progress:
    async def report(pct: int, message: str) -> None:
        db.update_target(target_id, status="running", progress=max(0, min(pct, 99)), message=message)

    return report


async def _demo_publish(platform: str, job: dict, progress: Progress) -> PublishResult:
    """자격증명이 없을 때 UI를 확인할 수 있도록 업로드를 흉내낸다."""
    steps = [
        (10, "업로드 세션 생성 중"),
        (35, "영상 전송 중 (35%)"),
        (65, "영상 전송 중 (65%)"),
        (85, "플랫폼 인코딩 대기 중"),
        (97, "게시 처리 중"),
    ]
    for pct, message in steps:
        await asyncio.sleep(0.9)
        await progress(pct, f"[데모] {message}")
    fake = random.randint(100000, 999999)
    return PublishResult(
        remote_id=f"demo_{fake}",
        url=DEMO_URLS[platform].format(n=fake),
        message="[데모] 실제 API 키가 없어 업로드를 시뮬레이션했습니다.",
    )


async def _run_target(job: dict, target: dict) -> str:
    target_id, platform = target["id"], target["platform"]
    progress = _progress_for(target_id)
    try:
        await progress(2, "준비 중")
        account = db.get_account(target["account_id"])
        if account is None:
            raise PublishError("연결된 계정을 찾을 수 없습니다. 계정을 다시 연결하세요.")
        options = (job.get("options") or {}).get(platform, {}) or {}

        if demo_platform(platform):
            result = await _demo_publish(platform, job, progress)
        else:
            result = await PUBLISHERS[platform](account, job, options, progress)

        db.update_target(
            target_id, status="success", progress=100,
            message=result.message, remote_id=result.remote_id, url=result.url,
        )
        return "success"
    except PublishError as exc:
        db.update_target(target_id, status="failed", message=str(exc)[:800])
        return "failed"
    except Exception as exc:  # 네트워크/예상 못한 오류
        db.update_target(target_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:800])
        return "failed"


async def run_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    db.set_job_status(job_id, "running")
    results = await asyncio.gather(*(_run_target(job, t) for t in job["targets"]))
    if all(r == "success" for r in results):
        status = "success"
    elif any(r == "success" for r in results):
        status = "partial"
    else:
        status = "failed"
    db.set_job_status(job_id, status)


def cleanup_old_files(max_age_days: int = 7) -> None:
    """오래된 업로드 원본 파일 정리(디스크 보호)."""
    cutoff = time.time() - max_age_days * 86400
    for path in Path(UPLOAD_DIR).glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
