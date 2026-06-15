"""Walk-forward ridge combiner: frozen causal features -> one forward-return score.

Role (pre-registration ``docs/preregistration/ridge-combiner-2026-06-09.md``): a
*ranker/sizer within* the event-selected short pool, not a selector. The event
trigger still decides which names enter; this maps a frozen feature vector to a
single predicted forward return so downstream sizing can rank within the pool.

Why numpy, not sklearn: sklearn is not a dependency (numpy + polars only), and the
closed-form ridge is ten lines that mirror the ``np.linalg.lstsq`` cross-sectional
fit already in ``risk_model.fit_factor_returns``. Ridge solves

    beta = (X'X + P)^-1 X'y ,   P = lambda * I  with the intercept row/col zeroed

so the intercept is never shrunk and lambda > 0 guarantees the system is solvable.

Causality is delegated to ``walk_forward``: every score is produced by a model fit
only on rows strictly before its test window (with an embargo so the training
target has completed). Standardization stats and lambda are chosen on the training
fold alone. These are pinned by tests (poison-future invariance, fold isolation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

import numpy as np
import polars as pl

from .walk_forward import (
    WalkForwardConfig,
    make_walk_forward_folds,
    select_test,
    select_train,
)

# The pre-registered frozen feature set (all causal at decision_ts EOD; all present
# in signal_harness.FEATURE_REGISTRY). residual_momentum is an optional 9th the
# scout left-joins when its parquet exists — not part of this default.
FROZEN_FEATURES: tuple[str, ...] = (
    "turnover_delta_7d",
    "realized_vol_7d",
    "funding_rate_z",
    "premium_index_z",
    "liquidity_rank",
    "dist_from_30d_high",
    "oi_to_adv",
    "xs_rank_ret_30d",
)


@dataclass(frozen=True)
class RidgeCombinerConfig:
    features: tuple[str, ...] = FROZEN_FEATURES
    target_col: str = "fwd_ret_1d"
    lambda_grid: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0, 1000.0)
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    inner_cv_folds: int = 3
    min_train_rows: int = 200
    standardize: bool = True
    # audit2: explicit forward horizon (days) of `target_col`. When set it drives the
    # embargo>=horizon+1 leak guard unconditionally; when None the horizon is parsed
    # from a `fwd_ret_Nd` name. A forward-returnish name that cannot be parsed AND has
    # no explicit horizon is a hard error (see __post_init__) — the guard can no longer
    # be silently bypassed by naming the target anything other than fwd_ret_Nd.
    forward_horizon: int | None = None

    def __post_init__(self) -> None:
        if not self.features:
            raise ValueError("features must be non-empty")
        if self.target_col in self.features:
            raise ValueError(f"target_col {self.target_col!r} must not be a feature (leak)")
        if any(lam <= 0 for lam in self.lambda_grid):
            raise ValueError("lambda_grid values must be positive (PD guarantee)")
        # Causality: the embargo must cover the target's forward horizon so the
        # newest training target (a fwd_ret_Nd realising N+1 days after the
        # decision) is fully resolved before the first test decision. Enforce the
        # WalkForwardConfig docstring invariant in code instead of by convention,
        # so wiring a multi-day target (e.g. fwd_ret_3d) without bumping embargo
        # is a hard error rather than silent in-fold target leakage (fake alpha).
        # audit2: resolve the forward horizon from the explicit field first, then fall
        # back to parsing a `fwd_ret_Nd` name. Previously the entire leak guard was
        # gated on the regex matching, so a multi-day target named anything else
        # (e.g. 'y_5d', 'forward_return_5d', a residualised name) silently skipped the
        # embargo check — textbook in-fold target leakage with no error.
        horizon: int | None = self.forward_horizon
        if horizon is None:
            horizon_match = re.search(r"fwd_ret_(\d+)d", self.target_col)
            if horizon_match is not None:
                horizon = int(horizon_match.group(1))
        if horizon is not None:
            if horizon < 0:
                raise ValueError(f"forward_horizon ({horizon}) must be >= 0")
            if self.walk_forward.embargo_days < horizon + 1:
                raise ValueError(
                    f"embargo_days ({self.walk_forward.embargo_days}) must be >= "
                    f"forward horizon + 1 ({horizon + 1}) for target_col "
                    f"{self.target_col!r}, else the test fold leaks into training"
                )
        elif re.search(r"(?:fwd|forward)", self.target_col, re.IGNORECASE) or re.search(
            r"\d+\s*d\b", self.target_col
        ):
            # A forward-returnish name we cannot parse to a horizon, with no explicit
            # forward_horizon -> refuse rather than silently skip the leak guard.
            raise ValueError(
                f"target_col {self.target_col!r} looks like a forward return but its "
                "horizon cannot be parsed; set RidgeCombinerConfig.forward_horizon "
                "explicitly so the embargo>=horizon+1 leak guard can be enforced"
            )


@dataclass(frozen=True)
class FoldCoefficients:
    """The fitted model for one walk-forward fold (for stability auditing)."""

    fold_index: int
    chosen_lambda: float
    intercept: float
    coef: dict[str, float]
    n_train: int
    n_test: int


# ---------------------------------------------------------------------------
# Core numeric primitives (pure numpy; no polars, no IO).
# ---------------------------------------------------------------------------
def ridge_fit(x: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """Closed-form ridge with an UNPENALIZED intercept.

    ``x`` is (n, k) standardized features (no intercept column). Returns a length
    ``k + 1`` vector ``[intercept, *coef]``.
    """
    n = x.shape[0]
    xi = np.column_stack([np.ones(n), x])
    k1 = xi.shape[1]
    penalty = np.eye(k1) * lam
    penalty[0, 0] = 0.0  # never shrink the intercept
    gram = xi.T @ xi + penalty
    try:
        beta = np.linalg.solve(gram, xi.T @ y)
    except np.linalg.LinAlgError:  # pragma: no cover - lam>0 makes this unreachable
        beta = np.linalg.lstsq(gram, xi.T @ y, rcond=None)[0]
    return beta


def ridge_predict(x: np.ndarray, beta: np.ndarray) -> np.ndarray:
    return beta[0] + x @ beta[1:]


def _standardize_fit(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-column mean/std from training rows. Zero-variance columns get std=1 so
    they contribute exactly 0 after centering rather than dividing by zero."""
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std = np.where(std > 1e-12, std, 1.0)
    return mean, std


