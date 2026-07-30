"""Small, receipt-bound CONTINUOUS status projection for account notifications.

The cycle ledger remains the durable analytical record.  This module publishes
only the handful of fields needed by the account owner's hourly notification so
that the latency-sensitive owner never scans a growing Parquet ledger.  A
consumer reads the producer's completion receipt first and accepts the status
projection only when both artifacts name the same cycle.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifact_snapshot import read_stable_file
from .deterministic_serialization import canonical_json
from .execution_environment import EXECUTION_ENVIRONMENT_VALUES
from .strategy_cycle_health import read_strategy_cycle_health


# v2: funnel rows gained the settled-funding admission stage ("funding"),
# 2026-07-26 single-component profile change point.
CONTINUOUS_CYCLE_STATUS_SCHEMA_VERSION = 2
CONTINUOUS_CYCLE_STATUS_FILENAME = "continuous_cycle_status.json"
CONTINUOUS_CYCLE_STATUS_MAX_BYTES = 64 * 1024
_FUNNEL_FIELDS = (
    "component",
    "d9",
    "liquidity",
    "event",
    "age",
    "funding",
    "available",
    "capacity",
    "reserved",
    "same_signal_reentry",
)
_BLOCKED_FIELDS = ("component", "symbol", "first_rejection_reason")


def _bounded_text(value: Any, *, label: str, maximum: int = 500) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    return rendered


def _nonnegative_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ContinuousCycleStatus:
    """Notification-sized projection of one fully persisted cycle payload."""

    cycle_id: str
    cycle_ts_ms: int
    environment: str
    btc_trend_gate: str
    btc_trend_gate_allows_entry: bool
    btc_trend_gate_value: float | None
    btc_trend_gate_lookback_days: int
    entry_funnel: tuple[dict[str, Any], ...]
    qualified_but_blocked: tuple[dict[str, str], ...]
    entry_first_rejection_reason: str
    entry_targets_queued: int
    schema_version: int = CONTINUOUS_CYCLE_STATUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CONTINUOUS_CYCLE_STATUS_SCHEMA_VERSION:
            raise ValueError(f"unsupported CONTINUOUS cycle-status schema {self.schema_version}")
        _bounded_text(self.cycle_id, label="cycle_id")
        if not self.cycle_id:
            raise ValueError("cycle_id is required")
        if type(self.cycle_ts_ms) is not int or self.cycle_ts_ms <= 0:
            raise ValueError("cycle_ts_ms must be a positive integer")
        if self.environment not in EXECUTION_ENVIRONMENT_VALUES:
            raise ValueError("environment must be exactly 'demo' or 'paper'")
        if self.btc_trend_gate not in {"off", "uptrend", "downtrend"}:
            raise ValueError("btc_trend_gate must be off, uptrend, or downtrend")
        if type(self.btc_trend_gate_allows_entry) is not bool:
            raise ValueError("btc_trend_gate_allows_entry must be boolean")
        if self.btc_trend_gate_value is not None and (
            type(self.btc_trend_gate_value) not in {int, float} or not math.isfinite(float(self.btc_trend_gate_value))
        ):
            raise ValueError("btc_trend_gate_value must be null or finite")
        _nonnegative_int(
            self.btc_trend_gate_lookback_days,
            label="btc_trend_gate_lookback_days",
        )
        _nonnegative_int(self.entry_targets_queued, label="entry_targets_queued")
        _bounded_text(
            self.entry_first_rejection_reason,
            label="entry_first_rejection_reason",
        )
        for index, row in enumerate(self.entry_funnel):
            if not isinstance(row, Mapping) or set(row) != set(_FUNNEL_FIELDS):
                raise ValueError(f"entry_funnel[{index}] has an invalid schema")
            _bounded_text(row["component"], label=f"entry_funnel[{index}].component", maximum=100)
            if not row["component"]:
                raise ValueError(f"entry_funnel[{index}].component is required")
            for field in _FUNNEL_FIELDS[1:]:
                _nonnegative_int(row[field], label=f"entry_funnel[{index}].{field}")
        for index, row in enumerate(self.qualified_but_blocked):
            if not isinstance(row, Mapping) or set(row) != set(_BLOCKED_FIELDS):
                raise ValueError(f"qualified_but_blocked[{index}] has an invalid schema")
            for field in _BLOCKED_FIELDS:
                text = _bounded_text(
                    row[field],
                    label=f"qualified_but_blocked[{index}].{field}",
                    maximum=200,
                )
                if not text:
                    raise ValueError(f"qualified_but_blocked[{index}].{field} is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_cycle_payload(cls, payload: Mapping[str, Any]) -> "ContinuousCycleStatus":
        try:
            funnel = json.loads(str(payload["entry_funnel_json"]))
            blocked = json.loads(str(payload["qualified_but_blocked_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cycle payload lacks valid entry diagnostics: {exc}") from exc
        if not isinstance(funnel, list) or not isinstance(blocked, list):
            raise ValueError("cycle entry diagnostics must contain JSON arrays")
        if not all(isinstance(row, Mapping) for row in funnel):
            raise ValueError("cycle entry funnel contains a non-object row")
        if not all(isinstance(row, Mapping) for row in blocked):
            raise ValueError("cycle blocked-symbol diagnostics contain a non-object row")
        value = payload.get("btc_trend_gate_value")
        allows_entry = payload.get("btc_trend_gate_allows_entry")
        if type(allows_entry) is not bool:
            raise ValueError("cycle BTC gate admission flag must be boolean")
        return cls(
            cycle_id=str(payload.get("cycle_id") or ""),
            cycle_ts_ms=int(payload.get("ts_ms") or 0),
            environment=str(payload.get("mode") or "").removesuffix("_target"),
            btc_trend_gate=str(payload.get("btc_trend_gate") or ""),
            btc_trend_gate_allows_entry=allows_entry,
            btc_trend_gate_value=(None if value is None else float(value)),
            btc_trend_gate_lookback_days=int(payload.get("btc_trend_gate_lookback_days") or 0),
            entry_funnel=tuple(dict(row) for row in funnel),
            qualified_but_blocked=tuple(
                {str(key): str(item) for key, item in row.items()} for row in blocked
            ),
            entry_first_rejection_reason=str(payload.get("entry_first_rejection_reason") or ""),
            entry_targets_queued=int(payload.get("entry_targets_queued") or 0),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContinuousCycleStatus":
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        if missing:
            raise ValueError(f"CONTINUOUS cycle status is missing fields: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"CONTINUOUS cycle status has unknown fields: {', '.join(unknown)}")
        funnel = payload["entry_funnel"]
        blocked = payload["qualified_but_blocked"]
        if not isinstance(funnel, list) or not isinstance(blocked, list):
            raise ValueError("CONTINUOUS cycle-status diagnostics must be arrays")
        try:
            return cls(
                schema_version=payload["schema_version"],
                cycle_id=payload["cycle_id"],
                cycle_ts_ms=payload["cycle_ts_ms"],
                environment=payload["environment"],
                btc_trend_gate=payload["btc_trend_gate"],
                btc_trend_gate_allows_entry=payload["btc_trend_gate_allows_entry"],
                btc_trend_gate_value=payload["btc_trend_gate_value"],
                btc_trend_gate_lookback_days=payload["btc_trend_gate_lookback_days"],
                entry_funnel=tuple(dict(row) for row in funnel),
                qualified_but_blocked=tuple(dict(row) for row in blocked),
                entry_first_rejection_reason=payload["entry_first_rejection_reason"],
                entry_targets_queued=payload["entry_targets_queued"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid CONTINUOUS cycle-status artifact: {exc}") from exc


def continuous_cycle_status_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ".cache" / CONTINUOUS_CYCLE_STATUS_FILENAME


def _atomic_private_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("CONTINUOUS cycle-status write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_continuous_cycle_status(
    root: str | Path,
    status: ContinuousCycleStatus,
) -> Path:
    path = continuous_cycle_status_path(root)
    data = canonical_json(status.to_dict()) + b"\n"
    if len(data) > CONTINUOUS_CYCLE_STATUS_MAX_BYTES:
        raise ValueError(
            "CONTINUOUS cycle-status projection exceeds the 64-KiB limit"
        )
    _atomic_private_replace(path, data)
    return path


def read_continuous_cycle_status(root: str | Path) -> ContinuousCycleStatus:
    path = continuous_cycle_status_path(root)
    try:
        snapshot = read_stable_file(
            path,
            label="CONTINUOUS cycle-status artifact",
            reject_empty=True,
            require_mode=0o600,
            require_single_link=True,
            max_bytes=CONTINUOUS_CYCLE_STATUS_MAX_BYTES,
        )
        payload = json.loads(snapshot.data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read CONTINUOUS cycle-status artifact {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("CONTINUOUS cycle-status artifact must contain an object")
    if canonical_json(payload) + b"\n" != snapshot.data:
        raise ValueError("CONTINUOUS cycle-status artifact is not canonical JSON")
    return ContinuousCycleStatus.from_dict(payload)


def _human_reason(value: str) -> str:
    return value.replace("_", " ").strip() or "none"


def render_continuous_cycle_status(
    status: ContinuousCycleStatus,
    *,
    now_ns: int | None = None,
) -> str:
    """Render the notification block for one completed cycle.

    The funnel and blocked lines describe the cycle that STARTED at
    ``cycle_ts_ms``, which can lag the surrounding message by several minutes
    (the reader serves the newest fully completed cycle).  When ``now_ns`` is
    provided the block names that instant and its age, so a reader cannot
    mistake a pre-close funnel snapshot for the account state rendered next to
    it in the same message.
    """

    gate_state = "OPEN" if status.btc_trend_gate_allows_entry else "BLOCKED"
    if status.btc_trend_gate == "off":
        gate_detail = "off"
    else:
        value = status.btc_trend_gate_value
        rendered_value = "unavailable" if value is None else f"{value:+.2%}"
        gate_detail = f"{status.btc_trend_gate} · {status.btc_trend_gate_lookback_days}d {rendered_value}"
    totals = {
        field: sum(int(row[field]) for row in status.entry_funnel)
        for field in ("d9", "liquidity", "event", "age", "funding", "capacity")
    }
    symbols = sorted({row["symbol"] for row in status.qualified_but_blocked})
    visible = symbols[:8]
    blocked_text = ", ".join(visible) if visible else "none"
    if len(symbols) > len(visible):
        blocked_text += f" +{len(symbols) - len(visible)} more"
    funnel_scope = "component opportunities"
    if now_ns is not None:
        cycle_hhmm = datetime.fromtimestamp(
            status.cycle_ts_ms / 1000.0, tz=timezone.utc
        ).strftime("%H:%M")
        age_minutes = max(
            0.0, (int(now_ns) - status.cycle_ts_ms * 1_000_000) / 60_000_000_000
        )
        funnel_scope = (
            f"component opportunities · cycle {cycle_hhmm} UTC, "
            f"{age_minutes:.1f} min before this update"
        )
    return "\n".join(
        (
            f"CONTINUOUS BTC gate: {gate_state} · {gate_detail}",
            f"CONTINUOUS funnel ({funnel_scope}): "
            f"D9 {totals['d9']} → liquidity {totals['liquidity']} → "
            f"event {totals['event']} → age {totals['age']} → "
            f"funding {totals['funding']} → capacity {totals['capacity']}",
            f"CONTINUOUS qualified but blocked: {blocked_text} · "
            f"first rejection {_human_reason(status.entry_first_rejection_reason)}",
        )
    )


class ContinuousCycleStatusReader:
    """Cache one exact projection and bind it to the producer receipt."""

    __slots__ = ("root", "environment", "max_age_ns", "_key", "_status")

    def __init__(
        self,
        root: str | Path,
        *,
        environment: str = "demo",
        max_age_minutes: float = 15.0,
    ) -> None:
        if environment not in EXECUTION_ENVIRONMENT_VALUES:
            raise ValueError("CONTINUOUS status environment must be demo or paper")
        if not math.isfinite(max_age_minutes) or max_age_minutes <= 0.0:
            raise ValueError("CONTINUOUS status max age must be positive and finite")
        self.root = Path(root).expanduser()
        self.environment = environment
        self.max_age_ns = int(max_age_minutes * 60.0 * 1_000_000_000)
        self._key: tuple[str, int, int] | None = None
        self._status: ContinuousCycleStatus | None = None

    def render(self, *, now_ns: int) -> str:
        try:
            health = read_strategy_cycle_health(self.root)
            if health.sleeve != "continuous" or health.environment != self.environment:
                raise ValueError("completion receipt scope mismatch")
            key = (health.cycle_id, health.cycle_ts_ms, health.completed_ts_ns)
            if self._key != key or self._status is None:
                status = read_continuous_cycle_status(self.root)
                if status.cycle_id != health.cycle_id or status.cycle_ts_ms != health.cycle_ts_ms:
                    raise ValueError("status projection does not match the completed cycle receipt")
                if status.environment != self.environment:
                    raise ValueError("status projection environment mismatch")
                self._key = key
                self._status = status
            age_ns = int(now_ns) - health.completed_ts_ns
            if age_ns < -5_000_000_000:
                raise ValueError("completion receipt is future-dated")
            if age_ns > self.max_age_ns:
                age_minutes = age_ns / 60_000_000_000
                return f"CONTINUOUS BTC gate: STALE · last completed cycle is {age_minutes:.1f} min old"
            assert self._status is not None
            return render_continuous_cycle_status(self._status, now_ns=int(now_ns))
        except (OSError, RuntimeError, ValueError) as exc:
            detail = str(exc).replace("\n", " ").strip()[:160]
            return f"CONTINUOUS BTC gate: unavailable · {detail or type(exc).__name__}"


__all__ = [
    "CONTINUOUS_CYCLE_STATUS_SCHEMA_VERSION",
    "ContinuousCycleStatus",
    "ContinuousCycleStatusReader",
    "continuous_cycle_status_path",
    "read_continuous_cycle_status",
    "render_continuous_cycle_status",
    "write_continuous_cycle_status",
]
