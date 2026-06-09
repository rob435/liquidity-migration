"""Expanding-window walk-forward folds for causal model fitting.

The rest of this repo backtests in a single full-window pass — fine for
hard-threshold rules whose parameters are chosen by a human, but a *fitted*
model trained on the whole history is in-sample by construction. This module is
the missing piece: it splits the daily time grid into expanding train/test folds
so a model can be refit on the past and scored on the strictly-later future, with
an embargo gap so the training target (a forward return) never overlaps the test
window.

Pure and engine-free: it operates on a sorted list of unique day timestamps and
emits fold boundaries. Selection of the actual panel rows is the caller's job
(``select_train`` / ``select_test`` are the canonical masks). Causality lives in
one invariant, pinned by tests:

    train rows satisfy  ts_ms < (test_start_ms - embargo_ms)
    test  rows satisfy  test_start_ms <= ts_ms < test_end_ms

so the newest training row's forward-return target completes before the first
test decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class WalkForwardConfig:
    """Expanding-window schedule, in calendar days."""

    warmup_days: int = 540
    """Days of history required before the first test fold opens (~18 months)."""
    step_days: int = 90
    """Test-fold width and refit cadence (~quarterly)."""
    embargo_days: int = 2
    """Gap between train end and test start. Must be >= (forward horizon + 1) so the
    last training row's target has completed before the test window opens."""

    def __post_init__(self) -> None:
        if self.warmup_days <= 0:
            raise ValueError("warmup_days must be positive")
        if self.step_days <= 0:
            raise ValueError("step_days must be positive")
        if self.embargo_days < 0:
            raise ValueError("embargo_days must be non-negative")


@dataclass(frozen=True)
class WalkForwardFold:
    """One expanding-window fold. All bounds are millisecond epoch.

    Train rows: ``train_start_ms <= ts_ms < train_end_ms``.
    Test  rows: ``test_start_ms  <= ts_ms < test_end_ms``.
    ``train_end_ms == test_start_ms - embargo_ms`` (the causal gap).
    """

    index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int

    def __post_init__(self) -> None:
        # The one invariant that makes the whole thing causal.
        if self.train_end_ms > self.test_start_ms:
            raise ValueError("train_end_ms must not exceed test_start_ms (look-ahead)")


def make_walk_forward_folds(
    unique_ts_ms: list[int], config: WalkForwardConfig
) -> list[WalkForwardFold]:
    """Build expanding folds covering the window after the warm-up.

    ``unique_ts_ms`` need not be sorted or unique — it is normalized here. The
    train start is the global first timestamp for every fold (expanding, never
    rolling); the embargo carves a gap before each test window.
    """
    if not unique_ts_ms:
        return []
    grid = sorted(set(int(t) for t in unique_ts_ms))
    first = grid[0]
    last = grid[-1]
    embargo_ms = config.embargo_days * MS_PER_DAY
    warmup_ms = config.warmup_days * MS_PER_DAY
    step_ms = config.step_days * MS_PER_DAY

    folds: list[WalkForwardFold] = []
    test_start = first + warmup_ms
    index = 0
    while test_start <= last:
        test_end = test_start + step_ms
        train_end = test_start - embargo_ms
        # Only emit a fold that has at least one possible training timestamp.
        if train_end > first:
            folds.append(
                WalkForwardFold(
                    index=index,
                    train_start_ms=first,
                    train_end_ms=train_end,
                    test_start_ms=test_start,
                    test_end_ms=min(test_end, last + 1),
                )
            )
            index += 1
        test_start = test_end
    return folds


def select_train(panel: pl.DataFrame, fold: WalkForwardFold) -> pl.DataFrame:
    """Rows usable for training this fold (causal + embargoed)."""
    return panel.filter(
        (pl.col("ts_ms") >= fold.train_start_ms) & (pl.col("ts_ms") < fold.train_end_ms)
    )


def select_test(panel: pl.DataFrame, fold: WalkForwardFold) -> pl.DataFrame:
    """Rows scored in this fold's test window."""
    return panel.filter(
        (pl.col("ts_ms") >= fold.test_start_ms) & (pl.col("ts_ms") < fold.test_end_ms)
    )
