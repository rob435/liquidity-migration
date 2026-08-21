from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

import liquidity_migration.research.execution.account_venue_accounting as accounting_module
from liquidity_migration.account.account_kernel import (
    AccountExecutionKernel,
    AccountRiskPolicy,
    AccountRiskSnapshot,
    DesiredTarget,
    InstrumentRules,
    MarketInputRef,
    account_transactions_path,
)
from liquidity_migration.research.execution.account_venue_accounting import (
    VenueAccountingRequirements,
    build_venue_accounting_receipt,
    load_venue_accounting_receipt,
    verify_venue_accounting_receipt,
    write_venue_accounting_receipt,
)
from liquidity_migration.core.deterministic_runtime import VirtualClock


ACCOUNT_ID = "bybit-demo-accounting-test"
START_MS = 1_000
END_MS = 5_000


def _market(*, price: float, key: str, exchange_ms: int) -> MarketInputRef:
    return MarketInputRef(
        input_key=key,
        symbol="BTCUSDT",
        exchange_ts_ns=exchange_ms * 1_000_000,
        local_receive_ts_ns=(exchange_ms + 1) * 1_000_000,
        reference_price=price,
        bid_price=price - 0.5,
        ask_price=price + 0.5,
        book_sequence=exchange_ms,
        source="test_l2",
    )


def _target(*, key: str, quantity: float, price: float) -> DesiredTarget:
    return DesiredTarget(
        decision_key=f"decision-{key}",
        target_key="continuous/accounting/BTCUSDT",
        sleeve="continuous",
        strategy_id="accounting",
        component_id="accounting",
        symbol="BTCUSDT",
        signed_qty=quantity,
        reference_price=price,
        leverage=2.0,
        reason=key,
    )


def _rules() -> dict[str, InstrumentRules]:
    return {
        "BTCUSDT": InstrumentRules(
            symbol="BTCUSDT",
            qty_step=0.001,
            min_qty=0.001,
            min_notional=1.0,
            max_order_qty=10.0,
            max_leverage=10.0,
        )
    }


def _snapshot() -> AccountRiskSnapshot:
    return AccountRiskSnapshot(
        equity_usdt=10_000.0,
        available_margin_usdt=9_000.0,
        snapshot_key="wallet-accounting",
        snapshot_ts_ns=1_100_000_000,
    )


def _policy() -> AccountRiskPolicy:
    return AccountRiskPolicy(
        max_component_gross_notional_usdt=10_000.0,
        max_account_gross_notional_usdt=10_000.0,
        max_initial_margin_usdt=10_000.0,
        max_leverage=10.0,
    )


