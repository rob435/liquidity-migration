"""audit2b — long-demo cycle telemetry truthfulness (unit: long_demo_telemetry).

Pins the fix for the flagged defect: the long cycle reported
``entries_parallel_workers`` as ``min(max_concurrent_entries, len(candidates))``
(a value that can be >1), but ``_execute_long_entries`` discards
``max_workers``/``private_client_factory`` and submits entries SEQUENTIALLY (one
``place_order`` at a time). The telemetry therefore claimed a parallelism that
never happened. The fix reports the ACTUAL worker count (always 1).

These tests FAIL on the pre-fix code and PASS now:
  * a full submit-mode cycle with 2 candidates + max_concurrent_entries=4 reports
    entries_parallel_workers == 1 (old code reported 2);
  * the dry-run / single-candidate happy path is unchanged (still 1 — guards the
    numerical-equivalence requirement: normal inputs are untouched);
  * _execute_long_entries runs every place_order on the SAME thread even when
    handed max_workers=4 — the behavioral root that makes any >1 count a lie.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import liquidity_migration.long_native_event_demo as lnd
from liquidity_migration.config import ResearchConfig
from liquidity_migration.long_native_event_demo import (
    LongNativeDemoCycleConfig,
    _execute_long_entries,
    run_long_native_demo_cycle,
)


# --------------------------------------------------------------------------- #
# Shared fakes                                                                  #
# --------------------------------------------------------------------------- #
class _ThreadRecordingClient:
    """Private client that records the thread each place_order ran on."""

    def __init__(self) -> None:
        self.place_threads: list[int] = []
        self.set_leverage_calls: list[dict] = []

    def set_leverage(self, **kw: Any) -> dict:
        self.set_leverage_calls.append(kw)
        return {}

    def place_order(self, **params: Any) -> dict:
        self.place_threads.append(threading.get_ident())
        return {"orderId": f"oid-{len(self.place_threads)}"}


def _candidate(symbol: str, signal_ts_ms: int = 1_700_000_000_000) -> dict[str, Any]:
    return {
        "trade_id": f"long-{symbol}-{signal_ts_ms}",
        "symbol": symbol,
        "side": "long",
        "pattern": "fomo_chase",
        "signal_ts_ms": signal_ts_ms,
        "signal_close": 100.0,
        "live_price": 99.0,
        "retrace_threshold": 99.0,
        "sniper_deadline_ms": signal_ts_ms + 6 * lnd.MS_PER_HOUR,
        "entry_reason": "sniper_retrace",
        "entry_ready_ts_ms": signal_ts_ms,
        "stop_loss_pct": 0.1,
        "take_profit_pct": 0.2,
        "max_hold_days": 3,
        "planned_exit_ts_ms": signal_ts_ms + 3 * lnd.MS_PER_DAY,
        "atr_14d_pct": 0.05,
        "realized_vol": 0.5,
        "position_weight": 1.0,
        "candidate_score": 0.2,
        "today_volume_rank": 1.0,
        "entry_policy": "v11a_sniper_retrace_fallthru",
        "entry_quality_tier": "sniper_retrace",
        "entry_rule": "sniper retrace",
    }


def _stub_cycle_dependencies(monkeypatch: pytest.MonkeyPatch, *, candidates: list[dict]) -> None:
    """Patch the heavy collaborators so the cycle reaches the entries-telemetry
    block deterministically and offline. Leaves the real `requested_entry_workers`
    computation + cycle-row assembly (the code under test) untouched."""
    universe = pl.DataFrame(
        {"symbol": [c["symbol"] for c in candidates] or ["AAAUSDT"]}
    )

    monkeypatch.setattr(lnd, "_demo_instruments", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        lnd, "_resolve_ticker_snapshot", lambda *a, **k: ([], "rest")
    )
    monkeypatch.setattr(lnd, "_normalize_tickers", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(lnd, "_build_long_universe", lambda *a, **k: universe)
    monkeypatch.setattr(
        lnd,
        "_resolve_private_snapshot",
        lambda *a, **k: (
            {
                "equity_usdt": 10_000.0,
                "wallet_error": "",
                "raw_open_orders": [],
                "open_order_error": "",
                "raw_positions": [],
                "position_error": "",
            },
            "rest",
        ),
    )
    monkeypatch.setattr(
        lnd, "_download_recent_1h_klines",
        lambda *a, **k: (
            pl.DataFrame(),
            {"cache_rows": 0, "fetched_rows": 0, "store_rows": 0, "store_symbols": 0},
        ),
    )
    monkeypatch.setattr(lnd, "build_long_features", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(
        lnd, "_apply_median_universe_selection", lambda features, **k: (features, 0)
    )
    monkeypatch.setattr(
        lnd, "_price_lookup_from_tickers_and_klines",
        lambda *a, **k: {c["symbol"]: c["live_price"] for c in candidates},
    )
    monkeypatch.setattr(lnd, "_contract_lookup", lambda *a, **k: {})
    monkeypatch.setattr(
        lnd, "_select_long_entry_candidates",
        lambda **k: (list(candidates), {"no_signal": 0}),
    )
    # Entries are a no-op for the telemetry test: we only care about the worker
    # count the cycle REPORTS, not the executed rows.
    monkeypatch.setattr(
        lnd, "_execute_long_entries", lambda *a, **k: ([], [], 0)
    )


def _run_cycle(tmp_path: Path, demo: LongNativeDemoCycleConfig) -> dict[str, Any]:
    return run_long_native_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=demo,
        private_client=_ThreadRecordingClient(),
        now_ms=1_700_000_300_000,
    )


# --------------------------------------------------------------------------- #
# 1. Submit-mode cycle reports the truthful (sequential) worker count           #
# --------------------------------------------------------------------------- #
def test_submit_cycle_reports_truthful_worker_count_of_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """submit_orders=True, max_concurrent_entries=4, 2 candidates: the OLD code
    set entries_parallel_workers = min(4, 2) = 2 and reported it, despite
    sequential execution. The fix reports the ACTUAL worker count, 1."""
    cands = [_candidate("AAAUSDT"), _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000)]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=True,
        confirm_demo_orders=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    # 2 candidates reached the entries block (proves the >1 branch could fire).
    assert payload["cycle"]["entry_candidates"] == 2
    # Truthful: sequential execution => 1 worker. OLD code reported 2 here.
    assert payload["cycle"]["entries_parallel_workers"] == 1


# --------------------------------------------------------------------------- #
# 2. Happy path unchanged (numerical-equivalence guard)                         #
# --------------------------------------------------------------------------- #
def test_dry_run_cycle_worker_count_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal dry-run path never entered the parallel branch and reported 1
    both before and after the fix — the fix must not perturb it."""
    cands = [_candidate("AAAUSDT"), _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000)]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=False,
        record_dry_run=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    assert payload["cycle"]["entry_candidates"] == 2
    assert payload["cycle"]["entries_parallel_workers"] == 1


