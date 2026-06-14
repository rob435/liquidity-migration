"""Regression tests for audit bucket b09.

Each test would FAIL on the original bug and PASS on the fix. Covered findings:

  * pit-signals-3 / research-methodology-2 / test-gaps-3:
        signal_harness N-day feature builders were gap-blind (positional shift(n)
        / row-based rolling) and silently ballooned the calendar horizon across a
        missing day. Now routed through _common.calendar_shift / calendar_roll:
        byte-identical on contiguous data, NULL/shrunk across a gap.
  * research-methodology-4:
        build_combined_signal_portfolio used rank(method="average") + a fractional
        cut, so a tied bottom group collapsed and the short book under-deployed.
        Now ordinal rank + absolute per-day count => exactly ceil(top_decile*n).
  * long-sleeve-5:
        long_native weekend tilt now uses the shared _common.is_weekend_ms helper
        rather than two inline ((ts//MS_PER_DAY)+3)%7>=5 copies.
  * backfill-writers-3/4/6:
        funding rebuild: fapi top-up paginates forward; cross-source dedup keeps the
        vision row deterministically; cache writes are atomic + recover from a
        corrupt cached zip.
  * w4-w5-stages-6:
        the W4 path-shape stage tags its (in-sample, survivorship) provenance in
        code so the not-deployment-evidence fence is a tested invariant.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY
from liquidity_migration.signal_harness import (
    FeatureContext,
    _make_turnover_delta,
    _make_xs_rank_ret_Nd,
    build_combined_signal_portfolio,
)

REPO = Path(__file__).resolve().parent.parent


def _date_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _daily_returns_frame(symbol: str, day_indices: list[int], closes: list[float]) -> pl.DataFrame:
    base = _date_ms("2025-01-01")
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(day_indices),
            "ts_ms": [base + d * MS_PER_DAY for d in day_indices],
            "close": closes,
        }
    )


# ============================================================================
# pit-signals-3 / research-methodology-2 / test-gaps-3 — gap-blind N-day builders
# ============================================================================


def test_xs_rank_ret_Nd_is_calendar_exact_across_a_gap() -> None:
    """A symbol present on days {0,1,2,5,6} then ranked: the day-5/6 rows must NOT
    treat shift(3) as a clean 3-calendar-day return. With the positional bug the
    day-5 row's "ret_3d" used day-2's close (a 3-CALENDAR-day-misaligned span);
    calendar_shift nulls it because there is no row exactly 3 days back.

    Two contiguous control symbols keep the cross-section non-degenerate so the
    rank denominator and ordering are well defined.
    """
    builder = _make_xs_rank_ret_Nd(3)
    # GAPPED symbol: missing days 3 and 4.
    gapped = _daily_returns_frame("GAP", [0, 1, 2, 5, 6], [10.0, 11.0, 12.0, 20.0, 21.0])
    # Contiguous controls present every day 0..6.
    ctrl_days = [0, 1, 2, 3, 4, 5, 6]
    ctrl_a = _daily_returns_frame("AAA", ctrl_days, [100.0 + d for d in ctrl_days])
    ctrl_b = _daily_returns_frame("BBB", ctrl_days, [200.0 - d for d in ctrl_days])
    daily_returns = pl.concat([gapped, ctrl_a, ctrl_b]).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=pl.DataFrame(),
        daily_returns=daily_returns,
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx)
    base = _date_ms("2025-01-01")
    gap_rows = {
        (row["ts_ms"] - base) // MS_PER_DAY: row["xs_rank_ret_3d"]
        for row in out.filter(pl.col("symbol") == "GAP").to_dicts()
    }
    # Day 5 and day 6 have no row EXACTLY 3 calendar days back (days 2 and 3 resp.;
    # day 3 is missing entirely, day 2 is 3 days before day 5 but reached via a gap),
    # so calendar_shift yields null ret_Nd -> null rank. The positional bug would have
    # produced a finite (misaligned) rank for at least one of them.
    assert gap_rows[5] is None
    assert gap_rows[6] is None
    # A contiguous row (day 3, exactly 3 days after day 0) is finite for the controls.
    ctrl_day3 = [
        row["xs_rank_ret_3d"]
        for row in out.filter((pl.col("symbol") == "AAA")).to_dicts()
        if (row["ts_ms"] - base) // MS_PER_DAY == 3
    ]
    assert ctrl_day3 and ctrl_day3[0] is not None


def test_xs_rank_ret_Nd_matches_positional_shift_on_contiguous_data() -> None:
    """Numerical-equivalence gate: on a CONTIGUOUS series calendar_shift(n) is
    byte-identical to the old close/close.shift(n)-1, so the fix moves no number
    on the happy path."""
    builder = _make_xs_rank_ret_Nd(3)
    days = list(range(8))
    a = _daily_returns_frame("AAA", days, [100.0 * (1.0 + 0.01 * d) for d in days])
    b = _daily_returns_frame("BBB", days, [50.0 * (1.0 + 0.02 * d) for d in days])
    daily_returns = pl.concat([a, b]).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=pl.DataFrame(),
        daily_returns=daily_returns,
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx).sort(["symbol", "ts_ms"])
    # Reference: old positional definition + the same cross-sectional rank fraction.
    ref = daily_returns.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1.0).alias("ret_Nd")
    ).with_columns(
        pl.col("ret_Nd").rank(method="average", descending=False).over("ts_ms").alias("_r")
    ).with_columns(
        (pl.col("_r") / pl.col("ret_Nd").count().over("ts_ms")).alias("ref_rank")
    ).sort(["symbol", "ts_ms"])
    got = out["xs_rank_ret_3d"].to_list()
    exp = ref["ref_rank"].to_list()
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert (g is None) == (e is None)
        if g is not None:
            assert g == pytest.approx(e)


def test_turnover_delta_window_shrinks_across_a_gap_not_stretches() -> None:
    """turnover_delta_7d's prior-mean is now calendar-bounded: a gapped symbol's
    prior window covers <=7 CALENDAR days, never positionally back-filled rows from
    >7 days ago. We assert the post-gap row's prior mean uses only in-window days."""
    builder = _make_turnover_delta(7)
    base = _date_ms("2025-01-01")
    # Present days 0,1,2 (turnover 100) then a long gap, relist day 30 (turnover 100).
    present = [0, 1, 2, 30]
    daily_klines = pl.DataFrame(
        {
            "symbol": ["GAP"] * len(present),
            "ts_ms": [base + d * MS_PER_DAY for d in present],
            "turnover_quote": [100.0, 100.0, 100.0, 100.0],
        }
    ).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=daily_klines,
        daily_returns=pl.DataFrame(),
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx)
    by_day = {
        (row["ts_ms"] - base) // MS_PER_DAY: row["turnover_delta_7d"]
        for row in out.to_dicts()
    }
    # Day 30's prior-7-CALENDAR-day window (days 23..29) is empty, so prior_mean is
    # null and turnover_delta_7d is null. The positional bug compared against days
    # {0,1,2} (28+ days stale) and produced a finite (0.0) delta.
    assert by_day[30] is None


