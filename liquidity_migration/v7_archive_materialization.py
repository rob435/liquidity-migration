"""Materialize immutable V7 sources before their live paths are reused.

The primary path runs while every managed unit is stopped.  It holds the
authenticated, account-wide demo lease, snapshots the exact journal and market
capture trees twice, publishes a read-only copy, and builds/reopens the existing
V7 archive-source map.  A recovery path can extract the same roots from a
verified ``--leave-stopped`` reset tar plus its SHA-256 sidecar.

This is evidence preservation only.  It cannot authorize execution or deploy.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .account_owner_lease import DemoAccountIdentity, DemoAccountMutationLease
from .account_reset_receipt import (
    DEMO_BOUNDARY,
    MANAGED_UNITS,
    PAPER_BOUNDARY,
)
from .bybit import BybitPrivateClient, resolve_demo_credentials
from .artifact_snapshot import StableFileSnapshot, read_stable_file, rename_noreplace
from .deterministic_serialization import canonical_json
from .execution_twin_calibration import load_calibration_receipt
from .execution_twin_drift import (
    build_v7_archive_source_map,
    load_v7_archive_source_map,
    write_v7_archive_source_map,
)


READ_ONLY_DIRECTORY_MODE = 0o500
READ_ONLY_FILE_MODE = 0o400
PRIVATE_FILE_MODE = 0o600
_ACCEPTED_INACTIVE_STATES = frozenset({"inactive", "failed"})


def _full_commit(value: object, *, label: str) -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full lowercase 40-character Git commit")
    return commit


def _repository(path: str | Path, *, expected_candidate_commit: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("repository root must be an absolute non-symlink directory")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("repository root is unavailable") from exc
    if not root.is_dir():
        raise ValueError("repository root must be a directory")
    expected = _full_commit(expected_candidate_commit, label="expected V7 candidate")
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot resolve V7 candidate Git HEAD") from exc
    if completed.stdout.strip() != expected:
        raise ValueError("repository HEAD differs from the expected V7 candidate")
    return root


def _private_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a non-symlink regular file")
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
        raise ValueError(f"{label} must have exact mode 0600")
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the materializer")
    return candidate.resolve(strict=True)


def _private_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        require_mode=PRIVATE_FILE_MODE,
        require_owner=True,
        require_single_link=False,
    )


def _snapshot_signature(
    snapshot: StableFileSnapshot,
) -> tuple[int, int, int, int, int, str]:
    return (
        snapshot.device,
        snapshot.inode,
        snapshot.metadata.st_mode,
        snapshot.size,
        snapshot.mtime_ns,
        snapshot.sha256,
    )


def _file_signature(path: Path, *, label: str) -> tuple[int, int, int, int, int, str]:
    return _snapshot_signature(_private_snapshot(path, label=label))


def _source_directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be absolute")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a non-symlink directory")
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the materializer")
    return candidate.resolve(strict=True)


def _fresh_empty_root(path: Path, *, label: str) -> None:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
        raise ValueError(f"{label} must be an owner-owned directory with exact mode 0700")
    try:
        next(path.iterdir())
    except StopIteration:
        return
    raise ValueError(f"{label} has already been reused after the reset")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _root_inputs(calibration: Mapping[str, Any], *, repository_root: Path) -> tuple[Path, Path, str, str]:
    inputs = calibration.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ValueError("V7 calibration lacks source inputs")
    account = _source_directory(str(inputs.get("account_root") or ""), label="live V7 account root")
    capture = _source_directory(
        str(inputs.get("market_capture_root") or ""),
        label="live V7 market-capture root",
    )
    if _paths_overlap(account, capture) or (
        account.stat().st_dev,
        account.stat().st_ino,
    ) == (capture.stat().st_dev, capture.stat().st_ino):
        raise ValueError("V7 account and market-capture roots overlap")
    try:
        account_relative = account.relative_to(repository_root)
        capture_relative = capture.relative_to(repository_root)
        account_relative.relative_to("data")
        capture_relative.relative_to("data")
    except ValueError as exc:
        raise ValueError("V7 live sources must stay below repository data/") from exc
    return account, capture, account_relative.as_posix(), capture_relative.as_posix()


def _output_paths(
    *,
    destination_root: str | Path,
    archive_map_output: str | Path,
    original_roots: Sequence[Path],
) -> tuple[Path, Path]:
    destination = Path(destination_root).expanduser()
    map_output = Path(archive_map_output).expanduser()
    for value, label in (
        (destination, "V7 archive destination"),
        (map_output, "V7 archive-source map output"),
    ):
        if not value.is_absolute() or value.is_symlink():
            raise ValueError(f"{label} must be an absolute non-symlink path")
        try:
            parent = value.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{label} parent must already exist") from exc
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError(f"{label} parent must be a non-symlink directory")
        if value.exists() or value.is_symlink():
            raise FileExistsError(f"{label} already exists: {value}")
    destination = destination.parent.resolve(strict=True) / destination.name
    map_output = map_output.parent.resolve(strict=True) / map_output.name
    if _paths_overlap(destination, map_output):
        raise ValueError("V7 archive destination and map output overlap")
    for source in original_roots:
        if _paths_overlap(destination, source) or _paths_overlap(map_output, source):
            raise ValueError("V7 archive outputs overlap a live source root")
    return destination, map_output


def _tree_manifest(root: Path) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []

    def visit(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda value: value.name):
            metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            if metadata.st_uid != os.geteuid():
                raise ValueError(f"source tree entry has another owner: {child}")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"source tree contains symlink: {child}")
            if stat.S_ISDIR(metadata.st_mode):
                rows.append({"path": relative, "type": "directory"})
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                snapshot = read_stable_file(
                    child,
                    label=f"source tree file {relative}",
                    require_owner=True,
                    require_single_link=False,
                )
                if (
                    snapshot.device,
                    snapshot.inode,
                    snapshot.metadata.st_mode,
                    snapshot.size,
                    snapshot.mtime_ns,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ):
                    raise RuntimeError(f"source file changed while hashed: {child}")
                rows.append(
                    {
                        "path": relative,
                        "type": "file",
                        "size_bytes": snapshot.size,
                        "sha256": snapshot.sha256,
                    }
                )
            else:
                raise ValueError(f"source tree contains special file: {child}")

    visit(root)
    digest = hashlib.sha256(canonical_json({"entries": rows})).hexdigest()
    return rows, digest


def _copy_file(source: Path, destination: Path) -> None:
    before = source.lstat()
    input_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    input_descriptor = os.open(str(source), input_flags)
    opened = os.fstat(input_descriptor)
    if not stat.S_ISREG(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
    ) != (before.st_dev, before.st_ino):
        os.close(input_descriptor)
        raise RuntimeError(f"source file identity changed before copy: {source}")
    try:
        descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE)
        try:
            while chunk := os.read(input_descriptor, 1024 * 1024):
                view = memoryview(chunk)
                offset = 0
                while offset < len(view):
                    written = os.write(descriptor, view[offset:])
                    if written <= 0:
                        raise OSError("V7 archive copy made no progress")
                    offset += written
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        opened_after = os.fstat(input_descriptor)
    finally:
        os.close(input_descriptor)
    after = source.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_mode,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    ) or (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError(f"source file changed while copied: {source}")


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for child in sorted(source.iterdir(), key=lambda value: value.name):
        metadata = child.lstat()
        target = destination / child.name
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"source tree contains symlink: {child}")
        if stat.S_ISDIR(metadata.st_mode):
            _copy_tree(child, target)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_file(child, target)
        else:
            raise ValueError(f"source tree contains special file: {child}")


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for directory, child_directories, files in os.walk(root):
        path = Path(directory)
        directories.append(path)
        for name in files:
            file_descriptor = os.open(str(path / name), os.O_RDONLY)
            try:
                os.fsync(file_descriptor)
            finally:
                os.close(file_descriptor)
        child_directories.sort()
    for directory_path in reversed(directories):
        descriptor = os.open(str(directory_path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _restrict_tree(root: Path) -> None:
    directories: list[Path] = []
    for directory, child_directories, files in os.walk(root):
        path = Path(directory)
        directories.append(path)
        for name in files:
            os.chmod(path / name, READ_ONLY_FILE_MODE, follow_symlinks=False)
        child_directories.sort()
    for directory_path in reversed(directories):
        os.chmod(directory_path, READ_ONLY_DIRECTORY_MODE, follow_symlinks=False)


def _verify_restricted_tree(root: Path) -> None:
    for directory, child_directories, files in os.walk(root):
        path = Path(directory)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != READ_ONLY_DIRECTORY_MODE
            or metadata.st_uid != os.geteuid()
        ):
            raise ValueError(f"V7 archive directory is not read-only/private: {path}")
        for name in files:
            child = path / name
            child_metadata = child.lstat()
            if (
                not stat.S_ISREG(child_metadata.st_mode)
                or stat.S_IMODE(child_metadata.st_mode) != READ_ONLY_FILE_MODE
                or child_metadata.st_uid != os.geteuid()
            ):
                raise ValueError(f"V7 archive file is not read-only/private: {child}")
        child_directories.sort()


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    directories: list[Path] = []
    for directory, child_directories, files in os.walk(root):
        path = Path(directory)
        directories.append(path)
        os.chmod(path, 0o700, follow_symlinks=False)
        for name in files:
            os.chmod(path / name, PRIVATE_FILE_MODE, follow_symlinks=False)
        child_directories.sort()
    for directory_path in reversed(directories):
        os.chmod(directory_path, 0o700, follow_symlinks=False)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish without replacing an existing destination."""
    rename_noreplace(source, destination, label="V7 archive destination")


