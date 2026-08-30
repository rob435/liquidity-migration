from __future__ import annotations

import hashlib
import json
import stat
import struct
from pathlib import Path

import pytest

from liquidity_migration.research.venue_wal_accounting import (
    CAPTURE_MAX_WINDOW_MS,
    EvidenceError,
    WAL_MAGIC,
    crc32c,
    _retention_start_ms,
    read_deployment_evidence,
    read_wal_family,
    reconcile,
    write_report,
)

EXPECTED_COMMIT = "a" * 40
ENGINE_BINARY_BYTES = b"fixture engine binary\n"
ENGINE_CONFIG_BYTES = b"[engine]\nfixture = true\n"
EXPECTED_BINARY_SHA256 = hashlib.sha256(ENGINE_BINARY_BYTES).hexdigest()
EXPECTED_CONFIG_SHA256 = hashlib.sha256(ENGINE_CONFIG_BYTES).hexdigest()
OTHER_ARTIFACT_SHA256 = {
    "launcher_sha256": "1" * 64,
    "control_helper_sha256": "2" * 64,
    "controls_sudoers_sha256": "3" * 64,
    "telegram_bot_sha256": "4" * 64,
}


def _frame(record: dict) -> bytes:
    payload = json.dumps(record, separators=(",", ":")).encode()
    return struct.pack("<II", len(payload), crc32c(payload)) + payload


def _write_wal(path: Path, records: list[dict]) -> None:
    path.write_bytes(WAL_MAGIC + b"".join(_frame(record) for record in records))


def _order(client_id: str, side: str, qty: float, reduce_only: bool) -> dict:
    return {
        "kind": "order_sent",
        "request": {
            "client_order_id": client_id,
            "strategy": 1,
            "symbol": 0,
            "side": side,
            "qty": qty,
            "kind": "Market",
            "stop": None,
            "reduce_only": reduce_only,
            "close_position": reduce_only,
        },
        "wire_ns": 1,
        "arrival_mid": 100.0,
    }


def _ack(client_id: str, order_id: str) -> dict:
    return {
        "kind": "order_update",
        "update": {
            "Ack": {
                "client_order_id": client_id,
                "venue_order_id": order_id,
                "sent_ns": 1,
                "ack_ns": 2,
            }
        },
    }


def _fill(
    client_id: str,
    exec_id: str,
    side: str,
    qty: float,
    px: float,
    fee: float,
    venue_ts_ms: int,
) -> dict:
    return {
        "kind": "order_update",
        "update": {
            "Fill": {
                "exec_id": exec_id,
                "client_order_id": client_id,
                "symbol": 0,
                "side": side,
                "qty": qty,
                "px": px,
                "fee": fee,
                "is_maker": False,
                "venue_ts_ms": venue_ts_ms,
                "recv_ns": venue_ts_ms * 1_000_000,
            }
        },
    }


def _wal_records() -> list[dict]:
    return [
        {
            "kind": "boot",
            "version": "engine-core 0.1.0",
            "config_sha256": EXPECTED_CONFIG_SHA256,
            "wall_ts_ms": 900,
        },
        {"kind": "names", "strategies": ["carry", "long"], "symbols": ["BTCUSDT"]},
        _order("long-entry", "Buy", 2.0, False),
        _ack("long-entry", "venue-entry"),
        _fill("long-entry", "exec-entry", "Buy", 2.0, 100.0, 0.10, 1_000),
        _order("long-exit", "Sell", 2.0, True),
        _ack("long-exit", "venue-exit"),
        _fill("long-exit", "exec-exit", "Sell", 2.0, 110.0, 0.11, 2_000),
    ]


