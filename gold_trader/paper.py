"""모의매매(페이퍼 트레이딩) 엔진: 운용 중인 전략 슬롯마다 새로 마감된 봉을 처리하고 모든 매수·매도를 장부에 남긴다.

  - 거래당 잔고의 risk_pct% 위험. 전체 최대 max_positions개, 전략당 최대 max_per_strategy개
  - 같은 전략 안에서는 같은 방향으로만 추가 진입 (전략끼리는 반대 방향도 가능 - 헤지 계좌 기준)
  - 레버리지 기준 증거금이 모자라면 진입하지 않음
  - 하루 손실이 daily_loss_pct%에 닿으면 그날(UTC) 신규 진입 중단

state/paper.json   잔고, 보유 포지션들, 슬롯별 마지막 처리 봉
state/journal.csv  거래 장부 (진입·청산 한 줄씩, 어느 전략인지 포함)
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass

import pandas as pd

from backtest import Costs
from evolve import STATE, load_champion
from gold import lot_size, margin_usd, pnl_usd
from learner import TradeFilter
from strategy import FEATURES, SLOTS, signals

STATE_FILE = STATE / "paper.json"
JOURNAL = STATE / "journal.csv"
JOURNAL_COLS = ["time", "action", "side", "price", "lots", "sl", "tp", "reason", "pnl_usd", "balance",
                "open_positions", "generation", "strategy"]


@dataclass
class Risk:
    risk_pct: float = 1.0         # 거래 1건당 잔고 대비 위험 %
    max_positions: int = 5        # 모든 전략 합쳐 동시에 들 수 있는 최대 포지션 수
    leverage: float = 100.0
    daily_loss_pct: float = 10.0  # 하루 손실 한도 % (0이면 없음)
    max_per_strategy: int = 2     # 전략 하나가 동시에 들 수 있는 최대 포지션 수


def load_state(balance: float) -> dict:
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if "positions" not in st:  # 아주 예전 형식(포지션 1개)
            st["positions"] = [st["position"]] if st.get("position") else []
            st.pop("position", None)
        if "slots" not in st:      # 단일 전략 시절 형식 → 슬롯 형식
            legacy = next((s for s in SLOTS if load_champion(s).get("active")), "trend_5")
            st["slots"] = {legacy: {"last_bar": st.pop("last_bar", None), "pending": st.pop("pending", None)}}
            for p in st["positions"]:
                p.setdefault("slot", legacy)
        return st
    return {"balance": balance, "start_balance": balance, "positions": [], "slots": {},
            "day": None, "day_start_balance": balance}


def save_state(st: dict) -> None:
    STATE.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def _upgrade_journal(path) -> None:
    """예전 장부에 새 열(strategy)이 없으면 열을 추가해 다시 저장한다."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
        header = rows[0].keys() if rows else []
    if rows and "strategy" not in header:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in JOURNAL_COLS})


def write(row: dict, path=JOURNAL) -> None:
    STATE.mkdir(exist_ok=True)
    new = not path.exists()
    if not new:
        _upgrade_journal(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:  # 엑셀에서 한글이 안 깨지게
        w = csv.DictWriter(f, fieldnames=JOURNAL_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_COLS})


def side(d: int) -> str:
    return "매수" if d > 0 else "매도"


def slots_to_run(st: dict | None = None) -> dict[str, dict]:
    """처리할 슬롯: 운용 중이거나, 운용이 끝났어도 아직 포지션이 남아 있는 슬롯."""
    held = {p.get("slot") for p in (st or {}).get("positions", [])}
    out = {}
    for s in SLOTS:
        ch = load_champion(s)
        if ch.get("active") or s in held:
            out[s] = ch
    return out


def step(dfs: dict[int, pd.DataFrame] | pd.DataFrame, costs: Costs = Costs(), balance: float = 10_000,
         risk: Risk = Risk(), now: pd.Timestamp | None = None, verbose: bool = True) -> dict:
    """슬롯마다 아직 처리하지 않은 '마감된' 봉을 순서대로 처리한다. 같은 봉을 두 번 처리하지 않는다.

    dfs: {봉 단위(분): 데이터}. 데이터프레임 하나만 주면 그 봉 단위 슬롯만 처리한다.
    """
    from data import bar_minutes

    if isinstance(dfs, pd.DataFrame):
        dfs = {bar_minutes(dfs): dfs}
    st = load_state(balance)
    now = now or pd.Timestamp.now(tz="UTC")
    for slot, ch in slots_to_run(st).items():
        df = dfs.get(ch.get("timeframe", 5))
        if df is not None and len(df):
            _step_slot(st, slot, ch, df, costs, risk, now, verbose)
    save_state(st)
    return st


