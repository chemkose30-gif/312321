"""TikTok Content Posting API v2 — OAuth + FILE_UPLOAD 게시."""
import asyncio
import time
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import PLATFORMS
from .base import Progress, PublishError, PublishResult, build_caption, stream_file

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
DEFAULT_SCOPES = "user.info.basic,video.publish,video.upload"


def scopes() -> str:
    """앱에서 지정한 권한 목록(없으면 기본값).

    틱톡은 승인되지 않은 scope 를 요청하면 로그인 자체를 거부하므로,
    승인된 것만 요청하도록 바꿀 수 있게 한다.
    """
    from .. import db

    return (db.get_setting("tiktok_scopes") or "").strip() or DEFAULT_SCOPES


def auth_url(state: str) -> str:
    cfg = PLATFORMS["tiktok"]
    params = {
        "client_key": cfg.client_id,
        "scope": scopes(),
        "response_type": "code",
        "redirect_uri": cfg.redirect_uri,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> list[str]:
    cfg = PLATFORMS["tiktok"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            TOKEN_URL,
            data={
                "client_key": cfg.client_id,
                "client_secret": cfg.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": cfg.redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if res.status_code >= 400:
            raise PublishError(f"TikTok 토큰 교환 실패: {res.text}")
        token = res.json()
        if token.get("error"):
            raise PublishError(f"TikTok 토큰 교환 실패: {token.get('error_description') or token['error']}")

        access = token["access_token"]
        info = await client.get(
            f"{API}/user/info/",
            params={"fields": "open_id,display_name,avatar_url"},
            headers={"Authorization": f"Bearer {access}"},
        )
        user = (info.json().get("data") or {}).get("user", {}) if info.status_code < 400 else {}
        creator = await client.post(
            f"{API}/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8"},
        )
        creator_data = (creator.json().get("data") or {}) if creator.status_code < 400 else {}

    account_id = db.upsert_account(
        "tiktok",
        token.get("open_id") or user.get("open_id") or "tiktok",
        user.get("display_name") or "TikTok 계정",
        avatar=user.get("avatar_url"),
        access_token=access,
        refresh_token=token.get("refresh_token"),
        expires_at=time.time() + int(token.get("expires_in", 86400)),
        meta={
            "privacy_options": creator_data.get("privacy_level_options") or ["SELF_ONLY"],
            "max_duration_sec": creator_data.get("max_video_post_duration_sec"),
        },
    )
    return [account_id]


async def _access_token(account: dict) -> str:
    if account.get("expires_at") and account["expires_at"] - 60 > time.time():
        return account["access_token"]
    if not account.get("refresh_token"):
        return account["access_token"]
    cfg = PLATFORMS["tiktok"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            TOKEN_URL,
            data={
                "client_key": cfg.client_id,
                "client_secret": cfg.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": account["refresh_token"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    token = res.json()
    if res.status_code >= 400 or token.get("error"):
        raise PublishError("TikTok 토큰 갱신 실패 — 계정을 다시 연결하세요.")
    db.update_account_tokens(
        account["id"], token["access_token"], token.get("refresh_token"),
        time.time() + int(token.get("expires_in", 86400)),
    )
    return token["access_token"]


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    token = await _access_token(account)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    size = job["video_size"]
    # 단일 청크 업로드(단일 청크는 5MB~64MB 권장, 작은 파일은 전체 1청크).
    payload = {
        "post_info": {
            "title": build_caption(job, limit=2200),
            "privacy_level": options.get("privacy") or "SELF_ONLY",
            "disable_duet": bool(options.get("disable_duet")),
            "disable_comment": bool(options.get("disable_comment")),
            "disable_stitch": bool(options.get("disable_stitch")),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        },
    }

    await progress(5, "게시 세션 생성 중")
    async with httpx.AsyncClient(timeout=None) as client:
        init = await client.post(f"{API}/post/publish/video/init/", headers=headers, json=payload)
        data = init.json() if init.content else {}
        error = (data.get("error") or {}).get("code", "ok")
        if init.status_code >= 400 or error not in ("ok", "", None):
            raise PublishError(f"TikTok 게시 초기화 실패: {(data.get('error') or {}).get('message') or init.text}")
        info = data.get("data") or {}
        publish_id, upload_url = info.get("publish_id"), info.get("upload_url")
        if not upload_url:
            raise PublishError("TikTok이 업로드 URL을 반환하지 않았습니다.")

        res = await client.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            content=stream_file(job["video_path"], progress, base_pct=8, span_pct=75),
        )
        if res.status_code >= 400:
            raise PublishError(f"TikTok 업로드 실패: {res.text}")

        # 인코딩/게시 완료까지 상태 폴링.
        status_label = "처리 중"
        for attempt in range(40):
            await asyncio.sleep(3)
            check = await client.post(
                f"{API}/post/publish/status/fetch/", headers=headers, json={"publish_id": publish_id}
            )
            body = (check.json().get("data") or {}) if check.status_code < 400 else {}
            status_label = body.get("status") or status_label
            await progress(min(85 + attempt, 97), f"TikTok {status_label}")
            if status_label in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                break
            if status_label == "FAILED":
                raise PublishError(f"TikTok 게시 실패: {body.get('fail_reason') or '알 수 없는 오류'}")

    message = "게시 완료"
    if status_label == "SEND_TO_USER_INBOX":
        message = "TikTok 앱 알림함으로 전송됨 — 앱에서 최종 게시하세요"
    return PublishResult(
        remote_id=publish_id,
        url=f"https://www.tiktok.com/@{account['name']}",
        message=message,
    )
