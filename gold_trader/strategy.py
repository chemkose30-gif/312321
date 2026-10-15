"""금(XAUUSD) 단타 전략 3종류와 전략 슬롯 5개.

  trend    추세 되돌림: 추세 방향으로 잠깐 눌렸다 다시 갈 때 진입 (기존 전략)
  breakout 세션 돌파: 아시아 시간 고점·저점을 런던·뉴욕 시간에 뚫으면 따라감
  meanrev  평균 회귀: 볼린저 밴드 밖으로 크게 벗어나면 평균 쪽으로 진입 (강한 추세에서는 쉼)

모든 전략은 봉 마감 시점의 신호(+1 매수, -1 매도)와 함께 손절 거리(sl_dist)·익절 거리(tp_dist)를
돌려준다. 그래서 백테스트·모의매매·자동매매는 전략 종류를 몰라도 된다.
공통으로 경제지표 발표 전후 30분, 금요일 19시(UTC) 이후에는 진입하지 않는다.
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd

from gold import news_blackout
from indicators import atr, ema, session_vwap, stochastic

FEATURES = ["stoch", "dist_slow", "dist_vwap", "atr_ratio", "hour_sin", "hour_cos", "slope"]

# 진화가 탐색하는 범위: (최소, 최대, 정수 여부)
SPACES = {
    "trend": {
        "ema_fast": (5, 20, True), "ema_slow": (15, 60, True), "ema_trend": (50, 300, True),
        "stoch_k": (5, 14, True), "stoch_low": (10.0, 35.0, False),
        "atr_sl": (0.8, 3.0, False), "rr": (1.0, 3.0, False), "max_hold": (6, 72, True),
        "start_hour": (0, 12, True), "end_hour": (13, 23, True),
        "use_vwap": (0, 1, True), "allow_short": (0, 1, True),
    },
    "breakout": {
        "range_start": (0, 3, True), "range_len": (4, 8, True),   # 아시아 범위 (UTC)
        "trade_end": (10, 20, True),                               # 이 시각(UTC)까지만 돌파 진입
        "buffer": (0.0, 0.6, False),                               # 범위 밖으로 ATR의 몇 배 더 나가야 돌파로 볼지
        "ema_trend": (50, 300, True), "use_trend": (0, 1, True),   # 큰 추세 방향 돌파만
        "atr_sl": (0.8, 3.0, False), "rr": (1.0, 3.0, False), "max_hold": (6, 72, True),
        "allow_short": (0, 1, True),
    },
    "meanrev": {
        "band_len": (10, 60, True), "band_k": (1.5, 3.5, False),   # 볼린저 밴드
        "trend_len": (50, 300, True), "guard": (0.2, 2.5, False),  # 추세가 이보다 강하면 쉼
        "atr_sl": (0.8, 3.0, False), "tp_frac": (0.4, 1.2, False), # 익절 = 평균선까지 거리의 몇 배
        "max_hold": (6, 72, True), "start_hour": (0, 12, True), "end_hour": (13, 23, True),
        "allow_short": (0, 1, True),
    },
}

DEFAULTS = {
    "trend": {"ema_fast": 9, "ema_slow": 21, "ema_trend": 200, "stoch_k": 5, "stoch_low": 20.0,
              "atr_sl": 1.2, "rr": 1.5, "max_hold": 24, "start_hour": 7, "end_hour": 17,
              "use_vwap": 1, "allow_short": 1},
    "breakout": {"range_start": 0, "range_len": 7, "trade_end": 16, "buffer": 0.2, "ema_trend": 200,
                 "use_trend": 0, "atr_sl": 1.5, "rr": 2.0, "max_hold": 36, "allow_short": 1},
    "meanrev": {"band_len": 20, "band_k": 2.2, "trend_len": 100, "guard": 1.0, "atr_sl": 1.5,
                "tp_frac": 0.8, "max_hold": 24, "start_hour": 1, "end_hour": 20, "allow_short": 1},
}
for _k, _d in DEFAULTS.items():
    _d["kind"] = _k

# 전략 슬롯: 같은 종류라도 봉 단위가 다르면 다른 슬롯. magic 은 MT5 주문 구분 번호
SLOTS = {
    "trend_1": {"kind": "trend", "tfs": [1], "name": "① 추세 1분봉", "magic": 20261011},
    "trend_5": {"kind": "trend", "tfs": [5], "name": "② 추세 5분봉", "magic": 20261015},
    "trend_15": {"kind": "trend", "tfs": [15], "name": "③ 추세 15분봉", "magic": 20261012},
    "breakout": {"kind": "breakout", "tfs": [1, 5, 15], "name": "④ 세션 돌파", "magic": 20261013},
    "meanrev": {"kind": "meanrev", "tfs": [1, 5, 15], "name": "⑤ 평균 회귀", "magic": 20261014},
}
MAGIC_TO_SLOT = {v["magic"]: k for k, v in SLOTS.items()}

# 예전 버전 호환: 추세 전략 기본값
SPACE, DEFAULT = SPACES["trend"], DEFAULTS["trend"]


def kind_of(p: dict) -> str:
    return p.get("kind", "trend")


def random_params(rng: random.Random, kind: str = "trend") -> dict:
    p = {"kind": kind}
    for k, (lo, hi, is_int) in SPACES[kind].items():
        p[k] = rng.randint(lo, hi) if is_int else round(rng.uniform(lo, hi), 2)
    return repair(p)


def mutate(p: dict, rng: random.Random, rate: float = 0.3) -> dict:
    q = dict(p)
    q["kind"] = kind_of(p)
    for k, (lo, hi, is_int) in SPACES[q["kind"]].items():
        if rng.random() < rate:
            v = q.get(k, DEFAULTS[q["kind"]][k]) + rng.gauss(0, (hi - lo) * 0.15)
            q[k] = int(round(min(hi, max(lo, v)))) if is_int else round(min(hi, max(lo, v)), 2)
    return repair(q)


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    kind = kind_of(a)
    return repair({"kind": kind, **{k: (a[k] if rng.random() < 0.5 else b[k]) for k in SPACES[kind]}})


def repair(p: dict) -> dict:
    if kind_of(p) == "trend":
        if p["ema_slow"] <= p["ema_fast"]:
            p["ema_slow"] = p["ema_fast"] + 5
        if p["ema_trend"] <= p["ema_slow"]:
            p["ema_trend"] = p["ema_slow"] + 20
    return p


def _common_mask(df: pd.DataFrame) -> np.ndarray:
    hour = df.index.hour
    return ~news_blackout(df.index) & ~((df.index.dayofweek == 4) & (hour >= 19))


def _features(out: pd.DataFrame, df: pd.DataFrame, a, st, fast, slow, vw) -> None:
    c, hour = df["close"], df.index.hour
    out["stoch"] = st / 100
    out["dist_slow"] = (c - slow) / a
    out["dist_vwap"] = (c - vw) / a
    out["atr_ratio"] = a / a.rolling(288, min_periods=20).mean()
    out["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    out["slope"] = fast.diff(3) / a


def _trend(df, p):
    c = df["close"]
    f, s, t = ema(c, p["ema_fast"]), ema(c, p["ema_slow"]), ema(c, p["ema_trend"])
    a = atr(df)
    st = stochastic(df, p["stoch_k"])
    vw = session_vwap(df)
    hour = df.index.hour
    ok = (hour >= p["start_hour"]) & (hour < p["end_hour"])
    lo, hi = p["stoch_low"], 100 - p["stoch_low"]
    long_ = (c > t) & (f > s) & (st.shift() < lo) & (st >= lo) & ok
    short = (c < t) & (f < s) & (st.shift() > hi) & (st <= hi) & ok
    if p["use_vwap"]:
        long_ &= c > vw
        short &= c < vw
    sl = a * p["atr_sl"]
    return long_, short, sl, sl * p["rr"], a, (st, f, s, vw), max(p["ema_trend"], 50)


def _breakout(df, p):
    c, h, l = df["close"], df["high"], df["low"]
    a = atr(df)
    hour = df.index.hour
    day = df.index.floor("D")
    r0, r1 = p["range_start"], p["range_start"] + p["range_len"]
    in_range = (hour >= r0) & (hour < r1)
    rh = h.where(in_range).groupby(day).transform("max")
    rl = l.where(in_range).groupby(day).transform("min")
    ok = (hour >= r1) & (hour < p["trade_end"]) & rh.notna()
    up, dn = rh + a * p["buffer"], rl - a * p["buffer"]
    long_ = ok & (c > up) & (c.shift() <= up.shift())
    short = ok & (c < dn) & (c.shift() >= dn.shift())
    if p["use_trend"]:
        t = ema(c, p["ema_trend"])
        long_ &= c > t
        short &= c < t
    # 하루에 방향별 첫 돌파만
    long_ &= long_.astype(int).groupby(day).cumsum() == 1
    short &= short.astype(int).groupby(day).cumsum() == 1
    sl = a * p["atr_sl"]
    feats = (stochastic(df, 14), ema(c, 21), ema(c, 50), session_vwap(df))
    return long_, short, sl, sl * p["rr"], a, feats, max(p["ema_trend"] if p["use_trend"] else 0, 50)


def _meanrev(df, p):
    c, o = df["close"], df["open"]
    a = atr(df)
    mid = c.rolling(p["band_len"]).mean()
    sd = c.rolling(p["band_len"]).std()
    hour = df.index.hour
    trend_strength = (ema(c, p["trend_len"]).diff(5) / a).abs()
    ok = (hour >= p["start_hour"]) & (hour < p["end_hour"]) & (trend_strength < p["guard"])
    lower, upper = mid - p["band_k"] * sd, mid + p["band_k"] * sd
    # 밴드 밖으로 나갔다가 반대 방향 봉(반전 기미)이 나올 때
    long_ = ok & (c.shift() < lower.shift()) & (c > o)
    short = ok & (c.shift() > upper.shift()) & (c < o)
    sl = a * p["atr_sl"]
    tp = ((mid - c).abs() * p["tp_frac"]).clip(lower=a * 0.5)
    feats = (stochastic(df, 14), ema(c, 21), ema(c, 50), session_vwap(df))
    return long_, short, sl, tp, a, feats, max(p["band_len"], p["trend_len"], 50)


KIND_FUNCS = {"trend": _trend, "breakout": _breakout, "meanrev": _meanrev}


def signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """봉 마감 시점의 신호(+1/-1/0), 손절·익절 거리(달러), ATR, 학습 필터용 특징값."""
    long_, short, sl, tp, a, (st, f, s, vw), warmup = KIND_FUNCS[kind_of(p)](df, p)
    mask = _common_mask(df)
    long_ = long_ & mask
    short = short & mask
    if not p.get("allow_short", 1):
        short = short & False
    out = pd.DataFrame(index=df.index)
    out["signal"] = np.where(long_, 1, np.where(short, -1, 0))
    out["sl_dist"] = sl
    out["tp_dist"] = tp
    out["atr"] = a
    _features(out, df, a, st, f, s, vw)
    out.iloc[:warmup, out.columns.get_loc("signal")] = 0   # 지표가 자리 잡기 전 신호 제거
    return out.fillna(0)
