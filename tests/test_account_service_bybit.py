from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.account_kernel import AccountState, InstrumentRules, OrderCommand, OrderState
from liquidity_migration.account_service_bybit import (
    BybitDemoAccountSnapshotProvider,
    CapturedBybitMarketProvider,
    CapturedPaperExecutionAdapter,
    VerifiedBybitDemoRulesProvider,
    instrument_rules_from_bybit_row,
    require_bybit_demo_order_ownership,
)
from liquidity_migration.account_service_runner import (
    load_demo_rules,
    load_risk_policy,
    require_order_submit_permission,
)
from liquidity_migration.deterministic_runtime import VirtualClock
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.execution_adapters import (
    ExecutionObservationType,
    ExecutionTwinConfig,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.market_capture import MarketCaptureConfig, SequenceAwareMarketRecorder


def _capture_config() -> MarketCaptureConfig:
    return MarketCaptureConfig(
        depth=50,
        segment_max_bytes=1_000_000,
        fsync_every_records=1,
        min_free_disk_bytes=1,
        ring_records_per_symbol=100,
    )


def test_captured_market_provider_links_decision_to_raw_book_record(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_010_000_000, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_capture_config(), clock=clock)
    recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_000,
            "cts": 1_799_999_999_999,
            "data": {
                "s": "BUSDT",
                "b": [["10.0", "2"]],
                "a": [["10.1", "3"]],
                "u": 100,
                "seq": 1_000,
            },
        },
        local_receive_ts_ns=1_800_000_000_005_000_000,
    )
    market = CapturedBybitMarketProvider(recorder).current(["BUSDT"], batch_id="batch-1")["BUSDT"]
    assert market.reference_price == pytest.approx(10.05)
    assert market.input_key == market.metadata["capture_record_id"]
    assert market.metadata["update_id"] == 100
    assert market.metadata["sequence_gap"] is False
    recorder.close()


def test_paper_adapter_executes_against_exact_captured_decision_book(tmp_path: Path) -> None:
    clock = VirtualClock(current_wall_ns=1_800_000_000_010_000_000, current_monotonic_ns=0)
    recorder = SequenceAwareMarketRecorder(tmp_path, config=_capture_config(), clock=clock)
    recorder.on_message(
        {
            "topic": "orderbook.50.BUSDT",
            "type": "snapshot",
            "ts": 1_800_000_000_000,
            "cts": 1_799_999_999_999,
            "data": {"s": "BUSDT", "b": [["10", "2"]], "a": [["10.1", "3"]], "u": 1, "seq": 1},
        },
        local_receive_ts_ns=1_800_000_000_005_000_000,
    )
    provider = CapturedBybitMarketProvider(recorder)
    market = provider.current(["BUSDT"], batch_id="paper-1")["BUSDT"]
    rules = {"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)}
    twin = MarketOrderExecutionTwin(
        books={},
        instrument_rules=rules,
        config=ExecutionTwinConfig(0.0, LatencyProfile(1, 1, 1), 10),
    )
    adapter = CapturedPaperExecutionAdapter(market_provider=provider, twin=twin)

    observations = list(
        adapter.submit(
            OrderCommand(
                command_id="cmd-1",
                batch_id="paper-1",
                symbol="BUSDT",
                side="Buy",
                qty=1.0,
                signed_qty=1.0,
                reduce_only=False,
                reference_price=market.reference_price,
                target_signed_qty=1.0,
                chunk_index=0,
                chunk_count=1,
            ),
            market,
        )
    )

    assert observations[0].observation_type == ExecutionObservationType.ACK
    assert observations[1].observation_type == ExecutionObservationType.FILL
    assert observations[1].price == 10.1
    recorder.close()


def test_demo_snapshot_provider_refuses_mainnet_and_reads_equity_available_margin() -> None:
    class Mainnet:
        demo = False

    with pytest.raises(ValueError, match="non-demo"):
        BybitDemoAccountSnapshotProvider(Mainnet())

    class Demo:
        demo = True

        def get_wallet_balance(self, **params: str):
            assert params == {"account_type": "UNIFIED", "coin": "USDT"}
            return {"list": [{"totalEquity": "100.5", "totalAvailableBalance": "80.25"}]}

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)
    snapshot = BybitDemoAccountSnapshotProvider(Demo(), clock=clock).current(batch_id="b1")
    assert snapshot.equity_usdt == 100.5
    assert snapshot.available_margin_usdt == 80.25
    assert snapshot.snapshot_ts_ns == 2_000_000_000
    assert snapshot.snapshot_key.startswith("bybit-demo:")


