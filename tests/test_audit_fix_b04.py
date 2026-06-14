"""Regression tests for audit bucket b04.

Findings: forward-replay-1/2/6/7, metrics-3/5/6, sizing-rebalance-2, ingestion-2,
ingestion-6, reports-charts-1. Each test fails on the original bug and passes on
the root-cause fix.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from liquidity_migration.continuous_forward_replay import (
    EQUITY_REL_TOL,
    build_full_ledger,
    forward_readiness_summary,
    frozen_hedge_instruments,
    frozen_hedge_mode,
    update_forward_ledger,
)
from liquidity_migration.continuous_rebalance import ContinuousRebalanceComponents
from liquidity_migration.downloaders import (
    _funding_interval_min,
    _normalize_funding,
    _normalize_open_interest,
)
from liquidity_migration.event_demo_reports import (
    _telegram_notification_reason,
    format_telegram_status_message,
)

MS_PER_DAY = 86_400_000
T0 = 1_680_307_200_000  # 2023-04-01


def _components(days: list[int]) -> dict[str, ContinuousRebalanceComponents]:
    from liquidity_migration.continuous_forward_replay import FROZEN_FORWARD_CONFIG

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

    # Recompute the expected calendar-basis stats the way the deployed reference
    # (scripts/continuous_deployed_equity.stats) does: scatter observed returns
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
    # check does NOT alarm) yet large enough on the >1.0 peak equity to exceed the old
    # pure 1e-9 absolute tol. basket_return is untouched. (0.8*rel_tol on the fixture's
    # ~1.6 peak equity -> ~1.3e-9 abs: above 1e-9, below the ~1.6e-9 isclose threshold.)
    perturbed = full.with_columns(
        (pl.col("equity") * (1.0 + 0.8 * EQUITY_REL_TOL)).alias("equity")
    )
    # Confirm the perturbation would have tripped a pure-absolute 1e-9 check.
    eq_old = full["equity"].to_numpy()
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


# --- ingestion-2: Bybit OI missing field is null, not a fabricated 0.0 ------
def test_bybit_open_interest_missing_field_is_null_not_zero() -> None:
    out = _normalize_open_interest("BTCUSDT", [{"timestamp": "1000"}])
    assert out[0]["open_interest"] is None
    # A missing openInterest must NOT coalesce into a fabricated 0.0 value.
    assert out[0]["open_interest_value"] is None


def test_bybit_open_interest_present_zero_is_real_zero() -> None:
    out = _normalize_open_interest(
        "BTCUSDT",
        [{"timestamp": "1000", "openInterest": "0", "openInterestValue": "0"}],
    )
    assert out[0]["open_interest"] == 0.0
    assert out[0]["open_interest_value"] == 0.0


def test_bybit_open_interest_value_falls_back_to_present_open_interest() -> None:
    # When openInterestValue is absent but openInterest is present, fall back.
    out = _normalize_open_interest("BTCUSDT", [{"timestamp": "1000", "openInterest": "42"}])
    assert out[0]["open_interest"] == 42.0
    assert out[0]["open_interest_value"] == 42.0


# --- ingestion-6: a literal "0" funding interval must not yield interval 0 --
def test_funding_interval_zero_string_falls_back_to_8h() -> None:
    # The `or 8` idiom failed here: int("0") == 0 is truthy as a STRING, so a 0h
    # interval would become 0 minutes and produce funding_rate_8h_equiv = inf.
    assert _funding_interval_min("0") == 8 * 60
    assert _funding_interval_min(0) == 8 * 60
    assert _funding_interval_min(None) == 8 * 60
    assert _funding_interval_min("") == 8 * 60
    assert _funding_interval_min(-1) == 8 * 60
    # Real cadences pass through unchanged.
    assert _funding_interval_min("1") == 60
    assert _funding_interval_min(4) == 240
    assert _funding_interval_min("8") == 480


def test_normalize_funding_zero_interval_does_not_emit_zero_minutes() -> None:
    rows = [{"fundingRateTimestamp": "1000", "fundingRate": "0.0001", "fundingIntervalHour": "0"}]
    out = _normalize_funding("BTCUSDT", rows)
    assert out[0]["funding_interval_min"] == 8 * 60  # not 0 -> no inf downstream
    assert out[0]["funding_interval_min"] > 0


# --- reports-charts-1: a wallet-read outage is surfaced, not masked ---------
def _status_payload(*, wallet_error: str, equity: float) -> dict:
    return {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": equity,
            "wallet_error": wallet_error,
            "entries_executed": 0,
            "entry_candidates": 0,
            "exits_executed": 0,
            "exit_candidates": 0,
            "position_report_error": "",
        },
        "bybit_position_summary": {},
        "ledger_position_summary": {},
    }


def test_wallet_error_tags_fallback_equity_and_is_surfaced() -> None:
    payload = _status_payload(wallet_error="wallet equity unavailable: timeout", equity=10_000.0)
    text = format_telegram_status_message(payload)
    # The fallback equity must NOT print as a clean read.
    assert "FALLBACK" in text
    assert "wallet_error=wallet equity unavailable: timeout" in text


def test_wallet_error_triggers_a_notification() -> None:
    payload = _status_payload(wallet_error="wallet equity unavailable: 403", equity=10_000.0)
    assert _telegram_notification_reason(payload) == "wallet_error"


def test_clean_wallet_read_is_not_tagged_or_notified() -> None:
    payload = _status_payload(wallet_error="", equity=12_345.0)
    text = format_telegram_status_message(payload)
    assert "FALLBACK" not in text
    assert "wallet_error" not in text
    assert _telegram_notification_reason(payload) == ""
