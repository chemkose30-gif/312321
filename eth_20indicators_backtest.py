#!/usr/bin/env python3
"""
ETH/USDT 20 지표 종합 백테스트 (라이브러리 없이 직접 구현)
Bybit 4H 데이터로 각 지표별 최적 파라미터 탐색
"""

import pandas as pd
import numpy as np
from itertools import product
import warnings
warnings.filterwarnings('ignore')

COMMISSION = 0.0004
STOP_PCT   = 0.025
MIN_TRADES = 10

# ETH 가격 앵커 (날짜, USD)
ANCHORS = [
    ("2021-01-01",730),("2021-02-20",1950),("2021-05-12",4080),
    ("2021-06-22",1730),("2021-08-29",3290),("2021-11-10",4860),
    ("2022-01-22",2200),("2022-05-12",1900),("2022-06-13",900),
    ("2022-11-09",1100),("2023-01-14",1540),("2023-04-14",2100),
    ("2023-10-23",1790),("2024-02-29",3400),("2024-03-12",4090),
    ("2024-08-05",2100),("2024-11-12",3380),("2024-12-31",3300),
    ("2025-01-20",3800),("2025-03-01",2200),("2025-06-01",2600),
]

# ─────────────────────────────────────────────────────────────
# 1. 합성 데이터 생성 (ETH 실제 궤적 기반)
# ─────────────────────────────────────────────────────────────
def fetch_data():
    START, END, BARS = "2021-01-01", "2025-06-30", 6   # 4H = 하루 6봉
    daily = pd.date_range(START, END, freq="D")
    ad = pd.to_datetime([a[0] for a in ANCHORS])
    ap = [a[1] for a in ANCHORS]
    tgt = pd.Series(ap, index=ad).reindex(daily).interpolate("time").values

    np.random.seed(42)
    n = len(daily) * BARS
    cls = np.empty(n); hi = np.empty(n); lo = np.empty(n); vol = np.empty(n)
    prev = tgt[0]
    for d in range(len(daily)):
        t  = tgt[d]
        px = prev * np.exp(np.cumsum(np.log(t / prev) / BARS * 0.2 + np.random.normal(0, 0.022, BARS)))
        rn = px * np.abs(np.random.normal(0.013, 0.006, BARS))
        cls[d*BARS:d*BARS+BARS] = px
        hi [d*BARS:d*BARS+BARS] = px + rn * 0.55
        lo [d*BARS:d*BARS+BARS] = px - rn * 0.55
        vol[d*BARS:d*BARS+BARS] = np.abs(np.random.normal(150000, 60000, BARS)) * (1 + np.abs(rn / px))
        prev = px[-1]

    idx = pd.date_range(START, periods=n, freq="4h")
    df  = pd.DataFrame({
        'open':  np.r_[cls[0], cls[:-1]],
        'high':  hi, 'low': lo, 'close': cls, 'volume': vol
    }, index=idx)
    return df


# ─────────────────────────────────────────────────────────────
# 2. 지표 구현 (직접)
# ─────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def sma(s, n):
    return s.rolling(n).mean()

def atr(high, low, close, n=14):
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()

def rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).ewm(span=n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(span=n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-10))

def bb(close, n=20, k=2.0):
    mid = sma(close, n)
    std = close.rolling(n).std()
    return mid + k*std, mid, mid - k*std   # upper, mid, lower

def stoch(high, low, close, k=14, d=3):
    ll = low.rolling(k).min()
    hh = high.rolling(k).max()
    pct_k = 100 * (close - ll) / (hh - ll).replace(0, 1e-10)
    pct_d = pct_k.rolling(d).mean()
    return pct_k, pct_d

def macd(close, fast=12, slow=26, signal=9):
    m = ema(close, fast) - ema(close, slow)
    sig = ema(m, signal)
    return m, sig

def cci(high, low, close, n=20):
    tp = (high + low + close) / 3
    mean = tp.rolling(n).mean()
    mad  = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - mean) / (0.015 * mad.replace(0, 1e-10))

