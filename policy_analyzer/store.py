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
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            planner_id INTEGER,              -- 로그인 실패 등은 NULL 가능
            ts INTEGER NOT NULL,
            ip TEXT,
            action TEXT NOT NULL,            -- login / view_customer / upload / delete ...
            target TEXT                      -- 고객·계약 번호 등 (개인정보 내용은 남기지 않음)
        );
        CREATE INDEX IF NOT EXISTS audit_planner_ts ON audit_log (planner_id, ts);
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            data_enc BLOB NOT NULL,          -- 상담일/방식/내용/다음 연락일/할 일/완료 여부
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS note_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            mime TEXT NOT NULL,
            data_enc BLOB NOT NULL,          -- 이미지 바이트 (암호화)
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS note_img_note ON note_images (note_id);
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(planners)")}
        for col, ddl in (("totp_enc", "BLOB"), ("totp_last", "INTEGER DEFAULT -1"), ("pw_changed_at", "INTEGER")):
            if col not in cols:
                conn.execute(f"ALTER TABLE planners ADD COLUMN {col} {ddl}")
        icols = {r["name"] for r in conn.execute("PRAGMA table_info(note_images)")}
        if "in_report" not in icols:  # 고객용 리포트에 넣을지 여부 (기본: 안 넣음)
            conn.execute("ALTER TABLE note_images ADD COLUMN in_report INTEGER DEFAULT 0")
    try:
        os.chmod(DB_PATH, 0o600)  # DB 파일은 소유자만 읽기·쓰기
    except OSError:
        pass


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


_DUMMY_HASH = hash_pw(secrets.token_hex(8))


def authenticate(username: str, pw: str):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM planners WHERE username = ?", (username,)).fetchone()
    if not row:
        check_pw(pw, _DUMMY_HASH)  # 없는 아이디도 같은 시간이 걸리게 (아이디 추측 방지)
        return None
    return row if check_pw(pw, row["pw_hash"]) else None


def change_password(planner_id: int, new_pw: str) -> None:
    with closing(db()) as conn, conn:
        conn.execute("UPDATE planners SET pw_hash = ?, pw_changed_at = ? WHERE id = ?",
                     (hash_pw(new_pw), int(time.time()), planner_id))


def get_totp(planner_id: int) -> tuple[str | None, int]:
    with closing(db()) as conn:
        r = conn.execute("SELECT totp_enc, totp_last FROM planners WHERE id = ?", (planner_id,)).fetchone()
    return (dec(r["totp_enc"]) if r and r["totp_enc"] else None), (r["totp_last"] if r and r["totp_last"] is not None else -1)


def set_totp(planner_id: int, secret: str | None, last: int = -1) -> None:
    with closing(db()) as conn, conn:
        conn.execute("UPDATE planners SET totp_enc = ?, totp_last = ? WHERE id = ?",
                     (enc(secret) if secret else None, last, planner_id))


def set_totp_last(planner_id: int, last: int) -> None:
    with closing(db()) as conn, conn:
        conn.execute("UPDATE planners SET totp_last = ? WHERE id = ?", (last, planner_id))


def reset_totp(username: str) -> bool:
    with closing(db()) as conn, conn:
        return conn.execute("UPDATE planners SET totp_enc = NULL, totp_last = -1 WHERE username = ?",
                            (username,)).rowcount > 0


# ── 접속 기록 (개인정보 안전성 확보조치 기준: 접속기록 보관) ─────────────────
def audit(planner_id: int | None, ip: str, action: str, target: str = "") -> None:
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO audit_log (planner_id, ts, ip, action, target) VALUES (?, ?, ?, ?, ?)",
                     (planner_id, int(time.time()), ip, action, target[:100]))


