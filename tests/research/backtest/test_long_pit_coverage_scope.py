from __future__ import annotations

import datetime as dt
from dataclasses import replace

import polars as pl

from liquidity_migration.research.backtest.long_native import (
    _assess_long_pit_coverage,
)
from liquidity_migration.rules.long_native import long_v12_profile


def _dates(start: str, end: str) -> list[dt.date]:
    current = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    output: list[dt.date] = []
    while current <= last:
        output.append(current)
        current += dt.timedelta(days=1)
    return output


def _hourly_klines(days: list[dt.date]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for day in days:
        day_start = dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc)
        for hour in range(24):
            rows.append(
                {
                    "date": day.isoformat(),
                    "symbol": "BTCUSDT",
                    "ts_ms": int((day_start + dt.timedelta(hours=hour)).timestamp() * 1000),
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                }
            )
    return pl.DataFrame(rows)


def test_long_pit_scope_matches_daily_feature_clock_at_both_boundaries() -> None:
    manifest_days = _dates("2024-12-24", "2025-10-23")
    clean_days = _dates("2024-12-25", "2025-10-22")
    manifest = pl.DataFrame(
        {
            "date": [day.isoformat() for day in manifest_days],
            "symbol": ["BTCUSDT"] * len(manifest_days),
        }
    )
    klines = _hourly_klines(clean_days)
    clean_config = replace(
        long_v12_profile(),
        start_date="2025-03-25",
        end_date="2025-10-24",
    )

    clean = _assess_long_pit_coverage(klines, manifest, config=clean_config)

    assert clean.scope.feature_lookback_days == 90
    assert clean.scope.input_start == "2024-12-25"
    assert clean.scope.input_end_exclusive == "2025-10-23"
    assert clean.run.passed is True
    assert clean.full_root.passed is False
    assert clean.full_root.missing_required_date_symbols == {
        ("2024-12-24", "BTCUSDT"),
        ("2025-10-23", "BTCUSDT"),
    }

    starts_one_day_too_early = _assess_long_pit_coverage(
        klines,
        manifest,
        config=replace(clean_config, start_date="2025-03-24"),
    )
    assert starts_one_day_too_early.run.missing_required_date_symbols == {("2024-12-24", "BTCUSDT")}

    ends_one_day_too_late = _assess_long_pit_coverage(
        klines,
        manifest,
        config=replace(clean_config, end_date="2025-10-25"),
    )
    assert ends_one_day_too_late.run.missing_required_date_symbols == {("2025-10-23", "BTCUSDT")}
