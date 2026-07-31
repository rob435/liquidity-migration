"""Descriptor-bound archive publication for the destructive account reset.

Creates a private archive inode exclusively, streams ``tar`` into that open
descriptor, publishes a digest sidecar, and can revalidate the exact artifacts
just before live state is removed. Service quiescence and the account leases
belong to the reset shell.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from liquidity_migration.ops.reset_path_safety import preflight_reset_targets


_READ_SIZE = 1024 * 1024
_SAFE_STEM = re.compile(r"ledger-reset-[0-9]{8}T[0-9]{6}Z(?:-[A-Za-z0-9][A-Za-z0-9._-]{0,63})?")
_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None
_TRUSTED_EXECUTABLE_PATH = "/usr/bin:/bin"


@dataclass(frozen=True, slots=True)
class ResetArchive:
    path: Path
    sidecar_path: Path
    sha256: str
    size_bytes: int
    device: int
    inode: int


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    try:
        return os.O_RDONLY | sum(getattr(os, name) for name in required)
    except AttributeError as exc:  # pragma: no cover - deployed only on POSIX
        raise RuntimeError("reset archive publication requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _regular_open_flags(*, writable: bool, exclusive: bool = False) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    try:
        flags |= os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - deployed only on POSIX
        raise RuntimeError("reset archive publication requires O_NOFOLLOW") from exc
    if exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


def _absolute(path: str | Path, *, repository: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = repository / candidate
    absolute = Path(os.path.abspath(candidate))
    if any(ord(character) < 32 or ord(character) == 127 for character in str(absolute)):
        raise ValueError("reset archive paths must not contain control characters")
    return absolute


def _trusted_directory(metadata: os.stat_result, *, path: Path, final: bool) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"reset archive directory component must be real: {path}")
    trusted_owners = {0, os.geteuid()}
    permissions = stat.S_IMODE(metadata.st_mode)
    writable_by_others = bool(permissions & 0o022)
    trusted_sticky_ancestor = (
        not final
        and metadata.st_uid == 0
        and bool(permissions & stat.S_ISVTX)
    )
    if (
        metadata.st_uid not in trusted_owners
        or (writable_by_others and not trusted_sticky_ancestor)
    ):
        raise ValueError(f"reset archive directory component is not trusted: {path}")
    if final and metadata.st_uid != os.geteuid():
        raise ValueError(f"reset archive directory must be owned by the reset user: {path}")


def _open_archive_directory(
    path: Path,
    *,
    create: bool,
) -> int:
    """Open ``path`` safely; newly created leaves use 0700, existing modes stay intact."""

    if path == Path("/"):
        raise ValueError("filesystem root cannot be used as the reset archive directory")
    flags = _directory_flags()
    descriptor = os.open("/", flags)
    current = Path("/")
    parts = path.parts[1:]
    try:
        for index, name in enumerate(parts):
            final = index == len(parts) - 1
            current /= name
            created_final = False
            if final and create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                    created_final = True
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(f"reset archive directory cannot be created safely: {current}") from exc
            try:
                observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child = os.open(name, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(f"reset archive directory component is unavailable: {current}") from exc
            try:
                opened = os.fstat(child)
                current_entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                identity = (observed.st_dev, observed.st_ino)
                if (
                    (opened.st_dev, opened.st_ino) != identity
                    or (current_entry.st_dev, current_entry.st_ino) != identity
                ):
                    raise RuntimeError(f"reset archive directory changed while it was opened: {current}")
                _trusted_directory(opened, path=current, final=final)
                _trusted_directory(current_entry, path=current, final=final)
                if final and created_final and stat.S_IMODE(opened.st_mode) != 0o700:
                    os.fchmod(child, 0o700)
                    os.fsync(child)
                    normalized = os.fstat(child)
                    normalized_entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        (normalized.st_dev, normalized.st_ino) != identity
                        or (normalized_entry.st_dev, normalized_entry.st_ino) != identity
                        or stat.S_IMODE(normalized.st_mode) != 0o700
                        or stat.S_IMODE(normalized_entry.st_mode) != 0o700
                    ):
                        raise RuntimeError(f"reset archive directory changed while it was normalized: {current}")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_archive_leaf(
    parent_fd: int,
    *,
    parent_path: Path,
    name: str,
    create: bool,
) -> int:
    """Open one archive leaf relative to its already validated parent."""

    if name in {"", ".", ".."} or "/" in name:
        raise ValueError("reset archive directory leaf is invalid")
    created = False
    path = parent_path / name
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError(f"reset archive directory cannot be created safely: {path}") from exc
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"reset archive directory component is unavailable: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (observed.st_dev, observed.st_ino)
        if (
            (opened.st_dev, opened.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
        ):
            raise RuntimeError(f"reset archive directory changed while it was opened: {path}")
        _trusted_directory(opened, path=path, final=True)
        _trusted_directory(current, path=path, final=True)
        if created and stat.S_IMODE(opened.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            normalized = os.fstat(descriptor)
            normalized_entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (normalized.st_dev, normalized.st_ino) != identity
                or (normalized_entry.st_dev, normalized_entry.st_ino) != identity
                or stat.S_IMODE(normalized.st_mode) != 0o700
                or stat.S_IMODE(normalized_entry.st_mode) != 0o700
            ):
                raise RuntimeError(f"reset archive directory changed while it was normalized: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mount_id_for_fd(descriptor: int) -> int | None:
    """Return Linux's mount identity so same-device bind mounts stay visible."""

    if _LINUX_FDINFO_ROOT is None:
        return None
    try:
        raw = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Linux mount identity cannot be read for archive descriptor {descriptor}"
        ) from exc
    values = [
        line.partition(":")[2].strip()
        for line in raw.splitlines()
        if line.partition(":")[0] == "mnt_id"
    ]
    if len(values) != 1:
        raise RuntimeError(f"Linux mount identity is invalid for archive descriptor {descriptor}")
    try:
        value = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(
            f"Linux mount identity is invalid for archive descriptor {descriptor}"
        ) from exc
    if value < 0:
        raise RuntimeError(f"Linux mount identity is invalid for archive descriptor {descriptor}")
    return value


