from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.runtime.account_service_runner as account_service_runner_module
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountState,
    InstrumentRules,
    MarketInputRef,
    OrderCommand,
    OrderState,
)
from liquidity_migration.policy.account_execution_config import load_demo_rules, load_risk_policy
from liquidity_migration.venue.account_service_bybit import (
    BybitAccountSnapshotProvider,
    CapturedBybitMarketProvider,
    VerifiedBybitDemoRulesProvider,
    inspect_bybit_order_ownership,
    instrument_rules_from_bybit_row,
    require_bybit_order_ownership,
)
from liquidity_migration.venue.account_reconcile import (
    BybitAccountFundingReconciler,
    BybitAccountReconciler,
)
from liquidity_migration.runtime.account_service_runner import (
    require_order_submit_permission,
    require_startup_reconciliation_safe,
)
from liquidity_migration.venue.bybit_execution_adapter import BybitDemoExecutionAdapter
from liquidity_migration.venue.venue_protection import BybitNativeProtectionManager
from liquidity_migration.core.deterministic_runtime import VirtualClock
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.venue_realm import VenueRealm
from liquidity_migration.venue.demo_rule_probe import (
    DEMO_RULE_PROBE_EVIDENCE_KIND,
    DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    ORDER_CANCEL_SOURCE,
    ORDER_CREATE_SOURCE,
    ORDER_HISTORY_SOURCE,
    TRADE_HISTORY_SOURCE,
)
from liquidity_migration.account.execution_adapters import (
    INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE,
    BookLevel,
    ExecutionObservationType,
    ExecutionTwinConfig,
    L2BookSnapshot,
    LatencyProfile,
    MarketOrderExecutionTwin,
)
from liquidity_migration.account.market_capture import MarketCaptureConfig, SequenceAwareMarketRecorder


def test_owner_reconciliation_cycle_refreshes_position_truth_after_funding() -> None:
    calls: list[str] = []

    class FundingReconciler:
        def reconcile_once(self) -> str:
            calls.append("funding")
            return "funding-report"

    class PositionReconciler:
        def reconcile_once(self) -> str:
            calls.append("position")
            return "position-report"

    position_report, funding_report = account_service_runner_module._run_reconciliation_cycle(
        reconciler=PositionReconciler(),
        funding_reconciler=FundingReconciler(),
    )

    assert calls == ["funding", "position"]
    assert position_report == "position-report"
    assert funding_report == "funding-report"


