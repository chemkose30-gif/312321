"""TeamHub - 사내 캘린더 & 업무지시 시스템 (FastAPI + SQLite)."""
import hashlib
import re
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import html
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import ecount
import mailer
import ai_mail
import mailin
import unipass
import webpush

BASE_DIR = Path(__file__).parent
DB_PATH = os.getenv("TEAMHUB_DB", str(BASE_DIR / "teamhub.db"))


@asynccontextmanager
async def lifespan(_app):
    mailer.start_scheduler(run_daily_reminders)
    threading.Thread(target=unipass_loop, daemon=True).start()
    try:
        with db() as c:
            merge_auto_bl_shipments(c)       # 예전에 따로 생긴 B/L 자동 등록 건 정리
    except Exception as e:  # noqa: BLE001
        print("[TeamHub] merge error:", e)
    yield


app = FastAPI(title="TeamHub", version="1.1.0", lifespan=lifespan)

PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("todo", "doing", "done", "hold")
SCOPES = ("company", "dept", "private")
STATUS_LABEL = {"todo": "대기", "doing": "진행중", "done": "완료", "hold": "보류"}


# ---------------------------------------------------------------- DB
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                dept TEXT NOT NULL DEFAULT '',
                position TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                pw_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                all_day INTEGER NOT NULL DEFAULT 0,
                color TEXT NOT NULL DEFAULT '#3b82f6',
                scope TEXT NOT NULL DEFAULT 'company',
                dept TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                assigner_id INTEGER NOT NULL REFERENCES users(id),
                assignee_id INTEGER NOT NULL REFERENCES users(id),
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'todo',
                due_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                result TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_no TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL,
                customer_contact TEXT NOT NULL DEFAULT '',
                customer_phone TEXT NOT NULL DEFAULT '',
                customer_email TEXT NOT NULL DEFAULT '',
                quote_date TEXT NOT NULL,
                valid_until TEXT NOT NULL DEFAULT '',
                delivery TEXT NOT NULL DEFAULT '',
                payment_terms TEXT NOT NULL DEFAULT '',
                vat_mode TEXT NOT NULL DEFAULT 'separate',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                supply_total INTEGER NOT NULL DEFAULT 0,
                vat_total INTEGER NOT NULL DEFAULT 0,
                grand_total INTEGER NOT NULL DEFAULT 0,
                task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quote_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                name TEXT NOT NULL,
                spec TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                qty REAL NOT NULL DEFAULT 0,
                unit_price REAL NOT NULL DEFAULT 0,
                supply INTEGER NOT NULL DEFAULT 0,
                vat INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_quotes_task ON quotes(task_id);
            CREATE TABLE IF NOT EXISTS shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                spec TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                customer TEXT NOT NULL DEFAULT '',
                qty REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT '',
                eta TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ordered',
                bl_no TEXT NOT NULL DEFAULT '',
                warehouse TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                arrived_at TEXT,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shipments_eta ON shipments(eta);
            CREATE TABLE IF NOT EXISTS inv_lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet TEXT NOT NULL DEFAULT '',
                row_no INTEGER NOT NULL DEFAULT 0,
                item TEXT NOT NULL,
                cas TEXT NOT NULL DEFAULT '',
                fema TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                info TEXT NOT NULL DEFAULT '',
                order_note TEXT NOT NULL DEFAULT '',
                lot_no TEXT NOT NULL DEFAULT '',
                bat_no TEXT NOT NULL DEFAULT '',
                packing TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT '',
                mfg_date TEXT NOT NULL DEFAULT '',
                expiry TEXT NOT NULL DEFAULT '',
                transport TEXT NOT NULL DEFAULT '',
                customs_date TEXT NOT NULL DEFAULT '',
                import_qty REAL,
                cost_fx REAL,
                rate REAL,
                cost_krw REAL,
                cost_est INTEGER NOT NULL DEFAULT 0,
                stock_qty REAL,
                stock_amt REAL
            );
            CREATE TABLE IF NOT EXISTS inv_ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id INTEGER NOT NULL REFERENCES inv_lots(id) ON DELETE CASCADE,
                ship_date TEXT NOT NULL,
                customer TEXT NOT NULL DEFAULT '',
                qty REAL NOT NULL DEFAULT 0,
                price REAL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_inv_ships_date ON inv_ships(ship_date);
            CREATE INDEX IF NOT EXISTS idx_inv_ships_lot ON inv_ships(lot_id);
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                date TEXT NOT NULL,
                item TEXT NOT NULL,
                item_base TEXT NOT NULL DEFAULT '',
                qty REAL NOT NULL DEFAULT 0,
                customer TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                sale_price REAL,
                buy_price REAL,
                origin TEXT NOT NULL DEFAULT '',
                sales REAL NOT NULL DEFAULT 0,
                purchase REAL NOT NULL DEFAULT 0,
                profit REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                src_row INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(year, date);
            CREATE TABLE IF NOT EXISTS mail_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                uniq TEXT UNIQUE NOT NULL,
                from_addr TEXT NOT NULL DEFAULT '',
                from_name TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                candidates TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mail_items_owner ON mail_items(owner_id, status, sent_at);
            CREATE TABLE IF NOT EXISTS bl_watch (
                number TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT '',
                owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                mail_id INTEGER,
                subject TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                eta_hint TEXT NOT NULL DEFAULT '',
                item_hint TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'wait',
                shipment_id INTEGER,
                tries INTEGER NOT NULL DEFAULT 0,
                last_try TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS push_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                ua TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ecount_products (
                code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '', price REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ecount_customers (
                code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ecount_warehouses (
                code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ecount_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER REFERENCES quotes(id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                ok INTEGER NOT NULL,
                slip_nos TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                user_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigner ON tasks(assigner_id);
            CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
            CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "email" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        for table, col, ddl in (("quotes", "cust_cd", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "ecount_quote_slip", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "ecount_sale_slip", "TEXT NOT NULL DEFAULT ''"),
                                ("quote_items", "prod_cd", "TEXT NOT NULL DEFAULT ''"),
                                ("ecount_customers", "memo", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "doc_type", "TEXT NOT NULL DEFAULT 'quote'"),
                                ("quotes", "source_id", "INTEGER"),
                                ("quotes", "customer_biz_no", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "customer_ceo", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "customer_address", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "customer_fax", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "transport", "TEXT NOT NULL DEFAULT ''"),
                                ("quote_items", "cas_no", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "ecount_wh", "TEXT NOT NULL DEFAULT ''"),
                                ("quotes", "ecount_old_slips", "TEXT NOT NULL DEFAULT ''"),
                                ("quote_items", "origin", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "translation", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "summary", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "ai_status", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "thread_key", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "msg_ref", "TEXT NOT NULL DEFAULT ''"),
                                ("mail_items", "t_candidates", "TEXT NOT NULL DEFAULT '[]'"),
                                ("mail_items", "t_count", "INTEGER NOT NULL DEFAULT 1"),
                                ("mail_items", "fp", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "hbl_no", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_cargo_no", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_status", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_arrived", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_in_at", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_shed", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_cleared_at", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_out_at", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_events", "TEXT NOT NULL DEFAULT '[]'"),
                                ("shipments", "cs_checked_at", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "cs_error", "TEXT NOT NULL DEFAULT ''"),
                                ("shipments", "src_key", "TEXT NOT NULL DEFAULT ''")):
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        # 엑셀로 가져왔는데 건명이 비어 있는 문서 → 품목으로 건명 채우기
        for q in c.execute("SELECT id FROM quotes WHERE title = '' AND note = '이카운트에서 가져옴'").fetchall():
            names = [r["name"] for r in c.execute("SELECT name FROM quote_items WHERE quote_id = ? ORDER BY seq", (q["id"],))]
            c.execute("UPDATE quotes SET title = ? WHERE id = ?", (auto_title(names), q["id"]))
        # 성능: 동시 읽기/쓰기(WAL) + 자주 찾는 열 색인
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_quote_items_quote ON quote_items(quote_id, seq);
            CREATE INDEX IF NOT EXISTS idx_quotes_type_date ON quotes(doc_type, quote_date DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_quotes_source ON quotes(source_id);
            CREATE INDEX IF NOT EXISTS idx_quotes_sale_slip ON quotes(ecount_sale_slip);
            CREATE INDEX IF NOT EXISTS idx_quotes_quote_slip ON quotes(ecount_quote_slip);
            CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id);
            CREATE INDEX IF NOT EXISTS idx_mail_log_task ON mail_log(task_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_mail_items_thread ON mail_items(thread_key, sent_at);
            CREATE INDEX IF NOT EXISTS idx_mail_items_fp ON mail_items(owner_id, fp);
            CREATE INDEX IF NOT EXISTS idx_mail_items_ref ON mail_items(owner_id, msg_ref);
        """)
        if not c.execute("SELECT 1 FROM settings WHERE key = 'secret'").fetchone():
            c.execute("INSERT INTO settings VALUES ('secret', ?)", (secrets.token_hex(32),))
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            salt = secrets.token_hex(16)
            pw = os.getenv("TEAMHUB_ADMIN_PASSWORD", "admin1234")
            c.execute(
                "INSERT INTO users (username, name, dept, position, role, pw_hash, salt, created_at)"
                " VALUES ('admin', '관리자', '경영지원', '관리자', 'admin', ?, ?, ?)",
                (hash_pw(pw, salt), salt, now()),
            )


def get_setting(c, key: str, default: str = "") -> str:
    row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(c, key: str, value: str):
    c.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, value))


def notify(c, user_id: int, message: str, task_id: Optional[int] = None):
    c.execute(
        "INSERT INTO notifications (user_id, message, task_id, created_at) VALUES (?, ?, ?, ?)",
        (user_id, message, task_id, now()),
    )
    push_async(user_id, {"title": "TeamHub", "body": message, "url": f"/#task={task_id}" if task_id else "/",
                         "tag": f"task-{task_id}" if task_id else "teamhub"})


# ---------------------------------------------------------------- 휴대폰/PC 알림 (Web Push)
def vapid_keys(c) -> dict:
    raw = get_setting(c, "vapid", "")
    if raw:
        return json.loads(raw)
    keys = webpush.generate_vapid()
    set_setting(c, "vapid", json.dumps(keys))
    return keys


def push_send(user_id: int, data: dict) -> list:
    """사용자의 모든 기기로 알림 발송 → [HTTP 상태코드…]. 끝난 구독(404/410)은 지운다."""
    with db() as c:
        vapid = vapid_keys(c)
        subs = [dict(r) for r in c.execute("SELECT * FROM push_subs WHERE user_id = ?", (user_id,))]
    subject = f"mailto:{mailer.MAIL_FROM}" if mailer.MAIL_FROM else mailer.BASE_URL
    results, dead = [], []
    for sub in subs:
        try:
            code = webpush.send(sub, data, vapid, subject)
        except Exception:
            code = 0
        results.append(code)
        if code in (403, 404, 410):
            dead.append(sub["id"])
    if dead:
        with db() as c:
            c.executemany("DELETE FROM push_subs WHERE id = ?", [(i,) for i in dead])
    return results


def push_async(user_id: int, data: dict):
    # 알림 저장(트랜잭션)이 끝난 뒤 보내도록 잠깐 기다렸다가 백그라운드로 발송
    def run():
        time.sleep(0.5)
        try:
            push_send(user_id, data)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- Auth
def current_user(authorization: str = Header(default="")) -> dict:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    with db() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token = ? AND u.active = 1",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "세션이 만료되었습니다. 다시 로그인하세요.")
    return dict(row)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user


def public_user(row) -> dict:
    d = dict(row)
    d.pop("pw_hash", None)
    d.pop("salt", None)
    return d


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


# 로그인 실패 제한: 같은 아이디로 10분 안에 10번 틀리면 10분간 잠금 (비밀번호 무작위 대입 방지)
LOGIN_FAILS: dict = {}
MAX_FAILS, LOCK_SECONDS = 10, 600


@app.post("/api/login")
def login(body: LoginIn):
    key = body.username.strip().lower()
    t = time.time()
    fails = [f for f in LOGIN_FAILS.get(key, []) if t - f < LOCK_SECONDS]
    if len(fails) >= MAX_FAILS:
        raise HTTPException(429, "로그인 실패가 많아 10분간 잠겼습니다. 잠시 후 다시 시도하세요.")
    with db() as c:
        u = c.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (body.username.strip(),)
        ).fetchone()
        if not u or hash_pw(body.password, u["salt"]) != u["pw_hash"]:
            LOGIN_FAILS[key] = fails + [t]
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
        LOGIN_FAILS.pop(key, None)
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token, u["id"], now()))
    return {"token": token, "user": public_user(u)}


@app.post("/api/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    d = public_user(user)
    with db() as c:
        d["can_profit"] = can_see_cost(c, user)
    return d


@app.put("/api/me/password")
def change_password(body: PasswordIn, user: dict = Depends(admin_user)):
    # 아이디/비밀번호 설정은 관리자 전용 (직원은 관리자에게 요청)
    if hash_pw(body.current_password, user["salt"]) != user["pw_hash"]:
        raise HTTPException(400, "현재 비밀번호가 올바르지 않습니다.")
    if len(body.new_password) < 6:
        raise HTTPException(400, "비밀번호는 6자 이상이어야 합니다.")
    salt = secrets.token_hex(16)
    with db() as c:
        c.execute(
            "UPDATE users SET pw_hash = ?, salt = ? WHERE id = ?",
            (hash_pw(body.new_password, salt), salt, user["id"]),
        )
    return {"ok": True}


# ---------------------------------------------------------------- Users
class UserIn(BaseModel):
    username: str
    name: str
    dept: str = ""
    position: str = ""
    email: str = ""
    role: str = "member"
    password: str


class UserUpdate(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    dept: Optional[str] = None
    position: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


def check_username(username: str):
    u = username.strip()
    if not (3 <= len(u) <= 30) or not all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in u):
        raise HTTPException(400, "아이디는 영문/숫자/._- 3~30자로 입력하세요.")


@app.get("/api/users")
def list_users(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM users ORDER BY active DESC, dept, name"
        ).fetchall()
    users = [public_user(r) for r in rows]
    if user["role"] != "admin":
        users = [u for u in users if u["active"]]
    return users


@app.post("/api/users")
def create_user(body: UserIn, _: dict = Depends(admin_user)):
    if body.role not in ("admin", "member"):
        raise HTTPException(400, "잘못된 권한입니다.")
    check_username(body.username)
    if len(body.password) < 6:
        raise HTTPException(400, "비밀번호는 6자 이상이어야 합니다.")
    salt = secrets.token_hex(16)
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO users (username, name, dept, position, email, role, pw_hash, salt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.username.strip(), body.name.strip(), body.dept.strip(), body.position.strip(),
                 body.email.strip(), body.role, hash_pw(body.password, salt), salt, now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, "이미 사용 중인 아이디입니다.")
        row = c.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return public_user(row)


@app.put("/api/users/{uid}")
def update_user(uid: int, body: UserUpdate, admin: dict = Depends(admin_user)):
    fields, values = [], []
    if body.username is not None:
        check_username(body.username)
        fields.append("username = ?")
        values.append(body.username.strip())
    for key in ("name", "dept", "position", "email"):
        val = getattr(body, key)
        if val is not None:
            fields.append(f"{key} = ?")
            values.append(val.strip())
    if body.role is not None:
        if body.role not in ("admin", "member"):
            raise HTTPException(400, "잘못된 권한입니다.")
        if uid == admin["id"] and body.role != "admin":
            raise HTTPException(400, "본인의 관리자 권한은 해제할 수 없습니다.")
        fields.append("role = ?")
        values.append(body.role)
    if body.active is not None:
        if uid == admin["id"] and not body.active:
            raise HTTPException(400, "본인 계정은 비활성화할 수 없습니다.")
        fields.append("active = ?")
        values.append(int(body.active))
    if body.password:
        if len(body.password) < 6:
            raise HTTPException(400, "비밀번호는 6자 이상이어야 합니다.")
        salt = secrets.token_hex(16)
        fields += ["pw_hash = ?", "salt = ?"]
        values += [hash_pw(body.password, salt), salt]
    with db() as c:
        if fields:
            try:
                c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*values, uid))
            except sqlite3.IntegrityError:
                raise HTTPException(400, "이미 사용 중인 아이디입니다.")
        if body.active is False or body.password:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "직원을 찾을 수 없습니다.")
    return public_user(row)


# ---------------------------------------------------------------- Events
class EventIn(BaseModel):
    title: str
    description: str = ""
    start: str  # "YYYY-MM-DDTHH:MM" 또는 "YYYY-MM-DD"
    end: str
    all_day: bool = False
    color: str = "#3b82f6"
    scope: str = "company"


def visible_event_clause(user: dict):
    return (
        "(e.scope = 'company' OR (e.scope = 'dept' AND e.dept = ?) OR e.created_by = ?)",
        [user["dept"], user["id"]],
    )


def check_event(body: EventIn):
    if not body.title.strip():
        raise HTTPException(400, "제목을 입력하세요.")
    if body.scope not in SCOPES:
        raise HTTPException(400, "잘못된 공개 범위입니다.")
    if body.end < body.start:
        raise HTTPException(400, "종료 시간이 시작 시간보다 빠릅니다.")


@app.get("/api/events")
def list_events(start: str, end: str, user: dict = Depends(current_user)):
    clause, params = visible_event_clause(user)
    with db() as c:
        events = c.execute(
            "SELECT e.*, u.name AS creator_name FROM events e JOIN users u ON u.id = e.created_by"
            f" WHERE {clause} AND e.start <= ? AND e.end >= ? ORDER BY e.start",
            (*params, end + "T23:59", start),
        ).fetchall()
        tasks = c.execute(
            "SELECT t.id, t.title, t.due_date, t.status, t.priority, a.name AS assignee_name"
            " FROM tasks t JOIN users a ON a.id = t.assignee_id"
            " WHERE (t.assignee_id = ? OR t.assigner_id = ?) AND t.due_date BETWEEN ? AND ?",
            (user["id"], user["id"], start, end),
        ).fetchall()
        ships = c.execute(
            "SELECT id, item, supplier, qty, unit, eta, status FROM shipments WHERE eta BETWEEN ? AND ? ORDER BY eta",
            (start, end),
        ).fetchall()
    return {"events": [dict(r) for r in events], "tasks": [dict(r) for r in tasks],
            "shipments": [dict(r) for r in ships]}


@app.post("/api/events")
def create_event(body: EventIn, user: dict = Depends(current_user)):
    check_event(body)
    with db() as c:
        cur = c.execute(
            "INSERT INTO events (title, description, start, end, all_day, color, scope, dept, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.title.strip(), body.description, body.start, body.end, int(body.all_day),
             body.color, body.scope, user["dept"], user["id"], now()),
        )
    return {"id": cur.lastrowid}


def editable_event(c, eid: int, user: dict):
    ev = c.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    if not ev:
        raise HTTPException(404, "일정을 찾을 수 없습니다.")
    if ev["created_by"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "일정 작성자 또는 관리자만 수정할 수 있습니다.")
    return ev


@app.put("/api/events/{eid}")
def update_event(eid: int, body: EventIn, user: dict = Depends(current_user)):
    check_event(body)
    with db() as c:
        editable_event(c, eid, user)
        c.execute(
            "UPDATE events SET title = ?, description = ?, start = ?, end = ?, all_day = ?,"
            " color = ?, scope = ? WHERE id = ?",
            (body.title.strip(), body.description, body.start, body.end, int(body.all_day),
             body.color, body.scope, eid),
        )
    return {"ok": True}


@app.delete("/api/events/{eid}")
def delete_event(eid: int, user: dict = Depends(current_user)):
    with db() as c:
        editable_event(c, eid, user)
        c.execute("DELETE FROM events WHERE id = ?", (eid,))
    return {"ok": True}


# ---------------------------------------------------------------- Tasks
class TaskIn(BaseModel):
    title: str
    description: str = ""
    assignee_ids: List[int]
    priority: str = "normal"
    due_date: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None


class CommentIn(BaseModel):
    body: str


TASK_SELECT = (
    "SELECT t.*, r.name AS assigner_name, r.dept AS assigner_dept, r.email AS assigner_email,"
    " a.name AS assignee_name, a.dept AS assignee_dept, a.email AS assignee_email,"
    " (SELECT COUNT(*) FROM task_comments tc WHERE tc.task_id = t.id) AS comment_count"
    " FROM tasks t JOIN users r ON r.id = t.assigner_id JOIN users a ON a.id = t.assignee_id"
)


@app.get("/api/tasks")
def list_tasks(box: str = "received", status: Optional[str] = None,
               user: dict = Depends(current_user)):
    where, params = [], []
    if box == "received":
        where.append("t.assignee_id = ?")
        params.append(user["id"])
    elif box == "sent":
        where.append("t.assigner_id = ?")
        params.append(user["id"])
    elif box == "all":
        if user["role"] != "admin":
            raise HTTPException(403, "관리자만 전체 업무를 볼 수 있습니다.")
    else:
        raise HTTPException(400, "잘못된 보관함입니다.")
    if status:
        where.append("t.status = ?")
        params.append(status)
    sql = TASK_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += (" ORDER BY CASE t.status WHEN 'done' THEN 1 ELSE 0 END,"
            " CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,"
            " COALESCE(t.due_date, '9999-12-31'), t.id DESC")
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def get_task_row(c, tid: int, user: dict):
    t = c.execute(TASK_SELECT + " WHERE t.id = ?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "업무를 찾을 수 없습니다.")
    if user["role"] != "admin" and user["id"] not in (t["assigner_id"], t["assignee_id"]):
        raise HTTPException(403, "이 업무에 접근할 권한이 없습니다.")
    return t


@app.post("/api/tasks")
def create_task(body: TaskIn, user: dict = Depends(current_user)):
    if not body.title.strip():
        raise HTTPException(400, "업무 제목을 입력하세요.")
    if not body.assignee_ids:
        raise HTTPException(400, "담당자를 한 명 이상 선택하세요.")
    if body.priority not in PRIORITIES:
        raise HTTPException(400, "잘못된 우선순위입니다.")
    ids = []
    with db() as c:
        for aid in dict.fromkeys(body.assignee_ids):
            if not c.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (aid,)).fetchone():
                raise HTTPException(400, f"존재하지 않는 담당자입니다: {aid}")
            ts = now()
            cur = c.execute(
                "INSERT INTO tasks (title, description, assigner_id, assignee_id, priority, due_date,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (body.title.strip(), body.description, user["id"], aid, body.priority,
                 body.due_date or None, ts, ts),
            )
            ids.append(cur.lastrowid)
            if aid != user["id"]:
                notify(c, aid, f"{user['name']}님이 업무를 지시했습니다: {body.title.strip()}", cur.lastrowid)
        secret = get_setting(c, "secret")
        rows = [c.execute(TASK_SELECT + " WHERE t.id = ?", (i,)).fetchone() for i in ids]
    for t in rows:
        if t["assignee_id"] != user["id"]:
            subject, body_html = mailer.assigned_mail(secret, t)
            mailer.send_async(db, t["assignee_email"], subject, body_html, t["id"], "assigned")
    return {"ids": ids}


@app.get("/api/tasks/{tid}")
def get_task(tid: int, user: dict = Depends(current_user)):
    with db() as c:
        t = get_task_row(c, tid, user)
        comments = c.execute(
            "SELECT tc.*, u.name AS user_name FROM task_comments tc JOIN users u ON u.id = tc.user_id"
            " WHERE tc.task_id = ? ORDER BY tc.id",
            (tid,),
        ).fetchall()
        c.execute("UPDATE notifications SET is_read = 1 WHERE task_id = ? AND user_id = ?",
                  (tid, user["id"]))
        mails = c.execute(
            "SELECT kind, recipient, subject, result, error, created_at FROM mail_log"
            " WHERE task_id = ? ORDER BY id DESC LIMIT 20", (tid,),
        ).fetchall()
        quotes = c.execute(
            "SELECT id, quote_no, doc_type, customer_name, grand_total, status FROM quotes WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
    return {**dict(t), "comments": [dict(r) for r in comments], "mails": [dict(r) for r in mails],
            "quotes": [dict(r) for r in quotes]}


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, body: TaskUpdate, user: dict = Depends(current_user)):
    with db() as c:
        t = get_task_row(c, tid, user)
        is_owner = user["id"] == t["assigner_id"] or user["role"] == "admin"
        fields, values = [], []
        for key in ("title", "description", "priority", "due_date"):
            val = getattr(body, key)
            if val is None:
                continue
            if not is_owner:
                raise HTTPException(403, "업무 내용은 지시자만 수정할 수 있습니다.")
            if key == "priority" and val not in PRIORITIES:
                raise HTTPException(400, "잘못된 우선순위입니다.")
            fields.append(f"{key} = ?")
            values.append(val or None if key == "due_date" else val)
        if body.status is not None and body.status not in STATUSES:
            raise HTTPException(400, "잘못된 상태입니다.")
        if fields:
            fields.append("updated_at = ?")
            values.append(now())
            c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", (*values, tid))
        mail = None
        if body.status is not None:
            mail = change_status(c, t, user, body.status)
    if mail:
        mailer.send_async(db, *mail)
    return {"ok": True}


def change_status(c, t, actor: dict, status: str, via_mail: bool = False):
    """업무 상태를 바꾸고 상대방에게 알림을 남긴다. 보낼 메일(to, subject, html, task_id, kind)을 반환."""
    if status == t["status"]:
        return None
    c.execute("UPDATE tasks SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
              (status, now() if status == "done" else None, now(), t["id"]))
    target_is_assigner = actor["id"] == t["assignee_id"]
    target = t["assigner_id"] if target_is_assigner else t["assignee_id"]
    if target == actor["id"]:
        return None
    how = " (메일에서 처리)" if via_mail else ""
    notify(c, target, f"{actor['name']}님이 '{t['title']}' 업무를"
           f" [{STATUS_LABEL[status]}](으)로 변경했습니다{how}.", t["id"])
    updated = c.execute(TASK_SELECT + " WHERE t.id = ?", (t["id"],)).fetchone()
    subject, body_html = mailer.status_mail(updated, actor["name"], status, via_mail)
    to = t["assigner_email"] if target_is_assigner else t["assignee_email"]
    return (to, subject, body_html, t["id"], f"status:{status}")


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, user: dict = Depends(current_user)):
    with db() as c:
        t = get_task_row(c, tid, user)
        if user["id"] != t["assigner_id"] and user["role"] != "admin":
            raise HTTPException(403, "지시자 또는 관리자만 삭제할 수 있습니다.")
        c.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    return {"ok": True}


@app.post("/api/tasks/{tid}/comments")
def add_comment(tid: int, body: CommentIn, user: dict = Depends(current_user)):
    if not body.body.strip():
        raise HTTPException(400, "내용을 입력하세요.")
    with db() as c:
        t = get_task_row(c, tid, user)
        c.execute(
            "INSERT INTO task_comments (task_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (tid, user["id"], body.body.strip(), now()),
        )
        for target in {t["assigner_id"], t["assignee_id"]} - {user["id"]}:
            notify(c, target, f"{user['name']}님이 '{t['title']}' 업무에 댓글을 남겼습니다.", tid)
    return {"ok": True}


# ---------------------------------------------------------------- 견적서
QUOTE_STATUSES = ("draft", "sent", "won", "lost")
DOC_TYPES = {"quote": ("Q", "견적서"), "statement": ("T", "거래명세서")}
VAT_MODES = ("separate", "included", "none")  # 부가세 별도 / 포함 / 면세(영세)
COMPANY_KEYS = ("company_name", "ceo", "biz_no", "biz_type", "biz_item", "address", "phone", "fax",
                "email", "stamp", "quote_footer")


class QuoteItemIn(BaseModel):
    prod_cd: str = ""
    cas_no: str = ""
    origin: str = ""
    name: str
    spec: str = ""
    unit: str = ""
    qty: float = 0
    unit_price: float = 0
    note: str = ""


class QuoteIn(BaseModel):
    doc_type: str = "quote"
    source_id: Optional[int] = None
    title: str = ""
    cust_cd: str = ""
    customer_biz_no: str = ""
    customer_ceo: str = ""
    customer_address: str = ""
    customer_fax: str = ""
    transport: str = ""
    customer_name: str
    customer_contact: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    quote_date: str
    valid_until: str = ""
    delivery: str = ""
    payment_terms: str = ""
    vat_mode: str = "separate"
    note: str = ""
    status: str = "draft"
    task_id: Optional[int] = None
    items: List[QuoteItemIn]
    complete_task: bool = False


def calc_line(item: QuoteItemIn, vat_mode: str):
    amount = round(item.qty * item.unit_price)
    if vat_mode == "separate":
        return amount, int(amount * 0.1)          # 세액 원 미만 절사
    if vat_mode == "included":
        supply = round(amount / 1.1)
        return supply, amount - supply
    return amount, 0


def check_quote(body: QuoteIn):
    if not body.customer_name.strip():
        raise HTTPException(400, "거래처명을 입력하세요.")
    if body.vat_mode not in VAT_MODES or body.status not in QUOTE_STATUSES or body.doc_type not in DOC_TYPES:
        raise HTTPException(400, "잘못된 입력입니다.")
    if body.doc_type == "statement" and body.status not in ("draft", "sent", "won"):
        body.status = "sent"
    items = [i for i in body.items if i.name.strip()]
    if not items:
        raise HTTPException(400, "품목을 한 개 이상 입력하세요.")
    return items


def next_quote_no(c, date: str, doc_type: str = "quote") -> str:
    prefix = DOC_TYPES[doc_type][0] + date.replace("-", "")[:8] + "-"
    row = c.execute("SELECT quote_no FROM quotes WHERE quote_no LIKE ? ORDER BY quote_no DESC LIMIT 1",
                    (prefix + "%",)).fetchone()
    n = int(row["quote_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{n:03d}"


def save_quote_items(c, qid: int, items, vat_mode: str):
    c.execute("DELETE FROM quote_items WHERE quote_id = ?", (qid,))
    supply_total = vat_total = 0
    for seq, it in enumerate(items, 1):
        supply, vat = calc_line(it, vat_mode)
        supply_total += supply
        vat_total += vat
        c.execute(
            "INSERT INTO quote_items (quote_id, seq, prod_cd, cas_no, origin, name, spec, unit, qty, unit_price, supply,"
            " vat, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (qid, seq, it.prod_cd.strip(), it.cas_no.strip(), it.origin.strip(), it.name.strip(), it.spec.strip(), it.unit.strip(), it.qty, it.unit_price,
             supply, vat, it.note.strip()),
        )
    c.execute("UPDATE quotes SET supply_total = ?, vat_total = ?, grand_total = ? WHERE id = ?",
              (supply_total, vat_total, supply_total + vat_total, qid))


def link_task(c, body: QuoteIn, user: dict, qid: int, quote_no: str):
    """견적서를 업무에 연결하고, 요청 시 업무를 완료 처리한다. 보낼 메일을 반환."""
    if not body.task_id:
        return None
    t = get_task_row(c, body.task_id, user)
    c.execute("INSERT INTO task_comments (task_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
              (t["id"], user["id"], f"{'📄' if body.doc_type == 'statement' else '🧾'} {DOC_TYPES[body.doc_type][1]}"
               f" {quote_no} ({body.customer_name.strip()}) 작성", now()))
    if body.complete_task:
        return change_status(c, t, user, "done")
    return None


def editable_quote(c, qid: int, user: dict):
    q = c.execute("SELECT * FROM quotes WHERE id = ?", (qid,)).fetchone()
    if not q:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if q["created_by"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403, "작성자 또는 관리자만 수정할 수 있습니다.")
    return q


QUOTE_FIELDS = ("title", "cust_cd", "customer_biz_no", "customer_ceo", "customer_address", "customer_fax", "transport",
                "customer_name", "customer_contact", "customer_phone", "customer_email",
                "quote_date", "valid_until", "delivery", "payment_terms", "vat_mode", "note", "status")


@app.get("/api/quotes")
def list_quotes(q: str = "", status: str = "", doc_type: str = "quote", year: str = "", month: str = "",
                page: int = 1, size: int = 30, user: dict = Depends(current_user)):
    where, params = ["qt.doc_type = ?"], [doc_type]
    if q:
        where.append("(qt.customer_name LIKE ? OR qt.title LIKE ? OR qt.quote_no LIKE ?"
                     " OR EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = qt.id AND qi.name LIKE ?))")
        params += [f"%{q}%"] * 4
    if status:
        where.append("qt.status = ?")
        params.append(status)
    if year:
        where.append("substr(qt.quote_date, 1, 4) = ?")
        params.append(year)
        if month:
            where.append("substr(qt.quote_date, 6, 2) = ?")
            params.append(f"{int(month):02d}")
    cond = " WHERE " + " AND ".join(where)
    size = max(10, min(size, 200))
    page = max(1, page)
    with db() as c:
        total, amount = c.execute(f"SELECT COUNT(*), COALESCE(SUM(grand_total), 0) FROM quotes qt{cond}",
                                  params).fetchone()
        years = [r[0] for r in c.execute(
            "SELECT DISTINCT substr(quote_date, 1, 4) FROM quotes WHERE doc_type = ? ORDER BY 1 DESC", (doc_type,))]
        # 1) 이 페이지에 들어갈 문서 id 만 먼저 고르고  2) 그 30건에 대해서만 품목 요약을 계산
        ids = [r[0] for r in c.execute(
            f"SELECT qt.id FROM quotes qt{cond} ORDER BY qt.quote_date DESC, qt.id DESC LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size))]
        marks = ",".join("?" * len(ids)) or "NULL"
        rows = c.execute(
            "SELECT qt.*, u.name AS creator_name,"
            " (SELECT name FROM quote_items qi WHERE qi.quote_id = qt.id ORDER BY seq LIMIT 1) AS first_item,"
            " s.item_count, s.total_qty, s.qty_unit"
            " FROM quotes qt JOIN users u ON u.id = qt.created_by"
            " LEFT JOIN (SELECT quote_id, COUNT(*) AS item_count, SUM(qty) AS total_qty,"
            "   CASE WHEN COUNT(DISTINCT unit) = 1 THEN MAX(unit) ELSE '' END AS qty_unit"
            f"   FROM quote_items WHERE quote_id IN ({marks}) GROUP BY quote_id) s ON s.quote_id = qt.id"
            f" WHERE qt.id IN ({marks}) ORDER BY qt.quote_date DESC, qt.id DESC",
            (*ids, *ids),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "amount": amount, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size), "years": years}


@app.get("/api/quotes/suggest")
def quote_suggest(user: dict = Depends(current_user)):
    """이전 견적서의 거래처/품목을 자동완성 후보로 제공."""
    with db() as c:
        customers = c.execute(
            "SELECT customer_name, customer_contact, customer_phone, customer_email,"
            " customer_biz_no, customer_ceo, customer_address, customer_fax FROM quotes"
            " WHERE id IN (SELECT MAX(id) FROM quotes GROUP BY customer_name) ORDER BY customer_name"
        ).fetchall()
        items = c.execute(
            "SELECT name, spec, unit, unit_price, cas_no, origin FROM quote_items"
            " WHERE id IN (SELECT MAX(id) FROM quote_items GROUP BY name, spec) ORDER BY name LIMIT 1000"
        ).fetchall()
    return {"customers": [dict(r) for r in customers], "items": [dict(r) for r in items]}


@app.get("/api/quotes/{qid}")
def get_quote(qid: int, user: dict = Depends(current_user)):
    with db() as c:
        q = c.execute(
            "SELECT qt.*, u.name AS creator_name, u.position AS creator_position, u.email AS creator_email"
            " FROM quotes qt JOIN users u ON u.id = qt.created_by WHERE qt.id = ?",
            (qid,),
        ).fetchone()
        if not q:
            raise HTTPException(404, "문서를 찾을 수 없습니다.")
        items = c.execute("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY seq", (qid,)).fetchall()
        children = c.execute("SELECT id, quote_no, quote_date, grand_total FROM quotes WHERE source_id = ? ORDER BY id",
                             (qid,)).fetchall()
        source = c.execute("SELECT id, quote_no FROM quotes WHERE id = ?", (q["source_id"],)).fetchone() \
            if q["source_id"] else None
    return {**dict(q), "items": [dict(r) for r in items], "statements": [dict(r) for r in children],
            "source": dict(source) if source else None}


@app.post("/api/quotes")
def create_quote(body: QuoteIn, user: dict = Depends(current_user)):
    items = check_quote(body)
    with db() as c:
        if body.task_id:
            get_task_row(c, body.task_id, user)
        source = None
        if body.source_id:
            source = c.execute("SELECT * FROM quotes WHERE id = ?", (body.source_id,)).fetchone()
            if not source:
                raise HTTPException(400, "원본 견적서를 찾을 수 없습니다.")
        quote_no = next_quote_no(c, body.quote_date, body.doc_type)
        ts = now()
        cur = c.execute(
            f"INSERT INTO quotes (quote_no, doc_type, source_id, {', '.join(QUOTE_FIELDS)}, task_id, created_by,"
            f" created_at, updated_at) VALUES (?, ?, ?, {', '.join('?' * len(QUOTE_FIELDS))}, ?, ?, ?, ?)",
            (quote_no, body.doc_type, body.source_id, *[getattr(body, f).strip() for f in QUOTE_FIELDS],
             body.task_id, user["id"], ts, ts),
        )
        qid = cur.lastrowid
        if source and body.doc_type == "statement" and source["status"] in ("draft", "sent"):
            c.execute("UPDATE quotes SET status = 'won', updated_at = ? WHERE id = ?", (ts, source["id"]))
        save_quote_items(c, qid, items, body.vat_mode)
        mail = link_task(c, body, user, qid, quote_no)
    if mail:
        mailer.send_async(db, *mail)
    return {"id": qid, "quote_no": quote_no}


@app.put("/api/quotes/{qid}")
def update_quote(qid: int, body: QuoteIn, user: dict = Depends(current_user)):
    items = check_quote(body)
    with db() as c:
        q = editable_quote(c, qid, user)
        body.doc_type = q["doc_type"]
        if q["doc_type"] == "statement" and body.status not in ("draft", "sent", "won"):
            body.status = "sent"
        c.execute(
            f"UPDATE quotes SET {', '.join(f + ' = ?' for f in QUOTE_FIELDS)}, updated_at = ? WHERE id = ?",
            (*[getattr(body, f).strip() for f in QUOTE_FIELDS], now(), qid),
        )
        save_quote_items(c, qid, items, body.vat_mode)
        mail = None
        if body.complete_task and q["task_id"]:
            t = get_task_row(c, q["task_id"], user)
            mail = change_status(c, t, user, "done")
    if mail:
        mailer.send_async(db, *mail)
    return {"id": qid, "quote_no": q["quote_no"],
            "ecount": {"quotation": q["ecount_quote_slip"], "sale": q["ecount_sale_slip"]}}


@app.patch("/api/quotes/{qid}/status")
def quote_status(qid: int, body: dict, user: dict = Depends(current_user)):
    st = body.get("status")
    if st not in QUOTE_STATUSES:
        raise HTTPException(400, "잘못된 상태입니다.")
    with db() as c:
        if not c.execute("SELECT 1 FROM quotes WHERE id = ?", (qid,)).fetchone():
            raise HTTPException(404, "문서를 찾을 수 없습니다.")
        c.execute("UPDATE quotes SET status = ?, updated_at = ? WHERE id = ?", (st, now(), qid))
    return {"ok": True}


@app.delete("/api/quotes/{qid}")
def delete_quote(qid: int, user: dict = Depends(current_user)):
    with db() as c:
        editable_quote(c, qid, user)
        c.execute("DELETE FROM quotes WHERE id = ?", (qid,))
    return {"ok": True}


@app.get("/api/company")
def get_company(user: dict = Depends(current_user)):
    with db() as c:
        return {k: get_setting(c, "company_" + k) for k in COMPANY_KEYS}


@app.put("/api/company")
def put_company(body: dict, _: dict = Depends(admin_user)):
    stamp = body.get("stamp", "")
    if stamp and (not str(stamp).startswith("data:image/") or len(stamp) > 400_000):
        raise HTTPException(400, "도장 이미지는 400KB 이하의 이미지 파일이어야 합니다.")
    with db() as c:
        for k in COMPANY_KEYS:
            if k in body:
                set_setting(c, "company_" + k, str(body[k] or "").strip())
    return {"ok": True}


# ---------------------------------------------------------------- 이카운트 ERP 연동
ECOUNT_KEYS = ("com_code", "user_id", "api_key", "is_test", "emp_cd", "default_wh", "path_products", "path_quotation",
               "path_quotation_list_key", "path_sale", "path_sale_list_key")


def ecount_cfg(c) -> dict:
    cfg = {k: get_setting(c, "ecount_" + k) for k in ECOUNT_KEYS}
    cfg["is_test"] = cfg["is_test"] or "1"
    return cfg


def ecount_client(c) -> ecount.Client:
    return ecount.get_client(ecount_cfg(c))


def ecount_log(c, quote_id, kind: str, ok: bool, slip_nos: str, message: str, user: dict):
    c.execute("INSERT INTO ecount_log (quote_id, kind, ok, slip_nos, message, user_id, created_at)"
              " VALUES (?, ?, ?, ?, ?, ?, ?)", (quote_id, kind, int(ok), slip_nos, message[:1000], user["id"], now()))


@app.get("/api/ecount/settings")
def ecount_get_settings(_: dict = Depends(admin_user)):
    with db() as c:
        cfg = ecount_cfg(c)
        last_sync = get_setting(c, "ecount_last_sync")
        counts = {t: c.execute(f"SELECT COUNT(*) FROM ecount_{t}").fetchone()[0]
                  for t in ("products", "customers", "warehouses")}
    has_key = bool(cfg.pop("api_key"))
    return {**cfg, "has_key": has_key, "defaults": ecount.DEFAULT_PATHS, "last_sync": last_sync, "counts": counts}


@app.put("/api/ecount/settings")
def ecount_put_settings(body: dict, _: dict = Depends(admin_user)):
    with db() as c:
        for k in ECOUNT_KEYS:
            if k not in body:
                continue
            if k == "api_key" and not str(body[k]).strip():
                continue  # 빈 값이면 기존 키 유지
            set_setting(c, "ecount_" + k, str(body[k] or "").strip())
    return {"ok": True}


@app.post("/api/ecount/test")
def ecount_test(_: dict = Depends(admin_user)):
    with db() as c:
        client = ecount_client(c)
    try:
        client.login(force=True)
    except ecount.EcountError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "zone": client.zone, "mode": "테스트 서버" if client.is_test else "실서비스"}


@app.post("/api/ecount/sync-products")
def ecount_sync_products(user: dict = Depends(admin_user)):
    with db() as c:
        client = ecount_client(c)
    try:
        rows = client.products()
    except ecount.EcountError as e:
        raise HTTPException(400, str(e))
    with db() as c:
        replace_master(c, "products", rows)
        set_setting(c, "ecount_last_sync", now())
    return {"ok": True, "count": len(rows)}


def replace_master(c, kind: str, rows: list):
    c.execute(f"DELETE FROM ecount_{kind}")
    if kind == "products":
        c.executemany("INSERT OR REPLACE INTO ecount_products (code, name, spec, unit, price) VALUES (?, ?, ?, ?, ?)",
                      [(r["code"], r.get("name", ""), r.get("spec", ""), r.get("unit", ""), r.get("price") or 0)
                       for r in rows])
    elif kind == "customers":
        c.executemany("INSERT OR REPLACE INTO ecount_customers (code, name, memo) VALUES (?, ?, ?)",
                      [(r["code"], r.get("name", ""), r.get("memo", "")) for r in rows])
    else:
        c.executemany(f"INSERT OR REPLACE INTO ecount_{kind} (code, name) VALUES (?, ?)",
                      [(r["code"], r.get("name", "")) for r in rows])


def parse_sheet(filename: str, data: bytes) -> list:
    """엑셀(xlsx)/CSV 를 행 목록으로."""
    if filename.lower().endswith(".csv"):
        import csv
        import io
        for enc in ("utf-8-sig", "cp949"):
            try:
                return list(csv.reader(io.StringIO(data.decode(enc))))
            except UnicodeDecodeError:
                continue
        raise HTTPException(400, "CSV 파일 인코딩을 읽을 수 없습니다.")
    try:
        return read_xlsx_values(data)
    except Exception:
        pass
    try:  # 표준 라이브러리 리더가 실패하면 openpyxl 로 한 번 더
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [["" if v is None else str(v).strip() for v in row] for row in wb.active.iter_rows(values_only=True)]
    except Exception:
        raise HTTPException(400, "엑셀 파일을 읽을 수 없습니다. 엑셀에서 열어 '다른 이름으로 저장 → Excel 통합 문서(.xlsx)'"
                                 " 또는 CSV 로 저장해서 올려주세요.")


def xlsx_sheet_names(data: bytes) -> list:
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    wb = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("xl/workbook.xml"))
    return [sh.get("name") for sh in wb.iter(m + "sheet")]


def read_xlsx_values(data: bytes, sheet=None) -> list:
    """xlsx 시트의 셀 값만 읽는다(sheet: 이름 또는 0부터 순번, 기본 첫 시트). 서식(스타일)은 무시 —
    이카운트 엑셀은 서식 정보가 표준과 달라 openpyxl 이 읽지 못하는 경우가 있다."""
    import io
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    # 첫 번째 시트 파일 찾기
    sheet_path = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = wb.findall("m:sheets/m:sheet", ns)
        if isinstance(sheet, int):
            first = sheets[sheet]
        elif sheet:
            first = next(sh for sh in sheets if sh.get("name") == sheet)
        else:
            first = sheets[0]
        rid = first.get(f"{{{ns['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.findall("pr:Relationship", ns):
            if rel.get("Id") == rid:
                target = rel.get("Target")
                sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
    except (StopIteration, IndexError):
        raise
    except Exception:
        pass
    if sheet_path not in names:
        sheet_path = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))[0]
    shared = []
    if "xl/sharedStrings.xml" in names:
        for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
            if el.tag == f"{{{ns['m']}}}si":
                shared.append("".join(t.text or "" for t in el.iter(f"{{{ns['m']}}}t")))
                el.clear()

    def col_index(ref: str) -> int:
        n = 0
        for ch in ref:
            if not ch.isalpha():
                break
            n = n * 26 + (ord(ch.upper()) - 64)
        return n - 1

    rows = []
    c_tag, v_tag, row_tag = f"{{{ns['m']}}}c", f"{{{ns['m']}}}v", f"{{{ns['m']}}}row"
    for _, el in ET.iterparse(z.open(sheet_path)):
        if el.tag != row_tag:
            continue
        rnum = int(el.get("r") or len(rows) + 1)
        while len(rows) < rnum - 1:
            rows.append([])
        vals = []
        for c in el.findall(c_tag):
            idx = col_index(c.get("r") or "") if c.get("r") else len(vals)
            t = c.get("t")
            if t == "inlineStr":
                v = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            else:
                ve = c.find(v_tag)
                v = ve.text if ve is not None and ve.text is not None else ""
                if t == "s" and v != "":
                    v = shared[int(v)]
                elif t in (None, "n") and v != "":
                    try:
                        f = float(v)
                        v = str(int(f)) if f == int(f) else str(f)
                    except ValueError:
                        pass
            while len(vals) < idx:
                vals.append("")
            vals.append(str(v).strip())
        rows.append(vals)
        el.clear()
    return rows


def rows_to_master(kind: str, rows: list) -> list:
    """이카운트에서 내려받은 목록의 머리글(…코드, …명, 규격, 단위, 단가)을 찾아 변환."""
    for hi, header in enumerate(rows[:15]):
        cells = [str(h).replace(" ", "") for h in header]
        code_i = next((i for i, h in enumerate(cells) if h.endswith("코드")), None)
        name_i = next((i for i, h in enumerate(cells) if h.endswith("명") and "코드" not in h), None)
        if code_i is None or name_i is None:
            continue
        find = lambda *keys: next((i for i, h in enumerate(cells) if any(k in h for k in keys)), None)
        spec_i, unit_i, price_i = find("규격"), find("단위"), find("출고단가", "판매단가", "단가")
        memo_i = find("주소", "비고", "적요")
        out = []
        for r in rows[hi + 1:]:
            get = lambda i: (str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else "")
            code = get(code_i)
            if not code:
                continue
            item = {"code": code, "name": get(name_i)}
            if kind == "customers":
                item["memo"] = get(memo_i)
            if kind == "products":
                try:
                    price = float(get(price_i).replace(",", "") or 0)
                except ValueError:
                    price = 0
                item.update(spec=get(spec_i), unit=get(unit_i), price=price)
            out.append(item)
        return out
    raise HTTPException(400, "머리글에서 '○○코드'와 '○○명' 열을 찾지 못했습니다. 이카운트에서 내려받은 목록 그대로 올려주세요.")


@app.post("/api/ecount/upload/{kind}")
async def ecount_upload(kind: str, file: UploadFile = File(...), _: dict = Depends(admin_user)):
    if kind not in ("products", "customers", "warehouses"):
        raise HTTPException(404, "잘못된 종류입니다.")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "파일이 너무 큽니다 (20MB 이하).")
    items = rows_to_master(kind, parse_sheet(file.filename or "", data))
    with db() as c:
        replace_master(c, kind, items)
    return {"ok": True, "count": len(items)}


class WarehouseIn(BaseModel):
    code: str
    name: str = ""


@app.put("/api/ecount/warehouses")
def ecount_put_warehouses(body: List[WarehouseIn], _: dict = Depends(admin_user)):
    rows = [{"code": w.code.strip(), "name": w.name.strip()} for w in body if w.code.strip()]
    with db() as c:
        replace_master(c, "warehouses", rows)
    return {"ok": True, "count": len(rows)}


def default_wh(c) -> str:
    """기본 출고 창고: 설정값 > 코드 2 > 이름에 '이알씨' > 첫 창고."""
    whs = [dict(r) for r in c.execute("SELECT code, name FROM ecount_warehouses ORDER BY code")]
    codes = {w["code"] for w in whs}
    v = get_setting(c, "ecount_default_wh")
    if v in codes:
        return v
    for w in whs:
        if w["code"].lstrip("0") == "2":
            return w["code"]
    for w in whs:
        if "이알씨" in w["name"].replace(" ", ""):
            return w["code"]
    return whs[0]["code"] if whs else ""


@app.get("/api/ecount/master")
def ecount_master(user: dict = Depends(current_user)):
    with db() as c:
        cfg = ecount_cfg(c)
        return {
            "enabled": bool(cfg["com_code"] and cfg["user_id"] and cfg["api_key"]),
            "mode": "test" if cfg["is_test"] in ("1", "true") else "live",
            "products": [dict(r) for r in c.execute("SELECT code, name, spec, unit, price FROM ecount_products ORDER BY name")],
            "customers": [dict(r) for r in c.execute("SELECT code, name, memo FROM ecount_customers ORDER BY name")],
            "warehouses": [dict(r) for r in c.execute("SELECT code, name FROM ecount_warehouses ORDER BY code")],
            "default_wh": default_wh(c),
        }


class EcountSendIn(BaseModel):
    kind: str  # quotation | sale
    wh_cd: str = ""
    io_date: str = ""
    force: bool = False


@app.post("/api/quotes/{qid}/ecount")
def ecount_send(qid: int, body: EcountSendIn, user: dict = Depends(current_user)):
    if body.kind not in ("quotation", "sale"):
        raise HTTPException(400, "잘못된 전송 종류입니다.")
    label = "견적서" if body.kind == "quotation" else "판매(거래명세서)"
    slip_col = "ecount_quote_slip" if body.kind == "quotation" else "ecount_sale_slip"
    with db() as c:
        q = editable_quote(c, qid, user)
        if q["doc_type"] == "statement" and body.kind != "sale":
            raise HTTPException(400, "거래명세서는 이카운트 판매로만 전송할 수 있습니다.")
        items = c.execute("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY seq", (qid,)).fetchall()
        problems = []
        if not q["cust_cd"]:
            problems.append("이카운트 거래처가 지정되지 않았습니다.")
        missing = [str(i["seq"]) for i in items if not i["prod_cd"]]
        if missing:
            problems.append(f"이카운트 품목이 지정되지 않은 행: {', '.join(missing)}번")
        if body.kind == "sale" and not body.wh_cd:
            problems.append("출고 창고를 선택하세요.")
        if problems:
            raise HTTPException(400, " / ".join(problems) + " (견적서 수정에서 지정하세요)")
        if q[slip_col] and not body.force:
            raise HTTPException(409, f"이미 이카운트에 {label}로 전송되었습니다 (전표 {q[slip_col]}).")
        client = ecount_client(c)
        emp_cd = ecount_cfg(c)["emp_cd"]
    lines = ecount.build_lines(dict(q), [dict(i) for i in items], q["cust_cd"], body.wh_cd,
                               body.io_date or q["quote_date"], emp_cd, body.kind)
    try:
        result = client.save_slip(body.kind, lines)
    except ecount.EcountError as e:
        with db() as c:
            ecount_log(c, qid, body.kind, False, "", str(e), user)
        raise HTTPException(400, f"이카운트 전송 실패: {e}")
    slips = ", ".join(result["slip_nos"])
    delete_task = None
    with db() as c:
        if result["ok"]:
            c.execute(f"UPDATE quotes SET {slip_col} = ?, updated_at = ? WHERE id = ?", (slips or "전송됨", now(), qid))
            if body.kind == "sale" and body.wh_cd:
                c.execute("UPDATE quotes SET ecount_wh = ? WHERE id = ?", (body.wh_cd, qid))
            old = q[slip_col]
            if old:
                # 이카운트 API 로는 수정·삭제가 안 되므로: 새 전표로 다시 등록 + 예전 전표 삭제를 할 일로 남김
                kind_label = "견적서" if body.kind == "quotation" else "판매"
                menu = "영업관리 > 견적서조회" if body.kind == "quotation" else "영업관리 > 판매조회"
                note = f"{kind_label} {old}"
                c.execute("UPDATE quotes SET ecount_old_slips = TRIM(ecount_old_slips || ? || ?, '; ') WHERE id = ?",
                          ("; " if q["ecount_old_slips"] else "", note, qid))
                ts = now()
                cur = c.execute(
                    "INSERT INTO tasks (title, description, assigner_id, assignee_id, priority, due_date, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'high', ?, ?, ?)",
                    (f"[이카운트] 예전 {kind_label} 전표 {old} 삭제 ({q['customer_name']})",
                     f"TeamHub {q['quote_no']} ({q['customer_name']}) 수정 후 이카운트에 새 전표 {slips}로 다시 등록했습니다.\n"
                     f"이카운트 {menu} 에서 예전 전표 {old} 를 찾아 삭제해 주세요. 삭제했으면 이 업무를 완료로 바꾸세요.",
                     user["id"], user["id"], datetime.now().strftime("%Y-%m-%d"), ts, ts))
                delete_task = cur.lastrowid
            if body.kind == "sale" and q["doc_type"] == "quote" and q["status"] in ("draft", "sent"):
                c.execute("UPDATE quotes SET status = 'won' WHERE id = ?", (qid,))
        msg = "; ".join(result["messages"]) or ("" if result["ok"] else json.dumps(result["raw"], ensure_ascii=False)[:500])
        if result.get("quota"):
            msg = (msg + " · " if msg else "") + result["quota"]
        ecount_log(c, qid, body.kind, result["ok"], slips, msg, user)
    if not result["ok"]:
        raise HTTPException(400, f"이카운트가 {label} 등록을 거부했습니다: {msg}")
    return {"ok": True, "slip_nos": result["slip_nos"], "mode": "테스트 서버" if client.is_test else "실서비스",
            "replaced": bool(delete_task), "delete_task": delete_task}


# ---------------------------------------------------------------- 이카운트 판매(거래명세서) 엑셀 가져오기
# 이카운트는 판매 조회 API 를 제공하지 않으므로 [판매조회/판매현황] 화면에서 내려받은 엑셀을 올려 가져온다.
SALE_COLS = {
    "slip": ("일자-no", "일자no", "전표번호", "견적번호"),
    "date": ("일자", "판매일", "거래일"),
    "cust_cd": ("거래처코드",),
    "customer": ("거래처명", "거래처"),
    "prod_cd": ("품목코드",),
    "name": ("품목명", "품명"),
    "spec": ("규격",),
    "unit": ("단위",),
    "qty": ("수량",),
    "price": ("단가",),
    "total": ("합계금액", "금액합계", "견적금액", "총금액", "합계", "총액"),
    "supply": ("공급가액", "금액"),
    "vat": ("부가세", "세액", "vat"),
    "note": ("적요", "비고"),
}


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _norm_header(h) -> str:
    return re.sub(r"[\s.\-–—‐−_·]", "", str(h or "")).lower()


CUST_WORDS = ("거래처", "판매처", "매출처", "고객")


def _map_sale_cols(cells: list) -> dict:
    """열 이름 일부만 맞아도 인식 (예: 일자-No. / 견적일자 / 수량(kg) / 금액합계)."""
    rules = [
        ("slip", lambda h: ("일자" in h and "no" in h) or h in ("월/일", "월일") or any(k in h for k in ("전표번호", "견적번호"))),
        ("vendor", lambda h: "구매처" in h),
        ("title", lambda h: "건명" in h or "제목" in h),
        ("transport", lambda h: "운송조건" in h or "transportation" in h),
        ("cas_no", lambda h: h.startswith("cas")),
        ("origin", lambda h: "원산지" in h or h.startswith("origin")),
        ("cust_cd", lambda h: any(k in h for k in CUST_WORDS) and "코드" in h),
        ("customer", lambda h: any(k in h for k in CUST_WORDS) and "코드" not in h),
        ("date", lambda h: "일자" in h or h in ("일", "날짜") or any(k in h for k in ("판매일", "거래일", "견적일", "작성일"))),
        ("prod_cd", lambda h: "품목" in h and "코드" in h),
        ("name", lambda h: ("품목" in h or "품명" in h) and "코드" not in h and "그룹" not in h),
        ("spec", lambda h: "규격" in h),
        ("unit", lambda h: h.startswith("단위")),
        ("qty", lambda h: h.startswith("수량")),
        ("price", lambda h: "단가" in h),
        ("total", lambda h: any(k in h for k in ("합계", "총액", "총금액", "견적금액"))),
        ("supply", lambda h: "공급가액" in h or (h.startswith("금액") and "합계" not in h)),
        ("vat", lambda h: "부가세" in h or "세액" in h or h.startswith("vat")),
        ("note", lambda h: "적요" in h or "비고" in h),
    ]
    col = {}
    for key, ok in rules:
        for i, h in enumerate(cells):
            if h and i not in col.values() and ok(h):
                col[key] = i
                break
    return col


def parse_sales_sheet(rows: list, force_year: Optional[int] = None):
    """머리글을 찾아 열을 매핑하고, 전표(일자-No) 단위로 묶은 목록을 반환."""
    candidates = []
    for hi in range(min(len(rows), 40)):
        candidates.append((hi + 1, [_norm_header(h) for h in rows[hi]]))
        if hi + 1 < len(rows):  # 두 줄로 된 머리글 (예: 금액 / 공급가액·부가세)
            nxt = rows[hi + 1]
            merged = [_norm_header(a) + _norm_header(nxt[i] if i < len(nxt) else "") for i, a in enumerate(rows[hi])]
            candidates.append((hi + 2, merged))
    for start, cells in candidates:
        if not any(any(k in h for k in CUST_WORDS) for h in cells):
            continue
        col = _map_sale_cols(cells)
        if "customer" not in col or ("slip" not in col and "date" not in col) or not (
                {"qty", "supply", "total", "price"} & set(col)):
            continue
        hi = start - 1
        groups, order = {}, []
        # 조회 기간(예: "회사명 : ○○ / 2000/01/01 ~ 2026/10/02")의 마지막 날짜 → 연도 없는 "11/16-1" 의 연도 추정용
        end_date = start_date = None
        for r in rows[:hi + 1]:
            found = re.findall(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", " ".join(str(v or "") for v in r))
            if found:
                start_date = tuple(int(x) for x in found[0])
                end_date = tuple(int(x) for x in found[-1])
        # 연도를 확실히 알 수 있으면 고정: 직접 지정 > 조회기간이 한 해 안
        fixed_year = force_year or (end_date[0] if end_date and start_date and start_date[0] == end_date[0] else None)
        parsed = []
        for r in rows[hi + 1:]:
            get = lambda k: (re.sub(r"\s+", " ", str(r[col[k]])).strip()
                             if k in col and col[k] < len(r) and r[col[k]] is not None else "")
            cust = get("customer")
            if not cust or any(w in cust for w in ("합계", "소계", "총계")) or cust.endswith(" 계"):
                continue
            raw = get("slip") or get("date")
            m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?:[^\d-]*-\s*(\d+))?", raw)
            if m:
                ymd, no = (int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4)
            else:
                m = re.search(r"^(\d{1,2})[./-](\d{1,2})(?:\s*-\s*(\d+))?", raw)
                if not m:
                    continue
                ymd, no = (None, int(m.group(1)), int(m.group(2))), m.group(3)
            name = get("name")
            if not name and not get("prod_cd"):
                continue
            qty, price = _num(get("qty")), _num(get("price"))
            vat = _num(get("vat")) if "vat" in col else None
            if "supply" in col and _num(get("supply")):
                supply = _num(get("supply"))
            elif "total" in col and _num(get("total")):
                # 공급가액 열이 없고 합계(부가세 포함)만 있는 양식
                supply = _num(get("total")) - (vat or 0)
            else:
                supply = round(qty * price)
                if vat is None:
                    vat = int(supply * 0.1)
            note = get("note")
            if get("vendor"):
                note = (note + " / " if note else "") + "구매처: " + get("vendor")
            parsed.append({"ymd": ymd, "no": no, "cust": cust, "cust_cd": get("cust_cd"), "title": get("title"),
                           "transport": get("transport"), "item": {
                "prod_cd": get("prod_cd"), "cas_no": get("cas_no"), "origin": get("origin"), "name": name, "spec": get("spec"), "unit": get("unit"), "qty": qty,
                "price": price, "supply": int(round(supply)), "vat": int(round(vat or 0)), "note": note}})
        # 연도 없는 날짜: 목록이 날짜순이라고 보고 아래(최근)에서 위로 올라가며 월/일이 커지면 한 해 전으로
        year = end_date[0] if end_date else datetime.now().year
        prev = None
        for p in reversed(parsed):
            y, mo, d = p["ymd"]
            if y is None and fixed_year:
                p["ymd"] = (fixed_year, mo, d)
                continue
            if y is None:
                if prev and (mo, d) > prev:
                    year -= 1
                elif prev is None and end_date and (mo, d) > end_date[1:]:
                    year -= 1
                p["ymd"] = (year, mo, d)
            else:
                year = y
            prev = (p["ymd"][1], p["ymd"][2])
        groups, order = {}, []
        for p in parsed:
            date = "%04d-%02d-%02d" % p["ymd"]
            key = f"{date}|{p['no']}" if p["no"] else f"{date}|{p['cust']}"
            if key not in groups:
                groups[key] = {"date": date, "slip": f"{date.replace('-', '')}-{p['no']}" if p["no"] else "",
                               "customer": p["cust"], "cust_cd": p["cust_cd"], "title": p["title"],
                               "transport": p["transport"], "items": []}
                order.append(key)
            groups[key]["items"].append(p["item"])
        for g in groups.values():
            g["title"] = g["title"] or auto_title([i["name"] for i in g["items"]])
        return [groups[k] for k in order], sorted(col) + (["year_fixed"] if fixed_year else [])
    seen = [" | ".join(str(v).strip() for v in r if str(v or "").strip())[:150] for r in rows[:12]
            if any(str(v or "").strip() for v in r)][:5]
    raise HTTPException(400, "머리글에서 '거래처명'과 '일자(일자-No.)', '수량/금액' 열을 찾지 못했습니다."
                             " 이 메시지를 캡처해서 보내주세요. 파일 앞부분: " + " // ".join(seen))


def auto_title(names: list) -> str:
    """건명이 없을 때 품목으로 만든다: '첫 품목 외 n건'."""
    names = [n for n in names if n]
    if not names:
        return ""
    first = names[0] if len(names[0]) <= 40 else names[0][:40] + "…"
    return first + (f" 외 {len(names) - 1}건" if len(names) > 1 else "")


def insert_import_items(c, qid: int, items: list):
    for seq, it in enumerate(items, 1):
        c.execute(
            "INSERT INTO quote_items (quote_id, seq, prod_cd, cas_no, origin, name, spec, unit, qty, unit_price, supply,"
            " vat, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (qid, seq, it["prod_cd"], it.get("cas_no", ""), it.get("origin", ""), it["name"], it["spec"], it["unit"], it["qty"], it["price"],
             it["supply"], it["vat"], it["note"]),
        )


@app.post("/api/statements/import")
async def import_statements(file: UploadFile = File(...), dry_run: bool = True, user: dict = Depends(admin_user)):
    return await import_docs(file, "statement", dry_run, user)


@app.post("/api/docs/import")
async def import_docs_api(file: UploadFile = File(...), doc_type: str = "statement", dry_run: bool = True,
                          overwrite: bool = False, year: Optional[int] = None, user: dict = Depends(admin_user)):
    if doc_type not in DOC_TYPES:
        raise HTTPException(400, "잘못된 문서 종류입니다.")
    return await import_docs(file, doc_type, dry_run, user, overwrite, year)


IMPORT_NOTE = "이카운트에서 가져옴"


@app.delete("/api/docs/imported")
def delete_imported(doc_type: str, _: dict = Depends(admin_user)):
    """엑셀로 가져온 문서(비고가 '이카운트에서 가져옴' 그대로인 것)를 모두 삭제 — 잘못 가져왔을 때 다시 하기용."""
    if doc_type not in DOC_TYPES:
        raise HTTPException(400, "잘못된 문서 종류입니다.")
    with db() as c:
        n = c.execute("DELETE FROM quotes WHERE doc_type = ? AND note = ?", (doc_type, IMPORT_NOTE)).rowcount
    return {"deleted": n}


async def import_docs(file: UploadFile, doc_type: str, dry_run: bool, user: dict, overwrite: bool = False,
                      year: Optional[int] = None):
    """이카운트 판매조회(거래명세서) / 견적서조회(견적서) 엑셀 가져오기."""
    slip_col = "ecount_sale_slip" if doc_type == "statement" else "ecount_quote_slip"
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "파일이 너무 큽니다 (20MB 이하).")
    slips, cols = parse_sales_sheet(parse_sheet(file.filename or "", data), year)
    created = skipped = updated = 0
    with db() as c:
        cust_codes = {r["name"]: r["code"] for r in c.execute("SELECT code, name FROM ecount_customers")}
        for g in slips:
            dup = g["slip"] and c.execute(
                f"SELECT id FROM quotes WHERE doc_type = ? AND {slip_col} = ?", (doc_type, g["slip"])).fetchone()
            g["duplicate"] = bool(dup)
            supply_total = sum(i["supply"] for i in g["items"])
            vat_total = sum(i["vat"] for i in g["items"])
            if dup and not overwrite:
                skipped += 1
                continue
            if dry_run:
                continue
            ts = now()
            if dup:
                # 덮어쓰기: 같은 전표로 가져왔던 문서의 품목/금액을 새 엑셀 내용으로 교체
                qid = dup["id"]
                c.execute("UPDATE quotes SET customer_name = ?, quote_date = ?, title = ?, supply_total = ?, vat_total = ?,"
                          " grand_total = ?, updated_at = ? WHERE id = ?",
                          (g["customer"], g["date"], g["title"], supply_total, vat_total, supply_total + vat_total,
                           ts, qid))
                c.execute("DELETE FROM quote_items WHERE quote_id = ?", (qid,))
                insert_import_items(c, qid, g["items"])
                updated += 1
                continue
            quote_no = next_quote_no(c, g["date"], doc_type)
            cur = c.execute(
                "INSERT INTO quotes (quote_no, doc_type, title, transport, customer_name, cust_cd, quote_date, vat_mode,"
                f" status, note, supply_total, vat_total, grand_total, {slip_col}, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'separate', 'sent', ?, ?, ?, ?, ?, ?, ?, ?)",
                (quote_no, doc_type, g["title"], g.get("transport", ""), g["customer"], g["cust_cd"] or cust_codes.get(g["customer"], ""), g["date"],
                 IMPORT_NOTE, supply_total, vat_total, supply_total + vat_total, g["slip"],
                 user["id"], ts, ts),
            )
            insert_import_items(c, cur.lastrowid, g["items"])
            created += 1
    by_date = sorted(slips, key=lambda g: g["date"])
    pick = by_date if len(by_date) <= 10 else by_date[:4] + [None] + by_date[-5:]
    preview = [None if g is None else {
        "date": g["date"], "slip": g["slip"], "customer": g["customer"], "title": g["title"], "lines": len(g["items"]),
        "total": sum(i["supply"] + i["vat"] for i in g["items"]), "duplicate": g["duplicate"]} for g in pick]
    years = {}
    for g in slips:
        years[g["date"][:4]] = years.get(g["date"][:4], 0) + 1
    period = [by_date[0]["date"], by_date[-1]["date"]] if by_date else []
    vat_assumed = not ({"supply", "total", "vat"} & set(cols))
    return {"slips": len(slips), "lines": sum(len(g["items"]) for g in slips), "created": created,
            "skipped": skipped, "updated": updated, "vat_assumed": vat_assumed, "years": years, "period": period, "columns": cols, "preview": preview, "dry_run": dry_run}


@app.get("/api/ecount/logs")
def ecount_logs(_: dict = Depends(admin_user)):
    with db() as c:
        rows = c.execute(
            "SELECT l.*, q.quote_no, u.name AS user_name FROM ecount_log l LEFT JOIN quotes q ON q.id = l.quote_id"
            " LEFT JOIN users u ON u.id = l.user_id ORDER BY l.id DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 매출(납품) 분석
DEFAULT_EXCLUDE = "켐코스, 이알씨"   # 자사·관계사: 매출분석에서 제외


def _norm_company(n: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s", "", str(n or "")).lower()


def exclude_matcher(c):
    """매출분석 제외 거래처 판별 함수 (설정의 쉼표 구분 목록, 이름 일부 일치)."""
    raw = get_setting(c, "analytics_exclude", DEFAULT_EXCLUDE)
    keys = [_norm_company(k) for k in raw.split(",") if _norm_company(k)]
    return lambda name: any(k in _norm_company(name) for k in keys)


# 같은 회사 다른 이름 합치기 (예: 녹원 / 녹원산업 / (주)녹원)
COMPANY_SUFFIX = r"(산업|상사|무역|화학|코리아|인터내셔널|기업|컴퍼니|corporation|corp|coltd|ltd|inc|co)$"


def _company_key(n: str) -> str:
    """이름 비교용 키: (주)·주식회사·띄어쓰기·기호를 빼고, 끝에 붙는 산업/상사 등도 뺀다.
    '1공장', '2공장' 처럼 공장·지점 표시는 남겨 서로 다른 곳으로 본다."""
    k = re.sub(r"[\s.,\-_·&＆()\[\]]", "", _norm_company(n))
    for _ in range(2):
        k = re.sub(COMPANY_SUFFIX, "", k)
    return k


def alias_map(c) -> dict:
    try:
        return json.loads(get_setting(c, "analytics_alias", "{}")) or {}
    except ValueError:
        return {}


def canon_fn(c):
    m = alias_map(c)
    return lambda n: m.get(n, n)


@app.get("/api/analytics/aliases")
def get_aliases(_: dict = Depends(current_user)):
    with db() as c:
        amap = alias_map(c)
        names = c.execute("SELECT customer_name, COUNT(*), SUM(supply_total) FROM quotes WHERE doc_type = 'statement'"
                          " AND status != 'draft' GROUP BY 1").fetchall()
    groups = {}
    for n, cnt, amt in names:
        groups.setdefault(_company_key(n), []).append({"name": n, "count": cnt, "amount": amt or 0})
    suggestions = []
    for key, g in groups.items():
        if len(g) < 2 or not key:
            continue
        canon = {amap.get(x["name"], x["name"]) for x in g}
        if len(canon) == 1 and all(x["name"] in amap or x["name"] in canon for x in g):
            continue   # 이미 하나로 합쳐짐
        g.sort(key=lambda x: -x["amount"])
        suggestions.append({"names": g, "canonical": g[0]["name"]})
    suggestions.sort(key=lambda s: -sum(x["amount"] for x in s["names"]))
    merged = {}
    for alias, canon in amap.items():
        merged.setdefault(canon, []).append(alias)
    return {"map": amap, "merged": [{"canonical": k, "aliases": sorted(v)} for k, v in sorted(merged.items())],
            "suggestions": suggestions, "all_names": sorted(n for n, _c, _a in names)}


@app.put("/api/analytics/aliases")
def put_aliases(body: dict, _: dict = Depends(admin_user)):
    amap = {str(k).strip(): str(v).strip() for k, v in (body.get("map") or {}).items()
            if str(k).strip() and str(v).strip() and str(k).strip() != str(v).strip()}
    with db() as c:
        set_setting(c, "analytics_alias", json.dumps(amap, ensure_ascii=False))
    return {"ok": True, "count": len(amap)}


@app.get("/api/analytics/exclude")
def get_exclude(_: dict = Depends(current_user)):
    with db() as c:
        return {"value": get_setting(c, "analytics_exclude", DEFAULT_EXCLUDE)}


@app.put("/api/analytics/exclude")
def put_exclude(body: dict, _: dict = Depends(admin_user)):
    with db() as c:
        set_setting(c, "analytics_exclude", str(body.get("value", "")).strip())
    return {"ok": True}


@app.get("/api/analytics/sales")
def sales_analytics(year: int = 0, basis: str = "supply", _: dict = Depends(current_user)):
    """거래명세서(발행·출고완료) 기준 거래처별 월별/연별 납품금액."""
    col = "grand_total" if basis == "total" else "supply_total"
    base = "FROM quotes WHERE doc_type = 'statement' AND status != 'draft' AND NOT excl(customer_name)"
    with db() as c:
        is_ex, canon = exclude_matcher(c), canon_fn(c)
        c.create_function("excl", 1, lambda n: 1 if is_ex(canon(n)) else 0, deterministic=True)
        c.create_function("canon", 1, canon, deterministic=True)
        years = [int(r[0]) for r in c.execute(f"SELECT DISTINCT substr(quote_date, 1, 4) {base} ORDER BY 1")]
        if not years:
            return {"years": [], "year": None, "monthly": {}, "customers": [], "yearly": {}}
        year = year if year in years else years[-1]
        monthly = {}  # {연도: [1~12월 합계]}
        for y in (year, year - 1):
            arr = [0] * 12
            for m, v in c.execute(f"SELECT CAST(substr(quote_date, 6, 2) AS INT), SUM({col}) {base}"
                                  " AND substr(quote_date, 1, 4) = ? GROUP BY 1", (str(y),)):
                arr[m - 1] = v or 0
            monthly[y] = arr
        # 거래처 × (기준연도 월별, 전년도 월별)
        cust = {}
        for name, y, m, v in c.execute(
                f"SELECT canon(customer_name), CAST(substr(quote_date, 1, 4) AS INT), CAST(substr(quote_date, 6, 2) AS INT),"
                f" SUM({col}) {base} AND substr(quote_date, 1, 4) IN (?, ?) GROUP BY 1, 2, 3", (str(year), str(year - 1))):
            e = cust.setdefault(name, {"name": name, "cur": [0] * 12, "prev": [0] * 12})
            (e["cur"] if y == year else e["prev"])[m - 1] = v or 0
        # 거래처 × 연도 합계
        yearly = {}
        for name, y, v in c.execute(f"SELECT canon(customer_name), CAST(substr(quote_date, 1, 4) AS INT), SUM({col}) {base}"
                                    " GROUP BY 1, 2"):
            yearly.setdefault(name, {})[y] = v or 0
        year_totals = {y: v or 0 for y, v in c.execute(
            f"SELECT CAST(substr(quote_date, 1, 4) AS INT), SUM({col}) {base} GROUP BY 1")}
    return {"years": years, "year": year, "monthly": monthly, "customers": list(cust.values()),
            "yearly": yearly, "year_totals": year_totals}


def _shift_months(d, months: int):
    """date 를 months 개월 이동 (말일 보정)."""
    import calendar
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


@app.get("/api/analytics/products")
def product_analytics(basis: str = "supply", user: dict = Depends(current_user)):
    """거래명세서(발행·출고완료) 품목 줄 → 제품 × 연도 · 거래처 × 연도 수량·금액.
    제품은 포장 표시('(25kg *4)')·용도 표시('_식')를 빼고 같은 이름끼리 묶는다."""
    with db() as c:
        is_ex, canon = exclude_matcher(c), canon_fn(c)
        rows = c.execute(
            "SELECT q.quote_date, q.customer_name, i.name, i.qty, i.unit, i.supply, i.vat FROM quote_items i"
            " JOIN quotes q ON q.id = i.quote_id WHERE q.doc_type = 'statement' AND q.status != 'draft' AND i.name != ''"
        ).fetchall()
    prods, custs, names, units, years = {}, {}, {}, {}, set()
    for r in rows:
        cust = canon(r["customer_name"])
        if is_ex(cust):
            continue
        y = r["quote_date"][:4]
        if not y.isdigit():
            continue
        years.add(int(y))
        base = item_base(r["name"])
        key = _item_norm(_CODE_RE.sub("", base)) or _item_norm(base)
        if not key:
            continue
        names.setdefault(key, {}).setdefault(base, 0)
        names[key][base] += 1
        u = (r["unit"] or "").strip().lower() or "kg"
        units.setdefault(key, {}).setdefault(u, 0)
        units[key][u] += 1
        amt = (r["supply"] or 0) + ((r["vat"] or 0) if basis == "total" else 0)
        qty = r["qty"] or 0
        for bucket, k1, k2 in ((prods, key, cust), (custs, cust, key)):
            e = bucket.setdefault(k1, {"years": {}, "by": {}, "last": ""})
            yy = e["years"].setdefault(y, {"qty": 0, "amount": 0, "count": 0})
            yy["qty"] += qty
            yy["amount"] += amt
            yy["count"] += 1
            b = e["by"].setdefault(k2, {})
            by = b.setdefault(y, {"qty": 0, "amount": 0, "count": 0})
            by["qty"] += qty
            by["amount"] += amt
            by["count"] += 1
            e["last"] = max(e["last"], r["quote_date"])
    pname = {k: max(v, key=v.get) for k, v in names.items()}
    punit = {k: max(v, key=v.get) for k, v in units.items()}

    def pack(bucket, label, sub_label):
        out = []
        for k, e in bucket.items():
            out.append({"key": k, "name": label(k), "years": e["years"], "last": e["last"],
                        "by": [{"key": k2, "name": sub_label(k2), "years": ys} for k2, ys in e["by"].items()],
                        "unit": punit.get(k, "") if bucket is prods else ""})
        return out
    return {"years": sorted(years), "products": pack(prods, lambda k: pname[k], lambda k: k),
            "customers": pack(custs, lambda k: k, lambda k: pname[k])}


@app.get("/api/analytics/decline")
def decline_analytics(months: int = 12, compare: str = "last_year", basis: str = "supply",
                      _: dict = Depends(current_user)):
    """납품이 줄어든 거래처(품목별 감소 포함)와 주문이 끊긴 거래처."""
    col = "grand_total" if basis == "total" else "supply_total"
    item_col = "supply + vat" if basis == "total" else "supply"
    months = max(1, min(months, 24))
    with db() as c:
        c.create_function("canon", 1, canon_fn(c), deterministic=True)
        last = c.execute("SELECT MAX(quote_date) FROM quotes WHERE doc_type = 'statement' AND status != 'draft'").fetchone()[0]
        if not last:
            return {"ref": None, "rows": [], "dormant": []}
        # 기준일: 오늘 (데이터가 오늘보다 옛날에 끝나면 마지막 거래일)
        ref = min(datetime.now().date(), datetime.strptime(last, "%Y-%m-%d").date())
        cur_from = _shift_months(ref, -months) + timedelta(days=1)
        if compare == "previous":
            cmp_to, cmp_from = cur_from - timedelta(days=1), _shift_months(cur_from, -months)
        else:
            cmp_from, cmp_to = _shift_months(cur_from, -12), _shift_months(ref, -12)
        rng = lambda a, b: (a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d"))
        cur_r, cmp_r = rng(cur_from, ref), rng(cmp_from, cmp_to)

        def per_customer(r):
            return {n: v or 0 for n, v in c.execute(
                f"SELECT canon(customer_name), SUM({col}) FROM quotes WHERE doc_type = 'statement' AND status != 'draft'"
                " AND quote_date BETWEEN ? AND ? GROUP BY 1", r)}

        def per_item(r):
            out = {}
            for n, item, unit, qty, amt in c.execute(
                    f"SELECT canon(q.customer_name), qi.name, MAX(qi.unit), SUM(qi.qty), SUM(qi.{item_col})"
                    " FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
                    " WHERE q.doc_type = 'statement' AND q.status != 'draft' AND q.quote_date BETWEEN ? AND ?"
                    " GROUP BY 1, 2", r):
                out.setdefault(n, {})[item] = (qty or 0, amt or 0, unit or "")
            return out

        is_ex = exclude_matcher(c)
        keep = lambda d: {k: v for k, v in d.items() if not is_ex(k)}
        cur_c, cmp_c = keep(per_customer(cur_r)), keep(per_customer(cmp_r))
        cur_i, cmp_i = keep(per_item(cur_r)), keep(per_item(cmp_r))
        rows = []
        for name, before in cmp_c.items():
            now_amt = cur_c.get(name, 0)
            if before <= 0 or now_amt >= before:
                continue
            items = []
            for item, (bq, ba, unit) in cmp_i.get(name, {}).items():
                nq, na, _u = cur_i.get(name, {}).get(item, (0, 0, unit))
                if na < ba:
                    items.append({"name": item, "unit": unit, "before_qty": bq, "now_qty": nq,
                                  "before_amt": ba, "now_amt": na, "drop": ba - na})
            items.sort(key=lambda x: -x["drop"])
            rows.append({"name": name, "before": before, "now": now_amt, "drop": before - now_amt,
                         "rate": (now_amt - before) / before * 100, "items": items})
        rows.sort(key=lambda r: -r["drop"])

        # 주문 간격 분석: 거래처별 주문 날짜(최근 3년)
        since = _shift_months(ref, -36).strftime("%Y-%m-%d")
        dates = {}
        for n, d in c.execute("SELECT canon(customer_name), quote_date FROM quotes WHERE doc_type = 'statement'"
                              " AND status != 'draft' AND quote_date BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY 2",
                              (since, ref.strftime("%Y-%m-%d"))):
            dates.setdefault(n, []).append(datetime.strptime(d, "%Y-%m-%d").date())
        year_amt = per_customer(rng(_shift_months(ref, -12) + timedelta(days=1), ref))
        prev_year_amt = per_customer(rng(_shift_months(ref, -24) + timedelta(days=1), _shift_months(ref, -12)))
        dormant = []
        for n, ds in dates.items():
            if len(ds) < 4 or is_ex(n):
                continue
            gaps = sorted((b - a).days for a, b in zip(ds, ds[1:]))
            typical = gaps[len(gaps) // 2]  # 중앙값: 가끔 있는 긴 공백에 덜 흔들림
            since_last = (ref - ds[-1]).days
            limit = max(typical * 2.5, typical + 30, 45)
            if since_last >= limit:
                dormant.append({"name": n, "orders": len(ds), "typical_gap": typical, "last": ds[-1].isoformat(),
                                "days": since_last, "ratio": since_last / max(typical, 1),
                                "year_amt": year_amt.get(n, 0), "prev_year_amt": prev_year_amt.get(n, 0)})
        dormant.sort(key=lambda r: (-(r["prev_year_amt"] + r["year_amt"])))
        dormant_names = {r["name"] for r in dormant}

        # 품목별 구매 패턴: 거래처 × 품목의 평소 구매 주기(최근 5년)보다 늦어진 것
        since5 = _shift_months(ref, -60).strftime("%Y-%m-%d")
        hist = {}
        for n, item, d, q, a in c.execute(
                f"SELECT canon(q.customer_name), qi.name, q.quote_date, SUM(qi.qty), SUM(qi.{item_col})"
                " FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
                " WHERE q.doc_type = 'statement' AND q.status != 'draft' AND q.quote_date BETWEEN ? AND ?"
                " GROUP BY 1, 2, 3 ORDER BY 3", (since5, ref.strftime("%Y-%m-%d"))):
            hist.setdefault((n, item), []).append((datetime.strptime(d, "%Y-%m-%d").date(), q or 0, a or 0))
        units = {(n, i): u for n, i, u in c.execute(
            "SELECT canon(q.customer_name), qi.name, MAX(qi.unit) FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
            " WHERE q.doc_type = 'statement' AND q.quote_date >= ? GROUP BY 1, 2", (since5,))}
        overdue = {}
        for (n, item), buys in hist.items():
            if len(buys) < 3 or is_ex(n):
                continue
            gaps = sorted((b[0] - a[0]).days for a, b in zip(buys, buys[1:]) if (b[0] - a[0]).days > 3)
            if len(gaps) < 2:
                continue
            g = gaps[len(gaps) // 2]                     # 평소 주기 (중앙값)
            last_d = buys[-1][0]
            since_last = (ref - last_d).days
            grace = min(max(g * 0.5, 30), 60)            # 여유: 주기의 절반 (30~60일)
            if since_last <= g + grace:
                continue
            if since_last > max(g * 3, g + 180):        # 너무 오래 전에 끊긴 품목은 제외 (최근에 늦어진 것만)
                continue
            span_years = max((buys[-1][0] - buys[0][0]).days / 365, 1)
            yearly = sum(b[2] for b in buys) / span_years   # 이 품목 연평균 금액
            qtys = sorted(b[1] for b in buys)
            overdue.setdefault(n, []).append({
                "item": item, "unit": units.get((n, item), ""), "cycle": g, "times": len(buys),
                "last": last_d.isoformat(), "days": since_last, "late": since_last - g,
                "usual_qty": qtys[len(qtys) // 2], "yearly_amt": yearly,
                "month": last_d.month if g >= 300 else None})
        patterns = [{"name": n, "items": sorted(v, key=lambda x: -x["yearly_amt"]),
                     "value": sum(x["yearly_amt"] for x in v), "stopped": n in dormant_names}
                    for n, v in overdue.items()]
        patterns.sort(key=lambda r: -r["value"])
    return {"ref": ref.isoformat(), "cur": cur_r, "cmp": cmp_r, "rows": rows, "dormant": dormant, "patterns": patterns,
            "cur_total": sum(cur_c.values()), "cmp_total": sum(cmp_c.values())}


# ---------------------------------------------------------------- 재고(나스 엑셀) · 원가 · 이익
INV_H1 = {"location": ("위치",), "loc_detail": ("위치상세",), "info": ("상세정보",), "kind": ("유형",),
          "order_note": ("발주시기",), "bat_no": ("batno",), "item": ("품목",), "lot_no": ("재고번호",),
          "import_qty": ("수입수량", "수량"), "packing": ("packing",), "origin": ("origin",), "mfg_date": ("제조일자",),
          "transport": ("운송수단",), "cost_fx": ("원가(외화)",), "customs_date": ("통관날짜", "통관일자"),
          "stock_qty": ("재고량",), "stock_amt": ("재고금액",), "cas": ("cas", "casno")}
INV_H2 = {"fema": ("fema",), "expiry": ("소비기한", "유통기한"), "cost_krw": ("원가(원화)",), "rate": ("기준환율", "환율")}
INV_TEXT = ("location", "loc_detail", "info", "kind", "order_note", "bat_no", "item", "lot_no", "packing", "origin",
            "mfg_date", "transport", "customs_date", "cas", "fema", "expiry")


def _inv_h(v) -> str:
    return re.sub(r"[\s.\-_]", "", str(v or "")).lower()


def _numn(v):
    """숫자로 읽을 수 없으면 None (빈칸·글자)."""
    s = str(v or "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def inv_date(v) -> str:
    """250106 / 2025.01.06 / 2025-01-06 / 250312(250430) / 엑셀 날짜숫자 → 2025-01-06. 못 읽으면 ''."""
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"^(\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})(?!\d)", s)
        if m:
            y, mo, d = 2000 + int(m[1]), int(m[2]), int(m[3])
        elif re.match(r"^\d{5}(\.0+)?$", s):     # 엑셀 날짜 일련번호
            dt = datetime(1899, 12, 30) + timedelta(days=int(float(s)))
            y, mo, d = dt.year, dt.month, dt.day
        else:
            return ""
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_inventory_sheet(name: str, rows: list) -> list:
    """재고 시트: 머리글 2줄(1줄: 품목·재고번호·출고일/거래처…, 2줄: Fema·원가(원화)·출고량/납품가…),
    로트 하나가 2줄(윗줄: 품목·수량·통관·출고일·거래처 /아랫줄: 원가(원화)·환율·출고량·납품가)."""
    hi = next((i for i, r in enumerate(rows[:10]) if "품목" in [_inv_h(x) for x in r]
               and "출고일" in [_inv_h(x) for x in r]), None)
    if hi is None:
        return []
    h1 = [_inv_h(x) for x in rows[hi]]
    h2 = [_inv_h(x) for x in (rows[hi + 1] if hi + 1 < len(rows) else [])]

    def find(hdr, keys):
        for k in keys:   # 정확히 일치 우선
            if k in hdr:
                return hdr.index(k)
        return next((i for i, h in enumerate(hdr) if h and any(h.startswith(k) for k in keys)), None)

    col1 = {f: find(h1, ks) for f, ks in INV_H1.items()}
    if col1["import_qty"] == col1["stock_qty"]:
        col1["import_qty"] = find(h1, ("수입수량",))
    col2 = {f: find(h2, ks) for f, ks in INV_H2.items()}
    if col1["cas"] is None and h2 and h2[0].startswith("fema"):
        col1["cas"] = 0      # CAS 머리글이 비어 있는 경우(Fema 윗칸)
    pairs = [j for j, h in enumerate(h1) if h == "출고일"]
    item_c = col1["item"]
    g = lambda r, j: (r[j] if j is not None and j < len(r) else "")
    lots = []
    i = hi + 2
    while i < len(rows):
        a = rows[i]
        b = rows[i + 1] if i + 1 < len(rows) else []
        item = str(g(a, item_c)).strip()
        if not item or g(b, item_c):
            i += 1
            continue
        nums = [_numn(g(a, col1[k])) for k in ("import_qty", "stock_qty", "cost_fx")] + [_numn(g(b, col2["cost_krw"]))]
        if all(n is None for n in nums):
            i += 1     # 구분 줄(예: Flavour natural)·색 설명 줄
            continue
        lot = {"sheet": name, "row_no": i + 1, "item": item}
        for f in INV_TEXT:
            src = (b, col2) if f in col2 else (a, col1)
            lot[f] = str(g(src[0], src[1].get(f))).strip()
        for f in ("expiry", "mfg_date", "customs_date"):
            lot[f] = inv_date(lot[f]) or lot[f]
        lot["location"] = (lot["location"] + " " + lot.pop("loc_detail")).strip()
        for f in ("import_qty", "cost_fx", "stock_qty", "stock_amt"):
            lot[f] = _numn(g(a, col1[f]))
        lot["cost_krw"] = _numn(g(b, col2["cost_krw"]))
        lot["rate"] = _numn(g(b, col2["rate"]))
        lot["cost_est"] = 0
        if not lot["cost_krw"] and lot["cost_fx"] and lot["rate"]:
            lot["cost_krw"], lot["cost_est"] = round(lot["cost_fx"] * lot["rate"], 2), 1
        ships = []
        for j in pairs:
            d = inv_date(g(a, j))
            if not d:
                continue
            qty, pr = _numn(g(b, j)), g(b, j + 1)
            price = _numn(pr)
            ships.append({"ship_date": d, "customer": str(g(a, j + 1)).strip(), "qty": qty or 0,
                          "price": price, "note": "" if price is not None else str(pr).strip()})
        lot["ships"] = ships
        lots.append(lot)
        i += 2
    return lots


def parse_inventory(data: bytes) -> list:
    lots = []
    for name in xlsx_sheet_names(data):
        try:
            lots += parse_inventory_sheet(name, read_xlsx_values(data, name))
        except Exception:
            continue
    # 원가(원화)가 아직 없는 로트(막 통관된 것 등): 같은 품목의 가장 최근 원가를 참고값으로
    latest = {}
    for l in sorted(lots, key=lambda l: l["customs_date"]):
        if l["cost_krw"] and not l["cost_est"]:
            latest[l["item"].lower()] = l["cost_krw"]
    for l in lots:
        if not l["cost_krw"] and latest.get(l["item"].lower()):
            l["cost_krw"], l["cost_est"] = latest[l["item"].lower()], 2
    return lots


INV_COLS = ("sheet", "row_no", "item", "cas", "fema", "kind", "location", "info", "order_note", "lot_no", "bat_no",
            "packing", "origin", "mfg_date", "expiry", "transport", "customs_date", "import_qty", "cost_fx", "rate",
            "cost_krw", "cost_est", "stock_qty", "stock_amt")


def file_date(filename: str) -> str:
    """파일 이름의 날짜(재고 기준일): '재고 2026.10.01.xlsx', '재고_20261001', '261001' → 2026-10-01."""
    stem = re.sub(r"\.\w+$", "", filename or "")
    for pat in (r"(20\d{2})[.\-_ ]?(\d{1,2})[.\-_ ]?(\d{1,2})(?!\d)", r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)"):
        for m in re.finditer(pat, stem):
            y = int(m[1]) + (2000 if len(m[1]) == 2 else 0)
            try:
                return datetime(y, int(m[2]), int(m[3])).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def save_inventory(c, lots: list, filename: str, by: str) -> dict:
    c.execute("DELETE FROM inv_ships")
    c.execute("DELETE FROM inv_lots")
    n_ship = 0
    for l in lots:
        cur = c.execute(f"INSERT INTO inv_lots ({', '.join(INV_COLS)}) VALUES ({', '.join('?' * len(INV_COLS))})",
                        [l.get(k) for k in INV_COLS])
        c.executemany("INSERT INTO inv_ships (lot_id, ship_date, customer, qty, price, note) VALUES (?, ?, ?, ?, ?, ?)",
                      [(cur.lastrowid, s["ship_date"], s["customer"], s["qty"], s["price"], s["note"]) for s in l["ships"]])
        n_ship += len(l["ships"])
    meta = {"filename": filename, "file_date": file_date(filename), "uploaded_at": now(), "by": by,
            "lots": len(lots), "ships": n_ship}
    set_setting(c, "inv_meta", json.dumps(meta, ensure_ascii=False))
    return meta


def inv_meta(c) -> dict:
    try:
        return json.loads(get_setting(c, "inv_meta", "{}")) or {}
    except ValueError:
        return {}


def can_see_cost(c, user: dict) -> bool:
    return user["role"] == "admin" or get_setting(c, "profit_public", "0") == "1"


def upload_user(authorization: str = Header(default=""), x_upload_key: str = Header(default="")) -> dict:
    """관리자 로그인 또는 사무실 PC 자동 업로드용 키(X-Upload-Key)."""
    if x_upload_key:
        with db() as c:
            key = get_setting(c, "inv_upload_key", "")
        if key and secrets.compare_digest(key, x_upload_key.strip()):
            return {"name": "자동 업로드", "role": "auto"}
        raise HTTPException(403, "업로드 키가 맞지 않습니다.")
    return admin_user(current_user(authorization))


@app.post("/api/inventory/upload")
async def inventory_upload(file: UploadFile = File(...), dry_run: bool = False, user: dict = Depends(upload_user),
                           x_file_name: str = Header(default="")):
    data = await file.read()
    filename = urllib.parse.unquote(x_file_name) if x_file_name else (file.filename or "")
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "xlsx 엑셀 파일만 올릴 수 있습니다.")
    try:
        lots = parse_inventory(data)
    except Exception as e:
        raise HTTPException(400, f"엑셀을 읽지 못했습니다: {e}")
    if not lots:
        raise HTTPException(400, "재고 시트(품목·출고일 머리글)를 찾지 못했습니다.")
    if dry_run:
        return {"lots": len(lots), "ships": sum(len(l["ships"]) for l in lots), "sheets": sorted({l["sheet"] for l in lots})}
    with db() as c:
        cur = inv_meta(c)
        new_d, cur_d = file_date(filename), cur.get("file_date", "")
        if user["role"] == "auto" and new_d and cur_d and new_d < cur_d:
            # 자동 업로드가 더 예전 날짜 파일을 올리려 하면 덮어쓰지 않음 (직접 올리기는 허용)
            return {**cur, "skipped": f"이미 {cur_d} 재고가 올라가 있어 {filename} 은(는) 건너뜀"}
        return save_inventory(c, lots, filename, user["name"])


@app.get("/api/inventory/settings")
def inventory_settings(_: dict = Depends(admin_user)):
    with db() as c:
        return inv_settings(c)


@app.put("/api/inventory/settings")
def inventory_put_settings(body: dict, _: dict = Depends(admin_user)):
    with db() as c:
        if body.get("new_key"):
            set_setting(c, "inv_upload_key", secrets.token_urlsafe(24))
        if "profit_public" in body:
            set_setting(c, "profit_public", "1" if body["profit_public"] else "0")
        for k in ("inv_dir", "ledger_dir", "inbound_dir"):
            if k in body:
                set_setting(c, k, str(body[k] or "").strip())
        return inv_settings(c)


def inv_settings(c) -> dict:
    return {"upload_key": get_setting(c, "inv_upload_key", ""), "profit_public": get_setting(c, "profit_public", "0") == "1",
            "inv_dir": get_setting(c, "inv_dir", "Z:\\VOL1\\공유문서\\창고관리"), "ledger_dir": get_setting(c, "ledger_dir", ""),
            "inbound_dir": get_setting(c, "inbound_dir", "Z:\\VOL1\\공유문서")}


@app.get("/api/inventory")
def inventory_list(user: dict = Depends(current_user)):
    """로트 목록(품목별로 화면에서 묶음). 원가는 볼 수 있는 사람에게만."""
    with db() as c:
        cost = can_see_cost(c, user)
        lots = [dict(r) for r in c.execute("SELECT * FROM inv_lots ORDER BY item COLLATE NOCASE, customs_date")]
        last = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT lot_id, MAX(ship_date), COUNT(*) FROM inv_ships GROUP BY lot_id")}
        meta = inv_meta(c)
    for l in lots:
        l["last_ship"], l["ship_count"] = last.get(l["id"], ("", 0))
        if not cost:
            for k in ("cost_fx", "rate", "cost_krw", "cost_est", "stock_amt"):
                l.pop(k, None)
    return {"lots": lots, "meta": meta, "can_cost": cost, "today": datetime.now().strftime("%Y-%m-%d")}


# ---- 📈 판매 예상: 작년·올해 판매량으로 올해 남은 출고를 예상하고, 재고 + 입고예정과 비교해 부족한 품목 표시
_CODE_RE = re.compile(r"\b(?:[a-z]{1,3})?\d{5,}\b", re.I)    # 끝에 붙는 제품코드: 937450, F13841, JC163890


def _item_norm(name: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(name or "").lower())


def _item_tokens(name: str) -> frozenset:
    s = _CODE_RE.sub(" ", str(name or "").lower())
    return frozenset(t for t in re.split(r"[^0-9a-z가-힣]+", s) if t)


class ItemMatcher:
    """다른 곳(매입매출장·입고예정)의 품명 → 재고 엑셀 품목. 이름 그대로 → 코드 뺀 이름 → 단어 묶음 → 단어가 거의 같은 것
    → 관리자가 직접 연결한 것 순서."""

    def __init__(self, items, links: dict):
        self.items = list(items)
        self.links = links
        self.by_norm, self.by_code, self.by_tok = {}, {}, {}
        for it in self.items:
            self.by_norm.setdefault(_item_norm(it), it)
            self.by_code.setdefault(_item_norm(_CODE_RE.sub("", it)), it)
            self.by_tok.setdefault(_item_tokens(it), it)
        self.cache = {}

    def __call__(self, name: str):
        if name in self.cache:
            return self.cache[name]
        hit = self.links.get(name)
        if hit is not None:
            hit = hit if hit in self.items else None       # 연결 해제("")면 None
        else:
            hit = self.by_norm.get(_item_norm(name)) or self.by_code.get(_item_norm(_CODE_RE.sub("", name))) \
                or self.by_tok.get(_item_tokens(name))
            if not hit:
                # 한쪽 단어가 다른 쪽에 다 들어 있으면 같은 품목으로 봄 (예: 'Heliotropine' ↔ 'Heliotropine(Piperonal)')
                # 여러 개가 걸리면 가장 많이 겹치는 것, 그래도 애매하면 연결 안 함
                toks = _item_tokens(name)
                cands = []
                for t, it in self.by_tok.items():
                    if not t or not toks:
                        continue
                    small = t if len(t) <= len(toks) else toks
                    j = len(t & toks) / len(t | toks)
                    if (t <= toks or toks <= t) and sum(map(len, small)) >= 6:
                        cands.append((j, it))
                    elif j >= 0.75:
                        cands.append((j, it))
                cands.sort(reverse=True)
                if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
                    hit = cands[0][1]
        self.cache[name] = hit
        return hit


def item_links(c) -> dict:
    try:
        return json.loads(get_setting(c, "item_links", "{}")) or {}
    except ValueError:
        return {}


@app.get("/api/inventory/forecast")
def inventory_forecast(user: dict = Depends(current_user)):
    today = date.today()
    y, ly = today.year, today.year - 1
    md = today.strftime("%m-%d")
    frac = max((today - date(y, 1, 1)).days + 1, 1) / ((date(y, 12, 31) - date(y, 1, 1)).days + 1)
    with db() as c:
        lots = [dict(r) for r in c.execute("SELECT item, kind, location, stock_qty, expiry, customs_date FROM inv_lots")]
        inv_items = sorted({l["item"] for l in lots})
        links = item_links(c)
        match = ItemMatcher(inv_items, links)
        # 수입 중(입고예정·인바운딩)인 것 — 재고 엑셀에 없는 품목도 새 줄로 넣는다
        ships = [dict(r) for r in c.execute(
            "SELECT id, item, qty, unit, eta, status, supplier, customer, cs_in_at, cs_arrived, cs_cleared_at, note"
            " FROM shipments WHERE status != 'arrived' ORDER BY eta")]
        new_names = {}
        for sh in ships:
            if not match(sh["item"]):
                base = item_base(clean_prnm(sh["item"]))
                k = _item_norm(_CODE_RE.sub("", base)) or _item_norm(base)
                if k:
                    new_names.setdefault(k, base)
        items = sorted(set(inv_items) | set(new_names.values()))
        match = ItemMatcher(items, links)
        # 판매량 = 매입매출장(있는 연도) 과 재고 엑셀 출고 기록 중 큰 쪽 (재고 엑셀은 다 쓴 로트가 지워져 예전 연도가 적게 나옴)
        src = {"ledger": {}, "inv": {}}
        unmatched = {}
        # 매입매출장은 재고에서 나간 줄(매입처에 '(재고)' 표시)만 — 나머지는 수입해서 바로 넘긴 건이라 재고와 무관
        for r in c.execute("SELECT item, item_base, date, qty FROM ledger WHERE year IN (?, ?) AND supplier LIKE '%재고%'",
                           (y, ly)):
            it = match(r["item_base"])
            if not it:
                if match.links.get(r["item_base"]) == "":
                    continue                     # '재고 품목 아님'으로 정한 품명
                u = unmatched.setdefault(r["item_base"], {"name": r["item_base"], "qty": 0, "count": 0})
                u["qty"] += r["qty"] or 0
                u["count"] += 1
                continue
            _fc_add(src["ledger"], it, r["date"], r["qty"] or 0, y, md)
        for r in c.execute("SELECT l.item, s.ship_date, s.qty FROM inv_ships s JOIN inv_lots l ON l.id = s.lot_id"
                           " WHERE s.ship_date >= ?", (f"{ly}-01-01",)):
            _fc_add(src["inv"], r["item"], r["ship_date"], r["qty"] or 0, y, md)
        # 통관이 끝나 재고 엑셀에 이미 들어간 건(같은 품목 로트의 통관일이 그 이후)은 두 번 세지 않음
        lot_customs = {}
        for l in lots:
            d0 = str(l.get("customs_date") or "")[:10]
            lot_customs[l["item"]] = max(lot_customs.get(l["item"], ""), d0)
        incoming = {}
        for sh in ships:
            it = match(sh["item"])
            if not it:
                continue
            cl = (sh["cs_cleared_at"] or "")[:10]
            if cl and lot_customs.get(it, "") >= cl:
                continue
            q = _kg_qty(sh["qty"] or 0, sh["unit"])
            e = incoming.setdefault(it, {"qty": 0, "eta": "", "list": []})
            if q is not None:
                e["qty"] += q
                e["eta"] = min(e["eta"] or sh["eta"], sh["eta"]) if sh["eta"] else e["eta"]
            stage = ("통관" if cl else "반입" if sh["cs_in_at"] else "입항" if sh["cs_arrived"] or sh["status"] == "customs"
                     else "선적" if sh["status"] == "shipped" else "발주")
            e["list"].append({"id": sh["id"], "name": sh["item"], "qty": sh["qty"] or 0, "unit": sh["unit"] or "kg",
                              "eta": sh["eta"] or "", "stage": stage, "supplier": sh["supplier"] or "",
                              "client": sh["customer"] or "", "guessed": "추정" in (sh["note"] or "")})
        checked = json.loads(get_setting(c, f"forecast_checked:{y}", "{}") or "{}")
    stock = {}
    for l in lots:
        if (l["stock_qty"] or 0) > 0.001 and not re.search(r"폐기|불용", f"{l['kind']} {l['location']}"):
            stock[l["item"]] = stock.get(l["item"], 0) + l["stock_qty"]
    rows = []
    inv_set = set(inv_items)
    for it in items:
        a, b = src["ledger"].get(it, {}), src["inv"].get(it, {})
        pick = lambda k: max(a.get(k, 0), b.get(k, 0))
        last, last_same, ytd = pick("last"), pick("last_same"), pick("ytd")
        inc_e = incoming.get(it, {})
        if not (last or ytd):
            continue                     # 판매 기록이 없는 품목은 예상할 게 없음
        if last and last_same:
            growth = min(ytd / last_same, 2.0)        # 올해가 작년 같은 기간보다 빠르면 최대 2배까지
            fc, basis = last * growth, f"작년 × 올해 증감 {growth:.0%}"
        elif last:
            fc, basis = last, "작년 판매량"
        else:
            fc, basis = (ytd / frac if frac >= 0.15 else ytd), "올해 추세"
        fc = max(fc, ytd)
        remain = max(fc - ytd, 0)
        st, inc = stock.get(it, 0), inc_e.get("qty", 0)
        lst = inc_e.get("list", [])
        short = remain - st - inc
        # 재고로 모자라는데 인바운딩·입고예정에 이미 있으면 '부족(발주 필요)'이 아니라 '입고 예정'
        level = "ok" if remain - st <= 0.001 else "incoming" if lst else "short"
        monthly = fc / 12
        # 지금 재고가 언제 바닥날지 (남은 기간 동안 고르게 나간다고 보고) → 입고 예정일보다 빠르면 표시
        left_days = (date(y, 12, 31) - today).days + 1
        daily = remain / left_days if left_days > 0 else 0
        runout = (today + timedelta(days=int(st / daily))).isoformat() if daily > 0 and st / daily < left_days else ""
        eta = inc_e.get("eta", "")
        first = next((x for x in lst if x["eta"] == eta), None)
        rows.append({"item": it, "last": last, "ytd": ytd, "forecast": fc, "remain": remain, "stock": st,
                     "incoming": inc, "incoming_eta": eta, "incoming_guess": bool(first and first["guessed"]),
                     "incoming_list": lst, "runout": runout, "late": bool(runout and eta and eta > runout),
                     "short": max(short, 0), "short_after": max(short, 0) if lst else 0,
                     "cover_months": (st / monthly) if monthly else None, "level": level, "basis": basis,
                     "checked": checked.get(it)})
    order = {"short": 0, "incoming": 1, "ok": 2}
    rows.sort(key=lambda r: (order[r["level"]], -(r["short"] or 0) if r["level"] == "short" else r["incoming_eta"] or "9",
                             -(r["remain"] or 0)))
    return {"year": y, "elapsed": frac, "rows": rows, "items": inv_items,
            "unmatched": sorted(unmatched.values(), key=lambda u: -u["qty"])[:40]}


def _kg_qty(qty: float, unit: str):
    """입고예정 수량 → kg (모르는 단위면 None)"""
    u = re.sub(r"[^a-z]", "", str(unit or "").lower())
    if u in ("", "kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "l", "lt", "ltr"):
        return qty
    if u in ("g", "gr", "gram", "grams"):
        return qty / 1000
    if u in ("mt", "t", "ton", "tons", "tonne"):
        return qty * 1000
    return None


def _fc_add(bucket: dict, item: str, d: str, qty: float, y: int, md: str):
    e = bucket.setdefault(item, {"last": 0, "last_same": 0, "ytd": 0})
    if d[:4] == str(y):
        e["ytd"] += qty
    elif d[:4] == str(y - 1):
        e["last"] += qty
        if d[5:10] <= md:
            e["last_same"] += qty


@app.put("/api/inventory/forecast/check")
def forecast_check(body: dict, user: dict = Depends(current_user)):
    """품목 옆 '확인' 체크 (올해 기준으로 저장)."""
    item = str(body.get("item", ""))
    y = date.today().year
    with db() as c:
        checked = json.loads(get_setting(c, f"forecast_checked:{y}", "{}") or "{}")
        if body.get("checked"):
            checked[item] = {"by": user["name"], "at": now()[:16], "memo": str(body.get("memo", ""))[:100]}
        else:
            checked.pop(item, None)
        set_setting(c, f"forecast_checked:{y}", json.dumps(checked, ensure_ascii=False))
    return {"ok": True}


@app.put("/api/inventory/item-links")
def put_item_links(body: dict, _: dict = Depends(admin_user)):
    """판매 품명 → 재고 품목 직접 연결 ({"name", "item"} — item 이 "" 이면 '재고 품목 아님')"""
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "품명이 없습니다.")
    with db() as c:
        links = item_links(c)
        if body.get("remove"):
            links.pop(name, None)
        else:
            links[name] = str(body.get("item", ""))
        set_setting(c, "item_links", json.dumps(links, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/inventory/lot/{lid}")
def inventory_lot(lid: int, user: dict = Depends(current_user)):
    with db() as c:
        lot = c.execute("SELECT * FROM inv_lots WHERE id = ?", (lid,)).fetchone()
        if not lot:
            raise HTTPException(404, "로트를 찾을 수 없습니다.")
        cost = can_see_cost(c, user)
        ships = [dict(r) for r in c.execute("SELECT ship_date, customer, qty FROM inv_ships"
                                            " WHERE lot_id = ? ORDER BY ship_date DESC", (lid,))]
    lot = dict(lot)
    if not cost:
        for k in ("cost_fx", "rate", "cost_krw", "cost_est", "stock_amt"):
            lot.pop(k, None)
    return {"lot": lot, "ships": ships, "can_cost": cost}


def inv_customer_fn(c):
    """재고 엑셀의 거래처 이름 → 거래명세서에서 쓰는 이름으로 맞춤.
    '한불화농 (반품)' → 한불화농, '서울향료1공장' / '서울향료㈜ 2공장' → 거래명세서의 같은 회사 이름."""
    canon = canon_fn(c)
    known = {}
    for n, cnt in c.execute("SELECT customer_name, COUNT(*) FROM quotes WHERE doc_type = 'statement'"
                            " GROUP BY 1 ORDER BY 2"):
        known[_company_key(n)] = n      # 가장 많이 쓴 이름이 마지막에 남음
    seen = {}

    def fn(name: str) -> str:
        n = re.sub(r"\(?\s*(반품|교환|반품교환)\s*\)?\s*$", "", str(name or "")).strip()
        key = _company_key(n)
        if not key:
            return n
        return canon(known.get(key) or seen.setdefault(key, n))
    return fn


# ---------------------------------------------------------------- 매입매출장 (일계장 엑셀) → 이익
LEDGER_COLS = {"month": ("월",), "day": ("일",), "item": ("품목",), "qty": ("수량",), "customer": ("매출처",),
               "supplier": ("매입처",), "sale_price": ("매출단가",), "buy_price": ("매입단가",), "origin": ("origin",),
               "sales": ("총매출액", "매출액"), "purchase": ("총매입액", "매입액"), "profit": ("총손익", "손익"),
               "note": ("비고",)}


def read_xls_sheets(data: bytes) -> dict:
    """.xls(옛 엑셀) → {시트이름: 행 목록}. 숫자는 문자열로 (read_xlsx_values 와 같은 모양)."""
    import xlrd
    book = xlrd.open_workbook(file_contents=data)
    out = {}
    for sh in book.sheets():
        rows = []
        for r in range(sh.nrows):
            vals = []
            for v in sh.row_values(r):
                if isinstance(v, float):
                    v = str(int(v)) if v == int(v) else repr(v)
                vals.append(str(v).strip())
            rows.append(vals)
        out[sh.name] = rows
    return out


def item_base(name: str) -> str:
    """'Heliotropine(Piperonal) (25kg *4)_향' → 'Heliotropine(Piperonal)' (포장·용도 표시 제거)."""
    n = re.sub(r"\s*\(\s*[\d.]+\s*(kg|g|l|ml|ea)\b[^)]*\).*$", "", str(name or ""), flags=re.I)
    n = re.sub(r"_.*$", "", n)
    return n.strip() or str(name or "").strip()


def parse_ledger_rows(rows: list):
    """머리글(월·일·품목·매출처·매입처·총매출액·총매입액…) 아래 거래 줄만 읽는다. 월 합계 줄은 건너뜀."""
    hi = next((i for i, r in enumerate(rows[:15]) if {"매출처", "매입처"} <= {_inv_h(x) for x in r}), None)
    if hi is None:
        return None, []
    hdr = [_inv_h(x) for x in rows[hi]]
    col = {}
    for f, keys in LEDGER_COLS.items():
        col[f] = next((i for k in keys for i, h in enumerate(hdr) if h == k or (len(k) > 1 and h.startswith(k))), None)
    if col["sales"] is None or col["purchase"] is None:
        return None, []
    title = " ".join(" ".join(r) for r in rows[:hi])
    m = re.search(r"(20\d{2})\s*년", title)
    year = int(m[1]) if m else None
    g = lambda r, f: (r[col[f]] if col[f] is not None and col[f] < len(r) else "")
    out = []
    for i, r in enumerate(rows[hi + 1:], hi + 2):
        mo, dd = _numn(g(r, "month")), _numn(g(r, "day"))
        item = str(g(r, "item")).strip()
        if not mo or not dd or not item or not (1 <= mo <= 12 and 1 <= dd <= 31):
            continue
        sales, purchase = _numn(g(r, "sales")) or 0, _numn(g(r, "purchase")) or 0
        profit = _numn(g(r, "profit"))
        out.append({"month": int(mo), "day": int(dd), "item": item, "item_base": item_base(item),
                    "qty": _numn(g(r, "qty")) or 0, "customer": str(g(r, "customer")).strip(),
                    "supplier": str(g(r, "supplier")).strip(), "sale_price": _numn(g(r, "sale_price")),
                    "buy_price": _numn(g(r, "buy_price")), "origin": str(g(r, "origin")).strip(),
                    "sales": sales, "purchase": purchase,
                    "profit": profit if profit is not None else sales - purchase,
                    "note": str(g(r, "note")).strip(), "src_row": i})
    return year, out


def parse_ledger(filename: str, data: bytes):
    """일계장 파일에서 '매입매출장' 시트(없으면 머리글이 맞는 시트)를 찾는다."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        sheets = read_xls_sheets(data)
    else:
        sheets = {n: read_xlsx_values(data, n) for n in xlsx_sheet_names(data)}
    names = sorted(sheets, key=lambda n: (_inv_h(n) != "매입매출장",))
    for n in names:
        year, rows = parse_ledger_rows(sheets[n])
        if rows:
            return n, year, rows
    return None, None, []


LEDGER_FIELDS = ("year", "month", "day", "date", "item", "item_base", "qty", "customer", "supplier", "sale_price",
                 "buy_price", "origin", "sales", "purchase", "profit", "note", "src_row")


def ledger_meta(c) -> dict:
    try:
        return json.loads(get_setting(c, "ledger_meta", "{}")) or {}
    except ValueError:
        return {}


@app.post("/api/ledger/upload")
async def ledger_upload(file: UploadFile = File(...), dry_run: bool = False, year: int = 0,
                        user: dict = Depends(upload_user), x_file_name: str = Header(default="")):
    data = await file.read()
    filename = urllib.parse.unquote(x_file_name) if x_file_name else (file.filename or "")
    if not filename.lower().endswith((".xls", ".xlsx", ".xlsm")):
        raise HTTPException(400, "엑셀 파일(xls, xlsx)만 올릴 수 있습니다.")
    try:
        sheet, y, rows = parse_ledger(filename, data)
    except Exception as e:
        raise HTTPException(400, f"엑셀을 읽지 못했습니다: {e}")
    if not rows:
        raise HTTPException(400, "매입매출장 시트(월·일·품목·매출처·매입처·총매출액·총매입액 머리글)를 찾지 못했습니다.")
    y = year or y or int((file_date(filename) or "0")[:4]) or None
    if not y:
        raise HTTPException(400, "연도를 알 수 없습니다. 연도를 골라 다시 올려 주세요.")
    summary = {"sheet": sheet, "year": y, "rows": len(rows), "sales": sum(r["sales"] for r in rows),
               "profit": sum(r["profit"] for r in rows), "last": max(f"{r['month']:02d}-{r['day']:02d}" for r in rows)}
    if dry_run:
        return summary
    with db() as c:
        meta = ledger_meta(c)
        cur = meta.get(str(y), {})
        fd = file_date(filename)
        if user["role"] == "auto" and fd and cur.get("file_date") and fd < cur["file_date"]:
            return {**summary, "skipped": f"이미 {cur['file_date']} 파일이 올라가 있어 건너뜀"}
        c.execute("DELETE FROM ledger WHERE year = ?", (y,))
        c.executemany(f"INSERT INTO ledger ({', '.join(LEDGER_FIELDS)}) VALUES ({', '.join('?' * len(LEDGER_FIELDS))})",
                      [[{**r, "year": y, "date": f"{y}-{r['month']:02d}-{r['day']:02d}"}.get(f) for f in LEDGER_FIELDS]
                       for r in rows])
        meta[str(y)] = {"filename": filename, "file_date": fd, "uploaded_at": now(), "by": user["name"],
                        "rows": len(rows), "last": summary["last"]}
        set_setting(c, "ledger_meta", json.dumps(meta, ensure_ascii=False))
    return summary


@app.get("/api/inventory/profit")
def inventory_profit(year: int = 0, low: float = 10, include_ex: bool = False, user: dict = Depends(current_user)):
    """매입매출장 기준 이익: 줄마다 총매출액 − 총매입액(= 총손익)."""
    with db() as c:
        if not can_see_cost(c, user):
            raise HTTPException(403, "이익 분석은 관리자만 볼 수 있습니다.")
        is_ex, canon = exclude_matcher(c), inv_customer_fn(c)
        rows = c.execute("SELECT * FROM ledger ORDER BY date, src_row").fetchall()
        meta = ledger_meta(c)
    years = sorted({r["year"] for r in rows})
    if not years:
        return {"years": [], "year": None, "meta": meta}
    year = year if year in years else years[-1]
    monthly = {y: {"rev": [0] * 12, "cost": [0] * 12, "profit": [0] * 12} for y in (year, year - 1)}
    yearly, cust, items, sups, low_rows = {}, {}, {}, {}, []
    stats = {"lines": 0, "excluded": 0, "excluded_sales": 0, "excluded_profit": 0}
    for r in rows:
        y, m = r["year"], r["month"]
        name = canon(r["customer"]) or "(매출처 없음)"
        if is_ex(name) and not include_ex:
            if y == year:
                stats["excluded"] += 1
                stats["excluded_sales"] += r["sales"]
                stats["excluded_profit"] += r["profit"]
            continue
        rev, cost, profit = r["sales"], r["purchase"], r["profit"]
        yy = yearly.setdefault(y, {"rev": 0, "cost": 0, "profit": 0})
        yy["rev"] += rev
        yy["cost"] += cost
        yy["profit"] += profit
        if y in monthly:
            mm = monthly[y]
            mm["rev"][m - 1] += rev
            mm["cost"][m - 1] += cost
            mm["profit"][m - 1] += profit
        if y != year:
            continue
        stats["lines"] += 1
        sup = re.sub(r"\(재고\)$", "", r["supplier"]).strip() or "(매입처 없음)"
        for key, bucket in ((name, cust), (r["item_base"], items), (sup, sups)):
            e = bucket.setdefault(key, {"name": key, "rev": 0, "cost": 0, "profit": 0, "qty": 0, "count": 0})
            e["rev"] += rev
            e["cost"] += cost
            e["profit"] += profit
            e["qty"] += r["qty"]
            e["count"] += 1
        margin = profit / rev * 100 if rev else 0
        if rev > 0 and margin < low:
            low_rows.append({"date": r["date"], "customer": name, "item": r["item"], "supplier": r["supplier"],
                             "qty": r["qty"], "price": r["sale_price"], "unit_cost": r["buy_price"],
                             "profit": profit, "margin": margin, "note": r["note"]})
    low_rows.sort(key=lambda x: (x["margin"], x["profit"]))
    by_profit = lambda dct: sorted(dct.values(), key=lambda e: -e["profit"])
    return {"years": years, "year": year, "monthly": monthly, "yearly": yearly, "customers": by_profit(cust),
            "items": by_profit(items), "suppliers": by_profit(sups), "low": low_rows[:300], "low_threshold": low,
            "stats": stats, "meta": meta, "include_ex": include_ex}


# ---------------------------------------------------------------- 입고 예정
SHIP_STATUSES = ("ordered", "shipped", "customs", "arrived")


class ShipmentIn(BaseModel):
    item: str
    spec: str = ""
    supplier: str = ""
    customer: str = ""
    qty: float = 0
    unit: str = ""
    eta: str
    status: str = "ordered"
    bl_no: str = ""
    hbl_no: str = ""
    warehouse: str = ""
    note: str = ""


SHIP_FIELDS = ("item", "spec", "supplier", "customer", "qty", "unit", "eta", "status", "bl_no", "hbl_no", "warehouse",
               "note")


def check_shipment(b: ShipmentIn):
    if not b.item.strip():
        raise HTTPException(400, "품목을 입력하세요.")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", b.eta or ""):
        raise HTTPException(400, "예상 입고일을 입력하세요.")
    if b.status not in SHIP_STATUSES:
        raise HTTPException(400, "잘못된 상태입니다.")


def ship_values(b: ShipmentIn):
    return [getattr(b, f).strip() if isinstance(getattr(b, f), str) else getattr(b, f) for f in SHIP_FIELDS]


@app.get("/api/shipments")
def list_shipments(view: str = "open", q: str = "", user: dict = Depends(current_user)):
    where, params = [], []
    if view == "open":
        where.append("s.status != 'arrived' AND s.cs_cleared_at = ''")     # 통관(수입신고 수리)이 끝난 건은 따로
    elif view == "cleared":
        where.append("s.status != 'arrived' AND s.cs_cleared_at != ''")
    elif view == "arrived":
        where.append("s.status = 'arrived'")
    if q:
        where.append("(s.item LIKE ? OR s.supplier LIKE ? OR s.customer LIKE ? OR s.bl_no LIKE ? OR s.hbl_no LIKE ?)")
        params += [f"%{q}%"] * 5
    order = "s.eta DESC, s.id DESC" if view in ("arrived", "cleared") else "s.eta, s.id"
    if view == "open":      # 진행중: 반입 → 입항·통관 중 → 선적 → 발주, 같은 단계 안에서는 예정일 순
        order = ("CASE WHEN s.cs_in_at != '' THEN 0 WHEN s.cs_arrived != '' OR s.status = 'customs' THEN 1"
                 " WHEN s.status = 'shipped' THEN 2 ELSE 3 END,"
                 " CASE WHEN s.cs_in_at != '' THEN s.cs_in_at ELSE s.eta END, s.id")
    with db() as c:
        rows = c.execute(
            "SELECT s.*, u.name AS creator_name FROM shipments s JOIN users u ON u.id = s.created_by"
            + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT 300", params).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/shipments")
def create_shipment(body: ShipmentIn, user: dict = Depends(current_user)):
    check_shipment(body)
    ts = now()
    with db() as c:
        cur = c.execute(
            f"INSERT INTO shipments ({', '.join(SHIP_FIELDS)}, arrived_at, created_by, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(SHIP_FIELDS))}, ?, ?, ?, ?)",
            (*ship_values(body), ts if body.status == "arrived" else None, user["id"], ts, ts))
    if body.bl_no.strip() or body.hbl_no.strip():
        unipass_soon(cur.lastrowid)
    return {"id": cur.lastrowid}


def unipass_soon(sid: int):
    """B/L 이 새로 들어오거나 바뀌면 바로 한 번 조회 (인증키가 있을 때만, 백그라운드)."""
    def run():
        time.sleep(1)
        try:
            with db() as c:
                if not get_setting(c, "unipass_key", ""):
                    return
            refresh_unipass(sid, quiet=True)
        except Exception as e:  # noqa: BLE001
            print("[TeamHub] unipass error:", e)
    threading.Thread(target=run, daemon=True).start()


@app.put("/api/shipments/{sid}")
def update_shipment(sid: int, body: ShipmentIn, user: dict = Depends(current_user)):
    check_shipment(body)
    with db() as c:
        old = c.execute("SELECT * FROM shipments WHERE id = ?", (sid,)).fetchone()
        if not old:
            raise HTTPException(404, "입고 예정을 찾을 수 없습니다.")
        arrived_at = old["arrived_at"] if body.status == "arrived" else None
        if body.status == "arrived" and not arrived_at:
            arrived_at = now()
        c.execute(f"UPDATE shipments SET {', '.join(f + ' = ?' for f in SHIP_FIELDS)}, arrived_at = ?, updated_at = ?"
                  " WHERE id = ?", (*ship_values(body), arrived_at, now(), sid))
        bl_changed = (old["bl_no"], old["hbl_no"]) != (body.bl_no.strip(), body.hbl_no.strip())
        if bl_changed:           # B/L 이 바뀌면 예전 조회 결과는 지운다
            c.execute("UPDATE shipments SET cs_cargo_no = '', cs_status = '', cs_arrived = '', cs_in_at = '', cs_shed = '',"
                      " cs_cleared_at = '', cs_out_at = '', cs_events = '[]', cs_checked_at = '', cs_error = '' WHERE id = ?",
                      (sid,))
    if bl_changed and (body.bl_no.strip() or body.hbl_no.strip()):
        unipass_soon(sid)
    return {"ok": True}


@app.patch("/api/shipments/{sid}/status")
def shipment_status(sid: int, body: dict, user: dict = Depends(current_user)):
    st = body.get("status")
    if st not in SHIP_STATUSES:
        raise HTTPException(400, "잘못된 상태입니다.")
    with db() as c:
        c.execute("UPDATE shipments SET status = ?, arrived_at = ?, updated_at = ? WHERE id = ?",
                  (st, now() if st == "arrived" else None, now(), sid))
    return {"ok": True}


# ---- 📋 인바운딩(수입 예정) 엑셀 → 입고예정 자동 등록·갱신
INB_COLS = {"done": ("",), "order": ("order", "soorpo"), "client": ("client",), "ref": ("ref",), "supplier": ("supplier",),
            "product": ("product", "goods"), "cas": ("cas",), "qty": ("qty",), "desp": ("desp", "despinv", "despetd"),
            "etd": ("etd",), "eta": ("eta",), "clear": ("clear",), "recd": ("recd",), "pymt": ("pymt",),
            "danger": ("danger",)}
MONTHS_EN = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                              "dec"))}


def inb_date(v) -> str:
    """엑셀 날짜숫자(46297) / 2026-07-08 / '→ 2026-07-08' / 250312 → 'YYYY-MM-DD'. 날짜가 아니면 ''."""
    s = str(v or "").strip().lstrip("→").strip()
    if re.match(r"^\d{5}(\.\d+)?$", s):
        n = float(s)
        if 30000 < n < 60000:
            return (datetime(1899, 12, 30) + timedelta(days=int(n))).strftime("%Y-%m-%d")
        return ""
    return inv_date(s)


def parse_inbound(data: bytes) -> list:
    """'Inbounding' 시트(없으면 머리글이 맞는 첫 시트): 머리글 Order·Client·Supplier·Product·Qty·ETD·ETA·Recd…"""
    names = xlsx_sheet_names(data)
    names = sorted(names, key=lambda n: ("inbound" not in n.lower(),))
    for name in names:
        rows = read_xlsx_values(data, name)
        hi = next((i for i, r in enumerate(rows[:10]) if {"supplier", "product", "eta"} <= {_inv_h(x) for x in r}), None)
        if hi is None:
            continue
        hdr = [_inv_h(x) for x in rows[hi]]
        col = {}
        for f, keys in INB_COLS.items():
            if f == "done":
                col[f] = 0
                continue
            col[f] = next((i for i, h in enumerate(hdr) if h in keys), None)
        if col["ref"] is not None:          # 'Ref.' 이 두 번 있음: 앞은 구분(Offer/월), 뒤는 비고
            refs = [i for i, h in enumerate(hdr) if h == "ref"]
            col["ref"], col["memo"] = refs[0], (refs[1] if len(refs) > 1 else None)
        g = lambda r, f: (str(r[col[f]]).strip() if col.get(f) is not None and col[f] < len(r) else "")
        out = []
        for i, r in enumerate(rows[hi + 1:], hi + 2):
            product, supplier = g(r, "product"), g(r, "supplier")
            if not product:
                continue
            order = g(r, "order")
            order_d = inb_date(order) or (order[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", order) else "")
            out.append({"row": i, "done": g(r, "done").upper() == "O", "order": order, "order_date": order_d,
                        "client": g(r, "client"), "ref": g(r, "ref"), "supplier": supplier, "product": product,
                        "cas": g(r, "cas"), "qty": _numn(g(r, "qty")) or 0, "desp_raw": g(r, "desp"),
                        "desp": inb_date(g(r, "desp")), "etd": inb_date(g(r, "etd")), "eta": inb_date(g(r, "eta")),
                        "eta_raw": g(r, "eta"), "clear": inb_date(g(r, "clear")), "clear_raw": g(r, "clear"),
                        "recd": inb_date(g(r, "recd")), "pymt": g(r, "pymt"), "memo": g(r, "memo"),
                        "danger": g(r, "danger")})
        return out
    return []


def _inb_key(r: dict) -> str:
    return f"{r['order']}|{_item_norm(r['supplier'])}|{_item_norm(r['product'])}"


def _inb_eta(r: dict):
    """예상 입고일: ETA → ETD+? (ETD 그대로) → 출고일 → 'Oct' 같은 월 표시(그 달 15일) → 주문일+30일. (날짜, 추정 여부)"""
    for k in ("eta", "etd", "desp"):
        if r[k]:
            return r[k], k != "eta"
    m = MONTHS_EN.get((r["ref"] or "")[:3].lower())
    base = date.fromisoformat(r["order_date"]) if r["order_date"] else date.today()
    if m:
        y = base.year + (1 if m < base.month - 1 else 0)
        return date(y, m, 15).isoformat(), True
    return (base + timedelta(days=30)).isoformat(), True


def sync_inbound(c, rows: list, by_id: int) -> dict:
    """진행 중인 줄(맨 앞 'O' 없음, 입고일 없음)은 입고예정으로 만들거나 갱신, 끝난 줄은 연결된 입고예정을 입고완료로."""
    existing = {r["src_key"]: dict(r) for r in c.execute("SELECT * FROM shipments WHERE src_key != ''")}
    manual = [dict(r) for r in c.execute("SELECT * FROM shipments WHERE src_key = '' AND status != 'arrived'")]
    stat = {"rows": len(rows), "open": 0, "created": 0, "updated": 0, "linked": 0, "arrived": 0}
    ts = now()
    seen = set()
    for r in rows:
        key = _inb_key(r)
        if key in seen:
            continue
        seen.add(key)
        sh = existing.get(key)
        finished = r["done"] or bool(r["recd"])
        if finished:
            if sh and sh["status"] != "arrived":
                c.execute("UPDATE shipments SET status = 'arrived', arrived_at = ?, updated_at = ? WHERE id = ?",
                          ((r["recd"] or ts[:10]) + " 00:00:00" if r["recd"] else ts, ts, sh["id"]))
                stat["arrived"] += 1
            continue
        stat["open"] += 1
        eta, guessed = _inb_eta(r)
        status = "customs" if r["clear"] else "shipped" if (r["etd"] or r["desp"]) and (r["etd"] or r["desp"]) <= ts[:10] \
            else "ordered"
        note = " · ".join(x for x in (
            f"[인바운딩] {r['order']}" + (f" / {r['ref']}" if r["ref"] else ""),
            "ETA 미정 — 추정일" if guessed else "", f"출고 {r['desp_raw']}" if r["desp_raw"] and not r["desp"] else "",
            f"통관 {r['clear_raw']}" if r["clear_raw"] else "", f"결제 {r['pymt']}" if r["pymt"] else "",
            r["memo"], f"위험물 {r['danger']}" if r["danger"] else "", f"CAS {r['cas']}" if r["cas"] else "") if x)
        vals = {"item": r["product"][:150], "supplier": r["supplier"][:100], "customer": r["client"][:60],
                "qty": r["qty"], "eta": eta, "note": note[:1000]}
        if not sh:
            # 직접 만들어 둔 입고예정과 같은 건이면(품목·공급사 같고 예정일 ±45일) 새로 만들지 않고 연결
            ni, ns = _item_norm(r["product"]), _item_norm(r["supplier"])
            for m in manual:
                mi = _item_norm(m["item"])
                if mi and (mi in ni or ni in mi) and (not m["supplier"] or _item_norm(m["supplier"])[:5] == ns[:5]) \
                        and abs((date.fromisoformat(m["eta"]) - date.fromisoformat(eta)).days) <= 45:
                    sh = m
                    manual.remove(m)
                    c.execute("UPDATE shipments SET src_key = ? WHERE id = ?", (key, m["id"]))
                    stat["linked"] += 1
                    break
        if sh:
            if sh["status"] == "arrived":
                continue                         # TeamHub 에서 입고완료로 바꾼 건은 그대로 둔다
            # 통관 단계는 UNI-PASS 가 더 정확하면 그쪽을 따름 (이미 customs 면 내리지 않음)
            st = sh["status"] if sh["status"] == "customs" else status
            c.execute("UPDATE shipments SET item = ?, supplier = ?, customer = ?, qty = ?, eta = ?, note = ?, status = ?,"
                      " updated_at = ? WHERE id = ?", (vals["item"], vals["supplier"], vals["customer"], vals["qty"],
                                                       vals["eta"], vals["note"], st, ts, sh["id"]))
            stat["updated"] += 1
        else:
            c.execute("INSERT INTO shipments (item, spec, supplier, customer, qty, unit, eta, status, bl_no, hbl_no, warehouse,"
                      " note, arrived_at, created_by, created_at, updated_at, src_key) VALUES"
                      " (?, '', ?, ?, ?, 'kg', ?, ?, '', '', '', ?, NULL, ?, ?, ?, ?)",
                      (vals["item"], vals["supplier"], vals["customer"], vals["qty"], vals["eta"], status, vals["note"],
                       by_id, ts, ts, key))
            stat["created"] += 1
    return stat


@app.post("/api/inbound/upload")
async def inbound_upload(file: UploadFile = File(...), dry_run: bool = False, user: dict = Depends(upload_user),
                         x_file_name: str = Header(default="")):
    data = await file.read()
    filename = urllib.parse.unquote(x_file_name) if x_file_name else (file.filename or "")
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "xlsx 엑셀 파일만 올릴 수 있습니다.")
    try:
        rows = parse_inbound(data)
    except Exception as e:
        raise HTTPException(400, f"엑셀을 읽지 못했습니다: {e}")
    if not rows:
        raise HTTPException(400, "인바운딩 시트(Supplier·Product·ETA 머리글)를 찾지 못했습니다.")
    open_rows = [r for r in rows if not (r["done"] or r["recd"])]
    if dry_run:
        return {"rows": len(rows), "open": len(open_rows)}
    with db() as c:
        by = user.get("id") or _first_admin(c)
        stat = sync_inbound(c, rows, by)
        stat["merged"] = merge_auto_bl_shipments(c)
        meta = {"filename": filename, "uploaded_at": now(), "by": user["name"], **stat}
        set_setting(c, "inbound_meta", json.dumps(meta, ensure_ascii=False))
        if stat["created"]:
            for a in c.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1"):
                notify(c, a[0], f"📋 인바운딩: 새 수입 예정 {stat['created']}건을 입고예정에 등록했습니다.")
    return meta


@app.get("/api/inbound/meta")
def inbound_meta(_: dict = Depends(current_user)):
    with db() as c:
        return json.loads(get_setting(c, "inbound_meta", "{}") or "{}")


# ---- UNI-PASS 로 B/L 진행 확인 (입항·반입·통관 수리·반출)
CS_MILESTONES = (("cs_arrived", "🛳 입항"), ("cs_in_at", "📦 반입"), ("cs_cleared_at", "✅ 통관 수리"), ("cs_out_at", "🚚 반출"))


def refresh_unipass(sid: int, quiet: bool = False) -> dict:
    """입고예정 하나를 UNI-PASS 로 조회해 저장. 새로 생긴 단계(입항·반입·수리·반출)는 등록한 사람에게 알림."""
    with db() as c:
        sh = c.execute("SELECT * FROM shipments WHERE id = ?", (sid,)).fetchone()
        key = get_setting(c, "unipass_key", "")
    if not sh:
        raise HTTPException(404, "입고 예정을 찾을 수 없습니다.")
    try:
        year = int((sh["eta"] or "")[:4] or 0)
        r = unipass.lookup(key, sh["bl_no"], sh["hbl_no"], 0)
        if not r.get("found") and year and year not in (datetime.now().year, datetime.now().year - 1):
            r = unipass.lookup(key, sh["bl_no"], sh["hbl_no"], year)
        err = "" if r.get("found") else "UNI-PASS 에서 이 B/L 을 찾지 못했습니다 (아직 적하목록이 안 올라왔거나 번호·연도가 다를 수 있음)."
    except unipass.UnipassError as e:
        r, err = {"found": False}, str(e)
    with db() as c:
        if not r.get("found"):
            c.execute("UPDATE shipments SET cs_checked_at = ?, cs_error = ? WHERE id = ?", (now(), err, sid))
            return {"found": False, "error": err}
        new = {"cs_arrived": r["arrived"], "cs_in_at": r["in_at"], "cs_cleared_at": r["cleared_at"], "cs_out_at": r["out_at"]}
        status = sh["status"]
        if status in ("ordered", "shipped") and (r["in_at"] or r["arrived"]):
            status = "customs"
        c.execute("UPDATE shipments SET cs_cargo_no = ?, cs_status = ?, cs_arrived = ?, cs_in_at = ?, cs_shed = ?,"
                  " cs_cleared_at = ?, cs_out_at = ?, cs_events = ?, cs_checked_at = ?, cs_error = '', status = ?,"
                  " updated_at = ? WHERE id = ?",
                  (r["cargo_no"], " · ".join(x for x in (r["status"], r["clearance"]) if x), r["arrived"], r["in_at"],
                   r["shed"], r["cleared_at"], r["out_at"], json.dumps(r["events"], ensure_ascii=False), now(), status,
                   now(), sid))
        if not quiet:
            for col, label in CS_MILESTONES:
                if new[col] and not sh[col]:
                    where = f" · {r['shed']}" if col == "cs_in_at" and r["shed"] else ""
                    notify(c, sh["created_by"], f"{label}: {sh['item']} ({new[col]}{where}) — UNI-PASS")
    return {"found": True, **r}


def unipass_loop():
    """진행 중인 입고예정(B/L 있음)을 낮 시간(7~21시)에 2시간마다 UNI-PASS 로 확인."""
    time.sleep(30)
    while True:
        try:
            if 7 <= datetime.now().hour < 21:
                with db() as c:
                    key = get_setting(c, "unipass_key", "")
                    cutoff = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                    ids = [r[0] for r in c.execute(
                        "SELECT id FROM shipments WHERE status != 'arrived' AND (bl_no != '' OR hbl_no != '')"
                        " AND cs_out_at = '' AND cs_checked_at < ? ORDER BY eta LIMIT 100", (cutoff,))] if key else []
                process_bl_watch()
                for sid in ids:
                    try:
                        refresh_unipass(sid)
                    except Exception as e:  # noqa: BLE001
                        print("[TeamHub] unipass error:", e)
                    time.sleep(2)
        except Exception as e:  # noqa: BLE001
            print("[TeamHub] unipass loop error:", e)
        time.sleep(600)


# ---- 메일에 나온 B/L·운송장 번호 → UNI-PASS 조회 → 입고예정 자동 등록
def shipment_by_number(c, num: str):
    n = mailin.norm_bl(num)
    for r in c.execute("SELECT id, bl_no, hbl_no FROM shipments WHERE bl_no != '' OR hbl_no != ''"):
        if n and n in (mailin.norm_bl(r["bl_no"]), mailin.norm_bl(r["hbl_no"])):
            return r["id"]
    return None


def watch_bl_numbers(c, mail_id: int, owner_id, it: dict, bulk: bool):
    """메일 본문에서 B/L·운송장 번호를 찾아 대기 목록에 넣는다 (이미 입고예정에 있는 번호는 제외)."""
    text = f"{it['subject']}\n{mailin.own_text(it['body']) or it['body']}"
    nums = mailin.find_bl_numbers(text)
    if not nums:
        return
    # 예전 메일을 한꺼번에 가져올 때는 최근 두 달 메일의 번호만 UNI-PASS 로 찾는다 (오래된 화물은 이미 끝났으므로)
    if bulk and it["sent_at"][:10] < (date.today() - timedelta(days=60)).isoformat():
        return
    eta = next((cd["date"] for cd in it["candidates"] if cd["kind"] == "ship" and not
                re.match(r"(etd|선적|출항)", (cd.get("label") or "").lower())), "")
    for num, kind in nums:
        if shipment_by_number(c, num):
            continue
        c.execute("INSERT OR IGNORE INTO bl_watch (number, kind, owner_id, mail_id, subject, sender, eta_hint, created_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (num, kind, owner_id, mail_id, it["subject"][:200],
                                                       it["from_name"][:100], eta, now()))


def _first_admin(c):
    r = c.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1 ORDER BY id LIMIT 1").fetchone()
    return r[0] if r else 1


def clean_prnm(name: str) -> str:
    """UNI-PASS 품명에서 품목분류(HS) 설명 빼기: 'CIS 6 NONENAL HS ALDEHYDES WHETHER OR NOT…' → 'CIS 6 NONENAL'"""
    n = str(name or "").strip()
    m = re.search(r"\b(H\.?S\.?(\s*CODE)?|WHETHER\s+OR\s+NOT|OF\s+HEADING|N\.?E\.?S\.?|OTHER\s+THAN)\b", n, re.I)
    if m and m.start() > 2:
        n = n[:m.start()]
    return re.sub(r"[\s,;:\-]+$", "", n).strip()


def find_open_shipment(c, item: str, eta: str):
    """B/L 이 아직 없는 진행 중 입고예정 중 같은 제품(이름이 같거나 한쪽이 다른 쪽을 포함)이고 예정일이 가까운 것."""
    rows = [dict(r) for r in c.execute("SELECT * FROM shipments WHERE status != 'arrived' AND bl_no = '' AND hbl_no = ''")]
    if not rows or not item:
        return None
    match = ItemMatcher([r["item"] for r in rows], {})
    hit = match(clean_prnm(item))
    if not hit:
        return None
    try:
        d0 = date.fromisoformat(eta[:10])
    except ValueError:
        d0 = date.today()
    cands = [r for r in rows if r["item"] == hit and -60 <= (date.fromisoformat(r["eta"]) - d0).days <= 90]
    return min(cands, key=lambda r: abs((date.fromisoformat(r["eta"]) - d0).days)) if cands else None


def merge_auto_bl_shipments(c) -> int:
    """예전에 B/L 로 따로 만들어진 입고예정이 인바운딩 등 기존 건과 같은 제품이면 합친다 (B/L·통관 정보를 옮기고 지움)."""
    merged = 0
    autos = [dict(r) for r in c.execute("SELECT * FROM shipments WHERE note LIKE '[자동 등록] UNI-PASS%' AND src_key = ''"
                                        " AND status != 'arrived'")]
    for a in autos:
        same = find_open_shipment(c, a["item"], a["eta"])
        if not same or same["id"] == a["id"]:
            continue
        cols = ("bl_no", "hbl_no", "cs_cargo_no", "cs_status", "cs_arrived", "cs_in_at", "cs_shed", "cs_cleared_at",
                "cs_out_at", "cs_events", "cs_checked_at", "cs_error")
        c.execute(f"UPDATE shipments SET {', '.join(k + ' = ?' for k in cols)}, status = ?,"
                  " eta = CASE WHEN ? != '' THEN ? ELSE eta END, updated_at = ? WHERE id = ?",
                  (*[a[k] for k in cols], a["status"] if a["status"] == "customs" else same["status"],
                   a["cs_arrived"], a["cs_arrived"], now(), same["id"]))
        c.execute("UPDATE bl_watch SET shipment_id = ? WHERE shipment_id = ?", (same["id"], a["id"]))
        c.execute("DELETE FROM shipments WHERE id = ?", (a["id"],))
        merged += 1
    return merged


def register_from_bl(num: str, kind: str = "", owner_id=None, subject: str = "", sender: str = "",
                     eta_hint: str = "", quiet: bool = False) -> dict:
    """번호 하나를 UNI-PASS 로 조회해서 찾으면 입고예정을 만든다. → {state, shipment_id, message}"""
    with db() as c:
        key = get_setting(c, "unipass_key", "")
        existing = shipment_by_number(c, num)
    if existing:
        return {"state": "exists", "shipment_id": existing, "message": "이미 입고예정에 있는 번호입니다."}
    if not key:
        return {"state": "wait", "message": "UNI-PASS 인증키가 없습니다."}
    r = {"found": False}
    order = [("hbl", "mbl")] if kind == "H" else [("mbl", "hbl")]
    for first, second in order:
        for as_ in (first, second):
            r = unipass.lookup(key, num if as_ == "mbl" else "", num if as_ == "hbl" else "")
            if r.get("found"):
                break
    if not r.get("found"):
        return {"state": "wait", "message": "UNI-PASS 에 아직 없습니다 (적하목록 제출 전일 수 있음). 2시간마다 다시 찾습니다."}
    # 이미 반출된 지 오래된 화물은 등록하지 않음 (예전 메일을 한꺼번에 가져온 경우)
    if r["out_at"] and r["out_at"][:10] < (date.today() - timedelta(days=3)).isoformat():
        return {"state": "old", "message": f"이미 {r['out_at'][:10]} 에 반출된 화물이라 등록하지 않았습니다."}
    w = re.match(r"([\d.]+)\s*(\w*)", r["weight"] or "")
    item = (clean_prnm(r["item"]) or mailin.clean_subject(subject) or num)[:120]
    eta = r["arrived"] or eta_hint or date.today().isoformat()
    status = "customs" if (r["arrived"] or r["in_at"]) else "shipped"
    mbl = r["mbl_no"] or (num if kind != "H" else "")
    hbl = r["hbl_no"] or (num if kind == "H" else "")
    with db() as c:
        if shipment_by_number(c, r["mbl_no"] or num) or shipment_by_number(c, r["hbl_no"] or num):
            return {"state": "exists", "message": "이미 입고예정에 있는 번호입니다."}
        oid = owner_id or _first_admin(c)
        # 인바운딩 등으로 이미 있는 입고예정(B/L 없음)과 같은 제품이면 새로 만들지 않고 B/L 만 붙인다
        same = find_open_shipment(c, item, eta)
        if same:
            c.execute("UPDATE shipments SET bl_no = ?, hbl_no = ?, eta = CASE WHEN ? != '' THEN ? ELSE eta END,"
                      " updated_at = ? WHERE id = ?", (mbl, hbl, r["arrived"], r["arrived"], now(), same["id"]))
            sid = same["id"]
    if same:
        refresh_unipass(sid, quiet=True)
        with db() as c:
            c.execute("UPDATE bl_watch SET state = 'done', shipment_id = ?, message = '기존 입고예정에 B/L 을 연결했습니다.'"
                      " WHERE number = ?", (sid, mailin.norm_bl(num)))
            if not quiet:
                notify(c, oid, f"🚢 B/L 연결: {same['item']} ← {num}" + (f" · 입항 {r['arrived']}" if r["arrived"] else ""))
        return {"state": "done", "shipment_id": sid, "message": f"기존 입고예정({same['item']})에 B/L 을 연결했습니다."}
    supplier = "" if "@" in (sender or "") else (sender or "")[:100]     # 메일 주소(택배·포워더 담당자)는 수출사가 아님
    with db() as c:
        ts = now()
        cur = c.execute(
            f"INSERT INTO shipments ({', '.join(SHIP_FIELDS)}, arrived_at, created_by, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(SHIP_FIELDS))}, NULL, ?, ?, ?)",
            (item, "", supplier, "", float(w[1]) if w else 0, (w[2] or "kg").lower() if w else "kg", eta, status,
             mbl, hbl, "",
             f"[자동 등록] UNI-PASS {num}" + (f" · 메일: {subject}" if subject else ""), oid, ts, ts))
        sid = cur.lastrowid
    refresh_unipass(sid, quiet=True)
    with db() as c:      # 같은 번호가 대기 목록에 있으면 끝난 것으로
        c.execute("UPDATE bl_watch SET state = 'done', shipment_id = ?, message = '입고예정에 등록했습니다.' WHERE number = ?",
                  (sid, mailin.norm_bl(num)))
    if not quiet:
        with db() as c:
            notify(c, oid, f"🚢 입고예정 자동 등록: {item} · {num}" + (f" · 입항 {r['arrived']}" if r["arrived"] else "")
                   + (f" · 반입 {r['in_at'][:10]}" if r["in_at"] else ""))
    return {"state": "done", "shipment_id": sid, "message": "입고예정에 등록했습니다."}


BL_LOCK = threading.Lock()


def process_bl_watch():
    """대기 목록의 번호를 UNI-PASS 로 다시 찾아본다. 3주 동안 못 찾으면 그만 찾는다."""
    if not BL_LOCK.acquire(blocking=False):
        return
    try:
        with db() as c:
            if not get_setting(c, "unipass_key", ""):
                return
            cutoff = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            stale = (datetime.now() - timedelta(days=21)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("UPDATE bl_watch SET state = 'gave_up' WHERE state = 'wait' AND created_at < ?", (stale,))
            rows = [dict(r) for r in c.execute("SELECT * FROM bl_watch WHERE state = 'wait' AND last_try < ?"
                                               " ORDER BY created_at DESC LIMIT 50", (cutoff,))]
        for w in rows:
            try:
                res = register_from_bl(w["number"], w["kind"], w["owner_id"], w["subject"], w["sender"], w["eta_hint"])
            except unipass.UnipassError as e:
                res = {"state": "wait", "message": str(e)}
            with db() as c:
                c.execute("UPDATE bl_watch SET state = ?, shipment_id = ?, tries = tries + 1, last_try = ?, message = ?"
                          " WHERE number = ?", (res["state"], res.get("shipment_id"), now(), res["message"], w["number"]))
            time.sleep(2)
    finally:
        BL_LOCK.release()


def bl_watch_soon():
    threading.Thread(target=lambda: (time.sleep(2), process_bl_watch()), daemon=True).start()


class BlIn(BaseModel):
    number: str
    kind: str = ""


@app.post("/api/shipments/from-bl")
def shipment_from_bl(body: BlIn, user: dict = Depends(current_user)):
    """B/L·운송장 번호를 넣으면 UNI-PASS 로 찾아 바로 입고예정 등록. 아직 없으면 대기 목록에 넣는다."""
    num = mailin.norm_bl(body.number)
    if len(num) < 6:
        raise HTTPException(400, "번호를 확인하세요.")
    try:
        res = register_from_bl(num, body.kind, user["id"], quiet=True)
    except unipass.UnipassError as e:
        raise HTTPException(400, str(e))
    if res["state"] == "wait":
        with db() as c:
            c.execute("INSERT OR REPLACE INTO bl_watch (number, kind, owner_id, state, last_try, tries, message, created_at)"
                      " VALUES (?, ?, ?, 'wait', ?, 1, ?, ?)", (num, body.kind, user["id"], now(), res["message"], now()))
    return res


@app.get("/api/bl-watch")
def bl_watch_list(user: dict = Depends(current_user)):
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM bl_watch WHERE state = 'wait' ORDER BY created_at DESC LIMIT 100")]
        return [r for r in rows if not shipment_by_number(c, r["number"])]


@app.delete("/api/bl-watch/{number}")
def bl_watch_delete(number: str, user: dict = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE bl_watch SET state = 'removed' WHERE number = ?", (number,))
    return {"ok": True}


@app.post("/api/shipments/{sid}/unipass")
def shipment_unipass(sid: int, _: dict = Depends(current_user)):
    return refresh_unipass(sid)


@app.get("/api/unipass/settings")
def unipass_settings(_: dict = Depends(admin_user)):
    with db() as c:
        key = get_setting(c, "unipass_key", "")
    return {"has_key": bool(key), "key_tail": key[-4:] if key else ""}


@app.put("/api/unipass/settings")
def unipass_put_settings(body: dict, _: dict = Depends(admin_user)):
    key = str(body.get("key", "")).strip()
    with db() as c:
        set_setting(c, "unipass_key", key)
    return {"ok": True, "has_key": bool(key)}


@app.post("/api/unipass/test")
def unipass_test(body: dict, _: dict = Depends(admin_user)):
    with db() as c:
        key = get_setting(c, "unipass_key", "")
    try:
        r = unipass.lookup(key, body.get("mbl", ""), body.get("hbl", ""), int(body.get("year") or 0))
    except unipass.UnipassError as e:
        raise HTTPException(400, str(e))
    return r


@app.delete("/api/shipments/{sid}")
def delete_shipment(sid: int, user: dict = Depends(current_user)):
    with db() as c:
        old = c.execute("SELECT created_by FROM shipments WHERE id = ?", (sid,)).fetchone()
        if not old:
            raise HTTPException(404, "입고 예정을 찾을 수 없습니다.")
        if old["created_by"] != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "등록한 사람 또는 관리자만 삭제할 수 있습니다.")
        c.execute("DELETE FROM shipments WHERE id = ?", (sid,))
    return {"ok": True}


# ---------------------------------------------------------------- Notifications / Dashboard
@app.get("/api/notifications")
def list_notifications(user: dict = Depends(current_user)):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user["id"],)
        ).fetchall()
        unread = c.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user["id"],)
        ).fetchone()[0]
    return {"unread": unread, "items": [dict(r) for r in rows]}