def _closed_journal(root: Path, *, fee_observed: bool = True) -> None:
    kernel = AccountExecutionKernel(
        root,
        account_id=ACCOUNT_ID,
        clock=VirtualClock(
            current_wall_ns=1_100_000_000,
            current_monotonic_ns=100_000_000,
        ),
        id_seed="accounting-test",
    )
    opening_market = _market(price=10.0, key="open-book", exchange_ms=1_200)
    opening = kernel.submit_targets(
        batch_id="open",
        market_inputs=[opening_market],
        targets=[_target(key="open", quantity=1.0, price=10.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    opening_command = opening.commands[0]
    kernel.record_ack(
        command_id=opening_command.command_id,
        accepted=True,
        venue_order_id="venue-open",
        exchange_ts_ns=1_300_000_000,
        local_ack_ts_ns=1_310_000_000,
    )
    kernel.record_fill(
        command_id=opening_command.command_id,
        execution_id="execution-open",
        signed_qty=1.0,
        price=10.0,
        fee_usdt=0.005,
        exchange_ts_ns=1_400_000_000,
        local_receive_ts_ns=1_410_000_000,
        metadata={
            "fee_observed": fee_observed,
            "fee_status": "observed_execution_fee",
            "fee_source": "bybit_execution_stream",
        },
    )

    closing_market = _market(price=11.0, key="close-book", exchange_ms=2_200)
    closing = kernel.submit_targets(
        batch_id="close",
        market_inputs=[closing_market],
        targets=[_target(key="close", quantity=0.0, price=11.0)],
        risk_snapshot=_snapshot(),
        risk_policy=_policy(),
        instrument_rules=_rules(),
    )
    closing_command = closing.commands[0]
    assert closing_command.reduce_only
    kernel.record_ack(
        command_id=closing_command.command_id,
        accepted=True,
        venue_order_id="venue-close",
        exchange_ts_ns=2_300_000_000,
        local_ack_ts_ns=2_310_000_000,
    )
    kernel.record_fill(
        command_id=closing_command.command_id,
        execution_id="execution-close",
        signed_qty=-1.0,
        price=11.0,
        fee_usdt=0.006,
        exchange_ts_ns=2_400_000_000,
        local_receive_ts_ns=2_410_000_000,
        metadata={
            "fee_observed": fee_observed,
            "fee_status": "observed_execution_fee",
            "fee_source": "bybit_execution_stream",
        },
    )
    finalized = kernel.finalize_flat_position(
        symbol="BTCUSDT",
        command_id=closing_command.command_id,
        exchange_ts_ns=2_400_000_000,
        local_receive_ts_ns=2_410_000_000,
        metadata={"venue_position_confirmed_flat": True},
    )
    assert finalized
    kernel.record_pnl(
        pnl_key="funding-settlement-1",
        close_key="",
        symbol="BTCUSDT",
        gross_pnl_usdt=0.0,
        fee_usdt=0.0,
        funding_usdt=0.02,
        net_pnl_usdt=0.02,
        exchange_ts_ns=3_000_000_000,
        local_receive_ts_ns=3_010_000_000,
        source="venue_funding_settlement",
        metadata={"venue_transaction_id": "settlement-1"},
    )


def _venue_sources() -> dict[str, list[dict[str, str]]]:
    return {
        "closed_pnl_rows": [
            {
                "symbol": "BTCUSDT",
                "orderId": "venue-close",
                "closedPnl": "0.989",
                "openFee": "0.005",
                "closeFee": "0.006",
                "updatedTime": "2500",
            }
        ],
        "trade_rows": [
            {
                "id": "transaction-open",
                "tradeId": "execution-open",
                "type": "TRADE",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "qty": "1",
                "tradePrice": "10",
                "orderId": "venue-open",
                "orderLinkId": "",
                "cashFlow": "0",
                "funding": "0",
                "fee": "0.005",
                "change": "-0.005",
                "transactionTime": "1400",
            },
            {
                "id": "transaction-close",
                "tradeId": "execution-close",
                "type": "TRADE",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "qty": "1",
                "tradePrice": "11",
                "orderId": "venue-close",
                "orderLinkId": "",
                "cashFlow": "1",
                "funding": "0",
                "fee": "0.006",
                "change": "0.994",
                "transactionTime": "2400",
            },
        ],
        "settlement_rows": [
            {
                "id": "settlement-1",
                "type": "SETTLEMENT",
                "category": "linear",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "cashFlow": "0",
                "funding": "0.02",
                "fee": "0",
                "change": "0.02",
                "transactionTime": "3000",
            }
        ],
        "pre_position_rows": [],
        "pre_open_order_rows": [],
        "post_position_rows": [],
        "post_open_order_rows": [],
    }


def _receipt(root: Path, **overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "account_root": root,
        "expected_account_id": ACCOUNT_ID,
        "query_start_ms": START_MS,
        "query_end_ms": END_MS,
        "observed_ts_ns": 5_100_000_000,
        **_venue_sources(),
    }
    values.update(overrides)
    return build_venue_accounting_receipt(**values)


def test_venue_accounting_receipt_replays_exact_sources_and_journal(
    tmp_path: Path,
) -> None:
    _closed_journal(tmp_path)

    receipt = _receipt(tmp_path)

    assert receipt["venue_accounting_gate_passed"] is True
    assert receipt["final_demo_flatness_gate_passed"] is True
    assert all(receipt["gates"].values())
    assert receipt["mismatches"] == []
    output = write_venue_accounting_receipt(
        (tmp_path / "evidence" / "venue-accounting.json").resolve(), receipt
    )
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert load_venue_accounting_receipt(output) == receipt
    preserved = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_venue_accounting_receipt(output, receipt)
    assert output.read_bytes() == preserved


def test_venue_accounting_receipt_rejects_tampering(tmp_path: Path) -> None:
    _closed_journal(tmp_path)
    receipt = _receipt(tmp_path)
    tampered = copy.deepcopy(receipt)
    tampered["venue_sources"]["trade_transactions"]["rows"][0]["fee"] = "0"

    with pytest.raises(ValueError, match="hash mismatch"):
        verify_venue_accounting_receipt(tampered)

    output = tmp_path / "venue-accounting.json"
    output.write_text(json.dumps(receipt), encoding="utf-8")
    os.chmod(output, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        load_venue_accounting_receipt(output)
    os.chmod(output, 0o600)
    with output.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    loaded = load_venue_accounting_receipt(output)
    assert loaded == receipt

    kernel = AccountExecutionKernel(tmp_path, account_id=ACCOUNT_ID)
    kernel.record_pnl(
        pnl_key="post-receipt-mutation",
        close_key="",
        symbol="BTCUSDT",
        gross_pnl_usdt=0.0,
        fee_usdt=0.0,
        funding_usdt=0.0,
        net_pnl_usdt=0.0,
        exchange_ts_ns=4_000_000_000,
        local_receive_ts_ns=4_010_000_000,
        source="test_post_receipt_mutation",
    )
    with pytest.raises(ValueError, match="does not reproduce"):
        verify_venue_accounting_receipt(receipt)


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (VenueAccountingRequirements(min_trade_rows=1), "min_trade_rows"),
        (VenueAccountingRequirements(min_closed_pnl_rows=0), "min_closed_pnl_rows"),
        (VenueAccountingRequirements(min_funding_rows=0), "min_funding_rows"),
        (
            VenueAccountingRequirements(quantity_abs_tolerance=1e-11),
            "quantity_abs_tolerance",
        ),
        (
            VenueAccountingRequirements(price_abs_tolerance=1e-7),
            "price_abs_tolerance",
        ),
        (
            VenueAccountingRequirements(amount_abs_tolerance=1e-7),
            "amount_abs_tolerance",
        ),
        (
            VenueAccountingRequirements(relative_tolerance=1e-8),
            "relative_tolerance",
        ),
    ],
)
def test_venue_accounting_verifier_rejects_self_hashed_weakened_gate(
    tmp_path: Path,
    requirements: VenueAccountingRequirements,
    message: str,
) -> None:
    _closed_journal(tmp_path)
    weak_receipt = _receipt(tmp_path, requirements=requirements)
    with pytest.raises(ValueError, match=message):
        verify_venue_accounting_receipt(weak_receipt)


def test_venue_accounting_rejects_journal_mutation_during_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _closed_journal(tmp_path)
    transaction = next(account_transactions_path(tmp_path).glob("*.json"))
    original_journal_sha256 = accounting_module._journal_sha256

    def mutate_after_initial_snapshot(
        events: list[accounting_module.AccountEvent],
    ) -> str:
        transaction.write_bytes(transaction.read_bytes())
        return original_journal_sha256(events)

    monkeypatch.setattr(
        accounting_module,
        "_journal_sha256",
        mutate_after_initial_snapshot,
    )
    with pytest.raises(RuntimeError, match="journal mutated during reconciliation"):
        _receipt(tmp_path)


def test_venue_accounting_receipt_fails_missing_funding_and_fee_provenance(
    tmp_path: Path,
) -> None:
    missing_funding_root = tmp_path / "missing-funding"
    _closed_journal(missing_funding_root)
    missing_funding = _receipt(missing_funding_root, settlement_rows=[])
    assert missing_funding["venue_accounting_gate_passed"] is False
    assert missing_funding["gates"]["funding_sample_floor"] is False
    assert missing_funding["gates"]["funding_identity_coverage"] is False

    unobserved_fee_root = tmp_path / "unobserved-fee"
    _closed_journal(unobserved_fee_root, fee_observed=False)
    unobserved_fee = _receipt(unobserved_fee_root)
    assert unobserved_fee["venue_accounting_gate_passed"] is False
    assert unobserved_fee["gates"]["fill_fee_provenance_observed"] is False


def test_venue_accounting_receipt_fails_transaction_value_mismatch(
    tmp_path: Path,
) -> None:
    _closed_journal(tmp_path)
    sources = _venue_sources()
    sources["trade_rows"][1]["tradePrice"] = "12"

    receipt = _receipt(tmp_path, **sources)

    assert receipt["venue_accounting_gate_passed"] is False
    assert receipt["gates"]["trade_fields_match"] is False
