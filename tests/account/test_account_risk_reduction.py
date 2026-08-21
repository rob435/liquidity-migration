from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account.account_kernel import (
    AccountEventType,
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
)
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import (
    AccountExecutionService,
    AccountTargetRequest,
    RequestedIntent,
    SleeveAdapterKind,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.account.execution_adapters import (
    ExecutionObservation,
    ExecutionObservationType,
)
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent


NOW_NS = 1_100_000_000


def _market(symbol: str, *, price: float) -> MarketInputRef:
    return MarketInputRef(
        input_key=f"book:{symbol}",
        symbol=symbol,
        exchange_ts_ns=900_000_000,
        local_receive_ts_ns=1_000_000_000,
        reference_price=price,
        bid_price=price * 0.99,
        ask_price=price * 1.01,
        book_sequence=1,
        source="risk-reduction-test",
    )


def _rule(symbol: str, *, min_qty: float = 0.1) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        qty_step=0.1,
        min_qty=min_qty,
        min_notional=1.0,
        max_order_qty=100.0,
        max_leverage=20.0,
    )


class MutableMarketProvider:
    def __init__(self) -> None:
        self.markets = {
            "BUSDT": _market("BUSDT", price=10.0),
            "ETHUSDT": _market("ETHUSDT", price=20.0),
        }
        self.missing: set[str] = set()
        self.stale: set[str] = set()
        self.requests: list[tuple[str, ...]] = []

    def current(self, symbols: list[str], *, batch_id: str) -> dict[str, MarketInputRef]:
        assert batch_id
        self.requests.append(tuple(symbols))
        output: dict[str, MarketInputRef] = {}
        for symbol in symbols:
            if symbol in self.missing:
                continue
            market = self.markets[symbol]
            output[symbol] = (
                replace(market, local_receive_ts_ns=1)
                if symbol in self.stale
                else market
            )
        return output


class MutableRulesProvider:
    def __init__(self) -> None:
        self.rules = {
            "BUSDT": _rule("BUSDT"),
            "ETHUSDT": _rule("ETHUSDT"),
        }
        self.missing: set[str] = set()
        self.requests: list[tuple[str, ...]] = []

    def current(self, symbols: list[str]) -> dict[str, InstrumentRules]:
        self.requests.append(tuple(symbols))
        return {
            symbol: self.rules[symbol]
            for symbol in symbols
            if symbol not in self.missing
        }


class MutableSnapshotProvider:
    def __init__(self) -> None:
        self.equity_usdt = 100.0
        self.available_margin_usdt = 100.0
        self.snapshot_ts_ns = 1_050_000_000
        self.raise_error = False

    def current(self, *, batch_id: str) -> AccountRiskSnapshot:
        if self.raise_error:
            raise RuntimeError("wallet unavailable")
        return AccountRiskSnapshot(
            equity_usdt=self.equity_usdt,
            available_margin_usdt=self.available_margin_usdt,
            snapshot_key=f"wallet:{batch_id}",
            snapshot_ts_ns=self.snapshot_ts_ns,
        )


class MutableHealthProvider:
    def __init__(self) -> None:
        self.blocked = False
        self.calls = 0
        self.position_blocked = False
        self.position_calls = 0

    def require_recent_healthy(self, *, max_age_ns: int) -> None:
        assert max_age_ns > 0
        self.calls += 1
        if self.blocked:
            raise RuntimeError("unrelated account health is stale")

    def require_recent_symbols_consistent(
        self,
        symbols: list[str],
        *,
        max_age_ns: int,
    ) -> None:
        assert symbols
        assert max_age_ns > 0
        self.position_calls += 1
        if self.position_blocked:
            raise RuntimeError(
                f"requested venue position truth contradicts reduction: {symbols[0]} venue flat"
            )


