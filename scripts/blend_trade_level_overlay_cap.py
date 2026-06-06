#!/usr/bin/env python3
"""Trade-level blend of primary and add-on overlay ledgers with an ex-ante cap.

The primary overlay is accepted first. Add-on trades are scaled down at entry if
the active primary+accepted-add-on notional would exceed the configured cap.
Returns are then rebuilt from the capped trade ledger via the normal MTM engine.
"""
from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

import polars as pl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.continuous_events import _daily_pnl_metrics, _portfolio_mtm_equity  # noqa: E402
from scripts.combine_daily_continuous_overlay import _load_report  # noqa: E402
from scripts.continuous_causal_rmom_vs_daily import ROOTS, VenueContext  # noqa: E402


VENUES = ("bybit", "binance")
DEFAULT_CELL = "q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop25_imp50_cap1m"


def _parse_venue_floats(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    out: dict[str, float] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"expected venue=value, got {item!r}")
        venue, value = item.split("=", 1)
        venue = venue.strip()
        if venue not in VENUES:
            raise ValueError(f"unknown venue {venue!r}")
        out[venue] = float(value)
    return out


def _parse_float_list(raw: str) -> tuple[float, ...]:
    values = tuple(float(x.strip()) for x in raw.split(",") if x.strip())
    if not values:
        raise ValueError("expected at least one numeric value")
    return values


def _parse_venues(raw: str) -> tuple[str, ...]:
    venues = tuple(x.strip() for x in raw.split(",") if x.strip())
    unknown = [venue for venue in venues if venue not in VENUES]
    if unknown:
        raise ValueError(f"unknown venue(s): {', '.join(unknown)}")
    return venues or VENUES


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


def _daily_component(primary_root: Path, venue: str) -> pl.DataFrame:
    return (
        pl.read_csv(primary_root / venue / "combined_equity.csv")
        .with_columns(pl.col("ts_ms").cast(pl.Int64))
        .select(
            "ts_ms",
            "date",
            pl.col("daily_return").alias("daily_return_raw"),
            pl.col("daily_return_effective").alias("daily_return_effective"),
        )
    )


def _scale_trade_frame(trades: pl.DataFrame, scale: float, *, source: str, cost_multiplier: float) -> pl.DataFrame:
    mult_cols = [c for c in ("notional_weight", "gross_return", "cost_return", "funding_return", "net_return") if c in trades.columns]
    out = trades.with_columns(pl.lit(source).alias("blend_source"), pl.lit(float(scale)).alias("blend_initial_scale"))
    if abs(scale - 1.0) > 1e-12 and mult_cols:
        out = out.with_columns([(pl.col(c) * float(scale)).alias(c) for c in mult_cols])
    if cost_multiplier != 1.0 and "cost_return" in out.columns:
        # MTM books cost_return on entry. Keep net_return aligned for additive audit/fallback paths.
        extra_cost = pl.col("cost_return") * (float(cost_multiplier) - 1.0)
        out = out.with_columns(
            (pl.col("net_return") + extra_cost).alias("net_return"),
            (pl.col("cost_return") * float(cost_multiplier)).alias("cost_return"),
        )
    return out


def _load_primary_trades(primary_root: Path, venue: str, *, cost_multiplier: float) -> pl.DataFrame:
    path = primary_root / venue / "filtered_overlay_trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return _scale_trade_frame(pl.read_csv(path), 1.0, source="primary", cost_multiplier=cost_multiplier)


def _load_addon_trades(
    *,
    addon_execution_root: Path,
    venue: str,
    cell_id: str,
    venue_scale: float,
    cost_multiplier: float,
) -> pl.DataFrame:
    path = addon_execution_root / venue / "cells" / cell_id / "short_only" / "trades.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return _scale_trade_frame(
        pl.read_csv(path),
        venue_scale,
        source="addon",
        cost_multiplier=cost_multiplier,
    )


def _apply_fraction(row: dict[str, Any], fraction: float) -> dict[str, Any]:
    out = dict(row)
    for col in ("notional_weight", "gross_return", "cost_return", "funding_return", "net_return"):
        if col in out and out[col] is not None:
            out[col] = float(out[col]) * fraction
    out["blend_accept_fraction"] = fraction
    return out