def test_submit_cycle_single_candidate_worker_count_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single candidate never trips the >1 branch (len(candidates) > 1 is
    False), so both old and new report 1 — the other happy path."""
    cands = [_candidate("AAAUSDT")]
    _stub_cycle_dependencies(monkeypatch, candidates=cands)
    demo = LongNativeDemoCycleConfig(
        submit_orders=True,
        confirm_demo_orders=True,
        max_concurrent_entries=4,
        max_new_entries_per_cycle=5,
        ws_klines_enabled=False,
    )
    payload = _run_cycle(tmp_path, demo)
    assert payload["cycle"]["entry_candidates"] == 1
    assert payload["cycle"]["entries_parallel_workers"] == 1


# --------------------------------------------------------------------------- #
# 3. Behavioral root: _execute_long_entries is sequential regardless of workers #
# --------------------------------------------------------------------------- #
def test_execute_long_entries_runs_on_single_thread_despite_max_workers() -> None:
    """The fact that makes any reported worker count >1 a lie: even handed
    max_workers=4 and a private_client_factory, _execute_long_entries submits
    every entry on the SAME (calling) thread — there is no parallelism."""
    client = _ThreadRecordingClient()
    contracts = {
        "AAAUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
        "BBBUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
        "CCCUSDT": {"tick_size": 0.01, "qty_step": 0.001, "min_order_qty": 0.0,
                    "min_notional_value": 0.0, "max_order_qty": 1e9},
    }
    cands = [
        _candidate("AAAUSDT"),
        _candidate("BBBUSDT", signal_ts_ms=1_700_000_001_000),
        _candidate("CCCUSDT", signal_ts_ms=1_700_000_002_000),
    ]
    demo = LongNativeDemoCycleConfig(
        submit_orders=True, confirm_demo_orders=True, entry_leverage=10.0
    )

    def _factory() -> _ThreadRecordingClient:  # would-be parallel worker client
        return _ThreadRecordingClient()

    _rows, _orders, _skipped = _execute_long_entries(
        cands,
        trading_client=client,
        demo=demo,
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.1,
        price_by_symbol={"AAAUSDT": 99.0, "BBBUSDT": 99.0, "CCCUSDT": 99.0},
        contract_by_symbol=contracts,
        now_ms=1_700_000_300_000,
        strategy_id="s",
        record_preflight=None,
        private_client_factory=_factory,
        execution_event_router=None,
        max_workers=4,  # explicitly request parallelism...
    )
    # ...yet all three place_order calls ran sequentially on this one thread via
    # the single injected trading_client (the factory was never used). Three
    # placements is itself proof the burst was not short-circuited.
    assert len(client.place_threads) == 3
    assert set(client.place_threads) == {threading.get_ident()}
