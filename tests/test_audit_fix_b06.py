"""Regression tests for audit bucket b06.

Owned modules:
  - liquidity_migration/reconciliation.py
  - liquidity_migration/depth_collector.py
  - scripts/continuous_ensemble_rebalance_scout.py

Each test would FAIL on the original bug and PASS on the fix. Finding ids are
named in the docstrings/comments so the mapping is auditable.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

import liquidity_migration.depth_collector as depth_collector
from liquidity_migration.continuous_rebalance import (
    ContinuousRebalanceRule,
    ContinuousRebalanceScaleState,
    compute_continuous_rebalance_scale,
)
from liquidity_migration.reconciliation import (
    MS_PER_DAY,
    SNIPER_TRADE_SUFFIX,
    _calendar_metrics,
    _continuous_cycle_daily_returns,
    _fee_adjusted_return,
    _trade_ledger_daily_returns,
    audit_continuous_rebalance_cycles,
    reconcile_paper_demo,
)

REPO = Path(__file__).resolve().parent.parent


def _load_scout_module():
    """Import scripts/continuous_ensemble_rebalance_scout.py as a module so its
    private helpers (_pooled) can be exercised directly."""
    path = REPO / "scripts" / "continuous_ensemble_rebalance_scout.py"
    spec = importlib.util.spec_from_file_location("_b06_ensemble_scout", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# code-quality-4 + cost-funding-2: _fee_adjusted_return source-convention guard
# --------------------------------------------------------------------------


def test_fee_adjusted_return_live_subtracts_fees_only_once() -> None:
    """code-quality-4: a LIVE (gross-of-cost) row has fees subtracted exactly once."""
    row = {
        "net_return": 0.010,  # gross-of-cost: gtr * notional_weight
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=False)
    assert out == pytest.approx(0.010 - (0.6 + 0.6) / 1000.0)


def test_fee_adjusted_return_backtest_does_not_double_subtract_fees() -> None:
    """code-quality-4: a BACKTEST (net-of-cost) row must NOT have fees re-subtracted.

    Original bug: _fee_adjusted_return ALWAYS subtracted fees, so a net-of-cost
    frame had its fees double-counted, understating the return. With net_of_cost=True
    the stored net return passes through unchanged.
    """
    net_of_cost_return = 0.008  # gross + cost_return + funding_return already applied
    row = {
        "net_return": net_of_cost_return,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.6,
        "exit_fee_usdt": 0.6,
    }
    out = _fee_adjusted_return(row, net_of_cost=True)
    assert out == pytest.approx(net_of_cost_return)
    # And demonstrably different from the (wrong) double-subtracted value.
    assert out != pytest.approx(net_of_cost_return - (0.6 + 0.6) / 1000.0)


def test_fee_adjusted_return_folds_in_funding_when_present_usdt() -> None:
    """cost-funding-2: when the live ledger carries a per-trade funding term it is
    folded into the return. A received funding credit (+usdt) improves the return."""
    base = {
        "net_return": 0.010,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    without = _fee_adjusted_return(dict(base), net_of_cost=False)
    with_funding = _fee_adjusted_return({**base, "funding_usdt": 2.0}, net_of_cost=False)
    assert with_funding == pytest.approx(without + 2.0 / 1000.0)


def test_fee_adjusted_return_folds_in_funding_when_present_fractional() -> None:
    """cost-funding-2: a fractional funding_return term is folded in directly."""
    base = {
        "net_return": 0.010,
        "equity_usdt": 1000.0,
        "entry_fee_usdt": 0.5,
        "exit_fee_usdt": 0.5,
    }
    without = _fee_adjusted_return(dict(base), net_of_cost=False)
    with_funding = _fee_adjusted_return({**base, "funding_return": 0.0015}, net_of_cost=False)
    assert with_funding == pytest.approx(without + 0.0015)


def test_trade_ledger_daily_returns_forwards_net_of_cost_flag() -> None:
    """code-quality-4: the daily aggregator forwards net_of_cost so a backtest frame
    is not silently fee-double-subtracted on its way into the daily series."""
    frame = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "net_return": 0.008,
                "equity_usdt": 1000.0,
                "entry_fee_usdt": 0.6,
                "exit_fee_usdt": 0.6,
                "exit_ts_ms": 5 * MS_PER_DAY + 1,
            }
        ]
    )
    live = _trade_ledger_daily_returns(frame, net_of_cost=False)
    backtest = _trade_ledger_daily_returns(frame, net_of_cost=True)
    day = 5 * MS_PER_DAY
    assert backtest[day] == pytest.approx(0.008)
    assert live[day] == pytest.approx(0.008 - (0.6 + 0.6) / 1000.0)
    assert backtest[day] != pytest.approx(live[day])


# --------------------------------------------------------------------------
# metrics-2 + reconciliation-2: _calendar_metrics compounds (not additive)
# --------------------------------------------------------------------------


def test_calendar_metrics_compounds_equity_matching_engine() -> None:
    """metrics-2 / reconciliation-2: equity must COMPOUND (equity *= 1+ret) to match
    the engine's rebalance_scaled_equity, not sum additively (equity += ret)."""
    start = 10 * MS_PER_DAY
    rets = [0.02, -0.01, 0.03, 0.015]
    returns_by_day = {start + i * MS_PER_DAY: r for i, r in enumerate(rets)}
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=start + 3 * MS_PER_DAY)

    # Reference: engine-style compounding equity curve.
    equity = 1.0
    for r in rets:
        equity *= 1.0 + r
    expected_total = equity - 1.0
    assert out["total_return"] == pytest.approx(expected_total)
    # The additive (buggy) total would have been sum(rets); confirm we diverge from it.
    additive_total = sum(rets)
    assert out["total_return"] != pytest.approx(additive_total)
    assert out["equity"][-1]["equity"] == pytest.approx(equity)


