"""Regression tests for scripts/reconcile.py (audit bucket b15).

Findings covered:

  reconciliation-3 reconcile wrapper always exited 0 (no machine-checkable gate)
  reconciliation-5 crashed leg rendered as benign '(no output)'
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


reconcile = _load("reconcile_b15", "scripts/reconcile.py")


def test_summarize_leg_passes_only_on_clean_leg() -> None:
    # reconciliation-5: a clean leg returns its summary line and ok=True.
    line, ok = reconcile._summarize_leg(
        "noise\nlong paper-demo reconciliation: paired=3 ok=True\nmore",
        "long paper-demo reconciliation", 0)
    assert ok is True
    assert line == "long paper-demo reconciliation: paired=3 ok=True"


def test_summarize_leg_flags_nonzero_exit_as_failed() -> None:
    # reconciliation-3/-5: a readiness gate that exits nonzero must NOT be summarized
    # as a clean/benign line. It must be rendered as an explicit FAILED marker and
    # report ok=False so the wrapper can exit nonzero.
    line, ok = reconcile._summarize_leg("Traceback (most recent call last): ...",
                                        "continuous forward readiness", 1)
    assert ok is False
    assert "FAILED" in line and "rc=1" in line


def test_summarize_leg_flags_missing_summary_as_failed_not_no_output() -> None:
    # reconciliation-5: a leg that printed nothing matching the needle (e.g. crashed
    # before its summary) must be FAILED, not the benign '(no output)' that made a
    # crash indistinguishable from a clean run-with-no-pairs.
    line, ok = reconcile._summarize_leg("partial output, no summary", "SUMMARY:", 0)
    assert ok is False
    assert line != "(no output)"
    assert "FAILED" in line


def test_reconcile_main_returns_nonzero_when_a_leg_fails(monkeypatch) -> None:
    # reconciliation-3: the wrapper must exit nonzero when a reconcile leg fails,
    # rather than unconditionally returning 0. Drive main() with the long sleeve
    # and a stubbed leg that reports a failure.
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("⚠️ FAILED (rc=1): gate failed", False))
    assert reconcile.main() == 1


def test_reconcile_main_returns_zero_when_leg_clean(monkeypatch) -> None:
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("long paper-demo reconciliation: ok", True))
    assert reconcile.main() == 0
