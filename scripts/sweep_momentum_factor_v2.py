"""Second momentum-factor sweep: target the 2025-2026 weakness seen in v2_long_short.

Hypotheses to test:
1. Very short formation (3d, 5d) captures shorter-cycle alt rotations.
2. Funding carry alone or carry-heavy works as a more orthogonal signal.
3. Tighter vol-target with higher gross_exposure (vol-targeting can hit
   leverage if signal is strong).
4. Faster rebalance (3-day) captures shorter-lived edges.
5. Smaller universe (top 15) where momentum is strongest (Liu-Tsyvinski).
6. Larger universe (top 50) for more diversification.
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

BASE = MomentumFactorConfig(
    start_date=START,
    end_date=END,
    mode=MODE_LONG_SHORT,
    short_quantile=0.20,
)

# Each entry is (name, config-overrides) extending BASE (which is already L/S).
VARIANTS: list[tuple[str, dict[str, Any]]] = [
    # ---- short-horizon variants ----
    ("v21_ls_3d_only", {"momentum_lookbacks_days": (3,)}),
    ("v22_ls_5d_only", {"momentum_lookbacks_days": (5,)}),
    ("v23_ls_3d_7d", {"momentum_lookbacks_days": (3, 7)}),

    # ---- fast rebal ----
    ("v24_ls_3day_rebal_7d_mom", {"rebalance_days": 3, "momentum_lookbacks_days": (7,)}),
    ("v25_ls_3day_rebal_3d_mom", {"rebalance_days": 3, "momentum_lookbacks_days": (3,)}),

    # ---- universe size ----
    ("v26_ls_universe50", {"universe_size": 50}),
    ("v27_ls_universe15", {"universe_size": 15}),
    ("v28_ls_universe100", {"universe_size": 100, "min_listing_history_days": 60}),

    # ---- carry-heavy ----
    ("v29_ls_carry_only", {"carry_weight": 5.0, "momentum_lookbacks_days": (7,)}),
    ("v30_ls_carry3", {"carry_weight": 3.0}),
    ("v31_ls_carry14d", {"carry_lookback_days": 14, "carry_weight": 1.5}),

    # ---- aggressive vol-target with leverage ----
    ("v32_ls_voltarget30_lev3", {
        "vol_target_annual": 0.30,
        "vol_target_max_scale": 3.0,
        "gross_exposure": 1.0,
    }),
    ("v33_ls_voltarget25_filters", {
        "vol_target_annual": 0.25,
        "vol_target_max_scale": 3.0,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
    }),
    ("v34_ls_voltarget20_top10", {
        "vol_target_annual": 0.20,
        "long_quantile": 0.10,
        "short_quantile": 0.10,
        "vol_target_max_scale": 3.0,
    }),

    # ---- conservative + regime-strict ----
    ("v35_ls_regime_off_flat", {"regime_off_scale": 0.0, "vol_target_annual": 0.15}),
    ("v36_ls_regime_strict_100sma", {"regime_sma_days": 100, "regime_off_scale": 0.0}),

    # ---- ensemble of best signals ----
    ("v37_ls_best_combo", {
        "momentum_lookbacks_days": (7, 14, 28),
        "vol_target_annual": 0.20,
        "vol_target_max_scale": 3.0,
        "require_positive_ts_momentum_for_longs": True,
        "require_negative_ts_momentum_for_shorts": True,
        "carry_weight": 1.0,
        "regime_off_scale": 0.0,
        "regime_sma_days": 100,
        "long_quantile": 0.15,
        "short_quantile": 0.15,
        "rebalance_days": 7,
    }),

    # ---- skip-2 days (reduce reversal contamination) ----
    ("v38_ls_skip2", {"momentum_skip_days": 2}),
    ("v39_ls_skip3", {"momentum_skip_days": 3}),

    # ---- shorter rebal, very short mom ----
    ("v40_ls_3day_rebal_5d_mom_voltarget", {
        "rebalance_days": 3,
        "momentum_lookbacks_days": (5,),
        "vol_target_annual": 0.20,
        "vol_target_max_scale": 3.0,
    }),
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
    print(f"Loading data from {ROOT} ...", flush=True)
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
    print(f"  loaded in {time.time() - t0:.1f}s · klines={klines.height}, funding={funding.height}", flush=True)
    print(flush=True)

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
                print(f"   SKIPPED: {res.get('reason')}", flush=True)
            else:
                print(
                    f"   trades={res['trades']:4d} ret={res['total_return']:+.2%} "
                    f"dd={res['max_dd']:+.2%} sharpe={res['sharpe']:+.2f} "
                    f"avgSplitSh={res['avg_split_sharpe']:+.2f} "
                    f"pf={res['profit_factor']:.2f} promote={res['promotion']}",
                    flush=True,
                )
        except Exception as e:
            print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
            rows.append({"name": name, "error": f"{type(e).__name__}: {e}"})

    print(flush=True)
    print("=" * 110, flush=True)
    print("SWEEP V2 SUMMARY (sorted by sharpe)", flush=True)
    print("=" * 110, flush=True)
    valid = [r for r in rows if "error" not in r and not r.get("skipped")]
    valid.sort(key=lambda r: r["sharpe"], reverse=True)
    print(f"{'name':<40} {'trades':>6} {'ret':>8} {'dd':>8} {'sharpe':>7} {'avgSpSh':>8} {'pf':>5} {'promo'}", flush=True)
    for r in valid:
        print(
            f"{r['name']:<40} {r['trades']:>6d} {r['total_return']:>+7.2%} {r['max_dd']:>+7.2%} "
            f"{r['sharpe']:>+7.2f} {r['avg_split_sharpe']:>+8.2f} {r['profit_factor']:>5.2f} {r['promotion']}",
            flush=True,
        )

    summary_df = pl.DataFrame([{k: v for k, v in r.items() if not isinstance(v, dict)} for r in valid])
    summary_df.write_csv(ROOT / "reports/momentum_factor_sweep_v2_summary.csv")
    print(f"\nsummary CSV: {ROOT / 'reports/momentum_factor_sweep_v2_summary.csv'}", flush=True)


if __name__ == "__main__":
    main()
