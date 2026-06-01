"""Unit tests for the fast liveness/safety watchdog's pure decision logic."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_demo_liveness.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_demo_liveness", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_demo_liveness"] = module
    spec.loader.exec_module(module)
    return module


M = _load()
HOUR = 3_600_000
MIN = 60_000


def test_cycle_liveness_fresh_vs_stale_vs_missing() -> None:
    now = 1_000 * HOUR
    assert M.evaluate_cycle_liveness(latest_cycle_ts_ms=now - 2 * MIN, now_ms=now, max_age_minutes=10, label="demo") is None
    stale = M.evaluate_cycle_liveness(latest_cycle_ts_ms=now - 30 * MIN, now_ms=now, max_age_minutes=10, label="demo")
    assert stale is not None and stale.severity == M.CRITICAL and "DOWN" in stale.message
    missing = M.evaluate_cycle_liveness(latest_cycle_ts_ms=None, now_ms=now, max_age_minutes=10, label="demo")
    assert missing is not None and missing.severity == M.CRITICAL


def test_unit_states_alert_only_on_not_active() -> None:
    states = {"a.service": "active", "b.service": "activating", "c.service": "failed", "d.service": "inactive"}
    alerts = M.evaluate_unit_states(states)
    keys = {a.key for a in alerts}
    assert keys == {"unit:c.service", "unit:d.service"}  # active + activating tolerated
    assert all(a.severity == M.CRITICAL for a in alerts)


def test_stop_protection_flags_missing_and_wrong_and_mismatch() -> None:
    open_trades = [
        {"symbol": "OKUSDT", "stop_price": 100.0},
        {"symbol": "NOSTOPUSDT", "stop_price": 50.0},
        {"symbol": "WRONGUSDT", "stop_price": 10.0},
        {"symbol": "GONEUSDT", "stop_price": 5.0},
    ]
    venue = {
        "OKUSDT": {"size": "1", "stopLoss": "100.05"},     # within 2% -> protected
        "NOSTOPUSDT": {"size": "2", "stopLoss": ""},        # no server-side stop -> CRITICAL
        "WRONGUSDT": {"size": "3", "stopLoss": "13.0"},     # 30% off -> CRITICAL
        "GONEUSDT": {"size": "0", "stopLoss": ""},          # venue flat but ledger open -> WARNING
    }
    alerts = {a.key: a for a in M.evaluate_stop_protection(open_trades=open_trades, venue_positions=venue)}
    assert "unprotected:OKUSDT" not in alerts
    assert alerts["unprotected:NOSTOPUSDT"].severity == M.CRITICAL
    assert alerts["unprotected:WRONGUSDT"].severity == M.CRITICAL
    assert alerts["mismatch:GONEUSDT"].severity == M.WARNING
    assert "CLOSE THE POSITION MANUALLY" in alerts["unprotected:NOSTOPUSDT"].message


def test_ws_staleness_threshold() -> None:
    now = 1_000 * HOUR
    assert M.evaluate_ws_staleness(store_max_ts_ms=now - 1 * HOUR, now_ms=now, max_lag_hours=6, label="demo") is None
    stale = M.evaluate_ws_staleness(store_max_ts_ms=now - 8 * HOUR, now_ms=now, max_lag_hours=6, label="demo")
    assert stale is not None and stale.severity == M.WARNING


def test_exchange_errors_surface_recent() -> None:
    recent = [
        {"position_report_error": "", "pending_order_fill_errors": 0},
        {"position_report_error": "wallet unavailable", "pending_order_fill_errors": 2},
    ]
    alerts = {a.key: a for a in M.evaluate_exchange_errors(recent=recent, label="demo")}
    assert "exch_pos_err:demo" in alerts
    assert "exch_fill_err:demo" in alerts


def test_cooldown_sends_new_suppresses_persisting_then_reresends_and_resolves() -> None:
    now = 1_000 * HOUR
    a = M.Alert(key="liveness:demo", severity=M.CRITICAL, message="down")

    # New condition -> sent, state stamped.
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)
    assert [x.key for x in to_send] == ["liveness:demo"] and resolved == []
    assert state == {"liveness:demo": now}

    # Persisting within cooldown -> suppressed.
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state=state, now_ms=now + 5 * MIN, cooldown_minutes=30)
    assert to_send == [] and resolved == []

    # Persisting past cooldown -> re-sent.
    later = now + 31 * MIN
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state=state, now_ms=later, cooldown_minutes=30)
    assert [x.key for x in to_send] == ["liveness:demo"] and state["liveness:demo"] == later

    # Condition cleared -> resolved + key dropped.
    to_send, resolved, state = M.select_alerts_to_send(active=[], state=state, now_ms=later + MIN, cooldown_minutes=30)
    assert to_send == [] and resolved == ["liveness:demo"] and state == {}
