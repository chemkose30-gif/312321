"""
BB + Stochastic 전략 백테스트
- Bollinger Bands(20, 2.0) + Stochastic(14, 3, 3)
- 바이낸스 OHLCV 데이터 사용 (로컬 실행용)
- 사용법: python bb_stoch_backtest.py --symbol ETHUSDT --tf 4h

필요 패키지: pip install requests numpy pandas tabulate
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
TF_LIMIT = {"1m": 3, "3m": 5, "5m": 7, "15m": 9, "30m": 10,
            "1h": 9, "2h": 8, "4h": 6, "6h": 5, "8h": 5,
            "12h": 4, "1d": 3}

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
    df = df.astype({"open": float, "high": float, "low": float,
                    "close": float, "volume": float})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("ts", inplace=True)
    df = df[["open","high","low","close","volume"]].copy()
    return df


# ──────────────────────────────────────────────────────────
#  지표 계산
# ──────────────────────────────────────────────────────────
def calc_bb(src: np.ndarray, period: int, std_mult: float):
    mid = pd.Series(src).rolling(period).mean().values
    std = pd.Series(src).rolling(period).std(ddof=0).values
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return mid, upper, lower


def calc_stoch(high: np.ndarray, low: np.ndarray, close: np.ndarray,
               k_len: int = 14, sm_k: int = 3, sm_d: int = 3):
    n = len(close)
    raw_k = np.full(n, np.nan)
    for i in range(k_len - 1, n):
        lo = low[i - k_len + 1: i + 1].min()
        hi = high[i - k_len + 1: i + 1].max()
        rng = hi - lo
        raw_k[i] = (close[i] - lo) / rng * 100 if rng > 0 else 50.0

    k_line = pd.Series(raw_k).rolling(sm_k).mean().values
    d_line = pd.Series(k_line).rolling(sm_d).mean().values
    return k_line, d_line


def calc_ema(src: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(src).ewm(span=period, adjust=False).mean().values


def calc_vol_ma(vol: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(vol).rolling(period).mean().values


# ──────────────────────────────────────────────────────────
#  백테스트 엔진
# ──────────────────────────────────────────────────────────
def backtest(
    df: pd.DataFrame,
    bb_period: int   = 20,
    bb_std:    float = 2.0,
    stoch_k:   int   = 14,
    stoch_smk: int   = 3,
    stoch_smd: int   = 3,
    os_level:  int   = 20,
    ob_level:  int   = 80,
    sl_pct:    float = 1.5,
    tp1_pct:   float = 1.0,
    tp2_pct:   float = 2.0,
    tp1_qty:   float = 0.5,
    use_mid_exit: bool = True,
    use_ema_filter:  bool = True,
    ema_len:         int  = 200,
    use_vol_filter:  bool = True,
    vol_ma_len:      int  = 20,
    vol_mult:        float = 1.0,
    use_bb_width:    bool = True,
    bb_width_max:    float = 0.10,
    leverage:        int   = 3,
    equity_pct:      float = 0.10,   # 10%
    commission_pct:  float = 0.04,   # 0.04%
) -> dict:

    c   = df["close"].values
    h   = df["high"].values
    lo  = df["low"].values
    vol = df["volume"].values
    n   = len(c)

    mid, upper, lower = calc_bb(c, bb_period, bb_std)
    k_line, d_line    = calc_stoch(h, lo, c, stoch_k, stoch_smk, stoch_smd)
    ema200            = calc_ema(c, ema_len)
    vol_ma            = calc_vol_ma(vol, vol_ma_len)

    # 거래 기록
    trades = []
    equity = 10000.0
    position = 0   # 0=없음, 1=롱, -1=숏
    entry_price = 0.0
    entry_equity = 0.0
    entry_qty1 = 0.0
    entry_qty2 = 0.0
    entry_bar  = 0
    partial_closed = False

    equity_curve = [equity]

    def commission(qty, price):
        return qty * price * (commission_pct / 100)

    for i in range(1, n):
        # 필터
        bb_width_val = (upper[i] - lower[i]) / mid[i] if mid[i] > 0 else 999.0

        ema_long_ok  = (not use_ema_filter) or (c[i] < ema200[i])
        ema_short_ok = (not use_ema_filter) or (c[i] > ema200[i])
        vol_ok       = (not use_vol_filter) or (vol[i] >= vol_ma[i] * vol_mult)
        bb_ok        = (not use_bb_width)   or (bb_width_val <= bb_width_max)

        k_prev = k_line[i-1]
        d_prev = d_line[i-1]
        k_cur  = k_line[i]
        d_cur  = d_line[i]

        long_cross  = (not np.isnan(k_prev)) and (not np.isnan(d_prev)) and k_prev <= d_prev and k_cur > d_cur
        short_cross = (not np.isnan(k_prev)) and (not np.isnan(d_prev)) and k_prev >= d_prev and k_cur < d_cur

        long_entry_sig  = (c[i] <= lower[i]) and (k_cur < os_level) and long_cross  and ema_long_ok  and vol_ok and bb_ok
        short_entry_sig = (c[i] >= upper[i]) and (k_cur > ob_level) and short_cross and ema_short_ok and vol_ok and bb_ok

        # ── 포지션 관리 ──
        if position != 0:
            price = c[i]

            # 롱 포지션
            if position == 1:
                sl_price  = entry_price * (1 - sl_pct / 100)
                tp1_price = entry_price * (1 + tp1_pct / 100)
                tp2_price = entry_price * (1 + tp2_pct / 100)

                # 손절
                if lo[i] <= sl_price:
                    exit_price = sl_price
                    pnl = (exit_price - entry_price) / entry_price * leverage
                    fee = commission(entry_qty1 + entry_qty2 if not partial_closed else entry_qty2, exit_price)
                    net = (entry_qty1 + entry_qty2 if not partial_closed else entry_qty2) * entry_price * pnl - fee
                    equity += net
                    trades.append({"type":"L","entry":entry_price,"exit":exit_price,
                                   "pnl_pct":pnl*100,"result":"SL","bars":i-entry_bar})
                    position = 0
                    partial_closed = False

                elif use_mid_exit:
                    # BB 중앙선 익절
                    if price >= mid[i]:
                        exit_price = price
                        pnl = (exit_price - entry_price) / entry_price * leverage
                        total_qty = entry_qty1 + entry_qty2
                        fee = commission(total_qty, exit_price)
                        net = total_qty * entry_price * pnl - fee
                        equity += net
                        trades.append({"type":"L","entry":entry_price,"exit":exit_price,
                                       "pnl_pct":pnl*100,"result":"MID","bars":i-entry_bar})
                        position = 0
                        partial_closed = False

                else:
                    # 분할 익절
                    if not partial_closed and h[i] >= tp1_price:
                        exit_price = tp1_price
                        pnl = (exit_price - entry_price) / entry_price * leverage
                        fee = commission(entry_qty1, exit_price)
                        net = entry_qty1 * entry_price * pnl - fee
                        equity += net
                        partial_closed = True

                    if partial_closed and h[i] >= tp2_price:
                        exit_price = tp2_price
                        pnl = (exit_price - entry_price) / entry_price * leverage
                        fee = commission(entry_qty2, exit_price)
                        net = entry_qty2 * entry_price * pnl - fee
                        equity += net
                        trades.append({"type":"L","entry":entry_price,"exit":exit_price,
                                       "pnl_pct":pnl*100,"result":"TP2","bars":i-entry_bar})
                        position = 0
                        partial_closed = False

            # 숏 포지션
            elif position == -1:
                sl_price  = entry_price * (1 + sl_pct / 100)
                tp1_price = entry_price * (1 - tp1_pct / 100)
                tp2_price = entry_price * (1 - tp2_pct / 100)

                if h[i] >= sl_price:
                    exit_price = sl_price
                    pnl = (entry_price - exit_price) / entry_price * leverage
                    fee = commission(entry_qty1 + entry_qty2 if not partial_closed else entry_qty2, exit_price)
                    net = (entry_qty1 + entry_qty2 if not partial_closed else entry_qty2) * entry_price * pnl - fee
                    equity += net
                    trades.append({"type":"S","entry":entry_price,"exit":exit_price,
                                   "pnl_pct":pnl*100,"result":"SL","bars":i-entry_bar})
                    position = 0
                    partial_closed = False

                elif use_mid_exit:
                    if price <= mid[i]:
                        exit_price = price
                        pnl = (entry_price - exit_price) / entry_price * leverage
                        total_qty = entry_qty1 + entry_qty2
                        fee = commission(total_qty, exit_price)
                        net = total_qty * entry_price * pnl - fee
                        equity += net
                        trades.append({"type":"S","entry":entry_price,"exit":exit_price,
                                       "pnl_pct":pnl*100,"result":"MID","bars":i-entry_bar})
                        position = 0
                        partial_closed = False

                else:
                    if not partial_closed and lo[i] <= tp1_price:
                        exit_price = tp1_price
                        pnl = (entry_price - exit_price) / entry_price * leverage
                        fee = commission(entry_qty1, exit_price)
                        net = entry_qty1 * entry_price * pnl - fee
                        equity += net
                        partial_closed = True

                    if partial_closed and lo[i] <= tp2_price:
                        exit_price = tp2_price
                        pnl = (entry_price - exit_price) / entry_price * leverage
                        fee = commission(entry_qty2, exit_price)
                        net = entry_qty2 * entry_price * pnl - fee
                        equity += net
                        trades.append({"type":"S","entry":entry_price,"exit":exit_price,
                                       "pnl_pct":pnl*100,"result":"TP2","bars":i-entry_bar})
                        position = 0
                        partial_closed = False

        # ── 신규 진입 ──
        if position == 0:
            if long_entry_sig:
                entry_price = c[i]
                trade_val   = equity * equity_pct * leverage
                entry_qty1  = trade_val * tp1_qty  / entry_price
                entry_qty2  = trade_val * (1 - tp1_qty) / entry_price
                fee = commission(entry_qty1 + entry_qty2, entry_price)
                equity -= fee
                entry_equity = equity
                entry_bar    = i
                partial_closed = False
                position = 1

            elif short_entry_sig:
                entry_price = c[i]
                trade_val   = equity * equity_pct * leverage
                entry_qty1  = trade_val * tp1_qty  / entry_price
                entry_qty2  = trade_val * (1 - tp1_qty) / entry_price
                fee = commission(entry_qty1 + entry_qty2, entry_price)
                equity -= fee
                entry_equity = equity
                entry_bar    = i
                partial_closed = False
                position = -1

        equity_curve.append(equity)

    # 미청산 포지션 강제 청산
    if position != 0:
        exit_price = c[-1]
        if position == 1:
            pnl = (exit_price - entry_price) / entry_price * leverage
        else:
            pnl = (entry_price - exit_price) / entry_price * leverage
        total_qty = entry_qty1 + entry_qty2 if not partial_closed else entry_qty2
        fee = commission(total_qty, exit_price)
        net = total_qty * entry_price * pnl - fee
        equity += net
        trades.append({"type":"L" if position==1 else "S",
                        "entry":entry_price,"exit":exit_price,
                        "pnl_pct":pnl*100,"result":"OPEN","bars":n-1-entry_bar})

    equity_curve[-1] = equity

    # ── 성과 지표 ──
    ec = np.array(equity_curve)
    total_return = (ec[-1] / ec[0] - 1) * 100
    n_trades = len(trades)
    wins  = [t for t in trades if t["pnl_pct"] > 0]
    losses= [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / n_trades * 100 if n_trades else 0.0
    avg_win  = np.mean([t["pnl_pct"] for t in wins])  if wins   else 0.0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0.0
    profit_factor = abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses)) \
                    if losses and sum(t["pnl_pct"] for t in losses) != 0 else float("inf")

    peak = np.maximum.accumulate(ec)
    dd   = (ec - peak) / peak * 100
    mdd  = dd.min()

    avg_bars = np.mean([t["bars"] for t in trades]) if trades else 0.0

    return {
        "total_return": total_return,
        "n_trades":     n_trades,
        "win_rate":     win_rate,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
        "profit_factor":profit_factor,
        "mdd":          mdd,
        "final_equity": ec[-1],
        "avg_bars":     avg_bars,
        "trades":       trades,
        "equity_curve": ec.tolist(),
    }


# ──────────────────────────────────────────────────────────
#  메인
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="BB+Stochastic 백테스트")
    parser.add_argument("--symbol",    default="ETHUSDT",  help="코인 심볼")
    parser.add_argument("--tf",        default="4h",       help="타임프레임 (예: 5m 15m 1h 4h)")
    parser.add_argument("--pages",     type=int, default=6,help="데이터 페이지 수 (×1000봉)")
    parser.add_argument("--bb_period", type=int, default=20)
    parser.add_argument("--bb_std",    type=float, default=2.0)
    parser.add_argument("--stoch_k",   type=int, default=14)
    parser.add_argument("--stoch_smk", type=int, default=3)
    parser.add_argument("--stoch_smd", type=int, default=3)
    parser.add_argument("--os",        type=int, default=20, help="과매도 기준")
    parser.add_argument("--ob",        type=int, default=80, help="과매수 기준")
    parser.add_argument("--sl",        type=float, default=1.5, help="손절 %%")
    parser.add_argument("--tp1",       type=float, default=1.0, help="1차 TP %%")
    parser.add_argument("--tp2",       type=float, default=2.0, help="2차 TP %%")
    parser.add_argument("--mid_exit",  action="store_true", default=True, help="BB 중앙선 익절")
    parser.add_argument("--no_mid_exit", action="store_true", help="분할 익절 모드 사용")
    parser.add_argument("--no_ema",    action="store_true",  help="EMA 필터 비활성화")
    parser.add_argument("--no_vol",    action="store_true",  help="거래량 필터 비활성화")
    parser.add_argument("--no_bb_w",   action="store_true",  help="BB 폭 필터 비활성화")
    parser.add_argument("--leverage",  type=int,   default=3,    help="레버리지")
    parser.add_argument("--equity",    type=float, default=0.10, help="자본 비율 (0.10 = 10%%)")
    parser.add_argument("--verbose",   action="store_true", help="개별 거래 출력")
    args = parser.parse_args()

    use_mid = args.mid_exit and not args.no_mid_exit

    print(f"\n{'='*60}")
    print(f"  BB + Stochastic 백테스트")
    print(f"  심볼: {args.symbol}  타임프레임: {args.tf}")
    print(f"  BB({args.bb_period},{args.bb_std})  Stoch({args.stoch_k},{args.stoch_smk},{args.stoch_smd})")
    print(f"  필터: EMA={'끄기' if args.no_ema else '켜기'}  거래량={'끄기' if args.no_vol else '켜기'}  BB폭={'끄기' if args.no_bb_w else '켜기'}")
    print(f"  SL={args.sl}%  TP1={args.tp1}%  TP2={args.tp2}%  {'중앙선 익절' if use_mid else '분할 익절'}")
    print(f"  레버리지={args.leverage}x  자본비율={args.equity*100:.0f}%")
    print(f"{'='*60}")

    print(f"\n데이터 다운로드 중... ({args.pages}×1000봉)")
    try:
        df = fetch_ohlcv(args.symbol, args.tf, pages=args.pages)
    except Exception as e:
        print(f"[오류] 데이터 다운로드 실패: {e}")
        sys.exit(1)

    print(f"데이터 기간: {df.index[0]} ~ {df.index[-1]}  ({len(df)} 봉)")

    result = backtest(
        df,
        bb_period    = args.bb_period,
        bb_std       = args.bb_std,
        stoch_k      = args.stoch_k,
        stoch_smk    = args.stoch_smk,
        stoch_smd    = args.stoch_smd,
        os_level     = args.os,
        ob_level     = args.ob,
        sl_pct       = args.sl,
        tp1_pct      = args.tp1,
        tp2_pct      = args.tp2,
        tp1_qty      = 0.5,
        use_mid_exit = use_mid,
        use_ema_filter  = not args.no_ema,
        ema_len         = 200,
        use_vol_filter  = not args.no_vol,
        vol_ma_len      = 20,
        vol_mult        = 1.0,
        use_bb_width    = not args.no_bb_w,
        bb_width_max    = 0.10,
        leverage        = args.leverage,
        equity_pct      = args.equity,
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

    # 결과별 분류
    results_by_type = {}
    for tr in t["trades"]:
        r = tr["result"]
        results_by_type[r] = results_by_type.get(r, 0) + 1
    if results_by_type:
        print(f"\n  청산 유형별: " + "  ".join(f"{k}:{v}" for k,v in sorted(results_by_type.items())))

    print()


if __name__ == "__main__":
    main()
