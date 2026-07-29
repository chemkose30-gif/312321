#!/usr/bin/env python3
"""
RTS-Style Multi-Coin Backtest  ─  수익률 순위
==============================================
전략 : SuperTrend (ATR) + RSI 모멘텀 필터
       (RTS v2.2.1 핵심 로직 간소화)
데이터: Binance USDT 스팟 (상위 100개 코인, 실제 시세)
타임프레임: 5m / 15m / 1h / 4h

실행
  pip install requests pandas numpy
  python rts_ranking.py

예상 소요 시간: 약 4~8분  (네트워크 속도에 따라 상이)
결과: 콘솔 출력 + rts_ranking_result.csv 저장
"""

import numpy as np
import pandas as pd
import requests
import time
import threading
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ───────────────────────────────────────────────────────
#  설정  (필요 시 수정)
# ───────────────────────────────────────────────────────
TOP_N       = 100       # 분석할 코인 수 (거래량 상위 N개)
LEVERAGE    = 3.0       # 레버리지 배수
COMMISSION  = 0.0004    # 편도 커미션 (0.04% = Bybit maker)
MIN_TRADES  = 8         # 최소 거래 수 (미달 시 결과 제외)
MAX_WORKERS = 12        # 병렬 스레드 수

# 타임프레임별 수집 페이지 (1페이지 = 1000봉)
TF_PAGES = {
    '5m' : 3,   # 3,000봉 × 5분   ≈  10일
    '15m': 9,   # 9,000봉 × 15분  ≈  94일
    '1h' : 9,   # 9,000봉 × 1시간 ≈ 375일
    '4h' : 6,   # 6,000봉 × 4시간 ≈ 1000일 (≈2.7년)
}
TIMEFRAMES = ['5m', '15m', '1h', '4h']

# SuperTrend 파라미터 (RTS 기본값)
ST_LEN  = 14
ST_MULT = 2.0
RSI_LEN = 13

BASE_URL = "https://api.binance.com"      # Binance 스팟
# BASE_URL = "https://fapi.binance.com"   # Binance 선물로 전환 시 주석 변경

# ───────────────────────────────────────────────────────
#  Binance API  (속도 제한 포함)
# ───────────────────────────────────────────────────────
_rate_lock  = threading.Lock()
_last_req   = [0.0]
REQ_GAP     = 0.06      # 요청 간 최소 간격 (초) ─ 16req/s, 약 1000 weight/min

def binance_get(path, params=None, retries=4):
    url = BASE_URL + path
    for attempt in range(retries):
        with _rate_lock:
            gap = REQ_GAP - (time.time() - _last_req[0])
            if gap > 0:
                time.sleep(gap)
            _last_req[0] = time.time()
        try:
            r = requests.get(url, params=params or {}, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 15))
                print(f"  ⚠  Rate-limit → {wait}초 대기", flush=True)
                time.sleep(wait)
                continue
            if r.status_code in (403, 451):
                return None      # 지역 차단 / 정책 차단
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt)
    return None

# ───────────────────────────────────────────────────────
#  심볼 목록 (거래량 상위 N개 USDT 쌍)
# ───────────────────────────────────────────────────────
_EXCL_SUFFIX = (
    'DOWNUSDT','UPUSDT','BULLUSDT','BEARUSDT',
    'BUSDUSDT','USDCUSDT','TUSDUSDT','FDUSDUSDT',
)

def get_top_symbols(n=100):
    path = "/fapi/v1/ticker/24hr" if "fapi" in BASE_URL else "/api/v3/ticker/24hr"
    data = binance_get(path)
    if not data:
        raise RuntimeError("Binance API 연결 실패 — 인터넷 연결을 확인하세요")
    items = [
        d for d in data
        if d["symbol"].endswith("USDT")
        and not any(d["symbol"].endswith(e) for e in _EXCL_SUFFIX)
        and float(d.get("quoteVolume", 0)) > 0
    ]
    items.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [d["symbol"] for d in items[:n]]

