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
from .montecarlo import MCConfig, score_of, seed_for, simulate_trade
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
    return float(np.mean([c * v for _, c, v in tail])) if tail else 0.0


def collect(cfg: ScanConfig, prices: KrxPrices, progress: Progress) -> dict:
    """국내 보통주 목록 → 시세 받기 → 상장 기간·거래대금 조건 통과 종목만 남긴다."""
    progress("종목 목록 받는 중", 0, 1)
    listing_date, listing = load_listing()
    # 당일 거래대금이 기준의 30%도 안 되는 종목은 시세를 받기 전에 미리 뺀다
    universe, skipped = domestic_universe(listing, min_amount=cfg.min_turnover * 0.3)
    meta = {u["code"]: u for u in universe}
    histories, errors = prices.bulk([u["code"] for u in universe], 320, cfg.workers,
                                    lambda d, t: progress("시세 받는 중", d, t))
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
            "eligible": eligible, "skipped": skipped, "errors": errors}


def scan(cfg: Optional[ScanConfig] = None, prices: Optional[KrxPrices] = None,
         progress: Optional[Progress] = None, seed: Optional[int] = None) -> dict:
    cfg = cfg or ScanConfig()
    prices = prices or KrxPrices()
    progress = progress or (lambda *a: None)
    started = datetime.now()
    c = collect(cfg, prices, progress)
    eligible, meta, errors = c["eligible"], c["meta"], c["errors"]

    strategy, cost = TrendMomentumStrategy(), Costs().round_trip()
    results = []
    for i, (code, rows) in enumerate(eligible.items()):
        if i % 50 == 0:
            progress("시뮬레이션 중", i, len(eligible))
        closes = [x for _, x, _ in rows]
        ev = _evaluate(closes, strategy, cfg, cost, code, rows[-1][0])
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


def validate(cfg: Optional[ScanConfig] = None, prices: Optional[KrxPrices] = None,
             n_dates: int = 12, step: int = 10, top_k: int = 10,
             progress: Optional[Progress] = None, seed: Optional[int] = 0) -> dict:
    """이미 받아 둔 시세로 과거 여러 시점에 스캔을 돌려, 뽑힌 종목이 그 뒤 실제로 어땠는지 본다.

    주의: 지금 상장된 종목만 쓰므로 그사이 상장폐지된 종목이 빠져 결과가 실제보다 좋게 나온다(생존 편향).
    """
    cfg = cfg or ScanConfig()
    prices = prices or KrxPrices()
    progress = progress or (lambda *a: None)
    strategy, cost = TrendMomentumStrategy(), Costs().round_trip()
    horizon = strategy.horizon_days

    data, index = {}, {}
    for f in sorted(prices.dir.glob("*.csv")):
        rows = prices.read(f.stem)
        if len(rows) >= cfg.min_history + horizon + 5:
            data[f.stem] = rows
            index[f.stem] = {d: i for i, (d, _, _) in enumerate(rows)}
    if not data:
        raise ValueError("받아 둔 시세가 없습니다. 먼저 전 종목 스캔을 한 번 실행하세요.")

    all_dates = sorted({d for rows in data.values() for d, _, _ in rows})
    last_ok = len(all_dates) - horizon - 1
    test_dates = [all_dates[last_ok - k * step] for k in range(n_dates) if last_ok - k * step > 0][::-1]

    groups: Dict[str, List[float]] = {"전체 종목 (아무거나 샀다면)": [], "전략 신호 전체": [],
                                      "시뮬레이션 승인": [], f"승인 중 점수 상위 {top_k}": []}
    per_date = []
    for n, td in enumerate(test_dates):
        progress("과거 시점 스캔 중", n, len(test_dates))
        picks = []
        for code, rows in data.items():
            idx = index[code].get(td)
            if idx is None or idx < cfg.min_history:
                continue
            if _turnover(rows[: idx + 1]) < cfg.min_turnover:
                continue
            closes = [c for _, c, _ in rows[: idx + 1]]
            future = [c for _, c, _ in rows[idx + 1: idx + 1 + horizon]]
            if len(future) < horizon:
                continue
            # 비교 기준(아무거나/신호 전체)은 전략 기본 익절선, 승인 종목은 시뮬레이션이 고른 익절선으로 계산
            actual = realized_return(closes[-1], future, strategy.target_pct, strategy.stop_pct, horizon, cost)
            groups["전체 종목 (아무거나 샀다면)"].append(actual)
            ev = _evaluate(closes, strategy, cfg, cost, code, td)
            if ev and ev["approved"]:
                actual = realized_return(closes[-1], future, ev["target_pct"], strategy.stop_pct, horizon, cost)
            if ev:
                groups["전략 신호 전체"].append(actual)
                if ev["approved"]:
                    groups["시뮬레이션 승인"].append(actual)
                    picks.append((ev["score"], actual, ev["mean"]))
        picks.sort(reverse=True)
        top = picks[:top_k]
        groups[f"승인 중 점수 상위 {top_k}"].extend(a for _, a, _ in top)
        per_date.append({"date": td, "approved": len(picks),
                         "top_mean": float(np.mean([a for _, a, _ in top])) if top else None,
                         "predicted_mean": float(np.mean([m for _, _, m in top])) if top else None})
    progress("완료", 1, 1)

    def summary(v):
        a = np.asarray(v)
        if not len(a):
            return {"n": 0, "mean": None, "median": None, "win_rate": None}
        return {"n": int(len(a)), "mean": float(a.mean()), "median": float(np.median(a)),
                "win_rate": float((a > 0).mean())}

    return {"stocks": len(data), "dates": test_dates, "horizon": horizon,
            "groups": {k: summary(v) for k, v in groups.items()}, "per_date": per_date}
