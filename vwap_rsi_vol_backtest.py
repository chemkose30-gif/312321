"""
VWAP + RSI + 거래량 전략 백테스트
- VWAP: 동적 지지/저항 기준선
- RSI: 모멘텀 방향 확인
- 거래량: 신호 강도 확인
- 사용법: python vwap_rsi_vol_backtest.py --symbol ETHUSDT --tf 1h

필요 패키지: pip install requests numpy pandas
"""
import argparse
import time
import sys
from typing import Optional

import numpy as np
import pandas as pd
import requests


# ──────────────────────────────────────────────────────────
#  Binance OHLCV 가져오기
# ──────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str, tf: str, pages: int = 6) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    end_time: Optional[int] = None

    for _ in range(pages):
        params = {"symbol": symbol, "interval": tf, "limit": 1000}
        if end_time:
            params["endTime"] = end_time - 1

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_candles = data + all_candles
        end_time = int(data[0][0])
        time.sleep(0.12)

    if not all_candles:
        raise RuntimeError(f"{symbol} {tf} 데이터 없음")

    df = pd.DataFrame(all_candles,
                      columns=["ts","open","high","low","close","volume",
                                "close_ts","qav","trades","tbav","tqav","_"])
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("ts", inplace=True)
    return df[["open","high","low","close","volume"]].copy()


