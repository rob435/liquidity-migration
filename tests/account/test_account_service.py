from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    InstrumentRules,
    MarketInputRef,
    NativeDisasterProtectionPolicy,
)
from liquidity_migration.venue.account_reconcile import BybitAccountReconciler
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountIntentInbox,
    AccountServiceReceipt,
    AccountTargetRequest,
    CompletedRequestCursor,
    RequestedIntent,
    SleeveAdapterKind,
    StaleEntryRequestExpired,
)
from liquidity_migration.account.execution_adapters import StaleUnsubmittedExposureCommand
from liquidity_migration.account.account_intent_client import completed_expired_entry_attempt_keys
from liquidity_migration.strategy.account_strategy_state import (
    canonical_strategy_trade_rows,
    target_reservation_rows,
)
from liquidity_migration.strategy.strategy_planning import suppress_target_intents
from liquidity_migration.marketdata.bybit_errors import BybitSubmissionUncertain
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.account.execution_adapters import (
    AmbiguousExposureSubmission,
    BookLevel,
    ExecutionObservation,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.account.market_capture import MarketCaptureError
from liquidity_migration.venue.protection_engine import AccountProtectionEngine
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent
from liquidity_migration.account.wedged_command_watch import DEFAULT_WEDGE_AFTER_NS
from liquidity_migration.venue.venue_protection import (
    NativeProtectionBreach,
    NativeProtectionPlan,
)


NOW_NS = 1_100_000_000


def _route(tmp_path: Path) -> AccountRoute:
    return ensure_account_route(
        account_id="service-account",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _inbox(tmp_path: Path) -> AccountIntentInbox:
    return AccountIntentInbox(_route(tmp_path))


def _rules() -> dict[str, InstrumentRules]:
    return {
        "BUSDT": InstrumentRules(
            symbol="BUSDT",
            qty_step=0.1,
            min_qty=0.1,
            min_notional=1.0,
            tick_size=0.1,
            max_order_qty=100.0,
            max_leverage=20.0,
        )
    }


def _market(*, key: str = "book-1", local_ns: int = 1_000_000_000) -> MarketInputRef:
    return MarketInputRef(
        input_key=key,
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=local_ns,
        reference_price=10.0,
        bid_price=9.9,
        ask_price=10.1,
        book_sequence=1,
        source="test",
    )


class MarketProvider:
    def __init__(self, market: MarketInputRef | None = None) -> None:
        self.market = market or _market()

    def current(self, symbols: list[str], *, batch_id: str) -> dict[str, MarketInputRef]:
        assert batch_id
        return {symbol: self.market for symbol in symbols}


class MissingCapturedMarketProvider:
    def current(self, symbols: list[str], *, batch_id: str) -> dict[str, MarketInputRef]:
        assert symbols and batch_id
        raise MarketCaptureError("no reconstructed book for BUSDT")


class SnapshotProvider:
    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        return AccountRiskSnapshot(
            equity_usdt=100.0,
            available_margin_usdt=100.0,
            snapshot_key=f"wallet:{batch_id}",
            snapshot_ts_ns=1_050_000_000,
        )


class RulesProvider:
    def current(self, symbols: list[str]) -> dict[str, InstrumentRules]:
        return {symbol: _rules()[symbol] for symbol in symbols}


class ConsistentPositionTruthProvider:
    def require_recent_symbols_consistent(
        self,
        symbols: list[str],
        *,
        max_age_ns: int,
    ) -> None:
        assert symbols
        assert max_age_ns > 0


class CountingTwin:
    name = "counting_twin"

    def __init__(self) -> None:
        book = L2BookSnapshot(
            symbol="BUSDT",
            sequence=1,
            previous_sequence=0,
            exchange_ts_ns=900_000_000,
            local_receive_ts_ns=1_000_000_000,
            bids=(BookLevel(9.9, 100.0),),
            asks=(BookLevel(10.1, 100.0),),
        )
        self.inner = MarketOrderExecutionTwin(
            books={"BUSDT": book},
            instrument_rules=_rules(),
            config=ExecutionTwinConfig(
                fee_bps=5.5,
                latency=LatencyProfile(1, 1, 1),
                max_decision_age_ns=1_000_000_000,
            ),
        )
        self.submit_calls = 0

    def submit(self, command: object, market_input: MarketInputRef):
        self.submit_calls += 1
        return self.inner.submit(command, market_input)


class ScriptedExecutionAdapter:
    """Small deterministic venue script for owner convergence failures."""

    name = "scripted_execution"

    def __init__(self, *outcomes: str, partial_qty: float = 0.6) -> None:
        self.outcomes = list(outcomes)
        self.partial_qty = partial_qty
        self.submissions: list[Any] = []

    @property
    def submit_calls(self) -> int:
        return len(self.submissions)

    def submit(self, command: Any, market_input: MarketInputRef):
        self.submissions.append(command)
        if not self.outcomes:
            raise AssertionError(f"unexpected execution submit {command.command_id}")
        outcome = self.outcomes.pop(0)
        if outcome == "crash":
            raise RuntimeError("simulated crash after command commit")

        ordinal = len(self.submissions)
        exchange_ns = market_input.exchange_ts_ns + ordinal * 100
        local_ns = market_input.local_receive_ts_ns + ordinal * 100
        if outcome == "reject":
            return (
                ExecutionObservation(
                    observation_type=ExecutionObservationType.ACK,
                    command_id=command.command_id,
                    exchange_ts_ns=exchange_ns,
                    local_receive_ts_ns=local_ns,
                    accepted=False,
                    rejection_key=f"execution:{command.command_id}:definite_reject",
                ),
            )

        ack = ExecutionObservation(
            observation_type=ExecutionObservationType.ACK,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ns,
            local_receive_ts_ns=local_ns,
            accepted=True,
            venue_order_id=f"venue:{command.command_id}",
        )
        if outcome == "working":
            return (ack,)
        if outcome == "cancel":
            return (
                ack,
                ExecutionObservation(
                    observation_type=ExecutionObservationType.ORDER_STATUS,
                    command_id=command.command_id,
                    exchange_ts_ns=exchange_ns + 1,
                    local_receive_ts_ns=local_ns + 1,
                    status="cancelled",
                    cumulative_filled_qty=0.0,
                ),
            )

        if outcome == "partial_cancel":
            filled_qty = min(abs(command.signed_qty), self.partial_qty)
        elif outcome == "fill":
            filled_qty = abs(command.signed_qty)
        else:
            raise AssertionError(f"unknown scripted execution outcome {outcome!r}")
        signed_fill = filled_qty if command.signed_qty > 0.0 else -filled_qty
        fill = ExecutionObservation(
            observation_type=ExecutionObservationType.FILL,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ns + 1,
            local_receive_ts_ns=local_ns + 1,
            venue_order_id=f"venue:{command.command_id}",
            execution_id=f"fill:{command.command_id}",
            signed_qty=signed_fill,
            price=market_input.reference_price,
            fee_usdt=0.0,
        )
        status = ExecutionObservation(
            observation_type=ExecutionObservationType.ORDER_STATUS,
            command_id=command.command_id,
            exchange_ts_ns=exchange_ns + 2,
            local_receive_ts_ns=local_ns + 2,
            status="filled" if outcome == "fill" else "partially_filled_cancelled",
            cumulative_filled_qty=filled_qty,
        )
        return ack, fill, status


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=1_000.0,
        max_account_gross_notional_usdt=1_000.0,
        max_symbol_notional_usdt=1_000.0,
        max_initial_margin_usdt=100.0,
        max_leverage=10.0,
    )


def _request(
    route: AccountRoute,
    *,
    request_id: str,
    batch_id: str,
    kind: SleeveAdapterKind,
    notional: float,
    created_ts_ns: int = NOW_NS,
    target_key: str | None = None,
    decision_key: str | None = None,
    metadata: dict[str, object] | None = None,
    reason: str = "test",
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=request_id,
        batch_id=batch_id,
        created_ts_ns=created_ts_ns,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(
            RequestedIntent(
                adapter_kind=kind,
                intent=SleeveTargetIntent(
                    decision_key=decision_key or f"decision:{batch_id}",
                    target_key=target_key or f"{kind.value}/main/BUSDT",
                    strategy_id=f"{kind.value}-v1",
                    component_id="main",
                    symbol="BUSDT",
                    signed_notional_usdt=notional,
                    leverage=10.0,
                    reason=reason,
                    metadata=dict(metadata or {}),
                ),
            ),
        ),
    )


def _native_breach_safety_request(
    route: AccountRoute,
    *,
    target_key: str = "risk/main/BUSDT",
    plan_key: str = "native:BUSDT:test",
    breached_signed_qty: float = -2.0,
    native_stop_price: float = 10.7,
    authenticated_breach_mark: float = 11.2,
) -> AccountTargetRequest:
    material = {
        "symbol": "BUSDT",
        "protection_plan_key": plan_key,
        "targets": [
            {
                "target_key": target_key,
                "source_request_id": "",
                "source_revision_ns": NOW_NS,
            }
        ],
    }
    suffix = hashlib.sha256(canonical_json(material)).hexdigest()[:20]
    request_id = f"protection:native-breach:BUSDT:{suffix}"
    return _request(
        route,
        request_id=request_id,
        batch_id=request_id,
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        target_key=target_key,
        reason="native_disaster_stop_breached",
        metadata={
            "authenticated_breach_mark": authenticated_breach_mark,
            "breach_observed_ts_ns": NOW_NS,
            "breached_signed_qty": breached_signed_qty,
            "native_stop_price": native_stop_price,
            "native_protection_plan_key": plan_key,
            "requested_by_strategy_id": "account-protection",
            "source_request_id": "",
            "source_revision_ns": NOW_NS,
        },
    )


def _service(
    root: Path,
    adapter: Any,
    *,
    market: MarketInputRef | None = None,
    max_market_age_ns: int = 5_000_000_000,
    clock: VirtualClock | None = None,
    convergence_retry_backoff_ns: int = 1_000_000_000,
    convergence_retry_backoff_cap_ns: int = 30_000_000_000,
    convergence_health_grace_ns: int = 30_000_000_000,
    max_convergence_retries: int = 3,
    resting_entry_quotes=None,
    new_risk_halt=None,
) -> AccountExecutionService:
    route = _route(root.parent)
    clock = clock or VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        route.account_path, account_id=route.account_id, clock=clock, id_seed="service-test"
    )
    return AccountExecutionService(
        route=route,
        kernel=kernel,
        market_provider=MarketProvider(market),
        snapshot_provider=SnapshotProvider(),
        rules_provider=RulesProvider(),
        risk_policy=_policy(),
        execution_adapter=adapter,
        native_protection_policy=(
            NativeDisasterProtectionPolicy(0.2) if str(getattr(adapter, "name", "")) == "bybit_demo" else None
        ),
        position_truth_provider=ConsistentPositionTruthProvider(),
        clock=clock,
        max_market_age_ns=max_market_age_ns,
        convergence_retry_backoff_ns=convergence_retry_backoff_ns,
        convergence_retry_backoff_cap_ns=convergence_retry_backoff_cap_ns,
        convergence_health_grace_ns=convergence_health_grace_ns,
        max_convergence_retries=max_convergence_retries,
        resting_entry_quotes=resting_entry_quotes,
        new_risk_halt=new_risk_halt,
    )


def test_durable_service_is_single_venue_owner_across_sequential_sleeve_requests(tmp_path: Path) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="continuous-1",
            batch_id="continuous-1",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    first = service.run_once(inbox)
    assert first is not None and first.accepted
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-2.0)

    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="long-1",
            batch_id="long-1",
            kind=SleeveAdapterKind.LONG,
            notional=10.0,
        )
    )
    second = service.run_once(inbox)
    assert second is not None and second.accepted
    # Existing -2 continuous plus +1 long = one net -1 venue position.
    assert service.kernel.state().aggregate_targets["BUSDT"] == pytest.approx(-1.0)
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-1.0)
    assert adapter.submit_calls == 2


def test_privileged_inbox_writes_hand_inodes_to_the_directory_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root mirror publishing into the paper user's inbox must not leave
    # root-owned 0600 files: on 2026-08-01 that poisoned the arrival sequence
    # and the paper owner crash-looped on it for two days.
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    inbox.submit(
        _request(
            route,
            request_id="ownership-seed",
            batch_id="ownership-seed",
            kind=SleeveAdapterKind.LONG,
            notional=10.0,
        )
    )

    recorded: list[tuple[int, int]] = []
    real_fchown = os.fchown

    def _recording_fchown(fd: int, uid: int, gid: int) -> None:
        recorded.append((uid, gid))
        real_fchown(fd, uid, gid)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", _recording_fchown)
    inbox.submit(
        _request(
            route,
            request_id="ownership-probe",
            batch_id="ownership-probe",
            kind=SleeveAdapterKind.LONG,
            notional=10.0,
        )
    )
    owner = os.stat(tmp_path / "inbox")
    # Arrival counter, arrival sidecar, and the request body all hand off.
    assert len(recorded) == 3
    assert set(recorded) == {(owner.st_uid, owner.st_gid)}


