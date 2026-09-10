"""Meta(Facebook/Instagram) 공용 OAuth — 페이지와 연결된 IG 비즈니스 계정을 함께 등록."""
import time
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import GRAPH, META_API_VERSION, PLATFORMS
from .base import PublishError

# publish_video 와 business_management 는 이제 이 흐름에 필요 없거나 앱에서
# 유효하지 않은 경우가 많아 기본값에서 제외한다. 필요하면 화면에서 조절.
DEFAULT_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
]


def scopes() -> str:
    """앱에서 지정한 권한 목록(없으면 기본값).

    이용 사례에 포함되지 않은 권한을 요청하면 메타가 'Invalid Scopes' 로 거부한다.
    """
    from .. import db

    return (db.get_setting("meta_scopes") or "").strip() or ",".join(DEFAULT_SCOPES)


def auth_url(state: str) -> str:
    cfg = PLATFORMS["facebook"]
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": scopes(),
        "state": state,
        # 이미 한 번 동의했더라도 페이지 선택 화면을 다시 띄운다
        # (처음에 페이지를 고르지 않으면 목록이 비어 온다)
        "auth_type": "rerequest",
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
            "가져올 수 있는 Facebook 페이지가 없습니다. 로그인 화면의 "
            "'이 앱이 액세스할 수 있는 페이지 선택' 단계에서 페이지를 체크했는지 확인하세요. "
            "이미 동의한 상태라면 페이스북 설정 > 비즈니스 통합에서 이 앱을 삭제한 뒤 "
            "다시 '로그인으로 연결'을 눌러주세요."
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