@app.post("/api/notifications/read-all")
def read_all(user: dict = Depends(current_user)):
    with db() as c:
        c.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user["id"],))
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(current_user)):
    today = datetime.now().strftime("%Y-%m-%d")
    clause, params = visible_event_clause(user)
    with db() as c:
        counts = {
            r["status"]: r["n"]
            for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE assignee_id = ? GROUP BY status",
                (user["id"],),
            )
        }
        overdue = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status != 'done' AND due_date < ?",
            (user["id"], today),
        ).fetchone()[0]
        sent_open = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE assigner_id = ? AND status != 'done'", (user["id"],)
        ).fetchone()[0]
        today_events = c.execute(
            "SELECT e.*, u.name AS creator_name FROM events e JOIN users u ON u.id = e.created_by"
            f" WHERE {clause} AND substr(e.start, 1, 10) <= ? AND substr(e.end, 1, 10) >= ?"
            " ORDER BY e.all_day DESC, e.start",
            (*params, today, today),
        ).fetchall()
        my_tasks = c.execute(
            TASK_SELECT + " WHERE t.assignee_id = ? AND t.status != 'done'"
            " ORDER BY COALESCE(t.due_date, '9999-12-31'),"
            " CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"
            " LIMIT 8",
            (user["id"],),
        ).fetchall()
    week_end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    with db() as c:
        # 진행중 화면과 같은 순서: 반입 → 입항·통관 → 선적 → 발주
        rank = ("CASE WHEN cs_in_at != '' THEN 0 WHEN cs_arrived != '' OR status = 'customs' THEN 1"
                " WHEN status = 'shipped' THEN 2 ELSE 3 END")
        open_ = "status != 'arrived' AND cs_cleared_at = ''"
        ships_port = c.execute(f"SELECT * FROM shipments WHERE {open_} AND (cs_in_at != '' OR cs_arrived != ''"
                               " OR status = 'customs') ORDER BY " + rank + ", CASE WHEN cs_in_at != '' THEN cs_in_at"
                               " ELSE eta END, id").fetchall()
        port_ids = {r["id"] for r in ships_port}
        ships_today = [r for r in c.execute(f"SELECT * FROM shipments WHERE eta = ? ORDER BY status = 'arrived', {rank}, id",
                                            (today,)).fetchall() if r["id"] not in port_ids]
        ships_week = [r for r in c.execute(f"SELECT * FROM shipments WHERE eta > ? AND eta <= ? AND {open_}"
                                           f" ORDER BY {rank}, eta", (today, week_end)).fetchall()
                      if r["id"] not in port_ids]
        ships_late = [r for r in c.execute(f"SELECT * FROM shipments WHERE eta < ? AND {open_} ORDER BY {rank}, eta",
                                           (today,)).fetchall() if r["id"] not in port_ids]
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    with db() as c:
        stmts = c.execute(
            "SELECT q.id, q.quote_date, q.customer_name, q.title, q.grand_total, q.status, u.name AS creator_name,"
            " (SELECT name FROM quote_items qi WHERE qi.quote_id = q.id ORDER BY seq LIMIT 1) AS first_item,"
            " (SELECT COUNT(*) FROM quote_items qi WHERE qi.quote_id = q.id) AS item_count"
            " FROM quotes q JOIN users u ON u.id = q.created_by"
            " WHERE q.doc_type = 'statement' AND q.note != ? AND ("
            "   (q.quote_date = ? AND q.status IN ('sent', 'won'))"
            "   OR (q.quote_date BETWEEN ? AND ? AND q.status = 'sent'))"
            " ORDER BY q.status = 'won', q.quote_date, q.id",
            (IMPORT_NOTE, today, week_ago, today),
        ).fetchall()
    return {
        "statements_todo": [dict(r) for r in stmts],
        "ships_today": [dict(r) for r in ships_today],
        "ships_week": [dict(r) for r in ships_week],
        "ships_late": [dict(r) for r in ships_late],
        "ships_port": [dict(r) for r in ships_port],
        "today": today,
        "counts": {s: counts.get(s, 0) for s in STATUSES},
        "overdue": overdue,
        "sent_open": sent_open,
        "today_events": [dict(r) for r in today_events],
        "my_tasks": [dict(r) for r in my_tasks],
    }


