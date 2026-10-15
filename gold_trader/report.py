"""거래 장부를 기간별로 정리해 마크다운 리포트를 만든다."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from paper import JOURNAL

REPORTS = Path(__file__).parent / "reports"


def weekly(end: pd.Timestamp | None = None, days: int = 7) -> Path:
    if not JOURNAL.exists():
        raise SystemExit("거래 장부가 아직 없습니다. 먼저 paper 또는 run 을 실행하세요.")
    j = pd.read_csv(JOURNAL, parse_dates=["time"], encoding="utf-8-sig")
    end = end or j["time"].max()
    start = end - pd.Timedelta(days=days)
    w = j[(j["time"] > start) & (j["time"] <= end)]
    entries = w[w["action"] == "진입"].reset_index(drop=True)
    exits = w[w["action"] == "청산"].reset_index(drop=True)
    pnl = exits["pnl_usd"].astype(float)
    wins = int((pnl > 0).sum())
    lines = [
        f"# 금(XAUUSD) 모의매매 주간 기록",
        f"",
        f"기간: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} (UTC)",
        f"",
        f"| 항목 | 값 |", f"|---|---|",
        f"| 매수 진입 | {int((entries['side'] == '매수').sum())}회 |",
        f"| 매도 진입 | {int((entries['side'] == '매도').sum())}회 |",
        f"| 청산 완료 | {len(exits)}회 (익절 {int((exits['reason'] == '익절').sum())}, "
        f"손절 {int((exits['reason'] == '손절').sum())}, 기타 {int((~exits['reason'].isin(['익절', '손절'])).sum())}) |",
        f"| 승률 | {wins / len(exits):.0%} |" if len(exits) else "| 승률 | - |",
        f"| 손익 합계 | {pnl.sum():+,.2f}$ |",
        f"| 최대 이익 / 최대 손실 | {pnl.max():+,.2f}$ / {pnl.min():+,.2f}$ |" if len(exits) else "| 최대 이익 / 최대 손실 | - |",
        f"| 기말 잔고 | {w['balance'].astype(float).iloc[-1]:,.2f}$ |" if len(w) else "| 기말 잔고 | - |",
        f"", f"## 매수·매도 기록", f"",
        "| 시각 (UTC) | 구분 | 방향 | 가격 | 랏 | 손절 | 익절 | 사유 | 손익($) | 잔고($) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in w.iterrows():
        f = lambda v: "" if pd.isna(v) else v
        lines.append(f"| {r['time']:%m-%d %H:%M} | {r['action']} | {r['side']} | {r['price']} | {r['lots']} | "
                     f"{f(r['sl'])} | {f(r['tp'])} | {r['reason']} | {f(r['pnl_usd'])} | {r['balance']} |")
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{end:%Y-%m-%d}_주간기록.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
