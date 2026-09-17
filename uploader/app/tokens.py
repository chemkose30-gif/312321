"""토큰 만료 확인과 갱신.

플랫폼마다 갱신 방법이 다르다.
- 유튜브 : refresh_token 으로 새 액세스 토큰 발급 (무기한 갱신 가능)
- 틱톡   : refresh_token 으로 갱신
- 인스타(직접 로그인) : 장기 토큰을 60일마다 연장
- 인스타(페북 경유)·페이스북 : 장기 사용자 토큰으로 받은 페이지 토큰이라
           사용자 토큰이 살아 있는 동안 유효하다. 만료되면 다시 로그인해야 한다.
"""
import time

from . import db
from .platforms import instagram_login, tiktok, youtube

# 이 기간 안에 만료되면 미리 갱신한다.
REFRESH_WINDOW_SEC = 7 * 86400
# 이 기간 안에 만료되면 화면에 경고한다.
WARN_WINDOW_SEC = 14 * 86400

RELOGIN_ONLY = "다시 로그인해야 합니다"


def status(account: dict) -> dict:
    """계정의 토큰 상태 — 화면에 그대로 뿌릴 수 있는 형태."""
    expires_at = account.get("expires_at") or 0
    left = expires_at - time.time() if expires_at else None

    if not account.get("linked"):
        state, text = "none", "로그인 연결 필요"
    elif left is None:
        state, text = "ok", "만료 없음"
    elif left <= 0:
        state, text = "expired", "만료됨"
    elif left < WARN_WINDOW_SEC:
        state, text = "soon", f"{int(left // 86400)}일 남음"
    else:
        state, text = "ok", f"{int(left // 86400)}일 남음"

    return {
        "state": state,
        "text": text,
        "expires_at": expires_at or None,
        "can_refresh": _refresher(account) is not None,
    }


def _refresher(account: dict):
    """이 계정을 자동 갱신할 수 있는 함수(없으면 None).

    목록 조회 때는 토큰 값을 싣지 않으므로 has_refresh 로도 판단한다.
    """
    platform = account.get("platform")
    has_refresh = bool(account.get("refresh_token") or account.get("has_refresh"))
    if platform == "youtube":
        return youtube._access_token if has_refresh else None
    if platform == "tiktok":
        return tiktok._access_token if has_refresh else None
    if platform == "instagram" and (account.get("meta") or {}).get("auth") == "instagram_login":
        return instagram_login._fresh_token
    return None


async def refresh(account_id: str) -> dict:
    """계정 하나의 토큰을 갱신한다."""
    account = db.get_account(account_id)
    if not account:
        return {"ok": False, "message": "계정을 찾을 수 없습니다."}
    if not account.get("access_token"):
        return {"ok": False, "message": "아직 로그인 연결이 안 된 계정입니다."}

    fn = _refresher(account)
    if fn is None:
        return {
            "ok": False,
            "message": f"이 계정은 자동 갱신을 지원하지 않습니다. 만료되면 {RELOGIN_ONLY}.",
        }
    try:
        # 각 플랫폼의 갱신 함수는 만료가 임박했을 때만 실제로 갱신한다.
        # 사용자가 직접 누른 경우에는 만료된 것처럼 넘겨 강제로 갱신시킨다.
        await fn({**account, "expires_at": 0})
    except Exception as exc:
        return {"ok": False, "message": f"갱신 실패: {type(exc).__name__}: {exc}"}

    fresh = db.get_account(account_id, with_tokens=False) or {}
    return {"ok": True, "message": "갱신했습니다.", "status": status(fresh)}


async def refresh_expiring() -> list[str]:
    """만료가 가까운 계정들을 미리 갱신한다. 갱신한 계정 이름 목록을 돌려준다."""
    done = []
    for account in db.list_accounts(with_tokens=True):
        if not account.get("access_token"):
            continue
        expires_at = account.get("expires_at") or 0
        if not expires_at or expires_at - time.time() > REFRESH_WINDOW_SEC:
            continue
        if _refresher(account) is None:
            continue
        result = await refresh(account["id"])
        label = account.get("display_name") or account["id"]
        print(f"[token] {account['platform']} {label} 갱신 {'성공' if result['ok'] else '실패'}"
              f" — {result['message']}")
        if result["ok"]:
            done.append(label)
    return done