# ---------------------------------------------------------------- Outlook 메일 연동
def mail_page(title: str, body: str, ok: bool = True) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TeamHub</title></head>
<body style="font-family:'Malgun Gothic',sans-serif;background:#f4f6fb;margin:0;padding:40px 16px">
<div style="max-width:440px;margin:0 auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 4px 16px rgba(0,0,0,.06)">
<div style="color:#2563eb;font-weight:800;font-size:18px;margin-bottom:12px">TeamHub</div>
<h2 style="color:{color};margin:0 0 12px">{html.escape(title)}</h2>{body}
<p style="margin-top:20px"><a href="/">TeamHub 열기 →</a></p></div></body></html>""")


def mail_task_lookup(tid: int, u: int, s: str, sig: str):
    with db() as c:
        secret = get_setting(c, "secret")
        if s not in STATUSES or not mailer.verify(secret, tid, u, s, sig):
            return None, None
        t = c.execute(TASK_SELECT + " WHERE t.id = ?", (tid,)).fetchone()
        actor = c.execute("SELECT * FROM users WHERE id = ? AND active = 1", (u,)).fetchone()
    if not t or not actor or t["assignee_id"] != u:
        return None, None
    return t, dict(actor)


@app.get("/mail/task/{tid}", response_class=HTMLResponse)
def mail_task_confirm(tid: int, u: int, s: str, sig: str):
    # 메일 보안 스캐너(Safe Links)가 링크를 미리 열어도 상태가 바뀌지 않도록 GET 은 확인 화면만 보여준다.
    t, _actor = mail_task_lookup(tid, u, s, sig)
    if not t:
        return mail_page("유효하지 않은 링크입니다", "<p>업무가 삭제되었거나 링크가 올바르지 않습니다.</p>", ok=False)
    label = STATUS_LABEL[s]
    return mail_page(f"업무를 [{label}](으)로 변경할까요?", f"""
