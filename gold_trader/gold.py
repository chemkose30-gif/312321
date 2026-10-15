"""금(XAUUSD) 전용 설정: 계약 단위, 랏 계산, 미국 경제지표 발표 시간 거래 금지."""
from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

CONTRACT_OZ = 100        # 1랏 = 100온스 → 가격 1달러 변동 = 랏당 100달러
MIN_LOT, LOT_STEP = 0.01, 0.01
NY = ZoneInfo("America/New_York")
EVENTS_FILE = Path(__file__).parent / "events.csv"

# FOMC 금리 결정일 (뉴욕 14:00 발표). 연준 일정 발표 후 events.csv 또는 이 목록을 갱신하세요.
FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]


def lot_size(balance: float, risk_pct: float, sl_distance: float) -> float:
    """손절까지 거리(달러)에 맞춰 잔고의 risk_pct%만 잃도록 랏을 계산.

    최소 랏(0.01)으로도 위험이 한도를 넘으면 0을 돌려준다 (= 진입하지 않음).
    """
    if sl_distance <= 0:
        return 0.0
    lots = balance * risk_pct / 100 / (sl_distance * CONTRACT_OZ)
    lots = np.floor(round(lots / LOT_STEP, 6)) * LOT_STEP
    return round(float(lots), 2) if lots >= MIN_LOT else 0.0


def margin_usd(price: float, lots: float, leverage: float) -> float:
    """필요 증거금. 예: 4155달러, 0.01랏, 100배 → 41.55달러."""
    return price * CONTRACT_OZ * lots / leverage


def pnl_usd(direction: int, entry: float, exit_: float, lots: float) -> float:
    return direction * (exit_ - entry) * CONTRACT_OZ * lots


def _ny(d: date, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, time(hh, mm), tzinfo=NY)).tz_convert("UTC")


def events(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, str]]:
    """기간 안의 주요 발표 시각(UTC). NFP는 매월 첫 금요일로 자동 계산, CPI 등은 events.csv에서 읽음."""
    out = []
    d = date(start.year, start.month, 1)
    while d <= end.date():
        first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
        out.append((_ny(first_fri, 8, 30), "NFP"))
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    out += [(_ny(date.fromisoformat(x), 14, 0), "FOMC") for x in FOMC]
    if EVENTS_FILE.exists():
        with open(EVENTS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(r for r in f if not r.startswith("#")):
                hh, mm = map(int, row["ny_time"].split(":"))
                out.append((_ny(date.fromisoformat(row["date"]), hh, mm), row["name"]))
    return [(t, n) for t, n in out if start - pd.Timedelta("1D") <= t <= end + pd.Timedelta("1D")]


def news_blackout(index: pd.DatetimeIndex, before_min: int = 30, after_min: int = 30) -> np.ndarray:
    """발표 전후 구간에 해당하는 봉은 True."""
    mask = np.zeros(len(index), dtype=bool)
    if len(index) == 0:
        return mask
    for t, _ in events(index[0], index[-1]):
        mask |= (index >= t - pd.Timedelta(minutes=before_min)) & (index <= t + pd.Timedelta(minutes=after_min))
    return mask
