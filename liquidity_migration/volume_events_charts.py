"""Shared chart writers. Originally a slice of the volume_events hub (the daily
SHORT engine, ERASED 2026-06-11 by operator order — `from
liquidity_migration.volume_events import X` no longer works); this module
survives standalone, consumed by the long/continuous report paths.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl



# Splits live exclusively on VolumeEventResearchConfig.splits now (default ()).
# Whole-period reporting is the post-rebuild norm; pristine OOS is the forward
# demo/paper ledger, not a backtest window.

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
    root: Path,
    equity: pl.DataFrame,
    raw_klines: pl.DataFrame,
    monthly: pl.DataFrame | None = None,
    png_name: str = "volume_event_best_equity_btc.png",
    title: str | None = None,
    subtitle: str | None = None,
    step: bool = True,
    overlays: Sequence[OverlaySpec] | None = None,
    strategy_name: str = "Strategy",
) -> dict[str, Any]:
    """Write the strategy-vs-BTC equity PNG. ``png_name`` lets other sleeves
    (e.g. ``long_native``, ``continuous``) reuse this without inheriting the
    short-sleeve filename — each sleeve drops its own ``*_equity_btc.png``
    alongside its research report. ``title``/``subtitle`` override the chart
    header (e.g. an EXPLORATORY-grade sleeve marks its curve as such).

    ``overlays`` layers extra normalised benchmark lines (a stock, an index, a
    second strategy) after BTC — build them with :func:`price_overlay_from_csv`.
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
    overlay_series = _overlay_series_dicts(overlays or [])
    series.extend(overlay_series)
    monthly_rows = _monthly_table_rows(equity=equity, monthly=monthly)
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
        **header,
    )
    return {
        "png": str(png_path),
        "series": {
            "strategy": len(strategy),
            "btc": len(btc),
            **{item["name"]: len(item["points"]) for item in overlay_series},
        },
        "overlays": [item["name"] for item in overlay_series],
        "monthly_rows": len(monthly_rows),
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
    finals: dict[str, float] = {}
    for item in series:
        points = item["points"]
        if points:
            finals[str(item["name"])] = float(points[-1]["value"])
    return finals

def _write_equity_benchmark_png(
    path: Path,
    *,
    series: list[dict[str, Any]],
    start: str,
    end: str,
    monthly_rows: list[dict[str, Any]] | None = None,
    title: str = "Strategy Equity vs BTC",
    subtitle: str = "Strategy and BTC are normalised to $1 at the strategy start; gridlines mark monthly dates and growth levels.",
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Pillow is required to write PNG equity charts") from exc

    scale = 2
    table_rows = monthly_rows or []
    width = 1600
    chart_height = 940
    table_height = 520 if table_rows else 0
    height = chart_height + table_height
    left, right, top, bottom = 120, 58, 150, 190
    plot_w = width - left - right
    plot_h = chart_height - top - bottom
    image = Image.new("RGBA", (width * scale, height * scale), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_regular = _chart_font(ImageFont, 22 * scale)
    font_small = _chart_font(ImageFont, 17 * scale)
    font_tiny = _chart_font(ImageFont, 14 * scale)
    font_table = _chart_font(ImageFont, 16 * scale)
    font_table_header = _chart_font(ImageFont, 15 * scale, bold=True)
    font_title = _chart_font(ImageFont, 32 * scale, bold=True)

    all_points = [point for item in series for point in item["points"]]
    if not all_points:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.resize((width, height)).save(path)
        return
    min_day = _parse_day(start) or _parse_day(all_points[0]["date"]) or date.today()
    max_day = _parse_day(end) or _parse_day(all_points[-1]["date"]) or min_day
    if max_day <= min_day:
        max_day = date.fromordinal(min_day.toordinal() + 1)
    values = [float(point["value"]) for point in all_points if math.isfinite(float(point["value"]))]
    y_min, y_max, y_ticks = _nice_axis(min(values), max(values), target_ticks=12)

    def sx(value: float) -> int:
        return int(round(value * scale))

    def sy(value: float) -> int:
        return int(round(value * scale))

    def x_pos(day_text: str) -> float:
        day = _parse_day(day_text) or min_day
        return left + (day.toordinal() - min_day.toordinal()) / (max_day.toordinal() - min_day.toordinal()) * plot_w

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
    text(left, 78, subtitle, (75, 85, 99, 255), font_small)
    rounded((left, top, left + plot_w, top + plot_h), 4, (249, 250, 251, 255), (229, 231, 235, 255))

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

    legend_x: float = left
    finals = _chart_final_values(series)
    legend_y = chart_height - 56
    for item in series:
        if not item["points"]:
            continue
        rgb = tuple(item["color"])
        label = f"{item['name']} {finals.get(str(item['name']), 0.0):.2f}x"
        line([(legend_x, legend_y), (legend_x + 42, legend_y)], (rgb[0], rgb[1], rgb[2], 230), 5)
        text(legend_x + 54, legend_y - 8, label, (17, 24, 39, 255), font_regular)
        label_w = draw.textlength(label, font=font_regular) / scale
        legend_x += 54 + label_w + 48
    if table_rows:
        _draw_monthly_return_table(
            text=text,
            rect=rect,
            rows=table_rows,
            left=left,
            top=chart_height + 130,
            width=plot_w,
            font_table=font_table,
            font_table_header=font_table_header,
        )

    image = image.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)

def _monthly_table_rows(*, equity: pl.DataFrame, monthly: pl.DataFrame | None) -> list[dict[str, Any]]:
    if monthly is not None and not monthly.is_empty() and _has_columns(monthly, "month", "strategy_return"):
        columns = ["month", "strategy_return"]
        if "trades" in monthly.columns:
            columns.append("trades")
        return [
            {
                "month": str(row["month"]),
                "return": _float_or_nan(row.get("strategy_return")),
                "trades": int(row.get("trades") or 0),
            }
            for row in monthly.select(columns).sort("month").to_dicts()
            if math.isfinite(_float_or_nan(row.get("strategy_return")))
        ]
    if equity.is_empty() or not _has_columns(equity, "date", "basket_return"):
        return []
    frame = (
        equity.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.len().alias("trades"),
            ]
        )
        .sort("month")
    )
    return [
        {
            "month": str(row["month"]),
            "return": _float_or_nan(row.get("strategy_return")),
            "trades": int(row.get("trades") or 0),
        }
        for row in frame.to_dicts()
        if math.isfinite(_float_or_nan(row.get("strategy_return")))
    ]

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
        text(x + block_w - 10, top + 9, "Trades", (51, 65, 85, 255), font_table_header, anchor="ra")
        for row_index, row in enumerate(block_rows):
            y = top + header_h + row_index * row_h
            if row_index % 2 == 1:
                rect((x, y, x + block_w, y + row_h), (248, 250, 252, 255))
            value = _float_or_nan(row.get("return"))
            color = (22, 101, 52, 255) if value >= 0.0 else (185, 28, 28, 255)
            text(x + 10, y + 7, str(row.get("month", "")), (51, 65, 85, 255), font_table)
            text(x + block_w * 0.48, y + 7, f"{value:+.2%}", color, font_table)
            text(x + block_w - 10, y + 7, str(int(row.get("trades") or 0)), (51, 65, 85, 255), font_table, anchor="ra")

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
    low = math.floor(max(0.0, min_value - span * 0.05) / step) * step
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
    for row in equity.sort("ts_ms").select(["date", "equity"]).to_dicts():
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


