"""
BB+Stochastic / VWAP+RSI+Vol 파라미터 최적화 (그리드 서치)

로컬 실행 (실제 데이터):
  python param_optimizer.py --strategy bb   --symbol ETHUSDT --tf 4h
  python param_optimizer.py --strategy vwap --symbol ETHUSDT --tf 1h

원격 환경 / 오프라인 (시뮬레이션 데이터):
  python param_optimizer.py --strategy bb   --sim
  python param_optimizer.py --strategy vwap --sim

필요 패키지: pip install requests numpy pandas
"""
import argparse
import itertools
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd

# ──────────────────────────────────────────────────────────
#  시뮬레이션 ETH OHLCV 생성
# ──────────────────────────────────────────────────────────
def make_sim_ohlcv(n_bars: int = 2000, tf_minutes: int = 240,
                   seed: int = 42) -> pd.DataFrame:
    """
    실제 ETH 통계에 근사한 합성 OHLCV 생성
    - 일 변동성 3%, 일 드리프트 0.05%
    - 추세 구간(60%) + 횡보 구간(40%) 혼합
    - 거래량 클러스터링 반영
    """
    rng = np.random.default_rng(seed)
    tf_frac = tf_minutes / (24 * 60)
    bar_vol   = 0.030 * np.sqrt(tf_frac)
    bar_drift = 0.0005 * tf_frac

    close = np.zeros(n_bars)
    close[0] = 2000.0

    # 추세/횡보 구간 마킹
    regime = np.zeros(n_bars, dtype=int)  # 0=횡보, 1=상승추세, -1=하락추세
    i = 0
    while i < n_bars:
        r = rng.choice([-1, 0, 1], p=[0.20, 0.40, 0.40])
        length = int(rng.integers(30, 120))
        regime[i:i+length] = r
        i += length

    for i in range(1, n_bars):
        drift_adj = bar_drift + regime[i] * bar_vol * 0.15
        shock = rng.normal(drift_adj, bar_vol)
        close[i] = close[i-1] * (1 + shock)

    # OHLCV 구성
    high_fac = 1 + np.abs(rng.normal(0, bar_vol * 0.5, n_bars))
    low_fac  = 1 - np.abs(rng.normal(0, bar_vol * 0.5, n_bars))
    open_ = np.roll(close, 1); open_[0] = close[0]

    high  = np.maximum(close * high_fac, open_ * high_fac)
    low   = np.minimum(close * low_fac,  open_ * low_fac)

    # 거래량: 변동성이 클 때 높음
    vol_base  = 50000.0
    vol_spike = np.abs(close / np.roll(close, 1) - 1) * 500000
    volume    = vol_base + vol_spike + rng.exponential(vol_base * 0.3, n_bars)

    idx = pd.date_range("2023-01-01", periods=n_bars, freq=f"{tf_minutes}min")
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close, "volume": volume}, index=idx)
    return df


# ──────────────────────────────────────────────────────────
#  Binance OHLCV
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
#  공통 지표
# ──────────────────────────────────────────────────────────
def calc_bb(src, period, std_mult):
    mid = pd.Series(src).rolling(period).mean().values
    std = pd.Series(src).rolling(period).std(ddof=0).values
    return mid, mid + std_mult*std, mid - std_mult*std

def calc_stoch(h, lo, c, k_len, sm_k, sm_d):
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(k_len-1, n):
        hi_ = h[i-k_len+1:i+1].max(); lo_ = lo[i-k_len+1:i+1].min()
        rng = hi_ - lo_
        raw[i] = (c[i]-lo_)/rng*100 if rng > 0 else 50.0
    k = pd.Series(raw).rolling(sm_k).mean().values
    d = pd.Series(k).rolling(sm_d).mean().values
    return k, d

def calc_ema(src, p):
    return pd.Series(src).ewm(span=p, adjust=False).mean().values

