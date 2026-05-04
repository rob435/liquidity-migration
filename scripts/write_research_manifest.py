from __future__ import annotations

import argparse
import glob
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_ARTIFACTS = (
    "data/research_reports/readiness/research_readiness_report.json",
    "data/research_reports/readiness/research_readiness_report.md",
    "data/daily-close-fade-pit-20230503-20260503/reports/archive_pit_coverage_report.json",
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/core/promotion/volume_promotion_report.json",
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/mid/promotion/volume_promotion_report.json",
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/tail/promotion/volume_promotion_report.json",
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/broad/promotion/volume_promotion_report.json",
    "data/daily-close-fade-1m-3y-current-top160-20230503-20260503/reports/daily_close_fade_grid_splits/daily_close_fade_promotion_report.json",
)
DEFAULT_ARTIFACT_GLOBS = (
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/*/promotion/volume_promotion_report.json",
    "data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/*/promotion/volume_promotion_report.md",
)


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    artifacts = list(args.artifact or DEFAULT_ARTIFACTS)
    manifest = build_research_manifest(
        repo_root=repo_root,
        artifact_paths=artifacts,
        artifact_globs=tuple(DEFAULT_ARTIFACT_GLOBS) + tuple(args.artifact_glob or ()),
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "research_artifact_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output_dir / "research_artifact_manifest.md").write_text(format_research_manifest(manifest), encoding="utf-8")
    missing = sum(1 for row in manifest["artifacts"] if row["status"] == "missing")
    print(
        f"research artifact manifest artifacts={len(manifest['artifacts'])} missing={missing} "
        f"path={output_dir / 'research_artifact_manifest.md'}"
    )
    return 2 if args.strict and missing else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write hashes and git metadata for research report artifacts.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default="data/research_reports/manifest")
    parser.add_argument("--artifact", action="append", help="Artifact path to include. Can be repeated.")
    parser.add_argument("--artifact-glob", action="append", help="Glob of artifact paths to include. Can be repeated.")
    parser.add_argument("--strict", action="store_true", help="Exit 2 if any requested artifact is missing.")
    return parser.parse_args()


def build_research_manifest(
    *,
    repo_root: Path,
    artifact_paths: list[str],
    artifact_globs: tuple[str, ...],
) -> dict[str, Any]:
    paths = _artifact_path_list(repo_root, artifact_paths=artifact_paths, artifact_globs=artifact_globs)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(repo_root),
        "git": git_metadata(repo_root),
        "artifacts": [artifact_row(repo_root, path) for path in paths],
    }


def artifact_row(repo_root: Path, path: Path) -> dict[str, Any]:
    absolute = path if path.is_absolute() else repo_root / path
    relative = _relative_path(repo_root, absolute)
    if not absolute.exists() or not absolute.is_file():
        return {
            "path": relative,
            "status": "missing",
        }
    stat = absolute.stat()
    return {
        "path": relative,
        "status": "present",
        "bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "sha256": sha256_file(absolute),
    }


def git_metadata(repo_root: Path) -> dict[str, Any]:
    commit = _git(repo_root, "rev-parse", "HEAD")
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(repo_root, "status", "--short")
    return {
        "commit": commit or "unknown",
        "branch": branch or "unknown",
        "dirty": bool(status),
        "status_short": status or "",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_research_manifest(manifest: dict[str, Any]) -> str:
    git = manifest.get("git", {})
    artifacts = manifest.get("artifacts", [])
    present = sum(1 for row in artifacts if row.get("status") == "present")
    missing = sum(1 for row in artifacts if row.get("status") == "missing")
    lines = [
        "# Research Artifact Manifest",
        "",
        f"Generated: {manifest.get('generated_at')}",
        f"Git branch: `{git.get('branch', 'unknown')}`",
        f"Git commit: `{git.get('commit', 'unknown')}`",
        f"Git dirty: {git.get('dirty', False)}",
        f"Artifacts: {present} present, {missing} missing",
        "",
        "| Status | Bytes | SHA256 | Path |",
        "|---|---:|---|---|",
    ]
    for row in artifacts:
        lines.append(
            f"| {row.get('status')} | {row.get('bytes', '')} | "
            f"`{str(row.get('sha256', ''))[:16]}` | `{row.get('path')}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _artifact_path_list(
    repo_root: Path,
    *,
    artifact_paths: list[str],
    artifact_globs: tuple[str, ...],
) -> list[Path]:
    seen = set()
    output: list[Path] = []
    for item in artifact_paths:
        path = Path(item)
        key = _artifact_key(repo_root, path)
        if key not in seen:
            output.append(path)
            seen.add(key)
    for pattern in artifact_globs:
        for match in sorted(glob.glob(str(repo_root / pattern), recursive=True)):
            absolute = Path(match)
            key = _artifact_key(repo_root, absolute)
            if key not in seen:
                output.append(absolute)
                seen.add(key)
    return output


def _relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _artifact_key(repo_root: Path, path: Path) -> str:
    absolute = path if path.is_absolute() else repo_root / path
    return str(absolute.resolve())


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


if __name__ == "__main__":
    raise SystemExit(main())
