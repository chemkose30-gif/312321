"""매개변수화된 금(XAUUSD) 단타 전략: 추세 방향 + 되돌림 진입 + ATR 손절/익절.

  롱: 가격 > 추세 EMA, 빠른 EMA > 느린 EMA, (옵션) 가격 > VWAP,
      스토캐스틱이 과매도선을 아래에서 위로 돌파, 허용된 시간대, 경제지표 발표 전후 30분 제외
  숏: 위 조건의 반대
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd

from gold import news_blackout
from indicators import atr, ema, session_vwap, stochastic

# 진화가 탐색하는 범위: (최소, 최대, 정수 여부)
SPACE = {
    "ema_fast": (5, 20, True),
    "ema_slow": (15, 60, True),
    "ema_trend": (50, 300, True),
    "stoch_k": (5, 14, True),
    "stoch_low": (10.0, 35.0, False),
    "atr_sl": (0.8, 3.0, False),
    "rr": (1.0, 3.0, False),
    "max_hold": (6, 72, True),
    "start_hour": (0, 12, True),   # UTC
    "end_hour": (13, 23, True),    # UTC
    "use_vwap": (0, 1, True),
    "allow_short": (0, 1, True),
}

DEFAULT = {
    "ema_fast": 9, "ema_slow": 21, "ema_trend": 200, "stoch_k": 5, "stoch_low": 20.0,
    "atr_sl": 1.2, "rr": 1.5, "max_hold": 24, "start_hour": 7, "end_hour": 17,
    "use_vwap": 1, "allow_short": 1,
}

FEATURES = ["stoch", "dist_slow", "dist_vwap", "atr_ratio", "hour_sin", "hour_cos", "slope"]


def random_params(rng: random.Random) -> dict:
    p = {}
    for k, (lo, hi, is_int) in SPACE.items():
        p[k] = rng.randint(lo, hi) if is_int else round(rng.uniform(lo, hi), 2)
    return repair(p)


def mutate(p: dict, rng: random.Random, rate: float = 0.3) -> dict:
    q = dict(p)
    for k, (lo, hi, is_int) in SPACE.items():
        if rng.random() < rate:
            step = (hi - lo) * 0.15
            v = q[k] + rng.gauss(0, step)
            q[k] = int(round(min(hi, max(lo, v)))) if is_int else round(min(hi, max(lo, v)), 2)
    return repair(q)


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    return repair({k: (a[k] if rng.random() < 0.5 else b[k]) for k in SPACE})


def repair(p: dict) -> dict:
    if p["ema_slow"] <= p["ema_fast"]:
        p["ema_slow"] = p["ema_fast"] + 5
    if p["ema_trend"] <= p["ema_slow"]:
        p["ema_trend"] = p["ema_slow"] + 20
    return p


def signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """봉 마감 시점에 계산한 신호(+1 롱, -1 숏, 0 없음)와 진입 시 특징값."""
    c = df["close"]
    f, s, t = ema(c, p["ema_fast"]), ema(c, p["ema_slow"]), ema(c, p["ema_trend"])
    a = atr(df)
    st = stochastic(df, p["stoch_k"])
    vw = session_vwap(df)
    hour = df.index.hour
    in_session = (hour >= p["start_hour"]) & (hour < p["end_hour"]) & ~news_blackout(df.index)
    in_session &= ~((df.index.dayofweek == 4) & (hour >= 19))  # 금요일 마감 전 신규 진입 금지
    lo, hi = p["stoch_low"], 100 - p["stoch_low"]

    long_ = (c > t) & (f > s) & (st.shift() < lo) & (st >= lo) & in_session
    short = (c < t) & (f < s) & (st.shift() > hi) & (st <= hi) & in_session
    if p["use_vwap"]:
        long_ &= c > vw
        short &= c < vw
    if not p["allow_short"]:
        short &= False

    out = pd.DataFrame(index=df.index)
    out["signal"] = np.where(long_, 1, np.where(short, -1, 0))
    out["atr"] = a
    out["stoch"] = st / 100
    out["dist_slow"] = (c - s) / a
    out["dist_vwap"] = (c - vw) / a
    out["atr_ratio"] = a / a.rolling(288, min_periods=20).mean()
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    out["slope"] = f.diff(3) / a
    # 워밍업 구간 신호 제거
    out.iloc[: max(p["ema_trend"], 50), out.columns.get_loc("signal")] = 0
    return out.fillna(0)
