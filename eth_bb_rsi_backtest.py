"""
ETH/USDT  1분봉  볼린저밴드 + RSI  백테스트
- 자금: $10,000  |  거래금액: 잔고의 3%
- 진입: BB 하단 터치 + RSI 과매도 → 롱  /  BB 상단 터치 + RSI 과매수 → 숏
- 청산 A: 반대편 밴드 도달
- 청산 B: 볼린저 중앙선(20SMA) 도달
- 손절: 진입가 대비 -1.5%
"""

import pandas as pd
import numpy as np
import time
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════
#  파라미터
# ═══════════════════════════════════════════════════════
INITIAL_CAPITAL = 10_000   # 초기 자금 (USD)
TRADE_PCT       = 0.03     # 거래금액 = 잔고의 3%
BB_PERIOD       = 20       # 볼린저밴드 기간 (20분)
BB_STD          = 2.0      # 표준편차 배수
RSI_PERIOD      = 14       # RSI 기간 (14분)
RSI_UPPER       = 65       # 과매수 (숏 진입)
RSI_LOWER       = 35       # 과매도 (롱 진입)
STOP_LOSS_PCT   = 0.015    # 손절: -1.5% (1분봉 기준)
START           = "2022-01-01"
END             = "2024-12-31"
# ═══════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────
#  실제 ETH 주요 가격 앵커 (2022~2024)
# ───────────────────────────────────────────────────────
ANCHORS = [
    ("2022-01-01", 3700), ("2022-01-22", 2200), ("2022-03-28", 3290),
    ("2022-05-01", 2680), ("2022-05-12", 1900), ("2022-06-13",  900),
    ("2022-08-13", 1960), ("2022-09-15", 1500), ("2022-09-30", 1310),
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
#  1분봉 데이터 생성
# ───────────────────────────────────────────────────────
def generate_1min_data() -> pd.DataFrame:
    daily_dates  = pd.date_range(START, END, freq="D")
    anchor_dates = pd.to_datetime([a[0] for a in ANCHORS])
    anchor_px    = [a[1] for a in ANCHORS]

    # 일별 목표 종가 (앵커 선형 보간)
    daily_target = (pd.Series(anchor_px, index=anchor_dates)
                    .reindex(daily_dates)
                    .interpolate(method="time")
                    .values)

    np.random.seed(2024)
    SIGMA_1MIN = 0.0013      # ETH 1분봉 평균 변동성 ≈ 0.13%
    MR_STRENGTH = 0.08       # 일 목표 방향으로 평균회귀 강도

    n_days  = len(daily_dates)
    all_cls = np.empty(n_days * 1440, dtype=np.float64)
    all_hi  = np.empty(n_days * 1440, dtype=np.float64)
    all_lo  = np.empty(n_days * 1440, dtype=np.float64)

    prev_close = daily_target[0]

    for d in range(n_days):
        target  = daily_target[d]
        noise   = np.random.normal(0, SIGMA_1MIN, 1440)
        # 일 목표가로 당기는 평균회귀 drift
        log_mr  = np.log(target / prev_close) / 1440 * MR_STRENGTH
        log_ret = log_mr + noise
        prices  = prev_close * np.exp(np.cumsum(log_ret))

        rng     = prices * np.abs(np.random.normal(0.0008, 0.0004, 1440))
        idx     = d * 1440
        all_cls[idx:idx+1440] = prices
        all_hi [idx:idx+1440] = prices + rng * 0.55
        all_lo [idx:idx+1440] = prices - rng * 0.55
        prev_close = prices[-1]

    all_times = pd.date_range(START, periods=n_days * 1440, freq="1min")

    df = pd.DataFrame({
        "Open"  : np.concatenate([[all_cls[0]], all_cls[:-1]]),
        "High"  : all_hi,
        "Low"   : all_lo,
        "Close" : all_cls,
    }, index=all_times)
    return df


# ───────────────────────────────────────────────────────
#  지표 계산 (pandas rolling, C 레벨 속도)
# ───────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
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
#  백테스트 엔진 (numpy 배열 직접 접근으로 속도 최적화)
# ───────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame, exit_mode: str) -> dict:
    df2 = df.dropna(subset=["bb_upper", "rsi"])

    cls  = df2["Close"].to_numpy()
    up   = df2["bb_upper"].to_numpy()
    mid  = df2["bb_mid"].to_numpy()
    low  = df2["bb_lower"].to_numpy()
    rsi  = df2["rsi"].to_numpy()
    dts  = df2.index.to_numpy()

    capital   = float(INITIAL_CAPITAL)
    pos       = None          # None | (side:+1/-1, entry_px, size_usd, entry_dt)
    trades    = []

    # equity 샘플: 매 1440봉(≈1일)마다 기록 (메모리 절약)
    equity_dates  = []
    equity_values = []

    n = len(cls)
    for i in range(n):
        price = cls[i]

        # 일별 equity 스냅샷 (i % 1440 == 0)
        if i % 1440 == 0:
            if pos is not None:
                s, ep, sz, _ = pos
                unr = (price - ep) / ep * s * sz
                equity_values.append(capital + unr)
            else:
                equity_values.append(capital)
            equity_dates.append(dts[i])

        if pos is None:
            # 진입
            if price <= low[i] and rsi[i] < RSI_LOWER:
                sz  = capital * TRADE_PCT
                pos = (1, price, sz, dts[i])
            elif price >= up[i] and rsi[i] > RSI_UPPER:
                sz  = capital * TRADE_PCT
                pos = (-1, price, sz, dts[i])
        else:
            s, ep, sz, edt = pos
            cur_ret = (price - ep) / ep * s

            stop_hit = cur_ret <= -STOP_LOSS_PCT

            if exit_mode == "opposite_band":
                tgt_hit = (s == 1  and price >= up[i]) or \
                          (s == -1 and price <= low[i])
            else:
                tgt_hit = (s == 1  and price >= mid[i]) or \
                          (s == -1 and price <= mid[i])

            if stop_hit or tgt_hit:
                pnl     = cur_ret * sz
                capital += pnl
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
                    "hold_min"  : int((dts[i] - edt) / np.timedelta64(1, "m")),
                })
                pos = None

    # 미결제 강제 청산
    if pos is not None:
        s, ep, sz, edt = pos
        price   = cls[-1]
        cur_ret = (price - ep) / ep * s
        pnl     = cur_ret * sz
        capital += pnl
        trades.append({
            "entry_date" : edt,
            "exit_date"  : dts[-1],
            "side"       : "long" if s == 1 else "short",
            "entry_price": ep,
            "exit_price" : price,
            "size_usd"   : sz,
            "pnl_usd"   : pnl,
            "pnl_pct"   : cur_ret * 100,
            "exit_type" : "강제청산",
            "hold_min"  : int((dts[-1] - edt) / np.timedelta64(1, "m")),
        })

    eq = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates), name="equity")
    return {
        "final_capital"    : capital,
        "total_return_pct" : (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100,
        "trades"           : pd.DataFrame(trades),
        "equity"           : eq,
    }


# ───────────────────────────────────────────────────────
#  결과 출력
# ───────────────────────────────────────────────────────
def max_drawdown(eq: pd.Series) -> float:
    return ((eq - eq.cummax()) / eq.cummax()).min() * 100


def sharpe(eq: pd.Series) -> float:
    r = eq.pct_change().dropna()
    return (r.mean() / r.std() * np.sqrt(252)) if r.std() else 0.0


def print_result(label: str, res: dict):
    t   = res["trades"]
    eq  = res["equity"]
    bar = "═" * 60

    print(f"\n{bar}")
    print(f"  【{label}】")
    print(f"{bar}")

    if t.empty:
        print("  거래 신호 없음"); return

    total   = len(t)
    wins    = t[t["pnl_usd"] > 0]
    losses  = t[t["pnl_usd"] <= 0]
    stops   = t[t["exit_type"] == "손절"]
    wr      = len(wins) / total * 100
    pf      = (wins["pnl_usd"].sum() / abs(losses["pnl_usd"].sum())
               if len(losses) and losses["pnl_usd"].sum() != 0 else float("inf"))
    avg_hold= t["hold_min"].mean()
    longs   = t[t["side"] == "long"]
    shorts  = t[t["side"] == "short"]

    print(f"  기간           : {START}  →  {END}  (1분봉)")
    print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
    print(f"  최종 자금      : $ {res['final_capital']:>10,.2f}")
    print(f"  총 수익률      :  {res['total_return_pct']:>+9.2f} %")
    print(f"  최대 낙폭(MDD) :  {max_drawdown(eq):>9.2f} %")
    print(f"  샤프 비율      :  {sharpe(eq):>9.2f}")
    print(f"{'─'*60}")
    print(f"  총 거래 수     : {total:>6} 건")
    print(f"  롱 거래        : {len(longs):>6} 건   (승 {len(longs[longs['pnl_usd']>0])}건)")
    print(f"  숏 거래        : {len(shorts):>6} 건   (승 {len(shorts[shorts['pnl_usd']>0])}건)")
    print(f"  손절 청산      : {len(stops):>6} 건   ({len(stops)/total*100:.1f}%)")
    print(f"  목표 청산      : {total-len(stops):>6} 건   ({(total-len(stops))/total*100:.1f}%)")
    print(f"  승률           :  {wr:>8.1f} %")
    print(f"  손익비(PF)     :  {pf:>8.2f}")
    print(f"  평균 보유시간  :  {avg_hold:>7.1f} 분  ({avg_hold/60:.1f}시간)")
    if len(wins):
        print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+8.2f}")
    if len(losses):
        print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+8.2f}")
    print(f"  최대 단일 수익 : $ {t['pnl_usd'].max():>+8.2f}")
    print(f"  최대 단일 손실 : $ {t['pnl_usd'].min():>+8.2f}")
    print(f"{'─'*60}")
    print(f"  최근 거래 10건:")
    for _, r in t.tail(10).iterrows():
        icon = "✅" if r["pnl_usd"] > 0 else "❌"
        side = "롱" if r["side"] == "long" else "숏"
        edt  = pd.Timestamp(r["entry_date"]).strftime("%m-%d %H:%M")
        print(f"    {icon} {edt} {side}"
              f"  진입${r['entry_price']:>6.0f}→${r['exit_price']:>6.0f}"
              f"  {r['hold_min']:>4}분"
              f"  PnL: ${r['pnl_usd']:>+7.2f} [{r['exit_type']}]")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════
