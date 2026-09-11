"""`scripts/dev.sh check` owns the pytest basetemp the pre-push hook used to build."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scripts" / "dev.sh"
PRE_PUSH = ROOT / "scripts" / "git-hooks" / "pre-push"


def _check(basetemp: str, *, cwd: Path, python: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `dev.sh check` with a chosen basetemp.

    `python` points PYTHON at a stub so the run stops at the first gate: the
    basetemp is resolved before any gate, which is all these tests assert.
    """
    environment = dict(os.environ)
    environment["PYTEST_BASETEMP"] = basetemp
    if python is not None:
        environment["PYTHON"] = str(python)
    return subprocess.run(
        ["bash", str(DEV), "check"],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _failing_python(tmp_path: Path) -> Path:
    stub = tmp_path / "python-stub"
    stub.write_text("#!/bin/sh\nexit 41\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_check_refuses_a_basetemp_inside_the_repository_before_any_gate(tmp_path: Path) -> None:
    inside = ROOT / "pytest-basetemp-refusal-fixture"
    completed = _check(str(inside), cwd=tmp_path)

    assert completed.returncode != 0
    assert "refusing pytest basetemp inside repository" in completed.stderr
    # Refused before the first gate: no doctor, no ruff, no directory left behind.
    assert "repository doctor" not in completed.stdout
    assert "[dev] ruff" not in completed.stdout
    assert not inside.exists()


def test_check_prints_and_passes_the_basetemp_it_supplies(tmp_path: Path) -> None:
    chosen = tmp_path / "basetemp"
    completed = _check(str(chosen), cwd=tmp_path, python=_failing_python(tmp_path))

    banner = f"[dev] pytest basetemp: {chosen.resolve()}"
    assert banner in completed.stdout, completed.stdout[:2000]
    assert chosen.is_dir()
    # The banner precedes the first gate, and the stub stops the run there.
    assert completed.stdout.index(banner) < completed.stdout.index("[dev] repository doctor")
    assert completed.returncode == 41

    body = DEV.read_text(encoding="utf-8")
    assert "pytest_args+=(--basetemp \"$pytest_basetemp\")" in body
    assert '-m pytest -q "${pytest_args[@]}"' in body


def test_the_pre_push_hook_delegates_the_whole_gate_to_dev_sh() -> None:
    hook = PRE_PUSH.read_text(encoding="utf-8")

    assert 'exec "$REPO_ROOT/scripts/dev.sh" check' in hook
    # The hook no longer resolves Python or builds a basetemp of its own.
    assert "PYTHON_BIN" not in hook
    assert "basetemp" not in hook
    assert "git rev-parse --local-env-vars" in hook


def test_check_reports_what_it_ran_and_what_it_skipped() -> None:
    usage = subprocess.run(
        ["bash", str(DEV), "help"], check=True, capture_output=True, text=True
    ).stdout
    assert "--basetemp" in usage
    assert "PYTEST_BASETEMP" in usage

    body = DEV.read_text(encoding="utf-8")
    assert '[dev] check complete; ran: $ran_list; skipped: $skipped_list' in body
    # ${array[*]} would join on IFS's first character and drop the space.
    assert 'ran_list="$(join_comma "${ran[@]}")"' in body
    assert 'skipped_list="none"' in body
    for gate in ("doctor", "ruff", "mypy", "pytest"):
        assert f"ran+=({gate})" in body
    assert 'skipped+=("shellcheck (not installed)")' in body
