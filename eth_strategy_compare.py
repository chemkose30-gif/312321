"""
ETH/USDT  4시간봉  전략 비교 백테스트
- 5가지 전략을 동일 데이터로 비교
- 자금: $10,000  |  거래금액: 잔고의 3%
- 기간: 2021-01-01 ~ 2024-12-31

[전략 목록]
 S1. 기준선     : BB 터치 → 반대매매 (중앙선 청산)
 S2. 트렌드필터 : EMA50 위 → 롱만 / 아래 → 숏만
 S3. BB 돌파    : 밴드 돌파 방향으로 추세 추종
 S4. 이중 밴드  : 1σ 진입 + 2σ 청산 (좁은 밴드 진입)
 S5. 복합 최적  : 트렌드 필터 + BB(2.5σ) + RSI(30/70) + ATR 손절
"""

import pandas as pd
import numpy as np
import time
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════
INITIAL_CAPITAL = 10_000
TRADE_PCT       = 0.03
START           = "2021-01-01"
END             = "2024-12-31"
BARS_PER_DAY    = 6          # 4시간봉 = 하루 6봉

ANCHORS = [
    ("2021-01-01",  730), ("2021-02-20", 1950), ("2021-05-12", 4080),
    ("2021-06-22", 1730), ("2021-08-29", 3290), ("2021-09-21", 2700),
    ("2021-11-10", 4860), ("2021-12-04", 3880), ("2021-12-31", 3680),
    ("2022-01-22", 2200), ("2022-03-28", 3290), ("2022-05-12", 1900),
    ("2022-06-13",  900), ("2022-08-13", 1960), ("2022-09-15", 1500),
    ("2022-11-09", 1100), ("2022-12-31", 1200),
    ("2023-01-14", 1540), ("2023-02-16", 1680), ("2023-04-14", 2100),
    ("2023-05-25", 1820), ("2023-06-10", 1660), ("2023-07-14", 1890),
    ("2023-08-17", 1570), ("2023-09-11", 1600), ("2023-10-23", 1790),
    ("2023-12-05", 2200), ("2023-12-31", 2280),
    ("2024-01-12", 2580), ("2024-02-29", 3400), ("2024-03-12", 4090),
    ("2024-04-15", 2900), ("2024-05-23", 3780), ("2024-07-05", 2870),
    ("2024-08-05", 2100), ("2024-09-13", 2340), ("2024-10-01", 2600),
    ("2024-11-12", 3380), ("2024-12-16", 4000), ("2024-12-31", 3300),
]


# ───────────────────────────────────────────────────────
#  4H 데이터 생성
# ───────────────────────────────────────────────────────
def generate_4h_data() -> pd.DataFrame:
    daily_dates  = pd.date_range(START, END, freq="D")
    anchor_dates = pd.to_datetime([a[0] for a in ANCHORS])
    anchor_px    = [a[1] for a in ANCHORS]
    daily_target = (pd.Series(anchor_px, index=anchor_dates)
                    .reindex(daily_dates).interpolate("time").values)

    np.random.seed(2024)
    SIGMA_4H    = 0.025    # ETH 4H 변동성 ≈ 2.5%
    MR_STRENGTH = 0.20

    n_days  = len(daily_dates)
    n_bars  = n_days * BARS_PER_DAY
    all_cls = np.empty(n_bars)
    all_hi  = np.empty(n_bars)
    all_lo  = np.empty(n_bars)

    prev = daily_target[0]
    for d in range(n_days):
        tgt   = daily_target[d]
        noise = np.random.normal(0, SIGMA_4H, BARS_PER_DAY)
        mr    = np.log(tgt / prev) / BARS_PER_DAY * MR_STRENGTH
        px    = prev * np.exp(np.cumsum(mr + noise))
        rng   = px * np.abs(np.random.normal(0.012, 0.006, BARS_PER_DAY))
        idx   = d * BARS_PER_DAY
        all_cls[idx:idx+BARS_PER_DAY] = px
        all_hi [idx:idx+BARS_PER_DAY] = px + rng * 0.55
        all_lo [idx:idx+BARS_PER_DAY] = px - rng * 0.55
        prev  = px[-1]

    all_times = pd.date_range(START, periods=n_bars, freq="4h")
    df = pd.DataFrame({
        "Open"  : np.concatenate([[all_cls[0]], all_cls[:-1]]),
        "High"  : all_hi,
        "Low"   : all_lo,
        "Close" : all_cls,
    }, index=all_times)
    return df


