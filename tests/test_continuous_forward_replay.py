"""Tests for the no-order forward signal-replay collector (R3, live-readiness)."""

from __future__ import annotations

import json
import logging

import numpy as np
import polars as pl
import pytest

from liquidity_migration.continuous_forward_replay import (
    EQUITY_REL_TOL,
    FROZEN_FORWARD_CONFIG,
    ForwardUpdateResult,
    _ledger_path,
    build_full_ledger,
    forward_readiness_summary,
    frozen_config_hash,
    frozen_hedge_instruments,
    frozen_hedge_mode,
    init_or_check_state,
    update_forward_ledger,
)
from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents

MS_PER_DAY = 86_400_000
T0 = 1_680_307_200_000  # 2023-04-01


def _pieces(n: int = 80) -> tuple[dict, dict, dict]:
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h = {d: (0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(days)}
    pieces = {}
    for j, name in enumerate(FROZEN_FORWARD_CONFIG["weights"]):
        raw = {d: -0.4 * h[d] + 0.0008 + 0.0001 * j for d in days}
        pieces[name] = ContinuousRebalanceComponents(
            days=days,
            raw_by_day=raw,
            gross_by_day=dict(raw),
            cost_events={},
            funding_by_day={},
            active_gross_start={d: 0.0 for d in days},
            impact_exponent=0.5,
        )
    return pieces, h, {d: 0.0001 for d in days}


def test_config_hash_pinning(tmp_path) -> None:
    init_or_check_state(tmp_path)
    init_or_check_state(tmp_path)  # idempotent under the same config
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    cfg["config_hash"] = "deadbeef"
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(RuntimeError, match="config hash mismatch"):
        init_or_check_state(tmp_path)


def test_append_then_idempotent_then_extend(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    cut = T0 + 69 * MS_PER_DAY

    r1 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert isinstance(r1, ForwardUpdateResult)
    assert r1.appended_days == 70 and r1.verified_overlap_days == 0

    r2 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert r2.appended_days == 0 and r2.verified_overlap_days == 70  # idempotent

    r3 = update_forward_ledger(tmp_path, "bybit", full)  # extend to all 80 days
    assert r3.appended_days == 10 and r3.total_days == 80
    assert r3.last_day_ms == T0 + 79 * MS_PER_DAY


def test_overlap_drift_alarm(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    # perturb one input day inside the stored window -> rebuilt ledger drifts
    pieces["turn3p3"].raw_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    pieces["turn3p3"].gross_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    drifted = build_full_ledger(pieces, h, fund)
    with pytest.raises(RuntimeError, match="drift"):
        update_forward_ledger(tmp_path, "bybit", drifted)


def test_readiness_summary_gates(tmp_path) -> None:
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    start = T0 + 40 * MS_PER_DAY
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=start)
    # Contiguous fixture: calendar span == observed rows.
    assert s["forward_days"] == 40
    assert s["ledger_days"] == 40
    assert s["tier3_days_gate_30"] is True
    assert s["tier3_return_positive"] == (s["forward_return_pct"] > 0)
    # object-identity stamp: the forward clock now tracks the live BTC+ETH 2f
    # object (forward-replay-1, operator decision 2026-06-14), so a readiness PASS
    # is forward evidence for the DEPLOYED book.
    assert s["hedge_mode"] == "2f"
    assert s["hedge_instruments"] == ["BTCUSDT", "ETHUSDT"]
    # empty venue -> zero days
    s2 = forward_readiness_summary(tmp_path, "binance", forward_start_ms=start)
    assert s2["forward_days"] == 0


def test_hash_changes_with_config() -> None:
    base = frozen_config_hash()
    tweaked = dict(FROZEN_FORWARD_CONFIG)
    tweaked["weights"] = {**FROZEN_FORWARD_CONFIG["weights"], "turn3p3": 0.31}
    assert frozen_config_hash(tweaked) != base


# --- relocated from test_audit_fix_b04.py (audit bucket b04) -----------------
# forward-replay-1/2/6/7, metrics-3/5/6, sizing-rebalance-2.


def _components(days: list[int]) -> dict[str, ContinuousRebalanceComponents]:
    h = {d: (0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(days)}
    pieces = {}
    for j, name in enumerate(FROZEN_FORWARD_CONFIG["weights"]):
        raw = {d: 0.002 + 0.0001 * j for d in days}
        pieces[name] = ContinuousRebalanceComponents(
            days=days,
            raw_by_day=raw,
            gross_by_day=dict(raw),
            cost_events={},
            funding_by_day={},
            active_gross_start={d: 0.0 for d in days},
            impact_exponent=0.5,
        )
    return pieces, h, {d: 0.0001 for d in days}


# --- forward-replay-2 / forward-replay-6 / metrics-3 / metrics-6 ------------
# Interior calendar gaps must not collapse the return numerator while the year
# denominator stays calendar. The summary builds a gap-filled calendar series so
# total/dd/years/Sharpe share one basis, and forward_days (calendar span) is
# distinct from ledger_days (observed rows).


def test_calendar_gap_does_not_inflate_forward_days_vs_observed_rows(tmp_path) -> None:
    # Six observed rows on calendar days 0,1,2,10,11,12 -> calendar span 13.
    offsets = [0, 1, 2, 10, 11, 12]
    days = [T0 + o * MS_PER_DAY for o in offsets]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=T0)

    assert s["forward_days"] == 13  # calendar span (inclusive)
    assert s["ledger_days"] == 6  # observed rows only
    # The gate must require 30 OBSERVED days, not 30 calendar span. Six rows over a
    # 13-day span must NOT satisfy a 30-day gate (the original code gated on span).
    assert s["tier3_days_gate_30"] is False


def test_forward_metrics_use_gap_filled_calendar_basis(tmp_path) -> None:
    offsets = [0, 1, 2, 10, 11, 12]
    days = [T0 + o * MS_PER_DAY for o in offsets]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=T0)

    # Recompute the expected calendar-basis stats the way the deployed refresh
    # reference (scripts/continuous_deployed_equity_refresh.stats) does: scatter observed returns
    # onto a zero-filled calendar grid.
    ledger = pl.read_csv(tmp_path / "bybit" / "forward_ledger.csv").sort("ts_ms")
    ts = ledger["ts_ms"].to_numpy()
    rets = ledger["basket_return"].to_numpy()
    span = int((ts[-1] - ts[0]) // MS_PER_DAY) + 1
    series = np.zeros(span)
    series[((ts - ts[0]) // MS_PER_DAY).astype(int)] = rets
    eq = np.cumprod(1.0 + series)
    total = float(eq[-1] - 1.0)
    years = (int(ts[-1]) - int(ts[0])) / (365.25 * MS_PER_DAY)
    # metrics-3: the shared annualized_sharpe convention is ddof=1 (sample std).
    sharpe = float(series.mean() / series.std(ddof=1) * (365.25**0.5))

    assert s["forward_return_pct"] == pytest.approx(round(total * 100, 2))
    assert s["forward_sharpe"] == pytest.approx(round(sharpe, 2))
    # MAR numerator (return) and denominator (years) share the calendar basis.
    if s["forward_mar"] is not None:
        dd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
        assert s["forward_mar"] == pytest.approx(round((total / years) / abs(dd), 2))


def test_year_span_has_no_off_by_one_vs_siblings(tmp_path) -> None:
    # metrics-6: years must use the raw span (last-first), NOT span+1 like the
    # gate basis. Build a 401-day contiguous window with a real interior drawdown
    # so MAR is finite, then confirm MAR uses the no-+1 years basis.
    days = [T0 + i * MS_PER_DAY for i in range(401)]
    pieces, h, fund = _components(days)
    # Force a drawdown mid-window in one component so dd < 0 (MAR finite).
    drop_day = days[200]
    for comp in pieces.values():
        comp.raw_by_day[drop_day] = -0.05
        comp.gross_by_day[drop_day] = -0.05
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=T0)

    ledger = pl.read_csv(tmp_path / "bybit" / "forward_ledger.csv").sort("ts_ms")
    ts = ledger["ts_ms"].to_numpy()
    rets = ledger["basket_return"].to_numpy()
    eq = np.cumprod(1.0 + rets)  # contiguous, so calendar == observed
    total = float(eq[-1] - 1.0)
    dd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    assert dd < 0  # a real drawdown exists
    years_no_plus_one = (int(ts[-1]) - int(ts[0])) / (365.25 * MS_PER_DAY)
    years_with_plus_one = s["forward_days"] / 365.25  # span+1 (the old basis)
    expected = round((total / years_no_plus_one) / abs(dd), 2)
    wrong = round((total / years_with_plus_one) / abs(dd), 2)
    assert s["forward_mar"] == pytest.approx(expected)
    # And the two bases are genuinely different on a long window (the bug was real).
    assert expected != wrong


# --- metrics-5: field renamed from tier3_mar_positive to tier3_return_positive
def test_tier3_field_named_for_what_it_tests(tmp_path) -> None:
    days = [T0 + i * MS_PER_DAY for i in range(40)]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=T0)

    assert "tier3_mar_positive" not in s  # misleading MAR name removed
    assert "tier3_return_positive" in s
    assert s["tier3_return_positive"] == (s["forward_return_pct"] > 0)


# --- forward-replay-1 / sizing-rebalance-2: object-identity stamp -----------
def test_readiness_stamps_hedge_mode_so_audit_cannot_conflate(tmp_path) -> None:
    days = [T0 + i * MS_PER_DAY for i in range(40)]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    s = forward_readiness_summary(tmp_path, "bybit", forward_start_ms=T0)

    # forward-replay-1 (operator decision 2026-06-14): the forward clock now tracks
    # the live BTC+ETH 2f object, so the readiness summary stamps "2f" and the audit
    # reads a PASS as forward evidence for the DEPLOYED book.
    assert s["hedge_mode"] == "2f"
    assert s["hedge_instruments"] == ["BTCUSDT", "ETHUSDT"]


def test_hedge_mode_helpers_distinguish_2f_from_btc_only() -> None:
    # The live BTC+ETH 2f object is now the frozen default (forward-replay-1).
    assert frozen_hedge_mode() == "2f"
    assert frozen_hedge_instruments() == ["BTCUSDT", "ETHUSDT"]
    # The helper still distinguishes a single-leg config (legacy / btc_only).
    one_f = {"hedge": {"instrument": "BTCUSDT"}}
    assert frozen_hedge_mode(one_f) == "btc_only"
    assert frozen_hedge_instruments(one_f) == ["BTCUSDT"]


# --- forward-replay-7: equity drift tolerance follows np.allclose policy ----
def test_equity_overlap_tolerance_allows_alpha_neutral_reassociation(tmp_path) -> None:
    days = [T0 + i * MS_PER_DAY for i in range(80)]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)

    # Simulate an alpha-neutral float-order reassociation of the cumulative equity:
    # a relative perturbation strictly inside EQUITY_REL_TOL (so the relative overlap
    # check does NOT alarm) yet large enough on the peak equity to exceed the old pure
    # 1e-9 absolute tol. Choose the bump as the midpoint of the valid window
    # (1e-9/peak, EQUITY_REL_TOL) so it is robust to the equity magnitude (which is lower
    # now that the daily vol adjuster is disabled in the current target).
    eq_old = full["equity"].to_numpy()
    peak = float(np.max(np.abs(eq_old)))
    assert peak > 1.0  # window (1e-9/peak, rel_tol) is non-empty only for peak > 1
    rel_bump = 0.5 * (1e-9 / peak + EQUITY_REL_TOL)
    perturbed = full.with_columns((pl.col("equity") * (1.0 + rel_bump)).alias("equity"))
    # Confirm the perturbation would have tripped a pure-absolute 1e-9 check.
    eq_new = perturbed["equity"].to_numpy()
    assert np.max(np.abs(eq_new - eq_old)) > 1e-9
    # ...but the relative-tolerant overlap check must NOT alarm.
    res = update_forward_ledger(tmp_path, "bybit", perturbed)
    assert res.appended_days == 0
    assert res.verified_overlap_days == 80


def test_equity_overlap_still_alarms_on_real_regression(tmp_path) -> None:
    days = [T0 + i * MS_PER_DAY for i in range(80)]
    pieces, h, fund = _components(days)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)
    # A genuine 1% equity regression must still hard-alarm.
    drifted = full.with_columns((pl.col("equity") * 1.01).alias("equity"))
    with pytest.raises(RuntimeError, match="drift"):
        update_forward_ledger(tmp_path, "bybit", drifted)


