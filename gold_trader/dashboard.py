"""대시보드: MT5 계좌, 거래 기록, 모의매매, AI 상태를 HTML 한 페이지로 만든다.

reports/dashboard.html 을 브라우저로 열면 되고, run/auto 실행 중에는 1분마다 갱신된다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from evolve import STATE, load_champions
from paper import JOURNAL, STATE_FILE

TEMPLATE = Path(__file__).parent / "dashboard_template.html"
OUT = Path(__file__).parent / "reports" / "dashboard.html"
from strategy import MAGIC_TO_SLOT, SLOTS


def _utc(ts: int, offset_h: float) -> str:
    """MT5 서버 시각(초) → UTC ISO 문자열 (브라우저가 현지 시간으로 표시)."""
    return pd.Timestamp(ts - offset_h * 3600, unit="s", tz="UTC").isoformat()


def collect_mt5(symbol: str) -> dict:
    import MetaTrader5 as mt5

    from data import _load_offsets, mt5_connect

    offset = _load_offsets().get(symbol, 0.0)
    mt5_connect(mt5)
    try:
        acc = mt5.account_info()
        positions = mt5.positions_get() or []
        deals = mt5.history_deals_get(datetime(2015, 1, 1), datetime.fromtimestamp(time.time() + 86400 * 2)) or []
    finally:
        mt5.shutdown()

    account = {
        "login": acc.login, "server": acc.server, "company": acc.company, "currency": acc.currency,
        "leverage": acc.leverage, "balance": acc.balance, "equity": acc.equity, "profit": acc.profit,
        "margin": acc.margin, "margin_free": acc.margin_free, "margin_level": acc.margin_level,
        "demo": acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
    }
    pos = [{
        "ticket": p.ticket, "time": _utc(p.time, offset), "symbol": p.symbol,
        "side": "매수" if p.type == mt5.POSITION_TYPE_BUY else "매도", "lots": p.volume,
        "open": p.price_open, "sl": p.sl, "tp": p.tp, "price": p.price_current,
        "profit": round(p.profit + p.swap, 2), "ai": p.magic in MAGIC_TO_SLOT,
        "strategy": SLOTS[MAGIC_TO_SLOT[p.magic]]["name"] if p.magic in MAGIC_TO_SLOT else "기타",
    } for p in positions]

    # 체결(deal)을 포지션 단위로 묶어 '완료된 거래' 목록을 만든다
    by_pos: dict[int, dict] = {}
    for d in deals:
        if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue  # 입출금, 크레딧 등은 거래가 아님
        t = by_pos.setdefault(d.position_id, {"profit": 0.0, "lots": 0.0, "symbol": d.symbol, "ai": False,
                                              "strategy": "기타"})
        t["profit"] += d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        if d.magic in MAGIC_TO_SLOT:
            t["ai"], t["strategy"] = True, SLOTS[MAGIC_TO_SLOT[d.magic]]["name"]
        if d.entry == mt5.DEAL_ENTRY_IN:
            t.update(open_time=_utc(d.time, offset), open=d.price,
                     side="매수" if d.type == mt5.DEAL_TYPE_BUY else "매도")
        else:
            t["lots"] += d.volume
            t.update(close_time=_utc(d.time, offset), close=d.price, reason=d.comment)
    open_ids = {p.ticket for p in positions}
    trades = [{"id": pid, **t, "profit": round(t["profit"], 2)} for pid, t in by_pos.items()
              if "close_time" in t and pid not in open_ids]
    trades.sort(key=lambda t: t["close_time"])
    return {"account": account, "positions": pos, "trades": trades}


def collect_paper() -> dict:
    out = {"trades": [], "positions": [], "balance": None, "start_balance": None}
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        out["balance"], out["start_balance"] = st.get("balance"), st.get("start_balance")
        out["positions"] = [{"time": pd.Timestamp(p["time"]).isoformat(), "side": "매수" if p["dir"] > 0 else "매도",
                             "strategy": SLOTS.get(p.get("slot"), {}).get("name", ""),
                             "lots": p["lots"], "open": round(p["entry"], 2), "sl": round(p["sl"], 2),
                             "tp": round(p["tp"], 2)} for p in st.get("positions", [])]
    if JOURNAL.exists():
        j = pd.read_csv(JOURNAL, encoding="utf-8-sig")
        opens: list[dict] = []
        for _, r in j.iterrows():
            if r["action"] == "진입":
                opens.append(r.to_dict())
            elif r["action"] == "청산":
                o = next((x for x in opens if x["side"] == r["side"]), None)  # 같은 방향 중 먼저 들어간 것부터
                if o:
                    opens.remove(o)
                out["trades"].append({
                    "open_time": pd.Timestamp(o["time"]).isoformat() if o else None,
                    "close_time": pd.Timestamp(r["time"]).isoformat(), "side": r["side"], "lots": r["lots"],
                    "open": o["price"] if o else None, "close": r["price"], "profit": float(r["pnl_usd"]),
                    "reason": r["reason"], "ai": True, "strategy": r.get("strategy") or "② 추세 5분봉"})
    return out


def collect_ai() -> dict:
    live_f = STATE / "live.json"
    paused = json.loads(live_f.read_text(encoding="utf-8")).get("paused", {}) if live_f.exists() else {}
    slots = []
    for slot, ch in load_champions().items():
        slots.append({"slot": slot, "name": SLOTS[slot]["name"], "kind": SLOTS[slot]["kind"],
                      "active": bool(ch.get("active")), "paused": slot in paused,
                      "generation": ch.get("generation", 0),
                      "timeframe": ch.get("timeframe", 5), "params": ch["params"], "filter": bool(ch.get("filter")),
                      "valid_stats": ch.get("valid_stats"), "by_timeframe": ch.get("by_timeframe"),
                      "promoted_at": ch.get("promoted_at")})
    hist = []
    f = STATE / "history.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines()[-20:]:
            e = json.loads(line)
            hist.append({"time": e["time"], "promoted": e["promoted"], "tf": e.get("challenger_timeframe", 5),
                         "slot": SLOTS.get(e.get("slot", "trend_5"), {}).get("name", ""),
                         "active": e.get("active"), "score": e["challenger_score"], "champ_score": e["champion_score"],
                         "win_rate": e["challenger_valid"].get("win_rate"), "trades": e["challenger_valid"].get("trades")})
    last = STATE / "last_improve.txt"
    return {"slots": slots, "last_improve": last.read_text(encoding="utf-8").strip() if last.exists() else None,
            "history": hist}


def build(settings: dict, symbol: str | None = None, use_mt5: bool = False) -> Path:
    payload = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(), "settings": settings,
               "mt5": None, "mt5_error": None, "paper": collect_paper(), "ai": collect_ai()}
    if use_mt5:
        try:
            payload["mt5"] = collect_mt5(symbol)
        except Exception as e:  # MT5가 꺼져 있어도 나머지는 보여준다
            payload["mt5_error"] = str(e)
    blob = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    return OUT
