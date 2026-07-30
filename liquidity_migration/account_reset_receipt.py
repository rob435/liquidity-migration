"""Source-reopening receipt for the demo/paper account epoch reset.

The shell reset remains the operational transaction because it owns systemd,
the demo-account lease, and the archive/remove/rebuild sequence.  This module
turns only a fully completed ``--execute --leave-stopped`` run into a durable
machine-checkable artifact.  Failed or interrupted resets never receive a
``passed`` receipt.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json


SCHEMA_VERSION = 1
KIND = "demo_paper_account_epoch_reset"
VALIDATOR = "account_reset_receipt_v1"
# Deliberately NOT every ExecutionEnvironment. This constant drives a
# destructive ledger reset, and the mainnet journal must not be reachable
# by a tool whose whole job is erasing account history.
ROOT_ENVIRONMENTS = ("demo", "paper")
ROOT_KINDS = ("account", "inbox", "capture")
MANAGED_UNITS = (
    "liquidity-migration-demo-liveness.timer",
    "liquidity-migration-continuous-hedge.timer",
    "liquidity-migration-continuous-rmom-refresh.timer",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-bybit-carry-demo.service",
    "liquidity-migration-bybit-carry-paper.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-demo-liveness.service",
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
)
DEMO_BOUNDARY = "venue_verified_flat_positions_0_open_orders_0"
PAPER_BOUNDARY = "archived_deterministic_epoch_not_carried_forward"
LIMITATIONS = (
    "systemd_inactivity_is_a_local_point_in_time_check_not_a_remote_attestation",
    "posix_mode_restrictions_are_not_worm_storage_or_a_signature",
    "receipt_establishes_an_epoch_boundary_not_execution_or_deploy_authority",
)
_IDENTITY_FIELDS = {
    "path",
    "size_bytes",
    "sha256",
    "device",
    "inode",
    "mtime_ns",
    "mode",
    "uid",
}
_SINGLETON_MANIFEST_KEYS = (
    "ledger_reset_utc",
    "git_head",
    "sleeves",
    "include_reports",
    "include_caches",
    "leave_stopped",
    "env_file",
    "account_env_file",
    "paper_account_env_file",
    "demo_account_lease_path",
    "demo_boundary",
    "paper_boundary",
    "active_before",
)
_REPEATED_MANIFEST_KEYS = (
    "account_epoch_target",
    "target",
    "preserved_risk_state",
)
_PAPER_RUNTIME_USER = "liquidity-migration-paper"
_ARCHIVE_READ_SIZE = 1024 * 1024
_MAX_MANIFEST_SIZE = 1024 * 1024
_MAX_SIDECAR_SIZE = 1024
_MAX_TAR_METADATA_SIZE = 1024 * 1024
_MAX_RECEIPT_SIZE = 1024 * 1024
_TRUSTED_EXECUTABLE_PATH = "/usr/bin:/bin"
_TRUSTED_GIT = Path("/usr/bin/git")
_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    mode: int
    uid: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _full_commit(value: object, *, label: str) -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full lowercase 40-character Git commit")
    return commit


def _repository(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("repository root must be an absolute non-symlink directory")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("repository root is unavailable") from exc
    if not root.is_dir():
        raise ValueError("repository root must be a directory")
    return root


def _git_head(repository_root: Path) -> str:
    try:
        git_executable = _TRUSTED_GIT.lstat()
    except OSError as exc:
        raise ValueError("trusted Git executable is unavailable") from exc
    if (
        stat.S_ISLNK(git_executable.st_mode)
        or not stat.S_ISREG(git_executable.st_mode)
        or git_executable.st_uid != 0
        or stat.S_IMODE(git_executable.st_mode) & 0o022
        or not stat.S_IMODE(git_executable.st_mode) & 0o111
    ):
        raise ValueError("trusted Git executable is unsafe")
    git_directory = repository_root / ".git"
    try:
        git_metadata = git_directory.lstat()
    except OSError as exc:
        raise ValueError("reset candidate Git directory is unavailable") from exc
    if git_directory.is_symlink() or not stat.S_ISDIR(git_metadata.st_mode):
        raise ValueError("reset candidate Git directory must be one real directory")
    git_environment = {
        "PATH": _TRUSTED_EXECUTABLE_PATH,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    git_prefix = [
        str(_TRUSTED_GIT),
        "--no-optional-locks",
        f"--git-dir={git_directory}",
        f"--work-tree={repository_root}",
        "-c",
        f"safe.directory={repository_root}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.filemode=true",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(repository_root),
    ]
    try:
        completed = subprocess.run(
            [*git_prefix, "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            env=git_environment,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot resolve reset candidate Git HEAD") from exc
    candidate = _full_commit(completed.stdout.strip(), label="reset candidate Git HEAD")
    try:
        with tempfile.TemporaryDirectory(prefix="liqmig-reset-index-") as temporary:
            clean_environment = {
                **git_environment,
                "GIT_INDEX_FILE": str(Path(temporary) / "index"),
            }
            subprocess.run(
                [*git_prefix, "read-tree", candidate],
                check=True,
                capture_output=True,
                text=True,
                env=clean_environment,
            )
            refreshed = subprocess.run(
                [*git_prefix, "update-index", "--refresh"],
                check=False,
                capture_output=True,
                text=True,
                env=clean_environment,
            )
            if refreshed.returncode not in {0, 1}:
                raise subprocess.CalledProcessError(
                    refreshed.returncode,
                    refreshed.args,
                    output=refreshed.stdout,
                    stderr=refreshed.stderr,
                )
            tracked = subprocess.run(
                [*git_prefix, "diff-index", "--quiet", candidate, "--"],
                check=False,
                capture_output=True,
                text=True,
                env=clean_environment,
            )
            if tracked.returncode not in {0, 1}:
                raise subprocess.CalledProcessError(
                    tracked.returncode,
                    tracked.args,
                    output=tracked.stdout,
                    stderr=tracked.stderr,
                )
            untracked = subprocess.run(
                [*git_prefix, "ls-files", "--others", "--exclude-standard"],
                check=True,
                capture_output=True,
                text=True,
                env=clean_environment,
            )
        final_head = subprocess.run(
            [*git_prefix, "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            env=git_environment,
        )
        final_git_executable = _TRUSTED_GIT.lstat()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot verify reset candidate checkout cleanliness") from exc
    if refreshed.returncode == 1 or tracked.returncode == 1 or untracked.stdout:
        raise ValueError("reset candidate checkout is dirty")
    if _full_commit(final_head.stdout.strip(), label="final reset candidate Git HEAD") != candidate:
        raise ValueError("reset candidate Git HEAD changed during verification")
    if _metadata_signature(final_git_executable) != _metadata_signature(git_executable):
        raise ValueError("trusted Git executable changed during verification")
    return candidate


def _trusted_systemctl(path: str | Path) -> tuple[Path, os.stat_result]:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("systemctl executable must be one absolute non-symlink path")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError("systemctl executable is unavailable") from exc
    permissions = stat.S_IMODE(metadata.st_mode)
    if (
        resolved != candidate
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or permissions & 0o022
        or not permissions & 0o111
    ):
        raise ValueError("systemctl executable is unsafe")
    return candidate, metadata


def _observe_managed_units_inactive(
    systemctl: str | Path,
    managed_units: Sequence[str],
) -> list[str]:
    units = list(managed_units)
    if tuple(units) != MANAGED_UNITS:
        raise ValueError("systemd inactivity verification requires the exact managed-unit order")
    executable, before = _trusted_systemctl(systemctl)
    environment = {
        "PATH": _TRUSTED_EXECUTABLE_PATH,
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
    }
    for unit in units:
        observed: dict[str, str] = {}
        for property_name in ("LoadState", "ActiveState"):
            try:
                completed = subprocess.run(
                    [
                        str(executable),
                        "show",
                        unit,
                        f"--property={property_name}",
                        "--value",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ValueError(f"cannot query systemd unit state: {unit}") from exc
            value = completed.stdout.strip()
            if not value or "\n" in value:
                raise ValueError(f"systemd returned malformed {property_name} for {unit}")
            observed[property_name] = value
        if observed != {"LoadState": "loaded", "ActiveState": "inactive"}:
            raise ValueError(
                f"managed systemd unit is not loaded and inactive: {unit} "
                f"(load={observed['LoadState']}, active={observed['ActiveState']})"
            )
    try:
        after = executable.lstat()
    except OSError as exc:
        raise ValueError("systemctl executable disappeared during unit verification") from exc
    if _metadata_signature(after) != _metadata_signature(before):
        raise ValueError("systemctl executable changed during unit verification")
    return units


def _private_snapshot(
    path: str | Path,
    *,
    label: str,
    max_bytes: int | None = None,
) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
        max_bytes=max_bytes,
    )


def _identity_from_snapshot(snapshot: StableFileSnapshot) -> FileIdentity:
    return FileIdentity(
        path=str(snapshot.path),
        size_bytes=snapshot.size,
        sha256=snapshot.sha256,
        device=snapshot.device,
        inode=snapshot.inode,
        mtime_ns=snapshot.mtime_ns,
        mode=snapshot.mode,
        uid=snapshot.uid,
    )


def _file_identity(
    path: str | Path,
    *,
    label: str,
    max_bytes: int | None = None,
) -> FileIdentity:
    return _identity_from_snapshot(
        read_stable_file(
            path,
            label=label,
            require_mode=0o600,
            require_owner=True,
            require_single_link=True,
            max_bytes=max_bytes,
        )
    )


def _metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_type(mode: int) -> int:
    return stat.S_IFMT(mode)


def _directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - supported runtime platforms are POSIX
        raise RuntimeError(
            "account reset receipt requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC"
        ) from exc


def _mount_id_for_fd(descriptor: int) -> int | None:
    if _LINUX_FDINFO_ROOT is None:
        return None
    try:
        raw = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Linux mount identity cannot be read for receipt descriptor {descriptor}"
        ) from exc
    values = [
        line.partition(":")[2].strip()
        for line in raw.splitlines()
        if line.partition(":")[0] == "mnt_id"
    ]
    if len(values) != 1:
        raise RuntimeError(f"Linux mount identity is invalid for receipt descriptor {descriptor}")
    try:
        value = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(
            f"Linux mount identity is invalid for receipt descriptor {descriptor}"
        ) from exc
    if value < 0:
        raise RuntimeError(f"Linux mount identity is invalid for receipt descriptor {descriptor}")
    return value


def _entry_metadata(directory_fd: int, name: str) -> os.stat_result:
    """Inspect one entry without following it; isolated for race regression injection."""

    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _open_absolute_directory(path: Path) -> int:
    """Open every absolute path component without following a symbolic link."""

    if not path.is_absolute():
        raise ValueError(f"account reset receipt directory must be absolute: {path}")
    descriptor = os.open("/", _directory_open_flags())
    current = Path("/")
    try:
        for name in path.parts[1:]:
            current /= name
            observed = _entry_metadata(descriptor, name)
            child = os.open(name, _directory_open_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                rebound = _entry_metadata(descriptor, name)
                identity = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
                if (
                    not stat.S_ISDIR(observed.st_mode)
                    or (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != identity
                    or (rebound.st_dev, rebound.st_ino, _file_type(rebound.st_mode)) != identity
                ):
                    raise RuntimeError(
                        f"account reset receipt directory changed while opened: {current}"
                    )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_absolute_directory(
    path: Path,
    descriptor: int,
    *,
    label: str,
) -> None:
    """Prove that a held directory is still reached through its absolute path/mount."""

    expected = os.fstat(descriptor)
    expected_mount_id = _mount_id_for_fd(descriptor)
    try:
        rebound = _open_absolute_directory(path)
    except OSError as exc:
        raise RuntimeError(f"{label} path or mount changed") from exc
    try:
        current = os.fstat(rebound)
        if (
            (current.st_dev, current.st_ino, _file_type(current.st_mode))
            != (expected.st_dev, expected.st_ino, stat.S_IFDIR)
            or _mount_id_for_fd(rebound) != expected_mount_id
        ):
            raise RuntimeError(f"{label} path or mount changed")
    finally:
        os.close(rebound)


def _entry_mount_id(
    directory_fd: int,
    name: str,
    *,
    path: Path,
    observed: os.stat_result,
) -> int | None:
    """Bind a Linux leaf with O_PATH so regular-file bind mounts are visible."""

    if _LINUX_FDINFO_ROOT is None:
        return None
    path_flag = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if path_flag is None or nofollow is None or cloexec is None:  # pragma: no cover - Linux supplies these flags
        raise RuntimeError("Linux receipt verification requires O_PATH and O_NOFOLLOW")
    flags = path_flag | nofollow | cloexec
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(f"fresh account root entry changed while inspected: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(directory_fd, name)
        expected = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != expected
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != expected
        ):
            raise RuntimeError(f"fresh account root entry changed while inspected: {path}")
        return _mount_id_for_fd(descriptor)
    finally:
        os.close(descriptor)


class _HashingDescriptorReader:
    """Bound each compressed-archive read while hashing one open descriptor."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        requested = _ARCHIVE_READ_SIZE if size < 0 else min(size, _ARCHIVE_READ_SIZE)
        chunk = os.read(self._descriptor, requested)
        self._digest.update(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _BoundedForwardReader:
    """Seek forward through a gzip stream while bounding parser-owned reads."""

    def __init__(self, source: gzip.GzipFile) -> None:
        self._source = source
        self._position = 0
        self._read_budget = _MAX_TAR_METADATA_SIZE + (2 * tarfile.BLOCKSIZE)

    def reset_read_budget(self, value: int) -> None:
        self._read_budget = value

    def tell(self) -> int:
        return self._position

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self._read_budget:
            raise tarfile.ReadError("reset archive tar metadata exceeds the safety limit")
        chunk = self._source.read(size)
        self._position += len(chunk)
        self._read_budget -= len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_CUR:
            target = self._position + offset
        elif whence == os.SEEK_SET:
            target = offset
        else:
            raise tarfile.StreamError("reset archive stream does not support end-relative seeking")
        if target < self._position:
            raise tarfile.StreamError("reset archive stream cannot seek backwards")
        while self._position < target:
            chunk = self._source.read(min(_ARCHIVE_READ_SIZE, target - self._position))
            if not chunk:
                break
            self._position += len(chunk)
        return self._position


def _stream_archive(
    path: str | Path,
    *,
    account_epoch_targets: Sequence[str],
) -> tuple[FileIdentity, bytes, dict[str, bool]]:
    """Hash and inspect a gzip tar without retaining the archive or member list."""

    candidate = Path(path).expanduser()
    absolute = Path(os.path.abspath(candidate))
    try:
        before_path = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"reset archive is unavailable: {candidate}") from exc
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError(f"reset archive must not be a symbolic link: {candidate}")
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"reset archive must be a regular file: {candidate}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(str(candidate), flags)
    except OSError as exc:
        raise ValueError(f"reset archive cannot be opened safely: {candidate}") from exc
    try:
        before_descriptor = os.fstat(descriptor)
        if _metadata_signature(before_descriptor) != _metadata_signature(before_path):
            raise RuntimeError(f"reset archive path changed while it was opened: {candidate}")
        if not stat.S_ISREG(before_descriptor.st_mode):
            raise ValueError(f"reset archive must be a regular file: {candidate}")
        if (
            stat.S_IMODE(before_descriptor.st_mode) != 0o600
            or before_descriptor.st_uid != os.geteuid()
            or before_descriptor.st_nlink != 1
        ):
            raise ValueError("reset archive must be verifier-owned mode 0600 and not hard-linked")

        reader = _HashingDescriptorReader(descriptor)
        presence = {target: False for target in account_epoch_targets}
        manifest_count = 0
        manifest_data: bytes | None = None
        manifest_is_unsafe = False
        try:
            with gzip.GzipFile(fileobj=cast(Any, reader), mode="rb") as compressed:
                tar_stream = _BoundedForwardReader(compressed)
                with tarfile.TarFile(fileobj=cast(Any, tar_stream), mode="r") as handle:
                    while True:
                        tar_stream.reset_read_budget(
                            _MAX_TAR_METADATA_SIZE + (2 * tarfile.BLOCKSIZE)
                        )
                        member = handle.next()
                        if member is None:
                            break
                        # Supported Python versions cache yielded TarInfo objects
                        # even for pipe-style iteration. Only this header is needed.
                        cast(Any, handle).members.clear()
                        if sum(
                            len(key) + len(value)
                            for key, value in handle.pax_headers.items()
                        ) > _MAX_TAR_METADATA_SIZE:
                            raise ValueError("reset archive global tar metadata is unsafe")
                        name = member.name.rstrip("/")
                        for target in presence:
                            if name == target or name.startswith(f"{target}/"):
                                presence[target] = True
                        if member.name != "ledger-reset-manifest.txt":
                            continue
                        manifest_count += 1
                        if manifest_count != 1:
                            continue
                        if (
                            not member.isfile()
                            or member.issym()
                            or member.islnk()
                            or member.size > _MAX_MANIFEST_SIZE
                        ):
                            manifest_is_unsafe = True
                            continue
                        extracted = handle.extractfile(member)
                        if extracted is None:
                            raise ValueError("reset archive manifest cannot be read")
                        tar_stream.reset_read_budget(_MAX_MANIFEST_SIZE + tarfile.BLOCKSIZE)
                        manifest_data = extracted.read(_MAX_MANIFEST_SIZE + 1)
                        if len(manifest_data) != member.size:
                            raise ValueError("reset archive manifest cannot be read")
                while compressed.read(_ARCHIVE_READ_SIZE):
                    pass
        except (EOFError, OSError, tarfile.TarError) as exc:
            raise ValueError("reset archive is not a readable gzip tar") from exc

        while reader.read(_ARCHIVE_READ_SIZE):
            pass
        after_descriptor = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    try:
        after_path = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"reset archive disappeared while it was verified: {candidate}") from exc
    signature = _metadata_signature(before_descriptor)
    if signature != _metadata_signature(after_descriptor) or signature != _metadata_signature(after_path):
        raise RuntimeError(f"reset archive changed while it was verified: {candidate}")
    if reader.bytes_read != after_descriptor.st_size:
        raise RuntimeError(f"reset archive size changed while it was verified: {candidate}")
    if manifest_count != 1:
        raise ValueError("reset archive must contain one exact embedded manifest")
    if manifest_is_unsafe:
        raise ValueError("reset archive manifest member is unsafe")
    if manifest_data is None:
        raise ValueError("reset archive manifest cannot be read")

    identity = FileIdentity(
        path=str(absolute),
        size_bytes=after_descriptor.st_size,
        sha256=reader.hexdigest(),
        device=after_descriptor.st_dev,
        inode=after_descriptor.st_ino,
        mtime_ns=after_descriptor.st_mtime_ns,
        mode=stat.S_IMODE(after_descriptor.st_mode),
        uid=after_descriptor.st_uid,
    )
    return identity, manifest_data, presence


