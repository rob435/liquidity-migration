from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AccountState,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account_service import AccountIntentInbox
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.account_strategy_state import (
    canonical_component_execution_anchors,
    canonical_strategy_trade_rows,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.execution_adapters import (
    BookLevel,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.protection_engine import AccountProtectionEngine, _protection_trigger_reason
from liquidity_migration.strategy_runtime import (
    AccountKernelRuntime,
    AdaptedIntent,
    LongTargetAdapter,
    SleeveTargetIntent,
)


RULES = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0, tick_size=0.1)}
POLICY = AccountRiskPolicy(1_000.0, 1_000.0, 1_000.0, 100.0, 10.0)


@pytest.mark.parametrize(
    ("qty", "mark", "stop", "take_profit", "expected"),
    [
        (1.0, 8.9, 9.0, 12.0, "stop_loss"),
        (1.0, 12.1, 9.0, 12.0, "take_profit"),
        (-1.0, 11.1, 11.0, 8.0, "stop_loss"),
        (-1.0, 7.9, 11.0, 8.0, "take_profit"),
        (1.0, 10.0, 9.0, 12.0, ""),
    ],
)
def test_protection_trigger_direction(
    qty: float,
    mark: float,
    stop: float,
    take_profit: float,
    expected: str,
) -> None:
    assert _protection_trigger_reason(
        signed_qty=qty,
        mark_price=mark,
        stop_price=stop,
        take_profit_price=take_profit,
    ) == expected


def _market(*, key: str, price: float, ts: int) -> MarketInputRef:
    return MarketInputRef(key, "BUSDT", ts - 10, ts, price)


def _twin(*, bid: float, ask: float, ts: int) -> MarketOrderExecutionTwin:
    return MarketOrderExecutionTwin(
        books={"BUSDT": L2BookSnapshot(
            symbol="BUSDT",
            sequence=ts,
            previous_sequence=ts - 1,
            exchange_ts_ns=ts - 10,
            local_receive_ts_ns=ts,
            bids=(BookLevel(bid, 100.0),),
            asks=(BookLevel(ask, 100.0),),
        )},
        instrument_rules=RULES,
        config=ExecutionTwinConfig(5.5, LatencyProfile(1, 1, 1), 100),
    )


def test_component_protection_refuses_target_without_confirmed_fill(
    tmp_path: Path,
) -> None:
    route = ensure_account_route(
        account_id="protection",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id)
    runtime = AccountKernelRuntime(kernel)
    market = _market(key="decision-book", price=10.0, ts=1_000)
    result = runtime.process_cycle(
        batch_id="entry-pending",
        intents=[
            AdaptedIntent(
                LongTargetAdapter(),
                SleeveTargetIntent(
                    decision_key="entry-pending",
                    target_key="long/main/BUSDT",
                    strategy_id="long-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=20.0,
                    leverage=10.0,
                    reason="entry",
                    metadata={"take_profit_pct": 0.20},
                ),
            )
        ],
        market_inputs={"BUSDT": market},
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-1", 990),
        risk_policy=POLICY,
        instrument_rules=RULES,
        execution_adapter=None,
    )
    assert result.target_result.accepted
    assert result.target_result.commands
    assert "BUSDT" not in kernel.state().positions

    engine = AccountProtectionEngine(
        kernel=kernel,
        inbox=AccountIntentInbox(route),
        instrument_rules=RULES,
    )
    assert engine.evaluate({"BUSDT": _market(key="crossed", price=100.0, ts=2_000)}) == ()


