"""Gated commit fusion, speculative leverage, and the span ledger.

The boundary order path used to pay two journal commits (plan, then attempt
claim) and a serial ~190ms leverage round trip per fresh entry symbol. These
tests pin the fused single-commit path, the gates that keep crash semantics
unchanged where declared, the speculative pre-commit leverage machinery and
its authority gate, and the receipt span ledger.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_contracts import AccountEventType
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    AmbiguousSubmissionAttemptError,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
    SubmissionFusionSpec,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountIntentInbox,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.account.execution_adapters import AmbiguousExposureSubmission
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.marketdata.bybit_errors import (
    BybitRequestRejected,
    BybitSubmissionUncertain,
)
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter


NOW_NS = 1_100_000_000


def _route(tmp_path: Path) -> AccountRoute:
    return ensure_account_route(
        account_id="service-account",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _rules(*, max_order_qty: float = 100.0) -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            tick_size=0.1,
            max_order_qty=max_order_qty,
            max_leverage=20.0,
        )
    }


def _market() -> MarketInputRef:
    return MarketInputRef(
        input_key="book-1",
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=10.0,
        bid_price=9.9,
        ask_price=10.1,
        book_sequence=1,
        source="test",
    )


class _MarketProvider:
    def current(self, symbols: list[str], *, batch_id: str) -> dict[str, MarketInputRef]:
        assert batch_id
        return {symbol: _market() for symbol in symbols}


class _SnapshotProvider:
    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            equity_usdt=100.0,
            available_margin_usdt=100.0,
            snapshot_key=f"wallet:{batch_id}",
            snapshot_ts_ns=1_050_000_000,
        )


class _RulesProvider:
    def __init__(self, rules: dict[str, InstrumentRules]) -> None:
        self.rules = rules

    def current(self, symbols: list[str]) -> dict[str, InstrumentRules]:
        return {symbol: self.rules[symbol] for symbol in symbols}


class _PositionTruth:
    def require_recent_symbols_consistent(
        self, symbols: list[str], *, max_age_ns: int
    ) -> None:
        assert symbols and max_age_ns > 0


class RecordingClient:
    """Scripted Bybit private client recording call order and threads.

    ``leverage_outcomes`` and ``order_outcomes`` are consumed one per call:
    an exception instance raises, anything else succeeds.
    """

    demo = True
    realm = "demo"

    def __init__(
        self,
        clock: VirtualClock,
        *,
        leverage_outcomes: list[BaseException | None] | None = None,
        order_outcomes: list[BaseException | None] | None = None,
    ) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.leverage_threads: list[str] = []
        self.leverage_outcomes = list(leverage_outcomes or [])
        self.order_outcomes = list(order_outcomes or [])
        self.orders_placed: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def set_leverage(self, **params: Any) -> dict[str, Any]:
        with self._lock:
            self.calls.append("set_leverage")
            self.leverage_threads.append(threading.current_thread().name)
            outcome = self.leverage_outcomes.pop(0) if self.leverage_outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return {}

    def place_order(self, **params: Any) -> dict[str, Any]:
        self.clock.advance_ns(1_000_000)
        self.calls.append("place_order")
        outcome = self.order_outcomes.pop(0) if self.order_outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        self.orders_placed.append(dict(params))
        return {
            "orderId": f"venue-{len(self.orders_placed)}",
            "_response_time_ms": "1100",
        }

    def place_orders_batch(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.clock.advance_ns(1_000_000)
        self.calls.append("place_orders_batch")
        results = []
        for row in rows:
            self.orders_placed.append(dict(row))
            results.append(
                {"orderId": f"venue-{len(self.orders_placed)}", "_row_code": 0}
            )
        return results


def _service(
    tmp_path: Path,
    adapter: Any,
    *,
    clock: VirtualClock,
    max_order_qty: float = 100.0,
) -> AccountExecutionService:
    route = _route(tmp_path)
    kernel = AccountExecutionKernel(
        route.account_path,
        account_id=route.account_id,
        clock=clock,
        id_seed="fusion-test",
    )
    return AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=_MarketProvider(),
        snapshot_provider=_SnapshotProvider(),
        rules_provider=_RulesProvider(_rules(max_order_qty=max_order_qty)),
        risk_policy=AccountRiskPolicy(
            max_component_gross_notional_usdt=1_000.0,
            max_account_gross_notional_usdt=1_000.0,
            max_symbol_notional_usdt=1_000.0,
            max_initial_margin_usdt=100.0,
            max_leverage=10.0,
        ),
        execution_adapter=adapter,
        native_protection_policy=NativeDisasterProtectionPolicy(0.2),
        position_truth_provider=_PositionTruth(),
        clock=clock,
    )


def _adapter(
    client: RecordingClient,
    *,
    clock: VirtualClock,
    sole_leverage_authority: bool = True,
) -> BybitDemoExecutionAdapter:
    return BybitDemoExecutionAdapter(
        client,
        clock=clock,
        sole_leverage_authority=sole_leverage_authority,
    )


def _request(
    route: AccountRoute,
    *,
    request_id: str,
    notional: float,
    leverage: float = 10.0,
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=request_id,
        batch_id=request_id,
        created_ts_ns=NOW_NS,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(
            RequestedIntent(
                adapter_kind=SleeveAdapterKind.LONG,
                intent=SleeveTargetIntent(
                    decision_key=f"decision:{request_id}",
                    target_key="long/main/BUSDT",
                    strategy_id="long-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=notional,
                    leverage=leverage,
                    reason="test",
                ),
            ),
        ),
    )


def _submission_attempt_events(kernel: AccountExecutionKernel) -> list[Any]:
    return [
        event
        for event in kernel.journal.events()
        if event.event_type == AccountEventType.SUBMISSION_ATTEMPT.value
    ]


def _count_separate_claims(
    monkeypatch: pytest.MonkeyPatch, kernel: AccountExecutionKernel
) -> list[dict[str, Any]]:
    """Count every separate (post-plan) claim transaction on this kernel."""

    recorded: list[dict[str, Any]] = []
    original = kernel.record_submission_attempt_batch

    def counting(**kwargs: Any) -> Any:
        recorded.append(dict(kwargs))
        return original(**kwargs)

    monkeypatch.setattr(kernel, "record_submission_attempt_batch", counting)
    return recorded


def _fill_open_position(service: AccountExecutionService, *, qty: float = 2.0) -> None:
    """Drive the acked entry to a filled position, kernel-level."""

    state = service.kernel._state_ref()
    command_id = next(
        command_id
        for command_id, order in state.orders.items()
        if order.status == "acknowledged"
    )
    service.kernel.record_fill(
        command_id=command_id,
        execution_id=f"fill:{command_id}",
        signed_qty=qty,
        price=10.0,
        fee_usdt=0.0,
        exchange_ts_ns=NOW_NS,
        local_receive_ts_ns=NOW_NS,
    )
    service.kernel.record_order_status(
        command_id=command_id,
        status="filled",
        cumulative_filled_qty=abs(qty),
        exchange_ts_ns=NOW_NS + 1,
        local_receive_ts_ns=NOW_NS + 1,
    )


# ---------------------------------------------------------------------------
# B1: gated commit fusion
# ---------------------------------------------------------------------------


def test_entry_batch_fuses_claim_into_plan_commit_when_leverage_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sole authority + speculative leverage: one commit claims plan AND attempt.

    Fails without the fix: the driver always ran ``record_submission_attempt``
    as a second transaction, so the separate-claim count below was 1 and no
    SUBMISSION_ATTEMPT carried ``fused_with_plan_commit``.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="fused-entry-1", notional=20.0))
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert separate_claims == []
    attempts = _submission_attempt_events(service.kernel)
    assert len(attempts) == 1
    payload = attempts[0].payload
    assert payload["fused_with_plan_commit"] is True
    assert payload["attempt"] == 1
    assert payload["adapter_name"] == "bybit_demo"
    # The speculative leverage call ran on a worker thread, before the create.
    assert client.calls == ["set_leverage", "place_order"]
    assert client.leverage_threads[0].startswith("account-leverage-speculate")
    command_id = receipt.command_ids[0]
    assert service.kernel.state().orders[command_id].submission_attempts == 1
    assert service.kernel.state().orders[command_id].status == "acknowledged"


def test_reduce_only_batch_fuses_even_under_shared_leverage_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every-command-reduce-only batches fuse always; no leverage involved."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock, sole_leverage_authority=False)
    service = _service(tmp_path, adapter, clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="seed-entry", notional=20.0))
    assert service.run_once(inbox) is not None
    _fill_open_position(service)

    inbox.submit(_request(service.route, request_id="fused-close-1", notional=0.0))
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)
    calls_before_close = len(client.calls)
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert separate_claims == []
    close_attempts = [
        event
        for event in _submission_attempt_events(service.kernel)
        if event.correlation_id == "fused-close-1"
    ]
    assert len(close_attempts) == 1
    assert close_attempts[0].payload["fused_with_plan_commit"] is True
    # A reduce-only command never negotiates leverage.
    assert client.calls[calls_before_close:] == ["place_order"]


def test_entry_batch_keeps_two_commits_under_shared_leverage_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared authority: no speculation, leverage after the plan commit, and
    the attempt claim stays its own transaction — today's exact sequence."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock, sole_leverage_authority=False)
    service = _service(tmp_path, adapter, clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="unfused-entry-1", notional=20.0))
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert len(separate_claims) == 1
    attempts = _submission_attempt_events(service.kernel)
    assert len(attempts) == 1
    assert "fused_with_plan_commit" not in attempts[0].payload
    # Leverage negotiated inline (owner thread), never speculatively.
    assert client.calls == ["set_leverage", "place_order"]
    assert all(name == threading.main_thread().name for name in client.leverage_threads)


def test_fused_entry_crash_before_wire_refuses_resend_on_replay(
    tmp_path: Path,
) -> None:
    """The declared trade: a fused claim with no wire wedges on replay.

    The replay pass sees ``submission_attempts == 1`` with an empty fused set
    (``result.events`` is empty on the idempotent-replay path), so the
    ambiguous-resend refusal fires and nothing is resent — zero double-send.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(
        clock, order_outcomes=[RuntimeError("power loss before wire outcome")]
    )
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="fused-crash-1", notional=20.0))

    with pytest.raises(RuntimeError, match="power loss"):
        service.run_once(inbox)
    command_id = next(iter(service.kernel.state().orders))
    assert service.kernel.state().orders[command_id].submission_attempts == 1
    assert client.calls.count("place_order") == 1

    with pytest.raises(AmbiguousExposureSubmission, match="refusing to resend"):
        service.run_once(inbox)
    assert client.calls.count("place_order") == 1
    assert service.kernel.state().orders[command_id].submission_attempts == 1


