"""
ETH/USDT  15분봉  S4 이중밴드 전략
- 4H에서 검증된 최고 전략을 15분봉으로 적용
- 목표: 하루 10번 이상 거래
- 자금: $10,000  |  거래금액: 잔고의 3%
- 손절: -0.8%  (15분봉 기준)
"""

import pandas as pd
import numpy as np
import time
import warnings
warnings.filterwarnings("ignore")

INITIAL_CAPITAL = 10_000
TRADE_PCT       = 0.03
BB_PERIOD       = 20
BB_STD_INNER    = 1.0    # 진입
BB_STD_OUTER    = 2.0    # 참조
RSI_PERIOD      = 14
RSI_UPPER       = 60
RSI_LOWER       = 40
STOP_LOSS_PCT   = 0.008  # -0.8%
START           = "2022-01-01"
END             = "2024-12-31"
BARS_PER_DAY    = 96     # 15분봉 = 하루 96봉

ANCHORS = [
    ("2022-01-01",3700),("2022-01-22",2200),("2022-03-28",3290),
    ("2022-05-12",1900),("2022-06-13",900), ("2022-08-13",1960),
    ("2022-09-15",1500),("2022-11-09",1100),("2022-12-31",1200),
    ("2023-01-14",1540),("2023-02-16",1680),("2023-04-14",2100),
    ("2023-05-25",1820),("2023-06-10",1660),("2023-07-14",1890),
    ("2023-08-17",1570),("2023-09-11",1600),("2023-10-23",1790),
    ("2023-12-05",2200),("2023-12-31",2280),
    ("2024-01-12",2580),("2024-02-29",3400),("2024-03-12",4090),
    ("2024-04-15",2900),("2024-05-23",3780),("2024-07-05",2870),
    ("2024-08-05",2100),("2024-09-13",2340),("2024-10-01",2600),
    ("2024-11-12",3380),("2024-12-16",4000),("2024-12-31",3300),
]


def generate_15m_data():
    daily  = pd.date_range(START, END, freq="D")
    ad     = pd.to_datetime([a[0] for a in ANCHORS])
    ap     = [a[1] for a in ANCHORS]
    tgt    = pd.Series(ap, index=ad).reindex(daily).interpolate("time").values

    np.random.seed(2024)
    SIGMA  = 0.005   # ETH 15분봉 변동성 ≈ 0.5%
    MR     = 0.15

    n      = len(daily) * BARS_PER_DAY
    cls    = np.empty(n)
    prev   = tgt[0]
    for d in range(len(daily)):
        t   = tgt[d]
        px  = prev * np.exp(np.cumsum(np.log(t/prev)/BARS_PER_DAY*MR + np.random.normal(0,SIGMA,BARS_PER_DAY)))
        cls[d*BARS_PER_DAY:d*BARS_PER_DAY+BARS_PER_DAY] = px
        prev = px[-1]

    rng = cls * np.abs(np.random.normal(0.003, 0.0015, n))
    idx = pd.date_range(START, periods=n, freq="15min")
    df  = pd.DataFrame({
        "Open" : np.r_[cls[0], cls[:-1]],
        "High" : cls + rng * 0.55,
        "Low"  : cls - rng * 0.55,
        "Close": cls,
    }, index=idx)
    return df


