"""Regression tests for audit bucket b05.

Covers the round-4 watchdog / liquidation-collector / CI-gate findings:

  * deploy-ci-1 / kill-switch-1: the continuous forward-report timer is monitored
    only under continuous_rmom_refresh_on, so the documented LONG-only kill-switch
    (both continuous sleeves off) no longer pages CRITICAL on an intentionally-
    disabled timer.
  * kill-switch-2: the rmom-refresh service+timer are monitored under the SAME
    predicate (CONTINUOUS_SLEEVE OR CONTINUOUS_PAPER_SLEEVE), not CONTINUOUS_SLEEVE
    alone — so paper-only mode still watches the refresh timer the deploy enables.
  * kill-switch-4: liveness root defaults are anchored at the repo dir, not the CWD,
    so a manual/cron invocation from another directory cannot silently disable a
    safety gather.
  * liquidation-collector-3: per-venue liquidation-capture freshness — a silent
    binance leg is caught even while a healthy bybit leg keeps the root mtime fresh.
  * telegram-alert-1: a dropped resolved note is tracked under a SEPARATE namespace,
    so it can no longer re-arm the alert-side cooldown and suppress a genuine re-fire.
  * telegram-alert-5: a monitored timer's FIRST not-active observation is a debounced
    WARNING; it escalates to CRITICAL only when still not-active on the next run, so a
    deploy-window blip never pages a CRITICAL.
  * liquidation-collector-4: per-line JSONL writes — a disk-full OSError tears at most
    the in-flight line and the dropped counter reflects only the lines that did not land.
  * liquidation-collector-5: the bybit collector writes the in-hand frame BEFORE the
    24h connection-age close, so the rollover frame's rows are not discarded.
  * liquidation-collector-6: per-venue side/price schema is documented and zero/negative
    price rows are dropped the way zero-qty rows are.
  * deploy-ci-2: the deploy workflow has a server-side ruff + full-pytest CI job that
    the deploy job depends on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_demo_liveness.py"

HOUR = 3_600_000
MIN = 60_000


def _load_liveness():
    spec = importlib.util.spec_from_file_location("check_demo_liveness_b05", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_demo_liveness_b05"] = module
    spec.loader.exec_module(module)
    return module


M = _load_liveness()


# --------------------------------------------------------------------------- #
# deploy-ci-1 / kill-switch-1 / kill-switch-2: unit monitoring follows the deploy
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


# --------------------------------------------------------------------------- #
# kill-switch-4: root defaults anchored at the repo dir, not the CWD
# --------------------------------------------------------------------------- #
def test_root_defaults_anchored_at_repo_not_cwd() -> None:
    """A manual/cron invocation from another CWD must not silently disable a safety
    gather: every default root resolves against the repo dir (absolute), not the
    relative working directory."""
    parser = M.build_arg_parser()
    args = parser.parse_args([])
    for attr in ("risk_root", "liquidations_root", "depth_root",
                 "continuous_root", "continuous_paper_root", "long_root", "hedge_root"):
        value = Path(getattr(args, attr))
        assert value.is_absolute(), f"{attr} default must be absolute, got {value}"
        assert value.is_relative_to(REPO_ROOT), f"{attr} must be under the repo dir, got {value}"
    # The documented '' skip sentinel is untouched by the anchoring.
    assert M._default_root("data/x") == str(REPO_ROOT / "data/x")


# --------------------------------------------------------------------------- #
# liquidation-collector-3: per-venue liquidation-capture freshness
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# telegram-alert-5: one-interval timer debounce (WARNING -> CRITICAL)
# --------------------------------------------------------------------------- #
def test_timer_not_active_debounced_warning_then_critical() -> None:
    """A timer's FIRST not-active observation (no prior) is a WARNING; on the SECOND
    consecutive not-active run (in prior) it escalates to CRITICAL. A deploy-window
    blip thus never pages a CRITICAL."""
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


# --------------------------------------------------------------------------- #
# telegram-alert-1: dropped resolved note must NOT re-arm the alert cooldown
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# main() end-to-end: a deploy-window timer blip stays a one-run WARNING
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# liquidation-collector-4/5/6: writer + parser fixes
# --------------------------------------------------------------------------- #
def _liq():
    import liquidity_migration.liquidation_collector as lc
    return lc


def test_writer_per_line_partial_failure_counts_only_unlanded(tmp_path, monkeypatch) -> None:
    """liquidation-collector-4: a disk-full OSError partway through a batch must leave
    every already-written line intact (no torn line) and count ONLY the rows that did
    not land — not the whole batch."""
    lc = _liq()
    w = lc.JsonlDayWriter(tmp_path)

    rows = [
        {"recv_ms": 1_765_000_000_000, "venue": "bybit", "symbol": f"S{i}",
         "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": 1}
        for i in range(5)
    ]

    real_open = Path.open

    class _FailAfter:
        """A file wrapper that raises OSError on the 3rd write (disk full mid-batch)."""

        def __init__(self, fh):
            self._fh = fh
            self._n = 0

        def write(self, data):
            self._n += 1
            if self._n == 3:
                raise OSError("No space left on device")
            return self._fh.write(data)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

    def fake_open(self, *args, **kwargs):
        return _FailAfter(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fake_open)
    w.write(rows)
    monkeypatch.undo()

    # 2 lines landed before the 3rd write raised; 3 did not.
    assert w.written == 2
    assert w.dropped == 3
    assert w.written_by_venue.get("bybit") == 2
    # Every persisted line is a complete JSON record (no torn trailing line).
    import json as _json
    f = next((tmp_path / "bybit").glob("*.jsonl"))
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 2
    for ln in lines:
        _json.loads(ln)  # must not raise


def test_bybit_on_message_writes_before_age_cap_close() -> None:
    """liquidation-collector-5: the bybit on_message must parse-and-write the in-hand
    frame BEFORE the 24h connection-age close, so the rollover frame's rows are not
    discarded. Assert the ordering within the on_message body in the source."""
    import inspect
    lc = _liq()
    src = inspect.getsource(lc._run_bybit)
    # Isolate the on_message body (from its def to the next nested def, on_pong).
    om_start = src.index("def on_message(")
    om_end = src.index("def on_pong(", om_start)
    on_message = src[om_start:om_end]
    # The write must precede the expiry check WITHIN on_message.
    write_idx = on_message.index("writer.write(parse_bybit_event")
    close_idx = on_message.index("_close_if_expired(ws)", write_idx)
    assert close_idx > write_idx, "on_message must write the frame before the age-cap close"
    # The OLD bug — a guard-return at the very top before any parse/write — must be gone:
    # on_message must not return early on expiry before having written the frame.
    first_line = on_message.split("\n", 1)[1].lstrip()
    assert not first_line.startswith("if _close_if_expired(ws):"), \
        "on_message must not short-circuit on expiry before writing the frame"


def test_parse_drops_zero_and_negative_price_rows() -> None:
    """liquidation-collector-6: a row with price<=0 (missing/garbage price) is dropped
    the same way zero-qty rows are, so notional aggregations are never polluted."""
    lc = _liq()
    recv = 1_765_000_000_000

    # Bybit: zero price dropped, positive price kept.
    assert lc.parse_bybit_event(
        {"topic": "allLiquidation.X", "data": {"s": "X", "S": "Buy", "v": "1", "p": "0"}}, recv) == []
    kept = lc.parse_bybit_event(
        {"topic": "allLiquidation.X", "data": {"s": "X", "S": "Buy", "v": "1", "p": "2.5"}}, recv)
    assert len(kept) == 1 and kept[0]["price"] == 2.5

    # Binance: ap=p=0 (the verified pollution case) dropped; ap fallback kept.
    assert lc.parse_binance_event(
        {"e": "forceOrder", "o": {"s": "Y", "S": "SELL", "q": "1", "ap": "0", "p": "0"}}, recv) == []
    kept_b = lc.parse_binance_event(
        {"e": "forceOrder", "o": {"s": "Y", "S": "SELL", "q": "1", "ap": "0", "p": "3"}}, recv)
    assert len(kept_b) == 1 and kept_b[0]["price"] == 3.0


def test_module_documents_per_venue_side_price_schema() -> None:
    """liquidation-collector-6: the module docstring documents the per-venue side
    casing/semantics and the zero-price drop so the eventual P12 consumer cannot
    silently mis-normalize."""
    lc = _liq()
    doc = lc.__doc__ or ""
    assert "Row schema" in doc
    assert "Buy" in doc and "BUY" in doc  # the casing divergence is documented
    assert "LIQUIDATED order" in doc or "liquidated" in doc.lower()
    assert "price > 0" in doc


# --------------------------------------------------------------------------- #
# deploy-ci-2: server-side CI gate
# --------------------------------------------------------------------------- #
def test_vps_deploy_workflow_has_full_suite_ci_gate() -> None:
    """deploy-ci-2: the deploy workflow must run a server-side ruff + full-pytest CI
    job, and the deploy job must depend on it — so an uninstalled local pre-push hook,
    a --no-verify, or a GitHub web edit can no longer auto-deploy untested code."""
    wf = (REPO_ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")

    # A dedicated CI job running the FULL gate (ruff over all three trees + pytest -q).
    assert "ruff check liquidity_migration tests scripts" in wf
    assert "pytest -q" in wf
    # The deploy job gates on it.
    assert "needs: ci" in wf
    # CI runs on PRs too (the deploy steps stay push/dispatch-guarded).
    assert "pull_request:" in wf
    # The deploy job must not touch the box on a PR.
    assert "github.event_name != 'pull_request'" in wf
