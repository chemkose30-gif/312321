"""금(XAUUSD) 자기 발전 AI 트레이더 CLI.

  python trader.py improve  --csv xauusd_m5.csv   # 한 사이클 학습/검증/교체
  python trader.py backtest --csv xauusd_m5.csv   # 현재 챔피언 성적
  python trader.py signal   --csv xauusd_m5.csv   # 마지막 봉 기준 신호 (진입/손절/익절가)
  python trader.py history                        # 발전 기록
  python trader.py run --mt5                      # 실시간 모의매매: 5분마다 매수·매도를 장부에 기록
  python trader.py paper --mt5                    # 모의매매 1회 처리 (작업 스케줄러로 반복 실행할 때)
  python trader.py report                         # 최근 7일 매수·매도 기록 리포트

데이터 원본: --mt5 (MetaTrader 5 실시간), --csv 파일, --yf (yfinance GC=F), --demo (합성 데이터, 동작 확인용)
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import data
from backtest import Costs, run
from evolve import STATE, improve, load_champion
from learner import TradeFilter
from paper import step
from report import weekly
from strategy import signals


def get_data(a):
    if a.mt5:
        return data.load_mt5(a.symbol, server_offset=a.server_offset)
    if a.csv:
        return data.load_csv(a.csv)
    if a.yf:
        return data.load_yfinance(a.symbol)
    if a.demo:
        return data.synthetic(days=a.days, seed=a.seed or 0)
    raise SystemExit("데이터 원본을 지정하세요: --csv 파일 | --yf | --demo")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 한글 깨짐 방지
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["improve", "backtest", "signal", "history", "paper", "run", "report"])
    ap.add_argument("--mt5", action="store_true")
    ap.add_argument("--server-offset", type=float, help="MT5 서버 시간 - UTC (예: 3). 생략하면 자동 감지")
    ap.add_argument("--balance", type=float, default=10_000, help="모의 계좌 시작 잔고 ($)")
    ap.add_argument("--risk", type=float, default=1.0, help="거래당 잔고 대비 위험 %%")
    ap.add_argument("--improve-days", type=float, default=7, help="run 중 자기 발전 주기 (일)")
    ap.add_argument("--days-report", type=int, default=7)
    ap.add_argument("--csv")
    ap.add_argument("--yf", action="store_true")
    ap.add_argument("--symbol", help="기본값: --mt5 는 XAUUSD, --yf 는 GC=F")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--slippage", type=float, default=0.10)
    ap.add_argument("--pop", type=int, default=24)
    ap.add_argument("--gens", type=int, default=12)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()

    a.symbol = a.symbol or ("XAUUSD.GOLD" if a.mt5 else "GC=F")
    if a.cmd == "report":
        print(f"리포트 저장: {weekly(days=a.days_report)}")
        return
    if a.cmd == "history":
        f = STATE / "history.jsonl"
        if not f.exists():
            print("아직 기록이 없습니다.")
            return
        for line in f.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            print(f"{e['time']}  {'채택' if e['promoted'] else '기각'}  "
                  f"챔피언 {e['champion_score']:+.2f} vs 도전자 {e['challenger_score']:+.2f}  "
                  f"도전자 승률 {e['challenger_valid']['win_rate']:.0%} / {e['challenger_valid']['trades']}회")
        return

    df = get_data(a)
    costs = Costs(a.spread, a.slippage)
    if a.cmd == "improve":
        improve(df, costs, pop=a.pop, gens=a.gens, min_trades=a.min_trades, seed=a.seed)
        return
    if a.cmd == "paper":
        st = step(df, costs, a.balance, a.risk)
        print(f"잔고 {st['balance']:.2f}$  보유 {st['position'] or '없음'}")
        return
    if a.cmd == "run":
        run_forever(a, costs)
        return

    ch = load_champion()
    flt = TradeFilter.from_dict(ch.get("filter"))
    if a.cmd == "backtest":
        res = run(df, ch["params"], costs, flt)
        print(f"챔피언 {ch.get('generation', 0)}세대  파라미터: {ch['params']}")
        print(res.stats)
        if not res.trades.empty:
            print(res.trades[["entry_time", "dir", "entry", "exit", "r", "reason"]].tail(10).to_string(index=False))
    elif a.cmd == "signal":
        sig = signals(df, ch["params"])
        last = sig.iloc[-1]
        d = int(last["signal"])
        if d != 0 and flt is not None:
            from strategy import FEATURES
            pr = float(flt.proba(sig[FEATURES].to_numpy()[-1:], sig["signal"].to_numpy()[-1:])[0])
            if pr < flt.threshold:
                print(f"{df.index[-1]}  신호 있으나 학습 필터가 거름 (승리 확률 추정 {pr:.0%})")
                return
        if d == 0:
            print(f"{df.index[-1]}  신호 없음 (종가 {df['close'].iloc[-1]:.2f})")
            return
        p = ch["params"]
        px = df["close"].iloc[-1]
        risk = last["atr"] * p["atr_sl"]
        print(f"{df.index[-1]}  {'매수' if d > 0 else '매도'} 신호")
        print(f"  기준가 {px:.2f}  손절 {px - d * risk:.2f}  익절 {px + d * risk * p['rr']:.2f}  "
              f"(최대 보유 {p['max_hold']}봉)")


def run_forever(a, costs):
    """5분마다 새 봉을 처리하고, improve_days 마다 스스로 전략을 갱신하며, 매일 주간 리포트를 다시 쓴다."""
    last_improve = 0.0
    print("실시간 모의매매 시작 (Ctrl+C로 종료). 기록: state/journal.csv, 리포트: reports/")
    while True:
        try:
            df = get_data(a)
            if time.time() - last_improve > a.improve_days * 86400:
                print("자기 발전 사이클 실행 중...")
                improve(df, costs, pop=a.pop, gens=a.gens, min_trades=a.min_trades, verbose=False)
                last_improve = time.time()
            step(df, costs, a.balance, a.risk)
            try:
                weekly()
            except SystemExit:
                pass
        except Exception as e:  # 연결 끊김 등은 다음 주기에 재시도
            print(f"오류: {e}")
        time.sleep(300 - time.time() % 300 + 5)   # 다음 5분봉 마감 직후


if __name__ == "__main__":
    main()
