"""Efficient frontier: trade count vs Sharpe across all FC variants tested in v4–v10 push."""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUTPUT = Path("/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration/docs/fc_efficient_frontier.png")

# (name, trades, IS Sharpe, color, is_champion)
POINTS = [
    ("v4c (champion)",          96,  2.48, (10, 68, 41, 255), True),
    ("v6a score 0.4",           94,  2.31, (165, 27, 27, 255), False),
    ("v6a score 0.5",           89,  2.34, (165, 27, 27, 255), False),
    ("v10 σ2.0 + score 0.6",    108, 2.18, (10, 68, 41, 255), False),
    ("v4c_conc8 (cd=3)",        111, 2.14, (10, 68, 41, 255), False),
    ("v8 multi uni10",          115, 1.89, (212, 102, 26, 255), False),
    ("v9 track-record",         69,  1.70, (212, 102, 26, 255), False),
    ("v7_uni10 owncoin",        195, 1.26, (212, 102, 26, 255), False),
    ("v7_uni30 score 0.65",     204, 0.64, (165, 27, 27, 255), False),
    ("v7_uni30 score 0.55",     316, 0.45, (165, 27, 27, 255), False),
    ("v8 multi uni30",          243, 0.59, (165, 27, 27, 255), False),
]
# Pareto-optimal points
PARETO = [(96, 2.48), (108, 2.18), (111, 2.14), (115, 1.89), (195, 1.26), (204, 0.64), (243, 0.59), (316, 0.45)]
PARETO.sort()

WIDTH, HEIGHT = 1400, 800
ML, MR, MT, MB = 90, 340, 80, 80
PW = WIDTH - ML - MR
PH = HEIGHT - MT - MB


def _font(size, bold=False):
    for p in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            from PIL import ImageFont
            return ImageFont.truetype(p, size, index=1 if bold and p.endswith(".ttc") else 0)
        except Exception:
            continue
    from PIL import ImageFont
    return ImageFont.load_default()


