"""
추세전환 전략 백테스트
- CCI + 일목균형표 + 파라볼릭 SAR + DMI + 스토캐스틱
- 진입: 지표별 점수 합산 >= 임계값
- 청산: 반대방향 점수 합산 >= 임계값 (다음 추세전환)

사용법:
  python trend_reversal_backtest.py --symbol ETHUSDT --tf 4h
  python trend_reversal_backtest.py --symbol ETHUSDT --tf 4h --verbose
"""
import argparse
import time
import sys
from typing import Optional

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────
#  데이터
# ──────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str, tf: str, pages: int = 6) -> pd.DataFrame:
    import requests
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
def calc_cci(h, lo, c, period=14):
    typical = (h + lo + c) / 3
    sma = pd.Series(typical).rolling(period).mean().values
    mad = pd.Series(typical).rolling(period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True).values
    return np.where(mad > 0, (typical - sma) / (0.015 * mad), 0.0)


def calc_ichimoku(h, lo, conv=9, base=26, span=52, disp=26):
    n = len(h)
    def donchian_mid(p):
        hi = pd.Series(h).rolling(p).max().values
        ll = pd.Series(lo).rolling(p).min().values
        return (hi + ll) / 2

    conv_line = donchian_mid(conv)
    base_line = donchian_mid(base)
    span_a    = (conv_line + base_line) / 2
    span_b    = donchian_mid(span)

    # 현재봉에서 disp 전의 선행스팬 (구름)
    cloud_a = np.full(n, np.nan)
    cloud_b = np.full(n, np.nan)
    for i in range(disp, n):
        cloud_a[i] = span_a[i - disp]
        cloud_b[i] = span_b[i - disp]

    return conv_line, base_line, cloud_a, cloud_b


def calc_sar(h, lo, start=0.02, inc=0.02, max_af=0.2):
    n = len(h)
    sar = np.full(n, np.nan)
    is_bull = True
    af = start
    ep = lo[0]
    sar[0] = h[0]

    for i in range(1, n):
        if is_bull:
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = min(sar[i], lo[i-1], lo[i-2] if i>1 else lo[i-1])
            if lo[i] < sar[i]:
                is_bull = False
                sar[i] = ep
                ep = lo[i]
                af = start
            else:
                if h[i] > ep:
                    ep = h[i]
                    af = min(af + inc, max_af)
        else:
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = max(sar[i], h[i-1], h[i-2] if i>1 else h[i-1])
            if h[i] > sar[i]:
                is_bull = True
                sar[i] = ep
                ep = h[i]
                af = start
            else:
                if lo[i] < ep:
                    ep = lo[i]
                    af = min(af + inc, max_af)
    return sar


def calc_dmi(h, lo, c, period=14):
    n = len(h)
    tr  = np.zeros(n)
    dmp = np.zeros(n)
    dmm = np.zeros(n)
    for i in range(1, n):
        hl = h[i] - lo[i]
        hc = abs(h[i] - c[i-1])
        lc = abs(lo[i] - c[i-1])
        tr[i] = max(hl, hc, lc)
        up = h[i] - h[i-1]
        dn = lo[i-1] - lo[i]
        dmp[i] = up if up > dn and up > 0 else 0.0
        dmm[i] = dn if dn > up and dn > 0 else 0.0

    def rma(arr, p):
        out = np.zeros(n)
        out[p] = np.mean(arr[:p+1])
        for i in range(p+1, n):
            out[i] = (out[i-1] * (p-1) + arr[i]) / p
        return out

    atr  = rma(tr,  period)
    pdm  = rma(dmp, period)
    mdm  = rma(dmm, period)

    di_p = np.where(atr>0, pdm/atr*100, 0.0)
    di_m = np.where(atr>0, mdm/atr*100, 0.0)
    dx   = np.where((di_p+di_m)>0, np.abs(di_p-di_m)/(di_p+di_m)*100, 0.0)
    adx  = rma(dx, period)
    return di_p, di_m, adx


def calc_stoch(h, lo, c, k_len=14, sm_k=3, sm_d=3):
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(k_len-1, n):
        hi = h[i-k_len+1:i+1].max(); ll = lo[i-k_len+1:i+1].min()
        r = hi - ll
        raw[i] = (c[i]-ll)/r*100 if r>0 else 50.0
    k = pd.Series(raw).rolling(sm_k).mean().values
    d = pd.Series(k).rolling(sm_d).mean().values
    return k, d


