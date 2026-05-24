#!/usr/bin/env python3
"""Grid-hunt long-only configs: daily Sharpe >= 3 and trades > 100 (exploratory)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from liquidity_migration.momentum_factor import (
    MODE_LONG_ONLY,
    MomentumFactorConfig,
    _bars_by_symbol,
    _filter_signal_window,
    _run_factor_pipeline,
    build_factor_features,
)
from liquidity_migration.momentum_trade_forensics import daily_sharpe_from_equity
from liquidity_migration.storage import read_dataset, read_dataset_columns
from liquidity_migration.trade_lifecycle import (
    _funding_lookup,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from liquidity_migration.volume_events import _exclude_symbols


ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h").expanduser()
START, END = "2023-05-03", "2026-05-18"
OUT = ROOT / "reports/momentum_lo_sharpe3_hunt"

BASE = dict(
    mode=MODE_LONG_ONLY,
    momentum_skip_days=0,
    carry_weight=0.0,
    require_positive_ts_momentum_for_longs=True,
    vol_target_annual=0.15,
    regime_off_scale=0.0,
    use_regime_filter=True,
)


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
        features=features,
        bars_by_symbol=bars,
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
    n = trades.height
    return {
        "name": name,
        "trades": n,
        "daily_sharpe": daily_sh,
        "basket_sharpe": summary.get("sharpe_like", 0.0),
        "total_return": summary.get("total_return", 0.0),
        "max_drawdown": summary.get("max_drawdown", 0.0),
        "win_rate": summary.get("trade_win_rate", 0.0),
        "config": {k: v for k, v in asdict(cfg).items() if v is not None},
        "hit_target": daily_sh >= 3.0 and n > 100,
    }


def build_grid() -> list[tuple[str, dict[str, Any]]]:
    """Staged grid (~180 configs) around forensics winners."""
    variants: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(tag: str, overrides: dict[str, Any]) -> None:
        if tag in seen:
            return
        seen.add(tag)
        variants.append((tag, {**BASE, **overrides}))

    # Core vol-cap ladder (main lever for Sharpe).
    for rebal, uni, lq, max_vol, max_rank, max_score, max_mom, vt in product(
        (7,),
        (30, 50),
        (0.20, 0.33),
        (0.63, 0.75, 0.90, 1.05, 1.20, 1.40, None),
        (None, 10, 15),
        (None, 0.5, 1.5),
        (None, 0.55),
        (0.15, 0.20, 0.25),
    ):
        o: dict[str, Any] = {
            "rebalance_days": rebal,
            "universe_size": uni,
            "long_quantile": lq,
            "vol_target_annual": vt,
            "max_realized_vol": max_vol,
        }
        if max_rank is not None:
            o["max_turnover_rank"] = max_rank
        if max_score is not None:
            o["max_composite_score"] = max_score
        if max_mom is not None:
            o["max_momentum_avg"] = max_mom
        tag = f"r{rebal}_u{uni}_q{int(lq*100)}_v{max_vol}_rk{max_rank}_sc{max_score}_m{max_mom}_vt{int(vt*100)}"
        add(tag, o)

    # Regime-soft + no-regime (more trades while filtered).
    for max_vol, max_rank in product((0.75, 0.90, 1.05, None), (None, 10, 15)):
        for regime_on, off_sc in ((True, 0.30), (False, 1.0)):
            o = {
                "rebalance_days": 7,
                "universe_size": 30,
                "long_quantile": 0.33,
                "vol_target_annual": 0.20,
                "use_regime_filter": regime_on,
                "regime_off_scale": off_sc,
            }
            if max_vol is not None:
                o["max_realized_vol"] = max_vol
            if max_rank is not None:
                o["max_turnover_rank"] = max_rank
            add(f"soft_rg{int(regime_on)}{int(off_sc*10)}_v{max_vol}_rk{max_rank}", o)

    # TS momentum floor variants on best vol band.
    for min_ts, max_vol in product((0.05, 0.10, 0.15, 0.20), (0.75, 0.90, 1.05)):
        add(
            f"ts{min_ts}_v{max_vol}",
            {
                "rebalance_days": 7,
                "universe_size": 30,
                "long_quantile": 0.33,
                "vol_target_annual": 0.20,
                "min_ts_momentum": min_ts,
                "max_realized_vol": max_vol,
                "max_composite_score": 1.5,
            },
        )

    return variants


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    variants = build_grid()
    print(f"Hunting {len(variants)} configs for daily Sharpe>=3, trades>100 ...")

    t0 = time.time()
    raw = read_dataset_columns(
        ROOT,
        "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    funding = read_dataset(ROOT, "funding")
    klines = _exclude_symbols(raw, DEFAULT_EXCLUDED_SYMBOLS)
    funding = _exclude_symbols(funding, DEFAULT_EXCLUDED_SYMBOLS)
    bars = _bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if not funding.is_empty() else None
    costs = CostConfig()
    print(f"  data loaded {time.time()-t0:.1f}s")

    feature_cache: dict[tuple[Any, ...], pl.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []

    for i, (name, overrides) in enumerate(variants):
        cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **overrides)
        sig = feature_signature(cfg)
        if sig not in feature_cache:
            feat = build_factor_features(klines, funding=funding, config=cfg)
            feature_cache[sig] = _filter_signal_window(feat, start=START, end=END)
        try:
            row = eval_cfg(
                name, cfg,
                features=feature_cache[sig],
                bars=bars,
                funding_lookup=funding_lookup,
                costs=costs,
            )
            rows.append(row)
            if row["hit_target"]:
                hits.append(row)
                print(f"  *** HIT {name}: trades={row['trades']} daily_sh={row['daily_sharpe']:.2f} ret={row['total_return']:+.2%}")
        except Exception as e:
            rows.append({"name": name, "error": str(e)})

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(variants)}] hits so far: {len(hits)}")

    flat = []
    for r in rows:
        if "error" in r:
            continue
        row = {k: v for k, v in r.items() if k != "config"}
        flat.append(row)
    df = pl.DataFrame(flat) if flat else pl.DataFrame()
    if not df.is_empty():
        df = df.sort("daily_sharpe", descending=True)
        df.write_csv(OUT / "hunt_results.csv")

    hits_sorted = sorted(hits, key=lambda r: (r["daily_sharpe"], r["trades"]), reverse=True)
    (OUT / "hits.json").write_text(json.dumps(hits_sorted, indent=2, default=str), encoding="utf-8")

    print(f"\nDone in {time.time()-t0:.0f}s. Hits: {len(hits)}")
    if hits_sorted:
        best = hits_sorted[0]
        print(f"BEST: {best['name']} trades={best['trades']} daily_sh={best['daily_sharpe']:.2f}")
        # Full report for best hit
        cfg = replace(MomentumFactorConfig(start_date=START, end_date=END), **{k: v for k, v in best["config"].items()})
        from liquidity_migration.momentum_factor import run_momentum_factor_research

        run_momentum_factor_research(
            ROOT,
            config=cfg,
            cost_config=costs,
            report_dir=OUT / f"best_{best['name']}",
        )
    else:
        top = df.filter(pl.col("trades") > 100).head(10) if not df.is_empty() else df.head(10)
        print("No hits. Top candidates with trades>100:")
        print(top)


if __name__ == "__main__":
    main()