def test_calendar_metrics_peak_relative_drawdown() -> None:
    """metrics-2 / reconciliation-2: drawdown is peak-RELATIVE ((equity-peak)/peak),
    not an absolute delta (equity-peak)."""
    start = 10 * MS_PER_DAY  # must be > 0 (the guard returns empty for start_day<=0)
    # up 10%, then down enough to draw down from the peak
    rets = [0.10, -0.20]
    returns_by_day = {start + i * MS_PER_DAY: r for i, r in enumerate(rets)}
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=start + MS_PER_DAY)
    peak = 1.10
    trough = 1.10 * (1.0 - 0.20)
    expected_dd = (trough - peak) / peak
    assert out["max_drawdown"] == pytest.approx(expected_dd)
    # Absolute (buggy) drawdown would be trough-peak (a different number).
    assert out["max_drawdown"] != pytest.approx(trough - peak)


def test_calendar_metrics_continuous_leg_matches_persisted_engine_equity() -> None:
    """reconciliation-2: driving the continuous leg through _calendar_metrics from
    rebalance_scaled_return must reproduce the engine's persisted scaled equity."""
    # Build cycle rows the way the engine persists them: scaled_equity compounds the
    # per-day rebalance_scaled_return.
    scaled_returns = [0.012, -0.008, 0.02, 0.005, -0.011]
    rows = []
    equity = 1.0
    peak = 1.0
    for i, sr in enumerate(scaled_returns):
        equity *= 1.0 + sr
        peak = max(peak, equity)
        day = (100 + i) * MS_PER_DAY
        rows.append(
            {
                "rebalance_day_ts": day,
                "ts_ms": day + 3600_000,
                "rebalance_scaled_return": sr,
                "rebalance_scaled_equity": equity,
                "rebalance_scaled_peak": peak,
            }
        )
    cycles = pl.DataFrame(rows)
    returns_by_day = _continuous_cycle_daily_returns(cycles)
    start = min(returns_by_day)
    end = max(returns_by_day)
    out = _calendar_metrics(returns_by_day, start_day=start, end_day=end)
    # The comparator's last equity point matches the engine's last persisted equity.
    assert out["equity"][-1]["equity"] == pytest.approx(equity)
    assert out["total_return"] == pytest.approx(equity - 1.0)