# ──────────────────────────────────────────────────────────
#  점수 계산
# ──────────────────────────────────────────────────────────
def calc_scores(i, c, h, lo,
                cci_arr, conv_line, base_line, cloud_a, cloud_b,
                sar_arr, di_p, di_m, adx_arr, k_line, d_line,
                cci_os, cci_ob, adx_thresh, st_os, st_ob):
    if i < 1:
        return 0, 0

    # CCI
    bull_cci = (cci_arr[i-1] < cci_os and cci_arr[i] >= cci_os) or \
               (cci_arr[i] > 0 and cci_arr[i-1] <= 0)
    bear_cci = (cci_arr[i-1] > cci_ob and cci_arr[i] <= cci_ob) or \
               (cci_arr[i] < 0 and cci_arr[i-1] >= 0)

    # 일목
    ca = cloud_a[i]; cb = cloud_b[i]
    if not np.isnan(ca) and not np.isnan(cb):
        ct = max(ca, cb); cb_ = min(ca, cb)
        above = c[i] > ct; below = c[i] < cb_
        conv_cross_up   = conv_line[i] > base_line[i] and conv_line[i-1] <= base_line[i-1]
        conv_cross_down = conv_line[i] < base_line[i] and conv_line[i-1] >= base_line[i-1]
        bull_ich = above or (conv_cross_up and not below)
        bear_ich = below or (conv_cross_down and not above)
    else:
        bull_ich = bear_ich = False

    # SAR
    sar_bull_cur  = c[i]   > sar_arr[i]
    sar_bull_prev = c[i-1] > sar_arr[i-1]
    bull_sar = sar_bull_cur and not sar_bull_prev
    bear_sar = not sar_bull_cur and sar_bull_prev

    # DMI
    dmi_bull = di_p[i] > di_m[i] and adx_arr[i] >= adx_thresh
    dmi_bear = di_m[i] > di_p[i] and adx_arr[i] >= adx_thresh
    dmi_bull_p = di_p[i-1] > di_m[i-1] and adx_arr[i-1] >= adx_thresh
    dmi_bear_p = di_m[i-1] > di_p[i-1] and adx_arr[i-1] >= adx_thresh
    bull_dmi = (di_p[i] > di_m[i] and di_p[i-1] <= di_m[i-1]) or (dmi_bull and not dmi_bull_p)
    bear_dmi = (di_m[i] > di_p[i] and di_m[i-1] <= di_p[i-1]) or (dmi_bear and not dmi_bear_p)

    # 스토캐스틱
    if not np.isnan(k_line[i]) and not np.isnan(d_line[i]):
        bull_stoch = k_line[i-1] <= d_line[i-1] and k_line[i] > d_line[i] and k_line[i] < 50
        bear_stoch = k_line[i-1] >= d_line[i-1] and k_line[i] < d_line[i] and k_line[i] > 50
    else:
        bull_stoch = bear_stoch = False

    bs = sum([bull_cci, bull_ich, bull_sar, bull_dmi, bull_stoch])
    ss = sum([bear_cci, bear_ich, bear_sar, bear_dmi, bear_stoch])
    return bs, ss


