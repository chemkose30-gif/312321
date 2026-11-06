"""토스증권 Open API 시세 조회 전용 클라이언트.

- 주문·계좌 API는 의도적으로 구현하지 않는다 (실제 돈이 움직이지 않도록).
- 인증: POST /oauth2/token (grant_type=client_credentials) → Authorization: Bearer <token>
- 클라이언트당 유효한 토큰이 하나뿐이라 새로 발급하면 이전 토큰이 끊긴다고 알려져 있어,
  토큰을 파일에 캐시해 만료 전까지 재사용한다.

주의: 엔드포인트 경로와 응답 필드명은 공개 자료끼리 다른 부분이 있어 검증되지 않았다.
`python -m paper_trader toss-check` 로 실제 응답을 확인한 뒤 .env 의 TOSS_*_PATH 값으로 맞춘다.
공식 명세: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import httpx

BASE_URL = "https://openapi.tossinvest.com"
TOKEN_CACHE = Path(".toss_token.json")


class TossError(RuntimeError):
    pass


class TossClient:
    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise TossError(".env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 넣어 주세요.")
        self.auth_style = os.getenv("TOSS_AUTH_STYLE", "body")  # body | basic
        self.token_path = os.getenv("TOSS_TOKEN_PATH", "/oauth2/token")
        self.candles_path = os.getenv("TOSS_CANDLES_PATH", "/api/v1/candles")
        self.price_path = os.getenv("TOSS_PRICE_PATH", "/api/v1/prices")
        self.http = httpx.Client(base_url=BASE_URL, timeout=15)

    # ---- 인증 ----
    def _cached_token(self) -> Optional[str]:
        try:
            d = json.loads(TOKEN_CACHE.read_text())
        except (OSError, ValueError):
            return None
        if d.get("client_id") == self.client_id and d.get("expires_at", 0) > time.time() + 60:
            return d["access_token"]
        return None

    def _issue_token(self) -> str:
        data = {"grant_type": "client_credentials"}
        auth = None
        if self.auth_style == "basic":
            auth = (self.client_id, self.client_secret)
        else:
            data |= {"client_id": self.client_id, "client_secret": self.client_secret}
        r = self.http.post(self.token_path, data=data, auth=auth)
        if r.status_code != 200:
            raise TossError(f"토큰 발급 실패 {r.status_code}: {r.text[:300]}  "
                            "(403이면 IP 허용 목록 확인, 401이면 키 또는 TOSS_AUTH_STYLE 확인)")
        body = r.json()
        token = body.get("access_token")
        if not token:
            raise TossError(f"응답에 access_token 이 없습니다: {list(body)}")
        expires_in = int(body.get("expires_in", 3600))
        TOKEN_CACHE.write_text(json.dumps({"client_id": self.client_id, "access_token": token,
                                           "expires_at": time.time() + expires_in}))
        try:
            TOKEN_CACHE.chmod(0o600)
        except OSError:
            pass
        return token

    def _token(self) -> str:
        return self._cached_token() or self._issue_token()

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        for attempt in range(2):
            r = self.http.get(path, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            if r.status_code == 401 and attempt == 0:
                TOKEN_CACHE.unlink(missing_ok=True)  # 만료·폐기된 토큰 → 한 번만 재발급
                continue
            if r.status_code != 200:
                raise TossError(f"GET {path} 실패 {r.status_code}: {r.text[:300]}")
            return r.json()
        raise TossError("인증 재시도 실패")

    # ---- 시세 ----
    def daily_bars(self, symbol: str, count: int = 2) -> List[dict]:
        """최근 일봉 (date, open, high, low, close). 장중에는 오늘 캔들이 진행 중인 값."""
        raw = self.get(self.candles_path, {"symbol": symbol, "interval": "1d", "count": count})
        out = {}
        for row in _find_list(raw):
            close = _pick(row, "close", "closePrice", "tradePrice", "c")
            ts = _pick(row, "date", "baseDate", "datetime", "timestamp", "time", "t")
            if close is None or ts is None:
                continue
            c = float(close)
            out[_to_date(ts)] = {"date": _to_date(ts), "close": c,
                                 "open": float(_pick(row, "open", "openPrice", "o") or c),
                                 "high": float(_pick(row, "high", "highPrice", "h") or c),
                                 "low": float(_pick(row, "low", "lowPrice", "l") or c)}
        return [out[d] for d in sorted(out)]

    def daily_closes(self, symbol: str, count: int = 300) -> List[Tuple[str, float]]:
        """응답의 nextBefore 로 이전 페이지를 이어 받아 최대 count 일치를 모은다."""
        page = int(os.getenv("TOSS_CANDLE_PAGE", "100"))
        before_param = os.getenv("TOSS_BEFORE_PARAM", "before")
        found: dict = {}
        before = None
        for _ in range(20):  # 무한 루프 방지
            params = {"symbol": symbol, "interval": "1d", "count": min(page, count)}
            if before:
                params[before_param] = before
            raw = self.get(self.candles_path, params)
            added = 0
            for row in _find_list(raw):
                close = _pick(row, "close", "closePrice", "tradePrice", "c")
                ts = _pick(row, "date", "baseDate", "datetime", "timestamp", "time", "t")
                if close is None or ts is None:
                    continue
                d = _to_date(ts)
                if d not in found:
                    found[d] = float(close)
                    added += 1
            nxt = _find_key(raw, "nextBefore")
            # 새 데이터가 없거나(파라미터가 무시됨) 다음 페이지가 없으면 중단
            if len(found) >= count or not added or not nxt or nxt == before:
                break
            before = nxt
        if not found:
            raise TossError("캔들 응답을 해석하지 못했습니다. `toss-check` 로 원본 응답을 확인하세요.")
        return sorted(found.items())[-count:]


def _find_list(obj: Any) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("candles", "data", "items", "result", "output", "body"):
            if key in obj:
                found = _find_list(obj[key])
                if found:
                    return found
    return []


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _pick(row: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(row, dict) and row.get(k) is not None:
            return row[k]
    return None


def _to_date(ts: Any) -> str:
    if isinstance(ts, int) and 19000101 <= ts <= 21001231:  # 20261106 형태 숫자
        ts = str(ts)
    if isinstance(ts, (int, float)):
        sec = ts / 1000 if ts > 1e11 else ts
        return datetime.fromtimestamp(sec).strftime("%Y-%m-%d")
    s = str(ts)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]
