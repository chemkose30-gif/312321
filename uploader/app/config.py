"""환경설정 로딩 + 토큰 암호화 헬퍼."""
import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "uploader.db"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "http://localhost:8100").rstrip("/")
def _load_or_create_secret() -> str:
    """토큰 암호화 키. 환경변수가 없으면 무작위로 만들어 data/secret.key 에 보관한다.

    (고정된 기본값을 쓰면 저장된 액세스 토큰을 누구나 복호화할 수 있다.)
    """
    from_env = os.getenv("APP_SECRET")
    if from_env:
        return from_env
    key_file = DATA_DIR / "secret.key"
    if key_file.exists():
        return key_file.read_text().strip()
    import secrets as _secrets

    value = _secrets.token_urlsafe(48)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file.write_text(value)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass  # 윈도우 등에서는 무시
    return value


APP_SECRET = _load_or_create_secret()
PORT = int(os.getenv("PORT") or 8100)

# 외부에 공개할 때 필요한 로그인 비밀번호. 비어 있으면 잠금이 꺼진다(로컬 전용).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
AUTH_ENABLED = bool(APP_PASSWORD)
SESSION_DAYS = int(os.getenv("SESSION_DAYS") or 14)

META_API_VERSION = os.getenv("META_API_VERSION") or "v21.0"
GRAPH = f"https://graph.facebook.com/{META_API_VERSION}"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{META_API_VERSION}"

# 업로드 허용 용량(바이트). 플랫폼별 실제 한도와 별개로 서버 보호용.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES") or 4 * 1024 * 1024 * 1024)


@dataclass(frozen=True)
class PlatformConfig:
    key: str
    label: str
    color: str
    client_id: str
    client_secret: str
    # OAuth 콜백 경로에 쓰이는 프로바이더 이름(인스타/페북은 Meta 하나를 공유).
    provider: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{PUBLIC_BASE_URL}/api/oauth/{self.provider}/callback"


PLATFORMS: dict[str, PlatformConfig] = {
    "youtube": PlatformConfig(
        key="youtube", label="YouTube", color="#ff0033", provider="youtube",
        client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
        client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
    ),
    "tiktok": PlatformConfig(
        key="tiktok", label="TikTok", color="#25f4ee", provider="tiktok",
        client_id=os.getenv("TIKTOK_CLIENT_KEY", ""),
        client_secret=os.getenv("TIKTOK_CLIENT_SECRET", ""),
    ),
    "instagram": PlatformConfig(
        key="instagram", label="Instagram", color="#e1306c", provider="meta",
        client_id=os.getenv("META_APP_ID", ""),
        client_secret=os.getenv("META_APP_SECRET", ""),
    ),
    "facebook": PlatformConfig(
        key="facebook", label="Facebook", color="#1877f2", provider="meta",
        client_id=os.getenv("META_APP_ID", ""),
        client_secret=os.getenv("META_APP_SECRET", ""),
    ),
}

PLATFORM_ORDER = ["youtube", "tiktok", "instagram", "facebook"]

# 자격증명이 하나도 없으면 데모 모드: 실제 API 대신 업로드를 시뮬레이션한다.
DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes")


def demo_platform(key: str) -> bool:
    """해당 플랫폼을 데모(모의 업로드)로 처리해야 하는지."""
    return DEMO_MODE or not PLATFORMS[key].configured


_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET.encode()).digest()))


def encrypt(value: str | None) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        # APP_SECRET이 바뀌면 기존 토큰은 복호화 불가 → 재연결 필요.
        return ""