def test_component_exit_stages_opposing_aggregate_to_flat_before_convergence(
    tmp_path: Path,
) -> None:
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    adapter = ScriptedExecutionAdapter("fill", "fill", "fill", "fill")
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=1,
    )
    route = _route(tmp_path)

    short = service.handle(
        _request(
            route,
            request_id="short-open",
            batch_id="short-open",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-50.0,
        )
    )
    long = service.handle(
        _request(
            route,
            request_id="long-open",
            batch_id="long-open",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert short.accepted and long.accepted
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-3.0)

    close_request = _request(
        route,
        request_id="short-close",
        batch_id="short-close",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=0.0,
        created_ts_ns=NOW_NS + 1,
    )
    close = service.handle(close_request)
    assert close.accepted
    staged = adapter.submissions[-1]
    assert staged.signed_qty == pytest.approx(3.0)
    assert staged.target_signed_qty == 0.0
    assert staged.reduce_only
    state = service.kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    assert state.aggregate_targets["BUSDT"] == pytest.approx(2.0)
    assert set(state.component_targets) == {"long/main/BUSDT"}
    risk = state.risk_decisions["short-close"]
    assert risk["staged_component_flat_symbols"] == ["BUSDT"]
    assert risk["staged_sign_flip_symbols"] == ["BUSDT"]

    submissions_before_replay = adapter.submit_calls
    assert service.handle(close_request) == close
    assert adapter.submit_calls == submissions_before_replay

    clock.advance_ns(1)
    converged = service.converge_once()
    assert converged is not None and converged.accepted
    reopened = adapter.submissions[-1]
    assert reopened.signed_qty == pytest.approx(2.0)
    assert not reopened.reduce_only
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert service.convergence_report().converged


def test_crash_after_kernel_execution_replays_request_without_resubmitting_filled_command(tmp_path: Path) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    request = _request(
        _route(tmp_path),
        request_id="crash-1",
        batch_id="crash-1",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
    )
    inbox.submit(request)
    arrival_path = inbox.root / "arrival" / inbox._filename(request.request_id)
    arrival_before = json.loads(arrival_path.read_bytes())
    assert arrival_before["arrival_sequence"] == 1
    claimed = inbox.claim_next()
    assert claimed is not None
    processing_path, claimed_request = claimed
    before_crash = service.handle(claimed_request)
    assert processing_path.exists()  # simulate process death before receipt/queue completion
    assert adapter.submit_calls == 1

    assert inbox.recover_processing() == 1
    after_restart = service.run_once(inbox)
    assert after_restart == before_crash
    assert adapter.submit_calls == 1
    assert len(list((inbox.root / "completed").glob("*.json"))) == 1
    assert json.loads(arrival_path.read_bytes()) == arrival_before
    assert inbox.submit(request).parent.name == "completed"
    assert json.loads(arrival_path.read_bytes()) == arrival_before


def test_lost_submit_response_reconciles_before_request_replay(tmp_path: Path) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    request = _request(
        _route(tmp_path),
        request_id="lost-response-1",
        batch_id="lost-response-1",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    inbox.submit(request)

    class LostResponseClient:
        demo = True
        realm = "demo"

        def __init__(self) -> None:
            self.command_id = ""
            self.submit_calls = 0
            self.last_place_params: dict[str, object] = {}

        def set_leverage(self, **_params: object) -> dict[str, object]:
            return {}

        def place_order(self, **params: object) -> dict[str, str]:
            self.submit_calls += 1
            self.command_id = str(params["orderLinkId"])
            self.last_place_params = dict(params)
            raise BybitSubmissionUncertain("venue accepted; HTTP response lost")

        def get_trade_history(self, **params: object) -> list[dict[str, str]]:
            assert params["order_link_id"] == self.command_id
            return [
                {
                    "orderLinkId": self.command_id,
                    "orderId": "venue-lost-1",
                    "execId": "fill-lost-1",
                    "execQty": "2",
                    "execPrice": "10",
                    "execFee": "0.01",
                    "execTime": "2",
                    "side": "Buy",
                }
            ]

        def get_order_history(self, **params: object) -> list[dict[str, str]]:
            assert params["order_link_id"] == self.command_id
            return [
                {
                    "orderLinkId": self.command_id,
                    "orderStatus": "Filled",
                    "cumExecQty": "2",
                    "updatedTime": "3",
                }
            ]

        def get_positions(self, **params: object) -> list[dict[str, str]]:
            assert params == {"settle_coin": "USDT"}
            return [{"symbol": "BUSDT", "side": "Buy", "size": "2"}]

        def get_open_orders(self, **params: object) -> list[dict[str, str]]:
            assert params in (
                {"settle_coin": "USDT"},
                {"settle_coin": "USDT", "order_filter": "StopOrder"},
            )
            return []

    client = LostResponseClient()
    first_service = _service(root, BybitDemoExecutionAdapter(client))
    with pytest.raises(BybitSubmissionUncertain, match="HTTP response lost"):
        first_service.run_once(inbox)
    state_after_loss = first_service.kernel.state()
    command_id = next(iter(state_after_loss.orders))
    assert state_after_loss.orders[command_id].status == "commanded"
    assert state_after_loss.orders[command_id].submission_attempts == 1
    assert state_after_loss.working_signed_qty("BUSDT") == pytest.approx(2.0)
    assert client.submit_calls == 1
    assert client.last_place_params["stopLoss"] == "8"
    assert client.last_place_params["slTriggerBy"] == "MarkPrice"
    assert client.last_place_params["tpslMode"] == "Full"
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1

    unreconciled = _service(root, BybitDemoExecutionAdapter(client))
    with pytest.raises(AmbiguousExposureSubmission, match="refusing to resend"):
        unreconciled.run_once(inbox)
    assert client.submit_calls == 1
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1

    reconcile_clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=200)
    reconciler = BybitAccountReconciler(
        kernel=first_service.kernel,
        client=client,
        instrument_rules=_rules(),
        clock=reconcile_clock,
    )
    report = reconciler.reconcile_once()
    assert report.healthy
    assert first_service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert first_service.kernel.state().orders[command_id].status == "filled"

    restarted_service = _service(root, BybitDemoExecutionAdapter(client))
    receipt = restarted_service.run_once(inbox)
    assert receipt is not None and receipt.accepted
    assert client.submit_calls == 1
    assert len(list((inbox.root / "completed").glob("*.json"))) == 1


def test_uncertain_leverage_response_retries_before_single_order_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    request = _request(
        _route(tmp_path),
        request_id="lost-leverage-response-1",
        batch_id="lost-leverage-response-1",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    inbox.submit(request)

    class LostLeverageResponseClient:
        demo = True

        def __init__(self) -> None:
            self.leverage_calls = 0
            self.order_calls = 0

        def set_leverage(self, **_params: object) -> dict[str, object]:
            self.leverage_calls += 1
            if self.leverage_calls == 1:
                raise BybitSubmissionUncertain("leverage response lost")
            return {}

        def place_order(self, **params: object) -> dict[str, str]:
            self.order_calls += 1
            assert params["stopLoss"] == "8"
            return {
                "orderId": "venue-after-leverage-retry-1",
                "_response_time_ms": "1100",
            }

    client = LostLeverageResponseClient()
    service = _service(root, BybitDemoExecutionAdapter(client))

    with pytest.raises(BybitSubmissionUncertain, match="leverage response lost"):
        service.run_once(inbox)
    command_id = next(iter(service.kernel.state().orders))
    after_leverage_loss = service.kernel.state().orders[command_id]
    assert after_leverage_loss.status == "commanded"
    assert after_leverage_loss.submission_attempts == 0
    assert client.leverage_calls == 1
    assert client.order_calls == 0
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1

    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.accepted
    submitted = service.kernel.state().orders[command_id]
    assert submitted.status == "acknowledged"
    assert submitted.submission_attempts == 1
    assert client.leverage_calls == 2
    assert client.order_calls == 1
    assert len(list((inbox.root / "completed").glob("*.json"))) == 1


def test_completed_request_id_cannot_be_reused_with_different_content(tmp_path: Path) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    original = _request(
        _route(tmp_path),
        request_id="immutable-1",
        batch_id="immutable-1",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
    )
    inbox.submit(original)
    receipt = service.run_once(inbox)
    assert receipt is not None
    completed = next((inbox.root / "completed").glob("*.json"))
    stored = json.loads(completed.read_text())
    assert stored["receipt"]["request_hash"] == original.content_hash()

    changed = _request(
        _route(tmp_path),
        request_id="immutable-1",
        batch_id="immutable-1",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-30.0,
    )
    with pytest.raises(ValueError, match="changed content"):
        inbox.submit(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route_id", "account-route-v1-" + "0" * 64),
        ("account_id", "wrong-account"),
        ("environment", "mainnet"),
    ],
)
def test_request_route_identity_mismatch_fails_before_queue_or_execution(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    request = replace(
        _request(
            service.route,
            request_id=f"wrong-{field}",
            batch_id=f"wrong-{field}",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        ),
        **{field: value},
    )

    with pytest.raises(ValueError, match="does not match account route"):
        inbox.submit(request)
    with pytest.raises(ValueError, match="does not match account route"):
        service.handle(request)

    assert not list((inbox.root / "pending").glob("*.json"))
    assert service.kernel.state().events_applied == 0
    assert adapter.submit_calls == 0


def test_service_rejects_cross_wired_inbox_before_claim_or_provider_calls(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    other_route = ensure_account_route(
        account_id="service-account",
        environment="demo",
        account_root=tmp_path / "other-account",
        inbox_root=tmp_path / "other-inbox",
    )
    other_inbox = AccountIntentInbox(other_route)
    other_inbox.submit(
        _request(
            other_route,
            request_id="cross-wired",
            batch_id="cross-wired",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )

    with pytest.raises(ValueError, match="service and inbox routes do not match"):
        service.run_once(other_inbox)

    assert len(list((other_inbox.root / "pending").glob("*.json"))) == 1
    assert service.kernel.state().events_applied == 0
    assert adapter.submit_calls == 0


def test_tampered_pending_request_fails_closed_on_every_read_path(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    request = _request(
        inbox.route,
        request_id="tampered-pending",
        batch_id="tampered-pending",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
    )
    path = inbox.submit(request)
    payload = json.loads(path.read_bytes())
    payload["account_id"] = "wrong-account"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable account target request"):
        inbox.contains(request.request_id)
    with pytest.raises(RuntimeError, match="unreadable account target request"):
        inbox.requested_symbols()
    with pytest.raises(ValueError, match="does not match account route"):
        inbox.claim_next()


def test_target_request_parser_rejects_missing_and_unknown_route_schema_fields(
    tmp_path: Path,
) -> None:
    payload = _request(
        _route(tmp_path),
        request_id="strict-schema",
        batch_id="strict-schema",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    ).to_dict()
    missing = dict(payload)
    missing.pop("route_id")
    with pytest.raises(ValueError, match="missing fields: route_id"):
        AccountTargetRequest.from_dict(missing)

    extended = dict(payload)
    extended["silent_extension"] = True
    with pytest.raises(ValueError, match="unknown fields: silent_extension"):
        AccountTargetRequest.from_dict(extended)


def test_inbox_exposes_pending_symbols_for_dynamic_capture(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="symbols-1",
            batch_id="symbols-1",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    assert inbox.requested_symbols() == {"BUSDT"}
    claimed = inbox.claim_next()
    assert claimed is not None
    assert inbox.requested_symbols() == {"BUSDT"}
    inbox.fail(claimed[0], error=RuntimeError("test terminal"))
    assert inbox.requested_symbols() == set()


def test_stale_market_input_releases_request_for_retry_without_kernel_mutation(tmp_path: Path) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter, market=_market(local_ns=1), max_market_age_ns=10)
    inbox = _inbox(tmp_path)
    target_key = "continuous/main/BUSDT"
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="stale-1",
            batch_id="stale-1",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
            target_key=target_key,
            decision_key="continuous-target/strategy/1000/entry/main",
            metadata={
                "entry_attempt_key": f"entry-attempt/{target_key}",
                "signal_ts_ms": 1_000,
                "signal_valid_until_ms": 2_000,
            },
        )
    )
    with pytest.raises(RuntimeError, match="stale market input"):
        service.run_once(inbox)
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert service.kernel.state().events_applied == 0
    assert adapter.submit_calls == 0


def test_expired_entry_is_completed_before_inputs_or_kernel_and_survives_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/signal-1/BUSDT"
    request = _request(
        _route(tmp_path),
        request_id="expired-entry-1",
        batch_id="expired-entry-1",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        created_ts_ns=1_000_000_000,
        target_key=target_key,
        decision_key="long-target/long-v1/1000/entry/signal-1",
        metadata={
            "entry_attempt_key": f"entry-attempt/{target_key}",
            "signal_ts_ms": 500,
            "signal_valid_until_ms": 1_000,
        },
    )
    inbox.submit(request)
    adapter = CountingTwin()
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock)

    receipt = service.run_once(inbox)
    assert receipt is not None
    assert receipt.disposition == "expired"
    assert receipt.rejection_keys == (f"account-service:entry-signal-expired:entry-attempt/{target_key}",)
    assert not receipt.accepted
    assert service.kernel.state().events_applied == 0
    assert adapter.submit_calls == 0
    # Scoped to the signal instant, so the same target minted from a later bar
    # is a new decision rather than a symbol retired for the journal's life.
    assert completed_expired_entry_attempt_keys(
        inbox,
        sleeve=SleeveAdapterKind.LONG,
        strategy_ids=("long-v1",),
    ) == frozenset({f"entry-attempt/{target_key}@500"})

    restarted = _service(root, adapter, clock=clock)
    assert inbox.submit(request).parent.name == "completed"
    assert restarted.run_once(inbox) is None
    assert restarted.kernel.state().events_applied == 0
    assert adapter.submit_calls == 0

    completed = next((inbox.root / "completed").glob("*.json"))
    tampered = json.loads(completed.read_bytes())
    tampered["receipt"]["request_hash"] = "0" * 64
    completed.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity validation"):
        completed_expired_entry_attempt_keys(
            inbox,
            sleeve=SleeveAdapterKind.LONG,
            strategy_ids=("long-v1",),
        )


def test_new_risk_halt_refuses_a_queued_entry_without_reading_a_book(tmp_path: Path) -> None:
    """The queue is the gap owner health cannot cover.

    A producer reads health once at the top of its cycle and publishes minutes
    later. A request written just before a loss-ceiling trip is already in the
    queue by the time health goes blocked, and admission is the last place that
    can still refuse it.
    """

    target_key = "long/long-v1/halted/BUSDT"
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="entry-after-trip",
            batch_id="entry-after-trip",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key=target_key,
            decision_key="long-target/long-v1/1100/entry/halted",
            metadata={
                "entry_attempt_key": f"entry-attempt/{target_key}",
                "signal_ts_ms": 1_000,
                "signal_valid_until_ms": NOW_NS // 1_000_000 + 60_000,
            },
        )
    )
    adapter = CountingTwin()
    service = _service(
        tmp_path / "account",
        adapter,
        new_risk_halt=lambda: "account daily loss ceiling: -120.00 USDT",
    )
    # No book at all: the refusal must not depend on one.
    service.market_provider.market = None

    receipt = service.run_once(inbox)

    assert receipt is not None
    assert not receipt.accepted
    assert receipt.disposition == "halted"
    assert receipt.rejection_keys == ("account-service:new-risk-halted",)
    assert adapter.submit_calls == 0
    assert service.kernel.state().events_applied == 0
    assert len(list((inbox.root / "completed").glob("*.json"))) == 1


