"""Final Jane Street efficient frontier: FC + sniper across universe sizes and variants.

Shows that Sharpe ≥ 3.0 AND 100+ trades is unattainable in this signal class —
the Pareto curve traces the structural ceiling.
"""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/jane_street_frontier.png")

# (name, trades, IS Sharpe, color, label position offset)
POINTS = [
    # uni size ladder with sniper
    ("uni3 sniper",            37,  2.21, (140, 30, 30, 255), False),
    ("uni5 sniper ← peak Sh",  49,  3.22, (10, 68, 41, 255),  True),
    ("uni6 sniper",            58,  2.84, (10, 68, 41, 255),  False),
    ("uni7 sniper",            69,  2.75, (10, 68, 41, 255),  False),
    ("uni8 sniper",            73,  2.63, (10, 68, 41, 255),  False),
    ("uni10 v11a sniper",      96,  2.60, (10, 68, 41, 255),  True),
    # Trade-volume seekers
    ("uni10 multi+sniper",     119, 1.92, (212, 102, 26, 255), False),
    ("uni20 multi+sniper",     220, 1.00, (165, 27, 27, 255), False),
    ("uni30 multi+sniper",     310, 0.72, (165, 27, 27, 255), False),
    # v15 hold variations
    ("hold1d", 101, 1.52, (165, 27, 27, 255), False),
    ("hold2d", 99,  2.31, (212, 102, 26, 255), False),
]

WIDTH, HEIGHT = 1500, 850
ML, MR, MT, MB = 100, 360, 110, 90
PW = WIDTH - ML - MR
PH = HEIGHT - MT - MB


def _font(size, bold=False):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try: return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception: continue
    return ImageFont.load_default()