# --------------------------------------------------------------------------
# reconciliation-4: audit_continuous_rebalance_cycles is O(n) and correct
# --------------------------------------------------------------------------


def _build_consistent_cycle_frame(n_days: int) -> pl.DataFrame:
    """Build a multi-day cycle frame whose persisted rebalance_target_scale and
    scaled equity/peak are internally consistent with the engine rule, so a correct
    audit reports zero scale_mismatches."""
    rule = ContinuousRebalanceRule()
    rows: list[dict] = []
    raw_returns: list[float] = []
    equity = 1.0
    peak = 1.0
    for i in range(n_days):
        day = (200 + i) * MS_PER_DAY
        state = ContinuousRebalanceScaleState(
            prior_raw_returns=tuple(raw_returns),
            prior_scaled_equity=equity if equity > 0.0 else 1.0,
            prior_scaled_peak=max(peak, equity, 1.0),
        )
        # The builder PERSISTS whatever the engine rule computes (it does not assume a
        # fixed scale), so the frame stays internally consistent for any window length.
        scale = compute_continuous_rebalance_scale(state, rule)
        # Small deterministic oscillation: non-zero variance (so the vol-target scale is
        # a realistic varying value, not a degenerate divide-by-1e-6) and a net-positive
        # drift (equity rises, no drawdown-half-scale, book stays solvent).
        raw_ret = 0.004 + 0.001 * ((i % 5) - 2) * 0.1
        scaled_ret = scale * raw_ret
        equity *= 1.0 + scaled_ret
        peak = max(peak, equity)
        rows.append(
            {
                "cycle_id": f"c{i}",
                "rebalance_day_ts": day,
                "ts_ms": day + 3600_000,
                "rebalance_raw_return": raw_ret,
                "rebalance_target_scale": scale,
                "rebalance_scaled_return": scaled_ret,
                "rebalance_scaled_equity": equity,
                "rebalance_scaled_peak": peak,
                "rebalance_resize_orders": 0,
                "rebalance_resize_skipped_same_day": "false",
            }
        )
        raw_returns.append(raw_ret)
    return pl.DataFrame(rows)


def test_audit_continuous_rebalance_cycles_consistent_frame_passes() -> None:
    """reconciliation-4: the refactored single-pass audit recomputes the SAME scale
    per day as the engine; a consistent frame has zero scale_mismatches."""
    cycles = _build_consistent_cycle_frame(40)
    orders = pl.DataFrame(schema={"resize_reason": pl.Utf8})
    out = audit_continuous_rebalance_cycles(cycles, orders)
    assert out["summary"]["scale_mismatches"] == 0
    assert out["summary"]["rebalance_cycles"] == 40
    assert out["summary"]["days"] == 40
    assert out["ok"] is True


def test_audit_continuous_rebalance_cycles_detects_tampered_scale() -> None:
    """reconciliation-4: the refactor must still DETECT a wrong persisted scale. The
    prior-state for each day must come from the days STRICTLY BEFORE it (the per-day
    forward reduction), not from the whole frame including the day itself."""
    cycles = _build_consistent_cycle_frame(40)
    # Corrupt one day's persisted scale.
    rows = cycles.to_dicts()
    rows[20]["rebalance_target_scale"] = rows[20]["rebalance_target_scale"] + 0.5
    tampered = pl.DataFrame(rows)
    orders = pl.DataFrame(schema={"resize_reason": pl.Utf8})
    out = audit_continuous_rebalance_cycles(tampered, orders)
    assert out["summary"]["scale_mismatches"] == 1
    assert any(issue["kind"] == "scale_mismatch" for issue in out["issues"])


