"""가격 데이터 로딩: CSV(MT5/TradingView 내보내기), yfinance, 또는 테스트용 합성 데이터."""
from __future__ import annotations

import numpy as np
import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def load_csv(path: str) -> pd.DataFrame:
    """time/open/high/low/close[/volume] 열을 가진 CSV를 읽는다. 시간은 UTC로 간주."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().lstrip("<").rstrip(">") for c in df.columns]
    # MT5 내보내기는 date, time 열이 따로 있다
    if "date" in df.columns and "time" in df.columns:
        df["time"] = df["date"].astype(str) + " " + df["time"].astype(str)
    tcol = next(c for c in ("time", "datetime", "date", "timestamp") if c in df.columns)
    df.index = pd.to_datetime(df[tcol], utc=True)
    for alt in ("tickvol", "tick_volume", "vol"):
        if "volume" not in df.columns and alt in df.columns:
            df["volume"] = df[alt]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df[COLS].astype(float).sort_index()


def load_yfinance(symbol: str = "GC=F", period: str = "60d", interval: str = "5m") -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"{symbol} 데이터를 받지 못했습니다 (네트워크/심볼 확인)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    return df[COLS].astype(float).dropna()


def synthetic(days: int = 90, seed: int = 0, start_price: float = 2400.0) -> pd.DataFrame:
    """국면 전환(추세/횡보)과 세션별 변동성을 흉내 낸 5분봉. 코드 동작 확인용일 뿐 실제 시장이 아니다."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-06", periods=days * 288, freq="5min", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    n = len(idx)
    hour = idx.hour.values
    session_vol = np.where((hour >= 7) & (hour < 17), 1.6, 0.7)  # 런던~뉴욕 시간대가 더 활발
    regime = np.zeros(n)
    drift = 0.0
    for i in range(n):
        if rng.random() < 1 / 600:
            drift = rng.choice([-1, 0, 0, 1]) * 0.00008
        regime[i] = drift
    ret = regime + rng.standard_t(4, n) * 0.00045 * session_vol
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.r_[start_price, close[:-1]]
    wick = np.abs(rng.normal(0, 0.0004, n)) * close * session_vol
    high = np.maximum(open_, close) + wick * rng.random(n)
    low = np.minimum(open_, close) - wick * rng.random(n)
    vol = rng.integers(50, 500, n) * session_vol
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


_offset_cache: dict = {}


def load_mt5(symbol: str = "XAUUSD", bars: int = 20000, server_offset: float | None = None) -> pd.DataFrame:
    """실행 중인 MetaTrader 5 터미널에서 5분봉을 읽는다 (Windows, pip install MetaTrader5).

    가격만 읽고 주문은 절대 넣지 않는다.
    """
    import time as _time

    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 연결 실패: {mt5.last_error()} (터미널이 켜져 있고 로그인돼 있는지 확인)")
    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"심볼 {symbol} 없음 (시장 감시 창의 이름 그대로 입력: 예 XAUUSD.GOLD)")
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, bars)
        if server_offset is None:
            server_offset = _offset_cache.get(symbol)
        if server_offset is None:
            # 장중이면 최신 틱의 서버 시각과 실제 UTC 차이로 서버 시간대를 추정한다
            tick = mt5.symbol_info_tick(symbol)
            gap = (tick.time - _time.time()) / 3600 if tick else None
            if gap is None or abs(gap - round(gap)) > 0.1:
                raise RuntimeError("서버 시간대를 자동으로 알 수 없습니다 (장 마감 중). --server-offset 3 처럼 지정하세요")
            server_offset = _offset_cache[symbol] = float(round(gap))
            print(f"MT5 서버 시간 = UTC{server_offset:+.0f} 로 감지")
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"{symbol} 데이터 없음 (브로커 심볼명 확인: XAUUSD, GOLD, XAUUSD.m 등)")
    df = pd.DataFrame(rates)
    # MT5 시각은 브로커 서버 시간(대부분 UTC+2/+3). server_offset 시간만큼 빼서 UTC로 맞춘다
    df.index = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=server_offset)
    df["volume"] = df["tick_volume"]
    return df[COLS].astype(float)