def _systemctl_executable(value: str | Path) -> Path:
    raw = str(value)
    found = raw if Path(raw).expanduser().is_absolute() else shutil.which(raw)
    if not found:
        raise ValueError(f"systemctl executable is unavailable: {raw}")
    executable = Path(found).expanduser().resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("systemctl path is not an executable regular file")
    return executable


def _require_managed_units_inactive(systemctl_bin: str | Path) -> None:
    executable = _systemctl_executable(systemctl_bin)
    for unit in MANAGED_UNITS:
        completed = subprocess.run(
            [str(executable), "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
        )
        state = completed.stdout.strip()
        if completed.returncode == 0:
            raise RuntimeError(f"managed unit is still active: {unit} ({state or 'active'})")
        if completed.returncode != 3 or state not in _ACCEPTED_INACTIVE_STATES:
            raise RuntimeError(
                f"managed unit inactivity is unknown: {unit} (exit={completed.returncode}, state={state or 'missing'})"
            )


@contextlib.contextmanager
def _authenticated_demo_lease() -> Iterator[DemoAccountMutationLease]:
    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        raise RuntimeError("V7 materialization requires the configured demo credentials")
    client = BybitPrivateClient(
        api_key=api_key,
        api_secret=api_secret,
        demo=True,
    )
    identity = DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info=client.get_api_key_information(),
    )
    lease = DemoAccountMutationLease(identity)
    with lease:
        yield lease


