"""
TradingView → Bybit 웹훅 서버
TradingView 알림을 받아 Bybit에 자동으로 주문하는 서버

설치:
  pip install flask pybit

실행:
  python webhook_server.py             # 실거래
  python webhook_server.py --demo      # 데모 계좌

TradingView 알림 설정:
  1. 전략 → 알림 추가
  2. Webhook URL: http://YOUR_SERVER_IP:5000/webhook
  3. 메시지: (Pine Script의 alert() 내용이 자동으로 전송됨)
"""

import argparse
import json
import logging
import math
from datetime import datetime
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

# ─────────────────────────────────────────────
#  설정
# ─────────────────────────────────────────────
API_KEY    = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"
PORT       = 5000
WEBHOOK_TOKEN = "YOUR_SECRET_TOKEN"   # 보안용 토큰 (TradingView URL에 포함)
LEVERAGE   = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)
session: HTTP = None


# ─────────────────────────────────────────────
#  Bybit 헬퍼
# ─────────────────────────────────────────────
def get_position(symbol: str) -> dict:
    r = session.get_positions(category="linear", symbol=symbol)
    for p in r["result"]["list"]:
        if float(p["size"]) > 0:
            return {
                "side":      p["side"],
                "size":      float(p["size"]),
                "avg_price": float(p["avgPrice"]),
            }
    return {"side": None, "size": 0.0, "avg_price": 0.0}


def round_qty(qty: float, symbol: str) -> float:
    """심볼별 최소 수량 단위로 반올림"""
    try:
        info = session.get_instruments_info(category="linear", symbol=symbol)
        step = float(info["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        return round(math.floor(qty / step) * step, decimals)
    except Exception:
        return round(qty, 3)


def place_order(symbol: str, side: str, qty: float) -> bool:
    qty = round_qty(qty, symbol)
    if qty <= 0:
        log.warning(f"수량이 0 이하 → 주문 취소  qty={qty}")
        return False
    try:
        r = session.place_order(
            category  = "linear",
            symbol    = symbol,
            side      = side,
            orderType = "Market",
            qty       = str(qty),
        )
        order_id = r["result"]["orderId"]
        log.info(f"주문 완료  {side} {symbol} qty={qty}  orderID={order_id}")
        return True
    except Exception as e:
        log.error(f"주문 오류: {e}")
        return False


def close_position(symbol: str) -> bool:
    pos = get_position(symbol)
    if pos["size"] == 0:
        log.info("청산할 포지션 없음")
        return True
    close_side = "Sell" if pos["side"] == "Buy" else "Buy"
    return place_order(symbol, close_side, pos["size"])


def set_leverage(symbol: str):
    try:
        session.set_leverage(
            category     = "linear",
            symbol       = symbol,
            buyLeverage  = str(LEVERAGE),
            sellLeverage = str(LEVERAGE),
        )
        log.info(f"레버리지 {LEVERAGE}x 설정 완료 ({symbol})")
    except Exception as e:
        log.warning(f"레버리지 설정 오류 (이미 설정된 경우 무시): {e}")


# ─────────────────────────────────────────────
#  웹훅 엔드포인트
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    # 토큰 검증
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN != "YOUR_SECRET_TOKEN" and token != WEBHOOK_TOKEN:
        log.warning(f"인증 실패  IP={request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    # JSON 파싱
    try:
        data = request.get_json(force=True)
    except Exception:
        raw = request.data.decode("utf-8")
        log.error(f"JSON 파싱 실패: {raw}")
        return jsonify({"error": "invalid json"}), 400

    action = data.get("action", "")
    symbol = data.get("symbol", "ETHUSDT")
    side   = data.get("side", "")
    layer  = data.get("layer", 1)

    log.info(f"알림 수신  action={action}  symbol={symbol}  side={side}"
             f"  layer={layer}  price={data.get('price')}")

    # ── 매수/물타기 ──
    if action == "buy":
        qty_str = data.get("qty", "0")
        try:
            qty = float(qty_str)
        except ValueError:
            return jsonify({"error": "invalid qty"}), 400

        set_leverage(symbol)
        success = place_order(symbol, side, qty)
        status = "ok" if success else "error"
        return jsonify({"status": status, "action": action,
                        "symbol": symbol, "side": side, "qty": qty, "layer": layer})

    # ── 전량 청산 ──
    elif action == "close":
        success = close_position(symbol)
        status = "ok" if success else "error"
        return jsonify({"status": status, "action": "close", "symbol": symbol})

    else:
        log.warning(f"알 수 없는 action: {action}")
        return jsonify({"error": f"unknown action: {action}"}), 400


@app.route("/status", methods=["GET"])
def status():
    """현재 포지션 상태 확인용"""
    symbol = request.args.get("symbol", "ETHUSDT")
    try:
        pos = get_position(symbol)
        return jsonify({"symbol": symbol, "position": pos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ─────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────
def main():
    global session

    parser = argparse.ArgumentParser(description="TradingView → Bybit 웹훅 서버")
    parser.add_argument("--demo", action="store_true", help="데모 계좌 사용")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    session = HTTP(
        testnet    = args.demo,
        api_key    = API_KEY,
        api_secret = API_SECRET,
    )

    mode = "데모" if args.demo else "실거래 ⚠️"
    log.info(f"서버 시작  모드={mode}  포트={args.port}")
    log.info(f"웹훅 URL: http://YOUR_IP:{args.port}/webhook?token={WEBHOOK_TOKEN}")
    log.info(f"상태 확인: http://YOUR_IP:{args.port}/status?symbol=ETHUSDT")

    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
