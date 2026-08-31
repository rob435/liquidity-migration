"""Deterministic research replay of the native Rust Exodus reducer."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.exodus_models import (
    ExodusState,
    ExodusTrigger,
)
from liquidity_migration.rules.rust_strategy_contract import (
    RustStrategyContract,
    load_rendered_native_config,
)


EXODUS_REPLAY_SCHEMA_VERSION = 1
EXODUS_DECISION_APPLICATION_ORDER = (
    "persist_checkpoint",
    "consume_carry_fire",
    "order_effects",
)
EXODUS_REPLAY_EVIDENCE_BOUNDARY = {
    "kind": "decision_contract_replay",
    "calls_live_reducer": True,
    "calls_rust_reducer": True,
    "publishes_targets": False,
    "proves_venue_fills": False,
    "notes": [
        "A typed event tape proves recorded decision inputs, not venue fills.",
        "Minute klines cannot prove tick order, queue position, entry or cover fills, or slippage.",
        "This replay checks the native reducer output, checkpoint bytes, and effect order; it does not authorize orders.",
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


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value)


def _effective_config(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = {
        "profile_name",
        "environment",
        "entry_leverage",
        "registered_rule_path",
        "registered_rule_sha256",
    }
    _exact_fields(payload, expected, label="Exodus replay effective_config")
    if not isinstance(payload["profile_name"], str) or not payload["profile_name"].strip():
        raise ValueError("Exodus replay profile_name is required")
    if not isinstance(payload["environment"], str):
        raise ValueError("Exodus replay environment must be a string")
    leverage = payload["entry_leverage"]
    if (
        isinstance(leverage, bool)
        or not isinstance(leverage, (int, float))
        or not math.isfinite(float(leverage))
        or float(leverage) <= 0.0
    ):
        raise ValueError("Exodus replay entry leverage must be positive and finite")
    if not isinstance(payload["registered_rule_path"], str):
        raise ValueError("Exodus replay registered rule path must be a string")
    if not isinstance(payload["registered_rule_sha256"], str):
        raise ValueError("Exodus replay registered rule digest must be a string")
    raw_rule_path = Path(str(payload["registered_rule_path"])).expanduser()
    rule_path = raw_rule_path if raw_rule_path.is_absolute() else _REPO_ROOT / raw_rule_path
    rule_bytes = rule_path.read_bytes()
    rule_sha256 = _sha256(rule_bytes)
    if rule_sha256 != payload["registered_rule_sha256"]:
        raise ValueError("Exodus replay registered rule digest does not match")
    environment = payload["environment"]
    if environment not in {"demo", "mainnet"}:
        raise ValueError("Exodus replay environment must be demo or mainnet")
    config = load_rendered_native_config(realm=environment, sleeve="exodus")
    expected_native = {
        "profile_name": payload["profile_name"],
        "environment": environment,
        "entry_leverage": payload["entry_leverage"],
        "rule_sha256": rule_sha256,
    }
    if any(config.get(key) != value for key, value in expected_native.items()):
        raise ValueError("Exodus replay config does not match the renderer-owned engine config")
    return config, {
        "profile_name": {"source": "replay input", "detail": config["profile_name"]},
        "rule": {
            "source": str(payload["registered_rule_path"]),
            "detail": rule_sha256,
        },
        "environment": {"source": "replay input", "detail": config["environment"]},
        "entry_leverage": {"source": "replay input", "detail": str(config["entry_leverage"])},
        "native_config": {
            "source": f"deploy/engine.{environment}.toml.template",
            "detail": "exodus_native.config_json",
        },
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
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw[1:]):
            raise ValueError("Exodus replay held_positions has an invalid quantity or entry price")
        qty = float(raw[1])
        entry_px = float(raw[2])
        if not math.isfinite(qty) or qty <= 0.0 or not math.isfinite(entry_px) or entry_px <= 0.0:
            raise ValueError("Exodus replay held_positions has an invalid quantity or entry price")
        holdings[symbol] = (str(raw[0]), qty, entry_px)
    return holdings


def _decision_input(payload: Mapping[str, Any]) -> dict[str, Any]:
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
    if type(payload["now_ms"]) is not int or payload["now_ms"] <= 0:
        raise ValueError("Exodus replay clock must be a positive integer")
    events = [ExodusTrigger.from_dict(_mapping(row, label="Exodus replay trigger")).to_dict() for row in raw_events]
    return {
        "now_ms": payload["now_ms"],
        "events": events,
        "held_symbols": _optional_symbol_set(payload["held_symbols"], label="held_symbols"),
        "working_entry_symbols": _optional_symbol_set(
            payload["working_entry_symbols"],
            label="working_entry_symbols",
        ),
        "held_positions": _optional_holdings(payload["held_positions"]),
    }


def _native_state(state: ExodusState) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "open": {
            record.symbol: {
                "symbol": record.symbol,
                "notional_usdt": record.notional_usdt,
                "settlement_ts_ms": record.settlement_ts_ms,
                "fired_ts_ms": record.fired_ts_ms,
                "target_qty": record.target_qty,
            }
            for record in state.open_records
        },
        "consumed_event_ids": list(state.consumed_event_ids),
        "entry_closed_ts_ms_by_symbol": dict(state.entry_closed_ts_ms_by_symbol),
        "refused_entries": [],
        "entry_retry_after_ms": {},
    }


def _rust_input(decision: Mapping[str, Any], fingerprint: str | None) -> dict[str, Any]:
    held_positions = decision["held_positions"]
    facts_held: dict[str, Any] = {}
    if held_positions is not None:
        for symbol, row in held_positions.items():
            side, qty, entry_px = row
            facts_held[symbol] = {
                "qty": qty,
                "side": "Sell" if side == "short" else "Buy",
                "px": entry_px,
                "entry_px": entry_px,
                "stop_px": entry_px * (1.35 if side == "short" else 0.65),
            }
    symbols = {event["symbol"] for event in decision["events"]} | set(facts_held)
    prices = {event["symbol"]: event["mark_px"] for event in decision["events"] if event["mark_px"] is not None}
    prices.update({symbol: row["px"] for symbol, row in facts_held.items()})
    instrument_rule = {
        "tick_size": 0.01,
        "qty_step": 0.01,
        "min_qty": 0.01,
        "min_notional": 0.01,
    }
    account_healthy = (
        decision["held_symbols"] is not None
        and decision["working_entry_symbols"] is not None
        and held_positions is not None
    )
    return {
        "now_ms": decision["now_ms"],
        "events": decision["events"],
        "facts": {
            "held": facts_held,
            "prices": prices,
            "rules": {symbol: instrument_rule for symbol in sorted(symbols)},
        },
        "owned_working_symbols": sorted(decision["working_entry_symbols"] or ()),
        "owned_opening_order_ids": {},
        "account_healthy": account_healthy,
        "checkpoint_fingerprint": fingerprint,
    }


def replay_exodus_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replay ordered cycles through one persistent native Rust process."""

    expected = {
        "schema_version",
        "name",
        "effective_config",
        "initial_state",
        "cycles",
    }
    _exact_fields(payload, expected, label="Exodus replay")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != EXODUS_REPLAY_SCHEMA_VERSION:
        raise ValueError("unsupported Exodus replay schema")
    name = payload["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Exodus replay name is required")
    config, provenance = _effective_config(
        _mapping(payload["effective_config"], label="Exodus replay effective_config")
    )
    prior = _native_state(
        ExodusState.from_dict(_mapping(payload["initial_state"], label="Exodus replay initial_state"))
    )
    raw_cycles = payload["cycles"]
    if not isinstance(raw_cycles, list) or not raw_cycles:
        raise ValueError("Exodus replay cycles must be a non-empty array")

    steps: list[dict[str, Any]] = []
    decision_fingerprint: str | None = None
    with RustStrategyContract() as contract:
        for index, raw_cycle in enumerate(raw_cycles):
            cycle = _mapping(raw_cycle, label=f"Exodus replay cycle {index}")
            _exact_fields(
                cycle,
                {"name", "decision_input"},
                label=f"Exodus replay cycle {index}",
            )
            cycle_name = cycle["name"]
            if not isinstance(cycle_name, str) or not cycle_name.strip():
                raise ValueError(f"Exodus replay cycle {index} name is required")
            prior_bytes = _canonical_bytes(prior)
            decision_input = _decision_input(
                _mapping(
                    cycle["decision_input"],
                    label=f"Exodus replay cycle {index} input",
                )
            )
            output = contract.request(
                {
                    "schema_version": 1,
                    "operation": "exodus_reduce",
                    "config": config,
                    "input": _rust_input(decision_input, decision_fingerprint),
                    "prior": prior,
                }
            )
            effects = output.get("execution", {}).get("effects")
            if not isinstance(effects, list) or not effects:
                raise RuntimeError("Rust Exodus reducer returned no durable checkpoint effect")
            checkpoint = effects[0]
            if checkpoint.get("kind") != "persist_checkpoint" or checkpoint.get("symbol") != "":
                raise RuntimeError("Rust Exodus reducer did not persist whole-sleeve state first")
            raw_checkpoint = checkpoint.get("payload")
            if not isinstance(raw_checkpoint, list) or any(
                type(byte) is not int or not 0 <= byte <= 255 for byte in raw_checkpoint
            ):
                raise RuntimeError("Rust Exodus reducer returned invalid checkpoint bytes")
            checkpoint_bytes = bytes(raw_checkpoint)
            next_state = output.get("next_state")
            if json.loads(checkpoint_bytes) != next_state:
                raise RuntimeError("Rust Exodus checkpoint bytes disagree with its typed state")
            fingerprint = checkpoint.get("config_fingerprint")
            if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                raise RuntimeError("Rust Exodus reducer returned an invalid decision fingerprint")
            if decision_fingerprint is not None and fingerprint != decision_fingerprint:
                raise RuntimeError("Rust Exodus decision fingerprint changed during one replay")
            decision_fingerprint = fingerprint
            output_bytes = _canonical_bytes(output)
            summary = output["summary"]
            steps.append(
                {
                    "name": cycle_name,
                    "now_ms": decision_input["now_ms"],
                    "application_order": list(EXODUS_DECISION_APPLICATION_ORDER),
                    "effect_order": [effect["kind"] for effect in effects],
                    "prior_checkpoint_utf8": prior_bytes.decode("utf-8"),
                    "prior_checkpoint_sha256": _sha256(prior_bytes),
                    "checkpoint_utf8": checkpoint_bytes.decode("utf-8"),
                    "checkpoint_sha256": _sha256(checkpoint_bytes),
                    "reducer_output": output,
                    "reducer_output_sha256": _sha256(output_bytes),
                    "opened_event_ids": summary["opened_event_ids"],
                    "opened_symbols": summary["opened_symbols"],
                    "covered_symbols": summary["covered_symbols"],
                    "entry_closed_symbols": summary["entry_closed_symbols"],
                    "retired_symbols": summary["retired_symbols"],
                    "blocked_events": summary["blocked_events"],
                    "next_cover_ts_ms": summary["next_cover_ts_ms"],
                }
            )
            prior = next_state

    return {
        "schema_version": EXODUS_REPLAY_SCHEMA_VERSION,
        "name": name,
        "decision_config_sha256": decision_fingerprint,
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
