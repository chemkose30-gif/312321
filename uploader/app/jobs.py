"""업로드 작업 오케스트레이션 — 플랫폼별 게시를 동시에 실행."""
import asyncio
import contextlib
import random
import shutil
import time
from pathlib import Path

from . import db
from .config import UPLOAD_DIR, demo_platform
from .platforms import facebook, instagram, instagram_login, tiktok, youtube
from .platforms.base import Progress, PublishError, PublishResult

async def _publish_instagram(account, job, options, progress):
    """계정이 어떤 방식으로 연결됐는지에 따라 게시 경로를 고른다."""
    if (account.get("meta") or {}).get("auth") == "instagram_login":
        return await instagram_login.publish(account, job, options, progress)
    return await instagram.publish(account, job, options, progress)


PUBLISHERS = {
    "youtube": youtube.publish,
    "tiktok": tiktok.publish,
    "instagram": _publish_instagram,
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
        elif not account.get("access_token"):
            # 아이디만 직접 등록해 둔 계정 — 아직 로그인 연결이 안 됨.
            raise PublishError(
                f"'{account['name']}'은 아이디만 등록된 계정입니다. "
                "계정 관리에서 로그인 연결을 마친 뒤 다시 시도하세요."
            )
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


# 디스크가 이만큼도 안 남으면 오래된 것부터 지운다. 여유가 없으면 쓰기가 급격히 느려진다.
MIN_FREE_BYTES = 3 * 1024**3
RETENTION_KEY = "media_retention_days"
DEFAULT_RETENTION_DAYS = 7


def retention_days() -> int:
    try:
        value = int(db.get_setting(RETENTION_KEY) or DEFAULT_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(1, min(value, 90))


def disk_usage() -> dict:
    """업로드 폴더가 있는 디스크의 사용량."""
    total, used, free = shutil.disk_usage(UPLOAD_DIR)
    uploads = 0
    for path in Path(UPLOAD_DIR).glob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                uploads += path.stat().st_size
    return {"total": total, "used": used, "free": free, "uploads": uploads}


def cleanup_old_files(max_age_days: int | None = None) -> dict:
    """오래된 업로드 원본을 정리한다. 그래도 여유가 없으면 오래된 순으로 더 지운다.

    게시가 진행 중인 영상은 건드리지 않는다.
    """
    days = retention_days() if max_age_days is None else max(0, max_age_days)
    protected = db.active_video_paths()
    removed, freed = 0, 0
    cutoff = time.time() - days * 86400
    files = []
    for path in Path(UPLOAD_DIR).glob("*"):
        try:
            if not path.is_file() or str(path) in protected:
                continue
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            with contextlib.suppress(OSError):
                size = stat.st_size
                path.unlink()
                removed += 1
                freed += size
            continue
        files.append((stat.st_mtime, stat.st_size, path))

    # 보존 기간 안이어도 디스크가 부족하면 오래된 것부터 비운다.
    files.sort()
    while files and shutil.disk_usage(UPLOAD_DIR).free < MIN_FREE_BYTES:
        _, size, path = files.pop(0)
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
            freed += size
    return {"removed": removed, "freed": freed, "retention_days": days}
