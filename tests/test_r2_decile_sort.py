"""Tests for R2 per-feature standalone decile-sort analytics."""
from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from liquidity_migration.r2_decile_sort import (
    DecileSpreadSummary,
    decile_spread_pnl,
    pca_variance_shares,
    spearman_correlation_matrix,
    summarize_pnl_series,
)


def _build_panel(
    *,
    n_days: int = 20,
    n_symbols: int = 20,
    feature_seed: int = 7,
    fwd_ret_seed: int = 13,
    realized_vol_seed: int = 23,
    feature_name: str = "feature_a",
    sign_correlation: float = -1.0,
) -> pl.DataFrame:
    """Synthetic panel where ``feature_name`` carries a known IC signal.

    ``sign_correlation`` controls the relationship between feature value and
    forward return:
      -1.0 → perfectly negative IC (high feature ⇒ low return), the R2
            convention. Top decile by feature should yield positive short P&L.
      +1.0 → positive IC.
       0.0 → no signal.

    Forward returns are constructed as
        ``fwd_ret = sign_correlation × (feature_z) + noise``
    where ``feature_z`` is the cross-sectional z-score per day. So if
    ``sign_correlation = -1.0`` the worst-fwd-return symbols are the
    highest-feature symbols → shorting them earns positive P&L.
    """
    rng_feat = np.random.default_rng(feature_seed)
    rng_fwd = np.random.default_rng(fwd_ret_seed)
    rng_vol = np.random.default_rng(realized_vol_seed)
    rows = []
    for d in range(n_days):
        ts_ms = (1_700_000_000 + d * 86_400) * 1000  # arbitrary daily ms
        feat_vals = rng_feat.standard_normal(n_symbols)
        feat_z = (feat_vals - feat_vals.mean()) / feat_vals.std()
        noise = rng_fwd.standard_normal(n_symbols) * 0.005  # 50 bps noise
        # Make the signal-driven fwd_ret have realistic crypto-ish magnitudes
        # (1% scale per std × cross-section)
        fwd_h3 = sign_correlation * feat_z * 0.01 + noise
        # 1d horizon uses slightly different noise so correlations across
        # horizons are non-degenerate.
        fwd_h1 = sign_correlation * feat_z * 0.006 + rng_fwd.standard_normal(n_symbols) * 0.003
        fwd_h7 = sign_correlation * feat_z * 0.02 + rng_fwd.standard_normal(n_symbols) * 0.008
        vols = 0.02 + rng_vol.uniform(0.0, 0.08, n_symbols)  # 2-10% daily vol
        for s_idx in range(n_symbols):
            rows.append({
                "symbol": f"S{s_idx:02d}",
                "ts_ms": ts_ms,
                "date": f"2025-{(d // 30) + 1:02d}-{(d % 30) + 1:02d}",
                feature_name: float(feat_vals[s_idx]),
                "fwd_ret_1d": float(fwd_h1[s_idx]),
                "fwd_ret_3d": float(fwd_h3[s_idx]),
                "fwd_ret_7d": float(fwd_h7[s_idx]),
                "realized_vol_7d": float(vols[s_idx]),
            })
    return pl.from_dicts(rows)


# ────────────────────────── decile_spread_pnl ──────────────────────────

def test_decile_spread_pnl_returns_expected_columns():
    panel = _build_panel()
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    assert set(pnl.columns) == {"date", "n_signals", "daily_pnl", "equity", "drawdown"}
    # n_days signal-days produce some pnl rows; the top decile of 20 names = 2
    # names per day, so n_signals should be 2 on each row.
    assert pnl.height > 0
    assert pnl["n_signals"].min() >= 1
    assert pnl["n_signals"].max() <= 2


def test_decile_spread_pnl_negative_ic_yields_positive_pnl():
    """With sign_correlation=-1.0 (the R2 convention), shorting the top decile
    earns positive P&L on average."""
    panel = _build_panel(sign_correlation=-1.0, n_days=200, n_symbols=30)
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    # Mean daily P&L should be > 0 (negative IC means top decile drops)
    mean_pnl = float(pnl["daily_pnl"].mean())
    assert mean_pnl > 0, f"expected positive mean pnl, got {mean_pnl}"