# --- relocated from test_audit_int_iC.py (audit bucket iC) ------------------
# forward-replay-5: opt-in self-heal for a LEGITIMATE historical data revision in
# ``update_forward_ledger``. A late-arriving/backfilled partition that the operator
# has confirmed (allow_history_revision=True) must RE-BASE the drifting overlap days
# to the rebuilt values and log the rewrite, instead of the permanent forward-clock
# stall a hard RuntimeError would cause. The default (allow_history_revision=False)
# must keep the hard-drift contract unchanged, and a vanished stored day must never
# be auto-healed. (Reuses the module-level _pieces fixture above.)


def test_default_keeps_hard_drift_contract(tmp_path) -> None:
    """allow_history_revision defaults False -> drift still hard-errors, nothing healed."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    before = _ledger_path(tmp_path, "bybit").read_text(encoding="utf-8")

    # Revise an input day inside the stored window -> rebuilt ledger drifts.
    pieces["turn3p3"].raw_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    pieces["turn3p3"].gross_by_day[T0 + 40 * MS_PER_DAY] += 0.01
    drifted = build_full_ledger(pieces, h, fund)

    with pytest.raises(RuntimeError, match="drift"):
        update_forward_ledger(tmp_path, "bybit", drifted)
    # Stored ledger must be untouched on the hard-error path.
    assert _ledger_path(tmp_path, "bybit").read_text(encoding="utf-8") == before


def test_history_revision_rebases_and_logs(tmp_path, caplog) -> None:
    """allow_history_revision=True re-bases drifting overlap days + logs the rewrite."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    cut = T0 + 69 * MS_PER_DAY
    r0 = update_forward_ledger(tmp_path, "bybit", full, end_day_ms=cut)
    assert r0.appended_days == 70 and r0.rebased_days == 0

    # Legitimately revise a historical day (simulating a backfilled partition that
    # changes that day's rebuilt basket_return), then extend to all 80 days.
    revised_day = T0 + 40 * MS_PER_DAY
    pieces["turn3p3"].raw_by_day[revised_day] += 0.02
    pieces["turn3p3"].gross_by_day[revised_day] += 0.02
    revised = build_full_ledger(pieces, h, fund)

    with caplog.at_level(logging.WARNING, logger="liquidity_migration.continuous_forward_replay"):
        res = update_forward_ledger(tmp_path, "bybit", revised, allow_history_revision=True)

    assert isinstance(res, ForwardUpdateResult)
    # At least the revised day re-based; equity compounds forward so downstream
    # overlap days also drift and re-base. No day is silently dropped.
    assert res.rebased_days >= 1
    assert res.appended_days == 10  # the new days (70..79) still append
    assert res.total_days == 80
    assert res.verified_overlap_days == 70 - res.rebased_days
    assert res.verified_overlap_days + res.rebased_days == 70

    # The rewrite is observable (WARNING log carries the day + the re-base note).
    text = caplog.text
    assert "HISTORY REVISION re-base" in text
    assert "re-based" in text
    assert str(revised_day) in text

    # The stored ledger now reflects the revised inputs: re-reading it and
    # re-running with the SAME revised ledger is idempotent (no further re-base).
    stored = pl.read_csv(_ledger_path(tmp_path, "bybit"))
    fresh = {int(r["ts_ms"]): r for r in revised.filter(pl.col("ts_ms") <= cut).to_dicts()}
    stored_revised = next(r for r in stored.to_dicts() if int(r["ts_ms"]) == revised_day)
    assert stored_revised["basket_return"] == pytest.approx(
        fresh[revised_day]["basket_return"], rel=1e-12, abs=1e-12
    )

    r_again = update_forward_ledger(tmp_path, "bybit", revised, allow_history_revision=True)
    assert r_again.rebased_days == 0
    assert r_again.appended_days == 0
    assert r_again.verified_overlap_days == 80


def test_clean_overlap_does_not_rebase_even_when_allowed(tmp_path) -> None:
    """With the flag on but no drift, behavior is identical: verify + append, no re-base."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full, end_day_ms=T0 + 69 * MS_PER_DAY)
    res = update_forward_ledger(tmp_path, "bybit", full, allow_history_revision=True)
    assert res.rebased_days == 0
    assert res.appended_days == 10
    assert res.verified_overlap_days == 70
    assert res.total_days == 80


def test_vanished_stored_day_never_auto_healed(tmp_path) -> None:
    """A stored day missing from the rebuilt ledger is a coverage loss -> always raises,
    even under allow_history_revision (it is not a value revision)."""
    pieces, h, fund = _pieces(80)
    full = build_full_ledger(pieces, h, fund)
    update_forward_ledger(tmp_path, "bybit", full)  # store all 80 days

    # Rebuild with fewer days so a previously-stored day vanishes from the rebuild.
    pieces_short, h_short, fund_short = _pieces(70)
    short = build_full_ledger(pieces_short, h_short, fund_short)
    with pytest.raises(RuntimeError, match="missing from the rebuilt ledger"):
        update_forward_ledger(tmp_path, "bybit", short, allow_history_revision=True)
