"""
텔레그램으로 MT5 시스템트레이딩을 원격 조작하는 봇 (Windows, MT5가 설치된 PC에서 실행)

명령
  /menu    버튼 메뉴 열기
  /on      시스템트레이딩 켜기
  /off     시스템트레이딩 끄기
  /status  상태 + 잔액/평가금/포지션
  /launch  MT5 켜기 (꺼져 있을 때)

보안: bot_config.ini 의 chat_id 에 등록된 사람만 명령할 수 있다.
설정 방법은 README.md 참고.
"""

import configparser
import os
import sys
import time
import traceback

import requests

import mt5_control as mc
from mt5_control import mt5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "bot_config.ini")

POLL_TIMEOUT = 30       # 텔레그램 long polling 대기(초)
WATCH_INTERVAL = 30     # 상태 변화 감시 주기(초)


def load_config() -> tuple[str, set[int]]:
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"설정 파일이 없습니다: {CONFIG_PATH}\n"
                 "bot_config.example.ini 를 bot_config.ini 로 복사한 뒤 토큰을 넣어주세요.")
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    token = cp.get("telegram", "token", fallback="").strip()
    if not token or token.startswith("여기에"):
        sys.exit("bot_config.ini 에 봇 토큰(token)을 넣어주세요.")
    raw = cp.get("telegram", "chat_id", fallback="")
    ids = {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}
    return token, ids


TOKEN, ALLOWED = load_config()
API = f"https://api.telegram.org/bot{TOKEN}"

MENU = {
    "inline_keyboard": [
        [{"text": "▶ 시스템트레이딩 ON", "callback_data": "on"},
         {"text": "■ 시스템트레이딩 OFF", "callback_data": "off"}],
        [{"text": "📊 상태", "callback_data": "status"},
         {"text": "🖥 MT5 켜기", "callback_data": "launch"}],
    ]
}


# ── 텔레그램 API ────────────────────────────────────────────────────────
def tg(method: str, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=POLL_TIMEOUT + 10)
        return r.json()
    except requests.RequestException as e:
        print(f"[텔레그램 오류] {e}")
        return {}


def send(chat_id: int, text: str, menu: bool = True) -> None:
    params = {"chat_id": chat_id, "text": text}
    if menu:
        params["reply_markup"] = MENU
    tg("sendMessage", **params)


def broadcast(text: str) -> None:
    for cid in ALLOWED:
        send(cid, text)


# ── MT5 작업 ───────────────────────────────────────────────────────────
def status_report() -> str:
    if not mc.connect(launch=False):
        return "⚠️ MT5가 실행 중이 아니거나 연결되지 않았습니다.\n/launch 로 켤 수 있습니다."
    state = mc.is_algo_on()
    lines = [f"시스템트레이딩: {'🟢 ON' if state else '🔴 OFF'}"]
    acc = mt5.account_info()
    if acc:
        lines += [
            f"계좌: {acc.login} ({acc.server})",
            f"잔액: {acc.balance:,.2f} {acc.currency}",
            f"평가금: {acc.equity:,.2f} {acc.currency}",
            f"평가손익: {acc.profit:+,.2f} {acc.currency}",
        ]
    positions = mt5.positions_get() or ()
    lines.append(f"보유 포지션: {len(positions)}개")
    for p in positions[:10]:
        side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        lines.append(f"  • {p.symbol} {side} {p.volume} @ {p.price_open} ({p.profit:+.2f})")
    return "\n".join(lines)


def do_set(want: bool) -> str:
    if not mc.connect(launch=False):
        return "⚠️ MT5가 실행 중이 아닙니다. /launch 로 먼저 켜주세요."
    ok = mc.set_algo(want)
    word = "ON" if want else "OFF"
    if ok:
        return f"✅ 시스템트레이딩 {word} 완료\n\n" + status_report()
    return f"❌ 시스템트레이딩 {word} 실패\n현재: {mc.status_text()}"


def do_launch() -> str:
    if mc.connect(launch=False):
        return "이미 MT5가 실행 중입니다.\n\n" + status_report()
    if mc.start(False):
        return "✅ MT5 실행 완료\n\n" + status_report()
    return "❌ MT5 실행 실패 (MT5_PATH 설정을 확인하세요)"


