"""Shared chart writers consumed by the long/continuous report paths."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl



# Splits live exclusively on VolumeEventResearchConfig.splits (default ()).
# The default therefore has no internal validation split. OOS status depends on
# exposure history, not on whether data is historical or forward
# (docs/governance.md).

from ._common import _float_or_nan, _parse_day
from .trade_lifecycle import _has_columns




def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "strategy_return": pl.Series([], dtype=pl.Float64),
                "long_return": pl.Series([], dtype=pl.Float64),
                "short_return": pl.Series([], dtype=pl.Float64),
                "cost_return": pl.Series([], dtype=pl.Float64),
                "funding_return": pl.Series([], dtype=pl.Float64),
                "baskets": pl.Series([], dtype=pl.Int64),
                "trades": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("short_return").sum().alias("short_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.len().alias("baskets"),
                pl.col("trades").sum().alias("trades"),
            ]
        )
        .sort("month")
    )

def _write_equity_benchmark_chart(
    output_dir: Path,
    *,
    equity: pl.DataFrame,
    raw_klines: pl.DataFrame,
    monthly: pl.DataFrame | None = None,
    png_name: str = "volume_event_best_equity_btc.png",
    title: str | None = None,
    subtitle: str | None = None,
    step: bool = True,
    strategy_name: str = "Strategy",
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the strategy-vs-BTC equity PNG. ``png_name`` lets active sleeves
    (e.g. ``long_native``, ``continuous``) reuse this without inheriting the
    default filename — each sleeve drops its own ``*_equity_btc.png``
    alongside its research report. ``title``/``subtitle`` override the chart
    header (e.g. an EXPLORATORY-grade sleeve marks its curve as such).

    ``strategy_name`` relabels the strategy line (e.g. ``"Strategy 3x"``).
    """
    strategy = _strategy_equity_series(equity)
    if not strategy:
        return {}
    if step:
        strategy = _step_fill_daily(strategy)
    start = strategy[0]["date"]
    end = strategy[-1]["date"]
    btc = _normalised_price_series(_btc_daily_close_series(raw_klines, start=start, end=end))
    series = [
        {"name": strategy_name, "color": (7, 14, 31), "alpha": 255, "width": 4, "points": strategy},
        {"name": "BTC", "color": (234, 88, 12), "alpha": 215, "width": 3, "points": btc},
    ]
    monthly_rows = _monthly_table_rows(equity=equity, monthly=monthly)
    # Label counts as trades only when the monthly input contains trade counts.
    has_real_monthly = (
        monthly is not None
        and not monthly.is_empty()
        and _has_columns(monthly, "month", "strategy_return", "trades")
    )
    _remove_stale_chart_artifacts(output_dir)
    png_path = output_dir / png_name
    header: dict[str, Any] = {}
    if title is not None:
        header["title"] = title
    if subtitle is not None:
        header["subtitle"] = subtitle
    _write_equity_benchmark_png(
        png_path,
        series=series,
        start=start,
        end=end,
        monthly_rows=monthly_rows,
        metrics=metrics,
        # The equity-derived fallback counts rows-with-marks per month, NOT trades —
        # label it honestly (a padded flat June showed "11 trades" with zero taken).
        count_label="Trades" if has_real_monthly else "Days",
        **header,
    )
    return {
        "png": str(png_path),
        "series": {
            "strategy": len(strategy),
            "btc": len(btc),
        },
        "monthly_rows": len(monthly_rows),
        "metric_tiles": len(_chart_metric_tiles(metrics)),
        "legend_items": sum(1 for item in series if item["points"]),
        "annotations": [],
    }

def _remove_stale_chart_artifacts(output_dir: Path) -> None:
    for name in (
        "volume_event_best_equity_btc_spy.png",
        "volume_event_best_equity_btc_spy.svg",
        "volume_event_best_equity_benchmarks.csv",
        "volume_event_best_equity_annotations.csv",
    ):
        try:
            (output_dir / name).unlink()
        except FileNotFoundError:
            pass

