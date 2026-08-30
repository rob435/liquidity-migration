from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

import liquidity_migration.strategy.exodus_producer as module
import liquidity_migration.strategy.strategy_event_clock as event_clock_module
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.engine_targets import parse_target_book_bytes
from liquidity_migration.rules.exodus_contract import (
    ExodusDecisionInput,
    ExodusState,
    decide_exodus,
)
from liquidity_migration.rules.exodus_short import ExodusShortRecord
from liquidity_migration.strategy.exodus_producer import (
    load_exodus_state,
    resolve_exodus_effective_config,
    run_exodus_cycle,
    save_exodus_state,
)
from liquidity_migration.strategy.exodus_producer_daemon import (
    ExodusProducerDaemon,
    exodus_wait_seconds,
)
from liquidity_migration.strategy.presettlement_events import (
    CarryPresettlementEvent,
    append_carry_presettlement_event,
    load_carry_presettlement_events,
)


NOW_MS = 1_800_000_000_000
SETTLEMENT_MS = NOW_MS + 10 * 60_000


def test_daemon_wait_is_cut_short_by_the_next_cover_clock() -> None:
    assert (
        exodus_wait_seconds(
            {"next_cover_ts_ms": NOW_MS + 12_500},
            interval_seconds=60.0,
            now_ms=NOW_MS,
        )
        == 12.5
    )
    assert (
        exodus_wait_seconds(
            {"next_cover_ts_ms": NOW_MS + 120_000},
            interval_seconds=60.0,
            now_ms=NOW_MS,
        )
        == 60.0
    )
    assert (
        exodus_wait_seconds(
            {"next_cover_ts_ms": None},
            interval_seconds=60.0,
            now_ms=NOW_MS,
        )
        == 60.0
    )


def _event(
    *,
    symbol: str = "AUSDT",
    carry_side: str | None = "long",
    carry_qty: float | None = 3.25,
    carry_avg_entry_px: float | None = 8.0,
    mark_px: float | None = 10.0,
    fired_ts_ms: int = NOW_MS,
    settlement_ts_ms: int = SETTLEMENT_MS,
    source_profile: str = "carry_hold_v7_live_v1",
    source_config_id: str = "lane2_carry_hold_v7",
) -> CarryPresettlementEvent:
    return CarryPresettlementEvent(
        environment="demo",
        source_profile=source_profile,
        source_config_id=source_config_id,
        decision_ts_ms=NOW_MS - 8 * 60 * 60_000,
        fired_ts_ms=fired_ts_ms,
        settlement_ts_ms=settlement_ts_ms,
        symbol=symbol,
        running_rate=-0.0002,
        mark_px=mark_px,
        carry_side=carry_side,
        carry_qty=carry_qty,
        carry_avg_entry_px=carry_avg_entry_px,
    )


def _config(tmp_path: Path, *, invocation_id: str = ""):
    return resolve_exodus_effective_config(
        profile_name="v1",
        environment="demo",
        data_root=(tmp_path / "state").resolve(),
        interval_seconds=60.0,
        event_path=(tmp_path / "carry-events.jsonl").resolve(),
        target_book_path=(tmp_path / "targets" / "exodus.json").resolve(),
        engine_heartbeat_path=(tmp_path / "heartbeat.json").resolve(),
        expected_account_user_id="account-1",
        invocation_id=invocation_id,
        entry_leverage=5.0,
        operational_profile_path=Path("configs/operational.demo.json").resolve(),
        operational_profile_sha256="11" * 32,
    )


def _stub_flat_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class FlatEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: FlatEngine(),
    )


