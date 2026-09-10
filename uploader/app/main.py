"""멀티 플랫폼 업로더 — YouTube / TikTok / Instagram / Facebook 동시 업로드."""
import asyncio
import contextlib
import ipaddress
import mimetypes
import os
import re
import secrets
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from urllib.parse import urlparse

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, credentials, db, jobs
from .config import (
    APP_PASSWORD,
    BASE_DIR,
    MAX_UPLOAD_BYTES,
    PLATFORM_ORDER,
    PLATFORMS,
    PUBLIC_BASE_URL,
    SESSION_DAYS,
    UPLOAD_DIR,
    demo_platform,
)
from .platforms import instagram_login, meta, tiktok, youtube
from .platforms.base import PublishError

STATIC_DIR = BASE_DIR / "static"

async def _periodic_cleanup() -> None:
    """24시간 켜 두는 경우를 위해 주기적으로 오래된 업로드 파일을 정리한다."""
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            jobs.cleanup_old_files()
        except Exception:  # 정리 실패가 서버를 멈추게 하지는 않는다
            pass


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db.connect()
    interrupted = db.fail_interrupted_jobs()
    if interrupted:
        print(f"[startup] 재시작으로 중단된 게시 {interrupted}건을 실패 처리했습니다.")
    jobs.cleanup_old_files()
    cleaner = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        cleaner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleaner


app = FastAPI(title="멀티 플랫폼 업로더", version="1.0.0", lifespan=lifespan)

# provider → (auth_url, exchange_code)
PROVIDERS = {
    "youtube": (youtube.auth_url, youtube.exchange_code),
    "tiktok": (tiktok.auth_url, tiktok.exchange_code),
    "meta": (meta.auth_url, meta.exchange_code),
    "instagram_login": (instagram_login.auth_url, instagram_login.exchange_code),
}


def provider_for(platform: str) -> str:
    """인스타는 두 가지 연결 방식 중 설정된 쪽을 쓴다."""
    if platform == "instagram" and instagram_login.enabled():
        return "instagram_login"
    return PLATFORMS[platform].provider


def redirect_uri_for(platform: str) -> str:
    return f"{PUBLIC_BASE_URL}/api/oauth/{provider_for(platform)}/callback"


# ── 로그인 ───────────────────────────────────────────────────
# 인증 없이 열어두는 경로: 로그인/최초설정 API, 상태 확인, 그리고 Instagram이
# 영상을 가져가는 /media/{token}(192비트 임의 토큰으로 보호).
OAUTH_STATE_COOKIE = "uploader_oauth_state"
# 플랫폼이 사이트 소유권을 확인할 때 요구하는 파일 (예: tiktokXXXX.txt) 이름 형식
VERIFY_FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.(txt|html)$")


def verification_content(path: str) -> str | None:
    """루트에 올려둔 소유권 확인 파일 내용(있으면)."""
    name = path.lstrip("/")
    if "/" in name or not VERIFY_FILE_RE.match(name):
        return None
    return db.get_setting(f"verify:{name}")
PUBLIC_PREFIXES = ("/media/", "/static/")
PUBLIC_PATHS = {
    "/api/login", "/api/setup-state", "/healthz", "/favicon.ico",
    # 플랫폼 콘솔이 요구하는 문서·콜백 — 로그인 없이 열려야 한다
    "/privacy", "/terms",
    "/api/meta/deauthorize", "/api/meta/data-deletion",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


# 프록시를 거쳐 들어온 요청임을 알려주는 헤더들.
# Render·Fly·nginx 같은 리버스 프록시 뒤에서는 소켓 IP가 내부 사설 IP로 보이므로
# IP만 보면 외부 접속을 '로컬'로 잘못 판단한다. 이 헤더가 있으면 외부로 간주한다.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "fly-client-ip")


def client_is_local(request: Request) -> bool:
    """서버가 도는 그 컴퓨터(또는 같은 사설망)에서 직접 온 요청인지."""
    if any(request.headers.get(header) for header in PROXY_HEADERS):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _no_store(page: str, status_code: int = 200) -> FileResponse:
    return FileResponse(
        STATIC_DIR / page, status_code=status_code, headers={"Cache-Control": "no-store"}
    )


@app.middleware("http")
async def guard(request: Request, call_next):
    path, method = request.url.path, request.method
    blocked: Response | None = None

    # 1) 다른 사이트에서 넘어온 상태 변경 요청 차단 (CSRF)
    origin = request.headers.get("origin")
    if method not in ("GET", "HEAD", "OPTIONS") and origin:
        if urlparse(origin).netloc != request.headers.get("host"):
            blocked = JSONResponse({"detail": "허용되지 않은 요청입니다."}, status_code=403)

    # 2) 로그인 확인 — 비밀번호가 없으면 최초 설정 화면부터
    # 플랫폼 소유권 확인 파일은 로그인 없이 그대로 내려줘야 한다
    if blocked is None and method == "GET":
        content = verification_content(path)
        if content is not None:
            return PlainTextResponse(content, headers=SECURITY_HEADERS)

    if blocked is None and not (path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)):
        if auth.needs_setup():
            setup_call = path == "/api/password" and method == "POST"
            if not setup_call:
                blocked = (
                    JSONResponse(
                        {"detail": "최초 비밀번호 설정이 필요합니다.", "needs_setup": True},
                        status_code=401,
                    )
                    if path.startswith("/api/")
                    else _no_store("setup.html")
                )
        elif not auth.valid_token(request.cookies.get(auth.COOKIE)):
            blocked = (
                JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
                if path.startswith("/api/")
                else _no_store("login.html")
            )

    response = blocked if blocked is not None else await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


