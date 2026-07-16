"""Regression checks for the canonical Bybit full-PIT builder."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/build_full_pit_bybit.sh"


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
