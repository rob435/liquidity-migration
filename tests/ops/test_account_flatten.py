from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.account.account_contracts import AccountState, PositionState
from liquidity_migration.account.account_route import AccountRoute, ensure_account_route
from liquidity_migration.account.account_service import SleeveAdapterKind
from liquidity_migration.ops.account_flatten import (
    FlattenRefused,
    build_flatten_plan,
    flatten_account,
    flatten_intents,
    observe_residual,
)


def _component(
    *,
    sleeve: str = "carry",
    strategy_id: str = "carry_hold_v3",
    component_id: str = "carry_hold",
    symbol: str = "BUSDT",
    signed_qty: float = 100.0,
    leverage: float = 2.0,
) -> tuple[str, dict[str, object]]:
    key = f"{sleeve}/{strategy_id}/{component_id}/{symbol}"
    return key, {
        "target_key": key,
        "sleeve": sleeve,
        "strategy_id": strategy_id,
        "component_id": component_id,
        "symbol": symbol,
        "signed_qty": signed_qty,
        "leverage": leverage,
    }


def _state(
    *,
    components: dict[str, dict[str, object]] | None = None,
    positions: dict[str, float] | None = None,
    aggregates: dict[str, float] | None = None,
    risk_decisions: dict[str, dict[str, object]] | None = None,
) -> AccountState:
    state = AccountState()
    state.component_targets.update(components or {})
    for symbol, qty in (positions or {}).items():
        state.positions[symbol] = PositionState(signed_qty=qty)
    state.aggregate_targets.update(aggregates or {})
    state.risk_decisions.update(risk_decisions or {})
    return state


def _plan(state: AccountState, **kwargs):
    return build_flatten_plan(
        state,
        route_id="route-1",
        environment="demo",
        account_id="acct",
        journal_sequence=7,
        **kwargs,
    )


