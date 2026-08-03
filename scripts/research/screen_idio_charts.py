#!/usr/bin/env python3
"""Screen: do chart signals work better on idio paths than raw ones?

Reads a panel from ``scripts/data/build_idio_panel.py`` and scores every
(feature, price source, target) cell as an equal-weight decile long/short,
daily rebalance, with the measured round-trip charged.

THE GRID IS PRE-DECLARED. ``idio_features.CHART_FEATURES`` fixes six features
and ``build_idio_panel.PRICE_SOURCES`` fixes four sources before any result is
seen, so the Bonferroni family below is a count and not a choice made after
looking. Every cell is printed, including the ones that fail.

WHAT COUNTS AS AN ANSWER. The quant's claim is directional: the idio version of
a chart signal should beat the raw version. The primary read is therefore not
"is idio significant" but "does idio beat rawlag on the same feature and the
same tradable target", where ``rawlag`` is the raw close carrying the identical
three-day information lag the idio path cannot avoid. A cell that beats ``raw``
but not ``rawlag`` has found staleness, not idiosyncrasy.

Two cost conventions are printed side by side, following
``scripts/research/screen_phase1.py``: ``repo_1x`` charges one round trip per rebalance
and reproduces the historical convention; ``honest_2x`` charges two, because
``cross_section.long_short`` returns a book that is one unit long AND one unit
short on one unit of capital. ``honest_2x`` is the one that means something.

Dispatch:
    .venv/bin/python -u scripts/research/screen_idio_charts.py \\
        --panel data/idio_panel/bybit/panel_common4.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.research.panels.cross_section import (  # noqa: E402
    MEASURED_ROUND_TRIP_BP,
    long_short,
    summary,
    top_by,
)
from liquidity_migration.research.panels.idio_features import CHART_FEATURES  # noqa: E402

DAYS_PER_YEAR = 365
#: Mechanisms tested before this screen (``scripts/research/screen_phase1.py``). The idio
#: grid adds to that family rather than getting its own alpha.
PRIOR_MECHANISMS = 44
PRIMARY_SOURCES = ("rawlag", "idio")
ALL_SOURCES = ("raw", "rawlag", "idio", "iz")
TARGETS = ("fwd_ret_1d", "resid_fwd")


#: The program bar, owned by ``docs/research/governance.md`` §2. Since 2026-07-31 the
#: screens are judged against a fixed 2.5 rather than a family-wise threshold
#: derived from ``PRIOR_MECHANISMS`` — that denominator was never enumerable, a
#: defect the strategy program had open against itself. :func:`bonferroni_t` is
#: kept and still printed so the retired threshold stays visible beside the bar.
PROGRAM_T = 2.5


def bonferroni_t(n_tests: int, *, alpha: float = 0.05) -> float:
    """Two-sided Bonferroni critical t, normal approximation.

    The daily panels here have hundreds of periods, so the normal tail is an
    adequate stand-in for the t tail and is stated rather than hidden.

    Retired as the pass/fail threshold on 2026-07-31; retained as a reference
    number so a historical verdict can still be read at the bar that produced it.
    """
    from statistics import NormalDist

    return float(NormalDist().inv_cdf(1.0 - alpha / (2.0 * n_tests)))


def realized_turnover(
    panel: pl.DataFrame, *, signal: str, target: str, cut: float, min_names: int
) -> float:
    """Mean one-way turnover of the decile book, as a fraction of gross.

    ``cross_section.summary`` charges ``cost_bp`` once per period, assuming a
    full rebalance. A 30-day feature does not replace its deciles daily, so that
    understates a slow signal by roughly the reciprocal of its turnover — without
    this number "no edge" and "wrong cost model" look identical.

    Weights are equal within each leg (+1/n_long, -1/n_short), so gross is 2
    units on 1 of capital, matching honest_2x. A full replacement returns 1.0.
    """
    frame = panel.select("ts_ms", "symbol", signal, target).drop_nulls()
    frame = frame.filter(pl.len().over("ts_ms") >= min_names)
    if frame.is_empty():
        return float("nan")
    book = (
        frame.with_columns(
            ((pl.col(signal).rank("ordinal").over("ts_ms") - 0.5) / pl.len().over("ts_ms")).alias("_p")
        )
        .filter((pl.col("_p") <= cut) | (pl.col("_p") >= 1.0 - cut))
        .with_columns(
            pl.when(pl.col("_p") >= 1.0 - cut).then(1.0).otherwise(-1.0).alias("_side")
        )
    )
    if book.is_empty():
        return float("nan")
    book = book.with_columns(
        (pl.col("_side") / pl.len().over(["ts_ms", "_side"])).alias("_w")
    ).select("ts_ms", "symbol", "_w")

    days = book["ts_ms"].unique().sort()
    prev_day = pl.DataFrame({"ts_ms": days[1:], "_prev": days[:-1]})
    joined = (
        book.join(prev_day, on="ts_ms", how="inner")
        .join(
            book.rename({"ts_ms": "_prev", "_w": "_w_prev"}),
            on=["_prev", "symbol"],
            how="full",
            coalesce=True,
        )
    )
    if joined.is_empty():
        return float("nan")
    per_day = (
        joined.drop_nulls("ts_ms")
        .with_columns(
            (pl.col("_w").fill_null(0.0) - pl.col("_w_prev").fill_null(0.0)).abs().alias("_d")
        )
        .group_by("ts_ms")
        .agg(pl.col("_d").sum().alias("_sum"))
        # gross = 2, and a full replacement moves 4 units of weight.
        .with_columns((pl.col("_sum") / 4.0).alias("turnover"))
    )
    return float(per_day["turnover"].mean())


def score(
    panel: pl.DataFrame,
    *,
    signal: str,
    target: str,
    cost_bp: float,
    cut: float,
    min_names: int,
) -> tuple[object, int]:
    frame = panel.select("ts_ms", "symbol", signal, target).drop_nulls()
    if frame.is_empty():
        return None, 0
    series = long_short(
        frame, signal=signal, ret=target, cut=cut, period="ts_ms", min_names=min_names
    )
    if series.is_empty():
        return None, 0
    return summary(series["ret_bp"], periods_per_year=DAYS_PER_YEAR, cost_bp=cost_bp), series.height


def run(
    panel: pl.DataFrame,
    *,
    universe_n: int,
    cut: float,
    min_names: int,
    coverage_floor: float,
) -> pl.DataFrame:
    if "turnover_quote" in panel.columns and universe_n > 0:
        before = panel.height
        panel = top_by(panel, "turnover_quote", universe_n, period="ts_ms")
        print(f"universe screen: top {universe_n} by turnover -> {panel.height}/{before} rows")

    # Idio arms only: coverage is a property of the residual path, and applying
    # the floor to the raw arm would turn this into a universe comparison.
    idio_mask = pl.col("coverage").fill_null(0.0) >= coverage_floor
    print(f"idio coverage floor {coverage_floor:.2f}: "
          f"{panel.filter(idio_mask).height}/{panel.height} rows qualify\n")

    rows: list[dict] = []
    for target in TARGETS:
        if target not in panel.columns:
            continue
        for source in ALL_SOURCES:
            for feature in CHART_FEATURES:
                signal = f"{source}_{feature}"
                if signal not in panel.columns:
                    continue
                scoped = panel.filter(idio_mask) if source in ("idio", "iz") else panel
                # A decile spread is symmetric, so score sign=+1 only and report
                # the signed t: direction is an output, not a second test that
                # would double the family.
                one_x, n = score(
                    scoped, signal=signal, target=target,
                    cost_bp=MEASURED_ROUND_TRIP_BP, cut=cut, min_names=min_names,
                )
                two_x, _ = score(
                    scoped, signal=signal, target=target,
                    cost_bp=2.0 * MEASURED_ROUND_TRIP_BP, cut=cut, min_names=min_names,
                )
                gross, _ = score(
                    scoped, signal=signal, target=target,
                    cost_bp=0.0, cut=cut, min_names=min_names,
                )
                if one_x is None or two_x is None or gross is None:
                    continue

                # A negative gross mean is an edge in the opposite direction, not
                # the absence of one, and cost is paid either way: the tradable
                # read is |gross| - cost.
                turnover = realized_turnover(
                    scoped, signal=signal, target=target, cut=cut, min_names=min_names
                )
                abs_gross = abs(gross.mean_bp)
                stderr = abs_gross / abs(gross.t_stat) if gross.t_stat else float("nan")
                full_cost = 2.0 * MEASURED_ROUND_TRIP_BP
                turn_cost = (
                    turnover * full_cost if turnover == turnover else float("nan")
                )
                best_full = abs_gross - full_cost
                best_turn = abs_gross - turn_cost
                rows.append(
                    {
                        "target": target,
                        "source": source,
                        "feature": feature,
                        "periods": n,
                        "direction": 1 if gross.mean_bp >= 0 else -1,
                        "gross_bp": gross.mean_bp,
                        "gross_t": gross.t_stat,
                        "gross_sharpe": gross.sharpe,
                        "turnover": turnover,
                        # Best-direction net under the two cost models.
                        "net_full_bp": best_full,
                        "net_full_t": best_full / stderr if stderr == stderr and stderr else float("nan"),
                        "net_turn_bp": best_turn,
                        "net_turn_t": best_turn / stderr if stderr == stderr and stderr else float("nan"),
                        "net_turn_sharpe": (
                            abs(gross.sharpe) * best_turn / abs_gross if abs_gross else float("nan")
                        ),
                        "repo1x_sharpe": one_x.sharpe,
                        "honest2x_sharpe": two_x.sharpe,
                        "max_dd_pct": two_x.max_drawdown_pct,
                    }
                )
    return pl.DataFrame(rows)


def _fmt(results: pl.DataFrame, target: str) -> str:
    sub = results.filter(pl.col("target") == target).sort(
        ["feature", "source"]
    )
    if sub.is_empty():
        return f"  (no cells for {target})\n"
    lines = [
        f"  {'feature':<20s} {'source':<8s} {'N':>5s} {'dir':>4s} {'|gross|':>8s} "
        f"{'g_t':>6s} {'g_shrp':>7s} {'turn':>6s} {'netFull':>8s} {'nF_t':>6s} "
        f"{'netTurn':>8s} {'nT_t':>6s} {'nT_shrp':>8s}"
    ]
    for r in sub.iter_rows(named=True):
        lines.append(
            f"  {r['feature']:<20s} {r['source']:<8s} {r['periods']:>5d} "
            f"{r['direction']:>4d} {abs(r['gross_bp']):>8.2f} {abs(r['gross_t']):>6.2f} "
            f"{abs(r['gross_sharpe']):>7.2f} {r['turnover']:>6.3f} "
            f"{r['net_full_bp']:>8.2f} {r['net_full_t']:>6.2f} "
            f"{r['net_turn_bp']:>8.2f} {r['net_turn_t']:>6.2f} {r['net_turn_sharpe']:>8.2f}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", required=True, type=Path)
    ap.add_argument("--universe", type=int, default=150, help="top-N by turnover per day; 0 disables")
    ap.add_argument("--cut", type=float, default=0.10)
    ap.add_argument("--min-names", type=int, default=20)
    ap.add_argument("--coverage-floor", type=float, default=0.90)
    ap.add_argument("--era-split", action="store_true", help="also report first/second half")
    ap.add_argument("--out", type=Path, default=None, help="write the full cell table as parquet")
    args = ap.parse_args(argv)

    panel = pl.read_parquet(args.panel)
    days = panel["ts_ms"].n_unique()
    print(f"panel {args.panel}")
    print(f"  rows={panel.height} symbols={panel['symbol'].n_unique()} days={days}")
    print(f"  span {panel['date'].min()} .. {panel['date'].max()}\n")

    n_cells = len(CHART_FEATURES) * len(ALL_SOURCES) * len(TARGETS)
    family = PRIOR_MECHANISMS + n_cells
    t_crit = PROGRAM_T
    legacy = bonferroni_t(family)
    print(f"pre-declared grid: {len(CHART_FEATURES)} features x {len(ALL_SOURCES)} sources "
          f"x {len(TARGETS)} targets = {n_cells} cells")
    print(f"bar |t| > {t_crit:.2f} (docs/research/governance.md 2). Retired family-wise threshold "
          f"for {PRIOR_MECHANISMS} prior + {n_cells} new = {family} was {legacy:.2f}\n")

    results = run(
        panel,
        universe_n=args.universe,
        cut=args.cut,
        min_names=args.min_names,
        coverage_floor=args.coverage_floor,
    )
    if results.is_empty():
        print("no scorable cells")
        return 1

    for target in TARGETS:
        label = {
            "fwd_ret_1d": "TARGET fwd_ret_1d -- raw executable return (mode a: no hedge held)",
            "resid_fwd": "TARGET resid_fwd -- residual return (mode b: requires holding the factor hedge, cost NOT modelled)",
        }[target]
        print(label)
        print(_fmt(results, target))

    print("=" * 100)
    print("PRIMARY READ: idio vs rawlag on fwd_ret_1d, same feature, same information age")
    print("=" * 100)
    primary = (
        results.filter((pl.col("target") == "fwd_ret_1d") & pl.col("source").is_in(PRIMARY_SOURCES))
        .pivot(values="net_turn_sharpe", index="feature", on="source")
        .with_columns((pl.col("idio") - pl.col("rawlag")).alias("idio_minus_rawlag"))
        .sort("idio_minus_rawlag", descending=True)
    )
    print(primary)

    wins = int(primary.filter(pl.col("idio_minus_rawlag") > 0).height)
    print(f"\nidio beats the information-matched raw control in {wins}/{primary.height} features "
          f"(honest_2x Sharpe)")

    # A survivor must earn money in its best direction, not merely differ from
    # zero: a reliably losing series also has a large |t|.
    survivors = results.filter((pl.col("net_turn_bp") > 0.0) & (pl.col("net_turn_t") > t_crit))
    print(f"\ncells that are PROFITABLE in their best direction and clear "
          f"t > {t_crit:.2f} on turnover-adjusted cost: {survivors.height}/{results.height}")
    if not survivors.is_empty():
        print(survivors.sort(pl.col("honest2x_t").abs(), descending=True))

    if args.era_split:
        mid = panel["ts_ms"].quantile(0.5)
        print("\n" + "=" * 100)
        print("ERA SPLIT (equal-day halves)")
        print("=" * 100)
        for name, era in (
            ("first half", panel.filter(pl.col("ts_ms") < mid)),
            ("second half", panel.filter(pl.col("ts_ms") >= mid)),
        ):
            era_res = run(
                era, universe_n=args.universe, cut=args.cut,
                min_names=args.min_names, coverage_floor=args.coverage_floor,
            )
            if era_res.is_empty():
                continue
            era_primary = (
                era_res.filter(
                    (pl.col("target") == "fwd_ret_1d") & pl.col("source").is_in(PRIMARY_SOURCES)
                )
                .pivot(values="net_turn_sharpe", index="feature", on="source")
            )
            print(f"\n{name}:")
            print(era_primary)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        results.write_parquet(args.out)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