def test_fused_reduce_only_crash_still_retries_like_today(tmp_path: Path) -> None:
    """Reduce-only crash semantics are literally unchanged: replay re-claims
    with ``allow_repeat`` and resends."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="seed-entry", notional=20.0))
    assert service.run_once(inbox) is not None
    _fill_open_position(service)

    client.order_outcomes = [RuntimeError("power loss before wire outcome")]
    inbox.submit(_request(service.route, request_id="crash-close-1", notional=0.0))
    with pytest.raises(RuntimeError, match="power loss"):
        service.run_once(inbox)
    close_command_id = next(
        command_id
        for command_id, order in service.kernel.state().orders.items()
        if order.batch_id == "crash-close-1"
    )
    assert service.kernel.state().orders[close_command_id].submission_attempts == 1

    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.accepted
    closed = service.kernel.state().orders[close_command_id]
    assert closed.submission_attempts == 2
    assert closed.status == "acknowledged"


def test_single_winner_claim_holds_inside_the_fused_transaction(
    tmp_path: Path,
) -> None:
    """A fused claim owns the attempt: a later separate claim must refuse."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(
        clock, order_outcomes=[RuntimeError("wire outcome never observed")]
    )
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="fused-winner-1", notional=20.0))
    with pytest.raises(RuntimeError, match="wire outcome never observed"):
        service.run_once(inbox)
    command_id = next(iter(service.kernel.state().orders))
    assert service.kernel.state().orders[command_id].submission_attempts == 1

    with pytest.raises(AmbiguousSubmissionAttemptError, match="already has a durable"):
        service.kernel.record_submission_attempt(
            command_id=command_id,
            adapter_name="late-claimer",
            allow_repeat=False,
        )


