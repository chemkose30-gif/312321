"""보유 종목 현재가 (장중 실시간 확인용).

- 토스 키가 있으면 토스 일봉 조회에서 오늘 캔들의 종가(= 장중에는 현재가)를 쓴다.
- 없으면 네이버 일봉(FinanceDataReader)에서 오늘 캔들을 쓴다. 캐시는 거치지 않는다.
여러 화면에서 동시에 요청해도 증권사 API를 과하게 부르지 않도록 10초 동안은 결과를 재사용한다.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

_cache: Dict[Tuple[str, str], Tuple[float, dict]] = {}
_lock = threading.Lock()
TTL = 10.0


def toss_available() -> bool:
    return bool(os.getenv("TOSS_CLIENT_ID") and os.getenv("TOSS_CLIENT_SECRET"))


def _toss_last2(client, symbol: str) -> List[Tuple[str, float]]:
    return client.daily_closes(symbol, 2)


def _naver_last2(symbol: str) -> List[Tuple[str, float]]:
    from .krx import _fdr_fetch
    rows = _fdr_fetch(symbol, (date.today() - timedelta(days=10)).isoformat())
    return [(d, c) for d, c, _ in rows][-2:]


def live_quotes(symbols: List[str], prefer: str = "auto") -> Tuple[Dict[str, dict], str, Dict[str, str]]:
    """{종목: {price, date, prev_close, change}}, 사용한 소스, 실패 종목"""
    source = "toss" if (prefer in ("auto", "toss") and toss_available()) else "krx"
    out, errors, todo = {}, {}, []
    now = time.time()
    with _lock:
        for s in symbols:
            hit = _cache.get((source, s))
            if hit and now - hit[0] < TTL:
                out[s] = hit[1]
            else:
                todo.append(s)

    def one(sym, fetch):
        try:
            rows = fetch(sym)
            if not rows:
                raise RuntimeError("시세 없음")
            d, price = rows[-1]
            prev = rows[-2][1] if len(rows) > 1 else None
            q = {"price": price, "date": d, "prev_close": prev,
                 "change": (price / prev - 1) if prev else None}
            with _lock:
                _cache[(source, sym)] = (time.time(), q)
            out[sym] = q
        except Exception as e:  # noqa: BLE001
            errors[sym] = str(e)[:200]

    if todo:
        if source == "toss":
            from .toss_client import TossClient
            client = TossClient()
            for s in todo:  # 토스는 호출 한도가 있어 순서대로
                one(s, lambda sym: _toss_last2(client, sym))
        else:
            with ThreadPoolExecutor(max_workers=5) as ex:
                list(ex.map(lambda s: one(s, _naver_last2), todo))
    return out, source, errors


def market_open(now: datetime | None = None) -> bool:
    """한국 정규장(평일 09:00~15:30). 이 PC 시계가 한국 시간이라고 가정한다. 공휴일은 모른다."""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return (9, 0) <= (now.hour, now.minute) <= (15, 30)