@app.get("/api/setup-state")
async def setup_state(request: Request) -> dict:
    """로그인 전에도 볼 수 있는 최소 정보(설정 필요 여부)."""
    return {"needs_setup": auth.needs_setup(), "can_setup_here": client_is_local(request)}


class LoginIn(BaseModel):
    password: str = ""


def _with_session(payload: dict) -> JSONResponse:
    response = JSONResponse(payload)
    response.set_cookie(
        auth.COOKIE,
        auth.issue_token(),
        httponly=True,
        samesite="lax",
        secure=PUBLIC_BASE_URL.startswith("https://"),
        max_age=SESSION_DAYS * 86400,
        path="/",
    )
    return response


@app.post("/api/login")
async def login(payload: LoginIn, request: Request) -> JSONResponse:
    if not auth.password_configured():
        return JSONResponse({"ok": True, "auth": False})
    ip = request.client.host if request.client else "unknown"
    if auth.throttled(ip):
        raise HTTPException(429, "로그인 시도가 너무 많습니다. 5분 뒤에 다시 시도하세요.")
    if not auth.check_password(payload.password):
        auth.record_failure(ip)
        await asyncio.sleep(1)  # 무차별 대입 지연
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    auth.clear_failures(ip)
    return _with_session({"ok": True})


class PasswordIn(BaseModel):
    new_password: str = ""
    current_password: str = ""


@app.get("/api/security")
async def security_state() -> dict:
    return {
        "locked": auth.password_configured(),
        "source": auth.password_source(),
        "changeable_in_app": False,  # 앱에서는 변경 불가 — 서버 환경변수로만
    }


