#!/usr/bin/env python3
"""Blend saved daily+overlay component artifacts.

This is an audit runner over existing combined_equity.csv files. It does not
rebuild PIT signals. The blend is daily-return based, so it is useful for sizing
and portfolio interaction checks, not for final execution accounting.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

import polars as pl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.continuous_events import _daily_pnl_metrics  # noqa: E402


VENUES = ("bybit", "binance")


def _parse_labeled_paths(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"path spec must be label=path; got {item!r}")
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


def _component_frame(root: Path, venue: str, prefix: str) -> pl.DataFrame:
    path = root / venue / "combined_equity.csv"
    df = pl.read_csv(path).with_columns(pl.col("ts_ms").cast(pl.Int64))
    daily_effective_col = "daily_return_effective" if "daily_return_effective" in df.columns else "daily_return"
    overlay_col = "scaled_overlay_return" if "scaled_overlay_return" in df.columns else "overlay_return"
    return df.select(
        "ts_ms",
        pl.col("daily_return").alias(f"{prefix}_daily_raw"),
        pl.col(daily_effective_col).alias(f"{prefix}_daily_effective"),
        pl.col(overlay_col).alias(f"{prefix}_overlay"),
        pl.col("basket_return").alias(f"{prefix}_basket"),
    )


def _equity(rows: pl.DataFrame, ret_col: str = "basket_return") -> pl.DataFrame:
    if rows.is_empty():
        return rows
    return (
        rows.sort("ts_ms")
        .with_columns(pl.col(ret_col).alias("basket_return"))
        .with_columns((1.0 + pl.col("basket_return")).cum_prod().alias("equity"))
        .with_columns((pl.col("equity") / pl.col("equity").cum_max() - 1.0).alias("drawdown"))
    )


def _metrics(rows: pl.DataFrame, ret_col: str = "basket_return") -> dict[str, Any]:
    equity = _equity(rows, ret_col)
    if equity.is_empty():
        return {"total_return": None, "mar": None, "max_drawdown": None, "sharpe": None}
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


def _daily_baseline(primary: pl.DataFrame) -> pl.DataFrame:
    return primary.select("ts_ms", pl.col("primary_daily_raw").alias("basket_return"))


def _blend_one(
    *,
    primary_root: Path,
    addon_root: Path,
    venue: str,
    overlap_scale: float,
    overlay_return_cap: float | None,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    primary = _component_frame(primary_root, venue, "primary")
    addon = _component_frame(addon_root, venue, "addon")
    days = sorted(set(primary["ts_ms"].to_list()) | set(addon["ts_ms"].to_list()))
    joined = (
        pl.DataFrame({"ts_ms": days})
        .join(primary, on="ts_ms", how="left")
        .join(addon, on="ts_ms", how="left")
        .fill_null(0.0)
        .sort("ts_ms")
    )
    joined = joined.with_columns(
        pl.from_epoch(pl.col("ts_ms"), "ms").dt.date().cast(pl.Utf8).alias("date"),
        (pl.col("primary_overlay") != 0.0).alias("primary_active"),
        (pl.col("addon_overlay") != 0.0).alias("addon_active"),
    ).with_columns(
        pl.when(pl.col("primary_active") & pl.col("addon_active"))
        .then(pl.col("addon_overlay") * float(overlap_scale))
        .otherwise(pl.col("addon_overlay"))
        .alias("addon_overlay_effective")
    )
    raw_overlay = pl.col("primary_overlay") + pl.col("addon_overlay_effective")
    if overlay_return_cap is None or overlay_return_cap <= 0.0:
        overlay_expr = raw_overlay
    else:
        cap = float(overlay_return_cap)
        overlay_expr = raw_overlay.clip(-cap, cap)
    joined = joined.with_columns(
        raw_overlay.alias("raw_total_overlay"),
        overlay_expr.alias("total_overlay"),
    ).with_columns(
        (pl.col("primary_daily_effective") + pl.col("total_overlay")).alias("basket_return"),
        (pl.col("raw_total_overlay") - pl.col("total_overlay")).alias("overlay_cap_delta"),
    )
    blended = _equity(joined)
    daily_m = _metrics(_daily_baseline(joined))
    primary_m = _metrics(joined.with_columns(pl.col("primary_basket").alias("basket_return")))
    addon_m = _metrics(joined.with_columns(pl.col("addon_basket").alias("basket_return")))
    blend_m = _metrics(joined)
    daily_dd = daily_m.get("max_drawdown")
    blend_dd = blend_m.get("max_drawdown")
    primary_mar = primary_m.get("mar")
    blend_mar = blend_m.get("mar")
    primary_total = primary_m.get("total_return")
    blend_total = blend_m.get("total_return")
    row = {
        "venue": venue,
        "overlap_scale": overlap_scale,
        "overlay_return_cap": overlay_return_cap,
        "primary_active_days": int(joined.select(pl.col("primary_active").sum()).item()),
        "addon_active_days": int(joined.select(pl.col("addon_active").sum()).item()),
        "overlap_days": int(joined.select((pl.col("primary_active") & pl.col("addon_active")).sum()).item()),
        "cap_days": int(joined.select((pl.col("overlay_cap_delta") != 0.0).sum()).item()),
        "daily_total_return": daily_m.get("total_return"),
        "daily_mar": daily_m.get("mar"),
        "daily_max_drawdown": daily_dd,
        "primary_total_return": primary_total,
        "primary_mar": primary_mar,
        "primary_max_drawdown": primary_m.get("max_drawdown"),
        "addon_total_return": addon_m.get("total_return"),
        "addon_mar": addon_m.get("mar"),
        "addon_max_drawdown": addon_m.get("max_drawdown"),
        "blend_total_return": blend_total,
        "blend_mar": blend_mar,
        "blend_sharpe": blend_m.get("sharpe"),
        "blend_max_drawdown": blend_dd,
        "blend_delta_return_vs_primary": (
            float(blend_total) - float(primary_total) if blend_total is not None and primary_total is not None else None
        ),
        "blend_delta_mar_vs_primary": (
            float(blend_mar) - float(primary_mar) if blend_mar is not None and primary_mar is not None else None
        ),
        "blend_dd_ratio_vs_daily": (
            abs(float(blend_dd)) / abs(float(daily_dd))
            if blend_dd is not None and daily_dd is not None and abs(float(daily_dd)) > 1e-12
            else None
        ),
    }
    row["beats_primary_return"] = (
        row["blend_delta_return_vs_primary"] is not None and float(row["blend_delta_return_vs_primary"]) > 0.0
    )
    row["beats_primary_mar"] = (
        row["blend_delta_mar_vs_primary"] is not None and float(row["blend_delta_mar_vs_primary"]) > 0.0
    )
    row["daily_dd_gate"] = row["blend_dd_ratio_vs_daily"] is not None and float(row["blend_dd_ratio_vs_daily"]) <= 1.10
    row["blend_pass"] = bool(row["beats_primary_return"] and row["beats_primary_mar"] and row["daily_dd_gate"])
    return blended, row


def _markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Saved Overlay Blend Audit",
        "",
        "Daily-return component blend from saved combined artifacts.",
        "",
        "| Addon | Venue | Overlap Scale | Cap | Overlap Days | Cap Days | Return | MAR | Sharpe | DD Ratio | Delta MAR | Pass |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['addon_label']} | {row['venue']} | {_fmt(row['overlap_scale'])} | "
            f"{_fmt(row.get('overlay_return_cap'))} | {row['overlap_days']} | {row['cap_days']} | "
            f"{_fmt(row.get('blend_total_return'))} | {_fmt(row.get('blend_mar'))} | "
            f"{_fmt(row.get('blend_sharpe'))} | {_fmt(row.get('blend_dd_ratio_vs_daily'))} | "
            f"{_fmt(row.get('blend_delta_mar_vs_primary'))} | {row.get('blend_pass')} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-root", required=True)
    parser.add_argument("--addon-roots", required=True, help="Comma-separated label=combined_root entries.")
    parser.add_argument("--overlap-scales", default="0,0.25,0.5,1.0")
    parser.add_argument("--overlay-return-caps", default="none")
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    primary_root = Path(args.primary_root).expanduser()
    addon_roots = _parse_labeled_paths(args.addon_roots)
    overlap_scales = [float(x.strip()) for x in args.overlap_scales.split(",") if x.strip()]
    caps = [
        None if x.strip().lower() in {"none", "off", "0"} else float(x.strip())
        for x in args.overlay_return_caps.split(",")
        if x.strip()
    ]
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for addon_label, addon_root in addon_roots.items():
        for overlap_scale in overlap_scales:
            for cap in caps:
                for venue in VENUES:
                    blended, row = _blend_one(
                        primary_root=primary_root,
                        addon_root=addon_root,
                        venue=venue,
                        overlap_scale=overlap_scale,
                        overlay_return_cap=cap,
                    )
                    row = {"addon_label": addon_label, **row}
                    rows.append(row)
                    tag = f"{addon_label}_overlap{overlap_scale:g}_cap{cap if cap is not None else 'none'}"
                    venue_dir = out_root / tag / venue
                    venue_dir.mkdir(parents=True, exist_ok=True)
                    blended.write_csv(venue_dir / "combined_equity.csv")
                    (venue_dir / "report.json").write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
    _write_csv(out_root / "summary.csv", rows)
    (out_root / "blend_audit.md").write_text(_markdown(rows), encoding="utf-8")
    print(f"summary={out_root / 'summary.csv'}")
    print(f"markdown={out_root / 'blend_audit.md'}")
    for row in rows:
        print(
            f"[{row['addon_label']} {row['venue']} overlap={row['overlap_scale']}] "
            f"blend MAR={row['blend_mar']} deltaMAR={row['blend_delta_mar_vs_primary']} "
            f"ddRatio={row['blend_dd_ratio_vs_daily']} pass={row['blend_pass']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
