"""Reusable cross-sectional evaluation primitives for Lane-1 anomaly reads.

The panel is the durable artifact and probes are disposable, but the *scoring*
must not be rewritten per probe: that is where a missing funding leg, a
different cut, or an overlapping-sample t-stat makes two reads incomparable.

Conventions, fixed here so every read means the same thing:

* Signals are ranked **within each period**, so a book is market-neutral by
  construction and a cross-sectional read never smuggles in market direction.
* ``sign=+1`` means "long the LOW end of the ranked variable". A negative
  result is reported as negative; callers are expected to state the profitable
  direction rather than silently flipping a sign to flatter a result.
* Returns passed in should already include every economic leg the trade has —
  for a funding-sorted signal, price alone has the wrong sign (see
  ``docs/research/research_findings.md``).
* ``summary`` reports the tail statistics this repository cares about, not just
  a Sharpe: loss concentration in the worst 1% and max drawdown, because the
  book being replaced failed on tail shape rather than on mean.

Nothing here grades anything. Lane-1 reads on seen data cannot grade the
configurations they selected; that requires a committed config scored on
post-commit days per ``AGENTS.md``.
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
#: Provenance: 85 fills over 4,406.62 USDT notional from an archived account
#: journal, 7.78 bp/side notional-weighted (median 11.00, range 5.50-11.00) =>
#: a 15.56 bp round trip. The distribution sits on Bybit's taker tiers, so fills
#: price as taker, not maker. Replaces the older 4.00 bp maker assumption; see
#: ``docs/research/research_findings.md`` §16.1 for the per-surface audit.
MEASURED_ROUND_TRIP_BP = 15.56

#: Round trip a perfect passive book would pay: 2.70 bp/side implied at a 100%
#: passive fill rate (``docs/research/research_findings.md``). A floor, not an achieved
#: cost.
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

    The default is :data:`MEASURED_ROUND_TRIP_BP`, not zero, so an omitted
    argument gets the real cost basis rather than a gross number. Pass
    ``cost_bp=0.0`` explicitly for a gross read, and label it as such.
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


