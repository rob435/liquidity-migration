from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.devtools import repo_doctor


ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scripts" / "dev.sh"
DOCTOR = ROOT / "scripts" / "devtools" / "repo_doctor.py"
PRE_PUSH = ROOT / "scripts" / "git-hooks" / "pre-push"


def test_direct_third_party_imports_are_declared() -> None:
    imported: set[str] = set()
    for base in (ROOT / "liquidity_migration", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".", 1)[0])

    distribution_for_import = {
        "PIL": "pillow",
        "certifi": "certifi",
        "matplotlib": "matplotlib",
        "numpy": "numpy",
        "polars": "polars",
        "pyarrow": "pyarrow",
        "pybit": "pybit",
        "websocket": "websocket-client",
        "yaml": "pyyaml",
    }
    required = {distribution_for_import[name] for name in imported & distribution_for_import.keys()}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        re.split(r"[<>=!~\[]", row, maxsplit=1)[0].lower().replace("_", "-")
        for row in project["dependencies"]
    }
    assert required <= declared


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


def test_skill_report_requires_one_linked_tree(tmp_path: Path) -> None:
    """`.claude/skills` is a symlink into `.codex/skills`: one tree, one edit,
    nothing to hand-copy or hash-compare."""

    codex = tmp_path / ".codex" / "skills" / "example"
    codex.mkdir(parents=True)
    (codex / "SKILL.md").write_text("content\n", encoding="utf-8")
    claude_root = tmp_path / ".claude"
    claude_root.mkdir(parents=True)
    (claude_root / "skills").symlink_to(Path("..") / ".codex" / "skills")

    report = repo_doctor.skill_mirror_report(tmp_path)
    assert report["status"] == "matched"
    assert report["mirror_is_link"] is True

    # A diverging hand-maintained copy is exactly what the check refuses.
    (claude_root / "skills").unlink()
    copy = claude_root / "skills" / "example"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("content\n", encoding="utf-8")
    report = repo_doctor.skill_mirror_report(tmp_path)
    assert report["status"] == "error"
    assert report["resolved_equal"] is False


def test_deploy_env_report_flags_a_toggle_nothing_reads(tmp_path: Path) -> None:
    """A deployed toggle whose reader was deleted must fail the doctor: the
    host file goes on carrying a dead switch, and flipping it does nothing."""

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "sleeves.env").write_text("LIVE_KEY=on\nDEAD_KEY=on\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "reader.sh").write_text('echo "$LIVE_KEY"\n', encoding="utf-8")
    (tmp_path / "liquidity_migration").mkdir()

    report = repo_doctor.deploy_env_report(tmp_path, read_elsewhere={})
    assert report["status"] == "error"
    assert report["orphans"] == [{"key": "DEAD_KEY", "defined_in": ["sleeves.env"]}]

    # Giving the dead key a reader clears the check.
    (scripts / "reader.sh").write_text('echo "$LIVE_KEY" "$DEAD_KEY"\n', encoding="utf-8")
    report = repo_doctor.deploy_env_report(tmp_path, read_elsewhere={})
    assert report["status"] == "matched"
    assert report["orphans"] == []


def test_deploy_env_report_matches_whole_names_only(tmp_path: Path) -> None:
    """ENGINE_LIVE inside LIVENESS_ENGINE_LIVE_FILE must not count as a read:
    a substring hit would hide exactly the drift the check exists to catch."""

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "engine.env").write_text("ENGINE_LIVE=false\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "reader.sh").write_text('echo "$LIVENESS_ENGINE_LIVE_FILE"\n', encoding="utf-8")
    (tmp_path / "liquidity_migration").mkdir()

    report = repo_doctor.deploy_env_report(tmp_path, read_elsewhere={})
    assert report["status"] == "error"
    assert [orphan["key"] for orphan in report["orphans"]] == ["ENGINE_LIVE"]


def test_deploy_env_allowlist_cannot_outlive_its_key(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "sleeves.env").write_text("LIVE_KEY=on\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "reader.sh").write_text('echo "$LIVE_KEY"\n', encoding="utf-8")
    (tmp_path / "liquidity_migration").mkdir()

    report = repo_doctor.deploy_env_report(
        tmp_path, read_elsewhere={"GONE_KEY": "was read by a binary"}
    )
    assert report["status"] == "matched"
    assert report["stale_allowlist"] == ["GONE_KEY"]


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
    # The doctor is reporting on this very checkout, so the answer is knowable:
    # the lock is the committed one, and the tree is whatever the run left.
    assert report["dependency_lock"]["status"] == "matched"
    # Internally consistent, always checkable: the doctor's own three fields
    # must agree with each other, so an inverted status or a miscount fails.
    own = report["git"]["changes"]
    assert report["git"]["change_count"] == len(own)
    assert report["git"]["status"] == ("dirty" if own else "clean")

    # And against git itself — but only when the tree held still across the
    # doctor's sample and ours. repo_doctor asks with --untracked-files=all, so
    # we must too (the default collapses an untracked directory to one line);
    # and a second session writing to this checkout between the two samples is
    # a real thing that happens here, not a fault in the doctor.
    def _porcelain() -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--short", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [line for line in out.splitlines() if line.strip()]

    if _porcelain() == _porcelain():
        assert own == _porcelain()
    assert report["skill_mirrors"]["status"] == "matched"
    assert report["deploy_env"]["status"] == "matched"
    assert report["deploy_env"]["stale_allowlist"] == []
    assert "graphify" not in report


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


def test_dev_type_gate_includes_venue_wal_accounting_surfaces() -> None:
    dev = DEV.read_text(encoding="utf-8")

    for target in (
        "liquidity_migration/research/venue_wal_accounting.py",
        "scripts/research/capture_bybit_account_history.py",
        "scripts/research/reconcile_venue_wal.py",
    ):
        assert target in dev
    assert "^liquidity_migration/research/venue_wal_accounting\\.py$" in dev


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
