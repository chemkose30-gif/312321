"""
Ethereum Bollinger Bands + RSI Backtester
- 자금: $10,000  |  거래금액: 자금의 3%
- 진입: 볼린저밴드 상/하단 터치 + RSI 필터 → 반대매매
- 청산 A: 반대편 밴드 도달 시
- 청산 B: 볼린저 중앙선(SMA) 도달 시
- 손절: 진입가 대비 -8% (트렌딩 시장 보호)
"""

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════
#  파라미터
# ═══════════════════════════════════════════════════════
INITIAL_CAPITAL = 10_000   # 초기 자금 (USD)
TRADE_PCT       = 0.03     # 거래금액 = 잔고의 3%
BB_PERIOD       = 20       # 볼린저밴드 이동평균 기간
BB_STD          = 2.0      # 표준편차 배수
RSI_PERIOD      = 14       # RSI 기간
RSI_UPPER       = 65       # RSI 과매수 기준 (숏 진입)
RSI_LOWER       = 35       # RSI 과매도 기준 (롱 진입)
STOP_LOSS_PCT   = 0.08     # 손절 기준: 진입가 대비 -8%
START           = "2021-01-01"
END             = "2024-12-31"


# ═══════════════════════════════════════════════════════
#  ETH 역사 데이터 생성 (실제 주요 가격 기반)
# ═══════════════════════════════════════════════════════
def generate_eth_data() -> pd.DataFrame:
    """
    실제 ETH/USD 역사적 고점·저점을 앵커로 삼아
    GBM + 평균회귀 일봉 데이터 생성
    """
    # 실제 ETH 주요 이벤트 가격 (날짜, 종가)
    anchors = [
        ("2021-01-01",  730),
        ("2021-02-20", 1950),
        ("2021-05-12", 4080),
        ("2021-06-22", 1730),
        ("2021-07-21", 1790),
        ("2021-08-29", 3290),
        ("2021-09-07", 3900),
        ("2021-09-21", 2700),
        ("2021-11-10", 4860),
        ("2021-12-04", 3880),
        ("2021-12-31", 3680),
        ("2022-01-22", 2200),
        ("2022-03-28", 3290),
        ("2022-05-01", 2680),
        ("2022-05-12", 1900),
        ("2022-06-13",  900),
        ("2022-08-13", 1960),
        ("2022-09-15", 1500),
        ("2022-09-30", 1310),
        ("2022-11-09", 1100),
        ("2022-12-31", 1200),
        ("2023-01-14", 1540),
        ("2023-02-16", 1680),
        ("2023-04-14", 2100),
        ("2023-05-25", 1820),
        ("2023-06-10", 1660),
        ("2023-07-14", 1890),
        ("2023-08-17", 1570),
        ("2023-09-11", 1600),
        ("2023-10-23", 1790),
        ("2023-12-05", 2200),
        ("2023-12-31", 2280),
        ("2024-01-12", 2580),
        ("2024-02-29", 3400),
        ("2024-03-12", 4090),
        ("2024-04-01", 3500),
        ("2024-04-15", 2900),
        ("2024-05-23", 3780),
        ("2024-06-24", 3380),
        ("2024-07-05", 2870),
        ("2024-08-05", 2100),
        ("2024-09-13", 2340),
        ("2024-10-01", 2600),
        ("2024-11-12", 3380),
        ("2024-12-05", 3900),
        ("2024-12-16", 4000),
        ("2024-12-31", 3300),
    ]

    anchor_dates  = pd.to_datetime([a[0] for a in anchors])
    anchor_prices = [a[1] for a in anchors]

    all_dates = pd.date_range(START, END, freq="D")
    # 앵커 사이 선형 보간
    base = (pd.Series(anchor_prices, index=anchor_dates)
            .reindex(all_dates)
            .interpolate(method="time"))

    # 현실적 일간 노이즈 (ETH 일간 변동성 ≈ 4%)
    np.random.seed(2024)
    n = len(all_dates)
    daily_noise = np.random.normal(0, 0.04, n)
    # 노이즈를 누적하지 않고 하루 단위 곱으로 적용 → 발산 방지
    close = base.values.copy().astype(float)
    for i in range(1, n):
        shock  = daily_noise[i]
        # 앵커 방향으로 약한 평균회귀 (±5% 당김)
        mr     = (base.values[i] - close[i-1]) / close[i-1] * 0.10
        close[i] = close[i-1] * (1 + shock * 0.5 + mr)
        close[i] = max(close[i], 100)  # 음수 방지

    df            = pd.DataFrame(index=all_dates)
    df.index.name = "Date"
    df["Close"]   = close
    rng           = close * np.abs(np.random.normal(0.025, 0.012, n))
    df["High"]    = df["Close"] + rng * 0.55
    df["Low"]     = df["Close"] - rng * 0.55
    df["Open"]    = df["Close"].shift(1).fillna(df["Close"].iloc[0])
    df["Volume"]  = np.random.randint(4_000_000, 18_000_000, n).astype(float)
    return df


