#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[1]
SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
FULL_LIVE_ARTIFACT = Path(
    os.environ.get("FULL_LIVE_ARTIFACT", str(SHARED / "full_live_system_backtest_2026-06-18"))
)

sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(FULL_LIVE_ARTIFACT))

import continuous_exit_cause_ablation as exitdiag  # noqa: E402
import continuous_live_feature_ablation_runner as broad  # noqa: E402

from liquidity_migration.continuous_events import build_continuous_panel  # noqa: E402


RUNGS: list[dict[str, Any]] = [
    {
        "id": "00_prefreeze_gate_off",
        "gate": "off",
        "state_exit": True,
        "stop_approach": True,
        "server_stop": False,
        "failed_fade": True,
        "breakeven": True,
        "exit_cooldown": True,
        "adverse_breaker": True,
        "hedge_regime": False,
    },
    {
        "id": "01_v2_gate_off_no_server_stop",
        "gate": "off",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
        "hedge_regime": False,
    },
    {
        "id": "02_v2_gate_off_server_stop",
        "gate": "off",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
        "hedge_regime": False,
    },
    {
        "id": "03_v2_gate_off_server_stop_breaker",
        "gate": "off",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": False,
    },
    {
        "id": "04_v2_uptrend_server_stop_breaker",
        "gate": "uptrend",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": False,
    },
    {
        "id": "05_v2_uptrend_server_stop_breaker_hedged",
        "source_rung": "04_v2_uptrend_server_stop_breaker",
        "gate": "uptrend",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": True,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": True,
    },
    {
        "id": "06_v2_uptrend_no_server_stop_no_breaker",
        "gate": "uptrend",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": False,
        "hedge_regime": False,
    },
    {
        "id": "07_v2_uptrend_no_server_stop_breaker",
        "gate": "uptrend",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": False,
    },
    {
        "id": "08_v2_uptrend_no_server_stop_breaker_hedged",
        "source_rung": "07_v2_uptrend_no_server_stop_breaker",
        "gate": "uptrend",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": True,
    },
    {
        "id": "09_v2_uptrend_no_server_stop_breaker_invvol",
        "gate": "uptrend",
        "sizing": "inverse_vol",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": False,
    },
    {
        "id": "10_v2_uptrend_no_server_stop_breaker_invvol_hedged",
        "source_rung": "09_v2_uptrend_no_server_stop_breaker_invvol",
        "gate": "uptrend",
        "sizing": "inverse_vol",
        "state_exit": False,
        "stop_approach": False,
        "server_stop": False,
        "failed_fade": False,
        "breakeven": False,
        "exit_cooldown": False,
        "adverse_breaker": True,
        "hedge_regime": True,
    },
]


def _counts(df: pl.DataFrame, col: str) -> dict[str, int]:
    return broad.counts(df, col)


def _pooled_table(out_root: Path, rows: list[dict[str, Any]]) -> dict[str, str]:
    table_path = out_root / "live_v2_redesign_table.csv"
    pooled_path = out_root / "pooled_live_v2_redesign_table.csv"
    df = pl.DataFrame(rows)
    if df.is_empty():
        return {"table": str(table_path), "pooled_table": str(pooled_path)}
    df = df.sort(["venue", "rung"])
    df.write_csv(table_path)
    pooled = (
        df.group_by("rung", maintain_order=True)
        .agg(
            [
                pl.col("total_return").mean().alias("mean_total_return"),
                pl.col("total_return").min().alias("min_total_return"),
                pl.col("total_return").max().alias("max_total_return"),
                pl.col("max_drawdown").mean().alias("mean_max_drawdown"),
                pl.col("n_trades").sum().alias("total_trades"),
            ]
        )
        .sort("rung")
    )
    pooled = pooled.with_columns(
        (pl.col("mean_total_return") - pl.col("mean_total_return").shift(1)).alias("delta_mean_return_vs_prev")
    )
    pooled.write_csv(pooled_path)
    return {"table": str(table_path), "pooled_table": str(pooled_path)}


