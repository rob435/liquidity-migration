"""Process leases for account owners and Bybit demo mutation authority."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_LINUX_FDINFO_ROOT = Path("/proc/self/fdinfo") if sys.platform.startswith("linux") else None
_CANONICAL_DEMO_LEASE_DIRECTORY = Path("/run/lock/liquidity-migration")


def _credential_fingerprint(api_key: str) -> str:
    normalized = str(api_key).strip()
    if not normalized:
        raise ValueError("demo API key is required for account identity")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_AUTHENTICATED_IDENTITY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class DemoAccountIdentity:
    """Credential-bound identity of one Bybit demo account."""

    user_id: str
    api_key_sha256: str = field(repr=False)
    environment: str = "demo"

    def __init__(
        self,
        *,
        user_id: str,
        api_key_sha256: str,
        environment: str,
        _authenticated_identity_token: object | None = None,
    ) -> None:
        if _authenticated_identity_token is not _AUTHENTICATED_IDENTITY_TOKEN:
            raise TypeError(
                "DemoAccountIdentity cannot be constructed directly; use "
                "DemoAccountIdentity.from_api_key_info()"
            )
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "api_key_sha256", api_key_sha256)
        object.__setattr__(self, "environment", environment)

    @classmethod
    def from_api_key_info(
        cls,
        *,
        api_key: str,
        api_key_info: Mapping[str, Any],
        environment: str = "demo",
    ) -> "DemoAccountIdentity":
        """Build identity from the authenticated ``query-api`` response.

        ``userID`` is the venue account identity.  The key fingerprint binds a
        held lease capability to the credential that produced that response
        without writing the credential itself to disk.
        """

        normalized_environment = str(environment).strip().lower()
        if normalized_environment != "demo":
            raise RuntimeError(
                "Bybit account mutation identity requires environment='demo'"
            )
        normalized_api_key = str(api_key).strip()
        reported_api_key = str(api_key_info.get("apiKey") or "").strip()
        if not reported_api_key or not hmac.compare_digest(
            normalized_api_key,
            reported_api_key,
        ):
            raise RuntimeError(
                "Bybit API key metadata does not match the configured demo API key"
            )
        raw_user_id = api_key_info.get("userID")
        if isinstance(raw_user_id, bool):
            raw_user_id = None
        try:
            numeric_user_id = int(str(raw_user_id).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Bybit demo API key metadata is missing a valid userID; "
                "cannot derive the canonical account-owner lease"
            ) from exc
        if numeric_user_id <= 0:
            raise RuntimeError(
                "Bybit demo API key metadata has a non-positive userID; "
                "cannot derive the canonical account-owner lease"
            )
        return cls(
            user_id=str(numeric_user_id),
            api_key_sha256=_credential_fingerprint(normalized_api_key),
            environment=normalized_environment,
            _authenticated_identity_token=_AUTHENTICATED_IDENTITY_TOKEN,
        )


def _canonical_demo_lease_directory() -> Path:
    """Return the fixed host-global namespace; there is no environment override."""

    return _CANONICAL_DEMO_LEASE_DIRECTORY


def canonical_demo_account_lease_path(identity: DemoAccountIdentity) -> Path:
    """Canonical lock path shared by all credentials for one demo account."""

    return _canonical_demo_lease_directory() / (
        f"bybit-{identity.environment}-user-{identity.user_id}.lock"
    )


def _is_canonical_demo_account_lease_path(path: Path) -> bool:
    prefix = "bybit-demo-user-"
    suffix = ".lock"
    if path.parent != _CANONICAL_DEMO_LEASE_DIRECTORY:
        return False
    name = path.name
    user_id = name[len(prefix) : -len(suffix)] if name.startswith(prefix) and name.endswith(suffix) else ""
    return user_id.isdigit() and int(user_id) > 0


@dataclass(frozen=True, slots=True)
class PreparedAccountOwnerLease:
    """Identity receipt for one safely prepared, content-preserving lease file."""

    path: Path
    device: int
    inode: int
    uid: int
    gid: int
    mount_id: int | None
    parent_device: int
    parent_inode: int
    parent_uid: int
    parent_gid: int
    parent_mount_id: int | None


class AccountOwnerLeaseAlreadyHeldError(RuntimeError):
    """The prepared inode is safe, but another open description holds its flock."""


@dataclass(slots=True)
class _LeaseDirectoryChain:
    descriptors: list[int]
    names: tuple[str, ...]
    created_parent: bool

    @property
    def parent_fd(self) -> int:
        return self.descriptors[-1]

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


def _lease_directory_open_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - required on supported POSIX deployments
        raise RuntimeError("account owner leases require O_DIRECTORY, O_NOFOLLOW, and O_CLOEXEC") from exc


def _lease_file_open_flags() -> int:
    try:
        return os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - required on supported POSIX deployments
        raise RuntimeError("account owner leases require O_NOFOLLOW and O_CLOEXEC") from exc


def _existing_lease_file_open_flags() -> int:
    try:
        return os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - required on supported POSIX deployments
        raise RuntimeError("account owner leases require O_NOFOLLOW and O_CLOEXEC") from exc


def _lease_mount_id_for_fd(descriptor: int) -> int | None:
    if _LINUX_FDINFO_ROOT is None:
        return None
    try:
        payload = (_LINUX_FDINFO_ROOT / str(descriptor)).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"account owner lease mount identity is unavailable for fd {descriptor}") from exc
    values = [line.partition(":")[2].strip() for line in payload.splitlines() if line.partition(":")[0] == "mnt_id"]
    if len(values) != 1:
        raise RuntimeError(f"account owner lease mount identity is invalid for fd {descriptor}")
    try:
        mount_id = int(values[0], 10)
    except ValueError as exc:
        raise RuntimeError(f"account owner lease mount identity is invalid for fd {descriptor}") from exc
    if mount_id < 0:
        raise RuntimeError(f"account owner lease mount identity is invalid for fd {descriptor}")
    return mount_id


def _lease_entry_metadata(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _canonical_demo_expected_owner() -> tuple[int, int]:
    return 0, 0


def _validate_lease_directory_chain(chain: _LeaseDirectoryChain, parent: Path) -> os.stat_result:
    root = os.fstat(chain.descriptors[0])
    if not stat.S_ISDIR(root.st_mode):  # pragma: no cover - opening / as O_DIRECTORY proves this
        raise RuntimeError("account owner lease filesystem root is not a directory")
    opened_names = chain.names[: len(chain.descriptors) - 1]
    for index, name in enumerate(opened_names, start=1):
        opened = os.fstat(chain.descriptors[index])
        current = _lease_entry_metadata(chain.descriptors[index - 1], name)
        identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != identity
        ):
            raise RuntimeError(f"account owner lease parent namespace changed: {parent}")
    return os.fstat(chain.parent_fd)


def _require_canonical_demo_anchor(chain: _LeaseDirectoryChain, parent: Path) -> None:
    if parent != _CANONICAL_DEMO_LEASE_DIRECTORY:
        return
    if len(chain.names) < 3 or len(chain.descriptors) < len(chain.names):
        raise RuntimeError(f"canonical demo lease namespace has no /run/lock anchor: {parent}")
    expected_uid, expected_gid = _canonical_demo_expected_owner()
    run_fd = chain.descriptors[len(chain.names) - 2]
    lock_fd = chain.descriptors[len(chain.names) - 1]
    run = os.fstat(run_fd)
    lock = os.fstat(lock_fd)
    run_mode = stat.S_IMODE(run.st_mode)
    lock_mode = stat.S_IMODE(lock.st_mode)
    if (
        not stat.S_ISDIR(run.st_mode)
        or not stat.S_ISDIR(lock.st_mode)
        or (run.st_uid, run.st_gid) != (expected_uid, expected_gid)
        or (lock.st_uid, lock.st_gid) != (expected_uid, expected_gid)
        or run_mode & 0o700 != 0o700
        or lock_mode & 0o700 != 0o700
        or run_mode & stat.S_IWGRP
        or (run_mode & stat.S_IWOTH and not run_mode & stat.S_ISVTX)
        or (lock_mode & stat.S_IWOTH and not lock_mode & stat.S_ISVTX)
    ):
        raise RuntimeError(f"canonical demo lease /run/lock anchor is not trusted: {parent.parent}")


def _open_lease_directory_chain(parent: Path, *, allow_create_parent: bool) -> _LeaseDirectoryChain:
    if not parent.is_absolute():  # pragma: no cover - callers normalize first
        raise RuntimeError(f"account owner lease parent must be absolute: {parent}")
    names = tuple(parent.parts[1:])
    try:
        root_fd = os.open("/", _lease_directory_open_flags())
    except OSError as exc:  # pragma: no cover - a usable POSIX host always opens /
        raise RuntimeError("account owner lease filesystem root cannot be opened") from exc
    chain = _LeaseDirectoryChain(descriptors=[root_fd], names=names, created_parent=False)
    try:
        for index, name in enumerate(names):
            is_parent = index == len(names) - 1
            try:
                descriptor = os.open(name, _lease_directory_open_flags(), dir_fd=chain.parent_fd)
            except FileNotFoundError as exc:
                if not is_parent or not allow_create_parent:
                    raise RuntimeError(
                        f"account owner lease parent namespace cannot be opened safely: {parent}"
                    ) from exc
                _validate_lease_directory_chain(chain, parent)
                _require_canonical_demo_anchor(chain, parent)
                try:
                    os.mkdir(name, 0o700, dir_fd=chain.parent_fd)
                    descriptor = os.open(name, _lease_directory_open_flags(), dir_fd=chain.parent_fd)
                except OSError as create_exc:
                    raise RuntimeError(
                        f"account owner lease parent namespace cannot be created safely: {parent}"
                    ) from create_exc
                chain.created_parent = True
            except OSError as exc:
                raise RuntimeError(
                    f"account owner lease parent namespace cannot be opened safely: {parent}"
                ) from exc
            chain.descriptors.append(descriptor)
            _validate_lease_directory_chain(chain, parent)
        return chain
    except BaseException:
        chain.close()
        raise


def _require_private_lease_parent(
    chain: _LeaseDirectoryChain,
    parent: Path,
    *,
    normalize_mode: bool,
    allow_private_parent_mount_boundary: bool = False,
) -> os.stat_result:
    metadata = _validate_lease_directory_chain(chain, parent)
    _require_canonical_demo_anchor(chain, parent)
    if len(chain.descriptors) < 2:
        raise RuntimeError(f"account owner lease parent namespace is not private: {parent}")
    ancestor = os.fstat(chain.descriptors[-2])
    parent_mount_id = _lease_mount_id_for_fd(chain.parent_fd)
    ancestor_mount_id = _lease_mount_id_for_fd(chain.descriptors[-2])
    mode = stat.S_IMODE(metadata.st_mode)
    accepted_private_mount_boundary = (
        allow_private_parent_mount_boundary
        and parent != _CANONICAL_DEMO_LEASE_DIRECTORY
        and os.geteuid() != 0
        and metadata.st_uid == os.geteuid()
    )
    if (
        metadata.st_dev != ancestor.st_dev
        or (
            parent_mount_id != ancestor_mount_id
            and not accepted_private_mount_boundary
        )
        or mode & 0o700 != 0o700
        or mode & 0o022
        or (os.geteuid() != 0 and metadata.st_uid != os.geteuid())
        or (
            parent == _CANONICAL_DEMO_LEASE_DIRECTORY
            and (metadata.st_uid, metadata.st_gid) != _canonical_demo_expected_owner()
        )
    ):
        raise RuntimeError(f"account owner lease parent namespace is not owner-controlled: {parent}")
    if mode != 0o700 and not normalize_mode:
        raise RuntimeError(f"account owner lease parent namespace is not private: {parent}")
    if mode != 0o700:
        os.fchmod(chain.parent_fd, 0o700)
    if normalize_mode:
        os.fsync(chain.parent_fd)
        if chain.created_parent:
            os.fsync(chain.descriptors[-2])
    final = _validate_lease_directory_chain(chain, parent)
    if (
        stat.S_IMODE(final.st_mode) != 0o700
        or final.st_uid != metadata.st_uid
        or final.st_gid != metadata.st_gid
        or _lease_mount_id_for_fd(chain.parent_fd) != parent_mount_id
    ):
        raise RuntimeError(f"account owner lease parent namespace changed: {parent}")
    return final


def _require_lease_leaf(
    *,
    path: Path,
    descriptor: int,
    parent_fd: int,
    parent_metadata: os.stat_result,
    require_parent_ownership: bool,
) -> tuple[os.stat_result, int | None]:
    opened = os.fstat(descriptor)
    current = _lease_entry_metadata(parent_fd, path.name)
    parent_mount_id = _lease_mount_id_for_fd(parent_fd)
    mount_id = _lease_mount_id_for_fd(descriptor)
    identity = (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != identity
        or opened.st_nlink != 1
        or current.st_nlink != 1
        or opened.st_dev != parent_metadata.st_dev
        or mount_id != parent_mount_id
        or (require_parent_ownership and (opened.st_uid, opened.st_gid) != (parent_metadata.st_uid, parent_metadata.st_gid))
        or (require_parent_ownership and (current.st_uid, current.st_gid) != (parent_metadata.st_uid, parent_metadata.st_gid))
        or (require_parent_ownership and stat.S_IMODE(opened.st_mode) != 0o600)
        or (require_parent_ownership and stat.S_IMODE(current.st_mode) != 0o600)
    ):
        raise RuntimeError(
            "account execution owner lease must be one path-bound, "
            f"single-link regular file on its parent mount: {path}"
        )
    return opened, mount_id


def _prepared_receipt(
    *,
    path: Path,
    leaf: os.stat_result,
    leaf_mount_id: int | None,
    parent: os.stat_result,
    parent_mount_id: int | None,
) -> PreparedAccountOwnerLease:
    return PreparedAccountOwnerLease(
        path=path,
        device=leaf.st_dev,
        inode=leaf.st_ino,
        uid=leaf.st_uid,
        gid=leaf.st_gid,
        mount_id=leaf_mount_id,
        parent_device=parent.st_dev,
        parent_inode=parent.st_ino,
        parent_uid=parent.st_uid,
        parent_gid=parent.st_gid,
        parent_mount_id=parent_mount_id,
    )


def _validate_prepared_account_owner_lease(
    path: Path,
    descriptor: int,
    prepared: PreparedAccountOwnerLease,
    *,
    allow_private_parent_mount_boundary: bool = False,
) -> None:
    chain = _open_lease_directory_chain(path.parent, allow_create_parent=False)
    try:
        parent = _require_private_lease_parent(
            chain,
            path.parent,
            normalize_mode=False,
            allow_private_parent_mount_boundary=allow_private_parent_mount_boundary,
        )
        leaf, mount_id = _require_lease_leaf(
            path=path,
            descriptor=descriptor,
            parent_fd=chain.parent_fd,
            parent_metadata=parent,
            require_parent_ownership=True,
        )
        if (
            (leaf.st_dev, leaf.st_ino, leaf.st_uid, leaf.st_gid)
            != (prepared.device, prepared.inode, prepared.uid, prepared.gid)
            or mount_id != prepared.mount_id
            or (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_gid)
            != (
                prepared.parent_device,
                prepared.parent_inode,
                prepared.parent_uid,
                prepared.parent_gid,
            )
            or _lease_mount_id_for_fd(chain.parent_fd) != prepared.parent_mount_id
        ):
            raise RuntimeError(f"account execution owner lease path changed during preparation: {path}")
    finally:
        chain.close()


def _open_prepared_account_owner_lease(
    path: Path,
    *,
    allow_private_parent_mount_boundary: bool = False,
) -> tuple[int, PreparedAccountOwnerLease]:
    chain = _open_lease_directory_chain(path.parent, allow_create_parent=True)
    descriptor = -1
    try:
        parent = _require_private_lease_parent(
            chain,
            path.parent,
            normalize_mode=True,
            allow_private_parent_mount_boundary=allow_private_parent_mount_boundary,
        )
        try:
            descriptor = os.open(path.name, _lease_file_open_flags(), 0o600, dir_fd=chain.parent_fd)
        except OSError as exc:
            raise RuntimeError(f"account execution owner lease cannot be opened safely: {path}") from exc
        leaf, _mount_id = _require_lease_leaf(
            path=path,
            descriptor=descriptor,
            parent_fd=chain.parent_fd,
            parent_metadata=parent,
            require_parent_ownership=False,
        )
        if (leaf.st_uid, leaf.st_gid) != (parent.st_uid, parent.st_gid):
            if os.geteuid() != 0:
                raise RuntimeError(f"account execution owner lease is not owned by its parent owner: {path}")
            os.fchown(descriptor, parent.st_uid, parent.st_gid)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.fsync(chain.parent_fd)
        final_leaf, final_mount_id = _require_lease_leaf(
            path=path,
            descriptor=descriptor,
            parent_fd=chain.parent_fd,
            parent_metadata=parent,
            require_parent_ownership=True,
        )
        prepared = _prepared_receipt(
            path=path,
            leaf=final_leaf,
            leaf_mount_id=final_mount_id,
            parent=parent,
            parent_mount_id=_lease_mount_id_for_fd(chain.parent_fd),
        )
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        chain.close()
    try:
        _validate_prepared_account_owner_lease(
            path,
            descriptor,
            prepared,
            allow_private_parent_mount_boundary=allow_private_parent_mount_boundary,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, prepared


def prepare_account_owner_lease(path: str | Path) -> PreparedAccountOwnerLease:
    """Prepare a durable lease inode without locking or altering its contents."""

    normalized = Path(os.path.abspath(Path(path).expanduser()))
    descriptor, prepared = _open_prepared_account_owner_lease(normalized)
    os.close(descriptor)
    return prepared


def _inspect_inherited_account_owner_lease(
    *,
    path: Path,
    descriptor: int,
    expected_device: int,
    expected_inode: int,
) -> PreparedAccountOwnerLease:
    chain = _open_lease_directory_chain(path.parent, allow_create_parent=False)
    try:
        parent = _require_private_lease_parent(chain, path.parent, normalize_mode=False)
        leaf, mount_id = _require_lease_leaf(
            path=path,
            descriptor=descriptor,
            parent_fd=chain.parent_fd,
            parent_metadata=parent,
            require_parent_ownership=True,
        )
        if (leaf.st_dev, leaf.st_ino) != (expected_device, expected_inode):
            raise RuntimeError(f"inherited account execution owner lease has the wrong identity: {path}")
        return _prepared_receipt(
            path=path,
            leaf=leaf,
            leaf_mount_id=mount_id,
            parent=parent,
            parent_mount_id=_lease_mount_id_for_fd(chain.parent_fd),
        )
    finally:
        chain.close()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - regular blocking file writes make progress or raise
            raise OSError("account owner lease metadata write made no progress")
        offset += written


def acquire_inherited_account_owner_lease(
    descriptor: int,
    path: str | Path,
    expected_device: int,
    expected_inode: int,
    environment: str,
    role: str,
    *,
    prepared_receipt: PreparedAccountOwnerLease | None = None,
) -> None:
    """Acquire and annotate a shell-owned lease descriptor without reopening it.

    The caller must retain its duplicate of ``descriptor`` after this function
    (or helper subprocess) returns. ``flock`` then remains attached to that
    shared open-file description until the caller closes its final duplicate.
    """

    normalized = Path(os.path.abspath(Path(path).expanduser()))
    if descriptor < 0 or expected_device < 0 or expected_inode <= 0:
        raise ValueError("inherited account owner lease descriptor and identity must be positive")
    normalized_environment = str(environment).strip().lower()
    if normalized_environment not in {"demo", "paper"}:
        raise ValueError("account owner lease environment must be demo or paper")
    if normalized_environment == "demo" and not _is_canonical_demo_account_lease_path(normalized):
        raise ValueError("demo account owner lease must use its canonical Bybit /run/lock identity path")
    normalized_role = str(role).strip()
    if not normalized_role:
        raise ValueError("account owner lease role must be non-empty")

    if prepared_receipt is None:
        prepared = _inspect_inherited_account_owner_lease(
            path=normalized,
            descriptor=descriptor,
            expected_device=expected_device,
            expected_inode=expected_inode,
        )
    else:
        prepared = prepared_receipt
        if (
            prepared.path != normalized
            or (prepared.device, prepared.inode) != (expected_device, expected_inode)
        ):
            raise ValueError("prepared account owner lease receipt does not match the inherited path identity")
        _validate_prepared_account_owner_lease(normalized, descriptor, prepared)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise AccountOwnerLeaseAlreadyHeldError(
            f"account execution owner lease is already held: {normalized}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"account execution owner lease cannot be locked: {normalized}") from exc

    _validate_prepared_account_owner_lease(normalized, descriptor, prepared)
    metadata: dict[str, Any] = {
        "environment": normalized_environment,
        "pid": os.getppid(),
        "role": normalized_role,
        "started_at_ns": time.time_ns(),
    }
    if normalized_environment == "demo":
        metadata["venue"] = "bybit"
    payload = (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    _write_all(descriptor, payload)
    os.fsync(descriptor)
    _validate_prepared_account_owner_lease(normalized, descriptor, prepared)


def revalidate_inherited_account_owner_lease(
    descriptor: int,
    prepared: PreparedAccountOwnerLease,
) -> None:
    """Revalidate a held inherited lease without rewriting its file."""

    if descriptor < 0:
        raise ValueError("inherited account owner lease descriptor must be non-negative")
    normalized = Path(os.path.abspath(prepared.path.expanduser()))
    if normalized != prepared.path:
        raise ValueError("prepared account owner lease path must be absolute and normalized")
    try:
        _validate_prepared_account_owner_lease(normalized, descriptor, prepared)
    except RuntimeError as exc:
        raise RuntimeError(
            f"account execution owner lease no longer matches its acquisition receipt: {normalized}"
        ) from exc

    chain = _open_lease_directory_chain(normalized.parent, allow_create_parent=False)
    contender = -1
    lock_is_held = False
    try:
        parent = _require_private_lease_parent(chain, normalized.parent, normalize_mode=False)
        try:
            contender = os.open(
                normalized.name,
                _existing_lease_file_open_flags(),
                dir_fd=chain.parent_fd,
            )
        except OSError as exc:
            raise RuntimeError(
                f"account execution owner lease cannot be reopened for lock revalidation: {normalized}"
            ) from exc
        leaf, mount_id = _require_lease_leaf(
            path=normalized,
            descriptor=contender,
            parent_fd=chain.parent_fd,
            parent_metadata=parent,
            require_parent_ownership=True,
        )
        if (
            (leaf.st_dev, leaf.st_ino, leaf.st_uid, leaf.st_gid)
            != (prepared.device, prepared.inode, prepared.uid, prepared.gid)
            or mount_id != prepared.mount_id
            or (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_gid)
            != (
                prepared.parent_device,
                prepared.parent_inode,
                prepared.parent_uid,
                prepared.parent_gid,
            )
            or _lease_mount_id_for_fd(chain.parent_fd) != prepared.parent_mount_id
        ):
            raise RuntimeError(
                f"account execution owner lease no longer matches its acquisition receipt: {normalized}"
            )
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_is_held = True
        except OSError as exc:
            raise RuntimeError(
                f"account execution owner lease lock cannot be revalidated: {normalized}"
            ) from exc
        else:
            fcntl.flock(contender, fcntl.LOCK_UN)
    finally:
        if contender >= 0:
            os.close(contender)
        chain.close()
    if not lock_is_held:
        raise RuntimeError(f"account execution owner lease is no longer held: {normalized}")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(
            f"account execution owner lease is held by another open description: {normalized}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"inherited account execution owner lease lock cannot be revalidated: {normalized}"
        ) from exc
    try:
        _validate_prepared_account_owner_lease(normalized, descriptor, prepared)
    except RuntimeError as exc:
        raise RuntimeError(
            f"account execution owner lease no longer matches its acquisition receipt: {normalized}"
        ) from exc


class AccountOwnerLease:
    """Kernel-enforced advisory lock held for a local owner's entire lifetime.

    This path-based lease remains appropriate for the paper account, whose
    identity is its canonical local route.  It is deliberately *not* accepted
    as Bybit mutation authority; venue mutation requires the subclass below.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        allow_private_parent_mount_boundary: bool = False,
    ) -> None:
        # Keep the lexical path rather than resolving it: a symlink must be
        # refused, not normalized into an apparently canonical lease target.
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._allow_private_parent_mount_boundary = (
            allow_private_parent_mount_boundary
        )
        self._file: Any | None = None
        self._holder_pid: int | None = None
        self._lease_identity: tuple[int, int] | None = None
        self._prepared_lease: PreparedAccountOwnerLease | None = None

    @property
    def held(self) -> bool:
        handle = self._file
        identity = self._lease_identity
        prepared = self._prepared_lease
        if (
            handle is None
            or handle.closed
            or self._holder_pid != os.getpid()
            or identity is None
            or prepared is None
        ):
            return False
        try:
            descriptor_metadata = os.fstat(handle.fileno())
            _validate_prepared_account_owner_lease(
                self.path,
                handle.fileno(),
                prepared,
                allow_private_parent_mount_boundary=(
                    self._allow_private_parent_mount_boundary
                ),
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            stat.S_ISREG(descriptor_metadata.st_mode)
            and descriptor_metadata.st_nlink == 1
            and (descriptor_metadata.st_dev, descriptor_metadata.st_ino) == identity
        )

    def _lease_metadata(self) -> dict[str, Any]:
        return {"pid": os.getpid()}

    def acquire(self) -> None:
        if self._file is not None:
            if self.held:
                return
            raise RuntimeError("account execution owner lease belongs to another process")
        descriptor, prepared = _open_prepared_account_owner_lease(
            self.path,
            allow_private_parent_mount_boundary=(
                self._allow_private_parent_mount_boundary
            ),
        )
        try:
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip()
            handle.close()
            raise RuntimeError(
                "account execution owner lease is already held"
                + (f": {owner}" if owner else "")
            ) from exc
        except BaseException:
            handle.close()
            raise
        try:
            _validate_prepared_account_owner_lease(
                self.path,
                handle.fileno(),
                prepared,
                allow_private_parent_mount_boundary=(
                    self._allow_private_parent_mount_boundary
                ),
            )
        except BaseException:
            handle.close()
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(self._lease_metadata(), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._file = handle
        self._holder_pid = os.getpid()
        self._lease_identity = (prepared.device, prepared.inode)
        self._prepared_lease = prepared
        if not self.held:
            self.close()
            raise RuntimeError(
                "account execution owner lease path changed during acquisition"
            )

    def close(self) -> None:
        handle = self._file
        holder_pid = self._holder_pid
        self._file = None
        self._holder_pid = None
        self._lease_identity = None
        self._prepared_lease = None
        if handle is None:
            return
        if holder_pid != os.getpid():
            # A fork child inherits the descriptor and its shared open-file
            # description, but not ownership of the parent's lease capability.
            # Explicit LOCK_UN here would release the kernel lock for the parent
            # as well. Closing only the child's duplicate keeps the parent's
            # descriptor—and therefore its lease—intact.
            handle.close()
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "AccountOwnerLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DemoAccountMutationLease(AccountOwnerLease):
    """Held canonical lease capability required for every demo mutation."""

    def __init__(self, identity: DemoAccountIdentity) -> None:
        if type(identity) is not DemoAccountIdentity:
            raise TypeError(
                "DemoAccountMutationLease requires an authenticated "
                "DemoAccountIdentity"
            )
        self._identity = identity
        super().__init__(canonical_demo_account_lease_path(identity))

    @property
    def identity(self) -> DemoAccountIdentity:
        return self._identity

    def acquire(self) -> None:
        try:
            super().acquire()
        except OSError as exc:
            raise RuntimeError(
                f"cannot acquire canonical Bybit demo account lease at {self.path}; "
                "fix host lock-directory permissions (no alternate path is allowed)"
            ) from exc

    def _lease_metadata(self) -> dict[str, Any]:
        return {
            "api_key_sha256": self.identity.api_key_sha256,
            "environment": self.identity.environment,
            "pid": os.getpid(),
            "user_id": self.identity.user_id,
            "venue": "bybit",
        }

    def require_held_for(
        self,
        *,
        api_key: str,
        environment: str,
        action: str,
    ) -> None:
        """Prove this process still owns the canonical credential/account lease."""

        expected_path = canonical_demo_account_lease_path(self.identity)
        if self.path != expected_path or not self.held:
            raise RuntimeError(
                f"Refusing to {action}: the canonical Bybit demo account mutation "
                "lease is not currently held by this process"
            )
        observed_fingerprint = _credential_fingerprint(api_key)
        if not hmac.compare_digest(
            self.identity.api_key_sha256,
            observed_fingerprint,
        ):
            raise RuntimeError(
                f"Refusing to {action}: the held Bybit demo account mutation "
                "lease belongs to a different API credential"
            )
        if not hmac.compare_digest(
            self.identity.environment,
            str(environment).strip().lower(),
        ):
            raise RuntimeError(
                f"Refusing to {action}: the held Bybit account mutation lease "
                "belongs to a different environment"
            )


def _mount_id_field(value: int | None) -> str:
    return "-" if value is None else str(value)


def _parse_mount_id_field(value: str) -> int | None:
    if value == "-":
        return None
    try:
        mount_id = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("mount identity must be a non-negative integer or '-'") from exc
    if mount_id < 0:
        raise argparse.ArgumentTypeError("mount identity must be a non-negative integer or '-'")
    return mount_id


def _prepared_cli_fields(prepared: PreparedAccountOwnerLease) -> tuple[str, ...]:
    return (
        str(prepared.device),
        str(prepared.inode),
        str(prepared.uid),
        str(prepared.gid),
        _mount_id_field(prepared.mount_id),
        str(prepared.parent_device),
        str(prepared.parent_inode),
        str(prepared.parent_uid),
        str(prepared.parent_gid),
        _mount_id_field(prepared.parent_mount_id),
    )


def _add_prepared_receipt_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("expected_device", type=int)
    parser.add_argument("expected_inode", type=int)
    parser.add_argument("expected_uid", type=int)
    parser.add_argument("expected_gid", type=int)
    parser.add_argument("expected_mount_id", type=_parse_mount_id_field)
    parser.add_argument("expected_parent_device", type=int)
    parser.add_argument("expected_parent_inode", type=int)
    parser.add_argument("expected_parent_uid", type=int)
    parser.add_argument("expected_parent_gid", type=int)
    parser.add_argument("expected_parent_mount_id", type=_parse_mount_id_field)


def _prepared_receipt_from_args(args: argparse.Namespace) -> PreparedAccountOwnerLease:
    return PreparedAccountOwnerLease(
        path=Path(os.path.abspath(Path(args.path).expanduser())),
        device=args.expected_device,
        inode=args.expected_inode,
        uid=args.expected_uid,
        gid=args.expected_gid,
        mount_id=args.expected_mount_id,
        parent_device=args.expected_parent_device,
        parent_inode=args.expected_parent_inode,
        parent_uid=args.expected_parent_uid,
        parent_gid=args.expected_parent_gid,
        parent_mount_id=args.expected_parent_mount_id,
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare account-owner lease inodes safely.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="prepare without locking or truncating metadata")
    prepare.add_argument("path")
    acquire = subparsers.add_parser(
        "acquire-inherited",
        help="lock, annotate, and revalidate an inherited shell descriptor",
    )
    acquire.add_argument("fd", type=int)
    acquire.add_argument("path")
    _add_prepared_receipt_arguments(acquire)
    acquire.add_argument("environment", choices=("demo", "paper"))
    acquire.add_argument("role")
    revalidate = subparsers.add_parser(
        "revalidate-inherited",
        help="revalidate a held inherited descriptor without rewriting metadata",
    )
    revalidate.add_argument("fd", type=int)
    revalidate.add_argument("path")
    _add_prepared_receipt_arguments(revalidate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    if args.command == "prepare":
        try:
            prepared = prepare_account_owner_lease(args.path)
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("\t".join(_prepared_cli_fields(prepared)))
        return 0
    if args.command == "acquire-inherited":
        try:
            prepared = _prepared_receipt_from_args(args)
            acquire_inherited_account_owner_lease(
                args.fd,
                args.path,
                args.expected_device,
                args.expected_inode,
                args.environment,
                args.role,
                prepared_receipt=prepared,
            )
        except AccountOwnerLeaseAlreadyHeldError as exc:
            print(str(exc), file=sys.stderr)
            return 73
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "revalidate-inherited":
        try:
            revalidate_inherited_account_owner_lease(
                args.fd,
                _prepared_receipt_from_args(args),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    raise AssertionError(args.command)  # pragma: no cover - argparse constrains the command


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration
    raise SystemExit(main())
