"""Bounded readiness gate for account-owner dependent services.

``After=``/``Requires=`` only order process startup; they do not prove the
owner initialized its route, reconciled its journal, published a healthy
head-bound observation, or has fresh market data. This is that fail-closed
boundary, for systemd ``ExecStartPost`` and checked deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .account_owner_health import (
    require_recent_account_owner_health,
    validate_systemd_invocation_id,
)
from .account_route import require_account_route
from .deterministic_serialization import canonical_json
from .execution_environment import (
    EXECUTION_ENVIRONMENT_CHOICES,
    account_id_for_environment,
    execution_environment,
)
from .market_capture import (
    MAX_OWNER_CAPTURE_RECORD_BYTES,
    OwnerCaptureReadinessSidecar,
    OwnerMarketReadinessSidecar,
    capture_record_id,
    owner_capture_readiness_path,
    owner_market_readiness_path,
)


DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_MAX_AGE_SECONDS = 30.0
REGISTERED_MAX_AGE_NS = int(DEFAULT_MAX_AGE_SECONDS * 1_000_000_000)
_MAX_CAPTURE_SIDECAR_BYTES = 16 * 1024


def _registered_max_age(max_age_ns: int) -> int:
    if type(max_age_ns) is not int or max_age_ns <= 0:
        raise ValueError("account-owner readiness max age must be a positive integer")
    if max_age_ns > REGISTERED_MAX_AGE_NS:
        raise ValueError(
            "account-owner readiness max age cannot exceed the registered 30 seconds"
        )
    return max_age_ns


@dataclass(frozen=True, slots=True)
class AccountOwnerReadiness:
    environment: str
    account_id: str
    route_id: str
    account_root: str
    inbox_root: str
    capture_root: str
    owner_invocation_id: str
    health_observed_ts_ns: int
    health_loop_sequence: int
    journal_sequence: int
    journal_state_hash: str
    market_symbol: str
    market_required_symbols_sha256: str
    market_required_symbol_count: int
    market_oldest_required_receive_ts_ns: int
    market_age_ns: int
    raw_market_persistence_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _absolute_directory(path: str | Path, *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a non-symlink directory")
    return candidate.resolve(strict=True)


def _stable_signature(metadata: os.stat_result) -> tuple[int, ...]:
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


def _stable_private_sidecar(
    path: Path,
    *,
    label: str = "capture",
    expected_owner_uid: int | None = None,
) -> bytes:
    """Read the small atomically replaced sidecar without following aliases."""

    owner_uid = os.geteuid() if expected_owner_uid is None else expected_owner_uid
    if type(owner_uid) is not int or owner_uid < 0:
        raise ValueError("expected readiness sidecar owner uid must be non-negative")

    try:
        before_path = path.lstat()
    except OSError as exc:
        raise ValueError(f"account owner {label} readiness sidecar is unavailable: {path}") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"account owner {label} readiness sidecar must be a regular non-symlink file")
    if before_path.st_size <= 0 or before_path.st_size > _MAX_CAPTURE_SIDECAR_BYTES:
        raise ValueError(f"account owner {label} readiness sidecar has an invalid bounded size")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise ValueError(f"account owner {label} readiness sidecar cannot be opened safely") from exc
    try:
        before_descriptor = os.fstat(descriptor)
        if _stable_signature(before_descriptor) != _stable_signature(before_path):
            raise RuntimeError(f"account owner {label} readiness sidecar changed while opening")
        if (
            not stat.S_ISREG(before_descriptor.st_mode)
            or stat.S_IMODE(before_descriptor.st_mode) != 0o600
            or before_descriptor.st_uid != owner_uid
            or before_descriptor.st_nlink != 1
        ):
            raise ValueError(f"account owner {label} readiness sidecar must be private and singly linked")
        chunks: list[bytes] = []
        remaining = before_descriptor.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                raise RuntimeError(f"account owner {label} readiness sidecar ended while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_descriptor = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"account owner {label} readiness sidecar disappeared while reading") from exc
    expected_signature = _stable_signature(before_descriptor)
    if (
        _stable_signature(after_descriptor) != expected_signature
        or _stable_signature(after_path) != expected_signature
    ):
        raise RuntimeError(f"account owner {label} readiness sidecar changed while reading")
    return data


def _read_owner_capture_sidecar(root: Path) -> OwnerCaptureReadinessSidecar:
    path = owner_capture_readiness_path(root)
    data = _stable_private_sidecar(path)
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("account owner capture readiness sidecar is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("account owner capture readiness sidecar must contain an object")
    try:
        return OwnerCaptureReadinessSidecar.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid account owner capture readiness sidecar: {exc}") from exc


def _read_owner_market_sidecar(
    root: Path,
    *,
    expected_owner_uid: int | None = None,
) -> OwnerMarketReadinessSidecar:
    path = owner_market_readiness_path(root)
    data = _stable_private_sidecar(
        path,
        label="market",
        expected_owner_uid=expected_owner_uid,
    )
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("account owner market readiness sidecar is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("account owner market readiness sidecar must contain an object")
    try:
        return OwnerMarketReadinessSidecar.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid account owner market readiness sidecar: {exc}") from exc


def _path_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Identity fields which do not change during a concurrent append."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    )


def _open_directory(parent_descriptor: int, name: str) -> int:
    try:
        before_path = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"owner capture segment directory is unavailable: {name}") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISDIR(before_path.st_mode):
        raise ValueError(f"owner capture segment path component is not a real directory: {name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(f"owner capture segment directory cannot be opened safely: {name}") from exc
    metadata = os.fstat(descriptor)
    if _path_identity(metadata) != _path_identity(before_path):
        os.close(descriptor)
        raise RuntimeError(f"owner capture segment directory changed while opening: {name}")
    return descriptor


def _pread_exact(descriptor: int, *, offset: int, length: int) -> bytes:
    chunks: list[bytes] = []
    cursor = offset
    remaining = length
    while remaining:
        chunk = os.pread(descriptor, min(remaining, 1024 * 1024), cursor)
        if not chunk:
            raise RuntimeError("owner capture segment ended before the referenced record")
        chunks.append(chunk)
        cursor += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_referenced_capture_record(
    root: Path,
    sidecar: OwnerCaptureReadinessSidecar,
) -> Mapping[str, Any]:
    """Open only the sidecar target and verify its immutable byte range."""

    parts = tuple(sidecar.segment_path.split("/"))
    if not parts:
        raise ValueError("owner capture segment path is empty")
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        root_descriptor = os.open(str(root), root_flags)
    except OSError as exc:
        raise ValueError("account capture root cannot be opened safely") from exc
    opened_directories = [root_descriptor]
    segment_descriptor: int | None = None
    try:
        root_metadata = os.fstat(root_descriptor)
        lexical_root = root.lstat()
        if _path_identity(root_metadata) != _path_identity(lexical_root):
            raise RuntimeError("account capture root changed while opening")
        parent_descriptor = root_descriptor
        for component in parts[:-1]:
            parent_descriptor = _open_directory(parent_descriptor, component)
            opened_directories.append(parent_descriptor)
        filename = parts[-1]
        try:
            before_path = os.stat(
                filename,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("referenced owner capture segment is unavailable") from exc
        if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
            raise ValueError("referenced owner capture segment must be a regular non-symlink file")
        segment_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            segment_descriptor = os.open(
                filename,
                segment_flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ValueError("referenced owner capture segment cannot be opened safely") from exc
        before_descriptor = os.fstat(segment_descriptor)
        if _path_identity(before_descriptor) != _path_identity(before_path):
            raise RuntimeError("referenced owner capture segment changed while opening")
        if (
            before_descriptor.st_dev != sidecar.segment_device
            or before_descriptor.st_ino != sidecar.segment_inode
        ):
            raise ValueError("referenced owner capture segment inode does not match the sidecar")
        if (
            stat.S_IMODE(before_descriptor.st_mode) != 0o600
            or before_descriptor.st_uid != os.geteuid()
            or before_descriptor.st_nlink != 1
        ):
            raise ValueError("referenced owner capture segment must be private and singly linked")
        range_end = sidecar.byte_offset + sidecar.byte_length
        if sidecar.byte_length > MAX_OWNER_CAPTURE_RECORD_BYTES or before_descriptor.st_size < range_end:
            raise ValueError("referenced owner capture byte range is outside the segment")
        data = _pread_exact(
            segment_descriptor,
            offset=sidecar.byte_offset,
            length=sidecar.byte_length,
        )
        after_descriptor = os.fstat(segment_descriptor)
        after_path = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _path_identity(after_descriptor) != _path_identity(before_descriptor)
            or _path_identity(after_path) != _path_identity(before_descriptor)
            or after_descriptor.st_size < range_end
        ):
            raise RuntimeError("referenced owner capture segment identity changed while reading")
    finally:
        if segment_descriptor is not None:
            os.close(segment_descriptor)
        for descriptor in reversed(opened_directories):
            os.close(descriptor)

    if hashlib.sha256(data).hexdigest() != sidecar.record_sha256:
        raise ValueError("referenced owner capture record hash does not match the sidecar")
    if not data.endswith(b"\n"):
        raise ValueError("referenced owner capture record is not a complete JSONL row")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("referenced owner capture record is invalid JSON") from exc
    if not isinstance(payload, dict) or canonical_json(payload) + b"\n" != data:
        raise ValueError("referenced owner capture record is not one canonical JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("referenced owner capture record has an unsupported schema")
    if payload.get("owner_invocation_id") != sidecar.owner_invocation_id:
        raise ValueError("referenced owner capture record invocation does not match the sidecar")
    if payload.get("local_receive_ts_ns") != sidecar.local_receive_ts_ns:
        raise ValueError("referenced owner capture record timestamp does not match the sidecar")
    if payload.get("record_id") != sidecar.record_id or capture_record_id(payload) != sidecar.record_id:
        raise ValueError("referenced owner capture record id does not match the sidecar")
    return payload


def latest_capture_receive_ts_ns(
    capture_root: str | Path,
    *,
    expected_invocation_id: str,
) -> int:
    """Verify and return the bounded sidecar arrival for one owner generation."""

    expected_generation = validate_systemd_invocation_id(
        expected_invocation_id,
        label="expected account-owner capture invocation id",
    )
    root = _absolute_directory(capture_root, label="account capture root")
    sidecar = _sidecar_for_generation(root, expected_generation=expected_generation)
    _read_referenced_capture_record(root, sidecar)
    return sidecar.local_receive_ts_ns


def latest_market_readiness(
    capture_root: str | Path,
    *,
    expected_invocation_id: str | None = None,
    expected_owner_uid: int | None = None,
) -> OwnerMarketReadinessSidecar:
    """Verify and return bounded live-L2 readiness independent of raw storage."""

    root = _absolute_directory(capture_root, label="account capture root")
    sidecar = _read_owner_market_sidecar(
        root,
        expected_owner_uid=expected_owner_uid,
    )
    if expected_invocation_id is not None:
        expected_generation = validate_systemd_invocation_id(
            expected_invocation_id,
            label="expected account-owner market invocation id",
        )
        if sidecar.owner_invocation_id != expected_generation:
            raise RuntimeError(
                "account owner market readiness does not match the current systemd generation: "
                f"market={sidecar.owner_invocation_id}, expected={expected_generation}"
            )
    if not sidecar.book_healthy or not sidecar.all_required_books_healthy:
        raise RuntimeError(
            "account owner market books are unhealthy: "
            f"representative={sidecar.symbol}, "
            f"healthy={sidecar.healthy_symbol_count}/"
            f"{sidecar.required_symbol_count}"
        )
    return sidecar


def latest_market_receive_ts_ns(
    capture_root: str | Path,
    *,
    expected_invocation_id: str | None = None,
    expected_owner_uid: int | None = None,
) -> int:
    sidecar = latest_market_readiness(
        capture_root,
        expected_invocation_id=expected_invocation_id,
        expected_owner_uid=expected_owner_uid,
    )
    timestamp = sidecar.oldest_required_receive_ts_ns
    if timestamp is None:
        raise RuntimeError("account owner required live-L2 timestamp is unavailable")
    return timestamp


def _sidecar_for_generation(
    root: Path,
    *,
    expected_generation: str,
) -> OwnerCaptureReadinessSidecar:
    sidecar = _read_owner_capture_sidecar(root)
    if sidecar.owner_invocation_id != expected_generation:
        raise RuntimeError(
            "account owner market capture does not match the current systemd generation: "
            f"capture={sidecar.owner_invocation_id}, expected={expected_generation}"
        )
    return sidecar


def require_account_owner_ready(
    *,
    environment: str,
    account_root: str | Path,
    inbox_root: str | Path,
    capture_root: str | Path,
    expected_invocation_id: str,
    expected_account_id: str | None = None,
    max_age_ns: int = REGISTERED_MAX_AGE_NS,
    now_ns: int | None = None,
) -> AccountOwnerReadiness:
    """Require one exact route, healthy owner, and fresh usable live L2."""

    selected = execution_environment(environment).value
    account_id = expected_account_id or account_id_for_environment(selected)
    if account_id != account_id_for_environment(selected):
        raise ValueError("account-owner readiness account id does not match its environment")
    max_age_ns = _registered_max_age(max_age_ns)
    expected_generation = validate_systemd_invocation_id(
        expected_invocation_id,
        label="expected account-owner invocation id",
    )
    explicit_now = None if now_ns is None else int(now_ns)
    if explicit_now is not None and explicit_now <= 0:
        raise ValueError("account-owner readiness observation time must be positive")

    account = _absolute_directory(account_root, label="account root")
    inbox = _absolute_directory(inbox_root, label="account inbox root")
    capture = _absolute_directory(capture_root, label="account capture root")
    if len({account, inbox, capture}) != 3:
        raise ValueError("account, inbox, and capture roots must be distinct")
    route = require_account_route(
        account_id=account_id,
        environment=selected,
        account_root=account,
        inbox_root=inbox,
    )
    health = require_recent_account_owner_health(
        account,
        environment=selected,
        max_age_ns=max_age_ns,
        now_ns=explicit_now,
        expected_account_id=account_id,
        expected_invocation_id=expected_generation,
    )
    market_sidecar = latest_market_readiness(
        capture,
        expected_invocation_id=expected_generation,
    )
    market_ts_ns = market_sidecar.oldest_required_receive_ts_ns
    if market_ts_ns is None:
        raise RuntimeError("account owner required live-L2 timestamp is unavailable")
    market_now_ns = time.time_ns() if explicit_now is None else explicit_now
    market_age_ns = market_now_ns - market_ts_ns
    if market_age_ns < 0 or market_age_ns > max_age_ns:
        raise RuntimeError(f"account owner live market is stale: age_ns={market_age_ns}")
    # These projections are bound by generation id, not wall-clock ordering:
    # their timestamps do not prove which completed first.
    return AccountOwnerReadiness(
        environment=selected,
        account_id=account_id,
        route_id=route.route_id,
        account_root=str(account),
        inbox_root=str(inbox),
        capture_root=str(capture),
        owner_invocation_id=health.invocation_id,
        health_observed_ts_ns=health.observed_ts_ns,
        health_loop_sequence=health.loop_sequence,
        journal_sequence=health.journal_sequence,
        journal_state_hash=health.journal_state_hash,
        market_symbol=market_sidecar.symbol,
        market_required_symbols_sha256=(
            market_sidecar.required_symbols_sha256
        ),
        market_required_symbol_count=market_sidecar.required_symbol_count,
        market_oldest_required_receive_ts_ns=market_ts_ns,
        market_age_ns=market_age_ns,
        raw_market_persistence_enabled=(
            market_sidecar.raw_market_persistence_enabled
        ),
    )


def wait_for_account_owner_ready(
    *,
    environment: str,
    account_root: str | Path,
    inbox_root: str | Path,
    capture_root: str | Path,
    expected_invocation_id: str,
    expected_account_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_age_ns: int = REGISTERED_MAX_AGE_NS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> AccountOwnerReadiness:
    """Poll the readiness predicate until success or one bounded failure."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("account-owner readiness timeout must be finite and positive")
    if not math.isfinite(poll_seconds) or poll_seconds <= 0.0:
        raise ValueError("account-owner readiness poll interval must be finite and positive")
    max_age_ns = _registered_max_age(max_age_ns)
    expected_generation = validate_systemd_invocation_id(
        expected_invocation_id,
        label="expected account-owner invocation id",
    )
    started = monotonic()
    deadline = started + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            return require_account_owner_ready(
                environment=environment,
                account_root=account_root,
                inbox_root=inbox_root,
                capture_root=capture_root,
                expected_invocation_id=expected_generation,
                expected_account_id=expected_account_id,
                max_age_ns=max_age_ns,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
        now = monotonic()
        if now >= deadline:
            detail = "unknown readiness failure" if last_error is None else f"{type(last_error).__name__}: {last_error}"
            raise TimeoutError(f"account owner did not become ready within {timeout_seconds:g}s: {detail}") from last_error
        sleep(min(poll_seconds, max(deadline - now, 0.0)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wait for an exact healthy account owner and fresh usable live L2"
    )
    parser.add_argument(
        "--environment", required=True, choices=EXECUTION_ENVIRONMENT_CHOICES
    )
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--inbox-root", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--expected-invocation-id", required=True)
    parser.add_argument("--expected-account-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not math.isfinite(args.max_age_seconds) or args.max_age_seconds <= 0.0:
        raise ValueError("--max-age-seconds must be finite and positive")
    max_age_ns = _registered_max_age(int(args.max_age_seconds * 1_000_000_000))
    receipt = wait_for_account_owner_ready(
        environment=args.environment,
        account_root=args.account_root,
        inbox_root=args.inbox_root,
        capture_root=args.capture_root,
        expected_invocation_id=args.expected_invocation_id,
        expected_account_id=args.expected_account_id or None,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        max_age_ns=max_age_ns,
    )
    print(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AccountOwnerReadiness",
    "latest_capture_receive_ts_ns",
    "latest_market_readiness",
    "latest_market_receive_ts_ns",
    "require_account_owner_ready",
    "wait_for_account_owner_ready",
]
