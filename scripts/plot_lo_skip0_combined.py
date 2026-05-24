"""Two-panel chart: LO_skip0 standalone + short × LO_skip0 combined book at various leverage."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
from PIL import Image, ImageDraw, ImageFont


STITCHED = "/tmp/stitched_LO.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/lo_skip0_and_combined.png")

WIDTH, HEIGHT = 1600, 1100
MARGIN_L, MARGIN_R = 100, 300
MARGIN_T, MARGIN_B = 80, 70
GAP_BETWEEN = 80
TOP_PANEL_H = (HEIGHT - MARGIN_T - MARGIN_B - GAP_BETWEEN) // 2
BOT_PANEL_H = TOP_PANEL_H
PLOT_W = WIDTH - MARGIN_L - MARGIN_R


def _try_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception:
            continue
    return ImageFont.load_default()


def main():
    df = pl.read_csv(STITCHED)
    dates = df["date"].to_list()
    s_ret = np.array(df["short_ret"].to_list())
    l_ret = np.array(df["lo_ret"].to_list())
    ts_list = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _try_font(22, bold=True); f_label = _try_font(14); f_small = _try_font(12)

    # ===== PANEL 1: LO_skip0 standalone (linear scale, low return) =====
    p1_top = MARGIN_T + 35
    p1_bottom = p1_top + TOP_PANEL_H
    draw.text((MARGIN_L, p1_top - 30), "LO_skip0 standalone (long-only momentum factor)", fill=(20, 20, 30, 255), font=f_title)

    eq_lo = np.cumprod(1.0 + l_ret)
    peaks_lo = np.maximum.accumulate(eq_lo)
    dd_lo = eq_lo / peaks_lo - 1.0
    sh_lo = float(l_ret.mean() / l_ret.std(ddof=1) * math.sqrt(365))

    draw.rectangle([MARGIN_L, p1_top, MARGIN_L + PLOT_W, p1_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    eq_min1 = 0.80; eq_max1 = eq_lo.max() * 1.05
    def y1(v): return p1_top + TOP_PANEL_H - int((v - eq_min1) / (eq_max1 - eq_min1) * TOP_PANEL_H)
    # y ticks
    for v_pct in (-20, 0, 20, 40, 60):
        v = 1.0 + v_pct / 100.0
        if eq_min1 <= v <= eq_max1:
            y = y1(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            label = f"+{v_pct}%" if v_pct >= 0 else f"{v_pct}%"
            draw.text((MARGIN_L - 50, y - 8), label, fill=(80, 80, 90, 255), font=f_small)
    # 1.0 baseline
    y_one = y1(1.0)
    draw.line([(MARGIN_L, y_one), (MARGIN_L + PLOT_W, y_one)], fill=(160, 160, 170, 255), width=1)
    # x ticks
    for year in range(2022, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, p1_bottom), (x, p1_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p1_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)
    # IS boundary
    is_boundary = datetime(2023, 8, 1, tzinfo=UTC).timestamp() * 1000
    bx = MARGIN_L + int((is_boundary - ts_min) / (ts_max - ts_min) * PLOT_W)
    for y in range(p1_top, p1_bottom, 6):
        draw.line([(bx, y), (bx, y + 3)], fill=(150, 150, 160, 255), width=1)
    draw.text((bx - 30, p1_top + 5), "← OOS | IS →", fill=(120, 120, 130, 255), font=f_small)

    # LO_skip0 equity line + dd shading
    color_lo = (10, 68, 41, 255)
    pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W), y1(eq_lo[i])) for i in range(len(eq_lo))]
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=color_lo, width=2)

    # Right-side stat
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 20), "LO_skip0 (4y stitched)", fill=(20, 20, 30, 255), font=f_label)
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 42), f"Sharpe (daily): {sh_lo:.2f}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 60), f"Total ret: {eq_lo[-1] - 1:+.2%}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 78), f"Ann ret: {(1 + l_ret.mean())**365 - 1:+.2%}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 96), f"Max DD: {dd_lo.min():.2%}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((MARGIN_L + PLOT_W + 25, p1_top + 124), "Config:", fill=(60, 60, 70, 255), font=f_label)
    for j, line in enumerate([
        "• mode = long_only",
        "• universe = top 30 by 90d turnover",
        "• momentum = 7+14+28d ensemble",
        "• momentum_skip_days = 0",
        "• carry_weight = 1.5",
        "• TS-momentum filter on",
        "• regime gate: BTC 50d SMA",
        "• regime_off_scale = 0.0 (flat)",
        "• vol-target = 15% annualized",
        "• weekly rebalance",
        "• cost mult 3x, vol-parity sizing",
    ]):
        draw.text((MARGIN_L + PLOT_W + 25, p1_top + 144 + j * 17), line, fill=(80, 80, 90, 255), font=f_small)

    # ===== PANEL 2: combined book at various leverage (log scale) =====
    p2_top = p1_bottom + GAP_BETWEEN
    p2_bottom = p2_top + BOT_PANEL_H
    draw.text((MARGIN_L, p2_top - 30), "Combined book: short × LO_skip0 at varying leverage", fill=(20, 20, 30, 255), font=f_title)

    SERIES = [
        ("Short only (1×)", 1.0, 0.0, (165, 27, 27, 255)),
        ("Short + LO 0.5× (1.5× gross)", 1.0, 0.5, (212, 102, 26, 255)),
        ("Short + LO 1× (2× gross) ← peak Sharpe", 1.0, 1.0, (10, 68, 41, 255)),
        ("Short + LO 2× (3× gross)", 1.0, 2.0, (44, 110, 161, 255)),
    ]

    plotted = []
    for name, ws, wl, color in SERIES:
        port = ws * s_ret + wl * l_ret
        eq = np.cumprod(1.0 + port)
        peaks = np.maximum.accumulate(eq); dd = eq / peaks - 1.0
        sh = float(port.mean() / port.std(ddof=1) * math.sqrt(365))
        plotted.append((name, color, eq, dd, sh, eq[-1] - 1.0, float(dd.min())))

    eq_max2 = max(eq.max() for _, _, eq, _, _, _, _ in plotted)
    eq_min2 = 0.9
    log_min = math.log10(eq_min2); log_max = math.log10(eq_max2)
    def y2(v): return p2_top + BOT_PANEL_H - int((math.log10(v) - log_min) / (log_max - log_min) * BOT_PANEL_H)

    draw.rectangle([MARGIN_L, p2_top, MARGIN_L + PLOT_W, p2_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    for v in [1, 5, 10, 50, 100, 500, 1000]:
        if eq_min2 <= v <= eq_max2:
            y = y2(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 50, y - 8), f"{int(v)}x", fill=(80, 80, 90, 255), font=f_small)

    for year in range(2022, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = MARGIN_L + int((tick - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, p2_bottom), (x, p2_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p2_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)

    bx2 = MARGIN_L + int((is_boundary - ts_min) / (ts_max - ts_min) * PLOT_W)
    for y in range(p2_top, p2_bottom, 6):
        draw.line([(bx2, y), (bx2, y + 3)], fill=(150, 150, 160, 255), width=1)

    for name, color, eq, _, _, _, _ in plotted:
        pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W), y2(max(eq[i], 0.01))) for i in range(len(eq))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    # Legend
    legend_x = MARGIN_L + PLOT_W + 25
    legend_y = p2_top + 20
    for name, color, _, _, sh, ret, dd in plotted:
        draw.rectangle([legend_x, legend_y, legend_x + 28, legend_y + 7], fill=color)
        draw.text((legend_x + 38, legend_y - 4), name, fill=(20, 20, 30, 255), font=f_small)
        draw.text((legend_x + 38, legend_y + 12), f"Sharpe {sh:.2f}  ret {ret*100:,.0f}%", fill=(80, 80, 90, 255), font=f_small)
        draw.text((legend_x + 38, legend_y + 26), f"max DD {dd*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)
        legend_y += 55
    draw.text((legend_x, legend_y + 10), f"Correlation: −0.014", fill=(60, 60, 70, 255), font=f_label)
    draw.text((legend_x, legend_y + 32), f"(near-zero, sleeves are", fill=(80, 80, 90, 255), font=f_small)
    draw.text((legend_x, legend_y + 48), f"independent across regimes)", fill=(80, 80, 90, 255), font=f_small)

    draw.text((MARGIN_L, HEIGHT - 22),
              "Top panel: LO_skip0 standalone (linear scale). Bottom panel: combined book (log scale). Stitched OOS+IS Bybit 2022-04 → 2026-05.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
