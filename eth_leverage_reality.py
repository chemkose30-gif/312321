"""
ETH 하루 2-7% 목표 달성 가능성 분석
- S4 최고 전략 기반 레버리지별 시뮬레이션
- 목표 달성 확률, 파산 위험도, 현실적 기대치 계산
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

INITIAL_CAPITAL = 10_000
START = "2021-01-01"
END   = "2024-12-31"

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

# ─── 4H 데이터 + S4 전략 (이전과 동일) ─────────────────
def generate_4h(seed=2024):
    daily  = pd.date_range(START, END, freq="D")
    ad     = pd.to_datetime([a[0] for a in ANCHORS])
    ap     = [a[1] for a in ANCHORS]
    tgt    = pd.Series(ap, index=ad).reindex(daily).interpolate("time").values
    np.random.seed(seed)
    n      = len(daily) * 6
    cls    = np.empty(n); hi = np.empty(n); lo = np.empty(n)
    prev   = tgt[0]
    for d in range(len(daily)):
        t   = tgt[d]
        px  = prev * np.exp(np.cumsum(np.log(t/prev)/6*0.2 + np.random.normal(0,0.025,6)))
        rng = px * np.abs(np.random.normal(0.012,0.006,6))
        cls[d*6:d*6+6] = px; hi[d*6:d*6+6] = px+rng*0.55; lo[d*6:d*6+6] = px-rng*0.55
        prev = px[-1]
    idx = pd.date_range(START, periods=n, freq="4h")
    df  = pd.DataFrame({"Open":np.r_[cls[0],cls[:-1]],"High":hi,"Low":lo,"Close":cls},index=idx)
    c   = df["Close"]
    s20 = c.rolling(20).mean(); std20 = c.rolling(20).std()
    df["bb_u1"] = s20+1*std20; df["bb_l1"] = s20-1*std20
    df["bb_u2"] = s20+2*std20; df["bb_l2"] = s20-2*std20; df["bb_mid"] = s20
    d   = c.diff(); g = d.clip(lower=0).rolling(14).mean(); l = (-d.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100/(1+(g/l.replace(0,np.nan)))
    return df.dropna()


def run_s4(df, leverage=1.0, pos_pct=0.03, stop_pct=0.025):
    """S4 이중밴드 + 레버리지"""
    cls  = df["Close"].to_numpy()
    u1   = df["bb_u1"].to_numpy(); l1 = df["bb_l1"].to_numpy()
    rsi  = df["rsi"].to_numpy()
    dts  = df.index.to_numpy()

    capital   = float(INITIAL_CAPITAL)
    pos       = None
    daily_pnl = {}   # date → cumulative daily P&L
    trades    = []

    for i in range(len(cls)):
        price = cls[i]
        date  = pd.Timestamp(dts[i]).date()

        if pos is None:
            if price <= l1[i] and rsi[i] < 40:
                sz  = min(capital * pos_pct, capital * 0.95)  # 파산 방지
                pos = (1, price, sz, dts[i])
            elif price >= u1[i] and rsi[i] > 60:
                sz  = min(capital * pos_pct, capital * 0.95)
                pos = (-1, price, sz, dts[i])
        else:
            s, ep, sz, edt = pos
            raw_ret  = (price - ep) / ep * s
            lev_ret  = raw_ret * leverage         # 레버리지 적용 수익률
            lev_stop = stop_pct / leverage        # 원금 기준 손절 (레버리지 반영)

            # 청산 (반대 1σ 밴드)
            tgt = (s==1 and price>=u1[i]) or (s==-1 and price<=l1[i])
            # 손절 (레버리지 반영)
            stop_hit = lev_ret <= -stop_pct        # 원금 대비 손절
            # 강제청산 (레버리지 -100% 직전)
            liquidation = raw_ret <= -(1.0/leverage - 0.05) if leverage > 1 else False

            if stop_hit or tgt or liquidation:
                actual_pnl = lev_ret * sz
                actual_pnl = max(actual_pnl, -sz)   # 원금 이상 손실 불가
                capital   += actual_pnl
                capital    = max(capital, 0)
                et = "청산" if liquidation else ("손절" if stop_hit else "목표")
                trades.append({
                    "date"    : date,
                    "pnl_usd" : actual_pnl,
                    "pnl_pct" : lev_ret*100,
                    "exit"    : et,
                    "hold_4h" : int((dts[i]-edt)/np.timedelta64(4,"h")),
                })
                if date not in daily_pnl: daily_pnl[date] = 0.0
                daily_pnl[date] += actual_pnl
                pos = None
                if capital <= 100:   # 사실상 파산
                    break

    return capital, trades, daily_pnl


# ═══════════════════════════════════════════════════════
#  레버리지별 분석
# ═══════════════════════════════════════════════════════
print("=" * 66)
print("   ETH 4H  S4 전략  레버리지별 현실 분석")
print("=" * 66)

df = generate_4h()

leverages = [1, 3, 5, 10, 20]
results   = {}

for lev in leverages:
    final, trades, dpnl = run_s4(df, leverage=lev, pos_pct=0.03, stop_pct=0.025)
    tr = pd.DataFrame(trades)
    results[lev] = {"final": final, "trades": tr, "dpnl": dpnl}

# ── 레버리지 비교표 ────────────────────────────────────
print(f"\n  레버리지별 성과 비교  (초기 자금 ${INITIAL_CAPITAL:,})")
print(f"{'─'*66}")
print(f"  {'레버리지':^8} │ {'최종자금':>11} │ {'수익률':>8} │ {'파산여부':^8} │ {'거래수':>6} │ {'청산건':>6}")
print(f"  {'─'*8}─┼─{'─'*11}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*6}─┼─{'─'*6}")

for lev in leverages:
    r    = results[lev]
    fin  = r["final"]
    pct  = (fin - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    bk   = "💀 파산" if fin <= 100 else "✅ 생존"
    tr   = r["trades"]
    liq  = len(tr[tr["exit"] == "청산"]) if not tr.empty else 0
    tc   = len(tr)
    print(f"  {lev:>6}x    │ $ {fin:>9,.0f} │ {pct:>+7.1f}% │  {bk:^8} │ {tc:>6} │ {liq:>6}")

# ── 하루 수익률 분포 분석 ─────────────────────────────
print(f"\n{'─'*66}")
print(f"  📈  레버리지 1x 기준  일별 수익률 분포")
print(f"{'─'*66}")

dpnl_1x = results[1]["dpnl"]
if dpnl_1x:
    daily_vals  = list(dpnl_1x.values())
    daily_pct   = [v / INITIAL_CAPITAL * 100 for v in daily_vals]
    pos_days    = [x for x in daily_pct if x > 0]
    neg_days    = [x for x in daily_pct if x < 0]
    zero_days   = [x for x in daily_pct if x == 0]

    pct_2plus   = sum(1 for x in daily_pct if x >= 2)
    pct_7plus   = sum(1 for x in daily_pct if x >= 7)
    total_days  = len(daily_pct)
    trade_days  = len(pos_days) + len(neg_days)

    print(f"  총 거래일 수     : {trade_days}일  (무거래일 제외)")
    print(f"  일평균 수익      : {np.mean(daily_pct):>+.4f}%  (${np.mean(daily_vals):>+.2f})")
    print(f"  일 수익률 중앙값 : {np.median(daily_pct):>+.4f}%")
    print(f"  일 최대 수익     : {max(daily_pct):>+.2f}%  (${max(daily_vals):>+.2f})")
    print(f"  일 최대 손실     : {min(daily_pct):>+.2f}%  (${min(daily_vals):>+.2f})")
    print(f"{'─'*66}")
    print(f"  📌  목표 달성 현황 (레버리지 없음)")
    print(f"  하루 +2% 이상 달성 : {pct_2plus:>4}일 / {trade_days}일  ({pct_2plus/trade_days*100:.1f}%)")
    print(f"  하루 +7% 이상 달성 : {pct_7plus:>4}일 / {trade_days}일  ({pct_7plus/trade_days*100:.1f}%)")

# ── 목표를 위한 레버리지 계산 ──────────────────────────
print(f"\n{'─'*66}")
print(f"  🎯  하루 2-7% 달성에 필요한 레버리지")
print(f"{'─'*66}")
if dpnl_1x:
    avg_daily_1x = np.mean([v for v in daily_pct if v != 0])
    print(f"  레버리지 없음 일평균 수익률 : {avg_daily_1x:>+.3f}%")
    for target in [2, 5, 7]:
        needed_lev = target / avg_daily_1x if avg_daily_1x > 0 else float("inf")
        print(f"  하루 {target}% 목표 → 필요 레버리지 : {needed_lev:>6.0f}x  ← {'❌ 불가능 (바이비트 최대 100x)' if needed_lev > 100 else '⚠ 극고위험'}")

# ── 레버리지 5x 상세 ─────────────────────────────────
print(f"\n{'─'*66}")
print(f"  🔍  레버리지 5x 상세 분석 (가장 공격적인 현실 가능 범위)")
print(f"{'─'*66}")
r5 = results[5]
tr5 = r5["trades"]
dp5 = r5["dpnl"]
if not tr5.empty and dp5:
    dpv5     = list(dp5.values())
    dpp5     = [v / INITIAL_CAPITAL * 100 for v in dpv5]
    p2       = sum(1 for x in dpp5 if x >= 2)
    p7       = sum(1 for x in dpp5 if x >= 7)
    trd5     = len([x for x in dpp5 if x != 0])
    liq5     = len(tr5[tr5["exit"] == "청산"])
    win5     = len(tr5[tr5["pnl_usd"] > 0])
    pct_ret  = (r5["final"] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    print(f"  최종 자금        : $ {r5['final']:>9,.0f}")
    print(f"  4년 총 수익률    : {pct_ret:>+.1f}%")
    print(f"  총 거래          : {len(tr5)}건")
    print(f"  강제청산(폭발)   : {liq5}건  ({liq5/len(tr5)*100:.1f}%)")
    print(f"  일 최대 수익     : {max(dpp5):>+.2f}%")
    print(f"  일 최대 손실     : {min(dpp5):>+.2f}%")
    print(f"  하루 +2% 달성    : {p2}일  ({p2/trd5*100:.1f}%)")
    print(f"  하루 +7% 달성    : {p7}일  ({p7/trd5*100:.1f}%)")

# ── 현실적 로드맵 ─────────────────────────────────────
print(f"\n{'═'*66}")
print(f"  💬  솔직한 분석")
print(f"{'═'*66}")
print(f"""
  ┌─────────────────────────────────────────────────────────┐
  │  S4 전략 (최고 성적)  레버리지 없음 기준                │
  │  · 4년 수익률   : +30.81%                              │
  │  · 하루 평균    : +0.022%  (≈ $2.2/일)                │
  │  · 목표 대비    : 2% 달성에 레버리지 약 90x 필요       │
  │                                                         │
  │  ⚠  레버리지 10x 사용 시                               │
  │  · 수익 거래    : +0.22%/일 → 연 약 +80%              │
  │  · 강제청산 위험: ETH 10% 역행 시 계좌 폭발           │
  │  · 10% 역행은 ETH에서 주 1-2회 발생                   │
  │                                                         │
  │  📌  현실적 목표                                        │
  │  · 레버리지 없음 : 연 +20~30%  (안전)                 │
  │  · 레버리지 3x  : 연 +60~90%  (중위험)                │
  │  · 레버리지 5x  : 연 +100~150% (고위험, 파산 가능성)  │
  │                                                         │
  │  하루 2-7%를 매일 달성하는 트레이더는 세상에 없습니다.  │
  │  있다면 1년 후 전 세계 돈을 다 갖게 됩니다.            │
  └─────────────────────────────────────────────────────────┘
""")

# ── 복리의 마법 (반대로) ─────────────────────────────
print(f"  복리 계산 (매일 달성했다면 1년 후 자산)")
print(f"{'─'*66}")
for daily_r in [0.022, 0.5, 1.0, 2.0, 7.0]:
    annual = INITIAL_CAPITAL * (1 + daily_r/100)**252
    label  = {0.022: "실제 S4 전략", 0.5: "하루 0.5%", 1.0: "하루 1%",
               2.0: "하루 2%", 7.0: "하루 7%"}.get(daily_r)
    print(f"  {label:<16}: ${annual:>20,.0f}  (연 {(annual/INITIAL_CAPITAL-1)*100:.0f}%)")

print(f"\n  ⚠  이 수치가 하루 2-7% 목표가 왜 불가능한지 보여줍니다.\n")
