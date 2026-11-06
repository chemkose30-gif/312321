"""국내(코스피·코스닥) 종목 목록과 일봉 시세. 해외 주식은 다루지 않는다.

- 종목 목록: FinanceDataReader 가 쓰는 KRX 상장종목 일별 캐시(GitHub)를 직접 읽는다.
- 일봉: FinanceDataReader(네이버, 수정주가)로 받아 cache/krx/{코드}.csv 에 저장하고,
  다음부터는 마지막 날짜 이후만 이어 받는다.
"""
from __future__ import annotations

import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

CACHE = Path("cache")
LISTING_URL = ("https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/refs/heads/master/"
               "data/listing/krx/{}.csv")
DOMESTIC_MARKETS = {"KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"}  # 코넥스·해외 제외
Row = Tuple[str, float, float]  # (날짜, 종가, 거래량)


# ---------------- 종목 목록 ----------------
def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_listing(text: str) -> List[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        if not r.get("Code") or not r.get("Name"):
            continue
        rows.append({
            "code": r["Code"].strip(), "name": r["Name"].strip(), "market": (r.get("Market") or "").strip(),
            "dept": (r.get("Dept") or "").strip(), "close": _num(r.get("Close")),
            "amount": _num(r.get("Amount")), "marcap": _num(r.get("Marcap")),
        })
    return rows


def _usable(rows: List[dict]) -> bool:
    """장 시작 전 등에는 가격·거래대금이 비어 있는 파일이 올라오므로 걸러낸다."""
    return len(rows) > 1000 and sum(r["amount"] > 0 for r in rows) > len(rows) * 0.5


def load_listing(cache_dir: Path = CACHE) -> Tuple[str, List[dict]]:
    """가장 최근 영업일의 전체 상장종목. 하루 한 번만 내려받고 나머지는 캐시를 쓴다."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    for back in range(0, 12):
        d = (today - timedelta(days=back)).isoformat()
        f = cache_dir / f"listing-{d}.csv"
        if f.exists():
            rows = _parse_listing(f.read_text(encoding="utf-8"))
            if _usable(rows):
                return d, rows
        try:
            r = httpx.get(LISTING_URL.format(d), timeout=20)
        except httpx.HTTPError as e:
            raise RuntimeError(f"종목 목록을 받지 못했습니다: {e}") from e
        if r.status_code == 200 and _usable(rows := _parse_listing(r.text)):
            f.write_text(r.text, encoding="utf-8")
            for old in cache_dir.glob("listing-*.csv"):
                if old != f:
                    old.unlink(missing_ok=True)
            return d, rows
    raise RuntimeError("최근 12일 안의 종목 목록을 찾지 못했습니다.")


def cached_names(cache_dir: Path = CACHE) -> Dict[str, str]:
    """네트워크 없이, 이미 받아 둔 종목 목록에서 코드→이름."""
    for f in sorted(cache_dir.glob("listing-*.csv"), reverse=True):
        return {r["code"]: r["name"] for r in _parse_listing(f.read_text(encoding="utf-8"))}
    return {}


def domestic_universe(listing: List[dict], min_amount: float = 0) -> Tuple[List[dict], Dict[str, int]]:
    """국내 보통주만 남긴다: 코넥스·우선주·스팩·관리종목·투자주의환기종목 제외."""
    out, skipped = [], {"코넥스/기타시장": 0, "우선주": 0, "스팩": 0, "관리·환기종목": 0, "당일 거래대금 미달": 0}
    for r in listing:
        if r["market"] not in DOMESTIC_MARKETS:
            skipped["코넥스/기타시장"] += 1
        elif not r["code"].endswith("0"):  # 우선주는 끝자리가 5, 7, 9, K 등
            skipped["우선주"] += 1
        elif "스팩" in r["name"] or "SPAC" in r["dept"]:
            skipped["스팩"] += 1
        elif "관리" in r["dept"] or "환기" in r["dept"]:
            skipped["관리·환기종목"] += 1
        elif r["amount"] < min_amount:
            skipped["당일 거래대금 미달"] += 1
        else:
            out.append(r)
    return out, skipped


# ---------------- 일봉 캐시 ----------------
def _fdr_fetch(code: str, start: str) -> List[Row]:
    import FinanceDataReader as fdr  # 무거워서 필요할 때만 불러온다

    df = fdr.DataReader(code, start)
    out = []
    for idx, r in df.iterrows():
        c = float(r.get("Close", 0) or 0)
        if c > 0:
            out.append((idx.strftime("%Y-%m-%d"), c, float(r.get("Volume", 0) or 0)))
    return out


def last_close_time(now: Optional[datetime] = None) -> datetime:
    """가장 최근 장 마감(15:40) 시각. 공휴일은 고려하지 않는다(그날은 한 번 더 받을 뿐)."""
    now = now or datetime.now()
    t = now.replace(hour=15, minute=40, second=0, microsecond=0)
    if now < t:
        t -= timedelta(days=1)
    while t.weekday() >= 5:
        t -= timedelta(days=1)
    return t


class KrxPrices:
    def __init__(self, cache_dir: Path = CACHE / "krx", fetch: Optional[Callable[[str, str], List[Row]]] = None):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fetch = fetch or _fdr_fetch

    def _path(self, code: str) -> Path:
        return self.dir / f"{code}.csv"

    def read(self, code: str) -> List[Row]:
        p = self._path(code)
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8") as f:
            return [(r[0], float(r[1]), float(r[2])) for r in csv.reader(f) if r and r[0] != "date"]

    def _write(self, code: str, rows: List[Row]) -> None:
        with open(self._path(code), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "close", "volume"])
            w.writerows(rows)

    def is_fresh(self, code: str) -> bool:
        p = self._path(code)
        return p.exists() and datetime.fromtimestamp(p.stat().st_mtime) >= last_close_time()

    def history(self, code: str, days: int = 320) -> List[Row]:
        if self.is_fresh(code):
            return self.read(code)[-days:]
        rows = self.read(code)
        if rows:
            start = (date.fromisoformat(rows[-1][0]) - timedelta(days=10)).isoformat()
            new = self._fetch_retry(code, start)
            overlap = {d: c for d, c, _ in new}
            # 겹치는 날 가격이 다르면 액면분할 등으로 수정주가가 바뀐 것 → 전체 다시 받기
            if any(abs(overlap[d] / c - 1) > 0.005 for d, c, _ in rows[-10:] if d in overlap):
                rows, new = [], self._fetch_retry(code, self._full_start(days))
        else:
            new = self._fetch_retry(code, self._full_start(days))
        merged = {d: (d, c, v) for d, c, v in rows}
        merged.update({d: (d, c, v) for d, c, v in new})
        out = sorted(merged.values())[-max(days, 400):]
        self._write(code, out)
        return out[-days:]

    @staticmethod
    def _full_start(days: int) -> str:
        return (date.today() - timedelta(days=int(days * 1.5) + 20)).isoformat()

    def _fetch_retry(self, code: str, start: str) -> List[Row]:
        for attempt in range(3):
            try:
                return self.fetch(code, start)
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return []

    def bulk(self, codes: List[str], days: int = 320, workers: int = 6,
             progress: Optional[Callable[[int, int], None]] = None) -> Tuple[Dict[str, List[Row]], Dict[str, str]]:
        out, errors = {}, {}
        total, done = len(codes), 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.history, c, days): c for c in codes}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    out[c] = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors[c] = str(e)[:200]
                done += 1
                if progress:
                    progress(done, total)
        return out, errors


class KrxSource:
    """다른 데이터 소스와 같은 형태: daily_closes(code, count) -> [(날짜, 종가)]"""

    def __init__(self, prices: Optional[KrxPrices] = None):
        self.prices = prices or KrxPrices()

    def daily_closes(self, symbol: str, count: int = 300) -> List[Tuple[str, float]]:
        if not (len(symbol) == 6 and symbol[:4].isdigit()):
            raise ValueError(f"국내 종목코드(6자리)가 아닙니다: {symbol}")
        return [(d, c) for d, c, _ in self.prices.history(symbol, count)]
