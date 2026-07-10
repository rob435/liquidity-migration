#!/usr/bin/env python3
"""Plan and readiness-check the registered strategy-overhaul population scout.

This surface deliberately does not run a backtest or select a strategy. It checks
outcome-blind feasibility, inventories proposed Tier-A0 roots and source/config
identities, and emits the ordered big-PC plan described in
``docs/preregistration/strategy-overhaul-scout-2026-07-10.md``.

``--phase0-inventory`` writes only deterministic schema, key, provenance,
resource, and instrument-map feasibility artifacts. Population builders are
exposed as pure library functions and will be wired to a checkpointed runner only
after Phase 0 and the two finite child contracts/analysis manifests are frozen.
No mode on this surface reads labels or runs a strategy.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if not any(Path(entry or os.getcwd()).resolve() == REPO for entry in sys.path):
    sys.path.insert(0, str(REPO))
BOOTSTRAP_SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


PREREG_DATE = "2026-07-10"
RUN_LABEL = "exploratory"
S01_STATUS_DERIVATION_VERSION = "strategy_overhaul_s01_status_v2"
CONTINUOUS_START_DATE = "2023-04-01"
LONG_START_DATE = "2023-06-15"
# Input readiness includes causal warmup, while output populations retain the
# signal windows above. CONTINUOUS needs 888 hourly observations; LONG's frozen
# feature set needs a prior 90-day observation plus the signal day.
CONTINUOUS_READ_START_DATE = "2023-02-23"
LONG_READ_START_DATE = "2023-03-16"
ROOT_START_DATE = min(CONTINUOUS_READ_START_DATE, LONG_READ_START_DATE)
SIGNAL_END_DATE = "2026-07-10"
LABEL_END_DATE = "2026-07-14"
CONTRACT = REPO / "docs" / "preregistration" / "strategy-overhaul-scout-2026-07-10.md"
DIAGNOSIS = REPO / "docs" / "strategy_overhaul_diagnosis_2026-07-10.md"
CONTINUOUS_TEMPLATE = REPO / "docs" / "preregistration" / "strategy-overhaul-continuous-a0.template.md"
CONTINUOUS_ANALYSIS_TEMPLATE = CONTINUOUS_TEMPLATE.with_name("strategy-overhaul-continuous-a0.analysis.template.json")
LONG_TEMPLATE = REPO / "docs" / "preregistration" / "strategy-overhaul-long-a0.template.md"
LONG_ANALYSIS_TEMPLATE = LONG_TEMPLATE.with_name("strategy-overhaul-long-a0.analysis.template.json")
CANONICAL_CHILDREN = {
    "continuous": (
        REPO / "docs" / "preregistration" / "strategy-overhaul-continuous-a0.md",
        REPO / "docs" / "preregistration" / "strategy-overhaul-continuous-a0.analysis.json",
    ),
    "long": (
        REPO / "docs" / "preregistration" / "strategy-overhaul-long-a0.md",
        REPO / "docs" / "preregistration" / "strategy-overhaul-long-a0.analysis.json",
    ),
}
CANONICAL_CHILD_OUTPUT_PATHS = tuple(
    sorted(str(path.relative_to(REPO)) for pair in CANONICAL_CHILDREN.values() for path in pair)
)
TRACKED_PATCH_ARTIFACT = "tracked_worktree.patch"
UNTRACKED_ARCHIVE_ARTIFACT = "untracked_sources.tar"
UNTRACKED_MANIFEST_ARTIFACT = "untracked_sources_manifest.json"
SOURCE_SNAPSHOT_ARTIFACT = "source_snapshot.json"
ENVIRONMENT_MANIFEST_ARTIFACT = "environment_manifest.json"
INSTRUMENT_MAP_ARTIFACT = "instrument_map_input.json"
S02_CONFIG_PARITY_MANIFEST_ARTIFACT = "s02_config_parity_manifest.json"
CONFIG_ARTIFACT_INDEX = "config_artifact_index.json"
CONFIG_ARTIFACT_PATHS = {
    sleeve: {
        "canonical_config": f"{sleeve}_canonical_config.json",
        "registered_scope": f"{sleeve}_registered_scope.json",
        "config_identity": f"{sleeve}_config_identity.json",
        **({"component_config": "continuous_component_config.json"} if sleeve == "continuous" else {}),
    }
    for sleeve in ("continuous", "long")
}


@dataclasses.dataclass(frozen=True)
class SourceSnapshot:
    """Exact reconstructable source state, excluding downstream S01 outputs."""

    git: dict[str, Any]
    manifest: dict[str, Any]
    tracked_patch: bytes
    untracked_archive: bytes
    untracked_manifest: dict[str, Any]


REQUIRED_S01_FIELDS = {
    "continuous": frozenset(
        {
            "run_id",
            "canonical_contract_path",
            "canonical_analysis_manifest_path",
            "repository_commit",
            "worktree_policy",
            "patch_bundle_sha256",
            "untracked_source_bundle_sha256",
            "environment_lock_path",
            "environment_lock_sha256",
            "canonical_config_json_path",
            "canonical_config_sha256",
            "config_identity_json_path",
            "config_identity_sha256",
            "registered_scope_json_path",
            "registered_scope_sha256",
            "component_config_json_path",
            "component_config_sha256",
            "s02_config_parity_manifest_path",
            "s02_config_parity_manifest_sha256",
            "source_function_hashes",
            "bybit_root_receipt",
            "binance_root_receipt",
            "pit_manifest_receipts",
            "signal_feature_schema_path",
            "signal_feature_schema_sha256",
            "entry_anchor_schema_path",
            "entry_anchor_schema_sha256",
            "path_label_schema_path",
            "path_label_schema_sha256",
            "instrument_map_version",
            "instrument_map_sha256",
            "instrument_map_row_coverage",
            "instrument_map_symbol_coverage",
            "phase0_support_counts",
            "resource_plan",
            "partition_checkpoint_plan",
            "output_root",
            "exposure_ledger_path",
            "label_tail_root_receipt",
        }
    ),
    "long": frozenset(
        {
            "run_id",
            "canonical_contract_path",
            "canonical_analysis_manifest_path",
            "repository_commit",
            "worktree_policy",
            "patch_bundle_sha256",
            "untracked_source_bundle_sha256",
            "environment_lock_path",
            "environment_lock_sha256",
            "canonical_config_json_path",
            "canonical_config_sha256",
            "config_identity_json_path",
            "config_identity_sha256",
            "registered_scope_json_path",
            "registered_scope_sha256",
            "s02_config_parity_manifest_path",
            "s02_config_parity_manifest_sha256",
            "source_function_hashes",
            "bybit_root_receipt",
            "binance_root_receipt",
            "pit_manifest_receipts",
            "signal_feature_schema_path",
            "signal_feature_schema_sha256",
            "entry_policy_schema_path",
            "entry_policy_schema_sha256",
            "path_label_schema_path",
            "path_label_schema_sha256",
            "instrument_map_version",
            "instrument_map_sha256",
            "instrument_map_row_coverage",
            "instrument_map_symbol_coverage",
            "phase0_support_counts",
            "resource_plan",
            "partition_checkpoint_plan",
            "output_root",
            "exposure_ledger_path",
            "label_tail_root_receipt",
        }
    ),
}
SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA"))).expanduser()
DEFAULT_ROOTS = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}
DEFAULT_PHASE0_OUTPUT_ROOT = SHARED / "strategy_overhaul_scout_2026-07-10" / "phase0"
FUNDING_DATASET = {"bybit": "funding", "binance": "binance_usdm_funding"}

PROPOSED_POINT_LABEL_HORIZONS_H = (1, 24, 72)
PROPOSED_EXCURSION_HORIZONS_H = (24, 72)

STAGES: tuple[dict[str, Any], ...] = (
    {
        "id": "S00",
        "name": "outcome_blind_feasibility",
        "depends_on": [],
        "claim": "availability, provenance, resources, schema, and support counts only",
    },
    {
        "id": "S01",
        "name": "freeze_sleeve_child_contracts",
        "depends_on": ["S00"],
        "claim": "separate finite CONTINUOUS-A0 and LONG-A0 contracts",
    },
    {
        "id": "S02",
        "name": "feature_only_population_tapes",
        "depends_on": ["S01"],
        "claim": "strictly signal-time pre-filter features and static-rule reconstruction",
    },
    {
        "id": "S03",
        "name": "entry_anchor_or_policy_artifacts",
        "depends_on": ["S02:parity_pass"],
        "claim": "separate keyed CONTINUOUS anchor and LONG entry-policy artifacts",
    },
    {
        "id": "S04",
        "name": "minimal_path_label_artifacts",
        "depends_on": ["S03:parity_pass"],
        "claim": "separate keyed 1h/24h/72h point paths and 24h/72h excursions",
    },
    {
        "id": "S05",
        "name": "finite_child_manifest_analysis",
        "depends_on": ["S04:parity_pass"],
        "claim": "support, overlap, small frozen univariate set, controls, dependence",
    },
    {
        "id": "S06",
        "name": "bounded_hypothesis_dossier",
        "depends_on": ["S05"],
        "claim": "at most two hypotheses per sleeve; default unidentified",
    },
)

DEFERRED_CONTRACTS = (
    "continuous_tp_hold_cost_surface",
    "long_stop_take_profit_surface",
    "sizing_and_btc_risk_calibration",
    "granular_adverse_state_active_contract",
    "fixed_epoch_forward_execution",
    "cross_sleeve_portfolio_margin_hedge",
    "deployment_or_prospective_evaluation",
)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _json_hash(value: Any) -> str:
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_child_excluding_pathspecs() -> list[str]:
    return [".", *(f":(exclude){path}" for path in CANONICAL_CHILD_OUTPUT_PATHS)]


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout


def _deterministic_untracked_archive(
    files: list[tuple[str, bytes]],
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative, data in files:
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            info.pax_headers = {}
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _capture_source_snapshot() -> SourceSnapshot:
    """Capture every reconstructable source byte, excluding canonical S01 outputs."""

    pathspecs = _canonical_child_excluding_pathspecs()
    commit = _git_bytes("rev-parse", "HEAD").decode("ascii").strip()
    tracked_patch = _git_bytes("diff", "--binary", "HEAD", "--", *pathspecs)
    tracked_names_raw = _git_bytes("diff", "--name-only", "-z", "HEAD", "--", *pathspecs)
    tracked_paths = sorted(
        path.decode("utf-8", errors="surrogateescape") for path in tracked_names_raw.split(b"\0") if path
    )
    untracked_raw = _git_bytes(
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
    )
    untracked_paths = sorted(
        path.decode("utf-8", errors="surrogateescape") for path in untracked_raw.split(b"\0") if path
    )

    untracked_files: list[tuple[str, bytes]] = []
    untracked_rows: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    for relative in untracked_paths:
        path = REPO / relative
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            unsupported.append({"path": relative, "reason": f"lstat failed: {exc}"})
            continue
        if not stat.S_ISREG(mode):
            unsupported.append(
                {
                    "path": relative,
                    "reason": "untracked path is not a regular file and cannot be source-bundled",
                }
            )
            continue
        data = path.read_bytes()
        untracked_files.append((relative, data))
        untracked_rows.append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "source_mode": f"{stat.S_IMODE(mode):04o}",
                "archive_mode": "0644",
                "archive_mtime": 0,
                "archive_uid": 0,
                "archive_gid": 0,
            }
        )

    untracked_archive = _deterministic_untracked_archive(untracked_files)
    untracked_archive_sha256 = hashlib.sha256(untracked_archive).hexdigest()
    untracked_manifest = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_untracked_source_manifest",
        "archive_path": UNTRACKED_ARCHIVE_ARTIFACT,
        "archive_format": "deterministic_posix_pax_tar",
        "archive_sha256": untracked_archive_sha256,
        "normalization": {
            "path_order": "unicode_codepoint_ascending_git_relative_path",
            "mode": "0644",
            "mtime": 0,
            "uid": 0,
            "gid": 0,
            "uname": "",
            "gname": "",
            "regular_files_only": True,
            "source_mode_recorded_separately": True,
        },
        "files": untracked_rows,
        "unsupported_untracked_paths": unsupported,
        "canonical_child_outputs_excluded": list(CANONICAL_CHILD_OUTPUT_PATHS),
    }
    untracked_manifest["artifact_sha256"] = _json_hash(untracked_manifest)
    untracked_manifest_file_sha256 = hashlib.sha256(_render_json(untracked_manifest)).hexdigest()
    tracked_patch_sha256 = hashlib.sha256(tracked_patch).hexdigest()
    clean = not tracked_patch and not untracked_rows and not unsupported
    snapshot_ready = not unsupported
    worktree_state = (
        "verified_clean_snapshot"
        if clean
        else "verified_reconstructable_dirty_snapshot"
        if snapshot_ready
        else "unreconstructable_source_state"
    )
    manifest = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_source_snapshot",
        "repository_commit": commit,
        "worktree_state": worktree_state,
        "snapshot_ready": snapshot_ready,
        "clean_excluding_canonical_child_outputs": clean,
        "canonical_child_outputs_excluded": list(CANONICAL_CHILD_OUTPUT_PATHS),
        "tracked_patch": {
            "path": TRACKED_PATCH_ARTIFACT,
            "sha256": tracked_patch_sha256,
            "bytes": len(tracked_patch),
            "command": "git diff --binary HEAD -- <canonical-child-excluding-pathspecs>",
        },
        "untracked_sources": {
            "archive_path": UNTRACKED_ARCHIVE_ARTIFACT,
            "archive_sha256": untracked_archive_sha256,
            "manifest_path": UNTRACKED_MANIFEST_ARTIFACT,
            "manifest_file_sha256": untracked_manifest_file_sha256,
            "manifest_payload_sha256": untracked_manifest["artifact_sha256"],
            "file_count": len(untracked_rows),
            "unsupported_path_count": len(unsupported),
        },
        "tracked_changed_paths": tracked_paths,
        "untracked_regular_paths": [row["path"] for row in untracked_rows],
    }
    manifest["artifact_sha256"] = _json_hash(manifest)
    git = {
        "commit": commit,
        "dirty_paths": [
            *(f"tracked:{path}" for path in tracked_paths),
            *(f"untracked:{row['path']}" for row in untracked_rows),
            *(f"unsupported:{row['path']}" for row in unsupported),
        ],
        "clean": clean,
        "clean_semantics": "excludes canonical downstream S01 child outputs",
        "snapshot_ready": snapshot_ready,
        "worktree_state": worktree_state,
        "tracked_diff_sha256": tracked_patch_sha256,
        "tracked_diff_bytes": len(tracked_patch),
        "untracked_source_bundle_sha256": untracked_archive_sha256,
        "untracked_source_file_count": len(untracked_rows),
        "untracked_manifest_file_sha256": untracked_manifest_file_sha256,
        "unsupported_untracked_paths": unsupported,
        "source_snapshot_sha256": manifest["artifact_sha256"],
        "canonical_child_outputs_excluded": list(CANONICAL_CHILD_OUTPUT_PATHS),
    }
    return SourceSnapshot(
        git=git,
        manifest=manifest,
        tracked_patch=tracked_patch,
        untracked_archive=untracked_archive,
        untracked_manifest=untracked_manifest,
    )


def _assert_source_snapshot_unchanged(expected: SourceSnapshot, observed: SourceSnapshot) -> None:
    if expected != observed:
        raise RuntimeError("repository source/git snapshot changed during Phase-0 scan; refusing mixed-identity bundle")


def _restore_source_snapshot(snapshot: SourceSnapshot, destination: Path) -> dict[str, Any]:
    """Restore and verify one snapshot into a clean checkout of its bound commit."""

    destination = destination.resolve()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if commit != snapshot.git["commit"]:
        raise RuntimeError("source snapshot restore checkout is not at the bound repository commit")
    pathspecs = _canonical_child_excluding_pathspecs()
    initial_patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", *pathspecs],
        cwd=destination,
        check=True,
        capture_output=True,
    ).stdout
    initial_untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
        cwd=destination,
        check=True,
        capture_output=True,
    ).stdout
    if initial_patch or initial_untracked:
        raise RuntimeError("source snapshot restore requires a clean canonical-child-excluding checkout")

    if snapshot.tracked_patch:
        subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
            cwd=destination,
            check=True,
            input=snapshot.tracked_patch,
            capture_output=True,
        )

    expected_rows = {str(row["path"]): row for row in snapshot.untracked_manifest["files"]}
    with tarfile.open(fileobj=io.BytesIO(snapshot.untracked_archive), mode="r:") as archive:
        members = archive.getmembers()
        if [member.name for member in members] != list(expected_rows):
            raise RuntimeError("untracked source archive paths disagree with its manifest")
        for member in members:
            if not member.isfile():
                raise RuntimeError(f"untracked source archive member is not regular: {member.name}")
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"untracked source archive path escapes checkout: {member.name}") from exc
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError(f"cannot read untracked source archive member: {member.name}")
            data = handle.read()
            row = expected_rows[member.name]
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise RuntimeError(f"untracked source archive content mismatch: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(int(str(row["source_mode"]), 8))

    restored_patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", *pathspecs],
        cwd=destination,
        check=True,
        capture_output=True,
    ).stdout
    restored_untracked_raw = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
        cwd=destination,
        check=True,
        capture_output=True,
    ).stdout
    restored_untracked = [
        path.decode("utf-8", errors="surrogateescape") for path in restored_untracked_raw.split(b"\0") if path
    ]
    if restored_patch != snapshot.tracked_patch:
        raise RuntimeError("restored tracked patch differs from the captured binary patch")
    if restored_untracked != list(expected_rows):
        raise RuntimeError("restored untracked path inventory differs from the captured manifest")
    return {
        "status": "VERIFIED",
        "repository_commit": commit,
        "tracked_patch_sha256": hashlib.sha256(restored_patch).hexdigest(),
        "untracked_file_count": len(restored_untracked),
        "source_snapshot_sha256": snapshot.manifest["artifact_sha256"],
    }


def _git_state() -> dict[str, Any]:
    return _capture_source_snapshot().git


_EXACT_LOCKFILE_NAMES = frozenset(
    {
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "package-lock.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)


def _repository_dependency_files() -> list[dict[str, Any]]:
    raw = _git_bytes("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    relative_paths = sorted(path.decode("utf-8", errors="surrogateescape") for path in raw.split(b"\0") if path)
    rows: list[dict[str, Any]] = []
    for relative in relative_paths:
        name = Path(relative).name
        lowered = name.lower()
        exact_lock = lowered in _EXACT_LOCKFILE_NAMES or lowered.startswith("conda-lock.")
        dependency_spec = (
            exact_lock
            or lowered == "pyproject.toml"
            or fnmatch.fnmatch(lowered, "requirements*.txt")
            or fnmatch.fnmatch(lowered, "environment*.yml")
            or fnmatch.fnmatch(lowered, "environment*.yaml")
        )
        if not dependency_spec:
            continue
        path = REPO / relative
        if not path.is_file():
            continue
        rows.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
                "classification": "exact_resolver_lock" if exact_lock else "dependency_specification",
                "sufficient_as_environment_lock": False,
            }
        )
    return rows


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_PHASE0_EXECUTED_MODULES = (
    "numpy",
    "numpy._core._multiarray_umath",
    "polars",
    "polars._plr",
    "pyarrow",
    "pyarrow.lib",
    "pyarrow.parquet",
)
_DISTRIBUTION_PROVENANCE_FILES = (
    "direct_url.json",
    "INSTALLER",
    "METADATA",
    "RECORD",
    "WHEEL",
)


def _distribution_receipt(distribution: Any) -> dict[str, Any]:
    name = distribution.metadata.get("Name") or getattr(distribution, "name", None)
    if not name:
        raise RuntimeError("installed distribution lacks a package name")
    version = str(distribution.version)
    try:
        location = str(Path(distribution.locate_file("")).resolve())
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"cannot resolve installed distribution location for {name}") from exc

    provenance_files: dict[str, dict[str, Any]] = {}
    for filename in _DISTRIBUTION_PROVENANCE_FILES:
        try:
            text_value = distribution.read_text(filename)
        except Exception as exc:  # importlib metadata backends are third-party implementations
            raise RuntimeError(f"cannot read {filename} metadata for installed distribution {name}") from exc
        if text_value is None:
            continue
        data = text_value.encode("utf-8")
        provenance_files[filename] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }

    declared_files = distribution.files
    if declared_files is None:
        raise RuntimeError(f"installed distribution {name} has no declared file manifest")
    declared_rows: list[dict[str, Any]] = []
    for entry in sorted(declared_files, key=lambda value: str(value)):
        file_hash = getattr(entry, "hash", None)
        declared_rows.append(
            {
                "path": str(entry),
                "declared_hash": (f"{file_hash.mode}={file_hash.value}" if file_hash is not None else None),
                "declared_size": getattr(entry, "size", None),
            }
        )
    declared_manifest_sha256 = _json_hash(declared_rows)
    payload = {
        "name": str(name),
        "normalized_name": _normalized_distribution_name(str(name)),
        "version": version,
        "location": location,
        "provenance_files": provenance_files,
        "declared_file_count": len(declared_rows),
        "declared_hashed_file_count": sum(row["declared_hash"] is not None for row in declared_rows),
        "declared_file_manifest_sha256": declared_manifest_sha256,
    }
    payload["distribution_identity_sha256"] = _json_hash(payload)
    return payload


def _executed_module_receipts() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in _PHASE0_EXECUTED_MODULES:
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        if not origin:
            raise RuntimeError(f"Phase-0 executed module {name} has no file origin")
        path = Path(origin).resolve()
        sha256 = _sha256_file(path)
        if sha256 is None:
            raise RuntimeError(f"Phase-0 executed module origin is not a regular file: {name} -> {path}")
        rows.append(
            {
                "module": name,
                "origin": str(path),
                "sha256": sha256,
                "bytes": path.stat().st_size,
            }
        )
    return rows


def _environment_receipt() -> dict[str, Any]:
    discovered = [_distribution_receipt(distribution) for distribution in importlib.metadata.distributions()]
    discovered.sort(key=lambda row: (row["normalized_name"], row["version"], row["name"]))
    distributions: list[dict[str, Any]] = []
    by_normalized_name: dict[str, dict[str, Any]] = {}
    conflicting_names: set[str] = set()
    for row in discovered:
        name = row["normalized_name"]
        prior = by_normalized_name.get(name)
        if prior is None:
            by_normalized_name[name] = row
            distributions.append(row)
        elif prior == row:
            # importlib.metadata may expose the same metadata directory twice
            # when two sys.path entries resolve to one physical location.  This
            # is a discovery alias, not two installed environments.  Collapse
            # only byte-for-byte identical receipts; distinct versions,
            # locations, manifests, or provenance still fail closed below.
            continue
        else:
            conflicting_names.add(name)
    if conflicting_names:
        raise RuntimeError(
            f"conflicting duplicate normalized installed distribution names: {sorted(conflicting_names)}"
        )
    executed_modules = _executed_module_receipts()
    python_executable = Path(sys.executable).resolve()
    python_executable_sha256 = _sha256_file(python_executable)
    if python_executable_sha256 is None:
        raise RuntimeError(f"Python executable is not a regular file: {python_executable}")
    return {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_exact_environment_manifest",
        "identity_strength": "distribution_provenance_declared_content_and_executed_module_bytes",
        "content_identity_ready": True,
        "lock_semantics": (
            "exact observed distribution names/versions/locations/provenance/declared-file manifests plus "
            "actual bytes for the Phase-0 numerical/Parquet modules; repository dependency files are "
            "content-bound inputs and never substitute for this observed environment"
        ),
        "content_scope_limitation": (
            "unexecuted third-party files are bound by installed metadata/RECORD declarations rather than "
            "rehashing every installed byte; every module imported for Phase-0 data access is byte-hashed"
        ),
        "python": {
            "version": platform.python_version(),
            "version_info": list(sys.version_info),
            "implementation": platform.python_implementation(),
            "implementation_version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "executable": str(python_executable),
            "executable_sha256": python_executable_sha256,
            "executable_bytes": python_executable.stat().st_size,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python_platform": platform.platform(),
        },
        "installed_distribution_count": len(distributions),
        "installed_distributions": distributions,
        "executed_module_count": len(executed_modules),
        "executed_modules": executed_modules,
        "repo_dependency_files": _repository_dependency_files(),
    }


def _environment_manifest_ready(environment: dict[str, Any]) -> bool:
    installed = environment.get("installed_distributions")
    executed = environment.get("executed_modules")
    python = environment.get("python")
    if not (
        environment.get("artifact_type") == "strategy_overhaul_exact_environment_manifest"
        and environment.get("content_identity_ready") is True
        and environment.get("identity_strength") == "distribution_provenance_declared_content_and_executed_module_bytes"
        and isinstance(installed, list)
        and bool(installed)
        and all(isinstance(row, dict) for row in installed)
        and environment.get("installed_distribution_count") == len(installed)
        and isinstance(executed, list)
        and environment.get("executed_module_count") == len(executed)
        and isinstance(python, dict)
        and isinstance(environment.get("platform"), dict)
        and isinstance(environment.get("repo_dependency_files"), list)
    ):
        return False
    normalized_names = [row.get("normalized_name") for row in installed]
    if (
        normalized_names != sorted(normalized_names)
        or len(normalized_names) != len(set(normalized_names))
        or any(
            row.get("distribution_identity_sha256")
            != _json_hash({key: value for key, value in row.items() if key != "distribution_identity_sha256"})
            for row in installed
        )
    ):
        return False
    if [row.get("module") for row in executed] != list(_PHASE0_EXECUTED_MODULES):
        return False
    for row in executed:
        origin = row.get("origin")
        if not origin or _sha256_file(Path(str(origin))) != row.get("sha256"):
            return False
    executable = python.get("executable")
    if not executable or _sha256_file(Path(str(executable))) != python.get("executable_sha256"):
        return False
    return True


def _source_receipts() -> dict[str, Any]:
    sources = (
        CONTRACT,
        DIAGNOSIS,
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_demo.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
        REPO / "liquidity_migration" / "continuous_component_sources.py",
        REPO / "liquidity_migration" / "promoted.py",
        REPO / "liquidity_migration" / "long_native.py",
        REPO / "liquidity_migration" / "long_native_event_demo.py",
        REPO / "liquidity_migration" / "binance_vision.py",
        REPO / "liquidity_migration" / "continuous_population_scout.py",
        REPO / "liquidity_migration" / "long_population_scout.py",
        REPO / "liquidity_migration" / "strategy_overhaul_phase0.py",
        REPO / "liquidity_migration" / "strategy_overhaul_population_keys.py",
        REPO / "liquidity_migration" / "strategy_overhaul_phase0_verifier.py",
        REPO / "liquidity_migration" / "strategy_overhaul_schemas.py",
        REPO / "liquidity_migration" / "strategy_overhaul_config_identity.py",
        REPO / "liquidity_migration" / "strategy_overhaul_context.py",
        REPO / "liquidity_migration" / "strategy_overhaul_identity_adapter.py",
        REPO / "liquidity_migration" / "strategy_overhaul_instrument_map.py",
        REPO / "liquidity_migration" / "strategy_overhaul_long_context.py",
        REPO / "liquidity_migration" / "strategy_overhaul_long_s02.py",
        REPO / "liquidity_migration" / "strategy_overhaul_long_sidecars.py",
        REPO / "liquidity_migration" / "strategy_overhaul_long_stages.py",
        REPO / "liquidity_migration" / "strategy_overhaul_projection.py",
        REPO / "liquidity_migration" / "strategy_overhaul_rmom_availability.py",
        REPO / "liquidity_migration" / "strategy_overhaul_root_snapshot.py",
        REPO / "liquidity_migration" / "strategy_overhaul_s02.py",
        REPO / "liquidity_migration" / "strategy_overhaul_stage_receipt.py",
        REPO / "scripts" / "build_full_pit_bybit.sh",
        REPO / "scripts" / "build_full_pit_binance.sh",
        REPO / "scripts" / "precompute_residual_momentum.py",
        REPO / "scripts" / "strategy_overhaul_scout_2026_07_10.py",
        CONTINUOUS_TEMPLATE,
        CONTINUOUS_ANALYSIS_TEMPLATE,
        LONG_TEMPLATE,
        LONG_ANALYSIS_TEMPLATE,
    )
    return {
        str(path.relative_to(REPO)): {
            "present": path.is_file(),
            "sha256": _sha256_file(path),
        }
        for path in sources
    }


def _assert_source_receipts_unchanged(expected: dict[str, Any]) -> None:
    observed = _source_receipts()
    if observed != expected:
        changed = sorted(key for key in set(observed) | set(expected) if observed.get(key) != expected.get(key))
        raise RuntimeError(f"Phase-0 source files changed during identity construction: {changed}")


def _config_receipts() -> dict[str, Any]:
    from liquidity_migration.strategy_overhaul_config_identity import (
        derive_a0_config_identities,
        s02_config_parity_manifest,
        verify_a0_config_identity,
    )

    identities = derive_a0_config_identities()
    if tuple(identities) != ("continuous", "long"):
        raise RuntimeError("canonical A0 config identities must contain continuous then long")
    for identity in identities.values():
        verify_a0_config_identity(identity)
    return {
        **identities,
        "s02_config_parity_manifest": s02_config_parity_manifest(identities),
    }


def _config_bundle_artifacts(
    configs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from liquidity_migration.strategy_overhaul_config_identity import verify_a0_config_identity

    payloads: dict[str, Any] = {}
    sleeves: dict[str, Any] = {}
    for sleeve in ("continuous", "long"):
        identity = configs.get(sleeve)
        if not isinstance(identity, dict):
            raise RuntimeError(f"missing canonical {sleeve} A0 config identity")
        verify_a0_config_identity(identity)
        role_payloads = {
            "canonical_config": identity["canonical_config"],
            "registered_scope": identity["scope"],
            "config_identity": identity,
        }
        if sleeve == "continuous":
            role_payloads["component_config"] = identity["component_config"]
        sleeve_index: dict[str, Any] = {}
        for role, payload in role_payloads.items():
            path = CONFIG_ARTIFACT_PATHS[sleeve][role]
            rendered = _render_json(payload)
            payloads[path] = payload
            sleeve_index[role] = {
                "path": path,
                "file_sha256": hashlib.sha256(rendered).hexdigest(),
                "payload_sha256": _json_hash(payload),
            }
        sleeves[sleeve] = sleeve_index

    parity = configs.get("s02_config_parity_manifest")
    if not isinstance(parity, dict) or parity.get("artifact_type") != "strategy_overhaul_a0_s02_config_parity_manifest":
        raise RuntimeError("canonical S02 config parity manifest is absent or invalid")
    parity_rendered = _render_json(parity)
    payloads[S02_CONFIG_PARITY_MANIFEST_ARTIFACT] = parity
    result = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_a0_config_artifact_index",
        "sleeves": sleeves,
        "s02_config_parity_manifest": {
            "path": S02_CONFIG_PARITY_MANIFEST_ARTIFACT,
            "file_sha256": hashlib.sha256(parity_rendered).hexdigest(),
            "payload_sha256": _json_hash(parity),
            "status": parity.get("status"),
        },
        "parity_status_semantics": (
            "UNWIRED is a blocking implementation status; artifact materialization does not clear config parity debt"
        ),
    }
    result["artifact_sha256"] = _json_hash(result)
    payloads[CONFIG_ARTIFACT_INDEX] = result
    return payloads, result


def _date_range(start_date: str, end_date: str) -> tuple[str, ...]:
    day = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    values: list[str] = []
    while day < end:
        values.append(day.isoformat())
        day += dt.timedelta(days=1)
    return tuple(values)


def _partition_has_file(path: Path) -> bool:
    try:
        return next((True for item in path.rglob("*.parquet") if item.is_file()), False)
    except OSError:
        return False


def _shallow_partition_inventory(
    root: Path,
    dataset: str,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Outcome-blind partition-name/first-file check for the fast Phase-0 plan."""
    base = root / dataset
    required = _date_range(start_date, end_date)
    present = (
        {path.name.split("=", 1)[1]: path for path in base.glob("date=*") if path.is_dir() and "=" in path.name}
        if base.is_dir()
        else {}
    )
    missing = [day for day in required if day not in present]
    empty = [day for day in required if day in present and not _partition_has_file(present[day])]
    return {
        "dataset": dataset,
        "exists": base.is_dir(),
        "required_start_date": start_date,
        "required_end_date_exclusive": end_date,
        "required_partition_count": len(required),
        "present_required_partition_count": len(required) - len(missing),
        "missing_partition_count": len(missing),
        "missing_partition_sample": missing[:10],
        "empty_partition_count": len(empty),
        "empty_partition_sample": empty[:10],
        "ready": base.is_dir() and not missing and not empty,
    }


