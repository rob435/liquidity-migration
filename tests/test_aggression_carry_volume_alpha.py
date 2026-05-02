from __future__ import annotations

from pathlib import Path

from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.storage import read_dataset
from aggression_carry.volume_alpha import build_volume_features, run_volume_alpha


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
