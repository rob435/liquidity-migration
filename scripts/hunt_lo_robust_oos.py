#!/usr/bin/env python3
"""Hunt LO configs: daily Sharpe>=3, trades>100, maximize oos_2025_2026 split Sharpe.

INTEGRITY WARNING — DO NOT RUN AS-IS (marked 2026-05-24 during cleanup).

The original `hunt_lo_sharpe3.py` was honest: it grid-mined IS Sharpe, found a
winner, and labelled the result `exploratory_in_sample`. Pre-2023 OOS sanity
(separate script) then confirmed the bybit overfit drop 3.95 → 0.73.

This script makes the methodology *worse*: `robust_score = 0.4*daily_sharpe +
0.6*oos_2025_2026_sharpe` turns the canonical walk-forward third into a
1,152-trial optimization target. The "winner" will look strong on both metrics
*because both were optimized simultaneously*, which masks the overfit instead
of exposing it. See memory `oos-vs-walkforward.md` and
`docs/backtesting_errors_we_never_repeat.md`.

What would actually be methodologically sound:
  1. Pre-register the top-5 from `hunt_lo_sharpe3.py` (or any other
     hypothesis-generation step) BEFORE looking at OOS metrics.
  2. Run those 5 once on a held-out window or via block-bootstrap CI on the
     IS period.
  3. Promote at most one based on the pre-registered ranking, not the
     OOS-conditioned ranking.

Execution bug also fixed below: `print(..., flush=True)` so the run is
visible when stdout is a pipe (the prior run sat silent for 7 min at 99%
CPU because Python block-buffers stdout when it's not a TTY).
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from liquidity_migration.momentum_factor import (
    MomentumFactorConfig,
    _bars_by_symbol,
    _filter_signal_window,
    _run_factor_pipeline,
    _split_rows,
    build_factor_features,
)
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
from liquidity_migration.storage import read_dataset, read_dataset_columns
from liquidity_migration.trade_lifecycle import (
    _funding_lookup,
    build_equity_curve,
    summarize_baskets,
)
from liquidity_migration.volume_events import _exclude_symbols


ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START, END = "2023-05-03", "2026-05-18"
OUT = ROOT / "reports/momentum_lo_robust_hunt"

BASE = dict(
    mode="long_only",
    momentum_skip_days=0,
    carry_weight=0.0,
    require_positive_ts_momentum_for_longs=True,
    vol_target_annual=0.15,
)


def eval_cfg(
    name: str,
    cfg: MomentumFactorConfig,
    *,
    features: pl.DataFrame,
    bars: dict,
    funding_lookup: dict | None,
    costs: CostConfig,
) -> dict[str, Any]:
    trades, _, _ = _run_factor_pipeline(
        features=features, bars_by_symbol=bars, funding_lookup=funding_lookup, config=cfg, costs=costs,
    )
    bt = TradeLifecycleConfig(
        score="momentum_factor", hold_days=cfg.rebalance_days, rebalance_days=cfg.rebalance_days,
        cost_multiplier=cfg.cost_multiplier,
    )
    baskets = summarize_baskets(trades, config=bt)
    equity = build_equity_curve(baskets)
    daily_sh = daily_sharpe_from_equity(equity)
    splits = {s["name"]: s for s in _split_rows(baskets, config=bt)}
    oos = splits.get("oos_2025_2026", {})
    n = trades.height
    hit = daily_sh >= 3.0 and n > 100
    robust_score = (daily_sh * 0.4 + float(oos.get("sharpe_like", 0)) * 0.6) if hit else 0.0
    return {
        "name": name,
        "trades": n,
        "daily_sharpe": daily_sh,
        "oos_sharpe": float(oos.get("sharpe_like", 0)),
        "oos_return": float(oos.get("total_return", 0)),
        "train_sharpe": float(splits.get("train_2023_2024", {}).get("sharpe_like", 0)),
        "val_sharpe": float(splits.get("validation_2024_2025", {}).get("sharpe_like", 0)),
        "total_return": float(equity["equity"][-1] - 1) if not equity.is_empty() else 0.0,
        "hit_target": hit,
        "robust_score": robust_score,
        "config": {
            k: v for k, v in cfg.__dict__.items()
            if v is not None and k not in ("exclude_symbols",)
        },
    }


def build_grid() -> list[tuple[str, dict[str, Any]]]:
    variants: list[tuple[str, dict[str, Any]]] = []
    for uni, lq, max_vol, max_rank, max_mom, max_sc, regime_on, off_sc, vt, rebal in product(
        (30, 50),
        (0.20, 0.33),
        (1.0, 1.2, 1.4, 1.6),
        (10, 15, None),
        (None, 0.55),
        (None, 1.5),
        (True, False),
        (0.0, 0.30),
        (0.15, 0.20),
        (7,),
    ):
        if not regime_on and off_sc != 0.0:
            continue
        o: dict[str, Any] = {
            **BASE,
            "universe_size": uni,
            "long_quantile": lq,
            "rebalance_days": rebal,
            "vol_target_annual": vt,
            "use_regime_filter": regime_on,
            "regime_off_scale": off_sc if regime_on else 1.0,
        }
        if max_vol is not None:
            o["max_realized_vol"] = max_vol
        if max_rank is not None:
            o["max_turnover_rank"] = max_rank
        if max_mom is not None:
            o["max_momentum_avg"] = max_mom
        if max_sc is not None:
            o["max_composite_score"] = max_sc
        tag = f"r{rebal}_u{uni}_q{int(lq*100)}_v{max_vol}_rk{max_rank}_rg{int(regime_on)}{int(off_sc*10)}_vt{int(vt*100)}"
        variants.append((tag, o))
    return variants


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    variants = build_grid()
    print(f"Robust hunt: {len(variants)} configs (optimize oos_2025_2026)", flush=True)

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

    cache: dict = {}
    rows: list[dict[str, Any]] = []
    t0 = time.time()

    for i, (name, overrides) in enumerate(variants):
        cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **overrides)
        sig = (
            cfg.universe_size, cfg.momentum_lookbacks_days, cfg.momentum_skip_days,
            cfg.universe_volume_window_days, cfg.regime_sma_days,
        )
        if sig not in cache:
            feat = build_factor_features(klines, funding=funding, config=cfg)
            cache[sig] = _filter_signal_window(feat, start=START, end=END)
        try:
            row = eval_cfg(name, cfg, features=cache[sig], bars=bars, funding_lookup=fl, costs=costs)
            rows.append(row)
            if row["hit_target"] and row["oos_sharpe"] >= 1.0:
                print(
                    f"  ** {name}: daily={row['daily_sharpe']:.2f} oos={row['oos_sharpe']:.2f} "
                    f"trades={row['trades']} robust={row['robust_score']:.2f}",
                    flush=True,
                )
        except Exception as e:
            rows.append({"name": name, "error": str(e)})
        if (i + 1) % 40 == 0:
            print(f"  [{i+1}/{len(variants)}]", flush=True)

    hits = [r for r in rows if r.get("hit_target")]
    hits.sort(key=lambda r: r["robust_score"], reverse=True)
    pl.DataFrame([{k: v for k, v in r.items() if k != "config"} for r in rows if "error" not in r]).write_csv(
        OUT / "robust_hunt.csv"
    )
    (OUT / "robust_top.json").write_text(json.dumps(hits[:30], indent=2, default=str), encoding="utf-8")

    print(f"\nDone {time.time()-t0:.0f}s. Hits (sh>=3, n>100): {len(hits)}")
    if hits:
        b = hits[0]
        print(f"BEST ROBUST: {b['name']} daily={b['daily_sharpe']:.2f} oos={b['oos_sharpe']:.2f} trades={b['trades']}")
        from liquidity_migration.momentum_factor import run_momentum_factor_research
        cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **b["config"])
        run_momentum_factor_research(ROOT, config=cfg, cost_config=costs, report_dir=OUT / "best_robust")


if __name__ == "__main__":
    main()
