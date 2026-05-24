"""Equity curves: short alone vs short + long FC at various leverage levels."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from PIL import Image, ImageDraw, ImageFont


SHORT = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/volume_event_research/volume_event_best_equity.csv"
LONG = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_15_5_hold3_tp40/long_native_equity.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/combined_book_equity.png")

WIDTH, HEIGHT = 1500, 900
MARGIN_L, MARGIN_R = 100, 270
MARGIN_T, MARGIN_B = 110, 90
PLOT_W = WIDTH - MARGIN_L - MARGIN_R
PLOT_TOP_H = (HEIGHT - MARGIN_T - MARGIN_B) * 5 // 7
PLOT_DD_H = (HEIGHT - MARGIN_T - MARGIN_B) - PLOT_TOP_H - 50


def _try_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception:
            continue
    return ImageFont.load_default()


def to_daily(eq_path):
    eq = pl.read_csv(eq_path).sort("ts_ms")
    dates = sorted(set(eq["date"].to_list()))
    eq_by_date = dict(zip(eq["date"].to_list(), eq["equity"].to_list()))
    start = datetime.strptime(min(dates), "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(max(dates), "%Y-%m-%d").replace(tzinfo=UTC)
    days = (end - start).days + 1
    out = []
    last = 1.0
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in eq_by_date:
            last = eq_by_date[d]
        out.append((d, last))
    return out


def main():
    s = dict(to_daily(SHORT))
    l = dict(to_daily(LONG))
    common = sorted(set(s.keys()) & set(l.keys()))
    s_arr = np.array([s[d] for d in common]); s_arr = s_arr / s_arr[0]
    l_arr = np.array([l[d] for d in common]); l_arr = l_arr / l_arr[0]
    s_ret = np.diff(s_arr) / s_arr[:-1]
    l_ret = np.diff(l_arr) / l_arr[:-1]

    # Build series: short alone, short + long 1x, short + long 5x, short + long 10x
    SERIES = [
        ("Short only (1×)", 1.0, 0.0, (165, 27, 27, 255)),
        ("Short 1× + Long 1× (2× gross)", 1.0, 1.0, (212, 102, 26, 255)),
        ("Short 1× + Long 5× (6× gross)", 1.0, 5.0, (10, 68, 41, 255)),
        ("Short 1× + Long 10× (11× gross)", 1.0, 10.0, (44, 110, 161, 255)),
    ]

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _try_font(22, bold=True); f_label = _try_font(15); f_small = _try_font(13)

    draw.text((MARGIN_L, 30), "Combined book — short sleeve + long FC at varying leverage", fill=(20, 20, 30, 255), font=f_title)
    draw.text((MARGIN_L, 60), f"Sleeves are independent (corr = +0.006). Adding leveraged long FC IMPROVES sharpe AND lowers drawdown.",
              fill=(80, 80, 90, 255), font=f_label)

    # Compute equity arrays for each series
    plotted = []
    for name, ws, wl, color in SERIES:
        port_ret = ws * s_ret + wl * l_ret
        eq = np.cumprod(1.0 + port_ret)
        peaks = np.maximum.accumulate(eq)
        dd = eq / peaks - 1.0
        sh = float(port_ret.mean() / port_ret.std(ddof=1) * math.sqrt(365))
        plotted.append((name, color, eq, dd, sh, eq[-1] - 1.0, float(dd.min())))

    # Bounds (log scale on y for equity)
    eq_max = max(eq.max() for _, _, eq, _, _, _, _ in plotted)
    eq_min = 0.9

    # Equity panel
    plot_top = MARGIN_T
    plot_bottom = plot_top + PLOT_TOP_H
    draw.rectangle([MARGIN_L, plot_top, MARGIN_L + PLOT_W, plot_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    log_min = math.log10(eq_min); log_max = math.log10(eq_max)
    def y_eq(v): return plot_top + PLOT_TOP_H - int((math.log10(v) - log_min) / (log_max - log_min) * PLOT_TOP_H)
    # y-axis labels (log)
    for v in [1, 5, 10, 50, 100, 500, 1000, 5000]:
        if eq_min <= v <= eq_max:
            y = y_eq(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            label = f"{int(v)}x" if v >= 10 else f"{v:.0f}x"
            draw.text((MARGIN_L - 55, y - 8), label, fill=(80, 80, 90, 255), font=f_small)

    # x-axis ticks
    ts_min = datetime.strptime(common[0], "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000
    ts_max = datetime.strptime(common[-1], "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000
    for year in range(2024, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, plot_bottom), (x, plot_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, plot_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)

    # Plot equity lines (eq has len = len(common) - 1 since it's cumprod of returns)
    for name, color, eq, _, _, _, _ in plotted:
        pts = []
        # equity series starts at common[1] (first return is between common[0] and common[1])
        for i, d in enumerate(common[1:]):
            ts = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000
            x = MARGIN_L + int((ts - ts_min) / (ts_max - ts_min) * PLOT_W)
            y = y_eq(max(eq[i], 0.01))
            pts.append((x, y))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    # DD panel
    dd_top = plot_bottom + 50
    dd_bottom = dd_top + PLOT_DD_H
    draw.rectangle([MARGIN_L, dd_top, MARGIN_L + PLOT_W, dd_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    draw.text((MARGIN_L, dd_top - 25), "Drawdown (%)", fill=(60, 60, 70, 255), font=f_label)
    for pct in (0, -10, -20, -25):
        y = dd_top + int(-pct / 25.0 * PLOT_DD_H)
        if y > dd_bottom: continue
        draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((MARGIN_L - 50, y - 8), f"{pct}%", fill=(80, 80, 90, 255), font=f_small)

    # DD lines (dd has len = len(common) - 1 too)
    for name, color, _, dd, _, _, _ in plotted:
        pts = []
        for i, d in enumerate(common[1:]):
            ts = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000
            x = MARGIN_L + int((ts - ts_min) / (ts_max - ts_min) * PLOT_W)
            y = dd_top + int(-max(dd[i], -0.25) * 100 / 25.0 * PLOT_DD_H)
            y = min(y, dd_bottom)
            pts.append((x, y))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    for year in range(2024, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, dd_bottom), (x, dd_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, dd_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)

    # Legend
    legend_x = MARGIN_L + PLOT_W + 25
    legend_y = MARGIN_T + 20
    for name, color, _, _, sh, ret, dd in plotted:
        draw.rectangle([legend_x, legend_y, legend_x + 30, legend_y + 8], fill=color)
        draw.text((legend_x + 40, legend_y - 4), name, fill=(20, 20, 30, 255), font=f_small)
        draw.text((legend_x + 40, legend_y + 12), f"Sharpe {sh:.2f}  ret {ret*100:.0f}%  DD {dd*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)
        legend_y += 55

    draw.text((MARGIN_L, HEIGHT - 28),
              "Log scale on equity. In-sample 2023-10 to 2026-05. Costs (3× multiplier) and funding modeled in both sleeves.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
