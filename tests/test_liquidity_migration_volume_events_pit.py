"""Tests for the PIT membership / full-PIT universe gate — the methodology-critical
no-look-ahead / no-survivorship boundary in liquidity_migration/volume_events_pit.py.

These pin the exact survivorship semantics: pre-listing and post-delisting empty
manifest claims are NOT required (false tripwires), but a genuine mid-history gap
within a symbol's traded lifespan IS required (real survivorship protection)."""
from __future__ import annotations

import polars as pl

from liquidity_migration.volume_events_pit import (
    _covered_kline_date_symbol_set,
    _full_pit_universe_pass,
    _pit_manifest_metadata,
    _required_pit_date_symbols,
    _symbol_kline_date_bounds,
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


def test_covered_set_requires_min_hourly_bars() -> None:
    # A day with < 20 hourly bars is treated as not-downloaded (data-presence gate).
    thin = _klines([("AAA", "2025-01-02")], bars_per_day=10)
    assert _covered_kline_date_symbol_set(thin) == set()
    full = _klines([("AAA", "2025-01-02")], bars_per_day=20)
    assert _covered_kline_date_symbol_set(full) == {("2025-01-02", "AAA")}
