from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.portfolio_hedge import run_portfolio_hedge_report


def _write_baskets(path: Path, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_csv(path / "volume_event_best_baskets.csv")


def test_portfolio_hedge_report_scores_overlay(tmp_path: Path) -> None:
    short_dir = tmp_path / "short"
    long_dir = tmp_path / "long"
    out_dir = tmp_path / "hedge"
    _write_baskets(
        short_dir,
        [
            {"exit_date": "2023-05-04", "basket_return": 0.10},
            {"exit_date": "2023-05-05", "basket_return": -0.10},
            {"exit_date": "2023-05-06", "basket_return": 0.05},
        ],
    )
    _write_baskets(
        long_dir,
        [
            {"exit_date": "2023-05-05", "basket_return": 0.04},
            {"exit_date": "2023-05-06", "basket_return": -0.01},
        ],
    )

    payload = run_portfolio_hedge_report(
        short_report_dir=short_dir,
        long_report_dirs=[long_dir],
        hedge_weights=[0.5],
        report_dir=out_dir,
    )

    assert Path(payload["summary_path"]).exists()
    assert Path(payload["report_path"]).exists()
    row = payload["rows"][0]
    assert row["long_name"] == "long"
    assert row["hedge_weight"] == 0.5
    assert row["long_return_on_short_bad_10pct"] > 0.0
    assert row["combo_max_drawdown"] > -0.10
