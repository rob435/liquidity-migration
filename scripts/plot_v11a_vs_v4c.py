"""v11a sniper vs v4c — standalone equity + combined book."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np, polars as pl
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/v11a_sniper_vs_v4c.png")
SHORT_STITCHED = "/tmp/stitched_returns.csv"

WIDTH, HEIGHT = 1600, 1100
ML, MR, MT, MB = 100, 320, 80, 70
GAP = 80
TOP_H = (HEIGHT - MT - MB - GAP) // 2
BOT_H = TOP_H
PW = WIDTH - ML - MR


def _font(size, bold=False):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try: return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception: continue
    return ImageFont.load_default()


def daily(p):
    eq = pl.read_csv(p).sort("ts_ms")
    by_d = dict(zip(eq["date"].to_list(), eq["equity"].to_list()))
    dates = sorted(by_d.keys())
    start = datetime.strptime(dates[0],"%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(dates[-1],"%Y-%m-%d").replace(tzinfo=UTC)
    out=[]; last=1.0
    for i in range((end-start).days+1):
        d = (start+timedelta(days=i)).strftime("%Y-%m-%d")
        if d in by_d: last=by_d[d]
        out.append((d,last))
    return out


def to_returns(eq):
    r=[]; p=eq[0][1]
    for d,v in eq[1:]:
        r.append((v-p)/p if p!=0 else 0); p=v
    return r


def stitch(is_p, oos_p, dates):
    is_eq=daily(is_p); oos_eq=daily(oos_p)
    is_start = datetime.strptime(is_eq[0][0],"%Y-%m-%d").replace(tzinfo=UTC)
    oos_map = dict(zip([x[0] for x in oos_eq[1:]], to_returns(oos_eq)))
    is_map = dict(zip([x[0] for x in is_eq[1:]], to_returns(is_eq)))
    combined = {}
    for d, r in oos_map.items():
        if datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=UTC) < is_start:
            combined[d] = r
    for d, r in is_map.items():
        combined[d] = r
    return np.array([combined.get(d, 0.0) for d in dates])


CANDS = [
    ("v4c (champion)", (165, 27, 27, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v4c_uni10_sigma2.5_3d_7d_OOS/long_native_equity.csv"),
    ("v11a sniper retrace 1%/6h", (10, 68, 41, 255),
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v11a_retrace1pct_6h_fallthru/long_native_equity.csv",
     "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v11a_retrace1pct_6h_OOS_bybit/long_native_equity.csv"),
]


def main():
    short_df = pl.read_csv(SHORT_STITCHED)
    short_map = dict(zip(short_df["date"].to_list(), short_df["short_ret"].to_list()))
    short_dates = sorted(short_map.keys())
    short_arr = np.array([short_map[d] for d in short_dates])
    short_sh = float(short_arr.mean() / short_arr.std(ddof=1) * math.sqrt(365))
    ts_list = [datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000 for d in short_dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]
    is_boundary = datetime(2023, 8, 1, tzinfo=UTC).timestamp() * 1000

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(12)

    # Compute series
    series = []
    for name, color, ip, op in CANDS:
        u = stitch(ip, op, short_dates)
        sh = float(u.mean()/u.std(ddof=1)*math.sqrt(365)) if u.std(ddof=1)>0 else 0
        eq = np.cumprod(1+u)
        peaks = np.maximum.accumulate(eq); dd = (eq/peaks-1).min()
        corr = float(np.corrcoef(short_arr, u)[0,1])
        series.append({"name": name, "color": color, "u": u, "eq": eq, "sh": sh,
                       "ret": eq[-1]-1, "dd": dd, "corr": corr})

    # ===== PANEL 1: standalone =====
    p1_top = MT + 35; p1_bottom = p1_top + TOP_H
    draw.text((ML, p1_top - 30), "Standalone equity — v4c vs v11a sniper (stitched OOS+IS Bybit, 4y)",
              fill=(20,20,30,255), font=f_title)
    draw.rectangle([ML, p1_top, ML+PW, p1_bottom], outline=(180,180,190,255), fill=(252,252,252,255), width=1)
    eq_max = max(s["eq"].max() for s in series) * 1.05
    eq_min = 0.85
    def y1(v): return p1_top + TOP_H - int((v-eq_min)/(eq_max-eq_min)*TOP_H)
    for vp in (-15,-10,-5,0,5,10,15,20,25,30,35):
        v = 1+vp/100
        if eq_min <= v <= eq_max:
            y = y1(v)
            draw.line([(ML-4,y),(ML,y)], fill=(120,120,130,255), width=1)
            draw.text((ML-50, y-8), f"+{vp}%" if vp>=0 else f"{vp}%", fill=(80,80,90,255), font=f_small)
    y0 = y1(1.0); draw.line([(ML,y0),(ML+PW,y0)], fill=(160,160,170,255), width=1)
    for yr in range(2021, 2027):
        for m in (1, 7):
            t = datetime(yr, m, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= t <= ts_max:
                x = ML + int((t-ts_min)/(ts_max-ts_min)*PW)
                draw.line([(x, p1_bottom), (x, p1_bottom+4)], fill=(120,120,130,255), width=1)
                draw.text((x-22, p1_bottom+8), f"{yr}-{m:02d}", fill=(80,80,90,255), font=f_small)
    bx = ML + int((is_boundary-ts_min)/(ts_max-ts_min)*PW)
    for y in range(p1_top, p1_bottom, 6):
        draw.line([(bx,y),(bx,y+3)], fill=(150,150,160,255), width=1)
    draw.text((bx-30, p1_top+5), "← OOS | IS →", fill=(120,120,130,255), font=f_small)
    for s in series:
        pts = [(ML + int((ts_list[i]-ts_min)/(ts_max-ts_min)*PW), y1(max(s["eq"][i], 0.5))) for i in range(len(s["eq"]))]
        for i in range(len(pts)-1):
            draw.line([pts[i], pts[i+1]], fill=s["color"], width=2)

    lx = ML + PW + 25; ly = p1_top + 10
    draw.text((lx, ly), "Standalone (stitched)", fill=(20,20,30,255), font=f_label); ly += 24
    for s in series:
        draw.rectangle([lx, ly, lx+28, ly+7], fill=s["color"])
        draw.text((lx+36, ly-4), s["name"], fill=(20,20,30,255), font=f_small)
        draw.text((lx+36, ly+11), f"Stitched Sh {s['sh']:+.2f}  ret {s['ret']*100:+.1f}%", fill=(80,80,90,255), font=f_small)
        draw.text((lx+36, ly+25), f"DD {s['dd']*100:.1f}%  corr {s['corr']:+.3f}", fill=(80,80,90,255), font=f_small)
        ly += 50

    ly += 10
    draw.text((lx, ly), "Per-window Sharpe", fill=(60,60,70,255), font=f_label); ly += 20
    draw.text((lx, ly), "IS Bybit:  v4c +2.48 → v11a +2.60", fill=(80,80,90,255), font=f_small); ly += 16
    draw.text((lx, ly), "OOS Bybit: v4c +2.38 → v11a +2.26", fill=(80,80,90,255), font=f_small); ly += 16
    draw.text((lx, ly), "OOS Binance: v4c +0.71 → v11a +1.06", fill=(80,80,90,255), font=f_small); ly += 22

    draw.text((lx, ly), "v11a config delta", fill=(60,60,70,255), font=f_label); ly += 18
    draw.text((lx, ly), "After FC signal fires at daily", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "close, wait up to 6h for price to", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "retrace 1% below signal close.", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "Enter on the retrace bar (limit-", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "style fill). If no retrace, fall", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "through to 6h deadline.", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "Same trade count, better fills.", fill=(80,80,90,255), font=f_small)

    # ===== PANEL 2: combined book =====
    p2_top = p1_bottom + GAP; p2_bottom = p2_top + BOT_H
    draw.text((ML, p2_top - 30), "Combined book — short + each FC variant at 5× long-side leverage (log scale)",
              fill=(20,20,30,255), font=f_title)
    draw.rectangle([ML, p2_top, ML+PW, p2_bottom], outline=(180,180,190,255), fill=(252,252,252,255), width=1)

    short_eq = np.cumprod(1+short_arr)
    short_peaks = np.maximum.accumulate(short_eq); short_dd_combo = (short_eq/short_peaks-1).min()
    combos = [("Short only (1×)", (130,130,130,255), short_eq, short_sh, short_eq[-1]-1, short_dd_combo)]
    for s in series:
        port = short_arr + 5.0 * s["u"]
        sh = float(port.mean()/port.std(ddof=1)*math.sqrt(365))
        eq = np.cumprod(1+port)
        peaks = np.maximum.accumulate(eq); dd_c = (eq/peaks-1).min()
        combos.append((f"+ {s['name']} 5×", s["color"], eq, sh, eq[-1]-1, dd_c))

    eq_max2 = max(c[2].max() for c in combos); eq_min2 = 0.9
    lmin = math.log10(eq_min2); lmax = math.log10(eq_max2)
    def y2(v): return p2_top + BOT_H - int((math.log10(v)-lmin)/(lmax-lmin)*BOT_H)
    for v in [1, 5, 10, 50, 100, 500, 1000, 5000]:
        if eq_min2 <= v <= eq_max2:
            y = y2(v); draw.line([(ML-4,y),(ML,y)], fill=(120,120,130,255), width=1)
            draw.text((ML-55, y-8), f"{int(v)}x", fill=(80,80,90,255), font=f_small)
    for yr in range(2021, 2027):
        for m in (1, 7):
            t = datetime(yr, m, 1, tzinfo=UTC).timestamp() * 1000
            if ts_min <= t <= ts_max:
                x = ML + int((t-ts_min)/(ts_max-ts_min)*PW)
                draw.line([(x, p2_bottom), (x, p2_bottom+4)], fill=(120,120,130,255), width=1)
                draw.text((x-22, p2_bottom+8), f"{yr}-{m:02d}", fill=(80,80,90,255), font=f_small)
    bx2 = ML + int((is_boundary-ts_min)/(ts_max-ts_min)*PW)
    for y in range(p2_top, p2_bottom, 6):
        draw.line([(bx2,y),(bx2,y+3)], fill=(150,150,160,255), width=1)
    for name, color, eq, sh, ret, dd in combos:
        pts = [(ML + int((ts_list[i]-ts_min)/(ts_max-ts_min)*PW), y2(max(eq[i], 0.01))) for i in range(len(eq))]
        for i in range(len(pts)-1):
            draw.line([pts[i], pts[i+1]], fill=color, width=2)

    lx = ML + PW + 25; ly = p2_top + 10
    draw.text((lx, ly), "Combined (5×)", fill=(20,20,30,255), font=f_label); ly += 24
    for name, color, eq, sh, ret, dd in combos:
        draw.rectangle([lx, ly, lx+28, ly+7], fill=color)
        draw.text((lx+36, ly-4), name, fill=(20,20,30,255), font=f_small)
        draw.text((lx+36, ly+11), f"Sh {sh:+.2f}  ret {ret*100:,.0f}%", fill=(80,80,90,255), font=f_small)
        draw.text((lx+36, ly+25), f"max DD {dd*100:.1f}%", fill=(80,80,90,255), font=f_small)
        ly += 50

    ly += 14
    draw.text((lx, ly), "Combined book deltas (5×)", fill=(60,60,70,255), font=f_label); ly += 18
    draw.text((lx, ly), "v4c → v11a", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "Sharpe: +3.59 → +3.66", fill=(10,68,41,255), font=f_small); ly += 14
    draw.text((lx, ly), "Combined 10×: +3.45 → +3.58", fill=(10,68,41,255), font=f_small)

    draw.text((ML, HEIGHT - 22),
              "v11a sniper retrace 1%/6h with fallthrough — beats v4c on stitched Sharpe (+1.42 → +1.64, +15%), return (+27.7% → +31.7%), and combined book at all leverages. Same trade count (96), better entry prices on the 70-80% of signals that retrace. Signal_ts and entry_ts now decoupled (mimics short-system's quality-squeeze architecture).",
              fill=(100,100,110,255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
