#!/usr/bin/env python3
"""Phase 5C hypothesis H1 — delta-neutral spot-perp carry vs the perp-only basket.

**External source, not this repository.** Robot Wealth, *"The Art and Science of
Trading Carry"*, describes the construction and — writing independently — predicts
the exact failure mode §18.4 measured:

    "An obvious one is shorting perpetual futures trading at a premium and longing
    the spot to hedge the risk, thus collecting the funding."

    "A messier variation is to create a long-short basket of perpetuals trading at
    a discount or premium, respectively. This trade will see a much higher return
    variance than the spot-perpetual version because the basket components will
    dislocate and do all sorts of weird idiosyncratic things."

§18.4's carry book *is* that messier variation, and it carries a 166-276% max
drawdown from precisely that idiosyncratic dislocation. The hypothesis under test
is that hedging each name with its own spot removes the dislocation while keeping
the funding.

**Mechanism and counterparty (why this should exist):** perpetual funding is paid
by leveraged directional longs to stay long. A short-perp/long-spot pair holds no
price view; it is paid for supplying the leverage those longs demand. The premium
is sticky because it is autocorrelated, so funding accrues faster than the basis
mean-reverts.

**Spot proxy — read this before believing any number here.** This repository holds
no spot dataset. The panel's ``by_index_close`` / venue index price is the venue's
own spot index (a basket of major spot exchanges) and is used as the spot leg. That
is a *mechanism* test, not an executable book: an index is synthetic, cannot be
bought, and a real spot leg carries its own fees, borrow, and per-exchange
tracking error. Treat a positive result as a reason to procure spot data, never as
a tradeable result.

Costs are charged honestly and asymmetrically: the perp side at the measured
7.78 bp/side, the spot side at 10.0 bp/side (Bybit/Binance spot taker is ~0.10%,
*worse* than perp). A delta-neutral pair therefore round-trips ~35.6 bp, more than
double the perp-only book.

Research-only. Lane-1 on seen data; grades nothing (``AGENTS.md``).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liquidity_migration.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    long_short,
    summary,
    top_by,
)
from liquidity_migration.lane2_blend import BlendConfig, prepare  # noqa: E402

_spec = importlib.util.spec_from_file_location("screen_phase1", REPO_ROOT / "scripts" / "screen_phase1.py")
assert _spec and _spec.loader
_sp = importlib.util.module_from_spec(_spec)
sys.modules["screen_phase1"] = _sp
_spec.loader.exec_module(_sp)

PERP_FEE_SIDE = MEASURED_ROUND_TRIP_BP / 2.0  # 7.78 bp, measured
SPOT_FEE_SIDE = 10.0  # ~0.10% Bybit/Binance spot taker


def attach_pair_return(prepared: pl.DataFrame, *, index_col: str, hold: int = 24) -> pl.DataFrame:
    """Attach the delta-neutral pair return to an **hourly** prepared frame.

    Per name over the hold: the short perp earns ``-perp_return``, the long spot
    earns ``+index_return``, and the short receives the funding a long would pay.
    Netting the two price legs leaves the *basis* change, so

        pair return = funding_received - (perp_return - index_return)

    which is the sticky-premium mechanism stated directly: you keep the funding
    unless the basis moves against you by more.

    This MUST run on the hourly frame, before any disjoint sampling. ``shift`` is
    positional, so on a 24h-sampled frame ``shift(-24)`` would reach 24 *days*
    ahead and difference a 24-day spot move against a 24-hour perp move. That bug
    produced +696 bp/day and Sharpe 4.09 on the first pass.
    """
    d = prepared.sort(["symbol", "bar_ts_ms"]).with_columns(
        pl.when(pl.col(index_col) > 0).then(pl.col(index_col)).otherwise(None).alias("_ix")
    )
    d = d.with_columns(
        (pl.col("_ix").shift(-hold).over("symbol") / pl.col("_ix") - 1.0).alias("index_return"),
        # Same contiguity guard prepare applies to the price leg: a gap must not
        # become a longer, unearned window.
        (
            pl.col("bar_ts_ms").shift(-hold).over("symbol") - pl.col("bar_ts_ms") == hold * 3_600_000
        ).alias("_ix_contig"),
    )
    d = d.filter(pl.col("_ix_contig") & pl.col("index_return").is_finite())
    # ``funding_paid`` is what a LONG pays over the window, so a short receives it.
    return d.with_columns(
        (pl.col("funding_paid") - (pl.col("price_return") - pl.col("index_return"))).alias("pair_return")
    )


def delta_neutral_book(universe: pl.DataFrame, *, cut: float) -> pl.DataFrame:
    """Equal-weight the top-``cut`` funding decile's delta-neutral pairs."""
    return (
        universe.with_columns(
            ((pl.col("by_funding").rank("ordinal").over("bar_ts_ms") - 0.5) / pl.len().over("bar_ts_ms")).alias("_p")
        )
        .filter(pl.col("_p") >= 1.0 - cut)
        .group_by("bar_ts_ms")
        .agg((pl.col("pair_return").mean() * 1e4).alias("ret_bp"))
        .sort("bar_ts_ms")
        .drop_nulls()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel-root", type=Path, required=True)
    ap.add_argument("--cut", type=float, default=0.10)
    args = ap.parse_args()

    panel = _sp.load_panel(args.panel_root)
    cfg = BlendConfig.from_json(REPO_ROOT / "configs" / "lane2_premium_momentum_blend_v1.json")
    ppy = 365

    # Delta-neutral pair: 2 legs opened + 2 closed, perp at 7.78, spot at 10.0.
    pair_cost = 2.0 * PERP_FEE_SIDE + 2.0 * SPOT_FEE_SIDE

    print("PHASE 5C / H1 - delta-neutral spot-perp carry (index price as spot proxy)")
    print(f"panel {panel.height:,} rows | {panel['symbol'].n_unique()} symbols | 24h disjoint holds")
    print(f"pair cost {pair_cost:.2f} bp/period (perp {PERP_FEE_SIDE:.2f}/side + spot {SPOT_FEE_SIDE:.2f}/side, both legs)")
    print(f"threshold t >= {_sp.BONFERRONI_T}\n")

    hdr = (f"{'venue':9s} {'construction':34s} {'n':>5s} {'mean bp':>9s} {'t':>7s} "
           f"{'Sharpe':>7s} {'wst1%':>8s} {'maxDD':>8s}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for venue, index_col in (("bybit", "by_index_close"),):
        view = _sp.venue_view(panel, venue)
        if index_col not in view.columns:
            print(f"  {venue}: no index column {index_col}; skipped")
            continue
        prep = prepare(view, cfg)
        u = _sp._disjoint(top_by(prep, "adv24", 100), 24)

        # Control: the perp-only cross-sectional basket from S18.4.
        perp_only = long_short(u, signal="by_funding", ret="net_return", sign=+1, cut=args.cut)
        turn = _sp.measure_turnover(u, [("by_funding", +1, 1.0)], args.cut)
        s_ctrl = summary(perp_only["ret_bp"].to_numpy(), periods_per_year=ppy, cost_bp=turn * PERP_FEE_SIDE)

        # Spot leg on the HOURLY frame, then rank/sample -- order matters, see
        # attach_pair_return's docstring.
        paired = attach_pair_return(prep, index_col=index_col)
        u_dn = _sp._disjoint(top_by(paired, "adv24", 100), 24)
        dn = delta_neutral_book(u_dn, cut=args.cut)
        s_dn = summary(dn["ret_bp"].to_numpy(), periods_per_year=ppy, cost_bp=pair_cost)
        # Gross, to separate the mechanism from the cost hurdle.
        s_dn_gross = summary(dn["ret_bp"].to_numpy(), periods_per_year=ppy, cost_bp=0.0)

        for label, s, book, cost in (
            ("perp-only basket (S18.4 control)", s_ctrl, perp_only, turn * PERP_FEE_SIDE),
            ("delta-neutral pair, GROSS", s_dn_gross, dn, 0.0),
            ("delta-neutral pair, net of cost", s_dn, dn, pair_cost),
        ):
            print(f"{venue:9s} {label:34s} {s.n:5d} {s.mean_bp:+9.2f} {s.t_stat:+7.2f} "
                  f"{s.sharpe:+7.2f} {s.worst_1pct_bp / 100:+7.2f}% {s.max_drawdown_pct:7.1f}%")
            rows.append((venue, label, s, book, cost))

    print("\nERA SPLITS")
    print("-" * 78)
    for venue, label, s, book, cost in rows:
        eras = _sp.era_split(book, cost)
        cells = "  ".join(f"{y}:{mn:+7.1f}" for y, (n, mn, t) in eras.items())
        neg = sum(1 for y, (n, mn, t) in eras.items() if mn < 0)
        print(f"  [{venue}] {label:34s} {cells}   neg_eras={neg}")

    print("\nTAIL COMPARISON is the hypothesis. Robot Wealth predicts the perp-only")
    print("basket has 'much higher return variance' from idiosyncratic dislocation.")
    print("\nSPOT PROXY CAVEAT: the index is a synthetic basket and cannot be bought.")
    print("A positive result here justifies procuring spot data; it is not a tradeable book.")
    print("\nLane-1 on seen data. Grades nothing; see AGENTS.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
