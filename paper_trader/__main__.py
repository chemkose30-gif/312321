"""명령줄 실행: python -m paper_trader <명령> ...

  backtest    과거 데이터로 "필터 사용 vs 미사용" 비교
  run         오늘 시세로 가상 계좌 1회 매매 (하루 1번 실행)
  status      가상 계좌 현황
  mc          한 종목에 매수 신호가 났다고 보고 1000회 시뮬레이션 결과 보기
  toss-check  토스 API 연결·응답 형식 확인 (토큰 값은 출력하지 않음)
  scan        국내(코스피·코스닥) 전 종목 스캔
  scan-check  과거 시점에 스캔을 돌려 뽑힌 종목이 실제로 어땠는지 검증
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .data import fetch_series
from .engine import Engine, backtest, metrics
from .montecarlo import MCConfig, simulate_trade
from .portfolio import Costs, Portfolio
from .strategy import TrendMomentumStrategy


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default="synthetic", choices=["synthetic", "krx", "yahoo", "csv", "toss"])
    p.add_argument("--symbols", nargs="+", default=["005930", "000660", "035420", "005380", "051910"])
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--csv-dir", default="data")
    p.add_argument("--paths", type=int, default=1000, help="시뮬레이션 경로 수")


def _fetch(args, count: int):
    series, errors = fetch_series(args.source, args.symbols, count, args.seed, args.csv_dir)
    for s, e in errors.items():
        print(f"[경고] {s} 시세 조회 실패: {e}", file=sys.stderr)
    if not series:
        sys.exit("시세를 하나도 가져오지 못했습니다.")
    return series


def cmd_backtest(args) -> None:
    series = _fetch(args, args.days)
    cfg = MCConfig(n_paths=args.paths)
    start, end, results, bh, _ = backtest(series, TrendMomentumStrategy(), args.capital, Costs(), cfg,
                                       seed=args.seed, verbose=args.verbose)
    print(f"\n백테스트 {start} ~ {end} | 종목 {', '.join(series)} | 소스 {args.source} | 초기자금 {args.capital:,.0f}원")
    print(f"{'':32}{'최종자산':>14}{'수익률':>9}{'최대낙폭':>9}{'거래':>6}{'승률':>7}{'평균손익':>9}")
    for name, (_, m) in results.items():
        print(f"{name:<30}{m.final_equity:>14,.0f}{m.total_return:>+9.1%}{m.max_drawdown:>9.1%}"
              f"{m.trades:>6}{m.win_rate:>7.0%}{m.avg_pnl_pct:>+9.2%}")
    print(f"{'균등 매수 후 보유 (벤치마크)':<30}{'':>14}{bh:>+9.1%}")
    print("\n* 과거 성과가 미래를 보장하지 않습니다. 수수료·세금·슬리피지 반영.")


def cmd_run(args) -> None:
    state = Path(args.state)
    if state.exists():
        pf = Portfolio.load(state)
    else:
        pf = Portfolio.new(args.capital)
        print(f"새 가상 계좌 생성: {args.capital:,.0f}원 → {state}")
    today = args.date or date.today().isoformat()
    if pf.equity_history and pf.equity_history[-1][0] == today and not args.force:
        sys.exit(f"{today} 에 이미 실행했습니다. 다시 하려면 --force")
    if args.symbols is None:  # 지정하지 않으면 감시 종목(watchlist.json) + 보유 종목
        from .web import load_watchlist
        args.symbols = load_watchlist()
    args.symbols = list(dict.fromkeys(args.symbols + list(pf.positions)))
    history = {s: [c for _, c in rows] for s, rows in _fetch(args, 300).items()}
    eng = Engine(pf, TrendMomentumStrategy(), MCConfig(n_paths=args.paths), use_filter=not args.no_filter)
    lines = eng.step(today, history)
    print("\n".join(lines) or "오늘은 매매 없음")
    pf.save(state)
    _print_status(pf, {s: h[-1] for s, h in history.items()})


def _print_status(pf: Portfolio, prices: dict) -> None:
    eq = pf.equity(prices)
    print(f"\n총자산 {eq:,.0f}원 ({eq / pf.initial_capital - 1:+.2%}) | 현금 {pf.cash:,.0f}원")
    for s, p in pf.positions.items():
        now = prices.get(s, p.cost_per_share)
        print(f"  {s}: {p.qty}주, 평단 {p.cost_per_share:,.0f} → 현재 {now:,.0f} "
              f"({now / p.cost_per_share - 1:+.2%}), 손절 {p.stop_price:,.0f} / 익절 {p.target_price:,.0f}, "
              f"{p.days_held}/{p.horizon_days}일")
    m = metrics(pf)
    if m.trades:
        print(f"  청산 {m.trades}건, 승률 {m.win_rate:.0%}, 평균 {m.avg_pnl_pct:+.2%}, 최대낙폭 {m.max_drawdown:.1%}")


def cmd_status(args) -> None:
    pf = Portfolio.load(Path(args.state))
    _print_status(pf, {})
    for t in pf.trades[-10:]:
        print("  ", json.dumps(t, ensure_ascii=False))


def cmd_mc(args) -> None:
    series = _fetch(args, 300)
    st = TrendMomentumStrategy()
    for sym, rows in series.items():
        closes = [c for _, c in rows]
        sig = st.decide(sym, closes, holding=False)
        res = simulate_trade(closes, st.target_pct, st.stop_pct, st.horizon_days, Costs().round_trip(),
                             MCConfig(n_paths=args.paths))
        print(f"{sym} 현재 {closes[-1]:,.0f} | 전략 신호 {sig.action} {sig.reason}")
        print(f"   가정: +{st.target_pct:.0%} 익절 / -{st.stop_pct:.0%} 손절 / {st.horizon_days}일 → {res.summary()}"
              f" → {'승인' if res.approved else '거절: ' + res.reason}")


def _bar(phase: str, done: int, total: int) -> None:
    if total > 1:
        print(f"\r{phase} {done}/{total}   ", end="", flush=True)
        if done == total:
            print()
    else:
        print(phase)


def cmd_scan(args) -> None:
    from .scanner import ScanConfig, scan
    r = scan(ScanConfig(min_turnover=args.min_turnover, top=args.top, workers=args.workers), progress=_bar)
    print(f"\n기준일 {r['as_of']} | 상장 {r['listed']} → 국내 보통주 {r['universe']} → 조건 통과 {r['eligible']} "
          f"→ 매수 신호 {r['signals']} → 시뮬레이션 승인 {r['approved']} ({r['seconds']}초)")
    if r["error_count"]:
        print(f"  시세 조회 실패 {r['error_count']}종목 (예: {list(r['errors'].items())[:2]})")
    print(f"{'코드':<8}{'종목명':<16}{'현재가':>10}{'기대수익':>9}{'CVaR5':>9}{'승률':>6}{'점수':>7}{'비중':>7}")
    for x in [x for x in r["results"] if x["approved"]][: args.top]:
        print(f"{x['code']:<8}{x['name'][:14]:<16}{x['price']:>10,.0f}{x['mean']:>+9.2%}{x['cvar5']:>+9.2%}"
              f"{x['win_rate']:>6.0%}{x['score']:>7.2f}{x['size']:>7.1%}")
    print("\n* 점수 = 기대수익 / 최악 5% 평균손실. 상위 종목일수록 운이 섞였을 가능성도 큽니다. scan-check 로 검증하세요.")


def cmd_scan_check(args) -> None:
    from .scanner import ScanConfig, validate
    v = validate(ScanConfig(min_turnover=args.min_turnover), n_dates=args.dates, top_k=args.top_k, progress=_bar)
    print(f"\n종목 {v['stocks']}개, 시점 {len(v['dates'])}개 ({v['dates'][0]} ~ {v['dates'][-1]}), "
          f"각 시점에 사서 최대 {v['horizon']}일 보유(익절/손절 규칙 동일, 비용 차감)")
    print(f"{'':28}{'건수':>7}{'평균':>9}{'중앙값':>9}{'승률':>7}")
    for k, g in v["groups"].items():
        if g["n"]:
            print(f"{k:<26}{g['n']:>7}{g['mean']:>+9.2%}{g['median']:>+9.2%}{g['win_rate']:>7.0%}")
    print("\n* 지금 상장된 종목만 써서 상장폐지 종목이 빠졌습니다(생존 편향). 실제보다 좋게 나올 수 있습니다.")


def cmd_toss_check(args) -> None:
    from .toss_client import TossClient
    c = TossClient()
    c._token()
    print("토큰 발급/캐시 OK (값은 출력하지 않음)")
    raw = c.get(c.candles_path, {"symbol": args.symbol, "interval": "1d", "count": 3})
    print("캔들 원본 응답(앞부분):", json.dumps(raw, ensure_ascii=False)[:1500])
    try:
        print("해석 결과:", c.daily_closes(args.symbol, 3))
        rows = c.daily_closes(args.symbol, 300)
        print(f"300일 요청 → {len(rows)}일 수신 ({rows[0][0]} ~ {rows[-1][0]})")
        if len(rows) < 61:
            print("  ! 전략에 최소 61일이 필요합니다. 페이지 넘김이 안 되는 것 같으니 이 결과를 알려 주세요.")
    except Exception as e:  # noqa: BLE001
        print("해석 실패:", e)


def main(argv=None) -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m paper_trader", description="가상머니 모의투자 엔진")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backtest", help="필터 사용 vs 미사용 비교")
    _common(p)
    p.add_argument("--days", type=int, default=750)
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_backtest)

    p = sub.add_parser("run", help="가상 계좌 1회 매매")
    _common(p)
    p.set_defaults(symbols=None)
    p.add_argument("--state", default="paper_state.json")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--date", help="기록할 날짜 (기본: 오늘)")
    p.add_argument("--no-filter", action="store_true", help="시뮬레이션 필터 끄기")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="가상 계좌 현황")
    p.add_argument("--state", default="paper_state.json")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("mc", help="종목별 1000회 시뮬레이션")
    _common(p)
    p.set_defaults(fn=cmd_mc)

    p = sub.add_parser("scan", help="국내 전 종목 스캔")
    p.add_argument("--min-turnover", type=float, default=1e9, help="20일 평균 거래대금 하한(원)")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("scan-check", help="스캔 방식 과거 검증")
    p.add_argument("--min-turnover", type=float, default=1e9)
    p.add_argument("--dates", type=int, default=12)
    p.add_argument("--top-k", type=int, default=10)
    p.set_defaults(fn=cmd_scan_check)

    p = sub.add_parser("toss-check", help="토스 API 연결 확인")
    p.add_argument("--symbol", default="005930")
    p.set_defaults(fn=cmd_toss_check)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