# ──────────────────────────────────────────────────────────
#  백테스트 엔진
# ──────────────────────────────────────────────────────────
def backtest(df, p):
    c  = df["close"].values
    h  = df["high"].values
    lo = df["low"].values
    n  = len(c)

    cci_arr  = calc_cci(h, lo, c, p["cci_len"])
    conv_line, base_line, cloud_a, cloud_b = calc_ichimoku(
        h, lo, p["ich_conv"], p["ich_base"], p["ich_span"], p["ich_disp"])
    sar_arr  = calc_sar(h, lo, p["sar_start"], p["sar_inc"], p["sar_max"])
    di_p, di_m, adx_arr = calc_dmi(h, lo, c, p["dmi_len"])
    k_line, d_line = calc_stoch(h, lo, c, p["st_k"], p["st_smk"], p["st_smd"])

    equity  = 10000.0
    pos     = 0
    ep      = 0.0
    eq_size = 0.0
    eb      = 0
    trades  = []
    fee_r   = p["commission"] / 100
    highest = 0.0
    lowest  = 9e9

    def fee(q, px): return q * px * fee_r

    for i in range(2, n):
        bs, ss = calc_scores(
            i, c, h, lo,
            cci_arr, conv_line, base_line, cloud_a, cloud_b,
            sar_arr, di_p, di_m, adx_arr, k_line, d_line,
            p["cci_os"], p["cci_ob"], p["adx_thresh"],
            p["st_os"], p["st_ob"]
        )

        sar_bull_cur  = c[i]   > sar_arr[i]
        sar_bull_prev = c[i-1] > sar_arr[i-1]
        sar_flip_up   = sar_bull_cur and not sar_bull_prev
        sar_flip_down = not sar_bull_cur and sar_bull_prev

        lev  = p["leverage"]
        eq_r = p["equity_pct"]

        if pos == 1:
            # 추적 손절
            if c[i] > highest:
                highest = c[i]
            trail_sl = highest * (1 - p["trail_sl"] / 100)
            hard_sl  = ep * (1 - p["sl"] / 100)
            sl_price = max(trail_sl, hard_sl) if p["use_trail"] else hard_sl

            if lo[i] <= sl_price:
                pnl = (sl_price - ep) / ep * lev
                equity += eq_size * ep * pnl - fee(eq_size, sl_price)
                trades.append({"type":"L","entry":ep,"exit":sl_price,
                                "pnl_pct":pnl*100,"result":"SL","bars":i-eb})
                pos = 0

            elif ss >= p["exit_thresh"] or (p["sar_exit"] and sar_flip_down):
                pnl = (c[i] - ep) / ep * lev
                equity += eq_size * ep * pnl - fee(eq_size, c[i])
                trades.append({"type":"L","entry":ep,"exit":c[i],
                                "pnl_pct":pnl*100,"result":"REV","bars":i-eb})
                pos = 0

        elif pos == -1:
            if c[i] < lowest:
                lowest = c[i]
            trail_sl = lowest * (1 + p["trail_sl"] / 100)
            hard_sl  = ep * (1 + p["sl"] / 100)
            sl_price = min(trail_sl, hard_sl) if p["use_trail"] else hard_sl

            if h[i] >= sl_price:
                pnl = (ep - sl_price) / ep * lev
                equity += eq_size * ep * pnl - fee(eq_size, sl_price)
                trades.append({"type":"S","entry":ep,"exit":sl_price,
                                "pnl_pct":pnl*100,"result":"SL","bars":i-eb})
                pos = 0

            elif bs >= p["exit_thresh"] or (p["sar_exit"] and sar_flip_up):
                pnl = (ep - c[i]) / ep * lev
                equity += eq_size * ep * pnl - fee(eq_size, c[i])
                trades.append({"type":"S","entry":ep,"exit":c[i],
                                "pnl_pct":pnl*100,"result":"REV","bars":i-eb})
                pos = 0

        if pos == 0:
            if bs >= p["bull_thresh"]:
                ep      = c[i]
                eq_size = equity * eq_r * lev / ep
                equity -= fee(eq_size, ep)
                eb = i; highest = ep; pos = 1

            elif ss >= p["bear_thresh"]:
                ep      = c[i]
                eq_size = equity * eq_r * lev / ep
                equity -= fee(eq_size, ep)
                eb = i; lowest = ep; pos = -1

    # 미청산 강제 청산
    if pos != 0:
        pnl = ((c[-1]-ep)/ep if pos==1 else (ep-c[-1])/ep) * p["leverage"]
        equity += eq_size * ep * pnl - fee(eq_size, c[-1])
        trades.append({"type":"L" if pos==1 else "S","entry":ep,"exit":c[-1],
                        "pnl_pct":pnl*100,"result":"OPEN","bars":n-1-eb})

    nt = len(trades)
    wins   = [t for t in trades if t["pnl_pct"]>0]
    losses = [t for t in trades if t["pnl_pct"]<=0]
    wr     = len(wins)/nt*100 if nt else 0.0
    aw     = np.mean([t["pnl_pct"] for t in wins])   if wins   else 0.0
    al     = np.mean([t["pnl_pct"] for t in losses])  if losses else 0.0
    suml   = abs(sum(t["pnl_pct"] for t in losses))
    pf     = sum(t["pnl_pct"] for t in wins)/suml if suml>0 else float("inf")
    ret    = (equity/10000-1)*100
    ab     = np.mean([t["bars"] for t in trades]) if trades else 0.0

    rc = {}
    for t in trades: rc[t["result"]] = rc.get(t["result"],0)+1

    return {"n_trades":nt,"win_rate":wr,"avg_win":aw,"avg_loss":al,
            "profit_factor":pf,"total_return":ret,"avg_bars":ab,
            "final_equity":equity,"trades":trades,"result_counts":rc}


