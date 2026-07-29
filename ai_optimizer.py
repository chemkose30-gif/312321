#!/usr/bin/env python3
"""
AI 지표 전략 자동 최적화
==========================
AI 직원 3명이 토론하며 지표를 반복 개선합니다.

  📊 Analyst  : 백테스트 수치 해석
  🎯 Critic   : 전략 약점 지적
  💡 Designer : 파라미터 개선안 제시

설치:
    pip install anthropic requests pandas numpy

설정:
    .env 파일에 추가:
        ANTHROPIC_API_KEY=sk-ant-...
        (Binance 키 불필요 — 공개 API 사용)

실행:
    python ai_optimizer.py
    python ai_optimizer.py --symbol BTCUSDT --tf 4h --rounds 7
"""

import os, sys, json, argparse, textwrap, time
import numpy  as np
import pandas as pd
import requests
from dotenv import load_dotenv
import anthropic

# ═══════════════════════════════════════════════════
#  기본 설정
# ═══════════════════════════════════════════════════
load_dotenv()
BINANCE_URL = "https://api.binance.com"
MODEL       = "claude-haiku-4-5-20251001"   # 빠르고 저렴
COMMISSION  = 0.0004
MIN_TRADES  = 8

# ═══════════════════════════════════════════════════
#  Binance 데이터
# ═══════════════════════════════════════════════════
def fetch_ohlcv(symbol: str, interval: str, pages: int = 6) -> pd.DataFrame:
    tf_map = {"5m":"5m","15m":"15m","1h":"1h","4h":"4h"}
    iv     = tf_map.get(interval, interval)
    rows   = []
    end    = None
    for _ in range(pages):
        params = {"symbol": symbol, "interval": iv, "limit": 1000}
        if end:
            params["endTime"] = end
        r = requests.get(f"{BINANCE_URL}/api/v3/klines", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows = data + rows
        end  = data[0][0] - 1
    if not rows:
        raise RuntimeError(f"{symbol} 데이터 없음")
    df = pd.DataFrame(rows, columns=[
        "ts","open","high","low","close","vol",
        "ct","qv","nt","tbb","tbq","_"
    ])
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    for c in ("open","high","low","close","vol"):
        df[c] = df[c].astype(float)
    return df.set_index("ts")[["open","high","low","close","vol"]]\
             .pipe(lambda x: x[~x.index.duplicated()]).sort_index()

# ═══════════════════════════════════════════════════
#  지표
# ═══════════════════════════════════════════════════
def calc_supertrend(df, n, mult):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr    = tr.ewm(alpha=1/n, adjust=False).mean()
    hl2    = (df["high"] + df["low"]) / 2
    ub_raw = (hl2 + mult * atr).values
    lb_raw = (hl2 - mult * atr).values
    close  = df["close"].values
    nb     = len(df)
    ub     = np.full(nb, np.nan)
    lb     = np.full(nb, np.nan)
    d      = np.ones(nb, dtype=np.int8)
    for i in range(1, nb):
        ub[i] = ub_raw[i] if (np.isnan(ub[i-1]) or ub_raw[i] < ub[i-1] or close[i-1] > ub[i-1]) else ub[i-1]
        lb[i] = lb_raw[i] if (np.isnan(lb[i-1]) or lb_raw[i] > lb[i-1] or close[i-1] < lb[i-1]) else lb[i-1]
        if   d[i-1]==-1 and close[i]>ub[i]: d[i]=1
        elif d[i-1]==1  and close[i]<lb[i]: d[i]=-1
        else:                                d[i]=d[i-1]
    return pd.Series(d, index=df.index)

def calc_rsi(s, n):
    delta = s.diff()
    g = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-delta).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + g/(l+1e-10))