def _active_same_symbol_primary_unrealized(row: dict[str, Any], active: list[tuple]) -> list[float]:
    symbol = str(row.get("symbol") or "")
    current_price = float(row.get("entry_price") or 0.0)
    if not symbol or current_price <= 0.0:
        return []
    out: list[float] = []
    for item in active:
        _exit_ts, _weight, active_symbol, _net, source, side, entry_price = item
        if active_symbol != symbol or source != "primary" or entry_price <= 0.0:
            continue
        if side == "short":
            out.append(float(entry_price) / current_price - 1.0)
        else:
            out.append(current_price / float(entry_price) - 1.0)
    return out


def _cap_addon_trades(
    primary: pl.DataFrame,
    addon: pl.DataFrame,
    *,
    max_active_notional: float,
    addon_rolling_window_hours: int = 0,
    addon_rolling_notional_cap: float = 0.0,
    addon_symbol_loss_pause_hours: int = 0,
    addon_symbol_loss_threshold: float = 0.0,
    block_addon_same_symbol_active: bool = False,
    addon_same_symbol_primary_min_unrealized: float | None = None,
    max_active_symbol_notional: float = 0.0,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    active: list[tuple[int, float, str, float, str, str, float]] = []
    rolling_addon: deque[tuple[int, float]] = deque()
    rolling_addon_sum = 0.0
    rolling_window_ms = int(addon_rolling_window_hours) * MS_PER_HOUR
    rolling_on = rolling_window_ms > 0 and addon_rolling_notional_cap > 0.0
    symbol_loss_pause_ms = int(addon_symbol_loss_pause_hours) * MS_PER_HOUR
    symbol_loss_pause_on = symbol_loss_pause_ms > 0
    symbol_losses: dict[str, deque[int]] = {}
    addon_full = addon_partial = addon_zero = 0
    addon_symbol_loss_skips = 0
    addon_same_symbol_active_skips = 0
    addon_primary_pnl_gate_skips = 0
    addon_active_cap_hits = addon_symbol_cap_hits = addon_rolling_cap_hits = 0
    addon_requested = float(addon["notional_weight"].sum()) if not addon.is_empty() else 0.0
    addon_accepted = 0.0
    combined = pl.concat([primary, addon], how="diagonal_relaxed").sort(
        ["entry_ts_ms", "blend_source"], descending=[False, True]
    )
    # Descending source puts "primary" before "addon" lexicographically for equal timestamps.
    for row in combined.iter_rows(named=True):
        entry_ts = int(row["entry_ts_ms"])
        while active and active[0][0] <= entry_ts:
            exit_ts, _closed_weight, closed_symbol, closed_net, _source, _side, _entry_price = heapq.heappop(active)
            if symbol_loss_pause_on and closed_net < float(addon_symbol_loss_threshold):
                symbol_losses.setdefault(closed_symbol, deque()).append(exit_ts)
        if rolling_on:
            rolling_floor = entry_ts - rolling_window_ms
            while rolling_addon and rolling_addon[0][0] <= rolling_floor:
                _, old_weight = rolling_addon.popleft()
                rolling_addon_sum -= old_weight
        active_notional = sum(weight for _, weight, _, _, _, _, _ in active)
        weight = float(row.get("notional_weight") or 0.0)
        source = str(row.get("blend_source") or "")
        symbol = str(row.get("symbol") or "")
        if symbol_loss_pause_on and symbol:
            losses = symbol_losses.get(symbol)
            if losses is not None:
                loss_floor = entry_ts - symbol_loss_pause_ms
                while losses and losses[0] < loss_floor:
                    losses.popleft()
        if source == "primary":
            accepted = _apply_fraction(row, 1.0)
            rows.append(accepted)
            if weight > 0:
                heapq.heappush(
                    active,
                    (
                        int(row["exit_ts_ms"]),
                        weight,
                        symbol,
                        float(row.get("net_return") or 0.0),
                        source,
                        str(row.get("side") or ""),
                        float(row.get("entry_price") or 0.0),
                    ),
                )
            continue
        if symbol_loss_pause_on and symbol and symbol_losses.get(symbol):
            addon_symbol_loss_skips += 1
            addon_zero += 1
            continue
        if block_addon_same_symbol_active and symbol and any(active_symbol == symbol for _, _, active_symbol, _, _, _, _ in active):
            addon_same_symbol_active_skips += 1
            addon_zero += 1
            continue
        if addon_same_symbol_primary_min_unrealized is not None:
            unrealized = _active_same_symbol_primary_unrealized(row, active)
            if unrealized and min(unrealized) < float(addon_same_symbol_primary_min_unrealized):
                addon_primary_pnl_gate_skips += 1
                addon_zero += 1
                continue
        available = max(float(max_active_notional) - active_notional, 0.0)
        active_fraction = min(1.0, available / weight) if weight > 1e-12 else 0.0
        symbol_fraction = 1.0
        if max_active_symbol_notional > 0.0 and symbol and weight > 1e-12:
            active_symbol_notional = sum(
                active_weight
                for _, active_weight, active_symbol, _, _, _, _ in active
                if active_symbol == symbol
            )
            symbol_available = max(float(max_active_symbol_notional) - active_symbol_notional, 0.0)
            symbol_fraction = min(1.0, symbol_available / weight)
        rolling_fraction = 1.0
        if rolling_on and weight > 1e-12:
            rolling_available = max(float(addon_rolling_notional_cap) - rolling_addon_sum, 0.0)
            rolling_fraction = min(1.0, rolling_available / weight)
        if active_fraction < 0.999999:
            addon_active_cap_hits += 1
        if symbol_fraction < 0.999999:
            addon_symbol_cap_hits += 1
        if rolling_fraction < 0.999999:
            addon_rolling_cap_hits += 1
        fraction = min(active_fraction, symbol_fraction, rolling_fraction)
        if fraction >= 0.999999:
            addon_full += 1
            fraction = 1.0
        elif fraction > 1e-9:
            addon_partial += 1
        else:
            addon_zero += 1
            fraction = 0.0
        if fraction <= 0.0:
            continue
        accepted = _apply_fraction(row, fraction)
        rows.append(accepted)
        accepted_weight = weight * fraction
        addon_accepted += accepted_weight
        if accepted_weight > 0:
            heapq.heappush(
                active,
                (
                    int(row["exit_ts_ms"]),
                    accepted_weight,
                    symbol,
                    float(accepted.get("net_return") or 0.0),
                    source,
                    str(row.get("side") or ""),
                    float(row.get("entry_price") or 0.0),
                ),
            )
            if rolling_on:
                rolling_addon.append((entry_ts, accepted_weight))
                rolling_addon_sum += accepted_weight
    out = pl.DataFrame(rows) if rows else primary.clear()
    stats = {
        "addon_rolling_window_hours": int(addon_rolling_window_hours),
        "addon_rolling_notional_cap": float(addon_rolling_notional_cap),
        "addon_symbol_loss_pause_hours": int(addon_symbol_loss_pause_hours),
        "addon_symbol_loss_threshold": float(addon_symbol_loss_threshold),
        "block_addon_same_symbol_active": bool(block_addon_same_symbol_active),
        "addon_same_symbol_primary_min_unrealized": addon_same_symbol_primary_min_unrealized,
        "max_active_symbol_notional": float(max_active_symbol_notional),
        "addon_requested_notional_sum": addon_requested,
        "addon_accepted_notional_sum": addon_accepted,
        "addon_acceptance_ratio": addon_accepted / addon_requested if addon_requested > 1e-12 else None,
        "addon_full_trades": addon_full,
        "addon_partial_trades": addon_partial,
        "addon_zero_trades": addon_zero,
        "addon_symbol_loss_skips": addon_symbol_loss_skips,
        "addon_same_symbol_active_skips": addon_same_symbol_active_skips,
        "addon_primary_pnl_gate_skips": addon_primary_pnl_gate_skips,
        "addon_active_cap_hits": addon_active_cap_hits,
        "addon_symbol_cap_hits": addon_symbol_cap_hits,
        "addon_rolling_cap_hits": addon_rolling_cap_hits,
        "accepted_trades": out.height,
        "accepted_addon_trades": addon_full + addon_partial,
        "primary_trades": primary.height,
    }
    return out, stats


def _returns_from_mtm(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    mtm = _portfolio_mtm_equity(trades, klines)
    return (
        mtm.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).cast(pl.Int64).alias("day_ts"))
        .group_by("day_ts")
        .agg(pl.col("basket_return").sum().alias("overlay_return"))
        .sort("day_ts")
    )


