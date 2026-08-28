from __future__ import annotations

import json

from liquidity_migration.strategy.strategy_host import StrategyHostDaemon


def _heartbeat(*, wall_ts_ms: int, positions: list[dict] | None = None) -> dict:
    return {
        "account_user_id": "555899665",
        "engine_version": "test",
        "entry_blockers": [],
        "working_entries": [],
        "may_open": True,
        "positions": positions or [],
        "realm": "demo",
        "strategies": ["carry"],
        "wall_ts_ms": wall_ts_ms,
        "wire_p50_ns": 10,
    }


def test_telemetry_only_heartbeat_replacements_do_not_wake_a_cycle(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    daemon = object.__new__(StrategyHostDaemon)
    daemon._engine_heartbeat_file = path
    path.write_text(json.dumps(_heartbeat(wall_ts_ms=1_000)), encoding="utf-8")
    before = daemon._engine_wake_projection()

    path.write_text(json.dumps(_heartbeat(wall_ts_ms=2_000)), encoding="utf-8")

    assert daemon._engine_wake_projection() == before


def test_position_or_blocker_change_wakes_a_cycle(tmp_path) -> None:
    path = tmp_path / "heartbeat.json"
    daemon = object.__new__(StrategyHostDaemon)
    daemon._engine_heartbeat_file = path
    path.write_text(json.dumps(_heartbeat(wall_ts_ms=1_000)), encoding="utf-8")
    before = daemon._engine_wake_projection()

    positions = [
        {
            "symbol": "BTCUSDT",
            "side": "long",
            "qty": 1.0,
            "entry_px": 50_000.0,
            "strategy": "carry",
        }
    ]
    path.write_text(
        json.dumps(_heartbeat(wall_ts_ms=2_000, positions=positions)),
        encoding="utf-8",
    )

    assert daemon._engine_wake_projection() != before
