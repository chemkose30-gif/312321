"""TeamHub - 사내 캘린더 & 업무지시 시스템 (FastAPI + SQLite)."""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DB_PATH = os.getenv("TEAMHUB_DB", str(BASE_DIR / "teamhub.db"))

app = FastAPI(title="TeamHub", version="1.0.0")

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
            CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigner ON tasks(assigner_id);
            CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
            CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
            """
        )
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            salt = secrets.token_hex(16)
            pw = os.getenv("TEAMHUB_ADMIN_PASSWORD", "admin1234")
            c.execute(
                "INSERT INTO users (username, name, dept, position, role, pw_hash, salt, created_at)"
                " VALUES ('admin', '관리자', '경영지원', '관리자', 'admin', ?, ?, ?)",
                (hash_pw(pw, salt), salt, now()),
            )


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
    role: str = "member"
    password: str


class UserUpdate(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    dept: Optional[str] = None
    position: Optional[str] = None
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
                "INSERT INTO users (username, name, dept, position, role, pw_hash, salt, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (body.username.strip(), body.name.strip(), body.dept.strip(), body.position.strip(),
                 body.role, hash_pw(body.password, salt), salt, now()),
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
    for key in ("name", "dept", "position"):
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
    "SELECT t.*, r.name AS assigner_name, r.dept AS assigner_dept,"
    " a.name AS assignee_name, a.dept AS assignee_dept,"
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
    return {**dict(t), "comments": [dict(r) for r in comments]}


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
        if body.status is not None:
            if body.status not in STATUSES:
                raise HTTPException(400, "잘못된 상태입니다.")
            fields.append("status = ?")
            values.append(body.status)
            fields.append("completed_at = ?")
            values.append(now() if body.status == "done" else None)
        if not fields:
            return {"ok": True}
        fields.append("updated_at = ?")
        values.append(now())
        c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", (*values, tid))
        if body.status is not None and body.status != t["status"]:
            target = t["assigner_id"] if user["id"] == t["assignee_id"] else t["assignee_id"]
            if target != user["id"]:
                notify(c, target, f"{user['name']}님이 '{t['title']}' 업무를"
                       f" [{STATUS_LABEL[body.status]}](으)로 변경했습니다.", tid)
    return {"ok": True}


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


# ---------------------------------------------------------------- Frontend
@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


init_db()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8100")))