def _compound(joined: pl.DataFrame) -> pl.DataFrame:
    return (
        joined.with_columns((pl.col("daily_return_effective") + pl.col("overlay_return")).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )


def _metrics_from_returns(rows: pl.DataFrame, ret_col: str) -> dict[str, Any]:
    eq = (
        rows.sort("ts_ms")
        .with_columns(pl.col(ret_col).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )
    m = _daily_pnl_metrics(eq)
    returns = eq["basket_return"].drop_nulls()
    sharpe = None
    if len(returns) > 1 and float(returns.std()) > 0.0:
        sharpe = float(returns.mean()) / float(returns.std()) * math.sqrt(365)
    return {**m, "sharpe": sharpe}


def _blend_venue(
    *,
    primary_root: Path,
    addon_execution_root: Path,
    venue: str,
    cell_id: str,
    venue_scale: float,
    max_active_notional: float,
    cost_multiplier: float,
    addon_rolling_window_hours: int,
    addon_rolling_notional_cap: float,
    addon_symbol_loss_pause_hours: int,
    addon_symbol_loss_threshold: float,
    block_addon_same_symbol_active: bool,
    addon_same_symbol_primary_min_unrealized: float | None,
    max_active_symbol_notional: float,
    klines: pl.DataFrame,
    out_root: Path,
) -> dict[str, Any]:
    primary_report = _load_report(primary_root / venue / "report.json")
    primary = _load_primary_trades(primary_root, venue, cost_multiplier=cost_multiplier)
    addon = _load_addon_trades(
        addon_execution_root=addon_execution_root,
        venue=venue,
        cell_id=cell_id,
        venue_scale=venue_scale,
        cost_multiplier=cost_multiplier,
    )
    capped, cap_stats = _cap_addon_trades(
        primary,
        addon,
        max_active_notional=max_active_notional,
        addon_rolling_window_hours=addon_rolling_window_hours,
        addon_rolling_notional_cap=addon_rolling_notional_cap,
        addon_symbol_loss_pause_hours=addon_symbol_loss_pause_hours,
        addon_symbol_loss_threshold=addon_symbol_loss_threshold,
        block_addon_same_symbol_active=block_addon_same_symbol_active,
        addon_same_symbol_primary_min_unrealized=addon_same_symbol_primary_min_unrealized,
        max_active_symbol_notional=max_active_symbol_notional,
    )
    overlay = _returns_from_mtm(capped, klines)
    daily = _daily_component(primary_root, venue).rename({"ts_ms": "day_ts"})
    days = sorted(set(daily["day_ts"].to_list()) | set(overlay["day_ts"].to_list()))
    joined = (
        pl.DataFrame({"day_ts": days})
        .join(daily, on="day_ts", how="left")
        .join(overlay, on="day_ts", how="left")
        .fill_null(0.0)
        .sort("day_ts")
        .rename({"day_ts": "ts_ms"})
        .with_columns(pl.from_epoch(pl.col("ts_ms"), "ms").dt.date().cast(pl.Utf8).alias("date"))
    )
    combined = _compound(joined)
    daily_m = _metrics_from_returns(joined, "daily_return_raw")
    blend_m = _daily_pnl_metrics(combined)
    daily_dd = daily_m.get("max_drawdown")
    blend_dd = blend_m.get("max_drawdown")
    daily_mar = daily_m.get("mar")
    primary_mar = primary_report.get("combined_mar")
    primary_total = primary_report.get("combined_total_return")
    row = {
        "venue": venue,
        "venue_scale": venue_scale,
        "max_active_notional": max_active_notional,
        "max_active_symbol_notional": max_active_symbol_notional,
        "cost_multiplier": cost_multiplier,
        **cap_stats,
        "daily_total_return": daily_m.get("total_return"),
        "daily_mar": daily_mar,
        "daily_max_drawdown": daily_dd,
        "primary_total_return": primary_total,
        "primary_mar": primary_mar,
        "primary_max_drawdown": primary_report.get("combined_max_drawdown"),
        "blend_total_return": blend_m.get("total_return"),
        "blend_mar": blend_m.get("mar"),
        "blend_max_drawdown": blend_dd,
        "blend_sharpe": blend_m.get("sharpe_like"),
        "blend_delta_total_return": (
            float(blend_m["total_return"]) - float(primary_total)
            if blend_m.get("total_return") is not None and primary_total is not None
            else None
        ),
        "blend_delta_mar": (
            float(blend_m["mar"]) - float(primary_mar)
            if blend_m.get("mar") is not None and primary_mar is not None
            else None
        ),
        "blend_dd_ratio_vs_daily": (
            abs(float(blend_dd)) / abs(float(daily_dd))
            if blend_dd is not None and daily_dd is not None and abs(float(daily_dd)) > 1e-12
            else None
        ),
    }
    row["blend_pass"] = bool(
        row["blend_delta_total_return"] is not None
        and row["blend_delta_mar"] is not None
        and float(row["blend_delta_total_return"]) > 0.0
        and float(row["blend_delta_mar"]) > 0.0
        and row["blend_dd_ratio_vs_daily"] is not None
        and float(row["blend_dd_ratio_vs_daily"]) <= 1.10
    )
    cap_label = f"cap{max_active_notional:g}"
    if max_active_symbol_notional > 0.0:
        cap_label = f"{cap_label}_symcap{max_active_symbol_notional:g}"
    out_dir = out_root / f"{cap_label}_cost{cost_multiplier:g}" / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    capped.write_csv(out_dir / "blended_trades.csv")
    combined.write_csv(out_dir / "combined_equity.csv")
    joined.write_csv(out_dir / "joined_returns.csv")
    (out_dir / "report.json").write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
    return row


def _markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Trade-Level Overlay Blend Cap",
        "",
        "Primary trades are accepted first; add-on trades are capped at entry by active notional.",
        "",
        "| Venue | Cost | Cap | Symbol Cap | Addon Accepted | Accept Ratio | Return | MAR | DD Ratio | Delta MAR | Pass |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['venue']} | {_fmt(row['cost_multiplier'])} | {_fmt(row['max_active_notional'])} | "
            f"{_fmt(row.get('max_active_symbol_notional'))} | "
            f"{row['accepted_addon_trades']} | {_fmt(row.get('addon_acceptance_ratio'))} | "
            f"{_fmt(row.get('blend_total_return'))} | {_fmt(row.get('blend_mar'))} | "
            f"{_fmt(row.get('blend_dd_ratio_vs_daily'))} | {_fmt(row.get('blend_delta_mar'))} | "
            f"{row.get('blend_pass')} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-root", required=True)
    parser.add_argument("--addon-execution-root", required=True)
    parser.add_argument("--cell-id", default=DEFAULT_CELL)
    parser.add_argument("--venue-scales", required=True)
    parser.add_argument("--venues", default=",".join(VENUES), help="Comma-separated venues to process.")
    parser.add_argument("--max-active-notionals", default="0.08,0.10,0.12,0.15")
    parser.add_argument(
        "--max-active-symbol-notionals",
        default="0.0",
        help="Comma-separated per-symbol active overlay caps; 0 disables.",
    )
    parser.add_argument("--venue-max-active-notionals", default=None, help="Optional venue=cap map; overrides the shared cap grid.")
    parser.add_argument(
        "--venue-max-active-symbol-notionals",
        default=None,
        help="Optional venue=cap map for per-symbol active overlay caps.",
    )
    parser.add_argument("--addon-rolling-window-hours", type=int, default=0)
    parser.add_argument("--addon-rolling-notional-cap", type=float, default=0.0)
    parser.add_argument(
        "--venue-addon-rolling-notional-caps",
        default=None,
        help="Optional venue=cap map for rolling accepted add-on notional; overrides the shared rolling cap.",
    )
    parser.add_argument("--addon-symbol-loss-pause-hours", type=int, default=0)
    parser.add_argument("--addon-symbol-loss-threshold", type=float, default=0.0)
    parser.add_argument("--block-addon-same-symbol-active", action="store_true")
    parser.add_argument(
        "--addon-same-symbol-primary-min-unrealized",
        type=float,
        default=None,
        help="Skip add-ons when an active same-symbol primary's unrealized gross return is below this threshold.",
    )
    parser.add_argument(
        "--venue-addon-same-symbol-primary-min-unrealized",
        default=None,
        help="Optional venue=threshold map; overrides the shared active-primary unrealized PnL gate.",
    )
    parser.add_argument("--cost-multiplier", type=float, default=1.0)
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    primary_root = Path(args.primary_root).expanduser()
    addon_execution_root = Path(args.addon_execution_root).expanduser()
    venue_scales = _parse_venue_floats(args.venue_scales)
    venues = _parse_venues(args.venues)
    venue_caps = _parse_venue_floats(args.venue_max_active_notionals)
    venue_symbol_caps = _parse_venue_floats(args.venue_max_active_symbol_notionals)
    venue_rolling_caps = _parse_venue_floats(args.venue_addon_rolling_notional_caps)
    venue_primary_pnl_gates = _parse_venue_floats(args.venue_addon_same_symbol_primary_min_unrealized)
    caps = list(_parse_float_list(args.max_active_notionals))
    symbol_caps = list(_parse_float_list(args.max_active_symbol_notionals))
    out_root = Path(args.out).expanduser()
    rows: list[dict[str, Any]] = []
    missing_scales = [venue for venue in venues if venue not in venue_scales]
    if missing_scales:
        raise ValueError(f"missing venue scale(s): {', '.join(missing_scales)}")
    klines_by_venue = {
        venue: VenueContext(ROOTS[venue], start="2023-04-01", end="2026-05-28", max_pad_hours=72).klines
        for venue in venues
    }
    if venue_caps:
        cap_venue_pairs = [
            (venue_caps[venue], venue, venue_symbol_caps.get(venue, symbol_cap))
            for venue in venues
            if venue in venue_caps
            for symbol_cap in symbol_caps
        ]
        missing_caps = [venue for venue in venues if venue not in venue_caps]
        if missing_caps:
            raise ValueError(f"missing venue cap(s): {', '.join(missing_caps)}")
    else:
        cap_venue_pairs = [
            (cap, venue, venue_symbol_caps.get(venue, symbol_cap))
            for cap in caps
            for venue in venues
            for symbol_cap in symbol_caps
        ]
    for cap, venue, symbol_cap in cap_venue_pairs:
        rolling_cap = venue_rolling_caps.get(venue, float(args.addon_rolling_notional_cap))
        primary_pnl_gate = venue_primary_pnl_gates.get(venue, args.addon_same_symbol_primary_min_unrealized)
        row = _blend_venue(
            primary_root=primary_root,
            addon_execution_root=addon_execution_root,
            venue=venue,
            cell_id=args.cell_id,
            venue_scale=venue_scales[venue],
            max_active_notional=cap,
            cost_multiplier=args.cost_multiplier,
            addon_rolling_window_hours=args.addon_rolling_window_hours,
            addon_rolling_notional_cap=rolling_cap,
            addon_symbol_loss_pause_hours=args.addon_symbol_loss_pause_hours,
            addon_symbol_loss_threshold=args.addon_symbol_loss_threshold,
            block_addon_same_symbol_active=args.block_addon_same_symbol_active,
            addon_same_symbol_primary_min_unrealized=primary_pnl_gate,
            max_active_symbol_notional=symbol_cap,
            klines=klines_by_venue[venue],
            out_root=out_root,
        )
        rows.append(row)
        print(
            f"[{venue}] cap={cap:g} cost={args.cost_multiplier:g} "
            f"symbolCap={symbol_cap:g} "
            f"ret={row['blend_total_return']} MAR={row['blend_mar']} "
            f"ddRatio={row['blend_dd_ratio_vs_daily']} addon={row['accepted_addon_trades']} "
            f"pass={row['blend_pass']}",
            flush=True,
        )
    _write_csv(out_root / "summary.csv", rows)
    (out_root / "trade_level_blend_cap.md").write_text(_markdown(rows), encoding="utf-8")
    print(f"summary={out_root / 'summary.csv'}")
    print(f"markdown={out_root / 'trade_level_blend_cap.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