# ───────────────────────────────────────────────────────
#  OHLCV 수집 (시간 역순으로 여러 페이지 누적)
# ───────────────────────────────────────────────────────
def fetch_ohlcv(symbol, interval, pages=1):
    kline_path = "/fapi/v1/klines" if "fapi" in BASE_URL else "/api/v3/klines"
    all_rows = []
    end_time = None
    for _ in range(pages):
        params = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end_time is not None:
            params["endTime"] = end_time
        raw = binance_get(kline_path, params)
        if not raw:
            break
        all_rows = raw + all_rows
        end_time = raw[0][0] - 1      # 이전 구간으로 이동
    if not all_rows:
        return None
    df = pd.DataFrame(
        all_rows,
        columns=["ts","open","high","low","close","vol",
                 "ct","qv","nt","tbb","tbq","_"]
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    for col in ("open","high","low","close","vol"):
        df[col] = df[col].astype(float)
    df = df.set_index("ts")[["open","high","low","close","vol"]]
    return df[~df.index.duplicated()].sort_index()

# ───────────────────────────────────────────────────────
#  지표 계산
# ───────────────────────────────────────────────────────
def calc_supertrend(df, n=14, mult=2.0):
    """ATR 기반 SuperTrend 방향  +1 = 상승추세 / -1 = 하락추세"""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/n, adjust=False).mean()
    hl2 = (df["high"] + df["low"]) / 2

    ub_raw = (hl2 + mult * atr).values
    lb_raw = (hl2 - mult * atr).values
    close  = df["close"].values
    nb     = len(df)

    ub   = np.full(nb, np.nan)
    lb   = np.full(nb, np.nan)
    dirs = np.ones(nb, dtype=np.int8)

    for i in range(1, nb):
        ub[i] = ub_raw[i] if (np.isnan(ub[i-1]) or ub_raw[i] < ub[i-1] or close[i-1] > ub[i-1]) else ub[i-1]
        lb[i] = lb_raw[i] if (np.isnan(lb[i-1]) or lb_raw[i] > lb[i-1] or close[i-1] < lb[i-1]) else lb[i-1]
        if   dirs[i-1] == -1 and close[i] > ub[i]: dirs[i] =  1
        elif dirs[i-1] ==  1 and close[i] < lb[i]: dirs[i] = -1
        else:                                        dirs[i] = dirs[i-1]

    return pd.Series(dirs, index=df.index)

def calc_rsi(series, n=13):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

# ───────────────────────────────────────────────────────
#  백테스트 엔진
# ───────────────────────────────────────────────────────
def backtest(df):
    if df is None or len(df) < 200:
        return None

    st  = calc_supertrend(df, ST_LEN, ST_MULT)
    rsi = calc_rsi(df["close"], RSI_LEN)

    # 진입 신호: ST 방향 전환 + RSI 확인
    flip_long  = (st ==  1) & (st.shift(1) == -1) & (rsi >  50)
    flip_short = (st == -1) & (st.shift(1) ==  1) & (rsi <  50)

    # 포지션 시계열 (ST 역방향 전환 시 자동 청산)
    pos_arr = np.zeros(len(df), dtype=np.int8)
    pos = 0
    for i in range(len(df)):
        if   flip_long.iloc[i]:          pos =  1
        elif flip_short.iloc[i]:         pos = -1
        elif st.iloc[i] == -pos:         pos =  0
        pos_arr[i] = pos

    # 거래 수익 계산 (이전봉 신호로 현재봉 시가 진입)
    c      = df["close"].values
    trades = []
    pos    = 0
    entry  = 0.0

    for i in range(1, len(c)):
        new_sig = pos_arr[i - 1]
        if new_sig != pos:
            if pos != 0:                             # 청산
                ret = (c[i] - entry) / entry * pos - COMMISSION * 2
                trades.append(ret)
                pos = 0
            if new_sig != 0:                         # 신규 진입
                pos   = new_sig
                entry = c[i]

    if pos != 0:                                     # 미청산 잔여 포지션
        ret = (c[-1] - entry) / entry * pos - COMMISSION * 2
        trades.append(ret)

    if len(trades) < MIN_TRADES:
        return None

    t    = np.array(trades) * LEVERAGE
    wins = t[t > 0];  loss = t[t < 0]

    pf   = wins.sum() / abs(loss.sum()) if (len(loss) and loss.sum() != 0) else 0.0
    wr   = len(wins) / len(t)
    cumr = np.cumprod(1 + t)
    peak = np.maximum.accumulate(cumr)
    mdd  = ((peak - cumr) / peak).max()

    return dict(
        pf     = round(float(pf),    3),
        wr     = round(wr * 100,     1),
        mdd    = round(float(mdd) * 100, 1),
        total  = round((float(cumr[-1]) - 1) * 100, 1),
        trades = len(t),
    )

# ───────────────────────────────────────────────────────
#  작업 단위 (스레드 풀에서 호출)
# ───────────────────────────────────────────────────────
_done  = [0]
_dlock = threading.Lock()
_total = [0]

def run_task(symbol, tf):
    try:
        df  = fetch_ohlcv(symbol, tf, TF_PAGES[tf])
        res = backtest(df)
        if res:
            res.update(symbol=symbol, tf=tf)
            return res
    except Exception:
        pass
    finally:
        with _dlock:
            _done[0] += 1
            n = _done[0]
            if n % 100 == 0 or n == _total[0]:
                pct = n / _total[0] * 100
                print(f"  진행 {n:>4}/{_total[0]}  ({pct:.0f}%)", flush=True)
    return None

