"""
보험증권 보장분석 - 설계사용 웹 서비스

  설계사가 고객 증권(PDF/사진)을 올리면 Claude 가 읽어서
  고객 정보 · 가입 현황 · 보장 합산 · 체크 포인트를 정리하고 고객별로 저장한다.

실행: uvicorn app:app --host 0.0.0.0 --port 8000
"""

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
REQUIRE_2FA = os.environ.get("REQUIRE_2FA", "1") == "1"
MAX_FILES = 20
MAX_TOTAL_BYTES = 30 * 1024 * 1024
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
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb;
        --danger:#dc2626; --warn-bg:#fef2f2; --warn:#b91c1c; --info-bg:#eff6ff; --info:#1d4ed8; --ok:#15803d; }
@media (prefers-color-scheme: dark) { :root { --bg:#111317; --card:#1b1e24; --text:#e8eaed; --muted:#9aa0a6; --line:#2d3139;
        --accent:#5b8def; --danger:#f06262; --warn-bg:#3a1d1d; --warn:#fca5a5; --info-bg:#1b2a44; --info:#93b4f5; --ok:#4ade80; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 64px; }
a { color:var(--accent); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
h1 { font-size:22px; margin:0 0 12px; } h2 { font-size:17px; margin:0 0 12px; } h3 { font-size:15px; margin:0 0 6px; }
label { display:block; margin:10px 0 4px; font-weight:600; font-size:14px; }
input[type=text], input[type=password], textarea { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px;
  background:var(--bg); color:var(--text); font-size:15px; font-family:inherit; }
textarea { font-family:ui-monospace, monospace; font-size:13px; min-height:420px; }
textarea.note { font-family:inherit; font-size:15px; min-height:110px; }
select, input[type=date] { padding:9px 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--text); font-size:15px; font-family:inherit; }
.note-item { border-top:1px solid var(--line); padding:12px 0; } .note-item:first-of-type { border-top:0; }
.note-body { white-space:pre-wrap; margin:6px 0; }
.tag { display:inline-block; white-space:nowrap; padding:1px 8px; border-radius:99px; font-size:12px; background:var(--line); }
.overdue { color:var(--warn); font-weight:600; }
.tag.warn-tag { background:var(--warn-bg); color:var(--warn); font-weight:600; }
.tag.ok-tag { background:#dcfce7; color:#166534; }
.manage { margin-top:10px; border-top:1px dashed var(--line); padding-top:10px; } .done { text-decoration:line-through; color:var(--muted); }
button, .btn { display:inline-block; white-space:nowrap; padding:9px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff;
  font-size:14px; cursor:pointer; text-decoration:none; }
.btn-ghost { background:transparent; color:var(--accent); border:1px solid var(--line); }
.btn-danger { background:var(--danger); } .btn-sm { padding:5px 10px; font-size:13px; }
.muted { color:var(--muted); font-size:13px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.between { justify-content:space-between; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.stat { border:1px solid var(--line); border-radius:10px; padding:12px; }
.stat b { display:block; font-size:20px; }
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
td.nowrap, th.nowrap { white-space:nowrap; } td.small { font-size:13px; }
td.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
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
    "view_customer": "고객 조회", "view_report": "리포트 조회", "create_customer": "고객 등록",
    "edit_customer": "고객 정보 수정", "delete_customer": "고객 삭제", "upload": "증권 업로드",
    "edit_policy": "계약 수정", "manage_policy": "계약 관리정보 수정", "delete_policy": "계약 삭제",
    "add_note": "상담 기록 작성", "edit_note": "상담 기록 수정", "delete_note": "상담 기록 삭제",
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
{"" if customer_id else "같은 이름·생년월일 고객이 있으면 그 고객에 자동으로 추가됩니다."}</p></form>"""


def find_customer(pid: int, insured: dict) -> int | None:
    name, birth = insured.get("name"), insured.get("birth_date")
    if not (name and birth):
        return None
    for c in store.list_customers(pid):
        if c.get("name") == name and c.get("birth_date") == birth:
            return c["id"]
    return None


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

    try:
        result = await run_in_threadpool(extractor.extract, payload)
    except extractor.ExtractError as ex:
        flash(request, str(ex))
        return RedirectResponse(back, status_code=303)

    insured = result.get("insured") or {}
    policies = result.get("policies") or []
    if not policies:
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
{take_flash(request)}
{todo_html(pid, notes, all_pol, names)}
<div class="card"><h2>새 고객 증권 분석</h2>{upload_form()}</div>
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

    rows = []
    for s in a["summary"]:
        if not s["amount"]:
            continue
        bm = s["benchmark"]
        state = "" if not bm else (f'<span class="enough">충분</span>' if s["amount"] >= bm else f'<span class="under">부족 ({won(bm)} 기준)</span>')
        note = SUMMARY_NOTES.get(s["category"], "")
        note = f"<br><span class='muted'>{note}</span>" if note else ""
        rows.append(f"""<tr><td class="nowrap">{e(s['category'])}{note}</td><td class="num">{won(s['amount'])}</td><td class="nowrap">{state}</td>
<td class="muted small">{e(', '.join(s['sources']))}</td></tr>""")
    for cat in a["missing_benchmarks"]:
        rows.append(f'<tr><td class="nowrap">{e(cat)}</td><td class="num under">없음</td><td class="nowrap"><span class="under">미가입 ({won(analysis.BENCHMARKS[cat])} 기준)</span></td><td></td></tr>')
    summary = f'<div class="tbl-wrap"><table><tr><th>보장</th><th>합계</th><th>판단</th><th>가입 내역</th></tr>{"".join(rows)}</table></div>'

    checks = "".join(f'<div class="check {lvl}"><b>{e(t)}</b><span>{e(d)}</span></div>' for lvl, t, d in a["checks"]) \
        or '<p class="enough">특별히 확인할 항목이 없습니다.</p>'

    return f"""<div class="card"><h2>요약</h2>{stats}</div>
<div class="card"><h2>체크 포인트</h2>{checks}</div>
<div class="card"><h2>보장 합산</h2>{summary}
<p class="muted">판단 기준은 상담용 참고값입니다. 고객의 소득·가족력·기존 계획에 따라 달라질 수 있습니다.</p></div>"""


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
    manage_html = manage_form(p) if editable else ""
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
    return f"""<form method="post" action="{action}">
<div class="row"><input type="date" name="date" value="{e(n.get('date') or date.today().isoformat())}" required>
<select name="kind">{kind_opts}</select></div>
<label>상담 내용</label><textarea class="note" name="content" required placeholder="고객 요청, 관심 상품, 가족 상황, 다음에 말할 것 등">{e(n.get('content') or '')}</textarea>
<div class="row" style="margin-top:8px"><label style="margin:0">다음 연락일</label><input type="date" name="next_date" value="{e(n.get('next_date') or '')}">
<input type="text" name="next_action" value="{e(n.get('next_action') or '')}" placeholder="할 일 (예: 암보험 설계안 전달)" style="flex:1;min-width:200px"></div>
<p><button class="btn-sm">{button}</button></p></form>"""


def notes_html(cid: int, notes: list[dict]) -> str:
    today = date.today().isoformat()
    items = []
    for n in notes:
        follow = ""
        if n.get("next_date"):
            cls = "done" if n.get("done") else ("overdue" if n["next_date"] < today else "")
            toggle = "다시 열기" if n.get("done") else "완료"
            follow = f"""<div class="row"><span class="{cls}">다음 연락 {e(n['next_date'])} · {e(n.get('next_action') or '')}</span>
<form method="post" action="/notes/{n['id']}/done"><input type="hidden" name="back" value="/customers/{cid}"><button class="btn-ghost btn-sm">{toggle}</button></form></div>"""
        items.append(f"""<div class="note-item"><div class="row between"><span><b>{e(n.get('date') or '')}</b> <span class="tag">{e(n.get('kind') or '')}</span></span>
<div class="row no-print"><a class="btn btn-ghost btn-sm" href="/notes/{n['id']}/edit">수정</a>
<form method="post" action="/notes/{n['id']}/delete" onsubmit="return confirm('이 상담 기록을 삭제할까요?')"><button class="btn-danger btn-sm">삭제</button></form></div></div>
<div class="note-body">{e(n.get('content') or '')}</div>{follow}</div>""")
    listing = "".join(items) or '<p class="muted">아직 상담 기록이 없습니다.</p>'
    return f"""<div class="card no-print"><h2>상담 기록 ({len(notes)})</h2>
<details {"open" if not notes else ""}><summary>+ 새 상담 기록 쓰기</summary>{note_form(f"/customers/{cid}/notes", {}, "기록 저장")}</details>
<div style="margin-top:12px">{listing}</div></div>"""


def note_fields(date_: str, kind: str, content: str, next_date: str, next_action: str, done: bool = False) -> dict:
    return {"date": date_, "kind": kind if kind in KINDS else "기타", "content": content.strip(),
            "next_date": next_date or None, "next_action": next_action.strip(), "done": done}


@app.post("/customers/{cid}/notes")
def add_note(request: Request, cid: int, date: str = Form(...), kind: str = Form("기타"), content: str = Form(...),
             next_date: str = Form(""), next_action: str = Form("")):
    pid = planner_id(request)
    get_customer_or_404(pid, cid)
    store.add_note(pid, cid, note_fields(date, kind, content, next_date, next_action))
    log(request, pid, "add_note", f"고객#{cid}")
    flash(request, "상담 기록을 저장했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


@app.get("/notes/{nid}/edit", response_class=HTMLResponse)
def edit_note_form(request: Request, nid: int):
    n = store.get_note(planner_id(request), nid)
    if not n:
        raise HTTPException(404)
    return page("상담 기록 수정", f"""<p><a href="/customers/{n['customer_id']}">← 돌아가기</a></p>
<div class="card"><h1>상담 기록 수정</h1>{note_form(f"/notes/{nid}/edit", n, "저장")}</div>""")


@app.post("/notes/{nid}/edit")
def edit_note(request: Request, nid: int, date: str = Form(...), kind: str = Form("기타"), content: str = Form(...),
              next_date: str = Form(""), next_action: str = Form("")):
    pid = planner_id(request)
    n = store.get_note(pid, nid)
    if not n:
        raise HTTPException(404)
    store.update_note(pid, nid, note_fields(date, kind, content, next_date, next_action, n.get("done", False)))
    log(request, pid, "edit_note", f"기록#{nid}")
    flash(request, "상담 기록을 수정했습니다.")
    return RedirectResponse(f"/customers/{n['customer_id']}", status_code=303)


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
<div class="row no-print"><a class="btn" href="/customers/{cid}/report" target="_blank">고객용 리포트</a>
<form method="post" action="/customers/{cid}/delete" onsubmit="return confirm('이 고객과 모든 계약 정보를 삭제할까요? 되돌릴 수 없습니다.')">
<button class="btn-danger btn-sm">고객 삭제</button></form></div></div>
{take_flash(request)}
{info}
{notes_html(cid, store.list_notes(pid, cid))}
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
<p class="muted">이 리포트는 증권 내용을 정리한 참고 자료이며, 정확한 보장 내용은 각 보험사 약관을 따릅니다.</p>""")


@app.post("/customers/{cid}/info")
def save_info(request: Request, cid: int, name: str = Form(""), birth_date: str = Form(""), gender: str = Form(""),
              phone: str = Form(""), address: str = Form(""), memo: str = Form("")):
    pid = planner_id(request)
    get_customer_or_404(pid, cid)
    store.save_customer(pid, customer_fields(name, birth_date, gender, phone, address, memo), cid)
    log(request, pid, "edit_customer", f"고객#{cid}")
    flash(request, "고객 정보를 저장했습니다.")
    return RedirectResponse(f"/customers/{cid}", status_code=303)


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
