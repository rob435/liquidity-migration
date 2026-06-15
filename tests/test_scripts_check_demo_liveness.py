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


def test_unit_states_timers_alert_on_not_active() -> None:
    """REGRESSION (audit 2026-06-12 round 3): a stopped/disabled TIMER reports
    'inactive', never 'failed' — under failed-only alerting a dead hedge timer
    meant a silently unhedged book forever. Timers must be 'active' (waiting)."""
    states = {
        "good.timer": "active",
        "stopped.timer": "inactive",
        "failed.timer": "failed",
        "ok.service": "inactive",  # services: inactive is a normal deploy state
    }
    alerts = {a.key: a for a in M.evaluate_unit_states(states)}
    assert set(alerts) == {"unit:stopped.timer", "unit:failed.timer"}
    assert "never fire" in alerts["unit:stopped.timer"].message


def test_gather_risk_alerts_pages_on_stale_or_missing_heartbeat(tmp_path) -> None:
    """REGRESSION (audit 2026-06-12 round 3): the short-sleeve erasure deleted the
    only ws_risk liveness check — a STOPPED ('inactive') or HUNG ('active') risk
    engine paged nobody while stop repair / orphan adoption / fill reconciliation
    were all dead. The engine heartbeats a cycle row every ~10s; its age is the
    only reliable liveness signal."""
    import argparse

    import polars as pl

    now = 1_000 * HOUR
    args = argparse.Namespace(max_cycle_age_min=10)
    root = tmp_path / "bybit-demo-event"

    # Root missing entirely -> dev-box convention: no alert (other gathers match).
    assert M.gather_risk_alerts(risk_root=root, now_ms=now, args=args) == []

    # Root exists but no cycles ever written -> CRITICAL (never started).
    root.mkdir(parents=True)
    alerts = M.gather_risk_alerts(risk_root=root, now_ms=now, args=args)
    assert [a.key for a in alerts] == ["liveness:ws_risk"]
    assert alerts[0].severity == M.CRITICAL

    # Fresh heartbeat -> clean; stale heartbeat -> CRITICAL.
    cyc_dir = root / "event_demo_cycles" / "date=2024-01-01"
    cyc_dir.mkdir(parents=True)
    pl.DataFrame([{"ts_ms": now - 2 * MIN}]).write_parquet(cyc_dir / "part.parquet")
    assert M.gather_risk_alerts(risk_root=root, now_ms=now, args=args) == []
    pl.DataFrame([{"ts_ms": now - 45 * MIN}]).write_parquet(cyc_dir / "part.parquet")
    alerts = M.gather_risk_alerts(risk_root=root, now_ms=now, args=args)
    assert [a.key for a in alerts] == ["liveness:ws_risk"]
    assert "DOWN/HUNG" in alerts[0].message


def test_gather_liquidation_capture_alerts_freshness(tmp_path) -> None:
    """The collector unit can never reach systemd 'failed' (RestartSec spaces starts
    beyond the start-limit window), so capture death must be caught by JSONL
    freshness (audit 2026-06-12 round 3). Venues that never wrote (region-blocked
    Binance leg) contribute nothing and never alarm."""
    import os

    now_ms = 1_000 * HOUR
    root = tmp_path / "liquidations"
    # Missing root / nothing ever captured -> no alert.
    assert M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3) == []
    (root / "bybit").mkdir(parents=True)
    assert M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3) == []

    f = root / "bybit" / "2024-01-01.jsonl"
    f.write_text("{}\n")
    fresh_s = (now_ms - 30 * MIN) / 1000.0
    os.utime(f, (fresh_s, fresh_s))
    assert M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3) == []

    stale_s = (now_ms - 5 * HOUR) / 1000.0
    os.utime(f, (stale_s, stale_s))
    alerts = M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3)
    # Per-venue keyed (audit 2026-06-12 round 4): the stale leg pages under its own key.
    assert [a.key for a in alerts] == ["liquidation_capture_stale:bybit"]
    assert alerts[0].severity == M.WARNING


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