def calc_rsi(c, p=14):
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta>0, delta, 0.0)
    loss = np.where(delta<0, -delta, 0.0)
    ag = pd.Series(gain).ewm(com=p-1, adjust=False).mean().values
    al = pd.Series(loss).ewm(com=p-1, adjust=False).mean().values
    rs = np.where(al==0, 100.0, ag/al)
    return 100-(100/(1+rs))

def calc_vol_ma(vol, p):
    return pd.Series(vol).rolling(p).mean().values

def calc_vwap_session(df):
    typical = (df["high"]+df["low"]+df["close"])/3
    vwap = np.full(len(df), np.nan)
    cum_pv = cum_vol = cum_pv2 = 0.0
    prev_date = None
    for i, (ts, row) in enumerate(df.iterrows()):
        cur_date = ts.floor("D")
        if cur_date != prev_date:
            cum_pv = cum_vol = cum_pv2 = 0.0
            prev_date = cur_date
        tp = typical.iloc[i]; v = row["volume"]
        cum_pv += tp*v; cum_vol += v; cum_pv2 += tp*tp*v
        if cum_vol > 0:
            vv = cum_pv/cum_vol
            std = np.sqrt(max(cum_pv2/cum_vol - vv**2, 0.0))
            vwap[i] = vv
    return vwap


# ──────────────────────────────────────────────────────────
#  BB+Stochastic 단일 백테스트
# ──────────────────────────────────────────────────────────
def run_bb(df, p):
    c=df["close"].values; h=df["high"].values; lo=df["low"].values
    vol=df["volume"].values; n=len(c)
    mid,upper,lower = calc_bb(c, p["bb_period"], p["bb_std"])
    k_line,d_line   = calc_stoch(h,lo,c, p["stoch_k"], p["stoch_smk"], p["stoch_smd"])
    ema200          = calc_ema(c, 200)
    vol_ma          = calc_vol_ma(vol, 20)

    equity=10000.0; pos=0; ep=0.0; eq1=eq2=0.0; eb=0; pc=False
    trades=[]
    fee_r = p["commission"]/100

    def fee(q,px): return q*px*fee_r

    for i in range(1,n):
        bw = (upper[i]-lower[i])/mid[i] if mid[i]>0 else 999.0
        ema_ok_l  = (not p["use_ema"]) or c[i]<ema200[i]
        ema_ok_s  = (not p["use_ema"]) or c[i]>ema200[i]
        vol_ok    = (not p["use_vol"]) or vol[i]>=vol_ma[i]*1.0
        bb_ok     = (not p["use_bbw"]) or bw<=p["bbw_max"]

        kp=k_line[i-1]; dp=d_line[i-1]; kc=k_line[i]; dc=d_line[i]
        if np.isnan(kp) or np.isnan(dp): continue
        lx = kp<=dp and kc>dc
        sx = kp>=dp and kc<dc

        long_sig  = c[i]<=lower[i] and kc<p["os"] and lx and ema_ok_l and vol_ok and bb_ok
        short_sig = c[i]>=upper[i] and kc>p["ob"] and sx and ema_ok_s and vol_ok and bb_ok

        lev = p["leverage"]; eq_r = p["equity_pct"]

        # 포지션 관리
        if pos==1:
            sl=ep*(1-p["sl"]/100)
            if lo[i]<=sl:
                rem=eq2 if pc else (eq1+eq2)
                pnl=(sl-ep)/ep*lev
                equity+=rem*ep*pnl-fee(rem,sl)
                trades.append({"pnl":pnl*100,"r":"SL"})
                pos=0; pc=False
            elif p["mid_exit"] and c[i]>=mid[i]:
                pnl=(c[i]-ep)/ep*lev
                equity+=(eq1+eq2)*ep*pnl-fee(eq1+eq2,c[i])
                trades.append({"pnl":pnl*100,"r":"MID"})
                pos=0; pc=False
            else:
                if not pc and h[i]>=ep*(1+p["tp1"]/100):
                    pnl=(ep*(1+p["tp1"]/100)-ep)/ep*lev
                    equity+=eq1*ep*pnl-fee(eq1,ep*(1+p["tp1"]/100))
                    pc=True
                if pc and h[i]>=ep*(1+p["tp2"]/100):
                    pnl=(ep*(1+p["tp2"]/100)-ep)/ep*lev
                    equity+=eq2*ep*pnl-fee(eq2,ep*(1+p["tp2"]/100))
                    trades.append({"pnl":pnl*100,"r":"TP2"})
                    pos=0; pc=False

        elif pos==-1:
            sl=ep*(1+p["sl"]/100)
            if h[i]>=sl:
                rem=eq2 if pc else (eq1+eq2)
                pnl=(ep-sl)/ep*lev
                equity+=rem*ep*pnl-fee(rem,sl)
                trades.append({"pnl":pnl*100,"r":"SL"})
                pos=0; pc=False
            elif p["mid_exit"] and c[i]<=mid[i]:
                pnl=(ep-c[i])/ep*lev
                equity+=(eq1+eq2)*ep*pnl-fee(eq1+eq2,c[i])
                trades.append({"pnl":pnl*100,"r":"MID"})
                pos=0; pc=False
            else:
                if not pc and lo[i]<=ep*(1-p["tp1"]/100):
                    pnl=(ep-ep*(1-p["tp1"]/100))/ep*lev
                    equity+=eq1*ep*pnl-fee(eq1,ep*(1-p["tp1"]/100))
                    pc=True
                if pc and lo[i]<=ep*(1-p["tp2"]/100):
                    pnl=(ep-ep*(1-p["tp2"]/100))/ep*lev
                    equity+=eq2*ep*pnl-fee(eq2,ep*(1-p["tp2"]/100))
                    trades.append({"pnl":pnl*100,"r":"TP2"})
                    pos=0; pc=False

        if pos==0:
            if long_sig:
                ep=c[i]; tv=equity*eq_r*lev; eq1=tv*0.5/ep; eq2=tv*0.5/ep
                equity-=fee(eq1+eq2,ep); eb=i; pc=False; pos=1
            elif short_sig:
                ep=c[i]; tv=equity*eq_r*lev; eq1=tv*0.5/ep; eq2=tv*0.5/ep
                equity-=fee(eq1+eq2,ep); eb=i; pc=False; pos=-1

    return _stats(trades, equity)


