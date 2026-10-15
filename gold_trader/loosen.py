"""진입 조건을 단계별로 느슨하게 했을 때 거래 수와 성적이 어떻게 바뀌는지 전략별로 표로 보여준다.

지금 챔피언 전략에서 시작해 조건을 하나씩 푼다. 공정하게 보려고 학습에 쓰지 않은
최근 30% 기간(검증 구간)의 성적을 기준으로 삼는다.
"""
from __future__ import annotations

import pandas as pd

from backtest import Costs, run
from evolve import load_champion
from learner import TradeFilter
from report import REPORTS
from strategy import SLOTS


def levels(p: dict, flt: TradeFilter | None):
    """(이름, 파라미터, 필터) 를 덜 느슨한 것부터. 전략 종류에 있는 조건만 푼다."""
    out = [("0. 지금 전략", dict(p), flt)]
    if flt is not None:
        out.append(("1. 학습 필터 기준 낮춤", dict(p), TradeFilter(flt.w, flt.b, flt.mu, flt.sd, flt.threshold - 0.05)))
    q = dict(p)
    out.append(("2. 학습 필터 끔", dict(q), None))
    if q.get("use_vwap"):
        q["use_vwap"] = 0
        out.append(("3. + VWAP 조건 끔", dict(q), None))
    if "stoch_low" in q:
        q["stoch_low"] = min(45.0, q["stoch_low"] + 10)
        out.append(("4. + 되돌림 기준 완화", dict(q), None))
    if q.get("use_trend"):
        q["use_trend"] = 0
        out.append(("3. + 추세 방향 조건 끔", dict(q), None))
    if "buffer" in q:
        q["buffer"] = 0.0
        out.append(("4. + 돌파 기준 완화", dict(q), None))
    if "band_k" in q:
        q["band_k"] = max(1.5, round(q["band_k"] - 0.5, 2))
        q["guard"] = min(2.5, round(q["guard"] + 0.5, 2))
        out.append(("4. + 밴드·추세 제한 완화", dict(q), None))
    if "start_hour" in q:
        q["start_hour"], q["end_hour"] = 0, 24
        out.append(("5. + 거래 시간 제한 없음", dict(q), None))
    elif "trade_end" in q:
        q["trade_end"] = 24
        out.append(("5. + 돌파 시간 제한 없음", dict(q), None))
    if not q.get("allow_short", 1):
        q["allow_short"] = 1
        out.append(("6. + 숏(매도)도 허용", dict(q), None))
    return out


def table(df: pd.DataFrame, costs: Costs, slot: str = "trend_5", valid_frac: float = 0.3):
    ch = load_champion(slot)
    cut = df.index[0] + (df.index[-1] - df.index[0]) * (1 - valid_frac)
    valid = df[df.index >= cut]
    days = max(1, valid.index.normalize().nunique())
    rows = []
    for i, (name, p, flt) in enumerate(levels(ch["params"], TradeFilter.from_dict(ch.get("filter")))):
        name = f"{i}. {name.split('. ', 1)[1]}"
        st = run(valid, p, costs, flt).stats
        rows.append({"단계": name, "하루 거래": round(st["trades"] / days, 1), "거래 수": st["trades"],
                     "승률": f"{st['win_rate']:.0%}", "평균(R)": st["avg_r"], "합계(R)": st["total_r"],
                     "손익비": st["profit_factor"], "최대 낙폭(R)": st["max_dd_r"]})
    return pd.DataFrame(rows), valid.index[0], valid.index[-1], days


def section(t: pd.DataFrame, start, end, days, tf: int, slot: str) -> str:
    lines = [f"## {SLOTS[slot]['name']} ({tf}분봉)", "",
             f"검증 기간 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} (거래일 {days}일)", "",
             "| " + " | ".join(t.columns) + " |", "|" + "---|" * len(t.columns)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in t.itertuples(index=False)]
    return "\n".join(lines)


def save(sections: list[str], risk_pct: float) -> str:
    text = "\n\n".join([f"# 진입 조건 완화 비교\n\nR = 손절 1번 금액, 거래당 위험 {risk_pct}%면 1R = 잔고의 {risk_pct}%"]
                       + sections)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "loosen.md"
    out.write_text(text + "\n", encoding="utf-8")
    return str(out)
