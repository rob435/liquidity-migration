"""Guards against circular-import regressions in the active module split.

Importing a sibling hub first in a fresh process deadlocks on partially initialized
shared modules; ``liquidity_migration/__init__.py`` preloads the needed hubs to break
it. The imports MUST run in a subprocess: once a hub is loaded in the pytest process the
cycle is masked and an in-process import would pass even with the bug back.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every active sibling produced by the public-data + chart/PIT + CLI splits,
# at its subpackage path.
_SPLIT_SIBLINGS = [
    "strategy.event_demo_data",
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
        f"cold import of liquidity_migration.{sibling} failed — likely a "
        f"hub<->sibling circular import regression:\n{proc.stderr}"
    )
