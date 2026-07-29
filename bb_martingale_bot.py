"""
Road To Billion - BB 마틴게일 Bybit 봇
⚠️ 경고: SL 없는 마틴게일 전략 - 강한 추세에서 계좌 전액 손실 위험
반드시 데모 계좌로 충분히 테스트할 것

Usage:
  python bb_martingale_bot.py --symbol ETHUSDT --demo    # 데모 계좌 (권장)
  python bb_martingale_bot.py --symbol ETHUSDT           # 실거래 (위험)
"""

import time
import argparse
import math
from datetime import datetime
from pybit.unified_trading import HTTP

# ─────────────────────────────────────────────
#  설정
# ─────────────────────────────────────────────
API_KEY    = "YOUR_API_KEY"
API_SECRET = "YOUR_API_SECRET"

# 전략 파라미터
BB_PERIOD   = 20
BB_STD      = 2.0
BASE_PCT    = 2.0    # 초기 진입 자본 %
MART_MULT   = 2.0    # 마틴게일 배수
MAX_LAYERS  = 5      # 최대 물타기 횟수
GRID_PCT    = 1.5    # 물타기 간격 %
TP_PCT      = 1.0    # TP % (평단가 기준)
LEVERAGE    = 3      # 레버리지
INTERVAL    = "30"   # 분봉 (30 = 30분)
LOOP_SEC    = 60     # 루프 간격 (초)


# ─────────────────────────────────────────────
#  유틸
# ─────────────────────────────────────────────
def now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def calc_bb(closes: list, period: int, std_mult: float):
    if len(closes) < period:
        return None, None, None
    data = closes[-period:]
    mid  = sum(data) / period
    var  = sum((x - mid) ** 2 for x in data) / period
    std  = math.sqrt(var)
    return mid, mid + std_mult * std, mid - std_mult * std