def test_chunked_fused_batch_pays_one_claimless_batch_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fused multi-command batch still takes the batched venue request and
    pays no separate claim transaction at all."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(
        tmp_path, _adapter(client, clock=clock), clock=clock, max_order_qty=1.0
    )
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="fused-chunks-1", notional=20.0))
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert len(receipt.command_ids) == 2
    assert separate_claims == []
    assert client.calls == ["set_leverage", "place_orders_batch"]
    attempts = _submission_attempt_events(service.kernel)
    assert len(attempts) == 2
    assert all(event.payload["fused_with_plan_commit"] is True for event in attempts)
    for command_id in receipt.command_ids:
        assert service.kernel.state().orders[command_id].status == "acknowledged"


# ---------------------------------------------------------------------------
# B2: speculative pre-commit leverage
# ---------------------------------------------------------------------------


def test_uncertain_speculative_leverage_retries_before_any_claim(
    tmp_path: Path,
) -> None:
    """Fused-path sibling of the pinned single-path test: an uncertain
    leverage answer propagates BEFORE any claim exists, and the retry pass
    completes the order."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(
        clock,
        leverage_outcomes=[BybitSubmissionUncertain("leverage response lost")],
    )
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="spec-uncertain-1", notional=20.0))

    with pytest.raises(BybitSubmissionUncertain, match="leverage response lost"):
        service.run_once(inbox)
    command_id = next(iter(service.kernel.state().orders))
    after_loss = service.kernel.state().orders[command_id]
    assert after_loss.status == "commanded"
    assert after_loss.submission_attempts == 0
    assert client.calls == ["set_leverage"]
    assert _submission_attempt_events(service.kernel) == []

    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.accepted
    submitted = service.kernel.state().orders[command_id]
    assert submitted.status == "acknowledged"
    assert submitted.submission_attempts == 1
    assert client.calls == ["set_leverage", "set_leverage", "place_order"]


def test_definite_speculative_leverage_reject_acks_without_second_call(
    tmp_path: Path,
) -> None:
    """A definite reject replays as the pre-create unaccepted ACK, without a
    second leverage round trip and without any create."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(
        clock,
        leverage_outcomes=[BybitRequestRejected("leverage refused for symbol")],
    )
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="spec-reject-1", notional=20.0))

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert client.calls == ["set_leverage"]
    order = service.kernel.state().orders[receipt.command_ids[0]]
    assert order.status == "rejected"
    assert order.rejection_key.endswith("set_leverage_failed")
    assert order.submission_attempts == 0


