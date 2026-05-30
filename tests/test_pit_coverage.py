"""Tests for the PIT coverage / staleness diagnostics (FIX C)."""

from __future__ import annotations

import datetime as dt

from liquidity_migration import pit_coverage as pc


def _mk(root, dataset, dates):
    for d in dates:
        (root / dataset / f"date={d}" / "symbol=FOOUSDT").mkdir(parents=True, exist_ok=True)


def test_latest_signal_trading_day_is_yesterday():
    assert pc.latest_signal_trading_day(dt.date(2026, 5, 30)) == dt.date(2026, 5, 29)


def test_max_partition_date_handles_symbol_first_layout(tmp_path):
    # Some datasets partition symbol=.../date=... instead of date=.../symbol=...;
    # the staleness guard must find the max date in either layout (and never crash).
    ds = tmp_path / pc.MANIFEST_DATASET
    (ds / "symbol=FOOUSDT" / "date=2026-05-28").mkdir(parents=True)
    (ds / "symbol=BARUSDT" / "date=2026-05-29").mkdir(parents=True)
    assert pc._max_partition_date(ds) == dt.date(2026, 5, 29)


def test_max_partition_date_missing_dataset_is_none(tmp_path):
    assert pc._max_partition_date(tmp_path / "does_not_exist") is None


def test_coverage_fresh_manifest_is_not_stale(tmp_path):
    _mk(tmp_path, pc.MANIFEST_DATASET, ["2026-05-28", "2026-05-29"])
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-29", "2026-05-30"])
    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 30))
    assert st.manifest_end == dt.date(2026, 5, 29)
    assert st.kline_end == dt.date(2026, 5, 30)
    assert st.latest_signal_trading_day == dt.date(2026, 5, 29)
    assert st.manifest_covers_latest_signal is True
    assert st.is_stale is False
    assert st.manifest_margin_days == 0
    assert st.manifest_lag_vs_klines_days == 1


def test_coverage_stale_manifest_flagged(tmp_path):
    # The original failure: klines through 05-30 but manifest stuck at 05-27.
    _mk(tmp_path, pc.MANIFEST_DATASET, ["2026-05-26", "2026-05-27"])
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-29", "2026-05-30"])
    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 30))
    assert st.is_stale is True
    assert st.manifest_margin_days == -2  # 05-27 is 2 days behind the 05-29 signal day
    msg = pc.format_coverage(st)
    assert "pit_membership_fail" in msg
    assert "archive-manifest" in msg


def test_coverage_missing_manifest(tmp_path):
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-30"])
    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 30))
    assert st.manifest_end is None
    assert st.is_stale is True
    assert "no archive manifest" in pc.format_coverage(st)