def test_account_owner_startup_requires_order_submit_permission() -> None:
    class Client:
        def __init__(self, metadata):
            self.metadata = metadata

        def get_api_key_information(self):
            return self.metadata

    require_order_submit_permission(
        Client(
            {
                "readOnly": 0,
                "permissions": {"ContractTrade": ["Order", "Position"]},
            }
        )
    )
    with pytest.raises(RuntimeError, match="readOnly=1"):
        require_order_submit_permission(
            Client(
                {
                    "readOnly": 1,
                    "permissions": {"ContractTrade": ["Order", "Position"]},
                }
            )
        )


class _StartupOpenOrderClient:
    demo = True

    def __init__(
        self,
        *,
        all_kinds: list[dict[str, Any]] | None = None,
        conditional: list[dict[str, Any]] | None = None,
        fail_query: str = "",
    ) -> None:
        self.all_kinds = list(all_kinds or [])
        self.conditional = list(conditional or [])
        self.fail_query = fail_query
        self.calls: list[dict[str, Any]] = []

    def get_open_orders(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(params))
        query = "conditional" if params.get("order_filter") == "StopOrder" else "all-kinds"
        if query == self.fail_query:
            raise RuntimeError(f"{query} unavailable")
        return list(self.conditional if query == "conditional" else self.all_kinds)


class _StaticKernel:
    def __init__(self, state: AccountState) -> None:
        self._state = state

    def state(self) -> AccountState:
        return self._state


@pytest.mark.parametrize("events_applied", [0, 1])
@pytest.mark.parametrize("conditional", [False, True])
def test_demo_owner_startup_rejects_flat_journal_stray_regular_or_conditional_order(
    events_applied: int,
    conditional: bool,
) -> None:
    row = {
        "symbol": "BUSDT",
        "orderId": "stray-1",
        "orderLinkId": "manual-order",
        "orderStatus": "Untriggered" if conditional else "New",
        "stopOrderType": "StopLoss" if conditional else "",
        "triggerPrice": "0.1" if conditional else "",
    }
    client = _StartupOpenOrderClient(
        all_kinds=[row],
        conditional=[row] if conditional else [],
    )

    with pytest.raises(RuntimeError, match="refused .*venue order"):
        require_bybit_demo_order_ownership(
            client=client,
            kernel=_StaticKernel(AccountState(events_applied=events_applied)),  # type: ignore[arg-type]
            # An empty journal must reject even if a verifier were accidentally
            # permissive; a non-empty flat journal has no native owner.
            native_order_verifier=(
                (lambda _row: True) if events_applied == 0 else (lambda _row: False)
            ),
        )

    assert client.calls == [
        {"settle_coin": "USDT"},
        {"settle_coin": "USDT", "order_filter": "StopOrder"},
    ]


def test_demo_owner_startup_accepts_clean_empty_venue_order_snapshot() -> None:
    client = _StartupOpenOrderClient()

    require_bybit_demo_order_ownership(
        client=client,
        kernel=_StaticKernel(AccountState()),  # type: ignore[arg-type]
    )

    assert client.calls == [
        {"settle_coin": "USDT"},
        {"settle_coin": "USDT", "order_filter": "StopOrder"},
    ]


def test_demo_owner_startup_accepts_exact_kernel_owned_restart_orders() -> None:
    state = AccountState(events_applied=5)
    commanded = OrderState(
        command_id="command-by-link",
        batch_id="batch-1",
        symbol="BUSDT",
        signed_qty=1.0,
        reduce_only=False,
    )
    acknowledged = OrderState(
        command_id="command-by-venue-id",
        batch_id="batch-2",
        symbol="BUSDT",
        signed_qty=-1.0,
        reduce_only=True,
        status="acknowledged",
        venue_order_id="venue-2",
    )
    state.orders = {
        commanded.command_id: commanded,
        acknowledged.command_id: acknowledged,
    }
    state.working_order_ids.update(state.orders)
    client = _StartupOpenOrderClient(all_kinds=[
        {
            "symbol": "BUSDT",
            "orderId": "venue-1",
            "orderLinkId": commanded.command_id,
            "orderStatus": "New",
        },
        {
            "symbol": "BUSDT",
            "orderId": acknowledged.venue_order_id,
            "orderLinkId": "",
            "orderStatus": "PartiallyFilled",
        },
    ])

    require_bybit_demo_order_ownership(
        client=client,
        kernel=_StaticKernel(state),  # type: ignore[arg-type]
    )