# ───────────────────────────────────────────────────────
#  지표 계산
# ───────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]

    # BB (20기간, 2σ)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_upper"]  = sma20 + 2.0 * std20
    df["bb_mid"]    = sma20
    df["bb_lower"]  = sma20 - 2.0 * std20

    # BB (20기간, 1σ — 이중밴드용)
    df["bb_inner_u"] = sma20 + 1.0 * std20
    df["bb_inner_l"] = sma20 - 1.0 * std20

    # BB (20기간, 2.5σ — S5용)
    df["bb_wide_u"]  = sma20 + 2.5 * std20
    df["bb_wide_l"]  = sma20 - 2.5 * std20

    # RSI 14
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)

    # EMA 50 (트렌드 필터)
    df["ema50"]  = c.ewm(span=50, adjust=False).mean()
    # EMA 200 (장기 추세)
    df["ema200"] = c.ewm(span=200, adjust=False).mean()

    # ATR 14
    prev_c = c.shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_c).abs(),
        (df["Low"]  - prev_c).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # BB 폭 (밴드 수축 감지)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma20

    return df


# ───────────────────────────────────────────────────────
#  범용 백테스트 엔진
# ───────────────────────────────────────────────────────
def backtest(df_clean, strategy_fn) -> dict:
    cols_needed = ["Close","High","Low","bb_upper","bb_mid","bb_lower",
                   "bb_inner_u","bb_inner_l","bb_wide_u","bb_wide_l",
                   "rsi","ema50","ema200","atr","bb_width"]
    df2 = df_clean.dropna(subset=cols_needed)

    arr = {c: df2[c].to_numpy() for c in cols_needed}
    dts = df2.index.to_numpy()

    capital  = float(INITIAL_CAPITAL)
    pos      = None
    trades   = []
    eq_d, eq_v = [], []

    n = len(dts)
    for i in range(n):
        price = arr["Close"][i]

        if i % BARS_PER_DAY == 0:
            unr = 0.0
            if pos:
                s, ep, sz, _ = pos
                unr = (price - ep) / ep * s * sz
            eq_v.append(capital + unr)
            eq_d.append(dts[i])

        if pos is None:
            sig = strategy_fn(i, arr, price)
            if sig in (1, -1):
                pos = (sig, price, capital * TRADE_PCT, dts[i])
        else:
            s, ep, sz, edt = pos
            cur_ret = (price - ep) / ep * s
            stop_hit, exit_hit = strategy_fn(i, arr, price, pos=pos)
            if stop_hit or exit_hit:
                pnl = cur_ret * sz
                capital += pnl
                trades.append({
                    "entry_date" : edt, "exit_date"  : dts[i],
                    "side"       : "long" if s == 1 else "short",
                    "entry_price": ep,   "exit_price" : price,
                    "pnl_usd"   : pnl,  "pnl_pct"   : cur_ret * 100,
                    "exit_type" : "손절" if stop_hit else "목표",
                    "hold_4h"   : int((dts[i] - edt) / np.timedelta64(4, "h")),
                })
                pos = None

    if pos:
        s, ep, sz, edt = pos
        price   = arr["Close"][-1]
        cur_ret = (price - ep) / ep * s
        pnl     = cur_ret * sz
        capital += pnl
        trades.append({
            "entry_date" : edt, "exit_date"  : dts[-1],
            "side"       : "long" if s == 1 else "short",
            "entry_price": ep,   "exit_price" : price,
            "pnl_usd"   : pnl,  "pnl_pct"   : cur_ret * 100,
            "exit_type" : "강제청산",
            "hold_4h"   : int((dts[-1] - edt) / np.timedelta64(4, "h")),
        })

    eq = pd.Series(eq_v, index=pd.DatetimeIndex(eq_d), name="equity")
    return {
        "final_capital"    : capital,
        "total_return_pct" : (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100,
        "trades"           : pd.DataFrame(trades),
        "equity"           : eq,
    }


# ═══════════════════════════════════════════════════════
#  전략 정의
# ═══════════════════════════════════════════════════════

# ── S1. 기준선: BB 터치 반대매매, 중앙선 청산, 고정 손절 -3% ──
def s1_baseline(i, a, price, pos=None):
    STOP = 0.03
    if pos is None:
        if price <= a["bb_lower"][i] and a["rsi"][i] < 35: return 1
        if price >= a["bb_upper"][i] and a["rsi"][i] > 65: return -1
        return 0
    s, ep, sz, _ = pos
    cur = (price - ep) / ep * s
    stop_hit = cur <= -STOP
    mid = a["bb_mid"][i]
    tgt = (s == 1 and price >= mid) or (s == -1 and price <= mid)
    return stop_hit, tgt


# ── S2. 트렌드 필터: EMA50 위→롱만, 아래→숏만 ──
def s2_trend_filter(i, a, price, pos=None):
    STOP = 0.03
    if pos is None:
        above_ema = price > a["ema50"][i]
        if above_ema  and price <= a["bb_lower"][i] and a["rsi"][i] < 40: return 1
        if not above_ema and price >= a["bb_upper"][i] and a["rsi"][i] > 60: return -1
        return 0
    s, ep, sz, _ = pos
    cur = (price - ep) / ep * s
    stop_hit = cur <= -STOP
    mid = a["bb_mid"][i]
    tgt = (s == 1 and price >= mid) or (s == -1 and price <= mid)
    return stop_hit, tgt


# ── S3. BB 돌파 추세 추종: 밴드 돌파 방향으로 진입 ──
def s3_breakout(i, a, price, pos=None):
    STOP = 0.04
    if pos is None:
        if i < 1: return 0
        prev = a["Close"][i-1]
        # 전봉이 밴드 안, 현봉이 밴드 밖 = 돌파
        if prev < a["bb_upper"][i-1] and price >= a["bb_upper"][i] and a["rsi"][i] > 55: return 1
        if prev > a["bb_lower"][i-1] and price <= a["bb_lower"][i] and a["rsi"][i] < 45: return -1
        return 0
    s, ep, sz, _ = pos
    cur = (price - ep) / ep * s
    stop_hit = cur <= -STOP
    # 반대편 밴드 또는 수익 +8%에서 청산
    upper = a["bb_upper"][i]; lower = a["bb_lower"][i]
    tgt = (s == 1 and (price >= upper * 1.02 or cur >= 0.08)) or \
          (s == -1 and (price <= lower * 0.98 or cur >= 0.08))
    return stop_hit, tgt


# ── S4. 이중 밴드: 1σ 내부 밴드 진입, 2σ 외부 밴드 청산 ──
def s4_double_band(i, a, price, pos=None):
    STOP = 0.025
    if pos is None:
        rsi = a["rsi"][i]
        if price <= a["bb_inner_l"][i] and rsi < 40: return 1
        if price >= a["bb_inner_u"][i] and rsi > 60: return -1
        return 0
    s, ep, sz, _ = pos
    cur = (price - ep) / ep * s
    stop_hit = cur <= -STOP
    # 반대편 1σ 밴드까지 청산
    tgt = (s == 1 and price >= a["bb_inner_u"][i]) or \
          (s == -1 and price <= a["bb_inner_l"][i])
    return stop_hit, tgt


# ── S5. 복합 최적: EMA200 트렌드 + BB(2.5σ) + RSI(30/70) + ATR 손절 ──
def s5_optimized(i, a, price, pos=None):
    if pos is None:
        above_ema200 = price > a["ema200"][i]
        rsi = a["rsi"][i]
        # 롱: 장기 상승장 + 극단적 과매도
        if above_ema200 and price <= a["bb_wide_l"][i] and rsi < 30: return 1
        # 숏: 장기 하락장 + 극단적 과매수
        if not above_ema200 and price >= a["bb_wide_u"][i] and rsi > 70: return -1
        return 0
    s, ep, sz, _ = pos
    cur = (price - ep) / ep * s
    atr = a["atr"][i]
    # ATR 기반 손절: 1.5 × ATR
    stop_dist = 1.5 * atr / ep
    stop_hit  = cur <= -stop_dist
    # 중앙선 청산 OR +5% 이익 실현
    mid = a["bb_mid"][i]
    tgt = (s == 1 and (price >= mid or cur >= 0.05)) or \
          (s == -1 and (price <= mid or cur >= 0.05))
    return stop_hit, tgt


# ───────────────────────────────────────────────────────
#  통계 계산
# ───────────────────────────────────────────────────────
def calc_stats(res: dict) -> dict:
    t  = res["trades"]
    eq = res["equity"]
    if t.empty:
        return {"total_return_pct": 0, "mdd": 0, "sharpe": 0,
                "total": 0, "wr": 0, "pf": 0, "avg_hold": 0,
                "final": INITIAL_CAPITAL}
    wins   = t[t["pnl_usd"] > 0]
    losses = t[t["pnl_usd"] <= 0]
    total  = len(t)
    wr     = len(wins) / total * 100
    pf_val = (wins["pnl_usd"].sum() / abs(losses["pnl_usd"].sum())
              if len(losses) and losses["pnl_usd"].sum() != 0 else float("inf"))
    roll_max = eq.cummax()
    mdd      = ((eq - roll_max) / roll_max).min() * 100
    r        = eq.pct_change().dropna()
    sharpe   = r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0
    avg_hold = t["hold_4h"].mean() * 4  # hours

    yr_pnl = {}
    for yr in [2021, 2022, 2023, 2024]:
        yt = t[pd.to_datetime(t["entry_date"]).dt.year == yr]
        yr_pnl[yr] = yt["pnl_usd"].sum()

    return {
        "total_return_pct": res["total_return_pct"],
        "final"           : res["final_capital"],
        "mdd"             : mdd,
        "sharpe"          : sharpe,
        "total"           : total,
        "wr"              : wr,
        "pf"              : pf_val,
        "avg_hold_h"      : avg_hold,
        "yr"              : yr_pnl,
        "trades"          : t,
        "equity"          : eq,
    }


# ───────────────────────────────────────────────────────
#  결과 출력
# ───────────────────────────────────────────────────────
def print_comparison(results: list):
    names = [
        "S1. 기준선 (BB반대+중앙선)",
        "S2. 트렌드 필터 (EMA50)",
        "S3. BB 돌파 추세추종",
        "S4. 이중 밴드 (1σ→2σ)",
        "S5. 복합 최적화 (EMA200+ATR)",
    ]

    bar = "═" * 68
    print(f"\n{bar}")
    print(f"  📊  4시간봉  전략 비교  ({START} ~ {END})")
    print(f"{bar}")
    print(f"  {'전략':^28} │ {'수익률':>7} │ {'MDD':>7} │ {'샤프':>6} │ {'거래':>5} │ {'승률':>6} │ {'PF':>5}")
    print(f"  {'─'*28}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*6}─┼─{'─'*5}─┼─{'─'*6}─┼─{'─'*5}")

    for name, s in zip(names, results):
        pct  = s["total_return_pct"]
        icon = "▲" if pct >= 0 else "▼"
        pf_s = f"{s['pf']:.2f}" if s['pf'] != float("inf") else " ∞  "
        print(f"  {name:<28} │ {icon}{pct:>+6.2f}% │ {s['mdd']:>+7.2f}% │ {s['sharpe']:>6.2f} │ {s['total']:>5} │ {s['wr']:>5.1f}% │ {pf_s:>5}")

    print(f"\n  {'─'*68}")
    print(f"  📅  연도별 순손익")
    print(f"  {'─'*68}")
    print(f"  {'':^4} │ {'S1':>9} │ {'S2':>9} │ {'S3':>9} │ {'S4':>9} │ {'S5':>9}")
    print(f"  {'─'*4}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*9}─┼─{'─'*9}")
    for yr in [2021, 2022, 2023, 2024]:
        row = f"  {yr} │"
        for s in results:
            v = s["yr"].get(yr, 0)
            row += f" {'▲' if v>=0 else '▼'}${v:>+7.0f} │"
        print(row)

    # 최고 성적 전략 상세
    best_idx = max(range(len(results)), key=lambda i: results[i]["total_return_pct"])
    best     = results[best_idx]
    bname    = names[best_idx]

    print(f"\n{bar}")
    print(f"  🏆  최고 성적  →  {bname}")
    print(f"{bar}")
    t = best["trades"]
    wins   = t[t["pnl_usd"] > 0]
    losses = t[t["pnl_usd"] <= 0]
    stops  = t[t["exit_type"] == "손절"]

    print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
    print(f"  최종 자금      : $ {best['final']:>10,.2f}")
    print(f"  총 수익률      :  {best['total_return_pct']:>+9.2f} %")
    print(f"  최대 낙폭(MDD) :  {best['mdd']:>9.2f} %")
    print(f"  샤프 비율      :  {best['sharpe']:>9.2f}")
    print(f"{'─'*68}")
    print(f"  총 거래 수     : {best['total']:>5} 건")
    print(f"  평균 보유시간  : {best['avg_hold_h']:>6.1f} 시간  ({best['avg_hold_h']/24:.1f}일)")
    print(f"  승률           :  {best['wr']:>7.1f} %")
    pf_v = best['pf']
    print(f"  손익비(PF)     :  {pf_v if pf_v != float('inf') else '∞':>7}")
    print(f"  손절 비율      :  {len(stops)/best['total']*100:>7.1f} %")
    if len(wins):
        print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+9.2f}   최대: ${wins['pnl_usd'].max():>+.2f}")
    if len(losses):
        print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+9.2f}   최대: ${losses['pnl_usd'].min():>+.2f}")

    print(f"{'─'*68}")
    print(f"  최근 거래 10건:")
    for _, r in t.tail(10).iterrows():
        icon = "✅" if r["pnl_usd"] > 0 else "❌"
        side = "롱" if r["side"] == "long" else "숏"
        edt  = pd.Timestamp(r["entry_date"]).strftime("%Y-%m-%d")
        hh   = r["hold_4h"] * 4
        print(f"    {icon} {edt} {side}  ${r['entry_price']:>5.0f}→${r['exit_price']:>5.0f}"
              f"  {hh:>5}h  PnL:${r['pnl_usd']:>+7.2f} [{r['exit_type']}]")

    # 월별 성과 (최고 전략)
    print(f"\n{'─'*68}")
    print(f"  📆  연도-월별 손익  ({bname})")
    print(f"{'─'*68}")
    t_copy = t.copy()
    t_copy["yr"]  = pd.to_datetime(t_copy["entry_date"]).dt.year
    t_copy["mon"] = pd.to_datetime(t_copy["entry_date"]).dt.month
    for yr in [2021, 2022, 2023, 2024]:
        ty = t_copy[t_copy["yr"] == yr]
        print(f"  ── {yr}년 ──")
        for m in range(1, 13):
            tm  = ty[ty["mon"] == m]
            if tm.empty: continue
            pnl = tm["pnl_usd"].sum()
            cnt = len(tm)
            ic  = "▲" if pnl >= 0 else "▼"
            bar_len = min(int(abs(pnl) / 8), 18)
            print(f"  {m:>2}월 {ic}${pnl:>+7.2f} ({cnt:>3}건) {'█'*bar_len}")

    print(f"\n{'═'*68}")
    print(f"  ⚠  시뮬레이션 데이터 기반 / 수수료·슬리피지 미반영")
    print(f"  ⚠  4H 바이비트 수수료 0.04%×{best['total']}건×2 ≈ ${INITIAL_CAPITAL*best['total']*2*0.0004:.0f} 추가 비용\n")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════
print("=" * 68)
print("   ETH/USDT  4H봉  5가지 전략 비교 백테스트")
print("=" * 68)

t0 = time.time()
print(f"\n  📥 4H 데이터 생성 중...")
df = generate_4h_data()
df = add_indicators(df)
print(f"  ✅ {len(df):,}개 캔들  |  ETH ${df['Close'].min():.0f} ~ ${df['Close'].max():.0f}")

print(f"\n  🔄 5가지 전략 백테스트 실행...")
strategies = [s1_baseline, s2_trend_filter, s3_breakout, s4_double_band, s5_optimized]
results    = []
for fn in strategies:
    res = backtest(df, fn)
    results.append(calc_stats(res))

print(f"  ✅ 완료  ({time.time()-t0:.1f}초)")

print_comparison(results)