def _calibration(
    calibration_file: str | Path,
) -> tuple[Path, dict[str, Any]]:
    snapshot = _private_snapshot(
        calibration_file,
        label="V7 calibration receipt",
    )
    payload = load_calibration_receipt(
        snapshot.path,
        require_registered_requirements=True,
        snapshot=snapshot,
    )
    if payload.get("execution_twin_gate_passed") is not True:
        raise ValueError("V7 calibration gate has not passed")
    return snapshot.path, payload


def _materialize_and_map(
    *,
    calibration_path: Path,
    calibration: Mapping[str, Any],
    destination: Path,
    map_output: Path,
    populate_stage: Any,
    expected_manifests: tuple[tuple[list[dict[str, Any]], str], tuple[list[dict[str, Any]], str]],
    source_mode: str,
    pre_publish_check: Any | None = None,
    pre_return_check: Any | None = None,
) -> dict[str, Any]:
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage.", dir=destination.parent))
    os.chmod(stage, 0o700)
    published = False
    map_path: Path | None = None
    map_identity: tuple[int, int] | None = None
    map_stage_root: Path | None = None
    try:
        populate_stage(stage)
        account_stage = stage / "account"
        capture_stage = stage / "market_capture"
        account_manifest = _tree_manifest(account_stage)
        capture_manifest = _tree_manifest(capture_stage)
        if account_manifest != expected_manifests[0]:
            raise RuntimeError("materialized V7 account tree differs from its source")
        if capture_manifest != expected_manifests[1]:
            raise RuntimeError("materialized V7 capture tree differs from its source")
        if not any(row["type"] == "file" for row in account_manifest[0]):
            raise ValueError("materialized V7 account tree has no files")
        if not any(row["type"] == "file" for row in capture_manifest[0]):
            raise ValueError("materialized V7 capture tree has no files")
        if pre_publish_check is not None:
            pre_publish_check()
        _fsync_tree(stage)
        _restrict_tree(stage)
        _verify_restricted_tree(stage)
        # Darwin refuses to rename a directory whose own mode lacks write
        # permission.  Keep only the unpublished staging root writable; every
        # child is already read-only.  The atomic rename publishes complete
        # contents, then the final root is immediately sealed and fsynced.
        os.chmod(stage, 0o700, follow_symlinks=False)
        _rename_noreplace(stage, destination)
        published = True
        os.chmod(destination, READ_ONLY_DIRECTORY_MODE, follow_symlinks=False)
        _fsync_tree(destination)
        _fsync_parent(destination)
        account_root = destination / "account"
        capture_root = destination / "market_capture"
        archive_map = build_v7_archive_source_map(
            calibration_file=calibration_path,
            archived_account_root=account_root,
            archived_market_capture_root=capture_root,
        )
        map_stage_root = Path(tempfile.mkdtemp(prefix=f".{map_output.name}.stage.", dir=map_output.parent))
        os.chmod(map_stage_root, 0o700)
        expected_map_stage = map_stage_root / "archive-source-map.json"
        written_map_stage = write_v7_archive_source_map(expected_map_stage, archive_map)
        if written_map_stage != expected_map_stage.resolve(strict=True):
            raise RuntimeError("V7 archive-source writer returned another output path")
        os.chmod(written_map_stage, PRIVATE_FILE_MODE, follow_symlinks=False)
        map_descriptor = os.open(str(written_map_stage), os.O_RDONLY)
        try:
            os.fsync(map_descriptor)
        finally:
            os.close(map_descriptor)
        _fsync_parent(written_map_stage)
        _rename_noreplace(written_map_stage, map_output)
        map_path = map_output
        map_metadata = map_path.lstat()
        map_identity = (map_metadata.st_dev, map_metadata.st_ino)
        shutil.rmtree(map_stage_root)
        map_stage_root = None
        _fsync_parent(map_path)
        loaded = load_v7_archive_source_map(
            map_path,
            calibration_receipt=calibration,
        )
        reopened_metadata = map_path.lstat()
        if (reopened_metadata.st_dev, reopened_metadata.st_ino) != map_identity:
            raise RuntimeError("V7 archive-source map identity changed while reopened")
        if canonical_json(loaded) != canonical_json(archive_map):
            raise RuntimeError("V7 archive-source map changed while reopened")
        _verify_restricted_tree(destination)
        if pre_return_check is not None:
            pre_return_check()
        return {
            "source_mode": source_mode,
            "destination_root": str(destination),
            "archived_account_root": str(account_root),
            "archived_market_capture_root": str(capture_root),
            "account_tree_sha256": account_manifest[1],
            "market_capture_tree_sha256": capture_manifest[1],
            "archive_map": str(map_path),
            "archive_map_artifact_sha256": loaded["artifact_sha256"],
            "execution_authorization": "not_granted",
        }
    except BaseException:
        if map_stage_root is not None and map_stage_root.exists():
            shutil.rmtree(map_stage_root)
            _fsync_parent(map_stage_root)
        if map_path is not None and map_identity is not None:
            try:
                observed = map_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (observed.st_dev, observed.st_ino) == map_identity:
                    map_path.unlink()
                    _fsync_parent(map_path)
        if published and destination.exists():
            _make_tree_writable(destination)
            shutil.rmtree(destination)
            _fsync_parent(destination)
        elif stage.exists():
            _make_tree_writable(stage)
            shutil.rmtree(stage)
        raise