def _capture_rows() -> list[dict]:
    return [
        {
            "_kind": "capture",
            "schema_version": 1,
            "complete": True,
            "realm": "demo",
            "api_base_url": "https://api-demo.bybit.com",
            "user_id": "venue-user-1",
            "credential_set": "read-only",
            "start_ms": 0,
            "end_ms_exclusive": 3_000,
            "retention_start_ms": -63_158_397_000,
            "venue_query_start_time_ms": 3_000,
            "venue_query_end_time_ms": 3_000,
            "sources": {
                "execution": {
                    "complete": True,
                    "endpoint": "/v5/execution/list",
                    "params": {"category": "linear", "settleCoin": "USDT", "limit": "100"},
                    "slices": 1,
                    "pages": 1,
                    "rows": 2,
                },
                "closed_pnl": {
                    "complete": True,
                    "endpoint": "/v5/position/closed-pnl",
                    "params": {"category": "linear", "limit": "100"},
                    "slices": 1,
                    "pages": 1,
                    "rows": 1,
                },
                "transaction": {
                    "complete": True,
                    "endpoint": "/v5/account/transaction-log",
                    "params": {
                        "accountType": "UNIFIED",
                        "category": "linear",
                        "currency": "USDT",
                        "limit": "50",
                    },
                    "slices": 1,
                    "pages": 1,
                    "rows": 3,
                },
            },
        },
        {
            "_kind": "execution",
            "execId": "exec-entry",
            "orderLinkId": "long-entry",
            "orderId": "venue-entry",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "execQty": "2",
            "execPrice": "100",
            "execFee": "0.10",
            "execTime": "1000",
            "isMaker": False,
            "execType": "Trade",
        },
        {
            "_kind": "execution",
            "execId": "exec-exit",
            "orderLinkId": "long-exit",
            "orderId": "venue-exit",
            "symbol": "BTCUSDT",
            "side": "Sell",
            "execQty": "2",
            "execPrice": "110",
            "execFee": "0.11",
            "execTime": "2000",
            "isMaker": False,
            "execType": "Trade",
        },
        {
            "_kind": "transaction",
            "id": "txn-entry",
            "tradeId": "exec-entry",
            "orderLinkId": "long-entry",
            "orderId": "venue-entry",
            "symbol": "BTCUSDT",
            "category": "linear",
            "currency": "USDT",
            "type": "TRADE",
            "side": "Buy",
            "qty": "2",
            "size": "2",
            "tradePrice": "100",
            "fee": "0.10",
            "funding": "",
            "cashFlow": "0",
            "change": "-0.10",
            "transactionTime": "1000",
        },
        {
            "_kind": "transaction",
            "id": "txn-funding",
            "tradeId": "funding-stamp",
            "orderLinkId": "",
            "orderId": "settlement-order",
            "symbol": "BTCUSDT",
            "category": "linear",
            "currency": "USDT",
            "type": "SETTLEMENT",
            "side": "Buy",
            "qty": "2",
            "size": "2",
            "tradePrice": "105",
            "fee": "0",
            "funding": "-0.05",
            "cashFlow": "0",
            "change": "-0.05",
            "transactionTime": "1500",
        },
        {
            "_kind": "transaction",
            "id": "txn-exit",
            "tradeId": "exec-exit",
            "orderLinkId": "long-exit",
            "orderId": "venue-exit",
            "symbol": "BTCUSDT",
            "category": "linear",
            "currency": "USDT",
            "type": "TRADE",
            "side": "Sell",
            "qty": "2",
            "size": "0",
            "tradePrice": "110",
            "fee": "0.11",
            "funding": "",
            "cashFlow": "20",
            "change": "19.89",
            "transactionTime": "2000",
        },
        {
            "_kind": "closed_pnl",
            "orderId": "venue-exit",
            "symbol": "BTCUSDT",
            "side": "Sell",
            "execType": "Trade",
            "closedSize": "2",
            "cumEntryValue": "200",
            "cumExitValue": "220",
            "avgEntryPrice": "100",
            "avgExitPrice": "110",
            "openFee": "0.10",
            "closeFee": "0.11",
            "closedPnl": "19.74",
            "fillCount": "1",
            "createdTime": "1900",
            "updatedTime": "2000",
        },
    ]


