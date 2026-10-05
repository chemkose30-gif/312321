"""SQLite 저장소 - 고객/계약 데이터는 DATA_KEY 로 암호화해서 저장"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies.db"))


def _fernet() -> Fernet:
    key = os.environ.get("DATA_KEY", "")
    if not key:
        raise RuntimeError('DATA_KEY 환경변수를 설정하세요. 생성: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    return Fernet(key.encode())


def enc(obj) -> bytes:
    return _fernet().encrypt(json.dumps(obj, ensure_ascii=False).encode())


def dec(blob):
    return json.loads(_fernet().decrypt(blob)) if blob else None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")  # 삭제한 고객 데이터가 파일에 남지 않도록 덮어씀
    return conn


def init_db() -> None:
    with closing(db()) as conn, conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS planners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pw_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            planner_id INTEGER NOT NULL REFERENCES planners(id) ON DELETE CASCADE,
            data_enc BLOB NOT NULL,          -- 이름/생년월일/성별/주소/연락처/메모
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            data_enc BLOB NOT NULL,          -- 계약 1건 (담보 목록 포함)
            source TEXT,                     -- 올린 파일명
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            data_enc BLOB NOT NULL,          -- 상담일/방식/내용/다음 연락일/할 일/완료 여부
            created_at INTEGER NOT NULL
        );
        """)


# ── 설계사 계정 ───────────────────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${h.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    salt_hex, h_hex = stored.split("$")
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(h.hex(), h_hex)


def add_planner(username: str, name: str, pw: str) -> None:
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO planners (username, name, pw_hash, created_at) VALUES (?, ?, ?, ?)",
                     (username, name, hash_pw(pw), int(time.time())))


def set_password(username: str, pw: str) -> bool:
    with closing(db()) as conn, conn:
        return conn.execute("UPDATE planners SET pw_hash = ? WHERE username = ?", (hash_pw(pw), username)).rowcount > 0


def authenticate(username: str, pw: str):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM planners WHERE username = ?", (username,)).fetchone()
    return row if row and check_pw(pw, row["pw_hash"]) else None


def get_planner(pid: int):
    with closing(db()) as conn:
        return conn.execute("SELECT id, username, name FROM planners WHERE id = ?", (pid,)).fetchone()


def list_planners():
    with closing(db()) as conn:
        return conn.execute("SELECT id, username, name, created_at FROM planners ORDER BY id").fetchall()


# ── 고객/계약 (항상 planner_id 로 범위 제한) ──────────────────────────────────
def list_customers(planner_id: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM policies p WHERE p.customer_id = c.id) AS n
                               FROM customers c WHERE planner_id = ? ORDER BY updated_at DESC""", (planner_id,)).fetchall()
    return [{"id": r["id"], "n": r["n"], "updated_at": r["updated_at"], **dec(r["data_enc"])} for r in rows]


def get_customer(planner_id: int, cid: int) -> dict | None:
    with closing(db()) as conn:
        r = conn.execute("SELECT * FROM customers WHERE id = ? AND planner_id = ?", (cid, planner_id)).fetchone()
    return {"id": r["id"], "created_at": r["created_at"], "updated_at": r["updated_at"], **dec(r["data_enc"])} if r else None


def save_customer(planner_id: int, data: dict, cid: int | None = None) -> int:
    now = int(time.time())
    with closing(db()) as conn, conn:
        if cid:
            conn.execute("UPDATE customers SET data_enc = ?, updated_at = ? WHERE id = ? AND planner_id = ?",
                         (enc(data), now, cid, planner_id))
            return cid
        return conn.execute("INSERT INTO customers (planner_id, data_enc, created_at, updated_at) VALUES (?, ?, ?, ?)",
                            (planner_id, enc(data), now, now)).lastrowid


def touch_customer(conn, cid: int) -> None:
    conn.execute("UPDATE customers SET updated_at = ? WHERE id = ?", (int(time.time()), cid))


def delete_customer(planner_id: int, cid: int) -> None:
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM customers WHERE id = ? AND planner_id = ?", (cid, planner_id))


def list_policies(planner_id: int, cid: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT p.* FROM policies p JOIN customers c ON c.id = p.customer_id
                               WHERE p.customer_id = ? AND c.planner_id = ? ORDER BY p.id""", (cid, planner_id)).fetchall()
    return [{"id": r["id"], "source": r["source"], "created_at": r["created_at"], **dec(r["data_enc"])} for r in rows]


