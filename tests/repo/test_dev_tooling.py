from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.devtools import repo_doctor


ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scripts" / "dev.sh"
DOCTOR = ROOT / "scripts" / "devtools" / "repo_doctor.py"
PRE_PUSH = ROOT / "scripts" / "git-hooks" / "pre-push"


def test_locked_requirement_parser_and_comparison(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text("# exact pins\nPyYAML==6.0.3\nruff==0.15.14\n", encoding="utf-8")

    locked = repo_doctor.read_locked_requirements(lock)

    assert locked == {
        "pyyaml": ("PyYAML", "6.0.3"),
        "ruff": ("ruff", "0.15.14"),
    }
    report = repo_doctor.compare_locked_versions(
        locked,
        {"pyyaml": "6.0.2", "ruff": None},
    )
    assert report["status"] == "drift"
    assert report["missing"] == [{"name": "ruff", "expected": "0.15.14"}]
    assert report["mismatched"] == [{"name": "PyYAML", "expected": "6.0.3", "installed": "6.0.2"}]


def test_locked_requirement_parser_rejects_ambiguous_lines(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text("ruff>=0.15\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid requirement pin"):
        repo_doctor.read_locked_requirements(lock)


def test_skill_report_compares_complete_trees(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "skills" / "example"
    claude = tmp_path / ".claude" / "skills" / "example"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    (codex / "SKILL.md").write_text("same\n", encoding="utf-8")
    (claude / "SKILL.md").write_text("same\n", encoding="utf-8")

    assert repo_doctor.skill_mirror_report(tmp_path)["status"] == "matched"

    (claude / "SKILL.md").write_text("different\n", encoding="utf-8")
    report = repo_doctor.skill_mirror_report(tmp_path)
    assert report["status"] == "error"
    assert report["mismatched"] == ["example/SKILL.md"]


def test_repository_doctor_emits_machine_readable_state() -> None:
    completed = subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(ROOT), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report["schema_version"] == 1
    assert report["repository"] == str(ROOT)
    assert report["python"]["supported"] is True
    assert report["git"]["status"] in {"clean", "dirty"}
    assert report["dependency_lock"]["status"] in {"matched", "drift"}
    assert report["skill_mirrors"]["status"] == "matched"
    assert report["graphify"]["status"] in {"ready", "missing_tool", "missing_report"}


def test_dev_help_works_outside_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(DEV), "help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "doctor" in completed.stdout
    assert "check" in completed.stdout
    assert "scripts/ops.sh --help" in completed.stdout


def test_dev_router_uses_selected_python_and_preserves_arguments(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    capture = tmp_path / "arguments.txt"
    fake_python.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    environment = {**os.environ, "PYTHON": str(fake_python), "CAPTURE": str(capture)}

    subprocess.run(
        ["bash", str(DEV), "lint", "--fix"],
        cwd=tmp_path,
        check=True,
        env=environment,
    )

    assert capture.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "ruff",
        "check",
        "liquidity_migration",
        "scripts",
        "tests",
        "--fix",
    ]


def test_pre_push_reuses_developer_gate_and_preserves_safe_basetemp() -> None:
    hook = PRE_PUSH.read_text(encoding="utf-8")

    assert '"$REPO_ROOT/scripts/dev.sh" check --basetemp "$PYTEST_BASETEMP"' in hook
    assert 'case "$PYTEST_BASETEMP" in' in hook
    assert '"$REPO_ROOT"|"$REPO_ROOT"/*)' in hook
