"""`engine_held_symbols` answers None for every kind of not-knowing.

The producers treat None as "no news" and leave their records alone; an
exception escaping instead crashes a producer pass that has no per-cycle
catch. The mid-read replacement case is the one that shipped wrong: the
engine rewrites its heartbeat every few seconds, so `read_stable_file`'s
RuntimeError ("changed while it was read") is an ordinary race, not a fault.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from liquidity_migration.runtime import engine_account_health
from liquidity_migration.runtime.engine_account_health import engine_held_symbols

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
    held = engine_held_symbols(
        "demo",
        max_age_ns=HOUR_NS,
        path=heartbeat,
        expected_account_user_id="555899665",
    )
    assert held == frozenset({"HOMEUSDT", "KAITOUSDT"})


def test_missing_heartbeat_is_none(tmp_path: Path) -> None:
    absent = tmp_path / "absent.json"
    assert engine_held_symbols("demo", max_age_ns=HOUR_NS, path=absent) is None


def test_mid_read_replacement_is_none(tmp_path: Path, monkeypatch) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat)

    def replaced_mid_read(*args: object, **kwargs: object) -> object:
        raise RuntimeError("engine heartbeat changed while it was read")

    monkeypatch.setattr(engine_account_health, "read_stable_file", replaced_mid_read)
    assert engine_held_symbols("demo", max_age_ns=HOUR_NS, path=heartbeat) is None


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
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    reading = engine_account_health.read_engine_account(heartbeat)

    assert set(reading.holdings_for_strategy("long")) == {"HOMEUSDT"}
    assert set(reading.holdings_for_strategy("carry")) == {"KAITOUSDT"}
    assert reading.entry_blockers_for_strategy("long") == {"XUSDT": "risk"}
    assert reading.entry_blockers_for_strategy("carry") == {"XUSDT": "floor"}


@pytest.mark.parametrize(
    "positions",
    [
        [{}],
        [{"symbol": "btcusdt", "side": "long", "qty": 1.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "buy", "qty": 1.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": 0.0, "entry_px": 1.0}],
        [{"symbol": "BTCUSDT", "side": "long", "qty": 1.0, "entry_px": 0.0}],
    ],
)
def test_malformed_position_rows_fail_closed(tmp_path: Path, positions: list) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=positions)
    with pytest.raises(ValueError, match="position"):
        engine_account_health.read_engine_account(heartbeat)