def _identity_from_payload(value: object, *, label: str) -> FileIdentity:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_FIELDS:
        raise ValueError(f"{label} identity has unexpected fields")
    uid_value = value.get("uid")
    identity = FileIdentity(
        path=str(value.get("path") or ""),
        size_bytes=int(value.get("size_bytes") or 0),
        sha256=_lower_sha256(value.get("sha256"), label=f"{label} hash"),
        device=int(value.get("device") or 0),
        inode=int(value.get("inode") or 0),
        mtime_ns=int(value.get("mtime_ns") or 0),
        mode=int(value.get("mode") or 0),
        uid=int(cast(int | str, uid_value)) if uid_value is not None else -1,
    )
    if (
        not Path(identity.path).is_absolute()
        or identity.size_bytes <= 0
        or identity.device <= 0
        or identity.inode <= 0
        or identity.mtime_ns <= 0
        or identity.mode != 0o600
        or identity.uid < 0
    ):
        raise ValueError(f"{label} identity is invalid")
    return identity


def _normalize_roots(
    roots: Mapping[str, Mapping[str, str | Path]], *, repository_root: Path
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    if set(roots) != set(ROOT_ENVIRONMENTS):
        raise ValueError("account epoch roots must contain exact demo and paper keys")
    absolute: dict[str, dict[str, str]] = {}
    relative: dict[str, dict[str, str]] = {}
    seen: list[Path] = []
    for environment in ROOT_ENVIRONMENTS:
        environment_roots = roots[environment]
        if set(environment_roots) != set(ROOT_KINDS):
            raise ValueError(f"{environment} account epoch roots must contain account/inbox/capture")
        absolute[environment] = {}
        relative[environment] = {}
        for kind in ROOT_KINDS:
            raw = Path(environment_roots[kind]).expanduser()
            candidate = raw if raw.is_absolute() else repository_root / raw
            resolved = candidate.resolve(strict=False)
            try:
                rel = resolved.relative_to(repository_root)
                rel.relative_to("data")
            except ValueError as exc:
                raise ValueError(f"{environment} {kind} root must remain below repository data/") from exc
            if any(resolved == prior or resolved in prior.parents or prior in resolved.parents for prior in seen):
                raise ValueError("account epoch roots must be pairwise disjoint")
            seen.append(resolved)
            absolute[environment][kind] = str(resolved)
            relative[environment][kind] = rel.as_posix()
    return absolute, relative


def _verify_fresh_root(
    path: Path,
    *,
    environment: str,
    kind: str,
    expected_uid: int,
) -> None:
    label = f"fresh {environment} {kind} root"
    parent_fd = -1
    root_fd = -1
    try:
        parent_fd = _open_absolute_directory(path.parent)
        observed = _entry_metadata(parent_fd, path.name)
        root_fd = os.open(path.name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(root_fd)
        current = _entry_metadata(parent_fd, path.name)
    except OSError as exc:
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        raise ValueError(f"{label} is unavailable or not a real directory") from exc
    try:
        root_identity = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
        if (
            not stat.S_ISDIR(observed.st_mode)
            or (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != root_identity
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != root_identity
        ):
            raise ValueError(f"{label} is not a directory")
        if stat.S_IMODE(opened.st_mode) != 0o700:
            raise ValueError(f"{label} must have mode 0700")
        if opened.st_uid != expected_uid:
            raise ValueError(f"{label} has another owner")
        root_mount_id = _mount_id_for_fd(root_fd)
        parent_metadata = os.fstat(parent_fd)
        if opened.st_dev != parent_metadata.st_dev or root_mount_id != _mount_id_for_fd(parent_fd):
            raise ValueError(f"{label} is a mount boundary")

        def visit(
            directory_fd: int,
            relative_parts: tuple[str, ...],
            *,
            in_lock_namespace: bool,
        ) -> tuple[bool, dict[tuple[str, ...], tuple[int, ...]]]:
            contains_lock_infrastructure = in_lock_namespace
            snapshot: dict[tuple[str, ...], tuple[int, ...]] = {}
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as exc:
                raise ValueError(f"{label} cannot be inspected") from exc
            for name in names:
                candidate_parts = (*relative_parts, name)
                candidate = path.joinpath(*candidate_parts)
                try:
                    entry = _entry_metadata(directory_fd, name)
                except OSError as exc:
                    raise ValueError(f"{label} contains unavailable state: {candidate}") from exc
                if stat.S_ISDIR(entry.st_mode):
                    try:
                        child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
                    except OSError as exc:
                        raise ValueError(
                            f"{label} contains unsafe directory state: {candidate}"
                        ) from exc
                    try:
                        child = os.fstat(child_fd)
                        rebound = _entry_metadata(directory_fd, name)
                        identity = (entry.st_dev, entry.st_ino, _file_type(entry.st_mode))
                        child_mount_id = _mount_id_for_fd(child_fd)
                        if (
                            (child.st_dev, child.st_ino, _file_type(child.st_mode)) != identity
                            or (rebound.st_dev, rebound.st_ino, _file_type(rebound.st_mode))
                            != identity
                            or child.st_dev != opened.st_dev
                            or child_mount_id != root_mount_id
                        ):
                            raise ValueError(
                                f"{label} contains unsafe directory state: {candidate}"
                            )
                        directory_mode = stat.S_IMODE(child.st_mode)
                        if (
                            directory_mode & 0o700 != 0o700
                            or directory_mode & 0o022
                            or child.st_uid != expected_uid
                        ):
                            raise ValueError(
                                f"{label} contains unsafe directory state: {candidate}"
                            )
                        child_lock_namespace = in_lock_namespace or (
                            not relative_parts and name == ".locks"
                        )
                        child_contains_lock, child_snapshot = visit(
                            child_fd,
                            candidate_parts,
                            in_lock_namespace=child_lock_namespace,
                        )
                        final_child = os.fstat(child_fd)
                        final_entry = _entry_metadata(directory_fd, name)
                        if (
                            _metadata_signature(final_child) != _metadata_signature(child)
                            or _metadata_signature(final_entry) != _metadata_signature(child)
                            or _mount_id_for_fd(child_fd) != child_mount_id
                        ):
                            raise RuntimeError(
                                f"{label} directory changed while inspected: {candidate}"
                            )
                    finally:
                        os.close(child_fd)
                    if not child_lock_namespace and not child_contains_lock:
                        raise ValueError(f"{label} is not empty of epoch payload: {candidate}")
                    snapshot[candidate_parts] = (
                        *_metadata_signature(child),
                        -1 if child_mount_id is None else child_mount_id,
                    )
                    snapshot.update(child_snapshot)
                    contains_lock_infrastructure = (
                        contains_lock_infrastructure
                        or child_lock_namespace
                        or child_contains_lock
                    )
                    continue
                if not stat.S_ISREG(entry.st_mode):
                    raise ValueError(f"{label} contains unsafe filesystem state: {candidate}")
                entry_mount_id = _entry_mount_id(
                    directory_fd,
                    name,
                    path=candidate,
                    observed=entry,
                )
                current_entry = _entry_metadata(directory_fd, name)
                if (
                    _metadata_signature(current_entry) != _metadata_signature(entry)
                    or entry.st_dev != opened.st_dev
                    or entry_mount_id != root_mount_id
                ):
                    raise ValueError(f"{label} contains unsafe filesystem state: {candidate}")
                if not in_lock_namespace and not name.endswith(".lock"):
                    raise ValueError(f"{label} is not empty of epoch payload: {candidate}")
                if (
                    entry.st_nlink != 1
                    or stat.S_IMODE(entry.st_mode) != 0o600
                    or entry.st_uid != expected_uid
                ):
                    raise ValueError(f"{label} contains unsafe filesystem state: {candidate}")
                snapshot[candidate_parts] = (
                    *_metadata_signature(entry),
                    -1 if entry_mount_id is None else entry_mount_id,
                )
                contains_lock_infrastructure = True
            return contains_lock_infrastructure, snapshot

        root_before = os.fstat(root_fd)
        _contains_lock, first_snapshot = visit(root_fd, (), in_lock_namespace=False)
        _contains_lock, final_snapshot = visit(root_fd, (), in_lock_namespace=False)
        root_after = os.fstat(root_fd)
        rebound_root = _entry_metadata(parent_fd, path.name)
        if (
            first_snapshot != final_snapshot
            or _metadata_signature(opened) != _metadata_signature(root_before)
            or _metadata_signature(root_before) != _metadata_signature(root_after)
            or _metadata_signature(root_after) != _metadata_signature(rebound_root)
            or _mount_id_for_fd(root_fd) != root_mount_id
        ):
            raise RuntimeError(f"{label} changed while it was verified")
        _revalidate_absolute_directory(path.parent, parent_fd, label=f"{label} parent")
    finally:
        os.close(root_fd)
        os.close(parent_fd)


def _paper_fresh_root_uid() -> int | None:
    if os.geteuid() != 0:
        return os.geteuid()
    try:
        return pwd.getpwnam(_PAPER_RUNTIME_USER).pw_uid
    except KeyError:
        # Unit-test/minimal-container roots may not provision the deployment
        # identity. The operational reset checks it before receipt creation;
        # here all paper roots must still agree on one observed owner.
        return None


def _verify_fresh_roots(roots: Mapping[str, Mapping[str, str]]) -> None:
    paper_uid = _paper_fresh_root_uid()
    observed_paper_uid: int | None = None
    for environment in ROOT_ENVIRONMENTS:
        for kind in ROOT_KINDS:
            path = Path(roots[environment][kind])
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ValueError(f"fresh {environment} {kind} root is unavailable") from exc
            if environment == "demo":
                expected_uid = os.geteuid()
            elif paper_uid is not None:
                expected_uid = paper_uid
            else:
                if observed_paper_uid is None:
                    observed_paper_uid = metadata.st_uid
                expected_uid = observed_paper_uid
            _verify_fresh_root(
                path,
                environment=environment,
                kind=kind,
                expected_uid=expected_uid,
            )


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reset archive manifest is not UTF-8") from exc
    singletons: dict[str, str] = {}
    repeated: dict[str, list[str]] = {key: [] for key in _REPEATED_MANIFEST_KEYS}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or "=" not in line:
            raise ValueError(f"reset archive manifest line {number} is malformed")
        key, value = line.split("=", 1)
        if key in repeated:
            repeated[key].append(value)
        elif key in _SINGLETON_MANIFEST_KEYS:
            if key in singletons:
                raise ValueError(f"reset archive manifest repeats {key}")
            singletons[key] = value
        else:
            raise ValueError(f"reset archive manifest has unknown key {key!r}")
    missing = set(_SINGLETON_MANIFEST_KEYS) - set(singletons)
    if missing:
        raise ValueError("reset archive manifest lacks required keys: " + ", ".join(sorted(missing)))
    stamp = singletons["ledger_reset_utc"]
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp) is None:
        raise ValueError("reset archive manifest timestamp is invalid")

    def flag(key: str) -> bool:
        raw = singletons[key]
        if raw not in {"0", "1"}:
            raise ValueError(f"reset archive manifest {key} must be 0 or 1")
        return raw == "1"

    sleeves = singletons["sleeves"].split()
    if not sleeves or len(sleeves) != len(set(sleeves)):
        raise ValueError("reset archive manifest sleeves are invalid")
    active_before = singletons["active_before"].split()
    if len(active_before) != len(set(active_before)):
        raise ValueError("reset archive manifest repeats active units")
    for key, values in repeated.items():
        if len(values) != len(set(values)):
            raise ValueError(f"reset archive manifest repeats {key}")
    return {
        "ledger_reset_utc": stamp,
        "git_head": _full_commit(singletons["git_head"], label="reset manifest Git head"),
        "sleeves": sleeves,
        "include_reports": flag("include_reports"),
        "include_caches": flag("include_caches"),
        "leave_stopped": flag("leave_stopped"),
        "env_file": singletons["env_file"],
        "account_env_file": singletons["account_env_file"],
        "paper_account_env_file": singletons["paper_account_env_file"],
        "demo_account_lease_path": singletons["demo_account_lease_path"],
        "demo_boundary": singletons["demo_boundary"],
        "paper_boundary": singletons["paper_boundary"],
        "active_before": active_before,
        "account_epoch_targets": repeated["account_epoch_target"],
        "archived_targets": repeated["target"],
        "preserved_risk_state": repeated["preserved_risk_state"],
    }


def _archive_bundle(
    archive_path: str | Path,
    sidecar_path: str | Path,
    *,
    account_epoch_targets: Sequence[str],
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> tuple[FileIdentity, FileIdentity, str, dict[str, Any], dict[str, bool]]:
    if archive_snapshot is not None and archive_snapshot.path != Path(archive_path).expanduser().absolute():
        raise ValueError("reset archive snapshot path differs")
    if sidecar_snapshot is None:
        sidecar_snapshot = _private_snapshot(
            sidecar_path,
            label="reset SHA-256 sidecar",
            max_bytes=_MAX_SIDECAR_SIZE,
        )
    elif sidecar_snapshot.path != Path(sidecar_path).expanduser().absolute():
        raise ValueError("reset SHA-256 sidecar snapshot path differs")
    checked_snapshots = [("reset SHA-256 sidecar", sidecar_snapshot)]
    if archive_snapshot is not None:
        checked_snapshots.append(("reset archive", archive_snapshot))
    for label, source in checked_snapshots:
        if source.mode != 0o600 or source.uid != os.geteuid() or source.nlink != 1:
            raise ValueError(f"{label} must be verifier-owned mode 0600 and not hard-linked")
    if sidecar_snapshot.size > _MAX_SIDECAR_SIZE:
        raise ValueError(f"reset SHA-256 sidecar exceeds the {_MAX_SIDECAR_SIZE}-byte size limit")

    archive, manifest_data, presence = _stream_archive(
        archive_path,
        account_epoch_targets=account_epoch_targets,
    )
    if archive_snapshot is not None and _identity_from_snapshot(archive_snapshot) != archive:
        raise RuntimeError("reset archive changed after its supplied snapshot")
    sidecar = _identity_from_snapshot(sidecar_snapshot)
    try:
        sidecar_text = sidecar_snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reset SHA-256 sidecar is unreadable") from exc
    expected_sidecar = f"{archive.sha256}  {Path(archive.path).name}\n"
    if sidecar_text != expected_sidecar:
        raise ValueError("reset SHA-256 sidecar does not exactly bind the archive")
    observed_sidecar = _file_identity(
        sidecar.path,
        label="reset SHA-256 sidecar",
        max_bytes=_MAX_SIDECAR_SIZE,
    )
    if observed_sidecar != sidecar:
        raise RuntimeError("reset archive bundle changed while it was verified")
    manifest = _parse_manifest(manifest_data)
    return (
        archive,
        sidecar,
        hashlib.sha256(manifest_data).hexdigest(),
        manifest,
        presence,
    )


def _validate_sequences(
    *,
    sleeves: Sequence[str],
    managed_units: Sequence[str],
    active_before: Sequence[str],
    inactive_after: Sequence[str],
    leave_stopped: bool,
) -> tuple[list[str], list[str], list[str], list[str]]:
    sleeve_list = list(sleeves)
    managed = list(managed_units)
    active = list(active_before)
    inactive = list(inactive_after)
    if not sleeve_list or len(sleeve_list) != len(set(sleeve_list)):
        raise ValueError("reset sleeves must be nonempty and unique")
    if tuple(sleeve_list) not in {
        # Every non-empty subset of the managed strategy ledgers, in the
        # canonical long -> continuous -> carry order the reset script emits.
        ("long",),
        ("continuous",),
        ("carry",),
        ("long", "continuous"),
        ("long", "carry"),
        ("continuous", "carry"),
        ("long", "continuous", "carry"),
        # Schema-v1 receipts written before the shared compatibility sleeve
        # was retired remain historical evidence and must stay readable.
        ("long", "continuous", "retire-shared-compat"),
    }:
        raise ValueError("reset sleeves are not one canonical reset selection")
    if tuple(managed) != MANAGED_UNITS:
        raise ValueError("reset receipt requires the exact managed-unit order")
    if len(active) != len(set(active)) or any(unit not in managed for unit in active):
        raise ValueError("active-before units are invalid")
    if active != [unit for unit in managed if unit in set(active)]:
        raise ValueError("active-before units are not in managed-unit order")
    if leave_stopped:
        if inactive != managed:
            raise ValueError("leave-stopped reset must verify every managed unit inactive")
    elif inactive:
        raise ValueError("non-leave-stopped reset cannot claim all units inactive")
    return sleeve_list, managed, active, inactive


def build_account_reset_receipt(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    started_ts_ns: int,
    finished_ts_ns: int,
    sleeves: Sequence[str],
    include_reports: bool,
    include_caches: bool,
    leave_stopped: bool,
    account_epoch_roots: Mapping[str, Mapping[str, str | Path]],
    managed_units: Sequence[str],
    active_before: Sequence[str],
    inactive_after: Sequence[str],
    archive_path: str | Path,
    sha256_sidecar_path: str | Path,
) -> dict[str, Any]:
    """Build a passed receipt after the reset has completed every safety check."""

    repository = _repository(repository_root)
    candidate = _full_commit(candidate_commit, label="reset candidate commit")
    if _git_head(repository) != candidate:
        raise ValueError("repository HEAD changed during the account reset")
    started = int(started_ts_ns)
    finished = int(finished_ts_ns)
    if started <= 0 or finished < started:
        raise ValueError("reset receipt timestamps are invalid")
    absolute_roots, relative_roots = _normalize_roots(account_epoch_roots, repository_root=repository)
    _verify_fresh_roots(absolute_roots)
    sleeve_list, managed, active, inactive = _validate_sequences(
        sleeves=sleeves,
        managed_units=managed_units,
        active_before=active_before,
        inactive_after=inactive_after,
        leave_stopped=leave_stopped,
    )
    expected_targets = [relative_roots[environment][kind] for environment in ROOT_ENVIRONMENTS for kind in ROOT_KINDS]
    archive, sidecar, manifest_hash, manifest, presence = _archive_bundle(
        archive_path,
        sha256_sidecar_path,
        account_epoch_targets=expected_targets,
    )
    if manifest["git_head"] != candidate:
        raise ValueError("reset archive manifest names another candidate commit")
    if manifest["sleeves"] != sleeve_list:
        raise ValueError("reset archive manifest names another sleeve selection")
    if manifest["include_reports"] is not bool(include_reports) or manifest["include_caches"] is not bool(
        include_caches
    ):
        raise ValueError("reset archive manifest option flags differ")
    if manifest["leave_stopped"] is not bool(leave_stopped):
        raise ValueError("reset archive manifest leave-stopped flag differs")
    if manifest["account_epoch_targets"] != expected_targets:
        raise ValueError("reset archive manifest names another account epoch root set")
    if manifest["active_before"] != active:
        raise ValueError("reset archive manifest active-before units differ")
    if manifest["demo_boundary"] != DEMO_BOUNDARY or manifest["paper_boundary"] != PAPER_BOUNDARY:
        raise ValueError("reset archive manifest boundary facts differ")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "status": "passed",
        "started_ts_ns": started,
        "finished_ts_ns": finished,
        "repository": {
            "root": str(repository),
            "candidate_commit": candidate,
        },
        "reset": {
            "mode": "execute",
            "sleeves": sleeve_list,
            "include_reports": bool(include_reports),
            "include_caches": bool(include_caches),
            "leave_stopped": bool(leave_stopped),
            "boundaries": {
                "demo": DEMO_BOUNDARY,
                "paper": PAPER_BOUNDARY,
            },
            "account_epoch_roots": absolute_roots,
            "account_epoch_relative_roots": relative_roots,
            "fresh_roots_verified": True,
        },
        "services": {
            "managed_units": managed,
            "active_before": active,
            "inactive_after": inactive,
            "restored_active_after": [] if leave_stopped else active,
            "all_managed_units_stopped_verified": bool(leave_stopped),
        },
        "archive": {
            "file": archive.to_dict(),
            "sha256_sidecar": sidecar.to_dict(),
            "embedded_manifest_sha256": manifest_hash,
            "embedded_manifest": manifest,
            "archived_account_epoch_presence": presence,
        },
        "execution_authorization": "not_granted",
        "limitations": list(LIMITATIONS),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _roots_from_payload(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("reset receipt account epoch roots are malformed")
    output: dict[str, dict[str, str]] = {}
    if set(value) != set(ROOT_ENVIRONMENTS):
        raise ValueError("reset receipt account epoch environments differ")
    for environment in ROOT_ENVIRONMENTS:
        raw = value[environment]
        if not isinstance(raw, Mapping) or set(raw) != set(ROOT_KINDS):
            raise ValueError(f"reset receipt {environment} roots are malformed")
        output[environment] = {kind: str(raw.get(kind) or "") for kind in ROOT_KINDS}
    return output


def verify_account_reset_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_candidate_commit: str | None = None,
    expected_roots: Mapping[str, Mapping[str, str | Path]] | None = None,
    require_leave_stopped: bool = False,
    require_fresh_roots: bool = False,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    payload = dict(receipt)
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "status",
        "started_ts_ns",
        "finished_ts_ns",
        "repository",
        "reset",
        "services",
        "archive",
        "execution_authorization",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("reset receipt has unexpected or missing fields")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or payload.get("status") != "passed"
    ):
        raise ValueError("reset receipt identity or status is invalid")
    if payload.get("artifact_sha256") != _self_hash(payload):
        raise ValueError("reset receipt self-hash is invalid")
    if payload.get("execution_authorization") != "not_granted":
        raise ValueError("reset receipt cannot grant execution authority")
    if payload.get("limitations") != list(LIMITATIONS):
        raise ValueError("reset receipt limitations differ")
    started = int(payload.get("started_ts_ns") or 0)
    finished = int(payload.get("finished_ts_ns") or 0)
    if started <= 0 or finished < started:
        raise ValueError("reset receipt timestamps are invalid")

    repository = payload.get("repository")
    if not isinstance(repository, Mapping) or set(repository) != {
        "root",
        "candidate_commit",
    }:
        raise ValueError("reset receipt repository binding is malformed")
    repository_root = _repository(str(repository.get("root") or ""))
    candidate = _full_commit(repository.get("candidate_commit"), label="reset receipt candidate")
    if expected_candidate_commit is not None and candidate != _full_commit(
        expected_candidate_commit, label="expected reset candidate"
    ):
        raise ValueError("reset receipt names another candidate commit")

    reset = payload.get("reset")
    expected_reset_keys = {
        "mode",
        "sleeves",
        "include_reports",
        "include_caches",
        "leave_stopped",
        "boundaries",
        "account_epoch_roots",
        "account_epoch_relative_roots",
        "fresh_roots_verified",
    }
    if not isinstance(reset, Mapping) or set(reset) != expected_reset_keys:
        raise ValueError("reset receipt operation binding is malformed")
    if reset.get("mode") != "execute" or reset.get("fresh_roots_verified") is not True:
        raise ValueError("reset receipt does not describe a completed execute reset")
    for flag in ("include_reports", "include_caches", "leave_stopped"):
        if type(reset.get(flag)) is not bool:
            raise ValueError(f"reset receipt {flag} must be boolean")
    sleeves = reset.get("sleeves")
    if not isinstance(sleeves, list) or any(not isinstance(item, str) for item in sleeves):
        raise ValueError("reset receipt sleeves are malformed")
    boundaries = reset.get("boundaries")
    if boundaries != {"demo": DEMO_BOUNDARY, "paper": PAPER_BOUNDARY}:
        raise ValueError("reset receipt boundary facts differ")
    roots = _roots_from_payload(reset.get("account_epoch_roots"))
    normalized_roots, relative_roots = _normalize_roots(roots, repository_root=repository_root)
    if roots != normalized_roots or reset.get("account_epoch_relative_roots") != relative_roots:
        raise ValueError("reset receipt root identities do not normalize")
    if expected_roots is not None:
        expected_absolute, expected_relative = _normalize_roots(expected_roots, repository_root=repository_root)
        if roots != expected_absolute or relative_roots != expected_relative:
            raise ValueError("reset receipt names another account epoch root set")
    if require_fresh_roots:
        _verify_fresh_roots(roots)

    services = payload.get("services")
    expected_service_keys = {
        "managed_units",
        "active_before",
        "inactive_after",
        "restored_active_after",
        "all_managed_units_stopped_verified",
    }
    if not isinstance(services, Mapping) or set(services) != expected_service_keys:
        raise ValueError("reset receipt service state is malformed")
    managed = services.get("managed_units")
    active = services.get("active_before")
    inactive = services.get("inactive_after")
    restored = services.get("restored_active_after")
    if not all(isinstance(value, list) for value in (managed, active, inactive, restored)):
        raise ValueError("reset receipt service lists are malformed")
    managed_list = cast(list[Any], managed)
    active_list = cast(list[Any], active)
    inactive_list = cast(list[Any], inactive)
    restored_list = cast(list[Any], restored)
    if any(
        not isinstance(item, str)
        for values in (managed_list, active_list, inactive_list, restored_list)
        for item in values
    ):
        raise ValueError("reset receipt service list entries must be strings")
    typed_managed = cast(list[str], managed_list)
    typed_active = cast(list[str], active_list)
    typed_inactive = cast(list[str], inactive_list)
    typed_restored = cast(list[str], restored_list)
    leave_stopped = bool(reset.get("leave_stopped"))
    _validate_sequences(
        sleeves=sleeves,
        managed_units=typed_managed,
        active_before=typed_active,
        inactive_after=typed_inactive,
        leave_stopped=leave_stopped,
    )
    expected_restored = [] if leave_stopped else typed_active
    if typed_restored != expected_restored or services.get("all_managed_units_stopped_verified") is not leave_stopped:
        raise ValueError("reset receipt aggregate service state is inconsistent")
    if require_leave_stopped and not leave_stopped:
        raise ValueError("reset receipt did not leave every managed unit stopped")

    archive = payload.get("archive")
    expected_archive_keys = {
        "file",
        "sha256_sidecar",
        "embedded_manifest_sha256",
        "embedded_manifest",
        "archived_account_epoch_presence",
    }
    if not isinstance(archive, Mapping) or set(archive) != expected_archive_keys:
        raise ValueError("reset receipt archive binding is malformed")
    archive_identity = _identity_from_payload(archive.get("file"), label="reset archive")
    sidecar_identity = _identity_from_payload(archive.get("sha256_sidecar"), label="reset SHA-256 sidecar")
    expected_targets = [
        relative_roots[environment][kind]
        for environment in ROOT_ENVIRONMENTS
        for kind in ROOT_KINDS
    ]
    (
        observed_archive,
        observed_sidecar,
        manifest_hash,
        manifest,
        presence,
    ) = _archive_bundle(
        archive_identity.path,
        sidecar_identity.path,
        account_epoch_targets=expected_targets,
        archive_snapshot=archive_snapshot,
        sidecar_snapshot=sidecar_snapshot,
    )
    if observed_archive != archive_identity or observed_sidecar != sidecar_identity:
        raise ValueError("reset archive bundle changed after receipt creation")
    if (
        archive.get("embedded_manifest_sha256") != manifest_hash
        or archive.get("embedded_manifest") != manifest
        or archive.get("archived_account_epoch_presence") != presence
    ):
        raise ValueError("reset archive manifest changed after receipt creation")
    if (
        manifest["git_head"] != candidate
        or manifest["sleeves"] != sleeves
        or manifest["include_reports"] is not reset.get("include_reports")
        or manifest["include_caches"] is not reset.get("include_caches")
        or manifest["leave_stopped"] is not leave_stopped
        or manifest["active_before"] != typed_active
        or manifest["account_epoch_targets"] != expected_targets
        or manifest["demo_boundary"] != DEMO_BOUNDARY
        or manifest["paper_boundary"] != PAPER_BOUNDARY
    ):
        raise ValueError("reset receipt does not reproduce from its archive manifest")
    return payload


def _load_account_reset_receipt_snapshot(
    snapshot: StableFileSnapshot,
    *,
    expected_mode: int,
    expected_candidate_commit: str | None = None,
    expected_roots: Mapping[str, Mapping[str, str | Path]] | None = None,
    require_leave_stopped: bool = False,
    require_fresh_roots: bool = False,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot.mode != expected_mode or snapshot.uid != os.geteuid() or snapshot.nlink != 1:
        raise ValueError(
            f"account reset receipt must be verifier-owned mode {expected_mode:04o} and not hard-linked"
        )
    if snapshot.size > _MAX_RECEIPT_SIZE:
        raise ValueError(
            f"account reset receipt exceeds the {_MAX_RECEIPT_SIZE}-byte size limit"
        )
    try:
        value = json.loads(
            snapshot.data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"account reset receipt contains non-finite token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("account reset receipt is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("account reset receipt must be a JSON object")
    payload = verify_account_reset_receipt(
        value,
        expected_candidate_commit=expected_candidate_commit,
        expected_roots=expected_roots,
        require_leave_stopped=require_leave_stopped,
        require_fresh_roots=require_fresh_roots,
        archive_snapshot=archive_snapshot,
        sidecar_snapshot=sidecar_snapshot,
    )
    return payload


def load_account_reset_receipt(
    path: str | Path,
    *,
    expected_candidate_commit: str | None = None,
    expected_roots: Mapping[str, Mapping[str, str | Path]] | None = None,
    require_leave_stopped: bool = False,
    require_fresh_roots: bool = False,
    snapshot: StableFileSnapshot | None = None,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _private_snapshot(
            path,
            label="account reset receipt",
            max_bytes=_MAX_RECEIPT_SIZE,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("account reset receipt snapshot path differs")
    return _load_account_reset_receipt_snapshot(
        snapshot,
        expected_mode=0o600,
        expected_candidate_commit=expected_candidate_commit,
        expected_roots=expected_roots,
        require_leave_stopped=require_leave_stopped,
        require_fresh_roots=require_fresh_roots,
        archive_snapshot=archive_snapshot,
        sidecar_snapshot=sidecar_snapshot,
    )


def _receipt_output_path(path: str | Path) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("account reset receipt output must be an absolute non-symlink path")
    if output.name in {"", ".", ".."} or any(
        ord(character) < 32 or ord(character) == 127 for character in output.name
    ):
        raise ValueError("account reset receipt output basename is invalid")
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("account reset receipt parent must already exist") from exc
    return parent / output.name


def _open_receipt_output_parent(
    path: str | Path,
    *,
    forbidden_roots: Sequence[str | Path],
) -> tuple[Path, int]:
    output = _receipt_output_path(path)
    try:
        parent_fd = _open_absolute_directory(output.parent)
    except OSError as exc:
        raise ValueError("account reset receipt parent must be a real directory") from exc
    try:
        _revalidate_absolute_directory(
            output.parent,
            parent_fd,
            label="account reset receipt parent",
        )
        try:
            _entry_metadata(parent_fd, output.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("account reset receipt output cannot be inspected") from exc
        else:
            raise FileExistsError(f"account reset receipt already exists: {output}")
        for raw in forbidden_roots:
            root = Path(raw).expanduser().resolve(strict=False)
            if output == root or root in output.parents:
                raise ValueError("account reset receipt output cannot be inside a reset root")
        return output, parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def validate_account_reset_receipt_output(
    path: str | Path,
    *,
    forbidden_roots: Sequence[str | Path] = (),
) -> Path:
    resolved, parent_fd = _open_receipt_output_parent(
        path,
        forbidden_roots=forbidden_roots,
    )
    os.close(parent_fd)
    return resolved


def _revalidate_receipt_leaf(
    parent_fd: int,
    *,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
    expected_mode: int,
    expected_nlink: int,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = _entry_metadata(parent_fd, name)
    if (
        (opened.st_dev, opened.st_ino) != expected
        or (current.st_dev, current.st_ino) != expected
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != expected_nlink
        or current.st_nlink != expected_nlink
        or opened.st_uid != os.geteuid()
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != expected_mode
        or stat.S_IMODE(current.st_mode) != expected_mode
    ):
        raise RuntimeError("account reset receipt path changed during publication")
    return opened


def _receipt_snapshot_from_descriptor(
    output: Path,
    *,
    parent_fd: int,
    descriptor: int,
    expected: tuple[int, int],
    expected_mode: int,
) -> StableFileSnapshot:
    before = _revalidate_receipt_leaf(
        parent_fd,
        name=output.name,
        descriptor=descriptor,
        expected=expected,
        expected_mode=expected_mode,
        expected_nlink=1,
    )
    if before.st_size > _MAX_RECEIPT_SIZE:
        raise ValueError(
            f"account reset receipt exceeds the {_MAX_RECEIPT_SIZE}-byte size limit"
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, (_MAX_RECEIPT_SIZE - size) + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > _MAX_RECEIPT_SIZE:
            raise ValueError(
                f"account reset receipt exceeds the {_MAX_RECEIPT_SIZE}-byte size limit"
            )
        chunks.append(chunk)
    after = _revalidate_receipt_leaf(
        parent_fd,
        name=output.name,
        descriptor=descriptor,
        expected=expected,
        expected_mode=expected_mode,
        expected_nlink=1,
    )
    if _metadata_signature(before) != _metadata_signature(after) or size != after.st_size:
        raise RuntimeError("account reset receipt changed during final verification")
    return StableFileSnapshot(path=output, data=b"".join(chunks), metadata=after)


def _require_current_candidate(payload: Mapping[str, Any]) -> None:
    repository = cast(Mapping[str, Any], payload["repository"])
    root = _repository(str(repository["root"]))
    candidate = _full_commit(
        repository["candidate_commit"],
        label="reset receipt candidate",
    )
    if _git_head(root) != candidate:
        raise ValueError("repository HEAD changed during account reset receipt publication")


def _delete_created_receipt(
    parent_fd: int,
    *,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(parent_fd, name)
    except FileNotFoundError:
        return
    if (
        (opened.st_dev, opened.st_ino) != expected
        or (current.st_dev, current.st_ino) != expected
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
    ):
        return
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def write_account_reset_receipt(
    path: str | Path,
    receipt: Mapping[str, Any],
    *,
    final_publication_check: Callable[[], None] | None = None,
) -> Path:
    """Validate privately, then atomically publish one crash-safe passed receipt."""

    payload = verify_account_reset_receipt(receipt)
    reset = payload["reset"]
    roots = _roots_from_payload(reset["account_epoch_roots"])
    forbidden_roots = [
        roots[environment][kind]
        for environment in ROOT_ENVIRONMENTS
        for kind in ROOT_KINDS
    ]
    output, parent_fd = _open_receipt_output_parent(
        path,
        forbidden_roots=forbidden_roots,
    )
    data = canonical_json(payload) + b"\n"
    if len(data) > _MAX_RECEIPT_SIZE:
        os.close(parent_fd)
        raise ValueError(
            f"account reset receipt exceeds the {_MAX_RECEIPT_SIZE}-byte size limit"
        )
    descriptor = -1
    expected: tuple[int, int] | None = None
    staging_name = ""
    staging_exists = False
    published = False
    try:
        _require_current_candidate(payload)
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        try:
            flags |= os.O_NOFOLLOW
        except AttributeError as exc:  # pragma: no cover - supported runtime platforms are POSIX
            raise RuntimeError("account reset receipt publication requires O_NOFOLLOW") from exc
        for _attempt in range(100):
            candidate_name = f".account-reset-receipt-stage-{secrets.token_hex(16)}"
            try:
                descriptor = os.open(candidate_name, flags, 0o400, dir_fd=parent_fd)
            except FileExistsError:
                continue
            staging_name = candidate_name
            staging_exists = True
            break
        if descriptor < 0:
            raise RuntimeError("account reset receipt staging namespace is exhausted")
        created = os.fstat(descriptor)
        expected = (created.st_dev, created.st_ino)
        os.fchmod(descriptor, 0o400)
        _revalidate_receipt_leaf(
            parent_fd,
            name=staging_name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o400,
            expected_nlink=1,
        )
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("account reset receipt write made no progress")
            offset += written
        os.fsync(descriptor)
        os.fsync(parent_fd)
        staging_path = output.parent / staging_name
        snapshot = _receipt_snapshot_from_descriptor(
            staging_path,
            parent_fd=parent_fd,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o400,
        )
        _load_account_reset_receipt_snapshot(
            snapshot,
            expected_mode=0o400,
            expected_candidate_commit=payload["repository"]["candidate_commit"],
            expected_roots=roots,
            require_leave_stopped=bool(reset["leave_stopped"]),
            require_fresh_roots=True,
        )
        if final_publication_check is not None:
            final_publication_check()
        _require_current_candidate(payload)
        _revalidate_absolute_directory(
            output.parent,
            parent_fd,
            label="account reset receipt parent",
        )
        try:
            os.link(
                staging_name,
                output.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FileExistsError(f"account reset receipt already exists: {output}") from exc
        published = True
        staging = _entry_metadata(parent_fd, staging_name)
        final = _entry_metadata(parent_fd, output.name)
        opened = os.fstat(descriptor)
        if (
            (staging.st_dev, staging.st_ino) != expected
            or (final.st_dev, final.st_ino) != expected
            or (opened.st_dev, opened.st_ino) != expected
            or staging.st_nlink != 2
            or final.st_nlink != 2
            or opened.st_nlink != 2
            or stat.S_IMODE(staging.st_mode) != 0o400
            or stat.S_IMODE(final.st_mode) != 0o400
            or stat.S_IMODE(opened.st_mode) != 0o400
        ):
            raise RuntimeError("account reset receipt link changed during publication")
        os.fsync(parent_fd)
        os.unlink(staging_name, dir_fd=parent_fd)
        staging_exists = False
        os.fsync(parent_fd)
        _revalidate_receipt_leaf(
            parent_fd,
            name=output.name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o400,
            expected_nlink=1,
        )
        linked_snapshot = _receipt_snapshot_from_descriptor(
            output,
            parent_fd=parent_fd,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o400,
        )
        _load_account_reset_receipt_snapshot(
            linked_snapshot,
            expected_mode=0o400,
            expected_candidate_commit=payload["repository"]["candidate_commit"],
            expected_roots=roots,
            require_leave_stopped=bool(reset["leave_stopped"]),
            require_fresh_roots=True,
        )
        if final_publication_check is not None:
            final_publication_check()
        _require_current_candidate(payload)
        _revalidate_absolute_directory(
            output.parent,
            parent_fd,
            label="account reset receipt parent",
        )
        _revalidate_receipt_leaf(
            parent_fd,
            name=output.name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o400,
            expected_nlink=1,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        _revalidate_absolute_directory(
            output.parent,
            parent_fd,
            label="account reset receipt parent",
        )
        _revalidate_receipt_leaf(
            parent_fd,
            name=output.name,
            descriptor=descriptor,
            expected=expected,
            expected_mode=0o600,
            expected_nlink=1,
        )
    except BaseException:
        if descriptor >= 0 and expected is not None:
            for name, exists in ((output.name, published), (staging_name, staging_exists)):
                if not name or not exists:
                    continue
                try:
                    _delete_created_receipt(
                        parent_fd,
                        name=name,
                        descriptor=descriptor,
                        expected=expected,
                    )
                except OSError:
                    pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or preflight a source-reopening account reset receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--forbidden-root", action="append", default=[])

    create = subparsers.add_parser("create")
    create.add_argument("--repository-root", required=True)
    create.add_argument("--candidate-commit", required=True)
    create.add_argument("--started-ts-ns", required=True, type=int)
    create.add_argument("--archive", required=True)
    create.add_argument("--sha256-sidecar", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--systemctl-bin", required=True)
    create.add_argument("--sleeve", action="append", required=True)
    create.add_argument("--include-reports", action="store_true")
    create.add_argument("--include-caches", action="store_true")
    create.add_argument("--leave-stopped", action="store_true")
    create.add_argument("--demo-account-root", required=True)
    create.add_argument("--demo-inbox-root", required=True)
    create.add_argument("--demo-capture-root", required=True)
    create.add_argument("--paper-account-root", required=True)
    create.add_argument("--paper-inbox-root", required=True)
    create.add_argument("--paper-capture-root", required=True)
    create.add_argument("--managed-unit", action="append", required=True)
    create.add_argument("--active-before-unit", action="append", default=[])
    create.add_argument("--inactive-after-unit", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            output = validate_account_reset_receipt_output(args.output, forbidden_roots=args.forbidden_root)
            print(json.dumps({"output": str(output), "available": True}, sort_keys=True))
            return 0
        roots = {
            "demo": {
                "account": args.demo_account_root,
                "inbox": args.demo_inbox_root,
                "capture": args.demo_capture_root,
            },
            "paper": {
                "account": args.paper_account_root,
                "inbox": args.paper_inbox_root,
                "capture": args.paper_capture_root,
            },
        }
        observed_inactive = _observe_managed_units_inactive(
            args.systemctl_bin,
            args.managed_unit,
        )
        if args.inactive_after_unit and args.inactive_after_unit != observed_inactive:
            raise ValueError("caller-supplied inactive units differ from observed systemd state")
        payload = build_account_reset_receipt(
            repository_root=args.repository_root,
            candidate_commit=args.candidate_commit,
            started_ts_ns=args.started_ts_ns,
            finished_ts_ns=time.time_ns(),
            sleeves=args.sleeve,
            include_reports=args.include_reports,
            include_caches=args.include_caches,
            leave_stopped=args.leave_stopped,
            account_epoch_roots=roots,
            managed_units=args.managed_unit,
            active_before=args.active_before_unit,
            inactive_after=observed_inactive,
            archive_path=args.archive,
            sha256_sidecar_path=args.sha256_sidecar,
        )
        def final_systemd_check() -> None:
            _observe_managed_units_inactive(
                args.systemctl_bin,
                args.managed_unit,
            )

        output = write_account_reset_receipt(
            args.output,
            payload,
            final_publication_check=final_systemd_check,
        )
        print(
            json.dumps(
                {
                    "output": str(output),
                    "artifact_sha256": payload["artifact_sha256"],
                    "status": payload["status"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