def _expected_state_contract_sha256(config: module.ExodusEffectiveConfig) -> str:
    payload = {
        "decision": config.decision.to_dict(),
        "expected_account_user_id": config.expected_account_user_id,
    }
    assert {field.name for field in dataclasses.fields(config)} == {
        "decision",
        "data_root",
        "interval_seconds",
        "event_path",
        "target_book_path",
        "engine_heartbeat_path",
        "expected_account_user_id",
        "invocation_id",
        "provenance",
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _expected_legacy_v2_config_sha256(config: module.ExodusEffectiveConfig) -> str:
    payload = {
        "profile_name": config.profile_name,
        "rule": dataclasses.asdict(config.rule),
        "environment": config.environment,
        "event_path": str(config.event_path),
        "target_book_path": str(config.target_book_path),
        "engine_heartbeat_path": str(config.engine_heartbeat_path),
        "expected_account_user_id": config.expected_account_user_id,
        "entry_leverage": config.entry_leverage,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _write_v1_state_identity(root: Path) -> Path:
    path = root / "exodus_state_identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "state_path": str(module.exodus_state_path(root).resolve()),
        "genesis_source": "adopted_owned",
        "legacy_path": "",
        "legacy_sha256": "",
    }
    path.write_bytes(canonical_json(payload) + b"\n")
    return path


def _write_v2_state_identity(
    root: Path,
    config: module.ExodusEffectiveConfig,
) -> Path:
    path = root / "exodus_state_identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "state_path": str(module.exodus_state_path(root).resolve()),
        "genesis_source": "adopted_owned",
        "legacy_path": "",
        "legacy_sha256": "",
        "effective_config_sha256": _expected_legacy_v2_config_sha256(config),
    }
    path.write_bytes(canonical_json(payload) + b"\n")
    return path


def test_effective_config_owns_data_root_and_daemon_cadence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    provenance = config.provenance_dict()

    assert config.data_root == (tmp_path / "state").resolve()
    assert config.interval_seconds == 60.0
    assert provenance["data_root"] == {
        "source": "global command line --data-root",
        "detail": str(config.data_root),
    }
    assert provenance["interval_seconds"] == {
        "source": "command line --interval-seconds",
        "detail": "60.0",
    }
    assert provenance["event_path"]["detail"] == str(config.event_path)
    assert provenance["target_book_path"]["detail"] == str(config.target_book_path)
    daemon = ExodusProducerDaemon(config=config)
    assert daemon.config.data_root == config.data_root
    assert daemon.config.interval_seconds == 60.0
    assert not hasattr(daemon, "data_root")
    assert not hasattr(daemon, "interval_seconds")


def test_new_state_identity_hashes_only_the_state_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    _stub_flat_engine(monkeypatch)

    run_exodus_cycle(config=config, now_ms=NOW_MS)

    identity = json.loads((state_root / "exodus_state_identity.json").read_text(encoding="utf-8"))
    assert set(identity) == {
        "schema_version",
        "state_path",
        "genesis_source",
        "legacy_path",
        "legacy_sha256",
        "state_contract_sha256",
    }
    assert identity["schema_version"] == 3
    assert identity["state_contract_sha256"] == _expected_state_contract_sha256(config)


@pytest.mark.parametrize(
    "drift",
    [
        "rule",
        "profile",
        "expected_account_user_id",
        "environment",
        "entry_leverage",
    ],
)
def test_state_identity_rejects_effective_config_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    _stub_flat_engine(monkeypatch)
    run_exodus_cycle(config=config, now_ms=NOW_MS)
    identity_path = state_root / "exodus_state_identity.json"
    identity_before = identity_path.read_bytes()
    published_before = config.target_book_path.read_bytes()

    if drift == "rule":
        changed = dataclasses.replace(
            config,
            decision=dataclasses.replace(
                config.decision,
                rule=dataclasses.replace(
                    config.rule,
                    cover_minutes_after_settlement=(config.rule.cover_minutes_after_settlement + 1),
                ),
            ),
        )
    elif drift == "profile":
        monkeypatch.setattr(module, "EXODUS_PROFILE_CHOICES", ("v1", "v2"))
        changed = dataclasses.replace(
            config,
            decision=dataclasses.replace(config.decision, profile_name="v2"),
        )
    elif drift == "expected_account_user_id":
        changed = dataclasses.replace(
            config,
            expected_account_user_id="account-2",
        )
    elif drift == "environment":
        changed = dataclasses.replace(
            config,
            decision=dataclasses.replace(config.decision, environment="mainnet"),
        )
    elif drift == "entry_leverage":
        changed = dataclasses.replace(
            config,
            decision=dataclasses.replace(config.decision, entry_leverage=4.0),
        )
    else:  # pragma: no cover - the parameter list is closed above
        raise AssertionError(f"unhandled drift case {drift}")

    with pytest.raises(RuntimeError, match="different effective config"):
        run_exodus_cycle(config=changed, now_ms=NOW_MS + 60_000)

    assert config.target_book_path.read_bytes() == published_before
    assert identity_path.read_bytes() == identity_before


