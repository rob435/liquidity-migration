"""Cross-language fixture replay through the native Rust reducers."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.exodus_models import carry_presettlement_event_id
from liquidity_migration.rules.rust_strategy_contract import (
    RustStrategyContract,
    load_rendered_native_config,
)


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def replay_long_fixture(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1 or payload.get("name") != "long_mainnet_recorded_replay_v1":
        raise ValueError("unsupported LONG native replay fixture")
    tape = _mapping(payload.get("strategy_event_tape"), label="LONG strategy_event_tape")
    text = tape.get("utf8")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("LONG strategy event tape is empty")
    expected_by_name = {
        "flat_entry": _mapping(
            payload.get("expected_decision_output"),
            label="LONG expected decision output",
        )
    }
    cases = payload.get("recorded_decision_cases")
    if not isinstance(cases, list):
        raise ValueError("LONG recorded_decision_cases must be an array")
    for case in cases:
        row = _mapping(case, label="LONG recorded decision case")
        name = row.get("name")
        if not isinstance(name, str) or name in expected_by_name:
            raise ValueError("LONG recorded decision case names must be unique strings")
        expected_by_name[name] = _mapping(
            row.get("expected_decision_output"),
            label=f"LONG expected output {name}",
        )

    config = load_rendered_native_config(realm="mainnet", sleeve="long")
    fixture_config = _mapping(payload.get("strategy_config"), label="LONG strategy_config")
    if fixture_config.get("profile_name") != config.get("profile_name"):
        raise ValueError("LONG fixture profile does not match the renderer-owned config")
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    with RustStrategyContract() as contract:
        for raw_line in text.splitlines():
            tape_row = _mapping(json.loads(raw_line), label="LONG tape row")
            envelope = _mapping(
                _mapping(
                    _mapping(tape_row.get("event"), label="LONG tape event").get("payload"),
                    label="LONG tape payload",
                ).get("replay_envelope"),
                label="LONG replay envelope",
            )
            name = envelope.get("case_name")
            if not isinstance(name, str) or name not in expected_by_name or name in seen:
                raise ValueError("LONG replay envelope has an unknown or duplicate case")
            input_payload = dict(_mapping(envelope.get("decision_input"), label=f"LONG decision input {name}"))
            if input_payload.pop("schema_version", None) != 1:
                raise ValueError("LONG decision input schema is unsupported")
            prior = _mapping(envelope.get("prior_state"), label=f"LONG prior state {name}")
            output = contract.request(
                {
                    "schema_version": 1,
                    "operation": "long_decide",
                    "config": config,
                    "input": input_payload,
                    "prior": prior,
                }
            )
            if output != expected_by_name[name]:
                raise ValueError(f"Rust LONG reducer diverged from recorded case {name}")
            outputs.append({"name": name, "output": output, "output_sha256": _sha256(output)})
            seen.add(name)
    if seen != set(expected_by_name):
        raise ValueError("LONG event tape does not cover every recorded decision case")
    return {
        "schema_version": 1,
        "sleeve": "long",
        "name": payload["name"],
        "calls_rust_reducer": True,
        "publishes_orders": False,
        "cases": outputs,
    }


def _carry_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(load_rendered_native_config(realm="demo", sleeve="carry"))
    fixture = _mapping(payload.get("strategy_config"), label="CARRY strategy_config")
    if fixture.get("profile_name") != config.get("profile_name"):
        raise ValueError("CARRY fixture profile does not match the renderer-owned config")
    for key in (
        "early_exit_enabled",
        "presettlement_exit_enabled",
        "notional_multiplier",
        "entry_leverage",
        "stop_loss_fraction",
        "max_new_entries_per_cycle",
        "capital_reference_usdt",
    ):
        config[key] = fixture[key]
    execution = dict(_mapping(fixture.get("execution"), label="CARRY execution config"))
    if execution.pop("schema_version", None) != 1:
        raise ValueError("CARRY execution schema is unsupported")
    config["execution"] = execution
    return config


def _carry_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    prior = _mapping(payload.get("prior_state"), label="CARRY prior_state")
    anchors = prior.get("sizing_anchors")
    fired = prior.get("fired_exits")
    if not isinstance(anchors, list) or not isinstance(fired, list):
        raise ValueError("CARRY prior state maps must be sorted pair arrays")
    return {
        "schema_version": 1,
        "scorer": {
            "by_symbol": {},
            "last_decision_ts_ms": 0,
            "first_replay_ts_ms": 0,
            "last_weights": {},
            "last_universe_size": 0,
        },
        "sizing_anchors": {str(row[0]): row[1] for row in anchors},
        "fired_exits": {row[0]: row[1] for row in fired},
        "desired_targets": {},
        "refused_entries": [],
        "entry_retry_after_ms": {},
        "last_publication_decision_ts_ms": 0,
        "current_decision": None,
    }


def _carry_request(payload: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    decision = _mapping(payload.get("decision_input"), label="CARRY decision_input")
    replay = _mapping(payload.get("rust_replay"), label="CARRY rust_replay")
    raw_held = replay.get("held")
    if not isinstance(raw_held, list):
        raise ValueError("CARRY held facts must be an array")
    held: dict[str, Any] = {}
    for raw in raw_held:
        row = _mapping(raw, label="CARRY held fact")
        held[row["symbol"]] = {
            "qty": row["qty"],
            "side": "Buy" if row["side"] == "buy" else "Sell",
            "px": row["px"],
            "entry_px": row["entry_px"],
            "stop_px": row["stop_px"],
        }
    prices = dict(_mapping(replay.get("prices"), label="CARRY prices"))
    weights = _mapping(
        _mapping(decision.get("decision"), label="CARRY decision").get("weights"),
        label="CARRY decision weights",
    )
    symbols = sorted(set(held) | set(prices) | set(weights))
    instrument_rule = dict(_mapping(replay.get("instrument_rule"), label="CARRY instrument rule"))
    durable_fires = []
    raw_durable = decision.get("durable_presettlement_fires")
    if not isinstance(raw_durable, list):
        raise ValueError("CARRY durable fires must be an array")
    for raw in raw_durable:
        fire = _mapping(raw, label="CARRY durable fire")
        durable_fires.append(
            {
                "event_id": carry_presettlement_event_id(
                    environment="demo",
                    source_config_id=config["rule"]["config_id"],
                    decision_ts_ms=fire["decision_ts_ms"],
                    settlement_ts_ms=fire["settlement_ts_ms"],
                    symbol=fire["symbol"],
                ),
                "environment": "demo",
                "source_profile": config["profile_name"],
                "source_config_id": config["rule"]["config_id"],
                "decision_ts_ms": fire["decision_ts_ms"],
                "fired_ts_ms": fire["observed_ts_ms"],
                "settlement_ts_ms": fire["settlement_ts_ms"],
                "symbol": fire["symbol"],
                "mark_px": fire["mark_px"],
                "carry_side": fire["carry_side"],
                "carry_qty": fire["carry_qty"],
            }
        )
    raw_presettlement = decision.get("presettlement")
    if not isinstance(raw_presettlement, list):
        raise ValueError("CARRY presettlement observations must be an array")
    presettlement = [
        {
            key: row[key]
            for key in (
                "symbol",
                "observed_ts_ms",
                "settlement_ts_ms",
                "running_rate",
                "mark_px",
            )
        }
        for row in raw_presettlement
    ]
    anchor_requests = decision.get("sizing_anchor_requests")
    if not isinstance(anchor_requests, list) or len(anchor_requests) > 1:
        raise ValueError("CARRY fixture supports at most one upcoming anchor")
    upcoming_equity = anchor_requests[0]["equity_usdt"] if anchor_requests else None
    return {
        "schema_version": 1,
        "operation": "carry_reduce",
        "config": config,
        "input": {
            "now_ms": decision["now_ms"],
            "decision": decision["decision"],
            "upcoming_decision": decision["upcoming_decision"],
            "settled_funding": decision["settled_funding"],
            "presettlement": presettlement,
            "durable_fires": durable_fires,
            "trail_by_symbol": dict(decision["trail_by_symbol"]),
            "entry_blockers": dict(decision["entry_blockers"]),
            "account_healthy": decision["account_health_error"] == "",
            "equity_usdt": decision["equity_usdt"],
            "upcoming_sizing_equity_usdt": upcoming_equity,
            "facts": {
                "held": held,
                "prices": prices,
                "rules": {symbol: instrument_rule for symbol in symbols},
            },
            "owned_working_symbols": [],
            "owned_opening_order_ids": {},
            "checkpoint_fingerprint": None,
            "signal_receipt": None,
        },
        "prior": _carry_state(payload),
        "signal_batch": None,
    }


def replay_carry_fixture(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1 or payload.get("name") != "carry_native_reducer_replay_v1":
        raise ValueError("unsupported CARRY native replay fixture")
    config = _carry_config(payload)
    with RustStrategyContract() as contract:
        output = contract.request(_carry_request(payload, config))
    expected = _mapping(
        payload.get("expected_decision_output"),
        label="CARRY expected decision output",
    )
    if output.get("effective_decision") != expected.get("effective_decision"):
        raise ValueError("Rust CARRY effective decision diverged from the recorded fixture")
    if output.get("summary") != expected.get("summary"):
        raise ValueError("Rust CARRY plan summary diverged from the recorded fixture")
    expected_state = _mapping(expected.get("next_state"), label="CARRY expected next state")
    state = _mapping(output.get("next_state"), label="Rust CARRY next state")
    anchors = [[int(key), value] for key, value in state["sizing_anchors"].items()]
    fires = [[key, value] for key, value in state["fired_exits"].items()]
    if anchors != expected_state["sizing_anchors"] or fires != expected_state["fired_exits"]:
        raise ValueError("Rust CARRY durable lifecycle state diverged from the recorded fixture")
    effects = output.get("execution", {}).get("effects")
    if not isinstance(effects, list):
        raise ValueError("Rust CARRY reducer returned invalid effects")
    checkpoints = [effect for effect in effects if effect.get("kind") == "persist_checkpoint"]
    if len(checkpoints) != 1:
        raise ValueError("Rust CARRY reducer did not return one checkpoint")
    return {
        "schema_version": 1,
        "sleeve": "carry",
        "name": payload["name"],
        "calls_rust_reducer": True,
        "publishes_orders": False,
        "effect_order": [effect["kind"] for effect in effects],
        "output": output,
        "output_sha256": _sha256(output),
    }


def replay_native_fixture_file(path: str | Path, *, sleeve: str) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    typed = _mapping(payload, label="native strategy replay fixture")
    if sleeve == "long":
        return replay_long_fixture(typed)
    if sleeve == "carry":
        return replay_carry_fixture(typed)
    raise ValueError("native fixture sleeve must be long or carry")


def render_native_fixture_report(report: Mapping[str, Any]) -> bytes:
    return canonical_json(report) + b"\n"
