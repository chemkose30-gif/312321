"""
MFI + Supertrend 15분봉 백테스트
백테스트 엔진과 데이터는 eth_20indicators_backtest.py 와 동일한 방식 사용
"""
import numpy as np
import pandas as pd
from itertools import product

# ── 공통 상수 ──────────────────────────────────────────────────────
COMMISSION = 0.0004   # 0.04% 편도 (Bybit maker)
STOP_PCT   = 0.025    # 손절 2.5%
BARS       = 96       # 15분봉 1일 = 96봉
LEVERAGE   = 3.0

# ── 데이터 생성 (15분봉, 2023-2025) ───────────────────────────────
def make_15m_data():
    np.random.seed(42)
    # 주요 앵커 가격 (일 단위)
    anchors = {
        "2021-01-01": 730,   "2021-03-15": 1750,  "2021-05-12": 4380,
        "2021-07-20": 1815,  "2021-11-10": 4740,  "2022-01-01": 3690,
        "2022-06-18": 880,   "2022-09-15": 1640,  "2022-11-11": 1070,
        "2023-01-01": 1200,  "2023-04-15": 2100,  "2023-08-15": 1650,
        "2023-10-20": 1800,  "2023-12-31": 2280,  "2024-03-15": 4050,
        "2024-05-20": 3100,  "2024-07-05": 2985,  "2024-09-15": 2420,
        "2024-11-20": 3360,  "2024-12-31": 3350,  "2025-01-20": 3280,
        "2025-03-15": 2800,  "2025-05-15": 2600,  "2025-06-30": 2500,
    }
    ad = pd.to_datetime(list(anchors.keys()))
    ap = list(anchors.values())
    all_days  = pd.date_range("2021-01-01", "2025-06-30", freq="D")
    tgt_full  = pd.Series(ap, index=ad).reindex(all_days).interpolate("time").ffill().bfill()

    daily = pd.date_range("2023-01-01", "2025-06-30", freq="D")
    tgt   = tgt_full.reindex(daily).values

    rows = []
    price = tgt[0]
    for day_idx, day in enumerate(daily):
        target = tgt[day_idx]
        for b in range(BARS):
            price += (target - price) * 0.001
            noise  = price * 0.006 * np.random.randn()
            o = price
            h = o + abs(price * 0.004 * np.random.randn())
            l = o - abs(price * 0.004 * np.random.randn())
            c = o + noise
            h = max(h, o, c);  l = min(l, o, c)
            vol = max(100, np.random.lognormal(10, 0.8))
            ts  = pd.Timestamp(day) + pd.Timedelta(minutes=15 * b)
            rows.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": vol})
            price = c

    df = pd.DataFrame(rows).set_index("ts")
    return df

# ── 지표 계산 ─────────────────────────────────────────────────────
def calc_supertrend(df, n=14, mult=3.0):
    hl2   = (df["high"] + df["low"]) / 2
    atr   = df["high"].combine(df["low"], max) - df["low"].combine(df["high"], min)
    # 실제 ATR (Wilder smoothing)
    tr    = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1/n, adjust=False).mean()

    ub = hl2 + mult * atr_s
    lb = hl2 - mult * atr_s

    final_ub = [np.nan] * len(df)
    final_lb = [np.nan] * len(df)
    direction = [1]   * len(df)
    close = df["close"].values

    for i in range(1, len(df)):
        # upper band
        if np.isnan(final_ub[i-1]) or ub.iloc[i] < final_ub[i-1] or close[i-1] > final_ub[i-1]:
            final_ub[i] = ub.iloc[i]
        else:
            final_ub[i] = final_ub[i-1]
        # lower band
        if np.isnan(final_lb[i-1]) or lb.iloc[i] > final_lb[i-1] or close[i-1] < final_lb[i-1]:
            final_lb[i] = lb.iloc[i]
        else:
            final_lb[i] = final_lb[i-1]
        # direction
        if direction[i-1] == -1 and close[i] > final_ub[i]:
            direction[i] = 1
        elif direction[i-1] == 1 and close[i] < final_lb[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]

    return pd.Series(direction, index=df.index)   # 1 = 상승, -1 = 하락

def calc_mfi(df, n=20):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    raw_mfi = df["volume"] * (2 * typical - df["high"] - df["low"])
    pos = raw_mfi.clip(lower=0)
    neg = (-raw_mfi).clip(lower=0)
    ratio = pos.rolling(n).sum() / (neg.rolling(n).sum().replace(0, 1e-10))
    return 100 - 100 / (1 + ratio)

