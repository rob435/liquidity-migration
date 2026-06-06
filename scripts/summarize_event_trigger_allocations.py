#!/usr/bin/env python3
"""Summarize saved daily+event-trigger allocation artifacts.

This is an audit helper, not a backtest runner. It reads already-written
combined-book artifacts, recomputes period metrics from daily returns with equity
reset inside each period, and writes comparison tables for full/early/recent,
calendar years, and daily-vs-overlay contribution.
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
DEFAULT_RUNS = {
    "scale1": "daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05",
    "global15": "daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_scale15_2026-06-05",
    "maxVenue": "daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_venue_scale_2026-06-05",
    "robustVenue": "daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_robust_venue_scale_2026-06-05",
    "rescueV2": "daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_v2_2026-06-05",
}


def _rebuild_equity(rows: pl.DataFrame, ret_col: str = "basket_return") -> pl.DataFrame:
    if rows.is_empty():
        return rows
    return (
        rows.sort("ts_ms")
        .with_columns(pl.col(ret_col).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )


def _metrics(rows: pl.DataFrame, ret_col: str = "basket_return") -> dict[str, Any]:
    if rows.is_empty():
        return {"total_return": None, "mar": None, "max_drawdown": None, "sharpe": None}
    equity = _rebuild_equity(rows, ret_col)
    m = _daily_pnl_metrics(equity)
    returns = equity["basket_return"].drop_nulls()
    sharpe = None
    if len(returns) > 1 and float(returns.std()) > 0.0:
        sharpe = float(returns.mean()) / float(returns.std()) * math.sqrt(365)
    return {
        "total_return": m.get("total_return"),
        "mar": m.get("mar"),
        "max_drawdown": m.get("max_drawdown"),
        "sharpe": sharpe,
    }


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.{digits}f}"


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


def _parse_runs(raw: str | None) -> dict[str, str]:
    if not raw:
        return DEFAULT_RUNS
    out: dict[str, str] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"run spec must be label=directory; got {item!r}")
        label, directory = item.split("=", 1)
        out[label.strip()] = directory.strip()
    return out


def _period_rows(label: str, venue: str, root: Path, split_date: str) -> list[dict[str, Any]]:
    combined = pl.read_csv(root / venue / "combined_equity.csv").with_columns(
        pl.col("date").str.to_date().alias("_date"),
        pl.col("date").str.slice(0, 4).alias("year"),
    )
    out: list[dict[str, Any]] = []
    periods = [
        ("full", combined),
        ("early", combined.filter(pl.col("_date") < pl.lit(split_date).str.to_date())),
        ("recent", combined.filter(pl.col("_date") >= pl.lit(split_date).str.to_date())),
    ]
    for period, rows in periods:
        out.append({"run": label, "venue": venue, "period": period, **_metrics(rows)})
    for year in sorted(combined["year"].unique().to_list()):
        out.append({"run": label, "venue": venue, "period": str(year), **_metrics(combined.filter(pl.col("year") == year))})
    return out


def _decomp_rows(label: str, venue: str, root: Path) -> list[dict[str, Any]]:
    summary = pl.read_csv(root / "summary.csv").filter(pl.col("venue") == venue)
    if summary.is_empty():
        raise ValueError(f"{root / 'summary.csv'} has no row for venue {venue!r}")
    scale_col = "compound_overlay_scale" if "compound_overlay_scale" in summary.columns else "overlay_scale"
    overlay_scale = float(summary[scale_col][0])
    joined = pl.read_csv(root / venue / "joined_returns.csv").with_columns(
        pl.col("ts_ms").cast(pl.Int64),
        pl.from_epoch(pl.col("ts_ms").cast(pl.Int64), "ms").dt.year().alias("year"),
        (overlay_scale * (pl.col("overlay_return") + pl.col("extra_overlay_cost_return"))).alias(
            "overlay_component"
        ),
    ).with_columns(
        (pl.col("daily_return") + pl.col("overlay_component")).alias("combined_component")
    )
    out: list[dict[str, Any]] = []
    for year in sorted(joined["year"].unique().to_list()):
        rows = joined.filter(pl.col("year") == year)
        daily = _metrics(rows, "daily_return")
        overlay = _metrics(rows, "overlay_component")
        combined = _metrics(rows, "combined_component")
        out.append(
            {
                "run": label,
                "venue": venue,
                "year": year,
                "overlay_scale": overlay_scale,
                "daily_total_return": daily["total_return"],
                "overlay_total_return": overlay["total_return"],
                "combined_total_return": combined["total_return"],
                "daily_max_drawdown": daily["max_drawdown"],
                "overlay_max_drawdown": overlay["max_drawdown"],
                "combined_max_drawdown": combined["max_drawdown"],
            }
        )
    return out


def _markdown(periods: list[dict[str, Any]], decomp: list[dict[str, Any]]) -> str:
    lines = [
        "# Event-Trigger Allocation Comparison",
        "",
        "Generated from saved daily+overlay artifacts. Period metrics reset equity at each split.",
        "",
        "## Full Window",
        "",
        "| Run | Venue | Return | MAR | Sharpe | Max DD |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in periods:
        if row["period"] == "full":
            lines.append(
                f"| {row['run']} | {row['venue']} | {_fmt(row['total_return'])} | {_fmt(row['mar'])} | "
                f"{_fmt(row['sharpe'])} | {_fmt(row['max_drawdown'])} |"
            )
    lines += [
        "",
        "## Early/Recent",
        "",
        "| Run | Venue | Period | Return | MAR | Sharpe | Max DD |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in periods:
        if row["period"] in {"early", "recent"}:
            lines.append(
                f"| {row['run']} | {row['venue']} | {row['period']} | {_fmt(row['total_return'])} | "
                f"{_fmt(row['mar'])} | {_fmt(row['sharpe'])} | {_fmt(row['max_drawdown'])} |"
            )
    lines += [
        "",
        "## Yearly Decomposition",
        "",
        "| Run | Venue | Year | Daily Ret | Overlay Ret | Combined Ret | Daily DD | Overlay DD | Combined DD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in decomp:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['year']} | {_fmt(row['daily_total_return'])} | "
            f"{_fmt(row['overlay_total_return'])} | {_fmt(row['combined_total_return'])} | "
            f"{_fmt(row['daily_max_drawdown'])} | {_fmt(row['overlay_max_drawdown'])} | "
            f"{_fmt(row['combined_max_drawdown'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", default=str(Path.home() / "SHARED_DATA"))
    parser.add_argument("--runs", default=None, help="Comma-separated label=directory list; defaults to current candidates.")
    parser.add_argument("--split-date", default="2025-06-01")
    parser.add_argument("--out", default=str(Path.home() / "SHARED_DATA" / "event_trigger_allocation_summary_2026-06-05"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact_root = Path(args.artifact_root).expanduser()
    out_root = Path(args.out).expanduser()
    runs = _parse_runs(args.runs)
    period_rows: list[dict[str, Any]] = []
    decomp_rows: list[dict[str, Any]] = []
    for label, directory in runs.items():
        root = artifact_root / directory
        if not root.exists():
            raise FileNotFoundError(root)
        for venue in VENUES:
            period_rows.extend(_period_rows(label, venue, root, args.split_date))
            decomp_rows.extend(_decomp_rows(label, venue, root))
    _write_csv(out_root / "period_metrics.csv", period_rows)
    _write_csv(out_root / "yearly_decomposition.csv", decomp_rows)
    (out_root / "allocation_comparison.md").write_text(_markdown(period_rows, decomp_rows), encoding="utf-8")
    print(f"period_metrics={out_root / 'period_metrics.csv'}")
    print(f"yearly_decomposition={out_root / 'yearly_decomposition.csv'}")
    print(f"markdown={out_root / 'allocation_comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
