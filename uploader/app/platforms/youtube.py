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
    # 조회수·시청시간 통계를 읽으려면 필요하다
    "https://www.googleapis.com/auth/yt-analytics.readonly",
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
    description = build_caption(job, include_title=False, limit=5000, max_tags=15)
    tags = [t.lstrip("#") for t in job.get("hashtags", [])]

    # 영어 현지화 — 영어권 시청자에게는 영어 제목/설명이 보인다.
    en = (job.get("options") or {}).get("localization_en") or {}
    en_title = (en.get("title") or "").strip()[:100]
    en_desc = (en.get("description") or "").strip()
    en_tags = [t.lstrip("#").strip() for t in (en.get("hashtags") or []) if str(t).strip()]
    if en_tags:
        en_desc = (en_desc + "\n\n" + " ".join(f"#{t}" for t in en_tags)).strip()
        tags = tags + en_tags       # 검색 태그는 언어 구분이 없어 함께 넣는다

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags[:15],
            "categoryId": str(options.get("category_id") or "24"),   # 기본: 엔터테인먼트
        },
        "status": {
            "privacyStatus": options.get("privacy") or "private",
            "selfDeclaredMadeForKids": bool(options.get("made_for_kids")),
        },
    }
    parts = "snippet,status"
    if en_title:
        body["snippet"]["defaultLanguage"] = "ko"     # 현지화를 쓰려면 원본 언어가 필요하다
        body["localizations"] = {"en": {"title": en_title, "description": en_desc[:5000]}}
        parts = "snippet,status,localizations"
    size = job["video_size"]

    await progress(5, "업로드 세션 생성 중")
    async with httpx.AsyncClient(timeout=None) as client:
        init = await client.post(
            f"{UPLOAD_API}/videos",
            params={"uploadType": "resumable", "part": parts},
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


# ── 저작권 검사 ──────────────────────────────────────────────
# 유튜브는 올라온 영상을 처리하면서 Content ID 로 검사한다. 그 결과 중
# API 로 볼 수 있는 것만 읽어온다. '수익만 권리자에게 넘어가는 클레임'은
# API 에 나오지 않으므로 스튜디오에서 봐야 한다.
STUDIO_URL = "https://studio.youtube.com/video/{video_id}/copyright"

# 유튜브가 돌려주는 거부 사유를 사람 말로 옮긴 것.
REJECTION_KO = {
    "copyright": "저작권 문제로 유튜브가 거부했습니다",
    "claim": "권리자 신고로 유튜브가 거부했습니다",
    "trademark": "상표권 문제로 유튜브가 거부했습니다",
    "duplicate": "이미 올린 영상과 같다고 판단해 거부했습니다",
    "inappropriate": "커뮤니티 가이드 위반으로 거부했습니다",
    "legal": "법적 사유로 거부했습니다",
    "length": "영상 길이 제한에 걸렸습니다",
    "termsOfUse": "이용약관 위반으로 거부했습니다",
}


async def inspect(account: dict, video_id: str) -> dict:
    """영상 하나의 처리·저작권 상태를 읽는다.

    state 는 셋 중 하나다.
      blocked — 거부됐거나 일부 국가에서 막혔다 (저작권 문제일 가능성이 높다)
      clean   — 처리가 끝났고 걸린 게 없다
      pending — 아직 처리 중이라 판단할 수 없다
    """
    token = await _access_token(account)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{API}/videos",
            params={"part": "status,contentDetails,processingDetails", "id": video_id},
            headers={"Authorization": f"Bearer {token}"},
        )
    if res.status_code >= 400:
        return {"state": "pending", "reason": f"상태를 읽지 못했습니다 ({res.status_code})"}

    items = (res.json() or {}).get("items") or []
    if not items:
        return {"state": "pending", "reason": "영상을 아직 찾을 수 없습니다"}

    item = items[0]
    status = item.get("status") or {}
    content = item.get("contentDetails") or {}
    processing = (item.get("processingDetails") or {}).get("processingStatus")

    upload_status = status.get("uploadStatus")
    if upload_status in ("rejected", "failed"):
        code = status.get("rejectionReason") or status.get("failureReason") or ""
        return {
            "state": "blocked",
            "reason": REJECTION_KO.get(code, f"유튜브가 거부했습니다 ({code or '사유 미상'})"),
            "studio": STUDIO_URL.format(video_id=video_id),
        }

    blocked_regions = ((content.get("regionRestriction") or {}).get("blocked")) or []
    if blocked_regions:
        head = ", ".join(blocked_regions[:5])
        more = f" 외 {len(blocked_regions) - 5}곳" if len(blocked_regions) > 5 else ""
        return {
            "state": "blocked",
            "reason": f"저작권 신고로 {len(blocked_regions)}개 국가에서 차단됐습니다 ({head}{more})",
            "studio": STUDIO_URL.format(video_id=video_id),
        }

    if upload_status == "processed" or processing == "succeeded":
        return {"state": "clean", "reason": "유튜브 검사에서 걸린 것이 없습니다",
                "studio": STUDIO_URL.format(video_id=video_id)}

    return {"state": "pending", "reason": "유튜브가 아직 영상을 처리하는 중입니다",
            "studio": STUDIO_URL.format(video_id=video_id)}


async def set_privacy(account: dict, video_id: str, privacy: str,
                      made_for_kids: bool = False) -> bool:
    """검사를 통과한 뒤 공개범위를 바꾼다."""
    token = await _access_token(account)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.put(
            f"{API}/videos",
            params={"part": "status"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "id": video_id,
                # part 에 status 만 넣었으므로 status 안의 값은 모두 다시 보내야 한다.
                "status": {"privacyStatus": privacy,
                           "selfDeclaredMadeForKids": bool(made_for_kids)},
            },
        )
    if res.status_code >= 400:
        print(f"[youtube] 공개범위 변경 실패 {res.status_code} {res.text[:200]}")
        return False
    return True
