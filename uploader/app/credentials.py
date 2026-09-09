"""플랫폼 API 키를 앱 화면에서 입력받아 보관한다.

Render 같은 곳에서 환경변수를 넣기 번거로운 경우를 위한 경로다.
값은 APP_SECRET 으로 암호화해 SQLite 에 저장하고, 조회 시에만 복호화한다.
우선순위: 화면에서 입력한 값 > 환경변수.
"""
from . import db
from .config import decrypt, encrypt

FIELDS = ("client_id", "client_secret")


def _key(platform: str, field: str) -> str:
    return f"cred:{platform}:{field}"


def get(platform: str, field: str) -> str:
    return decrypt(db.get_setting(_key(platform, field)) or "")


def save(platform: str, client_id: str, client_secret: str) -> None:
    db.set_setting(_key(platform, "client_id"), encrypt(client_id.strip()))
    db.set_setting(_key(platform, "client_secret"), encrypt(client_secret.strip()))


def clear(platform: str) -> None:
    for field in FIELDS:
        db.delete_setting(_key(platform, field))


def stored(platform: str) -> bool:
    """화면에서 입력한 키가 저장돼 있는지."""
    return bool(get(platform, "client_id") and get(platform, "client_secret"))
