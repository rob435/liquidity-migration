#!/usr/bin/env python3
"""Temporal and concentration audit for trade-level capped overlay blends."""
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


def _parse_cooldown_hours(raw: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        value = int(item)
        if value < 0:
            raise ValueError(f"cooldown hours must be non-negative; got {value}")
        out.append(value)
    return tuple(out)


def _parse_float_grid(raw: str) -> tuple[float, ...]:
    out: list[float] = []
    for part in raw.split(","):
        item = part.strip()
        if item:
            out.append(float(item))
    return tuple(out)


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


def _run_summary(root: Path, venue: str) -> dict[str, Any]:
    summary = pl.read_csv(root / "summary.csv").filter(pl.col("venue") == venue)
    if summary.is_empty():
        raise ValueError(f"{root / 'summary.csv'} has no row for {venue}")
    return dict(zip(summary.columns, summary.row(0), strict=False))


def _venue_dir(root: Path, venue: str) -> Path:
    summary = _run_summary(root, venue)
    cap = float(summary["max_active_notional"])
    cost = float(summary["cost_multiplier"])
    label = f"cap{cap:g}"
    threshold = summary.get("addon_same_symbol_primary_min_unrealized")
    if threshold not in (None, ""):
        label = f"{label}_pnl{float(threshold):g}"
    return root / f"{label}_cost{cost:g}" / venue


def _equity_from_returns(rows: pl.DataFrame, ret_col: str) -> pl.DataFrame:
    return (
        rows.sort("ts_ms")
        .with_columns(pl.col(ret_col).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )


def _metrics(rows: pl.DataFrame, ret_col: str) -> dict[str, Any]:
    if rows.is_empty():
        return {"total_return": None, "mar": None, "max_drawdown": None, "sharpe": None}
    equity = _equity_from_returns(rows, ret_col)
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


def _period_rows(label: str, venue: str, root: Path) -> list[dict[str, Any]]:
    combined = pl.read_csv(_venue_dir(root, venue) / "combined_equity.csv").with_columns(
        pl.col("date").str.slice(0, 4).alias("year")
    )
    joined = pl.read_csv(_venue_dir(root, venue) / "joined_returns.csv").with_columns(
        pl.col("ts_ms").cast(pl.Int64),
        pl.from_epoch(pl.col("ts_ms").cast(pl.Int64), "ms").dt.year().cast(pl.Utf8).alias("year"),
    )
    out: list[dict[str, Any]] = []
    periods: list[tuple[str, pl.DataFrame, pl.DataFrame]] = [("full", combined, joined)]
    for year in sorted(combined["year"].unique().to_list()):
        periods.append((str(year), combined.filter(pl.col("year") == year), joined.filter(pl.col("year") == str(year))))
    for period, combined_rows, joined_rows in periods:
        combined_m = _metrics(combined_rows, "basket_return")
        daily_raw_m = _metrics(joined_rows, "daily_return_raw")
        daily_eff_m = _metrics(joined_rows, "daily_return_effective")
        overlay_m = _metrics(joined_rows, "overlay_return")
        out.append(
            {
                "run": label,
                "venue": venue,
                "period": period,
                "combined_return": combined_m["total_return"],
                "combined_mar": combined_m["mar"],
                "combined_sharpe": combined_m["sharpe"],
                "combined_max_drawdown": combined_m["max_drawdown"],
                "daily_raw_return": daily_raw_m["total_return"],
                "daily_effective_return": daily_eff_m["total_return"],
                "overlay_return": overlay_m["total_return"],
                "overlay_max_drawdown": overlay_m["max_drawdown"],
            }
        )
    return out


def _trade_rows(label: str, venue: str, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trades = pl.read_csv(_venue_dir(root, venue) / "blended_trades.csv").with_columns(
        pl.col("entry_date").str.slice(0, 4).alias("entry_year")
    )
    total = float(trades["net_return"].sum()) if trades.height else 0.0
    positive = float(trades.filter(pl.col("net_return") > 0)["net_return"].sum()) if trades.height else 0.0
    by_source = (
        trades.group_by(["blend_source", "entry_year"])
        .agg(
            pl.len().alias("trades"),
            pl.col("net_return").sum().alias("net_return"),
            pl.col("notional_weight").sum().alias("notional_sum"),
            pl.col("blend_accept_fraction").mean().alias("mean_accept_fraction"),
            (pl.col("net_return") > 0).mean().alias("win_rate"),
        )
        .sort(["blend_source", "entry_year"])
    )
    source_rows = [
        {
            "run": label,
            "venue": venue,
            **row,
            "share_total": float(row["net_return"]) / total if abs(total) > 1e-12 else None,
        }
        for row in by_source.iter_rows(named=True)
    ]
    symbols = (
        trades.group_by(["blend_source", "symbol"])
        .agg(
            pl.len().alias("trades"),
            pl.col("net_return").sum().alias("net_return"),
            pl.col("notional_weight").sum().alias("notional_sum"),
            pl.col("blend_accept_fraction").mean().alias("mean_accept_fraction"),
            (pl.col("net_return") > 0).mean().alias("win_rate"),
        )
        .sort("net_return", descending=True)
    )
    symbol_rows: list[dict[str, Any]] = []
    for source in sorted(symbols["blend_source"].unique().to_list()):
        subset = symbols.filter(pl.col("blend_source") == source).sort("net_return", descending=True)
        source_total = float(trades.filter(pl.col("blend_source") == source)["net_return"].sum())
        source_positive = float(
            trades.filter((pl.col("blend_source") == source) & (pl.col("net_return") > 0))["net_return"].sum()
        )
        for rank, row in enumerate(subset.iter_rows(named=True), start=1):
            net = float(row["net_return"])
            symbol_rows.append(
                {
                    "run": label,
                    "venue": venue,
                    "rank": rank,
                    **row,
                    "share_source_total": net / source_total if abs(source_total) > 1e-12 else None,
                    "share_source_positive": net / source_positive if source_positive > 1e-12 and net > 0 else None,
                    "share_book_total": net / total if abs(total) > 1e-12 else None,
                    "share_book_positive": net / positive if positive > 1e-12 and net > 0 else None,
                }
            )
    concentration_rows: list[dict[str, Any]] = []
    for source in sorted(symbols["blend_source"].unique().to_list()):
        ranked = [r for r in symbol_rows if r["venue"] == venue and r["blend_source"] == source]
        ranked = sorted(ranked, key=lambda r: int(r["rank"]))
        base: dict[str, Any] = {"run": label, "venue": venue, "blend_source": source, "symbols": len(ranked)}
        for n in (1, 5, 10):
            top = ranked[:n]
            base[f"top{n}_share_source_total"] = sum(float(r["share_source_total"] or 0.0) for r in top)
            base[f"top{n}_share_source_positive"] = sum(float(r["share_source_positive"] or 0.0) for r in top)
            base[f"top{n}_trades"] = sum(int(r["trades"]) for r in top)
        concentration_rows.append(base)
    return source_rows, symbol_rows, concentration_rows


def _load_blended_trades(root: Path, venue: str) -> pl.DataFrame:
    path = _venue_dir(root, venue) / "blended_trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return pl.read_csv(path).with_columns(
        pl.col("entry_ts_ms").cast(pl.Int64),
        pl.col("exit_ts_ms").cast(pl.Int64),
        pl.col("net_return").cast(pl.Float64),
        pl.col("notional_weight").cast(pl.Float64),
    )


def _trade_day(ts_ms: int) -> int:
    return ts_ms // 86_400_000 if ts_ms > 0 else 0


def _gap_quantiles(gaps: list[float]) -> dict[str, Any]:
    if not gaps:
        return {
            "same_symbol_gap_count": 0,
            "min_same_symbol_gap_hours": None,
            "p10_same_symbol_gap_hours": None,
            "median_same_symbol_gap_hours": None,
            "reentries_under_6h": 0,
            "reentries_under_24h": 0,
            "reentries_under_48h": 0,
        }
    values = sorted(gaps)
    return {
        "same_symbol_gap_count": len(values),
        "min_same_symbol_gap_hours": values[0],
        "p10_same_symbol_gap_hours": values[min(len(values) - 1, int(0.10 * (len(values) - 1)))],
        "median_same_symbol_gap_hours": values[len(values) // 2],
        "reentries_under_6h": sum(1 for value in values if value < 6.0),
        "reentries_under_24h": sum(1 for value in values if value < 24.0),
        "reentries_under_48h": sum(1 for value in values if value < 48.0),
    }


def _same_symbol_gap_rows(label: str, venue: str, trades: pl.DataFrame) -> list[dict[str, Any]]:
    rows = trades.sort(["symbol", "entry_ts_ms", "blend_source", "trade_id"]).iter_rows(named=True)
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    combined_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source = str(row.get("blend_source") or "")
        symbol = str(row.get("symbol") or "")
        if not source or not symbol:
            continue
        by_group.setdefault((source, symbol), []).append(row)
        combined_by_symbol.setdefault(symbol, []).append(row)

    out: list[dict[str, Any]] = []
    for (source, symbol), source_rows in by_group.items():
        out.extend(_gap_rows_for_events(label, venue, source, symbol, source_rows))
    for symbol, symbol_rows in combined_by_symbol.items():
        out.extend(_gap_rows_for_events(label, venue, "combined", symbol, symbol_rows))
    out.sort(key=lambda row: (row["run"], row["venue"], row["source"], row["gap_hours"], row["symbol"]))
    return out


def _gap_rows_for_events(
    label: str,
    venue: str,
    source: str,
    symbol: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    events = sorted(rows, key=lambda row: (int(row["entry_ts_ms"]), str(row.get("blend_source") or ""), str(row.get("trade_id") or "")))
    for previous, current in zip(events, events[1:], strict=False):
        gap_hours = max(0.0, (int(current["entry_ts_ms"]) - int(previous["entry_ts_ms"])) / 3_600_000)
        out.append(
            {
                "run": label,
                "venue": venue,
                "source": source,
                "symbol": symbol,
                "previous_source": previous.get("blend_source"),
                "current_source": current.get("blend_source"),
                "previous_entry_ts_ms": previous.get("entry_ts_ms"),
                "entry_ts_ms": current.get("entry_ts_ms"),
                "gap_hours": gap_hours,
                "previous_trade_id": previous.get("trade_id"),
                "trade_id": current.get("trade_id"),
                "previous_net_return": previous.get("net_return"),
                "net_return": current.get("net_return"),
                "previous_notional_weight": previous.get("notional_weight"),
                "notional_weight": current.get("notional_weight"),
            }
        )
    return out


def _churn_summary_rows(label: str, venue: str, trades: pl.DataFrame) -> list[dict[str, Any]]:
    rows = trades.to_dicts()
    out: list[dict[str, Any]] = []
    for source in [*sorted({str(row.get("blend_source") or "") for row in rows if row.get("blend_source")}), "combined"]:
        source_rows = rows if source == "combined" else [row for row in rows if str(row.get("blend_source") or "") == source]
        if not source_rows:
            continue
        day_counts: dict[int, int] = {}
        symbol_day_counts: dict[tuple[str, int], int] = {}
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in source_rows:
            ts_ms = int(row["entry_ts_ms"])
            symbol = str(row.get("symbol") or "")
            day = _trade_day(ts_ms)
            day_counts[day] = day_counts.get(day, 0) + 1
            symbol_day_counts[(symbol, day)] = symbol_day_counts.get((symbol, day), 0) + 1
            by_symbol.setdefault(symbol, []).append(row)
        gaps: list[float] = []
        for symbol_rows in by_symbol.values():
            ordered = sorted(symbol_rows, key=lambda row: int(row["entry_ts_ms"]))
            for previous, current in zip(ordered, ordered[1:], strict=False):
                gaps.append(max(0.0, (int(current["entry_ts_ms"]) - int(previous["entry_ts_ms"])) / 3_600_000))
        peak_day = max(day_counts, key=day_counts.get, default=0)
        peak_symbol_day = max(symbol_day_counts, key=symbol_day_counts.get, default=("", 0))
        gap_stats = _gap_quantiles(gaps)
        out.append(
            {
                "run": label,
                "venue": venue,
                "source": source,
                "trades": len(source_rows),
                "symbols": len(by_symbol),
                "active_trade_days": len(day_counts),
                "avg_trades_per_active_day": len(source_rows) / len(day_counts) if day_counts else None,
                "max_trades_per_day": day_counts.get(peak_day, 0),
                "max_trades_day": peak_day,
                "max_trades_per_symbol_day": symbol_day_counts.get(peak_symbol_day, 0),
                "max_trades_symbol": peak_symbol_day[0],
                "max_trades_symbol_day": peak_symbol_day[1],
                "net_return_sum": sum(float(row.get("net_return") or 0.0) for row in source_rows),
                "notional_weight_sum": sum(float(row.get("notional_weight") or 0.0) for row in source_rows),
                **gap_stats,
            }
        )
    return out


def _addon_primary_overlap_summary(label: str, venue: str, trades: pl.DataFrame) -> dict[str, Any]:
    primary = [row for row in trades.filter(pl.col("blend_source") == "primary").iter_rows(named=True)]
    addon = [row for row in trades.filter(pl.col("blend_source") == "addon").iter_rows(named=True)]
    primary_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in primary:
        primary_by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)
    overlaps = 0
    unrealized: list[float] = []
    for row in addon:
        symbol = str(row.get("symbol") or "")
        entry_ts = int(row.get("entry_ts_ms") or 0)
        entry_price = float(row.get("entry_price") or 0.0)
        active = [
            primary_row
            for primary_row in primary_by_symbol.get(symbol, [])
            if int(primary_row.get("entry_ts_ms") or 0) <= entry_ts < int(primary_row.get("exit_ts_ms") or 0)
        ]
        if not active:
            continue
        overlaps += 1
        if entry_price > 0.0:
            vals = [
                float(primary_row.get("entry_price") or 0.0) / entry_price - 1.0
                for primary_row in active
                if float(primary_row.get("entry_price") or 0.0) > 0.0
            ]
            if vals:
                unrealized.append(min(vals))
    return {
        "run": label,
        "venue": venue,
        "addon_trades": len(addon),
        "addon_same_symbol_active_primary_trades": overlaps,
        "addon_same_symbol_active_primary_fraction": overlaps / len(addon) if addon else None,
        "mean_active_primary_unrealized_at_addon_entry": sum(unrealized) / len(unrealized) if unrealized else None,
        "min_active_primary_unrealized_at_addon_entry": min(unrealized) if unrealized else None,
    }


def _addon_primary_gate_candidates(trades: pl.DataFrame) -> list[dict[str, Any]]:
    primary = [row for row in trades.filter(pl.col("blend_source") == "primary").iter_rows(named=True)]
    addon = [row for row in trades.filter(pl.col("blend_source") == "addon").iter_rows(named=True)]
    primary_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in primary:
        primary_by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)
    out: list[dict[str, Any]] = []
    for row in addon:
        symbol = str(row.get("symbol") or "")
        entry_ts = int(row.get("entry_ts_ms") or 0)
        entry_price = float(row.get("entry_price") or 0.0)
        active = [
            primary_row
            for primary_row in primary_by_symbol.get(symbol, [])
            if int(primary_row.get("entry_ts_ms") or 0) <= entry_ts < int(primary_row.get("exit_ts_ms") or 0)
        ]
        vals = [
            float(primary_row.get("entry_price") or 0.0) / entry_price - 1.0
            for primary_row in active
            if entry_price > 0.0 and float(primary_row.get("entry_price") or 0.0) > 0.0
        ]
        item = dict(row)
        item["active_same_symbol_primary_count"] = len(active)
        item["worst_active_primary_unrealized"] = min(vals) if vals else None
        out.append(item)
    return out


def _primary_pnl_gate_sensitivity_rows(
    label: str,
    venue: str,
    trades: pl.DataFrame,
    thresholds: tuple[float, ...],
) -> list[dict[str, Any]]:
    addon = _addon_primary_gate_candidates(trades)
    total_return = sum(float(row.get("net_return") or 0.0) for row in addon)
    total_weight = sum(float(row.get("notional_weight") or 0.0) for row in addon)
    out: list[dict[str, Any]] = []
    for threshold in thresholds:
        skipped = [
            row
            for row in addon
            if row.get("worst_active_primary_unrealized") is not None
            and float(row["worst_active_primary_unrealized"]) < threshold
        ]
        skipped_return = sum(float(row.get("net_return") or 0.0) for row in skipped)
        skipped_weight = sum(float(row.get("notional_weight") or 0.0) for row in skipped)
        out.append(
            {
                "run": label,
                "venue": venue,
                "threshold": threshold,
                "addon_trades": len(addon),
                "skipped_trades": len(skipped),
                "kept_trades": len(addon) - len(skipped),
                "skipped_trade_fraction": len(skipped) / len(addon) if addon else None,
                "total_addon_net_return": total_return,
                "skipped_net_return": skipped_return,
                "kept_addon_net_return": total_return - skipped_return,
                "skipped_return_share": skipped_return / total_return if abs(total_return) > 1e-12 else None,
                "total_addon_notional_weight": total_weight,
                "skipped_notional_weight": skipped_weight,
                "skipped_weight_share": skipped_weight / total_weight if total_weight > 1e-12 else None,
                "skipped_positive_fraction": (
                    sum(1 for row in skipped if float(row.get("net_return") or 0.0) > 0.0) / len(skipped)
                    if skipped
                    else None
                ),
                "skipped_symbols": len({str(row.get("symbol") or "") for row in skipped}),
            }
        )
    return out


def _cooldown_sensitivity_rows(
    label: str,
    venue: str,
    trades: pl.DataFrame,
    cooldown_hours: tuple[int, ...],
) -> list[dict[str, Any]]:
    addon = sorted(
        [row for row in trades.filter(pl.col("blend_source") == "addon").iter_rows(named=True)],
        key=lambda row: (str(row.get("symbol") or ""), int(row.get("entry_ts_ms") or 0), str(row.get("trade_id") or "")),
    )
    total_return = sum(float(row.get("net_return") or 0.0) for row in addon)
    total_weight = sum(float(row.get("notional_weight") or 0.0) for row in addon)
    out: list[dict[str, Any]] = []
    for hours in cooldown_hours:
        cooldown_ms = int(hours) * 3_600_000
        last_accepted: dict[str, dict[str, Any]] = {}
        kept: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in addon:
            symbol = str(row.get("symbol") or "")
            previous = last_accepted.get(symbol)
            if previous is None or int(row["entry_ts_ms"]) - int(previous["entry_ts_ms"]) >= cooldown_ms:
                kept.append(row)
                last_accepted[symbol] = row
                continue
            skipped.append(row)
        skipped_return = sum(float(row.get("net_return") or 0.0) for row in skipped)
        skipped_weight = sum(float(row.get("notional_weight") or 0.0) for row in skipped)
        out.append(
            {
                "run": label,
                "venue": venue,
                "source": "addon",
                "cooldown_hours": hours,
                "trades": len(addon),
                "kept_trades": len(kept),
                "skipped_trades": len(skipped),
                "skipped_trade_fraction": len(skipped) / len(addon) if addon else None,
                "total_net_return": total_return,
                "kept_net_return": total_return - skipped_return,
                "skipped_net_return": skipped_return,
                "skipped_return_share": skipped_return / total_return if abs(total_return) > 1e-12 else None,
                "total_notional_weight": total_weight,
                "skipped_notional_weight": skipped_weight,
                "skipped_weight_share": skipped_weight / total_weight if total_weight > 1e-12 else None,
                "skipped_positive_fraction": (
                    sum(1 for row in skipped if float(row.get("net_return") or 0.0) > 0.0) / len(skipped)
                    if skipped
                    else None
                ),
                "skipped_symbols": len({str(row.get("symbol") or "") for row in skipped}),
            }
        )
    return out


def _markdown(
    periods: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    concentration: list[dict[str, Any]],
    churn: list[dict[str, Any]],
    overlap: list[dict[str, Any]],
    primary_pnl_gate: list[dict[str, Any]],
    cooldown: list[dict[str, Any]],
) -> str:
    lines = [
        "# Trade-Capped Blend Robustness Audit",
        "",
        "Saved-artifact audit of capped trade ledgers and combined equity.",
        "",
        "## Full Window",
        "",
        "| Run | Venue | Return | MAR | Sharpe | Max DD | Daily Raw | Daily Effective | Overlay |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in periods:
        if row["period"] == "full":
            lines.append(
                f"| {row['run']} | {row['venue']} | {_fmt(row['combined_return'])} | {_fmt(row['combined_mar'])} | "
                f"{_fmt(row['combined_sharpe'])} | {_fmt(row['combined_max_drawdown'])} | "
                f"{_fmt(row['daily_raw_return'])} | {_fmt(row['daily_effective_return'])} | {_fmt(row['overlay_return'])} |"
            )
    lines += [
        "",
        "## Yearly",
        "",
        "| Run | Venue | Year | Combined | MAR | Sharpe | Daily Raw | Daily Effective | Overlay | Max DD |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in periods:
        if row["period"] != "full":
            lines.append(
                f"| {row['run']} | {row['venue']} | {row['period']} | {_fmt(row['combined_return'])} | "
                f"{_fmt(row['combined_mar'])} | {_fmt(row['combined_sharpe'])} | {_fmt(row['daily_raw_return'])} | "
                f"{_fmt(row['daily_effective_return'])} | {_fmt(row['overlay_return'])} | "
                f"{_fmt(row['combined_max_drawdown'])} |"
            )
    lines += [
        "",
        "## Source-Year Contribution",
        "",
        "| Run | Venue | Source | Year | Trades | Net Return | Notional Sum | Win Rate | Share Total |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sources:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['blend_source']} | {row['entry_year']} | {row['trades']} | "
            f"{_fmt(row['net_return'])} | {_fmt(row['notional_sum'])} | {_fmt(row['win_rate'])} | "
            f"{_fmt(row['share_total'])} |"
        )
    lines += [
        "",
        "## Source Concentration",
        "",
        "| Run | Venue | Source | Symbols | Top1 Total | Top5 Total | Top10 Total | Top1 Positive | Top5 Positive | Top10 Positive |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in concentration:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['blend_source']} | {row['symbols']} | "
            f"{_fmt(row['top1_share_source_total'])} | {_fmt(row['top5_share_source_total'])} | "
            f"{_fmt(row['top10_share_source_total'])} | {_fmt(row['top1_share_source_positive'])} | "
            f"{_fmt(row['top5_share_source_positive'])} | {_fmt(row['top10_share_source_positive'])} |"
        )
    lines += [
        "",
        "## Churn Diagnostics",
        "",
        "| Run | Venue | Source | Trades | Symbols | Active Days | Avg/Day | Max/Day | Max Symbol-Day | Median Same-Symbol Gap h | <24h Reentries | Net Return |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in churn:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['source']} | {row['trades']} | {row['symbols']} | "
            f"{row['active_trade_days']} | {_fmt(row['avg_trades_per_active_day'])} | {row['max_trades_per_day']} | "
            f"{row['max_trades_per_symbol_day']} | {_fmt(row['median_same_symbol_gap_hours'])} | "
            f"{row['reentries_under_24h']} | {_fmt(row['net_return_sum'])} |"
        )
    lines += [
        "",
        "## Add-on Same-Symbol Primary Overlap",
        "",
        "| Run | Venue | Add-on Trades | Active Same-Symbol Primary | Fraction | Mean Primary Unrealized | Worst Primary Unrealized |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in overlap:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['addon_trades']} | "
            f"{row['addon_same_symbol_active_primary_trades']} | "
            f"{_fmt(row['addon_same_symbol_active_primary_fraction'])} | "
            f"{_fmt(row['mean_active_primary_unrealized_at_addon_entry'])} | "
            f"{_fmt(row['min_active_primary_unrealized_at_addon_entry'])} |"
        )
    lines += [
        "",
        "## Add-on Primary-PnL Gate Sensitivity",
        "",
        "This is an ex-post diagnostic on the already capped add-on trades. It is not a full MTM rerun.",
        "",
        "| Run | Venue | Threshold | Kept | Skipped | Skipped % | Kept Add-on Net | Skipped Net | Skipped Return Share | Skipped Positive % |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in primary_pnl_gate:
        lines.append(
            f"| {row['run']} | {row['venue']} | {_fmt(row['threshold'])} | {row['kept_trades']} | "
            f"{row['skipped_trades']} | {_fmt(row['skipped_trade_fraction'])} | "
            f"{_fmt(row['kept_addon_net_return'])} | {_fmt(row['skipped_net_return'])} | "
            f"{_fmt(row['skipped_return_share'])} | {_fmt(row['skipped_positive_fraction'])} |"
        )
    lines += [
        "",
        "## Add-on Same-Symbol Cooldown Sensitivity",
        "",
        "| Run | Venue | Cooldown h | Kept | Skipped | Skipped % | Kept Net | Skipped Net | Skipped Return Share | Skipped Positive % |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in cooldown:
        lines.append(
            f"| {row['run']} | {row['venue']} | {row['cooldown_hours']} | {row['kept_trades']} | "
            f"{row['skipped_trades']} | {_fmt(row['skipped_trade_fraction'])} | "
            f"{_fmt(row['kept_net_return'])} | {_fmt(row['skipped_net_return'])} | "
            f"{_fmt(row['skipped_return_share'])} | {_fmt(row['skipped_positive_fraction'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True, help="Comma-separated label=trade_cap_root entries.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--venues", default=",".join(VENUES), help="Comma-separated venues to audit.")
    parser.add_argument(
        "--cooldown-hours",
        default="6,12,24,48",
        help="Comma-separated same-symbol add-on cooldowns to simulate.",
    )
    parser.add_argument(
        "--primary-pnl-thresholds",
        default="-0.10,-0.05,0.0",
        help="Comma-separated active same-symbol primary unrealized-return gates to diagnose.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runs = _parse_runs(args.runs)
    venues = tuple(v.strip() for v in args.venues.split(",") if v.strip())
    unknown = [venue for venue in venues if venue not in VENUES]
    if unknown:
        raise SystemExit(f"unknown venue(s): {', '.join(unknown)}")
    cooldown_hours = _parse_cooldown_hours(args.cooldown_hours)
    primary_pnl_thresholds = _parse_float_grid(args.primary_pnl_thresholds)
    out_root = Path(args.out).expanduser()
    period_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    concentration_rows: list[dict[str, Any]] = []
    churn_rows: list[dict[str, Any]] = []
    same_symbol_gap_rows: list[dict[str, Any]] = []
    overlap_rows: list[dict[str, Any]] = []
    primary_pnl_gate_rows: list[dict[str, Any]] = []
    cooldown_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for label, root in runs.items():
        for venue in venues:
            summary_rows.append({"run": label, **_run_summary(root, venue)})
            period_rows.extend(_period_rows(label, venue, root))
            src, sym, conc = _trade_rows(label, venue, root)
            source_rows.extend(src)
            symbol_rows.extend(sym)
            concentration_rows.extend(conc)
            blended = _load_blended_trades(root, venue)
            churn_rows.extend(_churn_summary_rows(label, venue, blended))
            same_symbol_gap_rows.extend(_same_symbol_gap_rows(label, venue, blended))
            overlap_rows.append(_addon_primary_overlap_summary(label, venue, blended))
            primary_pnl_gate_rows.extend(_primary_pnl_gate_sensitivity_rows(label, venue, blended, primary_pnl_thresholds))
            cooldown_rows.extend(_cooldown_sensitivity_rows(label, venue, blended, cooldown_hours))
    _write_csv(out_root / "summary.csv", summary_rows)
    _write_csv(out_root / "period_metrics.csv", period_rows)
    _write_csv(out_root / "source_year_contribution.csv", source_rows)
    _write_csv(out_root / "symbol_concentration.csv", symbol_rows)
    _write_csv(out_root / "concentration_summary.csv", concentration_rows)
    _write_csv(out_root / "churn_summary.csv", churn_rows)
    _write_csv(out_root / "same_symbol_gaps.csv", same_symbol_gap_rows)
    _write_csv(out_root / "addon_primary_overlap.csv", overlap_rows)
    _write_csv(out_root / "primary_pnl_gate_sensitivity.csv", primary_pnl_gate_rows)
    _write_csv(out_root / "cooldown_sensitivity.csv", cooldown_rows)
    (out_root / "trade_cap_audit.md").write_text(
        _markdown(
            period_rows,
            source_rows,
            concentration_rows,
            churn_rows,
            overlap_rows,
            primary_pnl_gate_rows,
            cooldown_rows,
        ),
        encoding="utf-8",
    )
    print(f"summary={out_root / 'summary.csv'}")
    print(f"period_metrics={out_root / 'period_metrics.csv'}")
    print(f"source_year_contribution={out_root / 'source_year_contribution.csv'}")
    print(f"symbol_concentration={out_root / 'symbol_concentration.csv'}")
    print(f"concentration_summary={out_root / 'concentration_summary.csv'}")
    print(f"churn_summary={out_root / 'churn_summary.csv'}")
    print(f"same_symbol_gaps={out_root / 'same_symbol_gaps.csv'}")
    print(f"addon_primary_overlap={out_root / 'addon_primary_overlap.csv'}")
    print(f"primary_pnl_gate_sensitivity={out_root / 'primary_pnl_gate_sensitivity.csv'}")
    print(f"cooldown_sensitivity={out_root / 'cooldown_sensitivity.csv'}")
    print(f"markdown={out_root / 'trade_cap_audit.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