def test_shared_authority_never_calls_leverage_for_a_risk_rejected_batch(
    tmp_path: Path,
) -> None:
    """The safety property the gate exists for: under shared authority (the
    owner hand-sets leverage) a batch the RISK_DECISION rejects makes NO
    leverage call, so a hand-set value on a flat symbol survives."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock, sole_leverage_authority=False)
    service = _service(tmp_path, adapter, clock=clock)
    inbox = AccountIntentInbox(service.route)
    # Leverage 50 exceeds both the policy cap (10) and the venue cap (20).
    inbox.submit(
        _request(service.route, request_id="risk-reject-1", notional=20.0, leverage=50.0)
    )

    receipt = service.run_once(inbox)

    assert receipt is not None and not receipt.accepted
    assert client.calls == []


def test_speculation_workers_never_write_the_cache_directly(
    tmp_path: Path,
) -> None:
    """Worker outcomes reach ``_venue_leverage`` only through the join, on the
    owner thread."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock)
    fired = threading.Event()
    release = threading.Event()

    original = client.set_leverage

    def gated(**params: Any) -> dict[str, Any]:
        fired.set()
        assert release.wait(timeout=5.0)
        return original(**params)

    client.set_leverage = gated  # type: ignore[method-assign]
    join = adapter.begin_speculative_leverage([("BUSDT", 10.0)])
    assert join is not None
    assert fired.wait(timeout=5.0)
    # The worker has completed its venue call or is about to; either way the
    # cache stays untouched until the owner-thread join applies results.
    assert adapter.confirmed_venue_leverage() == {}
    release.set()
    join()
    assert adapter.confirmed_venue_leverage() == {"BUSDT": 10.0}


