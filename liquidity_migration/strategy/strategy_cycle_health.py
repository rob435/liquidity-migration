"""Durable completion evidence for one strategy-producer service generation.

Strategy cycle timestamps are causal scheduling inputs.  They deliberately do
not move when a slow cycle finishes, so they cannot also serve as operational
completion heartbeats.  This private projection is written only after the
cycle output and its scheduling evidence are durable.  It is not strategy
state and never participates in replay, accounting, or decision hashes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.env_flags import validate_systemd_invocation_id
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.account.execution_environment import EXECUTION_ENVIRONMENT_VALUES


STRATEGY_CYCLE_HEALTH_SCHEMA_VERSION = 1
STRATEGY_CYCLE_HEALTH_FILENAME = "strategy_cycle_health.json"
STRATEGY_CYCLE_HEALTH_MAX_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class StrategyCycleHealth:
    """Latest fully evidenced cycle completed by one systemd invocation."""

    sleeve: str
    environment: str
    cycle_id: str
    cycle_ts_ms: int
    completed_ts_ns: int
    invocation_id: str
    ws_kline_store_rows: int | None
    schema_version: int = STRATEGY_CYCLE_HEALTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != STRATEGY_CYCLE_HEALTH_SCHEMA_VERSION:
            raise ValueError(f"unsupported strategy-cycle health schema {self.schema_version}")
        if type(self.sleeve) is not str or self.sleeve not in {"long", "carry"}:
            raise ValueError("strategy-cycle health sleeve must be 'long' or 'carry'")
        if type(self.environment) is not str or self.environment not in EXECUTION_ENVIRONMENT_VALUES:
            raise ValueError("strategy-cycle health environment must be a registered execution environment")
        if type(self.cycle_id) is not str or not self.cycle_id or len(self.cycle_id) > 500:
            raise ValueError("strategy-cycle health cycle_id must be 1..500 characters")
        if type(self.cycle_ts_ms) is not int or self.cycle_ts_ms <= 0:
            raise ValueError("strategy-cycle health cycle_ts_ms must be a positive integer")
        if type(self.completed_ts_ns) is not int or self.completed_ts_ns <= 0:
            raise ValueError("strategy-cycle health completed_ts_ns must be a positive integer")
        if self.completed_ts_ns < self.cycle_ts_ms * 1_000_000:
            raise ValueError("strategy-cycle health completion cannot predate its causal cycle")
        validate_systemd_invocation_id(
            self.invocation_id,
            label="strategy-cycle health invocation_id",
        )
        if self.ws_kline_store_rows is not None and (
            type(self.ws_kline_store_rows) is not int or self.ws_kline_store_rows < 0
        ):
            raise ValueError("strategy-cycle health ws_kline_store_rows must be null or a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StrategyCycleHealth":
        expected = set(cls.__dataclass_fields__)
        missing = sorted(expected - set(payload))
        unknown = sorted(set(payload) - expected)
        if missing:
            raise ValueError(f"strategy-cycle health is missing fields: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"strategy-cycle health has unknown fields: {', '.join(unknown)}")
        try:
            return cls(
                schema_version=payload["schema_version"],
                sleeve=payload["sleeve"],
                environment=payload["environment"],
                cycle_id=payload["cycle_id"],
                cycle_ts_ms=payload["cycle_ts_ms"],
                completed_ts_ns=payload["completed_ts_ns"],
                invocation_id=payload["invocation_id"],
                ws_kline_store_rows=payload["ws_kline_store_rows"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid strategy-cycle health artifact: {exc}") from exc


def strategy_cycle_health_path(root: str | Path) -> Path:
    return Path(root).expanduser() / ".cache" / STRATEGY_CYCLE_HEALTH_FILENAME


def _atomic_private_replace(path: Path, data: bytes) -> None:
    """Replace one private projection without exposing a torn target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
        created = True
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("strategy-cycle health write made no progress")
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
        if created:
            temporary.unlink(missing_ok=True)
        raise


def write_strategy_cycle_health(
    root: str | Path,
    health: StrategyCycleHealth,
) -> Path:
    """Atomically publish the latest fully completed producer cycle."""

    path = strategy_cycle_health_path(root)
    _atomic_private_replace(path, canonical_json(health.to_dict()) + b"\n")
    return path


def read_strategy_cycle_health(root: str | Path) -> StrategyCycleHealth:
    """Read and strictly validate one private completion projection."""

    path = strategy_cycle_health_path(root)
    try:
        snapshot = read_stable_file(
            path,
            label="strategy-cycle health artifact",
            reject_empty=True,
            require_mode=0o600,
            require_single_link=True,
            max_bytes=STRATEGY_CYCLE_HEALTH_MAX_BYTES,
        )
        payload = json.loads(snapshot.data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read strategy-cycle health artifact {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"strategy-cycle health artifact must contain an object: {path}")
    if canonical_json(payload) + b"\n" != snapshot.data:
        raise ValueError(f"strategy-cycle health artifact is not canonical JSON: {path}")
    return StrategyCycleHealth.from_dict(payload)
