"""YouTube Data API v3 — OAuth + 재개 가능 업로드."""
import time
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import PLATFORMS
from .base import Progress, PublishError, PublishResult, build_caption, stream_file

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def auth_url(state: str) -> str:
    cfg = PLATFORMS["youtube"]
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> list[str]:
    """인가 코드 → 토큰 → 채널 정보 저장. 저장된 account id 목록 반환."""
    cfg = PLATFORMS["youtube"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": cfg.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if res.status_code >= 400:
            raise PublishError(f"YouTube 토큰 교환 실패: {res.text}")
        token = res.json()

        channel = await client.get(
            f"{API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        if channel.status_code >= 400:
            raise PublishError(f"YouTube 채널 조회 실패: {channel.text}")
        items = channel.json().get("items") or []
        if not items:
            raise PublishError("이 구글 계정에 연결된 YouTube 채널이 없습니다.")

    item = items[0]
    snippet = item["snippet"]
    account_id = db.upsert_account(
        "youtube",
        item["id"],
        snippet.get("title") or "YouTube 채널",
        avatar=(snippet.get("thumbnails", {}).get("default") or {}).get("url"),
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        expires_at=time.time() + int(token.get("expires_in", 3600)),
        meta={"custom_url": snippet.get("customUrl")},
    )
    return [account_id]


async def _access_token(account: dict) -> str:
    """만료 임박이면 refresh_token으로 갱신."""
    if account.get("expires_at") and account["expires_at"] - 60 > time.time():
        return account["access_token"]
    if not account.get("refresh_token"):
        return account["access_token"]
    cfg = PLATFORMS["youtube"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            TOKEN_URL,
            data={
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "refresh_token": account["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    if res.status_code >= 400:
        raise PublishError(f"YouTube 토큰 갱신 실패 — 계정을 다시 연결하세요: {res.text}")
    token = res.json()
    db.update_account_tokens(
        account["id"], token["access_token"], token.get("refresh_token"),
        time.time() + int(token.get("expires_in", 3600)),
    )
    return token["access_token"]


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    token = await _access_token(account)
    headers = {"Authorization": f"Bearer {token}"}
    title = (options.get("title") or job["title"] or job["video_name"])[:100]
    description = build_caption(job, include_title=False, limit=5000)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [t.lstrip("#") for t in job.get("hashtags", [])][:15],
            "categoryId": str(options.get("category_id") or "22"),
        },
        "status": {
            "privacyStatus": options.get("privacy") or "private",
            "selfDeclaredMadeForKids": bool(options.get("made_for_kids")),
        },
    }
    size = job["video_size"]

    await progress(5, "업로드 세션 생성 중")
    async with httpx.AsyncClient(timeout=None) as client:
        init = await client.post(
            f"{UPLOAD_API}/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **headers,
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/*",
            },
            json=body,
        )
        if init.status_code >= 400:
            raise PublishError(f"YouTube 업로드 세션 실패: {init.text}")
        location = init.headers.get("location")
        if not location:
            raise PublishError("YouTube가 업로드 URL을 반환하지 않았습니다.")

        res = await client.put(
            location,
            headers={**headers, "Content-Type": "video/*", "Content-Length": str(size)},
            content=stream_file(job["video_path"], progress, base_pct=8, span_pct=82),
        )
        if res.status_code >= 400:
            raise PublishError(f"YouTube 업로드 실패: {res.text}")
        video = res.json()
        video_id = video.get("id")

        if job.get("thumb_path") and video_id:
            await progress(94, "썸네일 등록 중")
            with open(job["thumb_path"], "rb") as fh:
                thumb = await client.post(
                    f"{UPLOAD_API}/thumbnails/set",
                    params={"videoId": video_id},
                    headers=headers,
                    content=fh.read(),
                )
            if thumb.status_code >= 400:
                # 썸네일 실패는 게시 자체를 실패로 보지 않는다.
                await progress(96, "썸네일 등록 실패(영상은 게시됨)")

    return PublishResult(
        remote_id=video_id,
        url=f"https://youtu.be/{video_id}" if video_id else None,
        message=f"게시 완료 · 공개범위 {body['status']['privacyStatus']}",
    )