def _chart_final_values(series: list[dict[str, Any]]) -> dict[str, float]:
    """Each series' legend multiple, measured at the LAST DATE COMMON to all series.

    Anchor to the earliest series end so benchmark multiples share one window.
    """
    # Pick each series' last value at or before the common boundary.
    finals: dict[str, float] = {}
    last_dates: list[date] = []
    for item in series:
        days = [d for p in item["points"] if (d := _parse_day(p["date"])) is not None]
        if days:
            last_dates.append(max(days))
    common_end = min(last_dates) if last_dates else None
    for item in series:
        points = item["points"]
        if not points:
            continue
        value: float | None = None
        if common_end is not None:
            best_day: date | None = None
            for point in points:
                day = _parse_day(point["date"])
                if day is not None and day <= common_end and (best_day is None or day > best_day):
                    best_day = day
                    value = float(point["value"])
        if value is None:
            value = float(points[-1]["value"])
        finals[str(item["name"])] = value
    return finals

def _write_equity_benchmark_png(
    path: Path,
    *,
    series: list[dict[str, Any]],
    start: str,
    end: str,
    monthly_rows: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    title: str = "Strategy Equity vs BTC",
    subtitle: str = "Strategy and BTC are normalised to $1 at the strategy start; gridlines mark monthly dates and growth levels.",
    count_label: str = "Trades",
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Pillow is required to write PNG equity charts") from exc

    scale = 2
    table_rows = monthly_rows or []
    metric_tiles = _chart_metric_tiles(metrics)
    width = 1600
    chart_height = 940
    table_height = 520 if table_rows else (118 if metric_tiles else 0)
    height = chart_height + table_height
    left, right, top, bottom = 120, 58, 150, 190
    plot_w = width - left - right
    plot_h = chart_height - top - bottom
    image = Image.new("RGBA", (width * scale, height * scale), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_tiny = _chart_font(ImageFont, 14 * scale)
    font_table = _chart_font(ImageFont, 16 * scale)
    font_table_header = _chart_font(ImageFont, 15 * scale, bold=True)
    font_metric_label = _chart_font(ImageFont, 13 * scale, bold=True)
    font_metric_value = _chart_font(ImageFont, 23 * scale, bold=True)
    font_legend_name = _chart_font(ImageFont, 19 * scale, bold=True)
    font_legend_value = _chart_font(ImageFont, 18 * scale)

    def _fit_font(content: str, base_px: int, *, bold: bool, min_px: int) -> Any:
        # Shrink-to-fit: a long header (e.g. "... — refreshed to 2026-06-12") must not run
        # off the canvas — the truncated part is usually the load-bearing claim.
        max_w = (width - left - 24) * scale
        size = base_px
        font = _chart_font(ImageFont, size * scale, bold=bold)
        while size > min_px and draw.textlength(content, font=font) > max_w:
            size -= 1
            font = _chart_font(ImageFont, size * scale, bold=bold)
        return font

    font_title = _fit_font(title, 32, bold=True, min_px=18)
    font_subtitle = _fit_font(subtitle, 17, bold=False, min_px=11)

    all_points = [point for item in series for point in item["points"]]
    if not all_points:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.resize((width, height)).save(path)
        return
    min_day = _parse_day(start) or _parse_day(all_points[0]["date"]) or date.today()
    max_day = _parse_day(end) or _parse_day(all_points[-1]["date"]) or min_day
    if max_day <= min_day:
        max_day = date.fromordinal(min_day.toordinal() + 1)
    # Small right-side headroom on the x domain so the final month tick (often the freshest,
    # most-looked-at label) sits inside the plot instead of flush against the right spine.
    # Ticks stay derived from the data end — no tick is drawn in the pad zone.
    axis_end_day = date.fromordinal(
        max_day.toordinal() + max(2, round((max_day.toordinal() - min_day.toordinal()) * 0.015))
    )
    values = [float(point["value"]) for point in all_points if math.isfinite(float(point["value"]))]
    y_min, y_max, y_ticks = _nice_axis(min(values), max(values), target_ticks=12)

    def sx(value: float) -> int:
        return int(round(value * scale))

    def sy(value: float) -> int:
        return int(round(value * scale))

    def x_pos(day_text: str) -> float:
        day = _parse_day(day_text) or min_day
        return left + (day.toordinal() - min_day.toordinal()) / (axis_end_day.toordinal() - min_day.toordinal()) * plot_w

    def y_pos(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_h

    def line(points: list[tuple[float, float]], fill: tuple[int, int, int, int], width_px: int = 1) -> None:
        if len(points) >= 2:
            draw.line(
                [(sx(x), sy(y)) for x, y in points],
                fill=_chart_opaque_fill(fill),
                width=max(1, width_px * scale),
                joint="curve",
            )

    def text(x: float, y: float, content: str, fill: tuple[int, int, int, int], font: Any, anchor: str | None = None) -> None:
        draw.text((sx(x), sy(y)), content, fill=_chart_opaque_fill(fill), font=font, anchor=anchor)

    def rotated_text(x: float, y: float, content: str, fill: tuple[int, int, int, int], font: Any) -> None:
        bbox = draw.textbbox((0, 0), content, font=font)
        pad = 4 * scale
        label_w = int(math.ceil(bbox[2] - bbox[0])) + pad * 2
        label_h = int(math.ceil(bbox[3] - bbox[1])) + pad * 2
        label = Image.new("RGBA", (label_w, label_h), (255, 255, 255, 0))
        label_draw = ImageDraw.Draw(label, "RGBA")
        label_draw.text((pad - bbox[0], pad - bbox[1]), content, fill=_chart_opaque_fill(fill), font=font)
        rotated = label.rotate(90, expand=True)
        image.alpha_composite(rotated, (sx(x) - rotated.width // 2, sy(y)))

    def rect(bounds: tuple[float, float, float, float], fill: tuple[int, int, int, int], outline: tuple[int, int, int, int] | None = None, width_px: int = 1) -> None:
        scaled = tuple(sx(bounds[idx]) if idx % 2 == 0 else sy(bounds[idx]) for idx in range(4))
        draw.rectangle(
            scaled,
            fill=_chart_opaque_fill(fill),
            outline=_chart_opaque_fill(outline) if outline is not None else None,
            width=max(1, width_px * scale),
        )

    def rounded(bounds: tuple[float, float, float, float], radius: float, fill: tuple[int, int, int, int], outline: tuple[int, int, int, int] | None = None) -> None:
        scaled = tuple(sx(bounds[idx]) if idx % 2 == 0 else sy(bounds[idx]) for idx in range(4))
        draw.rounded_rectangle(
            scaled,
            radius=sx(radius),
            fill=_chart_opaque_fill(fill),
            outline=_chart_opaque_fill(outline) if outline is not None else None,
            width=scale,
        )

    rect((0, 0, width, height), (255, 255, 255, 255))
    text(left, 46, title, (7, 14, 31, 255), font_title)
    text(left, 78, subtitle, (75, 85, 99, 255), font_subtitle)
    rounded((left, top, left + plot_w, top + plot_h), 4, (249, 250, 251, 255), (229, 231, 235, 255))
    metric_top = chart_height + 6
    table_top = chart_height + (104 if metric_tiles else 130)

    for value in y_ticks:
        y = y_pos(value)
        line([(left, y), (left + plot_w, y)], (221, 228, 238, 215), 1)
        line([(left - 6, y), (left, y)], (148, 163, 184, 255), 1)
        text(left - 14, y, f"{value:g}x", (71, 85, 105, 255), font_tiny, anchor="rm")
    x_ticks = _date_axis_ticks(min_day, max_day)
    for day in x_ticks:
        x = x_pos(day.isoformat())
        if day.month == 1:
            line([(x, top), (x, top + plot_h)], (203, 213, 225, 230), 1)
        else:
            line([(x, top), (x, top + plot_h)], (230, 236, 244, 175), 1)
        line([(x, top + plot_h), (x, top + plot_h + 6)], (148, 163, 184, 255), 1)
        rotated_text(x, top + plot_h + 12, day.strftime("%Y-%m"), (71, 85, 105, 255), font_tiny)
    line([(left, top), (left, top + plot_h), (left + plot_w, top + plot_h)], (148, 163, 184, 255), 1)

    for item in series:
        points = item["points"]
        coords = [(x_pos(point["date"]), y_pos(float(point["value"]))) for point in points]
        rgb = tuple(item["color"])
        line(coords, (rgb[0], rgb[1], rgb[2], int(item["alpha"])), int(item["width"]))

    _draw_chart_legend(
        rounded=rounded,
        line=line,
        text=text,
        measure=lambda content, font: draw.textlength(content, font=font) / scale,
        series=series,
        finals=_chart_final_values(series),
        left=left,
        top=chart_height - 78,
        width=plot_w,
        font_name=font_legend_name,
        font_value=font_legend_value,
    )
    if metric_tiles:
        _draw_chart_metric_strip(
            rounded=rounded,
            text=text,
            metrics=metric_tiles,
            left=left,
            top=metric_top,
            width=plot_w,
            font_label=font_metric_label,
            font_value=font_metric_value,
        )
    if table_rows:
        _draw_monthly_return_table(
            text=text,
            rect=rect,
            rows=table_rows,
            left=left,
            top=table_top,
            width=plot_w,
            font_table=font_table,
            font_table_header=font_table_header,
            count_label=count_label,
        )

    image = image.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def _chart_metric_tiles(metrics: dict[str, Any] | None) -> list[tuple[str, str, tuple[int, int, int, int]]]:
    if not metrics:
        return []

    def finite(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    def pct(key: str, *, signed: bool = True) -> str | None:
        value = finite(metrics.get(key))
        if value is None:
            return None
        return f"{value:+.2f}%" if signed else f"{value:.2f}%"

    def num(key: str, digits: int = 2) -> str | None:
        value = finite(metrics.get(key))
        if value is None:
            return None
        return f"{value:.{digits}f}"

    candidates: list[tuple[str, str | None, tuple[int, int, int, int]]] = [
        ("Total", pct("total_return_pct"), (22, 101, 52, 255)),
        ("Annualized", pct("annualized_pct"), (22, 101, 52, 255)),
        ("Max DD", pct("max_drawdown_pct", signed=False), (185, 28, 28, 255)),
        ("Worst Day", pct("worst_day_pct", signed=False), (185, 28, 28, 255)),
        ("Sharpe", num("sharpe_daily_ann"), (7, 14, 31, 255)),
        ("MAR", num("mar"), (7, 14, 31, 255)),
        ("Years", (f"{v:.2f}" if (v := finite(metrics.get("years"))) is not None else None), (7, 14, 31, 255)),
    ]
    return [(label, value, color) for label, value, color in candidates if value is not None]


def _draw_chart_metric_strip(
    *,
    rounded: Any,
    text: Any,
    metrics: list[tuple[str, str, tuple[int, int, int, int]]],
    left: float,
    top: float,
    width: float,
    font_label: Any,
    font_value: Any,
) -> None:
    if not metrics:
        return
    gap = 12
    tile_count = min(8, len(metrics))
    tile_w = (width - gap * (tile_count - 1)) / tile_count
    tile_h = 74
    for index, (label, value, color) in enumerate(metrics[:tile_count]):
        x = left + index * (tile_w + gap)
        rounded((x, top, x + tile_w, top + tile_h), 4, (248, 250, 252, 255), (226, 232, 240, 255))
        text(x + 12, top + 12, label.upper(), (71, 85, 105, 255), font_label)
        text(x + 12, top + 37, value, color, font_value)


def _draw_chart_legend(
    *,
    rounded: Any,
    line: Any,
    text: Any,
    measure: Any,
    series: list[dict[str, Any]],
    finals: dict[str, float],
    left: float,
    top: float,
    width: float,
    font_name: Any,
    font_value: Any,
) -> None:
    x = left
    y = top
    chip_h = 46
    gap = 14
    for item in series:
        if not item["points"]:
            continue
        rgb = tuple(item["color"])
        name = str(item["name"])
        value = f"{finals.get(name, 0.0):.2f}x"
        name_w = measure(name, font_name)
        value_w = measure(value, font_value)
        chip_w = max(138.0, 62.0 + name_w + 14.0 + value_w + 20.0)
        if x > left and x + chip_w > left + width:
            x = left
            y += chip_h + 10
        rounded((x, y, x + chip_w, y + chip_h), 6, (255, 255, 255, 255), (226, 232, 240, 255))
        line([(x + 13, y + 23), (x + 39, y + 23)], (rgb[0], rgb[1], rgb[2], 235), 5)
        text(x + 51, y + 12, name, (15, 23, 42, 255), font_name)
        text(x + 51 + name_w + 14, y + 13, value, (71, 85, 105, 255), font_value)
        x += chip_w + gap


def _monthly_return_color(value: float) -> tuple[int, int, int, int]:
    if abs(value) < 0.00005:
        return (100, 116, 139, 255)
    if value > 0.0:
        return (22, 101, 52, 255)
    return (185, 28, 28, 255)

def _fill_month_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert +0.00%/count-0 rows for months absent between the first and last row. A month
    where the book sat flat must show as zero, not vanish — a missing month reads as a
    rendering bug and hides that the strategy was alive but idle."""
    if not rows:
        return rows
    by_month = {row["month"]: row for row in rows}
    year, month = (int(part) for part in rows[0]["month"].split("-"))
    last = rows[-1]["month"]
    filled: list[dict[str, Any]] = []
    while True:
        key = f"{year:04d}-{month:02d}"
        filled.append(by_month.get(key, {"month": key, "return": 0.0, "count": 0}))
        if key >= last:
            return filled
        month += 1
        if month > 12:
            month = 1
            year += 1


def _monthly_table_rows(*, equity: pl.DataFrame, monthly: pl.DataFrame | None) -> list[dict[str, Any]]:
    """Rows are {month, return, count}; `count` is real trades when `monthly` carries
    a ``trades`` column, otherwise the number of equity rows in the month (rendered
    as "Days", not "Trades")."""
    has_monthly_returns = (
        monthly is not None and not monthly.is_empty() and _has_columns(monthly, "month", "strategy_return")
    )
    # Without a trades column, the fallback count is equity days and is labelled accordingly.
    if has_monthly_returns and monthly is not None and "trades" in monthly.columns:
        rows = [
            {
                "month": str(row["month"]),
                "return": _float_or_nan(row.get("strategy_return")),
                "count": int(row.get("trades") or 0),
            }
            for row in monthly.select(["month", "strategy_return", "trades"]).sort("month").to_dicts()
            if math.isfinite(_float_or_nan(row.get("strategy_return")))
        ]
        return _fill_month_gaps(rows)
    if has_monthly_returns and monthly is not None:
        # Monthly returns without a trades column: keep the real returns, derive the
        # count from equity rows per month (0 when equity is unavailable).
        day_counts: dict[str, int] = {}
        if not equity.is_empty() and _has_columns(equity, "date"):
            day_counts = {
                str(r["month"]): int(r["count"] or 0)
                for r in (
                    equity.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
                    .group_by("month")
                    .agg(pl.len().alias("count"))
                    .to_dicts()
                )
            }
        rows = [
            {
                "month": str(row["month"]),
                "return": _float_or_nan(row.get("strategy_return")),
                "count": day_counts.get(str(row["month"]), 0),
            }
            for row in monthly.select(["month", "strategy_return"]).sort("month").to_dicts()
            if math.isfinite(_float_or_nan(row.get("strategy_return")))
        ]
        return _fill_month_gaps(rows)
    if equity.is_empty() or not _has_columns(equity, "date", "basket_return"):
        return []
    frame = (
        equity.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.len().alias("count"),
            ]
        )
        .sort("month")
    )
    rows = [
        {
            "month": str(row["month"]),
            "return": _float_or_nan(row.get("strategy_return")),
            "count": int(row.get("count") or 0),
        }
        for row in frame.to_dicts()
        if math.isfinite(_float_or_nan(row.get("strategy_return")))
    ]
    return _fill_month_gaps(rows)

def _draw_monthly_return_table(
    *,
    text: Any,
    rect: Any,
    rows: list[dict[str, Any]],
    left: float,
    top: float,
    width: float,
    font_table: Any,
    font_table_header: Any,
    count_label: str = "Trades",
) -> None:
    if not rows:
        return
    block_count = min(4, max(1, math.ceil(len(rows) / 9)))
    rows_per_block = math.ceil(len(rows) / block_count)
    gap = 24
    block_w = (width - gap * (block_count - 1)) / block_count
    row_h = 31
    header_h = 34
    for block_index in range(block_count):
        start_index = block_index * rows_per_block
        block_rows = rows[start_index : start_index + rows_per_block]
        if not block_rows:
            continue
        x = left + block_index * (block_w + gap)
        block_h = header_h + len(block_rows) * row_h
        rect((x, top, x + block_w, top + block_h), (255, 255, 255, 255), (226, 232, 240, 255))
        rect((x, top, x + block_w, top + header_h), (241, 245, 249, 255), (226, 232, 240, 255))
        text(x + 10, top + 9, "Month", (51, 65, 85, 255), font_table_header)
        text(x + block_w * 0.48, top + 9, "Return", (51, 65, 85, 255), font_table_header)
        text(x + block_w - 10, top + 9, count_label, (51, 65, 85, 255), font_table_header, anchor="ra")
        for row_index, row in enumerate(block_rows):
            y = top + header_h + row_index * row_h
            if row_index % 2 == 1:
                rect((x, y, x + block_w, y + row_h), (248, 250, 252, 255))
            value = _float_or_nan(row.get("return"))
            color = _monthly_return_color(value)
            text(x + 10, y + 7, str(row.get("month", "")), (51, 65, 85, 255), font_table)
            text(x + block_w * 0.48, y + 7, f"{value:+.2%}", color, font_table)
            text(x + block_w - 10, y + 7, str(int(row.get("count") or 0)), (51, 65, 85, 255), font_table, anchor="ra")

def _chart_font(image_font: Any, size: int, *, bold: bool = False) -> Any:
    # Windows paths included: without them the loop exhausts and falls back to
    # PIL's fixed-size bitmap default, which IGNORES `size` — every chart
    # rendered on a Windows box came out with unreadably tiny text.
    names = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
        ]
    )
    for name in names:
        try:
            return image_font.truetype(name, size)
        except OSError:
            continue
    return image_font.load_default()

def _chart_opaque_fill(fill: tuple[int, ...]) -> tuple[int, int, int, int]:
    if len(fill) < 4:
        return (int(fill[0]), int(fill[1]), int(fill[2]), 255)
    alpha = max(0, min(255, int(fill[3]))) / 255.0
    return (
        int(round(int(fill[0]) * alpha + 255 * (1.0 - alpha))),
        int(round(int(fill[1]) * alpha + 255 * (1.0 - alpha))),
        int(round(int(fill[2]) * alpha + 255 * (1.0 - alpha))),
        255,
    )

def _nice_axis(min_value: float, max_value: float, *, target_ticks: int) -> tuple[float, float, list[float]]:
    span = max(max_value - min_value, 1e-9)
    step = _nice_step(span / max(target_ticks - 1, 1))
    # Let the axis follow negative equity so a levered wipeout remains visible.
    floor_candidate = min_value - span * 0.05
    low = math.floor((floor_candidate if min_value < 0.0 else max(0.0, floor_candidate)) / step) * step
    high = math.ceil((max_value + span * 0.06) / step) * step
    ticks = []
    value = low
    for _ in range(20):
        if value > high + step * 0.5:
            break
        ticks.append(round(value, 10))
        value += step
    return low, high, ticks

def _nice_step(value: float) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / 10**exponent
    if fraction <= 1.0:
        nice = 1.0
    elif fraction <= 2.0:
        nice = 2.0
    elif fraction <= 2.5:
        nice = 2.5
    elif fraction <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * 10**exponent

def _date_axis_ticks(start: date, end: date) -> list[date]:
    ticks = []
    year = start.year
    month = start.month
    current = date(year, month, 1)
    if current < start:
        month += 1
        if month > 12:
            month -= 12
            year += 1
        current = date(year, month, 1)
    while current <= end:
        ticks.append(current)
        month = current.month + 1
        year = current.year
        if month > 12:
            month -= 12
            year += 1
        current = date(year, month, 1)
    return ticks

def _strategy_equity_series(equity: pl.DataFrame) -> list[dict[str, Any]]:
    if equity.is_empty() or not _has_columns(equity, "date", "equity"):
        return []
    rows = []
    # Some report frames have dates but no timestamp column.
    sort_key = "ts_ms" if _has_columns(equity, "ts_ms") else "date"
    for row in equity.sort(sort_key).select(["date", "equity"]).to_dicts():
        value = _float_or_nan(row.get("equity"))
        day = _parse_day(row.get("date"))
        if day is not None and math.isfinite(value):
            rows.append({"date": day.isoformat(), "value": value})
    return rows

def _step_fill_daily(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Forward-fill an equity series to one point per calendar day so no-trade
    gaps render flat (a staircase) instead of a misleading diagonal.

    Realised-PnL equity only carries a point on trade-exit days; between exits the
    book is flat (holds are short and the gaps are position-free), so carrying the
    last value forward across the gap is the honest shape. Drawing a straight line
    between two far-apart exit points instead implies smooth daily growth that did
    not happen. A series already at daily granularity is unchanged: the fill only
    inserts the days strictly between consecutive points.
    """
    if len(points) < 2:
        return points
    ordered = sorted(points, key=lambda p: str(p["date"]))
    filled: list[dict[str, Any]] = []
    for index, point in enumerate(ordered):
        if index > 0:
            prev = ordered[index - 1]
            prev_day = _parse_day(prev["date"])
            cur_day = _parse_day(point["date"])
            if prev_day is not None and cur_day is not None:
                for ordinal in range(prev_day.toordinal() + 1, cur_day.toordinal()):
                    filled.append({"date": date.fromordinal(ordinal).isoformat(), "value": prev["value"]})
        filled.append(point)
    return filled

def _btc_daily_close_series(raw_klines: pl.DataFrame, *, start: str, end: str) -> list[dict[str, Any]]:
    if raw_klines.is_empty() or not _has_columns(raw_klines, "symbol", "date", "ts_ms", "close"):
        return []
    frame = (
        raw_klines.filter(
            (pl.col("symbol") == "BTCUSDT")
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
            & pl.col("close").is_not_null()
        )
        .sort("ts_ms")
        .group_by("date", maintain_order=True)
        .agg(pl.col("close").last().alias("value"))
        .sort("date")
    )
    return [
        {"date": str(row["date"]), "value": float(row["value"])}
        for row in frame.to_dicts()
        if row.get("value") is not None and math.isfinite(float(row["value"]))
    ]

def _normalised_price_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = [
        {"date": str(row["date"]), "value": float(row["value"])}
        for row in rows
        if row.get("value") is not None and math.isfinite(float(row["value"])) and float(row["value"]) > 0.0
    ]
    if not cleaned:
        return []
    base = cleaned[0]["value"]
    return [{"date": row["date"], "value": row["value"] / base} for row in cleaned]
