"""TeamHub - 사내 캘린더 & 업무지시 시스템 (FastAPI + SQLite)."""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import html
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
            CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigner ON tasks(assigner_id);
            CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
            CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "email" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
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


@app.post("/api/login")
def login(body: LoginIn):
    with db() as c:
        u = c.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (body.username.strip(),)
        ).fetchone()
        if not u or hash_pw(body.password, u["salt"]) != u["pw_hash"]:
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
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
    return {"events": [dict(r) for r in events], "tasks": [dict(r) for r in tasks]}


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
    return {**dict(t), "comments": [dict(r) for r in comments], "mails": [dict(r) for r in mails]}


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
    return {
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
    return FileResponse(BASE_DIR / "static" / "index.html")


init_db()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8100")))