def test_orphan_hedge_pages_only_when_timer_off_with_open_leg() -> None:
    # Timer enabled -> the daily hedge run manages the leg; never page.
    assert M.evaluate_orphan_hedge(
        hedge_timer_enabled=True,
        open_hedge_trades=[{"symbol": "BTCUSDT", "status": "open"}],
    ) is None
    # Timer disabled but nothing open -> nothing to orphan.
    assert M.evaluate_orphan_hedge(hedge_timer_enabled=False, open_hedge_trades=[]) is None
    assert M.evaluate_orphan_hedge(
        hedge_timer_enabled=False,
        open_hedge_trades=[{"symbol": "BTCUSDT", "status": "closed"}],
    ) is None
    # Timer disabled WITH an open leg -> CRITICAL orphan page (the money risk).
    a = M.evaluate_orphan_hedge(
        hedge_timer_enabled=False,
        open_hedge_trades=[
            {"symbol": "BTCUSDT", "status": "open"},
            {"symbol": "ETHUSDT", "status": "open"},
        ],
    )
    assert a is not None and a.severity == M.CRITICAL
    assert "ORPHANED HEDGE" in a.message and "BTCUSDT" in a.message and "ETHUSDT" in a.message


def test_ws_staleness_threshold() -> None:
    now = 1_000 * HOUR
    assert M.evaluate_ws_staleness(store_max_ts_ms=now - 1 * HOUR, now_ms=now, max_lag_hours=6, label="demo") is None
    stale = M.evaluate_ws_staleness(store_max_ts_ms=now - 8 * HOUR, now_ms=now, max_lag_hours=6, label="demo")
    assert stale is not None and stale.severity == M.WARNING


def test_cooldown_sends_new_suppresses_persisting_then_reresends_and_resolves() -> None:
    now = 1_000 * HOUR
    a = M.Alert(key="liveness:demo", severity=M.CRITICAL, message="down")

    # New condition -> sent, state stamped (cooldown ts + last-sent-severity marker).
    to_send, resolved, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)
    assert [x.key for x in to_send] == ["liveness:demo"] and resolved == []
    assert state == {"liveness:demo": now, f"{M._SEV_PREFIX}liveness:demo": M._SEVERITY_RANK[M.CRITICAL]}

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
    assert "continuous_universe_empty:bybit-continuous-demo-event" in keys
    assert "continuous_kline_store_empty:bybit-continuous-demo-event" in keys
    assert "stop_verify_unavailable_continuous" in keys

    # Sleeve toggled OFF (cycle_checks=False): daemon-liveness/rmom/universe checks
    # skip — an off daemon is intentional — but the stop/mismatch check on the
    # residual open row STILL runs, because "off" does not flatten (round 3).
    keys_off = {
        a.key
        for a in M.gather_continuous_alerts(
            continuous_root=root, now_ms=now, args=args, cycle_checks=False
        )
    }
    assert keys_off == {"stop_verify_unavailable_continuous"}


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
    # Label-keyed (audit 2026-06-12 round 3): demo and paper trip these
    # independently; a shared fixed key let one sleeve's cooldown suppress the
    # other's alert.
    assert "continuous_universe_empty:bybit-continuous-paper-event" in keys
    assert "continuous_kline_store_empty:bybit-continuous-paper-event" in keys
    assert "stop_verify_unavailable_continuous" not in keys


