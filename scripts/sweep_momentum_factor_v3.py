"""v3 sweep — paper-grounded multi-sleeve momentum factor.

Each variant is a controlled, one-axis addition on top of the academic
baseline (Liu-Tsyvinski-Wu 2022 CMOM). The point is to attribute Sharpe gains
to specific signals from specific papers, not stack filters until something
fires.

Variant order (each builds on the previous):
1. Liu-Tsyvinski-Wu 2022 CMOM baseline: 1-week formation, L/S top-bottom decile, weekly rebal.
2. + Carry overlay (Pirrong 2014 commodity carry; AQR funding-rate analog for crypto).
3. + Reversal sleeve (Jegadeesh 1990 / De Bondt-Thaler 1985, short horizon adapted to crypto).
4. + Both carry and reversal.
5. + TS-momentum filter (Moskowitz-Ooi-Pedersen 2012 / Hurst-Ooi-Pedersen 2017).
6. + Daniel-Moskowitz 2016 crash defense (regime gate to cash in down-trend BTC).
7. + AQR factor-portfolio vol-targeting (15% portfolio vol).
8. Multi-formation ensemble (7+14+28d, Asness-Moskowitz-Pedersen 2013 ensemble approach).
9. Asness-Moskowitz-Pedersen "Value and Momentum Everywhere" full stack.

After each step, decide whether the addition added real Sharpe.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS
from liquidity_migration.config import TradeLifecycleConfig
from liquidity_migration.momentum_factor import (
    MODE_LONG_SHORT,
    MomentumFactorConfig,
    SIZING_VOL_PARITY,
    _bars_by_symbol,
    _evaluate_promotion,
    _filter_signal_window,
    _monthly_returns,
    _run_factor_pipeline,
    _run_label,
    _split_rows,
    build_factor_features,
    format_factor_report,
)
from liquidity_migration.storage import read_dataset, read_dataset_columns
from liquidity_migration.trade_lifecycle import (
    _funding_lookup,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from liquidity_migration.volume_events import (
    _date_range,
    _exclude_symbols,
    _full_pit_universe_pass,
    _pit_manifest_metadata,
)


ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START = "2023-05-03"
END = "2026-05-18"


# Liu-Tsyvinski-Wu (2022) CMOM baseline. Universe=30 by 90d turnover, 1-week
# formation, L/S top decile vs bottom decile, weekly rebal, vol-parity sizing,
# no overlays.
LTW_BASELINE = MomentumFactorConfig(
    start_date=START,
    end_date=END,
    universe_size=30,
    universe_volume_window_days=90,
    min_listing_history_days=90,
    momentum_lookbacks_days=(7,),
    momentum_skip_days=1,
    mode=MODE_LONG_SHORT,
    long_quantile=0.10,
    short_quantile=0.10,
    sizing=SIZING_VOL_PARITY,
    rebalance_days=7,
    carry_weight=0.0,
    reversal_weight=0.0,
    require_positive_ts_momentum_for_longs=False,
    require_negative_ts_momentum_for_shorts=False,
    use_regime_filter=False,
    vol_target_annual=0.0,
    gross_exposure=1.0,
    max_position_weight=0.50,
)


# Each variant overrides LTW_BASELINE.
VARIANTS: list[tuple[str, dict[str, Any]]] = [
    # ---- step 1: pure Liu-Tsyvinski-Wu paper baseline ----
    ("a1_ltw_baseline", {}),

    # ---- step 2: + carry (Pirrong 2014 / Asness-Moskowitz-Pedersen 2013) ----
    ("a2_ltw_plus_carry", {"carry_weight": 0.5}),

    # ---- step 3: + reversal (Jegadeesh 1990) ----
    ("a3_ltw_plus_reversal1d", {"reversal_lookback_days": 1, "reversal_weight": 0.5}),
    ("a4_ltw_plus_reversal2d", {"reversal_lookback_days": 2, "reversal_weight": 0.5}),
    ("a5_ltw_plus_reversal3d", {"reversal_lookback_days": 3, "reversal_weight": 0.5}),

    # ---- step 4: carry + reversal stacked ----
    ("a6_ltw_plus_carry_reversal", {"carry_weight": 0.5, "reversal_lookback_days": 2, "reversal_weight": 0.5}),

    # ---- step 5: + TS-momentum filter (Hurst-Ooi-Pedersen 2017) ----
    ("a7_ltw_plus_carry_rev_tsfilter", {
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
    }),

    # ---- step 6: + Daniel-Moskowitz 2016 regime defense ----
    ("a8_ltw_full_plus_regime", {
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
    }),

    # ---- step 7: + vol-target (AQR factor-portfolio standard) ----
    ("a9_ltw_full_plus_voltarget15", {
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
        "vol_target_annual": 0.15,
        "vol_target_max_scale": 3.0,
    }),

    # ---- step 8: ensemble formation (Asness-Moskowitz-Pedersen 2013 multi-horizon) ----
    ("a10_amp_ensemble_full", {
        "momentum_lookbacks_days": (7, 14, 28),
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
        "vol_target_annual": 0.15,
        "vol_target_max_scale": 3.0,
    }),

    # ---- step 9: AMP "Value and Momentum Everywhere" weights tuned ----
    ("a11_amp_strong_carry_rev", {
        "momentum_lookbacks_days": (7, 14, 28),
        "carry_weight": 1.0,
        "reversal_lookback_days": 2,
        "reversal_weight": 1.0,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
        "vol_target_annual": 0.15,
        "vol_target_max_scale": 3.0,
    }),

    # ---- step 10: sanity — pure reversal sleeve (no momentum) to check reversal alone ----
    ("a12_pure_reversal2d", {
        "momentum_lookbacks_days": (1,),  # placeholder so momentum_z exists
        "carry_weight": 0.0,
        "reversal_lookback_days": 2,
        "reversal_weight": 5.0,  # overwhelm the momentum signal
    }),
    ("a13_pure_carry", {
        "carry_weight": 5.0,
    }),

    # ---- step 11: alternative — wider universe (top 50, more shallow signal) ----
    ("a14_amp_universe50", {
        "universe_size": 50,
        "momentum_lookbacks_days": (7, 14, 28),
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
        "vol_target_annual": 0.15,
        "vol_target_max_scale": 3.0,
    }),

    # ---- step 12: alternative — narrower universe (top 15) ----
    ("a15_amp_universe15", {
        "universe_size": 15,
        "momentum_lookbacks_days": (7, 14, 28),
        "carry_weight": 0.5,
        "reversal_lookback_days": 2,
        "reversal_weight": 0.5,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "use_regime_filter": True,
        "regime_sma_days": 50,
        "regime_off_scale": 0.0,
        "vol_target_annual": 0.15,
        "vol_target_max_scale": 3.0,
    }),
]


def run_variant(name, overrides, *, klines, funding, archive_manifest, bars_by_symbol, funding_lookup, full_pit_universe_pass, costs):
    cfg = replace(LTW_BASELINE, **overrides)
    output_dir = ROOT / f"reports/momentum_factor_{name}"
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    features = build_factor_features(klines, funding=funding, config=cfg)
    features = _filter_signal_window(features, start=cfg.start_date, end=cfg.end_date)
    if features.is_empty():
        return {"name": name, "skipped": True, "reason": "no features"}
    t_feat = time.time() - t0

    t1 = time.time()
    trades, lifecycle_stats, rebalance_log = _run_factor_pipeline(
        features=features, bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup, config=cfg, costs=costs,
    )
    t_pipe = time.time() - t1

    bt_config = TradeLifecycleConfig(
        score="momentum_factor", hold_days=cfg.rebalance_days,
        rebalance_days=cfg.rebalance_days, gross_exposure=cfg.gross_exposure,
        entry_delay_hours=cfg.entry_delay_hours, cost_multiplier=cfg.cost_multiplier,
        side_mode="long_high_short_low",
    )
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    monthly = _monthly_returns(baskets)
    splits = _split_rows(baskets, config=bt_config)
    funding_mode = summary.get("funding_mode", "missing")
    promotion = _evaluate_promotion(
        split_rows=splits, summary=summary, funding_mode=funding_mode,
        full_pit_universe_pass=full_pit_universe_pass, config=cfg,
    )

    if not trades.is_empty():
        trades.write_csv(output_dir / "momentum_factor_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "momentum_factor_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "momentum_factor_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "momentum_factor_monthly.csv")

    metadata = {
        "config": asdict(cfg), "rows": {"features": features.height, "rebalances": len(rebalance_log), "trades": trades.height, "baskets": baskets.height},
        "date_range": _date_range(features), "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {**asdict(costs), "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps, "cost_multiplier": cfg.cost_multiplier,
                       "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier},
        "summary": summary, "lifecycle": lifecycle_stats, "splits": splits, "promotion": promotion,
        "rebalance_log_tail": rebalance_log[-10:],
        "run_label": _run_label(config=cfg, archive_manifest=archive_manifest, full_pit_universe_pass=full_pit_universe_pass, funding_mode=funding_mode),
    }
    (output_dir / "momentum_factor_research_report.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    (output_dir / "momentum_factor_research_report.md").write_text(format_factor_report(metadata), encoding="utf-8")
    return {
        "name": name, "trades": trades.height, "rebalances": len(rebalance_log),
        "total_return": summary.get("total_return", 0.0), "sharpe": summary.get("sharpe_like", 0.0),
        "max_dd": summary.get("max_drawdown", 0.0), "worst_90d": summary.get("worst_90d_return", 0.0),
        "win_rate": summary.get("trade_win_rate", 0.0), "profit_factor": summary.get("profit_factor", 0.0),
        "funding_mode": funding_mode, "avg_split_sharpe": promotion["avg_split_sharpe"],
        "promotion": promotion["promotion_gate_pass"], "split_sharpes": [r["sharpe_like"] for r in splits],
        "t_feat": t_feat, "t_pipe": t_pipe,
    }


def main():
    print(f"Loading data from {ROOT} ...", flush=True)
    t0 = time.time()
    raw_klines = read_dataset_columns(ROOT, "klines_1h", columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"])
    funding = read_dataset(ROOT, "funding")
    archive_manifest = read_dataset(ROOT, "archive_trade_manifest")
    klines = _exclude_symbols(raw_klines, DEFAULT_EXCLUDED_SYMBOLS)
    funding = _exclude_symbols(funding, DEFAULT_EXCLUDED_SYMBOLS)
    archive_manifest = _exclude_symbols(archive_manifest, DEFAULT_EXCLUDED_SYMBOLS)
    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    bars_by_symbol = _bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None
    print(f"  loaded in {time.time() - t0:.1f}s · klines={klines.height}, funding={funding.height}", flush=True)
    print(flush=True)

    costs = CostConfig()
    rows = []
    for name, overrides in VARIANTS:
        print(f"-- running {name} ...", flush=True)
        try:
            res = run_variant(name, overrides, klines=klines, funding=funding, archive_manifest=archive_manifest, bars_by_symbol=bars_by_symbol, funding_lookup=funding_lookup, full_pit_universe_pass=full_pit_universe_pass, costs=costs)
            rows.append(res)
            if res.get("skipped"):
                print(f"   SKIPPED: {res.get('reason')}", flush=True)
            else:
                print(
                    f"   trades={res['trades']:4d} ret={res['total_return']:+.2%} "
                    f"dd={res['max_dd']:+.2%} sharpe={res['sharpe']:+.2f} "
                    f"avgSplitSh={res['avg_split_sharpe']:+.2f} "
                    f"splits=[{','.join(f'{s:+.2f}' for s in res['split_sharpes'])}] "
                    f"pf={res['profit_factor']:.2f} promote={res['promotion']}",
                    flush=True,
                )
        except Exception as e:
            print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
            rows.append({"name": name, "error": f"{type(e).__name__}: {e}"})

    print(flush=True)
    print("=" * 130, flush=True)
    print("V3 SWEEP SUMMARY (sorted by sharpe)", flush=True)
    print("=" * 130, flush=True)
    valid = [r for r in rows if "error" not in r and not r.get("skipped")]
    valid.sort(key=lambda r: r["sharpe"], reverse=True)
    print(f"{'name':<40} {'trades':>6} {'ret':>8} {'dd':>8} {'sharpe':>7} {'avgSpSh':>8} {'split_sharpes':<25} {'promo'}", flush=True)
    for r in valid:
        split_str = "[" + ",".join(f"{s:+.2f}" for s in r["split_sharpes"]) + "]"
        print(
            f"{r['name']:<40} {r['trades']:>6d} {r['total_return']:>+7.2%} {r['max_dd']:>+7.2%} "
            f"{r['sharpe']:>+7.2f} {r['avg_split_sharpe']:>+8.2f} {split_str:<25} {r['promotion']}",
            flush=True,
        )
    summary_df = pl.DataFrame([{k: v for k, v in r.items() if not isinstance(v, (dict, list))} for r in valid])
    summary_df.write_csv(ROOT / "reports/momentum_factor_sweep_v3_summary.csv")
    print(f"\nsummary CSV: {ROOT / 'reports/momentum_factor_sweep_v3_summary.csv'}", flush=True)


if __name__ == "__main__":
    main()