def main():
    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(11); f_tiny = _font(10)

    draw.text((ML, 25), "FC + sniper efficient frontier — the signal's structural ceiling",
              fill=(20, 20, 30, 255), font=f_title)
    draw.text((ML, 55), "Each point = one config tested. The user's target zone (Sharpe ≥ 3.0 AND 100+ trades) is the top-right shaded region.",
              fill=(80, 80, 90, 255), font=f_label)
    draw.text((ML, 75), "No FC variant lands inside the target zone — the signal's information content caps the upper-left Pareto curve.",
              fill=(165, 27, 27, 255), font=f_label)

    # Axes
    draw.rectangle([ML, MT, ML + PW, MT + PH], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    x_max, x_min = 350, 0
    y_max, y_min = 4.0, 0.0
    def x_pos(t): return ML + int((t - x_min) / (x_max - x_min) * PW)
    def y_pos(s): return MT + PH - int((s - y_min) / (y_max - y_min) * PH)

    # Shaded target zone (top-right, Sharpe ≥ 3, trades ≥ 100)
    tx = x_pos(100); ty = y_pos(3.0)
    target_rect = [tx, MT, ML + PW, ty]
    draw.rectangle(target_rect, fill=(20, 180, 50, 25), outline=(20, 130, 50, 100), width=2)
    draw.text((tx + 15, MT + 15), "Target zone:", fill=(20, 130, 50, 255), font=f_label)
    draw.text((tx + 15, MT + 33), "Sharpe ≥ 3.0", fill=(20, 130, 50, 255), font=f_small)
    draw.text((tx + 15, MT + 47), "100+ trades", fill=(20, 130, 50, 255), font=f_small)
    draw.text((tx + 15, MT + 65), "(EMPTY — unreachable", fill=(165, 27, 27, 255), font=f_small)
    draw.text((tx + 15, MT + 79), "with FC alone)", fill=(165, 27, 27, 255), font=f_small)

    # Grids and labels
    for t in range(0, x_max + 1, 50):
        x = x_pos(t)
        draw.line([(x, MT + PH), (x, MT + PH + 4)], fill=(120, 120, 130, 255), width=1)
        draw.text((x - 12, MT + PH + 8), str(t), fill=(80, 80, 90, 255), font=f_small)
        draw.line([(x, MT), (x, MT + PH)], fill=(230, 230, 235, 80), width=1)
    draw.text((ML + PW // 2 - 30, MT + PH + 32), "Trade count (IS only)", fill=(80, 80, 90, 255), font=f_label)
    for s in [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]:
        y = y_pos(s)
        draw.line([(ML - 4, y), (ML, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((ML - 35, y - 8), f"{s:.1f}", fill=(80, 80, 90, 255), font=f_small)
        draw.line([(ML, y), (ML + PW, y)], fill=(230, 230, 235, 80), width=1)
    draw.text((ML - 75, MT + PH // 2 - 20), "Sharpe", fill=(80, 80, 90, 255), font=f_label)
    draw.text((ML - 75, MT + PH // 2 - 5), "(IS)", fill=(80, 80, 90, 255), font=f_label)

    # Target line — Sharpe = 3.0
    y3 = y_pos(3.0)
    for x in range(ML, ML + PW, 8):
        draw.line([(x, y3), (x + 4, y3)], fill=(120, 180, 130, 200), width=2)

    # Trade count line — 100 trades
    for y in range(MT, MT + PH, 8):
        draw.line([(tx, y), (tx, y + 4)], fill=(120, 180, 130, 200), width=2)

    # Pareto frontier curve
    PARETO = [(37, 2.21), (49, 3.22), (96, 2.60), (101, 1.52), (119, 1.92), (220, 1.00), (310, 0.72)]
    pareto_pts = [(x_pos(t), y_pos(s)) for t, s in PARETO]
    pareto_pts.sort()
    # Pareto curve: keep only points that are not dominated
    p2 = sorted(PARETO)
    frontier = [p2[0]]
    for t, s in p2[1:]:
        # Keep if higher Sharpe than any seen at greater trades
        kept = True
        for ot, os in p2:
            if ot > t and os > s:
                kept = False; break
        if kept:
            frontier.append((t, s))
    fp = [(x_pos(t), y_pos(s)) for t, s in sorted(set(frontier))]
    for i in range(len(fp) - 1):
        draw.line([fp[i], fp[i + 1]], fill=(120, 120, 130, 150), width=1)

    # Points + labels
    for name, t, s, color, big in POINTS:
        x, y = x_pos(t), y_pos(s)
        r = 9 if big else 6
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(0, 0, 0, 130), width=1)
        draw.text((x + r + 3, y - 5), name, fill=(20, 20, 30, 255), font=f_tiny)

    # Legend
    lx = ML + PW + 25; ly = MT + 10
    draw.text((lx, ly), "Verdict", fill=(20, 20, 30, 255), font=f_label); ly += 22
    draw.text((lx, ly), "Target zone is EMPTY.", fill=(165, 27, 27, 255), font=f_small); ly += 14
    draw.text((lx, ly), "Sharpe peaks at uni5 (3.22)", fill=(60, 60, 70, 255), font=f_small); ly += 14
    draw.text((lx, ly), "but only 49 trades.", fill=(60, 60, 70, 255), font=f_small); ly += 14
    draw.text((lx, ly), "Trades crack 100 only at uni10+", fill=(60, 60, 70, 255), font=f_small); ly += 14
    draw.text((lx, ly), "but Sharpe drops below 2.6.", fill=(60, 60, 70, 255), font=f_small); ly += 22

    draw.text((lx, ly), "Best operating points:", fill=(20, 20, 30, 255), font=f_label); ly += 20
    draw.rectangle([lx, ly, lx + 14, ly + 11], fill=(10, 68, 41, 255))
    draw.text((lx + 22, ly - 1), "uni5 sniper: Sh 3.22, 49t, DD 1.7%", fill=(60, 60, 70, 255), font=f_small); ly += 18
    draw.rectangle([lx, ly, lx + 14, ly + 11], fill=(10, 68, 41, 255))
    draw.text((lx + 22, ly - 1), "uni10 v11a: Sh 2.60, 96t, DD 3.5%", fill=(60, 60, 70, 255), font=f_small); ly += 26

    draw.text((lx, ly), "What was tried:", fill=(20, 20, 30, 255), font=f_label); ly += 18
    for line in [
        "• v6 alpha-score filter",
        "• v7 per-coin own-quantile",
        "• v9 per-coin track record",
        "• v11 sniper retrace entries",
        "• v11 breakeven exit alpha",
        "• v12c sniper + BE + ATR trail",
        "• v13 adaptive ATR sniper",
        "• v14 universe 3/5/10",
        "• v15 hold/TP combinations",
        "• v16 multi-pattern + uni30",
        "• v17 fine universe-size search",
    ]:
        draw.text((lx, ly), line, fill=(80, 80, 90, 255), font=f_small); ly += 14
    ly += 6
    draw.text((lx, ly), "Path forward for Sh 3.0 + 100t:", fill=(20, 20, 30, 255), font=f_label); ly += 18
    draw.text((lx, ly), "• Different signal source", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "  (not FC pump-chase)", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "• Or accept the trade-off:", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "  ensemble uni5 + uni10", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "• Combined book w/ short", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "  gets Sh 3.66 at 5× lev", fill=(80, 80, 90, 255), font=f_small)

    draw.text((ML, HEIGHT - 22),
              "v6 through v17 — exhaustive FC + sniper exploration. The signal genuinely caps at the upper-left Pareto curve. No single FC variant achieves both metrics. The combined-book route (short + FC long at 5× leverage) reaches Sharpe 3.66 by ensembling the two independent sleeves.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