# ---------------------------------------------------------------------------
# B4: span ledger
# ---------------------------------------------------------------------------


def test_receipt_spans_are_monotonic_and_complete(tmp_path: Path) -> None:
    """A full submit round self-reports every milestone, in path order.

    Uses the unfused (shared-authority) path, whose chain is the brief's:
    inbox_claimed -> plan_committed -> leverage_done -> attempt_claimed ->
    barrier_done -> wire_sent -> ack_received.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock, sole_leverage_authority=False)
    service = _service(tmp_path, adapter, clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="spans-1", notional=20.0))

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    ordered = [
        "inbox_claimed_ts_ns",
        "plan_committed_ts_ns",
        "leverage_done_ts_ns",
        "attempt_claimed_ts_ns",
        "barrier_done_ts_ns",
        "wire_sent_ts_ns",
        "ack_received_ts_ns",
    ]
    assert all(name in receipt.spans for name in ordered), receipt.spans
    values = [receipt.spans[name] for name in ordered]
    assert values == sorted(values)
    assert all(value > 0 for value in values)
    assert receipt.spans["barrier_wait_ns"] >= 0
    # The completed/ receipt on disk carries the same spans, as integers.
    completed = next((inbox.root / "completed").glob("*.json"))
    stored = json.loads(completed.read_bytes())["receipt"]["spans"]
    assert stored == dict(receipt.spans)


def test_ack_metadata_carries_barrier_wait_and_socket_send(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="ack-meta-1", notional=20.0))
    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.accepted

    acks = [
        event
        for event in service.kernel.journal.events()
        if event.event_type == AccountEventType.ACK.value
    ]
    assert len(acks) == 1
    metadata = acks[0].payload["metadata"]
    assert metadata["barrier_wait_ns"] >= 0
    assert metadata["local_socket_send_ts_ns"] > 0


def test_submission_attempt_payload_carries_span_siblings(
    tmp_path: Path,
) -> None:
    """Both claim shapes journal the sibling keys, replay-safely."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    fused_client = RecordingClient(clock)
    fused_service = _service(
        tmp_path / "fused", _adapter(fused_client, clock=clock), clock=clock
    )
    fused_inbox = AccountIntentInbox(fused_service.route)
    fused_inbox.submit(
        _request(fused_service.route, request_id="span-fused-1", notional=20.0)
    )
    assert fused_service.run_once(fused_inbox) is not None
    fused_payload = _submission_attempt_events(fused_service.kernel)[0].payload
    assert fused_payload["claimed_ts_ns"] == fused_payload["local_start_ts_ns"]
    assert fused_payload["plan_committed_ts_ns"] == fused_payload["claimed_ts_ns"]
    assert fused_payload["leverage_wait_ns"] >= 0

    separate_clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    separate_client = RecordingClient(separate_clock)
    separate_adapter = _adapter(
        separate_client, clock=separate_clock, sole_leverage_authority=False
    )
    separate_service = _service(
        tmp_path / "separate", separate_adapter, clock=separate_clock
    )
    separate_inbox = AccountIntentInbox(separate_service.route)
    separate_inbox.submit(
        _request(separate_service.route, request_id="span-separate-1", notional=20.0)
    )
    assert separate_service.run_once(separate_inbox) is not None
    separate_payload = _submission_attempt_events(separate_service.kernel)[0].payload
    assert separate_payload["claimed_ts_ns"] == separate_payload["local_start_ts_ns"]
    assert separate_payload["plan_committed_ts_ns"] > 0
    assert (
        separate_payload["plan_committed_ts_ns"] <= separate_payload["claimed_ts_ns"]
    )


