#!/usr/bin/env python3
"""Read-only repository and developer-environment diagnostics.

Reports the local facts that commonly make repository work misleading: a dirty
tree, dependency drift, a broken skill-tree link, and a deployed toggle that
no code reads.
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
import time
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
    # One skill tree, two names: `.claude/skills` is a symlink into
    # `.codex/skills`, so an edit lands once instead of being hand-copied and
    # hash-compared across two trees.
    canonical_root = repository / ".codex" / "skills"
    mirror_root = repository / ".claude" / "skills"
    canonical = _skill_tree(canonical_root)
    resolved_equal = (
        canonical_root.is_dir()
        and mirror_root.exists()
        and mirror_root.resolve() == canonical_root.resolve()
    )
    matched = bool(canonical) and resolved_equal
    return {
        "status": "matched" if matched else "error",
        "canonical_root": str(canonical_root),
        "mirror_root": str(mirror_root),
        "canonical_file_count": len(canonical),
        "mirror_is_link": mirror_root.is_symlink(),
        "resolved_equal": resolved_equal,
    }


_ENV_KEY_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)

# Keys a deployed env file sets for a reader that is not repository source.
# Each entry needs the reason spelled out; an entry whose key has left the
# env files is reported stale so the list cannot quietly outlive its keys.
_DEPLOY_ENV_KEYS_READ_ELSEWHERE = {
    "RUST_LOG": (
        "read inside the engine binary by the tracing library "
        "(engine-core/src/main.rs), never as a literal in our source"
    ),
}

_DEPLOY_ENV_CONSUMER_SUFFIXES = {".py", ".sh", ".rs", ".service", ".timer", ".toml"}


def read_deploy_env_keys(deploy_root: Path) -> dict[str, list[str]]:
    """Every KEY a deploy env file or template sets, with the files that set it."""

    keys: dict[str, list[str]] = {}
    env_files = sorted(list(deploy_root.glob("*.env")) + list(deploy_root.glob("*.env.template")))
    for path in env_files:
        for key in _ENV_KEY_PATTERN.findall(path.read_text(encoding="utf-8")):
            keys.setdefault(key, []).append(path.name)
    return keys


def _deploy_env_consumer_corpus(repository: Path) -> str:
    """The text of every file that could legitimately read a deployed toggle.

    Docs and tests are deliberately absent: a doc mentioning a key does not
    read it, and a key read only by a test is exactly the drift this check
    exists to catch.
    """

    roots = [
        repository / "liquidity_migration",
        repository / "scripts",
        repository / "deploy",
        *sorted(repository.glob("engine/*/src")),
    ]
    pieces: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _DEPLOY_ENV_CONSUMER_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            pieces.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(pieces)


def deploy_env_report(
    repository: Path,
    *,
    read_elsewhere: Mapping[str, str] = _DEPLOY_ENV_KEYS_READ_ELSEWHERE,
) -> dict[str, Any]:
    """Flag deployed env keys that nothing in the repository reads.

    A toggle nothing reads is drift: either its reader was deleted (the host
    file goes on carrying a dead switch) or the key is a typo and the real
    reader silently falls back to its default.
    """

    deploy_root = repository / "deploy"
    keys = read_deploy_env_keys(deploy_root)
    if not keys:
        return {
            "status": "error",
            "error": f"no env keys found under {deploy_root}",
            "key_count": 0,
            "orphans": [],
            "stale_allowlist": sorted(read_elsewhere),
        }
    corpus = _deploy_env_consumer_corpus(repository)
    orphans = [
        {"key": key, "defined_in": defined_in}
        for key, defined_in in sorted(keys.items())
        if key not in read_elsewhere
        # Word-bounded so ENGINE_LIVE inside LIVENESS_ENGINE_... never counts.
        and re.search(rf"\b{re.escape(key)}\b", corpus) is None
    ]
    stale_allowlist = sorted(key for key in read_elsewhere if key not in keys)
    return {
        "status": "error" if orphans else "matched",
        "key_count": len(keys),
        "orphans": orphans,
        "read_elsewhere": {key: read_elsewhere[key] for key in sorted(read_elsewhere) if key in keys},
        "stale_allowlist": stale_allowlist,
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


_SECOND_WRITER_WINDOW_SECONDS = 600.0


def _recently_modified(repository: Path, changes: Sequence[str], now: float) -> list[str]:
    """Dirty paths whose mtime is inside the second-writer window.

    A dirty file touched in the last few minutes may belong to another agent
    session editing the same checkout — the failure mode is staging someone
    else's half-done work with a broad ``git add``.
    """

    fresh: list[str] = []
    for line in changes:
        path_field = line[3:]
        if " -> " in path_field:
            path_field = path_field.split(" -> ", 1)[1]
        path = repository / path_field.strip().strip('"')
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if 0 <= age <= _SECOND_WRITER_WINDOW_SECONDS:
            fresh.append(path_field.strip())
    return fresh


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
        "recently_modified": _recently_modified(repository, changes, time.time()),
    }


def build_report(repository: Path, *, strict_lock: bool = False) -> dict[str, Any]:
    python_supported = sys.version_info[:2] >= MINIMUM_PYTHON
    lock = dependency_lock_report(repository)
    skills = skill_mirror_report(repository)
    git = git_report(repository)
    deploy_env = deploy_env_report(repository)

    errors: list[str] = []
    warnings: list[str] = []
    if deploy_env["status"] == "error":
        detail = deploy_env.get("error") or ", ".join(
            orphan["key"] for orphan in deploy_env["orphans"]
        )
        errors.append(f"Deployed env toggle(s) nothing reads: {detail}")
    if deploy_env["stale_allowlist"]:
        warnings.append(
            "deploy-env allowlist names keys no env file sets: "
            + ", ".join(deploy_env["stale_allowlist"])
        )
    if not python_supported:
        errors.append(f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is required")
    if git["status"] == "error":
        errors.append(f"Git inspection failed: {git['error']}")
    if skills["status"] != "matched":
        errors.append(
            "`.claude/skills` must be a symlink resolving to `.codex/skills`"
        )
    if lock["status"] == "error":
        errors.append(f"Dependency lock inspection failed: {lock['error']}")
    elif lock["status"] == "drift":
        message = "The selected Python environment does not match requirements.lock"
        (errors if strict_lock else warnings).append(message)

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
        "deploy_env": deploy_env,
        "diagnostics": {"errors": errors, "warnings": warnings},
    }


def format_human(report: Mapping[str, Any]) -> str:
    python = report["python"]
    git = report["git"]
    lock = report["dependency_lock"]
    skills = report["skill_mirrors"]
    lines = [f"repository: {report['repository']}"]
    python_label = "ok" if python["supported"] else "error"
    lines.append(f"[{python_label}] python {python['version']} ({python['executable']})")
    if git["status"] == "error":
        lines.append(f"[error] git: {git['error']}")
    else:
        lines.append(f"[info] git {git['branch']}@{git['head']}: {git['change_count']} worktree change(s)")
        fresh = git.get("recently_modified", [])
        if fresh:
            lines.append(
                f"[warn] {len(fresh)} dirty file(s) modified in the last 10 minutes — "
                "a second writer may be active in this checkout; do not stage their work:"
            )
            lines.extend(f"  {name}" for name in fresh)
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
        lines.append(f"[ok] skills: {skills['canonical_file_count']} file(s), one linked tree")
    else:
        lines.append(
            "[error] .claude/skills must be a symlink to .codex/skills; inspect --json output"
        )
    deploy_env = report["deploy_env"]
    if deploy_env["status"] == "matched":
        lines.append(f"[ok] deploy env: {deploy_env['key_count']} key(s), every one read by code")
    else:
        lines.append(
            f"[error] deploy env: {deploy_env.get('error') or 'toggle(s) nothing reads'}"
        )
        for orphan in deploy_env["orphans"]:
            lines.append(f"  {orphan['key']} (set in {', '.join(orphan['defined_in'])})")
    for stale in deploy_env["stale_allowlist"]:
        lines.append(f"[warn] deploy-env allowlist entry {stale} matches no env file key")
    lines.append(f"overall: {report['overall']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[2],
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
