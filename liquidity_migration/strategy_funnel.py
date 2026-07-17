"""Observer-only strategy-funnel events and structural validation.

This module owns diagnostic serialization, not strategy decisions.  Callers
must compute gates from causal strategy state and pass immutable row snapshots;
writer failures are reported without becoming an admission gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

import polars as pl


_LOGGER = logging.getLogger(__name__)

GateState = Literal["pass", "fail", "missing", "not_applicable"]
GATE_STATES: tuple[GateState, ...] = (
    "pass",
    "fail",
    "missing",
    "not_applicable",
)


class DecisionFunnelObserver(Protocol):
    """Consume one complete causal evaluation without influencing its caller."""

    def observe(self, row: Mapping[str, Any]) -> None: ...


def canonical_payload(value: Any) -> bytes:
    """Return the stable JSON representation used for diagnostic identities."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_payload(value)).hexdigest()


def decision_funnel_source_key(
    *,
    sleeve: str,
    venue: str,
    symbol: str,
    signal_ts_ms: int,
) -> str:
    normalized = (
        str(sleeve).strip().lower(),
        str(venue).strip().lower(),
        str(symbol).strip().upper(),
        int(signal_ts_ms),
    )
    if not all(normalized[:3]) or normalized[3] <= 0:
        raise ValueError("decision-funnel source key requires sleeve, venue, symbol, and positive signal time")
    return ":".join((*normalized[:3], str(normalized[3])))


def gate_state(value: bool | None, *, applicable: bool = True) -> GateState:
    if not applicable:
        return "not_applicable"
    if value is None:
        return "missing"
    return "pass" if bool(value) else "fail"


def finalize_funnel_row(
    row: Mapping[str, Any],
    *,
    required_gate_order: Sequence[str],
) -> dict[str, Any]:
    """Add stable identity and rejection semantics to a complete evaluation."""

    output = dict(row)
    output["sleeve"] = str(output["sleeve"]).lower()
    output["venue"] = str(output["venue"]).lower()
    output["symbol"] = str(output["symbol"]).upper()
    output["signal_ts_ms"] = int(output["signal_ts_ms"])
    output["source_key"] = decision_funnel_source_key(
        sleeve=output["sleeve"],
        venue=output["venue"],
        symbol=output["symbol"],
        signal_ts_ms=output["signal_ts_ms"],
    )
    rejection_keys: list[str] = []
    for gate in required_gate_order:
        column = f"gate_{gate}"
        state = output.get(column)
        if state not in GATE_STATES:
            raise ValueError(f"{column} has invalid state {state!r}")
        if state in ("fail", "missing"):
            rejection_keys.append(gate)
    output["required_gate_order"] = list(required_gate_order)
    output["rejection_keys"] = rejection_keys
    output["first_rejection"] = rejection_keys[0] if rejection_keys else None
    output["barebones_accepted"] = all(
        output[f"gate_{gate}"] == "pass" for gate in required_gate_order
    )
    evaluation_ts_ms = int(output.get("evaluation_ts_ms") or output.get("decision_ts_ms") or output["signal_ts_ms"])
    output["evaluation_ts_ms"] = evaluation_ts_ms
    output.setdefault("first_evaluation_ts_ms", evaluation_ts_ms)
    output.setdefault("last_evaluation_ts_ms", evaluation_ts_ms)
    output.setdefault("evaluation_count", 1)
    output["gate_state_sha256"] = payload_sha256(
        {key: output[key] for key in sorted(output) if key.startswith("gate_")}
    )
    return output


