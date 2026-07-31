#!/usr/bin/env python3
"""Phase 1 re-screen: the mechanisms that survived something, priced honestly.

One pass, not a sweep (`docs/research_findings.md` §3). Every cell is reported;
a survivor must clear the Bonferroni threshold **t = 3.25** for the ~44
mechanisms this program has tested, not t = 2.

Two cost corrections are applied together, because both were wrong in the same
direction:

1. **Fee level.** The measured round trip is
   :data:`liquidity_migration.research.panels.cross_section.MEASURED_ROUND_TRIP_BP` (15.56 bp),
   not the 4 bp maker assumption the earlier reads used.
2. **Gross multiple.** ``cross_section.long_short`` returns a book that is
   *1 unit long + 1 unit short on one unit of capital* — 2x gross, as
   ``configs/lane2_premium_momentum_blend_v1.json`` states outright. Turning that
   over each period round-trips **two** units of notional, so the honest charge
   is ``2 x round_trip``. The repository convention charged one, which
   understates cost by 2x independently of the fee error.

Both conventions are printed side by side. `repo_1x` reproduces the historical
numbers; `honest_2x` is the one that means something.

Input: a panel built by ``scripts/data/build_cross_venue_panel.py``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.research.panels.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    Summary,
    long_short,
    summary,
    top_by,
)
from liquidity_migration.research.panels.lane2_blend import HOUR_MS, BlendConfig, prepare  # noqa: E402

#: Bonferroni threshold at alpha=0.05 for the ~44 mechanisms tested to date.
BONFERRONI_T = 3.25
DAY_MS = 86_400_000


def load_panel(root: Path) -> pl.DataFrame:
    shards = sorted(root.glob("*/panel.parquet"))
    if not shards:
        raise SystemExit(f"no panel shards under {root}")
    frames = [pl.read_parquet(p) for p in shards]
    common = set(frames[0].columns)
    for f in frames[1:]:
        common &= set(f.columns)
    cols = [c for c in frames[0].columns if c in common]
    return pl.concat([f.select(cols) for f in frames], how="vertical").sort(
        ["symbol", "bar_ts_ms"]
    )


#: Columns that make a venue the *traded* venue. ``prepare`` is written against
#: the Bybit names, so a Binance read is the same code over a renamed view —
#: both arms of the replication run one implementation.
BINANCE_VIEW = {
    "bn_close": "by_close",
    "bn_turnover_quote": "by_turnover_quote",
    "bn_funding": "by_funding",
    "bn_funding_age_h": "by_funding_age_h",
}


def venue_view(panel: pl.DataFrame, venue: str) -> pl.DataFrame:
    """Return the panel with ``venue`` as the traded venue.

    ``premium_diff_bp`` stays as built — a cross-venue difference is the same
    observable on either side. What changes is whose price you realise and whose
    funding you pay; a Bybit leg net of funding against a gross Binance leg is
    not a comparison.
    """
    if venue == "bybit":
        return panel
    if venue != "binance":
        raise ValueError(f"unknown venue {venue!r}")
    missing = [c for c in BINANCE_VIEW if c not in panel.columns]
    if missing:
        raise ValueError(f"panel lacks Binance columns: {missing}")
    keep = [c for c in panel.columns if c not in set(BINANCE_VIEW.values())]
    return panel.select(keep).rename(BINANCE_VIEW)


def _disjoint(universe: pl.DataFrame, hold_hours: int) -> pl.DataFrame:
    """Sample entries every ``hold_hours`` so holding windows never overlap.

    Overlapping entries do not bias the mean but badly inflate its t-statistic.
    """
    origin = int(universe["bar_ts_ms"].min())  # type: ignore[arg-type]
    return universe.with_columns(
        ((pl.col("bar_ts_ms") - origin) // HOUR_MS).alias("_off")
    ).filter(pl.col("_off") % hold_hours == 0)


def btc_trend(panel: pl.DataFrame, lookback_days: int = 30) -> pl.DataFrame:
    """Prior-N-day BTC return per decision bar, excluding the current bar.

    Mirrors ``continuous_events._btc_trend_returns``: the current bar is not in
    the window, so the regime value is known before the signal it gates.
    """
    h = lookback_days * 24
    btc = (
        panel.filter(pl.col("symbol") == "BTCUSDT")
        .sort("bar_ts_ms")
        .select("bar_ts_ms", "by_close")
    )
    return btc.with_columns(
        (pl.col("by_close").shift(1) / pl.col("by_close").shift(1 + h) - 1.0).alias("btc_30d")
    ).select("bar_ts_ms", "btc_30d")


def basket_short_book(universe: pl.DataFrame, signal: str, cut: float) -> pl.DataFrame:
    """Long the favoured decile, short an equal-notional basket of the universe.

    The tail of a decile short is ~95% idiosyncratic, so a short leg with no
    single-name exposure cannot carry it.
    """
    d = universe.select("bar_ts_ms", signal, "net_return").drop_nulls()
    return (
        d.with_columns(
            ((pl.col(signal).rank("ordinal").over("bar_ts_ms") - 0.5) / pl.len().over("bar_ts_ms")).alias("_p")
        )
        .group_by("bar_ts_ms")
        .agg(
            pl.col("net_return").filter(pl.col("_p") <= cut).mean().alias("_leg"),
            pl.col("net_return").mean().alias("_basket"),
        )
        .with_columns(((pl.col("_leg") - pl.col("_basket")) * 1e4).alias("ret_bp"))
        .select("bar_ts_ms", "ret_bp")
        .sort("bar_ts_ms")
        .drop_nulls()
    )


def conditional_short_book(
    universe: pl.DataFrame, *, drop_threshold: float, require_btc_uptrend: bool
) -> pl.DataFrame:
    """Short names that fell hard, optionally only in a BTC uptrend.

    Short-only and therefore directional: it carries market exposure the decile
    books do not.
    """
    d = universe.filter(pl.col("drop_4d") < drop_threshold)
    if require_btc_uptrend:
        d = d.filter(pl.col("btc_30d") > 0.0)
    return (
        d.group_by("bar_ts_ms")
        .agg((-pl.col("net_return").mean() * 1e4).alias("ret_bp"))
        .sort("bar_ts_ms")
        .drop_nulls()
    )


def _leg_weights(universe: pl.DataFrame, signal: str, cut: float, sign: int) -> pl.DataFrame:
    """Per-period equal-weight decile positions for one signal, signed."""
    d = (
        universe.select("bar_ts_ms", "symbol", signal)
        .drop_nulls()
        .with_columns(
            ((pl.col(signal).rank("ordinal").over("bar_ts_ms") - 0.5) / pl.len().over("bar_ts_ms")).alias("_p")
        )
        .filter((pl.col("_p") <= cut) | (pl.col("_p") >= 1.0 - cut))
        .with_columns(
            pl.when(pl.col("_p") <= cut)
            .then(pl.lit(float(sign)))
            .otherwise(pl.lit(-float(sign)))
            .alias("_side")
        )
    )
    counts = d.group_by(["bar_ts_ms", "_side"]).agg(pl.len().alias("_n"))
    return d.join(counts, on=["bar_ts_ms", "_side"], how="left").select(
        "bar_ts_ms", "symbol", (pl.col("_side") / pl.col("_n")).alias("w")
    )


def measure_turnover(universe: pl.DataFrame, specs: list[tuple[str, int, float]], cut: float) -> float:
    """Mean one-way notional traded per period, measured from actual positions.

    A full rebalance into a disjoint name set trades 4.0 units per period (close
    both legs, open both), 31.12 bp at 7.78 bp/side. Consecutive deciles overlap
    and a name held through pays nothing, so the charge is
    ``turnover x fee_per_side`` with turnover measured rather than assumed.

    ``specs`` is a list of ``(signal, sign, weight)`` so a blended book's netted
    positions are measured, not approximated by averaging two separate books.
    Returns units of notional per period; 4.0 means no overlap at all.
    """
    combined: pl.DataFrame | None = None
    for signal, sign, weight in specs:
        part = _leg_weights(universe, signal, cut, sign).with_columns(
            (pl.col("w") * weight).alias("w")
        )
        combined = part if combined is None else pl.concat([combined, part], how="vertical")
    if combined is None or combined.height == 0:
        return 4.0
    agg = combined.group_by(["bar_ts_ms", "symbol"]).agg(pl.col("w").sum())
    periods = sorted(int(t) for t in agg["bar_ts_ms"].unique().to_list())
    if len(periods) < 2:
        return 4.0
    by_period = {
        int(k[0] if isinstance(k, tuple) else k): dict(zip(v["symbol"].to_list(), v["w"].to_list()))
        for k, v in agg.partition_by("bar_ts_ms", as_dict=True).items()
    }
    total = 0.0
    for prev, cur in zip(periods, periods[1:]):
        a, b = by_period.get(prev, {}), by_period.get(cur, {})
        total += sum(abs(b.get(s, 0.0) - a.get(s, 0.0)) for s in set(a) | set(b))
    return total / (len(periods) - 1)


def era_split(book: pl.DataFrame, cost_bp: float) -> dict[int, tuple[int, float, float]]:
    out: dict[int, tuple[int, float, float]] = {}
    years = [dt.datetime.fromtimestamp(t / 1000, dt.UTC).year for t in book["bar_ts_ms"].to_list()]
    r = book["ret_bp"].to_numpy() - cost_bp
    for y in sorted(set(years)):
        m = np.array([yy == y for yy in years])
        s = r[m]
        if s.size < 5:
            continue
        sd = s.std(ddof=1)
        t = s.mean() / (sd / math.sqrt(s.size)) if sd > 0 else float("nan")
        out[y] = (int(s.size), float(s.mean()), float(t))
    return out




@dataclasses.dataclass(frozen=True)
class Cell:
    """One scored mechanism on one venue, at both cost conventions."""

    venue: str
    name: str
    gated: bool
    note: str
    charged_bp: float
    book: pl.DataFrame
    repo_1x: Summary
    honest: Summary


def build_cells(
    panel: pl.DataFrame, venue: str, cfg: BlendConfig, args
) -> tuple[list[Cell], set[tuple[int, str]]]:
    """Score every Phase 1 mechanism on one venue, gated and ungated.

    Also returns the ungated universe as ``{(bar_ts_ms, symbol)}``: each arm
    ranks liquidity on the venue it trades, so the replication can report the
    shared name-set fraction as a number.
    """
    view = venue_view(panel, venue)
    prepared = prepare(view, cfg)
    # BTC trend comes from the traded venue's own BTC price.
    prepared = prepared.join(btc_trend(view), on="bar_ts_ms", how="left").with_columns(
        (pl.col("by_close") / pl.col("by_close").shift(96).over("symbol") - 1.0).alias("drop_4d")
    )
    full = _disjoint(top_by(prepared, "adv24", args.top_n), args.hold_hours)
    gated_u = full.filter(pl.col("btc_30d") > 0.0)
    ppy = int(round(365 * 24 / args.hold_hours))
    fee_side = args.round_trip_bp / 2.0
    pw, mw = cfg.premium_weight, cfg.momentum_weight

    cells: list[Cell] = []
    for gated, u in ((False, full), (True, gated_u)):
        if u.height < 50:
            continue
        premium = long_short(u, signal="premium_diff_bp", ret="net_return", sign=+1, cut=args.cut)
        momentum = long_short(u, signal="momentum_1w", ret="net_return", sign=-1, cut=args.cut)
        carry = long_short(u, signal="by_funding", ret="net_return", sign=+1, cut=args.cut)
        blend = (
            premium.rename({"ret_bp": "p"})
            .join(momentum.rename({"ret_bp": "m"}), on="bar_ts_ms", how="inner")
            .with_columns((pw * pl.col("p") + mw * pl.col("m")).alias("ret_bp"))
            .select("bar_ts_ms", "ret_bp")
        )
        t_prem = measure_turnover(u, [("premium_diff_bp", +1, 1.0)], args.cut)
        t_mom = measure_turnover(u, [("momentum_1w", -1, 1.0)], args.cut)
        t_carry = measure_turnover(u, [("by_funding", +1, 1.0)], args.cut)
        t_blend = measure_turnover(u, [("premium_diff_bp", +1, pw), ("momentum_1w", -1, mw)], args.cut)
        # Basket short: the decile long leg rotates; the basket side is the whole
        # universe and only turns over as the universe changes.
        t_basket = t_prem / 2.0 + 1.0

        spec: list[tuple[str, pl.DataFrame, float, str]] = [
            ("premium_diff", premium, t_prem, "long/short decile"),
            ("momentum_1w continuation", momentum, t_mom, "long/short decile"),
            ("blend 50/50 (registered)", blend, t_blend, "long/short decile, netted"),
            ("funding carry (dead control)", carry, t_carry, "long/short decile"),
            ("basket short (S11.2 B)", basket_short_book(u, "premium_diff_bp", args.cut), t_basket,
             "decile long vs basket short"),
            ("short 4d drop < -10%",
             conditional_short_book(u, drop_threshold=-0.10, require_btc_uptrend=False),
             2.0, "SHORT-ONLY, directional"),
        ]
        for name, book, turn, note in spec:
            if book.height < 5:
                continue
            r = book["ret_bp"].to_numpy()
            charged = turn * fee_side
            cells.append(
                Cell(
                    venue=venue,
                    name=name,
                    gated=gated,
                    note=note,
                    charged_bp=charged,
                    book=book,
                    repo_1x=summary(r, periods_per_year=ppy, cost_bp=args.round_trip_bp),
                    honest=summary(r, periods_per_year=ppy, cost_bp=charged),
                )
            )
    keys = {
        (int(t), str(sym))
        for t, sym in zip(full["bar_ts_ms"].to_list(), full["symbol"].to_list())
    }
    return cells, keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel-root", type=Path, required=True)
    ap.add_argument("--venues", default="bybit,binance", help="comma list: bybit, binance")
    ap.add_argument("--top-n", type=int, default=100)
    ap.add_argument("--hold-hours", type=int, default=24)
    ap.add_argument("--cut", type=float, default=0.10)
    ap.add_argument("--round-trip-bp", type=float, default=MEASURED_ROUND_TRIP_BP)
    args = ap.parse_args()

    panel = load_panel(args.panel_root)
    cfg = BlendConfig.from_json(
        Path(__file__).resolve().parents[2] / "configs" / "lane2_premium_momentum_blend_v1.json"
    )
    cfg = type(cfg)(**{**cfg.__dict__, "hold_hours": args.hold_hours, "decile_cut": args.cut})
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]

    years = sorted({dt.datetime.fromtimestamp(t / 1000, dt.UTC).year for t in panel["bar_ts_ms"].to_list()})
    print(f"panel {panel.height:,} rows | {panel['symbol'].n_unique()} symbols | "
          f"{years[0]}..{years[-1]} | top-{args.top_n} | {args.hold_hours}h disjoint holds")
    print(f"round trip {args.round_trip_bp:.2f} bp ({args.round_trip_bp / 2:.2f} bp/side)"
          f"   threshold t >= {BONFERRONI_T}")

    cells: list[Cell] = []
    universes: dict[str, set[tuple[int, str]]] = {}
    for v in venues:
        vcells, vkeys = build_cells(panel, v, cfg, args)
        cells.extend(vcells)
        universes[v] = vkeys

    print("\n\nPHASE 1 - every cell, ungated, at the honest cost basis")
    print("=" * 100)
    hdr = (f"{'venue':9s} {'mechanism':30s} {'n':>5s} {'repo_1x':>17s} "
           f"{'honest':>17s} {'chg':>6s} {'survives':>9s}")
    print(hdr)
    print("-" * len(hdr))
    for c in [x for x in cells if not x.gated]:
        ok = "YES" if c.honest.t_stat >= BONFERRONI_T else "no"
        print(f"{c.venue:9s} {c.name:30s} {c.honest.n:5d} "
              f"{c.repo_1x.mean_bp:8.2f}bp t{c.repo_1x.t_stat:5.2f} "
              f"{c.honest.mean_bp:8.2f}bp t{c.honest.t_stat:5.2f} {c.charged_bp:5.1f} {ok:>9s}")

    survivors = [c for c in cells if not c.gated and c.honest.t_stat >= BONFERRONI_T]
    print(f"\nGATE 1: {len(survivors)} of {len([c for c in cells if not c.gated])} cells clear t >= {BONFERRONI_T}.")

    print("\n\n2A - CROSS-VENUE REPLICATION (sign agreement and effect ratio)")
    print("=" * 100)
    if len(venues) < 2:
        print("  needs two venues")
    else:
        ua, ub = universes[venues[0]], universes[venues[1]]
        inter = len(ua & ub)
        print(f"  universe overlap: {inter:,} shared name-periods of "
              f"{len(ua):,} / {len(ub):,} ({inter / max(len(ua | ub), 1) * 100:.1f}% Jaccard). "
              f"Each arm ranks turnover on the venue it trades.\n")
        by = {(c.venue, c.name): c for c in cells if not c.gated}
        names = [c.name for c in cells if c.venue == venues[0] and not c.gated]
        print(f"{'mechanism':30s} {venues[0]:>12s} {venues[1]:>12s} {'ratio':>8s} {'same sign':>10s} {'verdict':>10s}")
        print("-" * 88)
        for n in names:
            a, b = by.get((venues[0], n)), by.get((venues[1], n))
            if not a or not b:
                continue
            ma, mb = a.honest.mean_bp, b.honest.mean_bp
            same = (ma > 0) == (mb > 0)
            ratio = (mb / ma) if ma != 0 else float("nan")
            # Kill rule: opposite sign, or ratio outside [0.5, 2.0].
            kill = (not same) or not (0.5 <= ratio <= 2.0)
            print(f"{n:30s} {ma:11.2f}bp {mb:11.2f}bp {ratio:8.2f} {str(same):>10s} "
                  f"{'KILLED' if kill else 'replicates':>10s}")

    print("\n\n2B - REGIME CONDITIONING (BTC 30d uptrend gate, per book)")
    print("=" * 100)
    print(f"{'venue':9s} {'mechanism':30s} {'ungated':>20s} {'gated':>20s} {'helped':>8s}")
    print("-" * 92)
    gmap = {(c.venue, c.name): c for c in cells if c.gated}
    helped = total = 0
    for c in [x for x in cells if not x.gated]:
        g = gmap.get((c.venue, c.name))
        if not g:
            continue
        total += 1
        better = g.honest.sharpe > c.honest.sharpe
        helped += int(better)
        print(f"{c.venue:9s} {c.name:30s} "
              f"Sh{c.honest.sharpe:+6.2f} t{c.honest.t_stat:+5.2f} n{c.honest.n:5d} "
              f"Sh{g.honest.sharpe:+6.2f} t{g.honest.t_stat:+5.2f} n{g.honest.n:5d} "
              f"{'yes' if better else 'no':>8s}")
    if total:
        print(f"\n  gate improved Sharpe on {helped}/{total} books "
              f"({helped / total * 100:.0f}%). Kill rule: fewer than half is a kill.")

    print("\n\nDETAIL - honest cost basis, era splits, every cell")
    print("=" * 100)
    for c in cells:
        tag = "BTC-gated" if c.gated else "ungated"
        print(f"\n[{c.venue}] {c.name} ({tag})   [{c.note}]   charged {c.charged_bp:.2f} bp/period")
        h = c.honest
        print(f"  n={h.n}  mean {h.mean_bp:+.2f} bp  t {h.t_stat:+.2f}  Sharpe {h.sharpe:+.2f}  ann {h.ann_pct:+.1f}%")
        print(f"  maxDD {h.max_drawdown_pct:.1f}%  worst1% {h.worst_1pct_bp:+.0f}bp  "
              f"tailconc {h.tail_concentration_pct:.1f}%  hit {h.hit_rate_pct:.1f}%")
        eras = era_split(c.book, c.charged_bp)
        cells_txt = "  ".join(f"{y}: {mn:+7.1f}bp t{t:+5.2f} (n={n})" for y, (n, mn, t) in eras.items())
        print(f"  eras  {cells_txt}")

    print("\nLane-1 on seen data. Grades nothing; see AGENTS.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
