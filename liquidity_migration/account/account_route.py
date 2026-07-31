"""Strict filesystem identity for one account owner and its target inbox.

The journal and the target inbox are separate roots, so a configuration typo
could have a producer publishing into an inbox consumed by a different owner.
This binds the resolved root pair, account id, and environment into one
self-derived manifest mirrored at both roots.

Each mirror is created atomically, but two directory entries in different roots
cannot commit atomically together, so the only supported crash recovery is: one
valid mirror may create its missing peer, and only when it exactly equals the
requested route.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.artifact_snapshot import read_stable_file, rename_noreplace
from liquidity_migration.account.execution_environment import EXECUTION_ENVIRONMENT_VALUES
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.data.storage import exclusive_file_lock


ACCOUNT_ROUTE_SCHEMA_VERSION = 1
ACCOUNT_ROUTE_FILENAME = "account_route.json"
ACCOUNT_ROUTE_LOCK_DIRECTORY = ".locks"
ACCOUNT_ROUTE_LOCK_FILENAME = "account_route.lock"
_ROUTE_ID_PREFIX = "account-route-v1-"
_ROUTE_ID_DOMAIN = "liquidity_migration.account.account_route.v1"
_ROUTE_FIELDS = frozenset(
    {
        "schema_version",
        "route_id",
        "account_id",
        "environment",
        "account_root",
        "inbox_root",
    }
)


class AccountRouteError(RuntimeError):
    """Base class for fail-closed account-route errors."""


class AccountRouteConfigurationError(AccountRouteError):
    """The requested route identity is not canonical or supported."""


class AccountRouteMissingError(AccountRouteError):
    """A read-only route check found one or both manifests missing."""


class AccountRouteIntegrityError(AccountRouteError):
    """A stored route manifest is malformed, extended, or tampered with."""


class AccountRouteMismatchError(AccountRouteError):
    """Valid route evidence does not match the requested root pair."""


class AccountRouteCutoverRequiredError(AccountRouteError):
    """An unbound root contains state that cannot be silently adopted."""


@dataclass(frozen=True, slots=True)
class AccountRoute:
    """Canonical binding shared byte-for-byte by both route roots."""

    schema_version: int
    route_id: str
    account_id: str
    environment: str
    account_root: str
    inbox_root: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:  # bool is not a valid version
            raise AccountRouteIntegrityError("account route schema_version must be an integer")
        if self.schema_version != ACCOUNT_ROUTE_SCHEMA_VERSION:
            raise AccountRouteIntegrityError(f"unsupported account route schema version {self.schema_version!r}")
        _validate_account_id(self.account_id, error_type=AccountRouteIntegrityError)
        _validate_environment(self.environment, error_type=AccountRouteIntegrityError)
        _validate_stored_root(
            self.account_root,
            label="account_root",
            error_type=AccountRouteIntegrityError,
        )
        _validate_stored_root(
            self.inbox_root,
            label="inbox_root",
            error_type=AccountRouteIntegrityError,
        )
        if self.account_root == self.inbox_root:
            raise AccountRouteIntegrityError("account route account_root and inbox_root must be distinct")
        expected_route_id = _derive_route_id(
            account_id=self.account_id,
            environment=self.environment,
            account_root=self.account_root,
            inbox_root=self.inbox_root,
        )
        if self.route_id != expected_route_id:
            raise AccountRouteIntegrityError("account route route_id does not match its bound identity")

    @property
    def account_path(self) -> Path:
        return Path(self.account_root)

    @property
    def inbox_path(self) -> Path:
        return Path(self.inbox_root)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_id": self.route_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "account_root": self.account_root,
            "inbox_root": self.inbox_root,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AccountRoute:
        missing = sorted(_ROUTE_FIELDS - set(payload))
        unknown = sorted(set(payload) - _ROUTE_FIELDS)
        if missing:
            raise AccountRouteIntegrityError("account route is missing fields: " + ", ".join(missing))
        if unknown:
            raise AccountRouteIntegrityError("account route has unknown fields: " + ", ".join(unknown))
        for field_name in _ROUTE_FIELDS - {"schema_version"}:
            if type(payload[field_name]) is not str:
                raise AccountRouteIntegrityError(f"account route {field_name} must be a string")
        if type(payload["schema_version"]) is not int:
            raise AccountRouteIntegrityError("account route schema_version must be an integer")
        return cls(
            schema_version=payload["schema_version"],
            route_id=payload["route_id"],
            account_id=payload["account_id"],
            environment=payload["environment"],
            account_root=payload["account_root"],
            inbox_root=payload["inbox_root"],
        )


def derive_account_route(
    *,
    account_id: str,
    environment: str,
    account_root: str | Path,
    inbox_root: str | Path,
) -> AccountRoute:
    """Derive the sole valid manifest for an exact resolved route pair."""

    clean_account_id = _validate_account_id(
        account_id,
        error_type=AccountRouteConfigurationError,
    )
    clean_environment = _validate_environment(
        environment,
        error_type=AccountRouteConfigurationError,
    )
    resolved_account_root = _resolve_requested_root(
        account_root,
        label="account_root",
    )
    resolved_inbox_root = _resolve_requested_root(
        inbox_root,
        label="inbox_root",
    )
    if resolved_account_root == resolved_inbox_root:
        raise AccountRouteConfigurationError("account route account_root and inbox_root must be distinct")
    route_id = _derive_route_id(
        account_id=clean_account_id,
        environment=clean_environment,
        account_root=resolved_account_root,
        inbox_root=resolved_inbox_root,
    )
    return AccountRoute(
        schema_version=ACCOUNT_ROUTE_SCHEMA_VERSION,
        route_id=route_id,
        account_id=clean_account_id,
        environment=clean_environment,
        account_root=resolved_account_root,
        inbox_root=resolved_inbox_root,
    )


def account_route_manifest_path(root: str | Path) -> Path:
    """Return the resolved manifest path without creating filesystem state."""

    return Path(root).expanduser().resolve(strict=False) / ACCOUNT_ROUTE_FILENAME


def read_account_route_manifest(root: str | Path) -> AccountRoute:
    """Strictly read one mirror.

    Verifies canonical bytes and the self-derived id; one mirror is not proof
    the peer agrees, so runtime callers want :func:`require_account_route`.
    """

    return _read_manifest(account_route_manifest_path(root))


def require_account_route(
    *,
    account_id: str,
    environment: str,
    account_root: str | Path,
    inbox_root: str | Path,
    expected_owner_uid: int | None = None,
) -> AccountRoute:
    """Read-only, fail-closed validation for producers and privileged tools.

    Leave ``expected_owner_uid`` unset to require both manifests to belong to
    the current process owner; a root-only observer can instead bind the read
    to another runtime account's established owner UID.
    """

    if expected_owner_uid is not None and (
        isinstance(expected_owner_uid, bool)
        or not isinstance(expected_owner_uid, int)
        or expected_owner_uid < 0
    ):
        raise AccountRouteConfigurationError(
            "expected account route owner UID must be a nonnegative integer"
        )

    expected = derive_account_route(
        account_id=account_id,
        environment=environment,
        account_root=account_root,
        inbox_root=inbox_root,
    )
    account_manifest_path, inbox_manifest_path = _manifest_paths(expected)
    missing = [str(path) for path in (account_manifest_path, inbox_manifest_path) if not os.path.lexists(path)]
    if missing:
        raise AccountRouteMissingError("account route manifest is missing from: " + ", ".join(missing))
    account_manifest = _read_manifest(
        account_manifest_path,
        expected_owner_uid=expected_owner_uid,
    )
    inbox_manifest = _read_manifest(
        inbox_manifest_path,
        expected_owner_uid=expected_owner_uid,
    )
    _require_matching_manifests(
        account_manifest,
        inbox_manifest,
        expected=expected,
    )
    return expected


def ensure_account_route(
    *,
    account_id: str,
    environment: str,
    account_root: str | Path,
    inbox_root: str | Path,
) -> AccountRoute:
    """Owner-side initialization and exact one-mirror crash recovery.

    Existing manifests are immutable: never rewritten, adopted from disk, or
    repaired. An unbound root pair must be structurally empty, and so must a
    missing recovery side; prior journal/queue state needs an explicit cutover.
    """

    expected = derive_account_route(
        account_id=account_id,
        environment=environment,
        account_root=account_root,
        inbox_root=inbox_root,
    )
    expected.account_path.mkdir(parents=True, exist_ok=True)
    expected.inbox_path.mkdir(parents=True, exist_ok=True)
    lock_paths = sorted(
        {
            expected.account_path / ACCOUNT_ROUTE_LOCK_DIRECTORY / ACCOUNT_ROUTE_LOCK_FILENAME,
            expected.inbox_path / ACCOUNT_ROUTE_LOCK_DIRECTORY / ACCOUNT_ROUTE_LOCK_FILENAME,
        },
        key=str,
    )
    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(
                exclusive_file_lock(
                    lock_path,
                    stale_seconds=600,
                    poll_seconds=0.01,
                )
            )
        return _ensure_account_route_locked(expected)


def _ensure_account_route_locked(expected: AccountRoute) -> AccountRoute:
    account_manifest_path, inbox_manifest_path = _manifest_paths(expected)
    paths = (account_manifest_path, inbox_manifest_path)
    existing: dict[Path, AccountRoute] = {}
    for path in paths:
        if os.path.lexists(path):
            existing[path] = _read_manifest(path)

    if len(existing) == 2:
        _require_matching_manifests(
            existing[account_manifest_path],
            existing[inbox_manifest_path],
            expected=expected,
        )
        return expected

    if len(existing) == 1:
        surviving = next(iter(existing.values()))
        if surviving != expected:
            raise _route_mismatch(surviving, expected)
        missing_path = next(path for path in paths if path not in existing)
        _require_unbound_root_empty(
            missing_path.parent,
            role="missing route mirror",
        )
    else:
        _require_unbound_root_empty(
            expected.account_path,
            role="account root",
        )
        _require_unbound_root_empty(
            expected.inbox_path,
            role="inbox root",
        )

    data = _manifest_bytes(expected)
    for path in paths:
        if path not in existing:
            _atomic_create(path, data)

    # Re-read both mirrors rather than trusting the syscalls; this also catches
    # writers that ignore the initialization locks.
    account_manifest = _read_manifest(account_manifest_path)
    inbox_manifest = _read_manifest(inbox_manifest_path)
    _require_matching_manifests(
        account_manifest,
        inbox_manifest,
        expected=expected,
    )
    return expected


def _require_matching_manifests(
    account_manifest: AccountRoute,
    inbox_manifest: AccountRoute,
    *,
    expected: AccountRoute,
) -> None:
    if account_manifest != inbox_manifest:
        raise AccountRouteMismatchError(
            "account route manifests disagree: "
            f"account root has {account_manifest.route_id}, "
            f"inbox root has {inbox_manifest.route_id}"
        )
    if account_manifest != expected:
        raise _route_mismatch(account_manifest, expected)


def _route_mismatch(observed: AccountRoute, expected: AccountRoute) -> AccountRouteMismatchError:
    return AccountRouteMismatchError(
        "account route does not match requested account/environment/root pair: "
        f"stored {observed.route_id}, requested {expected.route_id}"
    )


def _read_manifest(
    path: Path,
    *,
    expected_owner_uid: int | None = None,
) -> AccountRoute:
    if path.is_symlink():
        raise AccountRouteIntegrityError(
            f"account route manifest must not be a symlink: {path}"
        )
    try:
        snapshot = read_stable_file(
            path,
            label="account route manifest",
            require_mode=0o600,
            require_owner=expected_owner_uid is None,
            require_single_link=True,
        )
        if expected_owner_uid is not None and snapshot.uid != expected_owner_uid:
            raise ValueError(
                "account route manifest has owner UID "
                f"{snapshot.uid}, expected {expected_owner_uid}: {path}"
            )
        raw = snapshot.data
        payload = json.loads(raw)
    except ValueError as exc:
        if not os.path.lexists(path):
            raise AccountRouteMissingError(
                f"account route manifest is missing: {path}"
            ) from exc
        raise AccountRouteIntegrityError(str(exc)) from exc
    except (RuntimeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountRouteIntegrityError(f"cannot read account route manifest {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AccountRouteIntegrityError(f"account route manifest must contain one object: {path}")
    route = AccountRoute.from_dict(payload)
    if raw != _manifest_bytes(route):
        raise AccountRouteIntegrityError(f"account route manifest is not canonical: {path}")
    return route


def _manifest_paths(route: AccountRoute) -> tuple[Path, Path]:
    return (
        route.account_path / ACCOUNT_ROUTE_FILENAME,
        route.inbox_path / ACCOUNT_ROUTE_FILENAME,
    )


def _manifest_bytes(route: AccountRoute) -> bytes:
    return canonical_json(route.to_dict()) + b"\n"


def _require_unbound_root_empty(root: Path, *, role: str) -> None:
    artifacts = _meaningful_unbound_artifacts(root)
    if not artifacts:
        return
    rendered = ", ".join(artifacts[:5])
    if len(artifacts) > 5:
        rendered += f", +{len(artifacts) - 5} more"
    raise AccountRouteCutoverRequiredError(
        f"refusing to bind {role} {root}: unbound root contains preexisting "
        f"artifacts ({rendered}); explicit route cutover is required"
    )


def _meaningful_unbound_artifacts(root: Path) -> tuple[str, ...]:
    artifacts: list[str] = []
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise AccountRouteCutoverRequiredError(
            f"cannot inspect unbound account route root {root}: {exc}"
        ) from exc

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise AccountRouteCutoverRequiredError(f"cannot inspect unbound account route root {root}: {exc}") from exc
        try:
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root)
                metadata = entry.stat(follow_symlinks=False)
                if _is_route_initializer_scaffolding(
                    relative,
                    is_regular_file=stat.S_ISREG(metadata.st_mode),
                ):
                    continue
                if _is_persistent_lock_scaffolding(
                    relative,
                    metadata=metadata,
                    root_uid=root_metadata.st_uid,
                ):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                else:
                    # Any non-directory entry may carry prior route meaning.
                    artifacts.append(relative.as_posix())
        except OSError as exc:
            raise AccountRouteCutoverRequiredError(f"cannot inspect unbound account route root {root}: {exc}") from exc

    visit(root)
    return tuple(sorted(artifacts))


def _is_persistent_lock_scaffolding(
    relative: Path,
    *,
    metadata: os.stat_result,
    root_uid: int,
) -> bool:
    """A persistent mutex inode is infrastructure, not prior account state."""
    in_lock_namespace = (
        len(relative.parts) > 1
        and relative.parts[0] == ACCOUNT_ROUTE_LOCK_DIRECTORY
    )
    return (
        (relative.name.endswith(".lock") or in_lock_namespace)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid == root_uid
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _is_route_initializer_scaffolding(
    relative: Path,
    *,
    is_regular_file: bool,
) -> bool:
    if not is_regular_file:
        return False
    return (
        relative.parent == Path(".")
        and relative.name.startswith(f".{ACCOUNT_ROUTE_FILENAME}.")
        and relative.name.endswith(".tmp")
    )


def _atomic_create(path: Path, data: bytes) -> None:
    """Atomically create one immutable mirror without overwrite semantics."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("account route manifest write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            rename_noreplace(
                temporary,
                path,
                label="account route manifest",
            )
            published = True
        except FileExistsError:
            # Another same-route initializer won the create race. Exact
            # canonical bytes are safe; anything else is conflicting evidence.
            observed = _read_manifest(path)
            if _manifest_bytes(observed) != data:
                raise AccountRouteMismatchError(f"account route manifest appeared with conflicting content: {path}")
        if published:
            _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _derive_route_id(
    *,
    account_id: str,
    environment: str,
    account_root: str,
    inbox_root: str,
) -> str:
    seed = {
        "domain": _ROUTE_ID_DOMAIN,
        "schema_version": ACCOUNT_ROUTE_SCHEMA_VERSION,
        "account_id": account_id,
        "environment": environment,
        "account_root": account_root,
        "inbox_root": inbox_root,
    }
    return _ROUTE_ID_PREFIX + hashlib.sha256(canonical_json(seed)).hexdigest()


