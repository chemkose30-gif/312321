from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def stochastic(df: pd.DataFrame, k: int = 5, smooth: int = 3) -> pd.Series:
    lo = df["low"].rolling(k).min()
    hi = df["high"].rolling(k).max()
    raw = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return raw.rolling(smooth).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """UTC 날짜마다 초기화되는 VWAP. 거래량이 없으면 대표가격의 누적 평균으로 대체."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"] if df["volume"].sum() > 0 else pd.Series(1.0, index=df.index)
    day = df.index.floor("D")
    return (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