def _capture_config() -> MarketCaptureConfig:
    return MarketCaptureConfig(
        depth=50,
        segment_max_bytes=1_000_000,
        fsync_every_records=1,
        min_free_disk_bytes=1,
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
    assert market.metadata["capture_segment_path"].endswith("segment-000000.jsonl")
    assert market.metadata["capture_byte_offset"] >= 0
    assert market.metadata["capture_byte_length"] > 0
    assert len(market.metadata["capture_record_sha256"]) == 64
    recorder.close()


def _valid_execution_book(*, symbol: str = "BUSDT") -> L2BookSnapshot:
    return L2BookSnapshot(
        symbol=symbol,
        sequence=10,
        previous_sequence=9,
        exchange_ts_ns=900,
        local_receive_ts_ns=1_000,
        bids=(BookLevel(10.0, 0.4), BookLevel(9.9, 0.8)),
        asks=(BookLevel(10.1, 0.4), BookLevel(10.2, 0.8)),
    )


def _execution_command(*, side: str, command_id: str = "command-1") -> OrderCommand:
    signed_qty = 1.0 if side == "Buy" else -1.0
    return OrderCommand(
        command_id=command_id,
        batch_id="batch-1",
        symbol="BUSDT",
        side=side,
        qty=1.0,
        signed_qty=signed_qty,
        reduce_only=False,
        reference_price=10.05,
        target_signed_qty=signed_qty,
        chunk_index=0,
        chunk_count=1,
        created_ts_ns=1_000,
    )


def _execution_twin(book: L2BookSnapshot) -> MarketOrderExecutionTwin:
    return MarketOrderExecutionTwin(
        books={"BUSDT": book},
        instrument_rules={
            "BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)
        },
        config=ExecutionTwinConfig(
            fee_bps=5.5,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=250_000_000,
        ),
    )


@pytest.mark.parametrize(
    ("side", "expected_prices"),
    [
        ("Buy", [10.1, 10.2]),
        ("Sell", [10.0, 9.9]),
    ],
)


def test_execution_twin_walks_valid_book_for_buy_and_sell(
    side: str,
    expected_prices: list[float],
) -> None:
    book = _valid_execution_book()
    observations = tuple(
        _execution_twin(book).submit(
            _execution_command(side=side),
            book.market_ref(input_key=f"{side.lower()}-book"),
        )
    )

    fills = [
        observation
        for observation in observations
        if observation.observation_type == ExecutionObservationType.FILL
    ]
    assert [fill.price for fill in fills] == expected_prices
    assert [abs(fill.signed_qty) for fill in fills] == pytest.approx([0.4, 0.6])
    assert all(
        observation.metadata["fill_partition_policy"] == "book_level"
        and observation.metadata["execution_model_scope"]
        == INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE
        for observation in observations
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bids": ()}, "empty_book_bids"),
        ({"asks": ()}, "empty_book_asks"),
        (
            {"bids": (BookLevel(float("nan"), 1.0),)},
            "non_finite_bid_level",
        ),
        (
            {"asks": (BookLevel(10.1, 0.0),)},
            "non_positive_ask_level",
        ),
        (
            {"bids": (BookLevel(9.9, 1.0), BookLevel(10.0, 1.0))},
            "non_monotonic_book_bids",
        ),
        (
            {"asks": (BookLevel(10.2, 1.0), BookLevel(10.1, 1.0))},
            "non_monotonic_book_asks",
        ),
        (
            {"bids": (BookLevel(10.2, 1.0),)},
            "crossed_book",
        ),
        ({"sequence": 0}, "invalid_book_sequence"),
        ({"previous_sequence": 10}, "invalid_book_previous_sequence"),
        ({"exchange_ts_ns": 0}, "invalid_book_exchange_timestamp"),
        ({"local_receive_ts_ns": 0}, "invalid_book_local_timestamp"),
        ({"sequence_gap": True}, "book_sequence_gap"),
    ],
)
def test_execution_twin_rejects_malformed_l2_without_sanitizing_levels(
    overrides: dict[str, Any],
    reason: str,
) -> None:
    fields: dict[str, Any] = {
        "symbol": "BUSDT",
        "sequence": 10,
        "previous_sequence": 9,
        "exchange_ts_ns": 900,
        "local_receive_ts_ns": 1_000,
        "bids": (BookLevel(10.0, 0.4), BookLevel(9.9, 0.8)),
        "asks": (BookLevel(10.1, 0.4), BookLevel(10.2, 0.8)),
    }
    book = L2BookSnapshot(**{**fields, **overrides})
    market = _valid_execution_book().market_ref(input_key="valid-market")
    observations = tuple(
        _execution_twin(book).submit(
            _execution_command(side="Buy"),
            market,
        )
    )

    assert len(observations) == 1
    rejection = observations[0]
    assert rejection.observation_type == ExecutionObservationType.ACK
    assert rejection.accepted is False
    assert rejection.rejection_key.endswith(f":{reason}")
    assert rejection.metadata["execution_model_scope"] == (
        INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE
    )
    with pytest.raises(ValueError, match=reason):
        book.market_ref(input_key="invalid-market")


