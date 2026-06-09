"""R1: walk-forward causal-allocator falsifier for the continuous winner weights.

Pre-registered in docs/preregistration/continuous-walkforward-allocator-2026-06-09.md.
Precomputes all 286 simplex ensembles through the engine, then walks forward quarterly:
chooser sees only trailing days, OOS days are scored. REF = fixed winner weights on the
same OOS days; haircut = 1 - causalA/REF pooled OOS Sharpe.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/continuous_walkforward_allocator.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    apply_rebalance_rule,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "continuous_walkforward_allocator_2026-06-09"
SOURCES = ["turn3p3", "turn4p3", "turn4p5", "age210tp14"]
WINNER = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
EQUAL = {s: 0.25 for s in SOURCES}
ANN = 365.25
SWITCH_BPS = 10.0
WARMUP_START = dt.date(2024, 4, 1)


def rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90,
        target_daily_vol=0.045,
        max_scale=4.0,
        drawdown_half_threshold=-0.04,
        drawdown_zero_threshold=None,
        resize_cost_bps=10.0,
        strategy_momentum_window_days=0,
    )


def simplex(step: float = 0.1) -> list[dict[str, float]]:
    grids = scout._weight_grid_portfolios(SOURCES, step)
    vecs = list(grids.values())
    # ensure the named reference vectors are present
    for ref in (WINNER, EQUAL):
        if not any(all(abs(v.get(s, 0.0) - ref[s]) < 1e-9 for s in SOURCES) for v in vecs):
            vecs.append(dict(ref))
    return vecs


def key(w: dict[str, float]) -> str:
    return "/".join(f"{w.get(s, 0.0):g}" for s in SOURCES)


def quarter_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    d = start
    while d < end:
        out.append(d)
        m = d.month + 3
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        d = dt.date(y, m, 1)
    return out


def series_metrics(dates: np.ndarray, rets: np.ndarray, lo: dt.date, hi: dt.date) -> dict:
    """Full-calendar metrics on ledger days within [lo, hi)."""
    m = (dates >= np.datetime64(lo)) & (dates < np.datetime64(hi))
    if m.sum() < 5:
        return {"sharpe": 0.0, "mar": 0.0, "ret_pct": 0.0, "dd_pct": 0.0, "n": int(m.sum())}
    ds = dates[m].astype("datetime64[D]").astype(dt.date)
    rs = rets[m]
    start, end = ds[0], ds[-1]
    n = (end - start).days + 1
    s = np.zeros(n)
    for d, r in zip(ds, rs):
        s[(d - start).days] += r
    eq = np.cumprod(1.0 + s)
    peak = np.maximum.accumulate(eq)
    dd = float((eq / peak - 1.0).min())
    total = float(eq[-1] - 1.0)
    years = n / ANN
    return {
        "sharpe": round(float(s.mean() / s.std() * np.sqrt(ANN)) if s.std() > 0 else 0.0, 3),
        "mar": round((total / years) / abs(dd) if dd < 0 else 0.0, 2),
        "ret_pct": round(total * 100, 2),
        "dd_pct": round(dd * 100, 2),
        "n": int(m.sum()),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    vecs = simplex()
    print(f"{len(vecs)} weight vectors")
    # Precompute every vector's rebalanced series per venue.
    store: dict[str, dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
    for venue in ["bybit", "binance"]:
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in SOURCES}
        vstore = {}
        for w in vecs:
            combined = scout._combine_components(pieces, {s: x for s, x in w.items() if x > 0})
            df = apply_rebalance_rule(combined, rule())
            dates = np.array(
                [dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date() for t in df["ts_ms"].to_list()],
                dtype="datetime64[D]",
            )
            vstore[key(w)] = (dates, df["basket_return"].to_numpy(), df["scale"].to_numpy())
        store[venue] = vstore
        print(f"[{venue}] precomputed {len(vstore)} ensembles")

    ledger_end = min(max(store[v][key(WINNER)][0]).astype(dt.date) for v in store)
    est_dates = quarter_dates(WARMUP_START, ledger_end)
    hist_start = dt.date(2000, 1, 1)

    def chooser_score(w_key: str, lo: dt.date, hi: dt.date, crit: str) -> float:
        per = [series_metrics(*store[v][w_key][:2], lo, hi) for v in ["bybit", "binance"]]
        if crit == "A":
            return float(np.mean([p["sharpe"] for p in per]))
        return float(min(p["mar"] for p in per))

    results: dict = {}
    choices_rows = []
    for crit, label in [("A", "causal_A_meanSharpe"), ("B", "causal_B_minMAR")]:
        for est_mode in ["expanding", "rolling12m"]:
            oos: dict[str, list] = {v: [] for v in ["bybit", "binance"]}
            prev_w: dict[str, float] | None = None
            for i, T in enumerate(est_dates):
                lo = hist_start if est_mode == "expanding" else T - dt.timedelta(days=365)
                scores = {key(w): chooser_score(key(w), lo, T, crit) for w in vecs}
                best_key = max(scores, key=lambda k: scores[k])
                best_w = {s: float(x) for s, x in zip(SOURCES, [float(p) for p in best_key.split("/")])}
                hi = est_dates[i + 1] if i + 1 < len(est_dates) else ledger_end + dt.timedelta(days=1)
                l1 = sum(abs(best_w[s] - (prev_w or best_w)[s]) for s in SOURCES)
                for v in ["bybit", "binance"]:
                    dates, rets, scales = store[v][best_key]
                    m = (dates >= np.datetime64(T)) & (dates < np.datetime64(hi))
                    r = rets[m].copy()
                    if r.size and l1 > 0:
                        r[0] -= l1 * scales[m][0] * SWITCH_BPS / 1e4
                    oos[v].append((dates[m], r))
                if est_mode == "expanding" and crit == "A":
                    choices_rows.append({"T": str(T), "weights": best_key, "l1_switch": round(l1, 2)})
                prev_w = best_w
            res = {}
            for v in ["bybit", "binance"]:
                dts = np.concatenate([x[0] for x in oos[v]])
                rs = np.concatenate([x[1] for x in oos[v]])
                res[v] = series_metrics(dts, rs, WARMUP_START, ledger_end + dt.timedelta(days=1))
            results[f"{label}_{est_mode}"] = res

    # references on the SAME OOS window
    for name, w in [("REF_fixed_winner", WINNER), ("C_equal_weight", EQUAL)]:
        res = {}
        for v in ["bybit", "binance"]:
            dates, rets, _ = store[v][key(w)]
            res[v] = series_metrics(dates, rets, WARMUP_START, ledger_end + dt.timedelta(days=1))
        results[name] = res

    def pooled_sharpe(name: str) -> float:
        return float(np.mean([results[name][v]["sharpe"] for v in ["bybit", "binance"]]))

    ref = pooled_sharpe("REF_fixed_winner")
    causal = pooled_sharpe("causal_A_meanSharpe_expanding")
    eqs = pooled_sharpe("C_equal_weight")
    haircut = 1.0 - causal / ref if ref > 0 else float("nan")
    verdict = {
        "pooled_oos_sharpe": {
            "REF_fixed_winner": round(ref, 3),
            "causal_A": round(causal, 3),
            "equal_weight": round(eqs, 3),
        },
        "haircut_pct": round(haircut * 100, 1),
        "equal_within_10pct_of_ref": bool(eqs >= 0.9 * ref),
        "causal_A_pos_ret_both": bool(
            all(results["causal_A_meanSharpe_expanding"][v]["ret_pct"] > 0 for v in ["bybit", "binance"])
        ),
    }
    report = {
        "preregistration": "docs/preregistration/continuous-walkforward-allocator-2026-06-09.md",
        "est_dates": [str(d) for d in est_dates],
        "results": results,
        "choices_expanding_A": choices_rows,
        "verdict": verdict,
    }
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({"results": results, "verdict": verdict}, indent=2))
    print("choices (A, expanding):")
    for r in choices_rows:
        print(f"  {r['T']}  ->  {r['weights']}   (L1 switch {r['l1_switch']})")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
