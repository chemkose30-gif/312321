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


# 플랫폼 쪽 일시 오류로 실패하면 이 간격으로 자동 재시도한다.
RETRY_DELAYS_MIN = (15, 60, 180)


def _retry_label(minutes: int) -> str:
    return f"{minutes}분" if minutes < 60 else f"{minutes // 60}시간"


def _schedule_retry(target: dict, message: str) -> bool:
    """일시 오류면 다음 재시도를 예약한다. 예약했으면 True."""
    count = int(target.get("retry_count") or 0)
    if count >= len(RETRY_DELAYS_MIN):
        return False
    minutes = RETRY_DELAYS_MIN[count]
    when = time.time() + minutes * 60
    clock = time.strftime("%H:%M", time.localtime(when))
    db.update_target(
        target["id"],
        status="retry",
        progress=0,
        message=f"{message[:400]} — {clock}에 자동으로 다시 올립니다"
                f" ({_retry_label(minutes)} 뒤 · 예약 {count + 1}/{len(RETRY_DELAYS_MIN)})",
        retry_at=when,
        retry_count=count + 1,
    )
    return True


def _log_target_failure(platform: str, target: dict, message: str, *, transient: bool) -> None:
    """게시 실패를 서버 로그에도 남긴다 — 어느 계정이 왜 막히는지 추적하려면 필요하다."""
    account = db.get_account(target["account_id"], with_tokens=False) or {}
    print(
        f"[publish] {platform} 실패 · 계정={account.get('display_name') or target['account_id']}"
        f" · 재시도대상={transient} · {message[:400]}"
    )


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

        # 이 계정에만 다른 제목을 쓰기로 했으면 그 제목으로 바꿔 게시한다.
        # (build_caption 이 job["title"] 을 보므로 작업 자체를 복사해서 넘긴다)
        override = (target.get("title") or "").strip()
        if override:
            job = {**job, "title": override}
            options = {**options, "title": override}

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
        _log_target_failure(platform, target, str(exc), transient=getattr(exc, "transient", False))
        if getattr(exc, "transient", False) and _schedule_retry(target, str(exc)):
            return "retry"
        db.update_target(target_id, status="failed", message=str(exc)[:800])
        return "failed"
    except Exception as exc:  # 네트워크/예상 못한 오류 — 일시적일 수 있어 다시 시도한다
        message = f"{type(exc).__name__}: {exc}"[:800]
        _log_target_failure(platform, target, message, transient=True)
        if _schedule_retry(target, message):
            return "retry"
        db.update_target(target_id, status="failed", message=message)
        return "failed"


def _job_status(results: list[str]) -> str:
    if "retry" in results:
        return "retrying"
    if all(r == "success" for r in results):
        return "success"
    if any(r == "success" for r in results):
        return "partial"
    return "failed"


async def retry_due_targets() -> int:
    """재시도할 때가 된 대상들을 다시 올린다. 처리한 개수를 돌려준다."""
    due = db.targets_due_for_retry(time.time())
    if not due:
        return 0

    by_job: dict[str, list[dict]] = {}
    for target in due:
        by_job.setdefault(target["job_id"], []).append(target)

    handled = 0
    for job_id, targets in by_job.items():
        job = db.get_job(job_id)
        if not job:
            continue
        if not Path(job["video_path"]).exists():
            for target in targets:
                db.update_target(
                    target["id"], status="failed",
                    message="영상 원본이 이미 지워져 다시 시도할 수 없습니다. 새로 올려주세요.",
                )
            continue
        db.set_job_status(job_id, "running")
        results = await asyncio.gather(*(_run_target(job, t) for t in targets))
        handled += len(results)
        # 이 작업의 모든 대상을 기준으로 상태를 다시 계산한다.
        fresh = db.get_job(job_id)
        db.set_job_status(job_id, _job_status([t["status"] for t in fresh["targets"]]))
        if not db.has_pending_retry(job_id) and delete_after_publish():
            freed = drop_job_video(fresh)
            if freed:
                print(f"[cleanup] 재시도 완료 후 원본 삭제 ({freed / 1024 / 1024:.0f}MB)")
    return handled


async def run_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    db.set_job_status(job_id, "running")
    results = await asyncio.gather(*(_run_target(job, t) for t in job["targets"]))
    db.set_job_status(job_id, _job_status(results))

    # 재시도가 남아 있으면 원본을 지우지 않는다(다시 보낼 때 필요).
    if not db.has_pending_retry(job_id) and delete_after_publish():
        freed = drop_job_video(job)
        if freed:
            print(f"[cleanup] 게시 완료 후 원본 삭제 ({freed / 1024 / 1024:.0f}MB)")


# 디스크가 이만큼도 안 남으면 오래된 것부터 지운다. 여유가 없으면 쓰기가 급격히 느려진다.
MIN_FREE_BYTES = 3 * 1024**3
RETENTION_KEY = "media_retention_days"
DEFAULT_RETENTION_DAYS = 7
# 게시가 끝나면 원본을 바로 지울지 ("on" 기본 / "off" 면 보관 기간까지 남긴다)
DELETE_AFTER_KEY = "delete_after_publish"


def delete_after_publish() -> bool:
    return (db.get_setting(DELETE_AFTER_KEY) or "on") != "off"


def drop_job_video(job: dict) -> int:
    """게시가 끝난 작업의 영상 원본을 지운다. 지운 바이트 수를 돌려준다."""
    path = Path(job.get("video_path") or "")
    if not path.name or not path.exists():
        return 0
    # 같은 파일을 쓰는 다른 작업이 아직 돌고 있으면 건드리지 않는다.
    if str(path) in db.active_video_paths():
        return 0
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except OSError:
        return 0


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