def test_new_risk_halt_still_lets_an_exit_through(tmp_path: Path) -> None:
    """Halting stops the next risk; it must never block closing what is open."""

    adapter = CountingTwin()
    service = _service(
        tmp_path / "account",
        adapter,
        new_risk_halt=lambda: "account daily loss ceiling: -120.00 USDT",
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="entry-before-trip",
            batch_id="entry-before-trip",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key="long/long-v1/open/BUSDT",
        )
    )
    service.new_risk_halt = None
    assert service.run_once(inbox) is not None
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)

    service.new_risk_halt = lambda: "account daily loss ceiling: -120.00 USDT"
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="flatten-after-trip",
            batch_id="flatten-after-trip",
            kind=SleeveAdapterKind.RISK,
            notional=0.0,
            target_key="long/long-v1/open/BUSDT",
        )
    )
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert receipt.disposition == "processed"
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(0.0)


def test_new_risk_halt_does_not_abandon_a_batch_already_in_the_journal(tmp_path: Path) -> None:
    """A committed batch replays past the halt, exactly as it replays past expiry.

    Its commands may be half-submitted at the venue; refusing the replay would
    strand them working with no way to flatten what they opened.
    """

    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/halt-mid-flight/BUSDT"
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="entry-crash-then-halt",
            batch_id="entry-crash-then-halt",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key=target_key,
            decision_key="long-target/long-v1/1100/entry/halt-mid-flight",
            metadata={
                "entry_attempt_key": f"entry-attempt/{target_key}",
                "signal_ts_ms": 1_000,
                "signal_valid_until_ms": NOW_NS // 1_000_000 + 60_000,
            },
        )
    )
    adapter = ScriptedExecutionAdapter("crash", "fill")
    service = _service(root, adapter)

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_once(inbox)
    assert "entry-crash-then-halt" in service.kernel.state().processed_batches

    service.new_risk_halt = lambda: "account daily loss ceiling: -120.00 USDT"
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert receipt.disposition == "processed"
    assert adapter.submit_calls == 2


def test_committed_entry_resumes_after_crash_even_if_signal_expires(tmp_path: Path) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/signal-crash/BUSDT"
    request = _request(
        _route(tmp_path),
        request_id="crash-before-expiry",
        batch_id="crash-before-expiry",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        target_key=target_key,
        decision_key="long-target/long-v1/1100/entry/signal-crash",
        metadata={
            "entry_attempt_key": f"entry-attempt/{target_key}",
            "signal_ts_ms": 1_000,
            "signal_valid_until_ms": 1_200,
        },
    )
    inbox.submit(request)
    adapter = ScriptedExecutionAdapter("crash", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock)

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_once(inbox)
    assert request.batch_id in service.kernel.state().processed_batches
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert service.kernel.state().events_applied > 0

    clock.advance_ns(200_000_000)
    service.market_provider.market = MarketInputRef(
        input_key="book-after-restart",
        symbol="BUSDT",
        exchange_ts_ns=1_250_000_000,
        local_receive_ts_ns=1_300_000_000,
        reference_price=20.0,
        bid_price=19.9,
        ask_price=20.1,
        book_sequence=2,
        source="test-restart",
    )

    class ChangedRulesProvider:
        def current(self, symbols: list[str]) -> dict[str, InstrumentRules]:
            return {
                symbol: InstrumentRules(
                    symbol=symbol,
                    qty_step=1.0,
                    min_qty=1.0,
                    min_notional=10.0,
                    max_order_qty=100.0,
                    max_leverage=20.0,
                )
                for symbol in symbols
            }

    service.rules_provider = ChangedRulesProvider()
    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.accepted
    assert receipt.disposition == "processed"
    assert adapter.submit_calls == 2
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert len(list((inbox.root / "completed").glob("*.json"))) == 1


def test_committed_batch_replays_instead_of_superseding_under_later_flat(tmp_path: Path) -> None:
    """A journal-committed batch must replay after a crash, never supersede.

    Completing it as superseded would strand its journaled-but-unsubmitted commands
    in working state forever: convergence then reports "working" for the symbol and
    can never flatten the venue position the submitted part opened.
    """

    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/replay-vs-supersede/BUSDT"
    entry = _request(
        _route(tmp_path),
        request_id="entry-committed-crash",
        batch_id="entry-committed-crash",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        target_key=target_key,
        decision_key="long-target/long-v1/1100/entry/replay-vs-supersede",
        metadata={
            "entry_attempt_key": f"entry-attempt/{target_key}",
            "signal_ts_ms": 1_000,
            "signal_valid_until_ms": 1_200,
        },
    )
    inbox.submit(entry)
    adapter = ScriptedExecutionAdapter("crash", "fill", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock)

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_once(inbox)
    assert entry.batch_id in service.kernel.state().processed_batches

    clock.advance_ns(200_000_000)
    flat = _request(
        _route(tmp_path),
        request_id="flat-after-crash",
        batch_id="flat-after-crash",
        kind=SleeveAdapterKind.LONG,
        notional=0.0,
        target_key=target_key,
        decision_key="long-target/long-v1/1101/flat/replay-vs-supersede",
    )
    inbox.submit(flat)

    receipt = service.run_once(inbox)

    assert receipt is not None
    assert receipt.request_id == "entry-committed-crash"
    assert receipt.disposition == "processed"
    assert receipt.accepted
    # The committed batch's unsubmitted command was actually replayed.
    assert adapter.submit_calls >= 2
    completed = list((inbox.root / "completed").glob("*.json"))
    assert len(completed) == 1


def test_failing_queue_head_does_not_starve_convergence(tmp_path: Path) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/starved-head/BUSDT"
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="head-that-always-fails",
            batch_id="head-that-always-fails",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key=target_key,
            decision_key="long-target/long-v1/1100/entry/starved-head",
            metadata={
                "entry_attempt_key": f"entry-attempt/{target_key}",
                "signal_ts_ms": 1_000,
                "signal_valid_until_ms": 1_200,
            },
        )
    )
    adapter = ScriptedExecutionAdapter("crash")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock)

    converge_calls: list[int] = []
    original_converge = service.converge_once

    def counting_converge():
        converge_calls.append(1)
        return original_converge()

    service.converge_once = counting_converge  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_once(inbox)

    # The head failure still surfaced, but canonical convergence work ran in
    # the same cycle instead of being starved by the perpetually failing head.
    assert converge_calls


def test_flat_exit_does_not_expire_even_with_stale_entry_metadata(tmp_path: Path) -> None:
    root = tmp_path / "account"
    inbox = _inbox(tmp_path)
    target_key = "long/long-v1/signal-1/BUSDT"
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="stale-metadata-flat",
            batch_id="stale-metadata-flat",
            kind=SleeveAdapterKind.LONG,
            notional=0.0,
            target_key=target_key,
            decision_key="long-target/long-v1/2000/exit/signal-1",
            metadata={
                "entry_attempt_key": f"entry-attempt/{target_key}",
                "signal_ts_ms": 500,
                "signal_valid_until_ms": 1_000,
            },
        )
    )
    service = _service(
        root,
        CountingTwin(),
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100),
    )

    receipt = service.run_once(inbox)
    assert receipt is not None
    assert receipt.disposition == "processed"


def test_expired_batch_receipt_terminalizes_only_the_expired_entry_attempt(
    tmp_path: Path,
) -> None:
    expired_key = "long/long-v1/expired/BUSDT"
    valid_key = "long/long-v1/valid/BUSDT"
    resize_key = "long/long-v1/resize/BUSDT"
    expired = _request(
        _route(tmp_path),
        request_id="expired-part",
        batch_id="expired-part",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
        target_key=expired_key,
        decision_key="long-target/long-v1/1000/entry/expired",
        metadata={
            "entry_attempt_key": f"entry-attempt/{expired_key}",
            "signal_ts_ms": 500,
            "signal_valid_until_ms": 1_000,
        },
    ).intents[0]
    valid = _request(
        _route(tmp_path),
        request_id="valid-part",
        batch_id="valid-part",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
        target_key=valid_key,
        decision_key="long-target/long-v1/1000/entry/valid",
        metadata={
            "entry_attempt_key": f"entry-attempt/{valid_key}",
            "signal_ts_ms": 1_000,
            "signal_valid_until_ms": 3_000,
        },
    ).intents[0]
    resize = _request(
        _route(tmp_path),
        request_id="resize-part",
        batch_id="resize-part",
        kind=SleeveAdapterKind.LONG,
        notional=5.0,
        target_key=resize_key,
        decision_key="long-target/long-v1/1000/resize/resize",
    ).intents[0]
    request = AccountTargetRequest(
        request_id="partially-expired-batch",
        batch_id="partially-expired-batch",
        created_ts_ns=1_000_000_000,
        route_id=_route(tmp_path).route_id,
        account_id=_route(tmp_path).account_id,
        environment=_route(tmp_path).environment,
        intents=(expired, valid, resize),
    )
    inbox = _inbox(tmp_path)
    inbox.submit(request)
    service = _service(
        tmp_path / "account",
        CountingTwin(),
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100),
    )

    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.disposition == "expired"
    assert completed_expired_entry_attempt_keys(
        inbox,
        sleeve=SleeveAdapterKind.LONG,
        strategy_ids=("long-v1",),
    ) == frozenset({f"entry-attempt/{expired_key}@500"})