@app.post("/api/password")
async def set_password(payload: PasswordIn, request: Request) -> JSONResponse:
    """최초 1회, 서버가 도는 컴퓨터에서만 비밀번호를 정한다.

    한 번 정해진 뒤에는 앱 안에서 바꿀 수 없다(세션을 탈취당해도 잠금을
    빼앗기지 않도록). 변경은 서버 환경변수 APP_PASSWORD 로만 한다.
    """
    if not auth.needs_setup():
        raise HTTPException(
            403,
            "비밀번호는 앱에서 바꿀 수 없습니다. 서버의 환경변수 APP_PASSWORD 를 "
            "바꾸고 재시작하세요(환경변수 값이 우선 적용됩니다).",
        )
    if not client_is_local(request):
        raise HTTPException(
            403,
            "최초 비밀번호는 서버가 설치된 컴퓨터에서만 정할 수 있습니다. "
            "원격 서버라면 환경변수 APP_PASSWORD 를 설정한 뒤 재시작하세요.",
        )
    new_password = payload.new_password.strip()
    if len(new_password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상으로 정하세요.")
    auth.set_password(new_password)
    return _with_session({"ok": True})


@app.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


POLICY_PAGE = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{max-width:720px;margin:0 auto;padding:40px 20px;font-family:'Pretendard','Apple SD Gothic Neo',
-apple-system,sans-serif;line-height:1.8;color:#1a1a1a}}h1{{font-size:24px;margin-bottom:8px}}
h2{{font-size:16px;margin:28px 0 8px}}p,li{{font-size:14.5px;color:#333}}
.meta{{color:#777;font-size:13px;margin-bottom:28px}}a{{color:#1877f2}}</style></head><body>{body}</body></html>"""

PRIVACY_BODY = """
<h1>개인정보처리방침</h1>
<p class="meta">최종 수정: 2026년 · 서비스 주소: {base}</p>
<p>이 서비스(이하 "앱")는 운영자 본인이 자신의 소셜 미디어 계정에 영상을 게시하기 위해
직접 설치·운영하는 도구입니다. 앱은 운영자의 서버에서만 동작하며, 제3자에게 데이터를 판매하거나
광고 목적으로 제공하지 않습니다.</p>
<h2>수집하는 정보</h2>
<ul>
<li>연결한 플랫폼 계정의 식별자, 표시 이름, 프로필 사진 주소</li>
<li>게시 권한을 위한 액세스 토큰 (암호화하여 저장)</li>
<li>업로드한 영상 파일과 제목·설명·해시태그 등 게시 내용</li>
<li>게시 결과 기록(성공/실패, 게시물 링크)</li>
</ul>
<h2>이용 목적</h2>
<p>수집한 정보는 오직 <b>운영자가 지정한 계정에 영상을 게시</b>하고 그 결과를 보여주기 위해서만 사용합니다.</p>
<h2>보관과 삭제</h2>
<ul>
<li>업로드한 영상 원본은 7일이 지나면 자동 삭제됩니다.</li>
<li>계정 연결 정보는 앱의 <b>계정 관리</b>에서 연결을 끊으면 즉시 삭제됩니다.</li>
<li>각 플랫폼에서도 앱 권한을 직접 해제할 수 있습니다.</li>
</ul>
<h2>제3자 제공</h2>
<p>정보를 제3자에게 제공하지 않습니다. 다만 게시를 위해 사용자가 선택한 플랫폼
(YouTube, TikTok, Instagram, Facebook)의 공식 API로 영상과 게시 내용이 전송됩니다.</p>
<h2>문의</h2>
<p>이 앱의 운영자에게 문의하세요.</p>
<p><a href="/terms">서비스 이용약관 보기</a></p>
"""

TERMS_BODY = """
<h1>서비스 이용약관</h1>
<p class="meta">최종 수정: 2026년 · 서비스 주소: {base}</p>
<h2>1. 서비스 소개</h2>
<p>이 앱은 하나의 영상을 여러 소셜 미디어 계정에 동시에 게시하도록 돕는 개인용 도구입니다.</p>
<h2>2. 이용 조건</h2>
<ul>
<li>이용자는 자신이 권한을 가진 계정만 연결해야 합니다.</li>
<li>이용자는 게시하는 콘텐츠에 대한 권리를 보유해야 하며, 각 플랫폼의 정책을 따라야 합니다.</li>
<li>불법 콘텐츠, 타인의 저작물 무단 게시, 스팸 목적의 사용을 금지합니다.</li>
</ul>
<h2>3. 책임의 한계</h2>
<p>이 앱은 각 플랫폼의 공식 API를 통해 게시를 중계할 뿐이며, 플랫폼의 정책 변경·점검·거부로
게시가 실패할 수 있습니다. 게시 결과와 그로 인한 영향에 대한 책임은 이용자에게 있습니다.</p>
<h2>4. 서비스 변경·중단</h2>
<p>운영자는 사전 통지 없이 서비스를 변경하거나 중단할 수 있습니다.</p>
<p><a href="/privacy">개인정보처리방침 보기</a></p>
"""


@app.get("/privacy")
async def privacy_page() -> HTMLResponse:
    return HTMLResponse(
        POLICY_PAGE.format(title="개인정보처리방침", body=PRIVACY_BODY.format(base=PUBLIC_BASE_URL))
    )


@app.get("/terms")
async def terms_page() -> HTMLResponse:
    return HTMLResponse(
        POLICY_PAGE.format(title="서비스 이용약관", body=TERMS_BODY.format(base=PUBLIC_BASE_URL))
    )


@app.post("/api/meta/deauthorize")
async def meta_deauthorize() -> dict:
    """메타가 '앱 연결 해제' 를 알릴 때 호출하는 주소.

    콘솔 설정에 URL 이 필요해서 열어둔다. 실제 토큰 정리는 사용자가
    계정 관리에서 연결을 끊을 때 이뤄진다.
    """
    return {"ok": True}


@app.post("/api/meta/data-deletion")
async def meta_data_deletion() -> dict:
    """메타의 데이터 삭제 요청 콜백. 처리 상태를 확인할 수 있는 주소를 돌려준다."""
    return {"url": f"{PUBLIC_BASE_URL}/privacy", "confirmation_code": secrets.token_hex(8)}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# ── 정적 파일 ────────────────────────────────────────────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


# 플랫폼별로 서버에 넣어야 하는 환경변수 이름 (설정이 안 됐을 때 화면에 안내)
ENV_KEYS = {
    "youtube": ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"],
    "tiktok": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"],
    "instagram": ["META_APP_ID", "META_APP_SECRET"],
    "facebook": ["META_APP_ID", "META_APP_SECRET"],
}


# ── 플랫폼 / 계정 ────────────────────────────────────────────
@app.get("/api/platforms")
async def get_platforms() -> dict:
    accounts = db.list_accounts()
    out = []
    for key in PLATFORM_ORDER:
        cfg = PLATFORMS[key]
        out.append({
            "key": key,
            "label": cfg.label,
            "color": cfg.color,
            "provider": cfg.provider,
            "configured": (
                instagram_login.configured() if key == "instagram" and instagram_login.enabled()
                else cfg.configured
            ),
            "auth_mode": "instagram" if key == "instagram" and instagram_login.enabled() else "facebook",
            "env_keys": ENV_KEYS[key],
            # 값은 절대 내보내지 않고, 서버가 그 이름의 값을 실제로 받았는지만 알려준다
            "env_status": {name: bool(os.getenv(name, "").strip()) for name in ENV_KEYS[key]},
            "key_source": "app" if credentials.stored(key) else ("env" if cfg.configured else None),
            "scopes": (
                tiktok.scopes() if key == "tiktok"
                else meta.scopes() if key in ("instagram", "facebook")
                else None
            ),
            "demo": demo_platform(key),
            "redirect_uri": redirect_uri_for(key),
            "accounts": [a for a in accounts if a["platform"] == key],
        })
    return {
        "platforms": out,
        "public_base_url": PUBLIC_BASE_URL,
        "auth_enabled": auth.password_configured(),
    }


@app.get("/api/oauth/{platform}/start")
async def oauth_start(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(404, "알 수 없는 플랫폼")
    cfg = PLATFORMS[platform]

    if platform == "instagram" and instagram_login.enabled():
        if not instagram_login.configured():
            raise HTTPException(400, "Instagram 앱 ID와 시크릿을 먼저 입력하세요.")
    elif demo_platform(platform):
        _create_demo_accounts(platform)
        return RedirectResponse(f"/?connected={platform}&demo=1", status_code=303)

    provider = provider_for(platform)
    state = secrets.token_urlsafe(24)
    db.save_state(state, provider)  # 예비 확인용
    response = RedirectResponse(PROVIDERS[provider][0](state), status_code=303)
    # 서명 쿠키에도 담아둔다 — 서버가 재시작되거나 데이터가 초기화돼도 연결이 이어지도록.
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        auth.sign_state(provider, state),
        httponly=True,
        samesite="lax",
        secure=PUBLIC_BASE_URL.startswith("https://"),
        max_age=1800,
        path="/",
    )
    return response


@app.get("/api/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    if provider not in PROVIDERS:
        raise HTTPException(404, "알 수 없는 인증 공급자")
    params = request.query_params
    if params.get("error"):
        detail = params.get("error_description") or params.get("error")
        return RedirectResponse(f"/?error={detail}", status_code=303)

    state = params.get("state") or ""
    cookie_ok = auth.verify_state(request.cookies.get(OAUTH_STATE_COOKIE), provider, state)
    if not cookie_ok and db.pop_state(state) != provider:
        return RedirectResponse(
            "/?error=" + (
                "로그인 연결 정보가 만료되었거나 이미 사용되었습니다. "
                "계정 관리에서 '로그인으로 연결'을 다시 눌러주세요. "
                "(이 페이지를 새로고침하면 같은 오류가 납니다)"
            ),
            status_code=303,
        )
    db.pop_state(state)

    code = params.get("code")
    if not code:
        return RedirectResponse("/?error=인가 코드가 없습니다.", status_code=303)

    try:
        await PROVIDERS[provider][1](code)
    except PublishError as exc:
        return RedirectResponse(f"/?error={exc}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/?error={type(exc).__name__}: {exc}", status_code=303)
    done = RedirectResponse(f"/?connected={provider}", status_code=303)
    done.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return done


class ManualAccount(BaseModel):
    platform: str
    handle: str
    label: str = ""


@app.post("/api/accounts/manual")
async def add_manual_account(payload: ManualAccount) -> dict:
    """로그인 없이 내 아이디만 먼저 목록에 등록한다.

    실제 게시는 이후 해당 플랫폼 로그인 연결을 마쳐야 가능하다
    (자격증명이 없는 데모 모드에서는 시뮬레이션으로 동작).
    """
    if payload.platform not in PLATFORMS:
        raise HTTPException(400, "알 수 없는 플랫폼입니다.")
    handle = payload.handle.strip().lstrip("@").strip()[:80]
    if not handle:
        raise HTTPException(400, "아이디를 입력하세요.")
    for account in db.list_accounts():
        if account["platform"] == payload.platform and account["external_id"] == handle:
            raise HTTPException(400, f"이미 등록된 아이디입니다: {handle}")
    account_id = db.upsert_account(
        payload.platform, handle, handle, meta={"manual": True}
    )
    if payload.label.strip():
        db.set_account_label(account_id, payload.label.strip()[:60])
    return {"id": account_id, "handle": handle}


class AccountPatch(BaseModel):
    label: str = ""


class PageById(BaseModel):
    page_id: str


@app.post("/api/accounts/facebook-page")
async def add_facebook_page(payload: PageById) -> dict:
    """페이지 ID를 직접 입력해 연결(자동 조회가 비어 올 때)."""
    if not payload.page_id.strip().isdigit():
        raise HTTPException(400, "페이지 ID는 숫자만 입력하세요.")
    account_id = await meta.add_page_by_id(payload.page_id)
    return {"ok": True, "id": account_id}


@app.patch("/api/accounts/{account_id}")
async def rename_account(account_id: str, payload: AccountPatch) -> dict:
    if not db.get_account(account_id, with_tokens=False):
        raise HTTPException(404, "계정을 찾을 수 없습니다.")
    db.set_account_label(account_id, payload.label.strip()[:60])
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def disconnect(account_id: str) -> dict:
    if not db.get_account(account_id, with_tokens=False):
        raise HTTPException(404, "계정을 찾을 수 없습니다.")
    db.delete_account(account_id)
    return {"ok": True}


def _create_demo_accounts(platform: str) -> None:
    """API 키 없이도 화면을 테스트할 수 있도록 가짜 계정을 만든다.

    누를 때마다 새 계정이 하나씩 추가되므로 다중 계정 운영도 미리 확인할 수 있다.
    """
    demo_names = {
        "youtube": ("데모 채널", "UCdemo"),
        "tiktok": ("demo_creator", "tt_demo"),
        "instagram": ("demo.official", "ig_demo"),
        "facebook": ("데모 페이지", "fb_demo"),
    }
    name, external = demo_names[platform]
    n = db.count_accounts(platform) + 1
    db.upsert_account(
        platform, f"{external}_{n}", f"{name} {n}", access_token="demo", meta={"demo": True}
    )


# ── 플랫폼 API 키 (화면에서 직접 입력) ───────────────────────
class PlatformKeys(BaseModel):
    platform: str
    client_id: str = ""
    client_secret: str = ""
    scopes: str = ""      # 틱톡·메타 — 승인된 권한만 요청하고 싶을 때
    auth_mode: str = ""   # 인스타 — "facebook"(기본) 또는 "instagram"(직접 로그인)


@app.post("/api/platform-keys")
async def save_platform_keys(payload: PlatformKeys) -> dict:
    """개발자 콘솔에서 받은 키를 앱에서 입력받아 암호화 저장한다."""
    if payload.platform not in PLATFORMS:
        raise HTTPException(400, "알 수 없는 플랫폼입니다.")
    client_id, client_secret = payload.client_id.strip(), payload.client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "두 값을 모두 입력하세요.")
    # 인스타 직접 로그인 방식은 별도의 Instagram 앱 자격증명을 쓴다
    if payload.platform == "instagram" and payload.auth_mode == "instagram":
        credentials.save(instagram_login.CRED_KEY, client_id, client_secret)
        db.set_setting(instagram_login.MODE_KEY, "instagram")
        return {"ok": True}
    if payload.platform == "instagram":
        db.set_setting(instagram_login.MODE_KEY, "facebook")

    credentials.save(payload.platform, client_id, client_secret)
    if payload.platform == "tiktok":
        db.set_setting("tiktok_scopes", payload.scopes.strip())
    if payload.platform in ("instagram", "facebook"):
        db.set_setting("meta_scopes", payload.scopes.strip())
    # Meta 는 인스타그램과 페이스북이 같은 앱을 쓴다.
    twin = {"instagram": "facebook", "facebook": "instagram"}.get(payload.platform)
    if twin:
        credentials.save(twin, client_id, client_secret)
    return {"ok": True}


@app.delete("/api/platform-keys/{platform}")
async def delete_platform_keys(platform: str) -> dict:
    if platform not in PLATFORMS:
        raise HTTPException(400, "알 수 없는 플랫폼입니다.")
    credentials.clear(platform)
    twin = {"instagram": "facebook", "facebook": "instagram"}.get(platform)
    if twin:
        credentials.clear(twin)
    return {"ok": True}


# ── 사이트 소유권 확인 파일 ──────────────────────────────────
class VerifyFile(BaseModel):
    filename: str
    content: str


@app.get("/api/verify-files")
async def list_verify_files() -> dict:
    names = [
        row["key"].removeprefix("verify:")
        for row in db._rows("SELECT key FROM settings WHERE key LIKE 'verify:%'")
    ]
    return {"files": [{"filename": n, "url": f"{PUBLIC_BASE_URL}/{n}"} for n in names]}


@app.post("/api/verify-files")
async def add_verify_file(payload: VerifyFile) -> dict:
    filename = payload.filename.strip().lstrip("/")
    if not VERIFY_FILE_RE.match(filename):
        raise HTTPException(400, "파일 이름은 영문·숫자와 .txt 또는 .html 형식이어야 합니다.")
    if len(payload.content) > 4096:
        raise HTTPException(400, "내용이 너무 깁니다.")
    db.set_setting(f"verify:{filename}", payload.content.strip())
    return {"ok": True, "url": f"{PUBLIC_BASE_URL}/{filename}"}


@app.delete("/api/verify-files/{filename}")
async def delete_verify_file(filename: str) -> dict:
    db.delete_setting(f"verify:{filename.lstrip('/')}")
    return {"ok": True}


# ── 채널 세트 ────────────────────────────────────────────────
class SetIn(BaseModel):
    name: str = ""
    account_ids: list[str] = Field(default_factory=list)


def _clean_set(payload: SetIn) -> tuple[str, list[str]]:
    name = payload.name.strip()[:40]
    if not name:
        raise HTTPException(400, "세트 이름을 입력하세요.")
    ids, seen = [], set()
    for account_id in payload.account_ids:
        if account_id in seen:
            continue
        if not db.get_account(account_id, with_tokens=False):
            raise HTTPException(400, "세트에 담을 수 없는 계정이 있습니다. 목록을 새로고침하세요.")
        seen.add(account_id)
        ids.append(account_id)
    if not ids:
        raise HTTPException(400, "세트에 넣을 계정을 1개 이상 선택하세요.")
    return name, ids


@app.get("/api/sets")
async def get_sets() -> dict:
    return {"sets": db.list_sets()}


@app.post("/api/sets")
async def create_set(payload: SetIn) -> dict:
    name, ids = _clean_set(payload)
    return {"id": db.create_set(name, ids)}


@app.patch("/api/sets/{set_id}")
async def update_set(set_id: str, payload: SetIn) -> dict:
    if not db.get_set(set_id):
        raise HTTPException(404, "세트를 찾을 수 없습니다.")
    name, ids = _clean_set(payload)
    db.update_set(set_id, name=name, account_ids=ids)
    return {"ok": True}


@app.delete("/api/sets/{set_id}")
async def remove_set(set_id: str) -> dict:
    if not db.get_set(set_id):
        raise HTTPException(404, "세트를 찾을 수 없습니다.")
    db.delete_set(set_id)
    return {"ok": True}


# ── 미디어 업로드 ────────────────────────────────────────────
@app.post("/api/media")
async def upload_media(file: UploadFile) -> dict:
    if not file.filename:
        raise HTTPException(400, "파일명이 없습니다.")
    content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "video/mp4"
    if not content_type.startswith("video/"):
        raise HTTPException(400, "영상 파일만 업로드할 수 있습니다.")

    suffix = Path(file.filename).suffix[:10] or ".mp4"
    dest = Path(UPLOAD_DIR) / f"{secrets.token_hex(12)}{suffix}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "파일이 너무 큽니다.")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    media = db.create_media(
        path=str(dest), name=file.filename, size=size, content_type=content_type
    )
    return {
        "id": media["id"],
        "name": media["name"],
        "size": media["size"],
        "content_type": media["content_type"],
        "preview_url": f"/media/{media['token']}",
    }


@app.post("/api/media/{media_id}/thumbnail")
async def upload_thumbnail(media_id: str, file: UploadFile) -> dict:
    media = db.get_media(media_id)
    if not media:
        raise HTTPException(404, "미디어를 찾을 수 없습니다.")
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(400, "이미지 파일만 업로드할 수 있습니다.")
    suffix = Path(file.filename or "thumb.jpg").suffix[:10] or ".jpg"
    dest = Path(UPLOAD_DIR) / f"{media_id}_thumb{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    db.set_media_thumb(media_id, str(dest))
    return {"ok": True}


RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@app.get("/media/{token}")
async def serve_media(token: str, request: Request):
    """업로드된 영상을 공개 제공한다(Instagram이 이 URL로 영상을 가져간다)."""
    media = db.get_media_by_token(token)
    if not media:
        job = db.find_job_by_media_token(token)
        media = (
            {"path": job["video_path"], "content_type": "video/mp4", "size": job["video_size"]}
            if job else None
        )
    if not media or not os.path.exists(media["path"]):
        raise HTTPException(404, "미디어를 찾을 수 없습니다.")

    path = Path(media["path"])
    total = path.stat().st_size
    content_type = media.get("content_type") or "video/mp4"
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path,
            media_type=content_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
        )

    match = RANGE_RE.fullmatch(range_header.strip())
    if not match:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    start = int(match.group(1)) if match.group(1) else 0
    end = int(match.group(2)) if match.group(2) else total - 1
    end = min(end, total - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})

    def iter_range():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = fh.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )


# ── 업로드 작업 ──────────────────────────────────────────────
class TargetIn(BaseModel):
    platform: str
    account_id: str


class JobIn(BaseModel):
    media_id: str
    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    targets: list[TargetIn]
    options: dict = Field(default_factory=dict)


@app.post("/api/jobs")
async def create_job(payload: JobIn, background: BackgroundTasks) -> dict:
    media = db.get_media(payload.media_id)
    if not media or not os.path.exists(media["path"]):
        raise HTTPException(404, "업로드된 영상을 찾을 수 없습니다. 영상을 다시 올려주세요.")
    if not payload.targets:
        raise HTTPException(400, "게시할 플랫폼을 1개 이상 선택하세요.")

    for target in payload.targets:
        if target.platform not in PLATFORMS:
            raise HTTPException(400, f"알 수 없는 플랫폼: {target.platform}")
        account = db.get_account(target.account_id, with_tokens=False)
        if not account or account["platform"] != target.platform:
            raise HTTPException(400, f"{PLATFORMS[target.platform].label} 계정이 연결되어 있지 않습니다.")

    job_id = db.create_job(
        title=payload.title.strip(),
        description=payload.description.strip(),
        hashtags=[t.strip().lstrip("#") for t in payload.hashtags if t.strip()],
        video_path=media["path"],
        video_name=media["name"],
        video_size=media["size"],
        thumb_path=media.get("thumb_path"),
        media_token=media["token"],
        options=payload.options,
    )
    for target in payload.targets:
        db.add_target(job_id, target.platform, target.account_id)

    background.add_task(_launch, job_id)
    return {"job_id": job_id}


async def _launch(job_id: str) -> None:
    # 백그라운드에서 게시를 진행하고, 응답은 즉시 반환한다.
    await asyncio.shield(jobs.run_job(job_id))


@app.get("/api/jobs")
async def get_jobs(limit: int = 30) -> dict:
    return {"jobs": db.list_jobs(limit)}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    job.pop("video_path", None)
    return job


@app.exception_handler(PublishError)
async def publish_error_handler(_: Request, exc: PublishError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run() -> None:
    import uvicorn

    from .config import PORT

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)


if __name__ == "__main__":
    run()
