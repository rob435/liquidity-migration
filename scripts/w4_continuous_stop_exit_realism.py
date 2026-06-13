#!/usr/bin/env python3
"""W4 Stage 1 stop/exit realism harness for the frozen continuous ensemble."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import rebuild_winner_base_component_ledgers as rb  # noqa: E402

from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    build_full_ledger,
    frozen_config_hash,
)
from liquidity_migration.continuous_rebalance import decompose_continuous_components  # noqa: E402


SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
SWEEP_TAG = "w4_continuous_stage1_stop_exit_2026-06-13"
COMPONENTS = {
    "turn3p3": ("continuous_merged_signal_raw_2026-06-07", "merged_signal"),
    "turn4p3": ("independent_continuous_entry_filter_sweep_exploratory_2026-06-07", "age240_turn4pop3_crowd2"),
    "turn4p5": ("independent_continuous_entry_filter_sweep_exploratory_2026-06-07", "age240_turn4pop5_crowd2"),
    "age210tp14": ("independent_continuous_tp_hold_sweep_exploratory_2026-06-07", "age210_tp14_hold24_invvol10_crowd2"),
}
CELL_OVERRIDES = {cell: overrides for (_artifact_root, cell), overrides in rb.CELLS.items()}
ARMS: dict[str, dict[str, Any]] = {
    "00_frozen_no_stop": {},
    "01_disaster_stop_capped": {
        "stop_loss_pct": 0.25,
        "stop_fill_mode": "bar_extreme_capped",
        "stop_slippage_cap_pct": 0.10,
    },
    "02_stop_ff6_be10": {
        "stop_loss_pct": 0.25,
        "stop_fill_mode": "bar_extreme_capped",
        "stop_slippage_cap_pct": 0.10,
        "failed_fade_hours": 6,
        "failed_fade_loss_pct": 0.04,
        "failed_fade_min_mfe_pct": 0.01,
        "breakeven_arm_pct": 0.10,
    },
    "03_stop_uncapped_extreme": {
        "stop_loss_pct": 0.25,
        "stop_fill_mode": "bar_extreme",
    },
}
MS_PER_DAY = 86_400_000


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w4_continuous_stop_exit_realism.py",
        REPO / "scripts" / "rebuild_winner_base_component_ledgers.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
        REPO / "liquidity_migration" / "trade_lifecycle.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _date_dirs(dataset: Path) -> list[tuple[str, Path]]:
    if not dataset.exists():
        return []
    out = []
    for item in dataset.iterdir():
        if item.is_dir() and item.name.startswith("date="):
            out.append((item.name.split("=", 1)[1], item))
    return sorted(out)


def _partition_pairs(dataset: Path, *, start: str | None, end: str | None) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    excluded = set(DEFAULT_EXCLUDED_SYMBOLS)
    for day, date_dir in _date_dirs(dataset):
        if start and day < start:
            continue
        if end and day >= end:
            continue
        symbol_dirs = [
            item for item in date_dir.iterdir()
            if item.is_dir() and item.name.startswith("symbol=")
        ]
        if symbol_dirs:
            for symbol_dir in symbol_dirs:
                symbol = symbol_dir.name.split("=", 1)[1]
                if symbol not in excluded:
                    pairs.add((day, symbol))
            continue

        parquet_files = sorted(date_dir.glob("*.parquet"))
        if not parquet_files:
            continue
        frame = pl.scan_parquet([str(path) for path in parquet_files]).select("symbol").collect()
        for (symbol,) in frame.unique().iter_rows():
            symbol = str(symbol)
            if symbol not in excluded:
                pairs.add((day, symbol))
    return pairs


def _kline_bounds(root: Path) -> tuple[dict[str, tuple[str, str]], set[tuple[str, str]]]:
    pairs = _partition_pairs(root / "klines_1h", start=None, end=None)
    bounds: dict[str, list[str]] = {}
    for day, symbol in pairs:
        if symbol not in bounds:
            bounds[symbol] = [day, day]
        else:
            bounds[symbol][0] = min(bounds[symbol][0], day)
            bounds[symbol][1] = max(bounds[symbol][1], day)
    return {symbol: (span[0], span[1]) for symbol, span in bounds.items()}, pairs


def _pit_partition_gate(root: Path, *, start: str, end: str) -> dict[str, Any]:
    bounds, all_kline_pairs = _kline_bounds(root)
    manifest_pairs = _partition_pairs(root / "archive_trade_manifest", start=start, end=end)
    kline_pairs = {(day, symbol) for day, symbol in all_kline_pairs if start <= day < end}
    required = {
        (day, symbol)
        for day, symbol in manifest_pairs
        if symbol in bounds and bounds[symbol][0] <= day <= bounds[symbol][1]
    }
    missing = sorted(required - kline_pairs)
    manifest_symbols = {symbol for _day, symbol in manifest_pairs}
    kline_symbols = set(bounds)
    missing_symbols = sorted(manifest_symbols - kline_symbols)
    return {
        "method": "date/symbol partition gate matching volume_events_pit required-span semantics",
        "window_start": start,
        "window_end_exclusive": end,
        "manifest_pairs": len(manifest_pairs),
        "required_pairs": len(required),
        "kline_pairs_in_window": len(kline_pairs),
        "manifest_symbols": len(manifest_symbols),
        "kline_symbols": len(kline_symbols),
        "missing_symbols": len(missing_symbols),
        "missing_required_pairs": len(missing),
        "missing_symbol_sample": missing_symbols[:20],
        "missing_required_pair_sample": missing[:20],
        "full_pit_universe_pass": bool(manifest_symbols) and not missing_symbols and not missing,
    }


def _btc_inputs(root: Path, venue: str, days: list[int]) -> tuple[dict[int, float], dict[int, float]]:
    closes = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by("day").agg(pl.col("close").last().alias("close"))
        .sort("day")
    )
    rets: dict[int, float] = {}
    prev_day, prev_close = None, None
    for day, close in closes.iter_rows():
        if prev_day is not None and int(day) - int(prev_day) == MS_PER_DAY and float(prev_close) > 0:
            rets[int(day)] = float(close) / float(prev_close) - 1.0
        prev_day, prev_close = int(day), float(close)
    funding_dir = root / ("funding" if venue == "bybit" else "binance_usdm_funding")
    funding: dict[int, float] = {}
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = funding_dir / f"date={date}" / "symbol=BTCUSDT"
        if part.exists():
            funding[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return rets, funding


def _component_config(component: str, cell: str, *, start: str, end: str, arm_overrides: dict[str, Any]) -> ContinuousEventConfig:
    overrides = CELL_OVERRIDES[cell]
    return ContinuousEventConfig(**{**rb.COMMON, **overrides, **arm_overrides, "start_date": start, "end_date": end})


def _load_component(report_dir: Path):
    payload = json.loads((report_dir / "continuous_report.json").read_text(encoding="utf-8"))
    trades = pl.read_csv(report_dir / "continuous_trades.csv")
    mtm = pl.read_csv(report_dir / "continuous_mtm_equity.csv").select("ts_ms", "basket_return").sort("ts_ms")
    return decompose_continuous_components(trades, mtm, payload["config"]), trades, payload


def _monthly_returns(ledger: pl.DataFrame) -> pl.DataFrame:
    if ledger.is_empty():
        return pl.DataFrame({"month": [], "strategy_return": []})
    return (
        ledger.select("ts_ms", "basket_return")
        .with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
        .sort("month")
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _adverse_rows(venue: str, arm: str, component: str, trades: pl.DataFrame) -> list[dict[str, Any]]:
    if trades.is_empty():
        return []
    cols = ["exit_reason", "net_return", "mae", "mfe"]
    present = [col for col in cols if col in trades.columns]
    frame = trades.select(present)
    rows = []
    for reason_frame in frame.partition_by("exit_reason", maintain_order=True):
        reason = str(reason_frame["exit_reason"][0])
        out = {
            "venue": venue,
            "arm": arm,
            "component": component,
            "exit_reason": reason,
            "n": reason_frame.height,
            "sum_net_return": float(reason_frame["net_return"].sum()) if "net_return" in reason_frame.columns else 0.0,
            "mean_net_return_bps": float(reason_frame["net_return"].mean() * 10_000.0) if "net_return" in reason_frame.columns else 0.0,
            "worst_net_return_bps": float(reason_frame["net_return"].min() * 10_000.0) if "net_return" in reason_frame.columns else 0.0,
        }
        if "mae" in reason_frame.columns:
            out["worst_mae_pct"] = float(reason_frame["mae"].min() * 100.0)
            out["mean_mae_pct"] = float(reason_frame["mae"].mean() * 100.0)
        if "mfe" in reason_frame.columns:
            out["best_mfe_pct"] = float(reason_frame["mfe"].max() * 100.0)
            out["mean_mfe_pct"] = float(reason_frame["mfe"].mean() * 100.0)
        rows.append(out)
    return rows


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    lines = [
        "# W4 Continuous Stage 1 Stop / Exit Realism",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        "",
        "## Summary",
        "",
        "| Venue | Arm | Trades | Return | MAR | Max DD | Worst Day | Bps/Trade | MAR Delta | Return Delta |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["summary_rows"]:
        lines.append(
            f"| `{row['venue']}` | `{row['arm']}` | {row['component_trades']} | "
            f"{row['total_return']:.4f} | {row['mar'] if row['mar'] is not None else 'NA'} | "
            f"{row['max_drawdown']:.4f} | {row['worst_day_return']:.4f} | "
            f"{row['bps_per_component_trade']:.2f} | "
            f"{row.get('delta_mar_vs_control') if row.get('delta_mar_vs_control') is not None else 'NA'} | "
            f"{row.get('delta_return_vs_control') if row.get('delta_return_vs_control') is not None else 'NA'} |"
        )
    lines.extend(
        [
            "",
            "## Registered Falsifier",
            "",
            payload["registered_falsifier"],
            "",
            "This stage is exploratory/mechanism evidence only. It is not paper-ready or real-money evidence.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": "docs/preregistration/2026-06-13-w4-continuous-stage1-stop-exit-realism.md",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "sweep_tag": SWEEP_TAG,
        "git_head": _git_head(),
        "code_hash": _code_hash(),
        "frozen_forward_config": FROZEN_FORWARD_CONFIG,
        "frozen_forward_config_hash": frozen_config_hash(),
        "arms": ARMS,
        "roots": {venue: str(ROOTS[venue]) for venue in venues},
        "pit_partition_gate": {},
        "summary_rows": [],
        "component_rows": [],
        "adverse_rows": [],
        "registered_falsifier": (
            "Reject this exact mechanism if 02_stop_ff6_be10 is non-positive on either venue, "
            "pooled MAR delta <= -0.50, any venue MAR delta <= -1.00, or uncapped stop fills "
            "change the conclusion."
        ),
    }

    for venue in venues:
        root = ROOTS[venue]
        pit = _pit_partition_gate(root, start=args.start, end=args.end)
        payload["pit_partition_gate"][venue] = pit
        for arm, arm_overrides in ARMS.items():
            pieces = {}
            component_trade_count = 0
            component_hashes: dict[str, str] = {}
            for component, (_artifact_root, cell) in COMPONENTS.items():
                report_dir = root / "reports" / SWEEP_TAG / arm / component
                cfg = _component_config(component, cell, start=args.start, end=args.end, arm_overrides=arm_overrides)
                result = run_continuous_event_research(root, config=cfg, report_dir=report_dir)
                comp, trades, _report = _load_component(report_dir)
                pieces[component] = comp
                component_trade_count += int(trades.height)
                component_hashes[component] = cfg.config_hash()
                payload["component_rows"].append(
                    {
                        "venue": venue,
                        "arm": arm,
                        "component": component,
                        "cell": cell,
                        "config_hash": cfg.config_hash(),
                        "report_dir": str(report_dir),
                        "n_fresh_entries": result.get("n_fresh_entries"),
                        "n_trades": int(trades.height),
                        "funding_mode": result.get("funding_mode"),
                    }
                )
                payload["adverse_rows"].extend(_adverse_rows(venue, arm, component, trades))

            all_days = sorted({day for comp in pieces.values() for day in comp.days})
            btc_rets, btc_funding = _btc_inputs(root, venue, all_days)
            ledger = build_full_ledger(pieces, btc_rets, btc_funding)
            metrics = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
            arm_dir = root / "reports" / SWEEP_TAG / arm
            arm_dir.mkdir(parents=True, exist_ok=True)
            ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
            monthly = _monthly_returns(ledger)
            monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
            report = {
                "preregistration": payload["preregistration"],
                "run_label": "exploratory",
                "date_range": {"start": args.start, "end": args.end},
                "best_scenario": {
                    "total_return": metrics.get("total_return", 0.0),
                    "max_drawdown": metrics.get("max_drawdown", 0.0),
                    "sharpe_like": metrics.get("sharpe_like", 0.0),
                    "trades": component_trade_count,
                },
                "pit_manifest": {
                    "full_pit_universe_pass": bool(pit["full_pit_universe_pass"]),
                    **pit,
                },
                "data_root": str(root),
                "venue": venue,
                "arm": arm,
                "component_config_hashes": component_hashes,
                "code_hash": payload["code_hash"],
                "frozen_forward_config_hash": payload["frozen_forward_config_hash"],
                "ledger": str(arm_dir / "ensemble_hedged_ledger.csv"),
            }
            (arm_dir / "volume_event_research_report.json").write_text(
                json.dumps(report, indent=2, default=str),
                encoding="utf-8",
            )
            payload["summary_rows"].append(
                {
                    "venue": venue,
                    "arm": arm,
                    "data_root": str(root),
                    "report_dir": str(arm_dir),
                    "full_pit_universe_pass": bool(pit["full_pit_universe_pass"]),
                    "component_trades": component_trade_count,
                    "total_return": float(metrics.get("total_return", 0.0)),
                    "annualized_return": float(metrics.get("annualized_return", 0.0)),
                    "mar": metrics.get("mar"),
                    "max_drawdown": float(metrics.get("max_drawdown", 0.0)),
                    "sharpe_like": float(metrics.get("sharpe_like", 0.0)),
                    "worst_day_return": float(metrics.get("worst_day_return", 0.0)),
                    "bps_per_component_trade": (
                        float(metrics.get("total_return", 0.0)) / max(component_trade_count, 1) * 10_000.0
                    ),
                }
            )

    controls = {row["venue"]: row for row in payload["summary_rows"] if row["arm"] == "00_frozen_no_stop"}
    for row in payload["summary_rows"]:
        control = controls.get(row["venue"])
        if not control or row["arm"] == "00_frozen_no_stop":
            row["delta_return_vs_control"] = 0.0
            row["delta_mar_vs_control"] = 0.0
            row["delta_max_drawdown_vs_control"] = 0.0
            continue
        row["delta_return_vs_control"] = row["total_return"] - control["total_return"]
        row["delta_mar_vs_control"] = (
            None if row["mar"] is None or control["mar"] is None else float(row["mar"]) - float(control["mar"])
        )
        row["delta_max_drawdown_vs_control"] = row["max_drawdown"] - control["max_drawdown"]

    _write_csv(out / "stage1_summary.csv", payload["summary_rows"])
    _write_csv(out / "stage1_components.csv", payload["component_rows"])
    _write_csv(out / "stage1_adverse_path.csv", payload["adverse_rows"])
    (out / "stage1_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage1_summary.md")
    return payload


def repair_pit_metadata(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    summary_path = out / "stage1_summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"missing stage1 summary: {summary_path}")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    repaired_at = dt.datetime.now(dt.timezone.utc).isoformat()
    previous_code_hash = payload.get("code_hash")
    payload["metadata_repair"] = {
        "utc": repaired_at,
        "reason": (
            "Correct archive_trade_manifest reader: manifests are date=*/part.parquet "
            "with symbol as a column, not date=*/symbol=* partitions."
        ),
        "previous_code_hash": previous_code_hash,
        "repair_code_hash": _code_hash(),
        "trade_simulation_rerun": False,
    }
    payload["code_hash"] = _code_hash()
    payload["pit_partition_gate"] = {
        venue: _pit_partition_gate(ROOTS[venue], start=args.start, end=args.end)
        for venue in venues
    }

    for row in payload.get("summary_rows", []):
        venue = str(row.get("venue"))
        if venue in payload["pit_partition_gate"]:
            row["full_pit_universe_pass"] = bool(
                payload["pit_partition_gate"][venue]["full_pit_universe_pass"]
            )

    for venue in venues:
        pit = payload["pit_partition_gate"][venue]
        root = ROOTS[venue]
        for arm in ARMS:
            report_path = root / "reports" / SWEEP_TAG / arm / "volume_event_research_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["pit_manifest"] = {
                "full_pit_universe_pass": bool(pit["full_pit_universe_pass"]),
                **pit,
            }
            report["metadata_repair"] = payload["metadata_repair"]
            report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    _write_csv(out / "stage1_summary.csv", payload["summary_rows"])
    summary_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage1_summary.md")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-06-10")
    parser.add_argument("--out", default=str(SHARED / "w4_continuous_stage1_stop_exit_2026-06-13"))
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Repair PIT metadata in existing Stage 1 artifacts without rerunning trade simulations.",
    )
    args = parser.parse_args()
    payload = repair_pit_metadata(args) if args.metadata_only else run_stage(args)
    print(json.dumps({"out": payload["out"], "summary_rows": payload["summary_rows"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