# ──────────────────────────────────────────────────────────
#  메인
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="추세전환 전략 백테스트")
    parser.add_argument("--symbol",       default="ETHUSDT")
    parser.add_argument("--tf",           default="4h")
    parser.add_argument("--pages",        type=int,   default=6)
    parser.add_argument("--cci_len",      type=int,   default=14)
    parser.add_argument("--cci_os",       type=int,   default=-100)
    parser.add_argument("--cci_ob",       type=int,   default=100)
    parser.add_argument("--ich_conv",     type=int,   default=9)
    parser.add_argument("--ich_base",     type=int,   default=26)
    parser.add_argument("--ich_span",     type=int,   default=52)
    parser.add_argument("--ich_disp",     type=int,   default=26)
    parser.add_argument("--sar_start",    type=float, default=0.02)
    parser.add_argument("--sar_inc",      type=float, default=0.02)
    parser.add_argument("--sar_max",      type=float, default=0.2)
    parser.add_argument("--dmi_len",      type=int,   default=14)
    parser.add_argument("--adx_thresh",   type=int,   default=20)
    parser.add_argument("--st_k",         type=int,   default=14)
    parser.add_argument("--st_smk",       type=int,   default=3)
    parser.add_argument("--st_smd",       type=int,   default=3)
    parser.add_argument("--st_os",        type=int,   default=20)
    parser.add_argument("--st_ob",        type=int,   default=80)
    parser.add_argument("--bull_thresh",  type=int,   default=3)
    parser.add_argument("--bear_thresh",  type=int,   default=3)
    parser.add_argument("--exit_thresh",  type=int,   default=3)
    parser.add_argument("--no_sar_exit",  action="store_true")
    parser.add_argument("--sl",           type=float, default=3.0)
    parser.add_argument("--trail_sl",     type=float, default=2.0)
    parser.add_argument("--no_trail",     action="store_true")
    parser.add_argument("--leverage",     type=int,   default=3)
    parser.add_argument("--equity",       type=float, default=0.10)
    parser.add_argument("--verbose",      action="store_true")
    args = parser.parse_args()

    p = {
        "cci_len":args.cci_len,"cci_os":args.cci_os,"cci_ob":args.cci_ob,
        "ich_conv":args.ich_conv,"ich_base":args.ich_base,
        "ich_span":args.ich_span,"ich_disp":args.ich_disp,
        "sar_start":args.sar_start,"sar_inc":args.sar_inc,"sar_max":args.sar_max,
        "dmi_len":args.dmi_len,"adx_thresh":args.adx_thresh,
        "st_k":args.st_k,"st_smk":args.st_smk,"st_smd":args.st_smd,
        "st_os":args.st_os,"st_ob":args.st_ob,
        "bull_thresh":args.bull_thresh,"bear_thresh":args.bear_thresh,
        "exit_thresh":args.exit_thresh,"sar_exit": not args.no_sar_exit,
        "sl":args.sl,"trail_sl":args.trail_sl,"use_trail": not args.no_trail,
        "leverage":args.leverage,"equity_pct":args.equity,"commission":0.04
    }

    print(f"\n{'='*60}")
    print(f"  추세전환 전략 백테스트")
    print(f"  심볼: {args.symbol}  타임프레임: {args.tf}")
    print(f"  진입 점수: 롱>={args.bull_thresh}  숏>={args.bear_thresh}  청산>={args.exit_thresh}")
    print(f"  SL={args.sl}%  추적SL={args.trail_sl}%  레버리지={args.leverage}x")
    print(f"{'='*60}")

    print(f"\n데이터 다운로드 중...")
    try:
        df = fetch_ohlcv(args.symbol, args.tf, args.pages)
    except Exception as e:
        print(f"[오류] {e}")
        sys.exit(1)

    print(f"기간: {df.index[0]} ~ {df.index[-1]}  ({len(df)} 봉)")
    result = backtest(df, p)

    t = result
    print(f"\n{'─'*50}")
    print(f"  총 수익률    : {t['total_return']:+.2f}%")
    print(f"  최종 자산    : ${t['final_equity']:,.2f}")
    print(f"  총 거래 수   : {t['n_trades']}건")
    print(f"  승률         : {t['win_rate']:.1f}%")
    print(f"  평균 수익    : {t['avg_win']:+.2f}%  |  평균 손실: {t['avg_loss']:+.2f}%")
    print(f"  손익비       : {t['profit_factor']:.2f}")
    print(f"  평균 보유봉  : {t['avg_bars']:.1f}")
    print(f"  청산 유형    : {t['result_counts']}")
    print(f"{'─'*50}")

    if args.verbose and t["trades"]:
        print(f"\n  최근 20건:")
        for idx, tr in enumerate(t["trades"][-20:], 1):
            print(f"  {idx:>3}  {'롱' if tr['type']=='L' else '숏'}  "
                  f"진입{tr['entry']:>10.2f}  청산{tr['exit']:>10.2f}  "
                  f"{tr['pnl_pct']:>+8.2f}%  {tr['result']}  {tr['bars']}봉")
    print()


if __name__ == "__main__":
    main()
