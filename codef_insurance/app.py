"""
설계사용 보험 계약정보 조회 웹 서비스

  설계사: 로그인 -> 고객별 조회 링크 생성 -> 고객에게 링크 전달 -> 결과 확인
  고객:   링크 접속 -> 동의 -> 본인 정보 입력 -> PASS/SMS 인증 -> 완료

보안 원칙
  - 고객의 내보험다보여 비밀번호는 DB 에 저장하지 않는다 (RSA 암호화된 값만 인증 진행 중 메모리에 잠깐 보관).
  - 조회 결과와 개인정보는 DATA_KEY 로 암호화해 SQLite 에 저장한다.
  - 동의 내역(문구 버전, 시각, IP)을 함께 기록하고, 설계사가 언제든 고객 데이터를 삭제할 수 있다.

실행: uvicorn app:app --host 0.0.0.0 --port 8000
"""

import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()
import codef  # noqa: E402  (.env 로드 후 import)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "insurance.db"))
PLANNER_PASSWORD = os.environ.get("PLANNER_PASSWORD", "")
DATA_KEY = os.environ.get("DATA_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PLANNER_NAME = os.environ.get("PLANNER_NAME", "담당 설계사")
RETENTION_TEXT = os.environ.get("CONSENT_RETENTION", "동의 철회 시까지 (철회 요청 시 지체 없이 파기)")
INVITE_TTL = 7 * 24 * 3600     # 조회 링크 유효기간
PENDING_TTL = 5 * 60           # 추가인증 대기 시간

CONSENT_VERSION = "v1"
CONSENT_TEXT = f"""[개인(신용)정보 수집·이용 동의]
1. 수집·이용 목적: 보험 가입내역 확인 및 보장 분석, 보험 상담
2. 수집 항목: 이름, 생년월일, 휴대폰 번호, 통신사, 보험 계약정보(내보험다보여 조회 결과)
   ※ 내보험다보여 아이디·비밀번호는 조회에만 사용하며 저장하지 않습니다.
3. 보유·이용 기간: {RETENTION_TEXT}
4. 수집·이용 주체: {PLANNER_NAME}
5. 동의를 거부할 수 있으며, 거부 시 보험 가입내역 조회 서비스를 이용할 수 없습니다."""
MARKETING_TEXT = "[선택] 보험 상품 안내 등 마케팅 목적의 연락에 동의합니다. (거부해도 조회는 가능)"

if not DATA_KEY:
    raise RuntimeError("DATA_KEY 환경변수를 설정하세요. 생성: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
if not PLANNER_PASSWORD:
    raise RuntimeError("PLANNER_PASSWORD 환경변수를 설정하세요.")
fernet = Fernet(DATA_KEY.encode())

app = FastAPI(title="보험 계약정보 조회")

# 설계사 로그인 세션, 고객 추가인증 대기 상태 (서버 재시작 시 초기화됨)
sessions: dict[str, float] = {}
pending: dict[str, dict] = {}


# ── DB ────────────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete = ON")  # 삭제한 고객 데이터가 파일에 남지 않도록 덮어씀
    return conn


def init_db() -> None:
    with closing(db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,           -- 설계사가 붙인 메모 (예: A고객 상담)
                status TEXT NOT NULL,          -- invited / done
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                personal_enc BLOB,             -- 이름/생년월일/연락처 (암호화)
                result_enc BLOB,               -- CODEF 응답 data (암호화)
                consent_version TEXT,
                consent_hash TEXT,
                consent_marketing INTEGER,
                consent_at INTEGER,
                consent_ip TEXT,
                consent_ua TEXT
            )""")


init_db()


def enc(obj) -> bytes:
    return fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode())


def dec(blob):
    return json.loads(fernet.decrypt(blob)) if blob else None


def get_customer_by_token(token: str):
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM customers WHERE token = ?", (token,)).fetchone()


# ── 공통 HTML ─────────────────────────────────────────────────────────────────
e = html.escape

STYLE = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#6b7280; --line:#e5e7eb; --accent:#2563eb; --danger:#dc2626; }
@media (prefers-color-scheme: dark) { :root { --bg:#111317; --card:#1b1e24; --text:#e8eaed; --muted:#9aa0a6; --line:#2d3139; --accent:#5b8def; --danger:#f06262; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
main { max-width:860px; margin:0 auto; padding:24px 16px 64px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
h1 { font-size:22px; margin:0 0 16px; } h2 { font-size:17px; margin:0 0 12px; }
label { display:block; margin:10px 0 4px; font-weight:600; font-size:14px; }
input[type=text], input[type=password], input[type=tel], select { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--text); font-size:15px; }
button, .btn { display:inline-block; padding:10px 16px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-size:15px; cursor:pointer; text-decoration:none; }
.btn-danger { background:var(--danger); } .btn-sm { padding:6px 10px; font-size:13px; }
.muted { color:var(--muted); font-size:13px; }
.err { color:var(--danger); font-weight:600; }
pre.consent { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:12px; font-size:13px; max-height:220px; overflow:auto; }
.check { display:flex; gap:8px; align-items:flex-start; font-weight:400; }
.tbl-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.badge { display:inline-block; padding:2px 8px; border-radius:99px; font-size:12px; background:var(--line); }
.badge.done { background:#16a34a; color:#fff; }
code { word-break:break-all; font-size:13px; }
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{e(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>""", status_code=status)


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def fmt_date(s) -> str:
    s = str(s or "")
    return f"{s[:4]}.{s[4:6]}.{s[6:8]}" if len(s) == 8 and s.isdigit() else (s or "-")


def fmt_won(v) -> str:
    try:
        return f"{int(float(v)):,}원"
    except (TypeError, ValueError):
        return e(str(v)) if v not in (None, "") else "-"


# ── 설계사 인증 ───────────────────────────────────────────────────────────────
def is_planner(request: Request) -> bool:
    sid = request.cookies.get("sid", "")
    return sid in sessions and sessions[sid] > time.time()


def require_planner(request: Request) -> None:
    if not is_planner(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    msg = '<p class="err">비밀번호가 올바르지 않습니다.</p>' if error else ""
    return page("설계사 로그인", f"""<div class="card"><h1>설계사 로그인</h1>{msg}
<form method="post" action="/login"><label>비밀번호</label><input type="password" name="password" required autofocus>
<p><button>로그인</button></p></form></div>""")


@app.post("/login")
def login(password: str = Form(...)):
    if not hmac.compare_digest(password.encode(), PLANNER_PASSWORD.encode()):
        time.sleep(1)
        return RedirectResponse("/login?error=1", status_code=303)
    sid = secrets.token_urlsafe(32)
    sessions[sid] = time.time() + 12 * 3600
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sid", sid, httponly=True, samesite="strict", secure=PUBLIC_BASE_URL.startswith("https"))
    return resp


@app.post("/logout")
def logout(request: Request):
    sessions.pop(request.cookies.get("sid", ""), None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ── 설계사 화면 ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, new: str = ""):
    require_planner(request)
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM customers ORDER BY id DESC").fetchall()

    notice = ""
    if new:
        c = get_customer_by_token(new)
        if c:
            link = f"{PUBLIC_BASE_URL}/c/{c['token']}"
            notice = f"""<div class="card"><h2>조회 링크가 생성됐습니다</h2>
<p>아래 링크를 <b>{e(c['label'])}</b>님께 보내주세요. (유효기간 7일, 1회용)</p>
<p><code>{e(link)}</code></p></div>"""

    trs = []
    now = time.time()
    for r in rows:
        if r["status"] == "done":
            p = dec(r["personal_enc"]) or {}
            badge = '<span class="badge done">조회완료</span>'
            who = f"{e(p.get('userName', ''))} <span class='muted'>{e(p.get('birthDate', ''))}</span>"
            action = f'<a class="btn btn-sm" href="/customers/{r["id"]}">결과 보기</a>'
        else:
            expired = now - r["created_at"] > INVITE_TTL
            badge = f'<span class="badge">{"링크만료" if expired else "대기중"}</span>'
            who = '<span class="muted">-</span>'
            action = "" if expired else f'<code>{e(PUBLIC_BASE_URL)}/c/{e(r["token"])}</code>'
        trs.append(f"""<tr><td>{e(r['label'])}</td><td>{who}</td><td>{badge}</td>
<td class="muted">{fmt_ts(r['created_at'])}<br>{fmt_ts(r['completed_at'])}</td><td>{action}</td>
<td><form method="post" action="/customers/{r['id']}/delete" onsubmit="return confirm('이 고객의 모든 데이터를 삭제할까요? 되돌릴 수 없습니다.')">
<button class="btn-danger btn-sm">삭제</button></form></td></tr>""")

    table = (f"""<div class="tbl-wrap"><table><tr><th>메모</th><th>고객</th><th>상태</th><th>생성 / 완료</th><th></th><th></th></tr>
{''.join(trs)}</table></div>""" if trs else '<p class="muted">아직 생성한 조회 링크가 없습니다.</p>')

    return page("고객 보험 조회", f"""
<div class="row" style="justify-content:space-between"><h1>고객 보험 조회</h1>
<form method="post" action="/logout"><button class="btn-sm">로그아웃</button></form></div>
{notice}
<div class="card"><h2>새 조회 링크 만들기</h2>
<form method="post" action="/invites" class="row"><input type="text" name="label" placeholder="고객 메모 (예: 홍길동 / 0101 상담)" required style="flex:1;min-width:200px">
<button>링크 생성</button></form></div>
<div class="card"><h2>고객 목록</h2>{table}</div>""")


@app.post("/invites")
def create_invite(request: Request, label: str = Form(...)):
    require_planner(request)
    token = secrets.token_urlsafe(24)
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO customers (token, label, status, created_at) VALUES (?, ?, 'invited', ?)",
                     (token, label.strip()[:100], int(time.time())))
    return RedirectResponse(f"/?new={token}", status_code=303)


@app.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(request: Request, cid: int):
    require_planner(request)
    with closing(db()) as conn:
        r = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if not r or r["status"] != "done":
        raise HTTPException(404)
    p = dec(r["personal_enc"]) or {}
    data = dec(r["result_enc"]) or {}

    contracts = data.get("resContractList") if isinstance(data, dict) else None
    if contracts:
        rows = "".join(f"""<tr><td>{e(str(c.get('resCompanyName', '-')))}</td><td>{e(str(c.get('resInsuranceName', '-')))}</td>
<td>{e(str(c.get('resContractStatus', '-')))}</td><td>{fmt_date(c.get('resContractStartDate'))} ~ {fmt_date(c.get('resContractEndDate'))}</td>
<td>{fmt_won(c.get('resMonthlyPremium'))}</td></tr>""" for c in contracts)
        summary = f"""<div class="tbl-wrap"><table><tr><th>보험사</th><th>상품명</th><th>상태</th><th>계약기간</th><th>보험료</th></tr>{rows}</table></div>"""
    else:
        summary = '<p class="muted">요약 표시용 필드(resContractList)를 찾지 못했습니다. 아래 원본 데이터를 확인하세요.</p>'

    return page(f"{p.get('userName', '')} 보험 계약정보", f"""
<p><a href="/">← 목록</a></p>
<div class="card"><h1>{e(p.get('userName', ''))} <span class="muted">{e(p.get('birthDate', ''))} · {e(p.get('phoneNo', ''))}</span></h1>
<p class="muted">메모: {e(r['label'])} · 조회일 {fmt_ts(r['completed_at'])}<br>
동의: {e(r['consent_version'] or '')} · {fmt_ts(r['consent_at'])} · IP {e(r['consent_ip'] or '')} · 마케팅 {'동의' if r['consent_marketing'] else '미동의'}</p></div>
<div class="card"><h2>계약 목록</h2>{summary}</div>
<div class="card"><h2>원본 데이터</h2><details><summary>펼치기</summary>
<pre class="consent" style="max-height:none">{e(json.dumps(data, ensure_ascii=False, indent=2))}</pre></details></div>""")


@app.post("/customers/{cid}/delete")
def delete_customer(request: Request, cid: int):
    require_planner(request)
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT token FROM customers WHERE id = ?", (cid,)).fetchone()
        if row:
            pending.pop(row["token"], None)
        conn.execute("DELETE FROM customers WHERE id = ?", (cid,))
    return RedirectResponse("/", status_code=303)


# ── 고객 화면 ─────────────────────────────────────────────────────────────────
def valid_invite(token: str):
    c = get_customer_by_token(token)
    if not c:
        return None, page("링크 오류", '<div class="card"><h1>유효하지 않은 링크입니다</h1><p>담당 설계사에게 문의해 주세요.</p></div>', 404)
    if c["status"] == "done":
        return None, page("조회 완료", '<div class="card"><h1>이미 조회가 완료됐습니다</h1><p>담당 설계사가 결과를 확인할 예정입니다.</p></div>')
    if time.time() - c["created_at"] > INVITE_TTL:
        return None, page("링크 만료", '<div class="card"><h1>링크가 만료됐습니다</h1><p>담당 설계사에게 새 링크를 요청해 주세요.</p></div>', 410)
    return c, None


def customer_form(token: str, error: str = "", v: dict | None = None) -> HTMLResponse:
    v = v or {}
    err = f'<p class="err">{e(error)}</p>' if error else ""
    tel_opts = "".join(f'<option value="{k}" {"selected" if v.get("telecom") == k else ""}>{n}</option>'
                       for k, n in (("skt", "SKT / SKT 알뜰폰"), ("kt", "KT / KT 알뜰폰"), ("lgu", "LG U+ / LG U+ 알뜰폰")))
    return page("보험 가입내역 조회", f"""
<div class="card"><h1>보험 가입내역 조회</h1>
<p>{e(PLANNER_NAME)}이(가) 보장 분석을 위해 요청한 조회입니다. 한국신용정보원 <b>내보험다보여</b>에 등록된 보험 계약정보를 조회합니다.</p>
<p class="muted">내보험다보여 계정이 없다면 먼저 <a href="https://cont.insure.or.kr" target="_blank" rel="noopener">cont.insure.or.kr</a>에서 회원가입해 주세요.</p></div>
<form method="post" action="/c/{e(token)}/start" class="card" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='조회 요청 중... (최대 1~2분)'">
{err}
<h2>1. 동의</h2>
<pre class="consent">{e(CONSENT_TEXT)}</pre>
<label class="check"><input type="checkbox" name="consent" value="1" required> [필수] 위 개인(신용)정보 수집·이용에 동의합니다.</label>
<label class="check"><input type="checkbox" name="marketing" value="1"> {e(MARKETING_TEXT)}</label>

<h2 style="margin-top:20px">2. 본인 정보</h2>
<label>이름</label><input type="text" name="userName" value="{e(v.get('userName', ''))}" required>
<label>생년월일 (8자리)</label><input type="tel" name="birthDate" value="{e(v.get('birthDate', ''))}" placeholder="19900101" pattern="\\d{{8}}" required>
<label>휴대폰 번호</label><input type="tel" name="phoneNo" value="{e(v.get('phoneNo', ''))}" placeholder="01012345678" required>
<label>통신사</label><select name="telecom">{tel_opts}</select>
<label>인증 방식</label><select name="authMethod"><option value="1">PASS 앱 인증</option><option value="0" {"selected" if v.get("authMethod") == "0" else ""}>문자(SMS) 인증</option></select>

<h2 style="margin-top:20px">3. 내보험다보여 로그인 정보</h2>
<p class="muted">조회에만 사용되며 저장되지 않고, 설계사에게도 보이지 않습니다.</p>
<label>아이디</label><input type="text" name="loginId" value="{e(v.get('loginId', ''))}" autocomplete="off" required>
<label>비밀번호</label><input type="password" name="loginPw" autocomplete="off" required>
<p style="margin-top:20px"><button>조회 요청</button></p>
</form>""")


def confirm_form(token: str, auth_method: str, error: str = "") -> HTMLResponse:
    err = f'<p class="err">{e(error)}</p>' if error else ""
    if auth_method == "1":
        guide = "<p>휴대폰에 온 <b>PASS 인증 요청을 승인</b>한 뒤 아래 버튼을 눌러 주세요.</p>"
        field = ""
    else:
        guide = "<p>문자로 받은 <b>인증번호</b>를 입력해 주세요.</p>"
        field = '<label>인증번호</label><input type="tel" name="smsAuthNo" pattern="\\d{4,8}" required autofocus>'
    return page("본인 인증", f"""<form method="post" action="/c/{e(token)}/confirm" class="card"
onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='확인 중...'">
<h1>본인 인증</h1>{err}{guide}{field}
<p class="muted">{PENDING_TTL // 60}분 안에 완료해 주세요.</p>
<p><button>인증 완료</button></p></form>""")


def save_result(token: str, request: Request, personal: dict, marketing: bool, data: dict) -> None:
    with closing(db()) as conn, conn:
        conn.execute("""UPDATE customers SET status='done', completed_at=?, personal_enc=?, result_enc=?,
            consent_version=?, consent_hash=?, consent_marketing=?, consent_at=?, consent_ip=?, consent_ua=?
            WHERE token=?""", (
            int(time.time()), enc(personal), enc(data),
            CONSENT_VERSION, hashlib.sha256(CONSENT_TEXT.encode()).hexdigest(), int(marketing), int(time.time()),
            request.client.host if request.client else "", request.headers.get("user-agent", "")[:300], token))


def done_page() -> HTMLResponse:
    return page("조회 완료", f"""<div class="card"><h1>조회가 완료됐습니다 ✅</h1>
<p>{e(PLANNER_NAME)}이(가) 결과를 확인한 뒤 연락드릴 예정입니다. 이 창은 닫으셔도 됩니다.</p>
<p class="muted">동의 철회 및 정보 삭제는 담당 설계사에게 요청해 주세요.</p></div>""")


@app.get("/c/{token}", response_class=HTMLResponse)
def customer_page(token: str):
    c, err = valid_invite(token)
    return err or customer_form(token)


@app.post("/c/{token}/start", response_class=HTMLResponse)
async def customer_start(request: Request, token: str,
                         consent: str = Form(""), marketing: str = Form(""),
                         userName: str = Form(...), birthDate: str = Form(...), phoneNo: str = Form(...),
                         telecom: str = Form("skt"), authMethod: str = Form("1"),
                         loginId: str = Form(...), loginPw: str = Form(...)):
    c, err = valid_invite(token)
    if err:
        return err
    v = {"userName": userName.strip(), "birthDate": "".join(ch for ch in birthDate if ch.isdigit()),
         "phoneNo": "".join(ch for ch in phoneNo if ch.isdigit()), "telecom": telecom,
         "authMethod": "0" if authMethod == "0" else "1", "loginId": loginId.strip()}
    if consent != "1":
        return customer_form(token, "필수 동의 항목에 동의해 주세요.", v)
    if len(v["birthDate"]) != 8 or not v["phoneNo"].startswith("01"):
        return customer_form(token, "생년월일 8자리와 휴대폰 번호를 확인해 주세요.", v)

    try:
        params = codef.contract_params({
            "id": v["loginId"], "password": loginPw, "userName": v["userName"], "birthDate": v["birthDate"],
            "phoneNo": v["phoneNo"], "telecom": codef.TELECOM.get(telecom, "0"), "authMethod": v["authMethod"]})
        result = await run_in_threadpool(codef.post, codef.CONTRACT_INFO_PATH, params)
    except Exception:
        return customer_form(token, "조회 서버와 통신 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.", v)

    personal = {k: v[k] for k in ("userName", "birthDate", "phoneNo")}
    code = codef.code_of(result)
    if code == codef.NEED_2WAY:
        pending[token] = {"params": params, "two_way": codef.two_way_info(result), "personal": personal,
                          "marketing": marketing == "1", "expires": time.time() + PENDING_TTL}
        return confirm_form(token, v["authMethod"])
    if code == codef.OK:
        save_result(token, request, personal, marketing == "1", result.get("data", {}))
        return done_page()
    return customer_form(token, f"조회 실패: {codef.message_of(result)}", v)


@app.post("/c/{token}/confirm", response_class=HTMLResponse)
async def customer_confirm(request: Request, token: str, smsAuthNo: str = Form("")):
    c, err = valid_invite(token)
    if err:
        return err
    st = pending.get(token)
    if not st or st["expires"] < time.time():
        pending.pop(token, None)
        return customer_form(token, "인증 시간이 지났습니다. 처음부터 다시 진행해 주세요.")

    try:
        result = await run_in_threadpool(codef.post, codef.CONTRACT_INFO_PATH,
                                         codef.confirm_params(st["params"], st["two_way"], smsAuthNo.strip()))
    except Exception:
        return confirm_form(token, st["params"]["authMethod"], "통신 중 오류가 발생했습니다. 다시 시도해 주세요.")

    code = codef.code_of(result)
    if code == codef.OK:
        pending.pop(token, None)
        save_result(token, request, st["personal"], st["marketing"], result.get("data", {}))
        return done_page()
    if code in (codef.NEED_2WAY, "CF-03003"):
        return confirm_form(token, st["params"]["authMethod"], "아직 인증이 완료되지 않았습니다. 인증 후 다시 눌러 주세요.")
    pending.pop(token, None)
    return customer_form(token, f"조회 실패: {codef.message_of(result)}")
