"""Durable liveness evidence for a single account-execution owner.

The health projection is deliberately separate from the canonical account
journal.  A process heartbeat is operational evidence, not a trading-domain
event, and must not change execution state hashes or paper/demo parity.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .artifact_snapshot import read_stable_file
from .account_contracts import (
    AccountEvent,
    GENESIS_HASH,
)
from .account_kernel import read_account_journal_head
from .deterministic_serialization import canonical_json


ACCOUNT_OWNER_HEALTH_SCHEMA_VERSION = 2
ACCOUNT_OWNER_HEALTH_FILENAME = "account_owner_health.json"
# Stable test value for fixtures. Production construction must always provide
# systemd's actual INVOCATION_ID explicitly; there is deliberately no dataclass
# default that could hide a missing generation binding.
TEST_ACCOUNT_OWNER_INVOCATION_ID = "00000000000000000000000000000001"
# Risk-increasing target producers require a much tighter bound than the
# operator-facing watchdog. Owners normally publish every five seconds.
TARGET_PRODUCER_HEALTH_MAX_AGE_NS = 30_000_000_000
ACCOUNT_OWNER_HEALTH_BIND_ATTEMPTS = 3
QUEUE_HEAD_MARKET_WARMUP_DETAIL_PREFIX = "waiting for queue-head market data:"
# The paper twin prefixes every health detail with its execution-model scope
# annotation, which is not a blocking reason. Strip that leading annotation
# before classifying the first real reason, or the bounded warmup transition
# pages CRITICAL on paper and self-resolves minutes later, every time
# (observed hourly on 2026-07-29: "execution_model_scope=…; waiting for
# queue-head market data: TLMUSDT:stale_book").
_HEALTH_DETAIL_ANNOTATION_PREFIXES = ("execution_model_scope=",)


def first_blocking_health_reason(detail: str) -> str:
    """Return the health detail without its leading non-blocking annotations."""

    remaining = str(detail).strip()
    while True:
        for annotation in _HEALTH_DETAIL_ANNOTATION_PREFIXES:
            if remaining.startswith(annotation):
                head, separator, tail = remaining.partition(";")
                if not separator:
                    return ""
                remaining = tail.strip()
                break
        else:
            return remaining


class AccountOwnerHealthStatus(StrEnum):
    HEALTHY = "healthy"
    BLOCKED = "blocked"


class AccountOwnerMarketWarmupPending(RuntimeError):
    """Fresh owner health is fail-closed during bounded queue-head L2 warmup."""


class AccountOwnerHealthHeadPending(RuntimeError):
    """Fresh healthy owner evidence is briefly behind an advancing journal."""


def validate_systemd_invocation_id(value: object, *, label: str = "systemd INVOCATION_ID") -> str:
    """Return one canonical non-zero systemd invocation identifier."""

    if type(value) is not str or len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be exactly 32 lowercase hexadecimal characters")
    if value == "0" * 32:
        raise ValueError(f"{label} cannot be the zero identifier")
    return value


def require_systemd_invocation_id(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Read and strictly validate the current service generation from systemd."""

    source = os.environ if environment is None else environment
    value = source.get("INVOCATION_ID")
    if value is None:
        raise RuntimeError("systemd INVOCATION_ID is required for account-owner startup")
    try:
        return validate_systemd_invocation_id(value)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


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
            f"attempts={item.retry_attempts}/{item.retry_budget_label}"
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

    ``invocation_id`` binds the heartbeat to one systemd service generation;
    ``journal_sequence`` and ``journal_state_hash`` bind it to the exact
    canonical state observed by that owner.  They are evidence references only;
    this projection never mutates that state.
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
    invocation_id: str
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
        validate_systemd_invocation_id(
            self.invocation_id,
            label="account-owner health invocation_id",
        )
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
                invocation_id=payload["invocation_id"],
                last_batch_id=str(payload["last_batch_id"]),
                detail=str(payload["detail"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid account-owner health artifact: {exc}") from exc


def account_owner_health_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ACCOUNT_OWNER_HEALTH_FILENAME


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created_temporary = False
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        created_temporary = True
        try:
            os.fchmod(descriptor, 0o600)
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
        if created_temporary:
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
        snapshot = read_stable_file(
            path,
            label="account-owner health artifact",
            reject_empty=True,
            require_single_link=True,
        )
        payload = json.loads(snapshot.data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    expected_invocation_id: str | None = None,
    head_binding: str = "exact",
) -> AccountOwnerHealth:
    """Require fresh health bound to a current authoritative journal head.

    The owner verifies and reconstructs the full journal before publishing any
    health for its systemd generation. Hot-path consumers bind that health to
    the latest immutable transaction segment without parsing and reducing every
    historical payload. Full journal audits remain separate and are not
    weakened by this metadata-only head read.

    ``head_binding`` selects the consumer contract. ``"exact"`` (default)
    requires the health snapshot to name the exact current head — a genuine
    safety property for sizing consumers, whose capital evidence must not
    predate a fill that already changed the balance. ``"allow_behind"`` is
    for liveness consumers: the background execution consumer appends fills
    independently of the owner loop, so during active trading the on-disk
    health ordinarily lags the live head by one transaction until the next
    republish. A fresh, healthy snapshot strictly behind an advancing journal
    is that ordinary race, not corruption; staleness stays bounded by
    ``max_age_ns`` and a health *ahead* of the journal still fails closed.
    """

    if environment not in {"demo", "paper"}:
        raise ValueError("expected owner environment must be 'demo' or 'paper'")
    if head_binding not in {"exact", "allow_behind"}:
        raise ValueError("head_binding must be 'exact' or 'allow_behind'")
    if max_age_ns <= 0:
        raise ValueError("max account-owner health age must be positive")
    if expected_account_id is not None and not expected_account_id:
        raise ValueError("expected account-owner account_id cannot be empty")
    expected_generation = (
        None
        if expected_invocation_id is None
        else validate_systemd_invocation_id(
            expected_invocation_id,
            label="expected account-owner invocation id",
        )
    )
    def validate_health(health: AccountOwnerHealth) -> None:
        if health.environment != environment:
            raise RuntimeError(f"account-owner health environment is {health.environment}, expected {environment}")
        if expected_generation is not None and health.invocation_id != expected_generation:
            raise RuntimeError(
                "account-owner health invocation id does not match the current systemd generation: "
                f"health={health.invocation_id}, expected={expected_generation}"
            )
        observed_now = time.time_ns() if now_ns is None else int(now_ns)
        age_ns = observed_now - health.observed_ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            raise RuntimeError(f"account-owner health is stale: age_ns={age_ns}")
        if expected_account_id is not None and health.account_id != expected_account_id:
            raise RuntimeError(
                f"account-owner health account_id is {health.account_id!r}, "
                f"expected {expected_account_id!r}"
            )
        # Freshness and generation precede status detail. Otherwise a process
        # that hangs while blocked can leave a nonterminal message masking a
        # stale heartbeat indefinitely from the watchdog.
        if AccountOwnerHealthStatus(health.status) is not AccountOwnerHealthStatus.HEALTHY:
            detail = health.detail or "no detail"
            error = f"account owner is blocked: {detail}"
            if first_blocking_health_reason(detail).startswith(QUEUE_HEAD_MARKET_WARMUP_DETAIL_PREFIX):
                raise AccountOwnerMarketWarmupPending(error)
            raise RuntimeError(error)
        if not health.requested_symbols_ready:
            raise RuntimeError("account owner has unready requested symbols")

    def matches_head(
        candidate: AccountOwnerHealth,
        head: AccountEvent | None,
    ) -> bool:
        journal_sequence = head.sequence if head is not None else 0
        journal_state_hash = head.state_hash if head is not None else GENESIS_HASH
        return (
            candidate.journal_sequence == journal_sequence
            and candidate.journal_state_hash == journal_state_hash
            and (head is None or candidate.account_id == head.account_id)
        )

    # Health publication follows the journal transaction it names. Read a
    # health/head/health triplet so either health observation may bind to the
    # immutable head's linearization point. Ordinary heartbeat replacement and
    # a concurrent append are harmless; a head with no matching fresh health
    # still fails closed after bounded retries.
    saw_churn = False
    health: AccountOwnerHealth | None = None
    head: AccountEvent | None = None
    for _attempt in range(ACCOUNT_OWNER_HEALTH_BIND_ATTEMPTS):
        before = read_account_owner_health(root)
        validate_health(before)
        head = read_account_journal_head(root)
        health = read_account_owner_health(root)
        validate_health(health)
        saw_churn = saw_churn or health != before
        if matches_head(health, head):
            return health
        if matches_head(before, head):
            return before
    assert health is not None
    journal_sequence = head.sequence if head is not None else 0
    journal_state_hash = head.state_hash if head is not None else GENESIS_HASH
    if (
        head_binding == "allow_behind"
        and health.journal_sequence < journal_sequence
        and (head is None or health.account_id == head.account_id)
    ):
        # Fresh, healthy, and strictly behind a live advancing journal: the
        # ordinary fill-thread publish race. Equal-sequence hash disagreement
        # still falls through to the corruption raises below.
        return health
    if (
        health.journal_sequence < journal_sequence
        and head is not None
        and health.account_id == head.account_id
    ):
        # Exact-head sizing consumers must not use the older capital snapshot,
        # but this ordinary publication race is retryable. Keep it distinct
        # from a health snapshot ahead of the journal or an equal-sequence hash
        # contradiction, which remain terminal integrity failures.
        raise AccountOwnerHealthHeadPending(
            "account-owner health journal sequence mismatch: "
            f"health={health.journal_sequence}, journal={journal_sequence}"
        )
    if saw_churn:
        raise RuntimeError("account-owner health changed while binding the journal head")
    if health.journal_sequence != journal_sequence:
        raise RuntimeError(
            "account-owner health journal sequence mismatch: "
            f"health={health.journal_sequence}, journal={journal_sequence}"
        )
    if health.journal_state_hash != journal_state_hash:
        raise RuntimeError("account-owner health journal state hash mismatch")
    assert head is not None
    raise RuntimeError(
        f"account-owner health account_id {health.account_id!r} does not match "
        f"journal account_id {head.account_id!r}"
    )
