from __future__ import annotations

from pathlib import Path

from aggression_carry.cli import main


def test_cli_fixture_pipeline_end_to_end(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert main(["--data-root", str(data_root), "volume-alpha"]) == 0
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-backtest",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--rebalance-days",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "volume-grid",
                "--start",
                "2025-01-02",
                "--end",
                "2025-01-05",
                "--hold-days",
                "1",
                "--quantiles",
                "0.5",
                "--fixed-stops",
                "0,0.001",
                "--vol-stops",
                "",
                "--rank-exits",
                "false",
                "--workers",
                "1",
            ]
        )
        == 0
    )

    assert (data_root / "reports" / "volume_alpha_report.md").exists()
    assert (data_root / "reports" / "volume_backtest_report.md").exists()
    assert (data_root / "reports" / "volume_grid_report.md").exists()
    assert main(["--data-root", str(data_root), "forward-report"]) == 0
    assert (data_root / "reports" / "forward_paper_report.md").exists()
