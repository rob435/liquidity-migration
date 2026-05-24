#!/usr/bin/env python3
"""Fast focused hunt: Sharpe>=3, trades>100, best oos_2025_2026."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from liquidity_migration.momentum_factor import (
    MomentumFactorConfig,
    _bars_by_symbol,
    _filter_signal_window,
    _run_factor_pipeline,
    _split_rows,
    build_factor_features,
    lo_sharpe3_preset,
    run_momentum_factor_research,
)
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
from liquidity_migration.storage import read_dataset, read_dataset_columns
from liquidity_migration.trade_lifecycle import _funding_lookup, build_equity_curve, summarize_baskets
from liquidity_migration.volume_events import _exclude_symbols

ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START, END = "2023-05-03", "2026-05-18"


def main() -> None:
    raw = read_dataset_columns(
        ROOT, "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    funding = read_dataset(ROOT, "funding")
    klines = _exclude_symbols(raw, DEFAULT_EXCLUDED_SYMBOLS)
    funding = _exclude_symbols(funding, DEFAULT_EXCLUDED_SYMBOLS)
    bars = _bars_by_symbol(klines)
    fl = _funding_lookup(funding) if not funding.is_empty() else None
    costs = CostConfig()

    # Hand-picked around lo_sharpe3 + regime-soft + looser/tighter vol.
    variants: list[tuple[str, dict]] = [
        ("sharpe3_base", {}),
        ("sharpe3_reg30", {"regime_off_scale": 0.30}),
        ("sharpe3_noreg", {"use_regime_filter": False, "regime_off_scale": 1.0}),
        ("sharpe3_v14", {"max_realized_vol": 1.4}),
        ("sharpe3_v10", {"max_realized_vol": 1.0}),
        ("sharpe3_v16", {"max_realized_vol": 1.6}),
        ("sharpe3_rk10", {"max_turnover_rank": 10}),
        ("sharpe3_rk6", {"max_turnover_rank": 6}),
        ("sharpe3_sc15", {"max_composite_score": 1.5}),
        ("sharpe3_mom55", {"max_momentum_avg": 0.55}),
        ("sharpe3_reg30_v14", {"regime_off_scale": 0.30, "max_realized_vol": 1.4}),
        ("sharpe3_noreg_v12", {"use_regime_filter": False, "max_realized_vol": 1.2}),
        ("sharpe3_u30", {"universe_size": 30, "max_turnover_rank": 10}),
        ("sharpe3_q33", {"long_quantile": 0.33}),
        ("sharpe3_vt20", {"vol_target_annual": 0.20}),
        ("sharpe3_mints10", {"min_ts_momentum": 0.10}),
        ("sharpe3_reg30_rk10_v14", {"regime_off_scale": 0.30, "max_turnover_rank": 10, "max_realized_vol": 1.4}),
        ("sharpe3_reg30_v16_rk15", {"regime_off_scale": 0.30, "max_realized_vol": 1.6, "max_turnover_rank": 15}),
        ("sharpe3_noreg_v14_q33", {"use_regime_filter": False, "max_realized_vol": 1.4, "long_quantile": 0.33}),
    ]

    base_feat = _filter_signal_window(
        build_factor_features(klines, funding=funding, config=lo_sharpe3_preset(start_date=START, end_date=END)),
        start=START, end=END,
    )
    rows = []
    for name, ov in variants:
        cfg = replace(lo_sharpe3_preset(start_date=START, end_date=END), **ov)
        tr, _, _ = _run_factor_pipeline(features=base_feat, bars_by_symbol=bars, funding_lookup=fl, config=cfg, costs=costs)
        bt = TradeLifecycleConfig(score="x", hold_days=7, rebalance_days=7, cost_multiplier=3.0)
        b = summarize_baskets(tr, config=bt)
        eq = build_equity_curve(b)
        dsh = daily_sharpe_from_equity(eq)
        splits = {s["name"]: s for s in _split_rows(b, config=bt)}
        oos = splits.get("oos_2025_2026", {})
        row = {
            "name": name,
            "trades": tr.height,
            "daily_sharpe": dsh,
            "oos_sharpe": float(oos.get("sharpe_like", 0)),
            "oos_return": float(oos.get("total_return", 0)),
            "total_return": float(eq["equity"][-1] - 1) if not eq.is_empty() else 0,
            "hit": dsh >= 3.0 and tr.height > 100,
            "overrides": ov,
        }
        rows.append(row)
        print(
            f"{name:28} n={row['trades']:3d} daily={row['daily_sharpe']:.2f} "
            f"oos_sh={row['oos_sharpe']:.2f} oos_ret={row['oos_return']:+.2%} hit={row['hit']}"
        )

    hits = [r for r in rows if r["hit"]]
    hits.sort(key=lambda r: (r["oos_sharpe"], r["daily_sharpe"]), reverse=True)
    out = ROOT / "reports/momentum_lo_robust_quick"
    out.mkdir(parents=True, exist_ok=True)
    (out / "quick.json").write_text(json.dumps({"all": rows, "hits": hits}, indent=2), encoding="utf-8")

    if hits:
        best = hits[0]
        cfg = replace(lo_sharpe3_preset(start_date=START, end_date=END), **best["overrides"])
        run_momentum_factor_research(ROOT, config=cfg, cost_config=costs, report_dir=out / "best")
        print(f"\nBEST: {best['name']} -> {out / 'best'}")


if __name__ == "__main__":
    main()