def _shallow_root_plan(venue: str, root: Path) -> dict[str, Any]:
    phase0 = {
        "klines": _shallow_partition_inventory(
            root,
            "klines_1h",
            start_date=ROOT_START_DATE,
            end_date=SIGNAL_END_DATE,
        ),
        "membership": _shallow_partition_inventory(
            root,
            "archive_trade_manifest",
            start_date=ROOT_START_DATE,
            end_date=SIGNAL_END_DATE,
        ),
    }
    proposed_label = {
        "klines": _shallow_partition_inventory(
            root,
            "klines_1h",
            start_date=ROOT_START_DATE,
            end_date=LABEL_END_DATE,
        ),
        "membership": phase0["membership"],
        "funding": _shallow_partition_inventory(
            root,
            FUNDING_DATASET[venue],
            start_date=ROOT_START_DATE,
            end_date=LABEL_END_DATE,
        ),
    }
    rmom_path = root / "residual_momentum.parquet"
    rmom = {
        "path": str(rmom_path),
        "present": rmom_path.is_file(),
        "compression_or_byte_size_read": False,
        "deep_validation_deferred": True,
    }
    phase0_ready = all(row["ready"] for row in phase0.values())
    a0_population_ready = proposed_label["klines"]["ready"] and proposed_label["membership"]["ready"]
    receipt_candidates = (
        root / "root_build_receipt.json",
        root / "_root_build_receipt.json",
        root / "reports" / "root_build_receipt.json",
    )
    receipt = next((path for path in receipt_candidates if path.is_file()), None)
    return {
        "venue": venue,
        "root": str(root.resolve()),
        "phase0_inventory": phase0,
        "proposed_label_inventory": proposed_label,
        "residual_momentum": rmom,
        "phase0_source_ready": phase0_ready,
        "tier_a0_population_ready": a0_population_ready,
        "rmom_reconstruction_ready": False,
        "tier_a0_label_ready": False,
        "tier_a1_funding_ready": False,
        "registered_receipt_ready": False,
        "root_receipt_present": receipt is not None,
        "root_receipt_path": str(receipt) if receipt is not None else None,
        "deep_validation_deferred": True,
        "deep_root_hash_requested": False,
    }