# ──────────────────────────────────────────────────────────
#  VWAP+RSI+Vol 단일 백테스트
# ──────────────────────────────────────────────────────────
def run_vwap(df, p):
    c=df["close"].values; h=df["high"].values; lo=df["low"].values
    vol=df["volume"].values; n=len(c)

    vwap    = calc_vwap_session(df)
    rsi_arr = calc_rsi(c, p["rsi_len"])
    vol_ma  = calc_vol_ma(vol, 20)

    equity=10000.0; pos=0; ep=0.0; eq1=eq2=0.0; eb=0; pc=False
    trades=[]
    fee_r = p["commission"]/100

    def fee(q,px): return q*px*fee_r

    for i in range(1,n):
        if np.isnan(vwap[i]) or np.isnan(rsi_arr[i]) or np.isnan(vol_ma[i]):
            continue
        vol_ok = vol[i]>=vol_ma[i]*p["vol_mult"]
        pra = c[i-1]>vwap[i-1] if not np.isnan(vwap[i-1]) else False
        cra = c[i]>vwap[i]
        vup  = (not pra) and cra
        vdn  = pra and (not cra)
        ri_up = rsi_arr[i-1]<p["rsi_os"] and rsi_arr[i]>=p["rsi_os"] and cra
        ri_dn = rsi_arr[i-1]>p["rsi_ob"] and rsi_arr[i]<=p["rsi_ob"] and not cra

        long_sig  = (vup or ri_up) and rsi_arr[i]>=p["rsi_long_min"] and vol_ok
        short_sig = (vdn or ri_dn) and rsi_arr[i]<=p["rsi_short_max"] and vol_ok

        lev=p["leverage"]; eq_r=p["equity_pct"]

        if pos==1:
            sl=ep*(1-p["sl"]/100)
            if lo[i]<=sl:
                rem=eq2 if pc else (eq1+eq2)
                pnl=(sl-ep)/ep*lev
                equity+=rem*ep*pnl-fee(rem,sl)
                trades.append({"pnl":pnl*100,"r":"SL"})
                pos=0; pc=False
            elif p["vwap_exit"] and c[i]<vwap[i] and not pc:
                pnl=(c[i]-ep)/ep*lev
                equity+=(eq1+eq2)*ep*pnl-fee(eq1+eq2,c[i])
                trades.append({"pnl":pnl*100,"r":"VWAP"})
                pos=0; pc=False
            else:
                if not pc and h[i]>=ep*(1+p["tp1"]/100):
                    pnl=(ep*(1+p["tp1"]/100)-ep)/ep*lev
                    equity+=eq1*ep*pnl-fee(eq1,ep*(1+p["tp1"]/100))
                    pc=True
                if pc and h[i]>=ep*(1+p["tp2"]/100):
                    pnl=(ep*(1+p["tp2"]/100)-ep)/ep*lev
                    equity+=eq2*ep*pnl-fee(eq2,ep*(1+p["tp2"]/100))
                    trades.append({"pnl":pnl*100,"r":"TP2"})
                    pos=0; pc=False

        elif pos==-1:
            sl=ep*(1+p["sl"]/100)
            if h[i]>=sl:
                rem=eq2 if pc else (eq1+eq2)
                pnl=(ep-sl)/ep*lev
                equity+=rem*ep*pnl-fee(rem,sl)
                trades.append({"pnl":pnl*100,"r":"SL"})
                pos=0; pc=False
            elif p["vwap_exit"] and c[i]>vwap[i] and not pc:
                pnl=(ep-c[i])/ep*lev
                equity+=(eq1+eq2)*ep*pnl-fee(eq1+eq2,c[i])
                trades.append({"pnl":pnl*100,"r":"VWAP"})
                pos=0; pc=False
            else:
                if not pc and lo[i]<=ep*(1-p["tp1"]/100):
                    pnl=(ep-ep*(1-p["tp1"]/100))/ep*lev
                    equity+=eq1*ep*pnl-fee(eq1,ep*(1-p["tp1"]/100))
                    pc=True
                if pc and lo[i]<=ep*(1-p["tp2"]/100):
                    pnl=(ep-ep*(1-p["tp2"]/100))/ep*lev
                    equity+=eq2*ep*pnl-fee(eq2,ep*(1-p["tp2"]/100))
                    trades.append({"pnl":pnl*100,"r":"TP2"})
                    pos=0; pc=False

        if pos==0:
            if long_sig:
                ep=c[i]; tv=equity*eq_r*lev; eq1=tv*0.5/ep; eq2=tv*0.5/ep
                equity-=fee(eq1+eq2,ep); eb=i; pc=False; pos=1
            elif short_sig:
                ep=c[i]; tv=equity*eq_r*lev; eq1=tv*0.5/ep; eq2=tv*0.5/ep
                equity-=fee(eq1+eq2,ep); eb=i; pc=False; pos=-1

    return _stats(trades, equity)


