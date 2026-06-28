"""Guards against circular-import regressions in the post-refactor module split.

The `event_demo` helpers and surviving standalone `volume_events_charts` /
`volume_events_pit` helpers are still imported by live long/continuous paths.
Importing a sibling FIRST in a fresh process used to deadlock on partially
initialized shared modules; `liquidity_migration/__init__.py` preloads the
needed hubs/helpers to break it. These tests pin that contract.

The imports MUST run in a subprocess: once a hub is loaded in the pytest process
the cycle is masked, so an in-process `import` would pass even if the bug
returned.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Every sibling module produced by the event_demo + surviving chart/PIT + cli splits.
_SPLIT_SIBLINGS = [
    "event_demo_data",
    "event_demo_reports",
    "event_demo_exits",
    "order_link_id",
    "volume_events_charts",
    "volume_events_pit",
    "cli_parsers",
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
