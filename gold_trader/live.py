"""MT5 자동매매: 챔피언 전략 신호로 실제 주문을 넣는다 (손절·익절을 주문과 함께 브로커 서버에 건다).

안전장치
  - 기본은 데모 계좌에서만 주문. 실계좌는 --allow-real 을 직접 줘야 동작
  - 거래당 위험 %, 최대 포지션 수, 최대 랏, 하루 손실 한도
  - 이 프로그램의 주문만 매직 넘버로 구분 (다른 EA 주문은 건드리지 않음)
  - gold_trader 폴더에 STOP 파일을 만들면 신규 진입 중단
기록: state/live_journal.csv
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from evolve import STATE, load_champion
from learner import TradeFilter
from paper import Risk, side, write
from strategy import FEATURES, signals

MAGIC = 20261015
LIVE_JOURNAL = STATE / "live_journal.csv"
LIVE_STATE = STATE / "live.json"
STOP_FILE = Path(__file__).parent / "STOP"


def _state() -> dict:
    if LIVE_STATE.exists():
        return json.loads(LIVE_STATE.read_text(encoding="utf-8"))
    return {"last_signal_bar": None, "logged_deals": [], "day": None, "day_start_balance": None,
            "since": time.time()}


def _save(st: dict) -> None:
    STATE.mkdir(exist_ok=True)
    st["logged_deals"] = st["logged_deals"][-500:]
    LIVE_STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def _filling(mt5, info) -> int:
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _lots(info, balance: float, risk_pct: float, sl_dist: float, max_lot: float) -> float:
    """잔고의 risk_pct%만 잃도록 랏 계산. 최소 랏으로도 한도를 넘으면 0."""
    per_lot = sl_dist * info.trade_contract_size        # 1랏이 손절까지 잃는 금액
    raw = balance * risk_pct / 100 / per_lot if per_lot > 0 else 0
    step = info.volume_step
    lots = np.floor(round(raw / step, 6)) * step
    lots = min(lots, info.volume_max, max_lot)
    return round(float(lots), 2) if lots >= info.volume_min else 0.0


def _close(mt5, pos, info, reason: str, log) -> None:
    tick = mt5.symbol_info_tick(pos.symbol)
    buy = pos.type == mt5.POSITION_TYPE_BUY
    r = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume, "position": pos.ticket,
        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
        "deviation": 30, "magic": MAGIC, "comment": f"gold_ai {reason}", "type_filling": _filling(mt5, info),
    })
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"청산 주문 실패 ({reason}) #{pos.ticket}: {getattr(r, 'comment', mt5.last_error())}")


def tick(df: pd.DataFrame, symbol: str, risk: Risk, max_lot: float, allow_real: bool, verbose=True) -> None:
    """새 봉 마감마다 한 번 호출: 체결 기록 → 시간·주말 청산 → 신규 진입."""
    import MetaTrader5 as mt5

    def log(msg):
        if verbose:
            print(f"{pd.Timestamp.now(tz='UTC'):%m-%d %H:%M}  {msg}")

    from data import mt5_connect

    mt5_connect(mt5)
    try:
        acc = mt5.account_info()
        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not allow_real:
            print("\n[중지] 실계좌입니다. 자동매매를 막았습니다. 데모 계좌로 로그인하거나, "
                  "위험을 이해했다면 --allow-real 을 붙여 실행하세요.")
            raise SystemExit(3)   # 3 = 다시 시작해도 소용없는 중지 (실행 파일이 재시작하지 않음)
        info = mt5.symbol_info(symbol)
        st = _state()
        ch = load_champion()
        p = ch["params"]
        tf = ch.get("timeframe", 5)

        # 1) 브로커에서 체결·청산된 내역을 장부에 기록 (손절·익절은 서버에서 실행됨)
        deals = mt5.history_deals_get(st["since"] - 86400, time.time() + 86400) or []
        for dl in deals:
            if dl.magic != MAGIC or dl.ticket in st["logged_deals"]:
                continue
            st["logged_deals"].append(dl.ticket)
            opening = dl.entry == mt5.DEAL_ENTRY_IN
            d = 1 if dl.type == mt5.DEAL_TYPE_BUY else -1
            write({"time": str(pd.Timestamp(dl.time, unit="s")), "action": "진입" if opening else "청산",
                   "side": side(d if opening else -d), "price": dl.price, "lots": dl.volume,
                   "reason": dl.comment, "pnl_usd": "" if opening else round(dl.profit + dl.commission + dl.swap, 2),
                   "balance": acc.balance, "generation": ch.get("generation", 0)}, LIVE_JOURNAL)
            if not opening:
                log(f"청산 {side(-d)} {dl.price}  손익 {dl.profit:+.2f}$  ({dl.comment})")

        # 2) 오래 들고 있거나 금요일 마감 직전이면 청산
        mine = [x for x in (mt5.positions_get(symbol=symbol) or []) if x.magic == MAGIC]
        now_srv = mt5.symbol_info_tick(symbol).time               # 서버 시각 기준으로 보유 시간 계산
        now_utc = pd.Timestamp.now(tz="UTC")
        friday_close = now_utc.dayofweek == 4 and (now_utc.hour, now_utc.minute) >= (20, 45)
        for pos in list(mine):
            if friday_close or now_srv - pos.time >= p["max_hold"] * tf * 60:
                _close(mt5, pos, info, "주말 전 청산" if friday_close else "시간초과", log)
                mine.remove(pos)

        # 3) 하루 손실 한도
        day = str(now_utc.date())
        if st["day"] != day:
            st["day"], st["day_start_balance"] = day, acc.balance
        day_loss = (st["day_start_balance"] - acc.equity) / st["day_start_balance"] * 100 if st["day_start_balance"] else 0

        # 4) 막 마감된 봉의 신호로 신규 진입
        closed = df[df.index + pd.Timedelta(minutes=tf) <= now_utc]
        sig = signals(closed, p)
        last_t = str(closed.index[-1])
        d = int(sig["signal"].iloc[-1])
        flt = TradeFilter.from_dict(ch.get("filter"))
        if d and flt is not None and not flt.allow(sig[FEATURES].to_numpy()[-1:], np.array([d]))[0]:
            d = 0
        reason = None
        if d == 0 or st["last_signal_bar"] == last_t:
            reason = ""
        elif STOP_FILE.exists():
            reason = "STOP 파일 있음"
        elif risk.daily_loss_pct and day_loss >= risk.daily_loss_pct:
            reason = f"하루 손실 한도 {risk.daily_loss_pct}% 도달"
        elif len(mine) >= risk.max_positions:
            reason = f"최대 포지션 {risk.max_positions}개 보유 중"
        elif any((x.type == mt5.POSITION_TYPE_BUY) != (d > 0) for x in mine):
            reason = "반대 방향 포지션 보유 중"
        if reason is None:
            st["last_signal_bar"] = last_t
            tk = mt5.symbol_info_tick(symbol)
            price = tk.ask if d > 0 else tk.bid
            sl_dist = float(sig["atr"].iloc[-1] * p["atr_sl"])
            lots = _lots(info, acc.balance, risk.risk_pct, sl_dist, max_lot)
            order_type = mt5.ORDER_TYPE_BUY if d > 0 else mt5.ORDER_TYPE_SELL
            margin = mt5.order_calc_margin(order_type, symbol, lots, price) if lots else None
            if lots == 0:
                log(f"{side(d)} 신호, 진입 안 함: 잔고 {acc.balance:.2f}$로는 최소 랏도 {risk.risk_pct}% 위험을 넘음")
            elif margin is None or margin > acc.margin_free:
                log(f"{side(d)} 신호, 진입 안 함: 증거금 부족 (필요 {margin}, 가용 {acc.margin_free:.2f}$)")
            else:
                digits = info.digits
                r = mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots, "type": order_type,
                    "price": price, "sl": round(price - d * sl_dist, digits),
                    "tp": round(price + d * sl_dist * p["rr"], digits), "deviation": 30, "magic": MAGIC,
                    "comment": "gold_ai", "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(mt5, info),
                })
                if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
                    log(f"주문 실패: {getattr(r, 'retcode', '')} {getattr(r, 'comment', mt5.last_error())}")
                else:
                    log(f"{side(d)} 주문 체결 {r.price}  {lots}랏  손절 {price - d * sl_dist:.2f}  "
                        f"익절 {price + d * sl_dist * p['rr']:.2f}  ({len(mine) + 1}/{risk.max_positions})")
        elif reason:
            st["last_signal_bar"] = last_t
            log(f"{side(d)} 신호 무시: {reason}")
        _save(st)
    finally:
        mt5.shutdown()
