"""Cold-import smoke tests for public modules with broad dependency surfaces."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SPLIT_SIBLINGS = [
    "research.backtest.volume_events_charts",
    "data.volume_events_pit",
    "cli.parsers",
]


@pytest.mark.parametrize("sibling", _SPLIT_SIBLINGS)
def test_split_sibling_imports_cold_in_fresh_process(sibling: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import liquidity_migration.{sibling}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"cold import of liquidity_migration.{sibling} failed:\n{proc.stderr}"
    )
