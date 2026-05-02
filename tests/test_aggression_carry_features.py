from __future__ import annotations

from pathlib import Path

import polars as pl

from aggression_carry.features import compute_features_from_store
from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.research import attach_forward_returns, run_alpha_report
from aggression_carry.storage import read_dataset
from aggression_carry.sweep import build_sweep_candidates, run_research_sweep
from aggression_carry.volume_alpha import build_volume_features, run_volume_alpha


def test_fixture_feature_pipeline_builds_expected_columns(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    features = compute_features_from_store(tmp_path)

    assert features.height > 0
    for col in [
        "aggression_z",
        "rel_volume_z",
        "momentum_z",
        "carry_z",
        "quality_z",
        "oi_impulse_z",
        "composite_score",
    ]:
        assert col in features.columns
    assert features["composite_score"].drop_nulls().len() > 0


def test_no_same_bar_leakage_uses_next_bar_entry(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    features = compute_features_from_store(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")
    returns = attach_forward_returns(features, klines, horizons_h=(4,))

    row = returns.filter(pl.col("forward_return_4h").is_not_nan()).sort(["symbol", "ts_ms"]).row(0, named=True)
    symbol = row["symbol"]
    ts_ms = row["ts_ms"]
    symbol_klines = klines.filter(pl.col("symbol") == symbol).sort("ts_ms")
    index = symbol_klines["ts_ms"].to_list().index(ts_ms)
    entry = symbol_klines["close"][index + 1]
    exit_ = symbol_klines["close"][index + 5]

    assert row["forward_return_4h"] == pl.Series([exit_ / entry]).log()[0]


def test_forward_returns_require_exact_hourly_exit_timestamp() -> None:
    features = pl.DataFrame(
        [{"ts_ms": 0, "symbol": "BTCUSDT", "composite_score": 1.0}]
    )
    klines = pl.DataFrame(
        [
            {"ts_ms": 0, "symbol": "BTCUSDT", "close": 100.0},
            {"ts_ms": 60 * 60 * 1000, "symbol": "BTCUSDT", "close": 101.0},
            {"ts_ms": 2 * 60 * 60 * 1000, "symbol": "BTCUSDT", "close": 102.0},
            {"ts_ms": 3 * 60 * 60 * 1000, "symbol": "BTCUSDT", "close": 103.0},
            {"ts_ms": 6 * 60 * 60 * 1000, "symbol": "BTCUSDT", "close": 106.0},
        ]
    )

    returns = attach_forward_returns(features, klines, horizons_h=(4,))

    assert returns["forward_return_4h"].is_nan()[0]


def test_alpha_report_contains_standalone_and_ablation_metrics(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    compute_features_from_store(tmp_path)

    payload = run_alpha_report(tmp_path)

    signal_names = {item["signal"] for item in payload["signals"]}
    ablation_names = {item["signal"] for item in payload["leave_one_out"]}
    assert "aggression" in signal_names
    assert "composite" in signal_names
    assert "without_aggression_confirmed" in ablation_names
    assert "mean_cost_adjusted_spread" in payload["signals"][0]
    assert payload["timestamp_ic"]
    assert payload["quantile_ledger"]
    assert payload["monthly_spreads"]
    assert payload["config_hash"]
    assert payload["date_range"]["start"]
    assert payload["acceptance_gates"]
    assert "aggression_cost_adjusted_spread_positive_4h" in {
        gate["gate"] for gate in payload["acceptance_gates"]
    }
    assert (tmp_path / "research_timestamp_ic").exists()
    assert (tmp_path / "research_quantile_ledger").exists()
    assert (tmp_path / "research_monthly_spreads").exists()
    assert (tmp_path / "reports" / "alpha_report.md").exists()


def test_research_sweep_compares_inverted_and_carry_candidates(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    features = compute_features_from_store(tmp_path)

    candidates = build_sweep_candidates(features)
    assert "score_carry_inverse_bad_stack" in candidates.columns
    assert "score_inverse_momentum" in candidates.columns

    payload = run_research_sweep(tmp_path)

    candidate_names = set(payload["candidates"])
    portfolio_candidates = {item["candidate"] for item in payload["portfolio"]}
    assert "carry_only" in candidate_names
    assert "inverse_momentum" in candidate_names
    assert "carry_inverse_bad_stack" in candidate_names
    assert candidate_names.issubset(portfolio_candidates)
    assert payload["best_by_horizon"]
    assert payload["best_portfolio"]
    assert (tmp_path / "reports" / "research_sweep.md").exists()
    assert (tmp_path / "research_sweep_metrics").exists()
    assert (tmp_path / "research_sweep_portfolio").exists()


def test_volume_alpha_isolated_daily_research_path(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)
    klines = read_dataset(tmp_path, "klines_1h")

    features = build_volume_features(klines)
    assert "volume_change_1d_z" in features.columns
    assert "volume_composite" in features.columns

    payload = run_volume_alpha(tmp_path, horizons_d=(1, 3), quantiles=(0.50,))

    signal_names = {item["signal"] for item in payload["metrics"]}
    portfolio_scores = {item["score"] for item in payload["portfolios"]}
    assert "volume_change_1d" in signal_names
    assert "volume_composite" in signal_names
    assert "volume_composite" in portfolio_scores
    assert payload["best_base_portfolio"]
    assert (tmp_path / "reports" / "volume_alpha_report.md").exists()
    assert (tmp_path / "volume_alpha_features").exists()
    assert (tmp_path / "volume_alpha_metrics").exists()
    assert (tmp_path / "volume_alpha_portfolios").exists()