def _stats(trades, equity):
    n = len(trades)
    if n == 0:
        return {"n":0,"wr":0,"pf":0,"ret":(equity/10000-1)*100,"mdd":0,"score":0}
    wins   = [t["pnl"] for t in trades if t["pnl"]>0]
    losses = [t["pnl"] for t in trades if t["pnl"]<=0]
    wr   = len(wins)/n*100
    aw   = np.mean(wins)  if wins   else 0.0
    al   = np.mean(losses) if losses else 0.0
    sum_l = abs(sum(losses)) if losses else 1e-9
    pf   = sum(wins)/sum_l if losses else float("inf")
    ret  = (equity/10000-1)*100
    score = wr * min(pf, 5) * np.sign(ret)
    return {"n":n,"wr":wr,"aw":aw,"al":al,"pf":pf,"ret":ret,"score":score}


# ──────────────────────────────────────────────────────────
#  그리드 서치 정의
# ──────────────────────────────────────────────────────────
BB_GRID = {
    "bb_period":  [14, 20, 25],
    "bb_std":     [1.5, 2.0, 2.5],
    "stoch_k":    [9, 14],
    "stoch_smk":  [3],
    "stoch_smd":  [3],
    "os":         [15, 20, 25],
    "ob":         [75, 80, 85],
    "sl":         [1.0, 1.5, 2.0],
    "tp1":        [0.8, 1.2, 1.8],
    "tp2":        [2.5],
    "mid_exit":   [True, False],
    "use_ema":    [True, False],
    "use_vol":    [True],
    "use_bbw":    [True],
    "bbw_max":    [0.10],
    # 고정값
    "leverage":   [3],
    "equity_pct": [0.10],
    "commission": [0.04],
}

