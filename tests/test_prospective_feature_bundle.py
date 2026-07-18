from __future__ import annotations

import polars as pl
from polars.testing import assert_frame_equal

from liquidity_migration.continuous_events import (
    continuous_source_decile_panel,
    cross_sectional_decile,
    per_symbol_timeseries_features,
)
from scripts.build_prospective_feature_bundle import (
    CONTINUOUS_TAIL_ROWS,
    FEATURE_SET,
    RMOM_QUANTILE,
    _continuous_chunk_features,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def test_continuous_chunk_carry_matches_monolithic_feature_owner() -> None:
    symbols = [f"S{index:02d}" for index in range(6)]
    hours = 1_250
    split = 1_100
    rows: list[dict[str, object]] = []
    for symbol_index, symbol in enumerate(symbols):
        price = 100.0 + symbol_index
        for hour in range(hours):
            move = 1.0 + (((hour * 13 + symbol_index * 7) % 17) - 8) * 0.0004
            price *= move
            rows.append(
                {
                    "ts_ms": hour * HOUR_MS,
                    "symbol": symbol,
                    "close": price,
                    "turnover_quote": 1_000_000.0 + symbol_index * 10_000.0 + hour,
                }
            )
    raw = pl.DataFrame(rows).sort(["symbol", "ts_ms"])
    rmom = pl.DataFrame(
        [
            {
                "symbol": symbol,
                "day_ts": day * DAY_MS,
                "residual_momentum": symbol_index * 0.001 + day * 0.00001,
            }
            for day in range((hours * HOUR_MS + DAY_MS - 1) // DAY_MS)
            for symbol_index, symbol in enumerate(symbols)
        ]
    )
    empty_carry = pl.DataFrame(
        schema={
            "ts_ms": pl.Int64,
            "symbol": pl.String,
            "close": pl.Float64,
            "turnover_quote": pl.Float64,
        }
    )
    first_raw = raw.filter(pl.col("ts_ms") < split * HOUR_MS)
    second_raw = raw.filter(pl.col("ts_ms") >= split * HOUR_MS)

    source_1, active_1, carry = _continuous_chunk_features(
        first_raw,
        empty_carry,
        rmom,
        chunk_start_ms=0,
        chunk_end_ms=split * HOUR_MS,
        output_start_ms=0,
    )
    source_2, active_2, final_carry = _continuous_chunk_features(
        second_raw,
        carry,
        rmom,
        chunk_start_ms=split * HOUR_MS,
        chunk_end_ms=hours * HOUR_MS,
        output_start_ms=0,
    )

    featured = per_symbol_timeseries_features(raw)
    expected_source = continuous_source_decile_panel(
        featured,
        rmom,
        feature_set=FEATURE_SET,
    )
    expected_active = cross_sectional_decile(
        featured,
        rmom,
        rmom_quantile=RMOM_QUANTILE,
        feature_set=FEATURE_SET,
    )
    actual_source = pl.concat([source_1, source_2], how="vertical").sort(["ts_ms", "symbol"])
    actual_active = pl.concat([active_1, active_2], how="vertical").sort(["ts_ms", "symbol"])

    assert_frame_equal(actual_source, expected_source.sort(["ts_ms", "symbol"]))
    assert_frame_equal(actual_active, expected_active.sort(["ts_ms", "symbol"]))
    assert final_carry.group_by("symbol").len()["len"].max() == CONTINUOUS_TAIL_ROWS
