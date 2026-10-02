"""TeamHub - 사내 캘린더 & 업무지시 시스템 (FastAPI + SQLite)."""
import hashlib
import re
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import html
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import ecount
import mailer

BASE_DIR = Path(__file__).parent
DB_PATH = os.getenv("TEAMHUB_DB", str(BASE_DIR / "teamhub.db"))


@asynccontextmanager
async def lifespan(_app):
    mailer.start_scheduler(run_daily_reminders)
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
                                ("quote_items", "origin", "TEXT NOT NULL DEFAULT ''")):
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
    return public_user(user)


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
    return {"id": qid, "quote_no": q["quote_no"]}


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
ECOUNT_KEYS = ("com_code", "user_id", "api_key", "is_test", "emp_cd", "path_products", "path_quotation",
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


def read_xlsx_values(data: bytes) -> list:
    """xlsx 의 첫 시트 셀 값만 읽는다. 서식(스타일)은 무시 — 이카운트 엑셀은 서식 정보가 표준과 달라
    openpyxl 이 읽지 못하는 경우가 있다."""
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
        first = wb.find("m:sheets/m:sheet", ns)
        rid = first.get(f"{{{ns['r']}}}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.findall("pr:Relationship", ns):
            if rel.get("Id") == rid:
                target = rel.get("Target")
                sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
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
                               body.io_date or q["quote_date"], emp_cd)
    try:
        result = client.save_slip(body.kind, lines)
    except ecount.EcountError as e:
        with db() as c:
            ecount_log(c, qid, body.kind, False, "", str(e), user)
        raise HTTPException(400, f"이카운트 전송 실패: {e}")
    slips = ", ".join(result["slip_nos"])
    with db() as c:
        if result["ok"]:
            c.execute(f"UPDATE quotes SET {slip_col} = ?, updated_at = ? WHERE id = ?", (slips or "전송됨", now(), qid))
            if body.kind == "sale" and q["doc_type"] == "quote" and q["status"] in ("draft", "sent"):
                c.execute("UPDATE quotes SET status = 'won' WHERE id = ?", (qid,))
        msg = "; ".join(result["messages"]) or ("" if result["ok"] else json.dumps(result["raw"], ensure_ascii=False)[:500])
        ecount_log(c, qid, body.kind, result["ok"], slips, msg, user)
    if not result["ok"]:
        raise HTTPException(400, f"이카운트가 {label} 등록을 거부했습니다: {msg}")
    return {"ok": True, "slip_nos": result["slip_nos"], "mode": "테스트 서버" if client.is_test else "실서비스"}


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
@app.get("/api/analytics/sales")
def sales_analytics(year: int = 0, basis: str = "supply", _: dict = Depends(current_user)):
    """거래명세서(발행·출고완료) 기준 거래처별 월별/연별 납품금액."""
    col = "grand_total" if basis == "total" else "supply_total"
    base = f"FROM quotes WHERE doc_type = 'statement' AND status != 'draft'"
    with db() as c:
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
                f"SELECT customer_name, CAST(substr(quote_date, 1, 4) AS INT), CAST(substr(quote_date, 6, 2) AS INT),"
                f" SUM({col}) {base} AND substr(quote_date, 1, 4) IN (?, ?) GROUP BY 1, 2, 3", (str(year), str(year - 1))):
            e = cust.setdefault(name, {"name": name, "cur": [0] * 12, "prev": [0] * 12})
            (e["cur"] if y == year else e["prev"])[m - 1] = v or 0
        # 거래처 × 연도 합계
        yearly = {}
        for name, y, v in c.execute(f"SELECT customer_name, CAST(substr(quote_date, 1, 4) AS INT), SUM({col}) {base}"
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


@app.get("/api/analytics/decline")
def decline_analytics(months: int = 12, compare: str = "last_year", basis: str = "supply",
                      _: dict = Depends(current_user)):
    """납품이 줄어든 거래처(품목별 감소 포함)와 주문이 끊긴 거래처."""
    col = "grand_total" if basis == "total" else "supply_total"
    item_col = "supply + vat" if basis == "total" else "supply"
    months = max(1, min(months, 24))
    with db() as c:
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
                f"SELECT customer_name, SUM({col}) FROM quotes WHERE doc_type = 'statement' AND status != 'draft'"
                " AND quote_date BETWEEN ? AND ? GROUP BY 1", r)}

        def per_item(r):
            out = {}
            for n, item, unit, qty, amt in c.execute(
                    f"SELECT q.customer_name, qi.name, MAX(qi.unit), SUM(qi.qty), SUM(qi.{item_col})"
                    " FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
                    " WHERE q.doc_type = 'statement' AND q.status != 'draft' AND q.quote_date BETWEEN ? AND ?"
                    " GROUP BY 1, 2", r):
                out.setdefault(n, {})[item] = (qty or 0, amt or 0, unit or "")
            return out

        cur_c, cmp_c = per_customer(cur_r), per_customer(cmp_r)
        cur_i, cmp_i = per_item(cur_r), per_item(cmp_r)
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
        for n, d in c.execute("SELECT customer_name, quote_date FROM quotes WHERE doc_type = 'statement'"
                              " AND status != 'draft' AND quote_date BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY 2",
                              (since, ref.strftime("%Y-%m-%d"))):
            dates.setdefault(n, []).append(datetime.strptime(d, "%Y-%m-%d").date())
        year_amt = per_customer(rng(_shift_months(ref, -12) + timedelta(days=1), ref))
        prev_year_amt = per_customer(rng(_shift_months(ref, -24) + timedelta(days=1), _shift_months(ref, -12)))
        dormant = []
        for n, ds in dates.items():
            if len(ds) < 4:
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
                f"SELECT q.customer_name, qi.name, q.quote_date, SUM(qi.qty), SUM(qi.{item_col})"
                " FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
                " WHERE q.doc_type = 'statement' AND q.status != 'draft' AND q.quote_date BETWEEN ? AND ?"
                " GROUP BY 1, 2, 3 ORDER BY 3", (since5, ref.strftime("%Y-%m-%d"))):
            hist.setdefault((n, item), []).append((datetime.strptime(d, "%Y-%m-%d").date(), q or 0, a or 0))
        units = {(n, i): u for n, i, u in c.execute(
            "SELECT q.customer_name, qi.name, MAX(qi.unit) FROM quote_items qi JOIN quotes q ON q.id = qi.quote_id"
            " WHERE q.doc_type = 'statement' AND q.quote_date >= ? GROUP BY 1, 2", (since5,))}
        overdue = {}
        for (n, item), buys in hist.items():
            if len(buys) < 3:
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
    warehouse: str = ""
    note: str = ""