# ═══════════════════════════════════════════════════════
#  지표 계산
# ═══════════════════════════════════════════════════════
def calc_bb(close, period, std_mult):
    sma   = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return sma + std_mult * sigma, sma, sma - std_mult * sigma


def calc_rsi(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ═══════════════════════════════════════════════════════
#  백테스트 엔진
# ═══════════════════════════════════════════════════════
def run_backtest(df: pd.DataFrame, exit_mode: str) -> dict:
    """
    exit_mode: 'opposite_band' (반대편 밴드) | 'middle_band' (중앙선)
    """
    capital      = float(INITIAL_CAPITAL)
    position     = None
    trades       = []
    equity_curve = []

    for i in range(BB_PERIOD + RSI_PERIOD, len(df)):
        row   = df.iloc[i]
        price = float(row["Close"])
        upper = float(row["bb_upper"])
        mid   = float(row["bb_mid"])
        lower = float(row["bb_lower"])
        rsi   = float(row["rsi"])

        # 현재 미실현 손익 포함 자산
        if position:
            ep = position["entry"]
            sz = position["size_usd"]
            unr = (price - ep) / ep * sz if position["side"] == "long" \
                  else (ep - price) / ep * sz
            equity_curve.append({"date": row.name, "equity": capital + unr})
        else:
            equity_curve.append({"date": row.name, "equity": capital})

        if position is None:
            # ── 진입 신호 ────────────────────────────────
            if price <= lower and rsi < RSI_LOWER:
                sz = capital * TRADE_PCT
                position = {"side": "long",  "entry": price, "size_usd": sz,
                            "entry_date": row.name}

            elif price >= upper and rsi > RSI_UPPER:
                sz = capital * TRADE_PCT
                position = {"side": "short", "entry": price, "size_usd": sz,
                            "entry_date": row.name}

        else:
            ep   = position["entry"]
            sz   = position["size_usd"]
            side = position["side"]

            # 현재 수익률 계산
            cur_ret = (price - ep) / ep if side == "long" else (ep - price) / ep

            # 손절 체크 (-8%)
            stop_hit  = cur_ret <= -STOP_LOSS_PCT

            # 청산 목표 체크
            target_hit = False
            if exit_mode == "opposite_band":
                target_hit = (side == "long"  and price >= upper) or \
                             (side == "short" and price <= lower)
            else:  # middle_band
                target_hit = (side == "long"  and price >= mid) or \
                             (side == "short" and price <= mid)

            if stop_hit or target_hit:
                pnl = cur_ret * sz
                capital += pnl
                trades.append({
                    "entry_date" : position["entry_date"],
                    "exit_date"  : row.name,
                    "side"       : side,
                    "entry_price": ep,
                    "exit_price" : price,
                    "size_usd"   : sz,
                    "pnl_usd"   : pnl,
                    "pnl_pct"   : cur_ret * 100,
                    "exit_type" : "손절" if stop_hit else "목표",
                })
                position = None

    # 마지막 미결 포지션 강제 청산
    if position:
        price   = float(df["Close"].iloc[-1])
        ep, sz  = position["entry"], position["size_usd"]
        cur_ret = (price - ep) / ep if position["side"] == "long" else (ep - price) / ep
        pnl     = cur_ret * sz
        capital += pnl
        trades.append({
            "entry_date" : position["entry_date"],
            "exit_date"  : df.index[-1],
            "side"       : position["side"],
            "entry_price": ep,
            "exit_price" : price,
            "size_usd"   : sz,
            "pnl_usd"   : pnl,
            "pnl_pct"   : cur_ret * 100,
            "exit_type" : "강제청산",
        })

    eq_df = pd.DataFrame(equity_curve).set_index("date")
    return {
        "final_capital"    : capital,
        "total_return_pct" : (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100,
        "trades"           : pd.DataFrame(trades),
        "equity"           : eq_df,
    }


# ═══════════════════════════════════════════════════════
#  결과 출력
# ═══════════════════════════════════════════════════════
def max_drawdown(equity: pd.Series) -> float:
    roll_max  = equity.cummax()
    dd        = (equity - roll_max) / roll_max
    return dd.min() * 100


def sharpe(equity: pd.Series, rf=0.0) -> float:
    ret = equity.pct_change().dropna()
    if ret.std() == 0:
        return 0.0
    return (ret.mean() - rf / 252) / ret.std() * np.sqrt(252)


def print_result(label: str, result: dict):
    trades = result["trades"]
    eq     = result["equity"]["equity"]

    total = len(trades)
    bar = "═" * 58

    print(f"\n{bar}")
    print(f"  【{label}】")
    print(f"{bar}")

    if total == 0:
        print("  → 거래 신호 없음")
        return

    wins   = trades[trades["pnl_usd"] > 0]
    losses = trades[trades["pnl_usd"] <= 0]
    stops  = trades[trades["exit_type"] == "손절"]
    wr     = len(wins) / total * 100
    pf     = wins["pnl_usd"].sum() / abs(losses["pnl_usd"].sum()) \
             if len(losses) and losses["pnl_usd"].sum() != 0 else float("inf")

    avg_hold = (trades["exit_date"] - trades["entry_date"]).dt.days.mean()

    print(f"  기간           : {START}  →  {END}")
    print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
    print(f"  최종 자금      : $ {result['final_capital']:>10,.2f}")
    print(f"  총 수익률      :  {result['total_return_pct']:>+8.2f} %")
    print(f"  최대 낙폭(MDD) :  {max_drawdown(eq):>8.2f} %")
    print(f"  샤프 비율      :  {sharpe(eq):>8.2f}")
    print(f"{'─'*58}")
    print(f"  총 거래 수     : {total:>5} 건")
    print(f"  롱 거래        : {len(trades[trades['side']=='long']):>5} 건")
    print(f"  숏 거래        : {len(trades[trades['side']=='short']):>5} 건")
    print(f"  손절 청산      : {len(stops):>5} 건")
    print(f"  승률           : {wr:>7.1f} %")
    print(f"  손익비(PF)     : {pf:>8.2f}")
    print(f"  평균 보유 기간 : {avg_hold:>7.1f} 일")
    print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+8.2f}" if len(wins) else "  평균 수익(승)  : 없음")
    print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+8.2f}" if len(losses) else "  평균 손실(패)  : 없음")
    print(f"  최대 단일 수익 : $ {trades['pnl_usd'].max():>+8.2f}")
    print(f"  최대 단일 손실 : $ {trades['pnl_usd'].min():>+8.2f}")
    print(f"{'─'*58}")
    print(f"  최근 거래 내역 (최대 8건):")
    tail = trades.tail(8)
    for _, t in tail.iterrows():
        icon  = "✅" if t["pnl_usd"] > 0 else "❌"
        side  = "롱" if t["side"] == "long" else "숏"
        hold  = (t["exit_date"] - t["entry_date"]).days
        print(f"    {icon} {t['entry_date'].strftime('%Y-%m-%d')} {side:2}"
              f"  진입${t['entry_price']:>6.0f}→청산${t['exit_price']:>6.0f}"
              f"  ({hold:>3}일) | PnL: ${t['pnl_usd']:>+7.2f}  [{t['exit_type']}]")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════
