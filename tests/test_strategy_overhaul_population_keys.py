"""Tests for outcome-blind source and signal population manifests."""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.strategy_overhaul_population_keys import (
    CONTINUOUS_KEY_SCHEMA,
    HOURLY_KEY_SCHEMA,
    LONG_KEY_SCHEMA,
    MANIFEST_KEY_SCHEMA,
    PopulationKeyError,
    PopulationKeyWindow,
    build_continuous_population_keys,
    build_long_population_keys,
    long_expected_population,
)


DAY0 = 20_000 * MS_PER_DAY


def _hourly(*, second_day_bars: int = 24) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol in ("AAAUSDT", "BBBUSDT"):
        for day, count in ((0, 24), (1, second_day_bars), (2, 24)):
            for hour in range(count):
                rows.append(
                    {
                        "symbol": symbol,
                        "ts_ms": DAY0 + day * MS_PER_DAY + hour * MS_PER_HOUR,
                    }
                )
    return pl.DataFrame(rows, schema=dict(HOURLY_KEY_SCHEMA))


def _manifest(*, include_bbb: bool = True) -> pl.DataFrame:
    symbols = ("AAAUSDT", "BBBUSDT") if include_bbb else ("AAAUSDT",)
    rows = [
        {
            "symbol": symbol,
            "manifest_date": dt.datetime.fromtimestamp(
                (DAY0 + day * MS_PER_DAY) / 1000,
                tz=dt.timezone.utc,
            ).date(),
        }
        for symbol in symbols
        for day in range(3)
    ]
    return pl.DataFrame(rows, schema=dict(MANIFEST_KEY_SCHEMA))


def _window(*, identity_history_start_ts_ms: int = DAY0) -> PopulationKeyWindow:
    return PopulationKeyWindow(
        identity_history_start_ts_ms=identity_history_start_ts_ms,
        causal_read_start_ts_ms=DAY0,
        signal_start_ts_ms=DAY0 + MS_PER_DAY,
        signal_end_ts_ms_exclusive=DAY0 + 3 * MS_PER_DAY,
    )


def test_continuous_keeps_full_source_then_selects_signal_window() -> None:
    result = build_continuous_population_keys(
        _hourly(),
        _manifest(),
        venue="bybit",
        window=_window(),
    )

    assert result.source_keys.schema == dict(CONTINUOUS_KEY_SCHEMA)
    assert result.signal_keys.schema == dict(CONTINUOUS_KEY_SCHEMA)
    assert result.source_keys.height == 2 * 3 * 24
    assert result.signal_keys.height == 2 * 2 * 24
    assert result.signal_keys["signal_ts_ms"].min() == DAY0 + MS_PER_DAY
    assert result.receipt["outcome_values_read"] is False
    assert result.receipt["root_completeness_proven"] is False


def test_manifest_membership_is_an_explicit_population_intersection() -> None:
    result = build_continuous_population_keys(
        _hourly(),
        _manifest(include_bbb=False),
        venue="binance",
        window=_window(),
    )

    assert result.source_keys["symbol"].unique().to_list() == ["AAAUSDT"]
    assert result.receipt["kline_without_membership_row_count"] == 72


def test_long_builds_daily_eligibility_and_age_before_signal_filter() -> None:
    result = build_long_population_keys(
        _hourly(),
        _manifest(),
        venue="bybit",
        window=_window(),
    )

    assert result.source_keys.schema == dict(LONG_KEY_SCHEMA)
    assert result.source_keys.height == 4
    assert result.signal_keys.height == 4
    assert result.source_keys.filter(pl.col("symbol") == "AAAUSDT")["symbol_age_days"].to_list() == [1, 2]
    assert result.source_keys["hourly_bar_count"].unique().to_list() == [24]
    assert result.receipt["age_left_censored_symbol_count"] == 2
    assert result.receipt["age_left_censored_symbol_sample"] == ["AAAUSDT", "BBBUSDT"]
    assert long_expected_population(result.signal_keys).columns == [
        "symbol",
        "signal_ts_ms",
        "symbol_age_days",
    ]


def test_long_rejects_thin_daily_rows_without_renumbering_calendar_age() -> None:
    result = build_long_population_keys(
        _hourly(second_day_bars=19),
        _manifest(),
        venue="bybit",
        window=_window(),
    )

    aaa = result.source_keys.filter(pl.col("symbol") == "AAAUSDT")
    assert (
        aaa["signal_ts_ms"].to_list()
        == [
            DAY0 + MS_PER_DAY,
            DAY0 + 3 * MS_PER_DAY,
        ][:1]
    )
    # The third bar day closes exactly at the exclusive boundary and is not an
    # S02 source row; the thin middle day cannot be fabricated as eligible.
    assert aaa["symbol_age_days"].to_list() == [1]