def calc_macd(s, fast, slow, signal):
    ema_f = s.ewm(span=fast,  adjust=False).mean()
    ema_s = s.ewm(span=slow,  adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig

def calc_bbands(s, n, std_mult):
    ma  = s.rolling(n).mean()
    std = s.rolling(n).std()
    return ma + std_mult*std, ma - std_mult*std

def calc_mfi(df, n):
    tp      = (df["high"] + df["low"] + df["close"]) / 3
    raw_mfi = df["vol"] * (2*tp - df["high"] - df["low"])
    pos = raw_mfi.clip(lower=0)
    neg = (-raw_mfi).clip(lower=0)
    ratio = pos.rolling(n).sum() / (neg.rolling(n).sum().replace(0, 1e-10))
    return 100 - 100/(1+ratio)

# ═══════════════════════════════════════════════════
#  백테스트 엔진
# ═══════════════════════════════════════════════════
def run_backtest(df: pd.DataFrame, params: dict, leverage: float = 3.0) -> dict | None:
    st_n    = params.get("st_n", 14)
    st_mult = params.get("st_mult", 2.5)
    rsi_n   = params.get("rsi_n", 13)
    rsi_lo  = params.get("rsi_long", 55)
    rsi_sh  = params.get("rsi_short", 45)
    extra   = params.get("extra_filter", "none")
    macd_f  = params.get("macd_fast", 12)
    macd_s  = params.get("macd_slow", 26)
    macd_sig= params.get("macd_signal", 9)
    bb_n    = params.get("bb_n", 20)
    bb_mult = params.get("bb_mult", 2.0)
    mfi_n   = params.get("mfi_n", 14)
    mfi_ob  = params.get("mfi_ob", 70)
    mfi_os  = params.get("mfi_os", 30)

    st  = calc_supertrend(df, st_n, st_mult)
    rsi = calc_rsi(df["close"], rsi_n)

    # 추가 필터
    extra_long  = pd.Series(True, index=df.index)
    extra_short = pd.Series(True, index=df.index)
    if extra == "macd":
        macd, msig  = calc_macd(df["close"], macd_f, macd_s, macd_sig)
        extra_long  = macd > msig
        extra_short = macd < msig
    elif extra == "bbands":
        ub, lb      = calc_bbands(df["close"], bb_n, bb_mult)
        extra_long  = df["close"] > lb
        extra_short = df["close"] < ub
    elif extra == "mfi":
        mfi         = calc_mfi(df, mfi_n)
        extra_long  = mfi < mfi_os
        extra_short = mfi > mfi_ob
    elif extra == "vol_filter":
        vol_ma      = df["vol"].rolling(20).mean()
        extra_long  = df["vol"] > vol_ma
        extra_short = df["vol"] > vol_ma

    flip_up   = (st ==  1) & (st.shift(1) == -1) & (rsi >  rsi_lo) & extra_long
    flip_down = (st == -1) & (st.shift(1) ==  1) & (rsi <  rsi_sh) & extra_short

    pos_arr = np.zeros(len(df), dtype=np.int8)
    pos = 0
    for i in range(len(df)):
        if   flip_up.iloc[i]:     pos =  1
        elif flip_down.iloc[i]:   pos = -1
        elif st.iloc[i] == -pos:  pos =  0
        pos_arr[i] = pos

    c = df["close"].values
    trades, pos, entry = [], 0, 0.0
    for i in range(1, len(c)):
        ns = pos_arr[i-1]
        if ns != pos:
            if pos != 0:
                trades.append((c[i]-entry)/entry*pos - COMMISSION*2)
            pos, entry = (ns, c[i]) if ns != 0 else (0, 0.0)
    if pos != 0:
        trades.append((c[-1]-entry)/entry*pos - COMMISSION*2)

    if len(trades) < MIN_TRADES:
        return None

    t    = np.array(trades) * leverage
    wins = t[t > 0];  loss = t[t < 0]
    pf   = wins.sum()/abs(loss.sum()) if len(loss) and loss.sum()!=0 else 0.0
    cumr = np.cumprod(1+t)
    peak = np.maximum.accumulate(cumr)
    mdd  = float(((peak-cumr)/peak).max())

    return dict(
        total  = round((float(cumr[-1])-1)*100, 2),
        pf     = round(float(pf), 3),
        wr     = round(len(wins)/len(t)*100, 1),
        mdd    = round(mdd*100, 1),
        trades = len(t),
        avg_r  = round(float(t.mean())*100, 3),
    )

# ═══════════════════════════════════════════════════
#  AI 직원들
# ═══════════════════════════════════════════════════
ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def call_ai(system: str, user: str) -> str:
    msg = ai.messages.create(
        model     = MODEL,
        max_tokens= 1024,
        system    = system,
        messages  = [{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()

def analyst(result: dict, params: dict, symbol: str, tf: str) -> str:
    sys_p = textwrap.dedent("""
        당신은 퀀트 애널리스트입니다. 백테스트 수치를 보고 전략 상태를 한국어로
        3~5문장으로 객관적으로 평가하세요.
        PF(손익비), 승률, MDD(최대낙폭), 거래수, 총수익을 모두 언급하세요.
    """)
    user_p = f"""
심볼: {symbol}  타임프레임: {tf}
파라미터: {json.dumps(params, ensure_ascii=False)}
결과:
  총수익   : {result['total']}%
  PF      : {result['pf']}
  승률     : {result['wr']}%
  MDD     : {result['mdd']}%
  거래수   : {result['trades']}
  평균수익률: {result['avg_r']}%/거래
"""
    return call_ai(sys_p, user_p)

def critic(analysis: str, result: dict, params: dict) -> str:
    sys_p = textwrap.dedent("""
        당신은 까다로운 리스크 매니저입니다. 전략의 약점을 한국어로
        구체적으로 3가지 이상 지적하세요. 수치 근거를 반드시 포함하세요.
        PF < 1.5, 승률 < 40%, MDD > 30% 는 즉시 지적하세요.
    """)
    user_p = f"""
애널리스트 평가: {analysis}

현재 파라미터: {json.dumps(params, ensure_ascii=False)}
결과: 총수익 {result['total']}%  PF {result['pf']}  승률 {result['wr']}%  MDD {result['mdd']}%  거래 {result['trades']}개

이 전략의 구체적인 약점을 지적하세요.
"""
    return call_ai(sys_p, user_p)

def designer(criticism: str, params: dict, history: list) -> dict:
    sys_p = textwrap.dedent("""
        당신은 알고리즘 트레이딩 전략 설계자입니다.
        비판을 바탕으로 파라미터를 개선하고 JSON으로만 응답하세요.
        다른 설명 없이 JSON 객체만 출력하세요.

        수정 가능한 파라미터:
          st_n       : SuperTrend ATR 기간 (5~30)
          st_mult    : SuperTrend 배수 (1.5~4.0)
          rsi_n      : RSI 기간 (5~21)
          rsi_long   : RSI 롱 진입 기준 (50~70)
          rsi_short  : RSI 숏 진입 기준 (30~50)
          extra_filter: 추가 필터 "none"|"macd"|"mfi"|"bbands"|"vol_filter"
          macd_fast  : MACD 빠른 EMA (8~20, extra_filter=macd 시)
          macd_slow  : MACD 느린 EMA (20~40)
          macd_signal: MACD 시그널 (5~15)
          mfi_n      : MFI 기간 (10~20, extra_filter=mfi 시)
          mfi_ob     : MFI 과매수 (65~85)
          mfi_os     : MFI 과매도 (15~35)
          bb_n       : BB 기간 (15~30, extra_filter=bbands 시)
          bb_mult    : BB 배수 (1.5~3.0)
    """)
    hist_str = "\n".join(
        f"  Round {i+1}: 총수익 {h['total']}%  PF {h['pf']}  승률 {h['wr']}%  파라미터: {h['params']}"
        for i, h in enumerate(history)
    )
    user_p = f"""
비판 내용: {criticism}

현재 파라미터: {json.dumps(params, ensure_ascii=False)}

이전 시도 내역:
{hist_str if history else "  (없음 — 첫 번째 시도)"}

개선된 파라미터를 JSON으로만 출력하세요.
이전에 시도한 파라미터와 너무 비슷하지 않게 하세요.
"""
    raw = call_ai(sys_p, user_p)
    # JSON 파싱
    start = raw.find("{");  end = raw.rfind("}") + 1
    return json.loads(raw[start:end])

# ═══════════════════════════════════════════════════
#  메인 최적화 루프
# ═══════════════════════════════════════════════════
def optimize(symbol: str, tf: str, rounds: int, leverage: float):
    print(f"\n{'='*60}")
    print(f"  AI 지표 최적화 시작")
    print(f"  심볼: {symbol}  타임프레임: {tf}  라운드: {rounds}  레버리지: {leverage}x")
    print(f"{'='*60}\n")

    print("▶ 시세 데이터 다운로드 중...", flush=True)
    tf_pages = {"5m":3, "15m":9, "1h":9, "4h":6}
    df = fetch_ohlcv(symbol, tf, tf_pages.get(tf, 6))
    bars = len(df)
    print(f"  {bars:,}봉  {df.index[0].date()} ~ {df.index[-1].date()}\n")

    # 초기 파라미터
    params = {
        "st_n": 14, "st_mult": 2.5,
        "rsi_n": 13, "rsi_long": 55, "rsi_short": 45,
        "extra_filter": "none",
    }

    history     = []
    best_result = None
    best_params = None

    for rnd in range(1, rounds + 1):
        print(f"{'─'*60}")
        print(f"  Round {rnd}/{rounds}")
        print(f"  파라미터: {params}")
        print(f"{'─'*60}")

        # 1) 백테스트
        result = run_backtest(df, params, leverage)
        if result is None:
            print(f"  ⚠️  거래 수 부족 ({MIN_TRADES}개 미만) — 파라미터 재조정")
            result = {"total": 0, "pf": 0, "wr": 0, "mdd": 0, "trades": 0, "avg_r": 0}

        print(f"\n  📈 백테스트 결과")
        print(f"     총수익 {result['total']:>8.1f}%  |  PF {result['pf']:.3f}  |  "
              f"승률 {result['wr']:.1f}%  |  MDD {result['mdd']:.1f}%  |  "
              f"거래 {result['trades']}회")

        # 최고 성과 갱신
        if best_result is None or result["total"] > best_result["total"]:
            best_result = result.copy()
            best_params = params.copy()

        if rnd == rounds:
            break   # 마지막 라운드는 분석만

        # 2) AI 토론
        print(f"\n  📊 Analyst 분석 중...", flush=True)
        analysis = analyst(result, params, symbol, tf)
        print(f"  {analysis}\n")

        print(f"  🎯 Critic 검토 중...", flush=True)
        criticism = critic(analysis, result, params)
        print(f"  {criticism}\n")

        print(f"  💡 Designer 개선안 도출 중...", flush=True)
        history.append({**result, "params": params.copy()})
        try:
            new_params = designer(criticism, params, history)
            # 유효 범위 클램핑
            new_params["st_n"]      = int(max(5,  min(30,  new_params.get("st_n",    params["st_n"]))))
            new_params["st_mult"]   = float(max(1.5, min(4.0, new_params.get("st_mult",  params["st_mult"]))))
            new_params["rsi_n"]     = int(max(5,  min(21,  new_params.get("rsi_n",   params["rsi_n"]))))
            new_params["rsi_long"]  = int(max(50, min(70,  new_params.get("rsi_long", params["rsi_long"]))))
            new_params["rsi_short"] = int(max(30, min(50,  new_params.get("rsi_short",params["rsi_short"]))))
            ef = new_params.get("extra_filter","none")
            if ef not in ("none","macd","mfi","bbands","vol_filter"):
                new_params["extra_filter"] = "none"
            print(f"  → 새 파라미터: {new_params}\n")
            params = new_params
        except Exception as e:
            print(f"  ⚠️  파라미터 파싱 실패 ({e}) — 기존 유지\n")

    # ── 최종 결과 ──────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  최적화 완료")
    print(f"{'='*60}")
    print(f"  최고 성과 파라미터:")
    for k, v in best_params.items():
        print(f"    {k:<18} = {v}")
    print(f"\n  총수익  {best_result['total']:>8.1f}%")
    print(f"  PF      {best_result['pf']:>8.3f}")
    print(f"  승률    {best_result['wr']:>8.1f}%")
    print(f"  MDD     {best_result['mdd']:>8.1f}%")
    print(f"  거래수  {best_result['trades']:>8}회")
    print(f"{'='*60}\n")

    return best_params, best_result

# ═══════════════════════════════════════════════════
#  진입점
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI 지표 전략 최적화")
    ap.add_argument("--symbol",   default="ETHUSDT",  help="코인 심볼 (예: BTCUSDT)")
    ap.add_argument("--tf",       default="4h",       help="타임프레임: 5m 15m 1h 4h")
    ap.add_argument("--rounds",   type=int, default=5, help="최적화 라운드 수 (기본 5)")
    ap.add_argument("--leverage", type=float, default=3.0, help="레버리지 배수")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("오류: ANTHROPIC_API_KEY 가 없습니다.")
        print(".env 파일에 ANTHROPIC_API_KEY=sk-ant-... 를 추가하세요.")
        sys.exit(1)

    optimize(args.symbol, args.tf, args.rounds, args.leverage)
