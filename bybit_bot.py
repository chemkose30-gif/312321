#!/usr/bin/env python3
"""
Bybit USDT Perpetual 자동 매매 봇
===================================
전략 : SuperTrend(14, 2.5) + RSI(13) 모멘텀 필터
기본  : ETHUSDT  4H  레버리지 3x

설치:
    pip install pybit pandas numpy python-dotenv requests schedule

설정:
    .env 파일 생성 후 아래 두 줄 입력
        API_KEY=여기에_키
        API_SECRET=여기에_시크릿
    TESTNET = True  로 먼저 검증 후 False 로 전환

실행:
    python bybit_bot.py
"""

import os, time, logging, math
from datetime import datetime, timezone

import numpy  as np
import pandas as pd
import schedule
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

# ═══════════════════════════════════════════════════
#  설정  ─ 여기서 수정
# ═══════════════════════════════════════════════════
SYMBOL       = "ETHUSDT"
INTERVAL     = "240"         # 4시간봉 (분 단위)
LEVERAGE     = 3             # 레버리지 배수
EQUITY_PCT   = 0.10          # 진입에 사용할 자본 비율 (10%)

STOP_PCT     = 0.025         # 손절 2.5%
TP1_R        = 1.0           # TP1  : 리스크의 1배
TP2_R        = 2.0           # TP2  : 리스크의 2배
TP3_R        = 3.0           # TP3  : 리스크의 3배
TP1_PCT      = 0.34          # TP1 에서 청산 비율 34%
TP2_PCT      = 0.50          # TP2 에서 잔여의 50%

ST_LEN       = 14
ST_MULT      = 2.5
RSI_LEN      = 13
RSI_LONG     = 55            # RSI > 55 일 때만 롱
RSI_SHORT    = 45            # RSI < 45 일 때만 숏

TESTNET      = True          # ★ 실전 전환 시 False 로 변경
LOG_FILE     = "bybit_bot.log"

# 텔레그램 알림 (선택 — 없으면 빈 문자열로)
TG_TOKEN     = ""
TG_CHAT_ID   = ""