def materialize_v7_from_stopped_roots(
    *,
    repository_root: str | Path,
    expected_candidate_commit: str,
    calibration_file: str | Path,
    destination_root: str | Path,
    archive_map_output: str | Path,
    systemctl_bin: str | Path = "systemctl",
) -> dict[str, Any]:
    """Snapshot stopped V7 roots and freeze their existing archive-source map."""

    repository = _repository(repository_root, expected_candidate_commit=expected_candidate_commit)
    calibration_path, calibration = _calibration(calibration_file)
    account, capture, _account_relative, _capture_relative = _root_inputs(calibration, repository_root=repository)
    destination, map_output = _output_paths(
        destination_root=destination_root,
        archive_map_output=archive_map_output,
        original_roots=(account, capture),
    )
    _require_managed_units_inactive(systemctl_bin)
    with _authenticated_demo_lease():
        _require_managed_units_inactive(systemctl_bin)
        account_before = _tree_manifest(account)
        capture_before = _tree_manifest(capture)

        def populate(stage: Path) -> None:
            _copy_tree(account, stage / "account")
            _copy_tree(capture, stage / "market_capture")

        def revalidate_sources() -> None:
            if _tree_manifest(account) != account_before or _tree_manifest(capture) != capture_before:
                raise RuntimeError("live V7 sources changed during materialization")
            _require_managed_units_inactive(systemctl_bin)

        result = _materialize_and_map(
            calibration_path=calibration_path,
            calibration=calibration,
            destination=destination,
            map_output=map_output,
            populate_stage=populate,
            expected_manifests=(account_before, capture_before),
            source_mode="stopped_live_roots",
            pre_publish_check=revalidate_sources,
            pre_return_check=revalidate_sources,
        )
        return result