def test_decile_spread_pnl_positive_ic_yields_negative_pnl():
    """Mirror sanity check: positive IC + shorting top decile = losing money."""
    panel = _build_panel(sign_correlation=+1.0, n_days=200, n_symbols=30)
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    mean_pnl = float(pnl["daily_pnl"].mean())
    assert mean_pnl < 0, f"expected negative mean pnl with positive-IC short, got {mean_pnl}"


def test_decile_spread_pnl_equity_curve_compounds():
    """equity[i] = equity[i-1] * (1 + daily_pnl[i]) — geometric compounding."""
    panel = _build_panel(n_days=50)
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    pnls = pnl["daily_pnl"].to_numpy()
    equity = pnl["equity"].to_numpy()
    expected = np.cumprod(1.0 + pnls)
    np.testing.assert_allclose(equity, expected, rtol=1e-10)


def test_decile_spread_pnl_drawdown_relative_to_running_peak():
    panel = _build_panel(n_days=100, sign_correlation=-0.5)
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    equity = pnl["equity"].to_numpy()
    peak = np.maximum.accumulate(equity)
    expected_dd = (equity - peak) / peak
    np.testing.assert_allclose(pnl["drawdown"].to_numpy(), expected_dd, rtol=1e-10)
    # Drawdown is ≤ 0
    assert pnl["drawdown"].max() <= 1e-12


def test_decile_spread_pnl_round_trip_cost_reduces_pnl():
    panel = _build_panel(sign_correlation=-1.0, n_days=100)
    pnl_no_cost = decile_spread_pnl(panel, feature="feature_a", horizon=3, cost_round_trip_bps=0.0)
    pnl_with_cost = decile_spread_pnl(panel, feature="feature_a", horizon=3, cost_round_trip_bps=50.0)
    # With cost, every daily P&L is exactly 50 bps lower
    diff = pnl_no_cost["daily_pnl"].to_numpy() - pnl_with_cost["daily_pnl"].to_numpy()
    np.testing.assert_allclose(diff, 50.0 / 10_000.0 * np.ones_like(diff), atol=1e-10)


def test_decile_spread_pnl_equal_weights_vs_risk_weights_differ():
    panel = _build_panel(sign_correlation=-1.0, n_days=100)
    pnl_eq = decile_spread_pnl(panel, feature="feature_a", horizon=3, use_risk_weights=False)
    pnl_rw = decile_spread_pnl(panel, feature="feature_a", horizon=3, use_risk_weights=True)
    # The two should differ in at least one day's P&L (risk-equal vs equal-weight
    # produces different weighted averages whenever vols are not all equal).
    diff = (pnl_eq["daily_pnl"].to_numpy() - pnl_rw["daily_pnl"].to_numpy())
    assert np.abs(diff).max() > 1e-6


def test_decile_spread_pnl_missing_feature_raises():
    panel = _build_panel()
    with pytest.raises(KeyError, match="feature column"):
        decile_spread_pnl(panel, feature="nonexistent_feature", horizon=3)


def test_decile_spread_pnl_missing_fwd_ret_raises():
    panel = _build_panel()
    with pytest.raises(KeyError, match="forward-return column"):
        decile_spread_pnl(panel, feature="feature_a", horizon=99)


def test_decile_spread_pnl_missing_realized_vol_raises_with_risk_weights():
    panel = _build_panel().drop("realized_vol_7d")
    with pytest.raises(KeyError, match="realized_vol"):
        decile_spread_pnl(panel, feature="feature_a", horizon=3, use_risk_weights=True)


def test_decile_spread_pnl_works_without_realized_vol_when_equal_weights():
    panel = _build_panel().drop("realized_vol_7d")
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3, use_risk_weights=False)
    assert pnl.height > 0


def test_decile_spread_pnl_invalid_top_decile_raises():
    panel = _build_panel()
    with pytest.raises(ValueError, match="top_decile"):
        decile_spread_pnl(panel, feature="feature_a", horizon=3, top_decile=0.0)
    with pytest.raises(ValueError, match="top_decile"):
        decile_spread_pnl(panel, feature="feature_a", horizon=3, top_decile=0.6)


# ────────────────────────── summarize_pnl_series ──────────────────────────