def add_indicators(df):
    c    = df["Close"]
    sma  = c.rolling(BB_PERIOD).mean()
    std  = c.rolling(BB_PERIOD).std()
    df["bb_u1"]  = sma + BB_STD_INNER * std
    df["bb_l1"]  = sma - BB_STD_INNER * std
    df["bb_mid"] = sma
    d    = c.diff()
    g    = d.clip(lower=0).rolling(RSI_PERIOD).mean()
    l    = (-d.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"] = 100 - 100 / (1 + g / l.replace(0, np.nan))
    return df


def run_backtest(df):
    df2  = df.dropna(subset=["bb_u1","rsi"])
    cls  = df2["Close"].to_numpy()
    u1   = df2["bb_u1"].to_numpy()
    l1   = df2["bb_l1"].to_numpy()
    rsi  = df2["rsi"].to_numpy()
    dts  = df2.index.to_numpy()

    capital   = float(INITIAL_CAPITAL)
    pos       = None
    trades    = []
    eq_d, eq_v = [], []

    for i in range(len(cls)):
        price = cls[i]
        if i % BARS_PER_DAY == 0:
            unr = (price-pos[1])/pos[1]*pos[0]*pos[2] if pos else 0
            eq_v.append(capital + unr)
            eq_d.append(dts[i])

        if pos is None:
            if price <= l1[i] and rsi[i] < RSI_LOWER:
                pos = (1, price, capital*TRADE_PCT, dts[i])
            elif price >= u1[i] and rsi[i] > RSI_UPPER:
                pos = (-1, price, capital*TRADE_PCT, dts[i])
        else:
            s, ep, sz, edt = pos
            cur = (price - ep) / ep * s
            stop = cur <= -STOP_LOSS_PCT
            tgt  = (s==1 and price>=u1[i]) or (s==-1 and price<=l1[i])
            if stop or tgt:
                pnl = cur * sz
                capital += pnl
                hold_m = int((dts[i]-edt)/np.timedelta64(1,"m"))
                trades.append({
                    "entry_date" : pd.Timestamp(edt),
                    "exit_date"  : pd.Timestamp(dts[i]),
                    "side"       : "롱" if s==1 else "숏",
                    "entry_price": ep,
                    "exit_price" : price,
                    "pnl_usd"   : pnl,
                    "pnl_pct"   : cur*100,
                    "exit_type" : "손절" if stop else "목표",
                    "hold_min"  : hold_m,
                })
                pos = None

    if pos:
        s,ep,sz,edt = pos
        price = cls[-1]
        cur = (price-ep)/ep*s
        trades.append({"entry_date":pd.Timestamp(edt),"exit_date":pd.Timestamp(dts[-1]),
                       "side":"롱" if s==1 else "숏","entry_price":ep,"exit_price":price,
                       "pnl_usd":cur*sz,"pnl_pct":cur*100,"exit_type":"강제청산","hold_min":0})

    t  = pd.DataFrame(trades)
    eq = pd.Series(eq_v, index=pd.DatetimeIndex(eq_d), name="equity")
    return {"final":capital,"pct":(capital-INITIAL_CAPITAL)/INITIAL_CAPITAL*100,"trades":t,"equity":eq}


# ─── 메인 ───────────────────────────────────────────────
print("=" * 58)
print("   ETH/USDT  15분봉  S4 이중밴드  백테스트")
print("=" * 58)

t0 = time.time()
print(f"\n  데이터 생성 중 ({START}~{END}, 15분봉)...")
df = generate_15m_data()
df = add_indicators(df)
print(f"  ✅ {len(df):,}개 캔들  ({time.time()-t0:.1f}초)")

print("  백테스트 실행 중...")
t1 = time.time()
res = run_backtest(df)
print(f"  ✅ 완료  ({time.time()-t1:.1f}초)\n")

t  = res["trades"]
eq = res["equity"]

if t.empty:
    print("거래 없음"); exit()

total   = len(t)
wins    = t[t["pnl_usd"] > 0]
losses  = t[t["pnl_usd"] <= 0]
stops   = t[t["exit_type"] == "손절"]
wr      = len(wins)/total*100
pf      = wins["pnl_usd"].sum()/abs(losses["pnl_usd"].sum()) if len(losses) else float("inf")
mdd     = ((eq-eq.cummax())/eq.cummax()).min()*100
r       = eq.pct_change().dropna()
sharpe  = r.mean()/r.std()*np.sqrt(252) if r.std() else 0

# 일별 거래 수 계산
t["date"] = t["entry_date"].dt.date
daily_cnt = t.groupby("date").size()
t["year"] = t["entry_date"].dt.year
t["month"]= t["entry_date"].dt.month

total_days = (pd.Timestamp(END)-pd.Timestamp(START)).days
trade_days = daily_cnt.shape[0]

print("=" * 58)
print("  📊  성과 요약")
print("=" * 58)
print(f"  기간           : {START} ~ {END}  (3년)")
print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
print(f"  최종 자금      : $ {res['final']:>10,.2f}")
print(f"  총 수익률      :  {res['pct']:>+9.2f} %")
print(f"  최대 낙폭(MDD) :  {mdd:>9.2f} %")
print(f"  샤프 비율      :  {sharpe:>9.2f}")
print(f"{'─'*58}")
print(f"  총 거래 수     : {total:>6,} 건")
print(f"  승률           :  {wr:>8.1f} %")
print(f"  손익비(PF)     :  {pf:>8.2f}")
print(f"  손절 비율      :  {len(stops)/total*100:>7.1f} %")
if len(wins):
    print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+8.2f}")
