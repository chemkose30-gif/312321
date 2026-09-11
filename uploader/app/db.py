"""SQLite 저장소: 연결된 계정 / 업로드 작업 / 플랫폼별 대상."""
import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from .config import DB_PATH, decrypt, encrypt

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    label         TEXT,
    category      TEXT,
    avatar        TEXT,
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    REAL,
    meta          TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    UNIQUE (platform, external_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    hashtags    TEXT NOT NULL DEFAULT '[]',
    video_path  TEXT NOT NULL,
    video_name  TEXT NOT NULL,
    video_size  INTEGER NOT NULL DEFAULT 0,
    thumb_path  TEXT,
    media_token TEXT NOT NULL DEFAULT '',
    options     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS job_targets (
    id         TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    platform   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    progress   INTEGER NOT NULL DEFAULT 0,
    message    TEXT NOT NULL DEFAULT '',
    remote_id  TEXT,
    url        TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id          TEXT PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'video/mp4',
    thumb_path  TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_sets (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    accounts   TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_targets_job ON job_targets(job_id);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """이전 버전 DB에 없는 컬럼을 채운다."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "label" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN label TEXT")
    if "category" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN category TEXT")


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = connect().execute(sql, params)
        connect().commit()
        return cur


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, params).fetchall()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ── OAuth state ──────────────────────────────────────────────
def save_state(state: str, provider: str) -> None:
    _exec("INSERT OR REPLACE INTO oauth_states VALUES (?,?,?)", (state, provider, time.time()))
    # 10분 지난 state 정리.
    _exec("DELETE FROM oauth_states WHERE created_at < ?", (time.time() - 600,))


def pop_state(state: str) -> str | None:
    rows = _rows("SELECT provider FROM oauth_states WHERE state=?", (state,))
    if not rows:
        return None
    _exec("DELETE FROM oauth_states WHERE state=?", (state,))
    return rows[0]["provider"]


# ── accounts ─────────────────────────────────────────────────
def _account_dict(row: sqlite3.Row, *, with_tokens: bool = False) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "platform": row["platform"],
        "external_id": row["external_id"],
        "name": row["name"],
        "label": row["label"],
        "display_name": row["label"] or row["name"],
        "category": row["category"] or "",
        "linked": bool(row["access_token"]),
        "manual": bool(json.loads(row["meta"] or "{}").get("manual")),
        "avatar": row["avatar"],
        "expires_at": row["expires_at"],
        "meta": json.loads(row["meta"] or "{}"),
        "created_at": row["created_at"],
    }
    if with_tokens:
        data["access_token"] = decrypt(row["access_token"])
        data["refresh_token"] = decrypt(row["refresh_token"])
    return data


def upsert_account(
    platform: str,
    external_id: str,
    name: str,
    *,
    avatar: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: float | None = None,
    meta: dict | None = None,
) -> str:
    existing = _rows(
        "SELECT id, refresh_token FROM accounts WHERE platform=? AND external_id=?",
        (platform, external_id),
    )
    account_id = existing[0]["id"] if existing else new_id("acc")
    # 재연결 시 refresh_token이 안 오는 경우(구글)에는 기존 값을 유지한다.
    refresh_enc = encrypt(refresh_token) if refresh_token else (existing[0]["refresh_token"] if existing else "")
    _exec(
        """INSERT INTO accounts (id, platform, external_id, name, avatar, access_token,
                                 refresh_token, expires_at, meta, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(platform, external_id) DO UPDATE SET
             name=excluded.name, avatar=excluded.avatar, access_token=excluded.access_token,
             refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, meta=excluded.meta""",
        (
            account_id, platform, external_id, name, avatar, encrypt(access_token),
            refresh_enc, expires_at, json.dumps(meta or {}, ensure_ascii=False), time.time(),
        ),
    )
    return account_id


def list_accounts(*, with_tokens: bool = False) -> list[dict]:
    rows = _rows("SELECT * FROM accounts ORDER BY created_at")
    return [_account_dict(r, with_tokens=with_tokens) for r in rows]


def get_account(account_id: str, *, with_tokens: bool = True) -> dict | None:
    rows = _rows("SELECT * FROM accounts WHERE id=?", (account_id,))
    return _account_dict(rows[0], with_tokens=with_tokens) if rows else None


def update_account_tokens(
    account_id: str, access_token: str, refresh_token: str | None, expires_at: float | None
) -> None:
    if refresh_token:
        _exec(
            "UPDATE accounts SET access_token=?, refresh_token=?, expires_at=? WHERE id=?",
            (encrypt(access_token), encrypt(refresh_token), expires_at, account_id),
        )
    else:
        _exec(
            "UPDATE accounts SET access_token=?, expires_at=? WHERE id=?",
            (encrypt(access_token), expires_at, account_id),
        )


def set_account_label(account_id: str, label: str) -> None:
    _exec("UPDATE accounts SET label=? WHERE id=?", (label or None, account_id))


def set_account_category(account_id: str, category: str) -> None:
    _exec("UPDATE accounts SET category=? WHERE id=?", (category or None, account_id))


def list_categories() -> list[str]:
    rows = _rows(
        "SELECT DISTINCT category FROM accounts WHERE category IS NOT NULL AND category <> ''"
        " ORDER BY category"
    )
    return [r["category"] for r in rows]


def count_accounts(platform: str) -> int:
    return _rows("SELECT COUNT(*) AS n FROM accounts WHERE platform=?", (platform,))[0]["n"]


def delete_account(account_id: str) -> None:
    _exec("DELETE FROM accounts WHERE id=?", (account_id,))


def delete_platform_accounts(platform: str) -> None:
    _exec("DELETE FROM accounts WHERE platform=?", (platform,))


# ── jobs ─────────────────────────────────────────────────────
def create_job(
    *,
    title: str,
    description: str,
    hashtags: list[str],
    video_path: str,
    video_name: str,
    video_size: int,
    thumb_path: str | None,
    media_token: str,
    options: dict,
) -> str:
    job_id = new_id("job")
    _exec(
        """INSERT INTO jobs (id, created_at, title, description, hashtags, video_path, video_name,
                             video_size, thumb_path, media_token, options, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending')""",
        (
            job_id, time.time(), title, description, json.dumps(hashtags, ensure_ascii=False),
            video_path, video_name, video_size, thumb_path, media_token,
            json.dumps(options, ensure_ascii=False),
        ),
    )
    return job_id


def add_target(job_id: str, platform: str, account_id: str) -> str:
    target_id = new_id("tgt")
    _exec(
        "INSERT INTO job_targets (id, job_id, platform, account_id, status, progress, message, updated_at)"
        " VALUES (?,?,?,?,'pending',0,'대기 중',?)",
        (target_id, job_id, platform, account_id, time.time()),
    )
    return target_id


def update_target(
    target_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    remote_id: str | None = None,
    url: str | None = None,
) -> None:
    sets, params = ["updated_at=?"], [time.time()]
    for column, value in (
        ("status", status), ("progress", progress), ("message", message),
        ("remote_id", remote_id), ("url", url),
    ):
        if value is not None:
            sets.append(f"{column}=?")
            params.append(value)
    params.append(target_id)
    _exec(f"UPDATE job_targets SET {', '.join(sets)} WHERE id=?", tuple(params))


def set_job_status(job_id: str, status: str) -> None:
    _exec("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


def _job_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "description": row["description"],
        "hashtags": json.loads(row["hashtags"] or "[]"),
        "video_path": row["video_path"],
        "video_name": row["video_name"],
        "video_size": row["video_size"],
        "thumb_path": row["thumb_path"],
        "media_token": row["media_token"],
        "options": json.loads(row["options"] or "{}"),
        "status": row["status"],
    }


def get_job(job_id: str) -> dict | None:
    rows = _rows("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        return None
    job = _job_dict(rows[0])
    job["targets"] = get_targets(job_id)
    return job


def get_targets(job_id: str) -> list[dict]:
    rows = _rows("SELECT * FROM job_targets WHERE job_id=? ORDER BY rowid", (job_id,))
    return [dict(r) for r in rows]


def list_jobs(limit: int = 30) -> list[dict]:
    rows = _rows("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    jobs = []
    for row in rows:
        job = _job_dict(row)
        job.pop("video_path", None)
        job["targets"] = get_targets(job["id"])
        jobs.append(job)
    return jobs


def active_video_paths() -> set[str]:
    """아직 게시 중인 작업이 쓰고 있는 영상 파일 경로 — 정리에서 제외해야 한다."""
    rows = _rows("SELECT video_path FROM jobs WHERE status IN ('running','pending')")
    return {r["video_path"] for r in rows if r["video_path"]}


def find_job_by_media_token(token: str) -> dict | None:
    rows = _rows("SELECT * FROM jobs WHERE media_token=?", (token,))
    return _job_dict(rows[0]) if rows else None


# ── media ────────────────────────────────────────────────────
def create_media(
    *, path: str, name: str, size: int, content_type: str, thumb_path: str | None = None
) -> dict:
    import secrets

    media_id, token = new_id("med"), secrets.token_urlsafe(24)
    _exec(
        "INSERT INTO media (id, token, path, name, size, content_type, thumb_path, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (media_id, token, path, name, size, content_type, thumb_path, time.time()),
    )
    return get_media(media_id)  # type: ignore[return-value]


def get_media(media_id: str) -> dict | None:
    rows = _rows("SELECT * FROM media WHERE id=?", (media_id,))
    return dict(rows[0]) if rows else None


def get_media_by_token(token: str) -> dict | None:
    rows = _rows("SELECT * FROM media WHERE token=?", (token,))
    return dict(rows[0]) if rows else None


def set_media_thumb(media_id: str, thumb_path: str) -> None:
    _exec("UPDATE media SET thumb_path=? WHERE id=?", (thumb_path, media_id))


# ── 채널 세트 ────────────────────────────────────────────────
def _set_dict(row: sqlite3.Row, valid: set[str]) -> dict:
    """세트에 담긴 계정 중 이미 삭제된 것은 빼고 돌려준다."""
    ids = [i for i in json.loads(row["accounts"] or "[]") if i in valid]
    return {"id": row["id"], "name": row["name"], "account_ids": ids, "created_at": row["created_at"]}


def _valid_account_ids() -> set[str]:
    return {r["id"] for r in _rows("SELECT id FROM accounts")}


def list_sets() -> list[dict]:
    valid = _valid_account_ids()
    return [_set_dict(r, valid) for r in _rows("SELECT * FROM channel_sets ORDER BY created_at")]


def get_set(set_id: str) -> dict | None:
    rows = _rows("SELECT * FROM channel_sets WHERE id=?", (set_id,))
    return _set_dict(rows[0], _valid_account_ids()) if rows else None


def create_set(name: str, account_ids: list[str]) -> str:
    set_id = new_id("set")
    _exec(
        "INSERT INTO channel_sets (id, name, accounts, created_at) VALUES (?,?,?,?)",
        (set_id, name, json.dumps(account_ids), time.time()),
    )
    return set_id


def update_set(set_id: str, *, name: str | None = None, account_ids: list[str] | None = None) -> None:
    if name is not None:
        _exec("UPDATE channel_sets SET name=? WHERE id=?", (name, set_id))
    if account_ids is not None:
        _exec("UPDATE channel_sets SET accounts=? WHERE id=?", (json.dumps(account_ids), set_id))


def delete_set(set_id: str) -> None:
    _exec("DELETE FROM channel_sets WHERE id=?", (set_id,))


# ── 설정값 ───────────────────────────────────────────────────
def get_setting(key: str) -> str | None:
    rows = _rows("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def set_setting(key: str, value: str) -> None:
    _exec(
        "INSERT INTO settings (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def delete_setting(key: str) -> None:
    _exec("DELETE FROM settings WHERE key=?", (key,))


def fail_interrupted_jobs() -> int:
    """서버가 죽는 바람에 '진행 중'으로 남은 작업을 실패로 정리한다.

    (재시작 뒤에도 영원히 진행 중으로 보이는 것을 막는다.)
    """
    cur = _exec(
        "UPDATE job_targets SET status='failed', message='서버가 재시작되어 중단되었습니다. 다시 올려주세요.',"
        " updated_at=? WHERE status IN ('running','pending')",
        (time.time(),),
    )
    changed = cur.rowcount or 0
    _exec("UPDATE jobs SET status='failed' WHERE status IN ('running','pending')")
    return changed