def test_audit_prior_state_is_strictly_causal() -> None:
    """reconciliation-4: equivalence with a brute-force O(n^2) reconstruction that
    rescans the frame per row. The refactored single-pass reduction must produce the
    SAME expected scale per day (prior state = days strictly before)."""
    cycles = _build_consistent_cycle_frame(35)
    rule = ContinuousRebalanceRule()
    rows = cycles.to_dicts()

    def brute_force_expected(rows: list[dict], current_day_ts: int) -> float:
        # Independent O(n) rescan of the WHOLE frame for each row (the old shape).
        current_day = (current_day_ts // MS_PER_DAY) * MS_PER_DAY
        latest_by_day: dict[int, tuple[tuple[int, int], float, dict]] = {}
        for idx, r in enumerate(rows):
            d = int(r["rebalance_day_ts"])
            d = (d // MS_PER_DAY) * MS_PER_DAY
            key = (int(r["ts_ms"]), idx)
            if d not in latest_by_day or key > latest_by_day[d][0]:
                latest_by_day[d] = (key, float(r["rebalance_raw_return"]), r)
        prior_days = sorted(d for d in latest_by_day if d < current_day)
        if not prior_days:
            state = ContinuousRebalanceScaleState(prior_raw_returns=())
        else:
            latest = latest_by_day[prior_days[-1]][2]
            eq = float(latest["rebalance_scaled_equity"]) or 1.0
            pk = float(latest["rebalance_scaled_peak"]) or max(eq, 1.0)
            state = ContinuousRebalanceScaleState(
                prior_raw_returns=tuple(latest_by_day[d][1] for d in prior_days),
                prior_scaled_equity=eq if eq > 0.0 else 1.0,
                prior_scaled_peak=max(pk, eq, 1.0),
            )
        return compute_continuous_rebalance_scale(state, rule)

    # Every persisted scale equals the brute-force expectation -> audit must pass clean.
    for r in rows:
        assert r["rebalance_target_scale"] == pytest.approx(
            brute_force_expected(rows, int(r["rebalance_day_ts"]))
        )
    out = audit_continuous_rebalance_cycles(cycles, pl.DataFrame(schema={"resize_reason": pl.Utf8}))
    assert out["summary"]["scale_mismatches"] == 0


# --------------------------------------------------------------------------
# sniper-2: snipe rows excluded from paper/demo pairing, reported separately
# --------------------------------------------------------------------------


def _recon_trade(trade_id: str, *, symbol: str, entry_ts_ms: int) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": "short",
        "signal_ts_ms": entry_ts_ms - 1000,
        "entry_ts_ms": entry_ts_ms,
        "entry_price": 100.0,
        "qty": 1.0,
        "status": "closed",
        "exit_price": 99.0,
        "exit_ts_ms": entry_ts_ms + 3600_000,
        "exit_reason": "tp",
    }


def test_snipe_rows_excluded_from_demo_only_count() -> None:
    """sniper-2: a filled demo snipe has no paper twin BY DESIGN. It must NOT inflate
    demo_only (the slippage/divergence diagnostic) and must be reported separately."""
    paper = pl.DataFrame([_recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000)])
    demo = pl.DataFrame(
        [
            _recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000),
            _recon_trade("ETH-1" + SNIPER_TRADE_SUFFIX, symbol="ETHUSDT", entry_ts_ms=2_000_000),
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    # The shared BTC trade pairs; the snipe is pulled out, so demo_only stays 0.
    assert summary["paired"] == 1
    assert summary["demo_only"] == 0
    assert summary["snipe_demo_only"] == 1
    # The snipe must not be counted in the pairing population.
    assert summary["demo_trades"] == 1


def test_snipe_exclusion_does_not_drop_real_demo_only() -> None:
    """sniper-2 guard: a genuine (non-snipe) demo-only row is still counted."""
    paper = pl.DataFrame([_recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000)])
    demo = pl.DataFrame(
        [
            _recon_trade("BTC-1", symbol="BTCUSDT", entry_ts_ms=1_000_000),
            _recon_trade("ETH-1", symbol="ETHUSDT", entry_ts_ms=2_000_000),  # real demo-only
            _recon_trade("XRP-1" + SNIPER_TRADE_SUFFIX, symbol="XRPUSDT", entry_ts_ms=3_000_000),
        ]
    )
    result = reconcile_paper_demo(paper, demo)
    summary = result["summary"]
    assert summary["paired"] == 1
    assert summary["demo_only"] == 1  # ETH-1 only
    assert summary["snipe_demo_only"] == 1  # XRP snipe reported apart


