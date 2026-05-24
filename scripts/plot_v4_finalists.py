"""Side-by-side: uni10 baseline vs v4c (uni10 sigma+3d+7d) vs v4g (uni50 sigma+3d+noeth).

Top panel: standalone stitched 4y equity.
Bottom panel: combined book at each variant's peak Sharpe leverage.
"""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np, polars as pl
from PIL import Image, ImageDraw, ImageFont

SHORT_STITCHED = "/tmp/stitched_returns.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/v4_finalists_comparison.png")

WIDTH, HEIGHT = 1700, 1100
MARGIN_L, MARGIN_R = 100, 360
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


def stitch(is_path, oos_path):
    is_eq = daily(is_path); oos_eq = daily(oos_path)
    is_start = datetime.strptime(is_eq[0][0], "%Y-%m-%d").replace(tzinfo=UTC)
    oos_r = to_returns(oos_eq); is_r = to_returns(is_eq)
    combined = [(d, r) for d, r in oos_r if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) < is_start]
    combined.extend(is_r)
    return dict(combined)


CANDS = [
    ("uni10 baseline (fixed 8/25)", (165, 27, 27, 255), 5,
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_uni10_only_IS_canonical/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_uni10_only_OOS_bybit/long_native_equity.csv"),
    ("v4c_uni10 (sigma 2.5 + 3d + 7d)", (10, 68, 41, 255), 5,
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d_OOS/long_native_equity.csv"),
    ("v4g_uni50 (sigma 2.5 + 3d + noeth)", (44, 110, 161, 255), 3,
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v4g_uni50_sigma2.5_3d_noeth/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v4g_uni50_sigma2.5_3d_noeth_OOS/long_native_equity.csv"),
]


def main():
    short_df = pl.read_csv(SHORT_STITCHED)
    short_map = dict(zip(short_df["date"].to_list(), short_df["short_ret"].to_list()))
    short_dates = sorted(short_map.keys())
    short_arr = np.array([short_map[d] for d in short_dates])
    short_sh = float(short_arr.mean() / short_arr.std(ddof=1) * math.sqrt(365))
    ts_list = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in short_dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]
    is_boundary = datetime(2023, 8, 1, tzinfo=UTC).timestamp() * 1000

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(11)

    # Compute series
    series = []
    for name, color, lev, ip, op in CANDS:
        sleeve = stitch(ip, op)
        u = np.array([sleeve.get(d, 0.0) for d in short_dates])
        sh_alone = float(u.mean() / u.std(ddof=1) * math.sqrt(365)) if u.std(ddof=1) > 0 else 0.0
        eq_alone = np.cumprod(1 + u)
        is_mask = np.array([t >= is_boundary for t in ts_list])
        is_part, oos_part = u[is_mask], u[~is_mask]
        sh_is = float(is_part.mean() / is_part.std(ddof=1) * math.sqrt(365)) if is_part.std(ddof=1) > 0 else 0.0
        sh_oos = float(oos_part.mean() / oos_part.std(ddof=1) * math.sqrt(365)) if oos_part.std(ddof=1) > 0 else 0.0
        ret_is = float(np.prod(1 + is_part) - 1)
        ret_oos = float(np.prod(1 + oos_part) - 1)
        corr = float(np.corrcoef(short_arr, u)[0, 1])
        port = short_arr + lev * u
        eq_combo = np.cumprod(1 + port)
        sh_combo = float(port.mean() / port.std(ddof=1) * math.sqrt(365))
        peaks = np.maximum.accumulate(eq_combo); dd_combo = (eq_combo / peaks - 1).min()
        series.append({"name": name, "color": color, "lev": lev, "u": u, "eq_alone": eq_alone,
                       "sh_alone": sh_alone, "ret_alone": eq_alone[-1] - 1, "sh_is": sh_is, "sh_oos": sh_oos,
                       "ret_is": ret_is, "ret_oos": ret_oos, "corr": corr,
                       "eq_combo": eq_combo, "sh_combo": sh_combo, "dd_combo": dd_combo,
                       "active": int((u != 0).sum())})

    # ===== PANEL 1: standalone =====
    p1_top = MARGIN_T + 35; p1_bottom = p1_top + TOP_H
    draw.text((MARGIN_L, p1_top - 30),
              "FC finalists standalone — stitched OOS+IS Bybit (2022-04 → 2026-05)",
              fill=(20, 20, 30, 255), font=f_title)
    draw.rectangle([MARGIN_L, p1_top, MARGIN_L + PLOT_W, p1_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    eq_max1 = max(s["eq_alone"].max() for s in series) * 1.05
    eq_min1 = 0.85
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
    for s in series:
        pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W),
                y1(max(s["eq_alone"][i], 0.5))) for i in range(len(s["eq_alone"]))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=s["color"], width=2)

    # Right-side legend
    lx = MARGIN_L + PLOT_W + 25
    ly = p1_top + 10
    draw.text((lx, ly), "Standalone (4y stitched)", fill=(20, 20, 30, 255), font=f_label)
    ly += 24
    for s in series:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=s["color"])
        draw.text((lx + 36, ly - 4), s["name"], fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 36, ly + 11), f"Stitched Sh {s['sh_alone']:+.2f}  ret {s['ret_alone']*100:+.1f}%", fill=(80, 80, 90, 255), font=f_small)
        draw.text((lx + 36, ly + 25), f"IS Sh {s['sh_is']:+.2f} ({s['ret_is']*100:+.1f}%) | OOS Sh {s['sh_oos']:+.2f} ({s['ret_oos']*100:+.1f}%)",
                  fill=(80, 80, 90, 255), font=f_small)
        draw.text((lx + 36, ly + 39), f"active days {s['active']} / 1286", fill=(80, 80, 90, 255), font=f_small)
        ly += 60

    # ===== PANEL 2: combined book at each variant's best lev =====
    p2_top = p1_bottom + GAP; p2_bottom = p2_top + BOT_H
    draw.text((MARGIN_L, p2_top - 30),
              f"Combined book — short + each finalist at its peak-Sharpe leverage (log scale)",
              fill=(20, 20, 30, 255), font=f_title)
    draw.rectangle([MARGIN_L, p2_top, MARGIN_L + PLOT_W, p2_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)

    short_eq = np.cumprod(1 + short_arr)
    short_peaks = np.maximum.accumulate(short_eq); short_dd = (short_eq / short_peaks - 1).min()
    combos = [("Short only (1×)", (130, 130, 130, 255), short_eq, short_sh, short_eq[-1] - 1, short_dd)]
    for s in series:
        combos.append((f"+ {s['name']} {s['lev']}×", s["color"], s["eq_combo"], s["sh_combo"],
                       s["eq_combo"][-1] - 1, s["dd_combo"]))

    eq_max2 = max(c[2].max() for c in combos); eq_min2 = 0.9
    lmin = math.log10(eq_min2); lmax = math.log10(eq_max2)
    def y2(v): return p2_top + BOT_H - int((math.log10(v) - lmin) / (lmax - lmin) * BOT_H)
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
    for name, color, eq, sh, ret, dd in combos:
        pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W),
                y2(max(eq[i], 0.01))) for i in range(len(eq))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    lx = MARGIN_L + PLOT_W + 25; ly = p2_top + 10
    draw.text((lx, ly), "Combined (peak Sharpe lev)", fill=(20, 20, 30, 255), font=f_label)
    ly += 24
    for name, color, eq, sh, ret, dd in combos:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=color)
        draw.text((lx + 36, ly - 4), name, fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 36, ly + 11), f"Sh {sh:+.2f}  ret {ret*100:,.0f}%", fill=(80, 80, 90, 255), font=f_small)
        draw.text((lx + 36, ly + 25), f"max DD {dd*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)
        ly += 50

    draw.text((MARGIN_L, HEIGHT - 22),
              "v4c (sigma-relative entry + 3d/7d triggers, top-10): best Sharpe and best risk-adjusted. v4g (uni50 noeth): 3× more trades but worse Sharpe. Intraday triggers tested (v5) and rejected — added noise.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
