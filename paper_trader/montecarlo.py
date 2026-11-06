"""매수 신호 하나를 1만 개의 가상 미래에서 미리 거래해 보는 시뮬레이터.

과거 일간 로그수익률을 블록 단위로 재추출(block bootstrap)해 가격 경로를 만들고,
일부 경로는 변동성을 키워(스트레스) 급락장을 섞는다. 각 경로에서 신호의
익절/손절/보유기간 규칙대로 거래해 수익 분포를 얻는다.

한계: 과거 분포를 섞은 것이므로 처음 보는 사건은 반영하지 못한다.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Optional, Sequence, Union

import numpy as np


@dataclass
class MCConfig:
    n_paths: int = 10000  # 1000개면 경계 종목의 판정이 시드마다 뒤집힘(신호 종목의 약 1/3) → 1만 개
    lookback: int = 250  # 재추출에 쓸 과거 거래일 수
    block: int = 5  # 블록 길이(일). 수익률의 연속성(변동성 군집)을 일부 보존
    stress_share: float = 0.2  # 스트레스 경로 비율
    stress_vol: float = 1.8  # 스트레스 경로의 변동성 배수
    min_edge: float = 0.005  # 승인 조건: 비용 차감 후 기대수익 ≥ 0.5%
    max_tail_loss: float = 0.12  # 승인 조건: 최악 5% 평균손실(CVaR5) ≤ 12%
    kelly_fraction: float = 0.25  # 켈리 비중의 1/4만 사용
    max_position: float = 0.20  # 종목당 최대 계좌 비중
    drift_shrink: float = 0.5  # 과거 평균 상승률을 얼마나 깎을지 (0.5 = 절반만 믿음). 급등주 과대평가 방지
    target_choices: tuple = (0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10)  # 종목마다 이 중 가장 좋은 익절선 선택
    confidence_z: float = 2.0  # 기대수익에서 오차(표준오차 × 2)를 뺀 값이 기준을 넘어야 승인 → 경계 종목은 일관되게 거절


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
    se: float = 0.0  # 기대수익 추정의 표준오차 (시뮬레이션 자체의 흔들림)
    target_pct: float = 0.0  # 이 결과에 쓴 익절선
    by_target: Optional[list] = None  # 여러 익절선을 시험했을 때 각각의 결과

    def summary(self) -> str:
        return (f"익절 +{self.target_pct:.0%} | 승률 {self.win_rate:.0%} | 기대수익 {self.mean:+.2%}±{2 * self.se:.2%} | 중앙값 {self.median:+.2%} | "
                f"하위5% {self.p5:+.2%} | CVaR5 {self.cvar5:+.2%} | 손절 {self.stop_rate:.0%} "
                f"익절 {self.target_rate:.0%}")


def seed_for(symbol: str, day: str) -> int:
    """같은 종목·같은 날이면 같은 경로가 나오도록 고정된 시드."""
    return zlib.crc32(f"{symbol}|{day}".encode())


def _rejected(reason: str) -> MCResult:
    return MCResult(0, 0, 0, 0, 0, 0, 0, 0, False, 0.0, reason)


def _judge(net: np.ndarray, stop_first, target_first, target_pct: float, cfg: MCConfig,
           keep_returns: bool) -> MCResult:
    n = len(net)
    worst = np.sort(net)[: max(1, n // 20)]
    mean, var = float(net.mean()), float(net.var())
    kelly = mean / var if var > 0 else 0.0
    size = float(np.clip(kelly * cfg.kelly_fraction, 0.0, cfg.max_position))
    res = MCResult(
        n_paths=n, win_rate=float((net > 0).mean()), mean=mean, median=float(np.median(net)),
        p5=float(np.percentile(net, 5)), cvar5=float(worst.mean()),
        stop_rate=float(stop_first.mean()), target_rate=float(target_first.mean()),
        approved=False, size_fraction=size, reason="",
        returns=net.round(5).tolist() if keep_returns else None, se=float(np.sqrt(var / n)),
        target_pct=target_pct,
    )
    lower = mean - cfg.confidence_z * res.se
    if mean < cfg.min_edge:
        res.reason = f"기대수익 {mean:+.2%} < 기준 {cfg.min_edge:.2%}"
    elif lower < cfg.min_edge:
        res.reason = f"경계: 기대수익 {mean:+.2%}이지만 오차(±{cfg.confidence_z * res.se:.2%})를 빼면 기준 {cfg.min_edge:.2%} 미만"
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


def score_of(mean: float, cvar5: float) -> float:
    """위험 대비 기대수익: 기대수익 / 최악 5% 평균손실."""
    return mean / max(abs(cvar5), 0.01)


def simulate_trade(closes: Sequence[float], target_pct: Union[float, Sequence[float]], stop_pct: float,
                   horizon_days: int, round_trip_cost: float, cfg: Optional[MCConfig] = None,
                   rng: Optional[np.random.Generator] = None, keep_returns: bool = False) -> MCResult:
    """target_pct 에 여러 값(예: 3%~10%)을 주면 같은 가상 미래 경로에서 모두 시험해
    승인된 것 중 위험 대비 기대수익이 가장 좋은 익절선을 고른다."""
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
    rows = np.arange(n)
    hit_s = cum <= -stop_pct
    first_s = np.where(hit_s.any(1), hit_s.argmax(1), h)

    targets = [float(target_pct)] if np.isscalar(target_pct) else [float(t) for t in target_pct]
    results = []
    for tgt in targets:
        hit_t = cum >= tgt
        first_t = np.where(hit_t.any(1), hit_t.argmax(1), h)
        stop_first = first_s < first_t
        target_first = first_t < first_s
        exit_ret = cum[:, -1].copy()  # 기본: 보유기간 만료 시 종가 청산
        # 손절/익절은 조건을 처음 넘은 날의 종가에 체결 (갭 하락이면 손절선보다 더 잃을 수 있음)
        exit_ret[stop_first] = cum[rows[stop_first], first_s[stop_first]]
        exit_ret[target_first] = cum[rows[target_first], first_t[target_first]]
        results.append(_judge(exit_ret - round_trip_cost, stop_first, target_first, tgt, cfg, keep_returns))

    best = max(results, key=lambda r: (r.approved, score_of(r.mean, r.cvar5)))
    if len(results) > 1:
        best.by_target = [{"target_pct": r.target_pct, "mean": r.mean, "se": r.se, "win_rate": r.win_rate,
                           "cvar5": r.cvar5, "target_rate": r.target_rate, "stop_rate": r.stop_rate,
                           "approved": r.approved, "score": score_of(r.mean, r.cvar5)} for r in results]
    return best


def simulate_overnight(opens: Sequence[float], closes: Sequence[float], round_trip_cost: float,
                       cfg: Optional[MCConfig] = None, rng: Optional[np.random.Generator] = None,
                       keep_returns: bool = False) -> MCResult:
    """종가에 사서 다음 날 시가에 파는 거래(종가 베팅)를 미리 해 본다.
    과거 '종가 → 다음 날 시가' 수익률을 재추출하고, 일부는 변동성을 키운다."""
    cfg = cfg or MCConfig()
    rng = rng or np.random.default_rng()
    o = np.asarray(opens, dtype=float)[-(cfg.lookback + 1):]
    c = np.asarray(closes, dtype=float)[-(cfg.lookback + 1):]
    gaps = np.log(o[1:] / c[:-1])
    gaps = gaps[np.isfinite(gaps)]
    if len(gaps) < 30:
        return _rejected("시뮬레이션용 과거 데이터 부족")
    if cfg.drift_shrink:
        gaps = gaps - gaps.mean() * cfg.drift_shrink
    g = gaps[rng.integers(0, len(gaps), size=cfg.n_paths)]
    k = int(cfg.n_paths * cfg.stress_share)
    if k:
        mu = gaps.mean()
        g[:k] = mu + (g[:k] - mu) * cfg.stress_vol
    net = np.exp(g) - 1 - round_trip_cost
    zeros = np.zeros(len(net), dtype=bool)
    return _judge(net, zeros, zeros, 0.0, cfg, keep_returns)