class ScriptedAdapter:
    name = "risk_reduction_script"

    def __init__(self, *outcomes: str) -> None:
        self.outcomes = list(outcomes)
        self.submissions: list[Any] = []

    def submit(self, command: Any, market_input: MarketInputRef):
        self.submissions.append(command)
        outcome = self.outcomes.pop(0)
        local_ns = NOW_NS + len(self.submissions)
        if outcome == "reject":
            return (ExecutionObservation(
                observation_type=ExecutionObservationType.ACK,
                command_id=command.command_id,
                exchange_ts_ns=local_ns,
                local_receive_ts_ns=local_ns,
                accepted=False,
                rejection_key=f"test:{command.command_id}:reject",
            ),)
        if outcome != "fill":
            raise AssertionError(f"unsupported outcome {outcome!r}")
        venue_order_id = f"venue:{command.command_id}"
        return (
            ExecutionObservation(
                observation_type=ExecutionObservationType.ACK,
                command_id=command.command_id,
                exchange_ts_ns=local_ns,
                local_receive_ts_ns=local_ns,
                accepted=True,
                venue_order_id=venue_order_id,
            ),
            ExecutionObservation(
                observation_type=ExecutionObservationType.FILL,
                command_id=command.command_id,
                exchange_ts_ns=local_ns + 1,
                local_receive_ts_ns=local_ns + 1,
                venue_order_id=venue_order_id,
                execution_id=f"fill:{command.command_id}",
                signed_qty=command.signed_qty,
                price=market_input.reference_price,
                fee_usdt=0.0,
            ),
            ExecutionObservation(
                observation_type=ExecutionObservationType.ORDER_STATUS,
                command_id=command.command_id,
                exchange_ts_ns=local_ns + 2,
                local_receive_ts_ns=local_ns + 2,
                status="filled",
                cumulative_filled_qty=command.qty,
            ),
        )


def _policy(*, tight: bool = False) -> AccountRiskPolicy:
    limit = 1.0 if tight else 1_000.0
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=limit,
        max_account_gross_notional_usdt=limit,
        max_initial_margin_usdt=0.1 if tight else 100.0,
        max_leverage=1.0 if tight else 10.0,
    )


def _request(
    route: AccountRoute,
    batch_id: str,
    *,
    symbol: str,
    notional: float,
    kind: SleeveAdapterKind = SleeveAdapterKind.CONTINUOUS,
    target_key: str | None = None,
) -> AccountTargetRequest:
    return AccountTargetRequest(
        request_id=batch_id,
        batch_id=batch_id,
        created_ts_ns=NOW_NS,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=(RequestedIntent(
            adapter_kind=kind,
            intent=SleeveTargetIntent(
                decision_key=f"decision:{batch_id}",
                target_key=target_key or f"continuous/main/{symbol}",
                strategy_id="risk-reduction-test",
                component_id="main",
                symbol=symbol,
                signed_notional_usdt=notional,
                leverage=10.0,
                reason="test",
            ),
        ),),
    )


def _service(
    root: Path,
    *,
    adapter: ScriptedAdapter,
) -> tuple[
    AccountExecutionService,
    MutableMarketProvider,
    MutableRulesProvider,
    MutableSnapshotProvider,
    MutableHealthProvider,
    VirtualClock,
]:
    route = ensure_account_route(
        account_id="risk-reduction-account",
        environment="demo",
        account_root=root,
        inbox_root=root.parent / "inbox",
    )
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    markets = MutableMarketProvider()
    rules = MutableRulesProvider()
    snapshot = MutableSnapshotProvider()
    health = MutableHealthProvider()
    service = AccountExecutionService(
        route=route,
        kernel=AccountExecutionKernel(
            route.account_path,
            account_id=route.account_id,
            clock=clock,
            id_seed="risk-reduction-test",
        ),
        market_provider=markets,
        snapshot_provider=snapshot,
        rules_provider=rules,
        risk_policy=_policy(),
        execution_adapter=adapter,
        clock=clock,
        health_provider=health,
        position_truth_provider=health,
        max_market_age_ns=200_000_000,
        max_snapshot_age_ns=200_000_000,
        convergence_retry_backoff_ns=100,
    )
    return service, markets, rules, snapshot, health, clock