# --------------------------------------------------------------------------
# depth-collector-2: per-cycle wall-clock budget aborts before bleeding the hour
# --------------------------------------------------------------------------


def test_collect_cycle_aborts_on_budget_and_reports_skipped(tmp_path, monkeypatch) -> None:
    """depth-collector-2: a slow cycle must stop at the wall-clock budget instead of
    bleeding past the hour, and must report how many symbols were skipped."""
    symbols = [f"S{i}USDT" for i in range(10)]

    # Each snapshot advances a fake monotonic clock by 1s; pacing is a no-op.
    clock = {"t": 0.0}
    monkeypatch.setattr(depth_collector.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)

    def fake_snapshot(sym: str) -> dict:
        clock["t"] += 1.0
        return {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0}

    monkeypatch.setattr(depth_collector, "snapshot_symbol", fake_snapshot)
    monkeypatch.setattr(depth_collector.os, "fsync", lambda *_a, **_k: None)

    stats = depth_collector.collect_cycle(tmp_path, symbols, cycle_budget_seconds=3.0)
    # Budget hit after ~3 snapshots; the remaining are skipped, not captured.
    assert stats["skipped"] > 0
    assert stats["ok"] + stats["skipped"] == len(symbols)
    assert stats["ok"] < len(symbols)


def test_collect_cycle_no_budget_collects_all(tmp_path, monkeypatch) -> None:
    """depth-collector-2: with the budget disabled (<=0) every symbol is captured."""
    symbols = [f"S{i}USDT" for i in range(5)]
    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(depth_collector.os, "fsync", lambda *_a, **_k: None)
    monkeypatch.setattr(
        depth_collector,
        "snapshot_symbol",
        lambda sym: {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0},
    )
    stats = depth_collector.collect_cycle(tmp_path, symbols, cycle_budget_seconds=0.0)
    assert stats["ok"] == 5
    assert stats["skipped"] == 0


# --------------------------------------------------------------------------
# depth-collector-3: retention (gzip/prune) + free-disk floor
# --------------------------------------------------------------------------


def test_enforce_retention_gzips_old_and_prunes_ancient(tmp_path) -> None:
    """depth-collector-3: day files older than the gzip window are compressed; files
    past the prune window are deleted; the current day is left untouched."""
    bybit = tmp_path / "bybit"
    bybit.mkdir(parents=True)
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)

    def day_file(days_ago: int) -> Path:
        d = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        p = bybit / f"{d}.jsonl"
        p.write_text('{"recv_ms": 1}\n', encoding="utf-8")
        return p

    current = day_file(0)
    recent = day_file(3)  # inside gzip window -> untouched
    old = day_file(10)  # past gzip window -> gzipped
    ancient = day_file(90)  # past prune window -> deleted

    stats = depth_collector.enforce_retention(
        tmp_path, gzip_after_days=7, prune_after_days=60, now=now
    )
    assert stats["gzipped"] == 1
    assert stats["pruned"] == 1
    assert current.exists()
    assert recent.exists()
    assert not old.exists() and old.with_suffix(".jsonl.gz").exists()
    assert not ancient.exists()


