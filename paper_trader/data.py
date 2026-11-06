"""시세 데이터 소스. 모두 `daily_closes(symbol, count) -> [(YYYY-MM-DD, 종가), ...]` 형태."""
from __future__ import annotations

import csv
import zlib
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

import httpx
import numpy as np

Series = List[Tuple[str, float]]


class SyntheticSource:
    """키 없이 테스트용. 상승·하락·횡보 국면이 바뀌는 가상 주가를 만든다 (종목명·시드로 고정)."""

    def __init__(self, seed: int = 0):
        self.seed = seed

    def daily_closes(self, symbol: str, count: int = 750) -> Series:
        rng = np.random.default_rng(self.seed * 7919 + zlib.crc32(symbol.encode()))
        regimes = [(0.0010, 0.012), (-0.0012, 0.022), (0.0002, 0.015)]  # (일 평균, 일 변동성)
        rets, i = [], int(rng.integers(len(regimes)))
        while len(rets) < count:
            mu, vol = regimes[i]
            length = int(rng.integers(40, 160))
            rets.extend(rng.standard_t(5, length) * vol * np.sqrt(3 / 5) + mu)
            i = int(rng.integers(len(regimes)))
        prices = 50_000 * np.exp(np.cumsum(rets[:count]))
        days, d = [], date(2024, 1, 2)
        while len(days) < count:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += timedelta(days=1)
        return list(zip(days, np.round(prices, 0).tolist()))


class YahooSource:
    """무료 시세 (키 불필요). 국내 6자리 코드는 기본 .KS(코스피)를 붙인다. 코스닥은 '035720.KQ'처럼 직접 지정."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

    def daily_closes(self, symbol: str, count: int = 750) -> Series:
        ticker = f"{symbol}.KS" if symbol.isdigit() and len(symbol) == 6 else symbol
        rng = "5y" if count > 500 else "2y" if count > 250 else "1y"
        r = httpx.get(self.URL.format(ticker), params={"range": rng, "interval": "1d"},
                      headers={"User-Agent": "Mozilla/5.0 paper-trader"}, timeout=15)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        out = [(date.fromtimestamp(t).isoformat(), float(c)) for t, c in zip(res["timestamp"], closes)
               if c is not None]
        return out[-count:]


class CsvSource:
    """폴더 안의 {종목}.csv (헤더: date,close) 를 읽는다."""

    def __init__(self, folder: str = "data"):
        self.folder = Path(folder)

    def daily_closes(self, symbol: str, count: int = 750) -> Series:
        with open(self.folder / f"{symbol}.csv", newline="", encoding="utf-8") as f:
            rows = [(r["date"][:10], float(r["close"])) for r in csv.DictReader(f) if r.get("close")]
        return sorted(rows)[-count:]


def get_source(name: str, seed: int = 0, csv_dir: str = "data"):
    if name == "synthetic":
        return SyntheticSource(seed)
    if name == "yahoo":
        return YahooSource()
    if name == "csv":
        return CsvSource(csv_dir)
    if name == "krx":
        from .krx import KrxSource
        return KrxSource()
    if name == "toss":
        from .toss_client import TossClient
        return TossClient()
    raise ValueError(f"알 수 없는 데이터 소스: {name}")


def fetch_series(source: str, symbols, count: int, seed: int = 0, csv_dir: str = "data"):
    """여러 종목 시세를 가져온다. 실패한 종목은 errors 에 모은다."""
    src = get_source(source, seed, csv_dir)
    series, errors = {}, {}
    for s in symbols:
        try:
            series[s] = src.daily_closes(s, count)
        except Exception as e:  # noqa: BLE001 - 종목 하나 실패해도 나머지는 진행
            errors[s] = str(e)
    return series, errors
