from __future__ import annotations

import pytest

from liquidity_migration.incident_simulation import SCENARIOS, run_all_incident_scenarios


def test_all_required_incidents_are_registered_and_deterministic(tmp_path) -> None:
    assert set(SCENARIOS) == {
        "venue_flat_ledger_open",
        "duplicate_fills",
        "missing_websocket_events",
        "partial_closes",
        "reduce_only_rejection",
        "changed_minimum_notional",
        "delayed_hedge",
        "correlated_ten_x_squeeze",
    }
    first = run_all_incident_scenarios(tmp_path / "first")
    second = run_all_incident_scenarios(tmp_path / "second")
    assert {
        name: (
            result.events,
            result.lifecycle_state,
            result.order_version,
            result.position_version,
            result.entry_filled_qty,
            result.closed_qty,
            result.assertions,
        )
        for name, result in first.items()
    } == {
        name: (
            result.events,
            result.lifecycle_state,
            result.order_version,
            result.position_version,
            result.entry_filled_qty,
            result.closed_qty,
            result.assertions,
        )
        for name, result in second.items()
    }


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_incident_replays_without_integrity_error(tmp_path, name: str) -> None:
    result = SCENARIOS[name](tmp_path / name)
    assert result.events > 0
    assert result.lifecycle_state