def test_expiry_suppresses_its_own_signal_but_not_the_next_one(tmp_path: Path) -> None:
    """One expiry must not retire the symbol for the life of the journal.

    The attempt key is a pure function of the target key, so before this the
    next cycle minted the identical key and suppressed itself forever — however
    fresh the decision behind it.
    """

    target_key = "long/long-v1/signal-1/BUSDT"
    attempt_key = f"entry-attempt/{target_key}"
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="expired-entry-1",
            batch_id="expired-entry-1",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            created_ts_ns=1_000_000_000,
            target_key=target_key,
            decision_key="long-target/long-v1/1000/entry/signal-1",
            metadata={
                "entry_attempt_key": attempt_key,
                "signal_ts_ms": 500,
                "signal_valid_until_ms": 1_000,
            },
        )
    )
    service = _service(
        tmp_path / "account",
        CountingTwin(),
        clock=VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100),
    )
    assert service.run_once(inbox) is not None

    terminal = completed_expired_entry_attempt_keys(
        inbox,
        sleeve=SleeveAdapterKind.LONG,
        strategy_ids=("long-v1",),
    )

    def entry(signal_ts_ms: int) -> RequestedIntent:
        return _request(
            _route(tmp_path),
            request_id=f"republished-{signal_ts_ms}",
            batch_id=f"republished-{signal_ts_ms}",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key=target_key,
            decision_key="long-target/long-v1/1000/entry/signal-1",
            metadata={
                "entry_attempt_key": attempt_key,
                "signal_ts_ms": signal_ts_ms,
                "signal_valid_until_ms": signal_ts_ms + 500,
            },
        ).intents[0]

    same_signal = suppress_target_intents(
        exit_intents=(),
        entry_intents=(entry(500),),
        unresolved_target_keys=frozenset(),
        terminal_entry_attempts=terminal,
    )
    assert same_signal.entry_intents == []
    assert same_signal.terminal_entry_attempt_suppressions == 1

    later_signal = suppress_target_intents(
        exit_intents=(),
        entry_intents=(entry(3_600_000),),
        unresolved_target_keys=frozenset(),
        terminal_entry_attempts=terminal,
    )
    assert len(later_signal.entry_intents) == 1
    assert later_signal.terminal_entry_attempt_suppressions == 0


def test_target_request_forbids_mixing_exit_and_entry(tmp_path: Path) -> None:
    entry = _request(
        _route(tmp_path),
        request_id="entry",
        batch_id="entry",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
    ).intents[0]
    exit_intent = _request(
        _route(tmp_path),
        request_id="exit",
        batch_id="exit",
        kind=SleeveAdapterKind.LONG,
        notional=0.0,
        target_key="long/other/BUSDT",
    ).intents[0]

    with pytest.raises(ValueError, match="cannot mix flat exits"):
        AccountTargetRequest(
            request_id="mixed",
            batch_id="mixed",
            created_ts_ns=NOW_NS,
            route_id=_route(tmp_path).route_id,
            account_id=_route(tmp_path).account_id,
            environment=_route(tmp_path).environment,
            intents=(exit_intent, entry),
        )


def test_definite_reject_is_retried_deterministically_until_fill(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("reject", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="reject-then-fill",
            batch_id="reject-then-fill",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )

    receipt = service.run_once(inbox)
    assert receipt is not None and receipt.accepted
    first_command = adapter.submissions[0]
    assert service.kernel.state().orders[first_command.command_id].status == "rejected"
    report = service.convergence_report()
    assert len(report.items) == 1
    assert report.items[0].retry_attempts == 0
    assert report.items[0].status == "retry_backoff"

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    retry = adapter.submissions[1]
    assert retry.batch_id.endswith("/0001")
    assert retry.command_id != first_command.command_id
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert service.convergence_report().converged


def test_rejected_zero_close_is_retained_and_retry_remains_reduce_only(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("fill", "reject", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="open-before-close",
            batch_id="open-before-close",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    assert service.run_once(inbox) is not None
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-2.0)

    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="rejected-flat-target",
            batch_id="rejected-flat-target",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=0.0,
        )
    )
    close_receipt = service.run_once(inbox)
    assert close_receipt is not None and close_receipt.accepted
    rejected_close = adapter.submissions[1]
    assert rejected_close.signed_qty == pytest.approx(2.0)
    assert rejected_close.reduce_only
    state = service.kernel.state()
    desire = state.component_target_desires["continuous/main/BUSDT"]
    assert desire["signed_qty"] == 0.0
    assert state.aggregate_targets["BUSDT"] == 0.0
    assert state.positions["BUSDT"].signed_qty == pytest.approx(-2.0)

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    retried_close = adapter.submissions[2]
    assert retried_close.signed_qty == pytest.approx(2.0)
    assert retried_close.reduce_only
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(0.0)
    assert service.convergence_report().converged


def test_reduce_only_convergence_persists_past_entry_retry_limit_with_capped_backoff(
    tmp_path: Path,
) -> None:
    adapter = ScriptedExecutionAdapter("fill", "reject", "reject", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_retry_backoff_cap_ns=150,
        convergence_health_grace_ns=50,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="open-for-persistent-close",
            batch_id="open-for-persistent-close",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    assert service.run_once(inbox) is not None

    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="persistent-close",
            batch_id="persistent-close",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=0.0,
        )
    )
    assert service.run_once(inbox) is not None
    assert adapter.submissions[-1].reduce_only

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    first_retry = adapter.submissions[-1]
    assert first_retry.batch_id.endswith("/0001")
    persistent = service.convergence_report()
    assert persistent.items[0].retry_attempts == 1
    assert persistent.items[0].retry_limit is None
    assert persistent.items[0].retry_budget_label == "persistent"
    assert persistent.items[0].retryable
    assert not persistent.items[0].exhausted
    assert not persistent.healthy

    clock.advance_ns(149)
    assert service.run_once(inbox) is None
    assert adapter.submit_calls == 3
    clock.advance_ns(1)
    assert service.run_once(inbox) is None
    second_retry = adapter.submissions[-1]
    assert second_retry.batch_id.endswith("/0002")
    assert second_retry.reduce_only
    assert adapter.submit_calls == 4
    assert service.convergence_report().converged