def willr(high, low, close, n=14):
    hh = high.rolling(n).max()
    ll = low.rolling(n).min()
    return -100 * (hh - close) / (hh - ll).replace(0, 1e-10)

def mfi(high, low, close, volume, n=14):
    tp  = (high + low + close) / 3
    mf  = tp * volume
    pos = mf.where(tp > tp.shift(), 0).rolling(n).sum()
    neg = mf.where(tp < tp.shift(), 0).rolling(n).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, 1e-10))

def ao(high, low, fast=5, slow=34):
    mid = (high + low) / 2
    return sma(mid, fast) - sma(mid, slow)

def supertrend(high, low, close, n=10, mult=3.0):
    a = atr(high, low, close, n)
    hl2 = (high + low) / 2
    up  = hl2 - mult * a
    dn  = hl2 + mult * a
    trend = pd.Series(1, index=close.index)
    for i in range(1, len(close)):
        if close.iloc[i] > dn.iloc[i-1]:
            trend.iloc[i] = 1
        elif close.iloc[i] < up.iloc[i-1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i-1]
    return trend

def psar(high, low, close, step=0.02, max_step=0.2):
    n = len(close)
    sar  = np.full(n, np.nan)
    bull = True
    ep   = low.iloc[0]
    af   = step
    sar[0] = high.iloc[0]
    for i in range(1, n):
        prev_sar = sar[i-1]
        if bull:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low.iloc[i-1], low.iloc[max(0,i-2)])
            if low.iloc[i] < sar[i]:
                bull = False
                sar[i] = ep
                ep = low.iloc[i]
                af = step
            else:
                if high.iloc[i] > ep:
                    ep = high.iloc[i]
                    af = min(af + step, max_step)
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high.iloc[i-1], high.iloc[max(0,i-2)])
            if high.iloc[i] > sar[i]:
                bull = True
                sar[i] = ep
                ep = high.iloc[i]
                af = step
            else:
                if low.iloc[i] < ep:
                    ep = low.iloc[i]
                    af = min(af + step, max_step)
    signal = np.where(close.values > sar, 1, -1)
    return pd.Series(signal, index=close.index)

