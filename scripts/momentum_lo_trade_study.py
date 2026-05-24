#!/usr/bin/env python3
"""Long-only momentum mega-sweep + trade-level forensics.

1. Runs ~70 LO variants on canonical root (feature cache per signature).
2. Pools and enriches all trades (path MAE/MFE, cross-sectional context).
3. Discovers entry filters on train split; reports val/test basket Sharpe.
4. Re-runs top filter configs through the full factor pipeline.

Usage:
    .venv/bin/python scripts/momentum_lo_trade_study.py
    .venv/bin/python scripts/momentum_lo_trade_study.py --skip-sweep  # forensics only
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS
from liquidity_migration.momentum_factor import (
    MODE_LONG_ONLY,
    MomentumFactorConfig,
    _bars_by_symbol,
    _filter_signal_window,
    _run_factor_pipeline,
    build_factor_features,
)
from liquidity_migration.momentum_trade_forensics import (
    TradeFilterRule,
    daily_sharpe_from_equity,
    enrich_trades,
    filtered_backtest_metrics,
    run_forensics_report,
    rules_to_factor_config,
)
from liquidity_migration.storage import read_dataset, read_dataset_columns
from liquidity_migration.trade_lifecycle import (
    _funding_lookup,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from liquidity_migration.config import TradeLifecycleConfig
from liquidity_migration.volume_events import _exclude_symbols, _full_pit_universe_pass


ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START = "2023-05-03"
END = "2026-05-18"
STUDY_DIR = ROOT / "reports/momentum_lo_trade_study"

LO_SKIP0 = {
    "mode": MODE_LONG_ONLY,
    "momentum_skip_days": 0,
    "carry_weight": 1.5,
    "require_positive_ts_momentum_for_longs": True,
    "vol_target_annual": 0.15,
    "regime_off_scale": 0.0,
    "use_regime_filter": True,
}


def feature_signature(cfg: MomentumFactorConfig) -> tuple[Any, ...]:
    return (
        cfg.universe_size,
        cfg.momentum_lookbacks_days,
        cfg.momentum_skip_days,
        cfg.universe_volume_window_days,
        cfg.min_listing_history_days,
        cfg.ts_momentum_lookback_days,
        cfg.reversal_lookback_days,
        cfg.carry_lookback_days,
        cfg.vol_estimate_window_days,
        cfg.regime_sma_days,
    )


def build_lo_variants() -> list[tuple[str, dict[str, Any]]]:
    """Curated LO grid (~48 variants): more trades via rebal/universe/quantile."""
    variants: list[tuple[str, dict[str, Any]]] = []
    regime_modes = (
        (True, 0.0),
        (True, 0.30),
        (False, 1.0),
    )
    for rebal, uni, lq, tsf, (regime_on, off_scale), vt, carry in product(
        (3, 7),
        (30, 50),
        (0.20, 0.33),
        (True, False),
        regime_modes,
        (0.0, 0.15),
        (0.0, 1.5),
    ):
        overrides = {
            **LO_SKIP0,
            "rebalance_days": rebal,
            "universe_size": uni,
            "long_quantile": lq,
            "require_positive_ts_momentum_for_longs": tsf,
            "use_regime_filter": regime_on,
            "regime_off_scale": off_scale,
            "vol_target_annual": vt,
            "carry_weight": carry,
        }
        name = (
            f"lo_r{rebal}_u{uni}_q{int(lq*100)}"
            f"_ts{int(tsf)}_rg{int(regime_on)}{int(off_scale*10)}"
            f"_vt{int(vt*100)}_c{int(carry*10)}"
        )
        variants.append((name, overrides))
    # Extra: wider book + faster rebalance without regime choke.
    for rebal, uni, lq in product((3,), (50,), (0.50,)):
        variants.append(
            (
                f"lo_r{rebal}_u{uni}_q{int(lq*100)}_wide_notr_noreg",
                {
                    **LO_SKIP0,
                    "rebalance_days": rebal,
                    "universe_size": uni,
                    "long_quantile": lq,
                    "require_positive_ts_momentum_for_longs": False,
                    "use_regime_filter": False,
                    "regime_off_scale": 1.0,
                    "vol_target_annual": 0.15,
                },
            )
        )
    return variants


def run_variant(
    name: str,
    cfg: MomentumFactorConfig,
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict,
    funding_lookup: dict | None,
    costs: CostConfig,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    trades, lifecycle, _log = _run_factor_pipeline(
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        costs=costs,
    )
    bt = TradeLifecycleConfig(
        score="momentum_factor",
        hold_days=cfg.rebalance_days,
        rebalance_days=cfg.rebalance_days,
        cost_multiplier=cfg.cost_multiplier,
    )
    baskets = summarize_baskets(trades, config=bt)
    equity = build_equity_curve(baskets)
    summary = summarize_trade_backtest(trades, baskets, equity, config=bt)
    daily_sh = daily_sharpe_from_equity(equity)
    meta = {
        "name": name,
        "trades": trades.height,
        "total_return": summary.get("total_return", 0.0),
        "sharpe_basket": summary.get("sharpe_like", 0.0),
        "sharpe_daily": daily_sh,
        "max_dd": summary.get("max_drawdown", 0.0),
        "win_rate": summary.get("trade_win_rate", 0.0),
        "lifecycle": lifecycle,
        "config": asdict(cfg),
    }
    return trades, meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument("--data-root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.data_root.expanduser()
    study_dir = root / "reports/momentum_lo_trade_study"
    study_dir.mkdir(parents=True, exist_ok=True)

    variants = build_lo_variants()
    print(f"Study dir: {study_dir}")
    print(f"Variants: {len(variants)}")

    if not args.skip_sweep:
        print("Loading klines ...")
        t0 = time.time()
        raw = read_dataset_columns(
            root,
            "klines_1h",
            columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
        )
        funding = read_dataset(root, "funding")
        manifest = read_dataset(root, "archive_trade_manifest")
        klines = _exclude_symbols(raw, DEFAULT_EXCLUDED_SYMBOLS)
        funding = _exclude_symbols(funding, DEFAULT_EXCLUDED_SYMBOLS)
        manifest = _exclude_symbols(manifest, DEFAULT_EXCLUDED_SYMBOLS)
        bars = _bars_by_symbol(klines)
        funding_lookup = _funding_lookup(funding) if not funding.is_empty() else None
        print(f"  loaded in {time.time()-t0:.1f}s")

        feature_cache: dict[tuple[Any, ...], pl.DataFrame] = {}
        costs = CostConfig()
        sweep_rows: list[dict[str, Any]] = []
        all_trades: list[pl.DataFrame] = []

        for i, (name, overrides) in enumerate(variants):
            cfg = replace(
                MomentumFactorConfig(start_date=START, end_date=END),
                **overrides,
            )
            sig = feature_signature(cfg)
            if sig not in feature_cache:
                feat = build_factor_features(klines, funding=funding, config=cfg)
                feature_cache[sig] = _filter_signal_window(feat, start=START, end=END)
            features = feature_cache[sig]

            trades, meta = run_variant(
                name, cfg, features=features, bars_by_symbol=bars, funding_lookup=funding_lookup, costs=costs,
            )
            sweep_rows.append(meta)
            if not trades.is_empty():
                tagged = trades.with_columns(
                    pl.lit(name).alias("variant"),
                    pl.lit(cfg.rebalance_days).alias("variant_rebalance_days"),
                )
                all_trades.append(tagged)
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(variants)}] last={name} trades={meta['trades']} sharpe_d={meta['sharpe_daily']:.2f}")

        flat_rows = [{k: v for k, v in r.items() if k != "lifecycle" and k != "config"} for r in sweep_rows]
        pl.DataFrame(flat_rows).write_csv(study_dir / "sweep_summary.csv")
        pooled = pl.concat(all_trades) if all_trades else pl.DataFrame()
        pooled.write_parquet(study_dir / "pooled_trades_raw.parquet")
        print(f"Pooled trades: {pooled.height}")

        # Enrich once using LO_skip0 feature frame (richest default universe).
        base_cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **LO_SKIP0)
        if feature_signature(base_cfg) not in feature_cache:
            feat = build_factor_features(klines, funding=funding, config=base_cfg)
            feature_cache[feature_signature(base_cfg)] = _filter_signal_window(feat, start=START, end=END)
        enriched = enrich_trades(pooled, features=feature_cache[feature_signature(base_cfg)], bars_by_symbol=bars)
        enriched.write_parquet(study_dir / "pooled_trades_enriched.parquet")
    else:
        enriched = pl.read_parquet(study_dir / "pooled_trades_enriched.parquet")
        pooled = pl.read_parquet(study_dir / "pooled_trades_raw.parquet")
        klines = read_dataset_columns(
            root,
            "klines_1h",
            columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
        )
        klines = _exclude_symbols(klines, DEFAULT_EXCLUDED_SYMBOLS)
        bars = _bars_by_symbol(klines)

    print("Running forensics ...")
    meta = run_forensics_report(enriched, output_dir=study_dir / "forensics")
    print(json.dumps(meta.get("best_combo"), indent=2, default=str))

    # Re-run top rules on LO_skip0 baseline trades only.
    lo_trades = enriched.filter(pl.col("variant").str.contains("lo_r7_u30_q20_ts1_rg10_vt15_c15_lb3"))
    if lo_trades.is_empty():
        lo_trades = enriched.filter(pl.col("variant").is_not_null()).head(0)  # fallback
        # Use closest to LO_skip0: rebalance 7, uni 30, q20, ts1, regime flat
        for pat in [
            "lo_r7_u30_q20_ts1_rg10_vt15_c15_lb3",
            "lo_r7_u30_q20_ts1_rg10_vt15_c15",
        ]:
            sub = enriched.filter(pl.col("variant").str.starts_with(pat))
            if not sub.is_empty():
                lo_trades = sub
                break
    if lo_trades.is_empty():
        lo_trades = enriched

    best_rules = meta.get("best_rules") or []
    if best_rules:
        rules = [TradeFilterRule(**r) for r in best_rules]
        filt_metrics = filtered_backtest_metrics(
            lo_trades,
            rules=rules,
            hold_days=int(lo_trades["variant_rebalance_days"][0]) if "variant_rebalance_days" in lo_trades.columns else 7,
        )
        (study_dir / "filtered_lo_metrics.json").write_text(
            json.dumps(filt_metrics, indent=2, default=str), encoding="utf-8",
        )
        print("Filtered LO metrics:", json.dumps(filt_metrics, indent=2, default=str))

        # Full pipeline with mapped config gates.
        base_cfg = replace(
            MomentumFactorConfig(start_date=START, end_date=END),
            **LO_SKIP0,
        )
        gated_cfg = rules_to_factor_config(rules, base_cfg)
        print("Gated config overrides:", {
            k: getattr(gated_cfg, k)
            for k in ("min_ts_momentum", "min_momentum_avg", "max_carry", "max_realized_vol", "min_composite_score")
            if getattr(gated_cfg, k) is not None
        })

    print(f"Done. Artifacts under {study_dir}")


if __name__ == "__main__":
    main()
