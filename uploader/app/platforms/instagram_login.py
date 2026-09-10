"""Instagram 자체 로그인 방식 (페이스북 페이지 없이 인스타만 연결).

메타의 'Instagram API with Instagram Login' 흐름이다.
- 페이스북 페이지가 필요 없고, 인스타 프로페셔널 계정만 있으면 된다.
- 앱 ID/시크릿도 페이스북 앱과 별개인 'Instagram 앱 ID / 시크릿' 을 쓴다.
"""
import asyncio
import time
from urllib.parse import urlencode

import httpx

from .. import credentials, db
from ..config import PUBLIC_BASE_URL
from .base import Progress, PublishError, PublishResult, build_caption

AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH = "https://graph.instagram.com/v23.0"
LONG_LIVED_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
SCOPES = "instagram_business_basic,instagram_business_content_publish"

CRED_KEY = "instagram_login"  # 인스타 전용 앱 자격증명 보관 키
MODE_KEY = "instagram_auth_mode"


def enabled() -> bool:
    """인스타를 '직접 로그인' 방식으로 쓰도록 설정돼 있는지."""
    return db.get_setting(MODE_KEY) == "instagram"


def app_id() -> str:
    return credentials.get(CRED_KEY, "client_id")


def app_secret() -> str:
    return credentials.get(CRED_KEY, "client_secret")


def configured() -> bool:
    return bool(app_id() and app_secret())


def redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/api/oauth/instagram_login/callback"


def auth_url(state: str) -> str:
    params = {
        "client_id": app_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            TOKEN_URL,
            data={
                "client_id": app_id(),
                "client_secret": app_secret(),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(),
                "code": code.split("#")[0],  # 인스타가 코드 끝에 '#_' 를 붙여 보낸다
            },
        )
        if res.status_code >= 400:
            raise PublishError(f"Instagram 토큰 교환 실패: {res.text[:300]}")
        token = res.json()
        short_lived = token.get("access_token")
        user_id = str(token.get("user_id") or "")
        if not short_lived:
            raise PublishError("Instagram이 액세스 토큰을 반환하지 않았습니다.")

        # 60일짜리 장기 토큰으로 교환
        long_lived = await client.get(
            LONG_LIVED_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret(),
                "access_token": short_lived,
            },
        )
        if long_lived.status_code < 400:
            body = long_lived.json()
            access_token = body.get("access_token", short_lived)
            expires_at = time.time() + int(body.get("expires_in", 60 * 24 * 3600))
        else:
            access_token, expires_at = short_lived, time.time() + 3600

        me = await client.get(
            f"{GRAPH}/me",
            params={
                "fields": "user_id,username,profile_picture_url",
                "access_token": access_token,
            },
        )
        profile = me.json() if me.status_code < 400 else {}

    external_id = str(profile.get("user_id") or user_id)
    if not external_id:
        raise PublishError("Instagram 계정 정보를 가져오지 못했습니다.")
    account_id = db.upsert_account(
        "instagram",
        external_id,
        profile.get("username") or "Instagram 계정",
        avatar=profile.get("profile_picture_url"),
        access_token=access_token,
        expires_at=expires_at,
        meta={"auth": "instagram_login"},
    )
    return [account_id]


async def _fresh_token(account: dict) -> str:
    """만료가 가까우면 장기 토큰을 갱신한다(60일짜리)."""
    token = account["access_token"]
    expires_at = account.get("expires_at") or 0
    if expires_at - time.time() > 7 * 86400:
        return token
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            REFRESH_URL, params={"grant_type": "ig_refresh_token", "access_token": token}
        )
    if res.status_code < 400:
        body = res.json()
        new_token = body.get("access_token", token)
        db.update_account_tokens(
            account["id"], new_token, None,
            time.time() + int(body.get("expires_in", 60 * 24 * 3600)),
        )
        return new_token
    return token


async def publish(account: dict, job: dict, options: dict, progress: Progress) -> PublishResult:
    from .instagram import media_url  # 공개 영상 주소 생성은 동일하게 사용

    token = await _fresh_token(account)
    ig_user_id = account["external_id"]
    video_url = media_url(job)
    if PUBLIC_BASE_URL.startswith("http://localhost") or PUBLIC_BASE_URL.startswith("http://127."):
        raise PublishError(
            "Instagram은 외부에서 접근 가능한 영상 URL이 필요합니다. "
            "PUBLIC_BASE_URL을 공개 주소로 설정하세요."
        )

    await progress(8, "릴스 컨테이너 생성 중")
    async with httpx.AsyncClient(timeout=None) as client:
        create = await client.post(
            f"{GRAPH}/{ig_user_id}/media",
            params={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": build_caption(job, limit=2200),
                "share_to_feed": "true" if options.get("share_to_feed", True) else "false",
                "access_token": token,
            },
        )
        if create.status_code >= 400:
            raise PublishError(f"Instagram 컨테이너 생성 실패: {create.text[:300]}")
        container_id = (create.json() or {}).get("id")
        if not container_id:
            raise PublishError("Instagram이 컨테이너 ID를 반환하지 않았습니다.")

        for attempt in range(60):
            await asyncio.sleep(4)
            status = await client.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
            body = status.json() if status.status_code < 400 else {}
            code = body.get("status_code")
            await progress(min(15 + attempt * 2, 85), f"Instagram 처리 중 ({code or '대기'})")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram 영상 처리 실패: {body.get('status') or code}")
        else:
            raise PublishError("Instagram 영상 처리 시간이 초과되었습니다.")

        await progress(92, "게시 중")
        published = await client.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            params={"creation_id": container_id, "access_token": token},
        )
        if published.status_code >= 400:
            raise PublishError(f"Instagram 게시 실패: {published.text[:300]}")
        media_id = (published.json() or {}).get("id")

        url = None
        if media_id:
            link = await client.get(
                f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}
            )
            if link.status_code < 400:
                url = (link.json() or {}).get("permalink")

    return PublishResult(remote_id=media_id, url=url, message=f"@{account['name']} 릴스 게시 완료")
