from __future__ import annotations

from dataclasses import replace

import pytest

from liquidity_migration.account.account_contracts import InstrumentRules, MarketInputRef, OrderCommand
from liquidity_migration.account.account_contracts import AccountState, OrderState
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import (
    BookLevel,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.runtime.passive_execution import (
    EXECUTION_ARM_METADATA_KEY,
    PASSIVE_MAKER_FEE_BPS,
    PASSIVE_TIMEOUT_NS,
    PassivePaperExecutionAdapter,
    execution_arm_for_component,
    recover_orphaned_working_orders,
)

_SYMBOL = "XUSDT"


def _arm_component(arm: str) -> str:
    """Find a deterministic component id hashing to the requested arm."""
    for index in range(64):
        candidate = f"trade-{index}"
        if execution_arm_for_component(candidate) == arm:
            return candidate
    raise AssertionError("no component found for arm " + arm)


def _book(*, bid: float, ask: float, sequence: int = 5, ts_ns: int = 1_000, qty: float = 100.0) -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol=_SYMBOL,
        sequence=sequence,
        previous_sequence=sequence - 1,
        exchange_ts_ns=ts_ns,
        local_receive_ts_ns=ts_ns,
        bids=(BookLevel(bid, qty), BookLevel(bid * 0.999, qty)),
        asks=(BookLevel(ask, qty), BookLevel(ask * 1.001, qty)),
    )


class _FakeRecorder:
    def __init__(self) -> None:
        self.books: dict[str, L2BookSnapshot | None] = {}

    def current_book(self, symbol: str, *, depth: int | None = None) -> L2BookSnapshot | None:
        return self.books.get(symbol)


class _FakeProvider:
    def __init__(self) -> None:
        self.recorder = _FakeRecorder()
        self.contexts: dict[str, L2BookSnapshot] = {}

    def execution_book(self, input_key: str) -> L2BookSnapshot:
        return self.contexts[input_key]


def _world(
    component_id: str,
    *,
    signed_qty: float = -10.0,
    reduce_only_max_decision_age_ns: int | None = None,
):
    clock = VirtualClock(current_wall_ns=10_000, current_monotonic_ns=10_000)
    provider = _FakeProvider()
    decision_book = _book(bid=99.0, ask=101.0, ts_ns=900)
    provider.contexts["ctx-1"] = decision_book
    twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules={_SYMBOL: InstrumentRules(_SYMBOL, 0.1, 0.1, 1.0)},
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(0, 0, 0, 0, 0, 0),
            max_decision_age_ns=250_000_000,
            residual_adverse_slippage_bps=2.0,
        ),
        name="paper_test",
        id_seed="paper-test",
    )
    adapter = PassivePaperExecutionAdapter(
        market_provider=provider,
        twin=twin,
        component_resolver=lambda command: (component_id, "continuous"),
        clock=clock,
        reduce_only_max_decision_age_ns=reduce_only_max_decision_age_ns,
    )
    command = OrderCommand(
        command_id="c1",
        batch_id="b1",
        symbol=_SYMBOL,
        side="sell" if signed_qty < 0 else "buy",
        qty=abs(signed_qty),
        signed_qty=signed_qty,
        reduce_only=False,
        reference_price=100.0,
        target_signed_qty=signed_qty,
        chunk_index=1,
        chunk_count=1,
        created_ts_ns=950,
    )
    market_input = MarketInputRef(
        input_key="ctx-1",
        symbol=_SYMBOL,
        exchange_ts_ns=900,
        local_receive_ts_ns=900,
        reference_price=100.0,
    )
    return clock, provider, adapter, command, market_input


def test_arm_assignment_is_deterministic() -> None:
    component = _arm_component("B")
    assert execution_arm_for_component(component) == "B"
    assert execution_arm_for_component(component) == "B"
    assert execution_arm_for_component(_arm_component("A")) == "A"


def test_arm_a_delegates_to_twin_with_metadata() -> None:
    _clock, _provider, adapter, command, market_input = _world(_arm_component("A"))
    observations = list(adapter.submit(command, market_input))
    assert adapter.pending_count() == 0
    kinds = [ExecutionObservationType(o.observation_type) for o in observations]
    assert ExecutionObservationType.ACK in kinds
    assert ExecutionObservationType.FILL in kinds
    assert all(o.metadata[EXECUTION_ARM_METADATA_KEY] == "A" for o in observations)


