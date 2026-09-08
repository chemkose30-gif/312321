"""Meta(Facebook/Instagram) 공용 OAuth — 페이지와 연결된 IG 비즈니스 계정을 함께 등록."""
import time
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import GRAPH, META_API_VERSION, PLATFORMS
from .base import PublishError

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "publish_video",
    "instagram_basic",
    "instagram_content_publish",
    "business_management",
]


def auth_url(state: str) -> str:
    cfg = PLATFORMS["facebook"]
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return f"https://www.facebook.com/{META_API_VERSION}/dialog/oauth?{urlencode(params)}"


async def exchange_code(code: str) -> list[str]:
    """코드 → 장기 사용자 토큰 → 페이지 목록 + 각 페이지의 IG 계정을 계정으로 저장."""
    cfg = PLATFORMS["facebook"]
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": cfg.redirect_uri,
                "code": code,
            },
        )
        if res.status_code >= 400:
            raise PublishError(f"Meta 토큰 교환 실패: {res.text}")
        user_token = res.json()["access_token"]

        # 60일짜리 장기 토큰으로 교환.
        longlived = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "fb_exchange_token": user_token,
            },
        )
        if longlived.status_code < 400:
            token_body = longlived.json()
            user_token = token_body.get("access_token", user_token)
            expires_at = time.time() + int(token_body.get("expires_in", 60 * 24 * 3600))
        else:
            expires_at = time.time() + 3600

        pages = await client.get(
            f"{GRAPH}/me/accounts",
            params={
                "fields": "id,name,access_token,picture{url},instagram_business_account{id,username,profile_picture_url}",
                "access_token": user_token,
            },
        )
        if pages.status_code >= 400:
            raise PublishError(f"Facebook 페이지 조회 실패: {pages.text}")
        items = pages.json().get("data") or []

    if not items:
        raise PublishError(
            "관리 중인 Facebook 페이지가 없습니다. 페이지를 만들고 앱에 권한을 부여한 뒤 다시 시도하세요."
        )

    account_ids: list[str] = []
    for page in items:
        page_token = page.get("access_token") or user_token
        account_ids.append(
            db.upsert_account(
                "facebook",
                page["id"],
                page.get("name") or "Facebook 페이지",
                avatar=((page.get("picture") or {}).get("data") or {}).get("url"),
                access_token=page_token,
                expires_at=expires_at,
                meta={"kind": "page"},
            )
        )
        ig = page.get("instagram_business_account")
        if ig:
            account_ids.append(
                db.upsert_account(
                    "instagram",
                    ig["id"],
                    ig.get("username") or "Instagram 계정",
                    avatar=ig.get("profile_picture_url"),
                    access_token=page_token,
                    expires_at=expires_at,
                    meta={"page_id": page["id"], "page_name": page.get("name")},
                )
            )
    return account_ids
