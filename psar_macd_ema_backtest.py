"""
PSAR + MACD + EMA 전략 백테스트
Usage:
  python psar_macd_ema_backtest.py --symbol ETHUSDT --tf 1h
  python psar_macd_ema_backtest.py --symbol ETHUSDT --tf 1h --no-sar-exit
  python psar_macd_ema_backtest.py --symbol ETHUSDT --tf 1h --macd-mode cross
  python psar_macd_ema_backtest.py --symbol ETHUSDT --tf 4h --leverage 5

Binance에서 OHLCV 데이터를 직접 가져오므로 로컬에서 실행하세요.
"""

import argparse
import requests
import numpy as np
import pandas as pd
from datetime import datetime


# ─────────────────────────────────────────────
#  데이터 수집
# ─────────────────────────────────────────────
def fetch_ohlcv(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data, columns=[
        "ts","open","high","low","close","volume",
        "close_ts","quote_vol","trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    df["ts"]    = pd.to_datetime(df["ts"], unit="ms")
    df["open"]  = df["open"].astype(float)
    df["high"]  = df["high"].astype(float)
    df["low"]   = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"]= df["volume"].astype(float)
    return df[["ts","open","high","low","close","volume"]].reset_index(drop=True)


# ─────────────────────────────────────────────
#  지표 계산
# ─────────────────────────────────────────────
def calc_psar(high: np.ndarray, low: np.ndarray,
              start: float = 0.02, increment: float = 0.02,
              maximum: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Wilder's Parabolic SAR.
    Returns (sar, trend) where trend: 1=uptrend, -1=downtrend
    """
    n = len(high)
    sar   = np.empty(n)
    ep    = np.empty(n)
    af    = np.empty(n)
    trend = np.empty(n, dtype=int)

    # 초기화: 첫 2봉으로 추세 결정
    if high[1] > high[0]:
        trend[0] = 1
        sar[0]   = low[0]
        ep[0]    = high[0]
    else:
        trend[0] = -1
        sar[0]   = high[0]
        ep[0]    = low[0]
    af[0] = start

    for i in range(1, n):
        prev_t  = trend[i-1]
        prev_s  = sar[i-1]
        prev_ep = ep[i-1]
        prev_af = af[i-1]

        if prev_t == 1:  # 상승 추세
            new_sar = prev_s + prev_af * (prev_ep - prev_s)
            # SAR은 이전 2봉의 저가보다 높을 수 없음
            if i >= 2:
                new_sar = min(new_sar, low[i-1], low[i-2])
            else:
                new_sar = min(new_sar, low[i-1])

            if low[i] < new_sar:           # 반전 → 하락 추세
                trend[i] = -1
                sar[i]   = prev_ep
                ep[i]    = low[i]
                af[i]    = start
            else:
                trend[i] = 1
                sar[i]   = new_sar
                if high[i] > prev_ep:      # 신고가 → AF 증가
                    ep[i] = high[i]
                    af[i] = min(prev_af + increment, maximum)
                else:
                    ep[i] = prev_ep
                    af[i] = prev_af

        else:  # 하락 추세
            new_sar = prev_s + prev_af * (prev_ep - prev_s)
            # SAR은 이전 2봉의 고가보다 낮을 수 없음
            if i >= 2:
                new_sar = max(new_sar, high[i-1], high[i-2])
            else:
                new_sar = max(new_sar, high[i-1])

            if high[i] > new_sar:          # 반전 → 상승 추세
                trend[i] = 1
                sar[i]   = prev_ep
                ep[i]    = high[i]
                af[i]    = start
            else:
                trend[i] = -1
                sar[i]   = new_sar
                if low[i] < prev_ep:       # 신저가 → AF 증가
                    ep[i] = low[i]
                    af[i] = min(prev_af + increment, maximum)
                else:
                    ep[i] = prev_ep
                    af[i] = prev_af

    return sar, trend


def calc_macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    def ema(arr, period):
        result = np.empty(len(arr))
        result[:] = np.nan
        k = 2.0 / (period + 1)
        # 첫 값은 SMA
        result[period-1] = np.mean(arr[:period])
        for i in range(period, len(arr)):
            result[i] = arr[i] * k + result[i-1] * (1 - k)
        return result

    ema_fast   = ema(close, fast)
    ema_slow   = ema(close, slow)
    macd_line  = ema_fast - ema_slow
    valid_mask = ~np.isnan(macd_line)
    sig_line   = np.full(len(close), np.nan)
    if np.sum(valid_mask) >= signal:
        start_idx = np.where(valid_mask)[0][0]
        sig_arr   = ema(macd_line[valid_mask], signal)
        sig_full  = np.full(len(close), np.nan)
        sig_full[valid_mask] = sig_arr
        sig_line  = sig_full
    hist = macd_line - sig_line
    return macd_line, sig_line, hist


def calc_ema(close: np.ndarray, period: int) -> np.ndarray:
    result = np.empty(len(close))
    result[:] = np.nan
    k = 2.0 / (period + 1)
    result[period-1] = np.mean(close[:period])
    for i in range(period, len(close)):
        result[i] = close[i] * k + result[i-1] * (1 - k)
    return result


# ─────────────────────────────────────────────
#  백테스트
# ─────────────────────────────────────────────
def backtest(df: pd.DataFrame,
             # SAR
             sar_start: float = 0.02,
             sar_inc:   float = 0.02,
             sar_max:   float = 0.2,
             # MACD
             macd_fast: int   = 12,
             macd_slow: int   = 26,
             macd_sig:  int   = 9,
             macd_mode: str   = "hist",   # "cross" | "hist" | "zero"
             # EMA
             use_ema:   bool  = True,
             ema_len:   int   = 200,
             use_ema2:  bool  = False,
             ema2_len:  int   = 50,
             # 청산
             sl_pct:    float = 2.0,
             tp1_pct:   float = 2.0,
             tp2_pct:   float = 4.0,
             tp1_qty:   float = 0.5,
             use_sar_exit: bool = True,
             # 리스크
             leverage:  int   = 3,
             equity_pct: float = 10.0,
             commission: float = 0.04,
             initial_capital: float = 10000.0,
             verbose:   bool  = False) -> dict:

    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    n      = len(df)

    # 지표 계산
    sar_vals, sar_trend = calc_psar(high, low, sar_start, sar_inc, sar_max)
    macd_line, sig_line, hist = calc_macd(close, macd_fast, macd_slow, macd_sig)
    ema_main = calc_ema(close, ema_len) if use_ema else np.full(n, -np.inf)
    ema2     = calc_ema(close, ema2_len) if use_ema2 else np.full(n, -np.inf)

    # SAR 상태
    sar_bull      = sar_vals < close
    sar_flip_up   = np.zeros(n, dtype=bool)
    sar_flip_down = np.zeros(n, dtype=bool)
    for i in range(1, n):
        sar_flip_up[i]   = sar_bull[i] and not sar_bull[i-1]
        sar_flip_down[i] = not sar_bull[i] and sar_bull[i-1]

    # 시뮬레이션 상태
    equity   = initial_capital
    position = 0.0    # 보유 수량 (양수=롱, 음수=숏)
    entry_px = 0.0
    entry_qty_full = 0.0
    tp1_done = False  # TP1 청산 완료 여부

    trades   = []
    warmup   = max(ema_len, macd_slow + macd_sig, 30)

    for i in range(warmup, n):
        px = close[i]
        if np.isnan(macd_line[i]) or np.isnan(sig_line[i]):
            continue

        # ── MACD 조건 ──
        if macd_mode == "cross":
            macd_ok_long  = macd_line[i] > sig_line[i] and macd_line[i-1] <= sig_line[i-1]
            macd_ok_short = macd_line[i] < sig_line[i] and macd_line[i-1] >= sig_line[i-1]
        elif macd_mode == "hist":
            macd_ok_long  = hist[i] > 0
            macd_ok_short = hist[i] < 0
        else:  # "zero"
            macd_ok_long  = macd_line[i] > 0
            macd_ok_short = macd_line[i] < 0

        # ── EMA 필터 ──
        ema_ok_long  = (not use_ema)  or (close[i] > ema_main[i] and not np.isnan(ema_main[i]))
        ema_ok_short = (not use_ema)  or (close[i] < ema_main[i] and not np.isnan(ema_main[i]))
        ema2_ok_long  = (not use_ema2) or (close[i] > ema2[i] and not np.isnan(ema2[i]))
        ema2_ok_short = (not use_ema2) or (close[i] < ema2[i] and not np.isnan(ema2[i]))

        # ── 청산 로직 ──
        if position > 0:  # 롱 포지션 보유 중
            sl_px  = entry_px * (1 - sl_pct / 100)
            tp1_px = entry_px * (1 + tp1_pct / 100)
            tp2_px = entry_px * (1 + tp2_pct / 100)

            closed = False
            close_comment = ""
            close_px = px

            if use_sar_exit:
                if low[i] <= sl_px:          # SL 먼저 (SAR 모드에서도 SL 유지)
                    close_px = sl_px
                    closed   = True
                    close_comment = "SL"
                elif sar_flip_down[i]:       # SAR 반전 청산
                    close_px = px
                    closed   = True
                    close_comment = "SAR반전"
            else:
                if not tp1_done and high[i] >= tp1_px:
                    # TP1 부분 청산
                    pnl = (tp1_px - entry_px) / entry_px * leverage * equity_pct / 100 * tp1_qty * equity
                    equity += pnl - abs(pnl + entry_px * position * tp1_qty) * commission / 100
                    position *= (1 - tp1_qty)
                    tp1_done = True
                    if verbose:
                        print(f"  [{df['ts'].iloc[i]}] TP1 부분청산 PnL={pnl:.2f} equity={equity:.2f}")

                if low[i] <= sl_px:
                    close_px = sl_px
                    closed   = True
                    close_comment = "SL"
                elif high[i] >= tp2_px:
                    close_px = tp2_px
                    closed   = True
                    close_comment = "TP2"

            if closed:
                pnl = (close_px - entry_px) / entry_px * leverage * (position / entry_qty_full) * equity_pct / 100 * equity
                fee = abs(position * close_px) * commission / 100
                equity += pnl - fee
                trades.append({
                    "direction": "Long",
                    "entry": entry_px,
                    "exit": close_px,
                    "comment": close_comment,
                    "pnl": round(pnl - fee, 4),
                    "equity": round(equity, 2)
                })
                if verbose:
                    print(f"  [{df['ts'].iloc[i]}] Long 청산 @ {close_px:.2f} ({close_comment}) PnL={pnl-fee:.2f} 잔고={equity:.2f}")
                position = 0.0
                tp1_done = False

        elif position < 0:  # 숏 포지션 보유 중
            sl_px  = entry_px * (1 + sl_pct / 100)
            tp1_px = entry_px * (1 - tp1_pct / 100)
            tp2_px = entry_px * (1 - tp2_pct / 100)

            closed = False
            close_comment = ""
            close_px = px

            if use_sar_exit:
                if high[i] >= sl_px:
                    close_px = sl_px
                    closed   = True
                    close_comment = "SL"
                elif sar_flip_up[i]:
                    close_px = px
                    closed   = True
                    close_comment = "SAR반전"
            else:
                if not tp1_done and low[i] <= tp1_px:
                    pnl = (entry_px - tp1_px) / entry_px * leverage * equity_pct / 100 * tp1_qty * equity
                    equity += pnl - abs(pnl) * commission / 100
                    position *= (1 - tp1_qty)
                    tp1_done = True

                if high[i] >= sl_px:
                    close_px = sl_px
                    closed   = True
                    close_comment = "SL"
                elif low[i] <= tp2_px:
                    close_px = tp2_px
                    closed   = True
                    close_comment = "TP2"

            if closed:
                pnl = (entry_px - close_px) / entry_px * leverage * (abs(position) / entry_qty_full) * equity_pct / 100 * equity
                fee = abs(position * close_px) * commission / 100
                equity += pnl - fee
                trades.append({
                    "direction": "Short",
                    "entry": entry_px,
                    "exit": close_px,
                    "comment": close_comment,
                    "pnl": round(pnl - fee, 4),
                    "equity": round(equity, 2)
                })
                if verbose:
                    print(f"  [{df['ts'].iloc[i]}] Short 청산 @ {close_px:.2f} ({close_comment}) PnL={pnl-fee:.2f} 잔고={equity:.2f}")
                position = 0.0
                tp1_done = False

        # ── 진입 ──
        if position == 0:
            long_entry  = (sar_flip_up[i]   and macd_ok_long  and ema_ok_long  and ema2_ok_long)
            short_entry = (sar_flip_down[i]  and macd_ok_short and ema_ok_short and ema2_ok_short)

            if long_entry:
                entry_px = px
                position = equity * (equity_pct / 100) * leverage / px
                entry_qty_full = position
                tp1_done = False
                if verbose:
                    print(f"[{df['ts'].iloc[i]}] Long  진입 @ {px:.2f}  qty={position:.4f}  잔고={equity:.2f}")

            elif short_entry:
                entry_px = px
                position = -(equity * (equity_pct / 100) * leverage / px)
                entry_qty_full = abs(position)
                tp1_done = False
                if verbose:
                    print(f"[{df['ts'].iloc[i]}] Short 진입 @ {px:.2f}  qty={abs(position):.4f}  잔고={equity:.2f}")

    # ── 결과 집계 ──
    if not trades:
        return {"total_trades": 0, "win_rate": 0, "profit_factor": 0,
                "net_pnl": 0, "final_equity": initial_capital}

    tdf   = pd.DataFrame(trades)
    wins  = tdf[tdf["pnl"] > 0]
    loses = tdf[tdf["pnl"] <= 0]
    win_r = len(wins) / len(tdf) * 100
    gross_profit = wins["pnl"].sum()
    gross_loss   = abs(loses["pnl"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    net_pnl = tdf["pnl"].sum()
    max_eq  = initial_capital
    max_dd  = 0.0
    cur_eq  = initial_capital
    for p in tdf["pnl"]:
        cur_eq += p
        if cur_eq > max_eq:
            max_eq = cur_eq
        dd = (max_eq - cur_eq) / max_eq * 100
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades":  len(tdf),
        "long_trades":   len(tdf[tdf["direction"]=="Long"]),
        "short_trades":  len(tdf[tdf["direction"]=="Short"]),
        "win_rate":      round(win_r, 2),
        "profit_factor": round(pf, 3),
        "net_pnl":       round(net_pnl, 2),
        "gross_profit":  round(gross_profit, 2),
        "gross_loss":    round(gross_loss, 2),
        "max_drawdown":  round(max_dd, 2),
        "final_equity":  round(equity, 2),
        "trades":        tdf
    }


# ─────────────────────────────────────────────
#  메인
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PSAR+MACD+EMA 백테스트")
    parser.add_argument("--symbol",    default="ETHUSDT")
    parser.add_argument("--tf",        default="1h",
                        choices=["15m","30m","1h","2h","4h","1d"])
    parser.add_argument("--limit",     type=int, default=1000)
    # SAR
    parser.add_argument("--sar-start", type=float, default=0.02)
    parser.add_argument("--sar-inc",   type=float, default=0.02)
    parser.add_argument("--sar-max",   type=float, default=0.2)
    # MACD
    parser.add_argument("--macd-fast", type=int,   default=12)
    parser.add_argument("--macd-slow", type=int,   default=26)
    parser.add_argument("--macd-sig",  type=int,   default=9)
    parser.add_argument("--macd-mode", default="hist",
                        choices=["cross","hist","zero"],
                        help="cross=신호선돌파 hist=히스토그램 zero=MACD선")
    # EMA
    parser.add_argument("--no-ema",    action="store_true")
    parser.add_argument("--ema-len",   type=int, default=200)
    # 청산
    parser.add_argument("--sl",        type=float, default=2.0)
    parser.add_argument("--tp1",       type=float, default=2.0)
    parser.add_argument("--tp2",       type=float, default=4.0)
    parser.add_argument("--no-sar-exit", action="store_true",
                        help="SAR 추적 청산 비활성화 (고정 TP/SL 사용)")
    # 리스크
    parser.add_argument("--leverage",  type=int,   default=3)
    parser.add_argument("--equity-pct",type=float, default=10.0)
    parser.add_argument("--verbose",   action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"  PSAR+MACD+EMA 백테스트: {args.symbol} {args.tf}")
    print(f"{'='*55}")

    print(f"Binance에서 데이터 수집 중... ({args.limit}봉)")
    try:
        df = fetch_ohlcv(args.symbol, args.tf, args.limit)
    except Exception as e:
        print(f"데이터 수집 실패: {e}")
        print("로컬 환경에서 실행하거나 인터넷 연결을 확인하세요.")
        return

    start_dt = df["ts"].iloc[0].strftime("%Y-%m-%d")
    end_dt   = df["ts"].iloc[-1].strftime("%Y-%m-%d")
    print(f"데이터: {start_dt} ~ {end_dt}  ({len(df)}봉)")

    res = backtest(
        df,
        sar_start    = args.sar_start,
        sar_inc      = args.sar_inc,
        sar_max      = args.sar_max,
        macd_fast    = args.macd_fast,
        macd_slow    = args.macd_slow,
        macd_sig     = args.macd_sig,
        macd_mode    = args.macd_mode,
        use_ema      = not args.no_ema,
        ema_len      = args.ema_len,
        sl_pct       = args.sl,
        tp1_pct      = args.tp1,
        tp2_pct      = args.tp2,
        use_sar_exit = not args.no_sar_exit,
        leverage     = args.leverage,
        equity_pct   = args.equity_pct,
        verbose      = args.verbose,
    )

    print(f"\n{'─'*40}")
    print(f"  파라미터")
    print(f"{'─'*40}")
    print(f"  SAR          : start={args.sar_start} / inc={args.sar_inc} / max={args.sar_max}")
    print(f"  MACD         : {args.macd_fast}/{args.macd_slow}/{args.macd_sig}  mode={args.macd_mode}")
    print(f"  EMA          : {'비활성화' if args.no_ema else str(args.ema_len)}")
    print(f"  SAR 추적청산 : {'OFF (고정TP/SL)' if args.no_sar_exit else 'ON'}")
    print(f"  레버리지/자본 : {args.leverage}x / {args.equity_pct}%")
    print(f"  SL/TP1/TP2   : {args.sl}% / {args.tp1}% / {args.tp2}%")

    print(f"\n{'─'*40}")
    print(f"  결과")
    print(f"{'─'*40}")

    if res["total_trades"] == 0:
        print("  거래 없음 (파라미터 조정 필요)")
        return

    print(f"  총 거래수     : {res['total_trades']}  (롱 {res['long_trades']} / 숏 {res['short_trades']})")
    print(f"  승률          : {res['win_rate']}%")
    print(f"  수익 지수(PF) : {res['profit_factor']}")
    print(f"  순수익        : ${res['net_pnl']:+.2f}")
    print(f"  총수익 / 총손실: ${res['gross_profit']:.2f} / ${res['gross_loss']:.2f}")
    print(f"  최대 낙폭     : {res['max_drawdown']}%")
    print(f"  최종 잔고     : ${res['final_equity']:.2f}  (초기 $10,000)")

    if args.verbose and "trades" in res:
        tdf = res["trades"]
        print(f"\n{'─'*40}")
        print("  청산 사유별 통계")
        print(f"{'─'*40}")
        summary = tdf.groupby("comment")["pnl"].agg(["count","sum","mean"])
        summary.columns = ["횟수","합계PnL","평균PnL"]
        print(summary.to_string())


if __name__ == "__main__":
    main()