def list_audit(planner_id: int | None = None, limit: int = 200):
    with closing(db()) as conn:
        if planner_id is None:
            return conn.execute("""SELECT a.*, p.username FROM audit_log a LEFT JOIN planners p ON p.id = a.planner_id
                                   ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
        return conn.execute("SELECT * FROM audit_log WHERE planner_id = ? ORDER BY id DESC LIMIT ?",
                            (planner_id, limit)).fetchall()


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


def add_note(planner_id: int, cid: int, data: dict) -> int | None:
    if not get_customer(planner_id, cid):
        return None
    with closing(db()) as conn, conn:
        nid = conn.execute("INSERT INTO notes (customer_id, data_enc, created_at) VALUES (?, ?, ?)",
                           (cid, enc(data), int(time.time()))).lastrowid
        touch_customer(conn, cid)
    return nid


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
        conn.execute("DELETE FROM notes WHERE id = ?", (nid,))  # 이미지는 ON DELETE CASCADE 로 함께 삭제
    return n["customer_id"]


# ── 상담 기록 첨부 이미지 (암호화 저장, 담당 설계사만 접근) ────────────────────
def add_note_image(planner_id: int, nid: int, mime: str, data: bytes) -> bool:
    if not get_note(planner_id, nid):
        return False
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO note_images (note_id, mime, data_enc, created_at) VALUES (?, ?, ?, ?)",
                     (nid, mime, _fernet().encrypt(data), int(time.time())))
    return True


def note_images(planner_id: int, nid: int) -> list[dict]:
    with closing(db()) as conn:
        rows = conn.execute("""SELECT i.id, i.in_report FROM note_images i JOIN notes n ON n.id = i.note_id
                               JOIN customers c ON c.id = n.customer_id
                               WHERE i.note_id = ? AND c.planner_id = ? ORDER BY i.id""", (nid, planner_id)).fetchall()
    return [{"id": r["id"], "in_report": bool(r["in_report"])} for r in rows]


def note_image_ids(planner_id: int, nid: int) -> list[int]:
    return [i["id"] for i in note_images(planner_id, nid)]


def report_image_ids(planner_id: int, cid: int) -> list[int]:
    """고객용 리포트에 넣기로 표시된 사진 (상담일 순)"""
    with closing(db()) as conn:
        rows = conn.execute("""SELECT i.id FROM note_images i JOIN notes n ON n.id = i.note_id
                               JOIN customers c ON c.id = n.customer_id
                               WHERE n.customer_id = ? AND c.planner_id = ? AND i.in_report = 1
                               ORDER BY n.id, i.id""", (cid, planner_id)).fetchall()
    return [r["id"] for r in rows]


def set_image_report(planner_id: int, img_id: int, on: bool) -> int | None:
    """이미지의 리포트 표시 여부 변경. 속한 note_id 반환."""
    with closing(db()) as conn, conn:
        r = conn.execute("""SELECT i.id, i.note_id FROM note_images i JOIN notes n ON n.id = i.note_id
                            JOIN customers c ON c.id = n.customer_id
                            WHERE i.id = ? AND c.planner_id = ?""", (img_id, planner_id)).fetchone()
        if not r:
            return None
        conn.execute("UPDATE note_images SET in_report = ? WHERE id = ?", (1 if on else 0, img_id))
    return r["note_id"]


def count_note_images(planner_id: int, nid: int) -> int:
    return len(note_image_ids(planner_id, nid))


def get_note_image(planner_id: int, img_id: int) -> tuple[str, bytes] | None:
    with closing(db()) as conn:
        r = conn.execute("""SELECT i.mime, i.data_enc FROM note_images i JOIN notes n ON n.id = i.note_id
                            JOIN customers c ON c.id = n.customer_id
                            WHERE i.id = ? AND c.planner_id = ?""", (img_id, planner_id)).fetchone()
    return (r["mime"], _fernet().decrypt(r["data_enc"])) if r else None


def delete_note_image(planner_id: int, img_id: int) -> int | None:
    """이미지가 속한 note_id 반환 (권한 확인 포함)"""
    with closing(db()) as conn, conn:
        r = conn.execute("""SELECT i.id, i.note_id FROM note_images i JOIN notes n ON n.id = i.note_id
                            JOIN customers c ON c.id = n.customer_id
                            WHERE i.id = ? AND c.planner_id = ?""", (img_id, planner_id)).fetchone()
        if not r:
            return None
        conn.execute("DELETE FROM note_images WHERE id = ?", (img_id,))
    return r["note_id"]