def test_sleeve_kill_switch_toggle(monkeypatch) -> None:
    """The watchdog skips an intentionally-off sleeve. Explicit env always wins; the
    unset-default mirrors deploy/lib_sleeves.sh: EVERY sleeve fails safe to OFF
    (audit 2026-06-12 round 3 — a missing sleeves.env must never resurrect an
    order-submitting sleeve)."""
    for off in ("off", "OFF", "false", "0", "no"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", off)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False, off
    for on in ("on", "ON", "1", "true", "yes"):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", on)
        assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is True, on
    # Unset -> fail-safe default OFF for every sleeve.
    monkeypatch.delenv("CONTINUOUS_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_SLEEVE", default="off") is False
    monkeypatch.delenv("LONG_SLEEVE", raising=False)
    assert M._sleeve_on("LONG_SLEEVE") is False
    monkeypatch.delenv("CONTINUOUS_PAPER_SLEEVE", raising=False)
    assert M._sleeve_on("CONTINUOUS_PAPER_SLEEVE") is False


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


def test_failed_telegram_send_does_not_advance_cooldown(tmp_path, monkeypatch, capsys) -> None:
    """REGRESSION (audit 2026-06-12): send_telegram_message returns False (no exception)
    when the TELEGRAM_* env is missing or the API answers non-2xx — the alert was
    invisible AND its cooldown was recorded as sent, suppressing a CRITICAL alert for
    the whole window. An undelivered alert must not advance its cooldown (next run
    retries) and the False outcome must be visible in the journal."""
    state_file = tmp_path / "state.json"
    alert = M.Alert(key="unit:fake.service", severity=M.CRITICAL, message="fake unit failed")

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["fake.service"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"fake.service": "failed"})
    # main() now calls evaluate_unit_states(states, prior_not_active_timers=...) for the
    # one-interval timer debounce, so the stub must accept that keyword (round 4).
    monkeypatch.setattr(M, "evaluate_unit_states", lambda states, *, prior_not_active_timers=None: [alert])
    monkeypatch.setattr(M, "send_telegram_message", lambda line: False)
    monkeypatch.setattr(
        "sys.argv",
        ["check_demo_liveness.py", "--telegram",
         "--continuous-root", "", "--continuous-paper-root", "", "--long-root", "",
         "--risk-root", "", "--liquidations-root", "",
         "--state-file", str(state_file)],
    )
    assert M.main() == 0
    out = capsys.readouterr().out
    assert "telegram send returned False" in out
    # cooldown NOT advanced: the alert key must be absent from the persisted state
    assert "unit:fake.service" not in M._load_state(state_file)

    # delivered send DOES advance the cooldown
    monkeypatch.setattr(M, "send_telegram_message", lambda line: True)
    assert M.main() == 0
    assert "unit:fake.service" in M._load_state(state_file)


# ---- audit2b: cooldown state-file fallback anchored at repo, not CWD (folded from test_audit2b_liveness_cwd.py) ----
def _run_both_roots_skipped(monkeypatch) -> None:
    """Run main() with every root skipped and no explicit --state-file, so the
    state-file path comes purely from the _state_root fallback under audit."""
    # No units / no roots: nothing pages, main() still computes + persists state.
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", "",
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
        ],
    )
    assert M.main() == 0


def test_state_file_fallback_anchored_at_repo_not_cwd(tmp_path, monkeypatch) -> None:
    """REGRESSION: with both sleeve roots skipped, the cooldown state file must land at
    ``_REPO_ROOT/data/.cache/liveness_watchdog.json`` regardless of CWD — NOT under a
    CWD-relative ``data/.cache``. Old code wrote it relative to the run directory.

    To avoid touching the real repo tree, the test points _REPO_ROOT at an isolated
    sandbox and runs main() from a *different* CWD; the state file must follow the
    anchored repo dir, never the CWD.
    """
    sandbox_repo = tmp_path / "repo"
    sandbox_repo.mkdir()
    run_cwd = tmp_path / "elsewhere"
    run_cwd.mkdir()

    monkeypatch.setattr(M, "_REPO_ROOT", sandbox_repo)
    monkeypatch.chdir(run_cwd)
    _run_both_roots_skipped(monkeypatch)

    anchored = sandbox_repo / "data" / ".cache" / "liveness_watchdog.json"
    cwd_relative = run_cwd / "data" / ".cache" / "liveness_watchdog.json"
    assert anchored.exists(), "state file must be anchored under the repo dir"
    assert not cwd_relative.exists(), "state file must NOT be written CWD-relative"