def test_state_identity_allows_new_invocation_cadence_and_io_paths_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _config(tmp_path, invocation_id="1" * 32)
    restarted = dataclasses.replace(
        _config(tmp_path, invocation_id="2" * 32),
        interval_seconds=30.0,
        event_path=(tmp_path / "relocated-events.jsonl").resolve(),
        target_book_path=(tmp_path / "relocated-targets" / "exodus.json").resolve(),
        engine_heartbeat_path=(tmp_path / "relocated-heartbeat.json").resolve(),
    )
    state_root = tmp_path / "state"
    _stub_flat_engine(monkeypatch)

    run_exodus_cycle(config=first, now_ms=NOW_MS)
    run_exodus_cycle(config=restarted, now_ms=NOW_MS + 60_000)

    identity = json.loads((state_root / "exodus_state_identity.json").read_text(encoding="utf-8"))
    assert identity["state_contract_sha256"] == _expected_state_contract_sha256(restarted)
    assert module.effective_config_sha256(first) != module.effective_config_sha256(restarted)
    assert restarted.target_book_path.exists()


def test_empty_v1_state_identity_upgrades_to_the_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    save_exodus_state(state_root, ExodusState())
    identity_path = _write_v1_state_identity(state_root)
    _stub_flat_engine(monkeypatch)

    run_exodus_cycle(config=config, now_ms=NOW_MS)

    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["schema_version"] == 3
    assert identity["state_contract_sha256"] == _expected_state_contract_sha256(config)


def test_nonempty_v2_state_identity_migrates_without_stranding_the_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    state = ExodusState(consumed_event_ids=(_event().event_id,))
    save_exodus_state(config.data_root, state)
    identity_path = _write_v2_state_identity(config.data_root, config)
    _stub_flat_engine(monkeypatch)

    run_exodus_cycle(config=config, now_ms=NOW_MS)

    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity["schema_version"] == 3
    assert identity["state_contract_sha256"] == _expected_state_contract_sha256(config)
    assert "effective_config_sha256" not in identity
    assert load_exodus_state(config.data_root) == state