def test_replay_reproduces_identical_target_event_content(tmp_path: Path) -> None:
    """Spans and timings never enter TARGET payloads: a replay of the same
    request appends nothing and every TARGET event is byte-identical."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    request = _request(service.route, request_id="replay-targets-1", notional=20.0)
    inbox.submit(request)
    first = service.run_once(inbox)
    assert first is not None and first.accepted

    events_before = [event.to_dict() for event in service.kernel.journal.events()]
    targets_before = [
        row for row in events_before if row["event_type"] == AccountEventType.TARGET.value
    ]
    assert targets_before
    for row in targets_before:
        assert "claimed_ts_ns" not in row["payload"]
        assert "plan_committed_ts_ns" not in row["payload"]
        assert "leverage_wait_ns" not in row["payload"]

    clock.advance_ns(1_000_000)
    replay = service.handle(request)
    assert replay.accepted
    assert replay.execution_event_ids == first.execution_event_ids
    events_after = [event.to_dict() for event in service.kernel.journal.events()]
    assert events_after == events_before


# ---------------------------------------------------------------------------
# B5: no full-journal read on the request path
# ---------------------------------------------------------------------------


def test_fresh_request_path_never_copies_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``handle`` used to call ``journal.events()`` — a full history copy —
    per completed request. Fails without the fix: the count below was 1."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="no-scan-1", notional=20.0))

    full_reads: list[int] = []
    original_events = service.kernel.journal.events

    def counting_events() -> Any:
        full_reads.append(1)
        return original_events()

    monkeypatch.setattr(service.kernel.journal, "events", counting_events)
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert receipt.execution_event_ids  # the ACK id still reaches the receipt
    assert full_reads == []


def test_an_armed_spec_for_another_batch_is_discarded_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec armed for batch A must never fuse batch B's claims.

    The review probe showed an exception between arming and the armed batch's
    own submit (a market-input gap, a zero reference price) left the spec
    armed, and the convergence retry inside the same failed pass consumed it.
    Fails without the fix: ``SubmissionFusionSpec`` had no ``batch_id`` and
    ``submit_targets`` fused whatever batch came next.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    service = _service(tmp_path, _adapter(client, clock=clock), clock=clock)
    inbox = AccountIntentInbox(service.route)
    inbox.submit(_request(service.route, request_id="innocent-entry-1", notional=20.0))
    # Arm as if a previous batch raised between arming and its submit.
    service.kernel.arm_submission_fusion(
        SubmissionFusionSpec(
            adapter_name="bybit_demo",
            batch_id="the-batch-that-raised",
            leverage_satisfied={"BUSDT": 10.0},
        )
    )
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    attempts = [
        event
        for event in _submission_attempt_events(service.kernel)
        if event.correlation_id == "innocent-entry-1"
    ]
    assert len(attempts) == 1
    # run_once re-armed for its own batch, so the innocent batch fuses on its
    # OWN spec (no separate claim); the stale spec must be gone, not merely
    # shadowed.
    assert separate_claims == []
    assert service.kernel._armed_submission_fusion is None
    kernel_only = AccountExecutionKernel(
        tmp_path / "solo", account_id="grouped-exit-account", clock=clock
    )
    kernel_only.arm_submission_fusion(
        SubmissionFusionSpec(
            adapter_name="bybit_demo",
            batch_id="armed-for-someone-else",
            leverage_satisfied={"BUSDT": 10.0},
        )
    )
    result = kernel_only.submit_targets(
        batch_id="a-different-batch",
        market_inputs=[
            MarketInputRef(
                "book:a-different-batch:BUSDT",
                "BUSDT",
                clock.wall_time_ns() - 2,
                clock.wall_time_ns() - 1,
                10.0,
            )
        ],
        targets=[
            DesiredTarget(
                decision_key="decision:a-different-batch",
                target_key="long/main/BUSDT",
                sleeve="long",
                strategy_id="long-v1",
                component_id="main",
                symbol="BUSDT",
                signed_qty=2.0,
                reference_price=10.0,
                leverage=10.0,
                reason="test",
                metadata={"signal_ts_ms": 123},
            )
        ],
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=1_000.0,
            available_margin_usdt=900.0,
            snapshot_key="wallet:a-different-batch",
            snapshot_ts_ns=clock.wall_time_ns(),
        ),
        risk_policy=AccountRiskPolicy(
            max_component_gross_notional_usdt=1_000.0,
            max_account_gross_notional_usdt=1_000.0,
            max_symbol_notional_usdt=1_000.0,
            max_initial_margin_usdt=100.0,
            max_leverage=10.0,
        ),
        instrument_rules=_rules(),
    )
    assert result.accepted
    # The mismatched spec was discarded: nothing fused, no attempt claimed.
    assert _submission_attempt_events(kernel_only) == []


def test_arming_requires_the_batch_id(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path / "kernel", account_id="grouped-exit-account", clock=clock
    )
    with pytest.raises(ValueError, match="batch_id"):
        kernel.arm_submission_fusion(
            SubmissionFusionSpec(adapter_name="bybit_demo", batch_id="  ")
        )


def test_speculative_outcomes_clear_even_when_the_next_round_has_no_plan(
    tmp_path: Path,
) -> None:
    """A stale outcome must not survive a cache-satisfied later round.

    Fails without the fix: ``begin_speculative_leverage`` returned early on an
    empty plan BEFORE clearing, so a reject stored for a risk-rejected batch
    answered a later batch's prepare for the same symbol within the TTL.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    adapter = _adapter(RecordingClient(clock), clock=clock)
    adapter._speculative_leverage_outcomes["XUSDT"] = (
        "rejected",
        3.0,
        RuntimeError("stale answer from a dead round"),
        clock.wall_time_ns() + 10_000_000_000,
    )

    assert adapter.begin_speculative_leverage([]) is None

    assert adapter._speculative_leverage_outcomes == {}


