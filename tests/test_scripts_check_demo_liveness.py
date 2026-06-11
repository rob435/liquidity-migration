"""Unit tests for the fast liveness/safety watchdog's pure decision logic."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_rmom_staleness_empty_fresh_and_stale() -> None:
    DAY = 24 * HOUR
    now = 1_000 * DAY
    # empty gate (no rmom row) -> CRITICAL silent-blackout
    empty = M.evaluate_rmom_staleness(max_rmom_day_ts=0, now_ms=now, max_stale_days=2, label="cont")
    assert empty is not None and empty.severity == M.CRITICAL and "EMPTY" in empty.message
    # today's row (a few hours ago) -> fresh, no alert
    assert M.evaluate_rmom_staleness(max_rmom_day_ts=now - 3 * HOUR, now_ms=now, max_stale_days=2, label="cont") is None
    # yesterday's row -> within the 2-day window, no alert (refresh ran on time)
    assert M.evaluate_rmom_staleness(max_rmom_day_ts=now - 1 * DAY, now_ms=now, max_stale_days=2, label="cont") is None
    # 3 days stale -> CRITICAL (refresh failed; live decile silently empties)
    stale = M.evaluate_rmom_staleness(max_rmom_day_ts=now - 3 * DAY, now_ms=now, max_stale_days=2, label="cont")
    assert stale is not None and stale.severity == M.CRITICAL and "STALE" in stale.message


def test_unit_states_alert_only_on_terminal_failed() -> None:
    # Transient restart states (activating/deactivating/inactive) must NOT alert —
    # they happen on every deploy; only the terminal 'failed' is unambiguous.
    states = {
        "a.service": "active",
        "b.service": "activating",
        "c.service": "deactivating",
        "d.service": "inactive",
        "e.service": "failed",
    }
    alerts = M.evaluate_unit_states(states)
    assert {a.key for a in alerts} == {"unit:e.service"}
    assert alerts[0].severity == M.CRITICAL


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



def test_gather_long_alerts_covers_cycle_age_and_stop_protection(tmp_path, monkeypatch) -> None:
    """The LONG sleeve runs on its own root with no rmom gate. gather_long_alerts must catch a
    hung/down cycle (the systemd-failed check can't, under Restart=always) and an unprotected or
    unverified open position -- previously the long sleeve had NO liveness/stop coverage at all."""
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10, settle_coin="USDT", max_ws_lag_hours=6)
    long_root = tmp_path / "bybit-long-demo-event"
    long_root.mkdir()
    # Last cycle 60 min ago (threshold 10) -> hung-daemon liveness alert.
    write_dataset(pl.DataFrame([{"cycle_id": "c1", "ts_ms": now - 60 * MIN}]),
                  long_root, "long_native_demo_cycles", partition_by=())
    write_dataset(pl.DataFrame([{"trade_id": "l1", "symbol": "BTCUSDT", "status": "open", "stop_price": 100.0}]),
                  long_root, "long_native_demo_trades", partition_by=())

    # (a) venue reachable, position has NO server-side stop -> unprotected + hung-cycle caught.
    monkeypatch.setattr(M, "_venue_positions",
                        lambda settle_coin="USDT": ({"BTCUSDT": {"size": "1", "stopLoss": ""}}, None))
    keys_a = {a.key for a in M.gather_long_alerts(long_root=long_root, now_ms=now, args=args)}
    assert "liveness:bybit-long-demo-event" in keys_a
    assert "unprotected:BTCUSDT" in keys_a

    # (b) venue probe failed -> long-specific unverified warning, NO false 'protected'.
    monkeypatch.setattr(M, "_venue_positions", lambda settle_coin="USDT": ({}, "RuntimeError: api down"))
    keys_b = {a.key for a in M.gather_long_alerts(long_root=long_root, now_ms=now, args=args)}
    assert "stop_verify_unavailable_long" in keys_b
    assert not any(k.startswith("unprotected:") for k in keys_b)


def test_gather_long_alerts_skips_when_root_absent(tmp_path) -> None:
    import argparse
    args = argparse.Namespace(max_cycle_age_min=10, settle_coin="USDT", max_ws_lag_hours=6)
    assert M.gather_long_alerts(long_root=tmp_path / "absent", now_ms=1_000 * HOUR, args=args) == []


def test_gather_continuous_alerts_warns_on_empty_universe_and_unverified_stop(tmp_path, monkeypatch) -> None:
    """Continuous-sleeve diagnosability: a zero universe / empty kline store is the same
    silent-zero-signal failure as a stale rmom gate (different upstream cause), and a venue-probe
    failure must not leave continuous open positions silently unverified."""
    import argparse

    import polars as pl

    from liquidity_migration.storage import write_dataset

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10, settle_coin="USDT", max_ws_lag_hours=6, max_rmom_stale_days=2.0)
    root = tmp_path / "bybit-continuous-demo-event"
    root.mkdir()
    # Fresh cycle + fresh rmom, but EMPTY universe and EMPTY kline store -> silent-zero-signal.
    write_dataset(pl.DataFrame([{"cycle_id": "c1", "ts_ms": now, "max_rmom_day_ts": now,
                                 "universe_symbols": 0, "kline_store_rows": 0}]),
                  root, "continuous_fade_demo_cycles", partition_by=())
    write_dataset(pl.DataFrame([{"trade_id": "k1", "symbol": "WIFUSDT", "status": "open", "stop_price": 1.0}]),
                  root, "continuous_fade_demo_trades", partition_by=())
    monkeypatch.setattr(M, "_venue_positions", lambda settle_coin="USDT": ({}, "RuntimeError: api down"))
    keys = {a.key for a in M.gather_continuous_alerts(continuous_root=root, now_ms=now, args=args)}
    assert "continuous_universe_empty" in keys
    assert "continuous_kline_store_empty" in keys
    assert "stop_verify_unavailable_continuous" in keys


def test_gather_continuous_paper_alerts_uses_paper_datasets_without_stop_check(tmp_path, monkeypatch) -> None:
    """Paper evidence writes continuous_fade_paper_* datasets and submits no orders, so the
    liveness watchdog must read the paper cycle ledger but skip venue stop verification."""
    import polars as pl

    args = SimpleNamespace(max_cycle_age_min=10, max_rmom_stale_days=2, settle_coin="USDT")
    now = 10_000_000
    root = tmp_path / "bybit-continuous-paper-event"
    root.mkdir()

    from liquidity_migration.storage import write_dataset

    write_dataset(
        pl.DataFrame([
            {
                "ts_ms": now - 60 * 60_000,
                "max_rmom_day_ts": 0,
                "universe_symbols": 0,
                "kline_store_rows": 0,
            }
        ]),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )
    write_dataset(
        pl.DataFrame([{"trade_id": "paper1", "symbol": "WIFUSDT", "status": "open"}]),
        root,
        "continuous_fade_paper_trades",
        partition_by=(),
    )
    monkeypatch.setattr(M, "_venue_positions", lambda settle_coin="USDT": (_ for _ in ()).throw(AssertionError))

    keys = {
        a.key
        for a in M.gather_continuous_alerts(
            continuous_root=root,
            now_ms=now,
            args=args,
            cycles_dataset="continuous_fade_paper_cycles",
            trades_dataset="continuous_fade_paper_trades",
            check_stops=False,
        )
    }

    assert "liveness:bybit-continuous-paper-event" in keys
    assert "rmom:bybit-continuous-paper-event" in keys
    assert "continuous_universe_empty" in keys
    assert "continuous_kline_store_empty" in keys
    assert "stop_verify_unavailable_continuous" not in keys


def test_sleeve_kill_switch_toggle(monkeypatch) -> None:
    """The watchdog skips an intentionally-off sleeve. Explicit env always wins; the
    unset-default is per-sleeve and mirrors deploy/lib_sleeves.sh (continuous off, short/long on)."""
    for off in ("off", "OFF", "false", "0", "no"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", off)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False, off
    for on in ("on", "ON", "1", "true", "yes"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", on)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is True, on
    # Unset -> per-sleeve default: continuous OFF (cannot resurrect the disabled sleeve),
    # short/long ON (identical to before the kill-switch).
    monkeypatch.delenv("CONTINUOUS_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False
    monkeypatch.delenv("SHORT_SLEEVE", raising=False)
    assert M._sleeve_on("SHORT_SLEEVE") is True
    monkeypatch.delenv("CONTINUOUS_PAPER_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_PAPER_SLEEVE") is True


def test_default_unit_monitoring_follows_sleeve_toggles(monkeypatch) -> None:
    monkeypatch.setenv("SHORT_SLEEVE", "off")
    monkeypatch.setenv("SHORT_PAPER_SLEEVE", "off")
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")

    units = M._default_units_for_toggles()

    assert "liquidity-migration-bybit-risk.service" in units
    assert "liquidity-migration-bybit-demo.service" not in units
    assert "liquidity-migration-bybit-paper.service" not in units
    assert "liquidity-migration-bybit-long-demo.service" in units
    assert "liquidity-migration-bybit-long-paper.service" in units
    assert "liquidity-migration-bybit-continuous-demo.service" in units
    assert "liquidity-migration-continuous-hedge.timer" in units
    assert "liquidity-migration-bybit-continuous-paper.service" not in units