def _step_slot(st, slot, ch, df, costs, risk, now, verbose) -> None:
    p, gen, tf = ch["params"], ch.get("generation", 0), ch.get("timeframe", 5)
    name = SLOTS[slot]["name"]
    sl_state = st["slots"].setdefault(slot, {"last_bar": None, "pending": None})
    sig = signals(df, p)
    s = sig["signal"].to_numpy()
    flt = TradeFilter.from_dict(ch.get("filter"))
    if flt is not None:
        s = s * flt.allow(sig[FEATURES].to_numpy(), s)
    if not ch.get("active"):
        s = s * 0                                # 대기 중인 슬롯은 남은 포지션 관리만
    closed = df.index + pd.Timedelta(minutes=tf) <= now   # 아직 만들어지는 중인 봉은 건너뜀
    half = costs.spread / 2 + costs.slippage
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    sl_d, tp_d = sig["sl_dist"].to_numpy(), sig["tp_dist"].to_numpy()
    last = pd.Timestamp(sl_state["last_bar"]) if sl_state["last_bar"] else None
    if last is None:                             # 처음 실행: 과거는 건너뛰고 지금부터 기록
        last = df.index[closed][-2] if closed.sum() > 1 else None
    positions = st["positions"]

    def log(ts, msg, **row):
        write({"time": ts, "balance": st["balance"], "open_positions": len(positions), "strategy": name, **row})
        if verbose:
            print(f"{ts}  [{name}] {msg}")

    def close_pos(pos, ts, px, reason):
        exit_px = px - pos["dir"] * half
        pnl = pnl_usd(pos["dir"], pos["entry"], exit_px, pos["lots"])
        st["balance"] = round(st["balance"] + pnl, 2)
        positions.remove(pos)
        log(ts, f"청산({reason}) {side(pos['dir'])} {exit_px:.2f}  {pos['lots']}랏  손익 {pnl:+.2f}$  "
                f"잔고 {st['balance']:.2f}$", action="청산", side=side(pos["dir"]), price=round(exit_px, 2),
            lots=pos["lots"], reason=reason, pnl_usd=round(pnl, 2), generation=pos["generation"])

    for i in range(len(df)):
        t = df.index[i]
        if not closed[i] or (last is not None and t <= last):
            continue
        ts = str(t)
        day = str(t.date())
        if st.get("day") != day:
            st["day"], st["day_start_balance"] = day, st["balance"]
        mine = [x for x in positions if x.get("slot") == slot]

        # 1) 직전 봉 신호 → 이번 봉 시가에 진입 (위험 %, 최대 개수, 증거금 확인)
        pend, sl_state["pending"] = sl_state["pending"], None
        if pend and len(positions) < risk.max_positions:
            d = pend["dir"]
            entry = o[i] + d * half
            lots = lot_size(st["balance"], risk.risk_pct, pend["sl"])
            used = sum(margin_usd(x["entry"], x["lots"], risk.leverage) for x in positions)
            floating = sum(pnl_usd(x["dir"], x["entry"], o[i], x["lots"]) for x in positions)
            need = margin_usd(entry, lots, risk.leverage)
            if lots == 0:
                log(ts, "진입 건너뜀: 잔고가 작아 최소 랏(0.01)도 위험 한도를 넘음",
                    action="건너뜀", side=side(d), price=round(entry, 2), reason="위험 한도 초과")
            elif st["balance"] + floating - used < need:
                log(ts, f"진입 건너뜀: 증거금 부족 (필요 {need:.2f}$)",
                    action="건너뜀", side=side(d), price=round(entry, 2), lots=lots, reason="증거금 부족")
            else:
                pos = {"slot": slot, "dir": d, "entry": entry, "sl": entry - d * pend["sl"],
                       "tp": entry + d * pend["tp"], "lots": lots, "bars": 0,
                       "max_hold": p["max_hold"], "generation": gen, "time": ts}
                positions.append(pos)
                log(ts, f"{side(d)} 진입 {entry:.2f}  {lots}랏  손절 {pos['sl']:.2f}  익절 {pos['tp']:.2f}  "
                        f"(전체 {len(positions)}/{risk.max_positions}, 증거금 {need:.2f}$)",
                    action="진입", side=side(d), price=round(entry, 2), lots=lots, sl=round(pos["sl"], 2),
                    tp=round(pos["tp"], 2), reason="신호", generation=gen)

        # 2) 이 전략의 보유 포지션마다 손절·익절·시간 청산 (같은 봉에서 둘 다 닿으면 손절)
        friday_close = t.dayofweek == 4 and (t.hour, t.minute) >= (20, 45)
        for pos in [x for x in positions if x.get("slot") == slot]:
            d = pos["dir"]
            if (l[i] <= pos["sl"]) if d > 0 else (h[i] >= pos["sl"]):
                close_pos(pos, ts, pos["sl"], "손절")
            elif (h[i] >= pos["tp"]) if d > 0 else (l[i] <= pos["tp"]):
                close_pos(pos, ts, pos["tp"], "익절")
            else:
                pos["bars"] += 1
                if pos["bars"] >= pos["max_hold"]:
                    close_pos(pos, ts, c[i], "시간초과")
                elif friday_close:
                    close_pos(pos, ts, c[i], "주말 전 청산")

        # 3) 새 신호: 전체·전략별 최대 개수, 전략 안에서는 같은 방향만, 하루 손실 한도 확인
        mine = [x for x in positions if x.get("slot") == slot]
        day_loss = (st["day_start_balance"] - st["balance"]) / st["day_start_balance"] * 100
        if (s[i] != 0 and sl_d[i] > 0 and len(positions) < risk.max_positions
                and len(mine) < risk.max_per_strategy and all(x["dir"] == s[i] for x in mine)
                and not (risk.daily_loss_pct and day_loss >= risk.daily_loss_pct)):
            sl_state["pending"] = {"dir": int(s[i]), "sl": float(sl_d[i]), "tp": float(tp_d[i])}
        sl_state["last_bar"] = ts