# --- modular benchmark overlays ------------------------------------------------
# Any chart can layer extra normalised benchmark lines (a stock, an index, a second
# strategy) on top of Strategy + BTC. An overlay is just a name + a normalised
# {date,value} series; pass a list to _write_equity_benchmark_chart(overlays=...).
# Colors auto-assign from the palette when left None, so callers usually only
# supply a name + the source CSV (see price_overlay_from_csv).
_OVERLAY_PALETTE: tuple[tuple[int, int, int], ...] = (
    (13, 148, 136),   # teal
    (124, 58, 237),   # violet
    (217, 70, 239),   # fuchsia
    (5, 150, 105),    # emerald
    (202, 138, 4),    # amber
)
_OVERLAY_DATE_COLS = ("date", "day", "timestamp", "ts_ms")
_OVERLAY_VALUE_COLS = ("value", "close", "adj_close", "adjclose", "price")


@dataclass
class OverlaySpec:
    """One benchmark line to overlay on the official chart.

    ``points`` is a normalised series ($1 at the strategy start) — build it with
    :func:`price_overlay_from_csv` (CSV of date,close) or hand it any
    ``[{date, value}]`` already passed through :func:`_normalised_price_series`.
    ``color=None`` auto-assigns the next palette color.
    """

    name: str
    points: list[dict[str, Any]] = field(default_factory=list)
    color: tuple[int, int, int] | None = None
    alpha: int = 230
    width: int = 3


def price_overlay_from_csv(
    path: str | Path,
    *,
    name: str,
    start: str,
    end: str,
    color: tuple[int, int, int] | None = None,
    alpha: int = 230,
    width: int = 3,
    date_col: str | None = None,
    value_col: str | None = None,
) -> OverlaySpec:
    """Load a price CSV into a normalised overlay clipped to ``[start, end]``.

    Robust to schema: the date column is auto-detected from
    (date/day/timestamp/ts_ms) and the value column from
    (value/close/adj_close/price), or pass ``date_col``/``value_col`` explicitly.
    Normalisation is to $1 at the first in-window point, matching Strategy/BTC.
    """
    df = pl.read_csv(Path(path).expanduser())
    cols = {c.lower(): c for c in df.columns}
    dcol = date_col or next((cols[c] for c in _OVERLAY_DATE_COLS if c in cols), None)
    vcol = value_col or next((cols[c] for c in _OVERLAY_VALUE_COLS if c in cols), None)
    if dcol is None or vcol is None:
        raise ValueError(f"{path}: need a date and a value/close column, got {df.columns}")
    if dcol.lower() == "ts_ms":
        date_expr = pl.from_epoch(pl.col(dcol), "ms").dt.date().cast(pl.Utf8)
    else:
        date_expr = pl.col(dcol).cast(pl.Utf8).str.slice(0, 10)
    frame = (
        df.select(date=date_expr, value=pl.col(vcol).cast(pl.Float64, strict=False))
        .filter((pl.col("date") >= start) & (pl.col("date") <= end) & pl.col("value").is_not_null())
        .sort("date")
    )
    return OverlaySpec(
        name=name,
        points=_normalised_price_series(frame.to_dicts()),
        color=color,
        alpha=alpha,
        width=width,
    )


def _overlay_series_dicts(overlays: Sequence[OverlaySpec]) -> list[dict[str, Any]]:
    """Convert overlay specs to renderer series dicts, auto-coloring from the
    palette where ``color is None`` and dropping overlays that have no points."""
    out: list[dict[str, Any]] = []
    palette_i = 0
    for overlay in overlays:
        if not overlay.points:
            continue
        color = overlay.color
        if color is None:
            color = _OVERLAY_PALETTE[palette_i % len(_OVERLAY_PALETTE)]
            palette_i += 1
        out.append(
            {"name": overlay.name, "color": color, "alpha": overlay.alpha,
             "width": overlay.width, "points": overlay.points}
        )
    return out