VWAP_GRID = {
    "rsi_len":       [9, 14, 21],
    "rsi_long_min":  [45, 50, 55],
    "rsi_short_max": [45, 50, 55],
    "rsi_os":        [35, 40],
    "rsi_ob":        [60, 65],
    "vol_mult":      [1.0, 1.2, 1.5],
    "sl":            [1.5, 2.0, 2.5],
    "tp1":           [1.0, 1.5, 2.0],
    "tp2":           [3.0],
    "vwap_exit":     [True, False],
    # 고정값
    "leverage":      [3],
    "equity_pct":    [0.10],
    "commission":    [0.04],
}


def expand_grid(grid: dict) -> List[dict]:
    keys = list(grid.keys())
    vals = list(grid.values())
    return [dict(zip(keys, combo)) for combo in itertools.product(*vals)]


# ──────────────────────────────────────────────────────────
#  병렬 실행
# ──────────────────────────────────────────────────────────
def run_grid(df, params_list, run_fn, max_workers=8, min_trades=5):
    results = []
    total = len(params_list)

    def worker(p):
        try:
            s = run_fn(df, p)
            if s["n"] >= min_trades:
                s.update(p)
                return s
        except Exception:
            pass
        return None

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker, p): p for p in params_list}
        for fut in as_completed(futures):
            done += 1
            if done % 200 == 0 or done == total:
                print(f"\r  진행: {done:5d}/{total}  완료...", end="", flush=True)
            res = fut.result()
            if res:
                results.append(res)

    print()
    return results


