"""한 번에 한 포지션만 드는 봉 단위 시뮬레이터. 결과는 R 배수(손절 폭 = 1R) 기준."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategy import FEATURES, signals


@dataclass
class Costs:
    spread: float = 0.30    # XAUUSD 스프레드 (달러). 브로커에 맞게 조정
    slippage: float = 0.10  # 진입/청산 각각에 붙는 미끄러짐 (달러)


@dataclass
class Result:
    trades: pd.DataFrame
    stats: dict = field(default_factory=dict)


def run(df: pd.DataFrame, p: dict, costs: Costs = Costs(), trade_filter=None) -> Result:
    sig = signals(df, p)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    s = sig["signal"].to_numpy()
    a = sig["atr"].to_numpy()
    feats = sig[FEATURES].to_numpy()
    if trade_filter is not None:
        allowed = trade_filter.allow(feats, s)
    else:
        allowed = np.ones(len(s), dtype=bool)

    half = costs.spread / 2 + costs.slippage
    rows = []
    n = len(df)
    next_free = 0
    for i in np.flatnonzero((s != 0) & allowed & (a > 0)):
        if i < next_free or i >= n - 1:
            continue
        d = s[i]
        e = i + 1                       # 다음 봉 시가에 진입
        entry = o[e] + d * half
        risk = a[i] * p["atr_sl"]
        sl = entry - d * risk
        tp = entry + d * risk * p["rr"]
        exit_px, j, reason = None, e, "time"
        last = min(n - 1, e + p["max_hold"])
        while j <= last:
            hit_sl = l[j] <= sl if d > 0 else h[j] >= sl
            hit_tp = h[j] >= tp if d > 0 else l[j] <= tp
            if hit_sl:                  # 같은 봉에서 둘 다 닿으면 손절로 본다 (보수적)
                exit_px, reason = sl, "sl"
                break
            if hit_tp:
                exit_px, reason = tp, "tp"
                break
            j += 1
        if exit_px is None:
            j = last
            exit_px = c[j]
        exit_px -= d * half
        r = d * (exit_px - entry) / risk
        rows.append((df.index[e], df.index[j], d, entry, exit_px, r, reason, *feats[i]))
        next_free = j + 1
    trades = pd.DataFrame(rows, columns=["entry_time", "exit_time", "dir", "entry", "exit", "r", "reason", *FEATURES])
    return Result(trades, stats(trades))


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0, "win_rate": 0.0, "total_r": 0.0, "avg_r": 0.0, "profit_factor": 0.0, "max_dd_r": 0.0}
    r = t["r"].to_numpy()
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, eq])[1:] - eq))
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": int(len(r)),
        "win_rate": round(float((r > 0).mean()), 3),
        "total_r": round(float(r.sum()), 2),
        "avg_r": round(float(r.mean()), 3),
        "profit_factor": round(float(gains / losses) if losses > 0 else 99.0, 2),
        "max_dd_r": round(dd, 2),
    }


def fitness(st: dict, min_trades: int = 30) -> float:
    """수익을 낙폭으로 나누고 거래 수가 적으면 강하게 깎는다. 운 좋은 소수 거래를 걸러내기 위함."""
    if st["trades"] < min_trades:
        return -100.0 + st["trades"]
    return st["total_r"] / (1.0 + st["max_dd_r"]) * min(1.0, np.sqrt(st["trades"] / 100))
