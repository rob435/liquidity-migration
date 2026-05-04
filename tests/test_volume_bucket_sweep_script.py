from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_volume_bucket_sweep as bucket_sweep


def test_bucket_sweep_default_scores_include_real_volume_change_scores() -> None:
    scores = bucket_sweep._csv_str(bucket_sweep.DEFAULT_SCORES)

    assert scores == (
        "dollar_volume_rank",
        "volume_change_1d",
        "volume_change_3d",
        "volume_persistence",
        "volume_composite",
    )


def test_bucket_sweep_summary_shows_score_name() -> None:
    report = bucket_sweep._format_summary(
        [
            {
                "bucket": "tail",
                "rank_min": 81,
                "rank_max": 160,
                "score": "volume_change_1d",
                "total_return": 0.42,
                "sharpe_like": 1.2,
                "max_drawdown": -0.1,
                "hold_days": 7,
                "quantile": 0.2,
                "rank_exit_enabled": False,
                "cost_multiplier": 2.0,
                "side_mode": "short_high_long_low",
                "long_return": 0.3,
                "short_return": 0.2,
            }
        ]
    )

    assert "| Bucket | Ranks | Score |" in report
    assert "| tail | 81-160 | volume_change_1d | 42.00% |" in report
