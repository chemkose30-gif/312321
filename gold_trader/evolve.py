"""자기 발전 루프: 진화 탐색 → 처음 보는 기간에서 검증 → 기존 챔피언보다 나을 때만 교체.

state/champion.json  현재 사용 중인 전략
state/history.jsonl  세대마다 무엇을 시도했고 왜 채택/기각했는지 기록
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pandas as pd

from backtest import Costs, fitness, run
from learner import TradeFilter
from strategy import DEFAULT, crossover, mutate, random_params

STATE = Path(__file__).parent / "state"


def load_champion() -> dict:
    f = STATE / "champion.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"params": DEFAULT, "filter": None, "generation": 0}


def save_champion(ch: dict) -> None:
    STATE.mkdir(exist_ok=True)
    (STATE / "champion.json").write_text(json.dumps(ch, indent=2, ensure_ascii=False))


def log(entry: dict) -> None:
    STATE.mkdir(exist_ok=True)
    with open(STATE / "history.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def split(df: pd.DataFrame, valid_frac: float):
    cut = int(len(df) * (1 - valid_frac))
    return df.iloc[:cut], df.iloc[cut:]


def evaluate(df, params, costs, flt=None, min_trades=30):
    st = run(df, params, costs, flt).stats
    return fitness(st, min_trades), st


def search(train, costs, seed_params, pop=24, gens=12, seed=None, min_trades=30, verbose=True):
    rng = random.Random(seed)
    population = [dict(seed_params)] + [mutate(seed_params, rng, 0.5) for _ in range(pop // 3)]
    population += [random_params(rng) for _ in range(pop - len(population))]
    scored = []
    for g in range(gens):
        scored = sorted(((evaluate(train, p, costs, min_trades=min_trades)[0], p) for p in population),
                        key=lambda x: x[0], reverse=True)
        if verbose:
            print(f"  세대 {g + 1:2d}/{gens}: 학습구간 최고 점수 {scored[0][0]:.2f}")
        elite = [p for _, p in scored[: max(2, pop // 4)]]
        children = []
        while len(children) < pop - len(elite):
            a, b = rng.sample(elite, 2)
            children.append(mutate(crossover(a, b, rng), rng))
        population = elite + children
    return [p for _, p in scored[:5]]


def improve(df: pd.DataFrame, costs: Costs = Costs(), valid_frac=0.3, pop=24, gens=12,
            min_trades=30, seed=None, verbose=True) -> dict:
    """한 번의 발전 사이클. 바뀐(또는 유지된) 챔피언을 돌려준다."""
    champ = load_champion()
    train, valid = split(df, valid_frac)
    min_valid = max(10, int(min_trades * valid_frac / (1 - valid_frac)))
    if verbose:
        print(f"학습 {train.index[0]:%Y-%m-%d}~{train.index[-1]:%Y-%m-%d} ({len(train)}봉), "
              f"검증 {valid.index[0]:%Y-%m-%d}~{valid.index[-1]:%Y-%m-%d} ({len(valid)}봉)")

    # 1) 현재 챔피언을 새 검증 구간에서 다시 평가 (시장이 바뀌었으면 점수가 떨어진다)
    champ_flt = TradeFilter.from_dict(champ.get("filter"))
    champ_score, champ_st = evaluate(valid, champ["params"], costs, champ_flt, min_valid)

    # 2) 학습 구간에서 진화 탐색
    finalists = search(train, costs, champ["params"], pop, gens, seed, min_trades, verbose)

    # 3) 결승 후보마다 학습형 필터를 붙여 보고, 검증 구간에서 겨룬다
    best = None
    for p in finalists:
        cands = [(None, *evaluate(valid, p, costs, None, min_valid))]
        tr = run(train, p, costs).trades
        if len(tr) >= 40:
            for th in (0.45, 0.5, 0.55):
                flt = TradeFilter(threshold=th).fit(tr)
                if run(train, p, costs, flt).stats["trades"] >= min_trades:
                    cands.append((flt, *evaluate(valid, p, costs, flt, min_valid)))
        for flt, sc, st in cands:
            if best is None or sc > best[2]:
                best = (p, flt, sc, st)

    p, flt, score, st = best
    # 4) 챔피언 교체 조건: 검증 점수가 더 높고, 검증 구간에서도 수익이 났을 것
    promoted = score > champ_score and st["total_r"] > 0
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": [str(df.index[0]), str(df.index[-1])],
        "champion_valid": champ_st, "champion_score": round(champ_score, 3),
        "challenger_valid": st, "challenger_score": round(score, 3),
        "challenger_params": p, "challenger_uses_filter": flt is not None,
        "promoted": promoted,
    }
    log(entry)
    if promoted:
        champ = {"params": p, "filter": flt.to_dict() if flt else None,
                 "generation": champ.get("generation", 0) + 1, "valid_stats": st,
                 "promoted_at": entry["time"]}
        save_champion(champ)
    if verbose:
        print(f"\n기존 챔피언 (검증): {champ_st}  점수 {champ_score:.2f}")
        print(f"도전자     (검증): {st}  점수 {score:.2f}  필터 {'사용' if flt else '없음'}")
        print("→ 도전자가 새 챔피언이 되었습니다." if promoted else "→ 기존 챔피언 유지 (도전자가 검증에서 이기지 못함).")
    return champ
