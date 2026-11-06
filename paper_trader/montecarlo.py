"""매수 신호 하나를 1000개의 가상 미래에서 미리 거래해 보는 시뮬레이터.

과거 일간 로그수익률을 블록 단위로 재추출(block bootstrap)해 가격 경로를 만들고,
일부 경로는 변동성을 키워(스트레스) 급락장을 섞는다. 각 경로에서 신호의
익절/손절/보유기간 규칙대로 거래해 수익 분포를 얻는다.

한계: 과거 분포를 섞은 것이므로 처음 보는 사건은 반영하지 못한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np


@dataclass
class MCConfig:
    n_paths: int = 1000
    lookback: int = 250  # 재추출에 쓸 과거 거래일 수
    block: int = 5  # 블록 길이(일). 수익률의 연속성(변동성 군집)을 일부 보존
    stress_share: float = 0.2  # 스트레스 경로 비율
    stress_vol: float = 1.8  # 스트레스 경로의 변동성 배수
    min_edge: float = 0.003  # 승인 조건: 비용 차감 후 기대수익 ≥ 0.3%
    max_tail_loss: float = 0.12  # 승인 조건: 최악 5% 평균손실(CVaR5) ≤ 12%
    kelly_fraction: float = 0.25  # 켈리 비중의 1/4만 사용
    max_position: float = 0.20  # 종목당 최대 계좌 비중
    drift_shrink: float = 0.0  # 과거 평균 상승률을 얼마나 깎을지 (0.5 = 절반만 믿음). 급등주 과대평가 방지


@dataclass
class MCResult:
    n_paths: int
    win_rate: float
    mean: float
    median: float
    p5: float
    cvar5: float
    stop_rate: float
    target_rate: float
    approved: bool
    size_fraction: float
    reason: str
    returns: Optional[list] = None  # keep_returns=True 일 때 경로별 순수익률

    def summary(self) -> str:
        return (f"승률 {self.win_rate:.0%} | 기대수익 {self.mean:+.2%} | 중앙값 {self.median:+.2%} | "
                f"하위5% {self.p5:+.2%} | CVaR5 {self.cvar5:+.2%} | 손절 {self.stop_rate:.0%} "
                f"익절 {self.target_rate:.0%} | 비중 {self.size_fraction:.1%}")


def _rejected(reason: str) -> MCResult:
    return MCResult(0, 0, 0, 0, 0, 0, 0, 0, False, 0.0, reason)


def simulate_trade(closes: Sequence[float], target_pct: float, stop_pct: float, horizon_days: int,
                   round_trip_cost: float, cfg: Optional[MCConfig] = None,
                   rng: Optional[np.random.Generator] = None, keep_returns: bool = False) -> MCResult:
    cfg = cfg or MCConfig()
    rng = rng or np.random.default_rng()
    prices = np.asarray(closes, dtype=float)[-(cfg.lookback + 1):]
    rets = np.diff(np.log(prices))
    if cfg.drift_shrink and len(rets):
        rets = rets - rets.mean() * cfg.drift_shrink
    if len(rets) < max(30, cfg.block * 2) or horizon_days < 1:
        return _rejected("시뮬레이션용 과거 데이터 부족")

    n, h, b = cfg.n_paths, horizon_days, cfg.block
    n_blocks = -(-h // b)
    starts = rng.integers(0, len(rets) - b + 1, size=(n, n_blocks))
    idx = (starts[:, :, None] + np.arange(b)).reshape(n, -1)[:, :h]
    paths = rets[idx]

    k = int(n * cfg.stress_share)
    if k:
        mu = rets.mean()
        paths[:k] = mu + (paths[:k] - mu) * cfg.stress_vol

    cum = np.exp(np.cumsum(paths, axis=1)) - 1  # 진입가 대비 누적 수익률 (일별 종가)
    hit_t, hit_s = cum >= target_pct, cum <= -stop_pct
    first_t = np.where(hit_t.any(1), hit_t.argmax(1), h)
    first_s = np.where(hit_s.any(1), hit_s.argmax(1), h)
    stop_first = first_s < first_t
    target_first = first_t < first_s

    exit_ret = cum[:, -1].copy()  # 기본: 보유기간 만료 시 종가 청산
    rows = np.arange(n)
    # 손절/익절은 조건을 처음 넘은 날의 종가에 체결 (갭 하락이면 손절선보다 더 잃을 수 있음)
    exit_ret[stop_first] = cum[rows[stop_first], first_s[stop_first]]
    exit_ret[target_first] = cum[rows[target_first], first_t[target_first]]
    net = exit_ret - round_trip_cost

    worst = np.sort(net)[: max(1, n // 20)]
    mean, var = float(net.mean()), float(net.var())
    kelly = mean / var if var > 0 else 0.0
    size = float(np.clip(kelly * cfg.kelly_fraction, 0.0, cfg.max_position))

    res = MCResult(
        n_paths=n, win_rate=float((net > 0).mean()), mean=mean, median=float(np.median(net)),
        p5=float(np.percentile(net, 5)), cvar5=float(worst.mean()),
        stop_rate=float(stop_first.mean()), target_rate=float(target_first.mean()),
        approved=False, size_fraction=size, reason="",
        returns=net.round(5).tolist() if keep_returns else None,
    )
    if mean < cfg.min_edge:
        res.reason = f"기대수익 {mean:+.2%} < 기준 {cfg.min_edge:.2%}"
    elif res.cvar5 < -cfg.max_tail_loss:
        res.reason = f"꼬리위험 CVaR5 {res.cvar5:+.2%} > 허용 -{cfg.max_tail_loss:.0%}"
    elif size <= 0:
        res.reason = "켈리 비중 0"
    else:
        res.approved = True
        res.reason = "승인"
    if not res.approved:
        res.size_fraction = 0.0
    return res
