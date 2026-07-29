"""
ETH/USDT  시간봉(1H)  볼린저밴드 + RSI  백테스트
- 자금: $10,000  |  거래금액: 잔고의 3%
- 진입: BB 하단 터치 + RSI 과매도 → 롱  /  BB 상단 터치 + RSI 과매수 → 숏
- 청산 A: 반대편 밴드 도달
- 청산 B: 볼린저 중앙선(20SMA) 도달
- 손절: 진입가 대비 -3%
"""

import pandas as pd
import numpy as np
import time
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════
#  파라미터
# ═══════════════════════════════════════════════════════
INITIAL_CAPITAL = 10_000
TRADE_PCT       = 0.03
BB_PERIOD       = 20       # 20시간
BB_STD          = 2.0
RSI_PERIOD      = 14       # 14시간
RSI_UPPER       = 65
RSI_LOWER       = 35
STOP_LOSS_PCT   = 0.03     # 손절 -3% (시간봉 기준)
START           = "2021-01-01"
END             = "2024-12-31"
BARS_PER_DAY    = 24       # 시간봉
# ═══════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────
#  실제 ETH 주요 가격 앵커
# ───────────────────────────────────────────────────────
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
#  시간봉 데이터 생성
# ───────────────────────────────────────────────────────
def generate_hourly_data() -> pd.DataFrame:
    daily_dates  = pd.date_range(START, END, freq="D")
    anchor_dates = pd.to_datetime([a[0] for a in ANCHORS])
    anchor_px    = [a[1] for a in ANCHORS]

    daily_target = (pd.Series(anchor_px, index=anchor_dates)
                    .reindex(daily_dates)
                    .interpolate(method="time")
                    .values)

    np.random.seed(2024)
    SIGMA_1H    = 0.012    # ETH 시간봉 평균 변동성 ≈ 1.2%
    MR_STRENGTH = 0.15

    n_days  = len(daily_dates)
    n_bars  = n_days * BARS_PER_DAY
    all_cls = np.empty(n_bars, dtype=np.float64)

    prev_close = daily_target[0]
    for d in range(n_days):
        target  = daily_target[d]
        noise   = np.random.normal(0, SIGMA_1H, BARS_PER_DAY)
        log_mr  = np.log(target / prev_close) / BARS_PER_DAY * MR_STRENGTH
        prices  = prev_close * np.exp(np.cumsum(log_mr + noise))
        idx     = d * BARS_PER_DAY
        all_cls[idx:idx + BARS_PER_DAY] = prices
        prev_close = prices[-1]

    all_times = pd.date_range(START, periods=n_bars, freq="1h")
    rng       = all_cls * np.abs(np.random.normal(0.006, 0.003, n_bars))

    df = pd.DataFrame({
        "Open"  : np.concatenate([[all_cls[0]], all_cls[:-1]]),
        "High"  : all_cls + rng * 0.55,
        "Low"   : all_cls - rng * 0.55,
        "Close" : all_cls,
    }, index=all_times)
    return df


# ───────────────────────────────────────────────────────
#  지표 계산
# ───────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c     = df["Close"]
    sma   = c.rolling(BB_PERIOD).mean()
    sigma = c.rolling(BB_PERIOD).std()
    df["bb_upper"] = sma + BB_STD * sigma
    df["bb_mid"]   = sma
    df["bb_lower"] = sma - BB_STD * sigma
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