def _safe_member_name(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"reset archive contains unsafe member path: {raw!r}")
    return path


def _reset_manifest(data: bytes) -> dict[str, Any]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("reset archive manifest is not UTF-8") from exc
    single: dict[str, str] = {}
    repeated: dict[str, list[str]] = {
        "account_epoch_target": [],
        "target": [],
    }
    for line in lines:
        if "=" not in line:
            raise ValueError("reset archive manifest is malformed")
        key, value = line.split("=", 1)
        if key in repeated:
            repeated[key].append(value)
        elif key in {
            "git_head",
            "leave_stopped",
            "demo_boundary",
            "paper_boundary",
        }:
            if key in single:
                raise ValueError(f"reset archive manifest repeats {key}")
            single[key] = value
    required = {"git_head", "leave_stopped", "demo_boundary", "paper_boundary"}
    if set(single) != required:
        raise ValueError("reset archive manifest lacks required V7 boundary fields")
    return {
        **single,
        "account_epoch_targets": repeated["account_epoch_target"],
        "archived_targets": repeated["target"],
    }


def _extract_reset_sources(
    *,
    archive: Path,
    sidecar: Path,
    expected_candidate_commit: str,
    account_prefix: str,
    capture_prefix: str,
    stage: Path,
) -> tuple[tuple[list[dict[str, Any]], str], tuple[list[dict[str, Any]], str]]:
    archive_snapshot = _private_snapshot(archive, label="V7 reset archive")
    sidecar_snapshot = _private_snapshot(
        sidecar,
        label="V7 reset archive sidecar",
    )
    archive_before = _snapshot_signature(archive_snapshot)
    sidecar_before = _snapshot_signature(sidecar_snapshot)
    expected_sidecar = f"{archive_before[-1]}  {archive.name}\n"
    try:
        sidecar_text = sidecar_snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("V7 reset archive sidecar is not UTF-8") from exc
    if sidecar_text != expected_sidecar:
        raise ValueError("V7 reset archive sidecar does not exactly bind the tar")
    prefixes = {
        PurePosixPath(account_prefix): stage / "account",
        PurePosixPath(capture_prefix): stage / "market_capture",
    }
    for destination in prefixes.values():
        destination.mkdir(mode=0o700)
    seen: set[PurePosixPath] = set()
    manifest_data: bytes | None = None
    selected_files = {prefix: 0 for prefix in prefixes}
    try:
        with tarfile.open(
            fileobj=io.BytesIO(archive_snapshot.data),
            mode="r:gz",
        ) as handle:
            for member in handle:
                member_path = _safe_member_name(member.name.rstrip("/"))
                if member_path in seen:
                    raise ValueError(f"reset archive repeats member {member.name!r}")
                seen.add(member_path)
                if member_path == PurePosixPath("ledger-reset-manifest.txt"):
                    if not member.isfile() or member.issym() or member.islnk() or member.size > 1024 * 1024:
                        raise ValueError("reset archive manifest member is unsafe")
                    extracted = handle.extractfile(member)
                    if extracted is None:
                        raise ValueError("reset archive manifest is unreadable")
                    manifest_data = extracted.read()
                    continue
                matched: tuple[PurePosixPath, Path] | None = None
                for prefix, destination in prefixes.items():
                    if member_path == prefix or prefix in member_path.parents:
                        matched = prefix, destination
                        break
                if matched is None:
                    continue
                prefix, destination = matched
                relative_parts = member_path.parts[len(prefix.parts) :]
                target = destination.joinpath(*relative_parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                elif member.isfile() and not member.issym() and not member.islnk():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    extracted = handle.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"reset archive file is unreadable: {member.name}")
                    descriptor = os.open(
                        str(target),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        PRIVATE_FILE_MODE,
                    )
                    try:
                        while chunk := extracted.read(1024 * 1024):
                            view = memoryview(chunk)
                            offset = 0
                            while offset < len(view):
                                written = os.write(descriptor, view[offset:])
                                if written <= 0:
                                    raise OSError("V7 tar extraction made no progress")
                                offset += written
                        os.fchmod(descriptor, PRIVATE_FILE_MODE)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    selected_files[prefix] += 1
                else:
                    raise ValueError(f"reset archive selected root contains unsafe member: {member.name}")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("V7 reset archive is not a readable gzip tar") from exc
    if manifest_data is None:
        raise ValueError("V7 reset archive has no embedded reset manifest")
    manifest = _reset_manifest(manifest_data)
    candidate = _full_commit(expected_candidate_commit, label="expected V7 candidate")
    if (
        manifest["git_head"] != candidate
        or manifest["leave_stopped"] != "1"
        or manifest["demo_boundary"] != DEMO_BOUNDARY
        or manifest["paper_boundary"] != PAPER_BOUNDARY
    ):
        raise ValueError("V7 reset archive does not establish the expected stopped boundary")
    for relative_prefix in (account_prefix, capture_prefix):
        if (
            relative_prefix not in manifest["account_epoch_targets"]
            or relative_prefix not in manifest["archived_targets"]
        ):
            raise ValueError(f"V7 reset archive did not archive exact source root {relative_prefix}")
    if any(count <= 0 for count in selected_files.values()):
        raise ValueError("V7 reset archive lacks account or capture source files")
    if (
        _file_signature(archive, label="V7 reset archive") != archive_before
        or _file_signature(sidecar, label="V7 reset archive sidecar") != sidecar_before
    ):
        raise RuntimeError("V7 reset archive bundle changed during extraction")
    return _tree_manifest(stage / "account"), _tree_manifest(stage / "market_capture")


