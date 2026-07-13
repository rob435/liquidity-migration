"""Durable liveness evidence for a single account-execution owner.

The health projection is deliberately separate from the canonical account
journal.  A process heartbeat is operational evidence, not a trading-domain
event, and must not change execution state hashes or paper/demo parity.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .account_kernel import GENESIS_HASH, read_account_journal
from .deterministic_serialization import canonical_json


ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION = 1
ACCOUNT_OWNER_HEALTH_FILENAME = "account_owner_health.json"
# Risk-increasing target producers require a much tighter bound than the
# operator-facing watchdog. Owners normally publish every five seconds.
TARGET_PRODUCER_HEALTH_MAX_AGE_NS = 30_000_000_000


class AccountOwnerHealthStatus(StrEnum):
    HEALTHY = "healthy"
    BLOCKED = "blocked"


def format_convergence_health(report: Any, *, max_items: int = 3) -> str:
    """Render stable, human-readable desired-vs-executed owner health.

    Ages deliberately stay out of the text so a pending transition does not
    rewrite the health artifact on every owner loop.  The report's ``healthy``
    flag still applies the configured age SLA.
    """

    items = tuple(report.items)
    if not items:
        return ""
    if max_items <= 0:
        raise ValueError("max convergence health items must be positive")
    rows = [
        (
            f"{item.symbol}:{item.status}:target={item.target_signed_qty:g}:"
            f"position={item.position_signed_qty:g}:working={item.working_order_count}:"
            f"residual={item.residual_signed_qty:g}:"
            f"attempts={item.retry_attempts}/{item.retry_limit}"
        )
        for item in items[:max_items]
    ]
    omitted = len(items) - len(rows)
    if omitted:
        rows.append(f"+{omitted} more")
    state = "unhealthy" if not report.healthy else "pending"
    return f"target convergence {state}: " + "; ".join(rows)


def fold_convergence_health(
    report: Any,
    *,
    status: AccountOwnerHealthStatus | str,
    detail: str = "",
) -> tuple[AccountOwnerHealthStatus, str]:
    """Fold convergence SLA state into the owner heartbeat projection."""

    output_status = AccountOwnerHealthStatus(status)
    convergence_detail = format_convergence_health(report)
    if not report.healthy:
        output_status = AccountOwnerHealthStatus.BLOCKED
    output_detail = "; ".join(
        part for part in (detail, convergence_detail) if part
    )[:1000]
    return output_status, output_detail


@dataclass(frozen=True, slots=True)
class AccountOwnerHealth:
    """Latest completed owner-loop observation.

    ``journal_sequence`` and ``journal_state_hash`` bind the operational
    heartbeat to the exact canonical state observed by the owner.  They are
    evidence references only; this projection never mutates that state.
    """

    owner: str
    environment: str
    account_id: str
    status: AccountOwnerHealthStatus | str
    observed_ts_ns: int
    loop_sequence: int
    journal_sequence: int
    journal_state_hash: str
    equity_usdt: float
    available_margin_usdt: float
    requested_symbols_ready: bool
    last_batch_id: str = ""
    detail: str = ""
    schema_version: int = ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION:
            raise ValueError(f"unsupported account-owner health schema {self.schema_version}")
        if self.owner != "account_execution":
            raise ValueError("account-owner health owner must be 'account_execution'")
        if self.environment not in {"paper", "demo"}:
            raise ValueError("account-owner health environment must be 'paper' or 'demo'")
        if not self.account_id:
            raise ValueError("account-owner health account_id is required")
        AccountOwnerHealthStatus(self.status)
        if self.observed_ts_ns <= 0:
            raise ValueError("account-owner health observed_ts_ns must be positive")
        if self.loop_sequence <= 0:
            raise ValueError("account-owner health loop_sequence must be positive")
        if self.journal_sequence < 0:
            raise ValueError("account-owner health journal_sequence cannot be negative")
        if len(self.journal_state_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.journal_state_hash
        ):
            raise ValueError("account-owner health journal_state_hash must be lowercase SHA-256")
        if not math.isfinite(self.equity_usdt) or self.equity_usdt <= 0.0:
            raise ValueError("account-owner health equity_usdt must be finite and positive")
        if not math.isfinite(self.available_margin_usdt) or self.available_margin_usdt < 0.0:
            raise ValueError("account-owner health available_margin_usdt must be finite and non-negative")
        if not isinstance(self.requested_symbols_ready, bool):
            raise ValueError("account-owner health requested_symbols_ready must be boolean")
        if len(self.last_batch_id) > 500:
            raise ValueError("account-owner health last_batch_id is too long")
        if len(self.detail) > 1000:
            raise ValueError("account-owner health detail is too long")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = AccountOwnerHealthStatus(self.status).value
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AccountOwnerHealth":
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        if missing:
            raise ValueError(f"account-owner health is missing fields: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"account-owner health has unknown fields: {', '.join(unknown)}")
        try:
            return cls(
                schema_version=int(payload["schema_version"]),
                owner=str(payload["owner"]),
                environment=str(payload["environment"]),
                account_id=str(payload["account_id"]),
                status=str(payload["status"]),
                observed_ts_ns=int(payload["observed_ts_ns"]),
                loop_sequence=int(payload["loop_sequence"]),
                journal_sequence=int(payload["journal_sequence"]),
                journal_state_hash=str(payload["journal_state_hash"]),
                equity_usdt=float(payload["equity_usdt"]),
                available_margin_usdt=float(payload["available_margin_usdt"]),
                requested_symbols_ready=payload["requested_symbols_ready"],
                last_batch_id=str(payload["last_batch_id"]),
                detail=str(payload["detail"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid account-owner health artifact: {exc}") from exc


def account_owner_health_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_OWNER_HEALTH_FILENAME


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account-owner health write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_account_owner_health(root: str | Path, health: AccountOwnerHealth) -> Path:
    """Atomically publish the latest owner observation under ``root``."""

    path = account_owner_health_path(root)
    _atomic_replace(path, canonical_json(health.to_dict()) + b"\n")
    return path


def read_account_owner_health(root: str | Path) -> AccountOwnerHealth:
    """Read and strictly validate the latest durable owner observation."""

    path = account_owner_health_path(root)
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read account-owner health artifact {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"account-owner health artifact must contain an object: {path}")
    return AccountOwnerHealth.from_dict(payload)


def require_recent_account_owner_health(
    root: str | Path,
    *,
    environment: str,
    max_age_ns: int,
    now_ns: int | None = None,
    expected_account_id: str | None = None,
) -> AccountOwnerHealth:
    """Require fresh health bound to the verified canonical journal head."""

    if environment not in {"demo", "paper"}:
        raise ValueError("expected owner environment must be 'demo' or 'paper'")
    if max_age_ns <= 0:
        raise ValueError("max account-owner health age must be positive")
    if expected_account_id is not None and not expected_account_id:
        raise ValueError("expected account-owner account_id cannot be empty")
    health = read_account_owner_health(root)
    if health.environment != environment:
        raise RuntimeError(f"account-owner health environment is {health.environment}, expected {environment}")
    if AccountOwnerHealthStatus(health.status) is not AccountOwnerHealthStatus.HEALTHY:
        raise RuntimeError(f"account owner is blocked: {health.detail or 'no detail'}")
    if not health.requested_symbols_ready:
        raise RuntimeError("account owner has unready requested symbols")
    observed_now = time.time_ns() if now_ns is None else int(now_ns)
    age_ns = observed_now - health.observed_ts_ns
    if age_ns < 0 or age_ns > max_age_ns:
        raise RuntimeError(f"account-owner health is stale: age_ns={age_ns}")
    if expected_account_id is not None and health.account_id != expected_account_id:
        raise RuntimeError(
            f"account-owner health account_id is {health.account_id!r}, "
            f"expected {expected_account_id!r}"
        )

    # The health file is an independently replaced operational projection. Read
    # it on both sides of the journal snapshot so a concurrent owner update
    # cannot accidentally bind one heartbeat to a different journal head.
    events = read_account_journal(root, verify=True)
    stable_health = read_account_owner_health(root)
    if stable_health != health:
        raise RuntimeError("account-owner health changed while binding the journal head")
    journal_sequence = events[-1].sequence if events else 0
    journal_state_hash = events[-1].state_hash if events else GENESIS_HASH
    if health.journal_sequence != journal_sequence:
        raise RuntimeError(
            "account-owner health journal sequence mismatch: "
            f"health={health.journal_sequence}, journal={journal_sequence}"
        )
    if health.journal_state_hash != journal_state_hash:
        raise RuntimeError("account-owner health journal state hash mismatch")
    if events and health.account_id != events[-1].account_id:
        raise RuntimeError(
            f"account-owner health account_id {health.account_id!r} does not match "
            f"journal account_id {events[-1].account_id!r}"
        )
    return health
