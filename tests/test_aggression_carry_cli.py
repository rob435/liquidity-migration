from __future__ import annotations

from pathlib import Path

from aggression_carry.cli import main


def test_cli_fixture_pipeline_end_to_end(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert main(["--data-root", str(data_root), "build-features"]) == 0
    assert main(["--data-root", str(data_root), "alpha-report"]) == 0
    assert main(["--data-root", str(data_root), "portfolio-backtest"]) == 0
    assert main(["--data-root", str(data_root), "research-sweep"]) == 0
    assert main(["--data-root", str(data_root), "volume-alpha"]) == 0

    assert (data_root / "features_1h").exists()
    assert (data_root / "reports" / "alpha_report.md").exists()
    assert (data_root / "reports" / "portfolio_backtest.md").exists()
    assert (data_root / "reports" / "research_sweep.md").exists()
    assert (data_root / "reports" / "volume_alpha_report.md").exists()