# ──────────────────────────────────────────────────────────
#  결과 출력
# ──────────────────────────────────────────────────────────
def print_results(results: List[dict], strategy: str, top_n: int = 30):
    if not results:
        print("결과 없음"); return

    df = pd.DataFrame(results)
    df = df.sort_values(["wr","pf","ret"], ascending=[False,False,False])

    if strategy == "bb":
        cols_show = ["bb_period","bb_std","stoch_k","stoch_smk","os","ob",
                     "sl","tp1","tp2","mid_exit","use_ema","use_vol","use_bbw"]
    else:
        cols_show = ["rsi_len","rsi_long_min","rsi_short_max","rsi_os","rsi_ob",
                     "vol_mult","sl","tp1","tp2","vwap_exit"]

    metric_cols = ["n","wr","aw","al","pf","ret"]
    show = [c for c in metric_cols+cols_show if c in df.columns]

    top = df.head(top_n)[show].copy()
    top["wr"]  = top["wr"].map("{:.1f}%".format)
    top["aw"]  = top["aw"].map("{:+.2f}%".format)
    top["al"]  = top["al"].map("{:+.2f}%".format)
    top["pf"]  = top["pf"].map(lambda x: f"{min(x,9.99):.2f}")
    top["ret"] = top["ret"].map("{:+.1f}%".format)

    # 컬럼 너비 맞춰 출력
    print(f"\n{'='*120}")
    print(f"  상위 {top_n}개 결과  (정렬: 승률 → 손익비 → 수익률)  전략: {strategy.upper()}")
    print(f"{'='*120}")
    header = f"{'#':>3} " + " ".join(f"{c:>10}" for c in top.columns)
    print(header)
    print("-"*len(header))
    for rank, (_, row) in enumerate(top.iterrows(), 1):
        line = f"{rank:>3} " + " ".join(f"{str(v):>10}" for v in row)
        print(line)
    print("="*120)

    # 요약 통계
    print(f"\n  전체 {len(df)}개 조합 중 유효 결과")
    print(f"  최고 승률: {df['wr'].max():.1f}%  |  최고 손익비: {min(df['pf'].max(),9.99):.2f}  |  최고 수익률: {df['ret'].max():+.1f}%")
    print(f"  평균 승률: {df['wr'].mean():.1f}%  |  평균 손익비: {min(df['pf'].mean(),9.99):.2f}  |  평균 수익률: {df['ret'].mean():+.1f}%")

    # CSV 저장
    fname = f"opt_{strategy}_results.csv"
    df.to_csv(fname, index=False)
    print(f"\n  전체 결과 저장: {fname}")


# ──────────────────────────────────────────────────────────
#  메인
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["bb","vwap","both"], default="both")
    parser.add_argument("--symbol",   default="ETHUSDT")
    parser.add_argument("--tf",       default="4h")
    parser.add_argument("--pages",    type=int, default=6)
    parser.add_argument("--sim",      action="store_true", help="시뮬레이션 데이터 사용")
    parser.add_argument("--top",      type=int, default=30)
    parser.add_argument("--workers",  type=int, default=8)
    parser.add_argument("--min_trades", type=int, default=8)
    args = parser.parse_args()

    TF_MIN = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240,"1d":1440}
    tf_min = TF_MIN.get(args.tf, 240)

    # 데이터 로드
    if args.sim:
        print(f"\n[시뮬레이션 모드] ETH 유사 합성 데이터 {2000}봉 생성...")
        df = make_sim_ohlcv(n_bars=2000, tf_minutes=tf_min)
    else:
        print(f"\n[실제 데이터] {args.symbol} {args.tf} 다운로드...")
        try:
            df = fetch_ohlcv(args.symbol, args.tf, args.pages)
        except Exception as e:
            print(f"  오류: {e}\n  --sim 옵션으로 시뮬레이션 데이터를 사용하세요.")
            sys.exit(1)

    print(f"  데이터: {len(df)}봉  {df.index[0]} ~ {df.index[-1]}")

    strategies = ["bb","vwap"] if args.strategy=="both" else [args.strategy]

    for strat in strategies:
        if strat == "bb":
            grid    = expand_grid(BB_GRID)
            run_fn  = run_bb
        else:
            grid    = expand_grid(VWAP_GRID)
            run_fn  = run_vwap

        print(f"\n{'─'*60}")
        print(f"  {strat.upper()} 전략 그리드 서치: {len(grid):,}개 조합")
        print(f"{'─'*60}")

        t0 = time.time()
        results = run_grid(df, grid, run_fn, args.workers, args.min_trades)
        elapsed = time.time() - t0

        print(f"  소요 시간: {elapsed:.1f}초  |  유효 결과: {len(results)}개")
        print_results(results, strat, args.top)

    print("\n완료!")

if __name__ == "__main__":
    main()
