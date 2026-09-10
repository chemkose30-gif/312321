"""Meta(Facebook/Instagram) 공용 OAuth — 페이지와 연결된 IG 비즈니스 계정을 함께 등록."""
import time
from urllib.parse import urlencode

import httpx

from .. import db
from ..config import GRAPH, META_API_VERSION, PLATFORMS, decrypt, encrypt
from .base import PublishError

# publish_video 와 business_management 는 이제 이 흐름에 필요 없거나 앱에서
# 유효하지 않은 경우가 많아 기본값에서 제외한다. 필요하면 화면에서 조절.
DEFAULT_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
    # 비즈니스 포트폴리오가 소유한 페이지를 찾으려면 필요하다
    "business_management",
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


PAGE_FIELDS = "id,name,access_token,picture{url},instagram_business_account{id,username,profile_picture_url}"


async def _owned_pages(user_token: str) -> list[dict]:
    """비즈니스 포트폴리오가 소유한 페이지 목록(개인 역할로는 안 잡히는 경우)."""
    pages: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        businesses = await client.get(
            f"{GRAPH}/me/businesses", params={"fields": "id,name", "access_token": user_token}
        )
        if businesses.status_code >= 400:
            return pages
        for business in businesses.json().get("data") or []:
            owned = await client.get(
                f"{GRAPH}/{business['id']}/owned_pages",
                params={"fields": PAGE_FIELDS, "access_token": user_token},
            )
            if owned.status_code >= 400:
                continue
            for page in owned.json().get("data") or []:
                if not page.get("access_token"):
                    # 포트폴리오 조회에는 페이지 토큰이 빠져 있어 따로 가져온다
                    detail = await client.get(
                        f"{GRAPH}/{page['id']}",
                        params={"fields": "access_token", "access_token": user_token},
                    )
                    if detail.status_code < 400:
                        page["access_token"] = (detail.json() or {}).get("access_token")
                if page.get("access_token"):
                    pages.append(page)
    return pages


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

        # 진단용: 어떤 계정으로 로그인했고 어떤 권한이 실제로 허용됐는지
        me = await client.get(f"{GRAPH}/me", params={"fields": "name,id", "access_token": user_token})
        who = me.json() if me.status_code < 400 else {}
        perms = await client.get(f"{GRAPH}/me/permissions", params={"access_token": user_token})
        perm_rows = (perms.json().get("data") or []) if perms.status_code < 400 else []
        granted = [r["permission"] for r in perm_rows if r.get("status") == "granted"]
        declined = [r["permission"] for r in perm_rows if r.get("status") != "granted"]

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

    # 자동 조회가 실패해도 페이지 ID로 직접 붙일 수 있도록 사용자 토큰을 보관한다.
    db.set_setting("meta_user_token", encrypt(user_token))
    db.set_setting("meta_user_token_expires", str(expires_at))

    # 비즈니스 포트폴리오가 소유한 페이지는 /me/accounts 에 안 나오는 경우가 있다.
    if not items:
        items = await _owned_pages(user_token)

    if not items:
        raise PublishError(
            "가져올 수 있는 Facebook 페이지가 없습니다. "
            f"[로그인한 계정: {who.get('name') or '알 수 없음'}] "
            f"[허용된 권한: {', '.join(granted) or '없음'}] "
            f"[거부된 권한: {', '.join(declined) or '없음'}] — "
            "허용된 권한에 pages_show_list 가 없으면 로그인 화면에서 페이지 접근을 허용하지 않은 것이고, "
            "권한은 있는데 목록이 비어 있으면 그 계정이 페이지 관리자가 아니거나 "
            "'이 앱이 액세스할 수 있는 페이지 선택'에서 페이지를 고르지 않은 것입니다."
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


async def add_page_by_id(page_id: str) -> str:
    """페이지 ID를 직접 받아 계정으로 등록한다.

    비즈니스 포트폴리오 소유 등으로 목록 조회가 비어 올 때의 우회로.
    직전 로그인에서 받은 사용자 토큰을 사용한다.
    """
    user_token = decrypt(db.get_setting("meta_user_token") or "")
    if not user_token:
        raise PublishError(
            "먼저 Facebook '로그인으로 연결'을 한 번 실행해 주세요. "
            "(그때 받은 권한으로 페이지를 붙입니다)"
        )
    page_id = page_id.strip()
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GRAPH}/{page_id}",
            params={"fields": PAGE_FIELDS, "access_token": user_token},
        )
    if res.status_code >= 400:
        raise PublishError(f"페이지 정보를 가져오지 못했습니다: {res.text[:200]}")
    page = res.json() or {}
    page_token = page.get("access_token")
    if not page_token:
        raise PublishError(
            "이 계정으로는 해당 페이지의 게시 권한을 받을 수 없습니다. "
            "페이지 설정에서 이 계정에 '전체 액세스 권한'이 있는지 확인하세요."
        )
    account_id = db.upsert_account(
        "facebook",
        page["id"],
        page.get("name") or "Facebook 페이지",
        avatar=((page.get("picture") or {}).get("data") or {}).get("url"),
        access_token=page_token,
        meta={"kind": "page", "added": "by_id"},
    )
    ig = page.get("instagram_business_account")
    if ig:
        db.upsert_account(
            "instagram",
            ig["id"],
            ig.get("username") or "Instagram 계정",
            avatar=ig.get("profile_picture_url"),
            access_token=page_token,
            meta={"page_id": page["id"], "page_name": page.get("name")},
        )
    return account_id
