"""Descriptor-rooted validation and mutation for demo/paper reset paths.

The reset script runs while writers are stopped, but it still treats every
pathname below the data root as untrusted.  This module plans a complete batch
before mutating it, binds every traversed object by descriptor and inode, and
uses Linux mount IDs to reject same-device bind mounts that ``st_dev`` cannot
distinguish.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None


@dataclass(frozen=True, slots=True)
class ResetTargetInspection:
    path: Path
    exists: bool
    entries: int


@dataclass(frozen=True, slots=True)
class ResetTargetRemoval:
    path: Path
    existed: bool
    removed_entries: int


@dataclass(frozen=True, slots=True)
class PaperRootNormalization:
    path: Path
    existed: bool
    root_created: bool
    normalized_directories: int
    normalized_files: int
    lock_directory_created: bool


@dataclass(frozen=True, slots=True)
class DemoRootNormalization:
    path: Path
    existed: bool
    root_created: bool
    lock_directory_created: bool
    cache_directory_created: bool
    kline_directory_created: bool
    snapshot_normalized: bool
    residual_momentum_normalized: bool


@dataclass(frozen=True, slots=True)
class _Entry:
    path: Path
    relative_parts: tuple[str, ...]
    device: int
    inode: int
    file_type: int
    link_count: int
    mount_id: int | None
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True, slots=True)
class _Target:
    path: Path
    relative_parts: tuple[str, ...]
    exists: bool
    missing_at: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _InspectionPlan:
    anchor: Path
    anchor_entry: _Entry
    directories: tuple[_Entry, ...]
    entries: tuple[_Entry, ...]
    targets: tuple[_Target, ...]


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _file_type(mode: int) -> int:
    return stat.S_IFMT(mode)


def _directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - deployment is POSIX
        raise RuntimeError("reset path safety requires O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _regular_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - deployment is POSIX
        raise RuntimeError("reset path safety requires O_NOFOLLOW and O_CLOEXEC") from exc


def _mount_id_for_fd(descriptor: int) -> int | None:
    if _LINUX_FDINFO_ROOT is None:
        return None
    try:
        raw = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Linux mount identity cannot be read for descriptor {descriptor}") from exc
    values = [line.partition(":")[2].strip() for line in raw.splitlines() if line.partition(":")[0] == "mnt_id"]
    if len(values) != 1:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}")
    try:
        value = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}") from exc
    if value < 0:
        raise RuntimeError(f"Linux mount identity is invalid for descriptor {descriptor}")
    return value


def _entry_metadata(directory_fd: int, name: str) -> os.stat_result:
    """Read a leaf without following it; separated for race-injection tests."""
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _entry_mount_id(
    directory_fd: int,
    name: str,
    path: Path,
    observed: os.stat_result,
) -> int | None:
    """Bind a Linux entry, including a regular-file bind mount, with O_PATH."""
    if _LINUX_FDINFO_ROOT is None:
        return None
    path_flag = getattr(os, "O_PATH", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if path_flag is None or nofollow is None or cloexec is None:  # pragma: no cover - Linux supplies these
        raise RuntimeError("Linux reset path safety requires O_PATH, O_NOFOLLOW, and O_CLOEXEC")
    kind = "directory" if stat.S_ISDIR(observed.st_mode) else "entry"
    try:
        descriptor = os.open(name, path_flag | nofollow | cloexec, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(f"reset {kind} changed while inspected: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(directory_fd, name)
        expected = (observed.st_dev, observed.st_ino, _file_type(observed.st_mode))
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != expected
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != expected
        ):
            raise ValueError(f"reset {kind} changed while inspected: {path}")
        return _mount_id_for_fd(descriptor)
    finally:
        os.close(descriptor)


def _snapshot(
    *,
    path: Path,
    relative_parts: tuple[str, ...],
    metadata: os.stat_result,
    mount_id: int | None,
) -> _Entry:
    return _Entry(
        path=path,
        relative_parts=relative_parts,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=_file_type(metadata.st_mode),
        link_count=metadata.st_nlink,
        mount_id=mount_id,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _normalize_target(anchor: Path, raw: str | Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        absolute = _lexical_absolute(candidate)
    else:
        cwd_candidate = _lexical_absolute(candidate)
        try:
            cwd_candidate.relative_to(anchor)
        except ValueError:
            absolute = _lexical_absolute(anchor / candidate)
        else:
            absolute = cwd_candidate
    try:
        relative = absolute.relative_to(anchor)
    except ValueError as exc:
        raise ValueError(f"reset target is outside trusted data anchor {anchor}: {absolute}") from exc
    parts = relative.parts
    if not parts:
        raise ValueError(f"trusted data anchor itself cannot be a reset target: {anchor}")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"reset target is not lexically safe: {absolute}")
    return absolute, parts


def _normalize_targets(
    anchor: Path,
    targets: Iterable[str | Path],
    *,
    allow_overlaps: bool,
) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    normalized: list[tuple[Path, tuple[str, ...]]] = []
    seen: set[Path] = set()
    for raw in targets:
        item = _normalize_target(anchor, raw)
        if item[0] in seen:
            continue
        seen.add(item[0])
        normalized.append(item)
    normalized.sort(key=lambda item: (len(item[1]), item[1]))
    if not allow_overlaps:
        for index, (left_path, left_parts) in enumerate(normalized):
            for right_path, right_parts in normalized[index + 1 :]:
                if right_parts[: len(left_parts)] == left_parts:
                    raise ValueError(f"reset targets must not overlap: {left_path} and {right_path}")
    if allow_overlaps:
        minimal: list[tuple[Path, tuple[str, ...]]] = []
        for item in normalized:
            if any(item[1][: len(parent[1])] == parent[1] for parent in minimal):
                continue
            minimal.append(item)
        normalized = minimal
    return tuple(normalized)


def _open_anchor(anchor: Path) -> tuple[int, _Entry]:
    try:
        descriptor = os.open(anchor, _directory_open_flags())
    except OSError as exc:
        raise ValueError(f"trusted data anchor must be a real directory: {anchor}") from exc
    try:
        opened = os.fstat(descriptor)
        current = anchor.lstat()
        mount_id = _mount_id_for_fd(descriptor)
        identity = (opened.st_dev, opened.st_ino, _file_type(opened.st_mode))
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
        ):
            raise ValueError(f"trusted data anchor must be a stable real directory: {anchor}")
        return descriptor, _snapshot(path=anchor, relative_parts=(), metadata=opened, mount_id=mount_id)
    except BaseException:
        os.close(descriptor)
        raise


def _require_supported_entry(path: Path, metadata: os.stat_result) -> None:
    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError(f"reset tree contains an unsupported special file: {path}")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise ValueError(f"reset tree contains a multiply-linked regular file: {path}")


def _build_inspection_plan(
    anchor_raw: str | Path,
    targets_raw: Iterable[str | Path],
    *,
    allow_overlaps: bool,
) -> _InspectionPlan:
    anchor = _lexical_absolute(anchor_raw)
    targets = _normalize_targets(anchor, targets_raw, allow_overlaps=allow_overlaps)
    anchor_fd, anchor_entry = _open_anchor(anchor)
    directories: dict[tuple[str, ...], _Entry] = {(): anchor_entry}
    entries: dict[tuple[str, ...], _Entry] = {}
    target_plans: list[_Target] = []

    def require_same_mount(path: Path, metadata: os.stat_result, mount_id: int | None) -> None:
        if metadata.st_dev != anchor_entry.device or mount_id != anchor_entry.mount_id:
            raise ValueError(f"reset path crosses a mount boundary: {path}")

    def open_directory(
        parent_fd: int,
        name: str,
        path: Path,
        relative_parts: tuple[str, ...],
        observed: os.stat_result,
        mount_id: int | None,
        *,
        record_entry: bool,
    ) -> int:
        try:
            descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"reset directory changed while inspected: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            current = _entry_metadata(parent_fd, name)
            opened_mount_id = _mount_id_for_fd(descriptor)
            expected = (observed.st_dev, observed.st_ino, stat.S_IFDIR)
            if (
                (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != expected
                or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != expected
                or mount_id != opened_mount_id
            ):
                raise ValueError(f"reset directory changed while inspected: {path}")
            require_same_mount(path, opened, opened_mount_id)
            planned = _snapshot(
                path=path,
                relative_parts=relative_parts,
                metadata=opened,
                mount_id=opened_mount_id,
            )
            previous = directories.get(relative_parts)
            if previous is not None and previous != planned:
                raise ValueError(f"reset directory changed between target inspections: {path}")
            directories[relative_parts] = planned
            if record_entry:
                entries.setdefault(relative_parts, planned)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def visit(directory_fd: int, relative_parts: tuple[str, ...]) -> None:
        directory = anchor.joinpath(*relative_parts)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ValueError(f"reset directory cannot be inspected: {directory}") from exc
        for name in names:
            child_parts = (*relative_parts, name)
            path = anchor.joinpath(*child_parts)
            try:
                metadata = _entry_metadata(directory_fd, name)
            except OSError as exc:
                raise ValueError(f"reset entry cannot be inspected: {path}") from exc
            _require_supported_entry(path, metadata)
            mount_id = _entry_mount_id(directory_fd, name, path, metadata)
            require_same_mount(path, metadata, mount_id)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = open_directory(
                    directory_fd,
                    name,
                    path,
                    child_parts,
                    metadata,
                    mount_id,
                    record_entry=True,
                )
                try:
                    visit(child_fd, child_parts)
                    final = os.fstat(child_fd)
                    current = _entry_metadata(directory_fd, name)
                    planned = directories[child_parts]
                    if (
                        (final.st_dev, final.st_ino, _file_type(final.st_mode))
                        != (planned.device, planned.inode, planned.file_type)
                        or (current.st_dev, current.st_ino, _file_type(current.st_mode))
                        != (planned.device, planned.inode, planned.file_type)
                        or _mount_id_for_fd(child_fd) != planned.mount_id
                    ):
                        raise ValueError(f"reset directory changed while inspected: {path}")
                finally:
                    os.close(child_fd)
            else:
                planned = _snapshot(
                    path=path,
                    relative_parts=child_parts,
                    metadata=metadata,
                    mount_id=mount_id,
                )
                previous = entries.get(child_parts)
                if previous is not None and previous != planned:
                    raise ValueError(f"reset entry changed between target inspections: {path}")
                entries[child_parts] = planned

    try:
        for target_path, target_parts in targets:
            current_fd = anchor_fd
            opened_fds: list[int] = []
            missing_at: tuple[str, ...] | None = None
            try:
                for depth, name in enumerate(target_parts, start=1):
                    current_parts = target_parts[:depth]
                    path = anchor.joinpath(*current_parts)
                    try:
                        metadata = _entry_metadata(current_fd, name)
                    except FileNotFoundError:
                        missing_at = current_parts
                        break
                    except OSError as exc:
                        raise ValueError(f"reset target cannot be inspected: {path}") from exc
                    _require_supported_entry(path, metadata)
                    mount_id = _entry_mount_id(current_fd, name, path, metadata)
                    require_same_mount(path, metadata, mount_id)
                    if depth < len(target_parts):
                        if not stat.S_ISDIR(metadata.st_mode):
                            raise ValueError(f"reset target parent must be a real directory: {path}")
                        child_fd = open_directory(
                            current_fd,
                            name,
                            path,
                            current_parts,
                            metadata,
                            mount_id,
                            record_entry=False,
                        )
                        opened_fds.append(child_fd)
                        current_fd = child_fd
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        child_fd = open_directory(
                            current_fd,
                            name,
                            path,
                            current_parts,
                            metadata,
                            mount_id,
                            record_entry=True,
                        )
                        try:
                            visit(child_fd, current_parts)
                        finally:
                            os.close(child_fd)
                    else:
                        entries[current_parts] = _snapshot(
                            path=path,
                            relative_parts=current_parts,
                            metadata=metadata,
                            mount_id=mount_id,
                        )
                target_plans.append(
                    _Target(
                        path=target_path,
                        relative_parts=target_parts,
                        exists=missing_at is None,
                        missing_at=missing_at,
                    )
                )
            finally:
                for descriptor in reversed(opened_fds):
                    os.close(descriptor)
    finally:
        os.close(anchor_fd)

    return _InspectionPlan(
        anchor=anchor,
        anchor_entry=anchor_entry,
        directories=tuple(sorted(directories.values(), key=lambda item: (len(item.relative_parts), item.relative_parts))),
        entries=tuple(sorted(entries.values(), key=lambda item: (len(item.relative_parts), item.relative_parts))),
        targets=tuple(target_plans),
    )


def preflight_reset_targets(
    anchor: str | Path,
    targets: Iterable[str | Path],
    *,
    reject_symlinks: bool = False,
) -> tuple[ResetTargetInspection, ...]:
    """Validate all selected roots before tar reads or reset mutations begin.

    Overlapping paths are collapsed to their shallowest root so callers may
    pass both selected strategy roots and their concrete generated targets.
    Missing paths are safe no-ops.  Existing trees reject special files,
    multiply-linked regular files, symlinked parent components, and root,
    nested-directory, or regular-file mount boundaries.
    """
    plan = _build_inspection_plan(anchor, targets, allow_overlaps=True)
    if reject_symlinks:
        for entry in plan.entries:
            if entry.file_type == stat.S_IFLNK:
                raise ValueError(f"strict reset preflight rejects a symlink: {entry.path}")
    results: list[ResetTargetInspection] = []
    for target in plan.targets:
        count = sum(
            entry.relative_parts[: len(target.relative_parts)] == target.relative_parts
            for entry in plan.entries
        )
        results.append(ResetTargetInspection(path=target.path, exists=target.exists, entries=count))
    return tuple(results)


class _ApplyContext:
    def __init__(self, plan: _InspectionPlan) -> None:
        self.plan = plan
        self.directory_plans = {entry.relative_parts: entry for entry in plan.directories}
        descriptor, current = _open_anchor(plan.anchor)
        expected = plan.anchor_entry
        if (
            (current.device, current.inode, current.file_type, current.mount_id)
            != (expected.device, expected.inode, expected.file_type, expected.mount_id)
        ):
            os.close(descriptor)
            raise RuntimeError(f"trusted data anchor changed before reset mutation: {plan.anchor}")
        self.open_directories: dict[tuple[str, ...], int] = {(): descriptor}

    def close(self) -> None:
        for _parts, descriptor in sorted(
            self.open_directories.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.open_directories.clear()

    def _validate_anchor(self) -> None:
        descriptor = self.open_directories[()]
        expected = self.plan.anchor_entry
        try:
            opened = os.fstat(descriptor)
            current = self.plan.anchor.lstat()
            mount_id = _mount_id_for_fd(descriptor)
        except OSError as exc:
            raise RuntimeError(f"trusted data anchor changed during reset mutation: {self.plan.anchor}") from exc
        identity = (expected.device, expected.inode, expected.file_type)
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != identity
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
            or mount_id != expected.mount_id
        ):
            raise RuntimeError(f"trusted data anchor changed during reset mutation: {self.plan.anchor}")

    def directory_fd(self, relative_parts: tuple[str, ...]) -> int:
        self._validate_anchor()
        current_fd = self.open_directories[()]
        current_parts: tuple[str, ...] = ()
        for name in relative_parts:
            child_parts = (*current_parts, name)
            planned = self.directory_plans.get(child_parts)
            if planned is None:
                raise RuntimeError(f"unplanned reset directory requested: {self.plan.anchor.joinpath(*child_parts)}")
            descriptor = self.open_directories.get(child_parts)
            if descriptor is None:
                try:
                    descriptor = os.open(name, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise RuntimeError(f"reset directory changed before mutation: {planned.path}") from exc
                self.open_directories[child_parts] = descriptor
            try:
                opened = os.fstat(descriptor)
                current = _entry_metadata(current_fd, name)
                mount_id = _mount_id_for_fd(descriptor)
            except OSError as exc:
                raise RuntimeError(f"reset directory changed before mutation: {planned.path}") from exc
            identity = (planned.device, planned.inode, planned.file_type)
            if (
                (opened.st_dev, opened.st_ino, _file_type(opened.st_mode)) != identity
                or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
                or mount_id != planned.mount_id
                or opened.st_dev != self.plan.anchor_entry.device
                or mount_id != self.plan.anchor_entry.mount_id
            ):
                raise RuntimeError(f"reset directory changed before mutation: {planned.path}")
            current_fd = descriptor
            current_parts = child_parts
        return current_fd

    def validate_entry(self, planned: _Entry) -> tuple[int, str, os.stat_result]:
        parent_fd = self.directory_fd(planned.relative_parts[:-1])
        name = planned.relative_parts[-1]
        try:
            current = _entry_metadata(parent_fd, name)
            mount_id = _entry_mount_id(parent_fd, name, planned.path, current)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"reset entry changed before mutation: {planned.path}") from exc
        if (
            (current.st_dev, current.st_ino, _file_type(current.st_mode))
            != (planned.device, planned.inode, planned.file_type)
            or (planned.file_type == stat.S_IFREG and current.st_nlink != planned.link_count)
            or current.st_dev != self.plan.anchor_entry.device
            or mount_id != planned.mount_id
            or mount_id != self.plan.anchor_entry.mount_id
        ):
            raise RuntimeError(f"reset entry changed before mutation: {planned.path}")
        return parent_fd, name, current


def _assert_missing_target(context: _ApplyContext, target: _Target) -> None:
    assert target.missing_at is not None
    parent_parts = target.missing_at[:-1]
    try:
        parent_fd = context.directory_fd(parent_parts)
    except RuntimeError:
        # If an earlier missing ancestor was itself unplanned, walk to the
        # deepest planned prefix and require the next component to stay absent.
        planned_prefix: tuple[str, ...] = ()
        for depth in range(1, len(parent_parts) + 1):
            candidate = parent_parts[:depth]
            if candidate not in context.directory_plans:
                break
            planned_prefix = candidate
        parent_fd = context.directory_fd(planned_prefix)
        missing_name = target.missing_at[len(planned_prefix)]
    else:
        missing_name = target.missing_at[-1]
    try:
        _entry_metadata(parent_fd, missing_name)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"missing reset target changed before mutation: {target.path}") from exc
    raise RuntimeError(f"missing reset target appeared before mutation: {target.path}")


def _assert_removed_target_absent(context: _ApplyContext, target: _Target) -> None:
    parent_fd = context.directory_fd(target.relative_parts[:-1])
    try:
        _entry_metadata(parent_fd, target.relative_parts[-1])
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"removed reset target cannot be revalidated: {target.path}") from exc
    raise RuntimeError(f"removed reset target reappeared during mutation: {target.path}")


def remove_reset_targets(
    anchor: str | Path,
    targets: Iterable[str | Path],
) -> tuple[ResetTargetRemoval, ...]:
    """Remove selected leaves/trees after a full descriptor-bound batch plan."""
    plan = _build_inspection_plan(anchor, targets, allow_overlaps=False)
    context = _ApplyContext(plan)
    removed: set[tuple[str, ...]] = set()
    try:
        for target in plan.targets:
            if not target.exists:
                _assert_missing_target(context, target)
        removals = sorted(plan.entries, key=lambda item: (len(item.relative_parts), item.relative_parts), reverse=True)
        for planned in removals:
            parent_fd, name, current = context.validate_entry(planned)
            try:
                if stat.S_ISDIR(current.st_mode):
                    child_fd = context.directory_fd(planned.relative_parts)
                    os.fsync(child_fd)
                    os.rmdir(name, dir_fd=parent_fd)
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
                    context.open_directories.pop(planned.relative_parts, None)
                else:
                    os.unlink(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as exc:
                raise RuntimeError(f"reset entry changed during removal: {planned.path}") from exc
            removed.add(planned.relative_parts)
        for target in plan.targets:
            if not target.exists:
                _assert_missing_target(context, target)
            else:
                _assert_removed_target_absent(context, target)
        context._validate_anchor()
    finally:
        context.close()
    return tuple(
        ResetTargetRemoval(
            path=target.path,
            existed=target.exists,
            removed_entries=sum(
                parts[: len(target.relative_parts)] == target.relative_parts for parts in removed
            ),
        )
        for target in plan.targets
    )


def _open_regular_for_mutation(context: _ApplyContext, planned: _Entry) -> int:
    parent_fd, name, current = context.validate_entry(planned)
    if not stat.S_ISREG(current.st_mode):
        raise RuntimeError(f"paper runtime file changed before normalization: {planned.path}")
    try:
        descriptor = os.open(name, _regular_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"paper runtime file changed before normalization: {planned.path}") from exc
    try:
        opened = os.fstat(descriptor)
        mount_id = _mount_id_for_fd(descriptor)
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode), opened.st_nlink)
            != (planned.device, planned.inode, planned.file_type, planned.link_count)
            or opened.st_dev != context.plan.anchor_entry.device
            or mount_id != planned.mount_id
            or mount_id != context.plan.anchor_entry.mount_id
        ):
            raise RuntimeError(f"paper runtime file changed before normalization: {planned.path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _set_descriptor_permissions(
    descriptor: int,
    *,
    uid: int,
    gid: int,
    mode: int,
    path: Path,
) -> None:
    try:
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != (uid, gid):
            os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        current = os.fstat(descriptor)
    except OSError as exc:
        raise RuntimeError(f"cannot normalize reset path permissions: {path}") from exc
    if current.st_uid != uid or current.st_gid != gid or stat.S_IMODE(current.st_mode) != mode:
        raise RuntimeError(f"reset path permissions did not normalize exactly: {path}")


def _restore_descriptor_permissions(descriptor: int, planned: _Entry) -> None:
    current = os.fstat(descriptor)
    if (current.st_uid, current.st_gid) != (planned.uid, planned.gid):
        os.fchown(descriptor, planned.uid, planned.gid)
    os.fchmod(descriptor, planned.mode)
    os.fsync(descriptor)


def _normalize_planned_regular(
    context: _ApplyContext,
    planned: _Entry,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    descriptor = _open_regular_for_mutation(context, planned)
    changed = False
    try:
        before = os.fstat(descriptor)
        if (
            before.st_uid != planned.uid
            or before.st_gid != planned.gid
            or stat.S_IMODE(before.st_mode) != planned.mode
            or before.st_nlink != 1
        ):
            raise RuntimeError(f"paper runtime file metadata changed before normalization: {planned.path}")
        changed = True
        _set_descriptor_permissions(descriptor, uid=uid, gid=gid, mode=mode, path=planned.path)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        _parent_fd, _name, current = context.validate_entry(planned)
        if (
            after.st_nlink != 1
            or current.st_uid != uid
            or current.st_gid != gid
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise RuntimeError(f"paper runtime file changed during normalization: {planned.path}")
    except BaseException:
        if changed:
            try:
                _restore_descriptor_permissions(descriptor, planned)
            except OSError as restore_exc:
                raise RuntimeError(
                    f"paper runtime file changed and original permissions could not be restored: {planned.path}"
                ) from restore_exc
        raise
    finally:
        os.close(descriptor)


def _normalize_planned_directory(
    context: _ApplyContext,
    planned: _Entry,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    descriptor = context.directory_fd(planned.relative_parts)
    before = os.fstat(descriptor)
    original = _Entry(
        path=planned.path,
        relative_parts=planned.relative_parts,
        device=before.st_dev,
        inode=before.st_ino,
        file_type=_file_type(before.st_mode),
        link_count=before.st_nlink,
        mount_id=planned.mount_id,
        uid=before.st_uid,
        gid=before.st_gid,
        mode=stat.S_IMODE(before.st_mode),
    )
    changed = False
    try:
        changed = True
        _set_descriptor_permissions(descriptor, uid=uid, gid=gid, mode=mode, path=planned.path)
        os.fsync(descriptor)
        rebound = context.directory_fd(planned.relative_parts)
        current = os.fstat(rebound)
        if current.st_uid != uid or current.st_gid != gid or stat.S_IMODE(current.st_mode) != mode:
            raise RuntimeError(f"paper runtime directory changed during normalization: {planned.path}")
    except BaseException:
        if changed:
            try:
                _restore_descriptor_permissions(descriptor, original)
            except OSError as restore_exc:
                raise RuntimeError(
                    f"paper runtime directory changed and original permissions could not be restored: {planned.path}"
                ) from restore_exc
        raise


def normalize_paper_runtime_roots(
    anchor: str | Path,
    roots: Iterable[str | Path],
    *,
    uid: int,
    gid: int,
    create_missing: bool = False,
) -> tuple[PaperRootNormalization, ...]:
    """Normalize complete paper trees without pathname-following chown/find.

    Every existing root is preflighted before the first mkdir, chown, or chmod.
    Symlinks, special files, multiply-linked files, and mount boundaries fail
    the whole batch.  Missing roots remain absent unless ``create_missing`` is
    selected; creation is limited to direct children of the trusted anchor.
    Each resulting root receives a private ``.locks`` directory.
    """
    if uid < 0 or gid < 0:
        raise ValueError("paper runtime uid and gid must be non-negative")
    plan = _build_inspection_plan(anchor, roots, allow_overlaps=False)
    _validate_paper_plan(plan)
    if create_missing:
        for target in plan.targets:
            if (
                not target.exists
                and (len(target.relative_parts) != 1 or target.missing_at != target.relative_parts)
            ):
                raise ValueError(f"only missing direct-child paper roots may be created: {target.path}")
    context = _ApplyContext(plan)
    created_locks: set[tuple[str, ...]] = set()
    created_lock_entries: dict[tuple[str, ...], _Entry] = {}
    created_root_entries: dict[tuple[str, ...], _Entry] = {}
    created_root_descriptors: dict[tuple[str, ...], int] = {}
    root_handles: dict[tuple[str, ...], int] = {}
    try:
        for target in plan.targets:
            if not target.exists:
                _assert_missing_target(context, target)
                if not create_missing:
                    continue
                anchor_fd = context.directory_fd(())
                root_fd, created_root = _create_bound_directory(
                    parent_fd=anchor_fd,
                    name=target.relative_parts[0],
                    path=target.path,
                    relative_parts=target.relative_parts,
                    anchor_entry=plan.anchor_entry,
                )
                created_root_entries[target.relative_parts] = created_root
                created_root_descriptors[target.relative_parts] = root_fd
            else:
                root_fd = context.directory_fd(target.relative_parts)
            root_handles[target.relative_parts] = root_fd
            lock_parts = (*target.relative_parts, ".locks")
            if lock_parts in context.directory_plans:
                continue
            try:
                os.mkdir(".locks", 0o700, dir_fd=root_fd)
                lock_fd = os.open(".locks", _directory_open_flags(), dir_fd=root_fd)
            except OSError as exc:
                raise RuntimeError(f"paper lock namespace changed before creation: {target.path / '.locks'}") from exc
            try:
                opened = os.fstat(lock_fd)
                current = _entry_metadata(root_fd, ".locks")
                mount_id = _mount_id_for_fd(lock_fd)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or (opened.st_dev, opened.st_ino, _file_type(opened.st_mode))
                    != (current.st_dev, current.st_ino, _file_type(current.st_mode))
                    or opened.st_dev != plan.anchor_entry.device
                    or mount_id != plan.anchor_entry.mount_id
                ):
                    raise RuntimeError(f"paper lock namespace changed during creation: {target.path / '.locks'}")
                _set_descriptor_permissions(
                    lock_fd,
                    uid=uid,
                    gid=gid,
                    mode=0o700,
                    path=target.path / ".locks",
                )
                rebound = _entry_metadata(root_fd, ".locks")
                if (
                    (rebound.st_dev, rebound.st_ino, _file_type(rebound.st_mode))
                    != (opened.st_dev, opened.st_ino, stat.S_IFDIR)
                    or rebound.st_uid != uid
                    or rebound.st_gid != gid
                    or stat.S_IMODE(rebound.st_mode) != 0o700
                ):
                    raise RuntimeError(f"paper lock namespace changed during normalization: {target.path / '.locks'}")
                os.fsync(lock_fd)
                os.fsync(root_fd)
                normalized_lock = os.fstat(lock_fd)
                created_lock_entries[lock_parts] = _snapshot(
                    path=target.path / ".locks",
                    relative_parts=lock_parts,
                    metadata=normalized_lock,
                    mount_id=_mount_id_for_fd(lock_fd),
                )
            finally:
                os.close(lock_fd)
            created_locks.add(lock_parts)

        regular_entries = [entry for entry in plan.entries if entry.file_type == stat.S_IFREG]
        for entry in regular_entries:
            _normalize_planned_regular(context, entry, uid=uid, gid=gid, mode=0o600)

        directory_entries = [entry for entry in plan.entries if entry.file_type == stat.S_IFDIR]
        for entry in sorted(directory_entries, key=lambda item: len(item.relative_parts), reverse=True):
            _normalize_planned_directory(context, entry, uid=uid, gid=gid, mode=0o700)
        for target in plan.targets:
            planned_created_root = created_root_entries.get(target.relative_parts)
            if planned_created_root is None:
                continue
            created_root_entries[target.relative_parts] = _normalize_created_directory(
                parent_fd=context.directory_fd(()),
                name=target.relative_parts[0],
                descriptor=root_handles[target.relative_parts],
                planned=planned_created_root,
                uid=uid,
                gid=gid,
                mode=0o700,
            )
        for entry in plan.entries:
            if entry.file_type == stat.S_IFREG:
                _parent_fd, _name, current = context.validate_entry(entry)
                if (
                    current.st_uid != uid
                    or current.st_gid != gid
                    or stat.S_IMODE(current.st_mode) != 0o600
                ):
                    raise RuntimeError(f"paper runtime file changed after normalization: {entry.path}")
            elif entry.file_type == stat.S_IFDIR:
                descriptor = context.directory_fd(entry.relative_parts)
                current = os.fstat(descriptor)
                if (
                    current.st_uid != uid
                    or current.st_gid != gid
                    or stat.S_IMODE(current.st_mode) != 0o700
                ):
                    raise RuntimeError(f"paper runtime directory changed after normalization: {entry.path}")
        context._validate_anchor()
        _verify_normalized_paper_tree(
            plan,
            created_entries={**created_root_entries, **created_lock_entries},
            uid=uid,
            gid=gid,
        )
    finally:
        for descriptor in created_root_descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        context.close()

    results: list[PaperRootNormalization] = []
    for target in plan.targets:
        prefix = target.relative_parts
        results.append(
            PaperRootNormalization(
                path=target.path,
                existed=target.exists,
                root_created=target.relative_parts in created_root_entries,
                normalized_directories=sum(
                    entry.file_type == stat.S_IFDIR and entry.relative_parts[: len(prefix)] == prefix
                    for entry in plan.entries
                )
                + int((*prefix, ".locks") in created_locks)
                + int(prefix in created_root_entries),
                normalized_files=sum(
                    entry.file_type == stat.S_IFREG and entry.relative_parts[: len(prefix)] == prefix
                    for entry in plan.entries
                ),
                lock_directory_created=(*prefix, ".locks") in created_locks,
            )
        )
    return tuple(results)


def _validate_paper_plan(plan: _InspectionPlan) -> None:
    entries_by_parts = {entry.relative_parts: entry for entry in plan.entries}
    for target in plan.targets:
        if not target.exists:
            continue
        root_entry = entries_by_parts[target.relative_parts]
        if root_entry.file_type != stat.S_IFDIR:
            raise ValueError(f"paper runtime root must be a real directory: {target.path}")
        lock_parts = (*target.relative_parts, ".locks")
        lock_entry = entries_by_parts.get(lock_parts)
        if lock_entry is not None and lock_entry.file_type != stat.S_IFDIR:
            raise ValueError(f"paper lock namespace must be a real directory: {target.path / '.locks'}")
    for entry in plan.entries:
        if entry.file_type == stat.S_IFLNK:
            raise ValueError(f"paper runtime tree contains a symlink: {entry.path}")


def preflight_paper_runtime_roots(
    anchor: str | Path,
    roots: Iterable[str | Path],
) -> tuple[ResetTargetInspection, ...]:
    """Apply the complete-tree paper policy without changing ownership or modes."""
    plan = _build_inspection_plan(anchor, roots, allow_overlaps=False)
    _validate_paper_plan(plan)
    return tuple(
        ResetTargetInspection(
            path=target.path,
            exists=target.exists,
            entries=sum(
                entry.relative_parts[: len(target.relative_parts)] == target.relative_parts
                for entry in plan.entries
            ),
        )
        for target in plan.targets
    )


def _verify_normalized_paper_tree(
    original: _InspectionPlan,
    *,
    created_entries: dict[tuple[str, ...], _Entry],
    uid: int,
    gid: int,
) -> None:
    final = _build_inspection_plan(
        original.anchor,
        (target.path for target in original.targets),
        allow_overlaps=False,
    )
    _validate_paper_plan(final)
    expected = {entry.relative_parts: entry for entry in original.entries}
    expected.update(created_entries)
    actual = {entry.relative_parts: entry for entry in final.entries}
    if set(actual) != set(expected):
        raise RuntimeError("paper runtime tree entries changed during normalization")
    for parts, entry in actual.items():
        planned = expected[parts]
        if (
            (entry.device, entry.inode, entry.file_type, entry.mount_id)
            != (planned.device, planned.inode, planned.file_type, planned.mount_id)
        ):
            raise RuntimeError(f"paper runtime entry changed during normalization: {entry.path}")
        expected_mode = 0o700 if entry.file_type == stat.S_IFDIR else 0o600
        if entry.uid != uid or entry.gid != gid or entry.mode != expected_mode:
            raise RuntimeError(f"paper runtime permissions changed during normalization: {entry.path}")


def _continuous_target_path(
    anchor: Path,
    targets: Sequence[_Target],
    continuous_root: str | Path | None,
) -> Path | None:
    if continuous_root is None:
        return None
    normalized, _parts = _normalize_target(anchor, continuous_root)
    if normalized not in {target.path for target in targets}:
        raise ValueError("continuous demo root must also be one of the demo runtime roots")
    return normalized


def _validate_demo_plan(
    plan: _InspectionPlan,
    *,
    continuous_path: Path | None,
) -> None:
    entries = {entry.relative_parts: entry for entry in plan.entries}
    for target in plan.targets:
        if not target.exists:
            continue
        root = entries[target.relative_parts]
        if root.file_type != stat.S_IFDIR:
            raise ValueError(f"demo runtime root must be a real directory: {target.path}")
        cache_parts = (*target.relative_parts, ".cache")
        lock_parts = (*target.relative_parts, ".locks")
        kline_parts = (*cache_parts, "ws_klines")
        snapshot_parts = (*kline_parts, "store.parquet")
        for parts, label in ((cache_parts, "cache"), (kline_parts, "kline cache")):
            entry = entries.get(parts)
            if entry is not None and entry.file_type != stat.S_IFDIR:
                raise ValueError(f"demo {label} must be a real directory: {entry.path}")
        lock_entry = entries.get(lock_parts)
        if lock_entry is not None and lock_entry.file_type != stat.S_IFDIR:
            raise ValueError(f"demo lock namespace must be a real directory: {lock_entry.path}")
        snapshot = entries.get(snapshot_parts)
        if snapshot is not None and snapshot.file_type != stat.S_IFREG:
            raise ValueError(f"demo kline snapshot must be a regular file: {snapshot.path}")
        residual_parts = (*target.relative_parts, "residual_momentum.parquet")
        residual = entries.get(residual_parts)
        if target.path == continuous_path:
            if residual is not None and residual.file_type != stat.S_IFREG:
                raise ValueError(f"demo residual momentum input must be a regular file: {residual.path}")


def preflight_demo_runtime_roots(
    anchor: str | Path,
    roots: Iterable[str | Path],
    *,
    continuous_root: str | Path | None = None,
) -> tuple[ResetTargetInspection, ...]:
    """Validate all demo roots/components before archive or reset mutations."""
    plan = _build_inspection_plan(anchor, roots, allow_overlaps=False)
    continuous_path = _continuous_target_path(plan.anchor, plan.targets, continuous_root)
    _validate_demo_plan(plan, continuous_path=continuous_path)
    return tuple(
        ResetTargetInspection(
            path=target.path,
            exists=target.exists,
            entries=sum(
                entry.relative_parts[: len(target.relative_parts)] == target.relative_parts
                for entry in plan.entries
            ),
        )
        for target in plan.targets
    )


def _create_bound_directory(
    *,
    parent_fd: int,
    name: str,
    path: Path,
    relative_parts: tuple[str, ...],
    anchor_entry: _Entry,
) -> tuple[int, _Entry]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"demo runtime directory changed before creation: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = _entry_metadata(parent_fd, name)
        mount_id = _mount_id_for_fd(descriptor)
        identity = (opened.st_dev, opened.st_ino, _file_type(opened.st_mode))
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino, _file_type(current.st_mode)) != identity
            or opened.st_dev != anchor_entry.device
            or mount_id != anchor_entry.mount_id
        ):
            raise RuntimeError(f"demo runtime directory changed during creation: {path}")
        return descriptor, _snapshot(
            path=path,
            relative_parts=relative_parts,
            metadata=opened,
            mount_id=mount_id,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _normalize_created_directory(
    *,
    parent_fd: int,
    name: str,
    descriptor: int,
    planned: _Entry,
    uid: int,
    gid: int,
    mode: int,
) -> _Entry:
    changed = False
    try:
        changed = True
        _set_descriptor_permissions(descriptor, uid=uid, gid=gid, mode=mode, path=planned.path)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        current = _entry_metadata(parent_fd, name)
        mount_id = _mount_id_for_fd(descriptor)
        if (
            (opened.st_dev, opened.st_ino, _file_type(opened.st_mode))
            != (planned.device, planned.inode, planned.file_type)
            or (current.st_dev, current.st_ino, _file_type(current.st_mode))
            != (planned.device, planned.inode, planned.file_type)
            or mount_id != planned.mount_id
            or current.st_uid != uid
            or current.st_gid != gid
            or stat.S_IMODE(current.st_mode) != mode
        ):
            raise RuntimeError(f"demo runtime directory changed during normalization: {planned.path}")
        os.fsync(parent_fd)
        return _snapshot(
            path=planned.path,
            relative_parts=planned.relative_parts,
            metadata=opened,
            mount_id=mount_id,
        )
    except BaseException:
        if changed:
            try:
                _restore_descriptor_permissions(descriptor, planned)
            except OSError as restore_exc:
                raise RuntimeError(
                    f"demo runtime directory changed and original permissions could not be restored: {planned.path}"
                ) from restore_exc
        raise


def normalize_demo_runtime_roots(
    anchor: str | Path,
    roots: Iterable[str | Path],
    *,
    uid: int,
    gid: int,
    continuous_root: str | Path | None = None,
    create_missing: bool = False,
) -> tuple[DemoRootNormalization, ...]:
    """Normalize only the shared demo root/cache contract by descriptor.

    Missing roots remain absent unless ``create_missing`` is selected for
    direct children of the trusted anchor.  Roots are planned as a batch, then
    ``.cache/ws_klines`` is created without following path components.  The
    root and cache use ``2710``, the kline directory uses ``2750``, and the
    optional snapshot and continuous residual input use ``0640``.
    """
    if uid < 0 or gid < 0:
        raise ValueError("demo runtime uid and gid must be non-negative")
    plan = _build_inspection_plan(anchor, roots, allow_overlaps=False)
    continuous_path = _continuous_target_path(plan.anchor, plan.targets, continuous_root)
    _validate_demo_plan(plan, continuous_path=continuous_path)
    if create_missing:
        for target in plan.targets:
            if (
                not target.exists
                and (len(target.relative_parts) != 1 or target.missing_at != target.relative_parts)
            ):
                raise ValueError(f"only missing direct-child demo roots may be created: {target.path}")
    entries = {entry.relative_parts: entry for entry in plan.entries}
    context = _ApplyContext(plan)
    created: dict[tuple[str, ...], _Entry] = {}
    created_descriptors: dict[tuple[str, ...], int] = {}
    created_root_descriptors: dict[tuple[str, ...], int] = {}
    handles: dict[tuple[str, ...], tuple[int, int, int, int]] = {}
    try:
        for target in plan.targets:
            if not target.exists:
                _assert_missing_target(context, target)
                if not create_missing:
                    continue
                root_fd, root_entry = _create_bound_directory(
                    parent_fd=context.directory_fd(()),
                    name=target.relative_parts[0],
                    path=target.path,
                    relative_parts=target.relative_parts,
                    anchor_entry=plan.anchor_entry,
                )
                created[target.relative_parts] = root_entry
                created_root_descriptors[target.relative_parts] = root_fd
            else:
                root_fd = context.directory_fd(target.relative_parts)
            lock_parts = (*target.relative_parts, ".locks")
            lock_entry = entries.get(lock_parts)
            if lock_entry is None:
                lock_fd, lock_entry = _create_bound_directory(
                    parent_fd=root_fd,
                    name=".locks",
                    path=target.path / ".locks",
                    relative_parts=lock_parts,
                    anchor_entry=plan.anchor_entry,
                )
                created[lock_parts] = lock_entry
                created_descriptors[lock_parts] = lock_fd
            else:
                lock_fd = context.directory_fd(lock_parts)
            cache_parts = (*target.relative_parts, ".cache")
            cache_entry = entries.get(cache_parts)
            if cache_entry is None:
                cache_fd, cache_entry = _create_bound_directory(
                    parent_fd=root_fd,
                    name=".cache",
                    path=target.path / ".cache",
                    relative_parts=cache_parts,
                    anchor_entry=plan.anchor_entry,
                )
                created[cache_parts] = cache_entry
                created_descriptors[cache_parts] = cache_fd
            else:
                cache_fd = context.directory_fd(cache_parts)
            kline_parts = (*cache_parts, "ws_klines")
            kline_entry = entries.get(kline_parts)
            if kline_entry is None:
                kline_fd, kline_entry = _create_bound_directory(
                    parent_fd=cache_fd,
                    name="ws_klines",
                    path=target.path / ".cache" / "ws_klines",
                    relative_parts=kline_parts,
                    anchor_entry=plan.anchor_entry,
                )
                created[kline_parts] = kline_entry
                created_descriptors[kline_parts] = kline_fd
            else:
                kline_fd = context.directory_fd(kline_parts)
            handles[target.relative_parts] = (root_fd, lock_fd, cache_fd, kline_fd)

        for target in plan.targets:
            if not target.exists and target.relative_parts not in created_root_descriptors:
                continue
            root_fd, lock_fd, cache_fd, kline_fd = handles[target.relative_parts]
            lock_parts = (*target.relative_parts, ".locks")
            cache_parts = (*target.relative_parts, ".cache")
            kline_parts = (*cache_parts, "ws_klines")
            snapshot = entries.get((*kline_parts, "store.parquet"))
            residual = entries.get((*target.relative_parts, "residual_momentum.parquet"))
            if snapshot is not None:
                _normalize_planned_regular(context, snapshot, uid=uid, gid=gid, mode=0o640)
            if target.path == continuous_path and residual is not None:
                _normalize_planned_regular(context, residual, uid=uid, gid=gid, mode=0o640)

            lock_entry = entries.get(lock_parts)
            if lock_entry is None:
                created[lock_parts] = _normalize_created_directory(
                    parent_fd=root_fd,
                    name=".locks",
                    descriptor=lock_fd,
                    planned=created[lock_parts],
                    uid=uid,
                    gid=gid,
                    mode=0o700,
                )
            else:
                _normalize_planned_directory(context, lock_entry, uid=uid, gid=gid, mode=0o700)

            kline_entry = entries.get(kline_parts)
            if kline_entry is None:
                created[kline_parts] = _normalize_created_directory(
                    parent_fd=cache_fd,
                    name="ws_klines",
                    descriptor=kline_fd,
                    planned=created[kline_parts],
                    uid=uid,
                    gid=gid,
                    mode=0o2750,
                )
            else:
                _normalize_planned_directory(context, kline_entry, uid=uid, gid=gid, mode=0o2750)
            cache_entry = entries.get(cache_parts)
            if cache_entry is None:
                created[cache_parts] = _normalize_created_directory(
                    parent_fd=root_fd,
                    name=".cache",
                    descriptor=cache_fd,
                    planned=created[cache_parts],
                    uid=uid,
                    gid=gid,
                    mode=0o2710,
                )
            else:
                _normalize_planned_directory(context, cache_entry, uid=uid, gid=gid, mode=0o2710)
            planned_root_entry = entries.get(target.relative_parts)
            if planned_root_entry is None:
                created[target.relative_parts] = _normalize_created_directory(
                    parent_fd=context.directory_fd(()),
                    name=target.relative_parts[0],
                    descriptor=root_fd,
                    planned=created[target.relative_parts],
                    uid=uid,
                    gid=gid,
                    mode=0o2710,
                )
            else:
                _normalize_planned_directory(context, planned_root_entry, uid=uid, gid=gid, mode=0o2710)
            os.fsync(root_fd)

        context._validate_anchor()
        _verify_normalized_demo_tree(
            plan,
            continuous_path=continuous_path,
            created=created,
            uid=uid,
            gid=gid,
        )
    finally:
        for descriptor in created_descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        for descriptor in created_root_descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        context.close()

    results: list[DemoRootNormalization] = []
    for target in plan.targets:
        cache_parts = (*target.relative_parts, ".cache")
        lock_parts = (*target.relative_parts, ".locks")
        kline_parts = (*cache_parts, "ws_klines")
        results.append(
            DemoRootNormalization(
                path=target.path,
                existed=target.exists,
                root_created=target.relative_parts in created_root_descriptors,
                lock_directory_created=lock_parts in created_descriptors,
                cache_directory_created=cache_parts in created,
                kline_directory_created=kline_parts in created,
                snapshot_normalized=(*kline_parts, "store.parquet") in entries,
                residual_momentum_normalized=(
                    target.path == continuous_path
                    and (*target.relative_parts, "residual_momentum.parquet") in entries
                ),
            )
        )
    return tuple(results)


def _verify_normalized_demo_tree(
    original: _InspectionPlan,
    *,
    continuous_path: Path | None,
    created: dict[tuple[str, ...], _Entry],
    uid: int,
    gid: int,
) -> None:
    final = _build_inspection_plan(
        original.anchor,
        (target.path for target in original.targets),
        allow_overlaps=False,
    )
    _validate_demo_plan(final, continuous_path=continuous_path)
    expected = {entry.relative_parts: entry for entry in original.entries}
    expected.update(created)
    actual = {entry.relative_parts: entry for entry in final.entries}
    if set(actual) != set(expected):
        raise RuntimeError("demo runtime tree entries changed during normalization")
    target_parts = {
        target.path: target.relative_parts
        for target in original.targets
        if target.exists or target.relative_parts in created
    }
    special_modes: dict[tuple[str, ...], int] = {}
    for path, parts in target_parts.items():
        cache_parts = (*parts, ".cache")
        lock_parts = (*parts, ".locks")
        kline_parts = (*cache_parts, "ws_klines")
        special_modes[parts] = 0o2710
        special_modes[lock_parts] = 0o700
        special_modes[cache_parts] = 0o2710
        special_modes[kline_parts] = 0o2750
        if (*kline_parts, "store.parquet") in expected:
            special_modes[(*kline_parts, "store.parquet")] = 0o640
        if path == continuous_path and (*parts, "residual_momentum.parquet") in expected:
            special_modes[(*parts, "residual_momentum.parquet")] = 0o640
    for parts, entry in actual.items():
        planned = expected[parts]
        if (
            (entry.device, entry.inode, entry.file_type, entry.mount_id)
            != (planned.device, planned.inode, planned.file_type, planned.mount_id)
        ):
            raise RuntimeError(f"demo runtime entry changed during normalization: {entry.path}")
        expected_mode = special_modes.get(parts)
        if expected_mode is not None:
            if entry.uid != uid or entry.gid != gid or entry.mode != expected_mode:
                raise RuntimeError(f"demo runtime permissions changed during normalization: {entry.path}")
        elif entry.uid != planned.uid or entry.gid != planned.gid or entry.mode != planned.mode:
            raise RuntimeError(f"unrelated demo runtime permissions changed during normalization: {entry.path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="validate targets before archive creation")
    preflight.add_argument("--anchor", required=True)
    preflight.add_argument("--target", action="append", default=[])
    preflight.add_argument("--reject-symlinks", action="store_true")

    preflight_paper = subparsers.add_parser(
        "preflight-paper",
        help="validate complete private paper trees before archive creation",
    )
    preflight_paper.add_argument("--anchor", required=True)
    preflight_paper.add_argument("--root", action="append", default=[])

    preflight_demo = subparsers.add_parser(
        "preflight-demo",
        help="validate demo cache and shared-input components before mutation",
    )
    preflight_demo.add_argument("--anchor", required=True)
    preflight_demo.add_argument("--root", action="append", default=[])
    preflight_demo.add_argument("--continuous-root")

    remove = subparsers.add_parser("remove", help="remove a prevalidated batch below the anchor")
    remove.add_argument("--anchor", required=True)
    remove.add_argument("--target", action="append", default=[])

    normalize = subparsers.add_parser("normalize-paper", help="normalize private paper runtime trees")
    normalize.add_argument("--anchor", required=True)
    normalize.add_argument("--root", action="append", default=[])
    normalize.add_argument("--uid", required=True, type=int)
    normalize.add_argument("--gid", required=True, type=int)
    normalize.add_argument("--create-missing", action="store_true")

    normalize_demo = subparsers.add_parser("normalize-demo", help="normalize shared demo cache paths")
    normalize_demo.add_argument("--anchor", required=True)
    normalize_demo.add_argument("--root", action="append", default=[])
    normalize_demo.add_argument("--uid", required=True, type=int)
    normalize_demo.add_argument("--gid", required=True, type=int)
    normalize_demo.add_argument("--continuous-root")
    normalize_demo.add_argument("--create-missing", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            inspected = preflight_reset_targets(
                args.anchor,
                args.target,
                reject_symlinks=args.reject_symlinks,
            )
            for inspection in inspected:
                print(
                    f"preflight path={inspection.path} exists={int(inspection.exists)} "
                    f"entries={inspection.entries}"
                )
        elif args.command == "preflight-paper":
            inspected = preflight_paper_runtime_roots(args.anchor, args.root)
            for inspection in inspected:
                print(
                    f"preflight-paper path={inspection.path} exists={int(inspection.exists)} "
                    f"entries={inspection.entries}"
                )
        elif args.command == "preflight-demo":
            inspected = preflight_demo_runtime_roots(
                args.anchor,
                args.root,
                continuous_root=args.continuous_root,
            )
            for inspection in inspected:
                print(
                    f"preflight-demo path={inspection.path} exists={int(inspection.exists)} "
                    f"entries={inspection.entries}"
                )
        elif args.command == "remove":
            removed = remove_reset_targets(args.anchor, args.target)
            for removal in removed:
                print(
                    f"removed path={removal.path} existed={int(removal.existed)} "
                    f"entries={removal.removed_entries}"
                )
        elif args.command == "normalize-paper":
            normalized = normalize_paper_runtime_roots(
                args.anchor,
                args.root,
                uid=args.uid,
                gid=args.gid,
                create_missing=args.create_missing,
            )
            for paper_normalization in normalized:
                print(
                    f"normalized path={paper_normalization.path} existed={int(paper_normalization.existed)} "
                    f"root_created={int(paper_normalization.root_created)} "
                    f"directories={paper_normalization.normalized_directories} "
                    f"files={paper_normalization.normalized_files} "
                    f"lock_created={int(paper_normalization.lock_directory_created)}"
                )
        else:
            normalized_demo = normalize_demo_runtime_roots(
                args.anchor,
                args.root,
                uid=args.uid,
                gid=args.gid,
                continuous_root=args.continuous_root,
                create_missing=args.create_missing,
            )
            for demo_normalization in normalized_demo:
                print(
                    f"normalized-demo path={demo_normalization.path} existed={int(demo_normalization.existed)} "
                    f"root_created={int(demo_normalization.root_created)} "
                    f"lock_created={int(demo_normalization.lock_directory_created)} "
                    f"cache_created={int(demo_normalization.cache_directory_created)} "
                    f"kline_created={int(demo_normalization.kline_directory_created)} "
                    f"snapshot={int(demo_normalization.snapshot_normalized)} "
                    f"residual={int(demo_normalization.residual_momentum_normalized)}"
                )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"reset path safety failed: {exc}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI contract
    raise SystemExit(main())
