"""Regression checks for the canonical Bybit full-PIT builder."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/data/build_full_pit_bybit.sh"


def test_builder_validates_independent_manifest_without_filtering_it() -> None:
    subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")
    manifest = text.rindex("archive-manifest --start")
    klines = text.rindex("archive-download-klines-1h-api")
    validation = text.rindex("validate-manifest --data-root")
    ancillary = text.rindex("\n  download-data \\")

    assert manifest < klines < validation < ancillary
    assert "filter-manifest --data-root" not in text
    assert "--min-existing-bars 20" in text
    assert "validate_bybit_manifest_provenance" in text


def test_help_and_unknown_arguments_cannot_start_a_build() -> None:
    help_result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    refused = subprocess.run(
        ["bash", str(SCRIPT), "unexpected"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0
    assert "Configuration is environment-only" in help_result.stdout
    assert refused.returncode == 2
    assert "accepts no positional arguments" in refused.stderr