def main():
    image = Image.new("RGBA", (WIDTH, HEIGHT), (252, 251, 247, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    f_title = _font(22, bold=True); f_label = _font(14); f_small = _font(11); f_tiny = _font(10)

    draw.text((ML, 25), "FC variant efficient frontier — Sharpe vs trade count",
              fill=(20, 20, 30, 255), font=f_title)
    draw.text((ML, 55), "Each point = one v6/v7/v8/v9/v10 variant tested. Up-and-right is better. The Pareto curve traces the achievable boundary.",
              fill=(80, 80, 90, 255), font=f_label)

    # Axes
    draw.rectangle([ML, MT, ML + PW, MT + PH], outline=(180, 180, 190, 255), fill=(252, 252, 252, 255), width=1)
    # x: trades 0–350, y: Sharpe 0–3
    x_max, x_min = 350, 0
    y_max, y_min = 3.0, 0.0
    def x_pos(t): return ML + int((t - x_min) / (x_max - x_min) * PW)
    def y_pos(s): return MT + PH - int((s - y_min) / (y_max - y_min) * PH)
    # x grid
    for t in range(0, x_max + 1, 50):
        x = x_pos(t)
        draw.line([(x, MT + PH), (x, MT + PH + 4)], fill=(120, 120, 130, 255), width=1)
        draw.text((x - 12, MT + PH + 8), str(t), fill=(80, 80, 90, 255), font=f_small)
        draw.line([(x, MT), (x, MT + PH)], fill=(230, 230, 235, 255), width=1)
    draw.text((ML + PW // 2 - 30, MT + PH + 30), "Trade count (IS only)", fill=(80, 80, 90, 255), font=f_label)
    # y grid
    for s in [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        y = y_pos(s)
        draw.line([(ML - 4, y), (ML, y)], fill=(120, 120, 130, 255), width=1)
        draw.text((ML - 35, y - 8), f"{s:.1f}", fill=(80, 80, 90, 255), font=f_small)
        draw.line([(ML, y), (ML + PW, y)], fill=(230, 230, 235, 255), width=1)
    draw.text((ML - 70, MT + PH // 2), "Sharpe", fill=(80, 80, 90, 255), font=f_label)

    # v4c baseline horizontal reference line
    yref = y_pos(2.48)
    for x in range(ML, ML + PW, 10):
        draw.line([(x, yref), (x + 5, yref)], fill=(120, 180, 130, 255), width=2)
    draw.text((ML + PW + 5, yref - 8), "v4c", fill=(10, 68, 41, 255), font=f_small)

    # Pareto curve
    pareto_pts = [(x_pos(t), y_pos(s)) for t, s in PARETO]
    for i in range(len(pareto_pts) - 1):
        draw.line([pareto_pts[i], pareto_pts[i + 1]], fill=(120, 120, 130, 180), width=1)

    # Points
    for name, t, s, color, champ in POINTS:
        x, y = x_pos(t), y_pos(s)
        r = 8 if champ else 6
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(0, 0, 0, 100), width=1)
        # Label offset
        draw.text((x + r + 3, y - 6), name, fill=(20, 20, 30, 255), font=f_tiny)

    # Legend
    lx = ML + PW + 25; ly = MT + 10
    draw.text((lx, ly), "Color key", fill=(20, 20, 30, 255), font=f_label); ly += 22
    for label, color in [("Improves on v4c uni10", (10, 68, 41, 255)),
                          ("Trade-off (more trades, less Sharpe)", (212, 102, 26, 255)),
                          ("Strictly dominated", (165, 27, 27, 255))]:
        draw.rectangle([lx, ly, lx + 16, ly + 12], fill=color)
        draw.text((lx + 22, ly - 1), label, fill=(80, 80, 90, 255), font=f_small)
        ly += 22

    ly += 14
    draw.text((lx, ly), "Pareto frontier:", fill=(20, 20, 30, 255), font=f_label); ly += 18
    draw.text((lx, ly), "v4c is the Sharpe-max point.", fill=(60, 60, 70, 255), font=f_small); ly += 16
    draw.text((lx, ly), "Moving right (+trades) costs Sharpe.", fill=(60, 60, 70, 255), font=f_small); ly += 16
    draw.text((lx, ly), "Steepest drop past 195 trades —", fill=(60, 60, 70, 255), font=f_small); ly += 14
    draw.text((lx, ly), "wider universe noise dominates.", fill=(60, 60, 70, 255), font=f_small); ly += 22

    draw.text((lx, ly), "User picks operating point:", fill=(20, 20, 30, 255), font=f_label); ly += 18
    draw.text((lx, ly), "• max Sharpe: v4c (96, +2.48)", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "• +12% trades: v10 (108, +2.18)", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "• +15% trades, low DD: v4c_conc8", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "• +100% trades: v7_uni10 owncoin", fill=(80, 80, 90, 255), font=f_small); ly += 14
    draw.text((lx, ly), "  (but Sharpe halves to 1.26)", fill=(80, 80, 90, 255), font=f_small); ly += 22

    draw.text((lx, ly), "Per-coin attribution (n=148):", fill=(20, 20, 30, 255), font=f_label); ly += 18
    draw.text((lx, ly), "  BTC=22 trd 50% +5.5%", fill=(80, 80, 90, 255), font=f_small); ly += 13
    draw.text((lx, ly), "  XRP=16 trd 38% +3.7%", fill=(80, 80, 90, 255), font=f_small); ly += 13
    draw.text((lx, ly), "  1000PEPE=11 trd 45% +4.1%", fill=(80, 80, 90, 255), font=f_small); ly += 13
    draw.text((lx, ly), "  ADA=8 trd 88% +3.5%", fill=(80, 80, 90, 255), font=f_small); ly += 13
    draw.text((lx, ly), "  SOL=13 trd 38% -0.5% (only loser)", fill=(165, 27, 27, 255), font=f_small); ly += 13

    draw.text((ML, HEIGHT - 22),
              "v4–v10 push tested: alpha score, sigma-relative + multi-day triggers, per-coin own-quantile triggers/filters, wider universes, multi-pattern, track-record sizing, capacity expansion. v4c remains Pareto-optimal for risk-adjusted return on top-10 universe.",
              fill=(100, 100, 110, 255), font=f_small)

    image.save(OUTPUT, format="PNG")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
