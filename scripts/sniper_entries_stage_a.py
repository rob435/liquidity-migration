"""S1 Stage-A: staged "sniper" entries on the continuous uptrend book.

Pre-registered in docs/preregistration/sniper-staged-entries-2026-06-09.md.
Tranche A = a x official trade; tranche B = (1-a) limit at entry*(1+x), fills iff
mae >= x, exits at the official exit (conservative). Scored through the frozen
combine + rebalance + hedge at the deployable anchor.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/sniper_entries_stage_a.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import continuous_hedge_engine_driver as drv  # noqa: E402

from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "sniper_entries_2026-06-09"
MS_PER_DAY = 86_400_000
X_GRID = (0.03, 0.05, 0.08)
A_GRID = (0.5, 0.7)
WEIGHTS = drv.WINNER_WEIGHTS


def trade_deltas(trades: pl.DataFrame, x: float, a: float, *, b: float | None = None, maker_b: bool = False, strict: float = 0.0) -> pl.DataFrame:
    """Per-trade policy delta vs official, in portfolio-return units, keyed by exit day.

    Substitutive mode (default): tranche A = a*w official + tranche B = (1-a)*w at the
    limit. Additive mode (Amendment 1): pass ``b`` — full base (a=1) PLUS B = b*w at
    the limit.
    """
    t = trades.with_columns(
        (pl.col("notional_weight").abs()).alias("w"),
        (pl.col("net_return") / pl.col("notional_weight").abs()).alias("net_pn"),
        (pl.col("cost_return") / pl.col("notional_weight").abs()).alias("cost_pn"),
        (pl.col("funding_return") / pl.col("notional_weight").abs()).alias("fund_pn"),
        ((pl.col("exit_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("exit_day"),
    )
    fill = pl.col("mae") <= -(x + strict)  # mae is signed pnl: -x means the wick touched entry*(1+x)
    pb = pl.col("entry_price") * (1.0 + x)
    gross_b_pn = (pb - pl.col("exit_price")) / pb  # short
    cost_b_pn = pl.col("cost_pn") * (0.5 if maker_b else 1.0)
    net_b_pn = gross_b_pn + cost_b_pn + pl.col("fund_pn")
    b_size = (1.0 - a) if b is None else b
    base_frac = a if b is None else 1.0
    t = t.with_columns(
        (
            base_frac * pl.col("w") * pl.col("net_pn")
            + pl.when(fill).then(b_size * pl.col("w") * net_b_pn).otherwise(0.0)
            - pl.col("w") * pl.col("net_pn")
        ).alias("delta"),
        fill.alias("filled"),
        (pl.when(fill).then(net_b_pn).otherwise(None)).alias("net_b_pn"),
    )
    return t.select(["exit_day", "delta", "filled", "net_b_pn", "net_pn", "w"])


def adjusted_components(pieces: dict[str, ContinuousRebalanceComponents], deltas: dict[str, pl.DataFrame]) -> dict[str, ContinuousRebalanceComponents]:
    out = {}
    for name, comp in pieces.items():
        dd = deltas[name].group_by("exit_day").agg(pl.col("delta").sum())
        add = {int(r["exit_day"]): float(r["delta"]) for r in dd.to_dicts()}
        raw = {d: comp.raw_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days}
        gross = {d: comp.gross_by_day.get(d, 0.0) + add.get(d, 0.0) for d in comp.days}
        out[name] = ContinuousRebalanceComponents(
            days=comp.days,
            raw_by_day=raw,
            gross_by_day=gross,
            cost_events=comp.cost_events,
            funding_by_day=comp.funding_by_day,
            active_gross_start=comp.active_gross_start,
            impact_exponent=comp.impact_exponent,
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"preregistration": "docs/preregistration/sniper-staged-entries-2026-06-09.md"}
    rows = []
    for venue in ["bybit", "binance"]:
        pieces = {}
        trades_by = {}
        for src in WEIGHTS:
            comp, _n, _cfg = scout._load_source(scout.SOURCES[src], venue)
            pieces[src] = comp
            _rp, tp, _mp = scout._source_paths(scout.SOURCES[src], venue)
            trades_by[src] = pl.read_csv(tp)
        h_ret, h_fund = drv.btc_inputs(venue, sorted(set().union(*[set(p.days) for p in pieces.values()])))

        def hedged_metrics(pcs):
            combined = combine_continuous_components(pcs, WEIGHTS)
            df = apply_rebalance_rule(combined, drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund)
            return drv.metrics(df)

        base = hedged_metrics(pieces)
        report[f"{venue}_baseline"] = base
        print(f"[{venue}] baseline hedged max4: MAR {base['mar']} Sharpe {base['sharpe']} ret {base['ret_pct']}%")

        for x in X_GRID:
            for a in A_GRID:
                deltas = {s: trade_deltas(trades_by[s], x, a) for s in WEIGHTS}
                m = hedged_metrics(adjusted_components(pieces, deltas))
                alld = pl.concat(list(deltas.values()))
                fills = alld.filter(pl.col("filled"))
                row = {
                    "venue": venue, "x": x, "a": a,
                    "mar": m["mar"], "d_mar": round(m["mar"] - base["mar"], 2),
                    "sharpe": m["sharpe"], "d_sharpe": round(m["sharpe"] - base["sharpe"], 3),
                    "ret_pct": m["ret_pct"], "dd_pct": m["dd_pct"], "d_dd": round(m["dd_pct"] - base["dd_pct"], 2),
                    "fill_rate": round(fills.height / alld.height, 3),
                    "mean_netB_pn_filled_bps": round(float(fills["net_b_pn"].mean() or 0) * 1e4, 1),
                    "mean_netA_pn_same_trades_bps": round(float(fills["net_pn"].mean() or 0) * 1e4, 1),
                }
                rows.append(row)
                print(f"  x={x:.0%} a={a}: MAR {m['mar']} (d {row['d_mar']:+0.2f}) Sh {m['sharpe']} dDD {row['d_dd']:+0.2f} fill {row['fill_rate']:.1%} | B-fills net {row['mean_netB_pn_filled_bps']} bps vs A-on-same {row['mean_netA_pn_same_trades_bps']} bps")

    report["cells"] = rows
    # sensitivities on best pooled cell
    by_cell: dict = {}
    for r in rows:
        by_cell.setdefault((r["x"], r["a"]), []).append(r)
    pooled = {k: float(np.mean([r["d_mar"] for r in v])) for k, v in by_cell.items() if len(v) == 2}
    best = max(pooled, key=lambda k: pooled[k])
    report["pooled_d_mar"] = {f"x{k[0]:g}_a{k[1]:g}": round(v, 2) for k, v in pooled.items()}
    report["best_cell"] = {"x": best[0], "a": best[1], "pooled_d_mar": round(pooled[best], 2)}
    sens = []
    for venue in ["bybit", "binance"]:
        pieces = {s: scout._load_source(scout.SOURCES[s], venue)[0] for s in WEIGHTS}
        trades_by = {s: pl.read_csv(scout._source_paths(scout.SOURCES[s], venue)[1]) for s in WEIGHTS}
        h_ret, h_fund = drv.btc_inputs(venue, sorted(set().union(*[set(p.days) for p in pieces.values()])))

        def hm(pcs):
            combined = combine_continuous_components(pcs, WEIGHTS)
            df = apply_rebalance_rule(combined, drv.winner_rule(4), ContinuousHedgeRule(90, 60, 2.0, 5.0), h_ret, h_fund)
            return drv.metrics(df)

        for tag, kw in [("a0.3", {"x": best[0], "a": 0.3}), ("maker", {"x": best[0], "a": best[1], "maker_b": True}), ("strict", {"x": best[0], "a": best[1], "strict": 0.002})]:
            deltas = {s: trade_deltas(trades_by[s], kw["x"], kw["a"], maker_b=kw.get("maker_b", False), strict=kw.get("strict", 0.0)) for s in WEIGHTS}
            m = hm(adjusted_components(pieces, deltas))
            sens.append({"venue": venue, "variant": tag, "mar": m["mar"], "sharpe": m["sharpe"], "dd_pct": m["dd_pct"]})
            print(f"  sens {venue} {tag}: MAR {m['mar']} Sh {m['sharpe']} DD {m['dd_pct']}")
    report["sensitivities_best_cell"] = sens
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