def test_nonempty_v1_state_identity_cannot_be_attributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    save_exodus_state(
        state_root,
        ExodusState(consumed_event_ids=(_event().event_id,)),
    )
    identity_path = _write_v1_state_identity(state_root)
    _stub_flat_engine(monkeypatch)

    with pytest.raises(RuntimeError, match="cannot be attributed.*nonempty"):
        run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert json.loads(identity_path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert not config.target_book_path.exists()


def test_empty_state_without_identity_is_adopted_by_the_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    save_exodus_state(state_root, ExodusState())
    _stub_flat_engine(monkeypatch)

    run_exodus_cycle(config=config, now_ms=NOW_MS)

    identity = json.loads((state_root / "exodus_state_identity.json").read_text(encoding="utf-8"))
    assert identity["schema_version"] == 3
    assert identity["state_contract_sha256"] == _expected_state_contract_sha256(config)


def test_nonempty_state_without_identity_cannot_be_attributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    state_root = tmp_path / "state"
    state = ExodusState(consumed_event_ids=(_event().event_id,))
    save_exodus_state(state_root, state)
    _stub_flat_engine(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="no config identity.*nonempty.*cannot be attributed",
    ):
        run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert load_exodus_state(state_root) == state
    assert not (state_root / "exodus_state_identity.json").exists()
    assert not config.target_book_path.exists()


def test_typed_event_tape_is_hash_chained_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "carry-events.jsonl"
    event = _event()

    first_hash, appended = append_carry_presettlement_event(path, event)
    second_hash, appended_again = append_carry_presettlement_event(path, event)

    assert appended is True
    assert appended_again is False
    assert second_hash == first_hash
    assert load_carry_presettlement_events(path) == (event,)
    assert len(path.read_bytes().splitlines()) == 1


def test_typed_event_tape_round_trips_a_missing_exact_holding(tmp_path: Path) -> None:
    path = tmp_path / "carry-events.jsonl"
    event = _event(
        carry_side=None,
        carry_qty=None,
        carry_avg_entry_px=None,
        mark_px=None,
    )

    append_carry_presettlement_event(path, event)

    assert load_carry_presettlement_events(path) == (event,)


def test_typed_event_tape_rejects_conflicting_semantic_retry(tmp_path: Path) -> None:
    path = tmp_path / "carry-events.jsonl"
    first = _event(carry_qty=3.25)
    changed = _event(carry_qty=4.0)
    assert first.event_id == changed.event_id
    append_carry_presettlement_event(path, first)

    with pytest.raises(ValueError, match="different contents"):
        append_carry_presettlement_event(path, changed)


def test_semantic_retry_repairs_an_uncertain_tape_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "carry-events.jsonl"
    prior = _event(
        symbol="BUSDT",
        fired_ts_ms=NOW_MS - 60_000,
        settlement_ts_ms=SETTLEMENT_MS,
    )
    event = _event()
    append_carry_presettlement_event(path, prior)

    real_fsync = event_clock_module.os.fsync
    monkeypatch.setattr(
        event_clock_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("uncertain fsync")),
    )
    with pytest.raises(OSError, match="uncertain fsync"):
        append_carry_presettlement_event(path, event)
    assert len(path.read_bytes().splitlines()) == 2

    fsync_calls: list[int] = []

    def observed_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(event_clock_module.os, "fsync", observed_fsync)
    _tape_hash, appended = append_carry_presettlement_event(path, event)

    assert appended is False
    assert fsync_calls
    assert load_carry_presettlement_events(path) == (prior, event)


def test_typed_event_rejects_missing_position_as_incomplete() -> None:
    with pytest.raises(ValueError, match="quantity without a side"):
        _event(carry_side=None, carry_qty=1.0, carry_avg_entry_px=8.0)


def test_pure_plan_opens_exact_abandoned_quantity_and_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    event = _event()

    output = decide_exodus(
        ExodusDecisionInput(
            now_ms=NOW_MS,
            events=(event,),
            held_symbols=frozenset(),
            working_entry_symbols=frozenset(),
        ),
        ExodusState(),
        config,
    )

    assert output.opened_event_ids == (event.event_id,)
    assert output.opened_symbols == ("AUSDT",)
    assert output.blocked_events == ()
    assert output.next_cover_ts_ms == SETTLEMENT_MS + 60 * 60_000
    (record,) = output.active_records
    assert record.target_qty == 3.25
    assert record.notional_usdt == 32.5
    book = parse_target_book_bytes(output.target_book_bytes)
    (target,) = book.targets
    assert target.symbol == "AUSDT"
    assert target.target_qty == -3.25
    assert target.notional_usdt == -32.5
    assert target.leverage == 5.0
    assert target.stop_loss_fraction == 0.35


def test_consumed_event_cannot_double_the_short(tmp_path: Path) -> None:
    config = _config(tmp_path)
    event = _event()
    first = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), frozenset(), frozenset()),
        ExodusState(),
        config,
    )
    second = decide_exodus(
        ExodusDecisionInput(NOW_MS + 60_000, (event,), frozenset(), frozenset()),
        first.final_state,
        config,
    )

    assert second.opened_event_ids == ()
    assert second.active_records == first.active_records
    assert second.final_state == first.final_state


@pytest.mark.parametrize(
    "event",
    [
        _event(source_profile="carry_hold_v6_live_v1"),
        _event(source_config_id="lane2_carry_hold_v6"),
    ],
)
def test_registered_rule_quarantines_an_incompatible_carry_source(
    tmp_path: Path, event: CarryPresettlementEvent
) -> None:
    output = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), frozenset(), frozenset()),
        ExodusState(),
        _config(tmp_path),
    )

    assert output.blocked_events == ((event.event_id, "incompatible_source"),)
    assert output.final_state.consumed_event_ids == ()


