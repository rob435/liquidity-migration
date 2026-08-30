from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import polars as pl

from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.operational_profile import load_operational_profile
from liquidity_migration.rules.long_contract import (
    ConfigLayer,
    DecisionInput,
    PriorState,
    decide,
    resolve_strategy_config,
)
from liquidity_migration.strategy.long_book_state import BOOK_STATE_VERSION, LongBookState
from liquidity_migration.strategy.long_native_event_demo import (
    _advance_long_book_state,
    _long_engine_target_book,
    _select_long_entry_candidates,
)
from liquidity_migration.strategy.strategy_event_clock import (
    DeterministicEventClock,
    MemoryStrategyEventTape,
    load_strategy_event_tape_bytes,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "long_cross_language_replay_v1.json"
REPO_ROOT = Path(__file__).parents[2]


def _resolved_config(bundle: dict[str, object]):
    assert set(bundle) == {
        "profile_name",
        "operational_profile",
        "layers",
        "resolved_config_sha256",
    }
    profile_identity = bundle["operational_profile"]
    assert isinstance(profile_identity, dict)
    profile_path = REPO_ROOT / str(profile_identity["path"])
    operational = load_operational_profile(profile_path)
    assert operational.source_sha256 == profile_identity["sha256"]

    layer_rows = bundle["layers"]
    assert isinstance(layer_rows, list)
    assert layer_rows == [
        {
            "source": "operational_profile",
            "detail": (f"{profile_identity['path']}#{profile_identity['sha256']}"),
            "values": {
                "notional_multiplier": operational.long.notional_multiplier,
                "entry_leverage": operational.long.entry_leverage,
                "order_notional_pct_equity": operational.long.order_notional_pct_equity,
                "max_new_entries_per_cycle": operational.long.max_new_entries_per_cycle,
            },
        }
    ]
    layers = tuple(ConfigLayer(**row) for row in layer_rows)
    config = resolve_strategy_config(str(bundle["profile_name"]), layers=layers)
    assert hashlib.sha256(canonical_json(config.as_json_dict())).hexdigest() == bundle["resolved_config_sha256"]
    return config


def _recorded_events(fixture: dict[str, object]):
    tape = fixture["strategy_event_tape"]
    assert isinstance(tape, dict)
    tape_bytes = str(tape["utf8"]).encode()
    assert hashlib.sha256(tape_bytes).hexdigest() == tape["sha256"]
    events, tape_hash = load_strategy_event_tape_bytes(tape_bytes)
    assert len(events) == tape["event_count"] == 5
    assert tape_hash == tape["final_tape_hash"]
    return events


def _replay_python_tape(fixture: dict[str, object]):
    events = _recorded_events(fixture)
    expected_order = [
        "flat_entry",
        "pending_entry_holds_original_target",
        "base_stop_exits_before_decay",
        "decayed_stop_exits_after_fill_clock",
        "time_exit_wins_at_fill_deadline",
    ]

    def run_cycle(event):
        decision_ts_ms = event.event_ts_ns // 1_000_000
        assert event.event_ts_ns == decision_ts_ms * 1_000_000
        assert event.source == "long-native-mainnet"
        payload = dict(event.payload)
        assert set(payload) == {
            "execution_environment",
            "strategy_profile",
            "replay_envelope",
        }
        assert payload["execution_environment"] == "mainnet"
        envelope = dict(payload["replay_envelope"])
        assert set(envelope) == {
            "schema_version",
            "case_name",
            "decision_input",
            "prior_state",
            "effective_config",
            "quote",
            "instrument_rule",
            "account",
        }
        assert envelope["schema_version"] == 1
        case_name = str(envelope["case_name"])
        assert case_name == expected_order[event.source_sequence - 1]
        assert event.kind == ("confirmed_bar" if case_name == "flat_entry" else "timer")
        config = _resolved_config(dict(envelope["effective_config"]))
        assert payload["strategy_profile"] == config.rule.execution_strategy_id

        recorded_input = dict(envelope["decision_input"])
        assert recorded_input.pop("schema_version") == 1
        assert recorded_input.pop("decision_ts_ms") == decision_ts_ms
        decision_input = DecisionInput(decision_ts_ms=decision_ts_ms, **recorded_input)
        prior_state = PriorState(**envelope["prior_state"])
        quote = dict(envelope["quote"])
        assert set(quote) == {
            "symbol",
            "bid_px",
            "bid_qty",
            "ask_px",
            "ask_qty",
            "venue_ts_ms",
            "seq",
        }
        quote_mid = (quote["bid_px"] + quote["ask_px"]) / 2.0
        assert quote["symbol"] == decision_input.symbol
        assert quote["venue_ts_ms"] == decision_ts_ms
        assert quote["seq"] == event.source_sequence
        assert quote["bid_px"] < quote["ask_px"]
        assert quote["bid_qty"] > 0.0
        assert quote["ask_qty"] > 0.0
        assert quote_mid == decision_input.market_price
        rule = dict(envelope["instrument_rule"])
        assert set(rule) == {"tick_size", "qty_step", "min_qty", "min_notional"}
        assert all(float(value) > 0.0 for value in rule.values())
        account = dict(envelope["account"])
        assert set(account) == {
            "equity_usdt",
            "available_usdt",
            "observed_ts_ns",
            "positions",
        }
        assert account["equity_usdt"] == decision_input.equity_usdt
        assert 0.0 <= account["available_usdt"] <= account["equity_usdt"]
        assert account["observed_ts_ns"] == event.event_ts_ns
        positions = account["positions"]
        assert isinstance(positions, list)
        if prior_state.filled:
            assert len(positions) == 1
            position = dict(positions[0])
            assert set(position) == {
                "symbol",
                "side",
                "qty",
                "px",
                "entry_px",
                "stop_attached",
                "stop_px",
                "leverage",
            }
            assert position["symbol"] == decision_input.symbol
            assert position["side"] == "buy"
            raw_qty = prior_state.target_notional_usdt / prior_state.entry_price
            assert abs(position["qty"] - raw_qty) <= rule["qty_step"] / 2.0
            assert position["px"] == decision_input.market_price
            assert position["entry_px"] == prior_state.entry_price
            assert position["stop_attached"] is True
            stop_fraction = (
                prior_state.decayed_stop_loss_fraction
                if decision_ts_ms - prior_state.entry_ts_ms >= prior_state.stop_decay_after_ms
                else prior_state.stop_loss_fraction
            )
            raw_stop = prior_state.entry_price * (1.0 - stop_fraction)
            assert abs(position["stop_px"] - raw_stop) <= rule["tick_size"] / 2.0
            assert position["leverage"] == config.entry_leverage
        else:
            assert positions == []

        output = decide(decision_input, prior_state, config)
        result = {
            "case_name": case_name,
            "decision_input": decision_input,
            "prior_state": prior_state,
            "config": config,
            "envelope": envelope,
            "output": output,
        }
        if case_name != "flat_entry":
            return result

        candidates, skips = _select_long_entry_candidates(
            features=pl.DataFrame([dict(decision_input.feature_row or {})]),
            all_trades=pl.DataFrame(),
            now_ms=decision_ts_ms,
            strategy=config.rule,
            price_by_symbol={decision_input.symbol: float(decision_input.market_price or 0.0)},
            effective_config=config,
            equity_usdt=decision_input.equity_usdt,
            attempted_signals_ms={},
            blocked_symbols=frozenset(),
            active_positions=prior_state.active_positions,
        )
        live_state, resized = _advance_long_book_state(
            LongBookState(),
            exit_plans=[],
            candidates=candidates,
            price_by_symbol={decision_input.symbol: float(decision_input.market_price or 0.0)},
            strategy_id=config.rule.execution_strategy_id,
            now_ms=decision_ts_ms,
            cooldown_days=int(config.rule.cooldown_days),
            held_symbols=frozenset(),
            max_new_entries=config.max_new_entries_per_cycle,
            max_total_positions=config.rule.max_concurrent_positions,
        )
        target_book = _long_engine_target_book(
            live_state,
            decision_ts_ms=decision_ts_ms,
            strategy_profile=config.rule.execution_strategy_id,
            effective_config=config,
        )
        state_payload = {
            "version": BOOK_STATE_VERSION,
            "held": [asdict(entry) for entry in sorted(live_state.held.values(), key=lambda entry: entry.symbol)],
            "left_at_ms": dict(sorted(live_state.left_at_ms.items())),
            "attempted_signals_ms": dict(sorted(live_state.attempted_signals_ms.items())),
        }
        result.update(
            candidates=candidates,
            skips=skips,
            resized=resized,
            state_payload=state_payload,
            target_book=target_book,
        )
        return result

    replay_recorder = MemoryStrategyEventTape()
    replay_clock = DeterministicEventClock(
        clock=VirtualClock(current_wall_ns=events[0].event_ts_ns),
        recorder=replay_recorder,
    )
    results = replay_clock.replay(events, run_cycle)
    assert replay_clock.tape_hash == fixture["strategy_event_tape"]["final_tape_hash"]
    replayed = {str(result["case_name"]): result for result in results}
    assert list(replayed) == expected_order
    return replayed


def test_long_fixture_pins_the_decision_and_exact_rust_handoff_bytes() -> None:
    fixture = json.loads(FIXTURE.read_text())
    replayed = _replay_python_tape(fixture)
    flat_entry = replayed["flat_entry"]
    decision_input = flat_entry["decision_input"]
    output = flat_entry["output"]
    config = flat_entry["config"]
    candidates = flat_entry["candidates"]
    skips = flat_entry["skips"]
    resized = flat_entry["resized"]
    state_payload = flat_entry["state_payload"]
    target_book = flat_entry["target_book"]
    quote = flat_entry["envelope"]["quote"]
    quote_mid = (quote["bid_px"] + quote["ask_px"]) / 2.0
    assert decision_input.market_price == quote_mid
    assert decision_input.signal_close == decision_input.feature_row["close"]
    assert quote_mid <= decision_input.signal_close * (1.0 - config.rule.fc_sniper_retrace_pct)

    canonical_config = json.loads(json.dumps(config.as_json_dict(), sort_keys=True))
    assert canonical_config == fixture["strategy_config"]
    assert output.as_json_dict() == fixture["expected_decision_output"]
    assert "take_profit" not in output.as_json_dict()

    assert not any(skips.values())
    assert len(candidates) == 1
    assert candidates[0]["target_notional_usdt"] == output.target_notional_usdt
    assert candidates[0]["entry_valid_until_ms"] == output.entry_valid_until_ms
    assert candidates[0]["stop_loss_pct"] == output.stop_loss_fraction
    assert candidates[0]["entry_leverage"] == output.entry_leverage

    assert resized == []
    assert state_payload == fixture["expected_live_state"]

    assert target_book == fixture["target_book_utf8"]
    assert hashlib.sha256(target_book.encode()).hexdigest() == fixture["target_book_sha256"]


def test_long_fixture_pins_existing_position_transitions() -> None:
    fixture = json.loads(FIXTURE.read_text())
    replayed = _replay_python_tape(fixture)
    expected_names = [case["name"] for case in fixture["python_decision_cases"]]
    assert expected_names == list(replayed)[1:]

    for case in fixture["python_decision_cases"]:
        assert set(case) == {"name", "expected_decision_output"}
        output = replayed[case["name"]]["output"]
        assert output.as_json_dict() == case["expected_decision_output"], case["name"]
