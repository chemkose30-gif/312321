"""단기 매매용 검색식. 모두 일봉(시가·고가·저가·종가·거래량)만으로 판단한다.

각 검색식은 i번째 날(그날 장 마감 후 판단)에 조건이 맞으면 이유 문자열을, 아니면 None 을 돌려준다.
배열은 종목 하나의 전체 일봉이고, i 보다 뒤의 값은 보지 않는다(미래 데이터 사용 금지).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np


@dataclass
class Bars:
    dates: List[str]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray

    @classmethod
    def from_rows(cls, rows) -> "Bars":
        """rows: (날짜, 종가, 거래량, 시가, 고가, 저가)"""
        a = np.asarray([r[1:6] for r in rows], dtype=float) if rows else np.zeros((0, 5))
        # 열: 0 종가, 1 거래량, 2 시가, 3 고가, 4 저가
        return cls([r[0] for r in rows], a[:, 2], a[:, 3], a[:, 4], a[:, 0], a[:, 1])

    def turnover(self, i: int) -> float:
        return float(self.c[i] * self.v[i])


@dataclass
class Context:
    """같은 날 전체 종목을 보고 정해지는 값."""
    turnover_rank: int = 10**9  # 그날 거래대금 순위 (1 = 최대)
    breadth: float = 0.5  # 그날 오른 종목 비율
    marcap: float = 0.0  # 시가총액(원), 모르면 0


@dataclass
class Screen:
    key: str
    name: str
    desc: str
    check: Callable[[Bars, int, Context], Optional[str]]
    exit: str  # "close": 종가 기준 익절/손절/기간 | "next_open": 다음 날 시가에 매도
    horizon: int  # 최대 보유 거래일
    default_target: float  # 시뮬레이션 필터 없이 쓸 때의 익절선
    stop: float = 0.04
    min_bars: int = 61


def _breakout(b: Bars, i: int, ctx: Context) -> Optional[str]:
    if i < 21:
        return None
    avg_v = b.v[i - 20:i].mean()
    rng = b.h[i] - b.l[i]
    chg = b.c[i] / b.c[i - 1] - 1
    if (avg_v > 0 and b.v[i] >= 3 * avg_v                      # 거래량 20일 평균의 3배 이상
            and b.c[i] >= b.h[i - 20:i].max()                    # 20일 최고가 돌파
            and 0.03 <= chg <= 0.15                              # +3% ~ +15%
            and b.turnover(i) >= 1e10                            # 거래대금 100억 이상
            and rng > 0 and (b.h[i] - b.c[i]) / rng <= 0.3       # 윗꼬리 짧음
            and (ctx.marcap == 0 or ctx.marcap >= 1e11)):        # 시총 1,000억 이상
        return f"거래량 {b.v[i] / avg_v:.1f}배, 20일 고가 돌파, {chg:+.1%}, 거래대금 {b.turnover(i) / 1e8:,.0f}억"
    return None


def _squeeze(b: Bars, i: int, ctx: Context) -> Optional[str]:
    if i < 21:
        return None
    ranges = b.h[i - 7:i] - b.l[i - 7:i]  # 어제까지 7일
    y = i - 1
    avg_v = b.v[i - 20:i].mean()
    ma20 = b.c[i - 19:i + 1].mean()
    if (ranges[-1] > 0 and ranges[-1] <= ranges.min()            # 어제가 7일 중 변동폭 최소 (NR7)
            and b.c[i] > b.h[y]                                  # 오늘 종가가 어제 고가 돌파
            and avg_v > 0 and b.v[i] >= 1.5 * avg_v              # 거래량 1.5배 이상
            and b.c[i] > ma20                                    # 20일선 위
            and b.turnover(i) >= 3e9):                           # 거래대금 30억 이상
        return (f"어제 7일 중 최소 변동폭({ranges[-1] / b.c[y]:.1%}) → 오늘 어제 고가 돌파, "
                f"거래량 {b.v[i] / avg_v:.1f}배")
    return None


def _closing_bet(b: Bars, i: int, ctx: Context) -> Optional[str]:
    if i < 1:
        return None
    chg = b.c[i] / b.c[i - 1] - 1
    if (0.05 <= chg < 0.29                                       # +5% 이상, 상한가(매수 어려움) 제외
            and b.c[i] >= 0.97 * b.h[i]                          # 고가 근처 마감
            and ctx.turnover_rank <= 50                          # 거래대금 상위 50
            and ctx.breadth >= 0.5):                             # 시장 전체가 오른 날 (오른 종목 50% 이상)
        return (f"{chg:+.1%}, 고가 대비 {b.c[i] / b.h[i] - 1:+.1%} 마감, 거래대금 {ctx.turnover_rank}위, "
                f"상승 종목 {ctx.breadth:.0%}")
    return None


SCREENS: Dict[str, Screen] = {s.key: s for s in [
    Screen("breakout", "① 거래량 돌파", "거래량 3배 이상 + 20일 고가 돌파 + 윗꼬리 짧은 양봉",
           _breakout, "close", horizon=5, default_target=0.05),
    Screen("squeeze", "③ 변동성 수축 후 돌파", "7일 중 가장 조용했던 다음 날 그 고가를 거래량 실어 돌파",
           _squeeze, "close", horizon=5, default_target=0.05),
    Screen("closing_bet", "④ 종가 베팅", "고가권 마감한 거래대금 상위 강세주를 종가에 사서 다음 날 시가에 매도",
           _closing_bet, "next_open", horizon=1, default_target=0.0),
]}


def day_context(bars: Dict[str, Bars], index: Dict[str, Dict[str, int]], day: str,
                marcap: Optional[Dict[str, float]] = None) -> Dict[str, Context]:
    """그날 전체 종목의 거래대금 순위와 오른 종목 비율."""
    marcap = marcap or {}
    today = []
    for code, b in bars.items():
        i = index[code].get(day)
        if i is None or i < 1:
            continue
        today.append((code, b.turnover(i), b.c[i] > b.c[i - 1]))
    if not today:
        return {}
    breadth = sum(up for _, _, up in today) / len(today)
    ranked = sorted(today, key=lambda x: -x[1])
    return {code: Context(turnover_rank=r + 1, breadth=breadth, marcap=marcap.get(code, 0.0))
            for r, (code, _, _) in enumerate(ranked)}
