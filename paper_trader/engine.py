"""하루 단위 매매 루프와 백테스트(필터 사용 vs 미사용 비교)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .montecarlo import MCConfig, seed_for, simulate_trade
from .portfolio import Costs, Portfolio
from .strategy import Strategy


class Engine:
    def __init__(self, portfolio: Portfolio, strategy: Strategy, mc_cfg: Optional[MCConfig] = None,
                 use_filter: bool = True, fixed_size: float = 0.2, seed: Optional[int] = None,
                 max_positions: int = 10, max_new: int = 3, names: Optional[Dict[str, str]] = None):
        self.pf = portfolio
        self.strategy = strategy
        self.mc_cfg = mc_cfg or MCConfig()
        self.use_filter = use_filter
        self.fixed_size = fixed_size  # 필터 미사용 시 종목당 고정 비중
        self.max_positions = max_positions  # 동시에 보유할 최대 종목 수
        self.max_new = max_new  # 하루 신규 매수 최대 종목 수
        self.names = names or {}

    def _label(self, sym: str) -> str:
        n = self.names.get(sym)
        return f"{n}({sym})" if n else sym

    def step(self, date: str, history: Dict[str, Sequence[float]]) -> List[str]:
        """history: 종목 → 오늘까지의 종가 목록. 오늘 종가(또는 현재가)에 체결한다고 가정."""
        log: List[str] = []
        prices = {s: float(h[-1]) for s, h in history.items() if len(h)}
        self.pf.last_prices.update(prices)

        # 1) 보유 종목 청산 점검
        for sym, pos in list(self.pf.positions.items()):
            if sym not in prices:
                log.append(f"[경고] {self._label(sym)} 오늘 시세가 없어 손절·익절 점검을 못 했습니다")
                continue
            p = prices[sym]
            pos.days_held += 1
            reason = None
            if p <= pos.stop_price:
                reason = "손절"
            elif p >= pos.target_price:
                reason = "익절"
            elif pos.days_held >= pos.horizon_days:
                reason = "보유기간 만료"
            else:
                sig = self.strategy.decide(sym, history[sym], holding=True)
                if sig.action == "SELL":
                    reason = sig.reason
            if reason:
                t = self.pf.sell(sym, p, date, reason)
                log.append(f"[매도] {self._label(sym)} {t['qty']}주 @ {t['price']:,.0f} ({reason}) "
                           f"손익 {t['pnl']:+,}원 ({t['pnl_pct']:+.2%})")

        # 2) 신규 진입 후보: 신호 → 시뮬레이션 승인 → 위험 대비 기대수익(점수) 순
        candidates, rejected = [], 0
        for sym, hist in history.items():
            if sym in self.pf.positions or sym not in prices:
                continue
            sig = self.strategy.decide(sym, hist, holding=False)
            if sig.action != "BUY":
                continue
            if self.use_filter:
                res = simulate_trade(hist, sig.target_pct, sig.stop_pct, sig.horizon_days,
                                     self.pf.costs.round_trip(), self.mc_cfg,
                                     np.random.default_rng(seed_for(sym, date)))
                if not res.approved:
                    rejected += 1
                    if len(history) <= 30:  # 종목이 적을 때만 거절 사유를 하나하나 적는다
                        log.append(f"[거절] {self._label(sym)} 신호({sig.reason}) → 시뮬레이션 {res.reason}")
                    continue
                score = res.mean / max(abs(res.cvar5), 0.01)
                candidates.append((score, sym, sig, res.size_fraction, f"{sig.reason} | MC {res.summary()}"))
            else:
                candidates.append((0.0, sym, sig, self.fixed_size, sig.reason))
        candidates.sort(key=lambda c: -c[0])
        if rejected and len(history) > 30:
            log.append(f"[거절] 매수 신호 {rejected + len(candidates)}종목 중 {rejected}종목은 시뮬레이션에서 거절")

        equity = self.pf.equity(prices)
        bought, waiting = 0, []
        for score, sym, sig, frac, note in candidates:
            if len(self.pf.positions) >= self.max_positions or bought >= self.max_new:
                waiting.append(self._label(sym))
                continue
            # 종목당 비중은 '1 / 최대 보유 종목 수'를 넘지 않게 해서 한도까지 고르게 담는다
            budget = equity * min(frac, 1 / self.max_positions)
            if self.pf.cash < budget * 0.5:
                log.append(f"[보류] {self._label(sym)} 승인됐지만 현금 부족 "
                           f"(필요 {budget:,.0f}원, 남은 현금 {self.pf.cash:,.0f}원)")
                continue
            t = self.pf.buy(sym, prices[sym], budget, date, sig.target_pct, sig.stop_pct,
                            sig.horizon_days, note)
            if t:
                bought += 1
                log.append(f"[매수] {self._label(sym)} {t['qty']}주 @ {t['price']:,.0f} ({t['amount']:,}원) "
                           f"점수 {score:.2f} — {note}")
            else:
                log.append(f"[보류] {self._label(sym)} 승인됐지만 현금 부족")
        if waiting:
            more = f" 외 {len(waiting) - 5}종목" if len(waiting) > 5 else ""
            log.append(f"[대기] 승인됐지만 보유 한도({self.max_positions}종목)·하루 매수 한도({self.max_new}종목) 때문에 "
                       f"못 산 종목: {', '.join(waiting[:5])}{more}")

        point = [date, round(self.pf.equity(prices))]
        if self.pf.equity_history and self.pf.equity_history[-1][0] == date:
            self.pf.equity_history[-1] = point  # 같은 날 강제 재실행 → 그날 기록을 덮어쓴다
        else:
            self.pf.equity_history.append(point)
        return log


@dataclass
class Metrics:
    final_equity: float
    total_return: float
    max_drawdown: float
    trades: int
    win_rate: float
    avg_pnl_pct: float


def metrics(pf: Portfolio) -> Metrics:
    eq = np.array([e for _, e in pf.equity_history], dtype=float) if pf.equity_history \
        else np.array([pf.initial_capital], dtype=float)
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min())
    closed = [t for t in pf.trades if t["side"] == "SELL"]
    wins = [t for t in closed if t["pnl"] > 0]
    return Metrics(
        final_equity=float(eq[-1]), total_return=float(eq[-1] / pf.initial_capital - 1),
        max_drawdown=mdd, trades=len(closed),
        win_rate=len(wins) / len(closed) if closed else 0.0,
        avg_pnl_pct=float(np.mean([t["pnl_pct"] for t in closed])) if closed else 0.0,
    )


def align(series: Dict[str, List[Tuple[str, float]]]) -> Tuple[List[str], Dict[str, List[float]]]:
    """모든 종목에 공통으로 있는 날짜만 남긴다."""
    common = set.intersection(*(set(d for d, _ in s) for s in series.values()))
    dates = sorted(common)
    closes = {sym: [c for d, c in sorted(s) if d in common] for sym, s in series.items()}
    return dates, closes


def backtest(series: Dict[str, List[Tuple[str, float]]], strategy: Strategy, capital: float,
             costs: Costs, mc_cfg: MCConfig, warmup: int = 120, seed: Optional[int] = None,
             verbose: bool = False):
    dates, closes = align(series)
    if len(dates) <= warmup:
        raise ValueError(f"데이터가 부족합니다: {len(dates)}일 (최소 {warmup + 1}일 필요)")
    slots = max(1, min(10, len(closes)))  # 종목이 적으면 그 수만큼 나눠 담는다 (5종목 → 종목당 최대 20%)
    engines = {
        "필터 사용 (전략 + 시뮬레이션)": Engine(Portfolio.new(capital, costs), strategy, mc_cfg, True,
                                        seed=seed, max_positions=slots),
        "필터 미사용 (전략 단독)": Engine(Portfolio.new(capital, costs), strategy, mc_cfg, False,
                                  fixed_size=mc_cfg.max_position, seed=seed, max_positions=slots),
    }
    for i in range(warmup, len(dates)):
        hist = {s: c[: i + 1] for s, c in closes.items()}
        for name, eng in engines.items():
            lines = eng.step(dates[i], hist)
            if verbose and lines:
                for line in lines:
                    print(f"{dates[i]} <{name[:5]}> {line}")

    # 벤치마크: 시작일에 균등 매수 후 보유
    bench = np.mean([np.asarray(c[warmup:]) / c[warmup] for c in closes.values()], axis=0) * capital
    bh = float(bench[-1] / capital - 1)
    results = {name: (eng.pf, metrics(eng.pf)) for name, eng in engines.items()}
    bench_curve = [[d, round(float(v))] for d, v in zip(dates[warmup:], bench)]
    return dates[warmup], dates[-1], results, bh, bench_curve