def test_consumed_old_source_cannot_block_a_due_cover(tmp_path: Path) -> None:
    old_event = _event(source_profile="carry_hold_v6_live_v1")
    due = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=32.5,
        settlement_ts_ms=NOW_MS - 60 * 60_000,
        fired_ts_ms=NOW_MS - 2 * 60 * 60_000,
        target_qty=3.25,
    )
    output = decide_exodus(
        ExodusDecisionInput(NOW_MS, (old_event,), frozenset(), frozenset()),
        ExodusState((due,), (old_event.event_id,)),
        _config(tmp_path),
    )

    assert output.covered_symbols == ("AUSDT",)
    assert output.blocked_events == ()
    assert parse_target_book_bytes(output.target_book_bytes).targets[0].notional_usdt == 0.0


def test_new_same_symbol_event_waits_for_the_prior_cover_to_finish(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    due = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=20.0,
        settlement_ts_ms=NOW_MS - 60 * 60_000,
        fired_ts_ms=NOW_MS - 2 * 60 * 60_000,
        target_qty=2.0,
    )
    event = _event()

    covering = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), frozenset(), frozenset()),
        ExodusState((due,), ()),
        config,
    )

    assert covering.opened_event_ids == ()
    assert covering.blocked_events == ((event.event_id, "symbol_cover_pending"),)
    assert covering.final_state == ExodusState()
    assert covering.final_state.consumed_event_ids == ()
    assert parse_target_book_bytes(covering.target_book_bytes).targets[0].notional_usdt == 0.0

    opened = decide_exodus(
        ExodusDecisionInput(
            NOW_MS + 60_000,
            (event,),
            frozenset(),
            frozenset(),
        ),
        covering.final_state,
        config,
    )
    assert opened.opened_event_ids == (event.event_id,)
    assert opened.final_state.consumed_event_ids == (event.event_id,)


def test_unknown_engine_state_blocks_without_consuming_the_event(
    tmp_path: Path,
) -> None:
    event = _event()

    output = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), None, None),
        ExodusState(),
        _config(tmp_path),
    )

    assert output.opened_event_ids == ()
    assert output.active_records == ()
    assert output.blocked_events == ((event.event_id, "engine_account_health_unavailable"),)
    assert output.final_state.consumed_event_ids == ()


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (_event(carry_side=None, carry_qty=None, carry_avg_entry_px=None), "no_exact_carry_long"),
        (_event(carry_side="short"), "no_exact_carry_long"),
        (_event(mark_px=None), "no_exact_carry_long"),
    ],
)
def test_incomplete_handoff_is_consumed_without_inventing_size(
    tmp_path: Path, event: CarryPresettlementEvent, reason: str
) -> None:
    output = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), frozenset(), frozenset()),
        ExodusState(),
        _config(tmp_path),
    )

    assert output.active_records == ()
    assert output.blocked_events == ((event.event_id, reason),)
    assert output.final_state.consumed_event_ids == (event.event_id,)


def test_cover_state_deletes_only_after_conclusive_engine_flat(tmp_path: Path) -> None:
    config = _config(tmp_path)
    due = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=32.5,
        settlement_ts_ms=NOW_MS - 60 * 60_000,
        fired_ts_ms=NOW_MS - 2 * 60 * 60_000,
        target_qty=3.25,
    )
    prior = ExodusState((due,), ())
    unknown = decide_exodus(ExodusDecisionInput(NOW_MS, (), None, None), prior, config)
    flat = decide_exodus(ExodusDecisionInput(NOW_MS, (), frozenset(), frozenset()), prior, config)

    assert unknown.cover_records == (due,)
    assert unknown.final_state.open_records == (due,)
    assert flat.staged_state.open_records == (due,)
    assert flat.final_state.open_records == ()
    (zero,) = parse_target_book_bytes(flat.target_book_bytes).targets
    assert zero.notional_usdt == 0.0