# ─────────────────────────────────────────────
#  Bybit 클라이언트
# ─────────────────────────────────────────────
class MartingaleBot:
    def __init__(self, symbol: str, demo: bool = True):
        self.symbol  = symbol
        self.session = HTTP(
            testnet    = demo,
            api_key    = API_KEY,
            api_secret = API_SECRET,
        )
        self.demo = demo

        # 상태
        self.direction     = None   # "Long" | "Short" | None
        self.layers        = 0
        self.last_add_price = None
        self.base_qty      = None   # 최초 진입 시 계산된 기본 수량
        self.avg_entry     = None

        print(f"[{now()}] 봇 시작 - {symbol}  {'[데모]' if demo else '[실거래⚠️]'}")
        self._set_leverage()

    def _set_leverage(self):
        try:
            self.session.set_leverage(
                category    = "linear",
                symbol      = self.symbol,
                buyLeverage = str(LEVERAGE),
                sellLeverage= str(LEVERAGE),
            )
            print(f"[{now()}] 레버리지 {LEVERAGE}x 설정 완료")
        except Exception as e:
            print(f"[{now()}] 레버리지 설정 오류 (이미 설정된 경우 무시): {e}")

    def get_equity(self) -> float:
        r = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(r["result"]["list"][0]["totalEquity"])

    def get_position(self) -> dict:
        r = self.session.get_positions(category="linear", symbol=self.symbol)
        positions = r["result"]["list"]
        for p in positions:
            if float(p["size"]) > 0:
                return {
                    "side": p["side"],           # "Buy" | "Sell"
                    "size": float(p["size"]),
                    "avg_price": float(p["avgPrice"]),
                }
        return {"side": None, "size": 0.0, "avg_price": 0.0}

    def get_klines(self, limit: int = 60) -> list:
        r = self.session.get_kline(
            category = "linear",
            symbol   = self.symbol,
            interval = INTERVAL,
            limit    = limit,
        )
        rows = r["result"]["list"]
        # rows: [timestamp, open, high, low, close, volume, turnover]
        # 최신 봉이 앞에 있으므로 역순 정렬
        rows.sort(key=lambda x: int(x[0]))
        return [float(row[4]) for row in rows]  # close만

    def get_price(self) -> float:
        r = self.session.get_tickers(category="linear", symbol=self.symbol)
        return float(r["result"]["list"][0]["lastPrice"])

    def place_order(self, side: str, qty: float, comment: str = "") -> bool:
        try:
            r = self.session.place_order(
                category   = "linear",
                symbol     = self.symbol,
                side       = side,   # "Buy" | "Sell"
                orderType  = "Market",
                qty        = str(round(qty, 3)),
                reduceOnly = False,
            )
            order_id = r["result"]["orderId"]
            print(f"[{now()}] {comment} 주문 완료  side={side}  qty={qty:.3f}  orderID={order_id}")
            return True
        except Exception as e:
            print(f"[{now()}] 주문 오류: {e}")
            return False

    def close_all(self, comment: str = "TP"):
        pos = self.get_position()
        if pos["size"] == 0:
            return
        close_side = "Sell" if pos["side"] == "Buy" else "Buy"
        self.place_order(close_side, pos["size"], f"{comment} 전량청산")
        self.direction      = None
        self.layers         = 0
        self.last_add_price = None
        self.base_qty       = None
        self.avg_entry      = None
        print(f"[{now()}] 포지션 청산 완료 ({comment})")

    def calc_base_qty(self, price: float) -> float:
        equity    = self.get_equity()
        notional  = equity * (BASE_PCT / 100.0) * LEVERAGE
        qty       = notional / price
        return round(qty, 3)

    # ─── 메인 루프 ───
    def run(self):
        print(f"[{now()}] 루프 시작 (간격 {LOOP_SEC}초)\n")

        while True:
            try:
                closes = self.get_klines(BB_PERIOD + 5)
                if len(closes) < BB_PERIOD:
                    time.sleep(LOOP_SEC)
                    continue

                price = self.get_price()
                bb_mid, bb_upper, bb_lower = calc_bb(closes, BB_PERIOD, BB_STD)
                pos   = self.get_position()

                # 실제 포지션과 상태 동기화
                if pos["side"] is None:
                    if self.direction is not None:
                        # 외부에서 청산된 경우
                        print(f"[{now()}] 포지션 없음 감지 → 상태 초기화")
                        self.direction = None
                        self.layers    = 0
                        self.last_add_price = None
                        self.base_qty  = None
                        self.avg_entry = None

                # ─── TP 체크 ───
                if pos["size"] > 0:
                    avg_px = pos["avg_price"]
                    tp_long  = pos["side"] == "Buy"  and price >= avg_px * (1 + TP_PCT / 100)
                    tp_short = pos["side"] == "Sell" and price <= avg_px * (1 - TP_PCT / 100)

                    if tp_long or tp_short:
                        pnl_pct = abs(price - avg_px) / avg_px * 100
                        print(f"[{now()}] TP 도달!  avg={avg_px:.2f}  현재={price:.2f}  수익={pnl_pct:.2f}%")
                        self.close_all("TP")
                        time.sleep(2)
                        continue

                # ─── 물타기 체크 ───
                if pos["side"] == "Buy" and self.layers < MAX_LAYERS:
                    if self.last_add_price and price <= self.last_add_price * (1 - GRID_PCT / 100):
                        add_qty = round(self.base_qty * (MART_MULT ** self.layers), 3)
                        print(f"[{now()}] 롱 물타기 {self.layers+1}단  qty={add_qty}  price={price:.2f}")
                        if self.place_order("Buy", add_qty, f"물타기{self.layers+1}"):
                            self.layers        += 1
                            self.last_add_price = price
                        time.sleep(2)
                        continue

                elif pos["side"] == "Sell" and self.layers < MAX_LAYERS:
                    if self.last_add_price and price >= self.last_add_price * (1 + GRID_PCT / 100):
                        add_qty = round(self.base_qty * (MART_MULT ** self.layers), 3)
                        print(f"[{now()}] 숏 물타기 {self.layers+1}단  qty={add_qty}  price={price:.2f}")
                        if self.place_order("Sell", add_qty, f"물타기{self.layers+1}"):
                            self.layers        += 1
                            self.last_add_price = price
                        time.sleep(2)
                        continue

                # ─── 신규 진입 체크 ───
                if pos["size"] == 0:
                    if price <= bb_lower:
                        qty = self.calc_base_qty(price)
                        print(f"[{now()}] 롱 진입  price={price:.2f}  BB하단={bb_lower:.2f}  qty={qty}")
                        if self.place_order("Buy", qty, "롱1"):
                            self.direction      = "Long"
                            self.layers         = 1
                            self.last_add_price = price
                            self.base_qty       = qty
                            self.avg_entry      = price

                    elif price >= bb_upper:
                        qty = self.calc_base_qty(price)
                        print(f"[{now()}] 숏 진입  price={price:.2f}  BB상단={bb_upper:.2f}  qty={qty}")
                        if self.place_order("Sell", qty, "숏1"):
                            self.direction      = "Short"
                            self.layers         = 1
                            self.last_add_price = price
                            self.base_qty       = qty
                            self.avg_entry      = price

                    else:
                        print(f"[{now()}] 대기  price={price:.2f}  BB[{bb_lower:.2f} ~ {bb_upper:.2f}]"
                              f"  레이어={self.layers}")

            except KeyboardInterrupt:
                print(f"\n[{now()}] 봇 중지 요청")
                ans = input("현재 포지션 청산하시겠습니까? (y/n): ")
                if ans.lower() == "y":
                    self.close_all("수동중지")
                break
            except Exception as e:
                print(f"[{now()}] 오류: {e}")

            time.sleep(LOOP_SEC)


# ─────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="BB 마틴게일 봇")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--demo",   action="store_true", help="데모 계좌 사용 (권장)")
    args = parser.parse_args()

    if not args.demo:
        print("=" * 50)
        print("⚠️  실거래 모드입니다!")
        print("⚠️  SL이 없는 마틴게일 전략은 계좌 전액 손실 위험이 있습니다.")
        print("=" * 50)
        confirm = input("계속하려면 'REAL' 입력: ")
        if confirm != "REAL":
            print("취소됨")
            return

    bot = MartingaleBot(symbol=args.symbol, demo=args.demo)
    bot.run()


if __name__ == "__main__":
    main()
