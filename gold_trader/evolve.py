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
        return json.loads(f.read_text(encoding="utf-8"))
    return {"timeframe": 5, "params": DEFAULT, "filter": None, "generation": 0}


def save_champion(ch: dict) -> None:
    STATE.mkdir(exist_ok=True)
    (STATE / "champion.json").write_text(json.dumps(ch, indent=2, ensure_ascii=False), encoding="utf-8")


def log(entry: dict) -> None:
    STATE.mkdir(exist_ok=True)
    with open(STATE / "history.jsonl", "a", encoding="utf-8") as f:
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


def align(dfs: dict[int, pd.DataFrame], min_span_frac: float = 0.6) -> dict[int, pd.DataFrame]:
    """모든 봉 단위를 같은 기간으로 자른다. 데이터가 너무 짧은 봉 단위(MT5 설정상 1분봉이 짧을 때 등)는 뺀다."""
    spans = {m: d.index[-1] - d.index[0] for m, d in dfs.items() if len(d) > 100}
    longest = max(spans.values())
    keep = {m for m, sp in spans.items() if sp >= longest * min_span_frac}
    for m in sorted(set(dfs) - keep):
        print(f"  {m}분봉은 데이터 기간이 짧아 이번 비교에서 제외")
    start = max(dfs[m].index[0] for m in keep)
    end = min(dfs[m].index[-1] for m in keep)
    return {m: dfs[m][(dfs[m].index >= start) & (dfs[m].index <= end)] for m in sorted(keep)}


def improve(dfs: dict[int, pd.DataFrame] | pd.DataFrame, costs: Costs = Costs(), valid_frac=0.3, pop=24,
            gens=12, min_trades=30, seed=None, verbose=True) -> dict:
    """한 번의 발전 사이클. 봉 단위마다 전략을 찾고, 같은 검증 기간에서 겨뤄 가장 나은 것을 고른다.

    dfs: {봉 단위(분): 데이터}. 데이터프레임 하나만 주면 그 봉 단위만 쓴다.
    거래당 위험이 같으므로 같은 기간의 R 합계(낙폭 반영)를 비교하면 봉 단위끼리도 공정하다.
    """
    from data import bar_minutes

    if isinstance(dfs, pd.DataFrame):
        dfs = {bar_minutes(dfs): dfs}
    dfs = align(dfs)
    any_df = next(iter(dfs.values()))
    cut = any_df.index[0] + (any_df.index[-1] - any_df.index[0]) * (1 - valid_frac)
    champ = load_champion()
    champ_tf = champ.get("timeframe", 5)
    if verbose:
        print(f"학습 {any_df.index[0]:%Y-%m-%d}~{cut:%Y-%m-%d}, 검증 {cut:%Y-%m-%d}~{any_df.index[-1]:%Y-%m-%d}, "
              f"봉 단위 후보 {list(dfs)}분, 비용 스프레드 {costs.spread}$ + 미끄러짐 {costs.slippage}$")
    min_valid = max(10, int(min_trades * valid_frac / (1 - valid_frac)))

    # 1) 현재 챔피언을 새 검증 구간에서 다시 평가 (시장이 바뀌었으면 점수가 떨어진다)
    if champ_tf in dfs:
        champ_flt = TradeFilter.from_dict(champ.get("filter"))
        champ_score, champ_st = evaluate(dfs[champ_tf][dfs[champ_tf].index >= cut], champ["params"], costs,
                                         champ_flt, min_valid)
    else:
        champ_score, champ_st = -1e9, {}

    # 2) 봉 단위마다 학습 구간에서 진화 탐색 → 3) 후보마다 학습 필터를 붙여 검증 구간에서 겨룸
    best, per_tf = None, {}
    for tf, df in dfs.items():
        train, valid = df[df.index < cut], df[df.index >= cut]
        if verbose:
            print(f"\n[{tf}분봉] 학습 {len(train)}봉, 검증 {len(valid)}봉")
        seed_params = champ["params"] if tf == champ_tf else DEFAULT
        finalists = search(train, costs, seed_params, pop, gens, seed, min_trades, verbose)
        tf_best = None
        for p in finalists:
            cands = [(None, *evaluate(valid, p, costs, None, min_valid))]
            tr = run(train, p, costs).trades
            if len(tr) >= 40:
                for th in (0.45, 0.5, 0.55):
                    flt = TradeFilter(threshold=th).fit(tr)
                    if run(train, p, costs, flt).stats["trades"] >= min_trades:
                        cands.append((flt, *evaluate(valid, p, costs, flt, min_valid)))
            for flt, sc, st in cands:
                if tf_best is None or sc > tf_best[3]:
                    tf_best = (tf, p, flt, sc, st)
        per_tf[tf] = {"score": round(tf_best[3], 3), **tf_best[4]}
        if verbose:
            print(f"  {tf}분봉 최고 후보 (검증): {tf_best[4]}  점수 {tf_best[3]:.2f}")
        if best is None or tf_best[3] > best[3]:
            best = tf_best

    tf, p, flt, score, st = best
    # 4) 챔피언 교체 조건: 검증 점수가 더 높고, 검증 구간에서도 수익이 났을 것
    promoted = bool(score > champ_score and st["total_r"] > 0)
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": [str(any_df.index[0]), str(any_df.index[-1])],
        "spread": costs.spread,
        "champion_timeframe": champ_tf, "champion_valid": champ_st, "champion_score": round(champ_score, 3),
        "challenger_timeframe": tf, "challenger_valid": st, "challenger_score": round(score, 3),
        "challenger_params": p, "challenger_uses_filter": flt is not None,
        "by_timeframe": per_tf, "promoted": promoted,
    }
    log(entry)
    if promoted:
        champ = {"timeframe": tf, "params": p, "filter": flt.to_dict() if flt else None,
                 "generation": champ.get("generation", 0) + 1, "valid_stats": st,
                 "by_timeframe": per_tf, "promoted_at": entry["time"]}
        save_champion(champ)
    if verbose:
        print(f"\n기존 챔피언 {champ_tf}분봉 (검증): {champ_st}  점수 {champ_score:.2f}")
        print(f"도전자 {tf}분봉 (검증): {st}  점수 {score:.2f}  필터 {'사용' if flt else '없음'}")
        print("→ 도전자가 새 챔피언이 되었습니다." if promoted else "→ 기존 챔피언 유지 (도전자가 검증에서 이기지 못함).")
    return champ