def observe_funnel_rows_safely(
    observer: DecisionFunnelObserver | None,
    rows: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Emit immutable copies and surface errors without changing decisions."""

    if observer is None:
        return ()
    errors: list[str] = []
    for row in rows:
        immutable = MappingProxyType(dict(row))
        try:
            observer.observe(immutable)
        except Exception as exc:  # noqa: BLE001 - diagnostic I/O cannot become an alpha gate
            message = f"{type(exc).__name__}: {exc}"[:500]
            errors.append(message)
            _LOGGER.exception("decision-funnel observer failed")
    return tuple(errors)


def _immutable_source_material(row: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "decision_ts_ms",
        "entry_ts_ms",
        "evaluation_ts_ms",
        "first_evaluation_ts_ms",
        "last_evaluation_ts_ms",
        "evaluation_count",
        "gate_state_sha256",
        "rejection_keys",
        "first_rejection",
        "barebones_accepted",
    }
    return {
        key: value
        for key, value in row.items()
        if key not in excluded and not key.startswith("gate_")
    }


class FunnelJsonlWriter:
    """Append source/evaluation changes with restart-safe duplicate suppression."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._source_hashes: dict[str, str] = {}
        self._state_hashes: dict[str, str] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                    key = str(event["source_key"])
                    event_type = str(event["event_type"])
                    source_hash = str(event["source_sha256"])
                    state_hash = str(event["gate_state_sha256"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid funnel event at {self.path}:{line_number}") from exc
                if event_type == "source":
                    if key in self._source_hashes:
                        raise ValueError(f"duplicate funnel source event for {key}")
                    self._source_hashes[key] = source_hash
                elif event_type == "transition":
                    if key not in self._source_hashes:
                        raise ValueError(f"funnel transition precedes source for {key}")
                else:
                    raise ValueError(f"invalid funnel event type {event_type!r}")
                if self._source_hashes[key] != source_hash:
                    raise ValueError(f"funnel source identity changed for {key}")
                self._state_hashes[key] = state_hash

    def observe(self, row: Mapping[str, Any]) -> None:
        source_key = str(row.get("source_key") or "")
        state_hash = str(row.get("gate_state_sha256") or "")
        if not source_key or not state_hash:
            raise ValueError("funnel writer requires finalized source_key and gate_state_sha256")
        source_hash = payload_sha256(_immutable_source_material(row))
        prior_source = self._source_hashes.get(source_key)
        if prior_source is not None and prior_source != source_hash:
            raise ValueError(f"funnel source identity changed for {source_key}")
        if self._state_hashes.get(source_key) == state_hash:
            return
        event_type = "source" if prior_source is None else "transition"
        event = {
            "schema_version": 1,
            "event_type": event_type,
            "source_key": source_key,
            "source_sha256": source_hash,
            "gate_state_sha256": state_hash,
            "evaluation_ts_ms": int(row["evaluation_ts_ms"]),
            "row": dict(row),
        }
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            payload = canonical_payload(event) + b"\n"
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("funnel event append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._source_hashes[source_key] = source_hash
        self._state_hashes[source_key] = state_hash


def validate_decision_funnel(
    frame: pl.DataFrame,
    *,
    required_gate_orders: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Validate source identity, gate ordering, and causal availability only."""

    required = {
        "source_key",
        "sleeve",
        "venue",
        "symbol",
        "signal_ts_ms",
        "feature_ts_ms",
        "data_available_ts_ms",
        "decision_ts_ms",
        "entry_ts_ms",
        "first_rejection",
        "barebones_accepted",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"decision funnel missing columns: {missing}")
    duplicates = frame.group_by("source_key").len().filter(pl.col("len") != 1)
    if not duplicates.is_empty():
        raise ValueError("decision funnel contains duplicate source keys")
    for row in frame.select(
        ["source_key", "sleeve", "venue", "symbol", "signal_ts_ms"]
    ).iter_rows(named=True):
        expected_key = decision_funnel_source_key(
            sleeve=row["sleeve"],
            venue=row["venue"],
            symbol=row["symbol"],
            signal_ts_ms=row["signal_ts_ms"],
        )
        if row["source_key"] != expected_key:
            raise ValueError("decision funnel source key does not match its source fields")
    invalid_clock = frame.filter(
        (pl.col("feature_ts_ms") > pl.col("data_available_ts_ms"))
        | (pl.col("data_available_ts_ms") > pl.col("decision_ts_ms"))
        | (
            pl.col("entry_ts_ms").is_not_null()
            & (pl.col("decision_ts_ms") > pl.col("entry_ts_ms"))
        )
    )
    if not invalid_clock.is_empty():
        raise ValueError("decision funnel contains noncausal clock ordering")
    for sleeve, order in required_gate_orders.items():
        part = frame.filter(pl.col("sleeve") == sleeve)
        for gate in order:
            column = f"gate_{gate}"
            if column not in part.columns:
                raise ValueError(f"{sleeve} funnel missing required {column}")
            invalid = part.filter((~pl.col(column).is_in(list(GATE_STATES))).fill_null(True))
            if not invalid.is_empty():
                raise ValueError(f"{sleeve} funnel contains invalid {column} state")
        for row in part.select(
            ["first_rejection", "barebones_accepted", *[f"gate_{gate}" for gate in order]]
        ).iter_rows(named=True):
            expected = next(
                (gate for gate in order if row[f"gate_{gate}"] in ("fail", "missing")),
                None,
            )
            if row["first_rejection"] != expected:
                raise ValueError(f"{sleeve} first-rejection ordering is inconsistent")
            expected_accepted = all(row[f"gate_{gate}"] == "pass" for gate in order)
            if row["barebones_accepted"] != expected_accepted:
                raise ValueError(f"{sleeve} accepted state is inconsistent with required gates")
    return {
        "rows": frame.height,
        "unique_source_keys": frame["source_key"].n_unique(),
        "accepted_rows": frame.filter(pl.col("barebones_accepted")).height,
    }


def validate_path_labels(labels: pl.DataFrame, funnel: pl.DataFrame) -> dict[str, Any]:
    """Validate label keys and observation clocks without reading label values."""

    required = {"source_key", "entry_ts_ms"}
    for hours in (1, 6, 24, 72):
        required.update(
            {
                f"target_{hours}h_ts_ms",
                f"actual_{hours}h_ts_ms",
                f"return_{hours}h",
                f"missing_{hours}h_reason",
            }
        )
    missing = sorted(required - set(labels.columns))
    if missing:
        raise ValueError(f"path labels missing columns: {missing}")
    if labels["source_key"].n_unique() != labels.height:
        raise ValueError("path labels contain duplicate source keys")
    accepted = set(funnel.filter(pl.col("barebones_accepted"))["source_key"].to_list())
    actual = set(labels["source_key"].to_list())
    if actual != accepted:
        raise ValueError("path-label keys do not exactly match accepted funnel keys")
    for hours in (1, 6, 24, 72):
        actual_column = f"actual_{hours}h_ts_ms"
        target_column = f"target_{hours}h_ts_ms"
        invalid = labels.filter(
            pl.col(actual_column).is_not_null()
            & (
                (pl.col(actual_column) < pl.col(target_column))
                | (pl.col(actual_column) > pl.col(target_column) + 3_600_000)
            )
        )
        if not invalid.is_empty():
            raise ValueError(f"{hours}h labels violate the one-hour observation-lag bound")
        inconsistent = labels.filter(
            (pl.col(actual_column).is_null() & pl.col(f"missing_{hours}h_reason").is_null())
            | (pl.col(actual_column).is_not_null() & pl.col(f"missing_{hours}h_reason").is_not_null())
        )
        if not inconsistent.is_empty():
            raise ValueError(f"{hours}h labels have inconsistent missingness provenance")
    return {
        "rows": labels.height,
        "unique_source_keys": labels["source_key"].n_unique(),
    }