# ───────────────────────────────────────────────────────
#  출력
# ───────────────────────────────────────────────────────
def print_table(df_sub, title, n=100):
    bar = "─" * 76
    print(f"\n{'═'*76}")
    print(f"  {title}")
    print(f"{'═'*76}")
    print(f"  {'순위':<5} {'심볼':<13} {'총수익':>9}  {'PF':>7}  {'승률':>6}  {'MDD':>7}  {'거래수'}")
    print(bar)
    for rank, (_, r) in enumerate(df_sub.head(n).iterrows(), 1):
        sym  = r["symbol"].replace("USDT", "")
        flag = "★" if r["pf"] >= 2.0 and r["total"] >= 30 else " "
        print(f"  {flag}{rank:<4} {sym:<13} {r['total']:>8.1f}%  "
              f"{r['pf']:>7.3f}  {r['wr']:>5.1f}%  "
              f"{r['mdd']:>6.1f}%  {r['trades']}")
    print(bar)

def show_results(df_res):
    # 타임프레임별 순위
    for tf in TIMEFRAMES:
        sub = df_res[df_res["tf"] == tf].sort_values("total", ascending=False)
        if sub.empty:
            continue
        pages = TF_PAGES[tf]
        tf_map = {"5m": f"≈{pages*1000//288}일",
                  "15m": f"≈{pages*1000//96}일",
                  "1h": f"≈{pages*1000//24}일",
                  "4h": f"≈{pages*1000//6}일"}
        print_table(sub, f"[{tf}] 수익률 순위  ({tf_map[tf]} 데이터)")

    # 통합 순위: 코인별 최고 타임프레임
    best_idx = df_res.groupby("symbol")["total"].idxmax()
    best = df_res.loc[best_idx].sort_values("total", ascending=False).reset_index(drop=True)
    best["rank"] = best.index + 1

    print(f"\n{'═'*76}")
    print(f"  전체 통합 순위  (코인별 최고 타임프레임 기준)")
    print(f"{'═'*76}")
    print(f"  {'순위':<5} {'심볼':<13} {'TF':<6} {'총수익':>9}  {'PF':>7}  {'승률':>6}  {'MDD':>7}  {'거래수'}")
    print("─" * 76)
    for _, r in best.head(100).iterrows():
        sym  = r["symbol"].replace("USDT", "")
        flag = "★" if r["pf"] >= 2.0 and r["total"] >= 30 else " "
        print(f"  {flag}{int(r['rank']):<4} {sym:<13} {r['tf']:<6} {r['total']:>8.1f}%  "
              f"{r['pf']:>7.3f}  {r['wr']:>5.1f}%  "
              f"{r['mdd']:>6.1f}%  {r['trades']}")
    print("─" * 76)
    print("  ★ = PF ≥ 2.0  이면서  총수익 ≥ 30%")

# ───────────────────────────────────────────────────────
#  메인
# ───────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  RTS-Style  멀티코인 수익률 순위")
    print(f"  레버리지 {LEVERAGE}x  |  커미션 {COMMISSION*100:.3f}%  |  최소거래 {MIN_TRADES}회")
    print("=" * 60)

    # 1) 심볼 목록
    print("\n▶ 상위 코인 목록 조회 중 ...")
    try:
        symbols = get_top_symbols(TOP_N)
    except RuntimeError as e:
        print(f"  오류: {e}")
        sys.exit(1)
    names = ", ".join(s.replace("USDT","") for s in symbols[:10])
    print(f"  {len(symbols)}개 확인:  {names} ...")

    # 2) 병렬 백테스트
    tasks = [(s, tf) for s in symbols for tf in TIMEFRAMES]
    _total[0] = len(tasks)
    _done[0]  = 0

    total_pages = sum(TF_PAGES[tf] for tf in TIMEFRAMES)
    print(f"\n▶ 백테스트 시작: {len(tasks)}개 작업  "
          f"({MAX_WORKERS} 스레드, 약 {len(tasks)*total_pages//400}~"
          f"{len(tasks)*total_pages//200}분 예상)")
    t0 = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(run_task, s, tf): (s, tf) for s, tf in tasks}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)

    elapsed = time.time() - t0
    print(f"\n  완료: {elapsed:.0f}초  |  유효결과 {len(results)}개 / {len(tasks)}개")

    if not results:
        print("유효한 결과가 없습니다. 네트워크 또는 데이터 문제를 확인하세요.")
        sys.exit(1)

    df_res = pd.DataFrame(results)

    # 3) 콘솔 출력
    show_results(df_res)

    # 4) CSV 저장
    csv_path = "rts_ranking_result.csv"
    (df_res
     .sort_values(["tf", "total"], ascending=[True, False])
     .to_csv(csv_path, index=False, encoding="utf-8-sig"))
    print(f"\n▶ 전체 결과 저장 → {csv_path}")
    print(f"  (엑셀에서 열면 타임프레임별 필터/정렬 가능)\n")

if __name__ == "__main__":
    main()