def test_execution_twin_rejects_book_and_market_symbol_mismatches() -> None:
    market = _valid_execution_book().market_ref(input_key="valid-market")
    wrong_book = _valid_execution_book(symbol="ETHUSDT")
    book_rejection = tuple(
        _execution_twin(wrong_book).submit(
            _execution_command(side="Buy", command_id="wrong-book"),
            market,
        )
    )[0]
    assert book_rejection.rejection_key.endswith(":book_symbol_mismatch")

    wrong_market = MarketInputRef(
        input_key="wrong-market",
        symbol="ETHUSDT",
        exchange_ts_ns=900,
        local_receive_ts_ns=1_000,
        reference_price=10.05,
        book_sequence=10,
    )
    market_rejection = tuple(
        _execution_twin(_valid_execution_book()).submit(
            _execution_command(side="Sell", command_id="wrong-market"),
            wrong_market,
        )
    )[0]
    assert market_rejection.rejection_key.endswith(":market_input_symbol_mismatch")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exchange_ts_ns": 0}, "invalid_market_input_exchange_timestamp"),
        ({"local_receive_ts_ns": 0}, "invalid_market_input_local_timestamp"),
        ({"reference_price": float("nan")}, "invalid_market_input_reference_price"),
        ({"book_sequence": 0}, "invalid_market_input_sequence"),
        (
            {"bid_price": 10.2, "ask_price": 10.1},
            "crossed_market_input",
        ),
    ],
)
def test_execution_twin_rejects_unusable_market_reference(
    overrides: dict[str, Any],
    reason: str,
) -> None:
    fields: dict[str, Any] = {
        "input_key": "market-input",
        "symbol": "BUSDT",
        "exchange_ts_ns": 900,
        "local_receive_ts_ns": 1_000,
        "reference_price": 10.05,
        "bid_price": 10.0,
        "ask_price": 10.1,
        "book_sequence": 10,
    }
    market = MarketInputRef(**{**fields, **overrides})
    rejection = tuple(
        _execution_twin(_valid_execution_book()).submit(
            _execution_command(side="Buy"),
            market,
        )
    )[0]

    assert rejection.accepted is False
    assert rejection.rejection_key.endswith(f":{reason}")
    assert rejection.metadata["execution_model_scope"] == (
        INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE
    )


class _WalletClient:
    demo = True
    realm = "demo"

    def __init__(self, account: dict[str, str]) -> None:
        self.account = account

    def get_wallet_balance(self, **params: str):
        assert params == {"account_type": "UNIFIED", "coin": "USDT"}
        return {"list": [self.account]}


class _MainnetWalletClient(_WalletClient):
    demo = False
    realm = "mainnet"


def test_snapshot_provider_refuses_a_client_that_names_no_parsable_realm() -> None:
    class Realmless:
        demo = True

    class BadRealm:
        demo = False
        realm = "paper"

    for client in (object(), Realmless(), BadRealm()):
        with pytest.raises(ValueError, match="naming venue realm"):
            BybitAccountSnapshotProvider(client)


def test_snapshot_provider_reads_the_same_equity_in_both_realms() -> None:
    account = {"totalEquity": "100.5", "totalAvailableBalance": "80.25"}
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)

    demo = BybitAccountSnapshotProvider(_WalletClient(account), clock=clock).current(batch_id="b1")
    mainnet = BybitAccountSnapshotProvider(
        _MainnetWalletClient(account), clock=clock
    ).current(batch_id="b1")

    assert demo.equity_usdt == mainnet.equity_usdt == 100.5
    assert demo.available_margin_usdt == mainnet.available_margin_usdt == 80.25
    assert demo.snapshot_ts_ns == mainnet.snapshot_ts_ns == 2_000_000_000
    assert demo.snapshot_key.startswith("bybit-demo:")
    assert mainnet.snapshot_key.startswith("bybit-mainnet:")
    # The digest covers realm-free material, so only the label differs.
    assert demo.snapshot_key.split(":")[1] == mainnet.snapshot_key.split(":")[1]


def test_snapshot_provider_names_the_realm_in_its_wallet_faults() -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)
    provider = BybitAccountSnapshotProvider(
        _MainnetWalletClient({"totalEquity": "0", "totalAvailableBalance": "1"}), clock=clock
    )
    with pytest.raises(RuntimeError, match="Bybit mainnet wallet snapshot has nonpositive equity"):
        provider.current(batch_id="b1")


def test_snapshot_provider_reads_the_coin_row_when_aggregates_blank() -> None:
    """The venue blanks the account-wide margin aggregates in some unified
    margin modes (observed live 2026-08-04 on the funded mainnet account)
    while the per-coin row stays populated; the snapshot falls back to it.
    """

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)
    account = {
        "totalEquity": "1417.03752928",
        "totalWalletBalance": "1417.03752928",
        "totalPerpUPL": "0",
        "totalAvailableBalance": "",
        "totalMarginBalance": "",
        "totalInitialMargin": "",
        "totalMaintenanceMargin": "",
        "accountIMRate": "",
        "coin": [
            {
                "coin": "USDT",
                "equity": "1417.00057425",
                "walletBalance": "1417.00057425",
                "unrealisedPnl": "0",
                "totalOrderIM": "10.5",
                "totalPositionIM": "6.5",
                "locked": "0",
            }
        ],
    }

    snapshot = BybitAccountSnapshotProvider(
        _MainnetWalletClient(account), clock=clock
    ).current(batch_id="b1")

    assert snapshot.equity_usdt == 1417.03752928
    assert snapshot.available_margin_usdt == pytest.approx(1417.00057425 - 17.0)


