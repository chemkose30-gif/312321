"""금(XAUUSD) 자기 발전 AI 트레이더 CLI.

  python trader.py improve  --csv xauusd_m5.csv   # 한 사이클 학습/검증/교체
  python trader.py backtest --csv xauusd_m5.csv   # 현재 챔피언 성적
  python trader.py signal   --csv xauusd_m5.csv   # 마지막 봉 기준 신호 (진입/손절/익절가)
  python trader.py history                        # 발전 기록

데이터 원본: --csv 파일, --yf (yfinance GC=F, 최근 60일 5분봉), --demo (합성 데이터, 동작 확인용)
"""
from __future__ import annotations

import argparse
import json

import data
from backtest import Costs, run
from evolve import STATE, improve, load_champion
from learner import TradeFilter
from strategy import signals


def get_data(a):
    if a.csv:
        return data.load_csv(a.csv)
    if a.yf:
        return data.load_yfinance(a.symbol)
    if a.demo:
        return data.synthetic(days=a.days, seed=a.seed or 0)
    raise SystemExit("데이터 원본을 지정하세요: --csv 파일 | --yf | --demo")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["improve", "backtest", "signal", "history"])
    ap.add_argument("--csv")
    ap.add_argument("--yf", action="store_true")
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--slippage", type=float, default=0.10)
    ap.add_argument("--pop", type=int, default=24)
    ap.add_argument("--gens", type=int, default=12)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()

    if a.cmd == "history":
        f = STATE / "history.jsonl"
        if not f.exists():
            print("아직 기록이 없습니다.")
            return
        for line in f.read_text().splitlines():
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


if __name__ == "__main__":
    main()
