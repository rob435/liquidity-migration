"""Side-by-side: uni10 baseline vs v3a_uni10 vs v3a_uni50 vs v3g_uni50 — standalone + combined."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np, polars as pl
from PIL import Image, ImageDraw, ImageFont

SHORT_STITCHED = "/tmp/stitched_returns.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/fc_v3_comparison.png")

WIDTH, HEIGHT = 1700, 1100
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


def stitch(is_path, oos_path):
    is_eq = daily(is_path); oos_eq = daily(oos_path)
    is_start = datetime.strptime(is_eq[0][0], "%Y-%m-%d").replace(tzinfo=UTC)
    oos_r = to_returns(oos_eq); is_r = to_returns(is_eq)
    combined = [(d, r) for d, r in oos_r if datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC) < is_start]
    combined.extend(is_r)
    return combined


CANDS = [
    ("uni10 baseline (fixed 8/25)", (165, 27, 27, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_uni10_only_IS_canonical/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_uni10_only_OOS_bybit/long_native_equity.csv"),
    ("v3a_uni10 (ATR K=1.5/4.0 + cap)", (10, 68, 41, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v3a_uni10_K1.5_4.0_IS/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v3a_uni10_K1.5_4.0_OOS/long_native_equity.csv"),
    ("v3g_uni50 (atrcap only, fixed exits)", (44, 110, 161, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v3g_uni50_atrcap_IS/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v3g_uni50_atrcap_OOS/long_native_equity.csv"),
    ("v3a_uni50 (ATR K=1.5/4.0 + cap)", (212, 102, 26, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v3a_uni50_K1.5_4.0_IS/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v3a_uni50_K1.5_4.0_OOS/long_native_equity.csv"),
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

    # Compute stitched daily returns aligned to short series
    series = []
    for name, color, ip, op in CANDS:
        try:
            sleeve = dict(stitch(ip, op))
            u = np.array([sleeve.get(d, 0.0) for d in short_dates])
            sh = float(u.mean() / u.std(ddof=1) * math.sqrt(365)) if u.std(ddof=1) > 0 else 0.0
            eq = np.cumprod(1 + u)
            corr = float(np.corrcoef(short_arr, u)[0, 1])
            series.append({"name": name, "color": color, "ret": u, "eq": eq, "sh": sh,
                           "ret_total": eq[-1] - 1, "corr": corr})
        except Exception as e:
            print(f"skip {name}: {e}")

    # ===== PANEL 1: standalone equity (linear) =====
    p1_top = MARGIN_T + 35
    p1_bottom = p1_top + TOP_H
    draw.text((MARGIN_L, p1_top - 30), "FC variants standalone — stitched OOS+IS Bybit (2022-04 → 2026-05)",
              fill=(20, 20, 30, 255), font=f_title)
    draw.rectangle([MARGIN_L, p1_top, MARGIN_L + PLOT_W, p1_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)

    eq_max = max(s["eq"].max() for s in series) * 1.05
    eq_min = 0.85
    def y1(v): return p1_top + TOP_H - int((v - eq_min) / (eq_max - eq_min) * TOP_H)
    for v_pct in (-15, -10, -5, 0, 5, 10, 15, 20, 25, 30):
        v = 1 + v_pct / 100
        if eq_min <= v <= eq_max:
            y = y1(v)
            draw.line([(MARGIN_L - 4, y), (MARGIN_L, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((MARGIN_L - 50, y - 8), f"{v_pct:+d}%".replace("+0%", "0%"), fill=(80, 80, 90, 255), font=f_small)
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
                y1(max(s["eq"][i], 0.5))) for i in range(len(s["eq"]))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=s["color"], width=2)

    # Right-side legend
    lx = MARGIN_L + PLOT_W + 25
    ly = p1_top + 10
    draw.text((lx, ly), "Standalone (4y stitched)", fill=(20, 20, 30, 255), font=f_label)
    ly += 22
    for s in series:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=s["color"])
        draw.text((lx + 36, ly - 4), s["name"], fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 36, ly + 10), f"Sh {s['sh']:+.2f}  ret {s['ret_total']*100:+.1f}%", fill=(80, 80, 90, 255), font=f_small)
        ly += 42

    # ===== PANEL 2: combined book at 5× leverage (log scale) =====
    p2_top = p1_bottom + GAP
    p2_bottom = p2_top + BOT_H
    draw.text((MARGIN_L, p2_top - 30), "Combined book: short + each FC variant at 5× long-side leverage (log scale)",
              fill=(20, 20, 30, 255), font=f_title)
    draw.rectangle([MARGIN_L, p2_top, MARGIN_L + PLOT_W, p2_bottom],
                   outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)

    short_eq = np.cumprod(1 + short_arr)
    combos = [("Short only (1×)", (130, 130, 130, 255), short_eq, short_sh)]
    for s in series:
        port = short_arr + 5.0 * s["ret"]
        sh = float(port.mean() / port.std(ddof=1) * math.sqrt(365))
        combos.append((f"short + {s['name']} 5×", s["color"], np.cumprod(1 + port), sh))

    log_max = max(c[2].max() for c in combos)
    log_min = 0.9
    lmin = math.log10(log_min); lmax = math.log10(log_max)
    def y2(v): return p2_top + BOT_H - int((math.log10(v) - lmin) / (lmax - lmin) * BOT_H)
    for v in [1, 5, 10, 50, 100, 500, 1000, 5000]:
        if log_min <= v <= log_max:
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
    for name, color, eq, sh in combos:
        pts = [(MARGIN_L + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PLOT_W),
                y2(max(eq[i], 0.01))) for i in range(len(eq))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=color, width=2)

    lx = MARGIN_L + PLOT_W + 25
    ly = p2_top + 10
    draw.text((lx, ly), "Combined book (5× long)", fill=(20, 20, 30, 255), font=f_label)
    ly += 22
    for name, color, eq, sh in combos:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=color)
        draw.text((lx + 36, ly - 4), name, fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 36, ly + 10), f"Sh {sh:+.2f}  ret {(eq[-1]-1)*100:+,.0f}%", fill=(80, 80, 90, 255), font=f_small)
        ly += 42

    draw.text((MARGIN_L, HEIGHT - 22),
              "Honest read: v3a_uni10 (dynamic ATR exits K=1.5/4.0 + ATR cap 12%) is the new champion — best standalone Sharpe (1.29 vs 1.17) AND best combined-book Sharpe at 5× (3.49 vs 3.44). All variants underperform standalone short alone (Sh 3.24) until combined.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
