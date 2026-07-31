#!/usr/bin/env python3
"""Read-only repository and developer-environment diagnostics.

Reports the local facts that commonly make repository work misleading: a dirty
tree, dependency drift, broken skill mirrors, missing navigation tooling.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MINIMUM_PYTHON = (3, 11)
_PIN_PATTERN = re.compile(r"([A-Za-z0-9_.-]+)==([^=\s]+)")
_NORMALIZE_PATTERN = re.compile(r"[-_.]+")
_IGNORED_SKILL_PARTS = {".DS_Store", "__pycache__"}


def normalize_distribution_name(name: str) -> str:
    """Return the normalized distribution key used for lock comparisons."""

    return _NORMALIZE_PATTERN.sub("-", name).lower()


def read_locked_requirements(path: Path) -> dict[str, tuple[str, str]]:
    """Parse the repository's exact ``name==version`` dependency contract."""

    locked: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid requirement pin at {path}:{line_number}: {line!r}")
        name, version = match.groups()
        key = normalize_distribution_name(name)
        if key in locked:
            raise ValueError(f"duplicate normalized requirement {name!r} at {path}:{line_number}")
        locked[key] = (name, version)
    if not locked:
        raise ValueError(f"dependency lock is empty: {path}")
    return locked


def compare_locked_versions(
    locked: Mapping[str, tuple[str, str]],
    installed: Mapping[str, str | None],
) -> dict[str, Any]:
    """Compare normalized installed versions with parsed exact pins."""

    missing: list[dict[str, str]] = []
    mismatched: list[dict[str, str]] = []
    for key in sorted(locked):
        name, expected = locked[key]
        actual = installed.get(key)
        if actual is None:
            missing.append({"name": name, "expected": expected})
        elif actual != expected:
            mismatched.append({"name": name, "expected": expected, "installed": actual})
    return {
        "status": "matched" if not missing and not mismatched else "drift",
        "expected_count": len(locked),
        "missing": missing,
        "mismatched": mismatched,
    }


def dependency_lock_report(repository: Path) -> dict[str, Any]:
    path = repository / "requirements.lock"
    try:
        locked = read_locked_requirements(path)
    except (OSError, ValueError) as exc:
        return {"status": "error", "path": str(path), "error": str(exc)}

    installed: dict[str, str | None] = {}
    for key, (name, _expected) in locked.items():
        try:
            installed[key] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed[key] = None
    return {"path": str(path), **compare_locked_versions(locked, installed)}


def _skill_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in _IGNORED_SKILL_PARTS for part in path.parts):
            continue
        relative = str(path.relative_to(root))
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def skill_mirror_report(repository: Path) -> dict[str, Any]:
    canonical_root = repository / ".codex" / "skills"
    mirror_root = repository / ".claude" / "skills"
    canonical = _skill_tree(canonical_root)
    mirror = _skill_tree(mirror_root)
    canonical_only = sorted(set(canonical) - set(mirror))
    mirror_only = sorted(set(mirror) - set(canonical))
    mismatched = sorted(path for path in set(canonical) & set(mirror) if canonical[path] != mirror[path])
    missing_root = [str(path) for path in (canonical_root, mirror_root) if not path.is_dir()]
    matched = bool(canonical) and not missing_root and not canonical_only and not mirror_only and not mismatched
    return {
        "status": "matched" if matched else "error",
        "canonical_root": str(canonical_root),
        "mirror_root": str(mirror_root),
        "canonical_file_count": len(canonical),
        "mirror_file_count": len(mirror),
        "missing_roots": missing_root,
        "canonical_only": canonical_only,
        "mirror_only": mirror_only,
        "mismatched": mismatched,
    }


def _git_output(repository: Path, *arguments: str, allow_failure: bool = False) -> str:
    command = [
        "git",
        "-c",
        f"safe.directory={repository}",
        "-C",
        str(repository),
        *arguments,
    ]
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    # Strip only line terminators: ``git status --short`` needs its leading
    # status columns preserved.
    return completed.stdout.rstrip("\r\n")


