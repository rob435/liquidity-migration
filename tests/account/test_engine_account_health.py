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

from liquidity_migration.account import engine_account_health
from liquidity_migration.account.engine_account_health import engine_held_symbols

HOUR_NS = 3_600 * 10**9


def _write_heartbeat(path: Path, *, positions: list | None = None) -> None:
    payload = {
        "account_equity_usdt": 1412.58,
        "account_available_usdt": 700.0,
        "account_observed_wall_ts_ms": int(time.time() * 1000),
        "account_user_id": "555899665",
        "realm": "demo",
        "positions": positions,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_held_symbols_come_from_a_recent_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.json"
    _write_heartbeat(heartbeat, positions=[{"symbol": "homeusdt"}, {"symbol": "KAITOUSDT"}])
    held = engine_held_symbols("demo", max_age_ns=HOUR_NS, path=heartbeat)
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