def test_explicit_state_file_unchanged(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: an explicit --state-file is honored verbatim and the
    fallback is never consulted (the fix only touches the both-roots-skipped fallback)."""
    explicit = tmp_path / "custom" / "state.json"
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", "",
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
            "--state-file", str(explicit),
        ],
    )
    assert M.main() == 0
    assert explicit.exists()


def test_continuous_root_still_drives_default_state_dir(tmp_path, monkeypatch) -> None:
    """NORMAL PATH unchanged: when --continuous-root IS provided, the state file still
    defaults to ``<continuous-root>/.cache/liveness_watchdog.json`` — the fallback's
    repo-anchoring change must not perturb the populated-root case."""
    croot = tmp_path / "bybit-continuous-demo-event"
    croot.mkdir()
    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: [])
    monkeypatch.setattr(M, "_unit_states", lambda units: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_demo_liveness.py",
            "--continuous-root", str(croot),
            "--continuous-paper-root", "",
            "--long-root", "",
            "--risk-root", "",
            "--liquidations-root", "",
            "--depth-root", "",
            "--hedge-root", "",
        ],
    )
    assert M.main() == 0
    assert (croot / ".cache" / "liveness_watchdog.json").exists()


# --------------------------------------------------------------------------- #
# audit bucket b05 (round-4 watchdog / kill-switch / telegram findings;
# folded from test_audit_fix_b05.py)
# --------------------------------------------------------------------------- #
def test_forward_report_and_rmom_timers_not_monitored_when_continuous_off(monkeypatch) -> None:
    """deploy-ci-1 / kill-switch-1: with BOTH continuous sleeves off (the documented
    LONG-only kill-switch), the deploy disables the continuous-forward-report and
    rmom-refresh timers (systemctl disable --now -> inactive). The watchdog must NOT
    monitor them in that mode, else it pages CRITICAL forever on a timer the deploy
    intentionally disabled."""
    monkeypatch.setenv("LONG_SLEEVE", "on")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "off")

    units = M._default_units_for_toggles()

    # The forward-report + rmom-refresh service/timer must be ABSENT when both off.
    for u in (
        "liquidity-migration-continuous-forward-report.service",
        "liquidity-migration-continuous-forward-report.timer",
        "liquidity-migration-continuous-rmom-refresh.service",
        "liquidity-migration-continuous-rmom-refresh.timer",
    ):
        assert u not in units, u
    # The unconditional combined-book-report timer (deploy always enables it) stays.
    assert "liquidity-migration-combined-book-report.timer" in units
    # Long sleeve units still present.
    assert "liquidity-migration-bybit-long-demo.service" in units


def test_rmom_refresh_monitored_in_paper_only_mode(monkeypatch) -> None:
    """kill-switch-2: the deploy enables the rmom-refresh timer under
    continuous_rmom_refresh_on (demo OR paper), because the paper shadow follows the
    same residual_momentum.parquet. So with CONTINUOUS_SLEEVE=off but
    CONTINUOUS_PAPER_SLEEVE=on, the refresh timer is enabled and MUST be monitored —
    monitoring it only under CONTINUOUS_SLEEVE left a dead refresh timer unwatched."""
    monkeypatch.setenv("LONG_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_SLEEVE", "off")
    monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", "on")

    units = M._default_units_for_toggles()

    assert "liquidity-migration-continuous-rmom-refresh.service" in units
    assert "liquidity-migration-continuous-rmom-refresh.timer" in units
    assert "liquidity-migration-continuous-forward-report.timer" in units
    # The DEMO-only hedge timer rides CONTINUOUS_SLEEVE alone, so it must be absent.
    assert "liquidity-migration-continuous-hedge.timer" not in units
    assert "liquidity-migration-bybit-continuous-demo.service" not in units
    # The paper daemon IS monitored.
    assert "liquidity-migration-bybit-continuous-paper.service" in units


def test_continuous_rmom_refresh_on_predicate(monkeypatch) -> None:
    """The shared helper must mirror deploy/lib_sleeves.sh continuous_rmom_refresh_on
    exactly: true iff EITHER continuous sleeve is on."""
    for demo, paper, expected in (
        ("off", "off", False),
        ("on", "off", True),
        ("off", "on", True),
        ("on", "on", True),
    ):
        monkeypatch.setenv("CONTINUOUS_SLEEVE", demo)
        monkeypatch.setenv("CONTINUOUS_PAPER_SLEEVE", paper)
        assert M._continuous_rmom_refresh_on() is expected, (demo, paper)
    # Unset both -> fail-safe off.
    monkeypatch.delenv("CONTINUOUS_SLEEVE", raising=False)
    monkeypatch.delenv("CONTINUOUS_PAPER_SLEEVE", raising=False)
    assert M._continuous_rmom_refresh_on() is False


def test_root_defaults_anchored_at_repo_not_cwd() -> None:
    """kill-switch-4: a manual/cron invocation from another CWD must not silently
    disable a safety gather: every default root resolves against the repo dir
    (absolute), not the relative working directory."""
    parser = M.build_arg_parser()
    args = parser.parse_args([])
    for attr in ("risk_root", "liquidations_root", "depth_root",
                 "continuous_root", "continuous_paper_root", "long_root", "hedge_root"):
        value = Path(getattr(args, attr))
        assert value.is_absolute(), f"{attr} default must be absolute, got {value}"
        assert value.is_relative_to(REPO_ROOT), f"{attr} must be under the repo dir, got {value}"
    # The documented '' skip sentinel is untouched by the anchoring.
    assert M._default_root("data/x") == str(REPO_ROOT / "data/x")


def test_silent_venue_caught_while_sibling_keeps_root_fresh(tmp_path) -> None:
    """liquidation-collector-3: a binance leg that wrote before but went silent must
    page even while a healthy bybit leg keeps the WHOLE-ROOT mtime fresh. A whole-root
    mtime check masked this (the 2026-06-10 incident)."""
    import os

    now_ms = 1_000 * HOUR
    root = tmp_path / "liquidations"
    (root / "bybit").mkdir(parents=True)
    (root / "binance").mkdir(parents=True)

    byb = root / "bybit" / "2024-01-01.jsonl"
    byb.write_text("{}\n")
    bin_ = root / "binance" / "2024-01-01.jsonl"
    bin_.write_text("{}\n")

    fresh_s = (now_ms - 10 * MIN) / 1000.0
    stale_s = (now_ms - 6 * HOUR) / 1000.0
    os.utime(byb, (fresh_s, fresh_s))     # bybit fresh -> root mtime looks healthy
    os.utime(bin_, (stale_s, stale_s))    # binance silent for 6h

    alerts = M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3)
    keys = {a.key for a in alerts}
    assert keys == {"liquidation_capture_stale:binance"}
    assert all(a.severity == M.WARNING for a in alerts)
    assert "binance" in alerts[0].message


def test_never_written_venue_does_not_alarm(tmp_path) -> None:
    """A venue that has NEVER written (region-blocked binance leg pending a
    permitted-region host) contributes no files and must NOT alarm — region-quiet is
    not broken (by design)."""
    import os

    now_ms = 1_000 * HOUR
    root = tmp_path / "liquidations"
    (root / "bybit").mkdir(parents=True)
    (root / "binance").mkdir(parents=True)  # exists but never wrote

    byb = root / "bybit" / "2024-01-01.jsonl"
    byb.write_text("{}\n")
    fresh_s = (now_ms - 10 * MIN) / 1000.0
    os.utime(byb, (fresh_s, fresh_s))

    assert M.gather_liquidation_capture_alerts(liquidations_root=root, now_ms=now_ms, max_age_hours=3) == []


def test_all_venues_stopped_each_pages(tmp_path) -> None:
    """If every venue that wrote goes stale, each pages its own per-venue WARNING
    (the all-venues-stopped case is still covered)."""
    import os

    now_ms = 1_000 * HOUR
    root = tmp_path / "liquidations"
    stale_s = (now_ms - 6 * HOUR) / 1000.0
    for venue in ("bybit", "binance"):
        (root / venue).mkdir(parents=True)
        f = root / venue / "2024-01-01.jsonl"
        f.write_text("{}\n")
        os.utime(f, (stale_s, stale_s))

    keys = {a.key for a in M.gather_liquidation_capture_alerts(
        liquidations_root=root, now_ms=now_ms, max_age_hours=3)}
    assert keys == {"liquidation_capture_stale:bybit", "liquidation_capture_stale:binance"}


def test_timer_not_active_debounced_warning_then_critical() -> None:
    """telegram-alert-5: a timer's FIRST not-active observation (no prior) is a WARNING;
    on the SECOND consecutive not-active run (in prior) it escalates to CRITICAL. A
    deploy-window blip thus never pages a CRITICAL."""
    states = {"x.timer": "inactive"}

    first = M.evaluate_unit_states(states, prior_not_active_timers=set())
    assert len(first) == 1 and first[0].key == "unit:x.timer"
    assert first[0].severity == M.WARNING
    assert "debouncing" in first[0].message

    second = M.evaluate_unit_states(states, prior_not_active_timers={"x.timer"})
    assert len(second) == 1 and second[0].severity == M.CRITICAL
    assert "never fire" in second[0].message


def test_timer_warning_to_critical_escalation_sends_inside_cooldown() -> None:
    """The debounced WARNING -> CRITICAL escalation must page IMMEDIATELY, even inside
    the cooldown window — a severity bump must never be swallowed by the cooldown."""
    now = 1_000 * HOUR
    warn = M.Alert(key="unit:x.timer", severity=M.WARNING, message="warn")
    crit = M.Alert(key="unit:x.timer", severity=M.CRITICAL, message="crit")

    # Run 1: WARNING sent, severity marker stamped.
    to_send, _resolved, state = M.select_alerts_to_send(
        active=[warn], state={}, now_ms=now, cooldown_minutes=30)
    assert [a.severity for a in to_send] == [M.WARNING]

    # Run 2 a few minutes later (WELL inside cooldown): same condition now CRITICAL ->
    # must still send because the severity escalated.
    to_send2, _r2, state2 = M.select_alerts_to_send(
        active=[crit], state=state, now_ms=now + 2 * MIN, cooldown_minutes=30)
    assert [a.severity for a in to_send2] == [M.CRITICAL]
    assert state2[f"{M._SEV_PREFIX}unit:x.timer"] == M._SEVERITY_RANK[M.CRITICAL]


def test_dropped_resolved_note_does_not_suppress_genuine_refire() -> None:
    """telegram-alert-1: the resolved-note retry is tracked under the ``resolved:``
    namespace, NOT by re-stamping the bare alert key. So a flapping safety condition
    that clears (resolved note dropped) and re-fires within the cooldown is NOT
    suppressed — it pages again immediately."""
    now = 1_000 * HOUR
    a = M.Alert(key="unprotected:BTCUSDT", severity=M.CRITICAL, message="unprotected")

    # (1) condition fires, sent.
    _ts, _rs, state = M.select_alerts_to_send(active=[a], state={}, now_ms=now, cooldown_minutes=30)
    assert "unprotected:BTCUSDT" in state

    # (2) condition clears -> resolved; the bare cooldown key is dropped. Simulate the
    # main()-side dropped resolved-note retry by re-adding ONLY the resolved: marker.
    _ts2, resolved2, state2 = M.select_alerts_to_send(
        active=[], state=state, now_ms=now + 5 * MIN, cooldown_minutes=30)
    assert resolved2 == ["unprotected:BTCUSDT"]
    assert "unprotected:BTCUSDT" not in state2  # bare cooldown key cleared
    state2[f"{M._RESOLVED_PREFIX}unprotected:BTCUSDT"] = now + 5 * MIN  # pending retry marker

    # (3) condition RE-FIRES well within the original cooldown window -> must send,
    # because the resolved: marker is in a reserved namespace that never arms the
    # alert-side cooldown.
    to_send3, _r3, _s3 = M.select_alerts_to_send(
        active=[a], state=state2, now_ms=now + 10 * MIN, cooldown_minutes=30)
    assert [x.key for x in to_send3] == ["unprotected:BTCUSDT"]


def test_reserved_namespaces_never_treated_as_active_alert_to_resolve() -> None:
    """The reserved bookkeeping namespaces (resolved:/pending_timer:/sev:) must never
    be surfaced as a resolved alert nor arm the cooldown — only the bare alert keys do."""
    now = 1_000 * HOUR
    state = {
        f"{M._PENDING_TIMER_PREFIX}x.timer": now,
        f"{M._SEV_PREFIX}unit:x.timer": 1,
    }
    to_send, resolved, _new = M.select_alerts_to_send(
        active=[], state=state, now_ms=now + MIN, cooldown_minutes=30)
    assert to_send == [] and resolved == []


def test_main_deploy_window_timer_blip_warns_then_self_resolves(tmp_path, monkeypatch, capsys) -> None:
    """End-to-end: a timer momentarily inactive during a deploy pages a WARNING on the
    first run (debounced, no CRITICAL); when it returns active next run it resolves —
    it never escalates to a false CRITICAL."""
    state_file = tmp_path / "state.json"
    sent: list[str] = []

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["blip.timer"])
    monkeypatch.setattr(M, "send_telegram_message", lambda line: sent.append(line) or True)

    common_argv = [
        "check_demo_liveness.py", "--telegram",
        "--continuous-root", "", "--continuous-paper-root", "", "--long-root", "",
        "--risk-root", "", "--liquidations-root", "", "--depth-root", "", "--hedge-root", "",
        "--state-file", str(state_file),
    ]

    # Run 1: timer inactive (deploy window) -> WARNING, NOT CRITICAL.
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "inactive"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out1 = capsys.readouterr().out
    assert "[WARNING]" in out1 and "[CRITICAL]" not in out1
    assert any("debouncing" in s for s in sent)

    # Run 2: timer back to active -> resolved note, still no CRITICAL.
    sent.clear()
    monkeypatch.setattr(M, "_unit_states", lambda units: {"blip.timer": "active"})
    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out2 = capsys.readouterr().out
    assert "resolved" in out2 and "[CRITICAL]" not in out2


def test_main_persistently_dead_timer_escalates_to_critical(tmp_path, monkeypatch, capsys) -> None:
    """A genuinely dead timer (not-active two consecutive runs) escalates from the
    debounced WARNING to a CRITICAL on the second run."""
    state_file = tmp_path / "state.json"

    monkeypatch.setattr(M, "_default_units_for_toggles", lambda: ["dead.timer"])
    monkeypatch.setattr(M, "_unit_states", lambda units: {"dead.timer": "inactive"})
    monkeypatch.setattr(M, "send_telegram_message", lambda line: True)

    common_argv = [
        "check_demo_liveness.py", "--telegram",
        "--continuous-root", "", "--continuous-paper-root", "", "--long-root", "",
        "--risk-root", "", "--liquidations-root", "", "--depth-root", "", "--hedge-root", "",
        "--state-file", str(state_file),
    ]

    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out1 = capsys.readouterr().out
    assert "[WARNING]" in out1 and "[CRITICAL]" not in out1

    monkeypatch.setattr("sys.argv", common_argv)
    assert M.main() == 0
    out2 = capsys.readouterr().out
    assert "[CRITICAL]" in out2