def git_report(repository: Path) -> dict[str, Any]:
    executable = shutil.which("git")
    if executable is None:
        return {"status": "error", "error": "git is not available"}
    try:
        head = _git_output(repository, "rev-parse", "--short=12", "HEAD")
        branch = _git_output(repository, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        changes_text = _git_output(repository, "status", "--short", "--untracked-files=all")
    except RuntimeError as exc:
        return {"status": "error", "executable": executable, "error": str(exc)}
    changes = changes_text.splitlines() if changes_text else []
    return {
        "status": "dirty" if changes else "clean",
        "executable": executable,
        "branch": branch or "(detached)",
        "head": head,
        "change_count": len(changes),
        "changes": changes,
    }


def graphify_report(repository: Path) -> dict[str, Any]:
    executable = shutil.which("graphify")
    report = repository / "graphify-out" / "GRAPH_REPORT.md"
    if executable is None:
        status = "missing_tool"
    elif not report.is_file():
        status = "missing_report"
    else:
        status = "ready"
    return {
        "status": status,
        "executable": executable,
        "report": str(report),
        "report_exists": report.is_file(),
    }


def build_report(repository: Path, *, strict_lock: bool = False) -> dict[str, Any]:
    python_supported = sys.version_info[:2] >= MINIMUM_PYTHON
    lock = dependency_lock_report(repository)
    skills = skill_mirror_report(repository)
    git = git_report(repository)
    graphify = graphify_report(repository)

    errors: list[str] = []
    warnings: list[str] = []
    if not python_supported:
        errors.append(f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is required")
    if git["status"] == "error":
        errors.append(f"Git inspection failed: {git['error']}")
    if skills["status"] != "matched":
        errors.append("Codex and Claude project skill trees are not mechanical mirrors")
    if lock["status"] == "error":
        errors.append(f"Dependency lock inspection failed: {lock['error']}")
    elif lock["status"] == "drift":
        message = "The selected Python environment does not match requirements.lock"
        (errors if strict_lock else warnings).append(message)
    if graphify["status"] == "missing_tool":
        warnings.append("Graphify is unavailable; direct source navigation still works")
    elif graphify["status"] == "missing_report":
        warnings.append("Graphify is installed but the repository report is missing")

    overall = "error" if errors else "warning" if warnings else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "overall": overall,
        "repository": str(repository),
        "python": {
            "executable": sys.executable,
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "minimum": ".".join(str(part) for part in MINIMUM_PYTHON),
            "supported": python_supported,
        },
        "git": git,
        "dependency_lock": lock,
        "skill_mirrors": skills,
        "graphify": graphify,
        "diagnostics": {"errors": errors, "warnings": warnings},
    }


def format_human(report: Mapping[str, Any]) -> str:
    python = report["python"]
    git = report["git"]
    lock = report["dependency_lock"]
    skills = report["skill_mirrors"]
    graphify = report["graphify"]
    lines = [f"repository: {report['repository']}"]
    python_label = "ok" if python["supported"] else "error"
    lines.append(f"[{python_label}] python {python['version']} ({python['executable']})")
    if git["status"] == "error":
        lines.append(f"[error] git: {git['error']}")
    else:
        lines.append(f"[info] git {git['branch']}@{git['head']}: {git['change_count']} worktree change(s)")
    if lock["status"] == "error":
        lines.append(f"[error] dependency lock: {lock['error']}")
    elif lock["status"] == "matched":
        lines.append(f"[ok] dependency lock: {lock['expected_count']} exact pin(s) matched")
    else:
        issue_count = len(lock["missing"]) + len(lock["mismatched"])
        strict_error = (
            "The selected Python environment does not match requirements.lock" in report["diagnostics"]["errors"]
        )
        lock_label = "error" if strict_error else "warn"
        lines.append(f"[{lock_label}] dependency lock: {issue_count} selected-environment drift item(s)")
        for item in lock["missing"]:
            lines.append(f"  missing {item['name']}=={item['expected']}")
        for item in lock["mismatched"]:
            lines.append(f"  {item['name']}: installed {item['installed']}, expected {item['expected']}")
    if skills["status"] == "matched":
        lines.append(f"[ok] skill mirrors: {skills['canonical_file_count']} file(s) matched")
    else:
        lines.append("[error] skill mirrors differ; inspect --json output for exact paths")
    graph_label = "ok" if graphify["status"] == "ready" else "warn"
    lines.append(f"[{graph_label}] graphify: {graphify['status']} ({graphify['report']})")
    lines.append(f"overall: {report['overall']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to the parent of scripts/.",
    )
    parser.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON.")
    parser.add_argument(
        "--strict-lock",
        action="store_true",
        help="Treat selected-environment dependency drift as an error.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = args.repo.expanduser().resolve()
    report = build_report(repository, strict_lock=args.strict_lock)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(report))
    return 1 if report["overall"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