print("=" * 60)
print("   ETH/USDT  1분봉  볼린저밴드 + RSI  백테스트")
print("=" * 60)

t0 = time.time()
print(f"\n  📥 1분봉 데이터 생성 중  ({START} ~ {END})...")
df = generate_1min_data()
print(f"  ✅ {len(df):,}개 캔들 생성  ({time.time()-t0:.1f}초)")

t1 = time.time()
print(f"\n  📐 지표 계산 중  (BB{BB_PERIOD}, RSI{RSI_PERIOD})...")
df = add_indicators(df)
print(f"  ✅ 완료  ({time.time()-t1:.1f}초)")

print(f"\n  ETH 가격 범위: ${df['Close'].min():.0f}  ~  ${df['Close'].max():.0f}")
print(f"\n{'─'*60}")
print(f"  ⚙  전략 파라미터")
print(f"{'─'*60}")
print(f"  볼린저밴드  : {BB_PERIOD}분 SMA ± {BB_STD}σ")
print(f"  RSI         : {RSI_PERIOD}분,  과매도 < {RSI_LOWER}  /  과매수 > {RSI_UPPER}")
print(f"  진입        : 하단 터치 + RSI<{RSI_LOWER} → 롱  |  상단 터치 + RSI>{RSI_UPPER} → 숏")
print(f"  손절        : 진입가 -{STOP_LOSS_PCT*100:.1f}%")
print(f"  거래 금액   : 잔고의 {TRADE_PCT*100:.0f}%")

