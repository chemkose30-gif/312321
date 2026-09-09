"""단일 비밀번호 로그인 — 외부에 공개할 때 최소한의 잠금."""
import hashlib
import hmac
import secrets
import time

from .config import APP_SECRET, SESSION_DAYS

COOKIE = "uploader_session"
_SIGN_KEY = hashlib.sha256(f"session:{APP_SECRET}".encode()).digest()

# IP별 로그인 실패 기록 (무차별 대입 완화)
_failures: dict[str, list[float]] = {}
MAX_FAILURES = 8
WINDOW = 300.0


def _sign(expires_at: int) -> str:
    return hmac.new(_SIGN_KEY, str(expires_at).encode(), hashlib.sha256).hexdigest()


def issue_token() -> str:
    expires_at = int(time.time()) + SESSION_DAYS * 86400
    return f"{expires_at}.{_sign(expires_at)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    raw_expiry, _, signature = token.partition(".")
    if not raw_expiry.isdigit():
        return False
    expires_at = int(raw_expiry)
    if expires_at < time.time():
        return False
    return hmac.compare_digest(signature, _sign(expires_at))


def check_password(candidate: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def throttled(ip: str) -> bool:
    """짧은 시간에 실패가 반복되면 잠시 막는다."""
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < WINDOW]
    _failures[ip] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def new_secret_hint() -> str:
    """설정 안내용 임의 비밀번호 예시."""
    return secrets.token_urlsafe(12)