def _standardize_apply(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return (x - mean) / std


def _rank(v: np.ndarray) -> np.ndarray:
    """Average-rank of a 1-D array (ties share their mean rank)."""
    # Resolve ties to their average rank for a stable Spearman. (np.unique sorts,
    # so cum/avg are positional ranks of the sorted uniques; inv maps them back.)
    _, inv, counts = np.unique(v, return_inverse=True, return_counts=True)
    cum = np.cumsum(counts) - counts
    avg = cum + (counts - 1) / 2.0
    return avg[inv]


def rank_ic(scores: np.ndarray, targets: np.ndarray) -> float:
    """Spearman rank correlation (Pearson on average-ranks). NaN-safe-ish: returns
    0.0 when either side is constant."""
    if len(scores) < 3:
        return 0.0
    rs = _rank(scores)
    rt = _rank(targets)
    if rs.std() < 1e-12 or rt.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(rs, rt)[0, 1])


# ---------------------------------------------------------------------------
# Lambda selection (inner expanding CV on the TRAINING fold only).
# ---------------------------------------------------------------------------
def select_lambda(
    train: pl.DataFrame, config: RidgeCombinerConfig
) -> float:
    """Pick lambda maximizing mean out-of-fold rank-IC over inner temporal folds.

    The inner split is over the training fold's own day grid (expanding), so no
    test-fold information ever touches lambda. Falls back to the grid median when
    the train set is too small to split.
    """
    feats = list(config.features)
    tgt = config.target_col
    sub = train.drop_nulls(subset=[*feats, tgt])
    grid_sorted = sorted(config.lambda_grid)
    if sub.height < max(config.min_train_rows, 3 * (len(feats) + 2)):
        return grid_sorted[len(grid_sorted) // 2]

    inner = make_walk_forward_folds(
        sub["ts_ms"].to_list(),
        WalkForwardConfig(
            warmup_days=max(1, _span_days(sub) // (config.inner_cv_folds + 1)),
            step_days=max(1, _span_days(sub) // (config.inner_cv_folds + 1)),
            embargo_days=config.walk_forward.embargo_days,
        ),
    )
    if not inner:
        return grid_sorted[len(grid_sorted) // 2]

    best_lambda = grid_sorted[len(grid_sorted) // 2]
    best_ic = -np.inf
    for lam in config.lambda_grid:
        ics: list[float] = []
        for fold in inner:
            tr = select_train(sub, fold)
            te = select_test(sub, fold)
            if tr.height < len(feats) + 2 or te.height < 3:
                continue
            xt = tr.select(feats).to_numpy()
            yt = tr[tgt].to_numpy()
            mean, std = _standardize_fit(xt) if config.standardize else (np.zeros(xt.shape[1]), np.ones(xt.shape[1]))
            beta = ridge_fit(_standardize_apply(xt, mean, std), yt, lam)
            xe = _standardize_apply(te.select(feats).to_numpy(), mean, std)
            preds = ridge_predict(xe, beta)
            ics.append(rank_ic(preds, te[tgt].to_numpy()))
        if ics:
            mean_ic = float(np.mean(ics))
            if mean_ic > best_ic:
                best_ic = mean_ic
                best_lambda = lam
    return best_lambda


def _span_days(panel: pl.DataFrame) -> int:
    if panel.is_empty():
        return 0
    lo_raw, hi_raw = panel["ts_ms"].min(), panel["ts_ms"].max()
    if lo_raw is None or hi_raw is None:  # all-null ts_ms: no usable span
        return 0
    lo, hi = int(cast(int, lo_raw)), int(cast(int, hi_raw))
    return max(1, (hi - lo) // 86_400_000)


# ---------------------------------------------------------------------------
# Walk-forward scoring (the public entry point).
# ---------------------------------------------------------------------------
def walk_forward_scores(
    panel: pl.DataFrame, config: RidgeCombinerConfig | None = None
) -> tuple[pl.DataFrame, list[FoldCoefficients]]:
    """Produce causal out-of-fold ridge scores for every test row.

    Returns ``(scores, fold_coefficients)`` where ``scores`` is
    ``(symbol, ts_ms, ridge_score)`` — the predicted forward return — and
    ``fold_coefficients`` is the per-fold fitted model for stability auditing.
    A row appears in ``scores`` exactly once: in the test fold that owns its date.
    """
    cfg = config or RidgeCombinerConfig()
    feats = list(cfg.features)
    tgt = cfg.target_col
    schema = {"symbol": pl.String, "ts_ms": pl.Int64, "ridge_score": pl.Float64}
    missing = [c for c in (*feats, tgt, "symbol", "ts_ms") if c not in panel.columns]
    if panel.is_empty() or missing:
        return pl.DataFrame(schema=schema), []

    folds = make_walk_forward_folds(panel["ts_ms"].to_list(), cfg.walk_forward)
    score_frames: list[pl.DataFrame] = []
    fold_coefs: list[FoldCoefficients] = []

    for fold in folds:
        train = select_train(panel, fold).drop_nulls(subset=[*feats, tgt])
        test = select_test(panel, fold).drop_nulls(subset=feats)
        if train.height < cfg.min_train_rows or test.is_empty():
            continue

        lam = select_lambda(train, cfg)
        xt = train.select(feats).to_numpy()
        yt = train[tgt].to_numpy()
        if cfg.standardize:
            mean, std = _standardize_fit(xt)
        else:
            mean, std = np.zeros(xt.shape[1]), np.ones(xt.shape[1])
        beta = ridge_fit(_standardize_apply(xt, mean, std), yt, lam)

        xe = _standardize_apply(test.select(feats).to_numpy(), mean, std)
        preds = ridge_predict(xe, beta)
        score_frames.append(
            test.select(["symbol", "ts_ms"]).with_columns(
                pl.Series("ridge_score", preds, dtype=pl.Float64)
            )
        )
        fold_coefs.append(
            FoldCoefficients(
                fold_index=fold.index,
                chosen_lambda=lam,
                intercept=float(beta[0]),
                coef={f: float(beta[i + 1]) for i, f in enumerate(feats)},
                n_train=train.height,
                n_test=test.height,
            )
        )

    if not score_frames:
        return pl.DataFrame(schema=schema), fold_coefs
    return pl.concat(score_frames).sort(["ts_ms", "symbol"]), fold_coefs


def coefficient_sign_consistency(
    fold_coefs: list[FoldCoefficients], features: tuple[str, ...] | None = None
) -> dict[str, float]:
    """Fraction of folds in which each feature's coefficient holds its modal sign.

    The pre-registration decision rule requires the dominant features to be
    sign-consistent across >=80% of folds; this computes that fraction per feature.
    """
    if not fold_coefs:
        return {}
    feats = list(features) if features is not None else list(fold_coefs[0].coef.keys())
    out: dict[str, float] = {}
    for f in feats:
        signs = [np.sign(fc.coef.get(f, 0.0)) for fc in fold_coefs]
        nonzero = [s for s in signs if s != 0]
        if not nonzero:
            out[f] = 0.0
            continue
        pos = sum(1 for s in nonzero if s > 0)
        neg = len(nonzero) - pos
        out[f] = max(pos, neg) / len(fold_coefs)
    return out