def test_completed_entry_stays_spent_across_stop_and_engine_restart(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    event = _event()
    opened = decide_exodus(
        ExodusDecisionInput(NOW_MS, (event,), frozenset(), frozenset(), {}),
        ExodusState(),
        config,
    )
    filled_at = NOW_MS + 60_000
    filled = decide_exodus(
        ExodusDecisionInput(
            filled_at,
            (event,),
            frozenset({"AUSDT"}),
            frozenset(),
            {"AUSDT": ("short", 3.25, 10.0)},
        ),
        opened.final_state,
        config,
    )

    assert filled.entry_closed_symbols == ("AUSDT",)
    assert filled.final_state.entry_closed_ts_ms_by_symbol == (("AUSDT", filled_at),)
    (held_target,) = parse_target_book_bytes(filled.target_book_bytes).targets
    assert held_target.entry_valid_until_ms == filled_at

    restarted_state = ExodusState.from_dict(filled.final_state.to_dict())
    after_stop = decide_exodus(
        ExodusDecisionInput(
            filled_at + 60_000,
            (event,),
            frozenset(),
            frozenset(),
            {},
        ),
        restarted_state,
        config,
    )

    assert after_stop.retired_symbols == ("AUSDT",)
    assert after_stop.active_records == ()
    assert after_stop.staged_state.open_records == opened.final_state.open_records
    assert after_stop.final_state.open_records == ()
    (zero,) = parse_target_book_bytes(after_stop.target_book_bytes).targets
    assert zero.symbol == "AUSDT"
    assert zero.notional_usdt == 0.0


def test_stopped_entry_state_survives_until_zero_book_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    record = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=32.5,
        settlement_ts_ms=SETTLEMENT_MS,
        fired_ts_ms=NOW_MS,
        target_qty=3.25,
    )
    prior = ExodusState(
        open_records=(record,),
        entry_closed_ts_ms_by_symbol=(("AUSDT", NOW_MS),),
    )
    state_root = tmp_path / "state"
    save_exodus_state(state_root, prior)
    module._create_state_identity(
        state_root,
        config=config,
        genesis_source="adopted_owned",
    )

    class FlatEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: FlatEngine(),
    )
    real_publish = module.publish_target_book

    def fail_book(*_args, **_kwargs) -> None:
        raise OSError("injected zero-book failure")

    monkeypatch.setattr(module, "publish_target_book", fail_book)
    with pytest.raises(OSError, match="injected zero-book failure"):
        run_exodus_cycle(config=config, now_ms=NOW_MS + 60_000)

    assert load_exodus_state(state_root) == prior

    monkeypatch.setattr(module, "publish_target_book", real_publish)
    run_exodus_cycle(config=config, now_ms=NOW_MS + 120_000)

    assert load_exodus_state(state_root) == ExodusState()
    (zero,) = parse_target_book_bytes(config.target_book_path.read_bytes()).targets
    assert zero.symbol == "AUSDT"
    assert zero.notional_usdt == 0.0


def test_state_migrates_the_combined_producer_schema(tmp_path: Path) -> None:
    legacy = {
        "schema_version": 2,
        "open": [
            {
                "symbol": "AUSDT",
                "notional_usdt": 32.5,
                "settlement_ts_ms": SETTLEMENT_MS,
                "fired_ts_ms": NOW_MS,
                "target_qty": 3.25,
            }
        ],
    }
    path = module.exodus_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    state = load_exodus_state(tmp_path)
    assert state.consumed_event_ids == ()
    assert state.open_records[0].target_qty == 3.25
    save_exodus_state(tmp_path, state)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 4


def test_cycle_durably_stages_new_exposure_before_book_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    event = _event()
    append_carry_presettlement_event(config.event_path, event)

    class HealthyEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: HealthyEngine(),
    )
    real_publish = module.publish_target_book

    def fail_book(*_args, **_kwargs) -> None:
        raise OSError("injected book failure")

    monkeypatch.setattr(module, "publish_target_book", fail_book)
    with pytest.raises(OSError, match="injected book failure"):
        run_exodus_cycle(config=config, now_ms=NOW_MS)

    staged = load_exodus_state(tmp_path / "state")
    assert staged.open_records[0].target_qty == 3.25
    assert staged.consumed_event_ids == (event.event_id,)

    monkeypatch.setattr(module, "publish_target_book", real_publish)
    payload = run_exodus_cycle(config=config, now_ms=NOW_MS + 60_000)
    assert payload["opened"] == []
    assert parse_target_book_bytes(config.target_book_path.read_bytes()).targets[0].target_qty == -3.25