def test_strict_reduction_never_bypasses_same_symbol_position_truth(
    tmp_path: Path,
) -> None:
    adapter = ScriptedAdapter("fill")
    service, _, _, _, health, _ = _service(
        tmp_path / "account",
        adapter=adapter,
    )
    assert service.handle(_request(service.route,"open", symbol="BUSDT", notional=-20.0)).accepted
    health.position_blocked = True

    with pytest.raises(RuntimeError, match="venue position truth contradicts reduction"):
        service.handle(_request(service.route,"close", symbol="BUSDT", notional=0.0))

    assert len(adapter.submissions) == 1
    assert health.position_calls == 1


@pytest.mark.parametrize("unrelated_book", ["missing", "stale"])
def test_normal_and_convergence_closes_ignore_only_unrelated_account_failures(
    tmp_path: Path,
    unrelated_book: str,
) -> None:
    adapter = ScriptedAdapter("fill", "fill", "reject", "fill")
    service, markets, rules, snapshot, health, clock = _service(
        tmp_path / "account",
        adapter=adapter,
    )
    assert service.handle(_request(service.route,"open-b", symbol="BUSDT", notional=-20.0)).accepted
    assert service.handle(_request(service.route,"open-eth", symbol="ETHUSDT", notional=-20.0)).accepted

    service.risk_policy = _policy(tight=True)
    snapshot.available_margin_usdt = -5.0
    health.blocked = True
    getattr(markets, unrelated_book).add("ETHUSDT")
    rules.missing.add("ETHUSDT")
    health_calls_before_close = health.calls
    market_calls_before_close = len(markets.requests)

    close = service.handle(_request(service.route,"close-b", symbol="BUSDT", notional=0.0))
    assert close.accepted
    rejected_close = adapter.submissions[-1]
    assert rejected_close.symbol == "BUSDT"
    assert rejected_close.reduce_only
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-2.0)
    assert health.calls == health_calls_before_close
    assert markets.requests[market_calls_before_close:] == [("BUSDT",)]
    risk_event = next(
        event
        for event in service.kernel.journal.events()
        if event.correlation_id == "close-b"
        and event.event_type == AccountEventType.RISK_DECISION.value
    )
    assert risk_event.payload["strictly_risk_reducing"] is True
    assert risk_event.payload["risk_evaluation_symbols"] == ["BUSDT"]

    clock.advance_ns(200)
    market_calls_before_retry = len(markets.requests)
    retry = service.converge_once()
    assert retry is not None and retry.accepted
    retried_close = adapter.submissions[-1]
    assert retried_close.batch_id.startswith("account-convergence/BUSDT/")
    assert retried_close.reduce_only
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(0.0)
    assert service.kernel.state().positions["ETHUSDT"].signed_qty == pytest.approx(-1.0)
    assert health.calls == health_calls_before_close
    assert markets.requests[market_calls_before_retry:] == [("BUSDT",)]


def test_low_margin_and_breached_caps_still_block_increase_and_sign_flip(
    tmp_path: Path,
) -> None:
    adapter = ScriptedAdapter("fill")
    service, _, _, snapshot, _, _ = _service(tmp_path / "account", adapter=adapter)
    assert service.handle(_request(service.route,"open", symbol="BUSDT", notional=-20.0)).accepted

    service.risk_policy = _policy(tight=True)
    snapshot.available_margin_usdt = -5.0
    increase = service.handle(_request(service.route,"increase", symbol="BUSDT", notional=-30.0))
    assert not increase.accepted
    assert any("negative_available_margin" in key for key in increase.rejection_keys)
    assert any("component_gross_limit" in key for key in increase.rejection_keys)
    assert len(adapter.submissions) == 1
    assert service.kernel.state().aggregate_targets["BUSDT"] == pytest.approx(-2.0)

    service.risk_policy = _policy()
    snapshot.available_margin_usdt = 100.0
    flip = service.handle(_request(service.route,
        "flip",
        symbol="BUSDT",
        notional=10.0,
        kind=SleeveAdapterKind.HEDGE,
        target_key="continuous/main/BUSDT",
    ))
    assert not flip.accepted
    assert flip.rejection_keys == (
        "account-risk:flip:sign_flip_requires_flat:BUSDT",
    )
    assert len(adapter.submissions) == 1


