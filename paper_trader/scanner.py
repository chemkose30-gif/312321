"""국내 전 종목 스캔: 전략 신호가 난 종목만 1만 회 시뮬레이션으로 검증한다.

종목이 많을수록 "우연히 많이 오른 종목"이 상위에 오기 쉬우므로
- 과거 상승률을 절반만 믿고(drift_shrink=0.5),
- 승인 기준을 계좌 매매보다 엄격하게(기대수익 0.5% 이상) 두고,
- 순위는 기대수익이 아니라 위험 대비 기대수익(기대수익 / 최악 5% 평균손실)으로 매긴다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

import numpy as np

from .krx import KrxPrices, domestic_universe, load_listing
from .montecarlo import MCConfig, score_of, seed_for, simulate_overnight, simulate_trade
from .screens import SCREENS, Bars, Context, Screen, day_context
from .portfolio import Costs
from .strategy import TrendMomentumStrategy

Progress = Callable[[str, int, int], None]


@dataclass
class ScanConfig:
    min_turnover: float = 1e9  # 20일 평균 거래대금(원)
    min_history: int = 250  # 상장 후 최소 거래일 (신규 상장주 제외)
    workers: int = 6
    top: int = 50
    mc: MCConfig = field(default_factory=MCConfig)  # 계좌 매매와 같은 기준


def _evaluate(closes: List[float], strategy, cfg: ScanConfig, cost: float, code: str, day: str) -> Optional[dict]:
    sig = strategy.decide(code, closes, holding=False)
    if sig.action != "BUY":
        return None
    # 종목·날짜별 고정 시드 → 웹의 "상세"에서 같은 경로로 똑같은 결과를 다시 볼 수 있다
    rng = np.random.default_rng(seed_for(code, day))
    res = simulate_trade(closes, cfg.mc.target_choices, sig.stop_pct, sig.horizon_days, cost, cfg.mc, rng)
    return {"signal": sig.reason, "target_pct": res.target_pct, "stop_pct": sig.stop_pct,
            "horizon_days": sig.horizon_days, "approved": res.approved, "reason": res.reason,
            "win_rate": res.win_rate, "mean": res.mean, "median": res.median, "p5": res.p5,
            "cvar5": res.cvar5, "stop_rate": res.stop_rate, "size": res.size_fraction,
            "score": score_of(res.mean, res.cvar5)}


def _turnover(rows) -> float:
    tail = rows[-20:]
    return float(np.mean([r[1] * r[2] for r in tail])) if tail else 0.0


def collect(cfg: ScanConfig, prices: KrxPrices, progress: Progress) -> dict:
    """국내 보통주 목록 → 시세 받기 → 상장 기간·거래대금 조건 통과 종목만 남긴다."""
    progress("종목 목록 받는 중", 0, 1)
    listing_date, listing = load_listing()
    # 당일 거래대금이 기준의 30%도 안 되는 종목은 시세를 받기 전에 미리 뺀다
    universe, skipped = domestic_universe(listing, min_amount=cfg.min_turnover * 0.3)
    meta = {u["code"]: u for u in universe}
    full, errors = prices.bulk([u["code"] for u in universe], 320, cfg.workers,
                               lambda d, t: progress("시세 받는 중", d, t), full=True)
    histories = {code: [r[:3] for r in rows] for code, rows in full.items()}
    skipped |= {"상장 기간 부족": 0, "20일 평균 거래대금 미달": 0, "시세 조회 실패": len(errors)}
    eligible = {}
    for code, rows in histories.items():
        if len(rows) < cfg.min_history:
            skipped["상장 기간 부족"] += 1
        elif _turnover(rows) < cfg.min_turnover:
            skipped["20일 평균 거래대금 미달"] += 1
        else:
            eligible[code] = rows
    return {"listing_date": listing_date, "listed": len(listing), "universe": len(universe), "meta": meta,
            "eligible": eligible, "full": {code: full[code] for code in eligible}, "skipped": skipped,
            "errors": errors}


def evaluate_screen(scr: Screen, b: Bars, i: int, ctx: Context, cfg: ScanConfig, cost: float,
                    code: str, day: str) -> Optional[dict]:
    """검색식에 걸리면 그 매매 규칙대로 시뮬레이션해 판정한다."""
    if i < scr.min_bars:
        return None
    reason = scr.check(b, i, ctx)
    if not reason:
        return None
    rng = np.random.default_rng(seed_for(code, day))
    if scr.exit == "next_open":
        res = simulate_overnight(b.o[: i + 1], b.c[: i + 1], cost, cfg.mc, rng)
        target = None
    else:
        res = simulate_trade(b.c[: i + 1], cfg.mc.target_choices, scr.stop, scr.horizon, cost, cfg.mc, rng)
        target = res.target_pct
    return {"signal": reason, "target_pct": target, "stop_pct": scr.stop if scr.exit == "close" else None,
            "horizon_days": scr.horizon, "exit": scr.exit, "approved": res.approved, "reason": res.reason,
            "win_rate": res.win_rate, "mean": res.mean, "median": res.median, "p5": res.p5,
            "cvar5": res.cvar5, "stop_rate": res.stop_rate, "size": res.size_fraction,
            "score": score_of(res.mean, res.cvar5)}


def scan(cfg: Optional[ScanConfig] = None, prices: Optional[KrxPrices] = None,
         progress: Optional[Progress] = None, seed: Optional[int] = None, screen: str = "trend") -> dict:
    cfg = cfg or ScanConfig()
    prices = prices or KrxPrices()
    progress = progress or (lambda *a: None)
    started = datetime.now()
    c = collect(cfg, prices, progress)
    eligible, meta, errors = c["eligible"], c["meta"], c["errors"]

    strategy, cost = TrendMomentumStrategy(), Costs().round_trip()
    results = []
    if screen != "trend":
        scr = SCREENS[screen]
        bars = {code: Bars.from_rows(r) for code, r in c["full"].items()}
        index = {code: {d: k for k, d in enumerate(b.dates)} for code, b in bars.items()}
        day = max((b.dates[-1] for b in bars.values() if b.dates), default=c["listing_date"])
        ctxs = day_context(bars, index, day, {code: m["marcap"] for code, m in meta.items()})
    for i, (code, rows) in enumerate(eligible.items()):
        if i % 50 == 0:
            progress("시뮬레이션 중", i, len(eligible))
        closes = [x for _, x, _ in rows]
        if screen == "trend":
            ev = _evaluate(closes, strategy, cfg, cost, code, rows[-1][0])
        else:
            k = index[code].get(day)
            ev = None if k is None else evaluate_screen(scr, bars[code], k, ctxs.get(code, Context()), cfg,
                                                        cost, code, day)
        if ev:
            m = meta[code]
            results.append({"code": code, "name": m["name"], "market": m["market"], "price": closes[-1],
                            "date": rows[-1][0], "turnover": _turnover(rows), **ev})
    results.sort(key=lambda r: (not r["approved"], -r["score"]))
    approved = [r for r in results if r["approved"]]
    progress("완료", 1, 1)
    return {
        "as_of": max((r["date"] for r in results), default=c["listing_date"]), "listing_date": c["listing_date"],
        "started": started.isoformat(timespec="seconds"),
        "seconds": round((datetime.now() - started).total_seconds(), 1),
        "listed": c["listed"], "universe": c["universe"], "eligible": len(eligible),
        "signals": len(results), "approved": len(approved), "skipped": c["skipped"],
        "errors": dict(list(errors.items())[:20]), "error_count": len(errors),
        "config": {"min_turnover": cfg.min_turnover, "min_history": cfg.min_history, "mc": asdict(cfg.mc)},
        "results": approved[: cfg.top] + [r for r in results if not r["approved"]][:30],
        "screen": screen, "screen_name": SCREENS[screen].name if screen in SCREENS else "추세 추종 (기존)",
    }


def universe_history(cfg: ScanConfig, prices: KrxPrices, progress: Progress,
                     extra: List[str] = ()) -> tuple:
    """가상계좌 '전 종목' 매매용: 조건 통과 종목 + 보유 종목의 종가와 이름."""
    c = collect(cfg, prices, progress)
    rows = dict(c["eligible"])
    errors = dict(c["errors"])
    for code in extra:  # 보유 종목은 조건에서 빠졌더라도 손절·익절 점검을 위해 포함
        if code not in rows:
            try:
                rows[code] = prices.history(code, 320)
            except Exception as e:  # noqa: BLE001
                errors[code] = str(e)[:200]
    closes = {code: [x for _, x, _ in r] for code, r in rows.items() if r}
    names = {code: m["name"] for code, m in c["meta"].items()}
    day = max((r[-1][0] for r in rows.values() if r), default=c["listing_date"])
    return day, closes, names, {**c, "errors": errors}


# ---------------- 과거 시점 스캔 검증 ----------------
def realized_return(entry: float, future: List[float], target: float, stop: float,
                    horizon: int, cost: float) -> float:
    """시뮬레이션과 같은 규칙(종가 기준 익절/손절/기간 만료)으로 실제 결과를 계산한다."""
    for p in future[:horizon]:
        r = p / entry - 1
        if r <= -stop or r >= target:
            return r - cost
    return future[min(horizon, len(future)) - 1] / entry - 1 - cost


def _realized(scr_exit: str, b: Bars, i: int, target: float, stop: float, horizon: int, cost: float):
    if scr_exit == "next_open":
        if i + 1 >= len(b.c):
            return None
        return b.o[i + 1] / b.c[i] - 1 - cost
    future = list(b.c[i + 1: i + 1 + horizon])
    if len(future) < horizon:
        return None
    return realized_return(b.c[i], future, target, stop, horizon, cost)


def validate(cfg: Optional[ScanConfig] = None, prices: Optional[KrxPrices] = None,
             n_days: int = 120, trend_dates: int = 12, progress: Optional[Progress] = None,
             paths: int = 3000, **_) -> dict:
    """이미 받아 둔 시세로 과거 여러 날에 검색식을 돌려, 걸린 종목을 그 규칙대로 샀다면 어땠는지 본다.
    검색식마다 같은 매도 규칙으로 '아무 종목이나 샀을 때'와 비교한다.

    주의: 지금 상장된 종목만 쓰므로 그사이 상장폐지된 종목이 빠져 결과가 실제보다 좋게 나온다(생존 편향).
    속도를 위해 시뮬레이션 경로는 3,000개로 줄인다.
    """
    cfg = cfg or ScanConfig()
    prices = prices or KrxPrices()
    progress = progress or (lambda *a: None)
    cost = Costs().round_trip()
    vcfg = ScanConfig(min_turnover=cfg.min_turnover, min_history=cfg.min_history,
                      mc=MCConfig(**{**cfg.mc.__dict__, "n_paths": paths}))
    from .krx import cached_listing_meta
    marcap = {k: m.get("marcap", 0.0) for k, m in cached_listing_meta().items()}

    progress("받아 둔 시세 읽는 중", 0, 1)
    bars, index = {}, {}
    for f in sorted(prices.dir.glob("*.csv")):
        rows = prices.read_full(f.stem)
        if len(rows) >= cfg.min_history:
            b = Bars.from_rows(rows)
            bars[f.stem], index[f.stem] = b, {d: k for k, d in enumerate(b.dates)}
    if not bars:
        raise ValueError("받아 둔 시세가 없습니다. 먼저 전 종목 스캔을 한 번 실행하세요.")
    all_dates = sorted({d for b in bars.values() for d in b.dates})

    def summary(v):
        a = np.asarray([x for x in v if x is not None])
        if not len(a):
            return {"n": 0, "mean": None, "median": None, "win_rate": None}
        return {"n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
                "win_rate": float((a > 0).mean())}

    trend = TrendMomentumStrategy()
    plans = [("trend", "추세 추종 (기존)", "close", trend.horizon_days, trend.target_pct)] + \
            [(k, s.name, s.exit, s.horizon, s.default_target) for k, s in SCREENS.items()]
    out = []
    for n_plan, (key, name, exit_rule, horizon, default_target) in enumerate(plans):
        last_ok = len(all_dates) - horizon - 1
        if key == "trend":  # 추세 추종은 신호가 많고 보유가 길어 10일 간격 12개 시점만
            days = [all_dates[last_ok - k * 10] for k in range(trend_dates) if last_ok - k * 10 > 0][::-1]
        else:
            days = all_dates[max(0, last_ok - n_days + 1): last_ok + 1]
        groups = {"base": [], "hit": [], "approved": []}
        hits_per_day = []
        for n, day in enumerate(days):
            if n % 5 == 0:
                progress(f"과거 검증 · {name}", n_plan * 1000 + n, len(plans) * 1000)
            ctxs = day_context(bars, index, day, marcap) if key != "trend" else {}
            hits = 0
            for code, b in bars.items():
                i = index[code].get(day)
                if i is None or i < cfg.min_history - 1:
                    continue
                if np.mean(b.c[max(0, i - 19): i + 1] * b.v[max(0, i - 19): i + 1]) < cfg.min_turnover:
                    continue
                base = _realized(exit_rule, b, i, default_target, 0.04, horizon, cost)
                if base is None:
                    continue
                groups["base"].append(base)
                if key == "trend":
                    ev = _evaluate(list(b.c[: i + 1]), trend, vcfg, cost, code, day)
                else:
                    ev = evaluate_screen(SCREENS[key], b, i, ctxs.get(code, Context()), vcfg, cost, code, day)
                if not ev:
                    continue
                hits += 1
                groups["hit"].append(base)
                if ev["approved"]:
                    tgt = ev["target_pct"] if ev["target_pct"] is not None else default_target
                    groups["approved"].append(_realized(exit_rule, b, i, tgt, 0.04, horizon, cost))
            hits_per_day.append(hits)
        out.append({"key": key, "name": name, "exit": exit_rule, "horizon": horizon, "days": len(days),
                    "from": days[0] if days else None, "to": days[-1] if days else None,
                    "hits_per_day": float(np.mean(hits_per_day)) if hits_per_day else 0.0,
                    "base": summary(groups["base"]), "hit": summary(groups["hit"]),
                    "approved": summary(groups["approved"])})
    progress("완료", 1, 1)
    return {"stocks": len(bars), "screens": out, "paths": paths}