def _root_plan(venue: str, root: Path, *, deep_root_hash: bool) -> dict[str, Any]:
    if not root.is_dir():
        return {
            "venue": venue,
            "root": str(root),
            "phase0_source_ready": False,
            "tier_a0_label_ready": False,
            "registered_receipt_ready": False,
            "failures": ["root does not exist"],
        }
    if not deep_root_hash:
        return _shallow_root_plan(venue, root)
    from scripts.continuous_tail_survival_2026_07_10 import root_inventory

    phase0_inventory = root_inventory(
        venue,
        root,
        start_date=ROOT_START_DATE,
        signal_end_date=SIGNAL_END_DATE,
        exit_end_date=SIGNAL_END_DATE,
        validate_build_receipt=False,
        exact_content_hash=False,
    )
    label_inventory = root_inventory(
        venue,
        root,
        start_date=ROOT_START_DATE,
        signal_end_date=SIGNAL_END_DATE,
        exit_end_date=LABEL_END_DATE,
        validate_build_receipt=deep_root_hash,
        exact_content_hash=deep_root_hash,
    )
    phase0_datasets = phase0_inventory.get("datasets", {})
    label_datasets = label_inventory.get("datasets", {})
    phase0_source_ready = all(bool((phase0_datasets.get(name) or {}).get("ready")) for name in ("klines", "membership"))
    tier_a0_population_ready = all(
        bool((label_datasets.get(name) or {}).get("ready")) for name in ("klines", "membership")
    )
    rmom_reconstruction_ready = bool((label_inventory.get("residual_momentum") or {}).get("ready"))
    tier_a0_label_ready = tier_a0_population_ready and rmom_reconstruction_ready
    tier_a1_funding_ready = tier_a0_label_ready and bool((label_datasets.get("funding") or {}).get("ready"))
    return {
        "venue": venue,
        "root": str(root.resolve()),
        "phase0_inventory": phase0_inventory,
        "proposed_label_inventory": label_inventory,
        "phase0_source_ready": phase0_source_ready,
        "tier_a0_population_ready": tier_a0_population_ready,
        "rmom_reconstruction_ready": rmom_reconstruction_ready,
        "tier_a0_label_ready": tier_a0_label_ready,
        "tier_a1_funding_ready": tier_a1_funding_ready,
        "registered_receipt_ready": bool((label_inventory.get("root_build_receipt") or {}).get("valid")),
        "deep_root_hash_requested": deep_root_hash,
    }