def test_summarize_pnl_series_empty_returns_zeros():
    empty = pl.DataFrame(
        schema={
            "date": pl.String, "n_signals": pl.Int64, "daily_pnl": pl.Float64,
            "equity": pl.Float64, "drawdown": pl.Float64,
        }
    )
    s = summarize_pnl_series(empty, feature="x", horizon=3, venue="bybit", window_days=365)
    assert s.total_return == 0.0
    assert s.mar == 0.0
    assert s.n_signal_days == 0


def test_summarize_pnl_series_zero_window_raises():
    panel = _build_panel()
    pnl = decile_spread_pnl(panel, feature="feature_a", horizon=3)
    with pytest.raises(ValueError, match="window_days"):
        summarize_pnl_series(pnl, feature="x", horizon=3, venue="bybit", window_days=0)


def test_summarize_pnl_series_recovers_basic_math():
    """Construct a known sequence of daily P&L: [+0.01, -0.005, +0.02]
    Expected:
      equity = [1.01, 1.00495, 1.02505]
      total_return = +2.50%
      max_drawdown = (1.00495 - 1.01) / 1.01 = -0.50%
      sharpe ≈ mean/std × sqrt(365) = (0.025/3 / std) × sqrt(365)
    """
    pnls = [0.01, -0.005, 0.02]
    equity = np.cumprod([1.0 + p for p in pnls])
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    pnl_df = pl.DataFrame({
        "date": ["d1", "d2", "d3"],
        "n_signals": [3, 3, 3],
        "daily_pnl": pnls,
        "equity": equity.tolist(),
        "drawdown": dd.tolist(),
    })
    s = summarize_pnl_series(pnl_df, feature="x", horizon=3, venue="bybit", window_days=365)
    assert s.total_return == pytest.approx(equity[-1] - 1, rel=1e-9)
    assert s.max_drawdown == pytest.approx(dd.min(), rel=1e-9)
    expected_mean = float(np.mean(pnls))
    expected_std = float(np.std(pnls, ddof=1))
    expected_sharpe = expected_mean / expected_std * math.sqrt(365)
    assert s.sharpe_like == pytest.approx(expected_sharpe, rel=1e-6)
    # MAR = annualized / |DD|. At window=365, annualized ≈ total_return geometrically.
    expected_ann = (1 + s.total_return) ** (365.25 / 365) - 1.0
    expected_mar = expected_ann / abs(s.max_drawdown)
    assert s.mar == pytest.approx(expected_mar, rel=1e-6)


def test_summarize_pnl_series_handles_total_loss_geometrically():
    """If cumulative return goes below -100%, annualization caps at -1.0."""
    pnls = [-0.5, -0.5, -0.6]  # 0.5 × 0.5 × 0.4 = 0.10 → -90% (>-100%, valid)
    equity = np.cumprod([1.0 + p for p in pnls])
    pnl_df = pl.DataFrame({
        "date": ["d1", "d2", "d3"],
        "n_signals": [1, 1, 1],
        "daily_pnl": pnls,
        "equity": equity.tolist(),
        "drawdown": [(equity[i] - max(equity[: i + 1])) / max(equity[: i + 1]) for i in range(3)],
    })
    s = summarize_pnl_series(pnl_df, feature="x", horizon=3, venue="bybit", window_days=365)
    assert s.total_return == pytest.approx(equity[-1] - 1, rel=1e-9)
    assert s.annualized_return > -1.0  # cumulative > -100% so annualization is real


# ────────────────────────── spearman_correlation_matrix ──────────────────

def test_correlation_matrix_identical_features_yield_perfect_correlation():
    """A feature with itself → ρ = 1.0."""
    pnl = pl.DataFrame({
        "date": ["d1", "d2", "d3", "d4", "d5"],
        "daily_pnl": [0.01, -0.02, 0.005, 0.0, -0.01],
    })
    per_feature = {"a": pnl, "b": pnl.clone()}
    matrix = spearman_correlation_matrix(per_feature, ["a", "b"])
    # diagonal ρ = 1, off-diagonal ρ = 1 since b ≡ a
    assert matrix["a"][0] == pytest.approx(1.0)
    assert matrix["b"][0] == pytest.approx(1.0)
    assert matrix["a"][1] == pytest.approx(1.0)
    assert matrix["b"][1] == pytest.approx(1.0)


