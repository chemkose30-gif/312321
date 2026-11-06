"""하루 단위 매매 루프와 백테스트(필터 사용 vs 미사용 비교)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .montecarlo import MCConfig, simulate_trade
from .portfolio import Costs, Portfolio
from .strategy import Strategy


class Engine:
    def __init__(self, portfolio: Portfolio, strategy: Strategy, mc_cfg: Optional[MCConfig] = None,
                 use_filter: bool = True, fixed_size: float = 0.2, seed: Optional[int] = None):
        self.pf = portfolio
        self.strategy = strategy
        self.mc_cfg = mc_cfg or MCConfig()
        self.use_filter = use_filter
        self.fixed_size = fixed_size  # 필터 미사용 시 종목당 고정 비중
        self.rng = np.random.default_rng(seed)

    def step(self, date: str, history: Dict[str, Sequence[float]]) -> List[str]:
        """history: 종목 → 오늘까지의 종가 목록. 오늘 종가(또는 현재가)에 체결한다고 가정."""
        log: List[str] = []
        prices = {s: float(h[-1]) for s, h in history.items() if len(h)}
        self.pf.last_prices.update(prices)

        # 1) 보유 종목 청산 점검
        for sym, pos in list(self.pf.positions.items()):
            if sym not in prices:
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
                log.append(f"[매도] {sym} {t['qty']}주 @ {t['price']:,.0f} ({reason}) "
                           f"손익 {t['pnl']:+,}원 ({t['pnl_pct']:+.2%})")

        # 2) 신규 진입
        equity = self.pf.equity(prices)
        for sym, hist in history.items():
            if sym in self.pf.positions or sym not in prices:
                continue
            sig = self.strategy.decide(sym, hist, holding=False)
            if sig.action != "BUY":
                continue
            if self.use_filter:
                res = simulate_trade(hist, sig.target_pct, sig.stop_pct, sig.horizon_days,
                                     self.pf.costs.round_trip(), self.mc_cfg, self.rng)
                if not res.approved:
                    log.append(f"[거절] {sym} 신호({sig.reason}) → 시뮬레이션 {res.reason}")
                    continue
                frac, note = res.size_fraction, f"{sig.reason} | MC {res.summary()}"
            else:
                frac, note = self.fixed_size, sig.reason
            t = self.pf.buy(sym, prices[sym], equity * frac, date, sig.target_pct, sig.stop_pct,
                            sig.horizon_days, note)
            if t:
                log.append(f"[매수] {sym} {t['qty']}주 @ {t['price']:,.0f} ({t['amount']:,}원) — {note}")
            else:
                log.append(f"[보류] {sym} 매수 신호지만 현금/비중 부족")

        self.pf.equity_history.append([date, round(self.pf.equity(prices))])
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
    engines = {
        "필터 사용 (전략 + 1000회 시뮬레이션)": Engine(Portfolio.new(capital, costs), strategy, mc_cfg, True, seed=seed),
        "필터 미사용 (전략 단독)": Engine(Portfolio.new(capital, costs), strategy, mc_cfg, False,
                                  fixed_size=mc_cfg.max_position, seed=seed),
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
