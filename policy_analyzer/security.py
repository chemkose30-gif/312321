"""보안 기능: 2단계 인증(TOTP), 로그인 시도 제한, IP 허용 목록, 업로드 파일 검사"""

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import struct
import time
from urllib.parse import quote

from fastapi import Request

# ── 2단계 인증 (Google Authenticator 등 OTP 앱, RFC 6238) ─────────────────────
ISSUER = os.environ.get("OTP_ISSUER", "보장분석")


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def verify_totp(secret: str, code: str, last_counter: int = -1) -> int | None:
    """맞으면 사용한 counter 반환 (같은 코드 재사용 방지용), 틀리면 None. 앞뒤 30초 허용"""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    now = int(time.time()) // 30
    for c in (now - 1, now, now + 1):
        if c > last_counter and hmac.compare_digest(_totp(secret, c), code):
            return c
    return None


def totp_uri(secret: str, username: str) -> str:
    return f"otpauth://totp/{quote(ISSUER)}:{quote(username)}?secret={secret}&issuer={quote(ISSUER)}"


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return img.to_string(encoding="unicode")


# ── 로그인 시도 제한 ──────────────────────────────────────────────────────────
MAX_FAILS = 5            # 이 횟수만큼 틀리면
LOCK_SECONDS = 15 * 60   # 이 시간 동안 잠금
_fails: dict[str, list[float]] = {}


def is_locked(*keys: str) -> bool:
    now = time.time()
    for k in keys:
        recent = [t for t in _fails.get(k, []) if now - t < LOCK_SECONDS]
        _fails[k] = recent
        if len(recent) >= MAX_FAILS:
            return True
    return False


def record_fail(*keys: str) -> None:
    for k in keys:
        _fails.setdefault(k, []).append(time.time())


def clear_fails(*keys: str) -> None:
    for k in keys:
        _fails.pop(k, None)


# ── 접속 IP ──────────────────────────────────────────────────────────────────
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"   # nginx 등 뒤에서 운영할 때만 1
_ALLOWED = [ipaddress.ip_network(x.strip(), strict=False)
            for x in os.environ.get("ALLOWED_IPS", "").split(",") if x.strip()]


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()  # 신뢰하는 프록시가 마지막에 붙인 값
    return request.client.host if request.client else ""


def ip_allowed(ip: str) -> bool:
    if not _ALLOWED:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _ALLOWED)


# ── 업로드 파일 실제 형식 검사 (확장자/헤더 위조 방지) ─────────────────────────
def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ── 응답 보안 헤더 ────────────────────────────────────────────────────────────
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def security_headers(https: bool) -> dict[str, str]:
    h = {
        "Content-Security-Policy": CSP,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",   # 고객 정보가 브라우저·프록시에 캐시되지 않도록
        "Pragma": "no-cache",
    }
    if https:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h
