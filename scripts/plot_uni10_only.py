"""Equity curve for uni10_only FC pattern — stitched IS+OOS Bybit."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np
import polars as pl
from PIL import Image, ImageDraw, ImageFont

IS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_uni10_only_IS_canonical/long_native_equity.csv"
OOS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_uni10_only_OOS_bybit/long_native_equity.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/uni10_only_equity.png")

WIDTH, HEIGHT = 1500, 800
MARGIN_L, MARGIN_R = 100, 60
MARGIN_T, MARGIN_B = 110, 90
PLOT_W = WIDTH - MARGIN_L - MARGIN_R
PLOT_TOP_H = (HEIGHT - MARGIN_T - MARGIN_B) * 5 // 7
PLOT_DD_H = (HEIGHT - MARGIN_T - MARGIN_B) - PLOT_TOP_H - 50


def _font(size, bold=False):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception:
            continue
    return ImageFont.load_default()


def to_daily(path):
    eq = pl.read_csv(path).sort("ts_ms")
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


def rets(daily):
    arr = np.array([v for _, v in daily])
    arr = arr / arr[0]
    return list(zip([d for d, _ in daily][1:], np.diff(arr) / arr[:-1]))


def main():
    oos_r = rets(to_daily(OOS))
    is_r = rets(to_daily(IS))
    is_start = is_r[0][0]
    is_start_dt = datetime.strptime(is_start, "%Y-%m-%d").replace(tzinfo=UTC)
    combined = [(d, r) for d, r in oos_r if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) < is_start_dt]
    combined.extend(is_r)
    dates = [d for d, _ in combined]
    ret_arr = np.array([r for _, r in combined])
    eq = np.cumprod(1 + ret_arr)
    peaks = np.maximum.accumulate(eq)
    dd = eq / peaks - 1.0
    full_sh = float(ret_arr.mean() / ret_arr.std(ddof=1) * math.sqrt(365)) if ret_arr.std(ddof=1) > 0 else 0
    # Split sharpes
    is_arr = np.array([r for d, r in combined if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) >= is_start_dt])
    oos_arr = np.array([r for d, r in combined if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) < is_start_dt])
    is_sh = float(is_arr.mean() / is_arr.std(ddof=1) * math.sqrt(365)) if is_arr.std(ddof=1) > 0 else 0
    oos_sh = float(oos_arr.mean() / oos_arr.std(ddof=1) * math.sqrt(365)) if oos_arr.std(ddof=1) > 0 else 0
    is_eq = np.cumprod(1 + is_arr)[-1] - 1
    oos_eq = np.cumprod(1 + oos_arr)[-1] - 1

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(12)
    draw.text((MARGIN_L, 30), "uni10_only FC FOMO chase — stitched OOS + IS Bybit", fill=(20, 20, 30, 255), font=f_title)
    draw.text((MARGIN_L, 60), f"Top-10 universe restriction. Standalone honest. OOS Sharpe {oos_sh:+.2f}, ret {oos_eq:+.2%} · IS Sharpe {is_sh:+.2f}, ret {is_eq:+.2%}",
              fill=(80, 80, 90, 255), font=f_label)

    # Equity panel — linear
    plot_top = MARGIN_T
    plot_bottom = plot_top + PLOT_TOP_H
    draw.rectangle([MARGIN_L, plot_top, MARGIN_L + PLOT_W, plot_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    eq_min = 0.85; eq_max = max(eq.max() * 1.05, 1.2)
    def y_eq(v): return plot_top + PLOT_TOP_H - int((v - eq_min) / (eq_max - eq_min) * PLOT_TOP_H)
    for v_pct in (-15, -10, -5, 0, 5, 10, 15, 20, 25):
        v = 1.0 + v_pct / 100.0
        if eq_min <= v <= eq_max:
            y = y_eq(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            label = f"+{v_pct}%" if v_pct >= 0 else f"{v_pct}%"
            draw.text((MARGIN_L - 55, y - 8), label, fill=(80, 80, 90, 255), font=f_small)
    y_one = y_eq(1.0)
    draw.line([(MARGIN_L, y_one), (MARGIN_L + PLOT_W, y_one)], fill=(160, 160, 170, 255), width=1)

    ts_list = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]
    for year in range(2021, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, plot_bottom), (x, plot_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, plot_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)

    # IS/OOS boundary
    bts = is_start_dt.timestamp() * 1000
    if ts_min <= bts <= ts_max:
        bx = MARGIN_L + int((bts - ts_min) / (ts_max - ts_min) * PLOT_W)
        for y in range(plot_top, plot_bottom, 6):
            draw.line([(bx, y), (bx, y + 3)], fill=(150, 150, 160, 255), width=1)
        draw.text((bx - 30, plot_top + 5), "← OOS | IS →", fill=(120, 120, 130, 255), font=f_small)

    color = (10, 68, 41, 255); color_oos = (165, 27, 27, 255)
    pts = []
    for i, ts in enumerate(ts_list):
        x = MARGIN_L + int((ts - ts_min) / (ts_max - ts_min) * PLOT_W)
        y = y_eq(max(eq[i], 0.5))
        pts.append((x, y))
    for i in range(len(pts) - 1):
        c = color if ts_list[i] >= bts else color_oos
        draw.line([pts[i], pts[i + 1]], fill=c, width=2)

    # DD panel
    dd_top = plot_bottom + 50
    dd_bottom = dd_top + PLOT_DD_H
    draw.rectangle([MARGIN_L, dd_top, MARGIN_L + PLOT_W, dd_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    draw.text((MARGIN_L, dd_top - 25), "Drawdown (%, clamped at -20%)", fill=(60, 60, 70, 255), font=f_label)
    for pct in (0, -5, -10, -15, -20):
        y = dd_top + int(-pct / 20.0 * PLOT_DD_H)
        draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((MARGIN_L - 50, y - 8), f"{pct}%", fill=(80, 80, 90, 255), font=f_small)
    for i, ts in enumerate(ts_list[:-1]):
        x1 = MARGIN_L + int((ts - ts_min) / (ts_max - ts_min) * PLOT_W)
        x2 = MARGIN_L + int((ts_list[i + 1] - ts_min) / (ts_max - ts_min) * PLOT_W)
        y1 = dd_top + int(-max(dd[i], -0.20) * 100 / 20.0 * PLOT_DD_H)
        y2 = dd_top + int(-max(dd[i + 1], -0.20) * 100 / 20.0 * PLOT_DD_H)
        y1 = min(y1, dd_bottom); y2 = min(y2, dd_bottom)
        c = color if ts >= bts else color_oos
        draw.line([(x1, y1), (x2, y2)], fill=c, width=2)
    for year in range(2021, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, dd_bottom), (x, dd_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, dd_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)

    draw.text((MARGIN_L, HEIGHT - 28),
              "Red = OOS pre-2023 (33 trades, fails). Green = IS 2023-2026 (46 trades, honest Sharpe 1.73 standalone, +16% return, -1.5% DD). Costs 3x, vol-parity sizing.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