def _validate_account_id(
    value: object,
    *,
    error_type: type[AccountRouteError],
) -> str:
    if type(value) is not str:
        raise error_type("account route account_id must be a string")
    if not value or value != value.strip():
        raise error_type("account route account_id must be non-empty canonical text without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise error_type("account route account_id must not contain control characters")
    return value


def _validate_environment(
    value: object,
    *,
    error_type: type[AccountRouteError],
) -> str:
    if type(value) is not str or value not in EXECUTION_ENVIRONMENT_VALUES:
        raise error_type(
            "account route environment must be exactly one of "
            + ", ".join(sorted(EXECUTION_ENVIRONMENT_VALUES))
        )
    return value


def _resolve_requested_root(value: str | Path, *, label: str) -> str:
    if not isinstance(value, (str, Path)):
        raise AccountRouteConfigurationError(f"account route {label} must be a filesystem path")
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise AccountRouteConfigurationError(f"account route {label} cannot be resolved: {exc}") from exc


def _validate_stored_root(
    value: object,
    *,
    label: str,
    error_type: type[AccountRouteError],
) -> str:
    if type(value) is not str or not value:
        raise error_type(f"account route {label} must be a non-empty string")
    try:
        resolved = str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise error_type(f"account route {label} cannot be resolved: {exc}") from exc
    if value != resolved:
        raise error_type(f"account route {label} must be an exact resolved absolute path")
    return value
