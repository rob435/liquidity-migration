"""Overlay v11a long sleeve at 10× leverage on top of the new Sharpe-4 short (q50-h2 config)."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np
import polars as pl
from PIL import Image, ImageDraw, ImageFont


SHORT_NEW_IS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/volume_event_THIS_SESSION_q50_h2/volume_event_best_equity.csv"
V11A_IS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v11a_retrace1pct_6h_fallthru/long_native_equity.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/sharpe4_short_plus_v11a_10x.png")

WIDTH, HEIGHT = 1700, 1000
ML, MR, MT, MB = 100, 360, 100, 90
GAP = 80
TOP_H = (HEIGHT - MT - MB - GAP) * 5 // 7
BOT_H = (HEIGHT - MT - MB - GAP) - TOP_H
PW = WIDTH - ML - MR


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
    out = []; last = 1.0
    for i in range((end - start).days + 1):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in by_d: last = by_d[d]
        out.append((d, last))
    return out


def main():
    # Build a common date axis (intersect IS windows)
    s_daily = daily(SHORT_NEW_IS)
    v_daily = daily(V11A_IS)
    s_map = dict(s_daily); v_map = dict(v_daily)
    common = sorted(set(s_map.keys()) & set(v_map.keys()))
    s_eq = np.array([s_map[d] for d in common]); s_eq = s_eq / s_eq[0]
    v_eq = np.array([v_map[d] for d in common]); v_eq = v_eq / v_eq[0]
    s_ret = np.diff(s_eq) / s_eq[:-1]
    v_ret = np.diff(v_eq) / v_eq[:-1]
    dates = common[1:]
    ts_list = [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in dates]

    # Series: short alone, short + v11a at 1x, 5x, 10x
    SERIES = [
        ("Short only (1×) — q50-h2 Sharpe-4 config", 1.0, 0.0, (165, 27, 27, 255)),
        ("Short + v11a 1× (2× gross)",  1.0, 1.0,  (212, 102, 26, 255)),
        ("Short + v11a 5× (6× gross)",  1.0, 5.0,  (10, 68, 41, 255)),
        ("Short + v11a 10× (11× gross) ← user-requested", 1.0, 10.0, (44, 110, 161, 255)),
    ]
    plotted = []
    for name, ws, wl, color in SERIES:
        port = ws * s_ret + wl * v_ret
        eq = np.cumprod(1 + port)
        peaks = np.maximum.accumulate(eq); dd = eq / peaks - 1.0
        sh = float(port.mean() / port.std(ddof=1) * math.sqrt(365)) if port.std(ddof=1) > 0 else 0.0
        plotted.append({"name": name, "color": color, "eq": eq, "dd": dd, "sh": sh,
                        "ret": eq[-1] - 1, "max_dd": float(dd.min())})

    corr = float(np.corrcoef(s_ret, v_ret)[0, 1])

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(12)

    draw.text((ML, 25), "Combined book — Sharpe-4 short (q50-h2) + v11a long sleeve at varying leverage",
              fill=(20, 20, 30, 255), font=f_title)
    draw.text((ML, 55), f"IS Bybit 2023-05 → 2026-05 ({len(dates)} days). Correlation: {corr:+.4f}. The 10× line is the user's requested overlay.",
              fill=(80, 80, 90, 255), font=f_label)

    # ===== TOP: equity (log scale) =====
    p1_top = MT
    p1_bottom = p1_top + TOP_H
    draw.rectangle([ML, p1_top, ML + PW, p1_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    ts_min, ts_max = ts_list[0], ts_list[-1]
    eq_max = max(s["eq"].max() for s in plotted) * 1.05
    eq_min = 0.9
    lmin = math.log10(eq_min); lmax = math.log10(eq_max)
    def y_eq(v): return p1_top + TOP_H - int((math.log10(max(v, eq_min)) - lmin) / (lmax - lmin) * TOP_H)
    for v in [1, 5, 10, 50, 100, 500, 1000, 5000, 10000]:
        if eq_min <= v <= eq_max:
            y = y_eq(v)
            draw.line([(ML - 4, y), (ML, y)], fill=(120, 120, 130, 255), width=1)
            draw.text((ML - 55, y - 8), f"{int(v)}x", fill=(80, 80, 90, 255), font=f_small)
    for year in range(2023, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = ML + int((tick - ts_min) / (ts_max - ts_min) * PW)
                draw.line([(x, p1_bottom), (x, p1_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p1_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)
    for s in plotted:
        pts = [(ML + int((ts_list[i] - ts_min) / (ts_max - ts_min) * PW), y_eq(s["eq"][i])) for i in range(len(s["eq"]))]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=s["color"], width=2)

    # ===== BOTTOM: drawdown =====
    p2_top = p1_bottom + GAP
    p2_bottom = p2_top + BOT_H
    draw.rectangle([ML, p2_top, ML + PW, p2_bottom], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    draw.text((ML, p2_top - 25), "Drawdown (%)", fill=(60, 60, 70, 255), font=f_label)
    for pct in (0, -5, -10, -15, -20, -25):
        y = p2_top + int(-pct / 25.0 * BOT_H)
        if y > p2_bottom: continue
        draw.line([(ML - 4, y), (ML, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((ML - 55, y - 8), f"{pct}%", fill=(80, 80, 90, 255), font=f_small)
    for year in range(2023, 2027):
        for month in (1, 7):
            tick = datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= tick <= ts_max:
                x = ML + int((tick - ts_min) / (ts_max - ts_min) * PW)
                draw.line([(x, p2_bottom), (x, p2_bottom + 4)], fill=(120, 120, 130, 255), width=1)
                draw.text((x - 22, p2_bottom + 8), f"{year}-{month:02d}", fill=(80, 80, 90, 255), font=f_small)
    for s in plotted:
        pts = []
        for i, ts in enumerate(ts_list):
            x = ML + int((ts - ts_min) / (ts_max - ts_min) * PW)
            y = p2_top + int(-max(s["dd"][i], -0.25) * 100 / 25.0 * BOT_H)
            y = min(y, p2_bottom)
            pts.append((x, y))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=s["color"], width=2)

    # Legend
    lx = ML + PW + 25; ly = MT + 10
    draw.text((lx, ly), "Standalone metrics (IS, 3y)", fill=(20, 20, 30, 255), font=f_label); ly += 22
    draw.text((lx, ly), f"Short q50-h2:  Sharpe {plotted[0]['sh']:+.2f}", fill=(80, 80, 90, 255), font=f_small); ly += 14
    sh_v = float(v_ret.mean() / v_ret.std(ddof=1) * math.sqrt(365)) if v_ret.std(ddof=1) > 0 else 0
    draw.text((lx, ly), f"v11a sniper:   Sharpe {sh_v:+.2f}", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), f"Correlation:   {corr:+.4f}", fill=(80, 80, 90, 255), font=f_small); ly += 26

    draw.text((lx, ly), "Combined-book results", fill=(20, 20, 30, 255), font=f_label); ly += 24
    for s in plotted:
        draw.rectangle([lx, ly, lx + 28, ly + 7], fill=s["color"])
        draw.text((lx + 38, ly - 4), s["name"], fill=(20, 20, 30, 255), font=f_small)
        draw.text((lx + 38, ly + 11), f"Sharpe {s['sh']:+.2f}  ret {s['ret']*100:,.0f}%", fill=(80, 80, 90, 255), font=f_small)
        draw.text((lx + 38, ly + 25), f"max DD {s['max_dd']*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)
        ly += 55

    ly += 10
    best = max(plotted, key=lambda s: s["sh"])
    draw.text((lx, ly), "Peak Sharpe:", fill=(20, 20, 30, 255), font=f_label); ly += 18
    draw.text((lx, ly), f"  {best['name'][:30]}", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), f"  Sharpe {best['sh']:+.2f}", fill=(10, 68, 41, 255), font=f_label); ly += 22

    draw.text((lx, ly), "At 10× v11a (user-requested):", fill=(20, 20, 30, 255), font=f_label); ly += 18
    p = plotted[3]
    draw.text((lx, ly), f"  Sharpe {p['sh']:+.2f}", fill=(44, 110, 161, 255), font=f_label); ly += 18
    draw.text((lx, ly), f"  return {p['ret']*100:,.0f}%", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), f"  max DD {p['max_dd']*100:.1f}%", fill=(80, 80, 90, 255), font=f_small)

    draw.text((ML, HEIGHT - 22),
              f"Log scale on equity. IS-only window (no OOS — new q50-h2 config has no OOS Bybit fill). v11a sniper standalone Sharpe {sh_v:+.2f} IS. Near-zero correlation lets v11a stack at high leverage without DD blowup.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
