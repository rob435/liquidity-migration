"""Reusable cross-sectional evaluation primitives for Lane-1 anomaly reads.

The panel is the durable artifact and individual probes are disposable, but the
*scoring* should not be rewritten per probe — that is where silent
inconsistencies (a missing funding leg, a different cut, an overlapping-sample
t-stat) creep in and make two reads incomparable.

Conventions, fixed here so every read means the same thing:

* Signals are ranked **within each period**, so a book is market-neutral by
  construction and a cross-sectional read never smuggles in market direction.
* ``sign=+1`` means "long the LOW end of the ranked variable". A negative
  result is reported as negative; callers are expected to state the profitable
  direction rather than silently flipping a sign to flatter a result.
* Returns passed in should already include every economic leg the trade has —
  for a funding-sorted signal, price alone has the wrong sign (see
  ``docs/anomaly_research_2026-07-24.md`` §4.1).
* ``summary`` reports the tail statistics this repository cares about, not just
  a Sharpe: loss concentration in the worst 1% and max drawdown, because the
  book being replaced failed on tail shape rather than on mean.

Nothing here grades anything. Lane-1 reads on seen data cannot grade the
configurations they selected; that requires a committed config scored on
post-commit days per ``docs/governance.md``.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import polars as pl

DEFAULT_CUT = 0.10

#: Measured round-trip execution cost in basis points, and the default cost basis
#: for every Lane-1 read in this repository.
#:
#: Provenance: measured 2026-07-25 from the archived pre-reset demo account
#: journal (the 2026-07-22 owner-authorized reset), read through
#: ``liquidity_migration.account_kernel``. 85 fills, 4,406.62 USDT notional,
#: 7.78 bp/side notional-weighted (median 11.00, range 5.50-11.00) => a 15.56 bp
#: round trip. The distribution sits exactly on Bybit's taker tiers, i.e. fills
#: price as **taker**, not maker.
#:
#: This replaces the 4.00 bp maker assumption used by the cross-venue anomaly
#: reads and by ``configs/lane2_premium_momentum_blend_v1.json``. A result that
#: only survives at 4 bp is not a result
#: (``docs/roadmap_2026-07-25.md`` §2, task 0.1).
#:
#: Note this is NOT a repo-wide correction: the LONG and CONTINUOUS engine
#: surfaces were never priced at 4 bp. See ``docs/anomaly_research_2026-07-24.md``
#: §16.1 for the per-surface audit.
MEASURED_ROUND_TRIP_BP = 15.56

#: Round trip a perfect passive book would pay, from the paper passive-execution
#: A/B (``docs/anomaly_research_2026-07-24.md`` §13): 2.70 bp/side implied at a
#: 100% passive fill rate. The floor, never an achieved cost.
PASSIVE_FLOOR_ROUND_TRIP_BP = 5.40


class CrossSectionError(ValueError):
    """A cross-sectional read was requested with incoherent inputs."""


@dataclasses.dataclass(frozen=True)
class Summary:
    """Descriptive statistics for one long/short return series, in bp."""

    n: int
    mean_bp: float
    t_stat: float
    sharpe: float
    ann_pct: float
    max_drawdown_pct: float
    worst_1pct_bp: float
    tail_concentration_pct: float
    hit_rate_pct: float

    def as_dict(self) -> dict[str, float | int]:
        return dataclasses.asdict(self)


def top_by(frame: pl.DataFrame, column: str, n: int, *, period: str = "bar_ts_ms") -> pl.DataFrame:
    """Restrict each period to its ``n`` largest rows by ``column``.

    Used for a hard tradeable-universe screen. A cross-sectional effect that
    only exists once thin names are included is a capacity illusion, so this
    is a first-class screen rather than a footnote.
    """

    if n <= 0:
        raise CrossSectionError("n must be positive")
    return frame.with_columns(
        pl.col(column).rank("ordinal", descending=True).over(period).alias("_rank")
    ).filter(pl.col("_rank") <= n).drop("_rank")


def long_short(
    frame: pl.DataFrame,
    *,
    signal: str,
    ret: str,
    sign: int = 1,
    cut: float = DEFAULT_CUT,
    period: str = "bar_ts_ms",
    min_names: int = 10,
) -> pl.DataFrame:
    """Per-period equal-weight decile long/short return, in basis points.

    ``min_names`` drops periods too sparse to form two meaningful tails; a
    2-name "decile" is noise that would otherwise inflate the series variance
    and the apparent significance.
    """

    if not 0.0 < cut < 0.5:
        raise CrossSectionError(f"cut must be in (0, 0.5), got {cut}")
    if sign not in (1, -1):
        raise CrossSectionError(f"sign must be +1 or -1, got {sign}")

    d = frame.drop_nulls([signal, ret])
    d = d.filter(pl.len().over(period) >= min_names)
    if d.is_empty():
        return pl.DataFrame(schema={period: frame.schema.get(period, pl.Int64), "ret_bp": pl.Float64})
    return (
        # Centred percentile: plain rank/len runs 1/n..1 rather than being
        # symmetric about 0.5, which hands the two tails different name counts
        # and quietly makes the book directional.
        d.with_columns(
            ((pl.col(signal).rank("ordinal").over(period) - 0.5) / pl.len().over(period)).alias("_p")
        )
        .filter((pl.col("_p") <= cut) | (pl.col("_p") >= 1.0 - cut))
        .group_by(period)
        # Each leg is averaged separately and then differenced, so the two legs
        # carry equal weight even when their counts differ by one name.
        .agg(
            pl.col(ret).filter(pl.col("_p") <= cut).mean().alias("_low"),
            pl.col(ret).filter(pl.col("_p") >= 1.0 - cut).mean().alias("_high"),
        )
        .with_columns(
            ((pl.col("_low") - pl.col("_high")) * float(sign) * 1e4).alias("ret_bp")
        )
        .select(period, "ret_bp")
        .sort(period)
    )


def summary(
    returns_bp: np.ndarray | pl.Series,
    *,
    periods_per_year: int,
    cost_bp: float = MEASURED_ROUND_TRIP_BP,
) -> Summary:
    """Summarise a per-period long/short series already expressed in bp.

    ``cost_bp`` is charged once per period, i.e. it assumes the book fully
    rebalances each period. That is the pessimistic reading; a book that
    turns over less should model its own turnover rather than lowering this.

    The default is :data:`MEASURED_ROUND_TRIP_BP`, not zero. A caller that omits
    the argument gets the honest cost basis rather than a gross number, because
    a gross number is a diagnostic and not a result (``docs/governance.md`` §2).
    Pass ``cost_bp=0.0`` explicitly when a gross read is genuinely what is
    wanted, and label it as such.
    """

    r = np.asarray(returns_bp, dtype=float)
    r = r[np.isfinite(r)] - float(cost_bp)
    n = int(r.size)
    if n < 5:
        nan = float("nan")
        return Summary(n, nan, nan, nan, nan, nan, nan, nan, nan)

    mean = float(r.mean())
    sd = float(r.std(ddof=1))
    stderr = sd / math.sqrt(n) if sd > 0 else float("nan")
    t_stat = mean / stderr if stderr and math.isfinite(stderr) else float("nan")
    sharpe = mean / sd * math.sqrt(periods_per_year) if sd > 0 else float("nan")

    k = max(1, n // 100)
    worst = np.sort(r)[:k]
    losses = r[r < 0].sum()
    concentration = float(worst.sum() / losses * 100.0) if losses < 0 else float("nan")

    equity = np.cumsum(r / 1e4)
    drawdown = float((np.maximum.accumulate(equity) - equity).max() * 100.0)

    return Summary(
        n=n,
        mean_bp=mean,
        t_stat=float(t_stat),
        sharpe=float(sharpe),
        ann_pct=mean / 1e4 * periods_per_year * 100.0,
        max_drawdown_pct=drawdown,
        worst_1pct_bp=float(worst.mean()),
        tail_concentration_pct=concentration,
        hit_rate_pct=float((r > 0).mean() * 100.0),
    )


def lag_screen(
    build,
    *,
    signal: str,
    lags: tuple[int, ...] = (0, 1, 4),
    **kwargs: object,
) -> dict[int, Summary]:
    """Re-score a signal at increasing publication delays.

    A cross-venue or price-derived signal can produce a large, highly
    significant, completely untradeable effect purely from a stale or
    bid-ask-bounced print, which reverts mechanically. Genuine slow convergence
    survives a delay; a stale-print artifact collapses. ``build(lag)`` must
    return a frame with the signal already shifted by ``lag`` periods.
    """

    out: dict[int, Summary] = {}
    for lag in lags:
        frame = build(lag)
        series = long_short(frame, signal=signal, **kwargs)  # type: ignore[arg-type]
        out[lag] = summary(series["ret_bp"], periods_per_year=kwargs.get("periods_per_year", 365))  # type: ignore[arg-type]
    return out