def materialize_v7_from_reset_archive(
    *,
    repository_root: str | Path,
    expected_candidate_commit: str,
    calibration_file: str | Path,
    reset_archive: str | Path,
    reset_sha256_sidecar: str | Path,
    destination_root: str | Path,
    archive_map_output: str | Path,
) -> dict[str, Any]:
    """Recover V7 immutable sources from a verified leave-stopped reset archive."""

    repository = _repository(repository_root, expected_candidate_commit=expected_candidate_commit)
    calibration_path, calibration = _calibration(calibration_file)
    account, capture, account_relative, capture_relative = _root_inputs(calibration, repository_root=repository)
    _fresh_empty_root(account, label="post-reset V7 account root")
    _fresh_empty_root(capture, label="post-reset V7 market-capture root")
    destination, map_output = _output_paths(
        destination_root=destination_root,
        archive_map_output=archive_map_output,
        original_roots=(account, capture),
    )
    archive = _private_file(reset_archive, label="V7 reset archive")
    sidecar = _private_file(reset_sha256_sidecar, label="V7 reset archive SHA-256 sidecar")

    def populate(stage: Path) -> None:
        _extract_reset_sources(
            archive=archive,
            sidecar=sidecar,
            expected_candidate_commit=expected_candidate_commit,
            account_prefix=account_relative,
            capture_prefix=capture_relative,
            stage=stage,
        )

    # The exact manifests are produced by the safe extractor.  Populate a
    # temporary preview first, delete it, then repeat into the transactional
    # stage.  Equal hashes across both passes detect archive mutation and make
    # the final stage comparison independent of copied summary fields.
    preview = Path(tempfile.mkdtemp(prefix=f".{destination.name}.preview.", dir=destination.parent))
    try:
        preview_expected = _extract_reset_sources(
            archive=archive,
            sidecar=sidecar,
            expected_candidate_commit=expected_candidate_commit,
            account_prefix=account_relative,
            capture_prefix=capture_relative,
            stage=preview,
        )
    finally:
        if preview.exists():
            shutil.rmtree(preview)
    return _materialize_and_map(
        calibration_path=calibration_path,
        calibration=calibration,
        destination=destination,
        map_output=map_output,
        populate_stage=populate,
        expected_manifests=preview_expected,
        source_mode="verified_reset_archive",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize immutable V7 roots and reopen their archive-source map")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repository-root", required=True)
        subparser.add_argument("--expected-candidate-commit", required=True)
        subparser.add_argument("--calibration-file", required=True)
        subparser.add_argument("--destination-root", required=True)
        subparser.add_argument("--archive-map-output", required=True)

    stopped = subparsers.add_parser(
        "from-stopped-roots",
        help="primary preregistered path: snapshot live V7 while every unit is stopped",
    )
    common(stopped)
    stopped.add_argument("--systemctl-bin", default="systemctl")

    reset = subparsers.add_parser(
        "from-reset-archive",
        help="recovery path: extract V7 from a verified leave-stopped reset archive",
    )
    common(reset)
    reset.add_argument("--reset-archive", required=True)
    reset.add_argument("--reset-sha256-sidecar", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    common = {
        "repository_root": args.repository_root,
        "expected_candidate_commit": args.expected_candidate_commit,
        "calibration_file": args.calibration_file,
        "destination_root": args.destination_root,
        "archive_map_output": args.archive_map_output,
    }
    try:
        if args.command == "from-stopped-roots":
            result = materialize_v7_from_stopped_roots(
                **common,
                systemctl_bin=args.systemctl_bin,
            )
        else:
            result = materialize_v7_from_reset_archive(
                **common,
                reset_archive=args.reset_archive,
                reset_sha256_sidecar=args.reset_sha256_sidecar,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
