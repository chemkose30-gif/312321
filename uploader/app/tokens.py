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
    """계정의 토큰 상태 — 화면에 그대로 뿌릴 수 있는 형태.

    유튜브·틱톡의 액세스 토큰은 1시간짜리지만 refresh_token 으로 언제든
    다시 받는다. 그 만료 시각은 사용자에게 의미가 없으므로 표시하지 않는다.
    """
    expires_at = account.get("expires_at") or 0
    left = expires_at - time.time() if expires_at else None

    if not account.get("linked"):
        return _row("none", "로그인 연결 필요", expires_at, account)

    if _auto_renewing(account):
        return _row("ok", "자동 갱신", expires_at, account)

    if left is None:
        return _row("ok", "만료 없음", expires_at, account)
    if left <= 0:
        return _row("expired", "만료됨", expires_at, account)
    if left < WARN_WINDOW_SEC:
        return _row("soon", f"{int(left // 86400)}일 남음", expires_at, account)
    return _row("ok", f"{int(left // 86400)}일 남음", expires_at, account)


def _row(state: str, text: str, expires_at: float, account: dict) -> dict:
    return {
        "state": state,
        "text": text,
        "auto": _auto_renewing(account),
        "expires_at": expires_at or None,
        "can_refresh": _refresher(account) is not None,
    }


def _auto_renewing(account: dict) -> bool:
    """refresh_token 으로 언제든 다시 받을 수 있는 계정인지."""
    return (
        account.get("platform") in ("youtube", "tiktok")
        and bool(account.get("refresh_token") or account.get("has_refresh"))
    )


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
        if _auto_renewing(account):
            continue      # 쓸 때 알아서 다시 받으므로 미리 갱신할 필요가 없다
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
