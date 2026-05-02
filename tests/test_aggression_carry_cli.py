from __future__ import annotations

from pathlib import Path

from aggression_carry.cli import main


def test_cli_fixture_pipeline_end_to_end(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    assert main(["--data-root", str(data_root), "download-data", "--fixture"]) == 0
    assert main(["--data-root", str(data_root), "volume-alpha"]) == 0

    assert (data_root / "reports" / "volume_alpha_report.md").exists()
