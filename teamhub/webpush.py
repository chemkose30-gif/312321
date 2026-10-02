"""Web Push 발송 (RFC 8291 aes128gcm 암호화 + RFC 8292 VAPID). 외부 라이브러리는 cryptography 만 사용.

브라우저(크롬·엣지·파이어폭스·사파리)가 준 구독 정보(endpoint, p256dh, auth)로 알림을 보낸다.
TeamHub 창이 닫혀 있어도 윈도우/휴대폰 알림으로 뜬다.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def generate_vapid() -> dict:
    """새 VAPID 키 쌍: {'private': PEM 문자열, 'public': 브라우저에 줄 b64url 공개키}"""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"private": pem, "public": b64u(_raw_public(key))}


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291: 받는 브라우저 공개키(p256dh)·auth 비밀값으로 본문을 암호화 (레코드 1개)."""
    ua_public = b64u_dec(p256dh)
    auth_secret = b64u_dec(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def vapid_header(endpoint: str, private_pem: str, public_b64: str, subject: str) -> str:
    u = urllib.parse.urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}
    signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode()) + "." +
                     b64u(json.dumps(claims, separators=(",", ":")).encode())).encode()
    key = serialization.load_pem_private_key(private_pem.encode(), None)
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    jwt = signing_input.decode() + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={public_b64}"


def send(sub: dict, data: dict, vapid: dict, subject: str, ttl: int = 86400) -> int:
    """알림 1건 발송 → HTTP 상태코드. 404/410 이면 구독이 끝난 것(삭제 대상)."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "Authorization": vapid_header(sub["endpoint"], vapid["private"], vapid["public"], subject),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(ttl), "Urgency": "high"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