def _route(tmp_path: Path, environment: str = "demo") -> AccountRoute:
    return ensure_account_route(
        account_id="flatten-account",
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


class TestPlan:
    def test_every_component_holding_exposure_is_planned(self) -> None:
        first_key, first = _component(symbol="BUSDT")
        second_key, second = _component(symbol="ESPUSDT", signed_qty=-50.0)
        plan = _plan(_state(components={first_key: first, second_key: second}))
        assert [item.symbol for item in plan.components] == ["BUSDT", "ESPUSDT"]
        assert [item.signed_qty for item in plan.components] == [100.0, -50.0]
        assert not plan.already_flat

    def test_a_component_already_at_zero_is_not_republished(self) -> None:
        key, payload = _component(signed_qty=0.0)
        plan = _plan(_state(components={key: payload}))
        assert plan.components == ()
        assert plan.already_flat

    def test_exposure_with_no_component_owner_is_reported_as_an_orphan(self) -> None:
        """The owner converges orphans by itself; flatten only has to say so."""

        plan = _plan(_state(positions={"TLMUSDT": 42.0}))
        assert plan.components == ()
        assert plan.orphan_positions == (("TLMUSDT", 42.0),)
        assert not plan.already_flat

    def test_a_position_its_component_still_owns_is_not_also_an_orphan(self) -> None:
        key, payload = _component(symbol="BUSDT")
        plan = _plan(_state(components={key: payload}, positions={"BUSDT": 100.0}))
        assert plan.orphan_positions == ()
        assert len(plan.components) == 1

    def test_filters_narrow_the_plan_and_record_what_they_left_behind(self) -> None:
        first_key, first = _component(symbol="BUSDT")
        second_key, second = _component(symbol="ESPUSDT", sleeve="long")
        state = _state(components={first_key: first, second_key: second})

        by_symbol = _plan(state, symbols=["busdt"])
        assert [item.symbol for item in by_symbol.components] == ["BUSDT"]
        assert [item.symbol for item in by_symbol.skipped_components] == ["ESPUSDT"]

        by_sleeve = _plan(state, sleeves=["long"])
        assert [item.symbol for item in by_sleeve.components] == ["ESPUSDT"]
        assert [item.symbol for item in by_sleeve.skipped_components] == ["BUSDT"]

    def test_a_component_without_a_symbol_refuses_rather_than_being_skipped(self) -> None:
        state = _state(components={"carry/s/c/": {"signed_qty": 1.0, "symbol": ""}})
        with pytest.raises(FlattenRefused, match="no symbol"):
            _plan(state)

    def test_a_non_finite_quantity_refuses_rather_than_reading_as_flat(self) -> None:
        key, payload = _component()
        payload["signed_qty"] = float("nan")
        with pytest.raises(FlattenRefused, match="non-finite"):
            _plan(_state(components={key: payload}))


class TestIntents:
    def test_each_intent_is_a_zero_replacement_for_its_own_component(self) -> None:
        key, payload = _component()
        intents = flatten_intents(_plan(_state(components={key: payload})), operator="op", reason="why")
        assert len(intents) == 1
        requested = intents[0]
        assert requested.adapter_kind is SleeveAdapterKind.RISK
        assert requested.intent.target_key == key
        assert requested.intent.symbol == "BUSDT"
        assert requested.intent.signed_notional_usdt == 0.0

    def test_the_owning_sleeve_travels_with_the_intent(self) -> None:
        """``RiskTargetAdapter`` refuses an intent that cannot name its owner."""

        key, payload = _component(sleeve="long")
        intents = flatten_intents(_plan(_state(components={key: payload})), operator="op", reason="why")
        assert intents[0].intent.metadata["owner_sleeve"] == "long"
        assert intents[0].intent.metadata["prior_signed_qty"] == 100.0

    @pytest.mark.parametrize("sleeve", ("carry", "long", "continuous", "hedge"))
    def test_the_real_risk_adapter_accepts_the_intent_and_restores_its_sleeve(
        self, sleeve: str
    ) -> None:
        """The join that matters: if the owner's adapter refused these, flatten
        would publish happily and change nothing.

        ``RiskTargetAdapter`` rewrites the sleeve back to the owning one, so the
        zero replaces the original component instead of adding a second owner
        beside it -- including for the long-only and short-only sleeves, whose
        own adapters would refuse to author a target for the other side.
        """

        from liquidity_migration.account.account_contracts import (
            InstrumentRules,
            MarketInputRef,
        )
        from liquidity_migration.account.strategy_runtime import RiskTargetAdapter

        key, payload = _component(sleeve=sleeve)
        intents = flatten_intents(
            _plan(_state(components={key: payload})), operator="op", reason="why"
        )
        target = RiskTargetAdapter().desired_target(
            intents[0].intent,
            MarketInputRef(
                input_key="k",
                symbol="BUSDT",
                reference_price=1.5,
                exchange_ts_ns=1,
                local_receive_ts_ns=2,
            ),
            InstrumentRules(
                symbol="BUSDT",
                qty_step=0.1,
                min_qty=0.1,
                min_notional=1.0,
                tick_size=0.1,
            ),
        )
        assert target.sleeve == sleeve, "the zero must replace the component that opened it"
        assert target.signed_qty == 0.0
        assert target.target_key == key
        assert target.metadata["requested_by"] == "account_risk"

    def test_operator_and_reason_are_required(self) -> None:
        key, payload = _component()
        plan = _plan(_state(components={key: payload}))
        with pytest.raises(ValueError, match="operator is required"):
            flatten_intents(plan, operator="  ", reason="why")
        with pytest.raises(ValueError, match="reason is required"):
            flatten_intents(plan, operator="op", reason="")


class TestResidual:
    def test_flat_needs_positions_targets_and_working_orders_all_empty(self) -> None:
        assert observe_residual(_state(), journal_sequence=1).flat
        assert not observe_residual(_state(positions={"BUSDT": 1.0}), journal_sequence=1).flat
        assert not observe_residual(_state(aggregates={"BUSDT": 1.0}), journal_sequence=1).flat

    def test_a_producer_republishing_a_planned_component_is_named(self) -> None:
        key, payload = _component()
        residual = observe_residual(
            _state(components={key: payload}),
            journal_sequence=1,
            planned_target_keys=[key],
        )
        assert residual.republished_target_keys == (key,)

    def test_dust_is_read_from_the_kernels_own_rejection(self) -> None:
        residual = observe_residual(
            _state(
                positions={"BUSDT": 0.05},
                risk_decisions={
                    "b1": {"rejection_keys": ["account-risk:b1:below_min_qty:BUSDT"]}
                },
            ),
            journal_sequence=1,
        )
        assert residual.dust_symbols == ("BUSDT",)
        assert residual.dust_limited
        assert not residual.flat

    def test_a_rejection_from_before_the_flatten_is_not_present_dust(self) -> None:
        """An old rejection against a since-resized position proves nothing now."""

        residual = observe_residual(
            _state(
                positions={"BUSDT": 500.0},
                risk_decisions={
                    "old": {"rejection_keys": ["account-risk:old:below_min_qty:BUSDT"]}
                },
            ),
            journal_sequence=1,
            baseline_batch_ids=frozenset({"old"}),
        )
        assert residual.dust_symbols == ()
        assert not residual.dust_limited

    def test_a_working_order_is_never_dust_limited(self) -> None:
        """Something is still in flight, so the residual is not yet terminal."""

        residual = observe_residual(
            _state(
                positions={"BUSDT": 0.05},
                aggregates={"BUSDT": 0.05},
                risk_decisions={
                    "b1": {"rejection_keys": ["account-risk:b1:below_min_qty:BUSDT"]}
                },
            ),
            journal_sequence=1,
        )
        assert not residual.dust_limited


class TestFlattenAccount:
    def test_an_empty_account_reports_already_flat_and_publishes_nothing(
        self, tmp_path: Path
    ) -> None:
        route = _route(tmp_path)
        outcome = flatten_account(
            account_root=route.account_root,
            inbox_root=route.inbox_root,
            environment="demo",
            account_id=route.account_id,
            operator="op",
            reason="test",
            execute=True,
        )
        assert outcome.status == "already_flat"
        assert outcome.ok
        assert not outcome.executed
        assert list((route.inbox_path / "pending").glob("*.json")) == []

    def test_the_environment_must_be_named_and_is_never_guessed(self, tmp_path: Path) -> None:
        route = _route(tmp_path)
        with pytest.raises(ValueError, match="explicitly set"):
            flatten_account(
                account_root=route.account_root,
                inbox_root=route.inbox_root,
                environment="",
                account_id=route.account_id,
                operator="op",
                reason="test",
            )

    def test_a_dry_run_reports_the_plan_without_touching_the_inbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        route = _route(tmp_path)
        key, payload = _component()
        _patch_journal(monkeypatch, _state(components={key: payload}, positions={"BUSDT": 100.0}))

        outcome = flatten_account(
            account_root=route.account_root,
            inbox_root=route.inbox_root,
            environment="demo",
            account_id=route.account_id,
            operator="op",
            reason="test",
            execute=False,
        )
        assert outcome.status == "planned"
        assert not outcome.executed
        assert [item.symbol for item in outcome.plan.components] == ["BUSDT"]
        assert list((route.inbox_path / "pending").glob("*.json")) == []

    def test_execute_queues_one_zero_target_per_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        route = _route(tmp_path)
        first_key, first = _component(symbol="BUSDT")
        second_key, second = _component(symbol="ESPUSDT", signed_qty=-5.0)
        _patch_journal(
            monkeypatch,
            _state(
                components={first_key: first, second_key: second},
                positions={"BUSDT": 100.0, "ESPUSDT": -5.0},
            ),
        )

        outcome = flatten_account(
            account_root=route.account_root,
            inbox_root=route.inbox_root,
            environment="demo",
            account_id=route.account_id,
            operator="op",
            reason="test",
            execute=True,
            wait_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        # No owner is running, so it cannot converge; the point is what it queued.
        assert outcome.executed
        assert outcome.status == "timed_out"
        # One request PER zero target: per-exit independence is the point of
        # this path — one symbol whose market data went missing must wedge
        # only its own request while every other close still goes out.
        assert len(outcome.published_request_ids) == 2

        queued = [
            json.loads(path.read_bytes())
            for path in sorted((route.inbox_path / "pending").glob("*.json"))
        ]
        assert len(queued) == 2
        assert all(len(request["intents"]) == 1 for request in queued)
        for request in queued:
            assert request["environment"] == "demo"
            for intent in request["intents"]:
                assert intent["adapter_kind"] == "risk"
                assert intent["intent"]["signed_notional_usdt"] == 0.0

    def test_a_timeout_names_what_is_still_standing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        route = _route(tmp_path)
        key, payload = _component()
        _patch_journal(monkeypatch, _state(components={key: payload}, positions={"BUSDT": 100.0}))

        outcome = flatten_account(
            account_root=route.account_root,
            inbox_root=route.inbox_root,
            environment="demo",
            account_id=route.account_id,
            operator="op",
            reason="test",
            execute=True,
            wait_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        assert outcome.status == "timed_out"
        assert not outcome.ok
        assert "positions remain" in outcome.detail
        assert "BUSDT" in outcome.detail

    def test_convergence_to_flat_is_reported_as_flat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        route = _route(tmp_path)
        key, payload = _component()
        states = iter(
            [
                _state(components={key: payload}, positions={"BUSDT": 100.0}),
                _state(),
            ]
        )
        _patch_journal(monkeypatch, states)

        outcome = flatten_account(
            account_root=route.account_root,
            inbox_root=route.inbox_root,
            environment="demo",
            account_id=route.account_id,
            operator="op",
            reason="test",
            execute=True,
            sleep=lambda _seconds: None,
        )
        assert outcome.status == "flat"
        assert outcome.ok
        assert outcome.residual is not None and outcome.residual.flat


def _patch_journal(monkeypatch: pytest.MonkeyPatch, states) -> None:
    """Drive the flatten's journal reads from a fixed sequence of states.

    The last state repeats, so a poll loop that never converges keeps seeing the
    same residual instead of running off the end of the iterator.
    """

    from liquidity_migration.ops import account_flatten as module

    class _Digest:
        def __init__(self, state: AccountState) -> None:
            self.state = state
            self.events: tuple[object, ...] = ()

    single = isinstance(states, AccountState)
    iterator = None if single else iter(states)
    last = states if single else None

    class _Cursor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def read(self, _root: Path) -> _Digest:
            nonlocal last
            if iterator is not None:
                last = next(iterator, last)
            assert last is not None
            return _Digest(last)

    monkeypatch.setattr(module, "AccountJournalCursor", _Cursor)


class TestLossCeilingFlatten:
    """The ceiling's job is to close the book, not to log that it should."""

    def test_a_trip_publishes_a_zero_target_for_every_open_component(
        self, tmp_path: Path
    ) -> None:
        from liquidity_migration.runtime.account_service_runner import (
            publish_loss_ceiling_flatten,
        )

        route = _route(tmp_path)
        first_key, first = _component(symbol="BUSDT")
        second_key, second = _component(symbol="ESPUSDT", signed_qty=-50.0)
        state = _state(components={first_key: first, second_key: second})

        class FakeKernel:
            def _state_ref(self):
                return state

        flatten = publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_000,
        )
        assert len(flatten.published_request_ids) >= 1
        assert flatten.unpublished_target_keys == ()
        assert {key.split("/")[-1] for key in flatten.planned_target_keys} == {
            "BUSDT",
            "ESPUSDT",
        }

        # The zero targets are really in the owner's inbox, which is what the
        # old code never managed: it called a path that could only claim a
        # native-stop breach and published nothing at all.
        queued = list((Path(route.inbox_root) / "pending").glob("*.json"))
        assert queued
        blob = "".join(path.read_text() for path in queued)
        assert "BUSDT" in blob and "ESPUSDT" in blob

    def test_a_flat_book_publishes_nothing(self, tmp_path: Path) -> None:
        from liquidity_migration.runtime.account_service_runner import (
            publish_loss_ceiling_flatten,
        )

        route = _route(tmp_path)
        state = _state()

        class FakeKernel:
            def _state_ref(self):
                return state

        flatten = publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_000,
        )
        assert flatten.planned_target_keys == frozenset()
        assert flatten.published_request_ids == ()
        assert flatten.unpublished_target_keys == ()

    def test_exposure_opened_after_the_trip_is_closed_too(self, tmp_path: Path) -> None:
        """The ceiling must not latch on "published once".

        A producer reads owner health once at the top of its cycle and then
        spends minutes on venue reads before publishing, so a cycle that began
        before the trip can still add exposure after the flatten. A one-shot
        flatten would leave that position open for the rest of a day whose
        ceiling is already breached.
        """

        from liquidity_migration.runtime.account_service_runner import (
            publish_loss_ceiling_flatten,
        )

        route = _route(tmp_path)
        first_key, first = _component(symbol="BUSDT")
        state = _state(components={first_key: first})

        class FakeKernel:
            def _state_ref(self):
                return state

        first_pass = publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_000,
        )
        assert len(first_pass.published_request_ids) == 1

        # Same open set on the next pass: the book is still converging, so
        # nothing is published again and the inbox is not flooded.
        quiet = publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_001,
            already_published=first_pass.planned_target_keys,
        )
        assert quiet.published_request_ids == ()
        assert quiet.planned_target_keys == first_pass.planned_target_keys

        # A straddling producer opens a second name after the trip. The open set
        # changed, so this one gets a zero target too.
        second_key, second = _component(symbol="ESPUSDT", signed_qty=-50.0)
        state.component_targets[second_key] = second
        after = publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_002,
            already_published=first_pass.planned_target_keys,
        )
        # The changed open set republishes BOTH zero targets, one request
        # each — per-exit independence on the loss-guard path.
        assert len(after.published_request_ids) == 2
        assert after.planned_target_keys == {first_key, second_key}
        queued = "".join(
            path.read_text() for path in (Path(route.inbox_root) / "pending").glob("*.json")
        )
        assert "ESPUSDT" in queued

    def test_a_component_that_will_not_publish_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A partial publication must not let the caller latch.

        Each exit is published independently, so one refusal leaves the rest of
        the book open. The caller latches on an empty second element, so
        reporting the refusal is what makes the next pass retry.
        """

        from liquidity_migration.account.account_intent_client import (
            AccountTargetPublisher,
        )
        from liquidity_migration.runtime import account_service_runner as module

        route = _route(tmp_path)
        first_key, first = _component(symbol="BUSDT")
        second_key, second = _component(symbol="ESPUSDT", signed_qty=-50.0)
        state = _state(components={first_key: first, second_key: second})

        class FakeKernel:
            def _state_ref(self):
                return state

        class RefusingPublisher:
            def __init__(self, target_route: AccountRoute) -> None:
                self._inner = AccountTargetPublisher(target_route)

            def publish(self, *, batch_id, intents, created_ts_ns):
                if any("ESPUSDT" in str(item.intent.target_key) for item in intents):
                    raise RuntimeError("inbox refused the zero target")
                return self._inner.publish(
                    batch_id=batch_id, intents=intents, created_ts_ns=created_ts_ns
                )

        monkeypatch.setattr(module, "AccountTargetPublisher", RefusingPublisher)

        flatten = module.publish_loss_ceiling_flatten(
            kernel=FakeKernel(),
            route=route,
            reason="account loss ceiling: test",
            now_ns=1_770_000_000_000_000_000,
        )
        assert len(flatten.published_request_ids) == 1
        assert [key.split("/")[-1] for key in flatten.unpublished_target_keys] == ["ESPUSDT"]