def test_ineligible_reduce_only_takes_arm_a_even_with_b_hash() -> None:
    _clock, _provider, adapter, command, market_input = _world(_arm_component("B"))
    reduce_command = OrderCommand(
        command_id="c1",
        batch_id="b1",
        symbol=_SYMBOL,
        side="sell",
        qty=10.0,
        signed_qty=-10.0,
        reduce_only=True,
        reference_price=100.0,
        target_signed_qty=0.0,
        chunk_index=1,
        chunk_count=1,
        created_ts_ns=950,
    )
    observations = list(adapter.submit(reduce_command, market_input))
    assert adapter.pending_count() == 0
    assert all(o.metadata[EXECUTION_ARM_METADATA_KEY] == "A" for o in observations)
    assert all(o.metadata["execution_arm_eligible"] is False for o in observations)


def test_reduce_only_uses_owner_freshness_without_relaxing_entry_limit() -> None:
    _clock, _provider, adapter, command, market_input = _world(
        _arm_component("A"),
        reduce_only_max_decision_age_ns=5_000_000_000,
    )
    over_entry_limit_ts_ns = market_input.local_receive_ts_ns + 300_000_000
    stale_entry = replace(
        command,
        command_id="stale-entry",
        created_ts_ns=over_entry_limit_ts_ns,
    )
    entry_observations = tuple(adapter.submit(stale_entry, market_input))
    assert len(entry_observations) == 1
    assert entry_observations[0].accepted is False
    assert entry_observations[0].metadata["reason"] == "stale_decision"
    assert entry_observations[0].metadata["decision_book_age_limit_ns"] == 250_000_000
    assert entry_observations[0].metadata["decision_book_age_limit_overridden"] is False
    with pytest.raises(ValueError, match="only for reduce-only"):
        tuple(
            adapter.twin.submit(
                stale_entry,
                market_input,
                decision_age_limit_ns=5_000_000_000,
                decision_age_limit_source="invalid_entry_relaxation",
            )
        )

    reduction = replace(
        command,
        command_id="fresh-enough-reduction",
        side="buy",
        signed_qty=10.0,
        reduce_only=True,
        target_signed_qty=0.0,
        created_ts_ns=over_entry_limit_ts_ns,
    )
    reduction_observations = tuple(adapter.submit(reduction, market_input))
    reduction_ack = reduction_observations[0]
    assert reduction_ack.accepted is True
    assert any(
        observation.observation_type == ExecutionObservationType.FILL
        for observation in reduction_observations
    )
    assert reduction_ack.metadata["decision_book_age_ns"] == 300_000_000
    assert reduction_ack.metadata["decision_book_age_limit_ns"] == 5_000_000_000
    assert (
        reduction_ack.metadata["decision_book_age_limit_source"]
        == "paper_owner_market_freshness"
    )
    assert reduction_ack.metadata["decision_book_age_limit_overridden"] is True
    assert all(
        observation.metadata["decision_book_age_limit_ns"] == 5_000_000_000
        for observation in reduction_observations
    )

    too_old_reduction = replace(
        reduction,
        command_id="too-old-reduction",
        created_ts_ns=market_input.local_receive_ts_ns + 5_000_000_001,
    )
    too_old_observations = tuple(adapter.submit(too_old_reduction, market_input))
    assert len(too_old_observations) == 1
    assert too_old_observations[0].accepted is False
    assert too_old_observations[0].metadata["reason"] == "stale_decision"
    assert too_old_observations[0].metadata["decision_book_age_limit_ns"] == 5_000_000_000


def test_arm_b_sell_rests_at_ask_with_ack_only() -> None:
    _clock, _provider, adapter, command, market_input = _world(_arm_component("B"))
    observations = list(adapter.submit(command, market_input))
    assert len(observations) == 1
    ack = observations[0]
    assert ExecutionObservationType(ack.observation_type) is ExecutionObservationType.ACK
    assert ack.accepted is True
    assert ack.metadata[EXECUTION_ARM_METADATA_KEY] == "B"
    assert ack.metadata["passive_limit_price"] == pytest.approx(101.0)
    assert adapter.pending_count() == 1


def test_passive_fill_on_bid_crossing_the_limit() -> None:
    clock, provider, adapter, command, market_input = _world(_arm_component("B"))
    adapter.submit(command, market_input)
    provider.recorder.books[_SYMBOL] = _book(bid=101.0, ask=101.5, sequence=7, ts_ns=2_000)
    observations = adapter.poll(now_ns=clock.wall_time_ns() + 1_000)
    assert len(observations) == 1
    fill = observations[0]
    assert ExecutionObservationType(fill.observation_type) is ExecutionObservationType.FILL
    assert fill.price == pytest.approx(101.0)
    assert fill.signed_qty == pytest.approx(-10.0)
    assert fill.fee_usdt == pytest.approx(10.0 * 101.0 * PASSIVE_MAKER_FEE_BPS / 10_000.0)
    assert fill.metadata["passive_fill"] is True
    assert adapter.pending_count() == 0