<p><b>{html.escape(t['title'])}</b><br><small>지시: {html.escape(t['assigner_name'])} · 현재 상태: {STATUS_LABEL[t['status']]}</small></p>
<form method="post"><input type="hidden" name="u" value="{u}"><input type="hidden" name="s" value="{s}">
<input type="hidden" name="sig" value="{html.escape(sig)}">
<button style="background:#2563eb;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:15px;font-weight:bold;cursor:pointer">
[{label}] 처리하기</button></form>""")


@app.post("/mail/task/{tid}", response_class=HTMLResponse)
def mail_task_apply(tid: int, u: int = Form(...), s: str = Form(...), sig: str = Form(...)):
    t, actor = mail_task_lookup(tid, u, s, sig)
    if not t:
        return mail_page("유효하지 않은 링크입니다", "<p>업무가 삭제되었거나 링크가 올바르지 않습니다.</p>", ok=False)
    with db() as c:
        mail = change_status(c, t, actor, s, via_mail=True)
    if mail:
        mailer.send_async(db, *mail)
    return mail_page(f"[{STATUS_LABEL[s]}] 처리되었습니다",
                     f"<p><b>{html.escape(t['title'])}</b></p><p>{html.escape(t['assigner_name'])}님에게 알림이 전송되었습니다.</p>")


class MailTestIn(BaseModel):
    to: str


@app.get("/api/mail/status")
def mail_status(_: dict = Depends(admin_user)):
    with db() as c:
        logs = c.execute("SELECT * FROM mail_log ORDER BY id DESC LIMIT 30").fetchall()
        last = get_setting(c, "last_reminder")
    return {**mailer.status_info(), "last_reminder": last, "logs": [dict(r) for r in logs]}


@app.post("/api/mail/test")
def mail_test(body: MailTestIn, _: dict = Depends(admin_user)):
    if not mailer.enabled():
        raise HTTPException(400, "메일 설정이 없습니다. README 의 Outlook 연동 설정을 확인하세요.")
    try:
        mailer._send(body.to.strip(), "[TeamHub] 테스트 메일",
                     mailer._layout("테스트 메일", "<p>Outlook 메일 연동이 정상 동작합니다.</p>"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"발송 실패: {e}")
    return {"ok": True}


def run_daily_reminders(today: str):
    """하루 한 번: 담당자에게 마감 임박/지연 업무, 지시자에게 지연 업무 요약 메일."""
    with db() as c:
        if get_setting(c, "last_reminder") == today:
            return
        set_setting(c, "last_reminder", today)
        secret = get_setting(c, "secret")
        open_rows = c.execute(
            TASK_SELECT + " WHERE t.status != 'done' AND t.due_date IS NOT NULL AND t.due_date <= ?"
            " ORDER BY t.due_date", (today,),
        ).fetchall()
    by_assignee, by_assigner = {}, {}
    for t in open_rows:
        by_assignee.setdefault(t["assignee_id"], []).append(t)
        if t["due_date"] < today and t["assigner_id"] != t["assignee_id"]:
            by_assigner.setdefault(t["assigner_id"], []).append(t)
    for tasks in by_assignee.values():
        subject, body_html = mailer.reminder_mail(secret, tasks[0]["assignee_name"], tasks, today)
        mailer.send_async(db, tasks[0]["assignee_email"], subject, body_html, None, "reminder")
    for tasks in by_assigner.values():
        subject, body_html = mailer.assigner_summary_mail(tasks[0]["assigner_name"], tasks, today)
        mailer.send_async(db, tasks[0]["assigner_email"], subject, body_html, None, "summary")


# ---------------------------------------------------------------- 📥 메일에서 일정 찾기 (쓰레드 단위)
# 같은 대화(회신·전달로 이어진 메일)는 thread_key 로 묶는다. 가장 최근 메일이 '대표'이고, 화면에는 대표만 보인다
# (나머지는 status='merged'). 일정 후보는 쓰레드 전체를 읽고 대표의 t_candidates 에 넣는다.
def _thread_key(c, oid, it) -> str:
    for ref in reversed(it["refs"]):
        row = c.execute("SELECT thread_key FROM mail_items WHERE owner_id IS ? AND msg_ref = ? AND thread_key != ''",
                        (oid, ref)).fetchone()
        if row:
            return row[0]
    return f"{oid or 0}:{mailin.thread_subject(it['subject'])}"


def save_mail_items(c, raw: bytes, owner_id: Optional[int] = None, bulk: bool = False) -> dict:
    """원본 메일을 읽어 저장. 전달한 사람(직원 메일 주소)으로 주인을 정한다. 같은 메일은 한 번만."""
    items = mailin.parse_raw(raw)
    emails = {r["email"].lower(): r["id"] for r in c.execute("SELECT id, email FROM users WHERE email != ''")}
    collect = (mailer.MAIL_FROM or "").lower()
    added, dup, skipped, touched = 0, 0, 0, {}
    for it in items:
        if it["bulk"]:
            skipped += 1
            continue                     # 광고·뉴스레터·자동 발송 메일은 저장하지 않음
        # 모으는 주소(info@ = 발송 메일함)는 주인 판단에서 뺀다 (관리자 메일로 등록돼 있어도 모든 메일이 관리자 것이 되지 않게)
        oid = owner_id or next((emails[a] for a in it["owners"] if a in emails and a != collect), None)
        uniq = it["msg_id"] or hashlib.sha1(f"{it['from_addr']}|{it['subject']}|{it['sent_at']}".encode()).hexdigest()
        if c.execute("SELECT 1 FROM mail_items WHERE owner_id IS ? AND (fp = ? OR (msg_ref != '' AND msg_ref = ?))",
                     (oid, it["fp"], it["msg_id"])).fetchone():
            dup += 1
            continue                     # 같은 메일이 이미 있음 (첨부 전달·자동 전달로 두 번 온 경우 등)
        key = _thread_key(c, oid, it)
        cur = c.execute("INSERT OR IGNORE INTO mail_items (owner_id, uniq, from_addr, from_name, subject, sent_at, body,"
                        " candidates, status, created_at, thread_key, msg_ref, fp)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'merged', ?, ?, ?, ?)",
                        (oid, f"{oid or 0}:{uniq}", it["from_addr"], it["from_name"], it["subject"], it["sent_at"], it["body"],
                         json.dumps(it["candidates"], ensure_ascii=False), now(), key, it["msg_id"], it["fp"]))
        if cur.rowcount:
            added += 1
            touched[key] = oid
            watch_bl_numbers(c, cur.lastrowid, oid, it, bulk)
    found, ai_queue, heads = 0, [], []
    for key in touched:
        head_id, cands, foreign = refresh_thread(c, key)
        found += bool(cands)
        if bulk:                         # 한꺼번에 가져오기: 알림·AI 는 다 끝난 뒤 한 번에
            heads.append(head_id)
            continue
        if ai_mail.enabled() and (cands or foreign):
            c.execute("UPDATE mail_items SET ai_status = 'pending' WHERE id = ?", (head_id,))
            ai_queue.append(head_id)
        else:
            notify_thread(c, head_id)
    # 일정을 못 찾은 쓰레드는 마지막 메일 뒤 30일이 지나면 정리
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM mail_items WHERE thread_key IN (SELECT thread_key FROM mail_items WHERE status = 'none')"
              " AND thread_key NOT IN (SELECT thread_key FROM mail_items WHERE created_at >= ?)", (cutoff,))
    if ai_queue:
        threading.Thread(target=run_ai_queue, args=(ai_queue,), daemon=True).start()
    if added and not bulk:
        bl_watch_soon()
    out = {"messages": len(items), "added": added, "duplicates": dup, "skipped": skipped, "with_schedule": found}
    if bulk:
        out["heads"] = heads
    return out


# ---- 메일함 통째로 가져오기 (Outlook 내보내기 .pst / Gmail Takeout .mbox / .zip / .eml)
MAIL_IMPORT_DIR = Path(os.getenv("TEAMHUB_IMPORT_DIR", "/tmp/teamhub-mail-import"))


def _iter_mailbox(path: Path, workdir: Path, password: str = ""):
    """파일 → 메일 원본(bytes) 하나씩."""
    import mailbox
    import shutil
    import subprocess
    import zipfile
    name = path.name.lower()
    if name.endswith((".pst", ".ost")):
        if not shutil.which("readpst"):
            raise RuntimeError("서버에 PST 변환 프로그램(readpst)이 없습니다. 서버 업데이트(update.sh)를 다시 실행하세요.")
        out = workdir / "pst"
        out.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["readpst", "-e", "-q", "-b", "-o", str(out), str(path)], capture_output=True, timeout=3600)
        if r.returncode != 0:
            msg = ((r.stderr or b"") + (r.stdout or b"")).decode(errors="replace").strip()[-300:]
            raise RuntimeError("PST 파일을 읽지 못했습니다. Outlook 에서 내보낸 .pst 파일이 맞는지, 비밀번호가 걸려 있지 않은지"
                               " 확인하세요." + (f" ({msg})" if msg else ""))
        for f in sorted(out.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                yield f.read_bytes()
    elif name.endswith(".mbox") or name.endswith(".mbx"):
        for msg in mailbox.mbox(str(path), create=False):
            yield msg.as_bytes()
    elif name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            pwd = password.encode() if password else None
            for info in z.infolist():
                low = info.filename.lower()
                if info.is_dir() or not low.endswith((".eml", ".mbox", ".pst", ".ost")):
                    continue
                target = workdir / "zip" / Path(info.filename).name
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with z.open(info, pwd=pwd) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                except RuntimeError as e:          # 암호 걸린 zip
                    raise RuntimeError("압축 파일 비밀번호가 필요합니다. 비밀번호 칸에 입력하세요." if "password required" in str(e)
                                       else "압축 파일 비밀번호가 맞지 않습니다.") from e
                except NotImplementedError as e:   # AES 방식 암호 (반디집·7-Zip 의 'AES' 선택)
                    raise RuntimeError("이 압축 파일은 AES 방식 암호라 서버에서 풀 수 없습니다. PC에서 압축을 푼 뒤 안의"
                                       " .pst/.mbox 파일을 올려 주세요.") from e
                if low.endswith(".eml"):
                    yield target.read_bytes()
                else:
                    yield from _iter_mailbox(target, workdir / f"z{info.header_offset}", password)
                target.unlink(missing_ok=True)
    else:
        yield path.read_bytes()


def _set_import(uid: int, **kw):
    with db() as c:
        st = json.loads(get_setting(c, f"mail_import:{uid}", "{}") or "{}")
        st.update(kw)
        set_setting(c, f"mail_import:{uid}", json.dumps(st, ensure_ascii=False))


def run_mail_import(path: Path, uid: int, months: int, password: str = ""):
    """백그라운드: 메일함 파일을 읽어 최근 N개월 메일만 저장 → 앞으로의 일정이 있는 쓰레드만 AI 분석 → 알림 한 번."""
    import shutil
    from email.parser import BytesHeaderParser
    from email.utils import parsedate_to_datetime
    workdir = path.parent
    since = datetime.now() - timedelta(days=31 * months) if months else datetime.min
    total = added = dup = old = 0
    heads, batch = set(), []

    def flush():
        nonlocal added, dup
        with db() as c:
            for raw in batch:
                try:
                    r = save_mail_items(c, raw, owner_id=uid, bulk=True)
                except Exception:
                    continue
                added += r["added"]
                dup += r["duplicates"]
                heads.update(r["heads"])
        batch.clear()
        _set_import(uid, read=total, added=added, duplicates=dup, skipped_old=old)

    try:
        for raw in _iter_mailbox(path, workdir, password):
            total += 1
            try:
                d = parsedate_to_datetime(BytesHeaderParser().parsebytes(raw[:20000])["date"])
                if d.tzinfo:
                    d = d.astimezone().replace(tzinfo=None)
                if d < since:
                    old += 1
                    continue
            except Exception:
                pass
            batch.append(raw)
            if len(batch) >= 100:
                flush()
        flush()
        with db() as c:
            live = [r["id"] for r in c.execute(
                f"SELECT id FROM mail_items WHERE id IN ({','.join('?' * len(heads)) or 'NULL'}) AND status = 'new'"
                " ORDER BY sent_at DESC", list(heads))]
            ai_ids = live[:150] if ai_mail.enabled() else []
            for i in ai_ids:
                c.execute("UPDATE mail_items SET ai_status = 'pending' WHERE id = ?", (i,))
            notify(c, uid, f"📥 메일함 가져오기 완료: {added}통 저장 · 확인할 일정 {len(live)}건"
                           + (f" (중복 {dup}통 제외)" if dup else ""))
        _set_import(uid, status="done", read=total, added=added, duplicates=dup, skipped_old=old, threads=len(live),
                    finished_at=now())
        bl_watch_soon()
        if ai_ids:
            run_ai_queue(ai_ids, quiet=True)      # 알림은 위에서 한 번만
    except Exception as e:
        _set_import(uid, status="error", error=str(e)[:300], finished_at=now())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/mailin/import")
async def mailin_import(file: UploadFile = File(...), months: int = 3, password: str = Form(""),
                       user: dict = Depends(current_user)):
    """내 메일함 파일(.pst/.mbox/.zip/.eml)을 올리면 백그라운드에서 가져온다."""
    import shutil
    name = Path(file.filename or "mail").name
    if not name.lower().endswith((".pst", ".ost", ".mbox", ".mbx", ".zip", ".eml")):
        raise HTTPException(400, "pst, mbox, zip, eml 파일만 올릴 수 있습니다.")
    with db() as c:
        st = json.loads(get_setting(c, f"mail_import:{user['id']}", "{}") or "{}")
    if st.get("status") == "running":
        raise HTTPException(400, "이미 가져오는 중입니다. 끝난 뒤 다시 올려 주세요.")
    workdir = MAIL_IMPORT_DIR / f"{user['id']}-{secrets.token_hex(4)}"
    workdir.mkdir(parents=True, exist_ok=True)
    path = workdir / name
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out, 1024 * 1024)
    months = max(0, min(int(months), 240))      # 0 = 전체 기간
    _set_import(user["id"], status="running", file=name, size=path.stat().st_size, months=months, read=0, added=0,
                duplicates=0, skipped_old=0, threads=0, error="", started_at=now(), finished_at="")
    # 비밀번호는 저장하지 않고 이번 가져오기에만 쓴다
    threading.Thread(target=run_mail_import, args=(path, user["id"], months, password), daemon=True).start()
    return {"ok": True}


@app.get("/api/mailin/import/status")
def mailin_import_status(user: dict = Depends(current_user)):
    with db() as c:
        return json.loads(get_setting(c, f"mail_import:{user['id']}", "{}") or "{}")


def thread_rows(c, key: str) -> list:
    return c.execute("SELECT * FROM mail_items WHERE thread_key = ? ORDER BY sent_at, id", (key,)).fetchall()


def match_shipments(c, cands: list, subject: str = "") -> list:
    """입고 후보가 이미 등록된 입고예정(아직 입고 전)과 같으면 연결 → 새로 만들지 않고 날짜 변경을 제안."""
    open_ships = [dict(r) for r in c.execute("SELECT id, item, eta, bl_no, hbl_no, supplier FROM shipments"
                                             " WHERE status != 'arrived'")]
    norm = lambda s: re.sub(r"[^0-9a-z가-힣]", "", str(s or "").lower())
    for cd in cands:
        # 출발(ETD·선적·출항) 날짜는 입고예정일(ETA)이 아니므로 연결하지 않음
        if cd["kind"] != "ship" or re.match(r"(etd|선적|출항)", (cd.get("label") or "").lower()):
            continue
        best = None
        for sh in open_ships:
            if cd.get("bl_no") and norm(cd["bl_no"]) in {norm(sh["bl_no"]), norm(sh["hbl_no"])} - {""}:
                best = sh
                break
            item = norm(cd.get("item") or cd.get("title") or subject)
            if item and len(item) >= 4 and norm(sh["item"]) and (norm(sh["item"]) in item or item in norm(sh["item"])):
                if not best or abs((date.fromisoformat(sh["eta"]) - date.fromisoformat(cd["date"])).days) < \
                        abs((date.fromisoformat(best["eta"]) - date.fromisoformat(cd["date"])).days):
                    best = sh
        if best:
            cd.update({"ship_id": best["id"], "ship_eta": best["eta"], "ship_item": best["item"]})
    return cands


def merge_rule_candidates(rows) -> list:
    """메일마다 찾은 날짜를 시간순으로 합침: 같은 종류·구분은 나중 메일 날짜로 바꾸고 이전 날짜를 prev_date 로."""
    merged = {}
    for r in rows:
        for cd in json.loads(r["candidates"] or "[]"):
            k = (cd["kind"], cd.get("label") or "", cd.get("time") or "" if cd["kind"] == "event" else "")
            if cd["kind"] == "event":
                k = (cd["kind"], cd["date"], cd.get("time") or "")
            old = merged.get(k)
            if old and old["date"] != cd["date"]:
                cd = {**cd, "prev_date": old.get("prev_date") or old["date"]}
            elif old and old.get("prev_date"):
                cd = {**cd, "prev_date": old["prev_date"]}
            merged[k] = cd
    return sorted(merged.values(), key=lambda c: ({"ship": 0, "event": 1, "task": 2}[c["kind"]], c["date"]))[:12]


def refresh_thread(c, key: str):
    """쓰레드의 대표(가장 최근 메일)를 정하고 규칙 방식 후보를 다시 계산. → (대표 id, 후보, 해외메일 여부)"""
    rows = thread_rows(c, key)
    head = rows[-1]
    prev = next((r for r in rows if r["status"] != "merged"), None)     # 지금까지의 대표
    cands = match_shipments(c, merge_rule_candidates(rows), head["subject"])
    # 상태: 예전 대표가 '무시'면 계속 무시 / 대표가 그대로이고 이미 처리했으면 유지 /
    #       아니면 앞으로(일주일 전 이후) 날짜가 있을 때만 '확인할 것' (예전 메일을 한꺼번에 가져와도 지난 일정은 쌓이지 않게)
    soon = (date.today() - timedelta(days=7)).isoformat()
    if prev is not None and prev["status"] == "ignored":
        status = "ignored"
    elif prev is not None and prev["id"] == head["id"] and prev["status"] == "done":
        status = "done"
    else:
        status = "new" if any(cd["date"] >= soon for cd in cands) else "none"
    c.execute("UPDATE mail_items SET status = 'merged' WHERE thread_key = ? AND id != ?", (key, head["id"]))
    c.execute("UPDATE mail_items SET status = ?, t_candidates = ?, t_count = ?, summary = CASE WHEN ? != '' THEN ? ELSE summary END"
              " WHERE id = ?", (status, json.dumps(cands, ensure_ascii=False), len(rows),
                                prev["summary"] if prev else "", prev["summary"] if prev else "", head["id"]))
    return head["id"], cands, ai_mail.is_foreign(head["body"])


def notify_thread(c, head_id: int):
    m = c.execute("SELECT * FROM mail_items WHERE id = ?", (head_id,)).fetchone()
    cands = json.loads(m["t_candidates"] or "[]")
    if m["owner_id"] and cands and m["status"] == "new":
        changed = [x for x in cands if x.get("prev_date") or x.get("ship_id")]
        what = f"일정 변경 {len(changed)}건" if changed else f"일정 {len(cands)}건"
        notify(c, m["owner_id"], f"📥 메일에서 {what}을 찾았습니다: {m['subject'][:40]}")


AI_LOCK = threading.Lock()


def ai_analyze_item(mid: int, quiet: bool = False):
    """쓰레드 전체를 Claude 로 읽기: 최신 메일 번역 + 쓰레드 기준 최신 일정. 결과로 후보를 바꾸고 주인에게 알림."""
    with db() as c:
        m = c.execute("SELECT * FROM mail_items WHERE id = ?", (mid,)).fetchone()
        if not m or m["status"] == "merged":
            return                       # 그 사이 새 메일이 와서 대표가 바뀜 → 새 대표가 다시 분석됨
        rows = thread_rows(c, m["thread_key"])
    msgs = [{"from": f"{r['from_name']} <{r['from_addr']}>", "sent_at": r["sent_at"], "subject": r["subject"],
             "body": r["body"] if r["id"] == m["id"] else (mailin.own_text(r["body"]) or r["body"][:3000])} for r in rows]
    try:
        res = ai_mail.analyze(m["subject"], f"{m['from_name']} <{m['from_addr']}>", m["sent_at"], ai_mail.thread_text(msgs))
        cands, err = ai_mail.to_candidates(res), ""
    except Exception as e:
        res, cands, err = {}, None, str(e)[:200]
    with db() as c:
        cur = c.execute("SELECT status FROM mail_items WHERE id = ?", (mid,)).fetchone()
        if not cur or cur["status"] == "merged":
            return
        if cands is not None:
            cands = match_shipments(c, cands, m["subject"])
            st = cur["status"]
            if st in ("new", "none"):
                soon = (date.today() - timedelta(days=7)).isoformat()
                st = "new" if any(cd["date"] >= soon for cd in cands) else "none"
            c.execute("UPDATE mail_items SET translation = ?, summary = ?, t_candidates = ?, status = ?, ai_status = 'done'"
                      " WHERE id = ?", (res.get("translation", ""), res.get("summary", ""),
                                        json.dumps(cands, ensure_ascii=False), st, mid))
        else:
            c.execute("UPDATE mail_items SET ai_status = ? WHERE id = ?", ("error: " + err, mid))
        if not quiet:
            notify_thread(c, mid)


def run_ai_queue(ids: list, quiet: bool = False):
    time.sleep(1)      # 메일 저장(트랜잭션)이 끝난 뒤 시작
    with AI_LOCK:      # 한 번에 하나씩 (요금·속도 제한)
        for mid in ids:
            ai_analyze_item(mid, quiet)


@app.post("/api/mailin/{mid}/analyze")
def mailin_analyze(mid: int, user: dict = Depends(current_user)):
    if not ai_mail.enabled():
        raise HTTPException(400, "AI 번역 설정(TEAMHUB_ANTHROPIC_API_KEY)이 없습니다.")
    with db() as c:
        mail_item(c, mid, user)
        c.execute("UPDATE mail_items SET ai_status = 'pending' WHERE id = ?", (mid,))
    threading.Thread(target=run_ai_queue, args=([mid],), daemon=True).start()
    return {"ok": True}


@app.post("/api/mailin")
async def mailin_receive(request: Request, x_upload_key: str = Header(default="")):
    """info@ 메일함(구글 Apps Script)이 받은 메일 원본을 보내는 곳. 업로드 키 필요."""
    with db() as c:
        key = get_setting(c, "inv_upload_key", "")
    if not key or not secrets.compare_digest(key, x_upload_key.strip()):
        raise HTTPException(403, "업로드 키가 맞지 않습니다.")
    raw = await request.body()
    if not raw or len(raw) > 40_000_000:
        raise HTTPException(400, "메일 내용이 없거나 너무 큽니다.")
    with db() as c:
        return save_mail_items(c, raw)


def mail_visible(user: dict):
    # 메일은 본인 것만. 관리자는 주인을 못 찾은 메일(직원 메일 주소 미등록)도 본다.
    if user["role"] == "admin":
        return "(m.owner_id = ? OR m.owner_id IS NULL)", [user["id"]]
    return "m.owner_id = ?", [user["id"]]


@app.get("/api/mailin")
def mailin_list(status: str = "new", q: str = "", user: dict = Depends(current_user)):
    clause, params = mail_visible(user)
    sql = (f"SELECT m.id, m.owner_id, m.from_addr, m.from_name, m.subject, m.sent_at, m.t_candidates AS candidates,"
           f" m.status, m.summary, m.ai_status, m.t_count, m.translation != '' AS translated FROM mail_items m"
           f" WHERE {clause} AND m.status != 'merged'")
    if status in ("new", "done", "ignored", "none"):
        sql += " AND m.status = ?"
        params.append(status)
    if q.strip():
        like = f"%{q.strip()}%"
        sql += (" AND m.thread_key IN (SELECT thread_key FROM mail_items WHERE subject LIKE ? OR from_name LIKE ?"
                " OR from_addr LIKE ? OR body LIKE ?)")
        params += [like] * 4
    with db() as c:
        rows = [dict(r) for r in c.execute(sql + " ORDER BY m.sent_at DESC LIMIT 300", params)]
        vc, vp = mail_visible(user)
        counts = {r[0]: r[1] for r in c.execute(f"SELECT m.status, COUNT(*) FROM mail_items m WHERE {vc} GROUP BY 1", vp)}
        for r in rows:        # 그 사이 등록된 입고예정과 다시 맞춰 봄 (자동 등록된 B/L 등)
            r["candidates"] = match_shipments(c, json.loads(r["candidates"] or "[]"), r["subject"])
    return {"items": rows, "counts": counts, "ai": ai_mail.enabled()}


def mail_item(c, mid: int, user: dict):
    clause, params = mail_visible(user)
    row = c.execute(f"SELECT * FROM mail_items m WHERE m.id = ? AND {clause}", (mid, *params)).fetchone()
    if not row:
        raise HTTPException(404, "메일을 찾을 수 없습니다.")
    return row


@app.get("/api/mailin/{mid}")
def mailin_get(mid: int, user: dict = Depends(current_user)):
    with db() as c:
        r = dict(mail_item(c, mid, user))
        r["thread"] = [{"id": t["id"], "from_name": t["from_name"], "from_addr": t["from_addr"], "sent_at": t["sent_at"],
                        "subject": t["subject"], "body": t["body"]} for t in thread_rows(c, r["thread_key"])]
        r["candidates"] = match_shipments(c, json.loads(r["t_candidates"] or "[]"), r["subject"])
    return r


@app.patch("/api/mailin/{mid}")
def mailin_status(mid: int, body: dict, user: dict = Depends(current_user)):
    st = body.get("status")
    if st not in ("new", "done", "ignored"):
        raise HTTPException(400, "잘못된 상태입니다.")
    with db() as c:
        if mail_item(c, mid, user)["status"] == "merged":
            raise HTTPException(400, "이 쓰레드에 새 메일이 와서 목록이 바뀌었습니다. 새로고침하세요.")
        c.execute("UPDATE mail_items SET status = ? WHERE id = ?", (st, mid))
    return {"ok": True}


@app.delete("/api/mailin/{mid}")
def mailin_delete(mid: int, user: dict = Depends(current_user)):
    """쓰레드 전체를 TeamHub 에서 지운다 (원래 메일함은 그대로)."""
    with db() as c:
        key = mail_item(c, mid, user)["thread_key"]
        c.execute("DELETE FROM mail_items WHERE thread_key = ? AND owner_id IS (SELECT owner_id FROM mail_items WHERE id = ?)",
                  (key, mid))
    return {"ok": True}


@app.get("/api/push/key")
def push_key(user: dict = Depends(current_user)):
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id = ?", (user["id"],)).fetchone()[0]
        return {"key": vapid_keys(c)["public"], "devices": n}


@app.post("/api/push/subscribe")
def push_subscribe(body: dict, user: dict = Depends(current_user), user_agent: str = Header(default="")):
    endpoint = str(body.get("endpoint", ""))
    keys = body.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "알림 구독 정보가 올바르지 않습니다.")
    with db() as c:
        c.execute("INSERT INTO push_subs (user_id, endpoint, p256dh, auth, ua, created_at) VALUES (?, ?, ?, ?, ?, ?)"
                  " ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh,"
                  " auth = excluded.auth, ua = excluded.ua",
                  (user["id"], endpoint, keys["p256dh"], keys["auth"], user_agent[:200], now()))
    return {"ok": True}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: dict, user: dict = Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM push_subs WHERE endpoint = ? AND user_id = ?", (str(body.get("endpoint", "")), user["id"]))
    return {"ok": True}


@app.post("/api/push/test")
def push_test(user: dict = Depends(current_user)):
    res = push_send(user["id"], {"title": "TeamHub", "body": f"{user['name']}님, 알림이 잘 옵니다 ✅", "url": "/",
                                 "tag": "test"})
    if not res:
        raise HTTPException(400, "이 계정에 알림을 켠 기기가 없습니다.")
    return {"sent": sum(1 for r in res if 200 <= r < 300), "total": len(res), "codes": res}


# ---------------------------------------------------------------- Frontend
@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon-{size}.png")
def icon(size: int):
    if size not in (180, 192, 512):
        raise HTTPException(404)
    return FileResponse(BASE_DIR / "static" / f"icon-{size}.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def index():
    # 업데이트 후 브라우저가 예전 화면을 쓰지 않도록 항상 새로 받게 한다
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


init_db()

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8100")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
