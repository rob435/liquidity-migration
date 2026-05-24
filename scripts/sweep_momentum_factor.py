"""Run a sweep of momentum-factor configurations against the canonical research root.

Loads klines/funding/manifest ONCE and reuses across variants. Each variant
writes its own report directory. A summary table is printed at the end and
saved to a CSV for downstream inspection.

Usage:
    .venv/bin/python scripts/sweep_momentum_factor.py
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS
from liquidity_migration.momentum_factor import (
    MODE_LONG_ONLY,
    MODE_LONG_SHORT,
    MomentumFactorConfig,
    SIZING_EQUAL,
    SIZING_VOL_PARITY,
    _bars_by_symbol,
    _empty_factor_trades,
    _evaluate_promotion,
    _filter_signal_window,
    _monthly_returns,
    _run_factor_pipeline,
    _run_label,
    _split_rows,
    build_factor_features,
    format_factor_report,
)
from liquidity_migration.config import TradeLifecycleConfig
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


BASE = MomentumFactorConfig(start_date=START, end_date=END)


# Each entry: (name, MomentumFactorConfig override kwargs)
VARIANTS: list[tuple[str, dict[str, Any]]] = [
    # ---- single-axis variations from default long_only ----
    ("v1_long_only_default", {}),
    ("v2_long_only_tighter_rank", {"long_quantile": 0.10}),
    ("v3_long_only_tsfilter", {"require_positive_ts_momentum_for_longs": True}),
    ("v4_long_only_voltarget15", {"vol_target_annual": 0.15}),
    ("v5_long_only_voltarget10", {"vol_target_annual": 0.10}),
    ("v6_long_only_no_carry", {"carry_weight": 0.0}),
    ("v7_long_only_strong_carry", {"carry_weight": 2.0}),
    ("v8_long_only_1w_only", {"momentum_lookbacks_days": (7,)}),
    ("v9_long_only_4w_only", {"momentum_lookbacks_days": (28,)}),
    ("v10_long_only_3day_rebal", {"rebalance_days": 3}),
    # ---- long-short variants ----
    ("v11_ls_default", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20}),
    ("v12_ls_voltarget15", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20, "vol_target_annual": 0.15}),
    ("v13_ls_voltarget10", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20, "vol_target_annual": 0.10}),
    ("v14_ls_tsfilter_both", {
        "mode": MODE_LONG_SHORT, "short_quantile": 0.20,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
    }),
    ("v15_ls_strong_carry", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20, "carry_weight": 2.0}),
    ("v16_ls_1w_voltarget", {
        "mode": MODE_LONG_SHORT, "short_quantile": 0.20,
        "momentum_lookbacks_days": (7,),
        "vol_target_annual": 0.15,
    }),
    ("v17_ls_kitchen_sink", {
        "mode": MODE_LONG_SHORT, "short_quantile": 0.20,
        "vol_target_annual": 0.15,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "carry_weight": 1.5,
        "regime_off_scale": 0.0,  # fully flat in off regime
    }),
    # ---- aggressive ----
    ("v18_ls_voltarget20", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20, "vol_target_annual": 0.20}),
    ("v19_ls_voltarget25", {"mode": MODE_LONG_SHORT, "short_quantile": 0.20, "vol_target_annual": 0.25}),
    ("v20_ls_top10pct", {"mode": MODE_LONG_SHORT, "short_quantile": 0.10, "long_quantile": 0.10, "vol_target_annual": 0.15}),
]


def run_variant(
    name: str,
    overrides: dict[str, Any],
    *,
    klines: pl.DataFrame,
    funding: pl.DataFrame,
    archive_manifest: pl.DataFrame,
    bars_by_symbol: dict,
    funding_lookup: dict | None,
    full_pit_universe_pass: bool,
    costs: CostConfig,
) -> dict[str, Any]:
    cfg = replace(BASE, **overrides)
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
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        costs=costs,
    )
    t_pipe = time.time() - t1

    bt_config = TradeLifecycleConfig(
        score="momentum_factor",
        hold_days=cfg.rebalance_days,
        rebalance_days=cfg.rebalance_days,
        gross_exposure=cfg.gross_exposure,
        entry_delay_hours=cfg.entry_delay_hours,
        cost_multiplier=cfg.cost_multiplier,
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
        "config": asdict(cfg),
        "rows": {
            "features": features.height,
            "rebalances": len(rebalance_log),
            "trades": trades.height,
            "baskets": baskets.height,
        },
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {
            **asdict(costs),
            "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
            "cost_multiplier": cfg.cost_multiplier,
            "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier,
        },
        "summary": summary,
        "lifecycle": lifecycle_stats,
        "splits": splits,
        "promotion": promotion,
        "rebalance_log_tail": rebalance_log[-10:],
        "run_label": _run_label(
            config=cfg, archive_manifest=archive_manifest,
            full_pit_universe_pass=full_pit_universe_pass, funding_mode=funding_mode,
        ),
    }
    (output_dir / "momentum_factor_research_report.json").write_text(
        json.dumps(metadata, indent=2, default=str), encoding="utf-8",
    )
    (output_dir / "momentum_factor_research_report.md").write_text(
        format_factor_report(metadata), encoding="utf-8",
    )
    return {
        "name": name,
        "trades": trades.height,
        "rebalances": len(rebalance_log),
        "total_return": summary.get("total_return", 0.0),
        "sharpe": summary.get("sharpe_like", 0.0),
        "max_dd": summary.get("max_drawdown", 0.0),
        "worst_90d": summary.get("worst_90d_return", 0.0),
        "win_rate": summary.get("trade_win_rate", 0.0),
        "profit_factor": summary.get("profit_factor", 0.0),
        "funding_mode": funding_mode,
        "avg_split_sharpe": promotion["avg_split_sharpe"],
        "promotion": promotion["promotion_gate_pass"],
        "t_feat": t_feat,
        "t_pipe": t_pipe,
    }


def main() -> None:
    print(f"Loading data from {ROOT} ...")
    t0 = time.time()
    raw_klines = read_dataset_columns(
        ROOT, "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    funding = read_dataset(ROOT, "funding")
    archive_manifest = read_dataset(ROOT, "archive_trade_manifest")
    klines = _exclude_symbols(raw_klines, DEFAULT_EXCLUDED_SYMBOLS)
    funding = _exclude_symbols(funding, DEFAULT_EXCLUDED_SYMBOLS)
    archive_manifest = _exclude_symbols(archive_manifest, DEFAULT_EXCLUDED_SYMBOLS)
    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    bars_by_symbol = _bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None
    print(f"  loaded in {time.time() - t0:.1f}s · klines={klines.height}, funding={funding.height}, manifest={archive_manifest.height}")
    print(f"  full PIT universe: {full_pit_universe_pass}")
    print()

    costs = CostConfig()
    rows = []
    for name, overrides in VARIANTS:
        print(f"-- running {name} ...", flush=True)
        try:
            res = run_variant(
                name, overrides,
                klines=klines, funding=funding, archive_manifest=archive_manifest,
                bars_by_symbol=bars_by_symbol, funding_lookup=funding_lookup,
                full_pit_universe_pass=full_pit_universe_pass, costs=costs,
            )
            rows.append(res)
            if res.get("skipped"):
                print(f"   SKIPPED: {res.get('reason')}")
            else:
                print(
                    f"   trades={res['trades']:4d} ret={res['total_return']:+.2%} "
                    f"dd={res['max_dd']:+.2%} sharpe={res['sharpe']:+.2f} "
                    f"avgSplitSh={res['avg_split_sharpe']:+.2f} "
                    f"pf={res['profit_factor']:.2f} promote={res['promotion']} "
                    f"(feat {res['t_feat']:.1f}s + pipe {res['t_pipe']:.1f}s)"
                )
        except Exception as e:
            print(f"   FAILED: {type(e).__name__}: {e}")
            rows.append({"name": name, "error": f"{type(e).__name__}: {e}"})

    # Summary table.
    print()
    print("=" * 100)
    print("SWEEP SUMMARY (sorted by sharpe)")
    print("=" * 100)
    valid = [r for r in rows if "error" not in r and not r.get("skipped")]
    valid.sort(key=lambda r: r["sharpe"], reverse=True)
    print(f"{'name':<30} {'trades':>6} {'ret':>8} {'dd':>8} {'sharpe':>7} {'avgSpSh':>8} {'pf':>5} {'promo'}")
    for r in valid:
        print(
            f"{r['name']:<30} {r['trades']:>6d} {r['total_return']:>+7.2%} {r['max_dd']:>+7.2%} "
            f"{r['sharpe']:>+7.2f} {r['avg_split_sharpe']:>+8.2f} {r['profit_factor']:>5.2f} {r['promotion']}"
        )

    # Write summary CSV.
    summary_df = pl.DataFrame([{k: v for k, v in r.items() if not isinstance(v, dict)} for r in valid])
    summary_df.write_csv(ROOT / "reports/momentum_factor_sweep_summary.csv")
    print()
    print(f"summary CSV: {ROOT / 'reports/momentum_factor_sweep_summary.csv'}")


if __name__ == "__main__":
    main()