def test_component_take_profit_emits_zero_target_and_never_direct_order(tmp_path: Path) -> None:
    route = ensure_account_route(
        account_id="protection",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    clock = VirtualClock(current_wall_ns=1_000, current_monotonic_ns=0)
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id, clock=clock)
    runtime = AccountKernelRuntime(kernel)
    entry_market = _market(key="entry-book", price=10.0, ts=1_000)
    runtime.process_cycle(
        batch_id="entry",
        intents=[AdaptedIntent(LongTargetAdapter(), SleeveTargetIntent(
            decision_key="entry-d",
            target_key="long/main/BUSDT",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_notional_usdt=20.0,
            leverage=10.0,
            reason="entry",
            metadata={"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
        ))],
        market_inputs={"BUSDT": entry_market},
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-1", 990),
        risk_policy=POLICY,
        instrument_rules=RULES,
        execution_adapter=_twin(bid=9.9, ask=10.1, ts=1_000),
    )
    assert kernel.state().positions["BUSDT"].signed_qty == 2.0

    inbox = AccountIntentInbox(route)
    engine = AccountProtectionEngine(
        kernel=kernel,
        inbox=inbox,
        instrument_rules=RULES,
    )
    # The execution twin filled at 10.1, not the 10.0 decision reference.
    # A 20% fill-anchored TP is 12.12 and rounds outward to the 12.2 tick.
    assert engine.evaluate({"BUSDT": _market(key="too-early", price=12.1, ts=1_900)}) == ()
    account_events = tuple(kernel.journal.events())
    verified_anchors = {
        anchor.target_key: anchor
        for anchor in canonical_component_execution_anchors(
            kernel.journal.root,
            account_events=account_events,
        )
    }
    with pytest.raises(ValueError, match="account event snapshot"):
        engine.evaluate(
            {"BUSDT": _market(key="cached-without-events", price=12.1, ts=1_950)},
            verified_execution_anchors=verified_anchors,
        )
    with pytest.raises(RuntimeError, match="does not match"):
        engine.evaluate(
            {"BUSDT": _market(key="stale-state", price=12.1, ts=1_975)},
            account_events=account_events,
            verified_execution_anchors=verified_anchors,
            trusted_account_state=AccountState(),
        )
    trigger_market = _market(key="tp-book", price=12.2, ts=2_000)
    requests = engine.evaluate(
        {"BUSDT": trigger_market},
        account_events=account_events,
        verified_execution_anchors=verified_anchors,
        trusted_account_state=kernel._state_ref(),
    )
    assert len(requests) == 1
    request = requests[0]
    intent = request.intents[0]
    assert request.created_ts_ns == trigger_market.local_receive_ts_ns
    assert intent.intent.signed_notional_usdt == 0.0
    assert intent.intent.reason == "take_profit"
    assert intent.intent.metadata["entry_fill_vwap"] == 10.1
    assert intent.intent.metadata["take_profit_price"] == 12.2
    assert intent.intent.metadata["trigger_exchange_ts_ns"] == trigger_market.exchange_ts_ns
    assert (
        intent.intent.metadata["trigger_local_receive_ts_ns"]
        == trigger_market.local_receive_ts_ns
    )
    assert kernel.state().protections[request.request_id]["status"] == "triggered"

    # Restart/evaluate before processing does not enqueue or notify again.
    assert (
        AccountProtectionEngine(
            kernel=kernel,
            inbox=inbox,
            instrument_rules=RULES,
        ).evaluate({"BUSDT": trigger_market})
        == ()
    )

    claimed = inbox.claim_next()
    assert claimed is not None
    _, durable_request = claimed
    clock.advance_to_wall_ns(2_000)
    output = runtime.process_cycle(
        batch_id=durable_request.batch_id,
        intents=[AdaptedIntent(item.adapter(), item.intent) for item in durable_request.intents],
        market_inputs={"BUSDT": trigger_market},
        risk_snapshot=AccountRiskSnapshot(102.0, 102.0, "wallet-2", 1_990),
        risk_policy=POLICY,
        instrument_rules=RULES,
        execution_adapter=_twin(bid=12.0, ask=12.2, ts=2_000),
    )
    assert output.target_result.commands[0].reduce_only
    assert kernel.state().positions["BUSDT"].signed_qty == 0.0

    projected = canonical_strategy_trade_rows(
        tmp_path / "account",
        sleeve="long",
        strategy_ids=("long-v1",),
    )
    assert projected.select(
        "trade_id",
        "strategy_id",
        "status",
        "exit_reason",
        "account_target_status",
    ).to_dicts() == [{
        "trade_id": "main",
        "strategy_id": "long-v1",
        "status": "closed",
        "exit_reason": "take_profit",
        "account_target_status": "converged",
    }]
    accepted_target = kernel.state().component_target_desires["long/main/BUSDT"]
    assert accepted_target["strategy_id"] == "long-v1"
    assert accepted_target["metadata"]["requested_by_strategy_id"] == "account-protection"
    assert accepted_target["metadata"]["requested_by"] == "account_risk"


def test_terminal_partial_entry_keeps_component_protection(tmp_path: Path) -> None:
    """A partially-filled-then-cancelled entry holds a live position; it must
    not lose stop/TP evaluation forever (no working remainder can trade after
    a terminal status, so the anti-race rationale no longer applies)."""

    route = ensure_account_route(
        account_id="protection",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    clock = VirtualClock(current_wall_ns=1_000, current_monotonic_ns=0)
    kernel = AccountExecutionKernel(route.account_path, account_id=route.account_id, clock=clock)
    runtime = AccountKernelRuntime(kernel)
    entry_market = _market(key="entry-book", price=10.0, ts=1_000)
    result = runtime.process_cycle(
        batch_id="entry-partial-terminal",
        intents=[AdaptedIntent(LongTargetAdapter(), SleeveTargetIntent(
            decision_key="entry-d",
            target_key="long/main/BUSDT",
            strategy_id="long-v1",
            component_id="main",
            symbol="BUSDT",
            signed_notional_usdt=20.0,
            leverage=10.0,
            reason="entry",
            metadata={"stop_loss_pct": 0.10, "take_profit_pct": 0.20},
        ))],
        market_inputs={"BUSDT": entry_market},
        risk_snapshot=AccountRiskSnapshot(100.0, 100.0, "wallet-1", 990),
        risk_policy=POLICY,
        instrument_rules=RULES,
        execution_adapter=None,
    )
    command = result.target_result.commands[0]
    kernel.record_ack(
        command_id=command.command_id,
        accepted=True,
        venue_order_id="venue-partial-terminal",
        exchange_ts_ns=1_100,
        local_ack_ts_ns=1_150,
    )
    kernel.record_fill(
        command_id=command.command_id,
        execution_id="partial-then-cancelled",
        signed_qty=command.signed_qty / 2.0,
        price=10.0,
        fee_usdt=0.0,
        exchange_ts_ns=1_200,
        local_receive_ts_ns=1_250,
        metadata={"terminal": True},
    )
    order = kernel.state().orders[command.command_id]
    assert order.status == "partially_filled_cancelled"
    position_qty = kernel.state().positions["BUSDT"].signed_qty
    assert position_qty > 0.0

    engine = AccountProtectionEngine(
        kernel=kernel,
        inbox=AccountIntentInbox(route),
        instrument_rules=RULES,
    )
    requests = engine.evaluate({"BUSDT": _market(key="tp-book", price=12.1, ts=2_000)})
    assert len(requests) == 1
    intent = requests[0].intents[0]
    assert intent.intent.signed_notional_usdt == 0.0
    assert intent.intent.reason == "take_profit"


def test_present_but_invalid_protection_fraction_fails_closed() -> None:
    from liquidity_migration.protection_engine import _optional_fraction

    assert _optional_fraction(None) is None
    assert _optional_fraction("") is None
    assert _optional_fraction(0.12) == 0.12
    for invalid in (8, 1.0, 0.0, -0.1, "8", float("nan"), True, object()):
        with pytest.raises(ValueError):
            _optional_fraction(invalid)