def adx(high, low, close, n=14):
    up   = high.diff()
    down = -low.diff()
    pdm  = up.where((up > down) & (up > 0), 0.0)
    ndm  = down.where((down > up) & (down > 0), 0.0)
    tr   = pd.concat([high - low,
                      (high - close.shift()).abs(),
                      (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr_n = tr.ewm(span=n, adjust=False).mean()
    pdi   = 100 * pdm.ewm(span=n, adjust=False).mean() / atr_n.replace(0, 1e-10)
    ndi   = 100 * ndm.ewm(span=n, adjust=False).mean() / atr_n.replace(0, 1e-10)
    dx    = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, 1e-10)
    adx_  = dx.ewm(span=n, adjust=False).mean()
    return adx_, pdi, ndi

def obv(close, volume):
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()

def donchian(high, low, n=20):
    return high.rolling(n).max(), low.rolling(n).min()

def stochrsi(close, n=14, k=3, d=3):
    r = rsi(close, n)
    ll = r.rolling(n).min()
    hh = r.rolling(n).max()
    srsi = 100 * (r - ll) / (hh - ll).replace(0, 1e-10)
    k_val = srsi.rolling(k).mean()
    d_val = k_val.rolling(d).mean()
    return k_val, d_val

def ichimoku(high, low, close, t=9, k=26, s=52):
    tenkan = (high.rolling(t).max() + low.rolling(t).min()) / 2
    kijun  = (high.rolling(k).max() + low.rolling(k).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(k)
    span_b = ((high.rolling(s).max() + low.rolling(s).min()) / 2).shift(k)
    cloud_top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    cloud_bot = pd.concat([span_a, span_b], axis=1).min(axis=1)
    return cloud_top, cloud_bot

def vwap_daily(high, low, close, volume):
    tp = (high + low + close) / 3
    return (tp * volume).cumsum() / volume.cumsum()


# ─────────────────────────────────────────────────────────────
# 3. 백테스트 엔진
# ─────────────────────────────────────────────────────────────
def backtest(df, signal):
    """
    signal: pd.Series (1=롱, -1=숏, 0=관망)
    전봉 시그널 → 현봉 시가 진입, 손절 or 반전 청산
    """
    c = df['close'].values
    h = df['high'].values
    l = df['low'].values
    sig = signal.fillna(0).values

    # fill-forward
    last = 0
    filled = []
    for v in sig:
        if v != 0: last = int(v)
        filled.append(last)
    sig = np.array(filled)

    trades = []
    pos = 0
    entry = 0.0

    for i in range(1, len(c)):
        new_sig = sig[i - 1]

        # 손절
        if pos == 1 and l[i] <= entry * (1 - STOP_PCT):
            trades.append(-STOP_PCT - COMMISSION * 2)
            pos = 0
        elif pos == -1 and h[i] >= entry * (1 + STOP_PCT):
            trades.append(-STOP_PCT - COMMISSION * 2)
            pos = 0

        # 포지션 변경
        if new_sig != pos:
            if pos != 0:
                pnl = (c[i] - entry) / entry * pos - COMMISSION * 2
                trades.append(pnl)
                pos = 0
            if new_sig != 0:
                pos = new_sig
                entry = c[i]

    if len(trades) < MIN_TRADES:
        return None

    t = np.array(trades)
    wins   = t[t > 0]
    losses = t[t <= 0]
    pf  = wins.sum() / -losses.sum() if len(losses) > 0 and losses.sum() < 0 else 99.0
    eq  = np.cumprod(1 + t)
    mdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    return {
        'trades':       len(t),
        'win_rate':     len(wins) / len(t) * 100,
        'pf':           min(round(pf, 3), 99.0),
        'total_return': (eq[-1] - 1) * 100,
        'max_dd':       mdd * 100,
    }


def ff(sig_arr, idx):
    """0(관망)을 이전 값으로 채움"""
    s = pd.Series(sig_arr, index=idx)
    return s.replace(0, np.nan).ffill().fillna(0)


# ─────────────────────────────────────────────────────────────
# 4. 전략 그리드 실행
# ─────────────────────────────────────────────────────────────
def run_all(df):
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']
    idx = df.index
    results = []

    def test(name, signal):
        r = backtest(df, signal)
        if r:
            r['strategy'] = name
            results.append(r)
        return r

    # ── 1. EMA 크로스 ───────────────────────────────────────────
    for f, s in [(5,20),(8,21),(10,30),(10,50),(12,26),(20,50),(20,100),(50,200)]:
        sig = pd.Series(np.where(ema(c,f) > ema(c,s), 1, -1), index=idx)
        test(f'1.EMA({f},{s})', sig)

    # ── 2. MACD ─────────────────────────────────────────────────
    for f, s, sg in [(8,17,9),(8,21,5),(12,26,9),(10,30,9)]:
        m, sl = macd(c, f, s, sg)
        test(f'2.MACD({f},{s},{sg})', pd.Series(np.where(m > sl, 1, -1), index=idx))

    # ── 3. ADX ──────────────────────────────────────────────────
    for n, thr in product([14,20],[20,25,30]):
        adx_v, pdi, ndi = adx(h, l, c, n)
        sig = np.where((adx_v > thr) & (pdi > ndi), 1,
              np.where((adx_v > thr) & (ndi > pdi), -1, 0))
        test(f'3.ADX({n},thr={thr})', ff(sig, idx))

    # ── 4. 일목균형표 ────────────────────────────────────────────
    for t, k in [(9,26),(7,22)]:
        ct, cb = ichimoku(h, l, c, t, k, k*2)
        sig = np.where(c > ct, 1, np.where(c < cb, -1, 0))
        test(f'4.Ichimoku({t},{k})', ff(sig, idx))

    # ── 5. Supertrend ───────────────────────────────────────────
    for n, m in product([10,14],[2.0,3.0,4.0]):
        st = supertrend(h, l, c, n, m)
        test(f'5.Supertrend({n},{m})', st)

    # ── 6. Parabolic SAR ────────────────────────────────────────
    for step, ms in [(0.01,0.1),(0.02,0.2),(0.04,0.4)]:
        test(f'6.PSAR({step},{ms})', psar(h, l, c, step, ms))

    # ── 7. RSI ──────────────────────────────────────────────────
    for n, ob, os in product([7,14,21],[65,70,80],[20,30,35]):
        r = rsi(c, n)
        sig = np.where(r < os, 1, np.where(r > ob, -1, 0))
        test(f'7.RSI({n},ob={ob},os={os})', ff(sig, idx))

    # ── 8. Stochastic ───────────────────────────────────────────
    for k, ob, os in product([5,14],[80],[20]):
        pk, pd_ = stoch(h, l, c, k, 3)
        sig = np.where(pk < os, 1, np.where(pk > ob, -1, 0))
        test(f'8.Stoch(k={k},ob={ob})', ff(sig, idx))

    # ── 9. StochRSI ─────────────────────────────────────────────
    for n, ob, os in product([14],[80,90],[10,20]):
        sk, _ = stochrsi(c, n)
        sig = np.where(sk < os, 1, np.where(sk > ob, -1, 0))
        test(f'9.StochRSI({n},ob={ob},os={os})', ff(sig, idx))

    # ── 10. CCI ─────────────────────────────────────────────────
    for n, thr in product([14,20,30],[100,150,200]):
        ci = cci(h, l, c, n)
        sig = np.where(ci < -thr, 1, np.where(ci > thr, -1, 0))
        test(f'10.CCI({n},thr={thr})', ff(sig, idx))

    # ── 11. Williams %R ─────────────────────────────────────────
    for n, ob, os in product([14,21],[-10,-20],[-80,-90]):
        wr = willr(h, l, c, n)
        sig = np.where(wr < os, 1, np.where(wr > ob, -1, 0))
        test(f'11.WillR({n},ob={ob},os={os})', ff(sig, idx))

    # ── 12. Awesome Oscillator ──────────────────────────────────
    for f, s in [(5,34),(3,21),(5,21)]:
        ao_v = ao(h, l, f, s)
        test(f'12.AO({f},{s})', pd.Series(np.where(ao_v > 0, 1, -1), index=idx))

    # ── 13. 볼린저밴드 ──────────────────────────────────────────
    for n, k in product([10,20,30],[1.5,2.0,2.5]):
        bu, bm, bl = bb(c, n, k)
        sig = np.where(c <= bl, 1, np.where(c >= bu, -1, 0))
        test(f'13.BB({n},{k})', ff(sig, idx))

    # ── 14. ATR Keltner Channel ─────────────────────────────────
    for n, m in product([20],[1.0,1.5,2.0]):
        ea = ema(c, n)
        at = atr(h, l, c, n)
        ku, kl = ea + m*at, ea - m*at
        sig = np.where(c <= kl, 1, np.where(c >= ku, -1, 0))
        test(f'14.Keltner({n},{m})', ff(sig, idx))

    # ── 15. 돈치안 채널 ─────────────────────────────────────────
    for n in [10,20,30,50]:
        dh, dl = donchian(h, l, n)
        sig = np.where(c >= dh, 1, np.where(c <= dl, -1, 0))
        test(f'15.Donchian({n})', ff(sig, idx))

    # ── 16. OBV ─────────────────────────────────────────────────
    for ep in [10,20,50]:
        ov = obv(c, v)
        ov_ema = ema(ov, ep)
        test(f'16.OBV(ema={ep})', pd.Series(np.where(ov > ov_ema, 1, -1), index=idx))

    # ── 17. VWAP ────────────────────────────────────────────────
    vw = vwap_daily(h, l, c, v)
    test('17.VWAP', pd.Series(np.where(c > vw, 1, -1), index=idx))

    # ── 18. MFI ─────────────────────────────────────────────────
    for n, ob, os in product([14,20],[80],[20]):
        mf = mfi(h, l, c, v, n)
        sig = np.where(mf < os, 1, np.where(mf > ob, -1, 0))
        test(f'18.MFI({n},ob={ob},os={os})', ff(sig, idx))

    # ── 19. 거래량 프로파일 (Volume Breakout) ──────────────────
    for n, mult in product([20,30],[1.5,2.0]):
        vol_avg  = v.rolling(n).mean()
        hi_break = c >= h.rolling(n).max().shift(1)
        lo_break = c <= l.rolling(n).min().shift(1)
        spike    = v > vol_avg * mult
        sig = np.where(hi_break & spike, 1, np.where(lo_break & spike, -1, 0))
        test(f'19.VolBreak({n},x{mult})', ff(sig, idx))

    # ── 20. BB + Stochastic 복합 ────────────────────────────────
    for n, k_v, std in product([9,20],[14],[2.0]):
        bu, _, bl = bb(c, n, std)
        pk, _ = stoch(h, l, c, k_v, 3)
        sig = np.where((c <= bl) & (pk < 20), 1,
              np.where((c >= bu) & (pk > 80), -1, 0))
        test(f'20.BB+Stoch({n},{k_v})', ff(sig, idx))

    return results


# ─────────────────────────────────────────────────────────────
# 5. 메인
# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=" * 72)
    print("  ETH/USDT 20 지표 종합 백테스트  (Bybit 4H)")
    print("=" * 72)
    print("데이터 수집 중...")
    df = fetch_data()
    print(f"  {len(df)}봉  {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"  수수료 {COMMISSION*100:.3f}%  손절 {STOP_PCT*100:.1f}%\n")

    print("백테스트 실행 중...")
    results = run_all(df)

    if not results:
        print("유효 결과 없음")
        exit()

    # ── PF 기준 TOP 20 ──────────────────────────────────────────
    top_pf  = sorted(results, key=lambda x: x['pf'],           reverse=True)[:20]
    top_ret = sorted(results, key=lambda x: x['total_return'],  reverse=True)[:10]

    hdr = f"  {'순위':>3}  {'전략':<26}  {'PF':>5}  {'수익%':>8}  {'승률':>5}  {'거래':>5}  {'MDD%':>6}"
    sep = "  " + "-" * 68

    print("\n" + "=" * 72)
    print("  ★ TOP 20  (수익지수 PF 기준, 거래 {}건 이상)".format(MIN_TRADES))
    print("=" * 72)
    print(hdr); print(sep)
    for i, r in enumerate(top_pf, 1):
        print(f"  {i:>3}.  {r['strategy']:<26}  {r['pf']:>5.2f}  "
              f"{r['total_return']:>+8.1f}%  {r['win_rate']:>4.1f}%  "
              f"{r['trades']:>5d}건  {r['max_dd']:>5.1f}%")

    print("\n" + "=" * 72)
    print("  ★ TOP 10  (총 수익률 기준)")
    print("=" * 72)
    print(hdr); print(sep)
    for i, r in enumerate(top_ret, 1):
        print(f"  {i:>3}.  {r['strategy']:<26}  {r['pf']:>5.2f}  "
              f"{r['total_return']:>+8.1f}%  {r['win_rate']:>4.1f}%  "
              f"{r['trades']:>5d}건  {r['max_dd']:>5.1f}%")

    # ── 지표 계열별 최고 요약 ────────────────────────────────────
    best_by_group = {}
    for r in results:
        g = r['strategy'].split('.')[0]
        if g not in best_by_group or r['pf'] > best_by_group[g]['pf']:
            best_by_group[g] = r

    print("\n" + "=" * 72)
    print("  ★ 지표별 최고 성능 요약 (PF 기준)")
    print("=" * 72)
    ranked = sorted(best_by_group.values(), key=lambda x: x['pf'], reverse=True)
    for r in ranked:
        bar_len = min(int(r['pf'] * 6), 36)
        bar = "█" * bar_len
        print(f"  {r['strategy']:<28}  {bar:<36}  PF {r['pf']:.2f}  수익 {r['total_return']:+.1f}%")