# ───────────────────────────────────────────────────────
#  백테스트 엔진
# ───────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame, exit_mode: str) -> dict:
    df2 = df.dropna(subset=["bb_upper", "rsi"])
    cls  = df2["Close"].to_numpy()
    up   = df2["bb_upper"].to_numpy()
    mid  = df2["bb_mid"].to_numpy()
    low  = df2["bb_lower"].to_numpy()
    rsi  = df2["rsi"].to_numpy()
    dts  = df2.index.to_numpy()

    capital = float(INITIAL_CAPITAL)
    pos     = None
    trades  = []
    eq_dates, eq_vals = [], []

    for i in range(len(cls)):
        price = cls[i]

        # 일별 equity (매 24봉)
        if i % BARS_PER_DAY == 0:
            if pos:
                s, ep, sz, _ = pos
                eq_vals.append(capital + (price - ep) / ep * s * sz)
            else:
                eq_vals.append(capital)
            eq_dates.append(dts[i])

        if pos is None:
            if price <= low[i] and rsi[i] < RSI_LOWER:
                pos = (1, price, capital * TRADE_PCT, dts[i])
            elif price >= up[i] and rsi[i] > RSI_UPPER:
                pos = (-1, price, capital * TRADE_PCT, dts[i])
        else:
            s, ep, sz, edt = pos
            cur_ret = (price - ep) / ep * s
            stop_hit = cur_ret <= -STOP_LOSS_PCT
            if exit_mode == "opposite_band":
                tgt = (s == 1 and price >= up[i]) or (s == -1 and price <= low[i])
            else:
                tgt = (s == 1 and price >= mid[i]) or (s == -1 and price <= mid[i])

            if stop_hit or tgt:
                pnl      = cur_ret * sz
                capital += pnl
                hold_h   = int((dts[i] - edt) / np.timedelta64(1, "h"))
                trades.append({
                    "entry_date" : edt,
                    "exit_date"  : dts[i],
                    "side"       : "long" if s == 1 else "short",
                    "entry_price": ep,
                    "exit_price" : price,
                    "size_usd"   : sz,
                    "pnl_usd"   : pnl,
                    "pnl_pct"   : cur_ret * 100,
                    "exit_type" : "손절" if stop_hit else "목표",
                    "hold_h"    : hold_h,
                })
                pos = None

    if pos:
        s, ep, sz, edt = pos
        price   = cls[-1]
        cur_ret = (price - ep) / ep * s
        pnl     = cur_ret * sz
        capital += pnl
        trades.append({
            "entry_date" : edt, "exit_date": dts[-1],
            "side"       : "long" if s == 1 else "short",
            "entry_price": ep,  "exit_price": price,
            "size_usd"   : sz,  "pnl_usd": pnl,
            "pnl_pct"   : cur_ret * 100,
            "exit_type" : "강제청산",
            "hold_h"    : int((dts[-1] - edt) / np.timedelta64(1, "h")),
        })

    eq = pd.Series(eq_vals, index=pd.DatetimeIndex(eq_dates), name="equity")
    return {
        "final_capital"    : capital,
        "total_return_pct" : (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100,
        "trades"           : pd.DataFrame(trades),
        "equity"           : eq,
    }


# ───────────────────────────────────────────────────────
#  출력 유틸
# ───────────────────────────────────────────────────────
def mdd(eq):
    return ((eq - eq.cummax()) / eq.cummax()).min() * 100

def sharpe(eq):
    r = eq.pct_change().dropna()
    return r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0


def print_result(label, res):
    t   = res["trades"]
    eq  = res["equity"]
    bar = "═" * 62

    print(f"\n{bar}")
    print(f"  【{label}】")
    print(f"{bar}")
    if t.empty:
        print("  거래 신호 없음"); return

    total  = len(t)
    wins   = t[t["pnl_usd"] > 0]
    losses = t[t["pnl_usd"] <= 0]
    stops  = t[t["exit_type"] == "손절"]
    wr     = len(wins) / total * 100
    pf     = (wins["pnl_usd"].sum() / abs(losses["pnl_usd"].sum())
              if len(losses) and losses["pnl_usd"].sum() != 0 else float("inf"))

    print(f"  기간           : {START}  →  {END}  (시간봉)")
    print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
    print(f"  최종 자금      : $ {res['final_capital']:>10,.2f}")
    print(f"  총 수익률      :  {res['total_return_pct']:>+9.2f} %")
    print(f"  최대 낙폭(MDD) :  {mdd(eq):>9.2f} %")
    print(f"  샤프 비율      :  {sharpe(eq):>9.2f}")
    print(f"{'─'*62}")
    print(f"  총 거래 수     : {total:>6} 건")
    print(f"  롱 거래        : {len(t[t['side']=='long']):>6} 건")
    print(f"  숏 거래        : {len(t[t['side']=='short']):>6} 건")
    print(f"  손절 청산      : {len(stops):>6} 건   ({len(stops)/total*100:.1f}%)")
    print(f"  목표 청산      : {total-len(stops):>6} 건   ({(total-len(stops))/total*100:.1f}%)")
    print(f"  승률           :  {wr:>8.1f} %")
    print(f"  손익비(PF)     :  {pf:>8.2f}")
    print(f"  평균 보유시간  :  {t['hold_h'].mean():>7.1f} 시간  ({t['hold_h'].mean()/24:.1f}일)")
    if len(wins):
        print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+8.2f}")
    if len(losses):
        print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+8.2f}")
    print(f"  최대 단일 수익 : $ {t['pnl_usd'].max():>+8.2f}")
    print(f"  최대 단일 손실 : $ {t['pnl_usd'].min():>+8.2f}")
    print(f"{'─'*62}")
    print(f"  최근 거래 10건:")
    for _, r in t.tail(10).iterrows():
        icon = "✅" if r["pnl_usd"] > 0 else "❌"
        side = "롱" if r["side"] == "long" else "숏"
        edt  = pd.Timestamp(r["entry_date"]).strftime("%Y-%m-%d %H:%M")
        print(f"    {icon} {edt} {side}"
              f"  ${r['entry_price']:>5.0f}→${r['exit_price']:>5.0f}"
              f"  {r['hold_h']:>4}시간"
              f"  PnL:${r['pnl_usd']:>+7.2f} [{r['exit_type']}]")