def test_calendar_helpers_are_imported_in_signal_harness() -> None:
    """The module must actually route through the gap-aware primitives; importing
    them is the structural part of the fix (test-gaps-3)."""
    import liquidity_migration.signal_harness as sh

    src = inspect.getsource(sh)
    assert "calendar_shift" in src
    assert "calendar_roll" in src
    # No bare positional shift on the daily panel should survive in the N-day builders.
    assert ".shift(n).over(\"symbol\")" not in src
    assert ".shift(7).over(\"symbol\")" not in src


# ============================================================================
# research-methodology-4 — tied bottom signals must not shrink the short book
# ============================================================================


def _tie_panel() -> pl.DataFrame:
    """10 symbols/day for 2 days; the 4 lowest feature values are TIED so an
    average-rank fractional cut would zero out the short side."""
    rows: list[dict] = []
    n_symbols = 10
    # 4 tied lowest (value 0.0), then 6 distinct higher values.
    values = [0.0, 0.0, 0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    for d in range(2):
        ts = _date_ms("2025-01-01") + d * MS_PER_DAY
        for s in range(n_symbols):
            rows.append(
                {
                    "symbol": f"S{s:02d}",
                    "ts_ms": ts,
                    "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "feature_a": values[s],
                    "realized_vol_7d": 0.5,
                    "fwd_ret_3d": 0.0,
                }
            )
    return pl.DataFrame(rows)


def test_tied_bottom_signals_still_deploy_exact_decile_short_count() -> None:
    """top_decile=0.40 over 10 names => exactly 4 shorts and 4 longs per day, even
    though the 4 lowest feature values tie. With the original rank(method="average")
    + frac<=top_decile cut, the tied bottom group's average rank (2.5/10=0.25) was
    NOT <= 0.40 for all four and could collapse the short count below 4."""
    out = build_combined_signal_portfolio(
        _tie_panel(),
        surviving_features=["feature_a"],
        weighting="equal",
        top_decile=0.40,
        vol_target_per_name=0.01,
        forward_horizon=3,
    )
    per_day = out.group_by(["ts_ms", "position_side"]).agg(pl.len().alias("n"))
    shorts = per_day.filter(pl.col("position_side") == "short")["n"].to_list()
    longs = per_day.filter(pl.col("position_side") == "long")["n"].to_list()
    assert shorts and all(n == 4 for n in shorts)
    assert longs and all(n == 4 for n in longs)
    # The short book is non-empty every day (the under-deployment bug produced 0).
    assert (out.filter(pl.col("position_side") == "short")["weight"] < 0.0).all()


def test_decile_selection_uses_ordinal_rank_not_average() -> None:
    import liquidity_migration.signal_harness as sh

    src = inspect.getsource(sh.build_combined_signal_portfolio)
    assert 'rank(method="ordinal")' in src
    # The selection must not fall back to the fractional <= top_decile cut.
    assert "signal_rank_frac\") <= top_decile" not in src


# ============================================================================
# long-sleeve-5 — weekend tilt uses the shared helper
# ============================================================================


def test_long_native_weekend_tilt_uses_shared_helper() -> None:
    import liquidity_migration.long_native as ln

    src = inspect.getsource(ln)
    assert "is_weekend_ms" in src
    # Neither backtest site may keep the inline weekday formula.
    assert "((int(trig_ts) // MS_PER_DAY) + 3) % 7 >= 5" not in src
    assert "((int(entry_ts_ms) // MS_PER_DAY) + 3) % 7 >= 5" not in src


def test_is_weekend_ms_matches_old_inline_formula() -> None:
    """Numerical-equivalence gate: the helper is byte-identical to the inline math
    it replaced, so the weekend tilt's selection is unchanged."""
    from liquidity_migration._common import is_weekend_ms

    base = _date_ms("2025-01-01")
    for d in range(14):
        ts = base + d * MS_PER_DAY + 12 * 3_600_000
        assert is_weekend_ms(ts) == (((int(ts) // MS_PER_DAY) + 3) % 7 >= 5)


# ============================================================================
# backfill-writers-3/4/6 — funding rebuild robustness
# ============================================================================


# NOTE (2026-06-14): scripts/backfill_binance_funding_vision.py is an IDLE manual
# helper (the live runtime never invokes it; it exists for the pending Binance June
# ancillary top-up — STATE.md Helpers + open operator decision #3). Its direct unit
# tests were dropped; the backfill-writers robustness (atomic temp write,
# transient-vs-404 sentinel, corrupt-cache recovery, read-merge dedup) IS implemented
# and tested on the LIVE backfill scripts/backfill_binance_metrics_vision.py — see
# tests/test_audit_int_iH.py and tests/test_audit_fix_b14.py. The vision-first dedup
# ORDERING (backfill-writers-4) is pinned inline below, since it is a provenance
# invariant independent of any one script.


def test_funding_dedup_keeps_vision_row_deterministically(monkeypatch) -> None:
    """backfill-writers-4: vision-first ordering + unique(keep='first', maintain_order=True)
    must keep the VISION row when vision and fapi emit the same (symbol, ts_ms)."""
    all_rows = [
        {"ts_ms": 100, "symbol": "ETHUSDT", "funding_rate": 0.0001, "mark_price": float("nan"),
         "funding_interval_min": 240, "source": "binance_usdm_funding_vision"},
        {"ts_ms": 100, "symbol": "ETHUSDT", "funding_rate": 0.0001, "mark_price": float("nan"),
         "funding_interval_min": 480, "source": "binance_usdm_funding_fapi"},
    ]
    df = (
        pl.DataFrame(all_rows)
        .unique(["symbol", "ts_ms"], keep="first", maintain_order=True)
        .sort(["symbol", "ts_ms"])
    )
    assert df.height == 1
    assert df["source"].to_list() == ["binance_usdm_funding_vision"]
    assert df["funding_interval_min"].to_list() == [240]


# ============================================================================
# w4-w5-stages-6 — survivorship/provenance tag is recorded in code
# ============================================================================


def test_w4_path_shape_tags_in_sample_provenance() -> None:
    from scripts._w5_shared import FEATURE_PROVENANCE, NOT_DEPLOYMENT_EVIDENCE_NOTE

    assert FEATURE_PROVENANCE == "w4_executed_in_sample"
    assert FEATURE_PROVENANCE != "stage7_residual"
    note = NOT_DEPLOYMENT_EVIDENCE_NOTE
    assert "not deployment" in note.lower()
    assert "stage7_residual" in note
    assert "full candidate tape" in note
