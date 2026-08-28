"""The producer consumes exact, fresh Rust heartbeat account truth."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from liquidity_migration.runtime import engine_account_health

HOUR_NS = 3_600 * 10**9


def _write_heartbeat(path: Path, *, positions: list | None = None, account_user_id: str = "555899665") -> None:
    payload = {
        "account_equity_usdt": 1412.58,
        "account_available_usdt": 700.0,
        "account_observed_wall_ts_ms": int(time.time() * 1000),
        "account_user_id": account_user_id,
        "realm": "demo",
        "positions": positions,
        "entry_blockers": [],
        "working_entries": [],
        "strategies": ["carry", "long"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_held_symbols_come_from_a_recent_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(
        heartbeat,
        positions=[
            {
                "symbol": "HOMEUSDT",
                "side": "long",
                "qty": 10.0,
                "entry_px": 0.5,
                "strategy": "long",
            },
            {
                "symbol": "KAITOUSDT",
                "side": "short",
                "qty": 2.0,
                "entry_px": 1.2,
                "strategy": None,
            },
        ],
    )
    reading = engine_account_health.require_recent_engine_account(
        "demo",
        max_age_ns=HOUR_NS,
        path=heartbeat,
        expected_account_user_id="555899665",
    )
    assert reading.held_symbols == frozenset({"HOMEUSDT", "KAITOUSDT"})


def test_recent_reading_requires_an_expected_account_id(tmp_path: Path, monkeypatch) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat)
    monkeypatch.delenv("EXPECTED_ENGINE_ACCOUNT_USER_ID", raising=False)
    with pytest.raises(ValueError, match="EXPECTED_ENGINE_ACCOUNT_USER_ID"):
        engine_account_health.require_recent_engine_account(
            "demo", max_age_ns=HOUR_NS, path=heartbeat
        )


def test_recent_reading_rejects_a_different_account(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, account_user_id="other")
    with pytest.raises(ValueError, match="not expected account"):
        engine_account_health.require_recent_engine_account(
            "demo",
            max_age_ns=HOUR_NS,
            path=heartbeat,
            expected_account_user_id="555899665",
        )


def test_holdings_and_blockers_are_sleeve_scoped(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(
        heartbeat,
        positions=[
            {
                "symbol": "HOMEUSDT",
                "side": "long",
                "qty": 10.0,
                "entry_px": 0.5,
                "strategy": "long",
            },
            {
                "symbol": "KAITOUSDT",
                "side": "short",
                "qty": 2.0,
                "entry_px": 1.2,
                "strategy": "carry",
            },
        ],
    )
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    payload["entry_blockers"] = [
        {"strategy": "long", "symbol": "XUSDT", "reason": "risk"},
        {"strategy": "carry", "symbol": "XUSDT", "reason": "floor"},
    ]
    payload["working_entries"] = [
        {"strategy": "long", "symbol": "PENDINGUSDT"},
    ]
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    reading = engine_account_health.read_engine_account(heartbeat)

    assert set(reading.holdings_for_strategy("long")) == {"HOMEUSDT"}
    assert set(reading.holdings_for_strategy("carry")) == {"KAITOUSDT"}
    assert reading.entry_blockers_for_strategy("long") == {"XUSDT": "risk"}
    assert reading.entry_blockers_for_strategy("carry") == {"XUSDT": "floor"}
    assert reading.working_entries_for_strategy("long") == frozenset({"PENDINGUSDT"})
    assert reading.working_entries_for_strategy("carry") == frozenset()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_equity_usdt", "1412.58"),
        ("account_available_usdt", True),
        ("account_observed_wall_ts_ms", 1.5),
        ("realm", "paper"),
    ],
)
def test_account_scalar_coercions_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=[])
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    payload[field] = value
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="heartbeat"):
        engine_account_health.read_engine_account(heartbeat)


@pytest.mark.parametrize(
    "positions",
    [
        [{}],
        [{"symbol": 123, "side": "long", "qty": 1.0, "entry_px": 1.0}],
        [{"symbol": "btcusdt", "side": "long", "qty": 1.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "buy", "qty": 1.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": True, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": 0.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": 1.0, "entry_px": True}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": 1.0, "entry_px": 0.0}],
    ],
)
def test_malformed_position_rows_fail_closed(tmp_path: Path, positions: list) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=positions)
    with pytest.raises(ValueError, match="position"):
        engine_account_health.read_engine_account(heartbeat)


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"strategy": "unknown", "symbol": "BTCUSDT"},
        {"strategy": "carry", "symbol": "btcusdt"},
        {"strategy": "carry", "symbol": "BTCUSDT", "reduce_only": False},
    ],
)
def test_malformed_working_entries_fail_closed(tmp_path: Path, row: dict) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=[])
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    payload["working_entries"] = [row]
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="working entry"):
        engine_account_health.read_engine_account(heartbeat)


def test_an_older_heartbeat_without_working_entries_is_unknown(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=[])
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    del payload["working_entries"]
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="working_entries"):
        engine_account_health.read_engine_account(heartbeat)
