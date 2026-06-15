"""Regression for audit2b/liveness_cwd: the cooldown state-file fallback must anchor
at the repo dir (NOT the CWD) when BOTH sleeve roots are explicitly skipped.

OLD code: ``_state_root = continuous_root or long_root or Path("data")`` — when both
``--continuous-root ''`` and ``--long-root ''`` are passed and no explicit ``--state-file``
is given, the fallback ``Path("data")`` resolves against the process CWD. A manual/cron
invocation from another directory then reads/writes a DIFFERENT cooldown state file each
run, so dedup is broken and every persisting condition re-pages. The fix anchors the
fallback at ``_REPO_ROOT / "data"`` like the _default_root root defaults already do.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_demo_liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_demo_liveness", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_demo_liveness"] = module
    spec.loader.exec_module(module)
    return module


M = _load()


def _run_both_roots_skipped(monkeypatch) -> None:
    """Run main() with every root skipped and no explicit --state-file, so the
    state-file path comes purely from the _state_root fallback under audit."""
    # No units / no roots: nothing pages, main() still computes + persists state.
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", "",
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
        ],
    )
    assert M.main() == 0


def test_state_file_fallback_anchored_at_repo_not_cwd(tmp_path, monkeypatch) -> None:
    """REGRESSION: with both sleeve roots skipped, the cooldown state file must land at
    ``_REPO_ROOT/data/.cache/liveness_watchdog.json`` regardless of CWD — NOT under a
    CWD-relative ``data/.cache``. Old code wrote it relative to the run directory.

    To avoid touching the real repo tree, the test points _REPO_ROOT at an isolated
    sandbox and runs main() from a *different* CWD; the state file must follow the
    anchored repo dir, never the CWD.
    """
    sandbox_repo = tmp_path / "repo"
    sandbox_repo.mkdir()
    run_cwd = tmp_path / "elsewhere"
    run_cwd.mkdir()

    monkeypatch.setattr(M, "_REPO_ROOT", sandbox_repo)
    monkeypatch.chdir(run_cwd)
    _run_both_roots_skipped(monkeypatch)

    anchored = sandbox_repo / "data" / ".cache" / "liveness_watchdog.json"
    cwd_relative = run_cwd / "data" / ".cache" / "liveness_watchdog.json"
    assert anchored.exists(), "state file must be anchored under the repo dir"
    assert not cwd_relative.exists(), "state file must NOT be written CWD-relative"


def test_explicit_state_file_unchanged(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: an explicit --state-file is honored verbatim and the
    fallback is never consulted (the fix only touches the both-roots-skipped fallback)."""
    explicit = tmp_path / "custom" / "state.json"
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", "",
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
            "--state-file", str(explicit),
        ],
    )
    assert M.main() == 0
    assert explicit.exists()


def test_continuous_root_still_drives_default_state_dir(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: when --continuous-root IS provided, the state file still
    defaults to ``<continuous-root>/.cache/liveness_watchdog.json`` — the fallback's
    repo-anchoring change must not perturb the populated-root case."""
    croot = tmp_path / "bybit-continuous-demo-event"
    croot.mkdir()
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", str(croot),
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
        ],
    )
    assert M.main() == 0
    assert (croot / ".cache" / "liveness_watchdog.json").exists()