# ── 백테스트 엔진 ─────────────────────────────────────────────────
def backtest(df, signal_series):
    c = df["close"].values
    l = df["low"].values
    h = df["high"].values
    sig = signal_series.fillna(0).values.astype(int)

    trades = []
    pos    = 0
    entry  = 0.0

    for i in range(1, len(c)):
        new_sig = sig[i - 1]   # 이전봉 신호로 현재봉 시가 진입

        # 손절 체크 (현재봉 저가/고가)
        if pos == 1 and l[i] <= entry * (1 - STOP_PCT):
            trades.append(-STOP_PCT - COMMISSION * 2)
            pos = 0
        elif pos == -1 and h[i] >= entry * (1 + STOP_PCT):
            trades.append(-STOP_PCT - COMMISSION * 2)
            pos = 0

        # 포지션 변경
        if new_sig != pos:
            if pos != 0:
                ret = (c[i] - entry) / entry * pos - COMMISSION * 2
                trades.append(ret)
                pos = 0
            if new_sig != 0:
                pos   = new_sig
                entry = c[i]

    if pos != 0:
        ret = (c[-1] - entry) / entry * pos - COMMISSION * 2
        trades.append(ret)

    if len(trades) < 10:
        return None

    t = np.array(trades) * LEVERAGE
    wins  = t[t > 0]
    loss  = t[t < 0]
    pf    = wins.sum() / abs(loss.sum()) if len(loss) and loss.sum() != 0 else 0
    wr    = len(wins) / len(t)
    cumr  = np.cumprod(1 + t)
    peak  = np.maximum.accumulate(cumr)
    mdd   = ((peak - cumr) / peak).max()
    total = cumr[-1] - 1

    return {"pf": round(pf, 3), "wr": round(wr*100, 1), "mdd": round(mdd*100, 1),
            "total": round(total*100, 1), "trades": len(t)}

# ── 메인 ─────────────────────────────────────────────────────────
print("데이터 생성 중...")
df = make_15m_data()
print(f"바 수: {len(df):,}  ({df.index[0].date()} ~ {df.index[-1].date()})")

# 파라미터 그리드
st_params  = [(10, 3.0), (14, 3.0), (14, 3.5), (14, 4.0), (20, 3.0), (20, 3.5)]
mfi_params = [(14, 75, 25), (14, 80, 20), (20, 75, 25), (20, 80, 20)]
use_mfi    = [True, False]

results = []
total_runs = len(st_params) * len(mfi_params) * len(use_mfi)
run = 0

for (stn, stm), (mfin, ob, os_), use_f in product(st_params, mfi_params, use_mfi):
    run += 1
    if run % 20 == 0:
        print(f"  진행: {run}/{total_runs}")

    st_dir = calc_supertrend(df, n=stn, mult=stm)
    mfi    = calc_mfi(df, n=mfin)

    # 신호 생성
    st_flip_up   = (st_dir == 1) & (st_dir.shift(1) == -1)
    st_flip_down = (st_dir == -1) & (st_dir.shift(1) == 1)

    mfi_bull = mfi < os_
    mfi_bear = mfi > ob

    if use_f:
        long_ok  = mfi_bull
        short_ok = mfi_bear
    else:
        long_ok  = pd.Series(True, index=df.index)
        short_ok = pd.Series(True, index=df.index)

    signal = pd.Series(0, index=df.index)
    signal[st_flip_up   & long_ok]  = 1
    signal[st_flip_down & short_ok] = -1

    # 포지션 유지 (신호 없으면 직전 포지션)
    pos_signal = pd.Series(0, index=df.index)
    pos = 0
    for i in range(len(signal)):
        s = signal.iloc[i]
        if s != 0:
            pos = s
        elif st_dir.iloc[i] == -pos:   # Supertrend 반전 시 청산
            pos = 0
        pos_signal.iloc[i] = pos

    res = backtest(df, pos_signal)
    if res:
        res.update({
            "st_n": stn, "st_mult": stm,
            "mfi_n": mfin, "ob": ob, "os": os_,
            "mfi_filter": use_f
        })
        results.append(res)

# ── 결과 출력 ─────────────────────────────────────────────────────
if not results:
    print("결과 없음")
else:
    df_res = pd.DataFrame(results).sort_values("pf", ascending=False)
    print("\n" + "="*85)
    print("MFI + Supertrend 15분봉 백테스트 결과 (상위 15개, PF 기준)")
    print("="*85)
    print(f"{'ST(n,mult)':<14} {'MFI(n,ob,os)':<16} {'필터':<6} {'PF':<7} {'승률':<7} {'거래':<7} {'MDD':<8} {'총수익'}")
    print("-"*85)
    for _, r in df_res.head(15).iterrows():
        st_str  = f"ST({r['st_n']},{r['st_mult']})"
        mfi_str = f"MFI({r['mfi_n']},{r['ob']},{r['os']})"
        flt     = "O" if r["mfi_filter"] else "X"
        print(f"{st_str:<14} {mfi_str:<16} {flt:<6} {r['pf']:<7} {r['wr']:<6}%  {r['trades']:<7} {r['mdd']:<7}%  {r['total']}%")

    best = df_res.iloc[0]
    print(f"\n★ 최적 파라미터: ST({best['st_n']}, {best['st_mult']}) + MFI({best['mfi_n']}, {best['ob']}, {best['os']}), MFI필터={'ON' if best['mfi_filter'] else 'OFF'}")
    print(f"   PF {best['pf']}  승률 {best['wr']}%  거래수 {best['trades']}  MDD -{best['mdd']}%  총수익 {best['total']}%")