def test_confirmed_leverage_is_empty_under_shared_authority(tmp_path: Path) -> None:
    """The fusion map is sole-authority only; the cache may be warm on mainnet.

    Fails without the fix: a warm cache (populated by an earlier batch's
    prepare step) fused funded-account entries, quietly extending the fused
    crash-window trade to mainnet.
    """

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    shared = _adapter(RecordingClient(clock), clock=clock, sole_leverage_authority=False)
    shared._venue_leverage["BUSDT"] = 10.0
    assert shared.confirmed_venue_leverage() == {}
    sole = _adapter(RecordingClient(clock), clock=clock, sole_leverage_authority=True)
    sole._venue_leverage["BUSDT"] = 10.0
    assert sole.confirmed_venue_leverage() == {"BUSDT": 10.0}


def test_warm_cache_entry_stays_two_commits_under_shared_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second entry on the same symbol, shared authority, cache now warm —
    the claim must STILL be its own transaction (mainnet order flow unchanged
    except reduce-only)."""

    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    client = RecordingClient(clock)
    adapter = _adapter(client, clock=clock, sole_leverage_authority=False)
    service = _service(tmp_path, adapter, clock=clock)
    inbox = AccountIntentInbox(service.route)
    # The exact reviewed attack shape: the cache is already warm for the
    # symbol (an earlier batch's prepare step, or the reconciler's retain,
    # put it there) when a fresh entry arrives on the shared-authority
    # account.
    adapter._venue_leverage["BUSDT"] = 10.0
    inbox.submit(_request(service.route, request_id="warm-cache-entry", notional=20.0))
    separate_claims = _count_separate_claims(monkeypatch, service.kernel)
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert len(separate_claims) == 1
    attempts = [
        event
        for event in _submission_attempt_events(service.kernel)
        if event.correlation_id == "warm-cache-entry"
    ]
    assert len(attempts) == 1
    assert "fused_with_plan_commit" not in attempts[0].payload