def test_free_disk_bytes_walks_to_existing_ancestor(tmp_path) -> None:
    """depth-collector-3: the free-disk probe resolves to an existing ancestor even
    when the target root does not exist yet (used as the pre-cycle guard)."""
    missing = tmp_path / "does" / "not" / "exist" / "depth"
    free = depth_collector.free_disk_bytes(missing)
    assert isinstance(free, int)
    assert free > 0


def test_min_free_disk_floor_is_defined_and_positive() -> None:
    """depth-collector-3: the guard threshold the daemon loop checks must exist."""
    assert isinstance(depth_collector.MIN_FREE_DISK_BYTES, int)
    assert depth_collector.MIN_FREE_DISK_BYTES > 0


# --------------------------------------------------------------------------
# depth-collector-4: tolerant reader skips a truncated trailing line; flush/fsync
# --------------------------------------------------------------------------


def test_iter_jsonl_rows_tolerates_truncated_trailing_line(tmp_path) -> None:
    """depth-collector-4: a crash mid-write can leave a truncated final line. The
    reader skips ONLY a bad trailing line; a bad EARLIER line is genuine corruption."""
    path = tmp_path / "2026-06-14.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3', encoding="utf-8")  # truncated tail
    rows = list(depth_collector.iter_jsonl_rows(path))
    assert rows == [{"a": 1}, {"b": 2}]


def test_iter_jsonl_rows_raises_on_interior_corruption(tmp_path) -> None:
    """depth-collector-4: a parse error on a NON-final line is real corruption and
    must propagate, not be silently swallowed."""
    path = tmp_path / "2026-06-14.jsonl"
    path.write_text('{"a": 1}\nNOT JSON\n{"c": 3}\n', encoding="utf-8")
    import json as _json

    with pytest.raises(_json.JSONDecodeError):
        list(depth_collector.iter_jsonl_rows(path))


