"""v4c_uni10 (sigma-relative + 3d + 7d triggers) — standalone + combined book at leverages."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np, polars as pl
from PIL import Image, ImageDraw, ImageFont

V4C_IS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d/long_native_equity.csv"
V4C_OOS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d_OOS/long_native_equity.csv"
SHORT_STITCHED = "/tmp/stitched_returns.csv"
STITCHED_OUT = "/tmp/stitched_v4c_uni10.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/v4c_uni10_and_combined.png")

WIDTH, HEIGHT = 1600, 1100
MARGIN_L, MARGIN_R = 100, 320
MARGIN_T, MARGIN_B = 80, 70
GAP = 80
TOP_H = (HEIGHT - MARGIN_T - MARGIN_B - GAP) // 2
BOT_H = TOP_H
PLOT_W = WIDTH - MARGIN_L - MARGIN_R


def _font(size, bold=False):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try: return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception: continue
    return ImageFont.load_default()


def daily(path):
    eq = pl.read_csv(path).sort("ts_ms")
    by_d = dict(zip(eq["date"].to_list(), eq["equity"].to_list()))
    dates = sorted(by_d.keys())
    start = datetime.strptime(dates[0], "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(dates[-1], "%Y-%m-%d").replace(tzinfo=UTC)
    days = (end - start).days + 1
    last = 1.0; out = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in by_d: last = by_d[d]
        out.append((d, last))
    return out


def to_returns(eq):
    rets, prev = [], eq[0][1]
    for d, v in eq[1:]:
        rets.append((d, (v - prev) / prev if prev != 0 else 0.0)); prev = v
    return rets


def main():
    is_eq = daily(V4C_IS); oos_eq = daily(V4C_OOS)
    is_start_dt = datetime.strptime(is_eq[0][0], "%Y-%m-%d").replace(tzinfo=UTC)
    oos_r = to_returns(oos_eq); is_r = to_returns(is_eq)
    combined = [(d, r) for d, r in oos_r if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) < is_start_dt]
    combined.extend(is_r)
    v4c_map = dict(combined)

    short_df = pl.read_csv(SHORT_STITCHED)
    rows = []
    for r in short_df.iter_rows(named=True):
        d = r["date"]
        rows.append({"date": d, "short_ret": r["short_ret"], "v4c_ret": v4c_map.get(d, 0.0)})
    pl.DataFrame(rows).write_csv(STITCHED_OUT)

    df = pl.read_csv(STITCHED_OUT)
    dates = df["date"].to_list()
    s_ret = np.array(df["short_ret"].to_list())
    v_ret = np.array(df["v4c_ret"].to_list())
    ts_list = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]
    is_boundary = datetime(2023, 8, 1, tzinfo=UTC).timestamp() * 1000

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(12)

    # standalone equity
    eq_v = np.cumprod(1.0 + v_ret)
    peaks_v = np.maximum.accumulate(eq_v); dd_v = eq_v / peaks_v - 1.0
    sh_v = float(v_ret.mean() / v_ret.std(ddof=1) * math.sqrt(365)) if v_ret.std(ddof=1) > 0 else 0.0
    is_mask = np.array([t >= is_boundary for t in ts_list])
    is_part = v_ret[is_mask]; oos_part = v_ret[~is_mask]
    sh_is = float(is_part.mean() / is_part.std(ddof=1) * math.sqrt(365)) if is_part.std(ddof=1) > 0 else 0.0
    sh_oos = float(oos_part.mean() / oos_part.std(ddof=1) * math.sqrt(365)) if oos_part.std(ddof=1) > 0 else 0.0
    ret_is = float(np.prod(1 + is_part) - 1)
    ret_oos = float(np.prod(1 + oos_part) - 1)

    # ===== PANEL 1 =====
    p1_top = MARGIN_T + 35; p1_bottom = p1_top + TOP_H
    draw.text((MARGIN_L, p1_top - 30),
              "v4c_uni10 standalone — FC + sigma-relative entry + 3d/7d triggers",
              fill=(20, 20, 30, 255), font=f_title)
    draw.rectangle([MARGIN_L, p1_top, MARGIN_L + PLOT_W, p1_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    eq_min1 = 0.85; eq_max1 = max(eq_v.max() * 1.05, 1.30)
    def y1(v): return p1_top + TOP_H - int((v - eq_min1) / (eq_max1 - eq_min1) * TOP_H)
    for vp in (-15, -10, -5, 0, 5, 10, 15, 20, 25, 30, 35):
        v = 1 + vp / 100
        if eq_min1 <= v <= eq_max1:
            y = y1(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 50, y - 8), f"+{vp}%" if vp >= 0 else f"{vp}%", fill=(80, 80, 90, 255), font=f_small)
    y0 = y1(1.0)
    draw.line([(MARGIN_L, y0), (MARGIN_L + PLOT_W, y0)], fill=(160, 160, 170, 255), width=1)
    for yr in range(2021, 2027):
        for m in (1, 7):
            t = datetime(yr, m, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= t <= ts_max:
                x = MARGIN_L + int((t - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, p1_bottom), (x, p1_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p1_bottom + 8), f"{yr}-{m:02d}", fill=(80, 80, 90, 255), font=f_small)
    bx = MARGIN_L + int((is_boundary - ts_min) / (ts_max - ts_min) * PLOT_W)
    for y in range(p1_top, p1_bottom, 6):
        draw.line([(bx, y), (bx, y + 3)], fill=(150, 150, 160, 255), width=1)
    draw.text((bx - 30, p1_top + 5), "← OOS | IS →", fill=(120, 120, 130, 255), font=f_small)

    color_is = (10, 68, 41, 255); color_oos = (165, 27, 27, 255)
    pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W), y1(max(eq_v[i], 0.5))) for i in range(len(eq_v))]
    for i in range(len(pts) - 1):
        c = color_is if ts_list[i] >= is_boundary else color_oos
        draw.line([pts[i], pts[i + 1]], fill=c, width=2)

    sx = MARGIN_L + PLOT_W + 25
    draw.text((sx, p1_top + 20), "v4c_uni10 (4y stitched)", fill=(20, 20, 30, 255), font=f_label)
    draw.text((sx, p1_top + 42), f"Stitched Sharpe: {sh_v:+.2f}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((sx, p1_top + 60), f"Stitched ret: {eq_v[-1]-1:+.2%}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((sx, p1_top + 78), f"Max DD: {dd_v.min():.2%}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((sx, p1_top + 96), f"Active days: {int((v_ret!=0).sum())} / {len(v_ret)}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((sx, p1_top + 120), "Split:", fill=(60, 60, 70, 255), font=f_label)
    draw.text((sx, p1_top + 140), f"OOS pre-2023: Sh {sh_oos:+.2f}  {ret_oos:+.2%}", fill=color_oos, font=f_small)
    draw.text((sx, p1_top + 158), f"IS 2023-26:   Sh {sh_is:+.2f}  {ret_is:+.2%}", fill=color_is, font=f_small)
    draw.text((sx, p1_top + 186), "What's new vs v3a:", fill=(60, 60, 70, 255), font=f_label)
    for j, line in enumerate([
        "• Entry threshold sigma-relative:",
        "  pump ≥ 2.5 × coin's 30d σ_daily",
        "  (per-coin scaling, looser for",
        "   low-vol coins)",
        "• Also fires on 3d cum pump",
        "  ≥ K × σ × sqrt(3)",
        "• Also fires on 7d cum pump",
        "  ≥ K × σ × sqrt(7)",
        "• All v3a winners kept:",
        "  ATR exits K=1.5/4.0, ATR cap 12%",
    ]):
        draw.text((sx, p1_top + 206 + j * 17), line, fill=(80, 80, 90, 255), font=f_small)

    # ===== PANEL 2: combined book =====
    p2_top = p1_bottom + GAP; p2_bottom = p2_top + BOT_H
    draw.text((MARGIN_L, p2_top - 30),
              "Combined book: short × v4c_uni10 at varying leverage",
              fill=(20, 20, 30, 255), font=f_title)

    SERIES = [
        ("Short only (1×)", 1.0, 0.0, (165, 27, 27, 255)),
        ("Short + v4c 1× (2× gross)", 1.0, 1.0, (212, 102, 26, 255)),
        ("Short + v4c 5× (6× gross)", 1.0, 5.0, (10, 68, 41, 255)),
        ("Short + v4c 10× (11× gross)", 1.0, 10.0, (44, 110, 161, 255)),
    ]

    plotted = []
    for name, ws, wl, color in SERIES:
        port = ws * s_ret + wl * v_ret
        eq = np.cumprod(1.0 + port)
        peaks = np.maximum.accumulate(eq); dd = eq / peaks - 1.0
        sh = float(port.mean() / port.std(ddof=1) * math.sqrt(365))
        plotted.append((name, color, eq, dd, sh, eq[-1] - 1.0, float(dd.min())))

    # Tag peak-Sharpe series
    best = max(plotted, key=lambda x: x[4])
    plotted = [(n + (" ← peak Sharpe" if n == best[0] and "Short only" not in n else ""), c, eq, dd, sh, r, ddmin)
               for (n, c, eq, dd, sh, r, ddmin) in plotted]

    eq_max2 = max(eq.max() for _, _, eq, _, _, _, _ in plotted); eq_min2 = 0.9
    lmin = math.log10(eq_min2); lmax = math.log10(eq_max2)
    def y2(v): return p2_top + BOT_H - int((math.log10(v) - lmin) / (lmax - lmin) * BOT_H)

    draw.rectangle([MARGIN_L, p2_top, MARGIN_L + PLOT_W, p2_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    for v in [1, 5, 10, 50, 100, 500, 1000, 5000]:
        if eq_min2 <= v <= eq_max2:
            y = y2(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 55, y - 8), f"{int(v)}x", fill=(80, 80, 90, 255), font=f_small)
    for yr in range(2021, 2027):
        for m in (1, 7):
            t = datetime(yr, m, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= t <= ts_max:
                x = MARGIN_L + int((t - ts_min) / (ts_max - ts_min) * PLOT_W)
                draw.line([(x, p2_bottom), (x, p2_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p2_bottom + 8), f"{yr}-{m:02d}", fill=(80, 80, 90, 255), font=f_small)
    bx2 = MARGIN_L + int((is_boundary - ts_min) / (ts_max - ts_min) * PLOT_W)
    for y in range(p2_top, p2_bottom, 6):
        draw.line([(bx2, y), (bx2, y + 3)], fill=(150, 150, 160, 255), width=1)
    for name, color, eq, _, _, _, _ in plotted:
        pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W), y2(max(eq[i], 0.01))) for i in range(len(eq))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    lx = MARGIN_L + PLOT_W + 25; ly = p2_top + 20
    for name, color, _, _, sh, ret, dd in plotted:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=color)
        draw.text((lx + 38, ly - 4), name, fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 38, ly + 12), f"Sharpe {sh:.2f}  ret {ret*100:,.0f}%", fill=(80, 80, 90, 255), font=f_small)
        draw.text((lx + 38, ly + 26), f"max DD {dd*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)
        ly += 55
    corr = float(np.corrcoef(s_ret, v_ret)[0, 1])
    draw.text((lx, ly + 10), f"Correlation: {corr:+.4f}", fill=(60, 60, 70, 255), font=f_label)
    draw.text((lx, ly + 32), "vs v3a_uni10:", fill=(60, 60, 70, 255), font=f_small)
    draw.text((lx, ly + 48), f"  +43 trades (43→{int((v_ret!=0).sum())})", fill=(80, 80, 90, 255), font=f_small)
    draw.text((lx, ly + 64), f"  Sharpe +1.29 → +{sh_v:.2f}", fill=(80, 80, 90, 255), font=f_small)
    draw.text((lx, ly + 80), f"  Ret +17.2% → {eq_v[-1]-1:+.1%}", fill=(80, 80, 90, 255), font=f_small)

    draw.text((MARGIN_L, HEIGHT - 22),
              "Sigma-relative entry: pump must exceed 2.5 × coin's 30d daily σ (not fixed 15%). 3d + 7d triggers OR-d with 1d. Captures slower-momentum moves the fixed threshold misses.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
