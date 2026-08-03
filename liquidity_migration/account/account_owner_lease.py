"""Process leases for account owners and Bybit demo mutation authority.

One kernel flock per venue account: the lease file is named by the
authenticated Bybit userID, so every process that could mutate the account —
the owner service, maintenance resets, research probes — contends for the
same lock regardless of which config path started it. The lock dies with the
process, so a crashed holder never leaves a stale lease behind.

flock binds to the file's identity, not its name: if the lease file is
deleted or replaced, a second process can lock a fresh file at the same path
and the mutex is silently gone. Every held-check therefore re-proves that
the locked descriptor is still the single regular file at the canonical
path.
"""

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

from liquidity_migration.account.execution_environment import (
    EXECUTION_ENVIRONMENT_CHOICES,
    EXECUTION_ENVIRONMENT_VALUES,
)
from liquidity_migration.core.venue_realm import VenueRealm, venue_realm


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

        ``userID`` is the venue account identity; the key fingerprint binds the
        lease to the credential that produced the response without writing the
        credential to disk.
        """

        # ``environment`` names a venue *realm*, not an execution environment,
        # and is baked into the canonical lease path so two realms cannot share
        # a lock.
        normalized_environment = venue_realm(environment).value
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


def canonical_demo_account_lease_path(identity: DemoAccountIdentity) -> Path:
    """Canonical lock path shared by all credentials for one demo account."""

    return _CANONICAL_DEMO_LEASE_DIRECTORY / (
        f"bybit-{identity.environment}-user-{identity.user_id}.lock"
    )


def _is_canonical_demo_account_lease_path(path: Path) -> bool:
    suffix = ".lock"
    if path.parent != _CANONICAL_DEMO_LEASE_DIRECTORY or not path.name.endswith(suffix):
        return False
    for realm in VenueRealm:
        prefix = f"bybit-{realm.value}-user-"
        if not path.name.startswith(prefix):
            continue
        user_id = path.name[len(prefix) : -len(suffix)]
        return user_id.isdigit() and int(user_id) > 0
    return False


class AccountOwnerLeaseAlreadyHeldError(RuntimeError):
    """Another open description already holds the flock."""


def _lease_file_open_flags() -> int:
    try:
        return os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError as exc:  # pragma: no cover - required on supported POSIX deployments
        raise RuntimeError("account owner leases require O_NOFOLLOW and O_CLOEXEC") from exc


def _descriptor_matches_path(descriptor: int, path: Path) -> bool:
    """The locked descriptor is still the single regular file at ``path``.

    flock protects an inode; this proves the inode is still the one a new
    contender would open, so a deleted or replaced lease file surfaces as
    "not held" instead of silently admitting a second owner.
    """

    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_nlink == 1
        and current.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - regular blocking file writes make progress or raise
            raise OSError("account owner lease metadata write made no progress")
        offset += written


def _write_lease_metadata(descriptor: int, metadata: Mapping[str, Any]) -> None:
    payload = (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    _write_all(descriptor, payload)
    os.fsync(descriptor)


def acquire_inherited_account_owner_lease(
    descriptor: int,
    path: str | Path,
    environment: str,
    role: str,
) -> None:
    """Acquire and annotate a shell-owned lease descriptor without reopening it.

    The caller must retain its duplicate of ``descriptor``: ``flock`` stays
    attached to that shared open-file description until the last duplicate
    closes.
    """

    normalized = Path(os.path.abspath(Path(path).expanduser()))
    if descriptor < 0:
        raise ValueError("inherited account owner lease descriptor must be non-negative")
    normalized_environment = str(environment).strip().lower()
    if normalized_environment not in EXECUTION_ENVIRONMENT_VALUES:
        raise ValueError(
            "account owner lease environment must be one of "
            + ", ".join(sorted(EXECUTION_ENVIRONMENT_VALUES))
        )
    if normalized_environment == "demo" and not _is_canonical_demo_account_lease_path(normalized):
        raise ValueError("demo account owner lease must use its canonical Bybit /run/lock identity path")
    normalized_role = str(role).strip()
    if not normalized_role:
        raise ValueError("account owner lease role must be non-empty")

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise AccountOwnerLeaseAlreadyHeldError(
            f"account execution owner lease is already held: {normalized}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"account execution owner lease cannot be locked: {normalized}") from exc
    # Identity check after the lock: a file replaced between the shell's open
    # and this lock would pass a pre-lock check and still leave the lock on an
    # orphaned inode.
    if not _descriptor_matches_path(descriptor, normalized):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        raise RuntimeError(
            f"account execution owner lease path changed during acquisition: {normalized}"
        )
    metadata: dict[str, Any] = {
        "environment": normalized_environment,
        "pid": os.getppid(),
        "role": normalized_role,
        "started_at_ns": time.time_ns(),
    }
    if normalized_environment == "demo":
        metadata["venue"] = "bybit"
    _write_lease_metadata(descriptor, metadata)


def revalidate_inherited_account_owner_lease(
    descriptor: int,
    path: str | Path,
) -> None:
    """Prove a held inherited lease still guards the canonical path."""

    if descriptor < 0:
        raise ValueError("inherited account owner lease descriptor must be non-negative")
    normalized = Path(os.path.abspath(Path(path).expanduser()))
    if not _descriptor_matches_path(descriptor, normalized):
        raise RuntimeError(
            f"account execution owner lease no longer matches the canonical path: {normalized}"
        )
    # A fresh descriptor must be refused the lock (someone holds it), and this
    # descriptor must be granted it (the holder is this open-file description;
    # flock re-acquisition on the holder is a no-op).
    contender = os.open(normalized, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(contender, fcntl.LOCK_UN)
            raise RuntimeError(f"account execution owner lease is no longer held: {normalized}")
    finally:
        os.close(contender)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeError(
            f"account execution owner lease is held by another open description: {normalized}"
        ) from exc


class AccountOwnerLease:
    """Kernel-enforced advisory lock held for a local owner's entire lifetime.

    Path-based; venue mutation requires the credential-bound subclass below.
    """

    def __init__(self, path: str | Path) -> None:
        # Lexical, not resolved: a symlink must be refused at open
        # (O_NOFOLLOW), not normalized into an apparently canonical target.
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self._file: Any | None = None
        self._holder_pid: int | None = None

    @property
    def held(self) -> bool:
        handle = self._file
        if handle is None or handle.closed or self._holder_pid != os.getpid():
            return False
        return _descriptor_matches_path(handle.fileno(), self.path)

    def _lease_metadata(self) -> dict[str, Any]:
        return {"pid": os.getpid()}

    def acquire(self) -> None:
        if self._file is not None:
            if self.held:
                return
            raise RuntimeError("account execution owner lease belongs to another process")
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(self.path, _lease_file_open_flags(), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
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
        if not _descriptor_matches_path(handle.fileno(), self.path):
            handle.close()
            raise RuntimeError(
                "account execution owner lease path changed during acquisition"
            )
        _write_lease_metadata(handle.fileno(), self._lease_metadata())
        self._file = handle
        self._holder_pid = os.getpid()

    def close(self) -> None:
        handle = self._file
        holder_pid = self._holder_pid
        self._file = None
        self._holder_pid = None
        if handle is None:
            return
        if holder_pid != os.getpid():
            # A fork child shares the open-file description, so LOCK_UN here
            # would release the parent's lock too. Close only the duplicate.
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


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Acquire account-owner leases on inherited shell descriptors.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    acquire = subparsers.add_parser(
        "acquire-inherited",
        help="lock and annotate an inherited shell descriptor",
    )
    acquire.add_argument("fd", type=int)
    acquire.add_argument("path")
    acquire.add_argument("environment", choices=EXECUTION_ENVIRONMENT_CHOICES)
    acquire.add_argument("role")
    revalidate = subparsers.add_parser(
        "revalidate-inherited",
        help="revalidate a held inherited descriptor without rewriting metadata",
    )
    revalidate.add_argument("fd", type=int)
    revalidate.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    if args.command == "acquire-inherited":
        try:
            acquire_inherited_account_owner_lease(
                args.fd,
                args.path,
                args.environment,
                args.role,
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
            revalidate_inherited_account_owner_lease(args.fd, args.path)
        except (OSError, RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    raise AssertionError(args.command)  # pragma: no cover - argparse constrains the command


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess integration
    raise SystemExit(main())