def all_policies(planner_id: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT p.* FROM policies p JOIN customers c ON c.id = p.customer_id
                               WHERE c.planner_id = ?""", (planner_id,)).fetchall()
    return [{"id": r["id"], "customer_id": r["customer_id"], **dec(r["data_enc"])} for r in rows]


def get_policy(planner_id: int, pid: int) -> dict | None:
    with closing(db()) as conn:
        r = conn.execute("""SELECT p.* FROM policies p JOIN customers c ON c.id = p.customer_id
                            WHERE p.id = ? AND c.planner_id = ?""", (pid, planner_id)).fetchone()
    return {"id": r["id"], "customer_id": r["customer_id"], **dec(r["data_enc"])} if r else None


def add_policies(planner_id: int, cid: int, policies: list[dict], source: str) -> tuple[int, int]:
    """같은 보험사+증권번호가 이미 있으면 새 내용으로 교체. (추가, 교체) 건수 반환"""
    existing = {(p.get("company"), p.get("policy_no")): p
                for p in list_policies(planner_id, cid) if p.get("policy_no")}
    added = replaced = 0
    now = int(time.time())
    with closing(db()) as conn, conn:
        for p in policies:
            key = (p.get("company"), p.get("policy_no"))
            if p.get("policy_no") and key in existing:
                old = existing[key]
                if old.get("manage"):  # 설계사가 입력한 납입·관리 정보는 유지
                    p = {**p, "manage": old["manage"]}
                conn.execute("UPDATE policies SET data_enc = ?, source = ?, created_at = ? WHERE id = ?",
                             (enc(p), source, now, old["id"]))
                replaced += 1
            else:
                conn.execute("INSERT INTO policies (customer_id, data_enc, source, created_at) VALUES (?, ?, ?, ?)",
                             (cid, enc(p), source, now))
                added += 1
        touch_customer(conn, cid)
    return added, replaced


def update_policy(planner_id: int, pid: int, data: dict) -> None:
    p = get_policy(planner_id, pid)
    if not p:
        return
    with closing(db()) as conn, conn:
        conn.execute("UPDATE policies SET data_enc = ? WHERE id = ?", (enc(data), pid))
        touch_customer(conn, p["customer_id"])


def delete_policy(planner_id: int, pid: int) -> int | None:
    p = get_policy(planner_id, pid)
    if not p:
        return None
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM policies WHERE id = ?", (pid,))
        touch_customer(conn, p["customer_id"])
    return p["customer_id"]


# ── 상담 기록 ─────────────────────────────────────────────────────────────────
def _note(r) -> dict:
    return {"id": r["id"], "customer_id": r["customer_id"], "created_at": r["created_at"], **dec(r["data_enc"])}


def list_notes(planner_id: int, cid: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT n.* FROM notes n JOIN customers c ON c.id = n.customer_id
                               WHERE n.customer_id = ? AND c.planner_id = ?""", (cid, planner_id)).fetchall()
    return sorted((_note(r) for r in rows), key=lambda n: (n.get("date") or "", n["id"]), reverse=True)


def all_notes(planner_id: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT n.* FROM notes n JOIN customers c ON c.id = n.customer_id
                               WHERE c.planner_id = ?""", (planner_id,)).fetchall()
    return [_note(r) for r in rows]


def get_note(planner_id: int, nid: int) -> dict | None:
    with closing(db()) as conn:
        r = conn.execute("""SELECT n.* FROM notes n JOIN customers c ON c.id = n.customer_id
                            WHERE n.id = ? AND c.planner_id = ?""", (nid, planner_id)).fetchone()
    return _note(r) if r else None


def add_note(planner_id: int, cid: int, data: dict) -> None:
    if not get_customer(planner_id, cid):
        return
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO notes (customer_id, data_enc, created_at) VALUES (?, ?, ?)",
                     (cid, enc(data), int(time.time())))
        touch_customer(conn, cid)


def update_note(planner_id: int, nid: int, data: dict) -> int | None:
    n = get_note(planner_id, nid)
    if not n:
        return None
    with closing(db()) as conn, conn:
        conn.execute("UPDATE notes SET data_enc = ? WHERE id = ?", (enc(data), nid))
        touch_customer(conn, n["customer_id"])
    return n["customer_id"]


def delete_note(planner_id: int, nid: int) -> int | None:
    n = get_note(planner_id, nid)
    if not n:
        return None
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
    return n["customer_id"]
