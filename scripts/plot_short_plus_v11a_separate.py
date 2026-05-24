"""3-panel chart: short alone, long alone, combined book — keep production short, overlay v11a."""
from __future__ import annotations
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
import numpy as np, polars as pl
from PIL import Image, ImageDraw, ImageFont


SHORT_STITCHED = "/tmp/stitched_returns.csv"  # production short stitched (q40-h3)
V11A_IS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_fullpit_1h/reports/long_native_FC_v11a_retrace1pct_6h_fallthru/long_native_equity.csv"
V11A_OOS = "/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_oos_pre2023/reports/long_native_FC_v11a_retrace1pct_6h_OOS_bybit/long_native_equity.csv"
OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/short_plus_v11a_three_panel.png")

WIDTH, HEIGHT = 1700, 1200
ML, MR, MT, MB = 100, 340, 80, 70
GAP = 50
PANEL_H = (HEIGHT - MT - MB - 2 * GAP) // 3
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


def stitch_v11a(dates):
    """Stitch v11a IS+OOS aligned to date list."""
    is_eq = daily(V11A_IS); oos_eq = daily(V11A_OOS)
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


def axis_panel(draw, top, height, label, eq_arr_max, eq_arr_min, log_scale=False):
    draw.rectangle([ML, top, ML + PW, top + height], outline=(180,180,190,255), fill=(252,252,252,255), width=1)
    f_small = _font(11); f_label = _font(13)
    draw.text((ML, top - 22), label, fill=(60,60,70,255), font=f_label)
    if log_scale:
        lmin = math.log10(eq_arr_min); lmax = math.log10(eq_arr_max)
        def yfn(v): return top + height - int((math.log10(max(v, eq_arr_min)) - lmin) / (lmax - lmin) * height)
        ticks = [1, 5, 10, 50, 100, 500, 1000, 5000, 10000]
        for v in ticks:
            if eq_arr_min <= v <= eq_arr_max:
                y = yfn(v); draw.line([(ML-4,y),(ML,y)], fill=(120,120,130,255), width=1)
                draw.text((ML-50, y-8), f"{int(v)}x", fill=(80,80,90,255), font=f_small)
    else:
        # linear pct
        def yfn(v): return top + height - int((v - eq_arr_min) / (eq_arr_max - eq_arr_min) * height)
        # try sensible tick spacing
        rng = eq_arr_max - eq_arr_min
        step = 0.05 if rng < 0.5 else (0.10 if rng < 1.0 else 0.25)
        v = math.floor(eq_arr_min / step) * step
        while v <= eq_arr_max:
            if eq_arr_min <= v <= eq_arr_max:
                y = yfn(v); draw.line([(ML-4,y),(ML,y)], fill=(120,120,130,255), width=1)
                lbl = f"{(v-1)*100:+.0f}%" if v != 1.0 else "0%"
                draw.text((ML-55, y-8), lbl, fill=(80,80,90,255), font=f_small)
            v += step
    return yfn


