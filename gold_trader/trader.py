"""금(XAUUSD) 자기 발전 AI 트레이더 CLI.

  python trader.py improve  --csv xauusd_m5.csv   # 한 사이클 학습/검증/교체
  python trader.py backtest --csv xauusd_m5.csv   # 현재 챔피언 성적
  python trader.py signal   --csv xauusd_m5.csv   # 마지막 봉 기준 신호 (진입/손절/익절가)
  python trader.py history                        # 발전 기록
  python trader.py run --mt5                      # 실시간 모의매매: 매수·매도를 장부에 기록
  python trader.py paper --mt5                    # 모의매매 1회 처리 (작업 스케줄러로 반복 실행할 때)
  python trader.py report                         # 최근 7일 매수·매도 기록 리포트
  python trader.py auto --mt5                     # MT5 자동매매 (기본: 데모 계좌만)
  python trader.py dashboard --mt5                # 대시보드 페이지 만들고 브라우저로 열기
  python trader.py loosen --mt5                   # 매수 조건을 단계별로 느슨하게 했을 때 거래 수·성적 비교

데이터 원본: --mt5 (MetaTrader 5 실시간), --csv 파일, --yf (yfinance GC=F), --demo (합성 데이터, 동작 확인용)
봉 단위(1·5·15분)는 학습 때 AI가 고른다. --csv/--yf/--demo 는 원본보다 긴 봉 단위만 쓸 수 있다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import data
from backtest import Costs, run
from evolve import STATE, improve, load_champion, load_champions
from learner import TradeFilter
from paper import Risk, step
from report import weekly
from strategy import FEATURES, SLOTS, signals


def base_data(a):
    if a.csv:
        return data.load_csv(a.csv)
    if a.yf:
        return data.load_yfinance(a.symbol)
    if a.demo:
        return data.synthetic(days=a.days, seed=a.seed or 0, minutes=1)
    raise SystemExit("데이터 원본을 지정하세요: --mt5 | --csv 파일 | --yf | --demo")


def get_data(a, minutes: int):
    """선택된 봉 단위의 최근 데이터."""
    if a.mt5:
        return data.load_mt5(a.symbol, minutes, server_offset=a.server_offset)
    base = base_data(a)
    if minutes < data.bar_minutes(base):
        raise SystemExit(f"원본 데이터가 {data.bar_minutes(base)}분봉이라 {minutes}분봉을 만들 수 없습니다")
    return data.resample(base, minutes)


def get_all(a) -> dict:
    """학습용: 고를 수 있는 모든 봉 단위의 데이터."""
    tfs = [int(x) for x in a.timeframes.split(",")]
    if a.mt5:
        out = {m: df for m, df in data.load_mt5_all(a.symbol, a.server_offset).items() if m in tfs}
    else:
        base = base_data(a)
        out = {m: data.resample(base, m) for m in tfs if m >= data.bar_minutes(base)}
    if not out:
        raise SystemExit("쓸 수 있는 봉 단위가 없습니다")
    return out


def costs_for(a, df) -> Costs:
    """--spread 를 주지 않으면 MT5에서 읽은 실제 스프레드, 그것도 없으면 0.30달러."""
    spread = a.spread if a.spread is not None else (df.attrs.get("spread") or 0.30)
    return Costs(spread, a.slippage)


def active_slots() -> dict:
    return {s: ch for s, ch in load_champions().items() if ch.get("active")}


def needed_tfs() -> list[int]:
    """운용 중인 전략과 아직 포지션이 남은 전략이 쓰는 봉 단위들."""
    from paper import STATE_FILE, slots_to_run

    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else None
    return sorted({ch.get("timeframe", 5) for ch in slots_to_run(st).values()})


def print_status() -> None:
    for s, ch in load_champions().items():
        v = ch.get("valid_stats") or {}
        state = "운용" if ch.get("active") else "대기"
        print(f"  {SLOTS[s]['name']:<10} {state}  {ch.get('timeframe', 5):>2}분봉  "
              + (f"검증 {v.get('trades', 0)}회 승률 {v.get('win_rate', 0):.0%} {v.get('total_r', 0):+.1f}R "
                 f"손익비 {v.get('profit_factor', 0)}" if v else "아직 학습 전"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 한글 깨짐 방지
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["improve", "backtest", "signal", "history", "paper", "run", "report", "auto", "dashboard", "loosen"])
    ap.add_argument("--mt5", action="store_true")
    ap.add_argument("--server-offset", type=float, help="MT5 서버 시간 - UTC (예: 3). 생략하면 자동 감지")
    ap.add_argument("--balance", type=float, default=10_000, help="모의 계좌 시작 잔고 ($)")
    ap.add_argument("--risk", type=float, default=1.0, help="거래당 잔고 대비 위험 %%")
    ap.add_argument("--max-positions", type=int, default=5, help="모든 전략 합쳐 동시에 들 수 있는 최대 포지션 수")
    ap.add_argument("--max-per-strategy", type=int, default=2, help="전략 하나가 동시에 들 수 있는 최대 포지션 수")
    ap.add_argument("--slot", choices=list(SLOTS), help="loosen/backtest/signal 에서 볼 전략 슬롯 (기본: 운용 중인 전부)")
    ap.add_argument("--leverage", type=float, default=100, help="레버리지 (모의매매 증거금 계산용)")
    ap.add_argument("--daily-loss", type=float, default=0, help="하루 손실 한도 %% (기본 0 = 없음). AI 거래 손익만 계산")
    ap.add_argument("--max-lot", type=float, default=0.10, help="자동매매 1회 최대 랏")
    ap.add_argument("--allow-real", action="store_true", help="실계좌 자동매매 허용 (주의)")
    ap.add_argument("--improve-days", default="2,5", help="자기 발전 요일들 (0=월 ... 6=일, PC 시간 기준). 기본 수,토")
    ap.add_argument("--improve-hour", type=int, default=10, help="자기 발전 시각 (PC 시간, 이 시각 이후 1회)")
    ap.add_argument("--days-report", type=int, default=7)
    ap.add_argument("--no-browser", action="store_true", help="대시보드를 브라우저로 열지 않음 (자동 재시작 때)")
    ap.add_argument("--csv")
    ap.add_argument("--yf", action="store_true")
    ap.add_argument("--symbol", help="기본값: --mt5 는 금 심볼 자동 찾기, --yf 는 GC=F")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--spread", type=float, help="스프레드 ($). 생략하면 MT5 실제값, 없으면 0.30")
    ap.add_argument("--timeframes", default="1,5,15", help="AI가 고를 봉 단위 후보 (분)")
    ap.add_argument("--slippage", type=float, default=0.10)
    ap.add_argument("--pop", type=int, default=24)
    ap.add_argument("--gens", type=int, default=12)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()

    a.symbol = a.symbol or ("auto" if a.mt5 else "GC=F")
    if a.mt5:
        a.symbol = data.resolve_mt5_symbol(a.symbol)
    if a.cmd == "dashboard":
        import webbrowser
        out = make_dashboard(a)
        print(f"대시보드 저장: {out}")
        webbrowser.open(out.resolve().as_uri())
        return
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
            by = "  ".join(f"{m}분 {v['score']:+.1f}" for m, v in e.get("by_timeframe", {}).items())
            slot = SLOTS.get(e.get("slot", "trend_5"), {}).get("name", "")
            print(f"{e['time']}  {slot:<10} {'채택' if e['promoted'] else '기각'}  "
                  f"챔피언 {e.get('champion_timeframe', 5)}분봉 {e['champion_score']:+.2f} vs "
                  f"도전자 {e.get('challenger_timeframe', 5)}분봉 {e['challenger_score']:+.2f}  "
                  f"도전자 승률 {e['challenger_valid']['win_rate']:.0%} / {e['challenger_valid']['trades']}회"
                  + (f"  [{by}]" if by else ""))
        return

    risk = Risk(a.risk, a.max_positions, a.leverage, a.daily_loss, a.max_per_strategy)
    if a.cmd == "auto":
        if not a.mt5:
            raise SystemExit("자동매매는 --mt5 로만 가능합니다")
        run_forever(a, risk, auto=True)
        return
    if a.cmd == "run":
        run_forever(a, risk)
        return
    if a.cmd == "loosen":
        import loosen

        slots = [a.slot] if a.slot else (list(active_slots()) or ["trend_5"])
        cache, md = {}, []
        for slot in slots:
            tf = load_champion(slot).get("timeframe", 5)
            if tf not in cache:
                cache[tf] = (data.load_mt5(a.symbol, tf, {1: 100_000, 5: 20_000, 15: 8_000}[tf], a.server_offset)
                             if a.mt5 else data.resample(base_data(a), tf))
            df = cache[tf]
            t, start, end, days = loosen.table(df, costs_for(a, df), slot)
            print(f"\n[{SLOTS[slot]['name']}] 매수 조건 완화 비교 ({tf}분봉, 검증 기간 {start:%Y-%m-%d}~{end:%Y-%m-%d}, "
                  f"거래일 {days}일)\n")
            print(t.to_string(index=False))
            md.append(loosen.section(t, start, end, days, tf, slot))
        print(f"\nR = 손절 1번 금액 (거래당 위험 {a.risk}%면 1R = 잔고의 {a.risk}%)")
        print(f"저장: {loosen.save(md, a.risk)}")
        return
    if a.cmd == "improve":
        dfs = get_all(a)
        improve(dfs, costs_for(a, next(iter(dfs.values()))), pop=a.pop, gens=a.gens,
                min_trades=a.min_trades, seed=a.seed, slots=[a.slot] if a.slot else None)
        STATE.mkdir(exist_ok=True)
        LAST_IMPROVE.write_text(datetime.now().date().isoformat(), encoding="utf-8")
        print("\n전략 상태:")
        print_status()
        return
    if a.cmd == "paper":
        dfs = {tf: get_data(a, tf) for tf in needed_tfs()}
        if dfs:
            st = step(dfs, costs_for(a, next(iter(dfs.values()))), a.balance, risk)
            print(f"잔고 {st['balance']:.2f}$  보유 {len(st['positions'])}개")
        else:
            print("운용 중인 전략이 없습니다. 먼저 improve 를 실행하세요.")
        return

    slots = [a.slot] if a.slot else list(active_slots())
    if not slots:
        print("운용 중인 전략이 없습니다. 상태:")
        print_status()
        return
    for slot in slots:
        ch = load_champion(slot)
        df = get_data(a, ch.get("timeframe", 5))
        costs = costs_for(a, df)
        flt = TradeFilter.from_dict(ch.get("filter"))
        name = SLOTS[slot]["name"]
        if a.cmd == "backtest":
            res = run(df, ch["params"], costs, flt)
            print(f"\n[{name}] {ch.get('generation', 0)}세대 {ch.get('timeframe', 5)}분봉  {res.stats}")
            if not res.trades.empty:
                print(res.trades[["entry_time", "dir", "entry", "exit", "r", "reason"]].tail(5).to_string(index=False))
        elif a.cmd == "signal":
            sig = signals(df, ch["params"])
            last = sig.iloc[-1]
            d = int(last["signal"])
            if d != 0 and flt is not None:
                pr = float(flt.proba(sig[FEATURES].to_numpy()[-1:], sig["signal"].to_numpy()[-1:])[0])
                if pr < flt.threshold:
                    print(f"[{name}] {df.index[-1]}  신호 있으나 학습 필터가 거름 (승리 확률 추정 {pr:.0%})")
                    continue
            if d == 0:
                print(f"[{name}] {df.index[-1]}  신호 없음 (종가 {df['close'].iloc[-1]:.2f})")
                continue
            px = df["close"].iloc[-1]
            print(f"[{name}] {df.index[-1]}  {'매수' if d > 0 else '매도'} 신호  기준가 {px:.2f}  "
                  f"손절 {px - d * last['sl_dist']:.2f}  익절 {px + d * last['tp_dist']:.2f}  "
                  f"(최대 보유 {ch['params']['max_hold']}봉)")


def make_dashboard(a):
    import dashboard

    settings = {"risk": a.risk, "max_positions": a.max_positions, "leverage": a.leverage,
                "daily_loss": a.daily_loss, "improve_days": a.improve_days, "improve_hour": a.improve_hour}
    return dashboard.build(settings, a.symbol, a.mt5)


def single_instance():
    """run/auto 가 두 개 동시에 돌면 주문이 두 번 들어가므로, 잠금 파일로 하나만 허용한다."""
    STATE.mkdir(exist_ok=True)
    f = open(STATE / "run.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("\n[중지] 이미 다른 창에서 실행 중입니다. 그 창을 닫은 뒤 다시 실행하세요.")
        raise SystemExit(3)
    return f   # 프로그램이 끝날 때까지 열어 둬야 잠금이 유지된다


DAYS = "월화수목금토일"
LAST_IMPROVE = STATE / "last_improve.txt"


def improve_due(a) -> bool:
    """한 번도 발전한 적이 없거나, 아직 학습 안 한 전략 슬롯이 있거나(새 버전으로 바꾼 직후),
    정한 요일·시각이 지났고 그날 아직 발전하지 않았으면 True."""
    from evolve import CHAMPS

    from evolve import VERSION, load_champion as _lc

    if not LAST_IMPROVE.exists() or any(not (CHAMPS / f"{s}.json").exists() for s in SLOTS):
        return True
    if any(_lc(s).get("version", 1) < VERSION for s in SLOTS):   # 학습 방식이 바뀐 새 버전 → 바로 재학습
        return True
    now = datetime.now()
    days = {int(x) for x in str(a.improve_days).split(",") if x.strip()}
    if now.weekday() not in days or now.hour < a.improve_hour:
        return False
    try:
        return LAST_IMPROVE.read_text(encoding="utf-8").strip() != now.date().isoformat()
    except OSError:
        return True


def run_improve(a) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M}  자기 발전 사이클 실행 중 (전략 {len(SLOTS)}개, 몇 분 걸립니다)...")
    dfs = get_all(a)
    costs = costs_for(a, next(iter(dfs.values())))
    improve(dfs, costs, pop=a.pop, gens=a.gens, min_trades=a.min_trades, verbose=False)
    STATE.mkdir(exist_ok=True)
    LAST_IMPROVE.write_text(datetime.now().date().isoformat(), encoding="utf-8")
    print("  전략 상태:")
    print_status()


def run_forever(a, risk, auto=False):
    """1분마다 확인해 선택된 봉 단위의 새 봉을 처리하고, 정한 요일에 스스로 전략을 갱신하며, 주간 리포트를 다시 쓴다.

    auto=True 이면 모의매매와 함께 MT5에 실제 주문도 넣는다.
    """
    print(f"{'자동매매' if auto else '실시간 모의매매'} 시작 (Ctrl+C로 종료). 위험 {risk.risk_pct}%/건, "
          f"전체 최대 {risk.max_positions}개·전략당 {risk.max_per_strategy}개, 레버리지 {risk.leverage:g}배, "
          f"하루 손실 한도 {risk.daily_loss_pct}%")
    names = ",".join(DAYS[int(x)] for x in str(a.improve_days).split(",") if x.strip())
    print(f"자기 발전: 매주 {names}요일 {a.improve_hour}시 이후 각 1회 (PC 시간)")
    print("기록: state/journal.csv (모의)" + (", state/live_journal.csv (실주문)" if auto else "")
          + ", 리포트: reports/, 대시보드: reports/dashboard.html (1분마다 갱신)")
    lock = single_instance()
    shown, opened = None, a.no_browser
    while True:
        try:
            if improve_due(a):
                run_improve(a)
            act = tuple(sorted(active_slots()))
            if act != shown:
                print("운용 중인 전략: " + (", ".join(SLOTS[s]["name"] for s in act) or "없음 (다음 학습까지 대기)"))
                shown = act
            dfs = {tf: get_data(a, tf) for tf in needed_tfs()}
            if dfs:
                costs = costs_for(a, next(iter(dfs.values())))
                step(dfs, costs, a.balance, risk, verbose=not auto)
                if auto:
                    import live
                    live.tick(dfs, a.symbol, risk, a.max_lot, a.allow_real)
            try:
                weekly()
            except SystemExit:
                pass
            out = make_dashboard(a)
            if not opened:
                import webbrowser
                webbrowser.open(out.resolve().as_uri())   # 처음 한 번 브라우저로 열기 (이후 페이지가 1분마다 새로고침)
                opened = True
        except SystemExit:
            raise
        except Exception as e:  # 연결 끊김 등은 다음 주기에 재시도
            print(f"오류: {e}")
        time.sleep(60 - time.time() % 60 + 3)   # 다음 1분봉 마감 직후 (1분봉 전략도 놓치지 않게)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        sys.exit(3)   # 사용자가 멈춘 것이므로 실행 파일이 재시작하지 않게
    except (RuntimeError, ImportError) as e:   # MT5 연결·심볼 문제 등은 짧은 안내만
        print(f"\n[오류] {e}")
        sys.exit(1)