def test_offsetting_component_removal_never_turns_exit_into_same_side_increase(
    tmp_path: Path,
) -> None:
    adapter = ScriptedAdapter("fill", "fill")
    service, _, _, snapshot, _, clock = _service(
        tmp_path / "account",
        adapter=adapter,
    )
    assert service.handle(_request(
        service.route,
        "open-short",
        symbol="BUSDT",
        notional=-50.0,
        target_key="continuous/main/BUSDT",
    )).accepted
    assert service.handle(_request(
        service.route,
        "open-offsetting-long",
        symbol="BUSDT",
        notional=20.0,
        kind=SleeveAdapterKind.LONG,
        target_key="long/main/BUSDT",
    )).accepted
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-3.0)

    service.risk_policy = _policy(tight=True)
    snapshot.available_margin_usdt = -5.0
    submissions_before_exit = len(adapter.submissions)
    removed = service.handle(_request(
        service.route,
        "remove-offsetting-long",
        symbol="BUSDT",
        notional=0.0,
        kind=SleeveAdapterKind.LONG,
        target_key="long/main/BUSDT",
    ))

    assert removed.accepted
    assert removed.command_ids == ()
    assert len(adapter.submissions) == submissions_before_exit
    state = service.kernel.state()
    assert state.positions["BUSDT"].signed_qty == pytest.approx(-3.0)
    assert state.aggregate_targets["BUSDT"] == pytest.approx(-5.0)
    risk = state.risk_decisions["remove-offsetting-long"]
    assert risk["strictly_risk_reducing"] is True
    assert risk["staged_component_flat_symbols"] == ["BUSDT"]
    assert risk["staged_sign_flip_symbols"] == []

    clock.advance_ns(100)
    convergence = service.converge_once()
    assert convergence is not None and not convergence.accepted
    assert any(
        "negative_available_margin" in key
        for key in convergence.rejection_keys
    )
    assert any("component_gross_limit" in key for key in convergence.rejection_keys)
    assert len(adapter.submissions) == submissions_before_exit
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-3.0)


def test_exit_exemption_keeps_current_symbol_venue_quantity_rules(
    tmp_path: Path,
) -> None:
    adapter = ScriptedAdapter("fill")
    service, _, rules, snapshot, _, _ = _service(tmp_path / "account", adapter=adapter)
    assert service.handle(_request(service.route,"open", symbol="BUSDT", notional=-20.0)).accepted
    service.risk_policy = _policy(tight=True)
    snapshot.available_margin_usdt = -5.0
    rules.rules["BUSDT"] = _rule("BUSDT", min_qty=3.0)

    close = service.handle(_request(service.route,"too-small-close", symbol="BUSDT", notional=0.0))
    assert not close.accepted
    assert close.rejection_keys == (
        "account-risk:too-small-close:below_min_qty:BUSDT",
    )
    assert len(adapter.submissions) == 1
    assert service.kernel.state().positions["BUSDT"].signed_qty == pytest.approx(-2.0)