def test_correlation_matrix_anti_correlated_features():
    """Negate one series → ρ = -1.0."""
    pnl_a = pl.DataFrame({
        "date": ["d1", "d2", "d3", "d4", "d5"],
        "daily_pnl": [0.01, -0.02, 0.005, 0.0, -0.01],
    })
    pnl_b = pl.DataFrame({
        "date": ["d1", "d2", "d3", "d4", "d5"],
        "daily_pnl": [-0.01, 0.02, -0.005, 0.0, 0.01],
    })
    matrix = spearman_correlation_matrix({"a": pnl_a, "b": pnl_b}, ["a", "b"])
    assert matrix["b"][0] == pytest.approx(-1.0)


def test_correlation_matrix_insufficient_overlap_returns_nan():
    """If fewer than 5 dates intersect, the off-diagonal cell is NaN."""
    pnl_a = pl.DataFrame({"date": ["d1", "d2", "d3"], "daily_pnl": [0.01, 0.02, 0.03]})
    pnl_b = pl.DataFrame({"date": ["d4", "d5", "d6"], "daily_pnl": [0.01, 0.02, 0.03]})
    matrix = spearman_correlation_matrix({"a": pnl_a, "b": pnl_b}, ["a", "b"])
    assert math.isnan(matrix["b"][0])
    assert math.isnan(matrix["a"][1])
    # Diagonal is still 1.0
    assert matrix["a"][0] == pytest.approx(1.0)


def test_correlation_matrix_missing_feature_raises():
    pnl = pl.DataFrame({"date": ["d1"], "daily_pnl": [0.0]})
    with pytest.raises(KeyError):
        spearman_correlation_matrix({"a": pnl}, ["a", "missing"])


# ────────────────────────── pca_variance_shares ──────────────────────────

def test_pca_variance_shares_sum_to_one():
    rng = np.random.default_rng(0)
    n = 60
    per_feature = {
        f"f{i}": pl.DataFrame({
            "date": [f"d{k}" for k in range(n)],
            "daily_pnl": rng.standard_normal(n).tolist(),
        })
        for i in range(3)
    }
    out = pca_variance_shares(per_feature, ["f0", "f1", "f2"])
    assert sum(out["explained_variance_ratio"]) == pytest.approx(1.0, abs=1e-9)
    assert out["cumulative_variance"][-1] == pytest.approx(1.0, abs=1e-9)


def test_pca_variance_shares_identical_features_first_pc_dominates():
    """Three identical P&L series → PC1 explains ~100%."""
    pnl = pl.DataFrame({"date": [f"d{k}" for k in range(20)], "daily_pnl": list(range(20))})
    per_feature = {"a": pnl, "b": pnl.clone(), "c": pnl.clone()}
    out = pca_variance_shares(per_feature, ["a", "b", "c"])
    assert out["explained_variance_ratio"][0] > 0.99


def test_pca_variance_shares_independent_features_distribute_variance():
    """Three independent random walks → variance distributed across PCs."""
    rng = np.random.default_rng(1)
    n = 100
    per_feature = {
        f"f{i}": pl.DataFrame({
            "date": [f"d{k}" for k in range(n)],
            "daily_pnl": rng.standard_normal(n).tolist(),
        })
        for i in range(3)
    }
    out = pca_variance_shares(per_feature, ["f0", "f1", "f2"])
    # No PC should hugely dominate; PC1 share well below 90%
    assert out["explained_variance_ratio"][0] < 0.9
    # All PCs should carry meaningful variance
    assert min(out["explained_variance_ratio"]) > 0.05


def test_pca_variance_shares_insufficient_data_raises():
    pnl = pl.DataFrame({"date": ["d1", "d2"], "daily_pnl": [0.0, 0.0]})
    with pytest.raises(ValueError, match="≥ 3 days"):
        pca_variance_shares({"a": pnl, "b": pnl.clone()}, ["a", "b"])


# ───────────────────────── DecileSpreadSummary dataclass ─────────────────

def test_decile_spread_summary_is_frozen():
    s = DecileSpreadSummary(
        feature="x", horizon=3, venue="bybit", window_days=1125,
        n_signal_days=100, total_signals=200, total_return=1.0,
        annualized_return=0.5, max_drawdown=-0.2, sharpe_like=1.5, mar=2.5,
    )
    with pytest.raises((AttributeError, TypeError)):
        s.feature = "y"  # type: ignore[misc]
