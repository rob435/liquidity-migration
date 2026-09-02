"""The checks a cell must survive after it beats its placebo.

A cell is one parameter value of one rule, scored as a delta against the
registered book plus the share of matched random draws that scored at least as
well. Five checks, each a pass or a fail with the number behind it:

- neighbours: the adjacent parameter values carry a delta of the same sign.
  A rule that loses at 9 bp and wins at 10 is a spike, not a plateau.
- lag: the same rule read one day late still beats its placebo.
- persistence: the rule required on two consecutive stamps still beats its placebo.
- mirror: the rule with its condition turned around does not also beat its
  placebo. If the mirror wins too, the construction made the result.
- concentration: the top three trades carry at most half of the gain.

Pure functions of numbers; the overlay harness produces the arms.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

PLACEBO_ALPHA = 0.05
MAX_TOP_SHARE = 0.5
TOP_TRADES = 3


@dataclass(frozen=True)
class Arm:
    """One scored variant: its delta against the book and the share of placebo draws scoring at least as well."""

    delta: float
    placebo_share: float

    def beats_placebo(self, alpha: float = PLACEBO_ALPHA) -> bool:
        return self.placebo_share <= alpha


@dataclass(frozen=True)
class Check:
    name: str
    value: float
    passes: bool
    note: str


@dataclass(frozen=True)
class PlateauReport:
    neighbours: Check
    lag: Check
    persistence: Check
    mirror: Check
    concentration: Check

    @property
    def checks(self) -> tuple[Check, ...]:
        return (self.neighbours, self.lag, self.persistence, self.mirror, self.concentration)

    @property
    def passes(self) -> bool:
        return all(c.passes for c in self.checks)

    def rows(self) -> list[dict[str, object]]:
        return [dict(check=c.name, value=c.value, passes=c.passes, note=c.note) for c in self.checks]


def _same_sign(a: float, b: float) -> bool:
    return a != 0 and b != 0 and (a > 0) == (b > 0)


def neighbour_check(cell_threshold: float, cell_delta: float, neighbours: Mapping[float, float]) -> Check:
    """The nearest threshold below and above the cell both share its sign.

    ``neighbours`` maps threshold to delta; the cell's own entry is ignored. A
    side with no threshold is not checked, but at least one neighbour is required.
    """
    below = [t for t in neighbours if t < cell_threshold]
    above = [t for t in neighbours if t > cell_threshold]
    picked = ([max(below)] if below else []) + ([min(above)] if above else [])
    if not picked:
        return Check("neighbours", math.nan, False, "no neighbouring threshold was run")
    deltas = [neighbours[t] for t in picked]
    weakest = min(deltas) if cell_delta > 0 else max(deltas)
    breaking = [t for t in picked if not _same_sign(cell_delta, neighbours[t])]
    if breaking:
        note = "sign flips at " + ", ".join(f"{t:g} ({neighbours[t]:+.4f})" for t in breaking)
        return Check("neighbours", weakest, False, note)
    return Check("neighbours", weakest, True, "both sides hold: " + ", ".join(f"{t:g} ({neighbours[t]:+.4f})" for t in picked))


def _still_wins(name: str, cell: Arm, variant: Arm, alpha: float) -> Check:
    ok = _same_sign(cell.delta, variant.delta) and variant.beats_placebo(alpha)
    note = f"delta {variant.delta:+.4f}, placebo share {variant.placebo_share:.3f} (cell {cell.delta:+.4f})"
    return Check(name, variant.delta, ok, note)


def lag_check(cell: Arm, lagged: Arm, *, alpha: float = PLACEBO_ALPHA) -> Check:
    return _still_wins("lag", cell, lagged, alpha)


def persistence_check(cell: Arm, persistent: Arm, *, alpha: float = PLACEBO_ALPHA) -> Check:
    return _still_wins("persistence", cell, persistent, alpha)


def mirror_check(cell: Arm, mirror: Arm, *, alpha: float = PLACEBO_ALPHA) -> Check:
    """Fails when the turned-around rule has the cell's sign and beats its placebo too."""
    also_wins = _same_sign(cell.delta, mirror.delta) and mirror.beats_placebo(alpha)
    note = f"mirror delta {mirror.delta:+.4f}, placebo share {mirror.placebo_share:.3f}"
    return Check("mirror", mirror.delta, not also_wins, note)


def top_share(per_trade_deltas: Sequence[float], top: int = TOP_TRADES) -> float:
    """Share of the summed positive total carried by the ``top`` largest trades; NaN when there is no gain."""
    total = float(sum(per_trade_deltas))
    if total <= 0:
        return math.nan
    biggest = sorted((float(d) for d in per_trade_deltas), reverse=True)[:top]
    return float(sum(biggest)) / total


def concentration_check(
    per_trade_deltas: Sequence[float], *, top: int = TOP_TRADES, max_share: float = MAX_TOP_SHARE
) -> Check:
    share = top_share(per_trade_deltas, top)
    if math.isnan(share):
        return Check("concentration", share, False, "no gain to spread")
    note = f"top {top} of {len(per_trade_deltas)} trades carry {share:.0%} of the gain"
    return Check("concentration", share, share <= max_share, note)


def plateau_checks(
    cell: Arm,
    *,
    cell_threshold: float,
    neighbours: Mapping[float, float],
    lagged: Arm,
    persistent: Arm,
    mirror: Arm,
    per_trade_deltas: Sequence[float],
    alpha: float = PLACEBO_ALPHA,
    max_top_share: float = MAX_TOP_SHARE,
) -> PlateauReport:
    return PlateauReport(
        neighbours=neighbour_check(cell_threshold, cell.delta, neighbours),
        lag=lag_check(cell, lagged, alpha=alpha),
        persistence=persistence_check(cell, persistent, alpha=alpha),
        mirror=mirror_check(cell, mirror, alpha=alpha),
        concentration=concentration_check(per_trade_deltas, max_share=max_top_share),
    )