def test_long_age_is_derived_before_the_causal_read_floor() -> None:
    prehistory_rows = [
        {
            "symbol": symbol,
            "ts_ms": DAY0 + day * MS_PER_DAY + hour * MS_PER_HOUR,
        }
        for symbol in ("AAAUSDT", "BBBUSDT")
        for day in (-2, -1)
        for hour in range(24)
    ]
    prehistory_manifest = [
        {
            "symbol": symbol,
            "manifest_date": dt.datetime.fromtimestamp(
                (DAY0 + day * MS_PER_DAY) / 1000,
                tz=dt.timezone.utc,
            ).date(),
        }
        for symbol in ("AAAUSDT", "BBBUSDT")
        for day in (-2, -1)
    ]
    hourly = pl.concat(
        [
            pl.DataFrame(prehistory_rows, schema=dict(HOURLY_KEY_SCHEMA)),
            _hourly(),
        ]
    )
    manifest = pl.concat(
        [
            pl.DataFrame(prehistory_manifest, schema=dict(MANIFEST_KEY_SCHEMA)),
            _manifest(),
        ]
    )

    result = build_long_population_keys(
        hourly,
        manifest,
        venue="bybit",
        window=_window(identity_history_start_ts_ms=DAY0 - 2 * MS_PER_DAY),
    )

    aaa = result.source_keys.filter(pl.col("symbol") == "AAAUSDT")
    assert aaa["symbol_age_days"].to_list() == [3, 4]
    assert result.receipt["age_history_start_ts_ms"] == DAY0 - 2 * MS_PER_DAY
    assert result.receipt["age_history_hourly_row_count"] == hourly.height
    assert result.receipt["age_anchor_semantics"] == (
        "first_eligible_daily_kline_row_in_supplied_full_root_history_before_membership_gate"
    )


def test_long_age_counts_eligible_kline_days_before_membership_gate() -> None:
    prehistory_rows = [
        {"symbol": "AAAUSDT", "ts_ms": DAY0 - MS_PER_DAY + hour * MS_PER_HOUR}
        for hour in range(24)
    ]
    hourly = pl.concat(
        [
            pl.DataFrame(prehistory_rows, schema=dict(HOURLY_KEY_SCHEMA)),
            _hourly().filter(pl.col("symbol") == "AAAUSDT"),
        ]
    )
    # Deliberately omit the leading prehistory day from PIT membership. The
    # production feature builder still counts that eligible kline day for age,
    # then the S02 population gate applies membership to retained days.
    manifest = _manifest(include_bbb=False)

    result = build_long_population_keys(
        hourly,
        manifest,
        venue="bybit",
        window=_window(identity_history_start_ts_ms=DAY0 - MS_PER_DAY),
    )

    assert result.source_keys["symbol_age_days"].to_list() == [2, 3]


def test_key_receipts_are_stable_and_change_with_identity() -> None:
    first = build_continuous_population_keys(
        _hourly(),
        _manifest(),
        venue="bybit",
        window=_window(),
    )
    second = build_continuous_population_keys(
        _hourly(),
        _manifest(),
        venue="bybit",
        window=_window(),
    )
    reduced = build_continuous_population_keys(
        _hourly().filter(pl.col("symbol") == "AAAUSDT"),
        _manifest(),
        venue="bybit",
        window=_window(),
    )

    assert_frame_equal(first.source_keys, second.source_keys)
    assert first.receipt == second.receipt
    assert first.receipt["artifact_sha256"] != reduced.receipt["artifact_sha256"]


def test_long_receipt_binds_thin_history_and_unused_manifest_identity_inputs() -> None:
    baseline = build_long_population_keys(
        _hourly(),
        _manifest(),
        venue="bybit",
        window=_window(identity_history_start_ts_ms=DAY0 - MS_PER_DAY),
    )
    thin_history = pl.concat(
        [
            pl.DataFrame(
                [{"symbol": "AAAUSDT", "ts_ms": DAY0 - MS_PER_DAY}],
                schema=dict(HOURLY_KEY_SCHEMA),
            ),
            _hourly(),
        ]
    )
    with_thin_history = build_long_population_keys(
        thin_history,
        _manifest(),
        venue="bybit",
        window=_window(identity_history_start_ts_ms=DAY0 - MS_PER_DAY),
    )
    unused_manifest = pl.concat(
        [
            _manifest(),
            pl.DataFrame(
                [{"symbol": "ZZZUSDT", "manifest_date": dt.datetime.fromtimestamp(DAY0 / 1000, tz=dt.timezone.utc).date()}],
                schema=dict(MANIFEST_KEY_SCHEMA),
            ),
        ]
    )
    with_unused_manifest = build_long_population_keys(
        _hourly(),
        unused_manifest,
        venue="bybit",
        window=_window(identity_history_start_ts_ms=DAY0 - MS_PER_DAY),
    )

    assert_frame_equal(baseline.source_keys, with_thin_history.source_keys)
    assert_frame_equal(baseline.source_keys, with_unused_manifest.source_keys)
    assert baseline.receipt["hourly_identity_input_sha256"] != (
        with_thin_history.receipt["hourly_identity_input_sha256"]
    )
    assert baseline.receipt["manifest_identity_input_sha256"] != (
        with_unused_manifest.receipt["manifest_identity_input_sha256"]
    )
    assert len({
        baseline.receipt["artifact_sha256"],
        with_thin_history.receipt["artifact_sha256"],
        with_unused_manifest.receipt["artifact_sha256"],
    }) == 3


@pytest.mark.parametrize("kind", ["duplicate_hour", "off_grid", "duplicate_manifest"])
def test_malformed_or_same_source_keys_fail_closed(kind: str) -> None:
    hourly = _hourly()
    manifest = _manifest()
    if kind == "duplicate_hour":
        hourly = pl.concat([hourly, hourly.head(1)])
    elif kind == "off_grid":
        hourly = hourly.with_columns(
            pl.when(pl.int_range(pl.len()) == 0).then(pl.col("ts_ms") + 1).otherwise(pl.col("ts_ms")).alias("ts_ms")
        )
    else:
        manifest = pl.concat([manifest, manifest.head(1)])

    with pytest.raises(PopulationKeyError):
        build_continuous_population_keys(
            hourly,
            manifest,
            venue="bybit",
            window=_window(),
        )
