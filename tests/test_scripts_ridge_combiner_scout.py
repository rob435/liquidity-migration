"""Pure-helper tests for scripts/ridge_combiner_scout.py (root parsing + the
out-of-fold IC reducers). The full panel build / engine path is operator-run on
the 23GB full-PIT roots and is not exercised here."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest


def _load(name: str):
    repo = Path(__file__).resolve().parents[1]
    scripts_dir = str(repo / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(name, repo / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCOUT = _load("ridge_combiner_scout")


def test_parse_roots_ok():
    roots = SCOUT._parse_roots("bybit=/a/b, binance=/c/d")
    assert set(roots) == {"bybit", "binance"}
    assert roots["bybit"] == Path("/a/b")
    assert roots["binance"] == Path("/c/d")


def test_parse_roots_rejects_malformed():
    with pytest.raises(ValueError, match="venue=path"):
        SCOUT._parse_roots("bybit")
    with pytest.raises(ValueError):
        SCOUT._parse_roots("")


def test_pooled_and_per_fold_ic_on_known_signal():
    from liquidity_migration.ridge_combiner import RidgeCombinerConfig
    from liquidity_migration.walk_forward import MS_PER_DAY, WalkForwardConfig

    cfg = RidgeCombinerConfig(
        features=("f0",),
        walk_forward=WalkForwardConfig(warmup_days=10, step_days=10, embargo_days=2),
        min_train_rows=5,
    )
    # Panel where target == score proxy: ridge_score perfectly ranks the target.
    start = 1_000 * MS_PER_DAY
    rows = []
    for d in range(40):
        ts = start + d * MS_PER_DAY
        for s in range(6):
            rows.append({"symbol": f"S{s}", "ts_ms": ts, "f0": float(s), "fwd_ret_1d": float(s)})
    panel = pl.DataFrame(rows)
    # Synthetic scores identical to the target -> rank-IC must be 1.0.
    scores = panel.select(["symbol", "ts_ms"]).with_columns(
        pl.col("symbol").str.strip_chars("S").cast(pl.Float64).alias("ridge_score")
    )
    assert SCOUT.pooled_ic(scores, panel, cfg) == pytest.approx(1.0)
    folds = SCOUT.per_fold_ic(scores, panel, cfg)
    assert folds, "expected per-fold IC rows"
    assert all(f["rank_ic"] == pytest.approx(1.0) for f in folds)


def test_pooled_ic_empty_scores_is_zero():
    from liquidity_migration.ridge_combiner import RidgeCombinerConfig

    empty = pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "ridge_score": pl.Float64})
    assert SCOUT.pooled_ic(empty, pl.DataFrame(), RidgeCombinerConfig()) == 0.0


def test_config_hash_is_stable_and_sensitive():
    from liquidity_migration.ridge_combiner import RidgeCombinerConfig
    from liquidity_migration.walk_forward import WalkForwardConfig

    a = RidgeCombinerConfig(features=("f0", "f1"))
    b = RidgeCombinerConfig(features=("f0", "f1"))
    c = RidgeCombinerConfig(features=("f0", "f1"), walk_forward=WalkForwardConfig(warmup_days=300))
    assert SCOUT._config_hash(a) == SCOUT._config_hash(b)
    assert SCOUT._config_hash(a) != SCOUT._config_hash(c)