def yearly_monthly_table(res_a, res_b):
    print(f"\n{'═'*62}")
    print("  📅  연도별 순손익")
    print(f"{'─'*62}")
    print(f"  {'연도':^6}  |  {'전략A (반대밴드)':^20}  |  {'전략B (중앙선)':^20}")
    print(f"  {'─'*6}─┼─{'─'*20}─┼─{'─'*20}")
    for yr in [2021, 2022, 2023, 2024]:
        for mode, res in [("A", res_a), ("B", res_b)]:
            t = res["trades"]
            val = t[pd.to_datetime(t["entry_date"]).dt.year == yr]["pnl_usd"].sum() if not t.empty else 0.0
            if mode == "A": va = val
            else:           vb = val
        ia = "▲" if va >= 0 else "▼"
        ib = "▲" if vb >= 0 else "▼"
        print(f"  {yr}  |  {ia} ${va:>+10,.2f}              |  {ib} ${vb:>+10,.2f}")

    # 월별 상세 (전략A 기준, 2023·2024)
    for yr in [2023, 2024]:
        print(f"\n{'─'*62}")
        print(f"  📆  {yr}년 월별 손익  (전략A | 전략B)")
        print(f"{'─'*62}")
        t_a = res_a["trades"]
        t_b = res_b["trades"]
        ta_yr = t_a[pd.to_datetime(t_a["entry_date"]).dt.year == yr].copy() if not t_a.empty else pd.DataFrame()
        tb_yr = t_b[pd.to_datetime(t_b["entry_date"]).dt.year == yr].copy() if not t_b.empty else pd.DataFrame()
        if not ta_yr.empty: ta_yr["month"] = pd.to_datetime(ta_yr["entry_date"]).dt.month
        if not tb_yr.empty: tb_yr["month"] = pd.to_datetime(tb_yr["entry_date"]).dt.month

        for m in range(1, 13):
            pa = ta_yr[ta_yr["month"] == m]["pnl_usd"].sum() if not ta_yr.empty else 0.0
            pb = tb_yr[tb_yr["month"] == m]["pnl_usd"].sum() if not tb_yr.empty else 0.0
            na = len(ta_yr[ta_yr["month"] == m]) if not ta_yr.empty else 0
            nb = len(tb_yr[tb_yr["month"] == m]) if not tb_yr.empty else 0
            ia = "▲" if pa >= 0 else "▼"
            ib = "▲" if pb >= 0 else "▼"
            bar_a = "█" * min(int(abs(pa) / 5 + 0.5), 15)
            bar_b = "█" * min(int(abs(pb) / 5 + 0.5), 15)
            print(f"  {m:>2}월  A:{ia}${pa:>+7.2f}({na:>3}건){bar_a:<15}  "
                  f"B:{ib}${pb:>+7.2f}({nb:>3}건){bar_b}")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════
