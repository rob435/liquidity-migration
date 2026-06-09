"""Walk-forward fold-generation tests: coverage, expanding train, and the one
causal invariant (train ends strictly before test start, minus embargo)."""
from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.walk_forward import (
    MS_PER_DAY,
    WalkForwardConfig,
    WalkForwardFold,
    make_walk_forward_folds,
    select_test,
    select_train,
)


def _grid(n_days: int, start_ms: int = 1_000 * MS_PER_DAY) -> list[int]:
    return [start_ms + i * MS_PER_DAY for i in range(n_days)]


def test_empty_grid_yields_no_folds():
    assert make_walk_forward_folds([], WalkForwardConfig()) == []


def test_warmup_shorter_than_span_yields_no_folds():
    grid = _grid(100)
    folds = make_walk_forward_folds(grid, WalkForwardConfig(warmup_days=540, step_days=90))
    assert folds == []


def test_folds_cover_post_warmup_window_and_expand():
    grid = _grid(900)
    cfg = WalkForwardConfig(warmup_days=540, step_days=90, embargo_days=2)
    folds = make_walk_forward_folds(grid, cfg)
    assert folds, "expected at least one fold over a 900-day span"
    # Expanding: every fold trains from the global first timestamp.
    assert all(f.train_start_ms == grid[0] for f in folds)
    # Test windows are contiguous and step_days wide (except the clamped last).
    for a, b in zip(folds, folds[1:]):
        assert b.test_start_ms == a.test_end_ms
    # Indices are dense and ordered.
    assert [f.index for f in folds] == list(range(len(folds)))


def test_causal_gap_invariant_holds_for_every_fold():
    grid = _grid(1200)
    cfg = WalkForwardConfig(warmup_days=540, step_days=90, embargo_days=2)
    folds = make_walk_forward_folds(grid, cfg)
    embargo_ms = cfg.embargo_days * MS_PER_DAY
    for f in folds:
        assert f.train_end_ms == f.test_start_ms - embargo_ms
        assert f.train_end_ms <= f.test_start_ms  # the look-ahead guard


def test_fold_rejects_lookahead_construction():
    with pytest.raises(ValueError, match="look-ahead"):
        WalkForwardFold(
            index=0, train_start_ms=0, train_end_ms=10, test_start_ms=5, test_end_ms=20
        )


def test_select_train_test_partition_by_the_gap():
    grid = _grid(900)
    cfg = WalkForwardConfig(warmup_days=540, step_days=90, embargo_days=2)
    folds = make_walk_forward_folds(grid, cfg)
    panel = pl.DataFrame({"symbol": ["X"] * len(grid), "ts_ms": grid})
    f = folds[0]
    tr = select_train(panel, f)
    te = select_test(panel, f)
    # No training timestamp lands at or after train_end; no test row before test_start.
    assert tr["ts_ms"].max() < f.train_end_ms
    assert te["ts_ms"].min() >= f.test_start_ms
    # The embargo days are excluded from BOTH sides.
    embargoed = [t for t in grid if f.train_end_ms <= t < f.test_start_ms]
    assert set(embargoed).isdisjoint(set(tr["ts_ms"].to_list()))
    assert set(embargoed).isdisjoint(set(te["ts_ms"].to_list()))


def test_config_validates_inputs():
    with pytest.raises(ValueError):
        WalkForwardConfig(warmup_days=0)
    with pytest.raises(ValueError):
        WalkForwardConfig(step_days=-1)
    with pytest.raises(ValueError):
        WalkForwardConfig(embargo_days=-1)
