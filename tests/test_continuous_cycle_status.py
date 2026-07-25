from __future__ import annotations

import os
from pathlib import Path

import pytest

from liquidity_migration.continuous_cycle_status import (
    ContinuousCycleStatus,
    ContinuousCycleStatusReader,
    continuous_cycle_status_path,
    read_continuous_cycle_status,
    render_continuous_cycle_status,
    write_continuous_cycle_status,
)
from liquidity_migration.strategy_cycle_health import (
    StrategyCycleHealth,
    write_strategy_cycle_health,
)


INVOCATION_ID = "34" * 16


def _status(**overrides: object) -> ContinuousCycleStatus:
    values: dict[str, object] = {
        "cycle_id": "continuous-target-continuous_fade_v2-2000",
        "cycle_ts_ms": 2_000,
        "environment": "demo",
        "btc_trend_gate": "uptrend",
        "btc_trend_gate_allows_entry": False,
        "btc_trend_gate_value": -0.00440801,
        "btc_trend_gate_lookback_days": 30,
        "entry_funnel": (
            {
                "component": "p3",
                "d9": 3,
                "liquidity": 2,
                "event": 2,
                "age": 1,
                "available": 1,
                "capacity": 1,
                "reserved": 0,
                "same_signal_reentry": 0,
            },
            {
                "component": "p4p5",
                "d9": 3,
                "liquidity": 2,
                "event": 1,
                "age": 1,
                "available": 1,
                "capacity": 1,
                "reserved": 0,
                "same_signal_reentry": 0,
            },
        ),
        "qualified_but_blocked": (
            {
                "component": "p3",
                "symbol": "AAAUSDT",
                "first_rejection_reason": "btc_trend_gate",
            },
            {
                "component": "p4p5",
                "symbol": "BBBUSDT",
                "first_rejection_reason": "btc_trend_gate",
            },
        ),
        "entry_first_rejection_reason": "btc_trend_gate",
        "entry_targets_queued": 0,
    }
    values.update(overrides)
    return ContinuousCycleStatus(**values)  # type: ignore[arg-type]


def _health(status: ContinuousCycleStatus, *, completed_ts_ns: int) -> StrategyCycleHealth:
    return StrategyCycleHealth(
        sleeve="continuous",
        environment=status.environment,
        cycle_id=status.cycle_id,
        cycle_ts_ms=status.cycle_ts_ms,
        completed_ts_ns=completed_ts_ns,
        invocation_id=INVOCATION_ID,
        ws_kline_store_rows=100,
    )


def test_status_round_trip_is_private_and_renders_operator_diagnostics(
    tmp_path: Path,
) -> None:
    status = _status()
    path = write_continuous_cycle_status(tmp_path, status)

    assert path == continuous_cycle_status_path(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_continuous_cycle_status(tmp_path) == status
    rendered = render_continuous_cycle_status(status)
    assert "CONTINUOUS BTC gate: BLOCKED · uptrend · 30d -0.44%" in rendered
    assert "CONTINUOUS funnel (component opportunities): " in rendered
    assert "D9 6 → liquidity 4 → event 3 → age 2 → capacity 2" in rendered
    assert "AAAUSDT, BBBUSDT" in rendered
    assert "first rejection btc trend gate" in rendered

    # With a reference instant the block names the cycle it describes, so a
    # reader cannot mistake a minutes-old funnel snapshot for send-time state.
    annotated = render_continuous_cycle_status(
        status,
        now_ns=status.cycle_ts_ms * 1_000_000 + 150_000_000_000,
    )
    assert (
        "CONTINUOUS funnel (component opportunities · cycle 00:00 UTC, "
        "2.5 min before this update): " in annotated
    )


def test_reader_binds_projection_to_receipt_and_marks_stale_cycles(
    tmp_path: Path,
) -> None:
    status = _status(cycle_ts_ms=1_000_000)
    completed_ts_ns = 2_000_000_000_000
    write_continuous_cycle_status(tmp_path, status)
    write_strategy_cycle_health(
        tmp_path,
        _health(status, completed_ts_ns=completed_ts_ns),
    )
    reader = ContinuousCycleStatusReader(
        tmp_path,
        environment="demo",
        max_age_minutes=15.0,
    )

    fresh = reader.render(now_ns=completed_ts_ns + 60_000_000_000)
    assert fresh.startswith("CONTINUOUS BTC gate: BLOCKED")
    assert "cycle 00:16 UTC, 17.7 min before this update" in fresh
    stale = reader.render(now_ns=completed_ts_ns + 16 * 60_000_000_000)
    assert stale == "CONTINUOUS BTC gate: STALE · last completed cycle is 16.0 min old"

    mismatched = _status(
        cycle_id="continuous-target-continuous_fade_v2-2001",
        cycle_ts_ms=1_000_001,
    )
    write_continuous_cycle_status(tmp_path, mismatched)
    restarted = ContinuousCycleStatusReader(tmp_path, environment="demo")
    assert "does not match the completed cycle receipt" in restarted.render(now_ns=completed_ts_ns + 1)


def test_reader_rejects_hardlinked_projection(tmp_path: Path) -> None:
    path = write_continuous_cycle_status(tmp_path, _status())
    os.link(path, tmp_path / "second-link.json")

    with pytest.raises(ValueError, match="hard-linked"):
        read_continuous_cycle_status(tmp_path)


def test_cycle_projection_rejects_non_object_diagnostic_rows() -> None:
    payload = {
        "cycle_id": "continuous-target-test",
        "ts_ms": 1_000,
        "mode": "demo_target",
        "btc_trend_gate": "uptrend",
        "btc_trend_gate_allows_entry": False,
        "btc_trend_gate_value": -0.1,
        "btc_trend_gate_lookback_days": 30,
        "entry_funnel_json": '["not-an-object"]',
        "qualified_but_blocked_json": "[]",
        "entry_first_rejection_reason": "btc_trend_gate",
        "entry_targets_queued": 0,
    }

    with pytest.raises(ValueError, match="non-object"):
        ContinuousCycleStatus.from_cycle_payload(payload)