def test_collect_cycle_flushes_to_disk(tmp_path, monkeypatch) -> None:
    """depth-collector-4: the writer flushes at cycle end so the captured rows are
    durably readable (and fsync is invoked)."""
    fsync_calls = {"n": 0}

    def fake_fsync(_fd: int) -> None:
        fsync_calls["n"] += 1

    monkeypatch.setattr(depth_collector.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(depth_collector.os, "fsync", fake_fsync)
    monkeypatch.setattr(
        depth_collector,
        "snapshot_symbol",
        lambda sym: {"recv_ms": 1, "venue": "bybit", "symbol": sym, "mid": 100.0},
    )
    depth_collector.collect_cycle(tmp_path, ["AAAUSDT", "BBBUSDT"], cycle_budget_seconds=0.0)
    assert fsync_calls["n"] >= 1
    # Rows are durably on disk and round-trip through the tolerant reader.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = tmp_path / "bybit" / f"{day}.jsonl"
    rows = list(depth_collector.iter_jsonl_rows(out))
    assert len(rows) == 2


# --------------------------------------------------------------------------
# alpha-scripts-1: NULL MAR is a HARD FAIL of the cross-venue gate / tiebreak
# --------------------------------------------------------------------------


def _scout_row(*, portfolio: str, venue: str, mar, max_drawdown: float, ret: float) -> dict:
    return {
        "venue": venue,
        "portfolio": portfolio,
        "risk_rule": "rr1",
        "component_trades": 10,
        "return": ret,
        "mar": mar,
        "max_drawdown": max_drawdown,
        "worst_day_return": -0.02,
        "delta_return_vs_turn3p3": None,
        "delta_mar_vs_turn3p3": None,
    }


def test_pooled_single_venue_mar_does_not_pass_both_venues_gate() -> None:
    """alpha-scripts-1: a combo with a finite MAR on only ONE venue (None on the
    other) must NOT be flagged target_mar_both, and a null MAR must not win min_mar.

    Original bug: polars .all()/.min() skip nulls, so [7.0, None] flagged
    target_mar_both=True and min_mar=7.0 (a cross-venue pass that never happened).
    """
    scout = _load_scout_module()
    rows = [
        _scout_row(portfolio="p_single", venue="bybit", mar=7.0, max_drawdown=-0.05, ret=1.5),
        _scout_row(portfolio="p_single", venue="binance", mar=None, max_drawdown=-1e-12, ret=1.5),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_single").to_dicts()[0]
    assert record["target_mar_both"] is False
    assert record["n_venues_with_mar"] == 1
    assert record["n_venues"] == 2
    # The null venue cannot win the tiebreak -> min_mar collapses to -inf.
    assert record["min_mar"] == float("-inf")


def test_pooled_both_venues_finite_mar_passes_gate() -> None:
    """alpha-scripts-1 guard: when BOTH venues have a finite MAR clearing the bar the
    gate still passes and min_mar is the real minimum (no false negative)."""
    scout = _load_scout_module()
    rows = [
        _scout_row(portfolio="p_both", venue="bybit", mar=7.0, max_drawdown=-0.05, ret=1.5),
        _scout_row(portfolio="p_both", venue="binance", mar=6.5, max_drawdown=-0.06, ret=1.4),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_both").to_dicts()[0]
    assert record["target_mar_both"] is True
    assert record["n_venues_with_mar"] == 2
    assert record["min_mar"] == pytest.approx(6.5)


def test_pooled_all_null_mar_does_not_pass_gate() -> None:
    """alpha-scripts-1: a fully-degenerate combo (None on both venues) must fail the
    gate; original .all() returned vacuous-True for [None, None]."""
    scout = _load_scout_module()
    rows = [
        _scout_row(portfolio="p_null", venue="bybit", mar=None, max_drawdown=-1e-12, ret=1.5),
        _scout_row(portfolio="p_null", venue="binance", mar=None, max_drawdown=-1e-12, ret=1.5),
    ]
    pooled = scout._pooled(rows)
    record = pooled.filter(pl.col("portfolio") == "p_null").to_dicts()[0]
    assert record["target_mar_both"] is False
    assert record["n_venues_with_mar"] == 0


# --------------------------------------------------------------------------
# alpha-scripts-2: in-sample selection is stamped (cardinality + flag + caveat)
# --------------------------------------------------------------------------


def test_scout_receipt_stamps_in_sample_selection(tmp_path, monkeypatch) -> None:
    """alpha-scripts-2: the run receipt must carry selection_is_in_sample=True, the
    grid cardinality, and a caveat so the output cannot be cited as candidate-grade."""
    import json as _json

    scout = _load_scout_module()

    # Avoid touching real SHARED_DATA: stub the source loader to a tiny synthetic book.
    from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents

    days = [(300 + i) * MS_PER_DAY for i in range(40)]
    comp = ContinuousRebalanceComponents(
        days=days,
        raw_by_day={d: 0.004 for d in days},
        gross_by_day={d: 0.004 for d in days},
        cost_events={d: [] for d in days},
        funding_by_day={d: 0.0 for d in days},
        active_gross_start={d: 1.0 for d in days},
        impact_exponent=0.5,
    )
    monkeypatch.setattr(scout, "_load_source", lambda spec, venue: (comp, 5, None))

    out_dir = tmp_path / "scout_out"
    argv = [
        "continuous_ensemble_rebalance_scout.py",
        "--out",
        str(out_dir),
        "--venues",
        "bybit,binance",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = scout.main()
    assert rc == 0

    receipt = _json.loads((out_dir / "run_receipt.json").read_text(encoding="utf-8"))
    assert receipt["selection_is_in_sample"] is True
    assert receipt["grid_cardinality"] == receipt["n_portfolios"] * receipt["n_risk_rules"]
    assert receipt["grid_cardinality"] > 0
    assert "holdout" in receipt["selection_caveat"].lower()
    assert (out_dir / "pooled.csv").exists()