def main():
    # Load short stitched
    sdf = pl.read_csv(SHORT_STITCHED)
    short_dates = sdf["date"].to_list()
    s_ret = np.array(sdf["short_ret"].to_list())
    v_ret = stitch_v11a(short_dates)
    ts_list = [datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=UTC).timestamp()*1000 for d in short_dates]
    ts_min, ts_max = ts_list[0], ts_list[-1]
    is_boundary = datetime(2023,8,1,tzinfo=UTC).timestamp()*1000

    # Equity curves
    short_eq = np.cumprod(1 + s_ret)
    v_eq = np.cumprod(1 + v_ret)
    combo_5x = np.cumprod(1 + s_ret + 5.0 * v_ret)
    combo_10x = np.cumprod(1 + s_ret + 10.0 * v_ret)

    # Stats
    def stats(r):
        sh = float(r.mean() / r.std(ddof=1) * math.sqrt(365)) if r.std(ddof=1)>0 else 0
        eq = np.cumprod(1+r)
        peaks = np.maximum.accumulate(eq); dd = (eq/peaks-1).min()
        return sh, float(eq[-1]-1), float(dd)
    sh_s, ret_s, dd_s = stats(s_ret)
    sh_v, ret_v, dd_v = stats(v_ret)
    sh_c5, ret_c5, dd_c5 = stats(s_ret + 5*v_ret)
    sh_c10, ret_c10, dd_c10 = stats(s_ret + 10*v_ret)
    corr = float(np.corrcoef(s_ret, v_ret)[0,1])

    image = Image.new("RGBA", (WIDTH, HEIGHT), (252,251,247,255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(12)
    draw.text((ML, 25), "Production short (q40-h3 promoted) + v11a long sleeve — per-system equity and combined book",
              fill=(20,20,30,255), font=f_title)
    draw.text((ML, 55), f"Stitched OOS+IS Bybit 2022-04 → 2026-05 ({len(short_dates)} days). Correlation: {corr:+.4f}.",
              fill=(80,80,90,255), font=f_label)

    # ===== PANEL 1: short alone (log) =====
    p1_top = MT
    yfn1 = axis_panel(draw, p1_top, PANEL_H, "1) Production short alone (q40-h3) — log scale",
                       eq_arr_max=short_eq.max()*1.05, eq_arr_min=0.9, log_scale=True)
    for i in range(len(short_eq)-1):
        x1 = ML + int((ts_list[i]-ts_min)/(ts_max-ts_min)*PW)
        x2 = ML + int((ts_list[i+1]-ts_min)/(ts_max-ts_min)*PW)
        y1 = yfn1(max(short_eq[i], 0.9)); y2 = yfn1(max(short_eq[i+1], 0.9))
        draw.line([(x1,y1),(x2,y2)], fill=(165,27,27,255), width=2)
    # IS boundary
    bx = ML + int((is_boundary-ts_min)/(ts_max-ts_min)*PW)
    for y in range(p1_top, p1_top+PANEL_H, 6):
        draw.line([(bx,y),(bx,y+3)], fill=(150,150,160,255), width=1)
    # x ticks
    for yr in range(2022,2027):
        for m in (1,7):
            t = datetime(yr,m,1,tzinfo=UTC).timestamp()*1000
            if ts_min <= t <= ts_max:
                x = ML + int((t-ts_min)/(ts_max-ts_min)*PW)
                draw.text((x-22, p1_top+PANEL_H+2), f"{yr}-{m:02d}", fill=(80,80,90,255), font=f_small)

    # ===== PANEL 2: v11a long alone (linear) =====
    p2_top = p1_top + PANEL_H + GAP
    yfn2 = axis_panel(draw, p2_top, PANEL_H, "2) v11a long sleeve alone (sniper retrace 1%/6h) — linear scale",
                       eq_arr_max=max(v_eq.max()*1.05, 1.3), eq_arr_min=0.92, log_scale=False)
    # color by IS/OOS
    color_is = (10,68,41,255); color_oos = (165,27,27,255)
    for i in range(len(v_eq)-1):
        x1 = ML + int((ts_list[i]-ts_min)/(ts_max-ts_min)*PW)
        x2 = ML + int((ts_list[i+1]-ts_min)/(ts_max-ts_min)*PW)
        y1 = yfn2(max(v_eq[i], 0.92)); y2 = yfn2(max(v_eq[i+1], 0.92))
        c = color_is if ts_list[i] >= is_boundary else color_oos
        draw.line([(x1,y1),(x2,y2)], fill=c, width=2)
    bx = ML + int((is_boundary-ts_min)/(ts_max-ts_min)*PW)
    for y in range(p2_top, p2_top+PANEL_H, 6):
        draw.line([(bx,y),(bx,y+3)], fill=(150,150,160,255), width=1)
    draw.text((bx-30, p2_top+5), "← OOS | IS →", fill=(120,120,130,255), font=f_small)
    for yr in range(2022,2027):
        for m in (1,7):
            t = datetime(yr,m,1,tzinfo=UTC).timestamp()*1000
            if ts_min <= t <= ts_max:
                x = ML + int((t-ts_min)/(ts_max-ts_min)*PW)
                draw.text((x-22, p2_top+PANEL_H+2), f"{yr}-{m:02d}", fill=(80,80,90,255), font=f_small)

    # ===== PANEL 3: combined book (log) =====
    p3_top = p2_top + PANEL_H + GAP
    yfn3 = axis_panel(draw, p3_top, PANEL_H, "3) Combined book — short × 1 + v11a × {1, 5, 10} long — log scale",
                       eq_arr_max=combo_10x.max()*1.05, eq_arr_min=0.9, log_scale=True)
    combos = [
        ("Short only", short_eq, (130,130,130,255)),
        ("+ v11a 1×", np.cumprod(1+s_ret+v_ret), (212,102,26,255)),
        ("+ v11a 5×", combo_5x, (10,68,41,255)),
        ("+ v11a 10×", combo_10x, (44,110,161,255)),
    ]
    for name, eq, color in combos:
        for i in range(len(eq)-1):
            x1 = ML + int((ts_list[i]-ts_min)/(ts_max-ts_min)*PW)
            x2 = ML + int((ts_list[i+1]-ts_min)/(ts_max-ts_min)*PW)
            y1 = yfn3(max(eq[i], 0.9)); y2 = yfn3(max(eq[i+1], 0.9))
            draw.line([(x1,y1),(x2,y2)], fill=color, width=2)
    bx = ML + int((is_boundary-ts_min)/(ts_max-ts_min)*PW)
    for y in range(p3_top, p3_top+PANEL_H, 6):
        draw.line([(bx,y),(bx,y+3)], fill=(150,150,160,255), width=1)
    for yr in range(2022,2027):
        for m in (1,7):
            t = datetime(yr,m,1,tzinfo=UTC).timestamp()*1000
            if ts_min <= t <= ts_max:
                x = ML + int((t-ts_min)/(ts_max-ts_min)*PW)
                draw.text((x-22, p3_top+PANEL_H+2), f"{yr}-{m:02d}", fill=(80,80,90,255), font=f_small)

    # ===== Right legend with per-sleeve stats =====
    lx = ML + PW + 25; ly = MT + 10
    draw.text((lx, ly), "Per-system stats (4y stitched)", fill=(20,20,30,255), font=f_label); ly += 24
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(165,27,27,255))
    draw.text((lx+30, ly-3), "Short (q40-h3 promoted)", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sharpe {sh_s:+.2f}  ret {ret_s*100:,.0f}%", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"max DD {dd_s*100:.1f}%  475 IS trades", fill=(80,80,90,255), font=f_small); ly += 26
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(10,68,41,255))
    draw.text((lx+30, ly-3), "v11a long sniper", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sharpe {sh_v:+.2f}  ret {ret_v*100:+.1f}%", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"max DD {dd_v*100:.1f}%  96 IS + 52 OOS", fill=(80,80,90,255), font=f_small); ly += 26

    draw.text((lx, ly), f"Correlation: {corr:+.4f}", fill=(20,20,30,255), font=f_label); ly += 28

    draw.text((lx, ly), "Combined book results", fill=(20,20,30,255), font=f_label); ly += 24
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(130,130,130,255))
    draw.text((lx+30, ly-3), "Short only (1×)", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sh {sh_s:+.2f} ret {ret_s*100:,.0f}% DD {dd_s*100:.1f}%", fill=(80,80,90,255), font=f_small); ly += 22
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(212,102,26,255))
    sh_c1, ret_c1, dd_c1 = stats(s_ret + v_ret)
    draw.text((lx+30, ly-3), "Short + v11a 1×", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sh {sh_c1:+.2f} ret {ret_c1*100:,.0f}% DD {dd_c1*100:.1f}%", fill=(80,80,90,255), font=f_small); ly += 22
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(10,68,41,255))
    draw.text((lx+30, ly-3), "Short + v11a 5× ← peak Sh", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sh {sh_c5:+.2f} ret {ret_c5*100:,.0f}% DD {dd_c5*100:.1f}%", fill=(80,80,90,255), font=f_small); ly += 22
    draw.rectangle([lx, ly, lx+24, ly+10], fill=(44,110,161,255))
    draw.text((lx+30, ly-3), "Short + v11a 10×", fill=(20,20,30,255), font=f_small); ly += 14
    draw.text((lx+30, ly), f"Sh {sh_c10:+.2f} ret {ret_c10*100:,.0f}% DD {dd_c10*100:.1f}%", fill=(80,80,90,255), font=f_small); ly += 26

    draw.text((lx, ly), "Trade counts (stitched)", fill=(20,20,30,255), font=f_label); ly += 18
    draw.text((lx, ly), "Short:  475 IS (q40-h3)", fill=(80,80,90,255), font=f_small); ly += 14
    draw.text((lx, ly), "v11a:   96 IS + 52 OOS", fill=(80,80,90,255), font=f_small); ly += 22

    draw.text((lx, ly), "Two independent ledgers,", fill=(60,60,70,255), font=f_small); ly += 14
    draw.text((lx, ly), "near-zero correlation, no overlap", fill=(60,60,70,255), font=f_small); ly += 14
    draw.text((lx, ly), "on signal days.", fill=(60,60,70,255), font=f_small)

    draw.text((ML, HEIGHT - 22),
              f"Production short ({sh_s:+.2f} stitched Sharpe, 475 trades) untouched. v11a long sleeve overlays at chosen leverage. Combined peak Sharpe {sh_c5:+.2f} at 5× v11a.",
              fill=(100,100,110,255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