print("=" * 58)
print("   ETH/USDT  볼린저밴드 + RSI  자동매매 백테스트")
print("=" * 58)
print(f"\n  📥 ETH 역사 데이터 생성 중 (실제 주요 가격 앵커 기반)...")

df = generate_eth_data()

# 지표 계산
df["bb_upper"], df["bb_mid"], df["bb_lower"] = calc_bb(df["Close"], BB_PERIOD, BB_STD)
df["rsi"] = calc_rsi(df["Close"], RSI_PERIOD)
df.dropna(inplace=True)

print(f"  ✅ {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}  ({len(df)}일)")
print(f"  ETH 가격 범위: ${df['Close'].min():.0f}  ~  ${df['Close'].max():.0f}")
print(f"\n{'─'*58}")
print(f"  ⚙  전략 파라미터")
print(f"{'─'*58}")
print(f"  볼린저밴드 : {BB_PERIOD}일 SMA ± {BB_STD}σ")
print(f"  RSI        : {RSI_PERIOD}일,  과매도 < {RSI_LOWER}  /  과매수 > {RSI_UPPER}")
print(f"  진입 조건  : 하단 터치(RSI<{RSI_LOWER}) → 롱  |  상단 터치(RSI>{RSI_UPPER}) → 숏")
print(f"  손절 기준  : 진입가 대비 -{STOP_LOSS_PCT*100:.0f}%")
print(f"  거래 금액  : 잔고의 {TRADE_PCT*100:.0f}%  (초기 ${INITIAL_CAPITAL*TRADE_PCT:.0f})")

