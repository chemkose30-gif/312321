"""
보험증권 보장분석 - 설계사용 웹 서비스

  설계사가 고객 증권(PDF/사진)을 올리면 Claude 가 읽어서
  고객 정보 · 가입 현황 · 보장 합산 · 체크 포인트를 정리하고 고객별로 저장한다.

실행: uvicorn app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import html
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

load_dotenv()
import analysis  # noqa: E402
import extractor  # noqa: E402
import security  # noqa: E402
import store  # noqa: E402

SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "1") == "1"
_BACKEND = "mock" if os.environ.get("ANALYZER_MOCK") == "1" else os.environ.get("EXTRACT_BACKEND", "local")
BACKEND_NOTE = {
    "local": "증권은 <b>서버 안에서만</b> 분석되며 외부로 전송되지 않습니다. OCR 자동인식이라 금액·담보가 틀릴 수 있으니 결과를 꼭 확인·수정하세요.",
    "claude": "⚠ 증권이 분석을 위해 <b>해외 서버(Claude)</b>로 전송됩니다. 소속사 규정을 확인하세요.",
    "mock": "(시험 모드: 실제 분석 없이 샘플 데이터가 들어갑니다.)",
}.get(_BACKEND, "")
REQUIRE_2FA = os.environ.get("REQUIRE_2FA", "1") == "1"
MAX_FILES = 20
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_NOTE_IMAGES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
SESSION_IDLE = 30 * 60        # 30분 동안 사용 없으면 로그아웃
SESSION_MAX = 8 * 3600        # 로그인 후 최대 8시간
PRE_AUTH_TTL = 5 * 60         # 비밀번호 확인 후 OTP 입력까지 허용 시간
MIN_PW = 10

store.init_db()
# API 문서 페이지(/docs 등)는 외부에 노출하지 않음
app = FastAPI(title="보험증권 보장분석", docs_url=None, redoc_url=None, openapi_url=None)
sessions: dict[str, dict] = {}       # sid -> {pid, created, last}
pre_auth: dict[str, dict] = {}       # 비밀번호만 통과한 상태 -> {pid, username, expires, secret(설정 중)}
flashes: dict[str, list[str]] = {}   # sid -> 다음 화면에 보여줄 메시지


@app.middleware("http")
async def guard(request: Request, call_next):
    ip = security.client_ip(request)
    if not security.ip_allowed(ip):
        return PlainTextResponse("접근이 허용되지 않은 IP입니다.", status_code=403)
    if int(request.headers.get("content-length") or 0) > MAX_TOTAL_BYTES + 5 * 1024 * 1024:
        return PlainTextResponse("요청이 너무 큽니다.", status_code=413)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        store.audit(None, ip, "blocked_cross_origin", request.url.path)
        return PlainTextResponse("잘못된 요청입니다. 페이지를 새로고침 후 다시 시도해 주세요.", status_code=403)
    resp = await call_next(request)
    for k, v in security.security_headers(SECURE_COOKIE).items():
        resp.headers.setdefault(k, v)
    return resp


def same_origin(request: Request) -> bool:
    """다른 사이트에서 몰래 보낸 요청(CSRF) 차단: Origin/Referer 가 이 사이트여야 함"""
    from urllib.parse import urlsplit
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    return bool(src) and src != "null" and urlsplit(src).netloc == host

e = html.escape


# ── 공통 ──────────────────────────────────────────────────────────────────────
STYLE = """
:root { --bg:#ffffff; --card:#ffffff; --text:#15221d; --muted:#67756f; --line:#e7ece9; --accent:#16a34a;
        --accent-strong:#15803d; --accent-weak:#e9f8ef; --danger:#dc2626; --warn-bg:#fef2f2; --warn:#b91c1c;
        --info-bg:#eafaf1; --info:#15803d; --ok:#16a34a; --shadow:0 1px 2px rgba(16,34,26,.05), 0 3px 12px rgba(16,34,26,.05); }
@media (prefers-color-scheme: dark) { :root { --bg:#0e1816; --card:#152220; --text:#e6efec; --muted:#8fa8a2; --line:#273934;
        --accent:#2fcabc; --accent-strong:#5ad7cb; --accent-weak:#16302c; --danger:#f06a66; --warn-bg:#3a1f1c; --warn:#f6a79b;
        --info-bg:#123330; --info:#5fd6c7; --ok:#4ade80; --shadow:0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.25); color-scheme:dark; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, "Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  background-image:linear-gradient(180deg, #f3faf5 0%, var(--bg) 220px); background-repeat:no-repeat; }
@media (prefers-color-scheme: dark) { body { background-image:none; } }
main { max-width:980px; margin:0 auto; padding:24px 18px 64px; }
a { color:var(--accent); }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:16px; box-shadow:var(--shadow); }
h1 { font-size:23px; margin:0 0 12px; font-weight:800; letter-spacing:-.02em; color:var(--text); text-wrap:balance; }
h2 { font-size:17px; margin:0 0 12px; font-weight:700; letter-spacing:-.01em; } h3 { font-size:15px; margin:0 0 6px; font-weight:700; }
label { display:block; margin:10px 0 4px; font-weight:600; font-size:14px; }
input[type=text], input[type=password], input[type=tel], input[type=number], input[type=email], textarea {
  width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:9px;
  background:var(--bg); color:var(--text); font-size:15px; font-family:inherit; }
textarea { font-family:ui-monospace, monospace; font-size:13px; min-height:420px; }
textarea.note { font-family:inherit; font-size:15px; min-height:110px; }
select, input[type=date] { padding:9px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--text); font-size:15px; font-family:inherit; }
.note-item { border-top:1px solid var(--line); padding:12px 0; } .note-item:first-of-type { border-top:0; }
.note-body { white-space:pre-wrap; margin:6px 0; }
.thumbs { display:flex; flex-wrap:wrap; gap:8px; margin:8px 0; }
.thumb { position:relative; display:inline-block; }
.thumb img { width:92px; height:92px; object-fit:cover; border-radius:8px; border:1px solid var(--line); display:block; }
.thumb-x { position:absolute; top:-7px; right:-7px; width:22px; height:22px; border-radius:50%; background:var(--danger); color:#fff; border:0; font-size:15px; line-height:1; cursor:pointer; padding:0; }
.thumb-box { display:flex; flex-direction:column; gap:5px; align-items:center; }
.rep-toggle { display:inline-flex; align-items:center; gap:4px; font-size:12px; color:var(--muted); cursor:pointer; font-weight:400; margin:0; }
.rep-toggle.on { color:var(--accent); font-weight:600; }
.report-photos { display:flex; flex-wrap:wrap; gap:10px; }
.report-img { max-width:260px; width:100%; border:1px solid var(--line); border-radius:8px; }
@media print { .report-img { max-width:46%; } }
table.matrix td, table.matrix th { border:1px solid var(--line); }
td.mx-g { color:#fff; font-weight:700; text-align:center; white-space:nowrap; width:70px; font-size:13px; }
td.mx-l { font-size:13.5px; } td.mx-v { font-weight:600; }
@media print { table.matrix td, table.matrix th { border:1px solid #999; } td.mx-g { -webkit-print-color-adjust:exact; print-color-adjust:exact; } }
.tag { display:inline-block; white-space:nowrap; padding:1px 8px; border-radius:99px; font-size:12px; background:var(--line); }
.overdue { color:var(--warn); font-weight:600; }
.tag.warn-tag { background:var(--warn-bg); color:var(--warn); font-weight:600; }
.tag.ok-tag { background:var(--info-bg); color:var(--ok); }
.tag.accent, .pill { display:inline-block; padding:3px 10px; border-radius:99px; font-size:12px; font-weight:600; background:var(--accent-weak); color:var(--accent-strong); }
.manage { margin-top:10px; border-top:1px dashed var(--line); padding-top:10px; } .done { text-decoration:line-through; color:var(--muted); }
button, .btn { display:inline-block; white-space:nowrap; padding:9px 15px; border:0; border-radius:9px; background:var(--accent); color:#fff;
  font-size:14px; font-weight:600; cursor:pointer; text-decoration:none; transition:filter .12s, background .12s; }
button:hover, .btn:hover { filter:brightness(1.06); }
.btn-ghost { background:transparent; color:var(--accent); border:1px solid var(--line); font-weight:600; }
.btn-ghost:hover { background:var(--accent-weak); border-color:var(--accent); filter:none; }
.btn-danger { background:var(--danger); } .btn-sm { padding:6px 11px; font-size:13px; }
input:focus, select:focus, textarea:focus { outline:2px solid var(--accent); outline-offset:0; border-color:var(--accent); }
.muted { color:var(--muted); font-size:13px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.between { justify-content:space-between; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.stat { border:1px solid var(--line); border-radius:12px; padding:14px; background:var(--card); }
.stat b { display:block; font-size:21px; font-weight:800; color:var(--accent-strong); letter-spacing:-.01em; }
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
td.nowrap, th.nowrap { white-space:nowrap; } td.small { font-size:13px; } .small { font-size:13px; }
td.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
tr.cat-row td { border-bottom:none; padding-top:12px; font-weight:600; }
tr.sub-row td { border-bottom:none; padding-top:2px; padding-bottom:2px; font-weight:400; }
tr.sub-row td.sub { padding-left:20px; }
tr.sub-row:last-of-type td, tr.cat-row + tr.cat-row td { border-bottom:1px solid var(--line); }
.check { border-radius:8px; padding:10px 12px; margin-bottom:8px; }
.check.warn { background:var(--warn-bg); color:var(--warn); } .check.info { background:var(--info-bg); color:var(--info); }
.check b { display:block; } .check span { color:var(--text); font-size:14px; }
.flash { background:var(--info-bg); color:var(--info); border-radius:8px; padding:10px 12px; margin-bottom:16px; }
.under { color:var(--warn); } .enough { color:var(--ok); }
.drop { border:2px dashed var(--line); border-radius:10px; padding:16px; }
details summary { cursor:pointer; }
@media print { .no-print { display:none !important; } body { background:#fff; color:#000; } .card { border:0; padding:0 0 12px; }
  main { max-width:none; } .check { border:1px solid #ccc; } }
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{e(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>""", status_code=status)


won = analysis.won


def ymd(s) -> str:
    d = analysis.parse_date(s)
    return d.strftime("%Y.%m.%d") if d else (e(str(s)) if s else "-")


def ts(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def customer_title(c: dict) -> str:
    age = analysis.age_on(analysis.parse_date(c.get("birth_date")), datetime.now().date())
    bits = [x for x in (f"{age}세" if age is not None else "", c.get("gender") or "") if x]
    return f"{e(c.get('name') or '이름 미확인')} <span class='muted'>{e(' · '.join(bits))}</span>"


# ── 로그인 ────────────────────────────────────────────────────────────────────
def planner_id(request: Request) -> int:
    sid = request.cookies.get("sid", "")
    sess = sessions.get(sid)
    now = time.time()
    if not sess or now - sess["last"] > SESSION_IDLE or now - sess["created"] > SESSION_MAX:
        sessions.pop(sid, None)
        raise HTTPException(status_code=303, headers={"Location": "/login?expired=1" if sess else "/login"})
    sess["last"] = now
    return sess["pid"]


def log(request: Request, pid: int | None, action: str, target="") -> None:
    store.audit(pid, security.client_ip(request), action, str(target))


def flash(request: Request, msg: str) -> None:
    flashes.setdefault(request.cookies.get("sid", ""), []).append(msg)


def take_flash(request: Request) -> str:
    msgs = flashes.pop(request.cookies.get("sid", ""), [])
    return "".join(f'<div class="flash">{e(m)}</div>' for m in msgs)


def login_page(msg: str = "", body: str = "") -> HTMLResponse:
    err = f'<p class="under">{e(msg)}</p>' if msg else ""
    body = body or """<form method="post" action="/login"><label>아이디</label><input type="text" name="username" required autofocus autocomplete="username">
<label>비밀번호</label><input type="password" name="password" required autocomplete="current-password"><p><button>로그인</button></p></form>"""
    return page("로그인", f'<div class="card" style="max-width:440px;margin:40px auto"><h1>보험증권 보장분석</h1>{err}{body}</div>')


def start_session(pid: int) -> RedirectResponse:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    sessions[sid] = {"pid": pid, "created": now, "last": now}
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sid", sid, httponly=True, samesite="strict", secure=SECURE_COOKIE, max_age=SESSION_MAX)
    resp.delete_cookie("pre")
    return resp


def get_pre(request: Request) -> dict | None:
    st = pre_auth.get(request.cookies.get("pre", ""))
    return st if st and st["expires"] > time.time() else None


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = "", expired: str = ""):
    msg = {"1": "아이디 또는 비밀번호가 올바르지 않습니다.", "locked": "로그인 시도가 너무 많습니다. 15분 후 다시 시도해 주세요."}.get(error, "")
    if expired and not msg:
        msg = "오래 사용하지 않아 로그아웃되었습니다. 다시 로그인해 주세요."
    return login_page(msg)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip, username = security.client_ip(request), username.strip()
    keys = (f"ip:{ip}", f"user:{username}")
    if security.is_locked(*keys):
        log(request, None, "login_locked", username)
        return RedirectResponse("/login?error=locked", status_code=303)
    p = store.authenticate(username, password)
    if not p:
        security.record_fail(*keys)
        log(request, None, "login_fail", username)
        time.sleep(1)
        return RedirectResponse("/login?error=1", status_code=303)
    secret, _ = store.get_totp(p["id"])
    if not REQUIRE_2FA and not secret:
        security.clear_fails(*keys)
        log(request, p["id"], "login")
        return start_session(p["id"])
    token = secrets.token_urlsafe(32)
    pre_auth[token] = {"pid": p["id"], "username": username, "expires": time.time() + PRE_AUTH_TTL,
                       "setup": None if secret else security.new_totp_secret()}
    resp = RedirectResponse("/login/otp", status_code=303)
    resp.set_cookie("pre", token, httponly=True, samesite="strict", secure=SECURE_COOKIE, max_age=PRE_AUTH_TTL)
    return resp


@app.get("/login/otp", response_class=HTMLResponse)
def otp_form(request: Request, error: str = ""):
    st = get_pre(request)
    if not st:
        return RedirectResponse("/login", status_code=303)
    msg = "인증 코드가 올바르지 않습니다." if error else ""
    code_input = """<form method="post" action="/login/otp"><label>인증 앱의 6자리 코드</label>
<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{6,7}" required autofocus>
<p><button>확인</button></p></form>"""
    if st["setup"]:
        uri = security.totp_uri(st["setup"], st["username"])
        body = f"""<h2>2단계 인증 설정 (최초 1회)</h2>
<p>휴대폰에 <b>Google Authenticator</b> 또는 <b>Microsoft Authenticator</b> 앱을 설치하고, 아래 QR 코드를 스캔하세요.</p>
<div style="background:#fff;padding:12px;width:220px">{security.qr_svg(uri).replace('<svg ', '<svg style="width:196px;height:196px" ', 1)}</div>
<p class="muted">QR 스캔이 안 되면 직접 입력: <code>{e(st['setup'])}</code></p>{code_input}"""
    else:
        body = code_input
    return login_page(msg, body)


@app.post("/login/otp")
def otp_submit(request: Request, code: str = Form(...)):
    st = get_pre(request)
    if not st:
        return RedirectResponse("/login", status_code=303)
    ip = security.client_ip(request)
    keys = (f"ip:{ip}", f"user:{st['username']}")
    if security.is_locked(*keys):
        pre_auth.pop(request.cookies.get("pre", ""), None)
        log(request, st["pid"], "login_locked", "otp")
        return RedirectResponse("/login?error=locked", status_code=303)
    secret, last = (st["setup"], -1) if st["setup"] else store.get_totp(st["pid"])
    counter = security.verify_totp(secret, code, last)
    if counter is None:
        security.record_fail(*keys)
        log(request, st["pid"], "otp_fail")
        return RedirectResponse("/login/otp?error=1", status_code=303)
    if st["setup"]:
        store.set_totp(st["pid"], st["setup"], counter)
        log(request, st["pid"], "2fa_setup")
    else:
        store.set_totp_last(st["pid"], counter)
    pre_auth.pop(request.cookies.get("pre", ""), None)
    security.clear_fails(*keys)
    log(request, st["pid"], "login")
    return start_session(st["pid"])


@app.post("/logout")
def logout(request: Request):
    sess = sessions.pop(request.cookies.get("sid", ""), None)
    if sess:
        log(request, sess["pid"], "logout")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ── 내 계정 (비밀번호 변경, 접속 기록) ────────────────────────────────────────
ACTION_NAMES = {
    "login": "로그인", "logout": "로그아웃", "login_fail": "로그인 실패", "otp_fail": "인증코드 실패",
    "login_locked": "로그인 잠김", "2fa_setup": "2단계 인증 설정", "password_change": "비밀번호 변경",
    "view_customer": "고객 조회", "view_report": "리포트 조회", "terms_analyze": "약관 분석", "create_customer": "고객 등록",
    "edit_customer": "고객 정보 수정", "delete_customer": "고객 삭제", "upload": "증권 업로드",
    "edit_policy": "계약 수정", "manage_policy": "계약 관리정보 수정", "delete_policy": "계약 삭제",
    "add_note": "상담 기록 작성", "edit_note": "상담 기록 수정", "delete_note": "상담 기록 삭제",
    "edit_notepad": "자유 메모 수정", "delete_note_image": "상담 사진 삭제",
    "blocked_cross_origin": "외부 사이트 요청 차단",
}


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, error: str = ""):
    pid = planner_id(request)
    me = store.get_planner(pid)
    rows = "".join(f"""<tr><td class="nowrap">{ts(r['ts'])}</td><td>{e(ACTION_NAMES.get(r['action'], r['action']))}</td>
<td>{e(r['target'] or '')}</td><td class="muted">{e(r['ip'] or '')}</td></tr>""" for r in store.list_audit(pid, 100))
    err = f'<p class="under">{e(error)}</p>' if error else ""
    return page("내 계정", f"""<p><a href="/">← 고객 목록</a></p>{take_flash(request)}
<div class="card"><h1>내 계정 · {e(me['name'])} ({e(me['username'])})</h1>
<p class="muted">2단계 인증: {"사용 중" if store.get_totp(pid)[0] else "미설정"} · 자동 로그아웃: {SESSION_IDLE // 60}분 미사용 시</p></div>
<form method="post" action="/account/password" class="card"><h2>비밀번호 변경</h2>{err}
<label>현재 비밀번호</label><input type="password" name="current" required autocomplete="current-password">
<label>새 비밀번호 ({MIN_PW}자 이상)</label><input type="password" name="new" required minlength="{MIN_PW}" autocomplete="new-password">
<label>새 비밀번호 확인</label><input type="password" name="confirm" required minlength="{MIN_PW}" autocomplete="new-password">
<p><button class="btn-sm">변경</button></p></form>
<div class="card"><h2>최근 접속·작업 기록</h2><p class="muted">내 계정으로 한 로그인과 고객정보 조회·수정 기록입니다. 모르는 기록이 있으면 즉시 비밀번호를 바꾸고 관리자에게 알리세요.</p>
<div class="tbl-wrap"><table><tr><th>시각</th><th>작업</th><th>대상</th><th>IP</th></tr>{rows}</table></div></div>""")


@app.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    pid = planner_id(request)
    me = store.get_planner(pid)
    if not store.authenticate(me["username"], current):
        log(request, pid, "login_fail", "password_change")
        return RedirectResponse(f"/account?error={quote('현재 비밀번호가 올바르지 않습니다.')}", status_code=303)
    if len(new) < MIN_PW or new != confirm:
        return RedirectResponse(f"/account?error={quote(f'새 비밀번호는 {MIN_PW}자 이상이고 확인 값과 같아야 합니다.')}", status_code=303)
    store.change_password(pid, new)
    log(request, pid, "password_change")
    keep = request.cookies.get("sid", "")
    for sid in [k for k, v in sessions.items() if v["pid"] == pid and k != keep]:
        sessions.pop(sid, None)  # 다른 기기의 로그인은 모두 끊음
    flash(request, "비밀번호를 변경했습니다. 다른 기기의 로그인은 해제되었습니다.")
    return RedirectResponse("/account", status_code=303)


# ── 업로드 & 분석 ─────────────────────────────────────────────────────────────
def upload_form(customer_id: int | None = None) -> str:
    hidden = f'<input type="hidden" name="customer_id" value="{customer_id}">' if customer_id else ""
    return f"""<form method="post" action="/upload" enctype="multipart/form-data" class="drop no-print"
onsubmit="const b=this.querySelector('button');b.disabled=true;b.textContent='증권 분석 중... (1~2분)'">{hidden}
<div class="row"><input type="file" name="files" accept=".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/*" multiple required>
<button>분석하기</button></div>
<p class="muted">PDF·JPG·PNG, 한 번에 {MAX_FILES}개 / 합계 30MB까지. 한 고객의 증권만 함께 올려 주세요.
<b>약관 PDF</b>를 올리면(여러 개 가능) 자동으로 약관 분석(이율·사업비·해지공제·예상 수익률)으로 처리하고, 결과 아래에 낸 돈을 직접 적을 수 있습니다.
{"" if customer_id else "같은 이름·생년월일 고객이 있으면 그 고객에 자동으로 추가됩니다. "}{BACKEND_NOTE}</p></form>"""


def find_customer(pid: int, insured: dict) -> int | None:
    name, birth = insured.get("name"), insured.get("birth_date")
    if not (name and birth):
        return None
    for c in store.list_customers(pid):
        if c.get("name") == name and c.get("birth_date") == birth:
            return c["id"]
    return None


# ── 약관 분석 (연금·저축성 상품 구조 + 수익률 시뮬레이션) ──────────────────────
TERMS_MAX_BYTES = 30 * 1024 * 1024


def terms_form(error: str = "") -> str:
    err = f'<p class="under">{e(error)}</p>' if error else ""
    return f"""<div class="card"><h1>약관 분석</h1>{err}
<p class="muted">연금·저축보험 약관 PDF를 올리면 이율(공시·최저보증), 사업비, 해지공제, 세제를 정리하고
사업비를 반영한 예상 수익률을 계산합니다. 약관에는 개인 계약 숫자가 없으므로 결과는 <b>상품 구조와 예시</b>입니다.</p>
<form method="post" action="/terms" enctype="multipart/form-data" class="drop"
onsubmit="const b=this.querySelector('button');b.disabled=true;b.textContent='약관 분석 중... (1~2분)'">
<div class="row"><input type="file" name="files" accept=".pdf,application/pdf" multiple required>
<label style="margin:0">월 보험료</label><input type="text" name="monthly" placeholder="약관 예시값" style="width:110px">
<label style="margin:0">납입 연수</label><input type="text" name="years" placeholder="예시값" style="width:70px">
<label style="margin:0">현재 공시이율(%)</label><input type="text" name="rate" placeholder="예: 2.5" style="width:70px">
<button>분석하기</button></div>
<p class="muted">수백 페이지 약관도 이율·사업비 관련 페이지만 골라서 분석합니다. 비워 두면 약관의 예시 기준으로 계산합니다.</p>
</form></div>"""


def _num(s: str) -> float:
    try:
        return float("".join(ch for ch in s if ch.isdigit() or ch == "."))
    except ValueError:
        return 0.0


def terms_mine_html(t: dict, rate: float, paid_count: int, paid_amount: float, current: float) -> str:
    """약관 결과에 내가 적은 납입 기록으로 계산한 '지금 해지하면' 박스"""
    import pension
    import terms as tm
    if not paid_count or not paid_amount:
        return ""
    monthly = paid_amount / paid_count
    if current:
        before, basis = current, "적은 해지환급금 기준"
    else:
        year = (paid_count - 1) // 12 + 1  # 경과연도 (57회 → 5년차)
        penalty = next((p["amount_won"] for p in t["surrender_penalty"] if int(p["year"]) == year), 0)
        if t.get("example_monthly_premium"):  # 해지공제는 예시 보험료 기준이라 내 보험료 비율로 환산
            penalty *= monthly / t["example_monthly_premium"]
        before = max(pension.value_after(t["fees"], monthly, paid_count, rate) - penalty, 0)
        basis = f"추정 (약관 사업비, 공시이율 {rate:g}%, 해지공제 약 {won(round(penalty, -3))})"
    taxed = "연금저축" in (t.get("product_type", "") + t.get("product_name", ""))
    tax = before * pension.SURRENDER_TAX_PCT / 100 if taxed else 0.0
    after = before - tax
    irr = tm._irr(monthly, paid_count, before) if before else 0.0
    return f"""<div class="check warn"><b>내 계약 · 지금 해지하면 ({paid_count}회 납입)</b><span>
낸 돈 <b>{won(paid_amount)}</b> → 해지환급금 {won(round(before, -3))}{f" − 기타소득세(16.5%) {won(round(tax, -3))}" if tax else ""}
= <b>실수령 약 {won(round(after, -3))}</b> · <b>{"손실" if after < paid_amount else "이익"} 약 {won(round(abs(paid_amount - after), -3))}</b>
· 지금까지 연수익률 {irr:.2f}%<br><span class="muted">{e(basis)}</span></span></div>"""


def terms_input_form(t: dict, total_pages: int, pages: list[int], f: dict, customers: list[dict] = ()) -> str:
    """약관 결과 아래 직접 적는 칸. 분석 결과(t)를 숨김값으로 넘겨 다시 분석(=API 비용) 없이 재계산."""
    v = lambda k: e(str(f.get(k) or ""))  # noqa: E731
    return f"""<div class="card no-print"><h2>✏ 직접 적기 (내 계약 숫자)</h2>
<form method="post" action="/terms/calc" class="row">
<input type="hidden" name="t" value="{e(json.dumps(t, ensure_ascii=False))}">
<input type="hidden" name="total" value="{total_pages}"><input type="hidden" name="pages" value="{e(','.join(map(str, pages)))}">
<label style="margin:0">월 보험료</label><input type="text" name="monthly" value="{v('monthly')}" placeholder="예: 255102" style="width:100px">
<label style="margin:0">납입 연수</label><input type="text" name="years" value="{v('years')}" placeholder="예: 32" style="width:50px">
<label style="margin:0">공시이율(%)</label><input type="text" name="rate" value="{v('rate')}" placeholder="2.5" style="width:50px">
<label style="margin:0">낸 회차</label><input type="text" name="paid_count" value="{v('paid_count')}" placeholder="예: 57" style="width:50px">
<label style="margin:0">낸 돈(원)</label><input type="text" name="paid_amount" value="{v('paid_amount')}" placeholder="예: 14540814" style="width:110px">
<label style="margin:0">현재 해지환급금(원)</label><input type="text" name="current" value="{v('current')}" placeholder="모르면 비워두기" style="width:120px">
<textarea name="pasted" rows="4" style="width:100%" placeholder="또는 보험사 앱 계약조회 화면 글을 그대로 붙여넣기 (예: 매월납, 32년 / 255,102원 / 57회 / 납입완료 14,540,814원)">{v('pasted')}</textarea>
<button class="btn-sm">계산</button>
<div style="width:100%;border-top:1px solid #ddd;margin-top:8px;padding-top:8px" class="row">
<b>고객에 저장</b>
<select name="customer_id"><option value="">+ 새 고객</option>{"".join(f'<option value="{c["id"]}">{e(c.get("name") or "이름 미확인")} {e(ymd(c.get("birth_date")))}</option>' for c in customers)}</select>
<label style="margin:0">새 고객 이름</label><input type="text" name="name" placeholder="새 고객일 때" style="width:90px">
<label style="margin:0">생년월일</label><input type="text" name="birth_date" placeholder="19900101" style="width:90px">
<button class="btn-sm" formaction="/terms/save">이 계약을 고객에 저장</button></div></form>
<p class="muted">보험사 앱의 납입내역·해지환급금 조회 화면 숫자를 적거나 붙여넣으면 실제 기준으로 계산합니다. 다시 분석하지 않으니 비용이 들지 않습니다.
저장하면 약관의 사업비가 함께 저장되어 고객 화면에서도 연금 수익률·해지 실수령액이 보입니다.</p></div>"""


def terms_policy(t: dict, f: dict) -> dict:
    """약관 분석 결과 + 직접 적은/붙여넣은 계약 숫자 → 고객 계약 데이터"""
    def first_year(word: str) -> float | None:
        xs = [x["pct_of_premium"] for x in t["fees"] if word in x["name"] and (x["from_year"] or 1) <= 1]
        return sum(xs) if xs else None
    years = int(_num(f.get("years", "")))
    cfg = {"paid_count": int(_num(f.get("paid_count", ""))) or None, "paid_amount": _num(f.get("paid_amount", "")) or None,
           "current_value": _num(f.get("current", "")) or None, "rate": _num(f.get("rate", "")) or None, "credit_pct": 13.2}
    for k, word in (("acq_pct", "체결"), ("mgmt_pct", "관리")):
        if (pct := first_year(word)) is not None:
            cfg[k] = pct
    return {"company": t.get("company") or "", "product_name": f.get("product_name") or t.get("product_name") or "",
            "policy_no": f.get("policy_no") or "", "contract_date": f.get("contract_date") or "",
            "monthly_premium": int(_num(f.get("monthly", ""))) or None,
            "payment_period": f"{years}년납" if years else "", "payment_cycle": "월납",
            "coverages": [], "exclusions": [], "pension": cfg}


def terms_block(t: dict, total: int, pages: list[int], f: dict | None = None, customers: list[dict] = ()) -> str:
    """약관 하나의 결과 + 입력칸. f = 직접 적은 값(문자열)"""
    f = f or {}
    m = _num(f.get("monthly", "")) or t.get("example_monthly_premium") or 0
    y = int(_num(f.get("years", "")) or t.get("example_pay_years") or 0)
    r = _num(f.get("rate", ""))
    rates = sorted({g["rate_pct"] for g in t["min_guarantee"][:1]} | ({r} if r else {2.5, 3.0}))
    mine = terms_mine_html(t, r or 2.5, int(_num(f.get("paid_count", ""))), _num(f.get("paid_amount", "")), _num(f.get("current", "")))
    return terms_result_html(t, m, y, rates, total, pages, mine) + terms_input_form(t, total, pages, f, customers)


def terms_result_html(t: dict, monthly: float, years: int, rates: list[float], total_pages: int, pages: list[int],
                      mine: str = "") -> str:
    import terms as tm
    mg = "".join(f"<tr><td>{e(g['period'])}</td><td class='num'>{g['rate_pct']:g}%</td></tr>" for g in t["min_guarantee"]) \
        or "<tr><td colspan='2' class='muted'>약관에서 찾지 못함</td></tr>"
    fees = "".join(f"<tr><td>{e(f['name'])}</td><td>{int(f['from_year'])}~{'' if f['to_year'] >= 99 else int(f['to_year'])}년</td>"
                   f"<td class='num'>{f['pct_of_premium']:g}%</td><td class='muted'>{e(f['note'])}</td></tr>" for f in t["fees"]) \
        or "<tr><td colspan='4' class='muted'>약관에서 찾지 못함</td></tr>"
    pen = " → ".join(f"{int(p['year'])}년 {won(p['amount_won'])}" for p in t["surrender_penalty"]) or "없음/찾지 못함"
    sims = [tm.simulate(t["fees"], monthly, years, r) for r in rates] if monthly and years else []
    sim_rows = "".join(f"<tr><td class='num'>{s['rate']:g}%</td><td class='num'>{won(round(s['paid'], -4))}</td>"
                       f"<td class='num'>{won(round(s['value'], -4))}</td><td class='num'>{s['ratio'] * 100:.1f}%</td>"
                       f"<td class='num'><b>{s['irr']:.2f}%</b></td></tr>" for s in sims)
    sim = (f"""<p class="muted">월 {won(monthly)} × {years}년 납입, 해당 이율이 계속 유지된다고 가정 (사업비 반영, 세액공제 미반영)</p>
<div class="tbl-wrap"><table><tr><th>적용 이율</th><th>낸 돈</th><th>{years}년 후 적립금</th><th>환급률</th><th>실질 연수익률</th></tr>{sim_rows}</table></div>"""
           if sims else '<p class="muted">월 보험료·납입 연수를 알 수 없어 시뮬레이션을 생략했습니다. 값을 넣고 다시 분석하세요.</p>')
    li = lambda xs: "".join(f"<li>{e(x)}</li>" for x in xs) or "<li class='muted'>없음</li>"  # noqa: E731
    return f"""<div class="card"><h1>{e(t['product_name'] or '약관 분석 결과')}</h1>
<p class="muted">{e(t['company'])} · {e(t['product_type'])} · 원본 {total_pages}쪽 중 관련 {len(pages)}쪽 분석 ({', '.join(map(str, pages))}쪽)</p>
<p><b>적립 이율:</b> {e(t['rate_type'] or '-')}</p>{mine}</div>
<div class="card"><h2>예상 수익률 시뮬레이션</h2>{sim}</div>
<div class="card"><h2>최저보증이율</h2><div class="tbl-wrap"><table><tr><th>기간</th><th>연복리</th></tr>{mg}</table></div></div>
<div class="card"><h2>사업비 (보험료에서 떼는 비용)</h2>
<p class="muted">예시 기준: {e(t['fee_example_basis'] or '-')}</p>
<div class="tbl-wrap"><table><tr><th>항목</th><th>기간</th><th>기본보험료 대비</th><th>비고</th></tr>{fees}</table></div>
<p class="muted">추가납입 비용: {t['extra_payment_fee_pct']:g}% · 연금 수령 시: {e(t['annuity_fee_note'] or '-')}</p></div>
<div class="card"><h2>해지공제 (조기 해지 페널티)</h2><p>{e(pen)}</p></div>
<div class="card"><h2>세제</h2><ul>{li(t['tax_notes'])}</ul>
<p class="muted">약관 작성 당시 세법 기준입니다. 현재 세액공제 한도·세율은 따로 확인하세요.</p></div>
<div class="card"><h2>주의할 점</h2><ul>{li(t['cautions'])}</ul></div>
<p class="muted">이 결과는 약관을 정리한 참고 자료입니다. 실제 수익률은 해지환급금 조회(낸 돈·현재 적립금)로 확인하세요.</p>"""


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    planner_id(request)
    return page("약관 분석", f'<p><a href="/">← 고객 목록</a></p>{take_flash(request)}{terms_form()}')


async def _terms_one(request: Request, pid: int, name: str, pdf: bytes, f: dict) -> str:
    """약관 하나 분석 → 결과 HTML (실패하면 오류 카드)"""
    import terms as tm
    try:
        small, total, pages = await run_in_threadpool(tm.select_pages, pdf)
        t = await run_in_threadpool(tm.extract_terms, small)
    except tm.TermsError as ex:
        return f'<div class="card"><h2>{e(name)}</h2><p class="under">분석 실패: {e(str(ex))}</p></div>'
    log(request, pid, "terms_analyze", f"{total}쪽")
    return terms_block(t, total, pages, f, store.list_customers(pid))


async def _terms_page(request: Request, pid: int, pdfs: list[tuple[str, bytes]], f: dict | None = None, head: str = ""):
    """약관 여러 개를 동시에 분석해 한 화면에 보여줌"""
    blocks = await asyncio.gather(*(_terms_one(request, pid, n, d, f or {}) for n, d in pdfs))
    nav = '<p><a href="/terms">← 다른 약관 분석</a> · <a href="/">고객 목록</a></p>'
    sep = '<hr style="margin:32px 0">'
    return page("약관 분석 결과", nav + head + sep.join(blocks))


@app.post("/terms", response_class=HTMLResponse)
async def terms_analyze(request: Request, files: list[UploadFile] = File(...), monthly: str = Form(""),
                        years: str = Form(""), rate: str = Form("")):
    pid = planner_id(request)
    if len(files) > MAX_FILES:
        return page("약관 분석", terms_form(f"한 번에 {MAX_FILES}개까지 올릴 수 있습니다."))
    pdfs, total = [], 0
    for up in files:
        data = await up.read()
        total += len(data)
        if security.sniff_mime(data) != extractor.PDF_TYPE:
            return page("약관 분석", terms_form(f"PDF 파일만 올릴 수 있습니다: {up.filename}"))
        pdfs.append((up.filename or "약관.pdf", data))
    if total > TERMS_MAX_BYTES:
        return page("약관 분석", terms_form("파일 합계가 30MB를 넘습니다. 나눠서 올려 주세요."))
    return await _terms_page(request, pid, pdfs, {"monthly": monthly, "years": years, "rate": rate})


def _terms_form_values(t: str, pages: str, f: dict, pasted: str) -> tuple[dict, list[int], dict]:
    """숨김값으로 넘어온 약관 결과 + 직접 적은 값 + 붙여넣은 글(빈 칸만 채움) 정리"""
    import pension
    f = {**f, "pasted": pasted[:5000]}
    for k, v in pension.parse_pasted(f["pasted"]).items():
        f[k] = f.get(k) or v
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError
    return data, [int(x) for x in pages.split(",") if x.strip()], f


@app.post("/terms/calc", response_class=HTMLResponse)
async def terms_calc(request: Request, t: str = Form(...), total: int = Form(0), pages: str = Form(""),
                     monthly: str = Form(""), years: str = Form(""), rate: str = Form(""),
                     paid_count: str = Form(""), paid_amount: str = Form(""), current: str = Form(""),
                     pasted: str = Form("")):
    """직접 적은 숫자로 약관 결과 재계산 (Claude 다시 호출 안 함)"""
    pid = planner_id(request)
    f = {"monthly": monthly, "years": years, "rate": rate, "paid_count": paid_count,
         "paid_amount": paid_amount, "current": current}
    try:
        data, nums, f = _terms_form_values(t, pages, f, pasted)
        body = terms_block(data, total, nums, f, store.list_customers(pid))
    except (ValueError, TypeError, KeyError, AttributeError):
        return page("약관 분석", terms_form("계산값을 읽지 못했습니다. 약관을 다시 올려 주세요."))
    return page("약관 분석 결과", '<p><a href="/terms">← 다른 약관 분석</a> · <a href="/">고객 목록</a></p>' + body)


@app.post("/terms/save")
async def terms_save(request: Request, t: str = Form(...), pages: str = Form(""),
                     monthly: str = Form(""), years: str = Form(""), rate: str = Form(""),
                     paid_count: str = Form(""), paid_amount: str = Form(""), current: str = Form(""),
                     pasted: str = Form(""), customer_id: str = Form(""), name: str = Form(""), birth_date: str = Form("")):
    """약관 결과 + 계약 숫자를 고객 계약으로 저장"""
    pid = planner_id(request)
    f = {"monthly": monthly, "years": years, "rate": rate, "paid_count": paid_count,
         "paid_amount": paid_amount, "current": current}
    try:
        data, _, f = _terms_form_values(t, pages, f, pasted)
        policy = terms_policy(data, f)
    except (ValueError, TypeError, KeyError, AttributeError):
        return page("약관 분석", terms_form("저장할 값을 읽지 못했습니다. 약관을 다시 올려 주세요."))
    if customer_id.isdigit():
        cid = int(customer_id)
        if not store.get_customer(pid, cid):
            raise HTTPException(404)
    else:
        bd = "".join(ch for ch in birth_date if ch.isdigit())[:8]
        cid = store.save_customer(pid, {"name": name.strip()[:50], "birth_date": bd or None, "gender": None,
                                        "address": None, "phone": None, "memo": ""})
    added, replaced = store.add_policies(pid, cid, [policy], "약관+직접입력")
    log(request, pid, "upload", f"고객#{cid} 약관 계약 저장")
    flash(request, "계약을 저장했습니다." + (" (같은 증권번호 계약을 갱신)" if replaced else "") + " 내용이 맞는지 확인해 주세요.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)

@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), customer_id: int | None = Form(None)):
    pid = planner_id(request)
    back = f"/customers/{customer_id}" if customer_id else "/"
    if customer_id and not store.get_customer(pid, customer_id):
        raise HTTPException(404)
    if len(files) > MAX_FILES:
        flash(request, f"파일은 한 번에 {MAX_FILES}개까지 올릴 수 있습니다.")
        return RedirectResponse(back, status_code=303)

    payload, total = [], 0
    for f in files:
        data = await f.read()
        total += len(data)
        mime = security.sniff_mime(data)  # 확장자·브라우저 정보가 아니라 실제 파일 내용으로 판별
        if not mime:
            flash(request, f"지원하지 않는 파일 형식입니다: {f.filename} (PDF, JPG, PNG, WEBP 가능. 아이폰 HEIC 사진은 JPG로 변환해 주세요)")
            return RedirectResponse(back, status_code=303)
        payload.append((f.filename or "file", mime, data))
    if total > MAX_TOTAL_BYTES:
        flash(request, "파일 합계가 30MB를 넘습니다. 나눠서 올려 주세요.")
        return RedirectResponse(back, status_code=303)

    import terms as tm
    pdfs = [(n, d) for n, m, d in payload if m == extractor.PDF_TYPE]
    is_terms = await asyncio.gather(*(run_in_threadpool(tm.is_terms_pdf, d) for _, d in pdfs))
    terms_pdfs = [x for x, ok in zip(pdfs, is_terms) if ok]
    terms_head = '<div class="flash">약관 파일로 확인되어 약관 분석 결과를 보여드립니다. 고객 계약 정보(증권)는 따로 올려 주세요.</div>'
    # 올린 파일이 전부 약관이면 → 여러 개 한꺼번에 약관 분석
    if terms_pdfs and len(terms_pdfs) == len(payload):
        return await _terms_page(request, pid, terms_pdfs, head=terms_head)
    # 증권과 약관을 섞어 올리면 약관은 빼고 증권만 추출 (약관은 따로 올리도록 안내)
    if terms_pdfs:
        payload = [x for x in payload if not any(x[2] is d for _, d in terms_pdfs)]
        flash(request, f"약관 파일 {len(terms_pdfs)}개는 제외하고 증권만 분석했습니다. 약관은 따로 올려 주세요.")

    try:
        result = await run_in_threadpool(extractor.extract, payload)
    except extractor.ExtractError as ex:
        flash(request, str(ex))
        return RedirectResponse(back, status_code=303)

    insured = result.get("insured") or {}
    policies = result.get("policies") or []
    if not policies:
        if pdfs:  # 증권 내용이 없으면 약관일 수 있으니 약관 분석으로 한 번 더 시도
            return await _terms_page(request, pid, pdfs, head=terms_head)
        flash(request, "증권에서 계약 정보를 찾지 못했습니다. 글씨가 잘 보이는 파일인지 확인해 주세요.")
        return RedirectResponse(back, status_code=303)

    cid = customer_id or find_customer(pid, insured)
    if cid:
        c = store.get_customer(pid, cid)
        merged = {k: c.get(k) for k in ("name", "birth_date", "gender", "address", "phone", "memo")}
        for k in ("name", "birth_date", "gender", "address", "phone"):
            if not merged.get(k) and insured.get(k):
                merged[k] = insured[k]
        store.save_customer(pid, merged, cid)
    else:
        cid = store.save_customer(pid, {**{k: insured.get(k) for k in ("name", "birth_date", "gender", "address", "phone")}, "memo": ""})

    added, replaced = store.add_policies(pid, cid, policies, ", ".join(n for n, _, _ in payload)[:300])
    log(request, pid, "upload", f"고객#{cid} 파일{len(payload)}개")
    flash(request, f"계약 {added}건 추가" + (f", {replaced}건 갱신(같은 증권번호)" if replaced else "") + " 완료. 추출 내용이 맞는지 확인해 주세요.")
    for w in result.get("warnings") or []:
        flash(request, f"⚠ {w}")
    if customer_id and insured.get("name") and store.get_customer(pid, cid).get("name") not in (None, insured.get("name")):
        flash(request, f"⚠ 증권의 피보험자({insured.get('name')})가 이 고객과 다릅니다. 확인해 주세요.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


# ── 고객 목록 ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = ""):
    pid = planner_id(request)
    me = store.get_planner(pid)
    customers = store.list_customers(pid)
    notes = store.all_notes(pid)
    all_pol = store.all_policies(pid)
    names = {c["id"]: c.get("name") or "이름 미확인" for c in customers}
    bad = {}
    for p in all_pol:
        st = analysis.manage(p).get("pay_status")
        if st in ("미납", "실효"):
            bad.setdefault(p["customer_id"], set()).add(st)
    if q:
        note_hit = {n["customer_id"] for n in notes if q in (n.get("content") or "") or q in (n.get("next_action") or "")}
        customers = [c for c in customers if c["id"] in note_hit or q in (c.get("name") or "")
                     or q in (c.get("phone") or "") or q in (c.get("memo") or "")]
    rows = "".join(f"""<tr><td><a href="/customers/{c['id']}">{customer_title(c)}</a>{''.join(f' <span class="tag warn-tag">{x}</span>' for x in sorted(bad.get(c['id'], ())))}</td>
<td>{ymd(c.get('birth_date'))}</td><td>{e(c.get('phone') or '-')}</td><td class="num">{c['n']}건</td>
<td class="muted">{ts(c['updated_at'])}</td></tr>""" for c in customers)
    table = (f'<div class="tbl-wrap"><table><tr><th>고객</th><th>생년월일</th><th>연락처</th><th>계약</th><th>최근 수정</th></tr>{rows}</table></div>'
             if rows else '<p class="muted">고객이 없습니다. 증권을 올리면 자동으로 고객이 만들어집니다.</p>')
    return page("고객 목록", f"""
<div class="row between"><h1>보험증권 보장분석</h1>
<form method="post" action="/logout" class="row"><a class="muted" href="/account">{e(me['name'])} · 내 계정</a><button class="btn-ghost btn-sm">로그아웃</button></form></div>
<p class="no-print"><a class="btn btn-ghost btn-sm" href="/tools/disability" target="_blank">🧮 후유장해 보험금 계산기</a></p>
{take_flash(request)}
{todo_html(pid, notes, all_pol, names)}
<div class="card"><h2>새 고객 증권·약관 분석</h2>{upload_form()}</div>
<div class="card"><div class="row between"><h2>고객 ({len(customers)})</h2>
<div class="row"><a class="btn btn-sm" href="/customers/new">+ 고객 직접 등록</a>
<form class="row" method="get"><input type="text" name="q" value="{e(q)}" placeholder="이름·연락처·메모·상담내용 검색" style="width:240px"><button class="btn-ghost btn-sm">검색</button></form></div></div>
{table}</div>""")


def todo_html(pid: int, notes: list[dict], policies: list[dict], names: dict[int, str]) -> str:
    """연락 예정(지난 것 + 7일 이내) + 계약 알림(미납·실효, 90일 이내 만기·갱신)"""
    today = date.today()
    soon = (today + timedelta(days=7)).isoformat()
    items = []  # (날짜, 고객id, 구분, 내용, 버튼)
    for n in notes:
        if n.get("next_date") and not n.get("done") and n["next_date"] <= soon:
            btn = f"""<form method="post" action="/notes/{n['id']}/done"><input type="hidden" name="back" value="/"><button class="btn-ghost btn-sm">완료</button></form>"""
            items.append((n["next_date"], n["customer_id"], '<span class="tag">연락</span>', e(n.get("next_action") or ""), btn))
    for p in policies:
        for d, lvl, text in analysis.policy_alerts(p, today):
            tag = '<span class="tag warn-tag">경고</span>' if lvl == "warn" else '<span class="tag">계약</span>'
            items.append((d, p["customer_id"], tag, e(text), ""))
    if not items:
        return ""
    items.sort(key=lambda x: x[0])
    rows = "".join(f"""<tr><td class="nowrap {'overdue' if d < today.isoformat() else ''}">{e(d)}</td><td>{tag}</td>
<td class="nowrap"><a href="/customers/{cid}">{e(names.get(cid, ''))}</a></td><td>{text}</td><td>{btn}</td></tr>""" for d, cid, tag, text, btn in items)
    return f"""<div class="card"><h2>할 일·알림 ({len(items)})</h2>
<p class="muted">연락 예정(지난 일정·7일 이내), 미납·실효 계약, {analysis.ALERT_DAYS}일 이내 만기·갱신 계약입니다.</p>
<div class="tbl-wrap"><table><tr><th>날짜</th><th>구분</th><th>고객</th><th>내용</th><th></th></tr>{rows}</table></div></div>"""


# ── 고객 상세 / 리포트 ────────────────────────────────────────────────────────
def get_customer_or_404(pid: int, cid: int) -> dict:
    c = store.get_customer(pid, cid)
    if not c:
        raise HTTPException(404)
    return c


SUMMARY_NOTES = {
    "실손의료비": "중복 보상 안 됨 · 최대값",
    "암치료비": "치료별 최대 지급액 합계",
    "암통원": "통원 1회당 금액 합계",
    "입원일당": "1일당 금액 합계",
}


def analysis_html(c: dict, policies: list[dict]) -> str:
    a = analysis.analyze(c, policies)
    stats = f"""<div class="grid">
<div class="stat"><span class="muted">유지 중인 계약</span><b>{a['active_count']}건</b></div>
<div class="stat"><span class="muted">월 보험료 합계</span><b>{won(a['monthly_premium'])}</b></div>
<div class="stat"><span class="muted">만기 지난 계약</span><b>{a['expired_count']}건</b></div>
<div class="stat"><span class="muted">확인할 항목</span><b>{sum(1 for x in a['checks'] if x[0] == 'warn')}개 경고 · {sum(1 for x in a['checks'] if x[0] == 'info')}개 참고</b></div></div>"""
    stats += pension_summary_html(policies)

    rows = []
    for s in a["summary"]:
        bm = s["benchmark"]
        state = "" if not bm else (f'<span class="enough">충분</span>' if s["amount"] >= bm else f'<span class="under">부족 ({won(bm)} 기준)</span>')
        note = SUMMARY_NOTES.get(s["category"], "")
        note = f" <span class='muted'>({note})</span>" if note else ""
        subtotal = won(s["amount"]) if s["amount"] else ""
        # 분류 머리행 (합계·판단)
        rows.append(f"""<tr class="cat-row"><td class="nowrap">{e(s['category'])}{note}</td>
<td class="num">{subtotal}</td><td class="nowrap">{state}</td></tr>""")
        # 그 분류의 모든 담보를 하나씩
        for it in s["items"]:
            amt = won(it["amount"]) if it["amount"] else "-"
            rnw = " · 갱신형" if it["renewable"] else ""
            rows.append(f"""<tr class="sub-row"><td class="sub">{e(it['name'])} <span class="muted small">({e(it['company'])}{rnw})</span></td>
<td class="num muted">{amt}</td><td></td></tr>""")
    for cat in a["missing_benchmarks"]:
        rows.append(f'<tr class="cat-row"><td class="nowrap">{e(cat)}</td><td class="num under">없음</td><td class="nowrap"><span class="under">미가입 ({won(analysis.BENCHMARKS[cat])} 기준)</span></td></tr>')
    rows += pension_sum_rows(policies)
    summary = f'<div class="tbl-wrap"><table><tr><th>보장 / 담보</th><th class="num">금액</th><th>판단</th></tr>{"".join(rows)}</table></div>'

    checks = "".join(f'<div class="check {lvl}"><b>{e(t)}</b><span>{e(d)}</span></div>' for lvl, t, d in a["checks"]) \
        or '<p class="enough">특별히 확인할 항목이 없습니다.</p>'

    return f"""<div class="card"><h2>요약</h2>{stats}</div>
<div class="card"><h2>체크 포인트</h2>{checks}</div>
<div class="card"><h2>보장 합산</h2>{summary}
<p class="muted">판단 기준은 상담용 참고값입니다. 고객의 소득·가족력·기존 계획에 따라 달라질 수 있습니다.</p></div>"""


def pension_sum_rows(policies: list[dict]) -> list[str]:
    """보장 합산 표에 연금·저축 계약 행 (금액 = 지금 해지 시 실수령, 판단 칸 = 낸 돈)"""
    import pension
    items = [(p, pension.analyze(p)) for p in policies if pension.is_pension(p)]
    if not items:
        return []
    sv = [r["surrender"] for _, r in items if r and r.get("ok") and r.get("surrender")]
    head = (f'<tr class="cat-row"><td class="nowrap">연금·저축 <span class="muted">(지금 해지 시 실수령)</span></td>'
            f'<td class="num">{won(round(sum(s["after_tax"] for s in sv), -3)) if sv else ""}</td>'
            f'<td class="nowrap">{"낸 돈 " + won(sum(s["paid"] for s in sv)) if sv else ""}</td></tr>')
    rows = [head]
    for p, r in items:
        s = r.get("surrender") if r and r.get("ok") else None
        amt = won(round(s["after_tax"], -3)) if s else "-"
        paid = f'<span class="muted small">낸 돈 {won(s["paid"])}</span>' if s else '<span class="muted small">계산값 입력 필요</span>'
        rows.append(f"""<tr class="sub-row"><td class="sub">{e(p.get('product_name') or '')} <span class="muted small">({e(p.get('company') or '')} · 월 {won(p.get('monthly_premium'))})</span></td>
<td class="num muted">{amt}</td><td>{paid}</td></tr>""")
    return rows


def pension_summary_html(policies: list[dict]) -> str:
    """요약 칸: 연금·저축 계약의 고객 납입 합계와 지금 해지 시 실수령 합계"""
    import pension
    rs = [r for r in (pension.analyze(p) for p in policies) if r and r.get("ok") and r.get("surrender")]
    if not rs:
        return ""
    paid = sum(r["surrender"]["paid"] for r in rs)
    after = sum(r["surrender"]["after_tax"] for r in rs)
    loss = paid - after
    return f"""<div class="grid" style="margin-top:12px">
<div class="stat"><span class="muted">연금·저축 고객이 낸 돈 ({len(rs)}건)</span><b>{won(round(paid, -3))}</b></div>
<div class="stat"><span class="muted">지금 해지 시 실수령 (세후)</span><b>{won(round(after, -3))}</b></div>
<div class="stat"><span class="muted">지금 해지 시 {"손실" if loss > 0 else "이익"}</span><b class="{'under' if loss > 0 else 'enough'}">{won(round(abs(loss), -3))}</b></div></div>
<p class="muted">{"모든 계약이 실제 기록 기준입니다." if all(r["paid_actual"] and r.get("actual") for r in rs) else
"실제 납입 기록·해지환급금을 입력하지 않은 계약은 '매달 납입' 가정의 추정치입니다. 각 계약의 '연금 수익률 → 수익률 계산값 입력'에서 보험사 앱의 실제 납입회차·납입액·해지환급금을 넣으면 정확해집니다."}</p>"""


def pension_html(p: dict, editable: bool) -> str:
    """연금·저축 계약이면 수익률·해지 시 금액 박스"""
    import pension
    r = pension.analyze(p)
    if r is None:
        return ""
    form = pension_form(p) if editable else ""
    if not r["ok"]:
        return f'<div class="manage"><h3>연금 수익률</h3><div class="check info"><b>계산 불가</b><span>{e(r["reason"])}</span></div>{form}</div>'
    rows = []
    if r.get("actual"):
        a = r["actual"]
        rows.append(f"<tr><td><b>실제 (입력한 적립금)</b></td><td class='num'>{won(r['paid'])}</td><td class='num'>{won(a['value'])}</td>"
                    f"<td class='num'>{a['ratio'] * 100:.1f}%</td><td class='num'><b>{a['irr']:.2f}%</b></td></tr>")
    for s in r["projections"]:
        rows.append(f"<tr><td>만기 예상 · 이율 {s['rate']:g}%</td><td class='num'>{won(round(s['paid'], -4))}</td>"
                    f"<td class='num'>{won(round(s['value'], -4))}</td><td class='num'>{s['ratio'] * 100:.0f}%</td>"
                    f"<td class='num'>{s['irr']:.2f}%</td></tr>")
    tax = ""
    if r.get("tax"):
        t = r["tax"]
        tax = (f"<p>세액공제: 연 납입 {won(t['annual'])} × {t['credit_pct']:g}% = <b>매년 약 {won(round(t['credit'], -3))} 환급</b> · "
               f"세액공제 포함 체감 수익률 약 <b>{t['felt_irr']:.2f}%</b> (이율 {r['rate']:g}% 기준)</p>")
    sv = r.get("surrender")
    surr = ""
    if sv:
        surr = f"""<div class="check warn"><b>지금 해지하면 ({r['months_paid']}회 납입 시점)</b><span>
낸 돈 {won(sv['paid'])} → 해지환급금 {won(round(sv['before_tax'], -3))}{f" − 기타소득세(16.5%) {won(round(sv['tax'], -3))}" if sv['tax'] else ""}
= <b>실수령 약 {won(round(sv['after_tax'], -3))}</b> · <b>손실 약 {won(round(sv['loss'], -3))}</b><br>
<span class="muted">{e(sv['basis'])}. {"그동안 받은 세액공제(약 " + won(round(sv['credits_received'], -3)) + ")를 감안해도 해지는 불리할 수 있습니다." if sv['tax'] else ""}</span></span></div>"""
    note = ("사업비는 입력값 기준" if r["custom_fees"] else
            f"사업비는 일반 가정(체결 {pension.DEFAULT_ACQ_PCT}%·10년, 관리 {pension.DEFAULT_MGMT_PCT}%) — 상품 약관과 다를 수 있음")
    return f"""<div class="manage"><h3>연금 수익률</h3>
<p class="muted">월 {won(r['monthly'])} × {f"{r['years']}년" if r['years'] else "납입기간 미확인(만기 예상 생략)"} · 현재 {r['months_paid']}/{r['total_months']}회 납입 · 낸 돈 {won(r['paid'])} <b>({"실제 기록" if r['paid_actual'] else "추정: 매달 납입 가정"})</b> · {note}</p>
<div class="tbl-wrap"><table><tr><th>구분</th><th>낸 돈</th><th>적립금</th><th>환급률</th><th>연수익률</th></tr>{''.join(rows)}</table></div>
{tax}{surr}{form}</div>"""


def pension_form(p: dict) -> str:
    """연금 계약에 실제 납입 기록·해지환급금 등을 직접 적는 칸 (기록이 없으면 펼쳐서 보여줌)"""
    import pension
    cfg = p.get("pension") or {}
    v = lambda k: e(str(cfg.get(k) if cfg.get(k) is not None else ""))  # noqa: E731
    is_open = "" if cfg.get("paid_amount") or cfg.get("paid_count") else " open"
    return f"""<details class="manage no-print"{is_open}><summary><b>✏ 낸 돈·해지환급금 직접 적기</b> (보험사 앱·납입내역 보고 입력)</summary>
<form method="post" action="/policies/{p['id']}/pension" class="row" style="margin-top:8px">
<label style="margin:0">실제 납입회차</label><input type="text" name="paid_count" value="{v('paid_count')}" placeholder="예: 57" style="width:60px">
<label style="margin:0">실제 납입액(원)</label><input type="text" name="paid_amount" value="{v('paid_amount')}" placeholder="예: 14540814" style="width:120px">
<label style="margin:0">현재 해지환급금(원)</label><input type="text" name="current_value" value="{v('current_value')}" style="width:130px">
<label style="margin:0">기준일</label><input type="date" name="as_of" value="{v('as_of')}">
<label style="margin:0">공시이율(%)</label><input type="text" name="rate" value="{v('rate')}" placeholder="{pension.DEFAULT_RATE}" style="width:60px">
<label style="margin:0">체결비용(%)</label><input type="text" name="acq_pct" value="{v('acq_pct')}" placeholder="{pension.DEFAULT_ACQ_PCT}" style="width:60px">
<label style="margin:0">관리비용(%)</label><input type="text" name="mgmt_pct" value="{v('mgmt_pct')}" placeholder="{pension.DEFAULT_MGMT_PCT}" style="width:60px">
<label style="margin:0">세액공제율</label><select name="credit_pct"><option value="13.2" {"selected" if cfg.get("credit_pct") != 16.5 else ""}>13.2%</option><option value="16.5" {"selected" if cfg.get("credit_pct") == 16.5 else ""}>16.5% (총급여 5,500만 이하)</option></select>
<button class="btn-sm">계산</button></form></details>"""


def policy_html(p: dict, editable: bool) -> str:
    covs = "".join(f"""<tr><td>{e(c.get('name') or '')}</td><td class="muted">{e(c.get('category') or '')}</td>
<td class="num">{won(c.get('amount'))}</td><td>{'갱신형' if c.get('renewable') else ('비갱신' if c.get('renewable') is False else '-')}</td>
<td>{ymd(c.get('end_date'))}</td></tr>""" for c in p.get("coverages") or [])
    actions = f"""<div class="row no-print"><a class="btn btn-ghost btn-sm" href="/policies/{p['id']}/edit">수정</a>
<form method="post" action="/policies/{p['id']}/delete" onsubmit="return confirm('이 계약을 삭제할까요?')"><button class="btn-danger btn-sm">삭제</button></form></div>""" if editable else ""
    excl = f'<p class="under">부담보·인수조건: {e(" / ".join(p["exclusions"]))}</p>' if p.get("exclusions") else ""
    m = analysis.manage(p)
    badges = ""
    if editable:
        st = m.get("pay_status")
        cls = "warn-tag" if st in ("미납", "실효") else ("ok-tag" if st == "정상" else "")
        badges = f""" <span class="tag">{e(analysis.OWNERS.get(m.get('owner', ''), '확인 전'))}</span>""" + \
                 (f' <span class="tag {cls}">{e(st)}</span>' if st else "")
    manage_html = pension_html(p, editable) + (manage_form(p) if editable else "")
    return f"""<div class="card"><div class="row between"><h3>{e(p.get('company') or '?')} · {e(p.get('product_name') or '')}{badges}</h3>{actions}</div>
<p class="muted">증권번호 {e(p.get('policy_no') or '-')} · 계약자 {e(p.get('contractor_name') or '-')} · 피보험자 {e(p.get('insured_name') or '-')}<br>
계약일 {ymd(p.get('contract_date'))} · 만기 {ymd(p.get('maturity_date'))} · {e(p.get('payment_period') or '-')} {e(p.get('payment_cycle') or '')}
· 월 {won(p.get('monthly_premium'))}{' · 갱신형' if p.get('is_renewable') else ''}</p>{excl}
<div class="tbl-wrap"><table><tr><th>담보</th><th>분류</th><th>가입금액</th><th>갱신</th><th>보장 종료</th></tr>{covs}</table></div>
{f'<p class="muted no-print">원본 파일: {e(p["source"])}</p>' if editable and p.get("source") else ''}{manage_html}</div>"""


def manage_form(p: dict) -> str:
    m = analysis.manage(p)
    owner_opts = "".join(f'<option value="{k}" {"selected" if m.get("owner", "") == k else ""}>{v}</option>' for k, v in analysis.OWNERS.items())
    st_opts = '<option value="">확인 전</option>' + "".join(
        f'<option {"selected" if m.get("pay_status") == s else ""}>{s}</option>' for s in analysis.PAY_STATUSES)
    summary = f"납입 상태 {e(m.get('pay_status') or '확인 전')}" + (f" · 매월 {e(str(m['pay_day']))}일 이체" if m.get("pay_day") else "") \
        + (f" · 확인일 {e(m['checked_at'])}" if m.get("checked_at") else "") + (f" · {e(m['memo'])}" if m.get("memo") else "")
    return f"""<details class="manage no-print"><summary>관리 정보 — {summary}</summary>
<form method="post" action="/policies/{p['id']}/manage" class="row" style="margin-top:8px">
<label style="margin:0">구분</label><select name="owner">{owner_opts}</select>
<label style="margin:0">납입 상태</label><select name="pay_status">{st_opts}</select>
<label style="margin:0">이체일</label><input type="text" name="pay_day" value="{e(str(m.get('pay_day') or ''))}" placeholder="25" style="width:60px">
<label style="margin:0">확인일</label><input type="date" name="checked_at" value="{e(m.get('checked_at') or date.today().isoformat())}">
<input type="text" name="memo" value="{e(m.get('memo') or '')}" placeholder="메모 (예: 고객 앱 캡처로 확인)" style="flex:1;min-width:180px">
<button class="btn-sm">저장</button></form></details>"""


def notepad_card(cid: int, text: str) -> str:
    return f"""<form method="post" action="/customers/{cid}/notepad" class="card no-print" id="notepad">
<div class="row between"><h2 style="margin:0">자유 메모장</h2><button class="btn-sm">저장</button></div>
<p class="muted" style="margin:4px 0 8px">대화 내용, 특이사항 등을 자유롭게 적어 두세요. 날짜별 상담 기록은 아래에 따로 있습니다.</p>
<textarea name="notepad" class="note" style="min-height:220px" placeholder="예) 배우자도 보험 점검 원함. 형이 암 병력 있어 암 보장 민감. 다음 달 이직 예정 - 단체보험 종료 확인.">{e(text)}</textarea>
</form>"""


def info_form(c: dict, action: str, title: str, button: str) -> str:
    v = lambda k: e(c.get(k) or "")  # noqa: E731
    return f"""<form method="post" action="{action}" class="card no-print"><h2>{title}</h2>
<div class="grid"><div><label>이름</label><input type="text" name="name" value="{v('name')}" required></div>
<div><label>생년월일 (YYYYMMDD)</label><input type="text" name="birth_date" value="{v('birth_date')}"></div>
<div><label>성별</label><input type="text" name="gender" value="{v('gender')}" placeholder="남/여"></div>
<div><label>연락처</label><input type="text" name="phone" value="{v('phone')}"></div></div>
<label>주소</label><input type="text" name="address" value="{v('address')}">
<label>메모 (직업, 가족관계, 소개자 등)</label><input type="text" name="memo" value="{v('memo')}">
<p class="row"><button class="btn-sm">{button}</button></p></form>"""


def customer_fields(name, birth_date, gender, phone, address, memo) -> dict:
    return {"name": name.strip(), "birth_date": "".join(ch for ch in birth_date if ch.isdigit()),
            "gender": gender.strip(), "phone": phone.strip(), "address": address.strip(), "memo": memo.strip()}


@app.get("/customers/new", response_class=HTMLResponse)
def new_customer_form(request: Request):
    planner_id(request)
    return page("고객 등록", f"""<p><a href="/">← 고객 목록</a></p>
{info_form({}, "/customers/new", "고객 직접 등록", "등록")}
<p class="muted">증권은 등록 후 고객 화면에서 올릴 수 있습니다.</p>""")


@app.post("/customers/new")
def new_customer(request: Request, name: str = Form(...), birth_date: str = Form(""), gender: str = Form(""),
                 phone: str = Form(""), address: str = Form(""), memo: str = Form("")):
    pid = planner_id(request)
    cid = store.save_customer(pid, customer_fields(name, birth_date, gender, phone, address, memo))
    log(request, pid, "create_customer", f"고객#{cid}")
    flash(request, "고객을 등록했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


# ── 상담 기록 ─────────────────────────────────────────────────────────────────
KINDS = ["전화", "방문", "카톡·문자", "기타"]


def note_form(action: str, n: dict, button: str) -> str:
    kind_opts = "".join(f'<option {"selected" if n.get("kind") == k else ""}>{k}</option>' for k in KINDS)
    return f"""<form method="post" action="{action}" enctype="multipart/form-data">
<div class="row"><input type="date" name="date" value="{e(n.get('date') or date.today().isoformat())}" required>
<select name="kind">{kind_opts}</select></div>
<label>상담 내용</label><textarea class="note" name="content" required placeholder="고객 요청, 관심 상품, 가족 상황, 다음에 말할 것 등">{e(n.get('content') or '')}</textarea>
<div class="row" style="margin-top:8px"><label style="margin:0">다음 연락일</label><input type="date" name="next_date" value="{e(n.get('next_date') or '')}">
<input type="text" name="next_action" value="{e(n.get('next_action') or '')}" placeholder="할 일 (예: 암보험 설계안 전달)" style="flex:1;min-width:200px"></div>
<label>사진 첨부 (선택, 최대 {MAX_NOTE_IMAGES}장)</label><input type="file" name="images" accept="image/*" multiple>
<p><button class="btn-sm">{button}</button></p></form>"""


async def read_note_images(request: Request, files: list[UploadFile], nid: int, pid: int) -> int:
    """업로드된 이미지들을 검증 후 저장. 저장한 장수 반환."""
    saved = already = store.count_note_images(pid, nid)
    for f in files or []:
        if not (f.filename and (f.filename.strip() or (f.size or 0))):
            continue
        if saved >= MAX_NOTE_IMAGES:
            flash(request, f"사진은 기록당 {MAX_NOTE_IMAGES}장까지입니다. 일부는 저장하지 않았습니다.")
            break
        data = await f.read()
        if len(data) > MAX_IMAGE_BYTES:
            flash(request, f"사진 한 장이 너무 큽니다(최대 {MAX_IMAGE_BYTES // (1024*1024)}MB): {f.filename}")
            continue
        mime = security.sniff_mime(data)
        if mime not in extractor.IMAGE_TYPES:
            flash(request, f"이미지 파일만 첨부할 수 있습니다: {f.filename}")
            continue
        if store.add_note_image(pid, nid, mime, data):
            saved += 1
    return saved - already


def notes_html(cid: int, notes: list[dict], pid: int) -> str:
    today = date.today().isoformat()
    items = []
    for n in notes:
        follow = ""
        if n.get("next_date"):
            cls = "done" if n.get("done") else ("overdue" if n["next_date"] < today else "")
            toggle = "다시 열기" if n.get("done") else "완료"
            follow = f"""<div class="row"><span class="{cls}">다음 연락 {e(n['next_date'])} · {e(n.get('next_action') or '')}</span>
<form method="post" action="/notes/{n['id']}/done"><input type="hidden" name="back" value="/customers/{cid}"><button class="btn-ghost btn-sm">{toggle}</button></form></div>"""
        imgs = store.note_image_ids(pid, n["id"])
        gallery = ('<div class="thumbs">' + "".join(
            f'<a href="/note-images/{i}" target="_blank" class="thumb"><img src="/note-images/{i}" alt="첨부 사진"></a>' for i in imgs
        ) + "</div>") if imgs else ""
        items.append(f"""<div class="note-item"><div class="row between"><span><b>{e(n.get('date') or '')}</b> <span class="tag">{e(n.get('kind') or '')}</span>{f' <span class="muted small">사진 {len(imgs)}</span>' if imgs else ''}</span>
<div class="row no-print"><a class="btn btn-ghost btn-sm" href="/notes/{n['id']}/edit">수정</a>
<form method="post" action="/notes/{n['id']}/delete" onsubmit="return confirm('이 상담 기록을 삭제할까요?')"><button class="btn-danger btn-sm">삭제</button></form></div></div>
<div class="note-body">{e(n.get('content') or '')}</div>{gallery}{follow}</div>""")
    listing = "".join(items) or '<p class="muted">아직 상담 기록이 없습니다.</p>'
    return f"""<div class="card no-print"><h2>상담 기록 ({len(notes)})</h2>
<details {"open" if not notes else ""}><summary>+ 새 상담 기록 쓰기</summary>{note_form(f"/customers/{cid}/notes", {}, "기록 저장")}</details>
<div style="margin-top:12px">{listing}</div></div>"""


def note_fields(date_: str, kind: str, content: str, next_date: str, next_action: str, done: bool = False) -> dict:
    return {"date": date_, "kind": kind if kind in KINDS else "기타", "content": content.strip(),
            "next_date": next_date or None, "next_action": next_action.strip(), "done": done}


@app.post("/customers/{cid}/notes")
async def add_note(request: Request, cid: int, date: str = Form(...), kind: str = Form("기타"), content: str = Form(...),
                   next_date: str = Form(""), next_action: str = Form(""), images: list[UploadFile] = File(None)):
    pid = planner_id(request)
    get_customer_or_404(pid, cid)
    nid = store.add_note(pid, cid, note_fields(date, kind, content, next_date, next_action))
    n = await read_note_images(request, images, nid, pid) if nid else 0
    log(request, pid, "add_note", f"고객#{cid}")
    flash(request, "상담 기록을 저장했습니다." + (f" 사진 {n}장 첨부." if n else ""))
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@app.get("/notes/{nid}/edit", response_class=HTMLResponse)
def edit_note_form(request: Request, nid: int):
    pid = planner_id(request)
    n = store.get_note(pid, nid)
    if not n:
        raise HTTPException(404)
    imgs = store.note_images(pid, nid)
    thumbs = "".join(f"""<div class="thumb-box"><span class="thumb"><img src="/note-images/{im['id']}" alt="첨부 사진">
<form method="post" action="/note-images/{im['id']}/delete" onsubmit="return confirm('이 사진을 삭제할까요?')"><button class="thumb-x" title="삭제">×</button></form></span>
<form method="post" action="/note-images/{im['id']}/report"><label class="rep-toggle {'on' if im['in_report'] else ''}">
<input type="checkbox" name="on" value="1" {'checked' if im['in_report'] else ''} onchange="this.form.submit()"> 리포트에 표시</label></form></div>""" for im in imgs)
    cur = f'<div class="card"><h2>첨부된 사진 ({len(imgs)})</h2><p class="muted" style="margin-top:-4px">"리포트에 표시"를 켠 사진만 고객용 리포트에 나옵니다.</p><div class="thumbs">{thumbs}</div></div>' if imgs else ""
    return page("상담 기록 수정", f"""<p><a href="/customers/{n['customer_id']}">← 돌아가기</a></p>
{take_flash(request)}
<div class="card"><h1>상담 기록 수정</h1>{note_form(f"/notes/{nid}/edit", n, "저장")}</div>{cur}""")


@app.post("/notes/{nid}/edit")
async def edit_note(request: Request, nid: int, date: str = Form(...), kind: str = Form("기타"), content: str = Form(...),
                    next_date: str = Form(""), next_action: str = Form(""), images: list[UploadFile] = File(None)):
    pid = planner_id(request)
    n = store.get_note(pid, nid)
    if not n:
        raise HTTPException(404)
    store.update_note(pid, nid, note_fields(date, kind, content, next_date, next_action, n.get("done", False)))
    added = await read_note_images(request, images, nid, pid)
    log(request, pid, "edit_note", f"기록#{nid}")
    flash(request, "상담 기록을 수정했습니다." + (f" 사진 {added}장 추가." if added else ""))
    return RedirectResponse(f"/customers/{n['customer_id']}", status_code=303)


@app.get("/note-images/{img_id}")
def serve_note_image(request: Request, img_id: int):
    pid = planner_id(request)
    img = store.get_note_image(pid, img_id)
    if not img:
        raise HTTPException(404)
    mime, data = img
    from fastapi.responses import Response
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-store", "Content-Disposition": "inline"})


@app.post("/note-images/{img_id}/delete")
def delete_note_image(request: Request, img_id: int):
    pid = planner_id(request)
    nid = store.delete_note_image(pid, img_id)
    if nid is None:
        raise HTTPException(404)
    log(request, pid, "delete_note_image", f"기록#{nid}")
    flash(request, "사진을 삭제했습니다.")
    return RedirectResponse(f"/notes/{nid}/edit", status_code=303)


@app.post("/note-images/{img_id}/report")
def toggle_image_report(request: Request, img_id: int, on: str = Form("")):
    pid = planner_id(request)
    nid = store.set_image_report(pid, img_id, on == "1")
    if nid is None:
        raise HTTPException(404)
    return RedirectResponse(f"/notes/{nid}/edit", status_code=303)


@app.post("/notes/{nid}/done")
def toggle_note_done(request: Request, nid: int, back: str = Form("/")):
    pid = planner_id(request)
    n = store.get_note(pid, nid)
    if not n:
        raise HTTPException(404)
    data = {k: v for k, v in n.items() if k not in ("id", "customer_id", "created_at")}
    store.update_note(pid, nid, {**data, "done": not n.get("done")})
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", status_code=303)


@app.post("/notes/{nid}/delete")
def delete_note(request: Request, nid: int):
    pid = planner_id(request)
    cid = store.delete_note(pid, nid)
    if cid is None:
        raise HTTPException(404)
    log(request, pid, "delete_note", f"기록#{nid}")
    flash(request, "상담 기록을 삭제했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@app.get("/customers/{cid}", response_class=HTMLResponse)
def customer_page(request: Request, cid: int):
    pid = planner_id(request)
    c = get_customer_or_404(pid, cid)
    log(request, pid, "view_customer", f"고객#{cid}")
    policies = store.list_policies(pid, cid)
    info = info_form(c, f"/customers/{cid}/info", "고객 정보", "저장")
    plist = "".join(policy_html(p, True) for p in policies) or '<p class="muted">계약이 없습니다.</p>'
    return page(c.get("name") or "고객", f"""
<p class="no-print"><a href="/">← 고객 목록</a></p>
<div class="row between"><h1>{customer_title(c)}</h1>
<div class="row no-print"><a class="btn" href="/customers/{cid}/matrix" target="_blank">보장분석표</a>
<a class="btn" href="/customers/{cid}/report" target="_blank">고객용 리포트</a>
<form method="post" action="/customers/{cid}/delete" onsubmit="return confirm('이 고객과 모든 계약 정보를 삭제할까요? 되돌릴 수 없습니다.')">
<button class="btn-danger btn-sm">고객 삭제</button></form></div></div>
{take_flash(request)}
{info}
{notepad_card(cid, c.get("notepad", ""))}
{notes_html(cid, store.list_notes(pid, cid), pid)}
<div class="card no-print"><h2>증권 추가</h2>{upload_form(cid)}</div>
{analysis_html(c, policies) if policies else ''}
<h2 style="margin-top:24px">가입 계약 ({len(policies)})</h2>{plist}""")


@app.get("/customers/{cid}/report", response_class=HTMLResponse)
def report(request: Request, cid: int):
    pid = planner_id(request)
    c = get_customer_or_404(pid, cid)
    log(request, pid, "view_report", f"고객#{cid}")
    me = store.get_planner(pid)
    policies = store.list_policies(pid, cid)
    return page(f"{c.get('name') or '고객'} 보장분석", f"""
<div class="row between no-print" style="margin-bottom:12px"><span class="muted">인쇄 → 'PDF로 저장'을 선택하면 파일로 받을 수 있습니다.</span>
<button onclick="print()">인쇄 / PDF 저장</button></div>
<div class="card"><h1>보장분석 리포트</h1>
<p><b>{e(c.get('name') or '')}</b> 님 · {ymd(c.get('birth_date'))} · {e(c.get('gender') or '')}<br>
<span class="muted">작성일 {datetime.now():%Y.%m.%d} · 담당 {e(me['name'])}</span></p></div>
{analysis_html(c, policies)}
<h2>가입 계약 상세</h2>{''.join(policy_html(p, False) for p in policies)}
{report_photos(pid, cid)}
<p class="muted">이 리포트는 증권 내용을 정리한 참고 자료이며, 정확한 보장 내용은 각 보험사 약관을 따릅니다.</p>""")


def report_photos(pid: int, cid: int) -> str:
    ids = store.report_image_ids(pid, cid)
    if not ids:
        return ""
    imgs = "".join(f'<img src="/note-images/{i}" alt="첨부 사진" class="report-img">' for i in ids)
    return f'<div class="card"><h2>첨부 사진</h2><div class="report-photos">{imgs}</div></div>'


# 보장분석표: 대분류별 표준 담보 항목. (그룹명, 색, [(표시이름, 분류, 핵심여부)])
MATRIX_GROUPS = [
    ("사망", "#c0392b", [("일반사망(생명보험)", "일반사망", True), ("질병사망(손해보험)", "질병사망", False), ("상해사망", "상해사망", False)]),
    ("후유장해", "#7f8c8d", [("질병후유장해", "질병후유장해", True), ("상해후유장해", "상해후유장해", False)]),
    ("암", "#2e4a8b", [("일반암(최초1회한)", "일반암", True), ("유사암·소액암", "유사암·소액암", False), ("특정암", "특정암", False)]),
    ("암치료비", "#2e6da4", [("항암방사선·약물·표적 등", "암치료비", True), ("암직접치료 통원", "암통원", False)]),
    ("뇌", "#16a085", [("뇌혈관질환 100%", "뇌혈관질환", True), ("뇌졸중·뇌출혈", "뇌졸중·뇌출혈", False)]),
    ("심장", "#2980b9", [("허혈성심장질환 100%", "허혈성심장질환", True), ("급성심근경색", "급성심근경색", False)]),
    ("수술", "#8e44ad", [("질병수술비", "질병수술비", True), ("상해수술비", "상해수술비", False)]),
    ("입원", "#d98880", [("입원일당(1일)", "입원일당", False)]),
    ("간병", "#e67e22", [("치매·간병(장기요양)", "치매·간병", False)]),
    ("실손의료비", "#27ae60", [("실손의료비", "실손의료비", True)]),
    ("배상책임", "#2c3e50", [("일상생활 배상책임", "배상책임", False)]),
    ("운전자", "#c0392b", [("운전자", "운전자", False)]),
    ("치아", "#16a085", [("치아", "치아", False)]),
    ("기타", "#7f8c8d", [("기타", "기타", False)]),
]


def matrix_html(c: dict, policies: list[dict]) -> str:
    a = analysis.analyze(c, policies)
    totals = {s["category"]: s["amount"] for s in a["summary"]}
    rows = []
    for gname, color, items in MATRIX_GROUPS:
        span = len(items)
        for idx, (label, cat, star) in enumerate(items):
            amt = totals.get(cat, 0)
            cell = f'<span class="enough">{won(amt)}</span>' if amt else '<span class="under">-</span>'
            gcell = (f'<td class="mx-g" rowspan="{span}" style="background:{color}">{e(gname)}</td>'
                     if idx == 0 else "")
            rows.append(f'<tr>{gcell}<td class="mx-l">{"★ " if star else ""}{e(label)}</td>'
                        f'<td class="num mx-v">{cell}</td></tr>')
    tbl = f'''<div class="tbl-wrap"><table class="matrix"><tr><th colspan="2">보장 항목</th><th class="num">가입금액(합계)</th></tr>{"".join(rows)}</table></div>'''
    return tbl


@app.get("/customers/{cid}/matrix", response_class=HTMLResponse)
def matrix(request: Request, cid: int):
    pid = planner_id(request)
    c = get_customer_or_404(pid, cid)
    log(request, pid, "view_report", f"고객#{cid}")
    me = store.get_planner(pid)
    policies = store.list_policies(pid, cid)
    a = analysis.analyze(c, policies)
    head = f"""<div class="card"><div class="row between"><h1>보장분석 보고서</h1></div>
<p><b>{e(c.get('name') or '')}</b> 님 · {ymd(c.get('birth_date'))} · {e(c.get('gender') or '')} · 보험나이 {a['age'] if a['age'] is not None else '-'}세<br>
<span class="muted">작성일 {datetime.now():%Y.%m.%d} · 담당 {e(me['name'])} · 유지계약 {a['active_count']}건 · 월 보험료 합계 {won(a['monthly_premium'])}</span></p></div>"""
    return page(f"{c.get('name') or '고객'} 보장분석표", f"""
<div class="row between no-print" style="margin-bottom:12px"><span class="muted">★ 는 핵심 보장. 인쇄 → 'PDF로 저장'으로 받을 수 있습니다.</span>
<button onclick="print()">인쇄 / PDF 저장</button></div>
{head}
<div class="card">{matrix_html(c, policies)}
<p class="muted">빈 칸('-')은 해당 보장이 없다는 뜻입니다. 금액은 유지 중인 계약의 합계이며, 상담용 참고 자료입니다. 정확한 내용은 증권·약관을 따릅니다.</p></div>""")


# 후유장해 보험금 계산기: 가입금액 × 지급률(%). 계산은 브라우저에서(서버 전송 없음).
# 부위·상태별 참고 지급률 요약 (약관 장해분류표 기준, 실제는 약관·판정 확인 필요)
DISABILITY_TABLE = {
    "눈": [("두 눈이 멀었을 때", 100), ("한 눈이 멀었을 때", 50), ("한 눈 교정시력 0.02 이하", 35),
           ("한 눈 교정시력 0.1 이하", 15), ("한 눈의 뚜렷한 운동장해", 10)],
    "귀": [("두 귀의 청력을 완전히 잃었을 때", 80), ("한 귀의 청력을 완전히 잃었을 때", 25),
           ("한 귀의 청력에 심한 장해(70dB 이상)", 15)],
    "코": [("코의 호흡기능을 완전히 잃었을 때", 15), ("코의 후각기능을 완전히 잃었을 때", 5)],
    "씹고 말하기": [("씹는기능과 말하는기능을 완전히 잃었을 때", 100), ("씹는기능을 완전히 잃었을 때", 80),
                 ("말하는기능을 완전히 잃었을 때", 80), ("씹거나 말하는기능에 뚜렷한 장해", 40), ("치아 5개 이상 결손", 5)],
    "외모(흉터)": [("외모에 뚜렷한 추상(흉터)", 15), ("외모에 약간의 추상(흉터)", 5)],
    "척추(등뼈)": [("척추에 심한 운동장해", 40), ("뚜렷한 운동장해", 30), ("약간의 운동장해", 10),
                ("척추에 심한 기형", 50), ("뚜렷한 기형", 30), ("약간의 기형", 15)],
    "팔": [("두 팔을 손목 이상에서 잃음", 100), ("한 팔을 손목 이상에서 잃음", 60),
          ("한 팔 3대 관절 중 1관절 완전 강직", 30), ("한 팔에 뚜렷한 장해", 20), ("한 팔에 약간의 장해", 5)],
    "다리": [("두 다리를 발목 이상에서 잃음", 100), ("한 다리를 발목 이상에서 잃음", 60),
            ("한 다리 3대 관절 중 1관절 완전 강직", 30), ("한 다리에 뚜렷한 장해", 20), ("한 다리에 약간의 장해", 5),
            ("한 다리가 5cm 이상 짧아짐", 30)],
    "손가락": [("한 손 다섯 손가락 모두 잃음", 55), ("한 손 엄지손가락을 잃음", 15), ("한 손 엄지손가락 기능장해", 10),
             ("엄지 외 손가락 1개를 잃음", 10)],
    "발가락": [("한 발 다섯 발가락 모두 잃음", 30), ("한 발 엄지발가락을 잃음", 8), ("엄지 외 발가락 1개를 잃음", 5)],
    "신경계·정신": [("일상생활 기본동작에 완전 제한(항상 간호)", 100), ("뚜렷한 제한", 40), ("약간의 제한", 10)],
    "흉복부장기": [("흉복부 장기에 심한 장해", 75), ("뚜렷한 장해", 50), ("약간의 장해", 15),
                ("비장 또는 신장을 잃음", 15)],
    "체간골": [("어깨뼈·골반뼈 등에 뚜렷한 기형", 15)],
}


@app.get("/tools/disability", response_class=HTMLResponse)
def disability_calc(request: Request, amount: int = 0):
    planner_id(request)
    import json as _json
    table_js = _json.dumps(DISABILITY_TABLE, ensure_ascii=False)
    init_man = max(0, amount // 10000) or 3000
    body = f"""
<p class="no-print"><a href="/">← 홈</a></p>
<div class="card"><h1>후유장해 보험금 계산기</h1>
<p class="muted" style="margin-top:-4px">가입금액 × 지급률(%) = 예상 보험금. 비례 지급형 담보의 단순 계산이며, 실제 지급 여부·금액은 약관과 장해 판정 결과를 확인해야 합니다.</p></div>

<div class="card"><h2>① 계산 조건</h2>
<label>후유장해 가입금액 (만원)</label>
<div class="row"><input type="number" id="amt" value="{init_man}" min="0" style="max-width:160px"> <span class="muted">만원</span>
<button type="button" class="btn-ghost btn-sm" onclick="setAmt(3000)">3천만원</button>
<button type="button" class="btn-ghost btn-sm" onclick="setAmt(5000)">5천만원</button>
<button type="button" class="btn-ghost btn-sm" onclick="setAmt(10000)">1억원</button></div>

<label style="margin-top:14px">부위·상태로 지급률 선택 (참고)</label>
<div class="row"><select id="part" onchange="fillStates()" style="max-width:200px"><option value="">부위 선택</option></select>
<select id="state" onchange="useState()" style="flex:1;min-width:200px"><option value="">부위를 먼저 선택</option></select></div>
<p class="muted">약관 장해분류표 요약입니다. 가입한 계약의 약관과 의학적 판정기준을 확인하세요.</p>

<label>장해율(지급률) %</label>
<div class="row"><input type="number" id="rate" value="15" min="0" max="100" step="0.1" style="max-width:120px"> <span class="muted">%</span>
<span id="quick"></span></div>
</div>

<div class="card"><h2>② 예상 보험금</h2>
<div style="font-size:30px;font-weight:800" id="result">-</div>
<div class="muted" id="resultWon"></div>
<div style="margin-top:8px" id="how"></div>
<p class="row no-print" style="margin-top:10px"><button type="button" class="btn-sm" onclick="copyText()">결과 문구 복사</button></p>
<p class="muted" id="copied" style="display:none">복사됐습니다.</p>
</div>

<div class="card"><h2>③ 가입금액 3개 비교 (현재 지급률 기준)</h2>
<div class="grid" id="cmp"></div></div>

<div class="card"><h2>④ 상담 설명 문구</h2>
<p id="script" class="note-body" style="background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px"></p></div>

<script>
var TABLE = {table_js};
function won(n){{ return (Math.floor(n)).toLocaleString('ko-KR') + '원'; }}
function man(n){{
  n = Math.floor(n);
  if(n>=100000000 && n%100000000===0) return (n/100000000)+'억원';
  if(n>=10000) return (Math.floor(n/10000)).toLocaleString('ko-KR')+'만원';
  return n.toLocaleString('ko-KR')+'원';
}}
function amtWon(){{ return (parseFloat(document.getElementById('amt').value)||0)*10000; }}
function rate(){{ return Math.min(100, Math.max(0, parseFloat(document.getElementById('rate').value)||0)); }}
function setAmt(m){{ document.getElementById('amt').value=m; calc(); }}
function setRate(r){{ document.getElementById('rate').value=r; calc(); }}
function fillParts(){{
  var p=document.getElementById('part');
  Object.keys(TABLE).forEach(function(k){{ var o=document.createElement('option'); o.value=k; o.textContent=k; p.appendChild(o); }});
}}
function fillStates(){{
  var part=document.getElementById('part').value, s=document.getElementById('state');
  s.innerHTML='';
  if(!part){{ s.innerHTML='<option value="">부위를 먼저 선택</option>'; return; }}
  var o0=document.createElement('option'); o0.value=''; o0.textContent='상태 선택'; s.appendChild(o0);
  TABLE[part].forEach(function(it){{ var o=document.createElement('option'); o.value=it[1]; o.textContent=it[0]+' ('+it[1]+'%)'; s.appendChild(o); }});
}}
function useState(){{ var v=document.getElementById('state').value; if(v){{ document.getElementById('rate').value=v; calc(); }} }}
function quickBtns(){{
  var q=document.getElementById('quick');
  [3,5,10,15,30,50,80,100].forEach(function(r){{ var b=document.createElement('button'); b.type='button'; b.className='btn-ghost btn-sm'; b.textContent=r+'%'; b.onclick=function(){{setRate(r);}}; q.appendChild(b); }});
}}
function calc(){{
  var a=amtWon(), r=rate(), pay=Math.floor(a*r/100);
  document.getElementById('result').textContent = man(pay);
  document.getElementById('resultWon').textContent = won(pay);
  document.getElementById('how').innerHTML = '<span class="muted">계산: '+man(a)+' × '+r+'% = '+won(pay)+'</span>';
  var cmp=document.getElementById('cmp'); cmp.innerHTML='';
  [3000,5000,10000].forEach(function(m){{
    var pv=Math.floor(m*10000*r/100);
    var d=document.createElement('div'); d.className='stat';
    d.innerHTML='<span class="muted">가입금액 '+man(m*10000)+'</span><b>'+man(pv)+'</b><span class="muted">'+won(pv)+'</span>';
    cmp.appendChild(d);
  }});
  document.getElementById('script').textContent =
    '가입금액 '+man(a)+'에 지급률 '+r+'%를 적용하면 예상 보험금은 '+won(pay)+'입니다. '+
    '같은 지급률이라도 가입금액에 따라 금액이 달라집니다. 실제 지급 여부와 금액은 가입하신 계약의 약관과 장해 판정 결과를 확인해야 합니다.';
}}
function copyText(){{
  var t=document.getElementById('script').textContent;
  function ok(){{ var c=document.getElementById('copied'); c.style.display='block'; setTimeout(function(){{c.style.display='none';}},1500); }}
  if(navigator.clipboard&&navigator.clipboard.writeText){{ navigator.clipboard.writeText(t).then(ok,function(){{ok();}}); }} else {{ ok(); }}
}}
['amt','rate'].forEach(function(id){{ document.getElementById(id).addEventListener('input', calc); }});
fillParts(); quickBtns(); calc();
</script>"""
    return page("후유장해 계산기", body)


@app.post("/customers/{cid}/info")
def save_info(request: Request, cid: int, name: str = Form(""), birth_date: str = Form(""), gender: str = Form(""),
              phone: str = Form(""), address: str = Form(""), memo: str = Form("")):
    pid = planner_id(request)
    c = get_customer_or_404(pid, cid)
    fields = customer_fields(name, birth_date, gender, phone, address, memo)
    fields["notepad"] = c.get("notepad", "")  # 자유 메모는 이 폼에서 건드리지 않음
    store.save_customer(pid, fields, cid)
    log(request, pid, "edit_customer", f"고객#{cid}")
    flash(request, "고객 정보를 저장했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@app.post("/customers/{cid}/notepad")
def save_notepad(request: Request, cid: int, notepad: str = Form("")):
    pid = planner_id(request)
    c = get_customer_or_404(pid, cid)
    c["notepad"] = notepad.strip()[:20000]
    store.save_customer(pid, {k: c.get(k) for k in
                              ("name", "birth_date", "gender", "phone", "address", "memo", "notepad")}, cid)
    log(request, pid, "edit_notepad", f"고객#{cid}")
    flash(request, "메모를 저장했습니다.")
    return RedirectResponse(f"/customers/{cid}#notepad", status_code=303)


@app.post("/customers/{cid}/delete")
def delete_customer(request: Request, cid: int):
    pid = planner_id(request)
    get_customer_or_404(pid, cid)
    store.delete_customer(pid, cid)
    log(request, pid, "delete_customer", f"고객#{cid}")
    flash(request, "고객 정보를 삭제했습니다.")
    return RedirectResponse("/", status_code=303)


# ── 계약 수정/삭제 ────────────────────────────────────────────────────────────
@app.get("/policies/{pid_}/edit", response_class=HTMLResponse)
def edit_policy_form(request: Request, pid_: int, error: str = ""):
    pid = planner_id(request)
    p = store.get_policy(pid, pid_)
    if not p:
        raise HTTPException(404)
    data = {k: v for k, v in p.items() if k not in ("id", "customer_id")}
    err = f'<p class="under">{e(error)}</p>' if error else ""
    return page("계약 수정", f"""<p><a href="/customers/{p['customer_id']}">← 돌아가기</a></p>
<form method="post" action="/policies/{pid_}/edit" class="card"><h1>계약 수정</h1>{err}
<p class="muted">잘못 읽힌 값을 고쳐 주세요. 금액은 원 단위 숫자, 날짜는 YYYYMMDD,
category 는 다음 중 하나: {e(', '.join(extractor.CATEGORIES))}</p>
<textarea name="data">{e(json.dumps(data, ensure_ascii=False, indent=2))}</textarea>
<p><button>저장</button></p></form>""")


@app.post("/policies/{pid_}/edit")
def edit_policy(request: Request, pid_: int, data: str = Form(...)):
    pid = planner_id(request)
    p = store.get_policy(pid, pid_)
    if not p:
        raise HTTPException(404)
    try:
        obj = json.loads(data)
        if not isinstance(obj, dict) or not isinstance(obj.get("coverages", []), list):
            raise ValueError
    except ValueError:
        return RedirectResponse(f"/policies/{pid_}/edit?error={quote('JSON 형식이 올바르지 않습니다.')}", status_code=303)
    store.update_policy(pid, pid_, extractor.mask_rrn(obj))
    log(request, pid, "edit_policy", f"계약#{pid_}")
    flash(request, "계약 정보를 수정했습니다.")
    return RedirectResponse(f"/customers/{p['customer_id']}", status_code=303)


@app.post("/policies/{pid_}/pension")
def save_pension(request: Request, pid_: int, current_value: str = Form(""), as_of: str = Form(""), rate: str = Form(""),
                 acq_pct: str = Form(""), mgmt_pct: str = Form(""), credit_pct: str = Form("13.2"),
                 paid_count: str = Form(""), paid_amount: str = Form("")):
    pid = planner_id(request)
    p = store.get_policy(pid, pid_)
    if not p:
        raise HTTPException(404)
    cfg = {"current_value": _num(current_value) or None, "as_of": as_of or None, "rate": _num(rate) or None,
           "credit_pct": 16.5 if _num(credit_pct) == 16.5 else 13.2,
           "paid_count": int(_num(paid_count)) or None, "paid_amount": _num(paid_amount) or None}
    for k, val in (("acq_pct", acq_pct), ("mgmt_pct", mgmt_pct)):
        if val.strip():
            cfg[k] = _num(val)
    data = {k: v for k, v in p.items() if k not in ("id", "customer_id")}
    data["pension"] = cfg
    store.update_policy(pid, pid_, data)
    log(request, pid, "manage_policy", f"계약#{pid_} 연금")
    flash(request, "연금 수익률 계산값을 저장했습니다.")
    return RedirectResponse(f"/customers/{p['customer_id']}", status_code=303)


@app.post("/policies/{pid_}/manage")
def save_manage(request: Request, pid_: int, owner: str = Form(""), pay_status: str = Form(""), pay_day: str = Form(""),
                checked_at: str = Form(""), memo: str = Form("")):
    pid = planner_id(request)
    p = store.get_policy(pid, pid_)
    if not p:
        raise HTTPException(404)
    day = "".join(ch for ch in pay_day if ch.isdigit())
    data = {k: v for k, v in p.items() if k not in ("id", "customer_id")}
    data["manage"] = {
        "owner": owner if owner in analysis.OWNERS else "",
        "pay_status": pay_status if pay_status in analysis.PAY_STATUSES else "",
        "pay_day": int(day) if day and 1 <= int(day) <= 31 else None,
        "checked_at": checked_at or None,
        "memo": memo.strip()[:200],
    }
    store.update_policy(pid, pid_, data)
    log(request, pid, "manage_policy", f"계약#{pid_}")
    flash(request, "관리 정보를 저장했습니다.")
    return RedirectResponse(f"/customers/{p['customer_id']}", status_code=303)


@app.post("/policies/{pid_}/delete")
def delete_policy(request: Request, pid_: int):
    pid = planner_id(request)
    cid = store.delete_policy(pid, pid_)
    if cid is None:
        raise HTTPException(404)
    log(request, pid, "delete_policy", f"계약#{pid_}")
    flash(request, "계약을 삭제했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)