print("=" * 62)
print("   ETH/USDT  시간봉(1H)  볼린저밴드 + RSI  백테스트")
print("=" * 62)

t0 = time.time()
print(f"\n  📥 시간봉 데이터 생성 중  ({START} ~ {END})...")
df = generate_hourly_data()
print(f"  ✅ {len(df):,}개 캔들 생성  ({time.time()-t0:.1f}초)")

t1 = time.time()
print(f"  📐 지표 계산 중  (BB{BB_PERIOD}H, RSI{RSI_PERIOD}H)...")
df = add_indicators(df)
print(f"  ✅ 완료  ({time.time()-t1:.1f}초)")
print(f"  ETH 가격 범위: ${df['Close'].min():.0f}  ~  ${df['Close'].max():.0f}")

print(f"\n{'─'*62}")
print(f"  ⚙  전략 파라미터")
print(f"{'─'*62}")
print(f"  볼린저밴드  : {BB_PERIOD}시간 SMA ± {BB_STD}σ")
print(f"  RSI         : {RSI_PERIOD}시간,  과매도 < {RSI_LOWER}  /  과매수 > {RSI_UPPER}")
print(f"  진입        : 하단 터치 + RSI<{RSI_LOWER} → 롱  |  상단 터치 + RSI>{RSI_UPPER} → 숏")
print(f"  손절        : 진입가 -{STOP_LOSS_PCT*100:.0f}%")
print(f"  거래 금액   : 잔고의 {TRADE_PCT*100:.0f}%  (초기 ${INITIAL_CAPITAL*TRADE_PCT:,.0f})")

t2 = time.time()
print(f"\n  🔄 백테스트 실행 중...")
res_a = run_backtest(df, "opposite_band")
res_b = run_backtest(df, "middle_band")
print(f"  ✅ 완료  ({time.time()-t2:.1f}초)")

print_result("전략 A  —  반대편 밴드 도달 시 청산", res_a)
print_result("전략 B  —  볼린저 중앙선 도달 시 청산", res_b)

yearly_monthly_table(res_a, res_b)

# 타임프레임 비교 요약
print(f"\n{'═'*62}")
print("  📊  최종 요약  (타임프레임 비교)")
print(f"{'─'*62}")
print(f"  {'':20}  {'전략A':>10}  {'전략B':>10}  {'거래수A':>8}  {'거래수B':>8}")
print(f"  {'─'*20}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*8}")

# 이번(시간봉)
pct_a = res_a["total_return_pct"]
pct_b = res_b["total_return_pct"]
ia    = "▲" if pct_a >= 0 else "▼"
ib    = "▲" if pct_b >= 0 else "▼"
print(f"  {'시간봉 (1H)':20}  {ia}{pct_a:>+8.2f}%  {ib}{pct_b:>+8.2f}%"
      f"  {len(res_a['trades']):>8,}  {len(res_b['trades']):>8,}")
print(f"{'─'*62}")
print(f"  참고 (1분봉):  전략A -3.27%  |  전략B -2.85%"
      f"  (28,735건 / 43,626건)")
print(f"  참고 (일봉) :  전략A -7.81%  |  전략B -7.78%"
      f"  (49건 / 56건)")
print(f"{'═'*62}")
print(f"\n  총 실행 시간: {time.time()-t0:.1f}초")
print(f"  ⚠  시뮬레이션 기반 / 수수료·슬리피지 미반영\n")