def _write_capture(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _deployment_kwargs(
    tmp_path: Path,
    *,
    receipt_commit: str = EXPECTED_COMMIT,
    receipt_binary_sha256: str = EXPECTED_BINARY_SHA256,
    binary_bytes: bytes = ENGINE_BINARY_BYTES,
    config_bytes: bytes = ENGINE_CONFIG_BYTES,
    expected_commit: str = EXPECTED_COMMIT,
    expected_binary_sha256: str = EXPECTED_BINARY_SHA256,
    expected_config_sha256: str = EXPECTED_CONFIG_SHA256,
) -> dict:
    receipt = tmp_path / "activation.complete"
    binary = tmp_path / "engine"
    config = tmp_path / "engine.toml"
    receipt.write_text(
        "\n".join(
            [
                f"commit={receipt_commit}",
                f"sha256={receipt_binary_sha256}",
                *(f"{name}={value}" for name, value in OTHER_ARTIFACT_SHA256.items()),
                "",
            ]
        ),
        encoding="ascii",
    )
    binary.write_bytes(binary_bytes)
    config.write_bytes(config_bytes)
    return {
        "deployment_receipt_path": receipt,
        "engine_binary_path": binary,
        "engine_config_path": config,
        "expected_commit": expected_commit,
        "expected_binary_sha256": expected_binary_sha256,
        "expected_config_sha256": expected_config_sha256,
    }


def _report(
    tmp_path: Path,
    rows: list[dict] | None = None,
    *,
    expected_realm: str = "demo",
    expected_user_id: str = "venue-user-1",
    records: list[dict] | None = None,
    deployment_options: dict | None = None,
    trade_execution_id: str | None = None,
) -> dict:
    wal = tmp_path / "engine.wal"
    venue = tmp_path / "venue.jsonl"
    _write_wal(wal, records if records is not None else _wal_records())
    _write_capture(venue, rows or _capture_rows())
    deployment = _deployment_kwargs(tmp_path, **(deployment_options or {}))
    return reconcile(
        wal,
        venue,
        expected_realm=expected_realm,
        expected_user_id=expected_user_id,
        **deployment,
        trade_execution_id=trade_execution_id,
    )


def test_crc32c_matches_the_standard_check_value() -> None:
    assert crc32c(b"123456789") == 0xE3069283


def test_exact_ids_fills_cash_and_funding_make_one_venue_confirmed_trade(tmp_path: Path) -> None:
    report = _report(tmp_path)

    assert report["validity"] == "valid"
    assert report["summary"] == {
        "closed_trades": 1,
        "wal_closed_trades": 1,
        "venue_confirmed": 1,
        "not_venue_confirmed": 0,
        "open_wal_positions": 0,
    }
    trade = report["trades"][0]
    assert trade["status"] == "venue_confirmed"
    assert trade["wal_execution_ids"] == ["exec-entry", "exec-exit"]
    assert trade["venue_order_ids"] == ["venue-entry", "venue-exit"]
    assert trade["settlement_ids"] == ["txn-funding"]
    assert trade["accounting"]["price_fee_net_usdt"] == "19.79"
    assert trade["accounting"]["funding_usdt"] == "-0.05"
    assert trade["accounting"]["venue_confirmed_net_usdt"] == "19.74"
    assert trade["fill_receipts"][0]["wal"]["exec_id"] == "exec-entry"
    assert trade["fill_receipts"][0]["wal"]["active_boot"] == {
        "wal_sequence": 1,
        "version": "engine-core 0.1.0",
        "config_sha256": EXPECTED_CONFIG_SHA256,
        "wall_ts_ms": 900,
    }
    assert trade["fill_receipts"][0]["wal"]["order_boot"] == (
        trade["fill_receipts"][0]["wal"]["active_boot"]
    )
    assert trade["fill_receipts"][0]["venue_execution"]["venue_order_id"] == "venue-entry"
    assert trade["fill_receipts"][0]["venue_transaction"]["transaction_id"] == "txn-entry"
    assert trade["settlement_receipts"][0]["funding"] == "-0.05"
    assert trade["closed_pnl_receipts"][0]["closed_pnl"] == "19.74"
    assert report["deployment"]["activation_receipt"]["commit"] == EXPECTED_COMMIT
    assert report["deployment"]["engine_binary_sha256"] == EXPECTED_BINARY_SHA256
    assert report["deployment"]["engine_config_sha256"] == EXPECTED_CONFIG_SHA256


def test_wrong_expected_account_withholds_venue_confirmation(tmp_path: Path) -> None:
    trade = _report(tmp_path, expected_user_id="other-user")["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("expected venue user" in issue for issue in trade["issues"])


def test_missing_execution_row_withholds_venue_confirmation(tmp_path: Path) -> None:
    rows = [row for row in _capture_rows() if row.get("execId") != "exec-exit"]
    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("exec-exit is absent" in issue for issue in trade["issues"])
    assert trade["accounting"]["venue_confirmed_net_usdt"] is None


def test_wrong_settlement_size_and_amount_withhold_confirmation(tmp_path: Path) -> None:
    rows = _capture_rows()
    settlement = next(row for row in rows if row.get("id") == "txn-funding")
    settlement["size"] = "1"
    settlement["funding"] = "-0.04"
    settlement["change"] = "-0.04"
    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("does not match the held position" in issue for issue in trade["issues"])
    assert any("closed-PnL all-in result differs" in issue for issue in trade["issues"])


def test_legacy_closed_pnl_and_transaction_file_is_diagnostic_only(tmp_path: Path) -> None:
    rows = [row for row in _capture_rows() if row.get("_kind") not in {"capture", "execution"}]
    for row in rows:
        if row.get("_kind") == "transaction":
            row["_kind"] = "txn"
    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("no capture-completeness manifest" in issue for issue in trade["issues"])
    assert any("absent from venue history" in issue for issue in trade["issues"])


def test_torn_wal_tail_is_reported_and_blocks_confirmation(tmp_path: Path) -> None:
    wal = tmp_path / "engine.wal"
    venue = tmp_path / "venue.jsonl"
    _write_wal(wal, _wal_records())
    wal.write_bytes(wal.read_bytes() + b"torn")
    _write_capture(venue, _capture_rows())

    read = read_wal_family(wal)
    report = reconcile(
        wal,
        venue,
        expected_realm="demo",
        expected_user_id="venue-user-1",
        **_deployment_kwargs(tmp_path),
    )

    assert read.damaged
    assert report["trades"][0]["status"] == "not_venue_confirmed"
    assert any("torn or damaged tail" in issue for issue in report["trades"][0]["issues"])


def test_fee_mismatch_is_not_hidden_by_matching_total_pnl(tmp_path: Path) -> None:
    rows = _capture_rows()
    execution = next(row for row in rows if row.get("execId") == "exec-entry")
    execution["execFee"] = "0.09"
    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("execution exec-entry fee differs" in issue for issue in trade["issues"])


def test_repeated_wal_execution_identity_withholds_confirmation(tmp_path: Path) -> None:
    wal = tmp_path / "engine.wal"
    venue = tmp_path / "venue.jsonl"
    records = _wal_records()
    records[-1]["update"]["Fill"]["exec_id"] = "exec-entry"
    _write_wal(wal, records)
    _write_capture(venue, _capture_rows())

    trade = reconcile(
        wal,
        venue,
        expected_realm="demo",
        expected_user_id="venue-user-1",
        **_deployment_kwargs(tmp_path),
    )["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("execution id 'exec-entry' is repeated" in issue for issue in trade["issues"])


def test_settlement_sharing_a_trade_timestamp_withholds_confirmation(tmp_path: Path) -> None:
    rows = _capture_rows()
    settlement = next(row for row in rows if row.get("id") == "txn-funding")
    settlement["transactionTime"] = "1000"

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("shares a timestamp with a trade" in issue for issue in trade["issues"])


def test_foreign_venue_execution_inside_the_trade_withholds_confirmation(tmp_path: Path) -> None:
    rows = _capture_rows()
    foreign_execution = dict(next(row for row in rows if row.get("execId") == "exec-entry"))
    foreign_execution["execId"] = "foreign-exec"
    foreign_execution["execTime"] = "1500"
    rows.append(foreign_execution)

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("foreign trade executions" in issue for issue in trade["issues"])


def test_closed_pnl_timestamp_cannot_precede_the_last_fill_for_its_order(tmp_path: Path) -> None:
    rows = _capture_rows()
    closed_pnl = next(row for row in rows if row.get("_kind") == "closed_pnl")
    closed_pnl["updatedTime"] = "1999"

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("update time precedes the WAL close" in issue for issue in trade["issues"])


def test_closed_pnl_timestamp_may_follow_the_last_fill_for_its_order(tmp_path: Path) -> None:
    rows = _capture_rows()
    closed_pnl = next(row for row in rows if row.get("_kind") == "closed_pnl")
    closed_pnl["updatedTime"] = "2001"

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "venue_confirmed"


def test_complete_manifest_with_a_truncated_body_withholds_confirmation(tmp_path: Path) -> None:
    rows = [row for row in _capture_rows() if row.get("execId") != "exec-entry"]

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("execution row count differs" in issue for issue in trade["issues"])


def test_manifest_requires_the_exact_seven_day_slice_count(tmp_path: Path) -> None:
    rows = _capture_rows()
    manifest = rows[0]
    end_ms = CAPTURE_MAX_WINDOW_MS + 1
    manifest["end_ms_exclusive"] = end_ms
    manifest["venue_query_start_time_ms"] = end_ms
    manifest["venue_query_end_time_ms"] = end_ms
    manifest["retention_start_ms"] = _retention_start_ms(end_ms)

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    slice_issues = [issue for issue in trade["issues"] if "slice count differs" in issue]
    assert len(slice_issues) == 3
    assert all("manifest=1, expected=2" in issue for issue in slice_issues)


def test_identical_duplicate_venue_identity_withholds_confirmation(tmp_path: Path) -> None:
    rows = _capture_rows()
    rows.append(dict(next(row for row in rows if row.get("execId") == "exec-entry")))
    manifest = rows[0]
    manifest["sources"]["execution"]["rows"] = 3

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("exec-entry' has duplicate rows" in issue for issue in trade["issues"])


def test_tiny_direct_fill_difference_is_not_treated_as_exact(tmp_path: Path) -> None:
    rows = _capture_rows()
    execution = next(row for row in rows if row.get("execId") == "exec-entry")
    execution["execPrice"] = "100.00000000001"

    trade = _report(tmp_path, rows)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("execution exec-entry price differs" in issue for issue in trade["issues"])


def test_fill_before_every_boot_withholds_confirmation(tmp_path: Path) -> None:
    records = _wal_records()
    boot = records.pop(0)
    records.insert(4, boot)

    trade = _report(tmp_path, records=records)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert trade["fill_receipts"][0]["wal"]["active_boot"] is None
    assert any("precedes every Boot" in issue for issue in trade["issues"])


def test_each_fill_uses_its_active_preceding_boot_config(tmp_path: Path) -> None:
    records = _wal_records()
    records.insert(
        -1,
        {
            "kind": "boot",
            "version": "engine-core 0.1.0",
            "config_sha256": "b" * 64,
            "wall_ts_ms": 1_900,
        },
    )

    trade = _report(tmp_path, records=records)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert trade["fill_receipts"][0]["wal"]["active_boot"]["config_sha256"] == (
        EXPECTED_CONFIG_SHA256
    )
    assert trade["fill_receipts"][1]["wal"]["active_boot"]["config_sha256"] == "b" * 64
    assert any("exec-exit uses Boot config" in issue for issue in trade["issues"])


def _recovered_entry_records(order_config_sha256: str) -> list[dict]:
    records = _wal_records()
    records[0]["config_sha256"] = order_config_sha256
    recovered = dict(records[4]["update"]["Fill"])
    recovered["kind"] = "recovered_fill"
    records[4:5] = [
        {
            "kind": "boot",
            "version": "engine-core 0.1.0",
            "config_sha256": EXPECTED_CONFIG_SHA256,
            "wall_ts_ms": 10_000,
        },
        recovered,
    ]
    return records


def test_recovered_fill_binds_order_and_recording_boots_by_wal_sequence(tmp_path: Path) -> None:
    report = _report(tmp_path, records=_recovered_entry_records(EXPECTED_CONFIG_SHA256))

    trade = report["trades"][0]
    assert trade["status"] == "venue_confirmed"
    wal_receipt = trade["fill_receipts"][0]["wal"]
    assert wal_receipt["order_boot"]["wal_sequence"] == 1
    assert wal_receipt["active_boot"]["wal_sequence"] == 5
    assert wal_receipt["active_boot"]["wall_ts_ms"] > wal_receipt["venue_ts_ms"]


def test_recovered_fill_rejects_wrong_order_lineage_boot(tmp_path: Path) -> None:
    trade = _report(tmp_path, records=_recovered_entry_records("b" * 64))["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("for its order-lineage" in issue for issue in trade["issues"])


@pytest.mark.parametrize("symbol", [None, ""])
def test_long_fill_with_missing_or_blank_symbol_is_a_global_blocker(
    tmp_path: Path, symbol: object
) -> None:
    records = _wal_records()
    records[4]["update"]["Fill"]["symbol"] = symbol

    trade = _report(tmp_path, records=records)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("has an unresolved symbol" in issue for issue in trade["issues"])


@pytest.mark.parametrize("quantity", [None, 0])
def test_long_fill_with_missing_or_nonpositive_quantity_is_a_global_blocker(
    tmp_path: Path, quantity: object
) -> None:
    records = _wal_records()
    malformed = _fill("long-entry", "malformed-extra", "Buy", 1.0, 105.0, 0.01, 1_500)
    malformed["update"]["Fill"]["qty"] = quantity
    records.insert(-3, malformed)

    trade = _report(tmp_path, records=records)["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("has no positive quantity" in issue for issue in trade["issues"])


def test_wrong_expected_commit_withholds_confirmation(tmp_path: Path) -> None:
    trade = _report(
        tmp_path,
        deployment_options={"expected_commit": "b" * 40},
    )["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("receipt commit differs" in issue for issue in trade["issues"])


def test_wrong_receipt_and_actual_binary_digests_withhold_confirmation(tmp_path: Path) -> None:
    receipt_mismatch = _report(
        tmp_path,
        deployment_options={"receipt_binary_sha256": "b" * 64},
    )["trades"][0]

    assert receipt_mismatch["status"] == "not_venue_confirmed"
    assert any("receipt engine digest differs" in issue for issue in receipt_mismatch["issues"])
    assert any("binary SHA-256 differs from the activation receipt" in issue for issue in receipt_mismatch["issues"])


def test_wrong_actual_binary_digest_withholds_confirmation(tmp_path: Path) -> None:
    trade = _report(
        tmp_path,
        deployment_options={"binary_bytes": b"different engine binary\n"},
    )["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("binary SHA-256 differs from the activation receipt" in issue for issue in trade["issues"])
    assert any("binary SHA-256 differs from the expected" in issue for issue in trade["issues"])


def test_wrong_actual_config_digest_withholds_confirmation(tmp_path: Path) -> None:
    trade = _report(
        tmp_path,
        deployment_options={"config_bytes": b"different config bytes\n"},
    )["trades"][0]

    assert trade["status"] == "not_venue_confirmed"
    assert any("config SHA-256 differs" in issue for issue in trade["issues"])


def test_activation_receipt_schema_is_exact_and_ordered(tmp_path: Path) -> None:
    arguments = _deployment_kwargs(tmp_path)
    receipt = arguments["deployment_receipt_path"]
    lines = receipt.read_text(encoding="ascii").splitlines()
    receipt.write_text("\n".join([lines[1], lines[0], *lines[2:], ""]), encoding="ascii")

    with pytest.raises(EvidenceError, match="missing or out of order"):
        read_deployment_evidence(
            receipt,
            arguments["engine_binary_path"],
            arguments["engine_config_path"],
            expected_commit=arguments["expected_commit"],
            expected_binary_sha256=arguments["expected_binary_sha256"],
            expected_config_sha256=arguments["expected_config_sha256"],
        )


def test_exact_execution_selector_ignores_other_closed_long_trades(tmp_path: Path) -> None:
    records = _wal_records()
    records.extend(
        [
            _order("proxy-entry", "Buy", 1.0, False),
            _ack("proxy-entry", "proxy-venue-entry"),
            _fill("proxy-entry", "proxy-exec-entry", "Buy", 1.0, 120.0, 0.06, 4_000),
            _order("proxy-exit", "Sell", 1.0, True),
            _ack("proxy-exit", "proxy-venue-exit"),
            _fill("proxy-exit", "proxy-exec-exit", "Sell", 1.0, 121.0, 0.06, 5_000),
        ]
    )

    report = _report(tmp_path, records=records, trade_execution_id="exec-entry")

    assert report["summary"]["wal_closed_trades"] == 2
    assert report["summary"]["closed_trades"] == 1
    assert report["trades"][0]["status"] == "venue_confirmed"


def test_report_file_is_new_and_owner_readable_only(tmp_path: Path) -> None:
    path = tmp_path / "accounting.json"

    write_report(path, {"summary": {"venue_confirmed": 1}})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"summary": {"venue_confirmed": 1}}
    with pytest.raises(FileExistsError):
        write_report(path, {"summary": {"venue_confirmed": 0}})
