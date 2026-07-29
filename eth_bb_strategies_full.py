"""
ETH/USDT  4H봉  볼린저밴드 전략 종합 비교
- 기존 챔피언 S4 + 7가지 새로운 BB 전략
- 동일 데이터, 동일 조건으로 공정 비교
"""

import pandas as pd
import numpy as np
import time
import warnings
warnings.filterwarnings("ignore")

INITIAL_CAPITAL = 10_000
TRADE_PCT       = 0.03
START           = "2021-01-01"
END             = "2024-12-31"
BARS_PER_DAY    = 6

ANCHORS = [
    ("2021-01-01",730),("2021-02-20",1950),("2021-05-12",4080),
    ("2021-06-22",1730),("2021-08-29",3290),("2021-09-21",2700),
    ("2021-11-10",4860),("2021-12-04",3880),("2021-12-31",3680),
    ("2022-01-22",2200),("2022-03-28",3290),("2022-05-12",1900),
    ("2022-06-13",900),("2022-08-13",1960),("2022-09-15",1500),
    ("2022-11-09",1100),("2022-12-31",1200),
    ("2023-01-14",1540),("2023-02-16",1680),("2023-04-14",2100),
    ("2023-05-25",1820),("2023-06-10",1660),("2023-07-14",1890),
    ("2023-08-17",1570),("2023-09-11",1600),("2023-10-23",1790),
    ("2023-12-05",2200),("2023-12-31",2280),
    ("2024-01-12",2580),("2024-02-29",3400),("2024-03-12",4090),
    ("2024-04-15",2900),("2024-05-23",3780),("2024-07-05",2870),
    ("2024-08-05",2100),("2024-09-13",2340),("2024-10-01",2600),
    ("2024-11-12",3380),("2024-12-16",4000),("2024-12-31",3300),
]


# ═══════════════════════════════════════════════════════
#  데이터 + 지표
# ═══════════════════════════════════════════════════════
def generate_4h():
    daily = pd.date_range(START,END,freq="D")
    ad    = pd.to_datetime([a[0] for a in ANCHORS])
    ap    = [a[1] for a in ANCHORS]
    tgt   = pd.Series(ap,index=ad).reindex(daily).interpolate("time").values
    np.random.seed(2024)
    n     = len(daily)*BARS_PER_DAY
    cls   = np.empty(n); hi=np.empty(n); lo=np.empty(n)
    prev  = tgt[0]
    for d in range(len(daily)):
        t   = tgt[d]
        px  = prev*np.exp(np.cumsum(np.log(t/prev)/BARS_PER_DAY*0.2+np.random.normal(0,0.025,BARS_PER_DAY)))
        rng = px*np.abs(np.random.normal(0.012,0.006,BARS_PER_DAY))
        cls[d*BARS_PER_DAY:d*BARS_PER_DAY+BARS_PER_DAY]=px
        hi [d*BARS_PER_DAY:d*BARS_PER_DAY+BARS_PER_DAY]=px+rng*0.55
        lo [d*BARS_PER_DAY:d*BARS_PER_DAY+BARS_PER_DAY]=px-rng*0.55
        prev=px[-1]
    idx = pd.date_range(START,periods=n,freq="4h")
    df  = pd.DataFrame({"Open":np.r_[cls[0],cls[:-1]],"High":hi,"Low":lo,"Close":cls},index=idx)
    return df