t2 = time.time()
print(f"\n  🔄 백테스트 실행 중...")
res_a = run_backtest(df, "opposite_band")
res_b = run_backtest(df, "middle_band")
print(f"  ✅ 완료  ({time.time()-t2:.1f}초)")

print_result("전략 A  —  반대편 밴드 도달 시 청산", res_a)
print_result("전략 B  —  볼린저 중앙선 도달 시 청산", res_b)

# 연도별 비교
print(f"\n{'═'*60}")
print("  📅  연도별 순손익")
print(f"{'─'*60}")
print(f"  {'연도':^6}  |  {'전략A':^18}  |  {'전략B':^18}")
print(f"  {'─'*6}─┼─{'─'*18}─┼─{'─'*18}")
for yr in [2022, 2023, 2024]:
    for mode, res in [("A", res_a), ("B", res_b)]:
        t = res["trades"]
        if t.empty:
            val = 0.0
        else:
            val = t[pd.to_datetime(t["entry_date"]).dt.year == yr]["pnl_usd"].sum()
        if mode == "A": va = val
        else:           vb = val
    ia = "▲" if va >= 0 else "▼"
    ib = "▲" if vb >= 0 else "▼"
    print(f"  {yr}  |  {ia} ${va:>+10,.2f}          |  {ib} ${vb:>+10,.2f}")

# 월별 상세 (2023년 예시)
print(f"\n{'─'*60}")
print("  📆  2023년 월별 손익 (전략A 기준)")
print(f"{'─'*60}")
t = res_a["trades"]
if not t.empty:
    t23 = t[pd.to_datetime(t["entry_date"]).dt.year == 2023].copy()
    t23["month"] = pd.to_datetime(t23["entry_date"]).dt.month
    for m in range(1, 13):
        m_t   = t23[t23["month"] == m]
        m_pnl = m_t["pnl_usd"].sum()
        m_cnt = len(m_t)
        bar   = "█" * int(abs(m_pnl) / 5 + 1) if m_cnt else ""
        sign  = "+" if m_pnl >= 0 else ""
        color = "▲" if m_pnl >= 0 else "▼"
        print(f"  {m:>2}월  {color} ${m_pnl:>+7.2f}  ({m_cnt:>3}건)  {bar}")

# 최종 요약
print(f"\n{'═'*60}")
print("  📊  최종 요약")
print(f"{'─'*60}")
for label, res in [("전략A (반대밴드)", res_a), ("전략B (중앙선) ", res_b)]:
    pct  = res["total_return_pct"]
    icon = "▲" if pct >= 0 else "▼"
    tc   = len(res["trades"])
    print(f"  {label}  :  {icon}  {pct:>+7.2f}%"
          f"  (${INITIAL_CAPITAL:,} → ${res['final_capital']:,.0f})  [{tc}건]")
print(f"{'═'*60}")
print(f"\n  총 실행 시간: {time.time()-t0:.1f}초")
print(f"\n  ⚠  시뮬레이션 기반 결과 / 실제 수수료·슬리피지 미반영\n")
