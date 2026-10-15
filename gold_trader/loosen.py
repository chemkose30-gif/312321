"""매수 조건을 단계별로 느슨하게 했을 때 거래 수와 성적이 어떻게 바뀌는지 표로 보여준다.

지금 챔피언 전략에서 시작해 조건을 하나씩 푼다. 공정하게 보려고 학습에 쓰지 않은
최근 30% 기간(검증 구간)의 성적을 기준으로 삼는다.
"""
from __future__ import annotations

import pandas as pd

from backtest import Costs, run
from evolve import load_champion
from learner import TradeFilter
from report import REPORTS


def levels(p: dict, flt: TradeFilter | None):
    """(이름, 파라미터, 필터) 를 덜 느슨한 것부터."""
    out = [("0. 지금 전략", dict(p), flt)]
    if flt is not None:
        out.append(("1. 학습 필터 기준 낮춤", dict(p), TradeFilter(flt.w, flt.b, flt.mu, flt.sd, flt.threshold - 0.05)))
    q = dict(p)
    out.append(("2. 학습 필터 끔", dict(q), None))
    q["use_vwap"] = 0
    out.append(("3. + VWAP 조건 끔", dict(q), None))
    q["stoch_low"] = min(45.0, q["stoch_low"] + 10)
    out.append(("4. + 되돌림 기준 완화", dict(q), None))
    q["start_hour"], q["end_hour"] = 0, 24
    out.append(("5. + 거래 시간 제한 없음", dict(q), None))
    if not q["allow_short"]:
        q["allow_short"] = 1
        out.append(("6. + 숏(매도)도 허용", dict(q), None))
    return out


def table(df: pd.DataFrame, costs: Costs, valid_frac: float = 0.3) -> pd.DataFrame:
    ch = load_champion()
    cut = df.index[0] + (df.index[-1] - df.index[0]) * (1 - valid_frac)
    valid = df[df.index >= cut]
    days = max(1, valid.index.normalize().nunique())
    rows = []
    for name, p, flt in levels(ch["params"], TradeFilter.from_dict(ch.get("filter"))):
        st = run(valid, p, costs, flt).stats
        rows.append({"단계": name, "하루 거래": round(st["trades"] / days, 1), "거래 수": st["trades"],
                     "승률": f"{st['win_rate']:.0%}", "평균(R)": st["avg_r"], "합계(R)": st["total_r"],
                     "손익비": st["profit_factor"], "최대 낙폭(R)": st["max_dd_r"]})
    return pd.DataFrame(rows), valid.index[0], valid.index[-1], days


def save(t: pd.DataFrame, start, end, days, tf: int, risk_pct: float) -> str:
    lines = [f"# 매수 조건 완화 비교 ({tf}분봉)", "",
             f"검증 기간 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} (거래일 {days}일). "
             f"R = 손절 1번 금액, 거래당 위험 {risk_pct}%면 1R = 잔고의 {risk_pct}%", "",
             "| " + " | ".join(t.columns) + " |", "|" + "---|" * len(t.columns)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in t.itertuples(index=False)]
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "loosen.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