def test_terminal_cancel_is_retried_after_backoff(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("cancel", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="cancelled-entry",
            batch_id="cancelled-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    cancelled = adapter.submissions[0]
    assert service.kernel.state().orders[cancelled.command_id].status == "cancelled"
    assert service.kernel.state().working_signed_qty("BUSDT") == 0.0

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    assert adapter.submissions[1].signed_qty == pytest.approx(2.0)
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert service.convergence_report().converged


def test_partial_fill_cancel_retries_only_the_residual(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("partial_cancel", "fill", partial_qty=0.6)
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="partial-entry",
            batch_id="partial-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    partial = adapter.submissions[0]
    state = service.kernel.state()
    assert state.orders[partial.command_id].status == "partially_filled_cancelled"
    assert state.positions["BUSDT"].signed_qty == pytest.approx(0.6)
    assert service.convergence_report().items[0].residual_signed_qty == pytest.approx(1.4)

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    residual = adapter.submissions[1]
    assert residual.signed_qty == pytest.approx(1.4)
    assert not residual.reduce_only
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert service.convergence_report().converged


def test_restart_after_terminal_retry_derives_next_ordinal_without_duplicate_command(
    tmp_path: Path,
) -> None:
    root = tmp_path / "account"
    adapter = ScriptedExecutionAdapter("reject", "cancel", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        root,
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="restart-terminal",
            batch_id="restart-terminal",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    assert adapter.submissions[1].batch_id.endswith("/0001")
    assert service.kernel.state().orders[adapter.submissions[1].command_id].status == "cancelled"

    restart_clock = VirtualClock(
        current_wall_ns=clock.wall_time_ns(),
        current_monotonic_ns=clock.monotonic_ns(),
    )
    restarted = _service(
        root,
        adapter,
        clock=restart_clock,
        convergence_retry_backoff_ns=100,
    )
    assert restarted.convergence_report().items[0].retry_attempts == 1
    assert restarted.converge_once() is None
    assert adapter.submit_calls == 2

    restart_clock.advance_ns(200)
    assert restarted.run_once(inbox) is None
    assert adapter.submissions[2].batch_id.endswith("/0002")
    assert len({command.command_id for command in adapter.submissions}) == 3
    retry_batches = {
        order.batch_id
        for order in restarted.kernel.state().orders.values()
        if order.batch_id.startswith("account-convergence/")
    }
    assert {batch.rsplit("/", 1)[-1] for batch in retry_batches} == {"0001", "0002"}
    assert restarted.convergence_report().converged


def test_crash_after_convergence_commit_replays_the_same_command_id(tmp_path: Path) -> None:
    root = tmp_path / "account"
    adapter = ScriptedExecutionAdapter("reject", "crash", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        root,
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="crash-convergence",
            batch_id="crash-convergence",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)
    with pytest.raises(RuntimeError, match="after command commit"):
        service.converge_once()
    crashed_command = adapter.submissions[1]
    assert crashed_command.batch_id.endswith("/0001")
    assert service.kernel.state().orders[crashed_command.command_id].status == "commanded"

    restart_clock = VirtualClock(
        current_wall_ns=clock.wall_time_ns(),
        current_monotonic_ns=clock.monotonic_ns(),
    )
    restarted = _service(
        root,
        adapter,
        clock=restart_clock,
        convergence_retry_backoff_ns=100,
    )
    replayed = restarted.converge_once()
    assert replayed is not None
    assert adapter.submissions[2].command_id == crashed_command.command_id
    assert adapter.submissions[2].batch_id == crashed_command.batch_id
    convergence_orders = [
        order for order in restarted.kernel.state().orders.values() if order.batch_id.startswith("account-convergence/")
    ]
    assert len(convergence_orders) == 1
    assert restarted.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert restarted.convergence_report().converged


def test_convergence_steps_over_a_wedged_batch_instead_of_starving(tmp_path: Path) -> None:
    """B15b: converge_once returns on the first commanded plan, so one wedged
    early-alphabet symbol starved convergence for every other symbol."""

    root = tmp_path / "account"
    adapter = ScriptedExecutionAdapter("reject", "crash", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock, convergence_retry_backoff_ns=100)
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="wedged-convergence",
            batch_id="wedged-convergence",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)
    with pytest.raises(RuntimeError, match="after command commit"):
        service.converge_once()
    crashed = adapter.submissions[1]
    assert not crashed.reduce_only
    assert service.kernel.state().orders[crashed.command_id].status == "commanded"

    # Minutes later it is not "in flight", it is wedged, and replaying it only
    # repeats the same failure forever.
    clock.advance_ns(DEFAULT_WEDGE_AFTER_NS + 1_000_000_000)
    restarted = _service(
        root,
        adapter,
        clock=VirtualClock(
            current_wall_ns=clock.wall_time_ns(),
            current_monotonic_ns=clock.monotonic_ns(),
        ),
        convergence_retry_backoff_ns=100,
    )
    submissions_before = adapter.submit_calls

    assert restarted.converge_once() is None
    assert adapter.submit_calls == submissions_before
    # Untouched: only an operator-authorized transition may terminalize it.
    assert restarted.kernel.state().orders[crashed.command_id].status == "commanded"


def test_crash_replay_resubmits_the_commanded_order_after_the_market_moved(
    tmp_path: Path,
) -> None:
    """Crash replay recomputes the batch's request hash, and convergence derives its
    targets from a fresh L2 book, so price movement between commit and replay must
    not turn the recomputed hash into an ``AccountJournalIntegrityError``.
    """

    root = tmp_path / "account"
    adapter = ScriptedExecutionAdapter("reject", "crash", "fill")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(root, adapter, clock=clock, convergence_retry_backoff_ns=100)
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="moving-market",
            batch_id="moving-market",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)
    with pytest.raises(RuntimeError, match="after command commit"):
        service.converge_once()
    crashed_command = adapter.submissions[1]
    assert service.kernel.state().orders[crashed_command.command_id].status == "commanded"

    moved = MarketInputRef(
        input_key="book-moved",
        symbol="BUSDT",
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=10.5,
        bid_price=10.4,
        ask_price=10.6,
        book_sequence=2,
        source="test",
    )
    restart_clock = VirtualClock(
        current_wall_ns=clock.wall_time_ns(),
        current_monotonic_ns=clock.monotonic_ns(),
    )
    restarted = _service(
        root,
        adapter,
        market=moved,
        clock=restart_clock,
        convergence_retry_backoff_ns=100,
    )
    replayed = restarted.converge_once()
    assert replayed is not None
    assert adapter.submissions[2].command_id == crashed_command.command_id
    assert adapter.submissions[2].batch_id == crashed_command.batch_id
    convergence_orders = [
        order
        for order in restarted.kernel.state().orders.values()
        if order.batch_id.startswith("account-convergence/")
    ]
    assert len(convergence_orders) == 1
    assert restarted.convergence_report().converged


def test_sliced_entry_progress_resets_the_retry_budget(tmp_path: Path) -> None:
    """A retry that made progress is not a failure.

    A large entry sized to the displayed touch arrives as several partial
    windows (2026-08-04 sizing program): each one terminates partially filled
    and convergence plans the next. The retry budget and backoff must count
    only the attempts since the newest fill, or the fourth window of a
    five-window entry dies as ``retry_exhausted``.
    """

    adapter = ScriptedExecutionAdapter(
        "partial_cancel",
        "partial_cancel",
        "partial_cancel",
        "partial_cancel",
        "fill",
        partial_qty=0.4,
    )
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        # A grace far below the loop cadence: every hand-over between windows
        # would flunk it without the exemption below.
        convergence_health_grace_ns=500,
        max_convergence_retries=3,
        # The manager reports a live window for the symbol throughout: the
        # exemption must hold even between windows, when nothing is working.
        resting_entry_quotes=lambda _symbol: True,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="sliced-entry",
            batch_id="sliced-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    for _ in range(8):
        clock.advance_ns(10_000)
        service.run_once(inbox)
        # Mid-sequence, between windows, nothing is working and the age is
        # far past the tiny grace: only the resting-window exemption keeps
        # the owner from flickering unhealthy at every hand-over.
        assert service.convergence_report().healthy

    report = service.convergence_report()
    assert not any(item.exhausted for item in report.items)
    assert report.healthy
    # The last step-quantization tail sits below the venue minimum, which is
    # the converged-dust state, not a failure.
    assert all(
        item.status == "converged_within_venue_minimum" for item in report.items
    )
    # Four touch-sized windows and the final remainder: five venue orders,
    # which is one more than the retry limit would ever allow without the
    # progress reset.
    assert adapter.submit_calls == 5


def test_retry_limit_exhaustion_blocks_health_immediately_and_stays_bounded(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("reject", "reject")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_health_grace_ns=500,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="bounded-retries",
            batch_id="bounded-retries",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    assert service.convergence_report().healthy

    clock.advance_ns(100)
    assert service.run_once(inbox) is None
    exhausted = service.convergence_report()
    assert exhausted.items[0].retry_attempts == 1
    assert exhausted.items[0].exhausted
    assert exhausted.items[0].status == "retry_exhausted"
    assert not exhausted.healthy
    with pytest.raises(RuntimeError, match="retry_exhausted"):
        exhausted.require_healthy()

    clock.advance_ns(400)
    overdue = service.convergence_report()
    assert not overdue.healthy
    with pytest.raises(RuntimeError, match="retry_exhausted"):
        overdue.require_healthy()
    assert service.run_once(inbox) is None
    assert adapter.submit_calls == 2


def test_retry_exhausted_unfilled_entry_unwinds_desire_and_releases_reservation(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("reject", "reject")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_health_grace_ns=500,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="unfilled-entry",
            batch_id="unfilled-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)
    assert service.run_once(inbox) is None

    # The incident is loud before any unwind: retries exhausted with zero fill.
    exhausted = service.convergence_report()
    assert exhausted.items[0].status == "retry_exhausted"
    assert not exhausted.healthy
    rows = canonical_strategy_trade_rows(_route(tmp_path).account_path, sleeve="long")
    assert rows["status"].to_list() == ["target_pending"]
    assert target_reservation_rows(rows)["target_key"].to_list() == ["long/main/BUSDT"]

    # The next convergence pass unwinds the provably unfilled entry desire.
    unwound = service.converge_once()
    assert unwound is not None
    assert unwound.accepted
    assert unwound.batch_id.startswith("account-convergence/BUSDT/")
    assert unwound.batch_id.endswith("/entry-unwind")
    assert not unwound.commands
    assert adapter.submit_calls == 2

    state = service.kernel.state()
    desire = state.component_target_desires["long/main/BUSDT"]
    assert float(desire["signed_qty"]) == pytest.approx(0.0)
    assert str(desire["reason"]) == "entry_retry_exhausted"
    assert state.aggregate_targets.get("BUSDT", 0.0) == pytest.approx(0.0)
    assert service.convergence_report().converged

    rows = canonical_strategy_trade_rows(_route(tmp_path).account_path, sleeve="long")
    assert rows["status"].to_list() == ["entry_retry_exhausted"]
    assert rows["entry_ts_ms"].to_list() == [None]
    assert target_reservation_rows(rows).is_empty()

    # Idempotent: the unwind fully settles the symbol.
    assert service.converge_once() is None


def test_retry_exhausted_partially_filled_entry_is_never_zero_targeted(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("partial_cancel", "reject")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_health_grace_ns=500,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="partial-then-exhausted",
            batch_id="partial-then-exhausted",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(0.6)
    clock.advance_ns(100)
    assert service.run_once(inbox) is None

    exhausted = service.convergence_report()
    assert exhausted.items[0].status == "retry_exhausted"
    assert not exhausted.healthy

    # Real exposure exists, so the unwind must never zero-target this entry.
    assert service.converge_once() is None
    state = service.kernel.state()
    assert float(state.component_target_desires["long/main/BUSDT"]["signed_qty"]) == pytest.approx(2.0)
    assert state.positions["BUSDT"].signed_qty == pytest.approx(0.6)
    assert not service.convergence_report().healthy
    rows = canonical_strategy_trade_rows(_route(tmp_path).account_path, sleeve="long")
    assert not target_reservation_rows(rows).is_empty()


def test_pending_replacements_coalesce_before_old_residual_can_trade(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("reject")
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="initial-residual",
            batch_id="initial-residual",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(100)  # the old -2 target is now retry-due

    older = _request(
        _route(tmp_path),
        request_id="fifo-older",
        batch_id="fifo-older",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-30.0,
        # Producer time is deliberately later than the request which arrives
        # after it. Only the inbox's durable local arrival sequence is causal.
        created_ts_ns=NOW_NS + 20,
    )
    newest = _request(
        _route(tmp_path),
        request_id="fifo-newest",
        batch_id="fifo-newest",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=0.0,
        created_ts_ns=NOW_NS + 10,
    )
    # Hash filename order and producer timestamps are both non-causal.
    assert inbox._filename(newest.request_id) < inbox._filename(older.request_id)
    inbox.submit(older)
    inbox.submit(newest)

    first = service.run_once(inbox)
    assert first is not None and first.batch_id == "fifo-newest"
    assert adapter.submit_calls == 1
    assert service.kernel.state().aggregate_targets["BUSDT"] == 0.0
    assert service.convergence_report().converged
    completed = [json.loads(path.read_bytes()) for path in (inbox.root / "completed").glob("*.json")]
    superseded = next(row for row in completed if row["request"]["request_id"] == "fifo-older")
    assert superseded["receipt"]["disposition"] == "superseded"
    assert superseded["receipt"]["superseded_by_request_id"] == "fifo-newest"

    assert service.run_once(inbox) is None
    assert adapter.submit_calls == 1


def test_backlogged_entry_is_never_opened_when_a_later_target_is_flat(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="stale-entry",
            batch_id="stale-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            created_ts_ns=NOW_NS,
        )
    )
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="latest-flat",
            batch_id="latest-flat",
            kind=SleeveAdapterKind.LONG,
            notional=0.0,
            created_ts_ns=NOW_NS + 1,
        )
    )

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.batch_id == "latest-flat"
    assert receipt.accepted
    assert receipt.command_ids == ()
    assert adapter.submit_calls == 0
    assert service.kernel.state().positions == {}
    rows = [json.loads(path.read_bytes()) for path in (inbox.root / "completed").glob("*.json")]
    stale = next(row for row in rows if row["request"]["request_id"] == "stale-entry")
    assert stale["receipt"]["accepted"] is False
    assert stale["receipt"]["disposition"] == "superseded"


def test_strict_expected_request_processes_only_one_arrival_transition(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    older = _request(
        _route(tmp_path),
        request_id="strict-older-entry",
        batch_id="strict-older-entry",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    later_flat = _request(
        _route(tmp_path),
        request_id="strict-later-flat",
        batch_id="strict-later-flat",
        kind=SleeveAdapterKind.LONG,
        notional=0.0,
        created_ts_ns=NOW_NS + 1,
    )
    inbox.submit(older)
    inbox.submit(later_flat)

    first = service.run_once(inbox, expected_request_id=older.request_id)

    assert first is not None and first.request_id == older.request_id
    assert first.disposition == "superseded"
    assert inbox.peek_next() == later_flat
    assert adapter.submit_calls == 0

    second = service.run_once(inbox, expected_request_id=later_flat.request_id)
    assert second is not None and second.request_id == later_flat.request_id
    assert adapter.submit_calls == 0


def test_strict_expected_request_releases_a_raced_later_head(tmp_path: Path) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    first = _request(
        _route(tmp_path),
        request_id="strict-first",
        batch_id="strict-first",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    later = _request(
        _route(tmp_path),
        request_id="strict-later",
        batch_id="strict-later",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
    )
    inbox.submit(first)
    inbox.submit(later)

    receipt = service.run_once(inbox, expected_request_id=later.request_id)

    assert receipt is None
    assert inbox.peek_next() == first
    assert adapter.submit_calls == 0


def test_empty_readiness_observation_cannot_claim_a_new_raced_request(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    raced = _request(
        _route(tmp_path),
        request_id="arrived-after-empty-readiness",
        batch_id="arrived-after-empty-readiness",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    inbox.submit(raced)

    receipt = service.run_once(inbox, expected_request_id="")

    assert receipt is None
    assert inbox.peek_next() == raced
    assert adapter.submit_calls == 0


def test_flat_safety_transition_is_not_superseded_by_later_reentry(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    flat = _request(
        _route(tmp_path),
        request_id="flat-first",
        batch_id="flat-first",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=0.0,
        created_ts_ns=NOW_NS,
    )
    reentry = _request(
        _route(tmp_path),
        request_id="reentry-later",
        batch_id="reentry-later",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
        created_ts_ns=NOW_NS + 1,
    )
    inbox.submit(flat)
    inbox.submit(reentry)

    assert inbox.claim_superseded() is None
    claimed = inbox.claim_next()
    assert claimed is not None and claimed[1].request_id == "flat-first"


def test_safety_flat_claim_bypasses_uncommitted_unrelated_entry(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    entry = _request(
        _route(tmp_path),
        request_id="unrelated-entry",
        batch_id="unrelated-entry",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        target_key="long/unrelated/BUSDT",
    )
    safety = _native_breach_safety_request(
        _route(tmp_path),
        target_key="continuous/open/BUSDT",
    )
    inbox.submit(entry)
    inbox.submit(safety)

    claimed = inbox.claim_next_safety_flat(
        processed_batches=set(),
        authorized_request_hashes={safety.request_id: safety.content_hash()},
    )

    assert claimed is not None and claimed[1] == safety
    assert inbox.peek_next() == entry


def test_safety_flat_claim_requires_exact_journal_authorization_hash(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    entry = _request(
        _route(tmp_path),
        request_id="entry-first",
        batch_id="entry-first",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    safety = _native_breach_safety_request(_route(tmp_path))
    inbox.submit(entry)
    inbox.submit(safety)

    assert (
        inbox.claim_next_safety_flat(
            processed_batches=set(),
            authorized_request_hashes={safety.request_id: "0" * 64},
        )
        is None
    )
    assert inbox.peek_next() == entry


def test_safety_flat_claim_never_bypasses_prior_committed_batch(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    committed = _request(
        _route(tmp_path),
        request_id="committed-entry",
        batch_id="committed-entry",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    safety = _native_breach_safety_request(_route(tmp_path))
    inbox.submit(committed)
    inbox.submit(safety)

    assert (
        inbox.claim_next_safety_flat(
            processed_batches={committed.batch_id},
            authorized_request_hashes={safety.request_id: safety.content_hash()},
        )
        is None
    )
    assert inbox.peek_next() == committed


def test_ordinary_risk_flat_without_native_breach_proof_stays_fifo(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    entry = _request(
        _route(tmp_path),
        request_id="entry-first",
        batch_id="entry-first",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    ordinary_flat = _request(
        _route(tmp_path),
        request_id="ordinary-risk-flat",
        batch_id="ordinary-risk-flat",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
    )
    inbox.submit(entry)
    inbox.submit(ordinary_flat)

    assert (
        inbox.claim_next_safety_flat(
            processed_batches=set(),
            authorized_request_hashes={},
        )
        is None
    )
    assert inbox.peek_next() == entry


def test_exit_preview_can_use_authenticated_breach_mark_when_l2_is_absent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "account", CountingTwin())
    service.market_provider = MissingCapturedMarketProvider()
    fallback = MarketInputRef(
        input_key="authenticated-breach",
        symbol="BUSDT",
        exchange_ts_ns=0,
        local_receive_ts_ns=NOW_NS,
        reference_price=11.2,
        source="bybit_authenticated_position_or_rejection",
        metadata={"exit_only_authenticated_fallback": True},
    )

    markets, _snapshot, _rules_by_symbol = service._execution_inputs(
        requested_symbols={"BUSDT"},
        batch_id="native-breach-flat",
        require_external_health=False,
        account_wide=False,
        allow_stale_market_for_reduction_preview=True,
        allow_unavailable_snapshot_for_reduction_preview=True,
        exit_market_fallbacks={"BUSDT": fallback},
    )

    assert markets["BUSDT"] == fallback


def test_native_breach_shaped_request_without_journal_authority_cannot_use_l2_fallback(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "account", ScriptedExecutionAdapter("fill"))
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    inbox.submit(
        _request(
            route,
            request_id="open-current",
            batch_id="open-current",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    opened = service.run_once(inbox)
    assert opened is not None and opened.accepted
    forged = _native_breach_safety_request(route, target_key="long/main/BUSDT")
    inbox.submit(forged)
    service.market_provider = MissingCapturedMarketProvider()

    with pytest.raises(MarketCaptureError, match="no reconstructed book"):
        service.run_once(inbox)

    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)
    assert inbox.peek_next() == forged


def test_native_breach_priority_path_flattens_without_l2_and_leaves_older_entry_pending(
    tmp_path: Path,
) -> None:
    adapter = ScriptedExecutionAdapter("fill", "fill")
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    current_key = "long/main/BUSDT"
    inbox.submit(
        _request(
            route,
            request_id="open-current",
            batch_id="open-current",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            target_key=current_key,
        )
    )
    opened = service.run_once(inbox)
    assert opened is not None and opened.accepted
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(2.0)

    older_unrelated = _request(
        route,
        request_id="older-unrelated-entry",
        batch_id="older-unrelated-entry",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-10.0,
        target_key="continuous/unrelated/BUSDT",
    )
    inbox.submit(older_unrelated)
    protection_engine = AccountProtectionEngine(
        kernel=service.kernel,
        inbox=inbox,
        instrument_rules={
            "BUSDT": replace(_rules()["BUSDT"], tick_size=0.1),
        },
    )
    published = protection_engine.evaluate_native_breaches(
        (
            NativeProtectionBreach(
                plan=NativeProtectionPlan(
                    protection_key="native-disaster:BUSDT:test",
                    symbol="BUSDT",
                    signed_qty=2.0,
                    stop_price=9.0,
                    stop_source="test",
                    target_keys=(current_key,),
                ),
                observed_mark=8.9,
                evidence_source="authenticated_position_snapshot",
                detail="BUSDT native stop absent and crossed",
                observed_ts_ns=NOW_NS,
            ),
        )
    )
    assert len(published) == 1
    safety_id = published[0].request_id
    service.market_provider = MissingCapturedMarketProvider()

    receipt = service.run_safety_flat_once(inbox)

    assert receipt is not None and receipt.request_id == safety_id and receipt.accepted
    state = service.kernel.state()
    assert state.positions["BUSDT"].signed_qty == 0.0
    command = state.orders[receipt.command_ids[0]]
    assert command.reduce_only is True
    assert command.status == "filled"
    assert inbox.peek_next() == older_unrelated
    assert adapter.submit_calls == 2


def test_inbox_fifo_uses_durable_arrival_order_across_mixed_producer_timestamps(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    arrived_first = _request(
        _route(tmp_path),
        request_id="arrived-first",
        batch_id="arrived-first",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        created_ts_ns=NOW_NS + 10_000,
    )
    arrived_second = _request(
        _route(tmp_path),
        request_id="arrived-second",
        batch_id="arrived-second",
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
        created_ts_ns=NOW_NS - 1,
    )
    inbox.submit(arrived_first)
    inbox.submit(arrived_second)

    claimed = inbox.claim_next()

    assert claimed is not None and claimed[1].request_id == "arrived-first"
    arrival_rows = [json.loads(path.read_bytes()) for path in (inbox.root / "arrival").glob("*.json")]
    assert {row["request_id"]: row["arrival_sequence"] for row in arrival_rows} == {
        "arrived-first": 1,
        "arrived-second": 2,
    }


@pytest.mark.parametrize("entry_kind", [SleeveAdapterKind.LONG, SleeveAdapterKind.CONTINUOUS])
def test_risk_flat_supersedes_older_strategy_entry_for_same_component(
    tmp_path: Path,
    entry_kind: SleeveAdapterKind,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    target_key = f"{entry_kind.value}/main/BUSDT"
    entry = _request(
        _route(tmp_path),
        request_id=f"{entry_kind.value}-entry",
        batch_id=f"{entry_kind.value}-entry",
        kind=entry_kind,
        notional=20.0 if entry_kind is SleeveAdapterKind.LONG else -20.0,
        created_ts_ns=NOW_NS + 1_000,
        target_key=target_key,
    )
    risk_flat = _request(
        _route(tmp_path),
        request_id=f"{entry_kind.value}-risk-flat",
        batch_id=f"{entry_kind.value}-risk-flat",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        created_ts_ns=NOW_NS - 1,
        target_key=target_key,
    )
    inbox.submit(entry)
    inbox.submit(risk_flat)

    receipt = service.run_once(inbox)

    assert receipt is not None and receipt.request_id == risk_flat.request_id
    assert receipt.accepted
    assert receipt.command_ids == ()
    assert adapter.submit_calls == 0
    completed = [json.loads(path.read_bytes()) for path in (inbox.root / "completed").glob("*.json")]
    old = next(row for row in completed if row["request"]["request_id"] == entry.request_id)
    assert old["receipt"]["disposition"] == "superseded"
    assert old["receipt"]["superseded_by_request_id"] == risk_flat.request_id


def test_separate_safety_flats_collectively_supersede_atomic_multi_component_entry(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    first_key = "long/strategy/component-a/BUSDT"
    second_key = "long/strategy/component-b/BUSDT"
    first_entry = _request(
        _route(tmp_path),
        request_id="entry-a-source",
        batch_id="entry-a-source",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
        target_key=first_key,
    ).intents[0]
    second_entry = _request(
        _route(tmp_path),
        request_id="entry-b-source",
        batch_id="entry-b-source",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
        target_key=second_key,
    ).intents[0]
    stale_atomic_entry = AccountTargetRequest(
        request_id="stale-atomic-entry",
        batch_id="stale-atomic-entry",
        created_ts_ns=NOW_NS,
        route_id=_route(tmp_path).route_id,
        account_id=_route(tmp_path).account_id,
        environment=_route(tmp_path).environment,
        intents=(first_entry, second_entry),
    )
    flat_a = _request(
        _route(tmp_path),
        request_id="risk-flat-a",
        batch_id="risk-flat-a",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        created_ts_ns=NOW_NS + 1,
        target_key=first_key,
    )
    flat_b = _request(
        _route(tmp_path),
        request_id="risk-flat-b",
        batch_id="risk-flat-b",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        created_ts_ns=NOW_NS + 2,
        target_key=second_key,
    )
    inbox.submit(stale_atomic_entry)
    inbox.submit(flat_a)
    inbox.submit(flat_b)

    first = service.run_once(inbox)

    assert first is not None and first.request_id == flat_a.request_id
    assert adapter.submit_calls == 0
    completed = [json.loads(path.read_bytes()) for path in (inbox.root / "completed").glob("*.json")]
    stale = next(row for row in completed if row["request"]["request_id"] == stale_atomic_entry.request_id)
    assert stale["receipt"]["disposition"] == "superseded"
    assert stale["receipt"]["superseded_by_request_ids"] == [
        flat_a.request_id,
        flat_b.request_id,
    ]

    second = service.run_once(inbox)
    assert second is not None and second.request_id == flat_b.request_id
    assert adapter.submit_calls == 0
    assert service.kernel.state().positions == {}


def test_delayed_stale_reentry_cannot_reopen_after_newer_flat_revision(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    target_key = "long/strategy/component-a/BUSDT"
    opened = _request(
        _route(tmp_path),
        request_id="open-current",
        batch_id="open-current",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        created_ts_ns=NOW_NS + 100,
        target_key=target_key,
    )
    flattened = _request(
        _route(tmp_path),
        request_id="flat-newer",
        batch_id="flat-newer",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        created_ts_ns=NOW_NS + 300,
        target_key=target_key,
    )
    assert service.handle(opened).accepted
    assert service.handle(flattened).accepted
    assert service.kernel.state().positions["BUSDT"].signed_qty == 0.0
    assert adapter.submit_calls == 2

    delayed_stale = _request(
        _route(tmp_path),
        request_id="entry-generated-before-flat-but-delayed",
        batch_id="entry-generated-before-flat-but-delayed",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        created_ts_ns=NOW_NS + 200,
        target_key=target_key,
    )
    inbox.submit(delayed_stale)

    receipt = service.run_once(inbox)

    assert receipt is not None and not receipt.accepted
    assert any("stale_component_revision" in key for key in receipt.rejection_keys)
    assert adapter.submit_calls == 2
    assert service.kernel.state().positions["BUSDT"].signed_qty == 0.0
    desire = service.kernel.state().component_target_desires[target_key]
    assert desire["signed_qty"] == 0.0
    assert desire["metadata"]["account_request_id"] == flattened.request_id


def test_nonzero_revisions_stay_fifo_so_delayed_stale_resize_is_rejected(
    tmp_path: Path,
) -> None:
    adapter = CountingTwin()
    service = _service(tmp_path / "account", adapter)
    inbox = _inbox(tmp_path)
    target_key = "long/strategy/component-a/BUSDT"
    newer_small = _request(
        _route(tmp_path),
        request_id="newer-small-arrived-first",
        batch_id="newer-small-arrived-first",
        kind=SleeveAdapterKind.LONG,
        notional=10.0,
        created_ts_ns=NOW_NS + 200,
        target_key=target_key,
    )
    delayed_stale_large = _request(
        _route(tmp_path),
        request_id="stale-large-arrived-second",
        batch_id="stale-large-arrived-second",
        kind=SleeveAdapterKind.LONG,
        notional=50.0,
        created_ts_ns=NOW_NS + 100,
        target_key=target_key,
    )
    inbox.submit(newer_small)
    inbox.submit(delayed_stale_large)

    first = service.run_once(inbox)
    second = service.run_once(inbox)

    assert first is not None and first.request_id == newer_small.request_id
    assert first.accepted
    assert second is not None and second.request_id == delayed_stale_large.request_id
    assert not second.accepted
    assert any("stale_component_revision" in key for key in second.rejection_keys)
    assert adapter.submit_calls == 1
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(1.0)
    desire = service.kernel.state().component_target_desires[target_key]
    assert desire["metadata"]["account_request_id"] == newer_small.request_id


def test_atomic_request_rejects_duplicate_component_across_adapter_kinds(tmp_path: Path) -> None:
    target_key = "long/main/BUSDT"
    long_entry = _request(
        _route(tmp_path),
        request_id="long-source",
        batch_id="long-source",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        target_key=target_key,
    ).intents[0]
    risk_flat = _request(
        _route(tmp_path),
        request_id="risk-source",
        batch_id="risk-source",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        target_key=target_key,
    ).intents[0]

    with pytest.raises(ValueError, match="same component twice"):
        AccountTargetRequest(
            request_id="duplicate-component",
            batch_id="duplicate-component",
            created_ts_ns=NOW_NS,
            route_id=_route(tmp_path).route_id,
            account_id=_route(tmp_path).account_id,
            environment=_route(tmp_path).environment,
            intents=(long_entry, risk_flat),
        )


def test_risk_flat_is_not_superseded_by_genuinely_later_strategy_reentry(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    target_key = "long/main/BUSDT"
    risk_flat = _request(
        _route(tmp_path),
        request_id="risk-flat-first",
        batch_id="risk-flat-first",
        kind=SleeveAdapterKind.RISK,
        notional=0.0,
        created_ts_ns=NOW_NS + 10_000,
        target_key=target_key,
    )
    reentry = _request(
        _route(tmp_path),
        request_id="long-reentry-second",
        batch_id="long-reentry-second",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
        created_ts_ns=NOW_NS - 1,
        target_key=target_key,
    )
    inbox.submit(risk_flat)
    inbox.submit(reentry)

    assert inbox.claim_superseded() is None
    claimed = inbox.claim_next()
    assert claimed is not None and claimed[1].request_id == risk_flat.request_id


def test_inbox_fails_closed_when_pending_request_loses_arrival_sidecar(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    request = _request(
        _route(tmp_path),
        request_id="missing-arrival",
        batch_id="missing-arrival",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    inbox.submit(request)
    (inbox.root / "arrival" / inbox._filename(request.request_id)).unlink()

    with pytest.raises(RuntimeError, match="lacks a durable arrival sequence"):
        inbox.claim_next()


def test_durable_request_evidence_reads_one_regular_file_snapshot(
    tmp_path: Path,
) -> None:
    inbox = _inbox(tmp_path)
    request = _request(
        _route(tmp_path),
        request_id="durable-snapshot",
        batch_id="durable-snapshot",
        kind=SleeveAdapterKind.LONG,
        notional=20.0,
    )
    published = inbox.submit(request)

    evidence = inbox.require_durable_request(request)

    assert evidence.path == published.absolute()
    assert evidence.queue_state == "pending"
    assert evidence.arrival_sequence == 1

    symlink_target = tmp_path / "replacement-request.json"
    symlink_target.write_bytes(published.read_bytes())
    published.unlink()
    published.symlink_to(symlink_target)

    with pytest.raises(RuntimeError, match="is unreadable"):
        inbox.require_durable_request(request)


def test_queued_replacement_does_not_cross_an_outstanding_working_order(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("working")
    service = _service(tmp_path / "account", adapter, convergence_retry_backoff_ns=100)
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="working-entry",
            batch_id="working-entry",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
        )
    )
    opened = service.run_once(inbox)
    assert opened is not None and opened.accepted
    working = adapter.submissions[0]
    assert service.kernel.state().orders[working.command_id].status == "acknowledged"
    assert service.kernel.state().working_signed_qty("BUSDT") == pytest.approx(-2.0)

    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="replace-working-with-flat",
            batch_id="replace-working-with-flat",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=0.0,
            created_ts_ns=NOW_NS + 1,
        )
    )
    replaced = service.run_once(inbox)
    assert replaced is not None and replaced.accepted
    assert replaced.command_ids == ()
    assert adapter.submit_calls == 1
    assert len(service.kernel.state().orders) == 1
    assert not any(order.reduce_only for order in service.kernel.state().orders.values())
    assert service.kernel.state().aggregate_targets["BUSDT"] == 0.0
    assert service.kernel.state().working_signed_qty("BUSDT") == pytest.approx(-2.0)

    service.runtime.driver.ingest(
        (
            ExecutionObservation(
                observation_type=ExecutionObservationType.ORDER_STATUS,
                command_id=working.command_id,
                exchange_ts_ns=900_001_000,
                local_receive_ts_ns=1_000_001_000,
                status="cancelled",
                cumulative_filled_qty=0.0,
            ),
        )
    )
    assert service.kernel.state().working_signed_qty("BUSDT") == 0.0
    assert service.convergence_report().converged
    assert service.run_once(inbox) is None
    assert adapter.submit_calls == 1


def test_sub_minimum_residual_is_converged_within_venue_granularity(tmp_path: Path) -> None:
    """A partial terminal fill can leave dust no venue-admissible order can express
    (here 0.05 against a 0.1 qty step). Retrying an impossible order only exhausts
    and pages; the item must classify as converged and stay healthy without retries.
    """

    adapter = ScriptedExecutionAdapter("partial_cancel", partial_qty=1.95)
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_health_grace_ns=500,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="dust-entry",
            batch_id="dust-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None

    report = service.convergence_report()
    assert report.items
    item = report.items[0]
    assert item.position_signed_qty == pytest.approx(1.95)
    assert abs(item.residual_signed_qty) == pytest.approx(0.05)
    assert item.venue_minimum_dust
    assert item.status == "converged_within_venue_minimum"
    assert not item.retryable
    assert not item.exhausted
    assert report.healthy
    report.require_healthy()

    # Dust is not ageable work: far beyond the grace window it stays healthy
    # and convergence has nothing to retry.
    clock.advance_ns(1_000_000)
    later = service.convergence_report()
    assert later.healthy
    assert service.converge_once() is None


def test_expressible_residual_still_retries_and_can_exhaust(tmp_path: Path) -> None:
    adapter = ScriptedExecutionAdapter("partial_cancel", "reject", partial_qty=1.9)
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=100,
        convergence_health_grace_ns=500,
        max_convergence_retries=1,
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="expressible-entry",
            batch_id="expressible-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None

    report = service.convergence_report()
    assert report.items
    item = report.items[0]
    assert abs(item.residual_signed_qty) == pytest.approx(0.1)
    assert not item.venue_minimum_dust
    assert item.status in {"retry_due", "retry_backoff"}


# --- terminal retirement of expired entry requests (2026-08-01 outage) --------


def _submit_entry_request(
    tmp_path: Path,
    inbox: AccountIntentInbox,
    *,
    signal_valid_until_ms: int,
    notional: float = 20.0,
) -> str:
    target_key = "long/long-v1/signal-retire/BUSDT"
    metadata: dict[str, object] = {}
    if notional != 0.0:
        metadata = {
            "entry_attempt_key": f"entry-attempt/{target_key}",
            "signal_ts_ms": 500,
            "signal_valid_until_ms": signal_valid_until_ms,
        }
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="retire-1",
            batch_id="retire-1",
            kind=SleeveAdapterKind.LONG,
            notional=notional,
            target_key=target_key,
            decision_key="long-target/long-v1/1000/entry/signal-retire",
            metadata=metadata,
        )
    )
    return target_key


def _stale_command_raiser(request: AccountTargetRequest) -> None:
    raise StaleUnsubmittedExposureCommand(
        "refusing to submit a stale exposure-increasing command: command=test"
    )


def test_stale_command_failure_with_expired_entries_retires_request_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox(tmp_path)
    _submit_entry_request(tmp_path, inbox, signal_valid_until_ms=1_000)
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(tmp_path / "account", CountingTwin(), clock=clock)
    monkeypatch.setattr(service, "handle", _stale_command_raiser)

    with pytest.raises(StaleEntryRequestExpired, match="entry request retired"):
        service.run_once(inbox)

    assert list((inbox.root / "pending").glob("*.json")) == []
    assert list((inbox.root / "processing").glob("*.json")) == []
    failed = list((inbox.root / "failed").glob("*.json"))
    assert len(failed) == 1
    payload = json.loads(failed[0].read_bytes())
    assert payload["error_type"] == "StaleEntryRequestExpired"
    # The record names both the expiry rule and the original refusal.
    assert "signal_valid_until_ms" in payload["error"]
    assert "StaleUnsubmittedExposureCommand" in payload["error"]


def test_stale_command_failure_with_valid_signal_releases_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox(tmp_path)
    _submit_entry_request(tmp_path, inbox, signal_valid_until_ms=3_000)
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(tmp_path / "account", CountingTwin(), clock=clock)
    monkeypatch.setattr(service, "handle", _stale_command_raiser)

    with pytest.raises(StaleUnsubmittedExposureCommand):
        service.run_once(inbox)

    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []


def test_non_stale_failure_with_expired_entries_still_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-execution must keep resuming even past signal expiry: the
    batch may hold attempted commands whose venue state has to reconcile."""

    inbox = _inbox(tmp_path)
    _submit_entry_request(tmp_path, inbox, signal_valid_until_ms=1_000)
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(tmp_path / "account", CountingTwin(), clock=clock)

    def _crash(request: AccountTargetRequest) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(service, "handle", _crash)

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.run_once(inbox)

    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []


def test_a_head_request_cannot_retry_past_the_inbox_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-01 loop shape: a head that fails every pass retires to
    failed/ once the retry budget is spent, instead of blocking the queue
    forever. The producer's next cycle publishes a fresh request."""

    inbox = _inbox(tmp_path)
    _submit_entry_request(tmp_path, inbox, signal_valid_until_ms=10_000_000_000)
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(tmp_path / "account", CountingTwin(), clock=clock)

    def _always_fails(request: AccountTargetRequest) -> None:
        raise RuntimeError("simulated persistent failure")

    monkeypatch.setattr(service, "handle", _always_fails)

    with pytest.raises(RuntimeError, match="persistent failure"):
        service.run_once(inbox)
    # Inside the budget: released back to pending for retry.
    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []

    clock.advance_ns(service.inbox_retry_budget_ns + 1)
    with pytest.raises(RuntimeError, match="persistent failure"):
        service.run_once(inbox)

    assert list((inbox.root / "pending").glob("*.json")) == []
    failed = list((inbox.root / "failed").glob("*.json"))
    assert len(failed) == 1
    payload = json.loads(failed[0].read_bytes())
    assert payload["error_type"] == "RuntimeError"


def test_exit_request_failure_never_retires_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = _inbox(tmp_path)
    _submit_entry_request(tmp_path, inbox, signal_valid_until_ms=1_000, notional=0.0)
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    service = _service(tmp_path / "account", CountingTwin(), clock=clock)
    monkeypatch.setattr(service, "handle", _stale_command_raiser)

    with pytest.raises(StaleUnsubmittedExposureCommand):
        service.run_once(inbox)

    assert len(list((inbox.root / "pending").glob("*.json"))) == 1
    assert list((inbox.root / "failed").glob("*.json")) == []


def _submit_and_complete(inbox: AccountIntentInbox, route: AccountRoute, *, request_id: str) -> AccountTargetRequest:
    request = _request(
        route,
        request_id=request_id,
        batch_id=request_id,
        kind=SleeveAdapterKind.CONTINUOUS,
        notional=-20.0,
    )
    inbox.submit(request)
    claimed = inbox.claim_next()
    assert claimed is not None
    path, _claimed_request = claimed
    inbox.complete(
        path,
        AccountServiceReceipt(
            request_id=request.request_id,
            request_hash=request.content_hash(),
            batch_id=request.batch_id,
            accepted=True,
            rejection_keys=(),
            command_ids=(),
            execution_event_ids=(),
            final_state_hash="",
        ),
    )
    return request


def test_new_completed_requests_parses_each_completed_file_once(tmp_path: Path, monkeypatch) -> None:
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    cursor = CompletedRequestCursor()
    _submit_and_complete(inbox, route, request_id="inc-1")

    parses: list[str] = []
    original = AccountIntentInbox._parse_completed_request_locked

    def counting(self, path):  # type: ignore[no-untyped-def]
        parses.append(path.name)
        return original(self, path)

    monkeypatch.setattr(AccountIntentInbox, "_parse_completed_request_locked", counting)

    first = inbox.new_completed_requests(cursor)
    assert [request.request_id for request, _receipt in first] == ["inc-1"]
    assert inbox.new_completed_requests(cursor) == ()
    assert parses == [inbox._filename("inc-1")]

    _submit_and_complete(inbox, route, request_id="inc-2")
    second = inbox.new_completed_requests(cursor)
    assert [request.request_id for request, _receipt in second] == ["inc-2"]
    assert parses == [inbox._filename("inc-1"), inbox._filename("inc-2")]
    assert cursor.generation == 0


def test_new_completed_requests_restarts_after_an_epoch_reset(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    cursor = CompletedRequestCursor()
    _submit_and_complete(inbox, route, request_id="reset-1")
    assert len(inbox.new_completed_requests(cursor)) == 1

    for path in (inbox.root / "completed").glob("*.json"):
        path.unlink()
    assert inbox.new_completed_requests(cursor) == ()
    assert cursor.generation == 1

    _submit_and_complete(inbox, route, request_id="reset-2")
    rows = inbox.new_completed_requests(cursor)
    assert [request.request_id for request, _receipt in rows] == ["reset-2"]


class _MembershipOnlySet(set):
    """A batch set that records how the safety-flat claim reads it."""

    def __init__(self, values: Any) -> None:
        super().__init__(values)
        self.contains_calls = 0
        self.iterations = 0

    def __contains__(self, value: object) -> bool:
        self.contains_calls += 1
        return super().__contains__(value)

    def __iter__(self) -> Any:
        self.iterations += 1
        return super().__iter__()


def test_safety_flat_claim_reads_the_live_batch_set_without_copying_it(tmp_path: Path) -> None:
    service = _service(tmp_path / "account", CountingTwin())
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="queued-entry",
            batch_id="queued-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    probe = _MembershipOnlySet(service.kernel._state_ref().processed_batches)
    service.kernel._state_ref().processed_batches = probe

    # With no authorized flat there is nothing the scan could ever claim, so it
    # does not run at all: it used to take the inbox lock and re-parse every
    # pending request on each owner pass to prove that, while producers waited
    # on the same lock to publish.
    assert service.run_safety_flat_once(inbox) is None
    assert probe.contains_calls == 0
    assert probe.iterations == 0

    # When a flat really is authorized the scan runs, and membership is still
    # the whole contract: the owner hands over the committed set itself rather
    # than iterating a defensive copy once per tick.
    claimed = inbox.claim_next_safety_flat(
        processed_batches=probe,
        authorized_request_hashes={"queued-entry": "not-the-right-hash"},
    )
    assert claimed is None
    assert probe.contains_calls == 1
    assert probe.iterations == 0


def test_committing_a_batch_leaves_the_prior_processed_batch_set_untouched(tmp_path: Path) -> None:
    service = _service(tmp_path / "account", CountingTwin())
    inbox = _inbox(tmp_path)
    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="first-batch",
            batch_id="first-batch",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
        )
    )
    assert service.run_once(inbox) is not None
    before = service.kernel._state_ref().processed_batches
    snapshot = set(before)
    assert "first-batch" in snapshot

    inbox.submit(
        _request(
            _route(tmp_path),
            request_id="second-batch",
            batch_id="second-batch",
            kind=SleeveAdapterKind.LONG,
            notional=30.0,
        )
    )
    assert service.run_once(inbox) is not None
    after = service.kernel._state_ref().processed_batches

    assert "second-batch" in after
    # A commit publishes a new state object; a reader still holding the old set
    # sees exactly what it saw before.
    assert after is not before
    assert before == snapshot


class _SymbolMarketProvider:
    """Per-symbol books captured at the current clock, so they never go stale."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock

    def current(self, symbols: list[str], *, batch_id: str) -> dict[str, MarketInputRef]:
        assert batch_id
        local_ns = self.clock.wall_time_ns()
        return {
            symbol: MarketInputRef(
                input_key=f"book-{symbol}-{batch_id}",
                symbol=symbol,
                exchange_ts_ns=local_ns - 100_000_000,
                local_receive_ts_ns=local_ns,
                reference_price=10.0,
                bid_price=9.9,
                ask_price=10.1,
                book_sequence=1,
                source="test",
            )
            for symbol in symbols
        }


class _SymbolRulesProvider:
    def __init__(self, rules: dict[str, InstrumentRules]) -> None:
        self.rules = rules

    def current(self, symbols: list[str]) -> dict[str, InstrumentRules]:
        return {symbol: self.rules[symbol] for symbol in symbols}


def _symbol_rules(symbol: str) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        qty_step=0.1,
        min_qty=0.1,
        min_notional=1.0,
        tick_size=0.1,
        max_order_qty=100.0,
        max_leverage=20.0,
    )


def _symbol_request(
    route: AccountRoute,
    *,
    request_id: str,
    batch_id: str,
    kind: SleeveAdapterKind,
    notional: float,
    symbol: str,
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=request_id,
        batch_id=batch_id,
        created_ts_ns=NOW_NS,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(
            RequestedIntent(
                adapter_kind=kind,
                intent=SleeveTargetIntent(
                    decision_key=f"decision:{batch_id}",
                    target_key=f"{kind.value}/main/{symbol}",
                    strategy_id=f"{kind.value}-v1",
                    component_id="main",
                    symbol=symbol,
                    signed_notional_usdt=notional,
                    leverage=10.0,
                    reason="test",
                    metadata={},
                ),
            ),
        ),
    )


@dataclass(frozen=True)
class _WalkedAnchor:
    revision_sequence: int
    anchor_ns: int
    trading_only_ns: int
    prefix_hits: int


def _walked_retry_anchor(
    events: Any,
    state: Any,
    *,
    symbol: str,
    generation: str,
    desired_since_ns: int,
) -> _WalkedAnchor:
    """The pre-hoist per-symbol journal walk, rewritten straight from the rule.

    ``trading_only_ns`` drops the correlation-prefix half, so a test can show
    that half is doing work rather than agreeing by accident.
    """

    revision_sequence = max(
        (
            int(state.component_target_desire_sequences.get(target_key) or 0)
            for target_key, payload in state.component_target_desires.items()
            if str(payload.get("symbol") or "").upper() == symbol
        ),
        default=0,
    )
    prefix = f"account-convergence/{symbol}/{generation}/"
    anchor = desired_since_ns
    trading_only = desired_since_ns
    prefix_hits = 0
    for event in events:
        if event.sequence < revision_sequence:
            continue
        if event.symbol == symbol and event.event_type in {
            AccountEventType.ACK.value,
            AccountEventType.FILL.value,
            AccountEventType.ORDER_STATUS.value,
        }:
            anchor = max(anchor, event.wall_ts_ns)
            trading_only = max(trading_only, event.wall_ts_ns)
        if event.correlation_id.startswith(prefix):
            prefix_hits += 1
            anchor = max(anchor, event.wall_ts_ns)
    return _WalkedAnchor(revision_sequence, anchor, trading_only, prefix_hits)


def test_convergence_retry_anchors_match_the_per_symbol_journal_walk(tmp_path: Path) -> None:
    backoff_ns = 1_000_000
    cap_ns = 30_000_000
    adapter = ScriptedExecutionAdapter("reject", "partial_cancel", *["reject"] * 14)
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=backoff_ns,
        convergence_retry_backoff_cap_ns=cap_ns,
        max_convergence_retries=10,
    )
    service.market_provider = _SymbolMarketProvider(clock)
    service.rules_provider = _SymbolRulesProvider(
        {"BUSDT": _symbol_rules("BUSDT"), "CUSDT": _symbol_rules("CUSDT")}
    )
    inbox = _inbox(tmp_path)

    inbox.submit(
        _symbol_request(
            _route(tmp_path),
            request_id="b-entry",
            batch_id="b-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            symbol="BUSDT",
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(1_000)
    inbox.submit(
        _symbol_request(
            _route(tmp_path),
            request_id="c-entry",
            batch_id="c-entry",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
            symbol="CUSDT",
        )
    )
    assert service.run_once(inbox) is not None

    # Let both symbols retry at least once so the journal also carries events
    # whose correlation id is a convergence batch of its own.
    for _ in range(10):
        clock.advance_ns(4 * backoff_ns)
        service.converge_once()
        items = service.convergence_report().items
        if len(items) == 2 and all(item.retry_attempts >= 1 for item in items):
            break

    # A new desire revision for BUSDT starts a new generation, so the retries
    # of the old generation must stop counting towards the new backoff.
    clock.advance_ns(backoff_ns)
    inbox.submit(
        _symbol_request(
            _route(tmp_path),
            request_id="b-entry-2",
            batch_id="b-entry-2",
            kind=SleeveAdapterKind.LONG,
            notional=30.0,
            symbol="BUSDT",
        )
    )
    assert service.run_once(inbox) is not None

    # A retry the risk kernel refuses leaves convergence-batch events with no
    # ack behind them, which is what makes the correlation-prefix half of the
    # walk the newest thing the anchor can see.
    restored_policy = service.risk_policy
    service.risk_policy = replace(service.risk_policy, max_symbol_notional_usdt=0.5)
    clock.advance_ns(64 * backoff_ns)
    refused = service.converge_once()
    assert refused is not None and not refused.accepted

    # One more ordinary retry afterwards, so the newest trading event in the
    # journal belongs to a single symbol: the other symbol's anchor must not
    # move with it.
    service.risk_policy = restored_policy
    # Short enough that only the freshly revised symbol is due again.
    clock.advance_ns(2 * backoff_ns)
    retried = service.converge_once()
    assert retried is not None and retried.accepted and retried.commands
    assert retried.batch_id.split("/")[1] != refused.batch_id.split("/")[1]

    events, state = service.kernel._snapshot_ref()
    items = {item.symbol: item for item in service.convergence_report().items}
    assert set(items) == {"BUSDT", "CUSDT"}

    walked: dict[str, _WalkedAnchor] = {}
    for symbol, item in items.items():
        assert item.retryable and item.next_retry_ts_ns is not None
        walked[symbol] = _walked_retry_anchor(
            events,
            state,
            symbol=symbol,
            generation=item.generation,
            desired_since_ns=item.desired_since_ns,
        )
        delay = min(backoff_ns * (2 ** min(item.retry_attempts, 62)), cap_ns)
        assert item.next_retry_ts_ns == walked[symbol].anchor_ns + delay

    # The two symbols really are anchored from different revisions; the refused
    # symbol is anchored on a convergence batch rather than on a trading event;
    # BUSDT carries retries of a stale generation that its new generation must
    # not count; and CUSDT's walk crosses a fill and a terminal status as well
    # as acks.
    assert walked["BUSDT"].revision_sequence != walked["CUSDT"].revision_sequence
    assert walked["CUSDT"].prefix_hits >= 1
    refused_symbol = refused.batch_id.split("/")[1]
    assert walked[refused_symbol].anchor_ns > walked[refused_symbol].trading_only_ns
    stale_generation_batches = {
        batch_id
        for batch_id in state.processed_batches
        if batch_id.startswith("account-convergence/BUSDT/")
        and not batch_id.startswith(f"account-convergence/BUSDT/{items['BUSDT'].generation}/")
    }
    assert stale_generation_batches
    assert items["BUSDT"].retry_attempts < len(
        {
            batch_id
            for batch_id in state.processed_batches
            if batch_id.startswith("account-convergence/BUSDT/")
        }
    )
    newest_trading_ns = max(
        event.wall_ts_ns
        for event in events
        if event.event_type
        in {
            AccountEventType.ACK.value,
            AccountEventType.FILL.value,
            AccountEventType.ORDER_STATUS.value,
        }
    )
    assert any(walk.anchor_ns < newest_trading_ns for walk in walked.values())
    crossed = {
        event.event_type
        for event in events
        if event.symbol == "CUSDT" and event.sequence >= walked["CUSDT"].revision_sequence
    }
    assert {
        AccountEventType.ACK.value,
        AccountEventType.FILL.value,
        AccountEventType.ORDER_STATUS.value,
    } <= crossed


def test_the_convergence_scan_memo_returns_what_a_rescan_would_have_built(
    tmp_path: Path,
) -> None:
    backoff_ns = 1_000_000
    adapter = ScriptedExecutionAdapter("reject", "partial_cancel", *["reject"] * 14)
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    service = _service(
        tmp_path / "account",
        adapter,
        clock=clock,
        convergence_retry_backoff_ns=backoff_ns,
        convergence_retry_backoff_cap_ns=30_000_000,
        max_convergence_retries=10,
    )
    service.market_provider = _SymbolMarketProvider(clock)
    service.rules_provider = _SymbolRulesProvider(
        {"BUSDT": _symbol_rules("BUSDT"), "CUSDT": _symbol_rules("CUSDT")}
    )
    inbox = _inbox(tmp_path)
    inbox.submit(
        _symbol_request(
            _route(tmp_path),
            request_id="b-entry",
            batch_id="b-entry",
            kind=SleeveAdapterKind.LONG,
            notional=20.0,
            symbol="BUSDT",
        )
    )
    assert service.run_once(inbox) is not None
    clock.advance_ns(1_000)
    inbox.submit(
        _symbol_request(
            _route(tmp_path),
            request_id="c-entry",
            batch_id="c-entry",
            kind=SleeveAdapterKind.CONTINUOUS,
            notional=-20.0,
            symbol="CUSDT",
        )
    )
    assert service.run_once(inbox) is not None
    for _ in range(10):
        clock.advance_ns(4 * backoff_ns)
        service.converge_once()

    # The memo is an acceleration, so a warm read and a cold rescan have to
    # agree exactly: a plan that differed would change what the owner sends.
    warm = service._convergence_plans()
    assert service._convergence_scan_memo is not None
    key_before = service._convergence_scan_memo[0]
    service._convergence_scan_memo = None
    rescanned = service._convergence_plans()
    assert [dataclasses.asdict(plan.item) for plan in rescanned] == [dataclasses.asdict(plan.item) for plan in warm]
    assert [plan.targets for plan in rescanned] == [plan.targets for plan in warm]
    assert [plan.entry_unwind_eligible for plan in rescanned] == [
        plan.entry_unwind_eligible for plan in warm
    ]

    # A journal that moves on invalidates the key rather than serving a stale
    # walk.
    clock.advance_ns(4 * backoff_ns)
    service.converge_once()
    service._convergence_plans()
    assert service._convergence_scan_memo is not None
    assert service._convergence_scan_memo[0] != key_before


def test_resubmitting_a_retired_request_leaves_one_durable_file(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    route = _route(tmp_path)
    request = _request(
        route,
        request_id="retry-me",
        batch_id="retry-me",
        kind=SleeveAdapterKind.CARRY,
        notional=25.0,
    )
    path = inbox.submit(request)
    filename = path.name

    # Retire it the way the stale-entry path does.
    failed = inbox.root / "failed" / filename
    failed.parent.mkdir(parents=True, exist_ok=True)
    failed.write_bytes(json.dumps({"request": request.to_dict()}).encode())
    path.unlink()

    # Re-publishing must not leave the retired copy beside the new one: two
    # durable files made require_durable_request raise, and that raise came out
    # of the protection engine's loop and stopped stop-loss evaluation for
    # every component.
    inbox.submit(request)
    durable = [
        state
        for state in ("pending", "processing", "completed", "failed")
        if (inbox.root / state / filename).exists()
    ]
    assert durable == ["pending"]
    evidence = inbox.require_durable_request(request)
    assert evidence is not None
