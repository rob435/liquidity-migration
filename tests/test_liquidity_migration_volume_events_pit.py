"""Tests for the PIT membership / full-PIT universe gate in
liquidity_migration/volume_events_pit.py.

Survivorship semantics: pre-listing and post-delisting empty manifest claims are NOT
required (they would be false tripwires), but a genuine mid-history gap within a
symbol's traded lifespan IS.
"""
from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.volume_events_pit import (
    _covered_kline_date_symbol_set,
    _full_pit_universe_pass,
    _pit_manifest_metadata,
    _required_pit_date_symbols,
    _symbol_kline_date_bounds,
    filter_klines_to_pit_membership,
)


def _klines(rows: list[tuple[str, str]], *, bars_per_day: int = 1) -> pl.DataFrame:
    """Build a klines frame with `bars_per_day` hourly rows per (symbol, date)."""
    out: list[dict[str, object]] = []
    for symbol, date in rows:
        for hour in range(bars_per_day):
            out.append({"symbol": symbol, "date": date, "ts_ms": hour})
    return pl.DataFrame(out)


def _manifest(pairs: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [s for s, _ in pairs], "date": [d for _, d in pairs]}
    )


def test_pit_filter_removes_nonmembers_before_features_and_preserves_order() -> None:
    day = 86_400_000
    klines = pl.DataFrame(
        {
            "ts_ms": [day, day, 2 * day, 2 * day],
            "symbol": ["AAA", "BBB", "AAA", "BBB"],
            "date": ["1970-01-02", "1970-01-02", "1970-01-03", "1970-01-03"],
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )
    manifest = _manifest(
        [
            ("AAA", "1970-01-02"),
            ("BBB", "1970-01-03"),
            ("BBB", "1970-01-03"),  # duplicate provenance row must not duplicate bars
        ]
    )

    filtered, receipt = filter_klines_to_pit_membership(klines, manifest)

    assert filtered.to_dicts() == [klines.row(0, named=True), klines.row(3, named=True)]
    assert filtered.schema == klines.schema
    assert receipt == {
        "schema_version": 1,
        "pit_membership_applied_before_features": True,
        "date_source": "date+ts_ms_verified",
        "input_rows": 4,
        "output_rows": 2,
        "dropped_rows": 2,
        "input_date_symbol_pairs": 4,
        "output_date_symbol_pairs": 2,
        "dropped_date_symbol_pairs": 2,
        "manifest_rows": 3,
        "manifest_date_symbol_pairs": 2,
        "duplicate_manifest_rows": 1,
    }


def test_pit_filter_derives_utc_date_when_kline_date_is_absent() -> None:
    klines = pl.DataFrame(
        {"ts_ms": [86_400_000, 2 * 86_400_000], "symbol": ["AAA", "AAA"], "close": [1.0, 2.0]}
    )
    filtered, receipt = filter_klines_to_pit_membership(
        klines,
        _manifest([("AAA", "1970-01-03")]),
    )
    assert filtered["ts_ms"].to_list() == [2 * 86_400_000]
    assert receipt["date_source"] == "ts_ms"


def test_pit_filter_rejects_date_timestamp_disagreement() -> None:
    klines = pl.DataFrame(
        {"ts_ms": [86_400_000], "symbol": ["AAA"], "date": ["1970-01-03"]}
    )
    with pytest.raises(RuntimeError, match="date disagrees"):
        filter_klines_to_pit_membership(
            klines,
            _manifest([("AAA", "1970-01-03")]),
        )


def test_required_pit_excludes_prelisting_and_postdelisting_keeps_midgap() -> None:
    # AAA trades 01-02 and 01-04 (a real one-day gap on 01-03 within its lifespan).
    klines = _klines([("AAA", "2025-01-02"), ("AAA", "2025-01-04")])
    # Manifest also claims a pre-listing day (01-01) and a post-delisting day (01-05).
    manifest = _manifest(
        [
            ("AAA", "2025-01-01"),  # pre-listing: before first kline -> NOT required
            ("AAA", "2025-01-02"),
            ("AAA", "2025-01-03"),  # mid-gap within lifespan -> REQUIRED
            ("AAA", "2025-01-04"),
            ("AAA", "2025-01-05"),  # post-delisting empty phantom -> NOT required
        ]
    )
    assert _symbol_kline_date_bounds(klines) == {"AAA": ("2025-01-02", "2025-01-04")}
    required = _required_pit_date_symbols(klines, manifest)
    assert required == {
        ("2025-01-02", "AAA"),
        ("2025-01-03", "AAA"),  # the genuine mid-history gap is still required
        ("2025-01-04", "AAA"),
    }


def test_required_pit_skips_symbol_absent_from_klines() -> None:
    klines = _klines([("AAA", "2025-01-02")])
    manifest = _manifest([("AAA", "2025-01-02"), ("BBB", "2025-01-02")])
    # BBB has no kline bounds, so none of its manifest dates are required.
    assert _required_pit_date_symbols(klines, manifest) == {("2025-01-02", "AAA")}


def test_full_pit_pass_true_when_covered_false_when_midgap_missing() -> None:
    manifest = _manifest(
        [("AAA", "2025-01-02"), ("AAA", "2025-01-03"), ("AAA", "2025-01-04")]
    )
    # Covered: every required (date, symbol) has >= 20 hourly bars.
    covered = _klines(
        [("AAA", "2025-01-02"), ("AAA", "2025-01-03"), ("AAA", "2025-01-04")],
        bars_per_day=24,
    )
    assert _full_pit_universe_pass(covered, manifest) is True
    # Mid-gap day 01-03 missing from klines -> required but not covered -> fail.
    holey = _klines([("AAA", "2025-01-02"), ("AAA", "2025-01-04")], bars_per_day=24)
    assert _full_pit_universe_pass(holey, manifest) is False


def test_full_pit_fails_when_current_listing_tail_is_missing() -> None:
    manifest = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA"],
            "date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "url": ["archive-1", "archive-2", "bybit_v5_listing"],
            "source": [
                "bybit_public_trading_archive",
                "bybit_public_trading_archive",
                "bybit_v5_listing",
            ],
        }
    )
    # The independently sourced v5 row says AAA was still Trading through
    # 01-03. Inferring the requirement from this incomplete kline tail would
    # instead redefine its lifespan as ending on 01-02 and false-pass.
    truncated = _klines(
        [("AAA", "2025-01-01"), ("AAA", "2025-01-02")],
        bars_per_day=24,
    )

    assert ("2025-01-03", "AAA") in _required_pit_date_symbols(truncated, manifest)
    assert _full_pit_universe_pass(truncated, manifest) is False
    metadata = _pit_manifest_metadata(
        manifest,
        pl.DataFrame({"symbol": ["AAA"]}),
        truncated,
    )
    assert metadata["required_manifest_date_symbols"] == 3
    assert metadata["required_manifest_date_symbols_missing_from_klines"] == 1
    assert metadata["full_pit_universe_pass"] is False


