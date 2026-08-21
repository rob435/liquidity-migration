"""Exit publication: per-exit independence by default, grouping opt-in.

Exits publish one request each so one symbol whose market data or rules went
missing wedges only its own request (review probes 2026-08-13 showed a
grouped request fails owner admission as a unit in that state, and the
loss-guard flatten latch then never republishes). ``group_exits=True`` opts
into ONE all-flat request for callers valuing the single owner pass; a
grouped publish that fails BEFORE landing falls back to per-exit requests,
and one that fails AFTER landing is reported as published with no fallback.
This file proves the default, both fallback edges, and — end to end through
``AccountKernel.submit_targets`` — that an all-flat batch is admitted and
yields reduce-only order commands, unaffected by a rejected entry batch.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_intent_client import (
    AccountTargetPublisher,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
)
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    TargetBatchResult,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import AccountIntentInbox, SleeveAdapterKind
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.strategy_event_clock import StrategyEvent
from liquidity_migration.strategy.strategy_target_replay import (
    PublishedTargetCyclePayload,
    capture_event_from_cycle,
)


def _route(tmp_path: Path) -> AccountRoute:
    return ensure_account_route(
        account_id="grouped-exit-account",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _carry_exit(symbol: str):
    target_key = component_target_key(
        sleeve=SleeveAdapterKind.CARRY,
        strategy_id="carry_hold",
        component_id="carry_hold",
        symbol=symbol,
    )
    return requested_target(
        adapter_kind=SleeveAdapterKind.CARRY,
        decision_key=f"carry-target/cycle/exit/{symbol}",
        target_key=target_key,
        strategy_id="carry_hold",
        component_id="carry_hold",
        symbol=symbol,
        signed_notional_usdt=0.0,
        leverage=2.0,
        reason="carry exit: funding normalized",
    )


def _carry_entry(symbol: str, *, notional: float = 50.0):
    target_key = component_target_key(
        sleeve=SleeveAdapterKind.CARRY,
        strategy_id="carry_hold",
        component_id="carry_hold",
        symbol=symbol,
    )
    return requested_target(
        adapter_kind=SleeveAdapterKind.CARRY,
        decision_key=f"carry-target/cycle/entry/{symbol}",
        target_key=target_key,
        strategy_id="carry_hold",
        component_id="carry_hold",
        symbol=symbol,
        signed_notional_usdt=notional,
        leverage=2.0,
        reason="carry entry: deep settled print",
    )


def test_a_malformed_exit_falls_back_and_publishes_the_healthy_ones(tmp_path: Path) -> None:
    """A grouped request the schema refuses must not strand the valid exits.

    Two exits for the SAME component make the grouped request malformed (a
    request cannot replace one component twice); the fallback publishes each
    exit as its own request, so both reach the inbox and no error remains.
    """

    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    duplicate_a = _carry_exit("AUSDT")
    duplicate_b = dataclasses.replace(
        duplicate_a,
        intent=dataclasses.replace(duplicate_a.intent, reason="carry exit: velocity exit"),
    )

    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="carry/cycle-malformed",
        exit_intents=(duplicate_a, duplicate_b),
        entry_intents=(),
        created_ts_ns=1_700_000_000_000_000_000,
    )

    assert result.exit_publication_mode == "independent"
    assert result.errors == ()
    assert [item.request.intents for item in result.exit_requests] == [
        (duplicate_a,),
        (duplicate_b,),
    ]
    assert len({item.request.request_id for item in result.exit_requests}) == 2
    assert len(AccountIntentInbox(route).unresolved_requests()) == 2


def test_grouped_exit_publication_is_captured_exit_first(tmp_path: Path) -> None:
    """The capture path re-reads the grouped exit byte-identically and keeps
    exits ahead of entries in publication order."""

    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route, clock=VirtualClock(current_wall_ns=1_000_000_000))
    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix="carry-target/capture-cycle",
        exit_intents=(_carry_exit("AUSDT"), _carry_exit("BUSDT")),
        entry_intents=(_carry_entry("CUSDT"),),
        created_ts_ns=1_000_000_000,
        group_exits=True,
    )
    assert publication.exit_publication_mode == "grouped"
    payload = PublishedTargetCyclePayload(
        {"cycle_id": "capture", "mode": "demo_target"},
        publication=publication,
        route=route,
    )
    event = StrategyEvent(
        event_ts_ns=1_000_000_000,
        ingest_ts_ns=1_000_000_010,
        source="carry:demo",
        source_sequence=1,
        kind="market_boundary",
        payload={"execution_environment": "demo", "strategy_profile": "carry_hold_v4_live_v1"},
    )

    capture = capture_event_from_cycle(event, payload, sleeve="carry")

    assert [row.stage for row in capture.requests] == ["exit", "entry"]
    assert len(capture.requests[0].request.intents) == 2
    assert capture.requests[0].arrival_sequence < capture.requests[1].arrival_sequence
    assert capture.decision_keys == (
        "carry-target/cycle/entry/CUSDT",
        "carry-target/cycle/exit/AUSDT",
        "carry-target/cycle/exit/BUSDT",
    )


# ---------------------------------------------------------------------------
# Kernel admission: the grouped all-flat batch the producer now publishes.
# ---------------------------------------------------------------------------

_SYMBOLS = ("AUSDT", "BUSDT")
_PRICE = 10.0


def _kernel(tmp_path: Path) -> AccountExecutionKernel:
    clock = VirtualClock(current_wall_ns=1_000_000_000, current_monotonic_ns=1)
    return AccountExecutionKernel(tmp_path / "kernel-account", account_id="demo", clock=clock)


def _submit_carry_batch(
    kernel: AccountExecutionKernel,
    *,
    batch: str,
    quantities: dict[str, float],
    reason: str,
) -> TargetBatchResult:
    return kernel.submit_targets(
        batch_id=batch,
        market_inputs=[
            MarketInputRef(
                f"book:{batch}:{symbol}",
                symbol,
                kernel.clock.wall_time_ns() - 2,
                kernel.clock.wall_time_ns() - 1,
                _PRICE,
            )
            for symbol in sorted(quantities)
        ],
        targets=[
            DesiredTarget(
                decision_key=f"decision:{batch}:{symbol}",
                target_key=f"carry/carry_hold/carry_hold/{symbol}",
                sleeve="carry",
                strategy_id="carry_hold",
                component_id="carry_hold",
                symbol=symbol,
                signed_qty=qty,
                reference_price=_PRICE,
                leverage=2.0,
                reason=reason,
                metadata={"signal_ts_ms": 123, "stop_price": 6.5},
            )
            for symbol, qty in sorted(quantities.items())
        ],
        risk_snapshot=AccountRiskSnapshot(
            10_000.0,
            9_000.0,
            f"wallet:{batch}",
            kernel.clock.wall_time_ns(),
        ),
        risk_policy=AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 10.0),
        instrument_rules={
            symbol: InstrumentRules(symbol, 0.1, 0.1, 1.0) for symbol in quantities
        },
    )


def _fill_all(kernel: AccountExecutionKernel, result: TargetBatchResult) -> None:
    for index, command in enumerate(result.commands):
        base_ts = 1_000_100_000 + index * 10_000
        kernel.record_ack(
            command_id=command.command_id,
            accepted=True,
            venue_order_id=f"venue:{command.command_id}",
            exchange_ts_ns=base_ts,
            local_ack_ts_ns=base_ts + 1,
        )
        kernel.record_fill(
            command_id=command.command_id,
            execution_id=f"fill:{command.command_id}",
            signed_qty=command.signed_qty,
            price=_PRICE,
            fee_usdt=0.01,
            exchange_ts_ns=base_ts + 2,
            local_receive_ts_ns=base_ts + 3,
            metadata={
                "fee_observed": True,
                "fee_status": "observed_execution_fee",
                "fee_source": "test",
            },
        )


def test_an_all_flat_grouped_batch_is_admitted_and_commands_are_reduce_only(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    opened = _submit_carry_batch(
        kernel,
        batch="carry/cycle-1/entry/1000",
        quantities={symbol: 2.0 for symbol in _SYMBOLS},
        reason="entry",
    )
    assert opened.accepted
    assert len(opened.commands) == len(_SYMBOLS)
    _fill_all(kernel, opened)

    flat = _submit_carry_batch(
        kernel,
        batch="carry/cycle-2/exit/2000",
        quantities={symbol: 0.0 for symbol in _SYMBOLS},
        reason="carry exit: funding normalized",
    )

    assert flat.accepted
    assert flat.rejection_keys == ()
    assert sorted(command.symbol for command in flat.commands) == sorted(_SYMBOLS)
    assert all(command.reduce_only for command in flat.commands)
    assert all(command.signed_qty == -2.0 for command in flat.commands)
    assert all(command.target_signed_qty == 0.0 for command in flat.commands)


def test_a_rejected_entry_batch_leaves_the_exit_batch_untouched(tmp_path: Path) -> None:
    """Exit and entry ride separate batch ids, so a rejected entry batch
    cannot pull the already-admitted all-flat exit batch down with it."""

    kernel = _kernel(tmp_path)
    opened = _submit_carry_batch(
        kernel,
        batch="carry/cycle-1/entry/1000",
        quantities={symbol: 2.0 for symbol in _SYMBOLS},
        reason="entry",
    )
    _fill_all(kernel, opened)
    exit_batch = "carry/cycle-2/exit/2000"
    flat = _submit_carry_batch(
        kernel,
        batch=exit_batch,
        quantities={symbol: 0.0 for symbol in _SYMBOLS},
        reason="carry exit: funding normalized",
    )
    assert flat.accepted

    entry_batch = "carry/cycle-2/entry/2000"
    # 200 x $10 = $2000 breaches every $1000 envelope limit in the policy.
    rejected = _submit_carry_batch(
        kernel,
        batch=entry_batch,
        quantities={"CUSDT": 200.0},
        reason="entry",
    )

    assert rejected.batch_id == entry_batch
    assert not rejected.accepted
    assert rejected.rejection_keys
    assert rejected.commands == ()
    state = kernel.state()
    assert state.risk_decisions[exit_batch]["accepted"] is True
    assert state.risk_decisions[entry_batch]["accepted"] is False
    # The admitted exit desire stands: both components still target zero.
    for symbol in _SYMBOLS:
        desire = state.component_target_desires[f"carry/carry_hold/carry_hold/{symbol}"]
        assert desire["signed_qty"] == 0.0


def test_exits_default_to_one_request_each(tmp_path: Path) -> None:
    """Per-exit independence is the default; grouping is opt-in.

    Review probes (2026-08-13) showed a grouped all-flat request fails owner
    admission as a unit when one symbol's market data is missing, and on the
    loss-guard flatten the republish latch then never fires. Fails without
    the fix: the grouped mode was the default.
    """

    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="carry/default-mode",
        exit_intents=(_carry_exit("AUSDT"), _carry_exit("BUSDT")),
        entry_intents=(),
        created_ts_ns=1_700_000_000_000_000_000,
    )
    assert result.exit_publication_mode == "independent"
    assert result.errors == ()
    assert len(result.exit_requests) == 2
    assert all(len(item.request.intents) == 1 for item in result.exit_requests)


def test_a_post_durability_failure_never_republishes_the_landed_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grouped submit that raises AFTER its request landed must not fall back.

    Fails without the fix: the fallback re-queued every exit as singles next
    to the durably-landed group — every exit twice, and the group absent from
    the publication (so absent from capture). Probe: an injected failure at
    the post-replace directory sync, the exact site the review demonstrated.
    """

    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    real_submit = AccountIntentInbox.submit

    def submit_then_die(self: AccountIntentInbox, request: Any) -> Any:
        real_submit(self, request)
        raise OSError("injected: directory sync failed after the rename")

    monkeypatch.setattr(AccountIntentInbox, "submit", submit_then_die)
    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="carry/post-durable",
        exit_intents=(_carry_exit("AUSDT"), _carry_exit("BUSDT")),
        entry_intents=(),
        created_ts_ns=1_700_000_000_000_000_000,
        group_exits=True,
    )
    monkeypatch.undo()

    assert result.exit_publication_mode == "grouped"
    assert "after the request landed" in result.exit_publication_note
    assert result.errors == ()
    assert len(result.exit_requests) == 1
    assert len(result.exit_requests[0].request.intents) == 2
    pending = list((Path(route.inbox_root) / "pending").glob("*.json"))
    assert len(pending) == 1
    assert pending[0] == result.exit_requests[0].path


def test_a_pre_durability_failure_still_falls_back_to_singles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the grouped request never landed, per-exit fallback still runs."""

    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    real_submit = AccountIntentInbox.submit
    calls = {"n": 0}

    def die_first_submit(self: AccountIntentInbox, request: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected: transport failed before anything landed")
        return real_submit(self, request)

    monkeypatch.setattr(AccountIntentInbox, "submit", die_first_submit)
    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="carry/pre-durable",
        exit_intents=(_carry_exit("AUSDT"), _carry_exit("BUSDT")),
        entry_intents=(),
        created_ts_ns=1_700_000_000_000_000_000,
        group_exits=True,
    )
    monkeypatch.undo()

    assert result.exit_publication_mode == "independent"
    assert result.exit_publication_note == ""
    assert result.errors == ()
    assert len(result.exit_requests) == 2
    assert all(len(item.request.intents) == 1 for item in result.exit_requests)
