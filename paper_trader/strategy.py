"""트레이더 전략 ("트레이더 AI" 자리).

`Strategy.decide()`만 같은 형태로 구현하면 LLM 기반 에이전트 등 어떤 전략이든 바꿔 끼울 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np


@dataclass
class Signal:
    action: str  # "BUY" | "SELL" | "HOLD"
    target_pct: float = 0.0  # 익절 기준 (+8% → 0.08)
    stop_pct: float = 0.0  # 손절 기준 (-4% → 0.04)
    horizon_days: int = 0  # 최대 보유 거래일
    reason: str = ""


class Strategy(Protocol):
    name: str

    def decide(self, symbol: str, closes: Sequence[float], holding: bool) -> Signal: ...


def rsi(closes: np.ndarray, period: int = 14) -> float:
    diff = np.diff(closes[-(period + 1):])
    gain = diff[diff > 0].sum() / period
    loss = -diff[diff < 0].sum() / period
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


class TrendMomentumStrategy:
    """단기 이동평균이 장기 이동평균 위 + 모멘텀 양수 + 과매수 아님 → 매수.

    보유 중에는 추세가 꺾이면(단기 < 장기) 매도. 손절/익절/기간 만료는 엔진이 처리한다.
    """

    name = "trend_momentum"

    def __init__(self, fast: int = 20, slow: int = 60, rsi_period: int = 14, rsi_max: float = 70,
                 target_pct: float = 0.08, stop_pct: float = 0.04, horizon_days: int = 20):
        self.fast, self.slow = fast, slow
        self.rsi_period, self.rsi_max = rsi_period, rsi_max
        self.target_pct, self.stop_pct, self.horizon_days = target_pct, stop_pct, horizon_days

    def decide(self, symbol: str, closes: Sequence[float], holding: bool) -> Signal:
        c = np.asarray(closes, dtype=float)
        if len(c) < self.slow + 1:
            return Signal("HOLD", reason="데이터 부족")
        sma_fast = c[-self.fast:].mean()
        sma_slow = c[-self.slow:].mean()
        if holding:
            if sma_fast < sma_slow:
                return Signal("SELL", reason=f"추세 이탈 (SMA{self.fast} < SMA{self.slow})")
            return Signal("HOLD")
        momentum = c[-1] / c[-self.fast - 1] - 1
        r = rsi(c, self.rsi_period)
        if sma_fast > sma_slow and momentum > 0 and r < self.rsi_max:
            return Signal("BUY", self.target_pct, self.stop_pct, self.horizon_days,
                          reason=f"상승 추세, {self.fast}일 모멘텀 {momentum:+.1%}, RSI {r:.0f}")
        return Signal("HOLD")