def test_current_listing_provenance_does_not_require_prelisting_day() -> None:
    manifest = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": ["2025-01-01", "2025-01-02"],
            "url": ["bybit_v5_listing", "bybit_v5_listing"],
            "source": ["bybit_v5_listing", "bybit_v5_listing"],
        }
    )
    klines = _klines([("AAA", "2025-01-02")], bars_per_day=24)

    assert _required_pit_date_symbols(klines, manifest) == {
        ("2025-01-02", "AAA")
    }


def test_reused_ticker_is_bounded_per_v5_listing_incarnation() -> None:
    manifest = pl.DataFrame(
        {
            "symbol": ["AAA"] * 8,
            "date": [
                "2025-01-01",
                "2025-01-02",
                "2025-01-03",
                "2025-01-05",
                "2025-01-06",
                "2025-01-07",
                "2025-01-08",
                "2025-01-09",
            ],
            "url": [
                "archive-old-1",
                "archive-old-2",
                "archive-empty-after-delisting",
                "bybit_v5_listing",
                "bybit_v5_listing",
                "archive-new-1",
                "archive-new-2",
                "bybit_v5_listing",
            ],
            "source": [
                "bybit_public_trading_archive",
                "bybit_public_trading_archive",
                "bybit_public_trading_archive",
                "bybit_v5_listing",
                "bybit_v5_listing",
                "bybit_public_trading_archive",
                "bybit_public_trading_archive",
                "bybit_v5_listing",
            ],
            "v5_observed_launch_date": ["2025-01-05"] * 8,
        }
    )
    klines = _klines(
        [
            ("AAA", "2025-01-01"),
            ("AAA", "2025-01-02"),
            ("AAA", "2025-01-07"),
            ("AAA", "2025-01-08"),
        ],
        bars_per_day=24,
    )

    required = _required_pit_date_symbols(klines, manifest)

    assert required == {
        ("2025-01-01", "AAA"),
        ("2025-01-02", "AAA"),
        ("2025-01-07", "AAA"),
        ("2025-01-08", "AAA"),
        # Independently inferred active tail remains required.
        ("2025-01-09", "AAA"),
    }
    assert ("2025-01-03", "AAA") not in required
    assert ("2025-01-05", "AAA") not in required
    assert ("2025-01-06", "AAA") not in required


def test_reused_ticker_still_requires_gap_inside_new_incarnation() -> None:
    manifest = pl.DataFrame(
        {
            "symbol": ["AAA"] * 5,
            "date": [
                "2025-01-01",
                "2025-01-02",
                "2025-01-07",
                "2025-01-08",
                "2025-01-09",
            ],
            "v5_observed_launch_date": ["2025-01-05"] * 5,
        }
    )
    klines = _klines(
        [
            ("AAA", "2025-01-01"),
            ("AAA", "2025-01-02"),
            ("AAA", "2025-01-07"),
            ("AAA", "2025-01-09"),
        ],
        bars_per_day=24,
    )

    required = _required_pit_date_symbols(klines, manifest)

    assert ("2025-01-08", "AAA") in required
    assert _full_pit_universe_pass(klines, manifest) is False


def test_covered_set_requires_min_hourly_bars() -> None:
    # A day with < 20 hourly bars is treated as not-downloaded (data-presence gate).
    thin = _klines([("AAA", "2025-01-02")], bars_per_day=10)
    assert _covered_kline_date_symbol_set(thin) == set()
    full = _klines([("AAA", "2025-01-02")], bars_per_day=20)
    assert _covered_kline_date_symbol_set(full) == {("2025-01-02", "AAA")}
