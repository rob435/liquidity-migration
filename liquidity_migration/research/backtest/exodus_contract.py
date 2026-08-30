"""Deterministic research replay of the live Exodus decision contract.

The replay consumes the rules-owned trigger, prior-state, engine-projection,
clock, and effective decision-config shapes. It calls the exact reducer used by
the live producer and never publishes a target book.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.exodus_contract import (
    EXODUS_DECISION_APPLICATION_ORDER,
    ExodusDecisionConfig,
    ExodusDecisionInput,
    ExodusState,
    ExodusTrigger,
    decide_exodus,
)
from liquidity_migration.rules.exodus_short import ExodusShortConfig


EXODUS_REPLAY_SCHEMA_VERSION = 1
EXODUS_REPLAY_EVIDENCE_BOUNDARY = {
    "kind": "decision_contract_replay",
    "calls_live_reducer": True,
    "publishes_targets": False,
    "proves_venue_fills": False,
    "notes": [
        "A typed event tape proves recorded decision inputs, not venue fills.",
        "Minute klines cannot prove tick order, queue position, entry or cover fills, or slippage.",
        "This replay checks decision, state, and target bytes; it does not authorize orders.",
    ],
}
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_fields(payload: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{label} has unexpected or missing fields")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _state_bytes(state: ExodusState) -> bytes:
    return canonical_json(state.to_dict()) + b"\n"


def _effective_config(payload: Mapping[str, Any]) -> tuple[ExodusDecisionConfig, dict[str, Any]]:
    expected = {
        "profile_name",
        "environment",
        "entry_leverage",
        "registered_rule_path",
        "registered_rule_sha256",
    }
    _exact_fields(payload, expected, label="Exodus replay effective_config")
    raw_rule_path = Path(str(payload["registered_rule_path"])).expanduser()
    rule_path = raw_rule_path if raw_rule_path.is_absolute() else _REPO_ROOT / raw_rule_path
    rule_bytes = rule_path.read_bytes()
    rule_sha256 = _sha256(rule_bytes)
    if rule_sha256 != payload["registered_rule_sha256"]:
        raise ValueError("Exodus replay registered rule digest does not match")
    config = ExodusDecisionConfig(
        profile_name=str(payload["profile_name"]),
        rule=ExodusShortConfig.from_json(rule_path),
        environment=str(payload["environment"]),
        entry_leverage=float(payload["entry_leverage"]),
    )
    return config, {
        "profile_name": {"source": "replay input", "detail": config.profile_name},
        "rule": {
            "source": str(payload["registered_rule_path"]),
            "detail": rule_sha256,
        },
        "environment": {"source": "replay input", "detail": config.environment},
        "entry_leverage": {"source": "replay input", "detail": str(config.entry_leverage)},
    }


def _optional_symbol_set(value: object, *, label: str) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(symbol, str) for symbol in value):
        raise ValueError(f"{label} must be null or an array of symbols")
    if value != sorted(set(value)):
        raise ValueError(f"{label} must be uniquely sorted")
    return frozenset(value)


def _optional_holdings(value: object) -> Mapping[str, tuple[str, float, float]] | None:
    if value is None:
        return None
    payload = _mapping(value, label="Exodus replay held_positions")
    if list(payload) != sorted(payload):
        raise ValueError("Exodus replay held_positions must be sorted by symbol")
    holdings: dict[str, tuple[str, float, float]] = {}
    for symbol, raw in payload.items():
        if not isinstance(symbol, str) or not isinstance(raw, list) or len(raw) != 3 or raw[0] not in {"long", "short"}:
            raise ValueError("Exodus replay held_positions has an invalid row")
        qty = float(raw[1])
        entry_px = float(raw[2])
        if not math.isfinite(qty) or qty <= 0.0 or not math.isfinite(entry_px) or entry_px <= 0.0:
            raise ValueError("Exodus replay held_positions has an invalid quantity or entry price")
        holdings[symbol] = (str(raw[0]), qty, entry_px)
    return holdings


def _decision_input(payload: Mapping[str, Any]) -> ExodusDecisionInput:
    expected = {
        "now_ms",
        "events",
        "held_symbols",
        "working_entry_symbols",
        "held_positions",
    }
    _exact_fields(payload, expected, label="Exodus replay decision_input")
    raw_events = payload["events"]
    if not isinstance(raw_events, list):
        raise ValueError("Exodus replay events must be an array")
    events = tuple(ExodusTrigger.from_dict(_mapping(row, label="Exodus replay trigger")) for row in raw_events)
    return ExodusDecisionInput(
        now_ms=payload["now_ms"],
        events=events,
        held_symbols=_optional_symbol_set(payload["held_symbols"], label="held_symbols"),
        working_entry_symbols=_optional_symbol_set(
            payload["working_entry_symbols"],
            label="working_entry_symbols",
        ),
        held_positions=_optional_holdings(payload["held_positions"]),
    )


def replay_exodus_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replay ordered cycles through the same reducer used by the daemon."""

    expected = {
        "schema_version",
        "name",
        "effective_config",
        "initial_state",
        "cycles",
    }
    _exact_fields(payload, expected, label="Exodus replay")
    if payload["schema_version"] != EXODUS_REPLAY_SCHEMA_VERSION:
        raise ValueError("unsupported Exodus replay schema")
    name = payload["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Exodus replay name is required")
    config, provenance = _effective_config(
        _mapping(payload["effective_config"], label="Exodus replay effective_config")
    )
    prior = ExodusState.from_dict(_mapping(payload["initial_state"], label="Exodus replay initial_state"))
    raw_cycles = payload["cycles"]
    if not isinstance(raw_cycles, list) or not raw_cycles:
        raise ValueError("Exodus replay cycles must be a non-empty array")

    steps: list[dict[str, Any]] = []
    for index, raw_cycle in enumerate(raw_cycles):
        cycle = _mapping(raw_cycle, label=f"Exodus replay cycle {index}")
        _exact_fields(cycle, {"name", "decision_input"}, label=f"Exodus replay cycle {index}")
        cycle_name = cycle["name"]
        if not isinstance(cycle_name, str) or not cycle_name.strip():
            raise ValueError(f"Exodus replay cycle {index} name is required")
        prior_bytes = _state_bytes(prior)
        decision_input = _decision_input(_mapping(cycle["decision_input"], label=f"Exodus replay cycle {index} input"))
        output = decide_exodus(decision_input, prior, config)
        staged_bytes = _state_bytes(output.staged_state)
        final_bytes = _state_bytes(output.final_state)
        steps.append(
            {
                "name": cycle_name,
                "now_ms": decision_input.now_ms,
                "application_order": list(EXODUS_DECISION_APPLICATION_ORDER),
                "prior_state_utf8": prior_bytes.decode("utf-8"),
                "prior_state_sha256": _sha256(prior_bytes),
                "staged_state_utf8": staged_bytes.decode("utf-8"),
                "staged_state_sha256": _sha256(staged_bytes),
                "target_book_utf8": output.target_book_bytes.decode("utf-8"),
                "target_book_sha256": _sha256(output.target_book_bytes),
                "final_state_utf8": final_bytes.decode("utf-8"),
                "final_state_sha256": _sha256(final_bytes),
                "opened_event_ids": list(output.opened_event_ids),
                "opened_symbols": list(output.opened_symbols),
                "covered_symbols": list(output.covered_symbols),
                "entry_closed_symbols": list(output.entry_closed_symbols),
                "retired_symbols": list(output.retired_symbols),
                "blocked_events": [list(row) for row in output.blocked_events],
                "next_cover_ts_ms": output.next_cover_ts_ms,
            }
        )
        prior = ExodusState.from_dict(json.loads(final_bytes))

    return {
        "schema_version": EXODUS_REPLAY_SCHEMA_VERSION,
        "name": name,
        "effective_config_sha256": config.sha256,
        "effective_config_provenance": provenance,
        "application_order": list(EXODUS_DECISION_APPLICATION_ORDER),
        "evidence_boundary": EXODUS_REPLAY_EVIDENCE_BOUNDARY,
        "steps": steps,
    }


def replay_exodus_contract_file(path: str | Path) -> dict[str, Any]:
    """Load one replay input and return the deterministic report."""

    source = Path(path).expanduser()
    payload = json.loads(source.read_text(encoding="utf-8"))
    return replay_exodus_contract(_mapping(payload, label="Exodus replay"))


def render_exodus_replay_report(report: Mapping[str, Any]) -> bytes:
    """Canonical bytes for a citable replay artifact."""

    return canonical_json(report) + b"\n"
