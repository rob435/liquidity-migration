"""Render equity curves (in-sample + 2 OOS roots) for the momentum factor.

Uses Pillow (already a project dep). Outputs PNG to docs/.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from PIL import Image, ImageDraw, ImageFont


CURVES = [
    ("LO_skip0  IS Bybit 2023-2026 (Sharpe 0.93)",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/momentum_factor_LO_skip0_canonical_2023_2026/momentum_factor_equity.csv",
     (10, 68, 41, 255)),    # dark green
    ("LO_skip0  OOS Bybit 2021-2023 (Sharpe 0.75)",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/momentum_factor_LO_skip0_OOS_bybit_pre2023/momentum_factor_equity.csv",
     (44, 110, 161, 255)),  # blue
    ("LO_skip0  OOS Binance 2020-2023 (Sharpe 1.39)",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/binance_oos_pit/reports/momentum_factor_LO_skip0_OOS_binance_pit/momentum_factor_equity.csv",
     (212, 102, 26, 255)),  # orange
]
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/momentum_factor_LO_skip0_equity_curves.png")

WIDTH, HEIGHT = 1500, 900
MARGIN_L, MARGIN_R = 80, 250
MARGIN_T, MARGIN_B = 100, 90
PLOT_W = WIDTH - MARGIN_L - MARGIN_R
PLOT_TOP_H = (HEIGHT - MARGIN_T - MARGIN_B) * 5 // 7  # equity plot
PLOT_DD_H = (HEIGHT - MARGIN_T - MARGIN_B) - PLOT_TOP_H - 50  # dd plot

X_START = datetime(2020, 1, 1, tzinfo=UTC)
X_END = datetime(2026, 6, 1, tzinfo=UTC)


def _try_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception:
            continue
    return ImageFont.load_default()


def _ts_to_x(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
    total = (X_END - X_START).total_seconds()
    pos = (dt - X_START).total_seconds() / total
    return MARGIN_L + int(pos * PLOT_W)


def _eq_to_y(equity: float, y_min: float, y_max: float, plot_top: int, plot_h: int) -> int:
    pos = (equity - y_min) / (y_max - y_min) if y_max > y_min else 0.5
    return plot_top + plot_h - int(pos * plot_h)


def _dd_to_y(dd_pct: float, plot_top: int, plot_h: int) -> int:
    # dd_pct is negative or 0; clamp at -50%
    dd_clamped = max(dd_pct, -50.0)
    pos = (-dd_clamped) / 50.0  # 0 at top, 1 at bottom
    return plot_top + int(pos * plot_h)


def main() -> None:
    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_title = _try_font(22, bold=True)
    font_label = _try_font(15)
    font_small = _try_font(13)

    # Load and pre-process curves
    series = []
    eq_min, eq_max = 1.0, 1.0
    for name, path, color in CURVES:
        df = pl.read_csv(path).sort("ts_ms")
        if df.is_empty():
            continue
        equity = [float(x) for x in df["equity"].to_list()]
        dd = [float(x) * 100 for x in df["drawdown"].to_list()]
        ts = [int(x) for x in df["ts_ms"].to_list()]
        # Rebase to 1.0 at first point
        base = equity[0] if equity[0] > 0 else 1.0
        rebased = [e / base for e in equity]
        eq_min = min(eq_min, *rebased)
        eq_max = max(eq_max, *rebased)
        series.append({"name": name, "color": color, "ts": ts, "eq": rebased, "dd": dd})

    # Tighten y-range
    eq_min = max(0.5, eq_min * 0.97)
    eq_max = eq_max * 1.05

    # ---- Title ----
    draw.text((MARGIN_L, 30), "Long-only cross-sectional momentum — LO_skip0 config", fill=(20, 20, 30, 255), font=font_title)
    draw.text((MARGIN_L, 60), "Counterpart to existing short sleeve. All three windows positive. Avg Sharpe ~1.0 across IS + 2 OOS roots.",
              fill=(80, 80, 90, 255), font=font_label)

    # ---- Equity plot frame ----
    plot_top = MARGIN_T
    plot_bottom = plot_top + PLOT_TOP_H
    draw.rectangle([MARGIN_L, plot_top, MARGIN_L + PLOT_W, plot_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    # 1.0 baseline
    y_one = _eq_to_y(1.0, eq_min, eq_max, plot_top, PLOT_TOP_H)
    draw.line([(MARGIN_L, y_one), (MARGIN_L + PLOT_W, y_one)], fill=(160, 160, 170, 255), width=1)
    # y-axis labels (equity)
    for v in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0):
        if eq_min <= v <= eq_max:
            y = _eq_to_y(v, eq_min, eq_max, plot_top, PLOT_TOP_H)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 55, y - 8), f"{v:.2f}x", fill=(80, 80, 90, 255), font=font_small)
    # x-axis ticks (years)
    for year in range(X_START.year, X_END.year + 1):
        x = _ts_to_x(int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000))
        draw.line([(x, plot_bottom), (x, plot_bottom + 4)], fill=(120, 120, 130, 255), width=1)
        draw.text((x - 14, plot_bottom + 8), str(year), fill=(80, 80, 90, 255), font=font_small)

    # ---- Plot equity lines ----
    for s in series:
        pts = [(_ts_to_x(t), _eq_to_y(e, eq_min, eq_max, plot_top, PLOT_TOP_H)) for t, e in zip(s["ts"], s["eq"])]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=s["color"], width=3)

    # ---- Drawdown subplot ----
    dd_top = plot_bottom + 50
    dd_bottom = dd_top + PLOT_DD_H
    draw.rectangle([MARGIN_L, dd_top, MARGIN_L + PLOT_W, dd_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    draw.text((MARGIN_L, dd_top - 25), "Drawdown (%, clamped at -50%)", fill=(60, 60, 70, 255), font=font_label)
    # 0% line
    y_zero = _dd_to_y(0.0, dd_top, PLOT_DD_H)
    draw.line([(MARGIN_L, y_zero), (MARGIN_L + PLOT_W, y_zero)], fill=(160, 160, 170, 255), width=1)
    for v in (0, -10, -20, -30, -40, -50):
        y = _dd_to_y(v, dd_top, PLOT_DD_H)
        draw.text((MARGIN_L - 50, y - 8), f"{v}%", fill=(80, 80, 90, 255), font=font_small)
    # x ticks
    for year in range(X_START.year, X_END.year + 1):
        x = _ts_to_x(int(datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000))
        draw.line([(x, dd_bottom), (x, dd_bottom + 4)], fill=(120, 120, 130, 255), width=1)
        draw.text((x - 14, dd_bottom + 8), str(year), fill=(80, 80, 90, 255), font=font_small)

    # ---- DD fills ----
    for s in series:
        fill_color = (s["color"][0], s["color"][1], s["color"][2], 90)
        outline_color = s["color"]
        prev_pt = None
        for t, d in zip(s["ts"], s["dd"]):
            x = _ts_to_x(t)
            y = _dd_to_y(d, dd_top, PLOT_DD_H)
            if prev_pt is not None:
                draw.polygon([prev_pt, (x, y), (x, y_zero), (prev_pt[0], y_zero)], fill=fill_color)
                draw.line([prev_pt, (x, y)], fill=outline_color, width=2)
            prev_pt = (x, y)

    # ---- Legend ----
    legend_x = MARGIN_L + PLOT_W + 25
    legend_y = MARGIN_T + 20
    for s in series:
        draw.rectangle([legend_x, legend_y, legend_x + 30, legend_y + 8], fill=s["color"])
        # word-wrap legend name
        parts = s["name"].split(" (")
        line1 = parts[0]
        line2 = "(" + parts[1] if len(parts) > 1 else ""
        draw.text((legend_x + 40, legend_y - 4), line1, fill=(20, 20, 30, 255), font=font_small)
        draw.text((legend_x + 40, legend_y + 12), line2, fill=(80, 80, 90, 255), font=font_small)
        legend_y += 50

    # Footer
    draw.text((MARGIN_L, HEIGHT - 28),
              "Each curve is rebased to 1.0 at its first basket. Different time periods plotted on a shared 2020-2026 axis. "
              "Costs (3x base round-trip) and funding modeled where available (in-sample only).",
              fill=(100, 100, 110, 255), font=font_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