SHIP_FIELDS = ("item", "spec", "supplier", "customer", "qty", "unit", "eta", "status", "bl_no", "warehouse", "note")


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
        where.append("s.status != 'arrived'")
    elif view == "arrived":
        where.append("s.status = 'arrived'")
    if q:
        where.append("(s.item LIKE ? OR s.supplier LIKE ? OR s.customer LIKE ? OR s.bl_no LIKE ?)")
        params += [f"%{q}%"] * 4
    order = "s.eta DESC, s.id DESC" if view == "arrived" else "s.eta, s.id"
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
    return {"id": cur.lastrowid}


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
        ships_today = c.execute("SELECT * FROM shipments WHERE eta = ? ORDER BY status = 'arrived', id", (today,)).fetchall()
        ships_week = c.execute("SELECT * FROM shipments WHERE eta > ? AND eta <= ? AND status != 'arrived' ORDER BY eta",
                               (today, week_end)).fetchall()
        ships_late = c.execute("SELECT * FROM shipments WHERE eta < ? AND status != 'arrived' ORDER BY eta",
                               (today,)).fetchall()
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


# ---------------------------------------------------------------- Frontend
@app.get("/")
def index():
    # 업데이트 후 브라우저가 예전 화면을 쓰지 않도록 항상 새로 받게 한다
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


init_db()

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8100")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
