"""모의매매(페이퍼 트레이딩) 엔진: 새로 마감된 봉마다 챔피언 전략을 실행하고 모든 매수·매도를 장부에 남긴다.

state/paper.json   잔고, 보유 포지션, 마지막으로 처리한 봉
state/journal.csv  거래 장부 (진입·청산 한 줄씩)
"""
from __future__ import annotations

import csv
import json

import pandas as pd

from backtest import Costs
from evolve import STATE, load_champion
from gold import lot_size, pnl_usd
from learner import TradeFilter
from strategy import FEATURES, signals

STATE_FILE = STATE / "paper.json"
JOURNAL = STATE / "journal.csv"
JOURNAL_COLS = ["time", "action", "side", "price", "lots", "sl", "tp", "reason", "pnl_usd", "balance", "generation"]
BAR = pd.Timedelta(minutes=5)


def load_state(balance: float) -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"balance": balance, "start_balance": balance, "position": None, "pending": None, "last_bar": None}


def save_state(st: dict) -> None:
    STATE.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def write(row: dict) -> None:
    STATE.mkdir(exist_ok=True)
    new = not JOURNAL.exists()
    with open(JOURNAL, "a", newline="", encoding="utf-8-sig") as f:  # 엑셀에서 한글이 안 깨지게
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_COLS})


def step(df: pd.DataFrame, costs: Costs = Costs(), balance: float = 10_000, risk_pct: float = 1.0,
         now: pd.Timestamp | None = None, verbose: bool = True) -> dict:
    """아직 처리하지 않은 '마감된' 봉을 순서대로 처리한다. 몇 번을 호출해도 같은 봉을 두 번 처리하지 않는다."""
    st = load_state(balance)
    ch = load_champion()
    p, gen = ch["params"], ch.get("generation", 0)
    flt = TradeFilter.from_dict(ch.get("filter"))
    sig = signals(df, p)
    s = sig["signal"].to_numpy()
    if flt is not None:
        ok = flt.allow(sig[FEATURES].to_numpy(), s)
        s = s * ok
    now = now or pd.Timestamp.now(tz="UTC")
    closed = df.index + BAR <= now               # 아직 만들어지는 중인 봉은 건너뜀
    half = costs.spread / 2 + costs.slippage
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    atr_ = sig["atr"].to_numpy()
    last = pd.Timestamp(st["last_bar"]) if st["last_bar"] else None
    if last is None:                             # 처음 실행: 과거는 건너뛰고 지금부터 기록
        last = df.index[closed][-2] if closed.sum() > 1 else None

    def close_pos(t, px, reason):
        pos = st["position"]
        exit_px = px - pos["dir"] * half
        pnl = pnl_usd(pos["dir"], pos["entry"], exit_px, pos["lots"])
        st["balance"] = round(st["balance"] + pnl, 2)
        write({"time": t, "action": "청산", "side": "매수" if pos["dir"] > 0 else "매도",
               "price": round(exit_px, 2), "lots": pos["lots"], "reason": reason,
               "pnl_usd": round(pnl, 2), "balance": st["balance"], "generation": pos["generation"]})
        if verbose:
            print(f"{t}  청산 {reason}  {exit_px:.2f}  손익 {pnl:+.2f}$  잔고 {st['balance']:.2f}$")
        st["position"] = None

    for i in range(len(df)):
        t = df.index[i]
        if not closed[i] or (last is not None and t <= last):
            continue
        ts = str(t)
        # 1) 직전 봉 신호 → 이번 봉 시가에 진입
        if st["pending"] and st["position"] is None:
            d = st["pending"]["dir"]
            entry = o[i] + d * half
            risk = st["pending"]["risk"]
            lots = lot_size(st["balance"], risk_pct, risk)
            st["position"] = {"dir": d, "entry": entry, "sl": entry - d * risk, "tp": entry + d * risk * p["rr"],
                              "lots": lots, "bars": 0, "max_hold": p["max_hold"], "generation": gen, "time": ts}
            pos = st["position"]
            write({"time": ts, "action": "진입", "side": "매수" if d > 0 else "매도", "price": round(entry, 2),
                   "lots": lots, "sl": round(pos["sl"], 2), "tp": round(pos["tp"], 2),
                   "reason": "신호", "balance": st["balance"], "generation": gen})
            if verbose:
                print(f"{ts}  {'매수' if d > 0 else '매도'} 진입 {entry:.2f}  {lots}랏  "
                      f"손절 {pos['sl']:.2f}  익절 {pos['tp']:.2f}")
        st["pending"] = None
        # 2) 보유 포지션 손절·익절·시간 청산 (같은 봉에서 둘 다 닿으면 손절)
        pos = st["position"]
        if pos:
            d = pos["dir"]
            if (l[i] <= pos["sl"]) if d > 0 else (h[i] >= pos["sl"]):
                close_pos(ts, pos["sl"], "손절")
            elif (h[i] >= pos["tp"]) if d > 0 else (l[i] <= pos["tp"]):
                close_pos(ts, pos["tp"], "익절")
            else:
                pos["bars"] += 1
                friday_close = t.dayofweek == 4 and (t.hour, t.minute) >= (20, 45)
                if pos["bars"] >= pos["max_hold"]:
                    close_pos(ts, c[i], "시간초과")
                elif friday_close:
                    close_pos(ts, c[i], "주말 전 청산")
        # 3) 포지션이 없으면 이번 봉 마감 신호 확인
        if st["position"] is None and s[i] != 0 and atr_[i] > 0:
            st["pending"] = {"dir": int(s[i]), "risk": float(atr_[i] * p["atr_sl"])}
        st["last_bar"] = ts
    save_state(st)
    return st
