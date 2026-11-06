"""모의투자 웹 API. main.py 에서 `app.include_router(router)` 로 붙인다.

실제 주문 기능은 없다. 계좌를 바꾸는 요청(매매 실행·초기화)은 이 PC(localhost)에서만 받는다.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .data import fetch_series
from .krx import CACHE, cached_names
from .engine import Engine, backtest, metrics
from .montecarlo import MCConfig, seed_for, simulate_trade
from .portfolio import Costs, Portfolio
from .krx import KrxPrices
from .scanner import ScanConfig, scan, universe_history, validate
from .strategy import TrendMomentumStrategy

router = APIRouter(prefix="/api/trader", tags=["paper-trader"])
STATE = Path(os.getenv("PAPER_STATE", "paper_state.json"))
DEFAULT_SYMBOLS = ["005930", "000660", "035420", "005380", "051910"]
SOURCES = ["krx", "toss", "synthetic", "yahoo", "csv"]
WATCHLIST = Path(os.getenv("PAPER_WATCHLIST", "watchlist.json"))
SCAN_FILE = CACHE / "scan_latest.json"
_lock = threading.Lock()


def load_watchlist() -> List[str]:
    try:
        return [s for s in json.loads(WATCHLIST.read_text(encoding="utf-8")) if isinstance(s, str)]
    except (OSError, ValueError):
        return list(DEFAULT_SYMBOLS)


def save_watchlist(symbols: List[str]) -> List[str]:
    clean = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))[:300]
    WATCHLIST.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    return clean


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
    names = cached_names()
    m = metrics(pf)
    eq = pf.equity({})
    positions = []
    for s, p in pf.positions.items():
        now = pf.last_prices.get(s, p.cost_per_share)
        positions.append({
            "symbol": s, "name": names.get(s, ""), "qty": p.qty, "cost": round(p.cost_per_share), "price": now,
            "value": round(p.qty * now), "pnl_pct": now / p.cost_per_share - 1,
            "stop": round(p.stop_price), "target": round(p.target_price),
            "days_held": p.days_held, "horizon": p.horizon_days, "entry_date": p.entry_date,
        })
    return {
        "exists": True, "initial_capital": pf.initial_capital, "cash": round(pf.cash),
        "equity": round(eq), "return": eq / pf.initial_capital - 1, "max_drawdown": m.max_drawdown,
        "closed_trades": m.trades, "win_rate": m.win_rate, "avg_pnl_pct": m.avg_pnl_pct,
        "positions": positions,
        "trades": [{**t, "name": names.get(t["symbol"], "")} for t in pf.trades[-50:][::-1]],
        "equity_history": _thin(pf.equity_history),
        "last_run": pf.equity_history[-1][0] if pf.equity_history else None,
        "settings": {**DEFAULT_SETTINGS, **pf.settings},
    }


# ---------- 조회 ----------
@router.get("/config")
def config():
    return {
        "default_symbols": DEFAULT_SYMBOLS, "watchlist": load_watchlist(), "sources": SOURCES,
        "toss_configured": bool(os.getenv("TOSS_CLIENT_ID") and os.getenv("TOSS_CLIENT_SECRET")),
        "state_exists": STATE.exists(), "costs": Costs().__dict__, "mc": MCConfig().__dict__,
    }


@router.get("/status")
def status():
    if not STATE.exists():
        return {"exists": False, "settings": DEFAULT_SETTINGS}
    return _status(Portfolio.load(STATE))


@router.get("/positions/history")
def positions_history(days: int = 120, source: Optional[str] = None):
    """보유 종목 차트용: 종목별 최근 종가. 소스를 안 주면 마지막 매매에 쓴 소스."""
    if not STATE.exists():
        return {"positions": []}
    pf = Portfolio.load(STATE)
    if not pf.positions:
        return {"positions": []}
    src = source or pf.settings.get("source", "krx")
    _check_source(src)
    series, errors = fetch_series(src, list(pf.positions), max(days, 30), seed=42)
    names = cached_names()
    out = []
    for sym, p in pf.positions.items():
        rows = series.get(sym, [])[-days:]
        out.append({"symbol": sym, "name": names.get(sym, ""), "entry_date": p.entry_date,
                    "cost": p.cost_per_share, "stop": p.stop_price, "target": p.target_price,
                    "qty": p.qty, "days_held": p.days_held, "horizon": p.horizon_days,
                    "history": rows, "error": errors.get(sym)})
    return {"source": src, "positions": out}


@router.get("/quotes")
def quotes():
    """보유 종목 현재가와 실시간 평가. 계좌 파일은 바꾸지 않는다 (손절·익절 매매는 '오늘 매매 실행' 때)."""
    from .quotes import live_quotes, market_open
    if not STATE.exists():
        return {"positions": [], "market_open": market_open()}
    pf = Portfolio.load(STATE)
    syms = list(pf.positions)
    q, source, errors = live_quotes(syms) if syms else ({}, "", {})
    names = cached_names()
    rows, value = [], 0.0
    for sym, p in pf.positions.items():
        price = q.get(sym, {}).get("price") or pf.last_prices.get(sym) or p.cost_per_share
        value += p.qty * price
        alert = "손절선 도달" if price <= p.stop_price else "익절선 도달" if price >= p.target_price else None
        rows.append({"symbol": sym, "name": names.get(sym, ""), "price": price, "date": q.get(sym, {}).get("date"),
                     "change": q.get(sym, {}).get("change"), "value": round(p.qty * price),
                     "pnl_pct": price / p.cost_per_share - 1, "alert": alert, "live": sym in q})
    equity = pf.cash + value
    return {"fetched_at": datetime.now().strftime("%H:%M:%S"), "market_open": market_open(), "source": source,
            "equity": round(equity), "return": equity / pf.initial_capital - 1, "cash": round(pf.cash),
            "positions": rows, "errors": errors}


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
DEFAULT_SETTINGS = {"mode": "universe", "max_positions": 10, "max_new": 3, "min_turnover": 1e9, "position_pct": 0.10}


class RunReq(BaseModel):
    mode: str = "universe"  # universe: 국내 전 종목 | watchlist: 감시 종목만
    source: str = "krx"  # watchlist 모드에서만 사용 (전 종목은 항상 국내 무료 시세)
    symbols: List[str] = Field(default_factory=list)  # 비우면 감시 종목 목록 사용
    use_filter: bool = True
    force: bool = False
    capital: float = 10_000_000
    paths: int = Field(10000, ge=1000, le=50000)
    max_positions: int = Field(10, ge=1, le=50)
    max_new: int = Field(3, ge=1, le=20)
    position_pct: float = Field(0.10, gt=0, le=1)  # 종목당 매수 금액 = 총자산 × 이 비율
    min_turnover: float = Field(1e9, ge=0)


def _do_run(req: RunReq) -> dict:
    with _lock:
        pf = Portfolio.load(STATE) if STATE.exists() else Portfolio.new(req.capital)
        pf.settings = {"mode": req.mode, "max_positions": req.max_positions, "max_new": req.max_new,
                       "position_pct": req.position_pct, "source": req.source if req.mode == "watchlist" else "krx",
                       "min_turnover": req.min_turnover}
        if req.mode == "universe":
            day, history, names, info = universe_history(ScanConfig(min_turnover=req.min_turnover), KrxPrices(),
                                                         _progress, extra=list(pf.positions))
            errors, source = info["errors"], "krx"
        else:
            _progress("감시 종목 시세 받는 중", 0, 1)
            watch = save_watchlist(req.symbols) if req.symbols else load_watchlist()
            # 감시 목록에서 뺐더라도 보유 중인 종목은 손절·익절 점검을 위해 항상 포함
            symbols = list(dict.fromkeys(watch + list(pf.positions)))
            series, errors = fetch_series(req.source, symbols, 300, seed=42)
            history = {s: [c for _, c in rows] for s, rows in series.items()}
            day = max((rows[-1][0] for rows in series.values() if rows), default=date.today().isoformat())
            names, source = cached_names(), req.source
        if not history:
            raise RuntimeError(f"시세를 하나도 가져오지 못했습니다. 예: {list(errors.items())[:3]}")
        # 같은 날 시세로 두 번 돌리면 보유일이 이중으로 늘어나므로 막는다 (주말에 눌러도 금요일 시세면 한 번만)
        if pf.equity_history and pf.equity_history[-1][0] == day and not req.force:
            raise RuntimeError(f"{day} 시세로 이미 매매했습니다. 다음 거래일 장 마감 후에 다시 누르세요. "
                               "(꼭 다시 하려면 '강제 재실행')")
        _progress("매매 판단 중", 0, 1)
        eng = Engine(pf, TrendMomentumStrategy(), MCConfig(n_paths=req.paths), use_filter=req.use_filter,
                     max_positions=req.max_positions, max_new=req.max_new, names=names,
                     position_pct=req.position_pct)
        log = eng.step(day, history)
        pf.save(STATE)
    return {"day": day, "mode": req.mode, "source": source, "candidates": len(history), "log": log,
            "errors": dict(list(errors.items())[:20]), "error_count": len(errors),
            "finished": datetime.now().isoformat(timespec="seconds")}


@router.post("/run")
def run(req: RunReq, request: Request):
    _local_only(request)
    if req.mode not in ("universe", "watchlist"):
        raise HTTPException(400, "mode 는 universe 또는 watchlist")
    _check_source(req.source)
    _start_job("run", lambda: _do_run(req))
    return scan_status()


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
    paths: int = Field(10000, ge=1000, le=50000)


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
    paths: int = Field(10000, ge=1000, le=50000)
    mode: str = "scan"  # (예전 호환용) 이제 스캔과 계좌 매매가 같은 기준을 쓴다
    auto_target: bool = True  # True 면 익절선 3~10% 중 가장 좋은 값을 고른다 (스캔·계좌와 동일)


@router.post("/mc")
def run_mc(req: MCReq):
    _check_source(req.source)
    series, errors = fetch_series(req.source, [req.symbol], 300, seed=42)
    if req.symbol not in series:
        raise HTTPException(502, errors.get(req.symbol, "시세 조회 실패"))
    closes = [c for _, c in series[req.symbol]]
    sig = TrendMomentumStrategy().decide(req.symbol, closes, holding=False)
    cfg = MCConfig(n_paths=req.paths)  # 스캔·계좌 매매와 같은 기준
    day = series[req.symbol][-1][0]
    targets = cfg.target_choices if req.auto_target else req.target_pct
    res = simulate_trade(closes, targets, req.stop_pct, req.horizon_days, Costs().round_trip(),
                         cfg, np.random.default_rng(seed_for(req.symbol, day)), keep_returns=True)
    rets = np.asarray(res.returns or [0.0])
    counts, edges = np.histogram(rets, bins=40)
    sample_dates = [d for d, _ in series[req.symbol][-120:]]
    return {
        "symbol": req.symbol, "name": cached_names().get(req.symbol, ""), "price": closes[-1], "date": day,
        "mode": req.mode, "auto_target": req.auto_target,
        "config": {"drift_shrink": cfg.drift_shrink, "min_edge": cfg.min_edge},
        "signal": sig.__dict__,
        "result": {k: v for k, v in res.__dict__.items() if k != "returns"},
        "histogram": {"counts": counts.tolist(), "edges": edges.round(5).tolist()},
        "price_history": list(zip(sample_dates, closes[-120:])),
    }


# ---------- 감시 종목 ----------
class WatchReq(BaseModel):
    symbols: List[str] = Field(default_factory=list)
    add: List[str] = Field(default_factory=list)


@router.get("/watchlist")
def get_watchlist():
    return {"symbols": load_watchlist()}


@router.post("/watchlist")
def set_watchlist(req: WatchReq, request: Request):
    _local_only(request)
    base = req.symbols or load_watchlist()
    return {"symbols": save_watchlist(base + req.add)}


# ---------- 국내 전 종목 스캔 (백그라운드) ----------
_scan = {"running": False, "kind": None, "phase": "", "done": 0, "total": 0, "error": None, "started": None}
_scan_lock = threading.Lock()


def _progress(phase: str, done: int, total: int) -> None:
    _scan.update(phase=phase, done=done, total=total)


JOB_FILES = {"scan": SCAN_FILE, "validate": CACHE / "validate_latest.json", "run": CACHE / "run_latest.json"}


def _job(kind: str, fn) -> None:
    try:
        result = fn()
        CACHE.mkdir(exist_ok=True)
        JOB_FILES[kind].write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        _scan.update(error=None)
    except Exception as e:  # noqa: BLE001
        _scan.update(error=str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")
    finally:
        _scan.update(running=False)


def _start_job(kind: str, fn) -> None:
    with _scan_lock:
        if _scan["running"]:
            raise HTTPException(409, "다른 작업(스캔·검증·매매)이 진행 중입니다. 끝난 뒤 다시 눌러 주세요.")
        _scan.update(running=True, kind=kind, phase="시작", done=0, total=0, error=None,
                     started=datetime.now().isoformat(timespec="seconds"))
    threading.Thread(target=_job, args=(kind, fn), daemon=True).start()


class ScanReq(BaseModel):
    min_turnover: float = Field(1e9, ge=0)
    top: int = Field(50, ge=5, le=300)
    workers: int = Field(6, ge=1, le=16)


@router.post("/scan")
def start_scan(req: ScanReq, request: Request):
    _local_only(request)
    cfg = ScanConfig(min_turnover=req.min_turnover, top=req.top, workers=req.workers)
    _start_job("scan", lambda: scan(cfg, progress=_progress))
    return scan_status()


class ValidateReq(BaseModel):
    min_turnover: float = Field(1e9, ge=0)
    n_dates: int = Field(12, ge=2, le=40)
    top_k: int = Field(10, ge=1, le=50)


@router.post("/scan/validate")
def start_validate(req: ValidateReq, request: Request):
    _local_only(request)
    cfg = ScanConfig(min_turnover=req.min_turnover)
    _start_job("validate", lambda: validate(cfg, n_dates=req.n_dates, top_k=req.top_k, progress=_progress))
    return scan_status()


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@router.get("/scan/status")
def scan_status():
    idle = not _scan["running"]
    return {**_scan, "latest": _read(SCAN_FILE) if idle else None,
            "validation": _read(JOB_FILES["validate"]) if idle else None,
            "run": _read(JOB_FILES["run"]) if idle else None}
