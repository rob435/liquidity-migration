from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.reconcile_bybit_demo_accounting as accounting_script


class Lease:
    instances: list["Lease"] = []

    def __init__(self, identity: Any) -> None:
        self.identity = identity
        self.acquired = False
        self.closed = False
        self.instances.append(self)

    def acquire(self) -> None:
        self.acquired = True

    def close(self) -> None:
        self.closed = True


class ReadOnlyClient:
    demo = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_api_key_information(self) -> dict[str, Any]:
        self.calls.append(("api_key_information", {}))
        return {"apiKey": "demo-key", "userID": 12345}

    def get_positions(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("positions", params))
        return []

    def get_open_orders(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("open_orders", params))
        return []

    def get_closed_pnl(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("closed_pnl", params))
        return [{"orderId": "close-1"}]

    def get_account_transactions(self, **params: Any) -> list[dict[str, Any]]:
        self.calls.append(("account_transactions", params))
        if params["transaction_type"] == "TRADE":
            return [{"tradeId": "fill-1"}]
        return [{"id": "funding-1"}]


def test_accounting_script_is_owner_serialized_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Lease.instances.clear()
    account_root = tmp_path / "account"
    account_root.mkdir()
    output = tmp_path / "venue-accounting.json"
    client = ReadOnlyClient()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(accounting_script, "DemoAccountMutationLease", Lease)
    monkeypatch.setattr(
        accounting_script,
        "resolve_demo_credentials",
        lambda: ("demo-key", "demo-secret"),
    )
    monkeypatch.setattr(
        accounting_script,
        "BybitPrivateClient",
        lambda **_kwargs: client,
    )

    def build_receipt(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "venue_accounting_gate_passed": True,
            "final_demo_flatness_gate_passed": True,
            "query_window_ms": {"start": 1_000, "end": 5_000},
            "sample_counts": {
                "canonical_targets": 2,
                "canonical_orders": 2,
                "canonical_fills": 2,
            },
            "mismatches": [],
        }

    def write_receipt(path: Path, receipt: dict[str, Any]) -> Path:
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    monkeypatch.setattr(
        accounting_script,
        "build_venue_accounting_receipt",
        build_receipt,
    )
    monkeypatch.setattr(
        accounting_script,
        "write_venue_accounting_receipt",
        write_receipt,
    )

    result = accounting_script.main(
        [
            "--account-root",
            str(account_root),
            "--account-id",
            "bybit-demo-unified",
            "--start-time-ms",
            "1000",
            "--end-time-ms",
            "5000",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert Lease.instances[0].acquired
    assert Lease.instances[0].closed
    assert Lease.instances[0].identity.user_id == "12345"
    assert Lease.instances[0].identity.environment == "demo"
    assert captured["closed_pnl_rows"] == [{"orderId": "close-1"}]
    assert captured["trade_rows"] == [{"tradeId": "fill-1"}]
    assert captured["settlement_rows"] == [{"id": "funding-1"}]
    assert captured["pre_position_rows"] == []
    assert captured["post_open_order_rows"] == []
    transaction_calls = [
        params for name, params in client.calls if name == "account_transactions"
    ]
    assert {call["transaction_type"] for call in transaction_calls} == {
        "TRADE",
        "SETTLEMENT",
    }
    assert all(call["strict"] is True for call in transaction_calls)
    assert all(
        name
        in {
            "api_key_information",
            "positions",
            "open_orders",
            "closed_pnl",
            "account_transactions",
        }
        for name, _ in client.calls
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "passed"
    assert summary["venue_accounting_gate_passed"] is True


def test_accounting_script_refuses_mainnet_before_constructing_owner_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Lease.instances.clear()
    account_root = tmp_path / "account"
    account_root.mkdir()
    monkeypatch.setattr(accounting_script, "DemoAccountMutationLease", Lease)
    monkeypatch.setattr(
        accounting_script,
        "resolve_demo_credentials",
        lambda: (_ for _ in ()).throw(
            RuntimeError("Bybit account cutover refuses REAL_MONEY/mainnet")
        ),
    )

    with pytest.raises(RuntimeError, match="refuses REAL_MONEY/mainnet"):
        accounting_script.main(
            [
                "--account-root",
                str(account_root),
                "--start-time-ms",
                "1000",
                "--end-time-ms",
                "5000",
                "--output",
                str(tmp_path / "receipt.json"),
            ]
        )

    assert Lease.instances == []


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--min-trade-rows", "1", "min_trade_rows"),
        ("--min-closed-pnl-rows", "0", "min_closed_pnl_rows"),
        ("--min-funding-rows", "0", "min_funding_rows"),
        ("--quantity-abs-tolerance", "1e-11", "quantity_abs_tolerance"),
        ("--price-abs-tolerance", "1e-7", "price_abs_tolerance"),
        ("--amount-abs-tolerance", "1e-7", "amount_abs_tolerance"),
        ("--relative-tolerance", "1e-8", "relative_tolerance"),
    ],
)
def test_accounting_script_rejects_weakened_gate_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
    message: str,
) -> None:
    credentials_called = False

    def credentials() -> tuple[str, str]:
        nonlocal credentials_called
        credentials_called = True
        return "demo-key", "demo-secret"

    monkeypatch.setattr(accounting_script, "resolve_demo_credentials", credentials)
    with pytest.raises(SystemExit) as raised:
        accounting_script.main(
            [
                "--account-root",
                str(tmp_path / "missing-account"),
                "--start-time-ms",
                "1000",
                "--end-time-ms",
                "5000",
                "--output",
                str(tmp_path / "receipt.json"),
                option,
                value,
            ]
        )
    assert raised.value.code == 2
    assert message in capsys.readouterr().err
    assert credentials_called is False