@pytest.mark.parametrize("snapshot_failure", ["raising", "stale"])
def test_close_uses_explicit_unavailable_capital_but_entry_requires_real_snapshot(
    tmp_path: Path,
    snapshot_failure: str,
) -> None:
    adapter = ScriptedAdapter("fill", "fill")
    service, _, _, snapshot, _, _ = _service(tmp_path / "account", adapter=adapter)
    assert service.handle(_request(service.route,"open", symbol="BUSDT", notional=-20.0)).accepted
    if snapshot_failure == "raising":
        snapshot.raise_error = True
    else:
        snapshot.snapshot_ts_ns = 1

    close = service.handle(_request(service.route,"close-without-capital", symbol="BUSDT", notional=0.0))
    assert close.accepted
    risk_event = next(
        event
        for event in service.kernel.journal.events()
        if event.correlation_id == "close-without-capital"
        and event.event_type == AccountEventType.RISK_DECISION.value
    )
    assert risk_event.payload["strictly_risk_reducing"] is True
    assert risk_event.payload["risk_snapshot_status"] == "unavailable_exit_only"
    assert risk_event.payload["risk_snapshot"]["equity_usdt"] == 0.0
    assert risk_event.payload["risk_snapshot"]["available_margin_usdt"] == 0.0
    assert risk_event.payload["risk_snapshot"]["snapshot_key"].startswith(
        "exit-only-capital-unavailable:"
    )

    expected = "wallet unavailable" if snapshot_failure == "raising" else "stale account snapshot"
    with pytest.raises(RuntimeError, match=expected):
        service.handle(_request(service.route,"blocked-reentry", symbol="BUSDT", notional=-10.0))
    assert len(adapter.submissions) == 2
    assert "blocked-reentry" not in service.kernel.state().processed_batches


@pytest.mark.parametrize("market_failure", ["stale", "sequence_gap"])
def test_requested_stale_book_allows_close_but_blocks_reentry(
    tmp_path: Path,
    market_failure: str,
) -> None:
    adapter = ScriptedAdapter("fill", "fill")
    service, markets, _, _, _, _ = _service(tmp_path / "account", adapter=adapter)
    assert service.handle(_request(service.route,"open", symbol="BUSDT", notional=-20.0)).accepted
    if market_failure == "stale":
        markets.stale.add("BUSDT")
        expected = "stale market input"
    else:
        markets.markets["BUSDT"] = replace(
            markets.markets["BUSDT"],
            metadata={"sequence_gap": True},
        )
        expected = "sequence gap"

    close = service.handle(_request(service.route,"close-on-stale-book", symbol="BUSDT", notional=0.0))
    assert close.accepted
    assert adapter.submissions[-1].reduce_only
    market_event = next(
        event
        for event in service.kernel.journal.events()
        if event.correlation_id == "close-on-stale-book"
        and event.event_type == AccountEventType.MARKET_INPUT_REF.value
    )
    assert str(market_event.payload["metadata"]["exit_only_preview_freshness"])

    with pytest.raises(RuntimeError, match=expected):
        service.handle(_request(service.route,"stale-book-reentry", symbol="BUSDT", notional=-10.0))
    assert len(adapter.submissions) == 2
    assert "stale-book-reentry" not in service.kernel.state().processed_batches


def test_kernel_rechecks_preview_only_inputs_before_any_entry(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=NOW_NS, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(
        tmp_path / "account",
        account_id="serialized-proof-account",
        clock=clock,
    )
    stale_market = replace(
        _market("BUSDT", price=10.0),
        metadata={"exit_only_preview_freshness": "stale_or_future"},
    )
    result = kernel.submit_targets(
        batch_id="must-not-enter",
        market_inputs=(stale_market,),
        targets=(DesiredTarget(
            decision_key="entry-decision",
            target_key="continuous/main/BUSDT",
            sleeve="continuous",
            strategy_id="serialized-proof-test",
            component_id="main",
            symbol="BUSDT",
            signed_qty=-1.0,
            reference_price=10.0,
            leverage=10.0,
            reason="test",
        ),),
        risk_snapshot=AccountRiskSnapshot(
            equity_usdt=0.0,
            available_margin_usdt=0.0,
            snapshot_key="exit-only-capital-unavailable:test",
            snapshot_ts_ns=NOW_NS,
        ),
        risk_policy=_policy(),
        instrument_rules={"BUSDT": _rule("BUSDT")},
        command_symbols={"BUSDT"},
        require_strict_risk_reduction=True,
    )
    assert not result.accepted
    assert "account-risk:must-not-enter:capital_snapshot_unavailable" in result.rejection_keys
    assert "account-risk:must-not-enter:market_input_not_fresh:BUSDT" in result.rejection_keys
    assert (
        "account-risk:must-not-enter:strict_risk_reduction_proof_failed"
        in result.rejection_keys
    )
    assert result.commands == ()