def test_cycle_archives_exact_target_bytes_before_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)

    class HealthyEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: HealthyEngine(),
    )
    payload = run_exodus_cycle(config=config, now_ms=NOW_MS)

    object_path = Path(payload["target_book_object_path"])
    assert object_path.read_bytes() == config.target_book_path.read_bytes()
    assert payload.target_book_path == object_path


def test_cycle_does_not_consume_or_open_when_engine_health_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    event = _event()
    append_carry_presettlement_event(config.event_path, event)
    state_root = tmp_path / "state"
    save_exodus_state(state_root, ExodusState())

    def no_engine(*_args, **_kwargs):
        raise OSError("no heartbeat")

    monkeypatch.setattr(module, "require_recent_engine_account", no_engine)
    payload = run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert payload["opened"] == []
    assert payload["events_consumed"] == 0
    assert payload["blocked_events"] == [f"{event.event_id}:engine_account_health_unavailable"]
    assert load_exodus_state(state_root) == ExodusState()
    assert parse_target_book_bytes(config.target_book_path.read_bytes()).targets == ()


def test_missing_owned_state_never_overwrites_an_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.target_book_path.parent.mkdir(parents=True)
    config.target_book_path.write_bytes(b"existing target bytes\n")

    class FlatEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: FlatEngine(),
    )

    with pytest.raises(RuntimeError, match="state is missing"):
        run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert config.target_book_path.read_bytes() == b"existing target bytes\n"


def test_missing_state_cannot_initialize_over_engine_exposure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)

    class ExposedEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {"AUSDT": ("short", 3.25, 10.0)}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset({"BUSDT"})

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: ExposedEngine(),
    )

    state_root = tmp_path / "state"
    with pytest.raises(RuntimeError, match="reports Exodus exposure"):
        run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert not module.exodus_state_path(state_root).exists()
    assert not config.target_book_path.exists()


def test_first_cycle_imports_the_combined_producer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    legacy = ExodusShortRecord(
        symbol="AUSDT",
        notional_usdt=32.5,
        settlement_ts_ms=SETTLEMENT_MS,
        fired_ts_ms=NOW_MS,
        target_qty=3.25,
    )
    (config.event_path.parent / "exodus_shorts.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "open": [
                    {
                        "symbol": legacy.symbol,
                        "notional_usdt": legacy.notional_usdt,
                        "settlement_ts_ms": legacy.settlement_ts_ms,
                        "fired_ts_ms": legacy.fired_ts_ms,
                        "target_qty": legacy.target_qty,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    config.target_book_path.parent.mkdir(parents=True)
    config.target_book_path.write_bytes(b"old combined-producer target\n")

    class HealthyEngine:
        strategies = frozenset({"exodus"})

        @staticmethod
        def holdings_for_strategy(_sleeve: str):
            return {"AUSDT": ("short", 3.25, 10.0)}

        @staticmethod
        def working_entries_for_strategy(_sleeve: str):
            return frozenset()

    monkeypatch.setattr(
        module,
        "require_recent_engine_account",
        lambda *_args, **_kwargs: HealthyEngine(),
    )

    payload = run_exodus_cycle(config=config, now_ms=NOW_MS)

    assert payload["state_source"].startswith("imported:")
    assert load_exodus_state(tmp_path / "state").open_records == (legacy,)
    assert parse_target_book_bytes(config.target_book_path.read_bytes()).targets[0].target_qty == -3.25
    assert (tmp_path / "state" / "exodus_state_identity.json").is_file()

    # Later owned state may have moved far beyond the retained combined file.
    # Losing it must not resurrect that stale one-time migration source.
    save_exodus_state(tmp_path / "state", ExodusState())
    module.exodus_state_path(tmp_path / "state").unlink()
    target_before = config.target_book_path.read_bytes()
    with pytest.raises(RuntimeError, match="after this state root was initialized"):
        run_exodus_cycle(config=config, now_ms=NOW_MS + 60_000)
    assert config.target_book_path.read_bytes() == target_before
