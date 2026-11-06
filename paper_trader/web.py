"""모의투자 웹 API. main.py 에서 `app.include_router(router)` 로 붙인다.

실제 주문 기능은 없다. 계좌를 바꾸는 요청(매매 실행·초기화)은 이 PC(localhost)에서만 받는다.
"""
from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .data import fetch_series
from .engine import Engine, backtest, metrics
from .montecarlo import MCConfig, simulate_trade
from .portfolio import Costs, Portfolio
from .strategy import TrendMomentumStrategy

router = APIRouter(prefix="/api/trader", tags=["paper-trader"])
STATE = Path(os.getenv("PAPER_STATE", "paper_state.json"))
DEFAULT_SYMBOLS = ["005930", "000660", "035420", "005380", "051910"]
SOURCES = ["synthetic", "yahoo", "toss", "csv"]
_lock = threading.Lock()


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "계좌를 바꾸는 요청은 이 PC(localhost)에서만 할 수 있습니다.")


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise HTTPException(400, f"알 수 없는 데이터 소스: {source}")


def _thin(curve: list, n: int = 400) -> list:
    if len(curve) <= n:
        return curve
    idx = np.linspace(0, len(curve) - 1, n).round().astype(int)
    return [curve[i] for i in idx]


def _status(pf: Portfolio) -> dict:
    m = metrics(pf)
    eq = pf.equity({})
    positions = []
    for s, p in pf.positions.items():
        now = pf.last_prices.get(s, p.cost_per_share)
        positions.append({
            "symbol": s, "qty": p.qty, "cost": round(p.cost_per_share), "price": now,
            "value": round(p.qty * now), "pnl_pct": now / p.cost_per_share - 1,
            "stop": round(p.stop_price), "target": round(p.target_price),
            "days_held": p.days_held, "horizon": p.horizon_days, "entry_date": p.entry_date,
        })
    return {
        "exists": True, "initial_capital": pf.initial_capital, "cash": round(pf.cash),
        "equity": round(eq), "return": eq / pf.initial_capital - 1, "max_drawdown": m.max_drawdown,
        "closed_trades": m.trades, "win_rate": m.win_rate, "avg_pnl_pct": m.avg_pnl_pct,
        "positions": positions, "trades": pf.trades[-50:][::-1],
        "equity_history": _thin(pf.equity_history),
        "last_run": pf.equity_history[-1][0] if pf.equity_history else None,
    }


# ---------- 조회 ----------
@router.get("/config")
def config():
    return {
        "default_symbols": DEFAULT_SYMBOLS, "sources": SOURCES,
        "toss_configured": bool(os.getenv("TOSS_CLIENT_ID") and os.getenv("TOSS_CLIENT_SECRET")),
        "state_exists": STATE.exists(), "costs": Costs().__dict__, "mc": MCConfig().__dict__,
    }


@router.get("/status")
def status():
    if not STATE.exists():
        return {"exists": False}
    return _status(Portfolio.load(STATE))


@router.get("/toss-check")
def toss_check(symbol: str = "005930"):
    from .toss_client import TossClient, TossError
    try:
        rows = TossClient().daily_closes(symbol, 300)
    except TossError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "days": len(rows), "first": rows[0][0], "last": rows[-1][0], "last_close": rows[-1][1]}


# ---------- 계좌 변경 (localhost 전용) ----------
class RunReq(BaseModel):
    source: str = "toss"
    symbols: List[str] = Field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    use_filter: bool = True
    force: bool = False
    capital: float = 10_000_000
    paths: int = Field(1000, ge=100, le=20000)


@router.post("/run")
def run(req: RunReq, request: Request):
    _local_only(request)
    _check_source(req.source)
    with _lock:
        pf = Portfolio.load(STATE) if STATE.exists() else Portfolio.new(req.capital)
        today = date.today().isoformat()
        if pf.equity_history and pf.equity_history[-1][0] == today and not req.force:
            raise HTTPException(409, f"{today} 에 이미 실행했습니다. 다시 하려면 '강제 재실행'을 켜세요.")
        series, errors = fetch_series(req.source, req.symbols, 300, seed=42)
        if not series:
            raise HTTPException(502, {"message": "시세를 하나도 가져오지 못했습니다.", "errors": errors})
        history = {s: [c for _, c in rows] for s, rows in series.items()}
        eng = Engine(pf, TrendMomentumStrategy(), MCConfig(n_paths=req.paths), use_filter=req.use_filter)
        log = eng.step(today, history)
        pf.save(STATE)
    return {"log": log, "errors": errors, "status": _status(pf)}


class ResetReq(BaseModel):
    capital: float = Field(10_000_000, gt=0)


@router.post("/reset")
def reset(req: ResetReq, request: Request):
    _local_only(request)
    with _lock:
        if STATE.exists():
            STATE.replace(STATE.with_name(STATE.stem + f".bak-{date.today().isoformat()}.json"))
        pf = Portfolio.new(req.capital)
        pf.save(STATE)
    return _status(pf)


# ---------- 분석 ----------
class BacktestReq(BaseModel):
    source: str = "synthetic"
    symbols: List[str] = Field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    days: int = Field(750, ge=150, le=2500)
    seed: int = 42
    capital: float = Field(10_000_000, gt=0)
    paths: int = Field(1000, ge=100, le=5000)


@router.post("/backtest")
def run_backtest(req: BacktestReq):
    _check_source(req.source)
    series, errors = fetch_series(req.source, req.symbols, req.days, seed=req.seed)
    if not series:
        raise HTTPException(502, {"message": "시세를 하나도 가져오지 못했습니다.", "errors": errors})
    try:
        start, end, results, bh, bench = backtest(series, TrendMomentumStrategy(), req.capital, Costs(),
                                                  MCConfig(n_paths=req.paths), seed=req.seed)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = []
    for name, (pf, m) in results.items():
        out.append({"name": name, **m.__dict__, "curve": _thin(pf.equity_history),
                    "recent_trades": [t for t in pf.trades if t["side"] == "SELL"][-30:][::-1]})
    return {"start": start, "end": end, "symbols": list(series), "errors": errors,
            "benchmark": {"total_return": bh, "curve": _thin(bench)}, "results": out}


class MCReq(BaseModel):
    source: str = "synthetic"
    symbol: str = "005930"
    target_pct: float = Field(0.08, gt=0, lt=1)
    stop_pct: float = Field(0.04, gt=0, lt=1)
    horizon_days: int = Field(20, ge=1, le=120)
    paths: int = Field(1000, ge=100, le=20000)


@router.post("/mc")
def run_mc(req: MCReq):
    _check_source(req.source)
    series, errors = fetch_series(req.source, [req.symbol], 300, seed=42)
    if req.symbol not in series:
        raise HTTPException(502, errors.get(req.symbol, "시세 조회 실패"))
    closes = [c for _, c in series[req.symbol]]
    sig = TrendMomentumStrategy().decide(req.symbol, closes, holding=False)
    res = simulate_trade(closes, req.target_pct, req.stop_pct, req.horizon_days, Costs().round_trip(),
                         MCConfig(n_paths=req.paths), keep_returns=True)
    rets = np.asarray(res.returns or [0.0])
    counts, edges = np.histogram(rets, bins=40)
    sample_dates = [d for d, _ in series[req.symbol][-120:]]
    return {
        "symbol": req.symbol, "price": closes[-1], "signal": sig.__dict__,
        "result": {k: v for k, v in res.__dict__.items() if k != "returns"},
        "histogram": {"counts": counts.tolist(), "edges": edges.round(5).tolist()},
        "price_history": list(zip(sample_dates, closes[-120:])),
    }
