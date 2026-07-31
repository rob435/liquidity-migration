"""Tests for the PIT coverage / staleness diagnostics (FIX C)."""

from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration.data import pit_coverage as pc
from liquidity_migration.core.symbol_codec import encode_symbol_partition


def _mk(root, dataset, dates, *, symbol="FOOUSDT"):
    for d in dates:
        part = root / dataset / f"date={d}" / f"symbol={symbol}"
        part.mkdir(parents=True, exist_ok=True)
        # A real (downloaded) partition carries a parquet; coverage only counts
        # partitions that actually hold data, not bare mkdir'd dirs.
        (part / "part.parquet").touch()


def test_latest_signal_trading_day_is_yesterday():
    assert pc.latest_signal_trading_day(dt.date(2026, 5, 30)) == dt.date(2026, 5, 29)


def test_max_partition_date_handles_symbol_first_layout(tmp_path):
    # Some datasets partition symbol=.../date=... instead of date=.../symbol=...;
    # the staleness guard must find the max date in either layout (and never crash).
    ds = tmp_path / pc.MANIFEST_DATASET
    (ds / "symbol=FOOUSDT" / "date=2026-05-28").mkdir(parents=True)
    (ds / "symbol=FOOUSDT" / "date=2026-05-28" / "part.parquet").touch()
    (ds / "symbol=BARUSDT" / "date=2026-05-29").mkdir(parents=True)
    (ds / "symbol=BARUSDT" / "date=2026-05-29" / "part.parquet").touch()
    assert pc._max_partition_date(ds) == dt.date(2026, 5, 29)


def test_max_partition_date_missing_dataset_is_none(tmp_path):
    assert pc._max_partition_date(tmp_path / "does_not_exist") is None


def test_max_partition_date_ignores_empty_partition_without_parquet(tmp_path):
    # write_dataset mkdir's the partition before writing the part; a crashed or
    # refused build leaves an empty date= dir that must NOT count as coverage
    # (else a stale manifest reads as FRESH).
    ds = tmp_path / pc.MANIFEST_DATASET
    (ds / "date=2026-05-29" / "symbol=FOOUSDT").mkdir(parents=True)  # empty, no parquet
    assert pc._max_partition_date(ds) is None
    # A partition that actually holds a parquet IS counted.
    real = ds / "date=2026-05-28" / "symbol=FOOUSDT"
    real.mkdir(parents=True)
    (real / "part.parquet").touch()
    assert pc._max_partition_date(ds) == dt.date(2026, 5, 28)


def test_symbols_on_date_handles_date_first_and_symbol_first_layouts(tmp_path):
    date_first = tmp_path / "date_first"
    _mk(tmp_path, "date_first", ["2026-05-30"], symbol="FOOUSDT")
    assert pc._symbols_on_date(date_first, dt.date(2026, 5, 30)) == {"FOOUSDT"}

    symbol_first = tmp_path / "symbol_first"
    part = symbol_first / "symbol=BARUSDT" / "date=2026-05-30"
    part.mkdir(parents=True)
    (part / "part.parquet").touch()
    assert pc._symbols_on_date(symbol_first, dt.date(2026, 5, 30)) == {"BARUSDT"}


def test_symbols_on_date_decodes_canonical_unicode_partition(tmp_path):
    symbol = "\u5e01\u5b89\u4eba\u751fUSDT"
    encoded = encode_symbol_partition(symbol)
    part = tmp_path / "dataset" / "date=2026-05-30" / f"symbol={encoded}"
    part.mkdir(parents=True)
    (part / "part.parquet").touch()

    assert pc._symbols_on_date(tmp_path / "dataset", dt.date(2026, 5, 30)) == {symbol}


def test_symbols_on_date_reads_date_level_manifest_parquet(tmp_path):
    ds = tmp_path / pc.MANIFEST_DATASET
    part = ds / "date=2026-05-30"
    part.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["FOOUSDT", "BARUSDT"],
            "date": ["2026-05-30", "2026-05-30"],
            "url": ["listing", "listing"],
        }
    ).write_parquet(part / "part.parquet")

    assert pc._symbols_on_date(ds, dt.date(2026, 5, 30)) == {"FOOUSDT", "BARUSDT"}


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
    assert st.per_symbol_manifest_lags == ()


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


def test_coverage_flags_per_symbol_manifest_lag_when_global_dates_match(tmp_path):
    # Global max dates match because FOO is current in both datasets, but BAR's
    # manifest is two days behind its kline coverage.
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-30"], symbol="FOOUSDT")
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-30"], symbol="BARUSDT")
    _mk(tmp_path, pc.MANIFEST_DATASET, ["2026-05-30"], symbol="FOOUSDT")
    _mk(tmp_path, pc.MANIFEST_DATASET, ["2026-05-28"], symbol="BARUSDT")

    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 31))

    assert st.manifest_end == dt.date(2026, 5, 30)
    assert st.kline_end == dt.date(2026, 5, 30)
    assert st.manifest_lag_vs_klines_days == 0
    assert len(st.per_symbol_manifest_lags) == 1
    lag = st.per_symbol_manifest_lags[0]
    assert lag.symbol == "BARUSDT"
    assert lag.manifest_end == dt.date(2026, 5, 28)
    assert lag.kline_day == dt.date(2026, 5, 30)
    assert lag.lag_days == 2
    msg = pc.format_coverage(st)
    assert "latest-day klines_1h coverage without matching archive-manifest coverage" in msg
    assert "BARUSDT" in msg


def test_coverage_flags_per_symbol_manifest_missing_within_tail_lookback(tmp_path):
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-30"], symbol="BARUSDT")
    _mk(tmp_path, pc.MANIFEST_DATASET, ["2026-05-30"], symbol="FOOUSDT")

    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 31))

    assert len(st.per_symbol_manifest_lags) == 1
    lag = st.per_symbol_manifest_lags[0]
    assert lag.symbol == "BARUSDT"
    assert lag.manifest_end is None
    assert lag.lag_days is None


def test_coverage_missing_manifest(tmp_path):
    _mk(tmp_path, pc.KLINE_DATASET, ["2026-05-30"])
    st = pc.coverage_status(tmp_path, today=dt.date(2026, 5, 30))
    assert st.manifest_end is None
    assert st.is_stale is True
    assert "no archive manifest" in pc.format_coverage(st)
