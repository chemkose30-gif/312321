"""자기 발전 루프: 전략 슬롯마다 진화 탐색 → 처음 보는 기간에서 검증 → 기존 챔피언보다 나을 때만 교체.

검증을 통과한(검증 기간에도 수익, 손익비 1.1 이상, 거래 수 충분) 슬롯만 '운용' 상태가 되어 매매한다.

state/champions/<슬롯>.json  슬롯별 현재 전략
state/history.jsonl          회차·슬롯별로 무엇을 시도했고 왜 채택/기각했는지 기록
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pandas as pd

from backtest import Costs, fitness, run
from learner import TradeFilter
from strategy import DEFAULTS, SLOTS, crossover, kind_of, mutate, random_params

STATE = Path(__file__).parent / "state"
CHAMPS = STATE / "champions"
MIN_PF = 1.1
MIN_WIN = 0.40        # 운용하려면 검증 승률이 이 이상
VERSION = 2           # 학습 방식이 바뀌면 올린다 → 예전 버전 챔피언은 즉시 다시 학습


def _migrate() -> None:
    """예전 버전의 단일 챔피언(state/champion.json)을 같은 봉 단위의 추세 슬롯으로 옮긴다."""
    old = STATE / "champion.json"
    if old.exists() and not CHAMPS.exists():
        ch = json.loads(old.read_text(encoding="utf-8"))
        slot = f"trend_{ch.get('timeframe', 5)}"
        if slot in SLOTS:
            ch["params"] = {"kind": "trend", **ch["params"]}
            ch["active"] = True
            save_champion(slot, ch)
        old.rename(old.with_suffix(".json.bak"))


def load_champion(slot: str) -> dict:
    _migrate()
    f = CHAMPS / f"{slot}.json"
    if f.exists():
        ch = json.loads(f.read_text(encoding="utf-8"))
        ch["params"].setdefault("kind", SLOTS[slot]["kind"])
        return ch
    kind = SLOTS[slot]["kind"]
    return {"timeframe": 5 if 5 in SLOTS[slot]["tfs"] else SLOTS[slot]["tfs"][0], "params": dict(DEFAULTS[kind]),
            "filter": None, "generation": 0, "active": False}


def load_champions() -> dict[str, dict]:
    return {s: load_champion(s) for s in SLOTS}


def save_champion(slot: str, ch: dict) -> None:
    CHAMPS.mkdir(parents=True, exist_ok=True)
    (CHAMPS / f"{slot}.json").write_text(json.dumps(ch, indent=2, ensure_ascii=False), encoding="utf-8")


def log(entry: dict) -> None:
    STATE.mkdir(exist_ok=True)
    with open(STATE / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def evaluate(df, params, costs, flt=None, min_trades=30):
    st = run(df, params, costs, flt).stats
    return fitness(st, min_trades), st


def passes(st: dict, min_trades: int) -> bool:
    """실제로 매매해도 될 만큼 검증 성적이 좋은가 (수익, 손익비, 승률, 거래 수, 구간별 안정성)."""
    return bool(st.get("trades", 0) >= min_trades and st.get("total_r", 0) > 0
                and st.get("profit_factor", 0) >= MIN_PF and st.get("win_rate", 0) >= MIN_WIN
                and st.get("stable", True))


def stability(valid: pd.DataFrame, params, costs, flt, parts: int = 3) -> tuple[bool, list]:
    """검증 기간을 3구간으로 나눠 각 구간 손익(R). 2구간 이상에서 수익이어야 안정적이라고 본다.

    한 구간에서 운 좋게 크게 벌어 전체가 좋아 보이는 전략을 걸러낸다.
    """
    trades = run(valid, params, costs, flt).trades
    if trades.empty:
        return False, [0.0] * parts
    edges = pd.date_range(valid.index[0], valid.index[-1], periods=parts + 1)
    rs = [round(float(trades.loc[(trades.entry_time >= a) & (trades.entry_time <= b), "r"].sum()), 2)
          for a, b in zip(edges[:-1], edges[1:])]
    return sum(r > 0 for r in rs) >= parts - 1, rs


def search(train, costs, seed_params, pop=24, gens=12, seed=None, min_trades=30, verbose=True):
    rng = random.Random(seed)
    kind = kind_of(seed_params)
    population = [dict(seed_params)] + [mutate(seed_params, rng, 0.5) for _ in range(pop // 3)]
    population += [random_params(rng, kind) for _ in range(pop - len(population))]
    scored = []
    for g in range(gens):
        scored = sorted(((evaluate(train, p, costs, min_trades=min_trades)[0], p) for p in population),
                        key=lambda x: x[0], reverse=True)
        if verbose:
            print(f"    세대 {g + 1:2d}/{gens}: 학습구간 최고 점수 {scored[0][0]:.2f}")
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


def improve_slot(slot: str, dfs: dict[int, pd.DataFrame], cut, costs: Costs, pop, gens, min_trades, min_valid,
                 seed, verbose) -> dict:
    cfg = SLOTS[slot]
    champ = load_champion(slot)
    champ_tf = champ.get("timeframe")
    tfs = [m for m in cfg["tfs"] if m in dfs]
    if not tfs:
        if verbose:
            print(f"  {cfg['name']}: 쓸 수 있는 봉 데이터가 없어 건너뜀")
        return champ

    # 1) 현재 챔피언을 새 검증 구간에서 다시 평가 (시장이 바뀌었으면 점수가 떨어진다)
    if champ_tf in dfs:
        v = dfs[champ_tf][dfs[champ_tf].index >= cut]
        champ_score, champ_st = evaluate(v, champ["params"], costs, TradeFilter.from_dict(champ.get("filter")),
                                         min_valid)
    else:
        champ_score, champ_st = -1e9, {}

    # 2) 봉 단위마다 학습 구간에서 진화 탐색 → 3) 후보마다 학습 필터를 붙여 검증 구간에서 겨룸
    best, per_tf = None, {}
    for tf in tfs:
        df = dfs[tf]
        train, valid = df[df.index < cut], df[df.index >= cut]
        if verbose:
            print(f"  [{cfg['name']} · {tf}분봉] 학습 {len(train)}봉, 검증 {len(valid)}봉")
        seed_params = champ["params"] if tf == champ_tf else dict(DEFAULTS[cfg["kind"]])
        tf_best = None
        for p in search(train, costs, seed_params, pop, gens, seed, min_trades, verbose):
            cands = [(None, *evaluate(valid, p, costs, None, min_valid))]
            tr = run(train, p, costs).trades
            if len(tr) >= 40:
                for th in (0.45, 0.5, 0.55, 0.6, 0.65):   # 높을수록 확신 높은 신호만 → 승률↑ 거래↓
                    flt = TradeFilter(threshold=th).fit(tr)
                    if run(train, p, costs, flt).stats["trades"] >= min_trades:
                        cands.append((flt, *evaluate(valid, p, costs, flt, min_valid)))
            for flt, sc, st in cands:
                if tf_best is None or sc > tf_best[3]:
                    tf_best = (tf, p, flt, sc, st)
        per_tf[tf] = {"score": round(tf_best[3], 3), **tf_best[4]}
        if best is None or tf_best[3] > best[3]:
            best = tf_best

    tf, p, flt, score, st = best
    # 4) 교체 조건: 검증 점수가 더 높고, 검증 구간에서도 수익이 났을 것
    promoted = bool(score > champ_score and st["total_r"] > 0)
    if promoted:
        champ = {"timeframe": tf, "params": p, "filter": flt.to_dict() if flt else None,
                 "generation": champ.get("generation", 0) + 1, "valid_stats": st,
                 "by_timeframe": per_tf, "promoted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    else:
        champ["valid_stats"] = champ_st or champ.get("valid_stats")
        champ["by_timeframe"] = per_tf
    vtf = champ["timeframe"]
    if champ.get("valid_stats") and vtf in dfs:
        ok, parts = stability(dfs[vtf][dfs[vtf].index >= cut], champ["params"], costs,
                              TradeFilter.from_dict(champ.get("filter")))
        champ["valid_stats"] = {**champ["valid_stats"], "stable": ok, "parts_r": parts}
    champ["active"] = passes(champ.get("valid_stats") or {}, min_valid)
    champ["version"] = VERSION
    champ["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_champion(slot, champ)
    log({"time": champ["checked_at"], "slot": slot,
         "champion_timeframe": champ_tf, "champion_valid": champ_st, "champion_score": round(champ_score, 3),
         "challenger_timeframe": tf, "challenger_valid": st, "challenger_score": round(score, 3),
         "challenger_params": p, "challenger_uses_filter": flt is not None, "by_timeframe": per_tf,
         "promoted": promoted, "active": champ["active"]})
    if verbose:
        vs = champ.get("valid_stats") or {}
        print(f"  → {cfg['name']}: {'새 전략으로 교체' if promoted else '기존 유지'} "
              f"({champ['timeframe']}분봉, 검증 {vs.get('trades', 0)}회 승률 {vs.get('win_rate', 0):.0%} "
              f"{vs.get('total_r', 0):+.1f}R 손익비 {vs.get('profit_factor', 0)} 구간별 {vs.get('parts_r')}) "
              f"→ {'운용' if champ['active'] else '대기 (검증 기준 미달)'}")
    return champ


def improve(dfs: dict[int, pd.DataFrame] | pd.DataFrame, costs: Costs = Costs(), valid_frac=0.3, pop=24,
            gens=12, min_trades=30, seed=None, verbose=True, slots=None) -> dict[str, dict]:
    """한 번의 발전 사이클. 모든 슬롯을 같은 학습/검증 기간으로 다시 학습하고 슬롯별 챔피언을 돌려준다.

    거래당 위험이 같으므로 같은 기간의 R 합계(낙폭 반영)로 봉 단위끼리 비교해도 공정하다.
    """
    from data import bar_minutes

    if isinstance(dfs, pd.DataFrame):
        dfs = {bar_minutes(dfs): dfs}
    dfs = align(dfs)
    any_df = next(iter(dfs.values()))
    cut = any_df.index[0] + (any_df.index[-1] - any_df.index[0]) * (1 - valid_frac)
    min_valid = max(10, int(min_trades * valid_frac / (1 - valid_frac)))
    if verbose:
        print(f"학습 {any_df.index[0]:%Y-%m-%d}~{cut:%Y-%m-%d}, 검증 {cut:%Y-%m-%d}~{any_df.index[-1]:%Y-%m-%d}, "
              f"봉 단위 {list(dfs)}분, 비용 스프레드 {costs.spread}$ + 미끄러짐 {costs.slippage}$")
    return {s: improve_slot(s, dfs, cut, costs, pop, gens, min_trades, min_valid, seed, verbose)
            for s in (slots or SLOTS)}
