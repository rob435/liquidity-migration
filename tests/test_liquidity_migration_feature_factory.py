from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.feature_factory import run_feature_factory_report


def test_feature_factory_report_scores_edges_against_shuffle_control(tmp_path: Path) -> None:
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    rows = []
    split_starts = ("2023-06-01", "2024-06-01", "2025-06-01")
    for split_index, entry_date in enumerate(split_starts):
        for index in range(18):
            value = index / 17.0
            rows.append(
                {
                    "symbol": f"COIN{split_index}_{index}USDT",
                    "entry_date": entry_date,
                    "net_return": -0.03 + 0.08 * value,
                    "event_uniqueness_score": value,
                    "liquidity_rank_improvement_3d": 30.0 + 90.0 * value,
                    "intraday_range_expansion_7d": 0.5 + value,
                    "crowding_entry_hour_signal_count": 2 if index % 3 == 0 else 1,
                }
            )
    pl.DataFrame(rows).write_csv(report_dir / "volume_event_best_trades.csv")

    splits = (
        ("train_2023_2024", "2023-05-03", "2024-05-03"),
        ("validation_2024_2025", "2024-05-03", "2025-05-03"),
        ("oos_2025_2026", "2025-05-03", "2026-05-03"),
    )
    payload = run_feature_factory_report(
        report_dir, min_rows=9, shuffle_samples=16, random_seed=5, splits=splits,
    )
    edge_by_feature = {row["feature"]: row for row in payload["edges"]}

    assert Path(payload["output_files"]["markdown"]).exists()
    assert Path(payload["output_files"]["coverage"]).exists()
    assert edge_by_feature["event_uniqueness_score"]["high_low_edge"] > 0.0
    assert edge_by_feature["event_uniqueness_score"]["edge_over_shuffle_median_abs"] > 1.0
    assert edge_by_feature["event_uniqueness_score"]["split_consistency"] >= 2