# ── 백테스트 실행 ──────────────────────────────────────
result_a = run_backtest(df.copy(), exit_mode="opposite_band")
result_b = run_backtest(df.copy(), exit_mode="middle_band")

print_result("전략 A  —  반대편 밴드 도달 시 청산", result_a)
print_result("전략 B  —  볼린저 중앙선 도달 시 청산", result_b)

# ── 연도별 비교 ────────────────────────────────────────
print(f"\n{'═'*58}")
print("  📅  연도별 순손익 비교")
print(f"{'─'*58}")
print(f"  {'연도':^6}  |  {'전략A (반대밴드)':^18}  |  {'전략B (중앙선)':^18}")
print(f"  {'─'*6}─┼─{'─'*18}─┼─{'─'*18}")
for year in [2021, 2022, 2023, 2024]:
    for mode, res in [("A", result_a), ("B", result_b)]:
        t   = res["trades"]
        if t.empty:
            val = 0.0
        else:
            yt  = t[pd.to_datetime(t["entry_date"]).dt.year == year]
            val = yt["pnl_usd"].sum()
        if mode == "A":
            val_a = val
        else:
            val_b = val
    icon_a = "▲" if val_a >= 0 else "▼"
    icon_b = "▲" if val_b >= 0 else "▼"
    print(f"  {year}  |  {icon_a} ${val_a:>+10,.2f}          |  {icon_b} ${val_b:>+10,.2f}")

# ── 최종 요약 ─────────────────────────────────────────
print(f"\n{'═'*58}")
print("  📊  최종 요약")
print(f"{'─'*58}")
for label, res in [("전략A (반대밴드)", result_a), ("전략B (중앙선)", result_b)]:
    pct  = res["total_return_pct"]
    icon = "▲" if pct >= 0 else "▼"
    print(f"  {label:16}  :  {icon}  {pct:>+7.2f}%  "
          f"(${INITIAL_CAPITAL:,.0f} → ${res['final_capital']:,.0f})")
print(f"{'═'*58}")
print("\n  ⚠  면책: 시뮬레이션 데이터 기반 결과이며 실제 수익을 보장하지 않습니다.")
print("  ⚠  실전 적용 전 추가 최적화(수수료, 슬리피지, 레버리지 등) 필요.\n")