def _contains_phase0_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return "REQUIRED_PHASE0_SUBSTITUTION" in value
    if isinstance(value, dict):
        return any(
            _contains_phase0_placeholder(key) or _contains_phase0_placeholder(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_phase0_placeholder(item) for item in value)
    return False


def _canonical_child_status(sleeve: str, contract: Path, analysis: Path) -> dict[str, Any]:
    """Validate canonical S01 filenames and return an external hash receipt."""

    reasons: list[str] = []
    contract_sha = _sha256_file(contract)
    analysis_sha = _sha256_file(analysis)
    if contract_sha is None:
        reasons.append("canonical contract file is absent")
    else:
        contract_text = contract.read_text(encoding="utf-8")
        if "REQUIRED_PHASE0_SUBSTITUTION" in contract_text:
            reasons.append("canonical contract contains unresolved Phase-0 placeholders")
        if "Status: FROZEN CANONICAL CHILD" not in contract_text:
            reasons.append("canonical contract does not declare FROZEN CANONICAL CHILD status")

    analysis_payload: dict[str, Any] | None = None
    if analysis_sha is None:
        reasons.append("canonical analysis manifest is absent")
    else:
        try:
            raw_payload = json.loads(analysis.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"canonical analysis manifest is invalid JSON: {exc}")
        else:
            if not isinstance(raw_payload, dict):
                reasons.append("canonical analysis manifest must be a JSON object")
            else:
                analysis_payload = raw_payload
                expected = {
                    "sleeve": sleeve.upper(),
                    "template_status": "FROZEN_CANONICAL_CHILD",
                    "execution_permitted": True,
                    "canonical_child_filename_created": True,
                }
                for field, expected_value in expected.items():
                    if analysis_payload.get(field) != expected_value:
                        reasons.append(f"canonical analysis manifest {field} must equal {expected_value!r}")
                hypotheses = analysis_payload.get("hypotheses")
                if not isinstance(hypotheses, list) or len(hypotheses) != 2:
                    reasons.append("canonical analysis manifest must freeze exactly two hypotheses")
                if _contains_phase0_placeholder(analysis_payload):
                    reasons.append("canonical analysis manifest contains unresolved Phase-0 placeholders")

    return {
        "sleeve": sleeve,
        "status": "READY" if not reasons else "NOT_READY",
        "contract_path": str(contract),
        "contract_sha256": contract_sha,
        "analysis_manifest_path": str(analysis),
        "analysis_manifest_sha256": analysis_sha,
        "validation_reasons": reasons,
        "hash_semantics": (
            "full-file hashes are recorded externally here; canonical files do not embed their own hashes"
        ),
    }


_DOWNSTREAM_READINESS_FIELDS = frozenset(
    {
        "child_contracts_present",
        "analysis_manifests_present",
        "canonical_children_valid",
        "outcome_run_ready",
    }
)


def _phase0_input_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Project a stable S00 identity that cannot depend on downstream S01 files."""

    excluded = {
        "canonical_child_freeze_receipt",
        "canonical_child_freeze_receipt_sha256",
        "generated_at_utc",
        "phase0_input_plan_sha256",
        "plan_sha256",
    }
    result = {key: _jsonable(value) for key, value in plan.items() if key not in excluded}
    result["schema_version"] = 2
    result["receipt_type"] = "strategy_overhaul_phase0_input_plan"
    result["readiness"] = {
        key: _jsonable(value) for key, value in plan["readiness"].items() if key not in _DOWNSTREAM_READINESS_FIELDS
    }
    result["canonical_child_status_semantics"] = (
        "downstream S01 freeze status is deliberately excluded from S00 identity"
    )
    result["phase0_input_plan_sha256"] = _json_hash(result)
    return result


def build_plan(
    args: argparse.Namespace,
    *,
    include_generated_at_utc: bool = True,
    git_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roots = {
        "bybit": Path(args.bybit_root).expanduser(),
        "binance": Path(args.binance_root).expanduser(),
    }
    git = _git_state() if git_state is None else _jsonable(git_state)
    root_plans = {
        venue: _root_plan(venue, root, deep_root_hash=bool(args.deep_root_hash)) for venue, root in roots.items()
    }
    source_receipts = _source_receipts()
    builders_present = all(
        source_receipts[name]["present"]
        for name in (
            "liquidity_migration/continuous_population_scout.py",
            "liquidity_migration/long_population_scout.py",
        )
    )
    both_phase0 = all(row.get("phase0_source_ready") for row in root_plans.values())
    both_tier_a0 = all(row.get("tier_a0_label_ready") for row in root_plans.values())
    both_receipts = all(row.get("registered_receipt_ready") for row in root_plans.values())
    configs = _config_receipts()
    config_parity_wired = bool((configs.get("s02_config_parity_manifest") or {}).get("status") == "WIRED")
    child_validation = {
        sleeve: _canonical_child_status(sleeve, contract, analysis)
        for sleeve, (contract, analysis) in CANONICAL_CHILDREN.items()
    }
    child_contracts_present = all(Path(row["contract_path"]).is_file() for row in child_validation.values())
    analysis_manifests_present = all(Path(row["analysis_manifest_path"]).is_file() for row in child_validation.values())
    canonical_children_valid = all(row["status"] == "READY" for row in child_validation.values())
    child_freeze_receipt = {
        "schema_version": 1,
        "receipt_type": "strategy_overhaul_canonical_child_freeze_status",
        "identity_role": "downstream_status_only_not_an_s00_identity_input",
        "sleeves": child_validation,
        "all_children_valid": canonical_children_valid,
    }
    child_freeze_receipt["artifact_sha256"] = _json_hash(child_freeze_receipt)
    source_snapshot_ready = bool(git.get("snapshot_ready", git.get("clean", False)))
    outcome_run_ready = bool(
        source_snapshot_ready
        and builders_present
        and both_tier_a0
        and both_receipts
        and canonical_children_valid
        and config_parity_wired
    )
    plan = {
        "schema_version": 1,
        "receipt_type": "strategy_overhaul_scout_plan",
        "run_label": RUN_LABEL,
        "preregistration_date": PREREG_DATE,
        "contract": str(CONTRACT),
        "diagnosis": str(DIAGNOSIS),
        "windows": {
            "continuous_start_date": CONTINUOUS_START_DATE,
            "continuous_read_start_date": CONTINUOUS_READ_START_DATE,
            "long_start_date": LONG_START_DATE,
            "long_read_start_date": LONG_READ_START_DATE,
            "signal_end_date_exclusive": SIGNAL_END_DATE,
            "label_end_date_exclusive": LABEL_END_DATE,
        },
        "proposed_minimal_labels": {
            "point_return_horizons_h": PROPOSED_POINT_LABEL_HORIZONS_H,
            "mfe_mae_horizons_h": PROPOSED_EXCURSION_HORIZONS_H,
            "status": "proposal_only_until_child_contracts_freeze",
        },
        "effective_sample_units": {
            "raw": ["venue", "symbol", "decision_ts_ms"],
            "decision": ["venue", "symbol", "signal_ts_ms"],
            "continuous_cluster": "simultaneous signal-hour/event wave",
            "long_cluster": "daily signal close",
            "cross_venue": "matched symbol and decision timestamp",
        },
        "stages": STAGES,
        "deferred_contracts": DEFERRED_CONTRACTS,
        "git": git,
        "sources": source_receipts,
        "configs": configs,
        "canonical_child_freeze_receipt": child_freeze_receipt,
        "canonical_child_freeze_receipt_sha256": child_freeze_receipt["artifact_sha256"],
        "roots": root_plans,
        "readiness": {
            "builders_present": builders_present,
            "both_venues_phase0_source_ready": both_phase0,
            "both_venues_tier_a0_label_ready": both_tier_a0,
            "both_venues_registered_receipts_ready": both_receipts,
            "child_contracts_present": child_contracts_present,
            "analysis_manifests_present": analysis_manifests_present,
            "canonical_children_valid": canonical_children_valid,
            "clean_commit": git["clean"],
            "source_snapshot_ready": source_snapshot_ready,
            "s02_config_parity_wired": config_parity_wired,
            "phase0_ready": both_phase0,
            "outcome_run_ready": outcome_run_ready,
            "result_if_run_now": "phase0_only" if both_phase0 else "incomplete",
        },
        "non_authorizations": [
            "no strategy selection",
            "no profile promotion",
            "no size increase",
            "no forward-clock reset",
            "no deployment",
            "no real-money enablement",
        ],
    }
    if include_generated_at_utc:
        plan["generated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    plan["phase0_input_plan_sha256"] = _phase0_input_plan(plan)["phase0_input_plan_sha256"]
    plan["plan_sha256"] = _json_hash(plan)
    return plan


def _load_instrument_map(
    path: Path | None,
    version_override: str | None,
) -> tuple[list[Any], str | None, dict[str, Any]]:
    if path is None:
        if version_override:
            raise ValueError("--instrument-map-version requires --instrument-map")
        artifact = {
            "schema_version": 1,
            "artifact_type": "strategy_overhaul_instrument_map_input",
            "status": "not_provided",
            "source_kind": "not_provided",
            "trust_class": "not_provided",
            "auto_derived": False,
            "source_path": None,
            "source_file_sha256": None,
            "version": None,
            "map_sha256": None,
            "entries": [],
        }
        artifact["artifact_sha256"] = _json_hash(artifact)
        return [], None, artifact
    resolved_path = path.expanduser().resolve()
    source_bytes = resolved_path.read_bytes()
    payload = json.loads(source_bytes)
    if isinstance(payload, list):
        entries = payload
        version = version_override
    elif isinstance(payload, dict):
        entries = payload.get("entries")
        version = version_override or payload.get("version")
    else:
        raise ValueError("instrument map must be a JSON list or object with entries")
    if not isinstance(entries, list):
        raise ValueError("instrument map entries must be a JSON list")
    if not version or not str(version).strip():
        raise ValueError("a non-blank instrument-map version is required")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("every instrument-map entry must be a JSON object")
    from liquidity_migration.strategy_overhaul_phase0 import _normalise_map_entries

    normalized_entries = _normalise_map_entries(entries)
    normalized_payload = [dataclasses.asdict(entry) for entry in normalized_entries]
    artifact = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_instrument_map_input",
        "status": "diagnostic_untrusted",
        "source_kind": "external_json",
        "trust_class": "external_untrusted",
        "auto_derived": False,
        "source_path": str(resolved_path),
        "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "version": str(version).strip(),
        "map_sha256": _json_hash(normalized_payload),
        "entry_count": len(normalized_payload),
        "entries": normalized_payload,
        "reconstruction_semantics": "normalized entries are the exact ordered input consumed by Phase-0",
        "review_status_trusted": False,
        "trusted_reviewer_bound_receipt_present": False,
        "canonical_readiness_blocker": (
            "external JSON and its review_status are self-asserted; no trusted reviewer-bound product/lifecycle/"
            "multiplier receipt is supplied"
        ),
    }
    artifact["artifact_sha256"] = _json_hash(artifact)
    return normalized_entries, str(version).strip(), artifact


def _derive_auto_instrument_map(
    roots: dict[str, Path],
    *,
    start_date: str,
    end_date_exclusive: str,
    batch_size: int,
) -> tuple[list[Any], str | None, dict[str, Any]]:
    from liquidity_migration.strategy_overhaul_instrument_map import (
        VENUE_LOCAL_REVIEW_STATUS,
        derive_venue_local_instrument_map_from_roots,
    )

    derived = derive_venue_local_instrument_map_from_roots(
        roots,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        batch_size=batch_size,
    )
    normalized_payload = [dataclasses.asdict(entry) for entry in derived.entries]
    source_projection = dict(derived.receipt["source_projection"])
    complete = bool(
        source_projection.get("registered_window_complete")
        and source_projection.get("source_projection_row_count")
        and set(source_projection.get("venues") or {}) == {"bybit", "binance"}
    )
    artifact = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_instrument_map_input",
        "status": "ready" if complete else "partial",
        "source_kind": "auto_derived_archive_trade_manifest_symbol_date_projection",
        "trust_class": "mechanically_derived_venue_local",
        "auto_derived": True,
        "source_path": None,
        "source_file_sha256": None,
        "source_projection": source_projection,
        "source_projection_sha256": source_projection["source_projection_sha256"],
        "source_projection_identity_sha256": source_projection["source_projection_identity_sha256"],
        "source_projection_row_count": source_projection["source_projection_row_count"],
        "source_window": source_projection["window"],
        "source_registered_window_complete": source_projection["registered_window_complete"],
        "version": derived.version,
        "map_sha256": _json_hash(normalized_payload),
        "entry_count": len(normalized_payload),
        "phase0_consumed_entry_count": len(normalized_payload) if complete else 0,
        "entries": normalized_payload,
        "review_status": VENUE_LOCAL_REVIEW_STATUS,
        "cross_venue_portability_ready": False,
        "review_status_trusted_for_cross_venue_portability": False,
        "trusted_reviewer_bound_receipt_present": False,
        "reconstruction_semantics": (
            "normalized entries and the exact manifest symbol/date source projection are bundled; "
            "canonical IDs remain venue-qualified"
        ),
    }
    if artifact["map_sha256"] != derived.receipt["map_sha256"]:
        raise RuntimeError("auto-derived instrument-map hash disagrees with normalized entries")
    artifact["artifact_sha256"] = _json_hash(artifact)
    return (
        list(derived.entries) if complete else [],
        derived.version if complete else None,
        artifact,
    )


def _resolve_phase0_instrument_map(
    *,
    path: Path | None,
    version_override: str | None,
    roots: dict[str, Path],
    start_date: str,
    end_date_exclusive: str,
    batch_size: int,
) -> tuple[list[Any], str | None, dict[str, Any]]:
    if path is not None or version_override is not None:
        return _load_instrument_map(path, version_override)
    return _derive_auto_instrument_map(
        roots,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        batch_size=batch_size,
    )


def _assert_instrument_map_unchanged(artifact: dict[str, Any]) -> None:
    if artifact.get("source_kind") != "external_json":
        return
    path = Path(str(artifact["source_path"]))
    if _sha256_file(path) != artifact.get("source_file_sha256"):
        raise RuntimeError("instrument-map source changed during Phase-0 scan")


def _assert_phase0_instrument_map_unchanged(
    expected: dict[str, Any],
    *,
    path: Path | None,
    version_override: str | None,
    roots: dict[str, Path],
    start_date: str,
    end_date_exclusive: str,
    batch_size: int,
) -> None:
    _entries, _version, observed = _resolve_phase0_instrument_map(
        path=path,
        version_override=version_override,
        roots=roots,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        batch_size=batch_size,
    )
    if observed != expected:
        raise RuntimeError("instrument-map source/projection changed during Phase-0 scan")


def _assert_auto_map_matches_phase0_inventory(
    artifact: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    if artifact.get("source_kind") != "auto_derived_archive_trade_manifest_symbol_date_projection":
        return
    source_venues = (artifact.get("source_projection") or {}).get("venues") or {}
    for venue in ("bybit", "binance"):
        source = source_venues.get(venue) or {}
        observed = ((inventory.get("field_availability") or {}).get(venue) or {}).get("archive_trade_manifest") or {}
        if source.get("storage_row_count_in_window") != observed.get("row_count") or source.get(
            "storage_key_provenance_projection_sha256"
        ) != observed.get("key_provenance_projection_sha256"):
            raise RuntimeError(f"auto-derived instrument-map source disagrees with Phase-0 manifest scan for {venue}")


def _load_registered_child_designs(
    expected_source_receipts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sleeves: dict[str, Any] = {}
    for sleeve, (contract_path, analysis_path) in CANONICAL_CHILDREN.items():
        template_contract = contract_path.with_name(contract_path.name.replace(".md", ".template.md"))
        template_analysis = analysis_path.with_name(
            analysis_path.name.replace(".analysis.json", ".analysis.template.json")
        )
        contract_bytes = template_contract.read_bytes()
        analysis_bytes = template_analysis.read_bytes()
        contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
        analysis_sha256 = hashlib.sha256(analysis_bytes).hexdigest()
        contract_text = contract_bytes.decode("utf-8")
        payload = json.loads(analysis_bytes)
        if not isinstance(payload, dict):
            raise RuntimeError(f"child analysis template must be a JSON object: {template_analysis}")
        if payload.get("sleeve") != sleeve.upper():
            raise RuntimeError(f"child analysis template sleeve mismatch: {template_analysis}")
        if payload.get("template_status") != "NON_EXECUTABLE_TEMPLATE":
            raise RuntimeError(f"child analysis template status changed unexpectedly: {template_analysis}")
        if payload.get("execution_permitted") is not False:
            raise RuntimeError(f"child analysis template must remain non-executable: {template_analysis}")
        hypotheses = payload.get("hypotheses")
        if not isinstance(hypotheses, list) or len(hypotheses) != 2:
            raise RuntimeError(f"child analysis template must contain exactly two hypotheses: {template_analysis}")
        required = payload.get("required_phase0_substitutions")
        if not isinstance(required, dict):
            raise RuntimeError(f"child analysis template lacks required substitutions: {template_analysis}")
        if set(required) != REQUIRED_S01_FIELDS[sleeve]:
            raise RuntimeError(f"child analysis template required-key set changed: {template_analysis}")
        if set(required.values()) != {"REQUIRED_PHASE0_SUBSTITUTION"}:
            raise RuntimeError(f"non-executable child template has pre-filled substitutions: {template_analysis}")
        marker = re.search(
            r"<!-- REQUIRED_PHASE0_KEYS_JSON: (\[.*?\]) -->",
            contract_text,
        )
        if marker is None:
            raise RuntimeError(f"child contract template lacks machine-readable substitution keys: {template_contract}")
        markdown_keys = json.loads(marker.group(1))
        if (
            not isinstance(markdown_keys, list)
            or len(markdown_keys) != len(set(markdown_keys))
            or set(markdown_keys) != set(required)
        ):
            raise RuntimeError(f"Markdown/JSON required substitution keys disagree: {template_contract}")
        if expected_source_receipts is not None:
            for path, observed_sha in (
                (template_contract, contract_sha256),
                (template_analysis, analysis_sha256),
            ):
                relative = str(path.relative_to(REPO))
                expected_sha = (expected_source_receipts.get(relative) or {}).get("sha256")
                if expected_sha != observed_sha:
                    raise RuntimeError(f"child design snapshot changed after source inventory: {relative}")
        sleeves[sleeve] = {
            "contract_template_path": str(template_contract),
            "contract_template_sha256": contract_sha256,
            "contract_template": contract_text,
            "analysis_template_path": str(template_analysis),
            "analysis_template_sha256": analysis_sha256,
            "analysis_template": payload,
        }
    result = {
        "schema_version": 1,
        "artifact_type": "registered_strategy_overhaul_child_designs",
        "outcome_values_read": False,
        "sleeves": sleeves,
    }
    result["artifact_sha256"] = _json_hash(result)
    return result


def _support_design_and_counts(
    inventory: dict[str, Any],
    designs: dict[str, Any],
) -> dict[str, Any]:
    venue_counts: dict[str, Any] = {}
    for venue in sorted(inventory["field_availability"]):
        fields = inventory["field_availability"][venue]
        pit = (inventory["pit_provenance"].get("venues") or {}).get(venue, {})
        manifest_coverage = (inventory["manifest_kline_coverage"].get("venues") or {}).get(venue, {})
        rmom = (inventory["rmom_population_coverage"].get("venues") or {}).get(venue, {})
        map_row = (inventory["instrument_map_coverage"].get("venues") or {}).get(venue, {})
        venue_counts[venue] = {
            "kline_row_count": (fields.get("klines_1h") or {}).get("row_count", 0),
            "kline_symbol_day_count": ((fields.get("klines_1h") or {}).get("grid_integrity") or {}).get(
                "symbol_day_count", 0
            ),
            "manifest_storage_row_count": pit.get("storage_row_count", 0),
            "manifest_membership_pair_count": pit.get("membership_pair_count", 0),
            "manifest_membership_coverage_fraction": manifest_coverage.get("membership_coverage_fraction", 0.0),
            "rmom_identity_coverage": rmom,
            "instrument_map_coverage": map_row,
        }
    sleeve_design = {
        sleeve: {
            "hypotheses": payload["analysis_template"]["hypotheses"],
            "support_thresholds": payload["analysis_template"]["support"],
            "dependence": payload["analysis_template"]["dependence"],
            "controls": payload["analysis_template"]["controls"],
            "folds": payload["analysis_template"]["folds"],
            "reconstruction_stages": payload["analysis_template"]["stages"],
            "feature_manifest": payload["analysis_template"]["feature_manifest"],
        }
        for sleeve, payload in designs["sleeves"].items()
    }
    sleeve_signal_window_counts: dict[str, Any] = {}
    for window in (inventory.get("window") or {}).get("sleeve_windows", []):
        sleeve = str(window["sleeve"])
        signal_start = str(window["signal_start_date"])
        signal_end = str(window["signal_end_date_exclusive"])
        if sleeve == "long":
            identity_start = (dt.date.fromisoformat(signal_start) - dt.timedelta(days=1)).isoformat()
            identity_end = (dt.date.fromisoformat(signal_end) - dt.timedelta(days=1)).isoformat()
            date_alignment = "membership_date = UTC date(signal_ts_ms - 1ms)"
        else:
            identity_start = signal_start
            identity_end = signal_end
            date_alignment = "manifest date uses the registered hourly signal date"
        sleeve_venues: dict[str, Any] = {}
        for venue in sorted(inventory["field_availability"]):
            manifest_daily = (
                (inventory["manifest_kline_coverage"].get("venues") or {}).get(venue, {}).get("daily_counts", [])
            )
            rmom_daily = (
                (inventory["rmom_population_coverage"].get("venues") or {}).get(venue, {}).get("daily_counts", [])
            )
            selected_manifest = [row for row in manifest_daily if identity_start <= str(row["date"]) < identity_end]
            selected_rmom = [row for row in rmom_daily if identity_start <= str(row["date"]) < identity_end]
            sleeve_venues[venue] = {
                "manifest_covered_symbol_day_count": sum(
                    int(row["covered_membership_symbol_day_count"]) for row in selected_manifest
                ),
                "manifest_covered_hourly_row_count": sum(
                    int(row["covered_kline_row_count"]) for row in selected_manifest
                ),
                "declared_non_provisional_rmom_symbol_day_count": sum(
                    int(row["declared_non_provisional_only_symbol_day_count"]) for row in selected_rmom
                ),
                "provisional_rmom_symbol_day_count": sum(
                    int(row["declared_provisional_only_symbol_day_count"]) for row in selected_rmom
                ),
                "unknown_or_mixed_rmom_symbol_day_count": sum(
                    int(row["provisional_status_unknown_only_symbol_day_count"])
                    + int(row["mixed_provisional_status_symbol_day_count"])
                    for row in selected_rmom
                ),
                "missing_rmom_identity_symbol_day_count": sum(
                    int(row["missing_rmom_identity_symbol_day_count"]) for row in selected_rmom
                ),
                "date_count_with_any_manifest_or_kline_identity": len(selected_manifest),
            }
        matched_daily = (
            inventory["instrument_map_coverage"].get("cross_venue", {}).get("daily_all_venue_matched_counts", [])
        )
        sleeve_signal_window_counts[sleeve] = {
            "signal_start_date": signal_start,
            "signal_end_date_exclusive": signal_end,
            "identity_membership_start_date": identity_start,
            "identity_membership_end_date_exclusive": identity_end,
            "date_alignment": date_alignment,
            "venues": sleeve_venues,
            "all_venue_matched_canonical_instrument_day_count": sum(
                int(row["matched_canonical_instrument_count"])
                for row in matched_daily
                if identity_start <= str(row["date"]) < identity_end
            ),
            "count_scope": (
                "exact signal window; manifest-gated identity rows before ranks, gates, classifiers, or outcomes"
            ),
        }
    expected_sleeves = set(designs["sleeves"])
    expected_venues = {"bybit", "binance"}
    support_failures: list[str] = []
    if set(sleeve_signal_window_counts) != expected_sleeves:
        support_failures.append("registered sleeve windows are incomplete")
    if set(inventory["field_availability"]) != expected_venues:
        support_failures.append("both registered venues are not present")
    if (inventory.get("readiness") or {}).get("status") != "READY":
        support_failures.append("Phase-0 inventory readiness is not READY")
    result = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_phase0_support_design_and_counts",
        "count_scope": "outcome-blind identity, provenance, and availability counts only",
        "venue_counts": venue_counts,
        "sleeve_signal_window_counts": sleeve_signal_window_counts,
        "sleeve_signal_window_counts_status": (
            "WINDOW_SCOPE_DECLARED" if set(sleeve_signal_window_counts) == expected_sleeves else "INCOMPLETE_SCOPE"
        ),
        "s01_support_substitution_ready": not support_failures,
        "s01_support_substitution_failures": support_failures,
        "cross_venue_counts": inventory["instrument_map_coverage"].get("cross_venue", {}),
        "sleeve_design": sleeve_design,
        "focal_arm_counts": {
            "status": "DEFERRED_TO_S02",
            "reason": (
                "gate, rank, classifier, and entry-policy arms cannot be counted without "
                "constructing the preregistered feature stages"
            ),
            "not_required_for_phase0_identity_support_substitution": True,
        },
        "outcome_values_read": False,
    }
    result["artifact_sha256"] = _json_hash(result)
    return result


def _s01_template_input_status(
    *,
    phase0_id: str,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    environment: dict[str, Any],
    designs: dict[str, Any],
    support_artifact: dict[str, Any],
    instrument_map_artifact: dict[str, Any] | None = None,
    config_artifact_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    map_report = inventory["instrument_map_coverage"]
    expected_venues = {"bybit", "binance"}
    map_venues = map_report.get("venues") or {}
    map_artifact = instrument_map_artifact or {}
    venue_local_map_ready = bool(
        map_report.get("venue_local_identity_ready")
        and map_report.get("map_version")
        and map_report.get("map_sha256")
        and set(map_venues) == expected_venues
        and map_artifact.get("status") == "ready"
        and map_artifact.get("version") == map_report.get("map_version")
        and map_artifact.get("map_sha256") == map_report.get("map_sha256")
        and bool(map_artifact.get("entries"))
    )
    git = plan["git"]
    environment_ready = _environment_manifest_ready(environment)
    environment_file_sha256 = hashlib.sha256(_render_json(environment)).hexdigest()
    supplied_config_index = config_artifact_index or {}
    try:
        _config_payloads, expected_config_index = _config_bundle_artifacts(plan["configs"])
    except (KeyError, RuntimeError, TypeError, ValueError):
        expected_config_index = {}
    config_artifacts_ready = bool(
        supplied_config_index
        and supplied_config_index == expected_config_index
        and supplied_config_index.get("artifact_sha256")
        == _json_hash({key: value for key, value in supplied_config_index.items() if key != "artifact_sha256"})
    )
    parity_index = supplied_config_index.get("s02_config_parity_manifest") or {}
    config_parity_status = parity_index.get("status")
    sleeves: dict[str, Any] = {}
    pit_key_hashes = {
        venue: (inventory["field_availability"][venue].get("archive_trade_manifest", {})).get(
            "key_provenance_projection_sha256"
        )
        for venue in inventory["field_availability"]
    }
    pit_receipts_ready = (
        set(inventory["field_availability"]) == expected_venues
        and (inventory.get("readiness") or {}).get("status") == "READY"
        and all(
            bool(inventory["field_availability"][venue].get("archive_trade_manifest", {}).get("ready"))
            and bool(pit_key_hashes.get(venue))
            for venue in expected_venues
        )
    )
    for sleeve, design in designs["sleeves"].items():
        requirements = design["analysis_template"]["required_phase0_substitutions"]
        resolved: dict[str, Any] = {
            "canonical_contract_path": str(CANONICAL_CHILDREN[sleeve][0]),
            "canonical_analysis_manifest_path": str(CANONICAL_CHILDREN[sleeve][1]),
            "repository_commit": git["commit"],
            "resource_plan": inventory["resource_estimate"],
            "partition_checkpoint_plan": inventory["resource_estimate"]["partition_checkpoint_plan"],
        }
        sleeve_config_index = (supplied_config_index.get("sleeves") or {}).get(sleeve) or {}
        if config_artifacts_ready:
            canonical_config = sleeve_config_index.get("canonical_config") or {}
            config_identity = sleeve_config_index.get("config_identity") or {}
            registered_scope = sleeve_config_index.get("registered_scope") or {}
            resolved.update(
                {
                    "canonical_config_json_path": canonical_config.get("path"),
                    "canonical_config_sha256": canonical_config.get("file_sha256"),
                    "config_identity_json_path": config_identity.get("path"),
                    "config_identity_sha256": config_identity.get("file_sha256"),
                    "registered_scope_json_path": registered_scope.get("path"),
                    "registered_scope_sha256": registered_scope.get("file_sha256"),
                    "s02_config_parity_manifest_path": parity_index.get("path"),
                    "s02_config_parity_manifest_sha256": parity_index.get("file_sha256"),
                }
            )
            if sleeve == "continuous":
                component_config = sleeve_config_index.get("component_config") or {}
                resolved["component_config_json_path"] = component_config.get("path")
                resolved["component_config_sha256"] = component_config.get("file_sha256")
        diagnostic_values: dict[str, Any] = {}
        pit_receipt_payload = {
            "artifact": "pit_provenance.json",
            "key_provenance_hashes": pit_key_hashes,
        }
        if pit_receipts_ready:
            resolved["pit_manifest_receipts"] = pit_receipt_payload
        else:
            diagnostic_values["pit_manifest_receipts"] = pit_receipt_payload
        support_payload = {
            "artifact": "support_design_and_counts.json",
            "sha256": support_artifact["artifact_sha256"],
            "scope": "exact registered signal windows, manifest-gated before features/outcomes",
        }
        if support_artifact.get("s01_support_substitution_ready"):
            resolved["phase0_support_counts"] = {
                **support_payload,
            }
        else:
            diagnostic_values["phase0_support_counts"] = support_payload
        if git.get("snapshot_ready") and git.get("tracked_diff_sha256") and git.get("untracked_source_bundle_sha256"):
            resolved["worktree_policy"] = git.get("worktree_state")
            resolved["patch_bundle_sha256"] = git["tracked_diff_sha256"]
            resolved["untracked_source_bundle_sha256"] = git["untracked_source_bundle_sha256"]
        if environment_ready:
            resolved["environment_lock_path"] = ENVIRONMENT_MANIFEST_ARTIFACT
            resolved["environment_lock_sha256"] = environment_file_sha256
        if venue_local_map_ready:
            resolved["instrument_map_version"] = map_report.get("map_version")
            resolved["instrument_map_sha256"] = map_report.get("map_sha256")
            resolved["instrument_map_row_coverage"] = {
                venue: row.get("row_coverage_fraction") for venue, row in (map_report.get("venues") or {}).items()
            }
            resolved["instrument_map_symbol_coverage"] = {
                venue: (
                    row.get("mapped_symbol_count", 0) / row.get("membership_symbol_count", 1)
                    if row.get("membership_symbol_count", 0)
                    else 0.0
                )
                for venue, row in (map_report.get("venues") or {}).items()
            }

        blockers: dict[str, str] = {}
        for field in requirements:
            if field in resolved:
                continue
            if field == "run_id":
                blockers[field] = "child run ID is derived only after every S01 input freezes"
            elif field in {"bybit_root_receipt", "binance_root_receipt"}:
                blockers[field] = (
                    "Phase 0 binds identity/provenance only; exact numeric root/content receipt is required"
                )
            elif field.startswith(
                ("signal_feature_schema", "entry_anchor_schema", "entry_policy_schema", "path_label_schema")
            ):
                blockers[field] = "complete stage schema artifact must be frozen in S01"
            elif field.startswith(
                (
                    "canonical_config",
                    "component_config",
                    "config_identity",
                    "registered_scope",
                    "s02_config_parity_manifest",
                )
            ):
                blockers[field] = "canonical config identity artifact is not reconstructably bundled and file-hashed"
            elif field == "source_function_hashes":
                blockers[field] = (
                    "Phase 0 records whole-file hashes only; exact function-level hashes are not yet materialized"
                )
            elif field.startswith("instrument_map"):
                blockers[field] = "no complete collision-free venue-local versioned instrument map was supplied"
            elif field.startswith(("worktree_policy", "patch_bundle", "untracked_source_bundle")):
                blockers[field] = "source snapshot is not verified as complete and reconstructable"
            elif field.startswith("environment_lock"):
                blockers[field] = "exact installed-distribution/platform environment manifest is unavailable"
            elif field in {"output_root", "exposure_ledger_path"}:
                blockers[field] = "child output and immutable exposure ledger are assigned at freeze"
            elif field == "label_tail_root_receipt":
                blockers[field] = "label-tail boundary 2026-07-14 exclusive is not available in Phase 0"
            elif field == "phase0_support_counts":
                blockers[field] = (
                    "registered signal-window counts are diagnostic but Phase-0 input readiness is incomplete"
                )
            elif field == "pit_manifest_receipts":
                blockers[field] = "both venues need READY manifest audits and non-empty key/provenance hashes"
            else:
                blockers[field] = "no deterministic Phase-0 substitution rule is registered"
        if set(resolved) & set(blockers):
            raise RuntimeError(f"S01 input status overlaps resolved and blocked fields for {sleeve}")
        if (set(resolved) | set(blockers)) != set(requirements):
            extra = set(resolved) - set(requirements)
            if extra:
                # Resolved helper fields are permitted only when the template
                # splits one conceptual requirement into path/hash keys.
                resolved = {key: value for key, value in resolved.items() if key in requirements}
            if (set(resolved) | set(blockers)) != set(requirements):
                raise RuntimeError(f"S01 input status did not cover every requirement for {sleeve}")
        implementation_blockers = (
            {
                "s02_config_parity": (
                    f"canonical parity manifest status is {config_parity_status!r}; stage consumers remain unwired"
                )
            }
            if config_parity_status != "WIRED"
            else {}
        )
        sleeves[sleeve] = {
            "status": "READY" if not blockers and not implementation_blockers else "NOT_READY",
            "required_field_count": len(requirements),
            "resolved": resolved,
            "diagnostic_values_not_resolved": diagnostic_values,
            "blockers": blockers,
            "implementation_blockers": implementation_blockers,
        }
    result = {
        "schema_version": 1,
        "artifact_type": "strategy_overhaul_s01_template_input_status",
        "phase0_id": phase0_id,
        "phase0_input_plan_sha256": plan["phase0_input_plan_sha256"],
        "phase0_inventory_sha256": inventory["artifact_sha256"],
        "source_snapshot_sha256": git.get("source_snapshot_sha256"),
        "environment_manifest_file_sha256": environment_file_sha256,
        "config_artifact_index_path": CONFIG_ARTIFACT_INDEX,
        "config_artifact_index_file_sha256": hashlib.sha256(_render_json(supplied_config_index)).hexdigest(),
        "config_artifact_index_payload_sha256": supplied_config_index.get("artifact_sha256"),
        "config_artifacts_file_hash_verified": config_artifacts_ready,
        "s02_config_parity_status": config_parity_status,
        "a0_config_identity_mismatch_retired": False,
        "instrument_map_identity_status": {
            "venue_local_substitution_ready": venue_local_map_ready,
            "source_kind": map_artifact.get("source_kind"),
            "auto_derived": bool(map_artifact.get("auto_derived")),
            "portable_matching_ready": bool(map_report.get("portable_matching_ready")),
            "portability_is_separate_from_venue_local_hypothesis_readiness": True,
            "source_projection_sha256": map_artifact.get("source_projection_sha256"),
            "source_projection_identity_sha256": map_artifact.get("source_projection_identity_sha256"),
            "source_projection_row_count": map_artifact.get("source_projection_row_count"),
            "source_registered_window_complete": map_artifact.get("source_registered_window_complete"),
            "reconstructable_artifact_path": (
                INSTRUMENT_MAP_ARTIFACT if map_artifact.get("status") == "ready" else None
            ),
            "reconstructable_artifact_sha256": map_artifact.get("artifact_sha256"),
        },
        "support_design_and_counts_sha256": support_artifact["artifact_sha256"],
        "sleeves": sleeves,
        "all_sleeves_ready": all(row["status"] == "READY" for row in sleeves.values()),
        "outcome_run_authorized": False,
    }
    result["artifact_sha256"] = _json_hash(result)
    return result


def _render_json(value: Any) -> bytes:
    return (json.dumps(_jsonable(value), indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def _verify_existing_bundle(
    run_dir: Path,
    *,
    phase0_id: str,
    expected_rendered: dict[str, bytes],
) -> None:
    receipt_path = run_dir / "receipt.json"
    if not receipt_path.is_file():
        raise RuntimeError(f"existing Phase-0 directory lacks receipt.json: {run_dir}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("phase0_id") != phase0_id:
        raise RuntimeError(f"existing Phase-0 receipt identity mismatch: {run_dir}")
    rows = receipt.get("files", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"existing Phase-0 receipt has invalid files list: {run_dir}")
    if len(rows) != len(expected_rendered):
        raise RuntimeError(f"existing Phase-0 receipt file count mismatch: {run_dir}")
    receipt_hashes = {str(row["path"]): row.get("sha256") for row in rows}
    expected_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in expected_rendered.items()}
    if receipt_hashes != expected_hashes:
        raise RuntimeError(f"existing Phase-0 bundle does not match the requested deterministic payload: {run_dir}")
    actual_files = {path.name for path in run_dir.iterdir() if path.is_file()}
    expected_files = set(expected_rendered) | {"receipt.json"}
    if actual_files != expected_files:
        raise RuntimeError(f"existing Phase-0 bundle file inventory mismatch: {run_dir}")
    for row in rows:
        path = run_dir / str(row["path"])
        if _sha256_file(path) != row.get("sha256"):
            raise RuntimeError(f"existing Phase-0 artifact hash mismatch: {path}")


def _write_phase0_bundle(
    output_root: Path,
    *,
    phase0_id: str,
    identity: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    extra_payloads: dict[str, Any] | None = None,
    extra_binary_payloads: dict[str, bytes] | None = None,
) -> tuple[Path, bool]:
    output_root = output_root.expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / phase0_id
    payloads: dict[str, Any] = {
        "command_plan.json": plan,
        "identity.json": identity,
        "phase0_inventory.json": inventory,
        "field_availability.json": inventory["field_availability"],
        "pit_provenance.json": inventory["pit_provenance"],
        "manifest_kline_coverage.json": inventory["manifest_kline_coverage"],
        "rmom_population_coverage.json": inventory["rmom_population_coverage"],
        "root_lineage.json": inventory["root_lineage"],
        "resource_estimate.json": inventory["resource_estimate"],
        "proposed_schemas.json": inventory["proposed_schemas"],
        "child_schema_registry.json": inventory["child_schema_registry"],
        "instrument_map_coverage.json": inventory["instrument_map_coverage"],
        "outcome_blind_audit.json": inventory["outcome_blind_audit"],
    }
    if extra_payloads:
        duplicate_names = set(payloads) & set(extra_payloads)
        if duplicate_names:
            raise RuntimeError(f"duplicate Phase-0 bundle payload names: {sorted(duplicate_names)}")
        payloads.update(extra_payloads)
    rendered = {name: _render_json(payload) for name, payload in payloads.items()}
    if extra_binary_payloads:
        duplicate_names = set(rendered) & set(extra_binary_payloads)
        if duplicate_names:
            raise RuntimeError(f"duplicate Phase-0 binary payload names: {sorted(duplicate_names)}")
        if not all(isinstance(data, bytes) for data in extra_binary_payloads.values()):
            raise TypeError("every Phase-0 binary payload must be bytes")
        rendered.update(extra_binary_payloads)
    if run_dir.exists():
        _verify_existing_bundle(
            run_dir,
            phase0_id=phase0_id,
            expected_rendered=rendered,
        )
        return run_dir, True

    receipt = {
        "schema_version": 1,
        "receipt_type": "strategy_overhaul_phase0_bundle",
        "phase0_id": phase0_id,
        "identity_sha256": _json_hash(identity),
        "inventory_sha256": inventory["artifact_sha256"],
        "readiness_status": inventory["readiness"]["status"],
        "outcome_run_authorized": False,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for name, data in sorted(rendered.items())
        ],
    }
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{phase0_id}.", dir=output_root))
    try:
        for name, data in rendered.items():
            (temp_dir / name).write_bytes(data)
        (temp_dir / "receipt.json").write_bytes(_render_json(receipt))
        os.replace(temp_dir, run_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return run_dir, False


def run_phase0_inventory(args: argparse.Namespace) -> int:
    if args.deep_root_hash:
        raise ValueError(
            "--deep-root-hash is not allowed in Phase 0 because byte hashes depend on outcome-bearing numeric values"
        )
    if args.write_plan is not None:
        raise ValueError("--write-plan is valid only with --plan")
    output_root = args.output_root.expanduser().resolve()
    if output_root.is_relative_to(REPO.resolve()):
        raise ValueError(
            "--output-root must be outside the repository so immutable Phase-0 bundles cannot enter "
            "their own source snapshot"
        )
    from liquidity_migration.strategy_overhaul_phase0 import canonicalize_phase0_roots

    phase0_roots = canonicalize_phase0_roots(
        {"bybit": args.bybit_root, "binance": args.binance_root},
        require_registered_venues=True,
    )
    canonical_arg_values = vars(args).copy()
    canonical_arg_values.update(
        bybit_root=phase0_roots["bybit"],
        binance_root=phase0_roots["binance"],
    )
    canonical_args = argparse.Namespace(**canonical_arg_values)
    source_snapshot = _capture_source_snapshot()
    if not source_snapshot.git["snapshot_ready"]:
        raise RuntimeError("Phase-0 source snapshot contains unsupported untracked paths and is not reconstructable")
    environment = _environment_receipt()
    environment_file_sha256 = hashlib.sha256(_render_json(environment)).hexdigest()
    plan = build_plan(
        canonical_args,
        include_generated_at_utc=False,
        git_state=source_snapshot.git,
    )
    phase0_input_plan = _phase0_input_plan(plan)
    if phase0_input_plan["phase0_input_plan_sha256"] != plan["phase0_input_plan_sha256"]:
        raise RuntimeError("Phase-0 input-plan projection hash is internally inconsistent")
    config_payloads, config_artifact_index = _config_bundle_artifacts(plan["configs"])
    config_artifact_file_sha256 = {
        path: hashlib.sha256(_render_json(payload)).hexdigest() for path, payload in sorted(config_payloads.items())
    }
    script_receipt = plan["sources"].get("scripts/strategy_overhaul_scout_2026_07_10.py") or {}
    if script_receipt.get("sha256") != BOOTSTRAP_SCRIPT_SHA256:
        raise RuntimeError("loaded Phase-0 bootstrap script differs from its captured source bytes")
    registered_designs = _load_registered_child_designs(plan["sources"])
    required_sources = (
        "liquidity_migration/strategy_overhaul_phase0.py",
        "liquidity_migration/strategy_overhaul_phase0_verifier.py",
        "liquidity_migration/strategy_overhaul_instrument_map.py",
        "liquidity_migration/strategy_overhaul_population_keys.py",
        "liquidity_migration/strategy_overhaul_schemas.py",
        "liquidity_migration/strategy_overhaul_config_identity.py",
        "scripts/strategy_overhaul_scout_2026_07_10.py",
        "docs/preregistration/strategy-overhaul-continuous-a0.template.md",
        "docs/preregistration/strategy-overhaul-continuous-a0.analysis.template.json",
        "docs/preregistration/strategy-overhaul-long-a0.template.md",
        "docs/preregistration/strategy-overhaul-long-a0.analysis.template.json",
    )
    missing_sources = [name for name in required_sources if not (plan["sources"].get(name) or {}).get("present")]
    if missing_sources:
        raise RuntimeError(f"Phase-0 source/template prerequisites missing: {missing_sources}")

    map_entries, map_version, instrument_map_artifact = _resolve_phase0_instrument_map(
        path=canonical_args.instrument_map,
        version_override=canonical_args.instrument_map_version,
        roots=phase0_roots,
        start_date=ROOT_START_DATE,
        end_date_exclusive=SIGNAL_END_DATE,
        batch_size=canonical_args.batch_size,
    )
    from liquidity_migration.strategy_overhaul_phase0 import (
        REGISTERED_SLEEVE_WINDOWS,
        build_phase0_artifacts,
    )

    inventory = build_phase0_artifacts(
        phase0_roots,
        start_date=ROOT_START_DATE,
        end_date_exclusive=SIGNAL_END_DATE,
        instrument_map=map_entries,
        instrument_map_version=map_version,
        instrument_map_authority=str(instrument_map_artifact["trust_class"]),
        sleeve_windows=REGISTERED_SLEEVE_WINDOWS,
        batch_size=canonical_args.batch_size,
    )
    _assert_auto_map_matches_phase0_inventory(instrument_map_artifact, inventory)
    map_coverage = inventory["instrument_map_coverage"]
    if instrument_map_artifact.get("status") == "ready" and (
        instrument_map_artifact.get("version") != map_coverage.get("map_version")
        or instrument_map_artifact.get("map_sha256") != map_coverage.get("map_sha256")
    ):
        raise RuntimeError("bundled instrument-map entries disagree with Phase-0 coverage identity")
    support_artifact = _support_design_and_counts(inventory, registered_designs)
    identity = {
        "schema_version": 2,
        "contract": plan["sources"][str(CONTRACT.relative_to(REPO))],
        "diagnosis": plan["sources"][str(DIAGNOSIS.relative_to(REPO))],
        "git": plan["git"],
        "source_snapshot_sha256": source_snapshot.manifest["artifact_sha256"],
        "environment_manifest_path": ENVIRONMENT_MANIFEST_ARTIFACT,
        "environment_manifest_file_sha256": environment_file_sha256,
        "instrument_map_artifact_path": INSTRUMENT_MAP_ARTIFACT,
        "instrument_map_artifact_sha256": instrument_map_artifact["artifact_sha256"],
        "config_artifact_index_path": CONFIG_ARTIFACT_INDEX,
        "config_artifact_index_file_sha256": config_artifact_file_sha256[CONFIG_ARTIFACT_INDEX],
        "config_artifact_index_payload_sha256": config_artifact_index["artifact_sha256"],
        "config_artifact_file_sha256": config_artifact_file_sha256,
        "sources": plan["sources"],
        "configs": plan["configs"],
        "windows": plan["windows"],
        "phase0_input_plan_sha256": phase0_input_plan["phase0_input_plan_sha256"],
        "registered_child_designs_sha256": registered_designs["artifact_sha256"],
        "support_design_and_counts_sha256": support_artifact["artifact_sha256"],
        "s01_status_derivation_version": S01_STATUS_DERIVATION_VERSION,
        "phase0_inventory_sha256": inventory["artifact_sha256"],
    }
    phase0_id = f"strategy-overhaul-phase0-{_json_hash(identity)[:20]}"
    s01_status = _s01_template_input_status(
        phase0_id=phase0_id,
        plan=plan,
        inventory=inventory,
        environment=environment,
        designs=registered_designs,
        support_artifact=support_artifact,
        instrument_map_artifact=instrument_map_artifact,
        config_artifact_index=config_artifact_index,
    )
    if _environment_receipt() != environment:
        raise RuntimeError("Python/platform environment changed during Phase-0 scan")
    if _config_receipts() != plan["configs"]:
        raise RuntimeError("canonical A0 config identities changed during Phase-0 scan")
    _assert_source_receipts_unchanged(plan["sources"])
    observed_source_snapshot = _capture_source_snapshot()
    _assert_source_snapshot_unchanged(source_snapshot, observed_source_snapshot)
    _assert_phase0_instrument_map_unchanged(
        instrument_map_artifact,
        path=canonical_args.instrument_map,
        version_override=canonical_args.instrument_map_version,
        roots=phase0_roots,
        start_date=ROOT_START_DATE,
        end_date_exclusive=SIGNAL_END_DATE,
        batch_size=canonical_args.batch_size,
    )
    run_dir, reused = _write_phase0_bundle(
        output_root,
        phase0_id=phase0_id,
        identity=identity,
        plan=phase0_input_plan,
        inventory=inventory,
        extra_payloads={
            "registered_child_designs.json": registered_designs,
            "support_design_and_counts.json": support_artifact,
            "s01_template_input_status.json": s01_status,
            ENVIRONMENT_MANIFEST_ARTIFACT: environment,
            SOURCE_SNAPSHOT_ARTIFACT: source_snapshot.manifest,
            UNTRACKED_MANIFEST_ARTIFACT: source_snapshot.untracked_manifest,
            INSTRUMENT_MAP_ARTIFACT: instrument_map_artifact,
            **config_payloads,
        },
        extra_binary_payloads={
            TRACKED_PATCH_ARTIFACT: source_snapshot.tracked_patch,
            UNTRACKED_ARCHIVE_ARTIFACT: source_snapshot.untracked_archive,
        },
    )
    summary = {
        "phase0_id": phase0_id,
        "run_dir": str(run_dir),
        "reused": reused,
        "readiness_status": inventory["readiness"]["status"],
        "portable_cross_venue_matching_ready": inventory["readiness"]["portable_cross_venue_matching_ready"],
        "instrument_map_source_kind": instrument_map_artifact["source_kind"],
        "instrument_map_source_window_complete": instrument_map_artifact.get("source_registered_window_complete"),
        "outcome_run_authorized": False,
        "inventory_sha256": inventory["artifact_sha256"],
        "s01_all_sleeves_ready": s01_status["all_sleeves_ready"],
        "s02_config_parity_status": s01_status["s02_config_parity_status"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if inventory["readiness"]["status"] == "READY" else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="print the frozen plan and read-only readiness receipt",
    )
    mode.add_argument(
        "--phase0-inventory",
        action="store_true",
        help="write deterministic outcome-blind Phase-0 inventory artifacts",
    )
    parser.add_argument("--bybit-root", default=str(DEFAULT_ROOTS["bybit"]))
    parser.add_argument("--binance-root", default=str(DEFAULT_ROOTS["binance"]))
    parser.add_argument(
        "--deep-root-hash",
        action="store_true",
        help="hash complete registered Tier-A windows and validate exact root receipts",
    )
    parser.add_argument(
        "--write-plan",
        type=Path,
        help="optional path for the JSON plan receipt; stdout is always written",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_PHASE0_OUTPUT_ROOT,
        help="immutable Phase-0 bundle parent (used only with --phase0-inventory)",
    )
    parser.add_argument(
        "--instrument-map",
        type=Path,
        help=(
            "optional external versioned instrument-map JSON; when omitted, Phase 0 derives a conservative "
            "venue-local map from the registered manifest symbol/date projection"
        ),
    )
    parser.add_argument(
        "--instrument-map-version",
        help="required for a list-form instrument map; overrides object-form version",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=65_536,
        help="identity/provenance rows per read-only Parquet batch",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.phase0_inventory:
        try:
            return run_phase0_inventory(args)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    if not args.plan:
        print(
            "REFUSED: choose --plan or --phase0-inventory; population and outcome "
            "execution remain gated on frozen child contracts",
            file=sys.stderr,
        )
        return 2
    plan = build_plan(args)
    rendered = json.dumps(plan, indent=2, default=str) + "\n"
    if args.write_plan is not None:
        path = args.write_plan.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if plan["readiness"]["phase0_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