ACTIONS = {
    "on": lambda: do_set(True),
    "off": lambda: do_set(False),
    "status": status_report,
    "launch": do_launch,
}


def handle(chat_id: int, cmd: str) -> None:
    if chat_id not in ALLOWED:
        send(chat_id, f"권한이 없습니다.\n당신의 chat_id: {chat_id}\n"
                      "이 번호를 bot_config.ini 의 chat_id 에 넣고 봇을 재시작하세요.", menu=False)
        print(f"[차단] 등록되지 않은 chat_id={chat_id} 명령='{cmd}'")
        return
    cmd = cmd.strip().lstrip("/").split("@")[0].split()[0].lower() if cmd.strip() else ""
    action = ACTIONS.get(cmd)
    if action is None:
        send(chat_id, "MT5 원격 컨트롤\n/on /off /status /launch\n또는 아래 버튼을 누르세요.")
        return
    if cmd in ("on", "off", "launch"):
        send(chat_id, "⏳ 처리 중...", menu=False)
    print(f"[명령] {chat_id}: {cmd}")
    try:
        send(chat_id, action())
        sync_state()  # 내가 바꾼 것은 '상태 변경' 알림을 중복으로 보내지 않음
    except Exception:
        traceback.print_exc()
        send(chat_id, "❌ 처리 중 오류가 발생했습니다. PC 콘솔을 확인하세요.")


# ── 상태 감시 (누가 PC에서 직접 바꾸거나 MT5가 꺼지면 알림) ─────────────
_last_state: object = "init"


def sync_state() -> None:
    global _last_state
    _last_state = mc.is_algo_on() if mc.connect(launch=False) else None


def watch() -> None:
    global _last_state
    state = mc.is_algo_on() if mc.connect(launch=False) else None
    if _last_state != "init" and state != _last_state:
        if state is None:
            broadcast("⚠️ MT5 연결이 끊겼습니다 (MT5가 꺼졌을 수 있음).")
        else:
            broadcast(f"ℹ️ 시스템트레이딩 상태 변경: {'🟢 ON' if state else '🔴 OFF'}")
    _last_state = state


# ── 메인 루프 ───────────────────────────────────────────────────────────
def main() -> None:
    if os.name != "nt":
        sys.exit("이 봇은 MT5가 설치된 Windows PC에서 실행해야 합니다.")

    me = tg("getMe")
    if not me.get("ok"):
        sys.exit(f"봇 토큰이 올바르지 않습니다: {me}")
    print(f"봇 시작: @{me['result']['username']}")
    if not ALLOWED:
        print("[안내] chat_id 가 비어 있습니다. 텔레그램에서 봇에게 아무 말이나 보내면 chat_id 를 알려줍니다.")

    tg("setMyCommands", commands=[
        {"command": "menu", "description": "버튼 메뉴"},
        {"command": "on", "description": "시스템트레이딩 켜기"},
        {"command": "off", "description": "시스템트레이딩 끄기"},
        {"command": "status", "description": "상태/잔액/포지션"},
        {"command": "launch", "description": "MT5 켜기"},
    ])

    # 봇이 꺼져 있던 동안 쌓인 옛 명령은 무시 (재시작 시 엉뚱하게 ON/OFF 되는 것 방지)
    offset = None
    old = tg("getUpdates", offset=-1, timeout=0).get("result", [])
    if old:
        offset = old[-1]["update_id"] + 1

    watch()
    broadcast("🤖 MT5 원격 컨트롤 봇이 시작되었습니다.\n\n" + status_report())
    next_watch = time.time() + WATCH_INTERVAL

    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                     allowed_updates=["message", "callback_query"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    tg("answerCallbackQuery", callback_query_id=cq["id"])
                    handle(cq["message"]["chat"]["id"], cq.get("data", ""))
                elif "message" in upd and "text" in upd["message"]:
                    msg = upd["message"]
                    handle(msg["chat"]["id"], msg["text"])
            if not res:
                time.sleep(5)  # 네트워크 오류 시 잠깐 대기

            if time.time() >= next_watch:
                watch()
                next_watch = time.time() + WATCH_INTERVAL
        except KeyboardInterrupt:
            print("봇 종료")
            break
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