def test_demo_owner_startup_accepts_journal_verified_native_restart_order() -> None:
    state = AccountState(events_applied=5)
    row = {
        "symbol": "BUSDT",
        "orderId": "native-stop-1",
        "orderLinkId": "",
        "orderStatus": "Untriggered",
        "stopOrderType": "StopLoss",
        "triggerPrice": "0.1",
    }
    client = _StartupOpenOrderClient(all_kinds=[row], conditional=[row])
    verified: list[str] = []

    require_bybit_demo_order_ownership(
        client=client,
        kernel=_StaticKernel(state),  # type: ignore[arg-type]
        native_order_verifier=lambda order: not verified.append(str(order["orderId"])),
    )

    # The all-kinds and StopOrder duplicate is audited once by durable orderId.
    assert verified == ["native-stop-1"]


@pytest.mark.parametrize("fail_query", ["all-kinds", "conditional"])
def test_demo_owner_startup_fails_closed_when_either_order_query_fails(
    fail_query: str,
) -> None:
    client = _StartupOpenOrderClient(fail_query=fail_query)

    with pytest.raises(RuntimeError, match=rf"{fail_query} open-order query failed"):
        require_bybit_demo_order_ownership(
            client=client,
            kernel=_StaticKernel(AccountState()),  # type: ignore[arg-type]
        )


def test_demo_rule_provider_never_falls_back_to_unverified_public_minimums() -> None:
    row = {
        "symbol": "BUSDT",
        "lotSizeFilter": {
            "qtyStep": "1",
            "minOrderQty": "1",
            "minNotionalValue": "5",
            "maxMktOrderQty": "1000",
        },
        "priceFilter": {"tickSize": "0.0001"},
        "leverageFilter": {"maxLeverage": "25"},
    }
    public = instrument_rules_from_bybit_row(
        row,
        source="bybit_public_instruments",
        environment="mainnet_public",
        observed_ts_ns=1,
    )
    with pytest.raises(ValueError, match="not explicitly verified for demo"):
        VerifiedBybitDemoRulesProvider({"BUSDT": public})

    demo = InstrumentRules(
        symbol="BUSDT",
        qty_step=1.0,
        min_qty=1.0,
        min_notional=1.0,
        max_order_qty=1000.0,
        max_leverage=25.0,
        source="demo_order_probe",
        environment="demo",
        observed_ts_ns=2,
    )
    provider = VerifiedBybitDemoRulesProvider({"BUSDT": demo})
    assert provider.current(["BUSDT"])["BUSDT"].min_notional == 1.0
    with pytest.raises(RuntimeError, match="missing verified"):
        provider.current(["BUSDT", "BTCUSDT"])


def test_runner_loaders_require_explicit_demo_rules_and_absolute_risk_limits(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_payload: dict[str, Any] = {
        "schema_version": 2,
        "environment": "demo",
        "verified_ts_ns": 123,
        "rules": {
            "BUSDT": {
                "qty_step": 1,
                "min_qty": 1,
                "min_notional": 1,
                "tick_size": 0.0001,
                "max_order_qty": 1000,
                "max_leverage": 25,
                "source": "demo_order_probe",
                "environment": "demo",
                "observed_ts_ns": 123,
            }
        },
        "evidence": {
            "BUSDT": {
                "lowest_accepted_notional_usdt": 1,
                "attempts": [{"accepted": True, "qty": 1, "notional_usdt": 1}],
            }
        },
        "artifact_sha256": "",
    }
    rules_payload["artifact_sha256"] = hashlib.sha256(canonical_json(rules_payload)).hexdigest()
    rules_path.write_text(json.dumps(rules_payload))
    rules = load_demo_rules(rules_path)
    assert rules["BUSDT"].source == "demo_order_probe"
    assert rules["BUSDT"].environment == "demo"

    tampered = dict(rules_payload)
    tampered["verified_ts_ns"] = 124
    rules_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="artifact_sha256"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    with pytest.raises(ValueError, match="stale"):
        load_demo_rules(rules_path, now_ns=10_000_000_000, max_age_seconds=1.0)

    policy_path = tmp_path / "policy.json"
    policy_path.write_text("""{
      "max_component_gross_notional_usdt": 1000,
      "max_account_gross_notional_usdt": 500,
      "max_symbol_notional_usdt": 100,
      "max_initial_margin_usdt": 50,
      "max_leverage": 10
    }""")
    policy = load_risk_policy(policy_path)
    assert policy.max_leverage == 10.0
    assert policy.max_symbol_notional_usdt == 100.0
