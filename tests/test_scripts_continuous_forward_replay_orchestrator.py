"""Tests for scripts/continuous_forward_replay_orchestrator.py.

forward-replay-5 (audit bucket b02): the orchestrator must isolate per-venue
drift/errors so one bad venue can't abort the run, and must exit non-zero when
the forward clock stalls (a venue drifted/failed) so a scheduled run cannot
silently no-op while forward_days stops advancing.

Relocated from tests/test_audit_fix_b02.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_orchestrator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "continuous_forward_replay_orchestrator.py"
    spec = importlib.util.spec_from_file_location("_orch_b02", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_orchestrator_run_venue_isolates_drift(monkeypatch) -> None:
    """A drift RuntimeError in one venue must be captured as a per-venue 'drift' status (so the
    other venue still runs), NOT propagate and abort the whole run."""
    orch = _load_orchestrator()

    def boom(venue, state_dir, fwd):
        raise RuntimeError(f"{venue}: forward-ledger drift on day 123 column equity: ...")

    monkeypatch.setattr(orch, "venue_update", boom)
    res = orch._run_venue("bybit", Path("/tmp/x"), 0)
    assert res["status"] == "drift"
    assert res["drift_detected"] is True
    assert res["appended_days"] == 0


def test_orchestrator_run_venue_isolates_generic_error(monkeypatch) -> None:
    """A non-drift failure is reported as 'error' (still isolated), not silently swallowed."""
    orch = _load_orchestrator()

    def boom(venue, state_dir, fwd):
        raise FileNotFoundError("no kline partitions")

    monkeypatch.setattr(orch, "venue_update", boom)
    res = orch._run_venue("binance", Path("/tmp/x"), 0)
    assert res["status"] == "error"
    assert res["drift_detected"] is False


def test_orchestrator_main_exits_nonzero_on_stall(monkeypatch, capsys) -> None:
    """A stalled clock (a venue that drifted/failed) must make main() exit non-zero so a manual or
    scheduled run cannot silently no-op while forward_days quietly stops advancing."""
    orch = _load_orchestrator()

    def one_ok_one_drift(venue, state_dir, fwd):
        if venue == "bybit":
            return {"venue": venue, "status": "ok", "appended_days": 3, "drift_detected": False}
        raise RuntimeError(f"{venue}: forward-ledger drift on day 9 column equity")

    monkeypatch.setattr(orch, "venue_update", one_ok_one_drift)
    monkeypatch.setattr("sys.argv", ["orch", "--venues", "bybit,binance", "--state-dir", "/tmp/sd_b02"])
    rc = orch.main()
    assert rc == 1
    out = capsys.readouterr()
    assert "binance" in out.err
