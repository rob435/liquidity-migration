"""Causal evidence for strategy books consumed by the Rust engine."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.engine_targets import parse_target_book_bytes
from liquidity_migration.strategy.strategy_event_clock import StrategyEvent


CAPTURE_SCHEMA_VERSION = 2
_GENESIS_HASH = hashlib.sha256(b"liquidity-migration-target-book-capture-v1").hexdigest()


def _book_snapshot(path: str | Path) -> tuple[Path, bytes, str, tuple[str, ...]]:
    resolved = Path(path).expanduser().absolute()
    snapshot = read_stable_file(
        resolved,
        label="engine target book",
        reject_empty=True,
        require_single_link=True,
    )
    parsed = parse_target_book_bytes(snapshot.data)
    decision_keys = tuple(f"{parsed.source}/{target.symbol}" for target in parsed.targets)
    return resolved, snapshot.data, hashlib.sha256(snapshot.data).hexdigest(), decision_keys


class PublishedTargetCyclePayload(dict[str, Any]):
    """Persisted cycle summary bound to the exact durable target book."""

    __slots__ = (
        "engine_target_book_path",
        "target_book_path",
        "target_book_sha256",
        "decision_keys",
    )

    target_book_path: Path
    target_book_sha256: str
    decision_keys: tuple[str, ...]

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        target_book_path: str | Path,
        target_book_object_path: str | Path | None = None,
    ) -> None:
        engine_path, engine_data, digest, decision_keys = _book_snapshot(target_book_path)
        path = engine_path
        if target_book_object_path is not None:
            path, object_data, object_digest, object_keys = _book_snapshot(target_book_object_path)
            if object_data != engine_data or object_digest != digest or object_keys != decision_keys:
                raise ValueError("active target book differs from its immutable object")
        super().__init__(payload)
        self.engine_target_book_path = engine_path
        self.target_book_path = path
        self.target_book_sha256 = digest
        self.decision_keys = decision_keys


@dataclass(frozen=True, slots=True)
class TargetBookCapture:
    event_id: str
    event_ts_ns: int
    environment: str
    sleeve: str
    strategy_profile: str
    target_book_sha256: str
    target_book_object: str
    decision_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_ts_ns": self.event_ts_ns,
            "environment": self.environment,
            "sleeve": self.sleeve,
            "strategy_profile": self.strategy_profile,
            "target_book_sha256": self.target_book_sha256,
            "target_book_object": self.target_book_object,
            "decision_keys": list(self.decision_keys),
        }


def capture_event_from_cycle(
    event: StrategyEvent,
    payload: PublishedTargetCyclePayload,
    *,
    sleeve: str,
) -> TargetBookCapture:
    if type(payload) is not PublishedTargetCyclePayload:
        raise TypeError("cycle result lacks a typed target-book receipt")
    _path, _data, current_digest, decision_keys = _book_snapshot(payload.target_book_path)
    if current_digest != payload.target_book_sha256 or decision_keys != payload.decision_keys:
        raise ValueError("target book changed between cycle completion and evidence capture")
    _engine_path, _engine_data, engine_digest, engine_keys = _book_snapshot(
        payload.engine_target_book_path
    )
    if engine_digest != current_digest or engine_keys != decision_keys:
        raise ValueError("active target book changed before evidence capture")
    environment = str(event.payload.get("execution_environment") or "")
    strategy_profile = str(event.payload.get("strategy_profile") or "")
    if not environment or not strategy_profile or not sleeve:
        raise ValueError("target-book capture identity is incomplete")
    return TargetBookCapture(
        event_id=event.event_id,
        event_ts_ns=event.event_ts_ns,
        environment=environment,
        sleeve=sleeve,
        strategy_profile=strategy_profile,
        target_book_sha256=current_digest,
        target_book_object=str(payload.target_book_path),
        decision_keys=decision_keys,
    )


class JsonlTargetBookCaptureTape:
    """Append-only hash chain binding input events to durable target books."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._tail_hash = _GENESIS_HASH
        self._seen_event_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return
        try:
            snapshot = read_stable_file(
                self.path,
                label="target-book capture tape",
                reject_empty=False,
                require_single_link=True,
            )
        except FileNotFoundError:  # pragma: no cover - read_stable_file normalizes this
            return
        tail_hash = _GENESIS_HASH
        for line_number, raw in enumerate(snapshot.data.splitlines(keepends=True), start=1):
            if not raw.endswith(b"\n"):
                raise ValueError(f"target-book capture tape has a partial line at {line_number}")
            row = json.loads(raw)
            if not isinstance(row, Mapping) or set(row) != {
                "schema_version",
                "prior_hash",
                "capture_hash",
                "capture",
            }:
                raise ValueError(f"target-book capture tape has invalid fields at {line_number}")
            if row["schema_version"] != CAPTURE_SCHEMA_VERSION or row["prior_hash"] != tail_hash:
                raise ValueError(f"target-book capture tape chain breaks at {line_number}")
            capture = row["capture"]
            if not isinstance(capture, Mapping):
                raise ValueError(f"target-book capture tape has invalid capture at {line_number}")
            expected = hashlib.sha256(tail_hash.encode("ascii") + canonical_json(capture)).hexdigest()
            if row["capture_hash"] != expected:
                raise ValueError(f"target-book capture tape hash mismatch at {line_number}")
            event_id = str(capture.get("event_id") or "")
            if not event_id or event_id in self._seen_event_ids:
                raise ValueError(f"target-book capture tape repeats an event at {line_number}")
            self._seen_event_ids.add(event_id)
            tail_hash = expected
        self._tail_hash = tail_hash

    def append_from_cycle(
        self,
        event: StrategyEvent,
        payload: PublishedTargetCyclePayload,
        *,
        sleeve: str,
    ) -> TargetBookCapture:
        capture = capture_event_from_cycle(event, payload, sleeve=sleeve)
        if capture.event_id in self._seen_event_ids:
            raise ValueError(f"duplicate target-book capture event: {capture.event_id}")
        capture_row = capture.to_dict()
        next_hash = hashlib.sha256(
            self._tail_hash.encode("ascii") + canonical_json(capture_row)
        ).hexdigest()
        row = canonical_json(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "prior_hash": self._tail_hash,
                "capture_hash": next_hash,
                "capture": capture_row,
            }
        ) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(self.path),
            os.O_CREAT
            | os.O_APPEND
            | os.O_WRONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(row)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("target-book capture append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._seen_event_ids.add(capture.event_id)
        self._tail_hash = next_hash
        return capture
