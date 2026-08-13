from __future__ import annotations

import json

import pytest

from liquidity_migration.account.account_intent_client import (
    AccountTargetPublisher,
    component_target_key,
    publish_exit_first_target_requests,
    requested_target,
    unresolved_target_snapshot,
)
from liquidity_migration.account.account_service import AccountIntentInbox, SleeveAdapterKind
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.strategy_targets import component_target_intent


def _route(tmp_path) -> AccountRoute:
    return ensure_account_route(
        account_id="test-account",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _assert_strict_route_identity(published, route: AccountRoute) -> None:
    request = published.request
    request.require_route(route)
    assert request.route_id == route.route_id
    assert request.account_id == route.account_id
    assert request.environment == route.environment
    assert published.path.parent == route.inbox_path / "pending"


def _intent(*, notional: float = 125.0):
    target_key = component_target_key(
        sleeve=SleeveAdapterKind.HEDGE,
        strategy_id="continuous_beta_v2",
        component_id="btc",
        symbol="BTCUSDT",
    )
    return requested_target(
        adapter_kind=SleeveAdapterKind.HEDGE,
        decision_key="hedge-cycle/1700000000000/BTCUSDT",
        target_key=target_key,
        strategy_id="continuous_beta_v2",
        component_id="btc",
        symbol="BTCUSDT",
        signed_notional_usdt=notional,
        leverage=10.0,
        reason="scheduled_target_refresh",
    )


def test_publisher_writes_one_immutable_atomic_request(tmp_path) -> None:
    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    published = publisher.publish(
        batch_id="hedge-cycle/1700000000000",
        intents=(_intent(),),
        created_ts_ns=1_700_000_000_000_000_000,
    )

    envelope = json.loads(published.path.read_bytes())
    # The queued file carries the request and the arrival order together, so a
    # publish is one atomic replace and the two can never disagree.
    assert envelope["arrival_sequence"] == 1
    assert envelope["request_hash"] == published.request.content_hash()
    payload = envelope["request"]
    assert payload == published.request.to_dict()
    assert len(list((route.inbox_path / "pending").glob("*.json"))) == 1
    assert payload["route_id"] == route.route_id
    assert payload["account_id"] == route.account_id
    assert payload["environment"] == route.environment
    assert payload["intents"][0]["intent"]["target_key"] == ("hedge/continuous_beta_v2/btc/BTCUSDT")

    replay = publisher.publish(
        batch_id="hedge-cycle/1700000000000",
        intents=(_intent(),),
        created_ts_ns=1_700_000_000_000_000_000,
    )
    assert replay.request.request_id == published.request.request_id
    assert replay.path == published.path
    assert len(list((route.inbox_path / "pending").glob("*.json"))) == 1


def test_one_batch_can_atomically_carry_two_hedge_legs(tmp_path) -> None:
    route = _route(tmp_path)
    btc = _intent()
    eth_key = component_target_key(
        sleeve="hedge",
        strategy_id="continuous_beta_v2",
        component_id="eth",
        symbol="ETHUSDT",
    )
    eth = requested_target(
        adapter_kind="hedge",
        decision_key="hedge-cycle/1700000000000/ETHUSDT",
        target_key=eth_key,
        strategy_id="continuous_beta_v2",
        component_id="eth",
        symbol="ETHUSDT",
        signed_notional_usdt=75.0,
        leverage=10.0,
        reason="scheduled_target_refresh",
    )

    AccountTargetPublisher(route).publish(
        batch_id="hedge-cycle/1700000000000",
        intents=(btc, eth),
        created_ts_ns=1_700_000_000_000_000_000,
    )
    claim = AccountIntentInbox(route).claim_next()
    assert claim is not None
    _path, request = claim
    assert [row.intent.symbol for row in request.intents] == ["BTCUSDT", "ETHUSDT"]


def test_target_key_and_target_validation_fail_closed() -> None:
    with pytest.raises(ValueError, match="cannot contain"):
        component_target_key(
            sleeve="long",
            strategy_id="bad/id",
            component_id="trade",
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="finite"):
        requested_target(
            adapter_kind="long",
            decision_key="d",
            target_key="long/s/t/BTCUSDT",
            strategy_id="s",
            component_id="t",
            symbol="BTCUSDT",
            signed_notional_usdt=float("nan"),
            leverage=10.0,
            reason="entry",
        )


def _component_intent(
    *,
    component: str,
    action: str,
    notional: float,
    adapter: SleeveAdapterKind = SleeveAdapterKind.CONTINUOUS,
):
    metadata = (
        {"signal_ts_ms": 1_700_000_000_000, "signal_valid_until_ms": 1_700_003_600_000} if action == "entry" else {}
    )
    return component_target_intent(
        adapter_kind=adapter,
        action=action,
        decision_ts_ms=1_700_000_000_100,
        strategy_id="strategy-v1",
        component_id=component,
        symbol=f"{component.upper()}USDT",
        signed_notional_usdt=notional,
        leverage=10.0,
        reason="test",
        metadata=metadata,
    )


def test_unresolved_snapshot_covers_pending_and_processing_by_target_owner(tmp_path) -> None:
    publisher = AccountTargetPublisher(_route(tmp_path))
    entry = _component_intent(component="b", action="entry", notional=-10.0)
    publisher.publish(
        batch_id="continuous-target/entry/1",
        intents=(entry,),
        created_ts_ns=1_700_000_000_000_000_000,
    )

    pending = unresolved_target_snapshot(
        publisher.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert pending.target_keys == frozenset({entry.intent.target_key})
    assert pending.entry_request_count == 1

    assert publisher.inbox.claim_next() is not None
    processing = unresolved_target_snapshot(
        publisher.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert processing.target_keys == pending.target_keys
    assert processing.entry_request_count == 1


def test_risk_authored_flat_suppresses_its_continuous_target_key(tmp_path) -> None:
    target_key = component_target_key(
        sleeve=SleeveAdapterKind.CONTINUOUS,
        strategy_id="strategy-v1",
        component_id="b",
        symbol="BUSDT",
    )
    risk_exit = requested_target(
        adapter_kind=SleeveAdapterKind.RISK,
        decision_key="risk/flat/1",
        target_key=target_key,
        strategy_id="strategy-v1",
        component_id="b",
        symbol="BUSDT",
        signed_notional_usdt=0.0,
        leverage=1.0,
        reason="risk_flat",
    )
    publisher = AccountTargetPublisher(_route(tmp_path))
    publisher.publish(
        batch_id="risk-flat/1",
        intents=(risk_exit,),
        created_ts_ns=1_700_000_000_000_000_000,
    )

    snapshot = unresolved_target_snapshot(
        publisher.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert snapshot.target_keys == frozenset({target_key})
    assert snapshot.entry_request_count == 0


def test_grouped_exit_failure_falls_back_to_independent_exits_and_blocks_entry(tmp_path) -> None:
    route = _route(tmp_path)
    delegate = AccountTargetPublisher(route)
    exit_a = _component_intent(component="a", action="exit", notional=0.0)
    exit_b = _component_intent(component="b", action="exit", notional=0.0)
    entry = _component_intent(component="c", action="entry", notional=-10.0)

    class OneBadExitPublisher:
        def __init__(self) -> None:
            self.attempted: list[tuple[str, ...]] = []

        def publish(self, **kwargs):
            keys = tuple(item.intent.target_key for item in kwargs["intents"])
            self.attempted.append(keys)
            if exit_a.intent.target_key in keys:
                raise OSError("simulated exit transport failure")
            return delegate.publish(**kwargs)

    publisher = OneBadExitPublisher()
    result = publish_exit_first_target_requests(
        publisher,  # type: ignore[arg-type]
        batch_prefix="continuous-target/cycle-1",
        exit_intents=(exit_a, exit_b),
        entry_intents=(entry,),
        created_ts_ns=1_700_000_000_000_000_000,
        group_exits=True,
    )

    # Grouped attempt first, then the per-exit fallback in caller order: the
    # bad exit fails alone and the healthy one still reaches the inbox.
    assert publisher.attempted == [
        (exit_a.intent.target_key, exit_b.intent.target_key),
        (exit_a.intent.target_key,),
        (exit_b.intent.target_key,),
    ]
    assert result.exit_publication_mode == "independent"
    assert len(result.exit_requests) == 1
    assert result.exit_requests[0].request.intents[0] == exit_b
    assert result.entry_requests == ()
    assert [(error.stage, error.target_key) for error in result.errors] == [("exit", exit_a.intent.target_key)]
    queued = AccountIntentInbox(route).unresolved_requests()
    assert len(queued) == 1
    assert queued[0].intents == (exit_b,)


def test_exit_first_success_groups_exits_then_one_entry_request(tmp_path) -> None:
    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    exits = (
        _component_intent(component="a", action="exit", notional=0.0),
        _component_intent(component="b", action="exit", notional=0.0),
    )
    entries = (
        _component_intent(
            component="c",
            action="entry",
            notional=-10.0,
            adapter=SleeveAdapterKind.LONG,
        ),
        _component_intent(
            component="d",
            action="entry",
            notional=-20.0,
            adapter=SleeveAdapterKind.LONG,
        ),
    )
    created = 1_700_000_000_000_000_000
    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="continuous-target/cycle-2",
        exit_intents=exits,
        entry_intents=entries,
        created_ts_ns=created,
        group_exits=True,
    )

    # ONE all-flat request carries every exit of the cycle.
    assert result.exit_publication_mode == "grouped"
    assert len(result.exit_requests) == 1
    exit_request = result.exit_requests[0].request
    assert exit_request.intents == exits
    assert exit_request.batch_id == f"continuous-target/cycle-2/exit/{created}"
    assert exit_request.created_ts_ns == created
    assert len(result.entry_requests) == 1
    entry_request = result.entry_requests[0].request
    assert entry_request.intents == entries
    # Entries keep the deterministic created + len(exits) stamp, distinct
    # from the exit group's, so entry request ids do not depend on exit mode.
    assert entry_request.created_ts_ns == created + len(exits)
    assert result.entry_request_ids == (entry_request.request_id,)
    for published in (*result.exit_requests, *result.entry_requests):
        _assert_strict_route_identity(published, route)
    queued = publisher.inbox.unresolved_requests()
    assert sorted(len(request.intents) for request in queued) == [2, 2]
    # Arrival order: the exit group precedes the entry group.
    first_claim = publisher.inbox.claim_next()
    assert first_claim is not None
    assert first_claim[1].request_id == exit_request.request_id
    second_claim = publisher.inbox.claim_next()
    assert second_claim is not None
    assert second_claim[1].request_id == entry_request.request_id


def test_independent_entries_publish_one_request_each_in_caller_order(tmp_path) -> None:
    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    entries = (
        _component_intent(component="c", action="entry", notional=-30.0),
        _component_intent(component="a", action="entry", notional=-10.0),
        _component_intent(component="b", action="entry", notional=-20.0),
    )
    created = 1_700_000_000_000_000_000

    result = publish_exit_first_target_requests(
        publisher,
        batch_prefix="continuous-target/cycle-independent",
        exit_intents=(),
        entry_intents=entries,
        created_ts_ns=created,
        independent_entry_requests=True,
    )

    assert not result.errors
    assert len(result.entry_requests) == 3
    assert len(set(result.entry_request_ids)) == 3
    assert [item.request.intents for item in result.entry_requests] == [(intent,) for intent in entries]
    assert [item.request.created_ts_ns for item in result.entry_requests] == [
        created,
        created + 1,
        created + 2,
    ]
    assert [item.request.batch_id.split("/")[-2] for item in result.entry_requests] == [
        "0000",
        "0001",
        "0002",
    ]
    for published in result.entry_requests:
        _assert_strict_route_identity(published, route)
    replay = publish_exit_first_target_requests(
        publisher,
        batch_prefix="continuous-target/cycle-independent",
        exit_intents=(),
        entry_intents=entries,
        created_ts_ns=created,
        independent_entry_requests=True,
    )
    assert replay.entry_request_ids == result.entry_request_ids
    assert [item.path for item in replay.entry_requests] == [item.path for item in result.entry_requests]
    assert len(list((route.inbox_path / "pending").glob("*.json"))) == 3

    claimed_target_keys: list[str] = []
    while claim := publisher.inbox.claim_next():
        _path, request = claim
        assert len(request.intents) == 1
        claimed_target_keys.append(request.intents[0].intent.target_key)
    assert claimed_target_keys == [intent.intent.target_key for intent in entries]


def test_independent_entry_error_stops_then_retry_resumes_unresolved_keys(tmp_path) -> None:
    route = _route(tmp_path)
    entries = (
        _component_intent(component="a", action="entry", notional=-10.0),
        _component_intent(component="b", action="entry", notional=-20.0),
        _component_intent(component="c", action="entry", notional=-30.0),
    )

    class FailSecondEntryPublisher(AccountTargetPublisher):
        def __init__(self, account_route: AccountRoute) -> None:
            super().__init__(account_route)
            self.attempted: list[str] = []

        def publish(self, **kwargs):
            target_key = kwargs["intents"][0].intent.target_key
            self.attempted.append(target_key)
            if target_key == entries[1].intent.target_key:
                raise OSError("simulated entry transport failure")
            return super().publish(**kwargs)

    publisher = FailSecondEntryPublisher(route)
    first = publish_exit_first_target_requests(
        publisher,
        batch_prefix="continuous-target/partial",
        exit_intents=(),
        entry_intents=entries,
        created_ts_ns=1_700_000_000_000_000_000,
        independent_entry_requests=True,
    )

    assert publisher.attempted == [
        entries[0].intent.target_key,
        entries[1].intent.target_key,
    ]
    assert [item.request.intents[0].intent.target_key for item in first.entry_requests] == [
        entries[0].intent.target_key
    ]
    assert [(error.stage, error.target_key) for error in first.errors] == [("entry", entries[1].intent.target_key)]
    unresolved = unresolved_target_snapshot(
        publisher.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert unresolved.target_keys == frozenset({entries[0].intent.target_key})

    remaining = tuple(intent for intent in entries if intent.intent.target_key not in unresolved.target_keys)
    retry = publish_exit_first_target_requests(
        AccountTargetPublisher(route),
        batch_prefix="continuous-target/partial-retry",
        exit_intents=(),
        entry_intents=remaining,
        created_ts_ns=1_700_000_000_100_000_000,
        independent_entry_requests=True,
    )
    assert not retry.errors
    assert [item.request.intents[0].intent.target_key for item in retry.entry_requests] == [
        entries[1].intent.target_key,
        entries[2].intent.target_key,
    ]
    final = unresolved_target_snapshot(
        publisher.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert final.target_keys == frozenset(intent.intent.target_key for intent in entries)
    assert final.entry_request_count == 3


def test_independent_entry_crash_after_durable_publish_recovers_by_target_key(tmp_path) -> None:
    route = _route(tmp_path)
    entries = (
        _component_intent(component="a", action="entry", notional=-10.0),
        _component_intent(component="b", action="entry", notional=-20.0),
        _component_intent(component="c", action="entry", notional=-30.0),
    )

    class SimulatedProcessCrash(BaseException):
        pass

    class CrashAfterSecondPublish(AccountTargetPublisher):
        def __init__(self, account_route: AccountRoute) -> None:
            super().__init__(account_route)
            self.publish_count = 0

        def publish(self, **kwargs):
            published = super().publish(**kwargs)
            self.publish_count += 1
            if self.publish_count == 2:
                raise SimulatedProcessCrash
            return published

    crashing = CrashAfterSecondPublish(route)
    with pytest.raises(SimulatedProcessCrash):
        publish_exit_first_target_requests(
            crashing,
            batch_prefix="continuous-target/crash",
            exit_intents=(),
            entry_intents=entries,
            created_ts_ns=1_700_000_000_000_000_000,
            independent_entry_requests=True,
        )

    unresolved = unresolved_target_snapshot(
        crashing.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert unresolved.target_keys == frozenset({entries[0].intent.target_key, entries[1].intent.target_key})
    remaining = tuple(intent for intent in entries if intent.intent.target_key not in unresolved.target_keys)
    assert remaining == (entries[2],)

    recovered = publish_exit_first_target_requests(
        AccountTargetPublisher(route),
        batch_prefix="continuous-target/crash-retry",
        exit_intents=(),
        entry_intents=remaining,
        created_ts_ns=1_700_000_000_100_000_000,
        independent_entry_requests=True,
    )
    assert recovered.entry_request_ids == (recovered.entry_requests[0].request.request_id,)
    final = unresolved_target_snapshot(
        crashing.inbox,
        sleeve=SleeveAdapterKind.CONTINUOUS,
    )
    assert final.target_keys == frozenset(intent.intent.target_key for intent in entries)
