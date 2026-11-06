"""명령줄 실행: python -m paper_trader <명령> ...

  backtest    과거 데이터로 "필터 사용 vs 미사용" 비교
  run         오늘 시세로 가상 계좌 1회 매매 (하루 1번 실행)
  status      가상 계좌 현황
  mc          한 종목에 매수 신호가 났다고 보고 미리 사보기 시뮬레이션 결과 보기 (1만 회)
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
import numpy as np

from .montecarlo import MCConfig, seed_for, simulate_trade
from .portfolio import Costs, Portfolio
from .strategy import TrendMomentumStrategy


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--source", default="synthetic", choices=["synthetic", "krx", "yahoo", "csv", "toss"])
    p.add_argument("--state", default="paper_state.json")
    p.add_argument("--symbols", nargs="+", default=["005930", "000660", "035420", "005380", "051910"])
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--csv-dir", default="data")
    p.add_argument("--paths", type=int, default=10000, help="시뮬레이션 경로 수")


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
    from . import web
    web.STATE = Path(args.state)
    web._progress = _bar  # 진행 상황을 터미널에 표시
    pf = Portfolio.load(web.STATE) if web.STATE.exists() else None
    prev = {**web.DEFAULT_SETTINGS, **(pf.settings if pf else {})}
    mode = ("universe" if args.universe else "watchlist" if (args.watchlist or args.symbols is not None)
            else prev["mode"])
    req = web.RunReq(mode=mode, source=args.source, symbols=args.symbols or [], force=args.force,
                     use_filter=not args.no_filter, capital=args.capital, paths=args.paths,
                     max_positions=args.max_positions or prev["max_positions"],
                     max_new=args.max_new or prev["max_new"], min_turnover=prev["min_turnover"],
                     position_pct=(args.position / 100) if args.position else prev["position_pct"])
    try:
        r = web._do_run(req)
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"\n[{'국내 전 종목' if mode == 'universe' else '감시 종목'}] {r['day']} 시세, 대상 {r['candidates']}종목")
    print("\n".join(r["log"]) or "오늘은 매매 없음")
    if r["error_count"]:
        print(f"[경고] 시세 조회 실패 {r['error_count']}종목")
    pf = Portfolio.load(web.STATE)
    _print_status(pf, {})


def _print_status(pf: Portfolio, prices: dict) -> None:
    eq = pf.equity(prices)
    last = pf.equity_history[-1][0] if pf.equity_history else "-"
    print(f"\n총자산 {eq:,.0f}원 ({eq / pf.initial_capital - 1:+.2%}) | 현금 {pf.cash:,.0f}원 | 가격 기준: {last} 매매 실행 시점")
    for s, p in pf.positions.items():
        now = prices.get(s) or pf.last_prices.get(s) or p.cost_per_share
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
        cfg = MCConfig(n_paths=args.paths)
        res = simulate_trade(closes, cfg.target_choices, st.stop_pct, st.horizon_days, Costs().round_trip(),
                             cfg, np.random.default_rng(seed_for(sym, rows[-1][0])))
        print(f"{sym} 현재 {closes[-1]:,.0f} | 전략 신호 {sig.action} {sig.reason}")
        print(f"   익절 3~10% 중 +{res.target_pct:.0%} 선택 / -{st.stop_pct:.0%} 손절 / {st.horizon_days}일 → {res.summary()}"
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
    r = scan(ScanConfig(min_turnover=args.min_turnover, top=args.top, workers=args.workers), progress=_bar,
             screen=args.screen)
    print(f"\n[{r['screen_name']}] 기준일 {r['as_of']} | 상장 {r['listed']} → 국내 보통주 {r['universe']} → 조건 통과 {r['eligible']} "
          f"→ 매수 신호 {r['signals']} → 시뮬레이션 승인 {r['approved']} ({r['seconds']}초)")
    if r["error_count"]:
        print(f"  시세 조회 실패 {r['error_count']}종목 (예: {list(r['errors'].items())[:2]})")
    print(f"{'코드':<8}{'종목명':<16}{'현재가':>10}{'기대수익':>9}{'CVaR5':>9}{'승률':>6}{'점수':>7}  매도")
    for x in [x for x in r["results"] if x["approved"]][: args.top]:
        sell = "다음 날 시가" if x.get("exit") == "next_open" else f"익절 +{x['target_pct']:.0%}"
        print(f"{x['code']:<8}{x['name'][:14]:<16}{x['price']:>10,.0f}{x['mean']:>+9.2%}{x['cvar5']:>+9.2%}"
              f"{x['win_rate']:>6.0%}{x['score']:>7.2f}  {sell}")
    if r["screen"] != "trend":
        print("\n검색식에 걸린 종목 (판정 포함):")
        for x in r["results"]:
            print(f"  {x['code']} {x['name'][:14]:<14} {x['signal']} → {'승인' if x['approved'] else x['reason']}")
    print("\n* 점수 = 기대수익 / 최악 5% 평균손실. 상위 종목일수록 운이 섞였을 가능성도 큽니다. scan-check 로 검증하세요.")


def cmd_scan_check(args) -> None:
    from .scanner import ScanConfig, validate
    v = validate(ScanConfig(min_turnover=args.min_turnover), n_days=args.days, progress=_bar)
    print(f"\n종목 {v['stocks']}개 · 비용 차감 · 손절 -4%")
    f = lambda g: f"{g['n']:>6} {g['mean']:>+7.2%} {g['win_rate']:>5.0%}" if g["n"] else f"{0:>6} {'-':>7} {'-':>5}"  # noqa: E731
    print(f"{'검색식':<22}{'기간':<24}{'하루':>6} | {'아무거나 (건수 평균 승률)':<22} | {'검색식 그대로':<20} | 시뮬레이션 승인")
    for s in v["screens"]:
        rule = "다음 날 시가 매도" if s["exit"] == "next_open" else f"최대 {s['horizon']}일"
        print(f"{s['name']:<20}{s['from']}~{s['to']}{s['hits_per_day']:>6.1f} | {f(s['base'])} | {f(s['hit'])} | {f(s['approved'])}  ({rule})")
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
    p.set_defaults(symbols=None, source="krx")
    p.add_argument("--watchlist", action="store_true", help="감시 종목(watchlist.json)에서만 고르기")
    p.add_argument("--capital", type=float, default=10_000_000)
    p.add_argument("--universe", action="store_true", help="국내 전 종목에서 고르기 (기본: 지난번 설정)")
    p.add_argument("--max-positions", type=int, help="동시 보유 최대 종목 수 (기본 10)")
    p.add_argument("--max-new", type=int, help="하루 신규 매수 최대 종목 수 (기본 3)")
    p.add_argument("--position", type=float, help="종목당 매수 비중 %% (기본 10)")
    p.add_argument("--no-filter", action="store_true", help="시뮬레이션 필터 끄기")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="가상 계좌 현황")
    p.add_argument("--state", default="paper_state.json")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("mc", help="종목별 미리 사보기 시뮬레이션")
    _common(p)
    p.set_defaults(fn=cmd_mc)

    p = sub.add_parser("scan", help="국내 전 종목 스캔")
    p.add_argument("--min-turnover", type=float, default=1e9, help="20일 평균 거래대금 하한(원)")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--screen", default="trend", choices=["trend", "breakout", "squeeze", "closing_bet"],
                   help="trend: 추세 추종, breakout: ①거래량 돌파, squeeze: ③변동성 수축 후 돌파, closing_bet: ④종가 베팅")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("scan-check", help="스캔 방식 과거 검증")
    p.add_argument("--min-turnover", type=float, default=1e9)
    p.add_argument("--days", type=int, default=120, help="단기 검색식을 시험할 최근 거래일 수")
    p.set_defaults(fn=cmd_scan_check)

    p = sub.add_parser("toss-check", help="토스 API 연결 확인")
    p.add_argument("--symbol", default="005930")
    p.set_defaults(fn=cmd_toss_check)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
