#!/usr/bin/env python3
"""Audit concentration in saved event-trigger overlay artifacts.

This reads existing execution + combined-book outputs only. It does not rebuild
signals or touch PIT roots.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Any

import polars as pl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402


VENUES = ("bybit", "binance")
DEFAULT_CELL = "q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop25_imp50_cap1m"


def _parse_runs(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"run spec must be label=path; got {item!r}")
        label, path = item.split("=", 1)
        out[label.strip()] = Path(path.strip()).expanduser()
    return out


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
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.{digits}f}"


def _metrics_from_returns(df: pl.DataFrame, ret_col: str) -> dict[str, Any]:
    if df.is_empty():
        return {"total_return": None, "mar": None, "max_drawdown": None, "sharpe": None}
    equity = (
        df.sort("ts_ms")
        .with_columns(pl.col(ret_col).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )
    metrics = _daily_pnl_metrics(equity)
    returns = equity["basket_return"].drop_nulls()
    sharpe = None
    if len(returns) > 1 and float(returns.std()) > 0.0:
        sharpe = float(returns.mean()) / float(returns.std()) * math.sqrt(365)
    return {
        "total_return": metrics.get("total_return"),
        "mar": metrics.get("mar"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": sharpe,
    }


def _run_summary(combined_root: Path, venue: str) -> dict[str, Any]:
    summary = pl.read_csv(combined_root / "summary.csv").filter(pl.col("venue") == venue)
    if summary.is_empty():
        raise ValueError(f"{combined_root / 'summary.csv'} has no row for {venue}")
    return dict(zip(summary.columns, summary.row(0), strict=False))


def _trade_concentration(
    *,
    label: str,
    venue: str,
    execution_root: Path,
    combined_root: Path,
    cell_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary = _run_summary(combined_root, venue)
    scale = float(summary.get("compound_overlay_scale") or summary.get("overlay_scale") or 1.0)
    cost_mult = float(summary.get("overlay_cost_multiplier") or 1.0)
    trades_path = execution_root / venue / "cells" / cell_id / "short_only" / "trades.csv"
    trades = pl.read_csv(trades_path)
    trades = trades.with_columns(
        pl.col("entry_date").str.slice(0, 4).alias("entry_year"),
        (scale * (pl.col("net_return") + (cost_mult - 1.0) * pl.col("cost_return"))).alias("scaled_net_return"),
    )
    total = float(trades["scaled_net_return"].sum()) if trades.height else 0.0
    gross_positive = float(trades.filter(pl.col("scaled_net_return") > 0)["scaled_net_return"].sum()) if trades.height else 0.0
    symbol = (
        trades.group_by("symbol")
        .agg(
            pl.len().alias("trades"),
            pl.col("scaled_net_return").sum().alias("scaled_net_return"),
            pl.col("scaled_net_return").mean().alias("mean_scaled_net_return"),
            (pl.col("scaled_net_return") > 0).mean().alias("win_rate"),
            pl.col("mae").min().alias("worst_mae"),
            pl.col("mfe").max().alias("best_mfe"),
        )
        .sort("scaled_net_return", descending=True)
    )
    symbol_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(symbol.iter_rows(named=True), start=1):
        contribution = float(row["scaled_net_return"])
        symbol_rows.append(
            {
                "run": label,
                "venue": venue,
                "rank": rank,
                **row,
                "share_of_total_return": contribution / total if abs(total) > 1e-12 else None,
                "share_of_positive_return": contribution / gross_positive if gross_positive > 1e-12 and contribution > 0 else None,
            }
        )
    year = (
        trades.group_by("entry_year")
        .agg(
            pl.len().alias("trades"),
            pl.col("scaled_net_return").sum().alias("scaled_net_return"),
            pl.col("scaled_net_return").mean().alias("mean_scaled_net_return"),
            (pl.col("scaled_net_return") > 0).mean().alias("win_rate"),
        )
        .sort("entry_year")
    )
    year_rows = [
        {
            "run": label,
            "venue": venue,
            **row,
            "share_of_total_return": float(row["scaled_net_return"]) / total if abs(total) > 1e-12 else None,
        }
        for row in year.iter_rows(named=True)
    ]
    return symbol_rows, year_rows


def _combined_rows(label: str, venue: str, combined_root: Path) -> list[dict[str, Any]]:
    combined = pl.read_csv(combined_root / venue / "combined_equity.csv").with_columns(
        pl.col("date").str.slice(0, 4).alias("year")
    )
    out: list[dict[str, Any]] = []
    for period, rows in [("full", combined)] + [
        (str(year), combined.filter(pl.col("year") == year)) for year in sorted(combined["year"].unique().to_list())
    ]:
        combined_m = _metrics_from_returns(rows, "basket_return")
        daily_m = _metrics_from_returns(rows, "daily_return")
        overlay_col = "scaled_overlay_return" if "scaled_overlay_return" in rows.columns else "overlay_return"
        overlay_m = _metrics_from_returns(rows, overlay_col)
        out.append(
            {
                "run": label,
                "venue": venue,
                "period": period,
                "combined_return": combined_m["total_return"],
                "combined_mar": combined_m["mar"],
                "combined_sharpe": combined_m["sharpe"],
                "combined_max_drawdown": combined_m["max_drawdown"],
                "daily_return": daily_m["total_return"],
                "overlay_return": overlay_m["total_return"],
                "overlay_max_drawdown": overlay_m["max_drawdown"],
            }
        )
    return out


def _scale_rows(label: str, venue: str, combined_root: Path) -> dict[str, Any]:
    row = _run_summary(combined_root, venue)
    keep = [
        "overlay_scale",
        "compound_overlay_scale",
        "overlay_cost_multiplier",
        "overlay_trades",
        "filtered_overlay_trades",
        "combined_total_return",
        "combined_mar",
        "combined_max_drawdown",
        "max_drawdown_abs_ratio",
        "venue_pass",
    ]
    return {"run": label, "venue": venue, **{k: row.get(k) for k in keep}}


def _concentration_summary(symbol_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in symbol_rows:
        grouped.setdefault((str(row["run"]), str(row["venue"])), []).append(row)
    out: list[dict[str, Any]] = []
    for (run, venue), rows in sorted(grouped.items()):
        ranked = sorted(rows, key=lambda r: int(r["rank"]))
        base: dict[str, Any] = {"run": run, "venue": venue, "symbols": len(ranked)}
        for n in (1, 5, 10):
            top = ranked[:n]
            base[f"top{n}_share_total"] = sum(float(r["share_of_total_return"] or 0.0) for r in top)
            base[f"top{n}_share_positive"] = sum(float(r["share_of_positive_return"] or 0.0) for r in top)
            base[f"top{n}_trades"] = sum(int(r["trades"]) for r in top)
        out.append(base)
    return out


def _markdown(
    scale_rows: list[dict[str, Any]],
    symbol_rows: list[dict[str, Any]],
    combined_rows: list[dict[str, Any]],
    concentration_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# Event-Trigger Concentration Audit",
        "",
        "Generated from saved execution and combined-book artifacts.",
        "",
        "## Scale Summary",
        "",
        "| Run | Venue | Scale | Trades | Return | MAR | DD Ratio | Pass |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in scale_rows:
        lines.append(
            f"| {row['run']} | {row['venue']} | {_fmt(row.get('compound_overlay_scale'))} | "
            f"{row.get('filtered_overlay_trades')} | {_fmt(row.get('combined_total_return'))} | "
            f"{_fmt(row.get('combined_mar'))} | {_fmt(row.get('max_drawdown_abs_ratio'))} | "
            f"{row.get('venue_pass')} |"
        )
    lines += [
        "",
        "## Concentration Summary",
        "",
        "| Run | Venue | Symbols | Top1 Total | Top5 Total | Top10 Total | Top1 Positive | Top5 Positive | Top10 Positive |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in concentration_rows:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['symbols']} | "
            f"{_fmt(row.get('top1_share_total'))} | {_fmt(row.get('top5_share_total'))} | "
            f"{_fmt(row.get('top10_share_total'))} | {_fmt(row.get('top1_share_positive'))} | "
            f"{_fmt(row.get('top5_share_positive'))} | {_fmt(row.get('top10_share_positive'))} |"
        )
    lines += [
        "",
        "## Top Symbol Concentration",
        "",
        "| Run | Venue | Rank | Symbol | Trades | Scaled Return | Share Total | Share Positive | Win Rate |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in [r for r in symbol_rows if int(r["rank"]) <= 10]:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['rank']} | {row['symbol']} | {row['trades']} | "
            f"{_fmt(row.get('scaled_net_return'))} | {_fmt(row.get('share_of_total_return'))} | "
            f"{_fmt(row.get('share_of_positive_return'))} | {_fmt(row.get('win_rate'))} |"
        )
    lines += [
        "",
        "## Combined Yearly",
        "",
        "| Run | Venue | Period | Combined Ret | MAR | Sharpe | Daily Ret | Overlay Ret | DD |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in combined_rows:
        if row["period"] == "full":
            continue
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['period']} | {_fmt(row.get('combined_return'))} | "
            f"{_fmt(row.get('combined_mar'))} | {_fmt(row.get('combined_sharpe'))} | "
            f"{_fmt(row.get('daily_return'))} | {_fmt(row.get('overlay_return'))} | "
            f"{_fmt(row.get('combined_max_drawdown'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-root", required=True)
    parser.add_argument("--cell-id", default=DEFAULT_CELL)
    parser.add_argument(
        "--runs",
        required=True,
        help="Comma-separated label=combined_root entries.",
    )
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    execution_root = Path(args.execution_root).expanduser()
    runs = _parse_runs(args.runs)
    out_root = Path(args.out).expanduser()
    all_symbols: list[dict[str, Any]] = []
    all_years: list[dict[str, Any]] = []
    all_combined: list[dict[str, Any]] = []
    all_scales: list[dict[str, Any]] = []
    for label, combined_root in runs.items():
        for venue in VENUES:
            symbols, years = _trade_concentration(
                label=label,
                venue=venue,
                execution_root=execution_root,
                combined_root=combined_root,
                cell_id=args.cell_id,
            )
            all_symbols.extend(symbols)
            all_years.extend(years)
            all_combined.extend(_combined_rows(label, venue, combined_root))
            all_scales.append(_scale_rows(label, venue, combined_root))
    _write_csv(out_root / "symbol_concentration.csv", all_symbols)
    _write_csv(out_root / "trade_year_concentration.csv", all_years)
    _write_csv(out_root / "combined_period_metrics.csv", all_combined)
    _write_csv(out_root / "scale_summary.csv", all_scales)
    concentration_rows = _concentration_summary(all_symbols)
    _write_csv(out_root / "concentration_summary.csv", concentration_rows)
    (out_root / "concentration_audit.md").write_text(
        _markdown(all_scales, all_symbols, all_combined, concentration_rows),
        encoding="utf-8",
    )
    print(f"symbol_concentration={out_root / 'symbol_concentration.csv'}")
    print(f"trade_year_concentration={out_root / 'trade_year_concentration.csv'}")
    print(f"combined_period_metrics={out_root / 'combined_period_metrics.csv'}")
    print(f"scale_summary={out_root / 'scale_summary.csv'}")
    print(f"concentration_summary={out_root / 'concentration_summary.csv'}")
    print(f"markdown={out_root / 'concentration_audit.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