# ──────────────────────────────────────────────────────────
#  지표 계산
# ──────────────────────────────────────────────────────────
def calc_vwap_session(df: pd.DataFrame):
    """
    일별 세션 VWAP 계산 (앵커: 매일 자정 UTC 리셋)
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    date_key = df.index.floor("D")

    vwap_arr = np.full(len(df), np.nan)
    upper_arr = np.full(len(df), np.nan)
    lower_arr = np.full(len(df), np.nan)

    cum_pv  = 0.0
    cum_vol = 0.0
    cum_pv2 = 0.0
    prev_date = None

    for i, (ts, row) in enumerate(df.iterrows()):
        cur_date = ts.floor("D")
        if cur_date != prev_date:
            cum_pv  = 0.0
            cum_vol = 0.0
            cum_pv2 = 0.0
            prev_date = cur_date

        tp = typical.iloc[i]
        v  = row["volume"]
        cum_pv  += tp * v
        cum_vol += v
        cum_pv2 += tp * tp * v

        if cum_vol > 0:
            vwap_v = cum_pv / cum_vol
            var_v  = cum_pv2 / cum_vol - vwap_v**2
            std_v  = np.sqrt(max(var_v, 0.0))
            vwap_arr[i]  = vwap_v
            upper_arr[i] = vwap_v + std_v
            lower_arr[i] = vwap_v - std_v

    return vwap_arr, upper_arr, lower_arr


def calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = pd.Series(gain).ewm(com=period-1, adjust=False).mean().values
    avg_l = pd.Series(loss).ewm(com=period-1, adjust=False).mean().values
    rs    = np.where(avg_l == 0, 100.0, avg_g / avg_l)
    return 100 - (100 / (1 + rs))


def calc_vol_ma(vol: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(vol).rolling(period).mean().values


# ──────────────────────────────────────────────────────────
#  백테스트 엔진
# ──────────────────────────────────────────────────────────
def backtest(
    df: pd.DataFrame,
    rsi_len:       int   = 14,
    rsi_long_min:  int   = 50,
    rsi_short_max: int   = 50,
    rsi_os:        int   = 40,
    rsi_ob:        int   = 60,
    vol_ma_len:    int   = 20,
    vol_mult:      float = 1.2,
    sl_pct:        float = 2.0,
    tp1_pct:       float = 1.5,
    tp2_pct:       float = 3.0,
    tp1_qty:       float = 0.5,
    use_vwap_exit: bool  = True,
    leverage:      int   = 3,
    equity_pct:    float = 0.10,
    commission_pct:float = 0.04,
) -> dict:

    c   = df["close"].values
    h   = df["high"].values
    lo  = df["low"].values
    vol = df["volume"].values
    n   = len(c)

    vwap, _, _ = calc_vwap_session(df)
    rsi_arr    = calc_rsi(c, rsi_len)
    vol_ma_arr = calc_vol_ma(vol, vol_ma_len)

    trades = []
    equity  = 10000.0
    position = 0
    entry_price  = 0.0
    entry_qty1   = 0.0
    entry_qty2   = 0.0
    entry_bar    = 0
    partial_done = False

    equity_curve = [equity]

    def fee(qty, price):
        return qty * price * (commission_pct / 100)

    for i in range(1, n):
        if np.isnan(vwap[i]) or np.isnan(rsi_arr[i]) or np.isnan(vol_ma_arr[i]):
            equity_curve.append(equity)
            continue

        vol_ok = vol[i] >= vol_ma_arr[i] * vol_mult

        # 크로스 신호
        prev_above = c[i-1] > vwap[i-1] if not np.isnan(vwap[i-1]) else False
        cur_above  = c[i]   > vwap[i]

        vwap_cross_up   = (not prev_above) and cur_above
        vwap_cross_down = prev_above and (not cur_above)

        rsi_cross_up   = rsi_arr[i-1] < rsi_os  and rsi_arr[i] >= rsi_os and cur_above
        rsi_cross_down = rsi_arr[i-1] > rsi_ob  and rsi_arr[i] <= rsi_ob and not cur_above

        long_signal  = (vwap_cross_up or rsi_cross_up) and rsi_arr[i] >= rsi_long_min  and vol_ok
        short_signal = (vwap_cross_down or rsi_cross_down) and rsi_arr[i] <= rsi_short_max and vol_ok

        # ── 포지션 관리 ──
        if position == 1:
            sl  = entry_price * (1 - sl_pct  / 100)
            tp1 = entry_price * (1 + tp1_pct / 100)
            tp2 = entry_price * (1 + tp2_pct / 100)

            if lo[i] <= sl:
                rem = entry_qty2 if partial_done else (entry_qty1 + entry_qty2)
                pnl = (sl - entry_price) / entry_price * leverage
                net = rem * entry_price * pnl - fee(rem, sl)
                equity += net
                trades.append({"type":"L","entry":entry_price,"exit":sl,
                                "pnl_pct":pnl*100,"result":"SL","bars":i-entry_bar})
                position = 0; partial_done = False

            elif use_vwap_exit and c[i] < vwap[i] and not partial_done:
                pnl = (c[i] - entry_price) / entry_price * leverage
                net = (entry_qty1 + entry_qty2) * entry_price * pnl - fee(entry_qty1+entry_qty2, c[i])
                equity += net
                trades.append({"type":"L","entry":entry_price,"exit":c[i],
                                "pnl_pct":pnl*100,"result":"VWAP","bars":i-entry_bar})
                position = 0; partial_done = False

            else:
                if not partial_done and h[i] >= tp1:
                    pnl = (tp1 - entry_price) / entry_price * leverage
                    net = entry_qty1 * entry_price * pnl - fee(entry_qty1, tp1)
                    equity += net
                    partial_done = True
                if partial_done and h[i] >= tp2:
                    pnl = (tp2 - entry_price) / entry_price * leverage
                    net = entry_qty2 * entry_price * pnl - fee(entry_qty2, tp2)
                    equity += net
                    trades.append({"type":"L","entry":entry_price,"exit":tp2,
                                   "pnl_pct":pnl*100,"result":"TP2","bars":i-entry_bar})
                    position = 0; partial_done = False

        elif position == -1:
            sl  = entry_price * (1 + sl_pct  / 100)
            tp1 = entry_price * (1 - tp1_pct / 100)
            tp2 = entry_price * (1 - tp2_pct / 100)

            if h[i] >= sl:
                rem = entry_qty2 if partial_done else (entry_qty1 + entry_qty2)
                pnl = (entry_price - sl) / entry_price * leverage
                net = rem * entry_price * pnl - fee(rem, sl)
                equity += net
                trades.append({"type":"S","entry":entry_price,"exit":sl,
                                "pnl_pct":pnl*100,"result":"SL","bars":i-entry_bar})
                position = 0; partial_done = False

            elif use_vwap_exit and c[i] > vwap[i] and not partial_done:
                pnl = (entry_price - c[i]) / entry_price * leverage
                net = (entry_qty1 + entry_qty2) * entry_price * pnl - fee(entry_qty1+entry_qty2, c[i])
                equity += net
                trades.append({"type":"S","entry":entry_price,"exit":c[i],
                                "pnl_pct":pnl*100,"result":"VWAP","bars":i-entry_bar})
                position = 0; partial_done = False

            else:
                if not partial_done and lo[i] <= tp1:
                    pnl = (entry_price - tp1) / entry_price * leverage
                    net = entry_qty1 * entry_price * pnl - fee(entry_qty1, tp1)
                    equity += net
                    partial_done = True
                if partial_done and lo[i] <= tp2:
                    pnl = (entry_price - tp2) / entry_price * leverage
                    net = entry_qty2 * entry_price * pnl - fee(entry_qty2, tp2)
                    equity += net
                    trades.append({"type":"S","entry":entry_price,"exit":tp2,
                                   "pnl_pct":pnl*100,"result":"TP2","bars":i-entry_bar})
                    position = 0; partial_done = False

        # ── 신규 진입 ──
        if position == 0:
            if long_signal:
                entry_price = c[i]
                tv = equity * equity_pct * leverage
                entry_qty1 = tv * tp1_qty / entry_price
                entry_qty2 = tv * (1 - tp1_qty) / entry_price
                equity -= fee(entry_qty1 + entry_qty2, entry_price)
                entry_bar = i; partial_done = False; position = 1

            elif short_signal:
                entry_price = c[i]
                tv = equity * equity_pct * leverage
                entry_qty1 = tv * tp1_qty / entry_price
                entry_qty2 = tv * (1 - tp1_qty) / entry_price
                equity -= fee(entry_qty1 + entry_qty2, entry_price)
                entry_bar = i; partial_done = False; position = -1

        equity_curve.append(equity)

    # 미청산 강제 청산
    if position != 0:
        ep = c[-1]
        pnl = ((ep - entry_price) if position == 1 else (entry_price - ep)) / entry_price * leverage
        rem = entry_qty2 if partial_done else (entry_qty1 + entry_qty2)
        net = rem * entry_price * pnl - fee(rem, ep)
        equity += net
        trades.append({"type":"L" if position==1 else "S",
                        "entry":entry_price,"exit":ep,
                        "pnl_pct":pnl*100,"result":"OPEN","bars":n-1-entry_bar})
    equity_curve[-1] = equity

    ec = np.array(equity_curve)
    total_return = (ec[-1] / ec[0] - 1) * 100
    n_t   = len(trades)
    wins  = [t for t in trades if t["pnl_pct"] > 0]
    losses= [t for t in trades if t["pnl_pct"] <= 0]
    wr    = len(wins) / n_t * 100 if n_t else 0.0
    aw    = np.mean([t["pnl_pct"] for t in wins])   if wins   else 0.0
    al    = np.mean([t["pnl_pct"] for t in losses])  if losses else 0.0
    pf    = abs(sum(t["pnl_pct"] for t in wins) /
                sum(t["pnl_pct"] for t in losses)) \
            if losses and sum(t["pnl_pct"] for t in losses) != 0 else float("inf")
    peak  = np.maximum.accumulate(ec)
    mdd   = ((ec - peak) / peak * 100).min()
    avg_b = np.mean([t["bars"] for t in trades]) if trades else 0.0

    return {"total_return":total_return,"n_trades":n_t,"win_rate":wr,
            "avg_win":aw,"avg_loss":al,"profit_factor":pf,"mdd":mdd,
            "final_equity":ec[-1],"avg_bars":avg_b,"trades":trades,"equity_curve":ec.tolist()}


# ──────────────────────────────────────────────────────────
#  메인
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="VWAP+RSI+거래량 백테스트")
    parser.add_argument("--symbol",        default="ETHUSDT")
    parser.add_argument("--tf",            default="1h")
    parser.add_argument("--pages",         type=int,   default=6)
    parser.add_argument("--rsi_len",       type=int,   default=14)
    parser.add_argument("--rsi_long_min",  type=int,   default=50)
    parser.add_argument("--rsi_short_max", type=int,   default=50)
    parser.add_argument("--rsi_os",        type=int,   default=40)
    parser.add_argument("--rsi_ob",        type=int,   default=60)
    parser.add_argument("--vol_mult",      type=float, default=1.2)
    parser.add_argument("--sl",            type=float, default=2.0)
    parser.add_argument("--tp1",           type=float, default=1.5)
    parser.add_argument("--tp2",           type=float, default=3.0)
    parser.add_argument("--no_vwap_exit",  action="store_true")
    parser.add_argument("--leverage",      type=int,   default=3)
    parser.add_argument("--equity",        type=float, default=0.10)
    parser.add_argument("--verbose",       action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  VWAP + RSI + 거래량 백테스트")
    print(f"  심볼: {args.symbol}  타임프레임: {args.tf}")
    print(f"  RSI({args.rsi_len})  롱기준>={args.rsi_long_min}  숏기준<={args.rsi_short_max}")
    print(f"  거래량 배수: {args.vol_mult}x")
    print(f"  SL={args.sl}%  TP1={args.tp1}%  TP2={args.tp2}%")
    print(f"  레버리지={args.leverage}x  자본비율={args.equity*100:.0f}%")
    print(f"{'='*60}")

    print(f"\n데이터 다운로드 중... ({args.pages}×1000봉)")
    try:
        df = fetch_ohlcv(args.symbol, args.tf, pages=args.pages)
    except Exception as e:
        print(f"[오류] {e}")
        sys.exit(1)

    print(f"데이터 기간: {df.index[0]} ~ {df.index[-1]}  ({len(df)} 봉)")

    result = backtest(
        df,
        rsi_len       = args.rsi_len,
        rsi_long_min  = args.rsi_long_min,
        rsi_short_max = args.rsi_short_max,
        rsi_os        = args.rsi_os,
        rsi_ob        = args.rsi_ob,
        vol_ma_len    = 20,
        vol_mult      = args.vol_mult,
        sl_pct        = args.sl,
        tp1_pct       = args.tp1,
        tp2_pct       = args.tp2,
        tp1_qty       = 0.5,
        use_vwap_exit = not args.no_vwap_exit,
        leverage      = args.leverage,
        equity_pct    = args.equity,
    )

    t = result
    print(f"\n{'─'*40}  결과  {'─'*40}")
    print(f"  총 수익률    : {t['total_return']:+.2f}%")
    print(f"  최종 자산    : ${t['final_equity']:,.2f}  (시작 $10,000)")
    print(f"  총 거래 수   : {t['n_trades']}건")
    print(f"  승률         : {t['win_rate']:.1f}%")
    print(f"  평균 수익    : {t['avg_win']:+.2f}%  |  평균 손실: {t['avg_loss']:+.2f}%")
    print(f"  손익비       : {t['profit_factor']:.2f}")
    print(f"  최대 낙폭    : {t['mdd']:.2f}%")
    print(f"  평균 보유봉  : {t['avg_bars']:.1f}")
    print(f"{'─'*87}")

    if args.verbose and t["trades"]:
        print(f"\n  최근 20건 거래:")
        print(f"  {'#':>4}  {'방향':4}  {'진입가':>10}  {'청산가':>10}  {'수익률':>8}  {'결과':5}  {'봉수':>5}")
        for idx, tr in enumerate(t["trades"][-20:], 1):
            print(f"  {idx:>4}  {'롱' if tr['type']=='L' else '숏':4}  "
                  f"{tr['entry']:>10.2f}  {tr['exit']:>10.2f}  "
                  f"{tr['pnl_pct']:>+8.2f}%  {tr['result']:5}  {tr['bars']:>5}")

    rc = {}
    for tr in t["trades"]:
        rc[tr["result"]] = rc.get(tr["result"], 0) + 1
    if rc:
        print(f"\n  청산 유형별: " + "  ".join(f"{k}:{v}" for k,v in sorted(rc.items())))
    print()


if __name__ == "__main__":
    main()
