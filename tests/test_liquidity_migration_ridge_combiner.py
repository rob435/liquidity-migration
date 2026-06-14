"""Ridge combiner tests: closed-form correctness, signal recovery, and the causal
guarantees the pre-registration leans on (future rows, the embargo gap, and a test
row's own target never affect its score)."""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration.ridge_combiner import (
    FoldCoefficients,
    RidgeCombinerConfig,
    coefficient_sign_consistency,
    rank_ic,
    ridge_fit,
    ridge_predict,
    walk_forward_scores,
)
from liquidity_migration.walk_forward import MS_PER_DAY, WalkForwardConfig

FEATURES = ("f0", "f1", "f2")


def _panel(n_days: int = 120, per_day: int = 12, seed: int = 0) -> pl.DataFrame:
    """Synthetic daily panel with a known linear target: y = 1.5*f0 - 0.8*f1 + noise.
    f2 is pure noise (should earn a ~0 coefficient)."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    start = 1_000 * MS_PER_DAY
    for d in range(n_days):
        ts = start + d * MS_PER_DAY
        for s in range(per_day):
            f0 = float(rng.normal())
            f1 = float(rng.normal())
            f2 = float(rng.normal())
            y = 1.5 * f0 - 0.8 * f1 + 0.1 * float(rng.normal())
            rows.append(
                {"symbol": f"S{s}", "ts_ms": ts, "f0": f0, "f1": f1, "f2": f2, "fwd_ret_1d": y}
            )
    return pl.DataFrame(rows)


def _cfg(**kw) -> RidgeCombinerConfig:
    base = dict(
        features=FEATURES,
        lambda_grid=(0.1, 1.0, 10.0),
        walk_forward=WalkForwardConfig(warmup_days=40, step_days=20, embargo_days=2),
        min_train_rows=50,
    )
    base.update(kw)
    return RidgeCombinerConfig(**base)


# --- numeric primitives -----------------------------------------------------
def test_ridge_fit_matches_normal_equations_reference():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(50, 3))
    y = rng.normal(size=50)
    lam = 4.0
    beta = ridge_fit(x, y, lam)
    # Independent reference: same penalized normal equations, intercept unpenalized.
    xi = np.column_stack([np.ones(50), x])
    p = np.eye(4) * lam
    p[0, 0] = 0.0
    ref = np.linalg.solve(xi.T @ xi + p, xi.T @ y)
    assert np.allclose(beta, ref)


def test_huge_lambda_collapses_coef_to_zero_intercept_to_mean():
    rng = np.random.default_rng(2)
    x = rng.normal(size=(200, 3))
    y = 3.0 + rng.normal(size=200)
    beta = ridge_fit(x, y, lam=1e12)
    assert np.allclose(beta[1:], 0.0, atol=1e-6)
    assert beta[0] == pytest.approx(y.mean(), abs=1e-3)


def test_ridge_predict_is_affine():
    beta = np.array([0.5, 2.0, -1.0])
    x = np.array([[1.0, 1.0], [0.0, 0.0]])
    assert np.allclose(ridge_predict(x, beta), [0.5 + 2.0 - 1.0, 0.5])


def test_rank_ic_monotonic_and_constant():
    assert rank_ic(np.array([1.0, 2, 3, 4]), np.array([10.0, 20, 30, 40])) == pytest.approx(1.0)
    assert rank_ic(np.array([1.0, 2, 3, 4]), np.array([40.0, 30, 20, 10])) == pytest.approx(-1.0)
    assert rank_ic(np.array([1.0, 1, 1, 1]), np.array([1.0, 2, 3, 4])) == 0.0


# --- config guards ----------------------------------------------------------
def test_target_in_features_is_rejected():
    with pytest.raises(ValueError, match="leak"):
        RidgeCombinerConfig(features=("f0", "fwd_ret_1d"))


def test_nonpositive_lambda_rejected():
    with pytest.raises(ValueError):
        RidgeCombinerConfig(features=FEATURES, lambda_grid=(0.0, 1.0))


def test_embargo_must_cover_forward_horizon():
    """The embargo must be >= forward_horizon + 1 so the newest training target
    is fully resolved before the first test decision. fwd_ret_3d with the default
    embargo=2 leaks the test fold into training and must be a hard config error."""
    with pytest.raises(ValueError, match="embargo"):
        RidgeCombinerConfig(
            features=FEATURES,
            target_col="fwd_ret_3d",
            walk_forward=WalkForwardConfig(embargo_days=2),
        )
    # embargo >= horizon + 1 is accepted (no raise).
    RidgeCombinerConfig(
        features=FEATURES,
        target_col="fwd_ret_3d",
        walk_forward=WalkForwardConfig(embargo_days=4),
    )
    # The house-default fwd_ret_1d with embargo=2 stays valid.
    RidgeCombinerConfig(features=FEATURES, target_col="fwd_ret_1d",
                        walk_forward=WalkForwardConfig(embargo_days=2))


# --- end-to-end behaviour ---------------------------------------------------
def test_walk_forward_recovers_known_signal():
    scores, coefs = walk_forward_scores(_panel(), _cfg())
    assert not scores.is_empty()
    assert coefs, "expected fitted folds"
    # Out-of-fold predictions correlate with realized target (the signal is real).
    joined = scores.join(_panel(), on=["symbol", "ts_ms"], how="inner")
    ic = rank_ic(joined["ridge_score"].to_numpy(), joined["fwd_ret_1d"].to_numpy())
    assert ic > 0.5, f"out-of-fold rank-IC unexpectedly low: {ic}"
    # The true drivers carry the right signs; noise feature stays near zero.
    last = coefs[-1]
    assert last.coef["f0"] > 0
    assert last.coef["f1"] < 0
    assert abs(last.coef["f2"]) < abs(last.coef["f0"])


def test_each_row_scored_at_most_once():
    scores, _ = walk_forward_scores(_panel(), _cfg())
    keyed = scores.select(["symbol", "ts_ms"])
    assert keyed.n_unique() == keyed.height


def test_future_rows_do_not_change_earlier_fold_scores():
    """Poison every column for dates at/after fold 0's test_end; fold 0's test-window
    scores must be byte-for-byte unchanged (it can only have used earlier rows)."""
    cfg = _cfg()
    panel = _panel()
    from liquidity_migration.walk_forward import make_walk_forward_folds

    fold0 = make_walk_forward_folds(panel["ts_ms"].to_list(), cfg.walk_forward)[0]
    base_scores, _ = walk_forward_scores(panel, cfg)
    base0 = base_scores.filter(
        (pl.col("ts_ms") >= fold0.test_start_ms) & (pl.col("ts_ms") < fold0.test_end_ms)
    ).sort(["ts_ms", "symbol"])

    poisoned = panel.with_columns(
        [
            pl.when(pl.col("ts_ms") >= fold0.test_end_ms)
            .then(pl.lit(9_999.0))
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("f0", "f1", "f2", "fwd_ret_1d")
        ]
    )
    pois_scores, _ = walk_forward_scores(poisoned, cfg)
    pois0 = pois_scores.filter(
        (pl.col("ts_ms") >= fold0.test_start_ms) & (pl.col("ts_ms") < fold0.test_end_ms)
    ).sort(["ts_ms", "symbol"])

    assert np.allclose(base0["ridge_score"].to_numpy(), pois0["ridge_score"].to_numpy())


def test_test_row_target_is_never_used_in_its_own_score():
    """A test row's own forward return must not affect its prediction (only its
    features feed the fitted model)."""
    cfg = _cfg()
    panel = _panel()
    base_scores, _ = walk_forward_scores(panel, cfg)
    from liquidity_migration.walk_forward import make_walk_forward_folds

    # Corrupt the target ONLY on fold 0's test rows: those rows are scored by fold 0
    # (features only) and are never training targets for their own fold, so their
    # predictions must be unchanged.
    fold0 = make_walk_forward_folds(panel["ts_ms"].to_list(), cfg.walk_forward)[0]
    poisoned = panel.with_columns(
        pl.when(
            (pl.col("ts_ms") >= fold0.test_start_ms) & (pl.col("ts_ms") < fold0.test_end_ms)
        )
        .then(pl.lit(-555.0))
        .otherwise(pl.col("fwd_ret_1d"))
        .alias("fwd_ret_1d")
    )
    pois_scores, _ = walk_forward_scores(poisoned, cfg)
    b0 = base_scores.filter(
        (pl.col("ts_ms") >= fold0.test_start_ms) & (pl.col("ts_ms") < fold0.test_end_ms)
    ).sort(["ts_ms", "symbol"])
    p0 = pois_scores.filter(
        (pl.col("ts_ms") >= fold0.test_start_ms) & (pl.col("ts_ms") < fold0.test_end_ms)
    ).sort(["ts_ms", "symbol"])
    assert np.allclose(b0["ridge_score"].to_numpy(), p0["ridge_score"].to_numpy())


def test_embargo_rows_are_excluded_from_that_folds_training():
    """A fold's embargo-gap rows (too recent for their target to have completed by
    test-start) must not enter that fold's training set, so corrupting them leaves
    the fold's fitted coefficients unchanged. (They are still SCORED — by the prior
    fold whose contiguous test window contains them — so global scores do change;
    the embargo guards training, not scoring.)"""
    cfg = _cfg()
    panel = _panel()
    from liquidity_migration.walk_forward import make_walk_forward_folds

    folds = make_walk_forward_folds(panel["ts_ms"].to_list(), cfg.walk_forward)
    target = folds[1]  # a fold with a real expanding train history before it
    _, base_coefs = walk_forward_scores(panel, cfg)
    base = next(c for c in base_coefs if c.fold_index == target.index)

    poisoned = panel.with_columns(
        [
            pl.when(
                (pl.col("ts_ms") >= target.train_end_ms)
                & (pl.col("ts_ms") < target.test_start_ms)
            )
            .then(pl.lit(7_777.0))
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("f0", "f1", "f2", "fwd_ret_1d")
        ]
    )
    _, pois_coefs = walk_forward_scores(poisoned, cfg)
    pois = next(c for c in pois_coefs if c.fold_index == target.index)
    assert base.coef == pytest.approx(pois.coef)
    assert base.intercept == pytest.approx(pois.intercept)


def test_empty_or_missing_columns_return_empty():
    scores, coefs = walk_forward_scores(pl.DataFrame(), _cfg())
    assert scores.is_empty() and coefs == []
    bad = pl.DataFrame({"symbol": ["A"], "ts_ms": [1], "f0": [0.1]})  # missing f1,f2,target
    scores2, coefs2 = walk_forward_scores(bad, _cfg())
    assert scores2.is_empty() and coefs2 == []


def test_coefficient_sign_consistency():
    coefs = [
        FoldCoefficients(0, 1.0, 0.0, {"a": 0.5, "b": -0.2}, 100, 10),
        FoldCoefficients(1, 1.0, 0.0, {"a": 0.7, "b": 0.1}, 100, 10),
        FoldCoefficients(2, 1.0, 0.0, {"a": 0.3, "b": -0.4}, 100, 10),
    ]
    cons = coefficient_sign_consistency(coefs)
    assert cons["a"] == pytest.approx(1.0)  # always positive
    assert cons["b"] == pytest.approx(2 / 3)  # 2 of 3 negative
