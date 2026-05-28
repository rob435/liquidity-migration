"""Guards against circular-import regressions in the post-refactor module split.

The `event_demo` and `volume_events` monoliths were each split into a hub plus
sibling modules. The hub eagerly imports its siblings (re-export + the hub's own
cycle code calls them) and each sibling imports shared helpers back from the hub
— a hub<->sibling cycle. Importing a sibling FIRST in a fresh process used to
deadlock on the partially-initialized hub; `liquidity_migration/__init__.py`
preloads the hubs to break it. These tests pin that contract.

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

# Every sibling module produced by the event_demo + volume_events splits.
_SPLIT_SIBLINGS = [
    "event_demo_data",
    "event_demo_entries",
    "event_demo_planning",
    "event_demo_reports",
    "event_demo_exits",
    "volume_events_filters",
    "volume_events_features",
    "volume_events_charts",
    "volume_events_validation",
]


@pytest.mark.parametrize("sibling", _SPLIT_SIBLINGS)
def test_split_sibling_imports_cold_in_fresh_process(sibling: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import liquidity_migration.{sibling}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"cold import of liquidity_migration.{sibling} failed — likely a "
        f"hub<->sibling circular import regression:\n{proc.stderr}"
    )
