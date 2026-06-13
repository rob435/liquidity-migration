"""E2 Stage 1 — BTC-trend regime-response family (V0/V1/V2), decisive window run.

Pre-registration: docs/preregistration/2026-06-12-e2-regime-response-family.md
Variants (fixed up front): V0 = live binary uptrend gate; V1 = uptrend_capped
(on iff 0 < trend <= +0.20); V2 = soft3 (euphoria off; in-band full; trend<=0
quarter-size top-composite-quintile only). Engine modes added 2026-06-12 with
tests; existing gate paths byte-identical.

Mechanics: per variant x venue, the four winner_base component cells run
through `run_continuous_event_research` (V0 reuses the parity-verified
rebuilt ledgers — p3 858/857 bybit, 722 exact binance); components combine
via the canonical `combine_continuous_components` on the FROZEN receipt
weights (.30/.20/.40/.10) and the winner risk rule w90/tv0.045/max4/ddh-0.04;
the 2x-cost arm applies the scout's `_apply_cost_multiplier` convention at
the combined level. Funding ON (engine default; per-venue funding_mode
disclosed by each cell's report).

Decision rule (receipt, interpreted ex ante): a variant WINS iff at 1x both
venue returns positive AND pooled (mean-across-venues) MAR-delta vs V0 >
+0.1 AND neither venue MAR-delta < -0.5, AND at 2x cost both venue returns
stay positive with pooled MAR-delta vs V0 > 0. Both miss => V0 stands, NULL.

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/e2_regime_family.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402

from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    apply_rebalance_rule,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
OUT = SHARED / "e2_regime_family_2026-06-12"

COMMON = dict(
    side="short", decile=9, rmom_quantile=0.25, feature_set=("max_ret168",),
    liq_turnover_min=500_000.0, entry_delay_hours=1, exit_mode="fixed",
    hold_hours=24, sizing_mode="inverse_vol", target_vol_per_name=0.01,
    vol_weight_clamp=2.0, entry_crowding_max_fresh=2, use_funding=True,
)
CELLS: dict[str, dict] = {
    "merged_signal": dict(entry_event_trigger="turn3_pop3", age_days_min=240, take_profit_pct=0.10),
    "age240_turn4pop3_crowd2": dict(entry_event_trigger="turn4_pop3", age_days_min=240, take_profit_pct=0.10),
    "age240_turn4pop5_crowd2": dict(entry_event_trigger="turn4_pop5", age_days_min=240, take_profit_pct=0.10),
    "age210_tp14_hold24_invvol10_crowd2": dict(entry_event_trigger="none", age_days_min=210, take_profit_pct=0.14),
}
# scout source name -> (cell dir name, frozen winner_base weight)
WINNER = {
    "turn3p3": ("merged_signal", 0.30),
    "turn4p3": ("age240_turn4pop3_crowd2", 0.20),
    "turn4p5": ("age240_turn4pop5_crowd2", 0.40),
    "age210tp14": ("age210_tp14_hold24_invvol10_crowd2", 0.10),
}
V0_ROOTS = {
    "merged_signal": SHARED / "continuous_merged_signal_raw_2026-06-07",
    "age240_turn4pop3_crowd2": SHARED / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
    "age240_turn4pop5_crowd2": SHARED / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
    "age210_tp14_hold24_invvol10_crowd2": SHARED / "independent_continuous_tp_hold_sweep_exploratory_2026-06-07",
}
VARIANT_GATES = {
    "V1": dict(btc_trend_gate="uptrend_capped", btc_trend_euphoria_cap=0.20),
    "V2": dict(btc_trend_gate="soft3", btc_trend_euphoria_cap=0.20, btc_soft3_size_frac=0.25),
}
RULE = ContinuousRebalanceRule(
    realized_vol_window_days=90, target_daily_vol=0.045, max_scale=4.0,
    drawdown_half_threshold=-0.04, resize_cost_bps=10.0,
    strategy_momentum_window_days=0,
)


def cell_dir(variant: str, venue: str, cell: str) -> Path:
    if variant == "V0":
        return V0_ROOTS[cell] / venue / cell
    return OUT / variant / venue / cell


def ensure_variant_cells(variant: str, venue: str) -> None:
    if variant == "V0":
        return
    for cell, overrides in CELLS.items():
        out_dir = cell_dir(variant, venue, cell)
        if (out_dir / "continuous_trades.csv").exists():
            print(f"[{variant}/{venue}] {cell}: exists, skip", flush=True)
            continue
        t0 = time.time()
        cfg = ContinuousEventConfig(**{**COMMON, **overrides, **VARIANT_GATES[variant]})
        payload = run_continuous_event_research(ROOTS[venue], config=cfg, report_dir=out_dir)
        print(
            f"[{variant}/{venue}] {cell}: {payload['n_trades']} trades "
            f"(funding={payload['funding_mode']}, {time.time() - t0:.0f}s)",
            flush=True,
        )


def variant_metrics(variant: str, venue: str) -> dict[str, dict]:
    comps: dict[str, object] = {}
    weights: dict[str, float] = {}
    for src, (cell, w) in WINNER.items():
        spec = scout.SourceSpec(cell_dir(variant, venue, cell).parent.parent, cell)
        comp, _count, _cfg = scout._load_source(spec, venue)
        comps[src] = comp
        weights[src] = w
    out: dict[str, dict] = {}
    for arm, mult in (("base", 1.0), ("cost2x", 2.0)):
        combined = scout._combine_components(comps, weights)
        combined = scout._apply_cost_multiplier(combined, mult)
        scaled = apply_rebalance_rule(combined, RULE)
        m = _daily_pnl_metrics(scaled.select("ts_ms", "equity", "drawdown", "basket_return"))
        out[arm] = {
            "return": m.get("total_return"), "mar": m.get("mar"),
            "max_drawdown": m.get("max_drawdown"), "worst_day": m.get("worst_day_return"),
        }
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for variant in ("V0", "V1", "V2"):
        results[variant] = {}
        for venue in ROOTS:
            ensure_variant_cells(variant, venue)
            results[variant][venue] = variant_metrics(variant, venue)
            r = results[variant][venue]
            print(f"== {variant}/{venue}: base ret {r['base']['return']:+.4f} MAR {r['base']['mar']:.2f} "
                  f"| 2x ret {r['cost2x']['return']:+.4f} MAR {r['cost2x']['mar']:.2f}", flush=True)

    verdict: dict[str, dict] = {}
    for variant in ("V1", "V2"):
        rows = {}
        for arm in ("base", "cost2x"):
            d_by = results[variant]["bybit"][arm]["mar"] - results["V0"]["bybit"][arm]["mar"]
            d_bn = results[variant]["binance"][arm]["mar"] - results["V0"]["binance"][arm]["mar"]
            rows[arm] = {
                "mar_delta_bybit": round(d_by, 3), "mar_delta_binance": round(d_bn, 3),
                "mar_delta_pooled": round((d_by + d_bn) / 2.0, 3),
                "ret_bybit": round(results[variant]["bybit"][arm]["return"], 4),
                "ret_binance": round(results[variant]["binance"][arm]["return"], 4),
            }
        win = (
            rows["base"]["ret_bybit"] > 0 and rows["base"]["ret_binance"] > 0
            and rows["base"]["mar_delta_pooled"] > 0.1
            and min(rows["base"]["mar_delta_bybit"], rows["base"]["mar_delta_binance"]) > -0.5
            and rows["cost2x"]["ret_bybit"] > 0 and rows["cost2x"]["ret_binance"] > 0
            and rows["cost2x"]["mar_delta_pooled"] > 0.0
        )
        verdict[variant] = {**rows, "WIN": win}

    report = {"results": results, "verdict": verdict,
              "rule": "w90/tv0.045/max4/ddh-0.04, frozen winner weights, funding on"}
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(verdict, indent=2))
    print(f"artifacts: {OUT}")


if __name__ == "__main__":
    main()