def test_snapshot_provider_charges_unrealized_losses_never_gains() -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)
    base = {
        "totalEquity": "100",
        "totalAvailableBalance": "",
        "totalMarginBalance": "",
        "totalInitialMargin": "",
    }
    coin = {
        "coin": "USDT",
        "walletBalance": "100",
        "totalOrderIM": "10",
        "totalPositionIM": "0",
        "locked": "0",
    }

    losing = BybitAccountSnapshotProvider(
        _MainnetWalletClient({**base, "coin": [{**coin, "unrealisedPnl": "-30"}]}),
        clock=clock,
    ).current(batch_id="b1")
    winning = BybitAccountSnapshotProvider(
        _MainnetWalletClient({**base, "coin": [{**coin, "unrealisedPnl": "30"}]}),
        clock=clock,
    ).current(batch_id="b1")

    assert losing.available_margin_usdt == pytest.approx(60.0)
    assert winning.available_margin_usdt == pytest.approx(90.0)


def test_snapshot_provider_fails_closed_when_nothing_numeric_remains() -> None:
    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=0)
    all_blank = {
        "totalEquity": "",
        "totalWalletBalance": "",
        "totalPerpUPL": "",
        "totalAvailableBalance": "",
        "totalMarginBalance": "",
        "totalInitialMargin": "",
    }

    provider = BybitAccountSnapshotProvider(_MainnetWalletClient(all_blank), clock=clock)
    with pytest.raises(RuntimeError, match="carries no numeric equity"):
        provider.current(batch_id="b1")

    # Equity resolves but every available-margin rung is blank, including a
    # coin row missing its margin deductions: refuse rather than default them.
    no_available = BybitAccountSnapshotProvider(
        _MainnetWalletClient(
            {
                **all_blank,
                "totalEquity": "100",
                "coin": [{"coin": "USDT", "walletBalance": "100", "unrealisedPnl": "0"}],
            }
        ),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="carries no numeric available margin"):
        no_available.current(batch_id="b1")


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
    realm = "demo"

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
        require_bybit_order_ownership(
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

    require_bybit_order_ownership(
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

    require_bybit_order_ownership(
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

    require_bybit_order_ownership(
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
        require_bybit_order_ownership(
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
    order_id = "probe-order-1"
    order_link_id = "lm-demo-rule-BUSDT-test-1"
    rules_payload: dict[str, Any] = {
        "schema_version": DEMO_RULES_SCHEMA_VERSION,
        "kind": DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": 123,
        "max_probe_notional_usdt": 200.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "rules": {
            "BUSDT": {
                "qty_step": 1,
                "min_qty": 1,
                "min_notional": 1,
                "tick_size": 0.0001,
                "max_order_qty": 1000,
                "max_leverage": 25,
                "source": "bybit_demo_post_only_acceptance_probe",
                "environment": "demo",
                "observed_ts_ns": 123,
            }
        },
        "evidence": {
            "BUSDT": {
                "schema_version": DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
                "kind": DEMO_RULE_PROBE_EVIDENCE_KIND,
                "environment": "demo",
                "observed_ts_ns": 123,
                "symbol": "BUSDT",
                "probe_price": 1,
                "probe_distance_bps": 100,
                "lowest_accepted_qty": 1,
                "lowest_accepted_notional_usdt": 1,
                "highest_rejected_qty": 0,
                "highest_rejected_notional_usdt": 0,
                "tested_leverage": 10,
                "terminal_history_timeout_seconds": 5.0,
                "terminal_history_poll_seconds": 0.1,
                "terminal_history_max_polls": 50,
                "required_terminal_confirmation_polls": 2,
                "attempts": [{
                    "step_count": 1,
                    "qty": 1,
                    "notional_usdt": 1,
                    "accepted": True,
                    "outcome": "verified_cancelled_no_fill",
                    "rejection": "",
                    "order_link_id": order_link_id,
                    "order_id": order_id,
                    "create_ack_source": ORDER_CREATE_SOURCE,
                    "create_ack_order_id": order_id,
                    "create_ack_order_link_id": order_link_id,
                    "cancel_ack_source": ORDER_CANCEL_SOURCE,
                    "cancel_ack_order_id": order_id,
                    "cancel_ack_order_link_id": order_link_id,
                    "order_history_source": ORDER_HISTORY_SOURCE,
                    "order_history_query_symbol": "BUSDT",
                    "order_history_query_order_id": order_id,
                    "order_history_query_order_link_id": order_link_id,
                    "terminal_order_id": order_id,
                    "terminal_order_link_id": order_link_id,
                    "terminal_status": "Cancelled",
                    "terminal_cum_exec_qty": "0",
                    "terminal_cum_exec_value": "0",
                    "terminal_observed_ts_ns": 123,
                    "terminal_poll_count": 2,
                    "terminal_confirmation_polls": 2,
                    "trade_history_source": TRADE_HISTORY_SOURCE,
                    "trade_history_query_symbol": "BUSDT",
                    "trade_history_query_order_id": order_id,
                    "trade_history_query_order_link_id": order_link_id,
                    "trade_history_row_count": 0,
                }],
            }
        },
        "artifact_sha256": "",
    }
    rules_payload["artifact_sha256"] = hashlib.sha256(canonical_json(rules_payload)).hexdigest()
    rules_path.write_text(json.dumps(rules_payload))
    rules = load_demo_rules(rules_path)
    assert rules["BUSDT"].source == "bybit_demo_post_only_acceptance_probe"
    assert rules["BUSDT"].environment == "demo"

    weak_contract = json.loads(json.dumps(rules_payload))
    weak_contract["probe_distance_bps"] = 50
    weak_contract["artifact_sha256"] = ""
    weak_contract["artifact_sha256"] = hashlib.sha256(
        canonical_json(weak_contract)
    ).hexdigest()
    rules_path.write_text(json.dumps(weak_contract))
    with pytest.raises(ValueError, match="fixed prospectively at 100 bps"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    over_cap_attempt = json.loads(json.dumps(rules_payload))
    over_cap_attempt["rules"]["BUSDT"]["min_notional"] = 201
    over_cap_attempt["evidence"]["BUSDT"]["lowest_accepted_qty"] = 201
    over_cap_attempt["evidence"]["BUSDT"]["lowest_accepted_notional_usdt"] = 201
    over_cap_attempt["evidence"]["BUSDT"]["attempts"][0]["step_count"] = 201
    over_cap_attempt["evidence"]["BUSDT"]["attempts"][0]["qty"] = 201
    over_cap_attempt["evidence"]["BUSDT"]["attempts"][0]["notional_usdt"] = 201
    over_cap_attempt["artifact_sha256"] = ""
    over_cap_attempt["artifact_sha256"] = hashlib.sha256(
        canonical_json(over_cap_attempt)
    ).hexdigest()
    rules_path.write_text(json.dumps(over_cap_attempt))
    with pytest.raises(ValueError, match="exceeds the receipt probe-notional cap"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    excess_leverage = json.loads(json.dumps(rules_payload))
    excess_leverage["evidence"]["BUSDT"]["tested_leverage"] = 11
    excess_leverage["artifact_sha256"] = ""
    excess_leverage["artifact_sha256"] = hashlib.sha256(
        canonical_json(excess_leverage)
    ).hexdigest()
    rules_path.write_text(json.dumps(excess_leverage))
    with pytest.raises(ValueError, match="tested leverage exceeds registered 10x"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    legacy_payload = json.loads(json.dumps(rules_payload))
    legacy_payload["evidence"]["BUSDT"] = {
        "lowest_accepted_notional_usdt": 1,
        "attempts": [{"accepted": True}],
    }
    legacy_payload["artifact_sha256"] = ""
    legacy_payload["artifact_sha256"] = hashlib.sha256(
        canonical_json(legacy_payload)
    ).hexdigest()
    rules_path.write_text(json.dumps(legacy_payload))
    with pytest.raises(ValueError, match="probe evidence identity"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    wrong_identity = json.loads(json.dumps(rules_payload))
    wrong_identity["evidence"]["BUSDT"]["attempts"][0]["terminal_order_id"] = "wrong"
    wrong_identity["artifact_sha256"] = ""
    wrong_identity["artifact_sha256"] = hashlib.sha256(
        canonical_json(wrong_identity)
    ).hexdigest()
    rules_path.write_text(json.dumps(wrong_identity))
    with pytest.raises(ValueError, match="does not bind"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    tampered = dict(rules_payload)
    tampered["verified_ts_ns"] = 124
    rules_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="artifact_sha256"):
        load_demo_rules(rules_path)
    rules_path.write_text(json.dumps(rules_payload))

    with pytest.raises(ValueError, match="stale"):
        load_demo_rules(rules_path, now_ns=10_000_000_000, max_age_seconds=1.0)
    with pytest.raises(ValueError, match="finite and positive"):
        load_demo_rules(rules_path, max_age_seconds=float("nan"))

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


def test_order_ownership_classifies_identically_in_both_realms() -> None:
    class _MainnetStartupClient(_StartupOpenOrderClient):
        demo = False
        realm = "mainnet"

    stray = {
        "symbol": "BUSDT",
        "orderId": "stray-1",
        "orderLinkId": "manual-order",
        "orderStatus": "New",
    }
    snapshots = {
        factory.realm: inspect_bybit_order_ownership(
            client=factory(all_kinds=[stray]),
            state=AccountState(events_applied=4),
            native_order_verifier=lambda _row: False,
        )
        for factory in (_StartupOpenOrderClient, _MainnetStartupClient)
    }

    assert snapshots["demo"] == snapshots["mainnet"]
    assert [order.description for order in snapshots["mainnet"].unowned_orders] == [
        "regular BUSDT orderId=stray-1 orderLinkId=manual-order"
    ]


def test_order_ownership_refuses_a_client_that_names_no_parsable_realm() -> None:
    class Realmless(_StartupOpenOrderClient):
        realm = None

    with pytest.raises(ValueError, match="naming venue realm"):
        inspect_bybit_order_ownership(client=Realmless(), state=AccountState())
    with pytest.raises(ValueError, match="naming venue realm"):
        require_bybit_order_ownership(
            client=Realmless(),
            kernel=_StaticKernel(AccountState()),  # type: ignore[arg-type]
        )


def test_mainnet_owner_startup_refuses_an_unowned_venue_order_by_realm_name() -> None:
    class _MainnetStartupClient(_StartupOpenOrderClient):
        demo = False
        realm = "mainnet"

    client = _MainnetStartupClient(all_kinds=[{"symbol": "BUSDT", "orderId": "manual-1"}])

    with pytest.raises(RuntimeError, match="Bybit mainnet startup refused venue orders"):
        require_bybit_order_ownership(
            client=client,
            kernel=_StaticKernel(AccountState()),  # type: ignore[arg-type]
        )
    assert client.calls == [
        {"settle_coin": "USDT"},
        {"settle_coin": "USDT", "order_filter": "StopOrder"},
    ]


class _MainnetOwnerClient:
    """One mainnet-realm private client answering every read the owner start-up makes."""

    demo = False
    realm = "mainnet"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_open_orders(self, **_params: Any) -> list[dict[str, Any]]:
        self.calls.append("get_open_orders")
        return []

    def get_positions(self, **_params: Any) -> list[dict[str, Any]]:
        self.calls.append("get_positions")
        return []

    def get_trade_history(self, **_params: Any) -> list[dict[str, Any]]:
        self.calls.append("get_trade_history")
        return []

    def get_order_history(self, **_params: Any) -> list[dict[str, Any]]:
        self.calls.append("get_order_history")
        return []

    def get_account_transactions(self, **_params: Any) -> list[dict[str, Any]]:
        self.calls.append("get_account_transactions")
        return []

    def get_wallet_balance(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("get_wallet_balance")
        return {"list": [{"totalEquity": "2500", "totalAvailableBalance": "2400"}]}


def test_mainnet_owner_start_up_reads_get_past_construction(tmp_path: Path) -> None:
    """The defect this closes: on mainnet none of these could even be built.

    Same order as ``run_account_execution_service``: ownership check, bootstrap
    reconcile, funding reconcile, wallet snapshot.
    """

    clock = VirtualClock(current_wall_ns=2_000_000_000, current_monotonic_ns=100)
    kernel = AccountExecutionKernel(tmp_path, account_id="mainnet-owner", clock=clock)
    client = _MainnetOwnerClient()

    require_bybit_order_ownership(client=client, kernel=kernel)

    reconciler = BybitAccountReconciler(
        kernel=kernel,
        client=client,
        instrument_rules={"BUSDT": InstrumentRules("BUSDT", 0.1, 0.1, 1.0)},
        clock=clock,
    )
    bootstrap = reconciler.reconcile_once()
    require_startup_reconciliation_safe(bootstrap)
    assert bootstrap.healthy
    assert bootstrap.snapshot_key.startswith("bybit-mainnet-position:")

    funding_reconciler = BybitAccountFundingReconciler(kernel=kernel, client=client, clock=clock)
    clock.advance_ns(1_000_000_000)
    assert funding_reconciler.reconcile_once().healthy

    snapshot = BybitAccountSnapshotProvider(client, clock=clock).current(batch_id="owner-health/bootstrap")
    assert snapshot.equity_usdt == 2500.0
    assert snapshot.snapshot_key.startswith("bybit-mainnet:")

    reconciler.require_recent_healthy(max_age_ns=1)
    funding_reconciler.require_recent_healthy(max_age_ns=1)
    # Every leg must have reached the venue. A funding pass whose window has not
    # opened yet returns healthy without one REST call, so assert the call, not
    # the verdict.
    assert client.calls == [
        # startup ownership: all-kinds, then the explicit StopOrder query
        "get_open_orders",
        "get_open_orders",
        # reconcile: position truth, then the same ownership pair again
        "get_positions",
        "get_open_orders",
        "get_open_orders",
        "get_account_transactions",
        "get_wallet_balance",
    ]


def test_both_owner_constructors_accept_a_coherent_mainnet_client(tmp_path: Path) -> None:
    """The two constructors are realm-agnostic; arming is decided elsewhere.

    ``BybitNativeProtectionManager`` is built at ``account_service_runner.py:716``
    and ``BybitDemoExecutionAdapter`` after it. Both used to refuse any non-demo
    client outright, which blocked a mainnet owner from starting at all. Neither
    refuses a realm now: whether mainnet may trade is decided by credential
    resolution, which requires ``REAL_MONEY``.
    """

    client = _MainnetOwnerClient()
    kernel = AccountExecutionKernel(tmp_path, account_id="mainnet-owner")

    manager = BybitNativeProtectionManager(
        kernel=kernel,
        client=client,
        instrument_rules={},
        fallback_stop_fraction=0.35,
    )
    assert manager.realm is VenueRealm.MAINNET
    assert BybitDemoExecutionAdapter(client).realm is VenueRealm.MAINNET


def test_both_owner_constructors_refuse_a_self_contradictory_client(tmp_path: Path) -> None:
    """A declared realm that the transport does not address is still refused.

    This is what the demo-only fences were actually worth: a client claiming
    mainnet while its transport addresses demo (or the reverse) would install
    stops and send orders somewhere other than where the exposure is.
    """

    class _Contradictory:
        demo = True
        realm = "mainnet"

    kernel = AccountExecutionKernel(tmp_path, account_id="contradictory-owner")

    with pytest.raises(ValueError, match="contradicts its mainnet realm"):
        BybitNativeProtectionManager(
            kernel=kernel,
            client=_Contradictory(),
            instrument_rules={},
            fallback_stop_fraction=0.35,
        )
    with pytest.raises(ValueError, match="contradicts its mainnet realm"):
        BybitDemoExecutionAdapter(_Contradictory())


def test_owner_constructors_refuse_an_unrealmed_non_demo_client(tmp_path: Path) -> None:
    """An object with no realm reads as demo, so ``demo=False`` stays refused.

    Hand-rolled doubles carry no ``realm``; treating them as demo keeps them
    usable while preserving the old refusal for anything that turned demo off
    without saying what it turned it on to.
    """

    class _Unrealmed:
        demo = False

    kernel = AccountExecutionKernel(tmp_path, account_id="unrealmed-owner")

    with pytest.raises(ValueError, match="contradicts its demo realm"):
        BybitNativeProtectionManager(
            kernel=kernel,
            client=_Unrealmed(),
            instrument_rules={},
            fallback_stop_fraction=0.35,
        )
    with pytest.raises(ValueError, match="contradicts its demo realm"):
        BybitDemoExecutionAdapter(_Unrealmed())
