"""가상 계좌: 현금, 보유종목, 체결 기록. 수수료·세금·슬리피지를 반영한다."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Costs:
    fee_rate: float = 0.00015  # 매수·매도 각각 위탁수수료 (증권사·이벤트마다 다름)
    sell_tax: float = 0.0020  # 매도 시 거래세 (국내주식 기준, 연도·시장별로 다르니 확인 필요)
    slippage: float = 0.0005  # 체결 오차 (매수는 비싸게, 매도는 싸게)

    def round_trip(self) -> float:
        return 2 * self.fee_rate + self.sell_tax + 2 * self.slippage


@dataclass
class Position:
    symbol: str
    qty: int
    cost_per_share: float  # 수수료 포함 1주당 매입원가
    entry_date: str
    target_price: float
    stop_price: float
    horizon_days: int
    days_held: int = 0


@dataclass
class Portfolio:
    cash: float
    initial_capital: float
    costs: Costs = field(default_factory=Costs)
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[dict] = field(default_factory=list)
    equity_history: List[list] = field(default_factory=list)
    last_prices: Dict[str, float] = field(default_factory=dict)
    settings: Dict[str, object] = field(default_factory=dict)  # 매매 방식(전 종목/감시 종목), 한도 등

    @classmethod
    def new(cls, capital: float, costs: Optional[Costs] = None) -> "Portfolio":
        return cls(cash=capital, initial_capital=capital, costs=costs or Costs())

    def equity(self, prices: Dict[str, float]) -> float:
        def px(s: str, p: Position) -> float:
            return prices.get(s) or self.last_prices.get(s) or p.cost_per_share
        value = sum(p.qty * px(s, p) for s, p in self.positions.items())
        return self.cash + value

    def buy(self, symbol: str, price: float, budget: float, date: str, target_pct: float,
            stop_pct: float, horizon_days: int, note: str = "") -> Optional[dict]:
        fill = price * (1 + self.costs.slippage)
        unit_cost = fill * (1 + self.costs.fee_rate)
        qty = math.floor(min(budget, self.cash) / unit_cost)
        if qty < 1:
            return None
        total = qty * unit_cost
        self.cash -= total
        self.positions[symbol] = Position(symbol, qty, unit_cost, date, price * (1 + target_pct),
                                          price * (1 - stop_pct), horizon_days)
        trade = {"date": date, "side": "BUY", "symbol": symbol, "qty": qty, "price": round(fill, 2),
                 "amount": round(total), "note": note}
        self.trades.append(trade)
        return trade

    def sell(self, symbol: str, price: float, date: str, reason: str) -> dict:
        pos = self.positions.pop(symbol)
        fill = price * (1 - self.costs.slippage)
        proceeds = pos.qty * fill * (1 - self.costs.fee_rate - self.costs.sell_tax)
        pnl = proceeds - pos.qty * pos.cost_per_share
        self.cash += proceeds
        trade = {"date": date, "side": "SELL", "symbol": symbol, "qty": pos.qty, "price": round(fill, 2),
                 "amount": round(proceeds), "pnl": round(pnl),
                 "pnl_pct": round(pnl / (pos.qty * pos.cost_per_share), 4),
                 "days_held": pos.days_held, "note": reason}
        self.trades.append(trade)
        return trade

    # ---- 저장/불러오기 ----
    def save(self, path: Path) -> None:
        data = asdict(self)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Portfolio":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["costs"] = Costs(**d["costs"])
        d["positions"] = {s: Position(**p) for s, p in d["positions"].items()}
        return cls(**d)