def add_all_indicators(df):
    c  = df["Close"]; h = df["High"]; l = df["Low"]

    # BB 기본 (20기간)
    s20  = c.rolling(20).mean(); std20 = c.rolling(20).std()
    df["bb_u2"]  = s20 + 2.0*std20;  df["bb_l2"]  = s20 - 2.0*std20
    df["bb_u1"]  = s20 + 1.0*std20;  df["bb_l1"]  = s20 - 1.0*std20
    df["bb_u25"] = s20 + 2.5*std20;  df["bb_l25"] = s20 - 2.5*std20
    df["bb_mid"] = s20

    # BB 10기간 (단기)
    s10  = c.rolling(10).mean(); std10 = c.rolling(10).std()
    df["bb10_u"] = s10 + 2.0*std10;  df["bb10_l"] = s10 - 2.0*std10
    df["bb10_m"] = s10

    # BB 50기간 (장기)
    s50  = c.rolling(50).mean(); std50 = c.rolling(50).std()
    df["bb50_u"] = s50 + 2.0*std50;  df["bb50_l"] = s50 - 2.0*std50
    df["bb50_m"] = s50

    # BB 폭 (밴드 폭 / SMA) — 스퀴즈 감지
    df["bb_width"] = (df["bb_u2"] - df["bb_l2"]) / s20
    df["bw_ma20"]  = df["bb_width"].rolling(20).mean()   # 폭 평균

    # RSI 14
    d2 = c.diff(); g = d2.clip(lower=0).rolling(14).mean()
    ll = (-d2.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100/(1+g/ll.replace(0,np.nan))

    # Stochastic RSI (RSI의 Stochastic)
    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    df["srsi_k"] = (df["rsi"] - rsi_min) / (rsi_max - rsi_min + 1e-9) * 100
    df["srsi_d"] = df["srsi_k"].rolling(3).mean()

    # EMA 50, 200
    df["ema50"]  = c.ewm(span=50,adjust=False).mean()
    df["ema200"] = c.ewm(span=200,adjust=False).mean()

    # ATR 14
    prev_c = c.shift(1)
    tr = pd.concat([(h-l),(h-prev_c).abs(),(l-prev_c).abs()],axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # Keltner Channel (ATR 기반, 스퀴즈 감지용)
    df["kc_u"] = s20 + 1.5*df["atr"]
    df["kc_l"] = s20 - 1.5*df["atr"]

    # %B (가격이 밴드 내 위치)
    df["pctB"] = (c - df["bb_l2"]) / (df["bb_u2"] - df["bb_l2"] + 1e-9)

    # MACD
    ema12 = c.ewm(span=12,adjust=False).mean()
    ema26 = c.ewm(span=26,adjust=False).mean()
    df["macd"]   = ema12 - ema26
    df["macd_s"] = df["macd"].ewm(span=9,adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_s"]

    return df


# ═══════════════════════════════════════════════════════
#  백테스트 엔진 (공용)
# ═══════════════════════════════════════════════════════
def backtest(df, entry_fn, exit_fn, stop_pct):
    df2  = df.dropna()
    arr  = {c: df2[c].to_numpy() for c in df2.columns}
    dts  = df2.index.to_numpy()
    cls  = arr["Close"]

    capital = float(INITIAL_CAPITAL)
    pos = None
    trades = []
    eq_d, eq_v = [], []

    for i in range(len(cls)):
        price = cls[i]
        if i % BARS_PER_DAY == 0:
            unr = (price-pos[1])/pos[1]*pos[0]*pos[2] if pos else 0
            eq_v.append(capital+unr); eq_d.append(dts[i])

        if pos is None:
            sig = entry_fn(i, arr, price)
            if sig in (1,-1):
                pos = (sig, price, capital*TRADE_PCT, dts[i])
        else:
            s,ep,sz,edt = pos
            cur = (price-ep)/ep*s
            stop_hit = cur <= -stop_pct
            tgt_hit  = exit_fn(i, arr, price, s, ep)
            if stop_hit or tgt_hit:
                pnl = cur*sz; capital += pnl
                trades.append({
                    "entry_date":pd.Timestamp(edt),"exit_date":pd.Timestamp(dts[i]),
                    "side":"롱" if s==1 else "숏","entry_price":ep,"exit_price":price,
                    "pnl_usd":pnl,"pnl_pct":cur*100,
                    "exit_type":"손절" if stop_hit else "목표",
                    "hold_4h":int((dts[i]-edt)/np.timedelta64(4,"h")),
                })
                pos=None

    if pos:
        s,ep,sz,edt=pos; cur=(cls[-1]-ep)/ep*s; pnl=cur*sz; capital+=pnl
        trades.append({"entry_date":pd.Timestamp(edt),"exit_date":pd.Timestamp(dts[-1]),
                       "side":"롱" if s==1 else "숏","entry_price":ep,"exit_price":cls[-1],
                       "pnl_usd":pnl,"pnl_pct":cur*100,"exit_type":"강제청산","hold_4h":0})

    t  = pd.DataFrame(trades)
    eq = pd.Series(eq_v,index=pd.DatetimeIndex(eq_d),name="equity")
    return {"final":capital,"pct":(capital-INITIAL_CAPITAL)/INITIAL_CAPITAL*100,"trades":t,"equity":eq}


def stats(res):
    t=res["trades"]; eq=res["equity"]
    if t.empty: return {"pct":0,"mdd":0,"sharpe":0,"total":0,"wr":0,"pf":0,"hold":0,"final":INITIAL_CAPITAL}
    wins=t[t["pnl_usd"]>0]; losses=t[t["pnl_usd"]<=0]; total=len(t)
    pf = wins["pnl_usd"].sum()/abs(losses["pnl_usd"].sum()) if len(losses) and losses["pnl_usd"].sum()!=0 else 999
    mdd= ((eq-eq.cummax())/eq.cummax()).min()*100
    r  = eq.pct_change().dropna()
    sh = r.mean()/r.std()*np.sqrt(252) if r.std() else 0
    yr_pnl={yr:t[pd.to_datetime(t["entry_date"]).dt.year==yr]["pnl_usd"].sum() for yr in [2021,2022,2023,2024]}
    return {"pct":res["pct"],"mdd":mdd,"sharpe":sh,"total":total,"wr":len(wins)/total*100,
            "pf":pf,"hold":t["hold_4h"].mean()*4,"final":res["final"],"yr":yr_pnl,"trades":t,"equity":eq}


# ═══════════════════════════════════════════════════════
#  전략 정의
# ═══════════════════════════════════════════════════════

# ── 기존 챔피언: S4 이중밴드 ──
def s4_entry(i,a,p):
    if p<=a["bb_l1"][i] and a["rsi"][i]<40: return 1
    if p>=a["bb_u1"][i] and a["rsi"][i]>60: return -1
    return 0
def s4_exit(i,a,p,s,ep):
    return (s==1 and p>=a["bb_u1"][i]) or (s==-1 and p<=a["bb_l1"][i])


# ── N1. BB 스퀴즈 돌파 ──────────────────────────────────
# 밴드가 좁아지면(스퀴즈) → 확장될 때 방향으로 진입
def n1_entry(i,a,p):
    if i<5: return 0
    # 스퀴즈 감지: BB가 Keltner 안에 있음
    squeeze_on = a["bb_u2"][i-1]<a["kc_u"][i-1] and a["bb_l2"][i-1]>a["kc_l"][i-1]
    if not squeeze_on: return 0
    # 돌파: 스퀴즈 해제 + 방향
    squeeze_off = a["bb_u2"][i]>=a["kc_u"][i] or a["bb_l2"][i]<=a["kc_l"][i]
    if not squeeze_off: return 0
    macd = a["macd_h"][i]
    if macd>0 and a["rsi"][i]>50: return 1
    if macd<0 and a["rsi"][i]<50: return -1
    return 0
def n1_exit(i,a,p,s,ep):
    return (s==1 and p>=a["bb_u2"][i]*1.01) or (s==-1 and p<=a["bb_l2"][i]*0.99)


# ── N2. %B + MACD 다이버전스 ─────────────────────────────
# %B가 극단(0.05 이하/0.95 이상) + MACD 방향 전환
def n2_entry(i,a,p):
    if i<3: return 0
    pB = a["pctB"][i]; mh = a["macd_h"][i]; mh_prev = a["macd_h"][i-1]
    # 롱: 하단 과매도 + MACD 히스토그램 반등
    if pB<0.05 and mh>mh_prev and a["rsi"][i]<45: return 1
    # 숏: 상단 과매수 + MACD 히스토그램 하락
    if pB>0.95 and mh<mh_prev and a["rsi"][i]>55: return -1
    return 0
def n2_exit(i,a,p,s,ep):
    return (s==1 and a["pctB"][i]>=0.5) or (s==-1 and a["pctB"][i]<=0.5)


# ── N3. 다중 BB (10기간+50기간) ──────────────────────────
# 단기 BB(10) 터치 + 장기 BB(50) 방향 일치
def n3_entry(i,a,p):
    # 장기 트렌드: 장기 BB 상단 위 → 상승장
    long_up   = p > a["bb50_m"][i]
    long_down = p < a["bb50_m"][i]
    rsi = a["rsi"][i]
    # 단기 BB 내부밴드 터치
    if long_up   and p<=a["bb10_l"][i] and rsi<45: return 1
    if long_down and p>=a["bb10_u"][i] and rsi>55: return -1
    return 0
def n3_exit(i,a,p,s,ep):
    return (s==1 and p>=a["bb10_m"][i]) or (s==-1 and p<=a["bb10_m"][i])


# ── N4. Stochastic RSI + BB ──────────────────────────────
# BB 터치 + StochRSI 극단에서 반전 신호
def n4_entry(i,a,p):
    if i<3: return 0
    k=a["srsi_k"][i]; d=a["srsi_d"][i]
    k_prev=a["srsi_k"][i-1]; d_prev=a["srsi_d"][i-1]
    # K선이 D선 상향 돌파 + 하단 밴드
    if p<=a["bb_l2"][i] and k>d and k_prev<=d_prev and k<25: return 1
    # K선이 D선 하향 돌파 + 상단 밴드
    if p>=a["bb_u2"][i] and k<d and k_prev>=d_prev and k>75: return -1
    return 0
def n4_exit(i,a,p,s,ep):
    return (s==1 and p>=a["bb_mid"][i]) or (s==-1 and p<=a["bb_mid"][i])


# ── N5. BB 밴드 워킹 (추세 편승) ─────────────────────────
# 강한 추세에서 밴드를 따라 걷는 구간 포착
def n5_entry(i,a,p):
    if i<5: return 0
    # 최근 3봉이 연속 상단 밴드 위 = 상승 워킹
    if (a["Close"][i-2]>=a["bb_u2"][i-2] and
        a["Close"][i-1]>=a["bb_u2"][i-1] and
        p>=a["bb_u2"][i] and a["macd_h"][i]>0): return 1
    # 최근 3봉이 연속 하단 밴드 아래 = 하락 워킹
    if (a["Close"][i-2]<=a["bb_l2"][i-2] and
        a["Close"][i-1]<=a["bb_l2"][i-1] and
        p<=a["bb_l2"][i] and a["macd_h"][i]<0): return -1
    return 0
def n5_exit(i,a,p,s,ep):
    # 중앙선으로 복귀할 때 청산
    return (s==1 and p<=a["bb_mid"][i]) or (s==-1 and p>=a["bb_mid"][i])


# ── N6. BB 2.5σ 극단 반전 ────────────────────────────────
# 2.5σ는 통계적으로 매우 드문 극단 → 반전 가능성 높음
def n6_entry(i,a,p):
    rsi=a["rsi"][i]; srsi=a["srsi_k"][i]
    if p<=a["bb_l25"][i] and rsi<30 and srsi<15: return 1
    if p>=a["bb_u25"][i] and rsi>70 and srsi>85: return -1
    return 0
def n6_exit(i,a,p,s,ep):
    return (s==1 and p>=a["bb_mid"][i]) or (s==-1 and p<=a["bb_mid"][i])


# ── N7. BB + EMA200 추세필터 + ATR 청산 ──────────────────
# EMA200으로 큰 방향 확인 + 1σ 진입 + ATR 트레일링
def n7_entry(i,a,p):
    above200 = p>a["ema200"][i]
    rsi=a["rsi"][i]
    if above200 and p<=a["bb_l1"][i] and rsi<42: return 1
    if not above200 and p>=a["bb_u1"][i] and rsi>58: return -1
    return 0
def n7_exit(i,a,p,s,ep):
    atr=a["atr"][i]
    # ATR 기반 이익 목표: 2×ATR
    profit_target=(p-ep)/ep*s >= 2*atr/ep
    mid_hit=(s==1 and p>=a["bb_mid"][i]) or (s==-1 and p<=a["bb_mid"][i])
    return profit_target or mid_hit


# ═══════════════════════════════════════════════════════
#  실행 + 출력
# ═══════════════════════════════════════════════════════
print("="*68)
print("   ETH/USDT  4H봉  볼린저밴드 전략 종합 발굴")
print("="*68)

t0=time.time()
print(f"\n  데이터 생성 + 지표 계산 중...")
df = generate_4h()
df = add_all_indicators(df)
print(f"  ✅ {len(df):,}개 캔들  ({time.time()-t0:.1f}초)\n")

STRATEGIES = [
    ("S4  이중밴드 (챔피언)",   s4_entry,  s4_exit,  0.025),
    ("N1  BB 스퀴즈 돌파",      n1_entry,  n1_exit,  0.040),
    ("N2  %B + MACD",           n2_entry,  n2_exit,  0.030),
    ("N3  다중BB(10+50기간)",    n3_entry,  n3_exit,  0.025),
    ("N4  Stoch RSI + BB",      n4_entry,  n4_exit,  0.030),
    ("N5  BB 밴드 워킹",        n5_entry,  n5_exit,  0.040),
    ("N6  BB 2.5σ 극단 반전",   n6_entry,  n6_exit,  0.030),
    ("N7  BB+EMA200+ATR청산",   n7_entry,  n7_exit,  0.030),
]

print("  🔄 8가지 전략 백테스트 실행 중...")
results = []
for name,ef,xf,sp in STRATEGIES:
    res = backtest(df, ef, xf, sp)
    s   = stats(res)
    s["name"] = name
    results.append(s)
    icon = "▲" if s["pct"]>=0 else "▼"
    print(f"  {name:<26}  {icon}{s['pct']:>+7.2f}%  ({s['total']}건)")

# ── 순위표 ──────────────────────────────────────────────
results_sorted = sorted(results, key=lambda x: x["pct"], reverse=True)

print(f"\n{'='*68}")
print(f"  🏆  전략 순위 (수익률 기준)")
print(f"{'='*68}")
print(f"  {'순위':^4} │ {'전략':^26} │ {'수익률':>7} │ {'MDD':>7} │ {'샤프':>6} │ {'거래':>5} │ {'PF':>5}")
print(f"  {'─'*4}─┼─{'─'*26}─┼─{'─'*7}─┼─{'─'*7}─┼─{'─'*6}─┼─{'─'*5}─┼─{'─'*5}")

medals = ["🥇","🥈","🥉","④","⑤","⑥","⑦","⑧"]
for rank, s in enumerate(results_sorted):
    ic = "▲" if s["pct"]>=0 else "▼"
    pf = f"{min(s['pf'],99.9):.2f}"
    print(f"  {medals[rank]:^4} │ {s['name']:<26} │ {ic}{s['pct']:>+6.2f}% │ {s['mdd']:>+7.2f}% │ {s['sharpe']:>6.2f} │ {s['total']:>5} │ {pf:>5}")

# ── 연도별 ──────────────────────────────────────────────
print(f"\n{'─'*68}")
print(f"  📅  연도별 순손익 (상위 4개)")
print(f"{'─'*68}")
top4 = results_sorted[:4]
header = "  {:<4} │".format("연도")
for s in top4: header += f" {s['name'][:12]:^14} │"
print(header)
print(f"  {'─'*4}─┼─" + "─┼─".join(["─"*14]*4))
for yr in [2021,2022,2023,2024]:
    row = f"  {yr} │"
    for s in top4:
        v=s["yr"].get(yr,0)
        row += f" {'▲' if v>=0 else '▼'}${v:>+8,.0f}    │"
    print(row)

# ── 최고 전략 상세 ──────────────────────────────────────
best = results_sorted[0]
t    = best["trades"]
eq   = best["equity"]
wins = t[t["pnl_usd"]>0]; losses=t[t["pnl_usd"]<=0]; stops=t[t["exit_type"]=="손절"]

print(f"\n{'='*68}")
print(f"  🏆  최고 전략 상세  →  {best['name']}")
print(f"{'='*68}")
print(f"  초기 자금      : $ {INITIAL_CAPITAL:>10,.2f}")
print(f"  최종 자금      : $ {best['final']:>10,.2f}")
print(f"  총 수익률      :  {best['pct']:>+9.2f} %")
print(f"  최대 낙폭(MDD) :  {best['mdd']:>9.2f} %")
print(f"  샤프 비율      :  {best['sharpe']:>9.2f}")
print(f"{'─'*68}")
print(f"  총 거래 수     : {best['total']:>5} 건")
print(f"  평균 보유      : {best['hold']:>5.1f} 시간  ({best['hold']/24:.1f}일)")
print(f"  승률           :  {best['wr']:>7.1f} %")
print(f"  손익비(PF)     :  {min(best['pf'],99.9):>7.2f}")
print(f"  손절 비율      :  {len(stops)/best['total']*100:>7.1f} %")
if len(wins):   print(f"  평균 수익(승)  : $ {wins['pnl_usd'].mean():>+8.2f}")
if len(losses): print(f"  평균 손실(패)  : $ {losses['pnl_usd'].mean():>+8.2f}")
print(f"  최대 단일 수익 : $ {t['pnl_usd'].max():>+8.2f}")
print(f"  최대 단일 손실 : $ {t['pnl_usd'].min():>+8.2f}")

# 월별
print(f"\n{'─'*68}")
t["yr"]  = t["entry_date"].dt.year
t["mon"] = t["entry_date"].dt.month
for yr in [2021,2022,2023,2024]:
    ty = t[t["yr"]==yr]
    yr_pnl = ty["pnl_usd"].sum()
    ic = "▲" if yr_pnl>=0 else "▼"
    print(f"  ── {yr}년  합계 {ic}${yr_pnl:>+,.0f} ──")
    for m in range(1,13):
        mt  = ty[ty["mon"]==m]
        if mt.empty: continue
        pnl = mt["pnl_usd"].sum(); cnt=len(mt)
        ic2 = "▲" if pnl>=0 else "▼"
        bar = "█"*min(int(abs(pnl)/10),20)
        print(f"  {m:>2}월 {ic2}${pnl:>+7.2f} ({cnt:>3}건) {bar}")

print(f"\n  최근 거래 10건:")
for _,r in t.tail(10).iterrows():
    ic   = "✅" if r["pnl_usd"]>0 else "❌"
    side = "롱" if r["side"]=="롱" else "숏"
    dt   = r["entry_date"].strftime("%Y-%m-%d")
    print(f"  {ic} {dt} {side}  ${r['entry_price']:>5.0f}→${r['exit_price']:>5.0f}"
          f"  {r['hold_4h']*4:>4}h  ${r['pnl_usd']:>+7.2f} [{r['exit_type']}]")

# 수수료 후 순수익
fee = best["total"] * 2 * 0.0004 * (INITIAL_CAPITAL*TRADE_PCT)
net = best["final"] - INITIAL_CAPITAL - fee
print(f"\n{'='*68}")
print(f"  💰  실전 예상 수익 (수수료 포함)")
print(f"{'─'*68}")
print(f"  전략 수익      : ${best['final']-INITIAL_CAPITAL:>+,.2f}")
print(f"  수수료 (-0.04%): -${fee:>,.2f}  ({best['total']}건 × 2 × 0.04% × $300)")
print(f"  메이커 시 수수료: -${best['total']*2*0.0001*INITIAL_CAPITAL*TRADE_PCT:>,.2f}  (0.01%)")
print(f"  순수익 (테이커): ${net:>+,.2f}  ({net/INITIAL_CAPITAL*100:>+.1f}%)")
print(f"{'='*68}\n")
