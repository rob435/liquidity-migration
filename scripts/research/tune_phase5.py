#!/usr/bin/env python3
"""Phase 5A — tune gates and filters for *t*, not for mean.

Every filter in this program was chosen to raise the mean, but
``t = effect x sqrt(n)``, so a filter that raises the mean while cutting the
sample can *lower* the evidence it produces:

| sample kept | effect multiple needed just to hold t constant |
| ---: | ---: |
| 75% | x1.15 |
| 50% | x1.41 |
| 25% | x2.00 |

This is a one-dimensional sensitivity on an already-selected book, not a new
mechanism hunt, so it does not carry the program-wide multiple-testing cost. It
carries its own, over the grid, and the discipline that follows from that is
absolute: **report the entire curve, never the best cell.** A t that peaks sharply
at one setting and collapses either side is overfitting. A broad plateau is a real
parameter.

Costs are charged at each cell's own *measured* turnover, because strictness
changes turnover and a fixed charge would bias the curve toward whichever end
churns less. Details in `docs/research/research_findings.md`.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import statistics
import sys
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.research.panels.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    long_short,
    summary,
    top_by,
)
from liquidity_migration.research.panels.lane2_blend import BlendConfig, prepare  # noqa: E402

_spec = importlib.util.spec_from_file_location("screen_phase1", REPO_ROOT / "scripts" / "research" / "screen_phase1.py")
assert _spec and _spec.loader
_sp = importlib.util.module_from_spec(_spec)
sys.modules["screen_phase1"] = _sp
_spec.loader.exec_module(_sp)

BONFERRONI_T = _sp.BONFERRONI_T


def _t_and_mean(book: pl.DataFrame, cost_bp: float, ppy: int) -> tuple[int, float, float, float]:
    r = book["ret_bp"].to_numpy()
    s = summary(r, periods_per_year=ppy, cost_bp=cost_bp)
    return s.n, s.mean_bp, s.t_stat, s.sharpe


def _curve_shape(ts: list[float]) -> str:
    """Plateau or spike? A single dominant cell is the overfitting signature."""
    finite = [t for t in ts if math.isfinite(t)]
    if len(finite) < 3:
        return "too few cells"
    best = max(finite)
    if best <= 0:
        return "no positive cell"
    near = sum(1 for t in finite if t >= 0.8 * best)
    frac = near / len(finite)
    spread = statistics.pstdev(finite) if len(finite) > 1 else 0.0
    if frac >= 0.5:
        return f"PLATEAU ({near}/{len(finite)} cells within 20% of peak, sd {spread:.2f})"
    if near <= 1:
        return f"SPIKE - single cell carries it (sd {spread:.2f}) -> treat as overfit"
    return f"mixed ({near}/{len(finite)} cells within 20% of peak, sd {spread:.2f})"


def sweep_cut(prepared: pl.DataFrame, signal: str, sign: int, top_n: int, hold: int,
              fee_side: float, ppy: int, cuts: list[float]) -> list[tuple]:
    out = []
    for cut in cuts:
        u = _sp._disjoint(top_by(prepared, "adv24", top_n), hold)
        book = long_short(u, signal=signal, ret="net_return", sign=sign, cut=cut)
        if book.height < 20:
            continue
        turn = _sp.measure_turnover(u, [(signal, sign, 1.0)], cut)
        n, mean, t, sh = _t_and_mean(book, turn * fee_side, ppy)
        out.append((f"{cut:.2f}", n, mean, t, sh, turn * fee_side))
    return out


def sweep_top_n(prepared: pl.DataFrame, signal: str, sign: int, cut: float, hold: int,
                fee_side: float, ppy: int, ns: list[int]) -> list[tuple]:
    out = []
    for top_n in ns:
        u = _sp._disjoint(top_by(prepared, "adv24", top_n), hold)
        book = long_short(u, signal=signal, ret="net_return", sign=sign, cut=cut)
        if book.height < 20:
            continue
        turn = _sp.measure_turnover(u, [(signal, sign, 1.0)], cut)
        n, mean, t, sh = _t_and_mean(book, turn * fee_side, ppy)
        out.append((str(top_n), n, mean, t, sh, turn * fee_side))
    return out


def sweep_btc_gate(prepared: pl.DataFrame, signal: str, sign: int, top_n: int, cut: float,
                   hold: int, fee_side: float, ppy: int, thresholds: list[float | None]) -> list[tuple]:
    out = []
    for thr in thresholds:
        u = _sp._disjoint(top_by(prepared, "adv24", top_n), hold)
        label = "off" if thr is None else f">{thr:+.2f}"
        if thr is not None:
            u = u.filter(pl.col("btc_30d") > thr)
        if u.height < 200:
            continue
        book = long_short(u, signal=signal, ret="net_return", sign=sign, cut=cut)
        if book.height < 20:
            continue
        turn = _sp.measure_turnover(u, [(signal, sign, 1.0)], cut)
        n, mean, t, sh = _t_and_mean(book, turn * fee_side, ppy)
        out.append((label, n, mean, t, sh, turn * fee_side))
    return out


def sweep_drop(prepared: pl.DataFrame, top_n: int, hold: int, fee_side: float, ppy: int,
               drops: list[float], gate: bool) -> list[tuple]:
    out = []
    for d in drops:
        u = _sp._disjoint(top_by(prepared, "adv24", top_n), hold)
        book = _sp.conditional_short_book(u, drop_threshold=d, require_btc_uptrend=gate)
        if book.height < 20:
            continue
        n, mean, t, sh = _t_and_mean(book, 2.0 * fee_side, ppy)
        out.append((f"{d * 100:.0f}%", n, mean, t, sh, 2.0 * fee_side))
    return out


def report(title: str, param: str, rows: list[tuple], baseline: str | None) -> None:
    print(f"\n{title}")
    print(f"  {param:>8s} {'n':>6s} {'mean bp':>9s} {'t':>7s} {'Sharpe':>7s} {'chg bp':>7s}   {'':3s}")
    print("  " + "-" * 60)
    if not rows:
        print("  (no admissible cells)")
        return
    ts = [r[3] for r in rows]
    best = max((t for t in ts if math.isfinite(t)), default=float("nan"))
    for label, n, mean, t, sh, chg in rows:
        mark = ""
        if math.isfinite(t) and math.isfinite(best) and t == best:
            mark = "<- max t"
        if baseline is not None and label == baseline:
            mark = (mark + "  [registered]").strip()
        print(f"  {label:>8s} {n:6d} {mean:+9.2f} {t:+7.2f} {sh:+7.2f} {chg:7.1f}   {mark}")
    print(f"  shape: {_curve_shape(ts)}")
    if math.isfinite(best):
        print(f"  peak t {best:+.2f} vs threshold {BONFERRONI_T} -> "
              f"{'CLEARS' if best >= BONFERRONI_T else 'still short'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel-root", type=Path, required=True)
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--hold-hours", type=int, default=24)
    ap.add_argument("--round-trip-bp", type=float, default=MEASURED_ROUND_TRIP_BP)
    args = ap.parse_args()

    panel = _sp.load_panel(args.panel_root)
    cfg = BlendConfig.from_json(REPO_ROOT / "configs" / "lane2_premium_momentum_blend_v1.json")
    cfg = type(cfg)(**{**cfg.__dict__, "hold_hours": args.hold_hours})
    view = _sp.venue_view(panel, args.venue)
    prepared = prepare(view, cfg)
    prepared = prepared.join(_sp.btc_trend(view), on="bar_ts_ms", how="left").with_columns(
        (pl.col("by_close") / pl.col("by_close").shift(96).over("symbol") - 1.0).alias("drop_4d")
    )
    fee_side = args.round_trip_bp / 2.0
    ppy = int(round(365 * 24 / args.hold_hours))
    hold = args.hold_hours

    print(f"PHASE 5A - tune for t, not mean   [{args.venue}]")
    print(f"panel {panel.height:,} rows | {panel['symbol'].n_unique()} symbols | "
          f"{hold}h disjoint holds | {args.round_trip_bp:.2f} bp round trip "
          f"({fee_side:.2f} bp/side, charged at each cell's measured turnover)")
    print(f"threshold t >= {BONFERRONI_T}. Whole curves below; the max-t cell is marked but NOT selected.")

    # cut must be strictly inside (0, 0.5); 0.45 is the loosest admissible cell.
    cuts = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.45]
    ns = [30, 50, 75, 100, 150, 200, 300, 500]
    gates = [None, -0.20, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20]
    drops = [-0.30, -0.20, -0.15, -0.10, -0.07, -0.05, -0.03]

    for name, sig, sign in [
        ("momentum_1w continuation", "momentum_1w", -1),
        ("funding carry", "by_funding", +1),
        ("premium_diff", "premium_diff_bp", +1),
    ]:
        print(f"\n\n{'=' * 66}\n{name}\n{'=' * 66}")
        report(f"[{name}] decile cut (universe top-100)", "cut",
               sweep_cut(prepared, sig, sign, 100, hold, fee_side, ppy, cuts), "0.10")
        report(f"[{name}] universe size (cut 0.10)", "top_n",
               sweep_top_n(prepared, sig, sign, 0.10, hold, fee_side, ppy, ns), "100")
        report(f"[{name}] BTC 30d regime gate (top-100, cut 0.10)", "gate",
               sweep_btc_gate(prepared, sig, sign, 100, 0.10, hold, fee_side, ppy, gates), "off")

    print(f"\n\n{'=' * 66}\nconditional short: 4d drop threshold\n{'=' * 66}")
    report("[short 4d drop] threshold, gate OFF", "drop",
           sweep_drop(prepared, 100, hold, fee_side, ppy, drops, False), "-10%")
    report("[short 4d drop] threshold, BTC uptrend gate ON", "drop",
           sweep_drop(prepared, 100, hold, fee_side, ppy, drops, True), "-10%")

    print("\n\nHow to read this: a PLATEAU means the parameter is not the thing "
          "carrying the result.\nA SPIKE means the cell is the result, which is overfitting. "
          "No cell here is\nselected as a candidate; selection requires clearing "
          f"t >= {BONFERRONI_T} and replicating in 2A.")
    print("\nLane-1 on seen data. Grades nothing; see AGENTS.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