# ═══════════════════════════════════════════════════
#  로깅
# ═══════════════════════════════════════════════════
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)s  %(message)s",
    handlers= [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  텔레그램 알림
# ═══════════════════════════════════════════════════
def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": f"[봇] {msg}"},
            timeout=5,
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════
#  Bybit 클라이언트
# ═══════════════════════════════════════════════════
load_dotenv()
client = HTTP(
    testnet    = TESTNET,
    api_key    = os.getenv("API_KEY"),
    api_secret = os.getenv("API_SECRET"),
)

def setup_leverage():
    try:
        client.set_leverage(
            category     = "linear",
            symbol       = SYMBOL,
            buyLeverage  = str(LEVERAGE),
            sellLeverage = str(LEVERAGE),
        )
        log.info(f"레버리지 {LEVERAGE}x 설정 완료")
    except Exception as e:
        if "leverage not modified" in str(e).lower():
            pass
        else:
            log.warning(f"레버리지 설정 실패: {e}")

# ═══════════════════════════════════════════════════
#  잔고 조회
# ═══════════════════════════════════════════════════
def get_equity() -> float:
    res = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    bal = res["result"]["list"][0]["totalEquity"]
    return float(bal)

# ═══════════════════════════════════════════════════
#  현재 포지션
# ═══════════════════════════════════════════════════
def get_position() -> dict:
    """{'side': 'Buy'|'Sell'|None, 'size': float, 'entry': float}"""
    res  = client.get_positions(category="linear", symbol=SYMBOL)
    data = res["result"]["list"]
    for p in data:
        size = float(p["size"])
        if size > 0:
            return {"side": p["side"], "size": size, "entry": float(p["avgPrice"])}
    return {"side": None, "size": 0.0, "entry": 0.0}

# ═══════════════════════════════════════════════════
#  OHLCV 데이터
# ═══════════════════════════════════════════════════
def fetch_ohlcv(limit=200) -> pd.DataFrame:
    res  = client.get_kline(
        category = "linear",
        symbol   = SYMBOL,
        interval = INTERVAL,
        limit    = limit,
    )
    rows = res["result"]["list"]
    df   = pd.DataFrame(rows, columns=[
        "ts","open","high","low","close","vol","turnover"
    ])
    df["ts"]    = pd.to_datetime(df["ts"].astype(int), unit="ms")
    for col in ("open","high","low","close","vol"):
        df[col] = df[col].astype(float)
    df = df.set_index("ts").sort_index()
    return df.iloc[:-1]   # 마지막 미완성 봉 제외

# ═══════════════════════════════════════════════════
#  지표
# ═══════════════════════════════════════════════════
def calc_supertrend(df: pd.DataFrame, n=14, mult=2.5):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr    = tr.ewm(alpha=1/n, adjust=False).mean()
    hl2    = (df["high"] + df["low"]) / 2
    ub_raw = (hl2 + mult * atr).values
    lb_raw = (hl2 - mult * atr).values
    close  = df["close"].values
    nb     = len(df)

    ub  = np.full(nb, np.nan)
    lb  = np.full(nb, np.nan)
    dir_= np.ones(nb, dtype=np.int8)

    for i in range(1, nb):
        ub[i]  = ub_raw[i] if (np.isnan(ub[i-1]) or ub_raw[i] < ub[i-1] or close[i-1] > ub[i-1]) else ub[i-1]
        lb[i]  = lb_raw[i] if (np.isnan(lb[i-1]) or lb_raw[i] > lb[i-1] or close[i-1] < lb[i-1]) else lb[i-1]
        if   dir_[i-1] == -1 and close[i] > ub[i]:  dir_[i] =  1
        elif dir_[i-1] ==  1 and close[i] < lb[i]:  dir_[i] = -1
        else:                                         dir_[i] = dir_[i-1]

    return pd.Series(dir_, index=df.index)

def calc_rsi(series: pd.Series, n=13) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

def get_signal(df: pd.DataFrame) -> int:
    """
    +1 = 롱 진입  /  -1 = 숏 진입  /  0 = 없음
    ST가 방향 전환 + RSI 확인 → 신호 발생
    """
    st  = calc_supertrend(df, ST_LEN, ST_MULT)
    rsi = calc_rsi(df["close"], RSI_LEN)

    prev_st  = st.iloc[-2];  curr_st  = st.iloc[-1]
    curr_rsi = rsi.iloc[-1]

    if prev_st == -1 and curr_st == 1 and curr_rsi > RSI_LONG:
        return 1
    if prev_st == 1 and curr_st == -1 and curr_rsi < RSI_SHORT:
        return -1
    return 0

# ═══════════════════════════════════════════════════
#  주문 실행
# ═══════════════════════════════════════════════════
def round_qty(qty: float, step: float) -> float:
    """거래소 최소 수량 단위로 내림"""
    return math.floor(qty / step) * step

def get_min_qty_step() -> float:
    """심볼의 최소 주문 수량 단위"""
    res  = client.get_instruments_info(category="linear", symbol=SYMBOL)
    info = res["result"]["list"][0]["lotSizeFilter"]
    return float(info["qtyStep"])

def place_entry(side: str, price: float):
    """진입 주문 + SL/TP 설정"""
    equity   = get_equity()
    qty_step = get_min_qty_step()
    notional = equity * EQUITY_PCT * LEVERAGE
    qty      = round_qty(notional / price, qty_step)

    if qty <= 0:
        log.error("수량 계산 오류 — 잔고 부족 가능성")
        return

    risk = price * STOP_PCT

    if side == "Buy":
        sl  = round(price - risk,              2)
        tp1 = round(price + risk * TP1_R,      2)
        tp2 = round(price + risk * TP2_R,      2)
        tp3 = round(price + risk * TP3_R,      2)
    else:
        sl  = round(price + risk,              2)
        tp1 = round(price - risk * TP1_R,      2)
        tp2 = round(price - risk * TP2_R,      2)
        tp3 = round(price - risk * TP3_R,      2)

    log.info(f"진입 → {side}  qty={qty}  price≈{price}  SL={sl}  TP1={tp1} TP2={tp2} TP3={tp3}")

    # 마켓 진입
    try:
        client.place_order(
            category     = "linear",
            symbol       = SYMBOL,
            side         = side,
            orderType    = "Market",
            qty          = str(qty),
            stopLoss     = str(sl),
            slTriggerBy  = "LastPrice",
            reduceOnly   = False,
        )
    except Exception as e:
        log.error(f"진입 주문 실패: {e}")
        return

    time.sleep(0.5)   # 주문 체결 대기

    # TP1 — 34%
    tp1_qty = round_qty(qty * TP1_PCT, qty_step)
    # TP2 — 잔여의 50%
    tp2_qty = round_qty((qty - tp1_qty) * TP2_PCT, qty_step)
    # TP3 — 나머지 전량
    tp3_qty = round_qty(qty - tp1_qty - tp2_qty, qty_step)

    for tp_price, tp_qty, label in [
        (tp1, tp1_qty, "TP1"),
        (tp2, tp2_qty, "TP2"),
        (tp3, tp3_qty, "TP3"),
    ]:
        if tp_qty <= 0:
            continue
        try:
            client.place_order(
                category   = "linear",
                symbol     = SYMBOL,
                side       = "Sell" if side == "Buy" else "Buy",
                orderType  = "Limit",
                qty        = str(tp_qty),
                price      = str(tp_price),
                reduceOnly = True,
                timeInForce= "GTC",
            )
            log.info(f"  {label} 지정가 주문: {tp_price}  qty={tp_qty}")
        except Exception as e:
            log.warning(f"  {label} 주문 실패: {e}")

    msg = f"{'롱' if side=='Buy' else '숏'} 진입  {SYMBOL}  {price}  SL={sl}  TP1={tp1}"
    log.info(msg);  tg(msg)

def close_position(pos: dict):
    """포지션 전량 시장가 청산"""
    close_side = "Sell" if pos["side"] == "Buy" else "Buy"
    try:
        client.place_order(
            category   = "linear",
            symbol     = SYMBOL,
            side       = close_side,
            orderType  = "Market",
            qty        = str(pos["size"]),
            reduceOnly = True,
        )
        msg = f"청산  {pos['side']}  {pos['size']} {SYMBOL}"
        log.info(msg);  tg(msg)
    except Exception as e:
        log.error(f"청산 실패: {e}")

def cancel_open_orders():
    try:
        client.cancel_all_orders(category="linear", symbol=SYMBOL)
    except Exception:
        pass

# ═══════════════════════════════════════════════════
#  메인 루틴 (봉 마감마다 실행)
# ═══════════════════════════════════════════════════
def run():
    log.info("─" * 50)
    log.info(f"체크  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    try:
        df  = fetch_ohlcv(200)
        sig = get_signal(df)
        pos = get_position()
        price = float(df["close"].iloc[-1])

        st_dir  = calc_supertrend(df, ST_LEN, ST_MULT).iloc[-1]
        rsi_val = calc_rsi(df["close"], RSI_LEN).iloc[-1]
        log.info(f"  price={price:.2f}  ST={'▲' if st_dir==1 else '▼'}  RSI={rsi_val:.1f}  sig={sig}  pos={pos['side']}")

        # ── 청산 조건: 반대 방향 ST 전환 ──────────────────
        if pos["side"] == "Buy"  and st_dir == -1:
            log.info("  ST 하락 전환 → 롱 청산");  cancel_open_orders();  close_position(pos)
            return
        if pos["side"] == "Sell" and st_dir ==  1:
            log.info("  ST 상승 전환 → 숏 청산");  cancel_open_orders();  close_position(pos)
            return

        # ── 진입 조건 ─────────────────────────────────────
        if sig != 0 and pos["side"] is None:
            cancel_open_orders()
            place_entry("Buy" if sig == 1 else "Sell", price)

        # ── 반대 신호: 청산 후 반전 진입 ─────────────────
        elif sig == 1 and pos["side"] == "Sell":
            cancel_open_orders();  close_position(pos);  time.sleep(1)
            place_entry("Buy", price)
        elif sig == -1 and pos["side"] == "Buy":
            cancel_open_orders();  close_position(pos);  time.sleep(1)
            place_entry("Sell", price)

    except Exception as e:
        log.exception(f"오류 발생: {e}")
        tg(f"⚠️ 봇 오류: {e}")

# ═══════════════════════════════════════════════════
#  스케줄러 (4H 봉 마감 후 1분에 실행)
# ═══════════════════════════════════════════════════
def main():
    net = "테스트넷" if TESTNET else "★ 실전"
    log.info("=" * 50)
    log.info(f"  Bybit 자동 매매 봇 시작  [{net}]")
    log.info(f"  {SYMBOL}  {INTERVAL}분봉  레버리지 {LEVERAGE}x  자본비율 {EQUITY_PCT*100:.0f}%")
    log.info("=" * 50)
    tg(f"봇 시작 [{net}]  {SYMBOL} {INTERVAL}분봉 {LEVERAGE}x")

    setup_leverage()

    # 4H 봉 마감: 00:01 / 04:01 / 08:01 / 12:01 / 16:01 / 20:01 UTC
    for h in ("00:01","04:01","08:01","12:01","16:01","20:01"):
        schedule.every().day.at(h).do(run)

    log.info("스케줄 등록 완료. 대기 중...")
    run()   # 시작 즉시 1회 실행

    while True:
        schedule.run_pending()
        time.sleep(10)

if __name__ == "__main__":
    main()