def _validate_archive_mount_relation(
    *,
    data_directory: Path,
    archive_directory: Path,
    data_fd: int,
    archive_fd: int,
) -> None:
    """Reject alternate mount views of the filesystem holding reset targets.

    A bind alias keeps ``st_dev`` but gets a distinct Linux mount ID, so
    refusing alternate mount views closes hidden same/nested/ancestor aliases
    while still allowing an archive directory on another filesystem.
    """

    try:
        data_metadata = os.fstat(data_fd)
        archive_metadata = os.fstat(archive_fd)
    except OSError as exc:
        raise ValueError("reset archive mount relation cannot be inspected") from exc
    if (
        archive_metadata.st_dev == data_metadata.st_dev
        and _mount_id_for_fd(archive_fd) != _mount_id_for_fd(data_fd)
    ):
        raise ValueError(
            "reset archive directory must not be an alternate mount view of the reset data filesystem "
            f"(archive {archive_directory}, data {data_directory})"
        )


def _require_private_regular(metadata: os.stat_result, *, label: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError(f"{label} must be reset-user-owned mode 0600, regular, and not hard-linked")


def _entry_identity(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _revalidate_open_file(
    directory_fd: int,
    *,
    name: str,
    descriptor: int,
    expected: tuple[int, int],
    label: str,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = _entry_identity(directory_fd, name)
    if (opened.st_dev, opened.st_ino) != expected or (current.st_dev, current.st_ino) != expected:
        raise RuntimeError(f"{label} path changed during publication")
    _require_private_regular(opened, label=label)
    _require_private_regular(current, label=label)
    return opened


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while publishing reset archive metadata")
        view = view[written:]


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _READ_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _repository(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("repository root must be one absolute non-symlink directory")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository root must be one directory")
    return root


def _archive_targets(repository: Path, targets: Iterable[str | Path]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in targets:
        candidate = Path(raw)
        if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError(f"reset archive target must be a normalized relative path: {raw}")
        if candidate.parts[0] != "data":
            raise ValueError(f"reset archive target must stay below data/: {raw}")
        absolute = repository / candidate
        try:
            absolute.lstat()
        except OSError as exc:
            raise ValueError(f"reset archive target is unavailable: {candidate}") from exc
        normalized.append(str(candidate))
    if len(normalized) != len(set(normalized)):
        raise ValueError("reset archive targets must be distinct")
    return tuple(normalized)


def create_reset_archive(
    *,
    repository: str | Path,
    archive_directory: str | Path,
    stem: str,
    manifest: str | Path,
    targets: Sequence[str | Path],
) -> ResetArchive:
    """Create and durably publish one archive without reopening its output path."""

    root = _repository(repository)
    if _SAFE_STEM.fullmatch(stem) is None:
        raise ValueError("reset archive stem is invalid")
    manifest_path = Path(manifest).expanduser()
    if not manifest_path.is_absolute() or manifest_path.is_symlink():
        raise ValueError("reset archive manifest must be one absolute non-symlink path")
    manifest_name = manifest_path.name
    if manifest_name.startswith("-") or any(
        ord(character) < 32 or ord(character) == 127 for character in manifest_name
    ):
        raise ValueError("reset archive manifest basename is invalid")
    manifest_metadata = manifest_path.stat()
    _require_private_regular(manifest_metadata, label="reset archive manifest")
    selected_targets = _archive_targets(root, targets)
    preflight_reset_targets(
        root / "data",
        tuple(root / target for target in selected_targets),
    )
    directory_path = _absolute(archive_directory, repository=root)
    for target in selected_targets:
        target_path = root / target
        if (
            directory_path == target_path
            or directory_path in target_path.parents
            or target_path in directory_path.parents
        ):
            raise ValueError("reset archive directory and targets must be disjoint")
    if directory_path == Path("/"):
        raise ValueError("filesystem root cannot be used as the reset archive directory")
    data_directory = root / "data"
    data_fd = _open_archive_directory(data_directory, create=False)
    parent_path = directory_path.parent
    parent_fd = -1
    directory_fd = -1
    try:
        if parent_path == Path("/"):
            parent_fd = os.open("/", _directory_flags())
        else:
            parent_fd = _open_archive_directory(parent_path, create=False)
        _validate_archive_mount_relation(
            data_directory=data_directory,
            archive_directory=parent_path,
            data_fd=data_fd,
            archive_fd=parent_fd,
        )
        directory_fd = _open_archive_leaf(
            parent_fd,
            parent_path=parent_path,
            name=directory_path.name,
            create=True,
        )
        _validate_archive_mount_relation(
            data_directory=data_directory,
            archive_directory=directory_path,
            data_fd=data_fd,
            archive_fd=directory_fd,
        )
    except BaseException:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(data_fd)
    archive_fd = -1
    sidecar_fd = -1
    archive_name = ""
    sidecar_name = ""
    sidecar_created = False
    try:
        for counter in range(1, 10001):
            suffix = "" if counter == 1 else f"-{counter}"
            candidate_name = f"{stem}{suffix}.tar.gz"
            try:
                archive_fd = os.open(
                    candidate_name,
                    _regular_open_flags(writable=True, exclusive=True),
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            archive_name = candidate_name
            break
        if archive_fd < 0:
            raise RuntimeError("reset archive namespace exhausted")
        os.fchmod(archive_fd, 0o600)
        initial = os.fstat(archive_fd)
        archive_identity = (initial.st_dev, initial.st_ino)
        _revalidate_open_file(
            directory_fd,
            name=archive_name,
            descriptor=archive_fd,
            expected=archive_identity,
            label="reset archive",
        )

        command = ["tar", "-czf", "-", "-C", str(root), *selected_targets]
        command.extend(("-C", str(manifest_path.parent), manifest_name))
        tar_environment = {
            "PATH": _TRUSTED_EXECUTABLE_PATH,
            "LANG": "C",
            "LC_ALL": "C",
            # Suppresses macOS AppleDouble metadata; the fixed env also stops
            # TAR_OPTIONS (e.g. --dereference) being inherited.
            "COPYFILE_DISABLE": "1",
        }
        try:
            subprocess.run(command, check=True, stdout=archive_fd, env=tar_environment)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("reset archive tar creation failed") from exc
        os.fsync(archive_fd)

        os.lseek(archive_fd, 0, os.SEEK_SET)
        try:
            subprocess.run(
                ["tar", "-tzf", "-"],
                check=True,
                stdin=archive_fd,
                stdout=subprocess.DEVNULL,
                env=tar_environment,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("reset archive tar verification failed") from exc
        digest, size = _hash_descriptor(archive_fd)
        after_archive = _revalidate_open_file(
            directory_fd,
            name=archive_name,
            descriptor=archive_fd,
            expected=archive_identity,
            label="reset archive",
        )
        if after_archive.st_size != size:
            raise RuntimeError("reset archive size changed while it was hashed")

        sidecar_name = f"{archive_name}.sha256"
        try:
            sidecar_fd = os.open(
                sidecar_name,
                _regular_open_flags(writable=True, exclusive=True),
                0o600,
                dir_fd=directory_fd,
            )
            sidecar_created = True
        except OSError as exc:
            raise RuntimeError("reset archive digest sidecar cannot be created exclusively") from exc
        os.fchmod(sidecar_fd, 0o600)
        sidecar_identity = (os.fstat(sidecar_fd).st_dev, os.fstat(sidecar_fd).st_ino)
        _write_all(sidecar_fd, f"{digest}  {archive_name}\n".encode("utf-8"))
        os.fsync(sidecar_fd)
        _revalidate_open_file(
            directory_fd,
            name=sidecar_name,
            descriptor=sidecar_fd,
            expected=sidecar_identity,
            label="reset archive digest sidecar",
        )
        _revalidate_open_file(
            directory_fd,
            name=archive_name,
            descriptor=archive_fd,
            expected=archive_identity,
            label="reset archive",
        )
        os.fsync(directory_fd)
        return ResetArchive(
            path=directory_path / archive_name,
            sidecar_path=directory_path / sidecar_name,
            sha256=digest,
            size_bytes=size,
            device=archive_identity[0],
            inode=archive_identity[1],
        )
    except BaseException:
        if sidecar_fd >= 0:
            try:
                os.close(sidecar_fd)
            except OSError:
                pass
            sidecar_fd = -1
        if archive_fd >= 0:
            try:
                os.close(archive_fd)
            except OSError:
                pass
            archive_fd = -1
        cleanup_names = ([sidecar_name] if sidecar_created else []) + ([archive_name] if archive_name else [])
        for name in cleanup_names:
            if not name:
                continue
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        raise
    finally:
        if sidecar_fd >= 0:
            os.close(sidecar_fd)
        if archive_fd >= 0:
            os.close(archive_fd)
        os.close(directory_fd)


def verify_reset_archive(
    *,
    repository: str | Path,
    archive: str | Path,
    sidecar: str | Path,
    expected_sha256: str,
    expected_device: int,
    expected_inode: int,
    expected_size: int,
) -> ResetArchive:
    """Reopen and verify the exact private archive immediately before deletion."""

    root = _repository(repository)
    archive_path = _absolute(archive, repository=root)
    sidecar_path = _absolute(sidecar, repository=root)
    if archive_path.parent != sidecar_path.parent or sidecar_path.name != f"{archive_path.name}.sha256":
        raise ValueError("reset archive and digest sidecar paths do not match")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected reset archive SHA-256 is invalid")
    if min(expected_device, expected_inode, expected_size) < 0:
        raise ValueError("expected reset archive identity is invalid")

    directory_fd = _open_archive_directory(archive_path.parent, create=False)
    archive_fd = -1
    sidecar_fd = -1
    try:
        archive_fd = os.open(
            archive_path.name,
            _regular_open_flags(writable=False),
            dir_fd=directory_fd,
        )
        archive_metadata = _revalidate_open_file(
            directory_fd,
            name=archive_path.name,
            descriptor=archive_fd,
            expected=(expected_device, expected_inode),
            label="reset archive",
        )
        if archive_metadata.st_size != expected_size:
            raise RuntimeError("reset archive size changed before live-state removal")
        digest, size = _hash_descriptor(archive_fd)
        if digest != expected_sha256 or size != expected_size:
            raise RuntimeError("reset archive digest changed before live-state removal")

        sidecar_fd = os.open(
            sidecar_path.name,
            _regular_open_flags(writable=False),
            dir_fd=directory_fd,
        )
        sidecar_metadata = os.fstat(sidecar_fd)
        sidecar_entry = _entry_identity(directory_fd, sidecar_path.name)
        sidecar_identity = (sidecar_metadata.st_dev, sidecar_metadata.st_ino)
        if (sidecar_entry.st_dev, sidecar_entry.st_ino) != sidecar_identity:
            raise RuntimeError("reset archive digest sidecar path changed before live-state removal")
        _require_private_regular(sidecar_metadata, label="reset archive digest sidecar")
        _require_private_regular(sidecar_entry, label="reset archive digest sidecar")
        sidecar_size_limit = 256
        payload = os.read(sidecar_fd, sidecar_size_limit + 1)
        if len(payload) > sidecar_size_limit or os.read(sidecar_fd, 1):
            raise RuntimeError("reset archive digest sidecar is invalid")
        expected_payload = f"{expected_sha256}  {archive_path.name}\n".encode("utf-8")
        if payload != expected_payload:
            raise RuntimeError("reset archive digest sidecar changed before live-state removal")
        return ResetArchive(
            path=archive_path,
            sidecar_path=sidecar_path,
            sha256=digest,
            size_bytes=size,
            device=expected_device,
            inode=expected_inode,
        )
    finally:
        if sidecar_fd >= 0:
            os.close(sidecar_fd)
        if archive_fd >= 0:
            os.close(archive_fd)
        os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repository", required=True)
    create.add_argument("--archive-directory", required=True)
    create.add_argument("--stem", required=True)
    create.add_argument("--manifest", required=True)
    create.add_argument("--target", action="append", default=[])
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repository", required=True)
    verify.add_argument("--archive", required=True)
    verify.add_argument("--sidecar", required=True)
    verify.add_argument("--sha256", required=True)
    verify.add_argument("--device", required=True, type=int)
    verify.add_argument("--inode", required=True, type=int)
    verify.add_argument("--size", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create":
        artifact = create_reset_archive(
            repository=args.repository,
            archive_directory=args.archive_directory,
            stem=args.stem,
            manifest=args.manifest,
            targets=args.target,
        )
        print(
            "\t".join(
                (
                    str(artifact.path),
                    str(artifact.sidecar_path),
                    artifact.sha256,
                    str(artifact.size_bytes),
                    str(artifact.device),
                    str(artifact.inode),
                )
            )
        )
        return 0
    verify_reset_archive(
        repository=args.repository,
        archive=args.archive,
        sidecar=args.sidecar,
        expected_sha256=args.sha256,
        expected_device=args.device,
        expected_inode=args.inode,
        expected_size=args.size,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI contract
    sys.exit(main())
