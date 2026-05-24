#!/usr/bin/env python3
"""Plot LO_skip0 vs lo_sharpe3 equity curves."""
from __future__ import annotations

from pathlib import Path

import polars as pl

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("pip install pillow")

ROOT = Path("~/SHARED_DATA/bybit_fullpit_1h/reports").expanduser()
OUT = Path(__file__).resolve().parents[1] / "docs" / "lo_sharpe3_equity.png"

SERIES = [
    ("LO_skip0 (Sharpe ~2.4)", ROOT / "momentum_lo_LO_skip0_baseline/momentum_factor_equity.csv", (44, 110, 161, 255)),
    ("lo_sharpe3 (Sharpe ~3.9)", ROOT / "momentum_lo_sharpe3_winner/momentum_factor_equity.csv", (10, 68, 41, 255)),
]


def main() -> None:
    W, H, M = 1100, 520, 60
    img = Image.new("RGBA", (W, H), (252, 252, 250, 255))
    draw = ImageDraw.Draw(img)
    try:
        ft = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
        fs = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 13)
    except OSError:
        ft = fs = ImageFont.load_default()

    draw.text((M, 20), "Long-only momentum: baseline vs Sharpe-3 winner", fill=(20, 20, 30), font=ft)
    pw, ph = W - 2 * M, H - 2 * M - 30
    x0, y0 = M, M + 30

    all_eq = []
    for _, path, _ in SERIES:
        if path.exists():
            all_eq.append(pl.read_csv(path)["equity"].to_list())
    ymin = min(min(e) for e in all_eq) * 0.98
    ymax = max(max(e) for e in all_eq) * 1.02

    def xy(i: int, eq: float) -> tuple[int, int]:
        n = len(all_eq[0]) - 1
        x = x0 + int(i / max(n, 1) * pw)
        y = y0 + ph - int((eq - ymin) / (ymax - ymin) * ph)
        return x, y

    for label, path, color in SERIES:
        if not path.exists():
            continue
        eqs = pl.read_csv(path)["equity"].to_list()
        pts = [xy(i, e) for i, e in enumerate(eqs)]
        for a, b in zip(pts, pts[1:]):
            draw.line([a, b], fill=color, width=2)
        draw.text((M + 10, 50 + 22 * SERIES.index((label, path, color))), label, fill=color[:3], font=fs)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