def test_repeg_follows_the_touch_then_fills_at_new_limit() -> None:
    clock, provider, adapter, command, market_input = _world(_arm_component("B"))
    adapter.submit(command, market_input)
    # ask improves upward (bid stays inside the chase bound): re-peg, no fill
    provider.recorder.books[_SYMBOL] = _book(bid=100.95, ask=101.4, sequence=7, ts_ns=2_000)
    assert adapter.poll(now_ns=clock.wall_time_ns() + 1_000) == ()
    assert adapter.pending_count() == 1
    # bid then crosses the re-pegged limit
    provider.recorder.books[_SYMBOL] = _book(bid=101.4, ask=101.6, sequence=8, ts_ns=3_000)
    observations = adapter.poll(now_ns=clock.wall_time_ns() + 2_000)
    assert len(observations) == 1
    fill = observations[0]
    assert fill.price == pytest.approx(101.4)
    assert fill.metadata["passive_repeg_count"] == 1


def test_timeout_falls_back_to_market_walk_with_taker_costs() -> None:
    clock, provider, adapter, command, market_input = _world(_arm_component("B"))
    adapter.submit(command, market_input)
    provider.recorder.books[_SYMBOL] = _book(bid=100.5, ask=101.2, sequence=7, ts_ns=2_000)
    observations = adapter.poll(now_ns=clock.wall_time_ns() + PASSIVE_TIMEOUT_NS + 1)
    assert adapter.pending_count() == 0
    fills = [o for o in observations if ExecutionObservationType(o.observation_type) is ExecutionObservationType.FILL]
    assert fills
    first = fills[0]
    # sell fallback walks the bids with the twin's residual slippage and taker fee
    assert first.price == pytest.approx(100.5 * (1.0 - 2.0 / 10_000.0))
    assert first.metadata["passive_fallback_reason"] == "timeout"
    assert first.metadata["taker_fee_bps"] == pytest.approx(5.5)


def test_adverse_move_through_limit_triggers_chase_before_timeout() -> None:
    clock, provider, adapter, command, market_input = _world(_arm_component("B"))
    adapter.submit(command, market_input)
    # initial limit 101.0; bid collapsing 10+ bps below it forces the chase
    chased_bid = 101.0 * (1.0 - 11.0 / 10_000.0)
    provider.recorder.books[_SYMBOL] = _book(bid=chased_bid, ask=101.05, sequence=7, ts_ns=2_000)
    observations = adapter.poll(now_ns=clock.wall_time_ns() + 1_000)
    fills = [o for o in observations if ExecutionObservationType(o.observation_type) is ExecutionObservationType.FILL]
    assert fills
    assert fills[0].metadata["passive_fallback_reason"] == "chase"


def test_deadline_without_live_book_cancels_cleanly() -> None:
    clock, provider, adapter, command, market_input = _world(_arm_component("B"))
    adapter.submit(command, market_input)
    provider.recorder.books[_SYMBOL] = None
    observations = adapter.poll(now_ns=clock.wall_time_ns() + PASSIVE_TIMEOUT_NS + 1)
    assert len(observations) == 1
    status = observations[0]
    assert ExecutionObservationType(status.observation_type) is ExecutionObservationType.ORDER_STATUS
    assert status.status == "cancelled"
    assert adapter.pending_count() == 0


def test_restart_recovery_cancels_acknowledged_working_orders() -> None:
    state = AccountState()
    state.orders["c-open"] = OrderState(
        command_id="c-open",
        batch_id="b1",
        symbol=_SYMBOL,
        signed_qty=-10.0,
        reduce_only=False,
        status="acknowledged",
    )
    state.orders["c-done"] = OrderState(
        command_id="c-done",
        batch_id="b1",
        symbol=_SYMBOL,
        signed_qty=-10.0,
        reduce_only=False,
        status="filled",
        filled_signed_qty=-10.0,
    )
    state.working_order_ids.add("c-open")
    observations = recover_orphaned_working_orders(state, now_ns=5_000)
    assert len(observations) == 1
    assert observations[0].command_id == "c-open"
    assert observations[0].status == "cancelled"