if len(losses):
    print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+8.2f}")

print(f"\n{'─'*58}")
print(f"  ⏱  거래 빈도")
print(f"{'─'*58}")
print(f"  하루 평균  : {total/total_days:>6.1f} 건")
print(f"  거래일 기준: {total/trade_days:>6.1f} 건/일")
print(f"  주  평균  : {total/total_days*7:>6.1f} 건")
print(f"  월  평균  : {total/total_days*30:>6.1f} 건")
print(f"\n  하루 거래 수 분포:")
pct_10p = (daily_cnt >= 10).sum()
pct_5_9 = ((daily_cnt >= 5) & (daily_cnt < 10)).sum()
pct_2_4 = ((daily_cnt >= 2) & (daily_cnt < 5)).sum()
pct_1   = (daily_cnt == 1).sum()
print(f"   10건 이상 : {pct_10p:>4}일  ({pct_10p/trade_days*100:>4.1f}%)")
print(f"    5~9건   : {pct_5_9:>4}일  ({pct_5_9/trade_days*100:>4.1f}%)")
print(f"    2~4건   : {pct_2_4:>4}일  ({pct_2_4/trade_days*100:>4.1f}%)")
print(f"    1건     : {pct_1:>4}일  ({pct_1/trade_days*100:>4.1f}%)")
print(f"  최다 거래일: {daily_cnt.max()}건")

print(f"\n  ⏱  평균 보유 시간")
print(f"{'─'*58}")
avg_h = t["hold_min"].mean()
print(f"  평균          : {avg_h:.0f}분  ({avg_h/60:.1f}시간)")
print(f"  15분 이내     : {len(t[t['hold_min']<=15]):>5}건  ({len(t[t['hold_min']<=15])/total*100:.1f}%)")
print(f"  1시간 이내    : {len(t[t['hold_min']<=60]):>5}건  ({len(t[t['hold_min']<=60])/total*100:.1f}%)")
print(f"  4시간 이내    : {len(t[t['hold_min']<=240]):>5}건  ({len(t[t['hold_min']<=240])/total*100:.1f}%)")

print(f"\n  📅  연도별 성과")
print(f"{'─'*58}")
for yr in [2022,2023,2024]:
    yt = t[t["year"]==yr]
    yw = yt[yt["pnl_usd"]>0]
    pnl= yt["pnl_usd"].sum()
    cnt= len(yt)
    wr_yr = len(yw)/cnt*100 if cnt else 0
    ic = "▲" if pnl>=0 else "▼"
    print(f"  {yr}년  {ic}${pnl:>+8.2f}  ({cnt:>4}건,  승률{wr_yr:.0f}%,  일평균{cnt/365:.1f}건)")

print(f"\n  📆  월별 손익 (2023년)")
print(f"{'─'*58}")
t23 = t[t["year"]==2023]
for m in range(1,13):
    mt  = t23[t23["month"]==m]
    pnl = mt["pnl_usd"].sum()
    cnt = len(mt)
    ic  = "▲" if pnl>=0 else "▼"
    bar = "█"*min(int(abs(pnl)/15),20)
    print(f"  {m:>2}월  {ic}${pnl:>+7.2f}  ({cnt:>3}건,  일평균{cnt/30:.1f}건)  {bar}")

print(f"\n  최근 거래 10건:")
print(f"{'─'*58}")
for _,r2 in t.tail(10).iterrows():
    ic = "✅" if r2["pnl_usd"]>0 else "❌"
    dt = r2["entry_date"].strftime("%m-%d %H:%M")
    print(f"  {ic} {dt} {r2['side']}  ${r2['entry_price']:>5.0f}→${r2['exit_price']:>5.0f}"
          f"  {r2['hold_min']:>4}분  ${r2['pnl_usd']:>+6.2f} [{r2['exit_type']}]")

print(f"\n{'='*58}")
print(f"  수수료 예상 (바이비트 0.04% × {total}건 × 2 × $300)")
fee = total * 2 * 0.0004 * (INITIAL_CAPITAL*TRADE_PCT)
print(f"  ≈ ${fee:,.0f}  (수익의 약 {fee/((res['final']-INITIAL_CAPITAL) if res['final']>INITIAL_CAPITAL else 1)*100:.0f}%)")
print(f"{'='*58}\n")
