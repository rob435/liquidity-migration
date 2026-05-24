"""Render equity curve for the FC FOMO chase pattern (Sharpe 1.5 honest)."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
from PIL import Image, ImageDraw, ImageFont


EQUITY_PATH = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_15_5_hold3_tp40/long_native_equity.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/long_native_FC_equity.png")

WIDTH, HEIGHT = 1500, 800
MARGIN_L, MARGIN_R = 90, 50
MARGIN_T, MARGIN_B = 110, 90
PLOT_W = WIDTH - MARGIN_L - MARGIN_R
PLOT_TOP_H = (HEIGHT - MARGIN_T - MARGIN_B) * 5 // 7
PLOT_DD_H = (HEIGHT - MARGIN_T - MARGIN_B) - PLOT_TOP_H - 50


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


def main() -> None:
    df = pl.read_csv(EQUITY_PATH).sort("ts_ms")
    # Build daily equity series
    dates = sorted(set(df["date"].to_list()))
    eq_by_date = dict(zip(df["date"].to_list(), df["equity"].to_list()))
    from datetime import timedelta
    start = datetime.strptime(min(dates), "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(max(dates), "%Y-%m-%d").replace(tzinfo=UTC)
    days = (end - start).days + 1
    daily_eq = []
    last = 1.0
    ts_list = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in eq_by_date:
            last = eq_by_date[d]
        daily_eq.append(last)
        ts_list.append((start + timedelta(days=i)).timestamp() * 1000)
    daily_eq_arr = np.asarray(daily_eq, dtype=float)
    peak = np.maximum.accumulate(daily_eq_arr)
    dd_arr = daily_eq_arr / peak - 1.0
    # Daily Sharpe
    drs = np.diff(daily_eq_arr) / daily_eq_arr[:-1]
    sharpe_daily = float(drs.mean() / drs.std(ddof=1) * math.sqrt(365)) if drs.std(ddof=1) > 0 else 0.0
    total_ret = daily_eq_arr[-1] - 1.0
    max_dd = float(dd_arr.min())

    # Plot bounds
    eq_min = max(0.95, daily_eq_arr.min() * 0.99)
    eq_max = daily_eq_arr.max() * 1.02
    ts_min = ts_list[0]
    ts_max = ts_list[-1]

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    font_title = _try_font(22, bold=True)
    font_label = _try_font(15)
    font_small = _try_font(13)

    draw.text((MARGIN_L, 30), "Long-native FOMO chase (FC_15_5_hold3_tp40) — equity curve", fill=(20, 20, 30, 255), font=font_title)
    draw.text((MARGIN_L, 60), f"Bybit canonical 2023-08 → 2026-05 · honest daily-aligned Sharpe {sharpe_daily:.2f} · total return {total_ret:+.2%} · max DD {max_dd:+.2%}",
              fill=(80, 80, 90, 255), font=font_label)

    # Equity panel
    plot_top = MARGIN_T
    plot_bottom = plot_top + PLOT_TOP_H
    draw.rectangle([MARGIN_L, plot_top, MARGIN_L + PLOT_W, plot_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    # 1.0 baseline
    y_one = plot_top + PLOT_TOP_H - int((1.0 - eq_min) / (eq_max - eq_min) * PLOT_TOP_H)
    draw.line([(MARGIN_L, y_one), (MARGIN_L + PLOT_W, y_one)], fill=(160, 160, 170, 255), width=1)
    # y-axis labels
    for v_pct in (0, 5, 10, 15, 20):
        v = 1.0 + v_pct / 100.0
        if eq_min <= v <= eq_max:
            y = plot_top + PLOT_TOP_H - int((v - eq_min) / (eq_max - eq_min) * PLOT_TOP_H)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 60, y - 8), f"+{v_pct}%", fill=(80, 80, 90, 255), font=font_small)
    # x-axis ticks (months)
    for year in range(2023, 2027):
        for month in (1, 7):
            tick_ts = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick_ts <= ts_max:
                x = MARGIN_L + int((tick_ts - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, plot_bottom), (x, plot_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, plot_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=font_small)

    # Plot equity line
    color = (10, 68, 41, 255)
    pts = []
    for i, ts in enumerate(ts_list):
        x = MARGIN_L + int((ts - ts_min) / (ts_max - ts_min) * PLOT_W)
        y = plot_top + PLOT_TOP_H - int((daily_eq_arr[i] - eq_min) / (eq_max - eq_min) * PLOT_TOP_H)
        pts.append((x, y))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=color, width=3)

    # DD panel
    dd_top = plot_bottom + 50
    dd_bottom = dd_top + PLOT_DD_H
    draw.rectangle([MARGIN_L, dd_top, MARGIN_L + PLOT_W, dd_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    draw.text((MARGIN_L, dd_top - 25), "Drawdown (%)", fill=(60, 60, 70, 255), font=font_label)
    y_zero = dd_top
    for pct in (0, -1, -2, -3, -4, -5):
        y = dd_top + int(-pct / 5.0 * PLOT_DD_H)
        draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((MARGIN_L - 50, y - 8), f"{pct}%", fill=(80, 80, 90, 255), font=font_small)
    # DD fill
    fill_color = (color[0], color[1], color[2], 80)
    for i in range(len(pts) - 1):
        x1, y1 = pts[i][0], dd_top + int(-dd_arr[i] * 100 / 5.0 * PLOT_DD_H)
        x2, y2 = pts[i + 1][0], dd_top + int(-dd_arr[i + 1] * 100 / 5.0 * PLOT_DD_H)
        y1 = min(y1, dd_bottom); y2 = min(y2, dd_bottom)
        draw.polygon([(x1, y1), (x2, y2), (x2, y_zero), (x1, y_zero)], fill=fill_color)
        draw.line([(x1, y1), (x2, y2)], fill=color, width=2)

    # x-axis on DD
    for year in range(2023, 2027):
        for month in (1, 7):
            tick_ts = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick_ts <= ts_max:
                x = MARGIN_L + int((tick_ts - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, dd_bottom), (x, dd_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, dd_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=font_small)

    # Footer
    draw.text((MARGIN_L, HEIGHT - 28),
              f"38 trades · pattern: 1d return ≥+15% + top 5 volume rank + close-loc ≥0.7 + BTC>30d SMA + ETH>30d SMA · stop 8% / TP 40% / hold ≤3d / cost 3× / cooldown 7d. NOT OOS-survived.",
              fill=(100, 100, 110, 255), font=font_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