def run_venue(
    venue: str,
    *,
    start_date: str,
    end_date: str,
    out_root: Path,
    scratch_base: Path | None,
    only_rungs: set[str] | None,
) -> list[dict[str, Any]]:
    real_root = broad.ROOTS[venue]
    scratch = broad.resolve_scratch(real_root, out_root, end_date, scratch_base)
    print(f"[{venue}] scratch={scratch}", flush=True)
    panel_cfg = broad.base_cfg(start_date, end_date, gate="off", sizing="flat", live_exits=False)
    panel = build_continuous_panel(scratch, panel_cfg)
    print(f"[{venue}] panel rows={panel.height}", flush=True)

    fixed_cfg = exitdiag.make_cfg(start_date, end_date, RUNGS[1])
    state_cfg = exitdiag.make_cfg(start_date, end_date, RUNGS[0])
    fixed_candidates = broad.shared_candidates(panel, fixed_cfg)
    state_candidates = broad.shared_candidates(panel, state_cfg)
    symbols: set[str] = set()
    for candidates in (fixed_candidates, state_candidates):
        if not candidates.is_empty():
            symbols.update(str(s) for s in candidates["symbol"].unique().to_list())
    load_cfg = exitdiag.make_cfg(start_date, end_date, RUNGS[4])
    bars, funding_lookup, klines, btc_trend = broad.load_market(scratch, load_cfg, symbols)
    ages = broad.live.listing_ts(scratch)

    rows: list[dict[str, Any]] = []
    cache: dict[str, tuple[Any, pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[str, Any]]] = {}
    baseline_trades: pl.DataFrame | None = None
    requested_rungs = only_rungs or {str(r["id"]) for r in RUNGS}
    required_sources = {
        str(r["source_rung"])
        for r in RUNGS
        if str(r["id"]) in requested_rungs and r.get("source_rung")
    }
    selected_rungs = []
    for rung in RUNGS:
        rid = str(rung["id"])
        if rid in requested_rungs or rid in required_sources:
            selected_rungs.append(rung)
    for rung in selected_rungs:
        rid = str(rung["id"])
        out_dir = out_root / venue / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[{venue}] rung={rid}", flush=True)
        if rung.get("hedge_regime"):
            source = str(rung["source_rung"])
            cfg, trades, mtm, _rebalanced, meta = cache[source]
            rebalanced = broad.apply_hedge(venue, real_root, cfg, trades, mtm)
            summary_meta = {"source_rung": source, **meta}
        else:
            cfg = exitdiag.make_cfg(start_date, end_date, rung)
            candidates = state_candidates if rung["state_exit"] else fixed_candidates
            trades, mtm, rebalanced, summary_meta = exitdiag.run_exit_rung(
                venue=venue,
                cfg=cfg,
                opts=rung,
                candidates=candidates,
                bars=bars,
                funding_lookup=funding_lookup,
                klines=klines,
                btc_trend=btc_trend,
                ages=ages,
                out_dir=out_dir,
            )
            if baseline_trades is None and rid == "01_v2_gate_off_no_server_stop":
                baseline_trades = trades
        trades_path = out_dir / "trades.csv"
        mtm_path = out_dir / "mtm.csv"
        rb_path = out_dir / ("rebalanced_hedged.csv" if rung.get("hedge_regime") else "rebalanced_unhedged.csv")
        trades.write_csv(trades_path)
        mtm.write_csv(mtm_path)
        rebalanced.write_csv(rb_path)
        reason_stats = exitdiag.exit_reason_stats(trades)
        if not reason_stats.is_empty():
            reason_stats.write_csv(out_dir / "exit_reason_stats.csv")
        metrics = broad.stats(rebalanced)
        raw_metrics = broad.stats(mtm)
        delta = {}
        if baseline_trades is not None and rid in requested_rungs:
            delta = exitdiag.matched_trade_delta(
                venue=venue,
                rung=rid,
                baseline=baseline_trades,
                trades=trades,
                out_dir=out_dir,
            )
        summary = {
            "venue": venue,
            "rung": rid,
            "run_label": "exploratory_registered",
            "config": asdict(cfg),
            "redesign_cell": rung,
            "data_root": str(real_root),
            "scratch_root": str(scratch),
            "metrics": metrics,
            "raw_mtm_metrics": raw_metrics,
            "n_trades": int(trades.height),
            "exit_reasons": _counts(trades, "exit_reason"),
            "component_trades": _counts(trades, "component"),
            "meta": summary_meta,
            "matched_trade_delta": delta,
            "paths": {"trades": str(trades_path), "mtm": str(mtm_path), "rebalanced": str(rb_path)},
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        cache[rid] = (cfg, trades, mtm, rebalanced, summary_meta)
        row = {
            "venue": venue,
            "rung": rid,
            "gate": str(rung["gate"]),
            "server_stop": bool(rung["server_stop"]),
            "adverse_breaker": bool(rung["adverse_breaker"]),
            "hedge_regime": bool(rung.get("hedge_regime", False)),
            "total_return": metrics["total_return"],
            "max_drawdown": metrics["max_drawdown"],
            "mar": metrics["mar"],
            "sharpe_like": metrics["sharpe_like"],
            "worst_day_return": metrics["worst_day_return"],
            "n_trades": int(trades.height),
            "exit_reasons": json.dumps(_counts(trades, "exit_reason"), sort_keys=True),
        }
        if delta:
            row.update({k: v for k, v in delta.items() if k not in {"venue", "rung"}})
        if rid in requested_rungs:
            rows.append(row)
        print(
            f"[{venue}] {rid} ret={metrics['total_return']:+.4f} "
            f"dd={metrics['max_drawdown']:+.4f} sr={metrics['sharpe_like']:+.2f} trades={trades.height}",
            flush=True,
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-date", default="2023-04-01")
    ap.add_argument("--end-date", required=True)
    ap.add_argument("--venues", nargs="+", default=["bybit", "binance"], choices=["bybit", "binance"])
    ap.add_argument("--out-root", default="backtest-runs/continuous_live_v2_redesign_2026-06-18")
    ap.add_argument("--scratch-base", default=str(FULL_LIVE_ARTIFACT))
    ap.add_argument("--only-rungs", nargs="*", default=None, help="optional subset of rung ids to run")
    args = ap.parse_args()
    out_root = Path(args.out_root).expanduser()
    if not out_root.is_absolute():
        out_root = REPO / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    scratch_base = Path(args.scratch_base).expanduser() if args.scratch_base else None
    only_rungs = set(args.only_rungs) if args.only_rungs else None

    rows: list[dict[str, Any]] = []
    for venue in args.venues:
        rows.extend(
            run_venue(
                venue,
                start_date=args.start_date,
                end_date=args.end_date,
                out_root=out_root,
                scratch_base=scratch_base,
                only_rungs=only_rungs,
            )
        )
    paths = _pooled_table(out_root, rows)
    summary = {
        "run_label": "exploratory_registered",
        "preregistration": "docs/preregistration/2026-06-18-continuous-live-v2-exit-redesign.md",
        "purpose": "Registered live-v2 redesign replay after stop_approach was identified as the live lifecycle collapse point.",
        "window": {"start_date": args.start_date, "end_date_exclusive": args.end_date},
        "rungs": RUNGS,
        "only_rungs": sorted(only_rungs) if only_rungs else None,
        "venues": args.venues,
        "scratch_base": str(scratch_base) if scratch_base else None,
        "artifact_dependency": str(FULL_LIVE_ARTIFACT),
        "table": rows,
        "paths": paths,
        "known_omissions": [
            "Sniper PostOnly add-on is not replayed.",
            "Server stop is approximated on hourly high/low/close bars.",
            "This is a demo/paper redesign replay, not real-money promotion evidence.",
        ],
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"summary: {out_root / 'summary.json'}", flush=True)
    print(f"live_v2_redesign_table: {paths['table']}", flush=True)
    print(f"pooled_live_v2_redesign_table: {paths['pooled_table']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
