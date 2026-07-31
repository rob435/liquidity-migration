from __future__ import annotations

import datetime as dt

import polars as pl

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.strategy_funnel import (
    validate_decision_funnel,
    validate_path_labels,
)
from scripts.data.build_candidate_tape import (
    CONTINUOUS_REQUIRED_GATES,
    _bars_by_symbol,
    _build_continuous_funnel,
    _build_path_labels,
    _valid_ohlc,
)


def test_invalid_ohlc_is_explicitly_rejected() -> None:
    frame = pl.DataFrame(
        {
            "open": [1.0, None],
            "high": [1.1, 1.1],
            "low": [0.9, 0.9],
            "close": [1.0, 1.0],
        }
    )

    assert frame.filter(_valid_ohlc()).height == 1


def test_continuous_candidate_partition_is_structurally_complete() -> None:
    start = dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    symbols = [f"S{index:02d}USDT" for index in range(40)]
    rows = []
    for symbol_index, symbol in enumerate(symbols):
        base = 100.0 + symbol_index
        for hour in range(-60, 81):
            ts_ms = start_ms + hour * MS_PER_HOUR
            close = base * (1.0 + (hour + 60) * 0.0001)
            if hour >= 0:
                close *= 1.0 + symbol_index / 1_000.0
            rows.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": close * 0.999,
                    "high": close * 1.001,
                    "low": close * 0.998,
                    "close": close,
                    "volume_base": 10_000.0,
                    "turnover_quote": 600_000.0 + symbol_index,
                    "date": dt.datetime.fromtimestamp(
                        ts_ms / 1000,
                        tz=dt.timezone.utc,
                    ).date().isoformat(),
                }
            )
    klines = pl.from_dicts(rows)
    manifest = pl.from_dicts(
        [
            {
                "date": "2026-07-05",
                "symbol": symbol,
                "first_archive_observed_date": "2025-01-01",
                "membership_source": "fixture",
                "membership_inferred": False,
                "membership_provenance_limitation": None,
            }
            for symbol in symbols
        ]
    )
    bars = _bars_by_symbol(klines)

    funnel, expected_keys = _build_continuous_funnel(
        klines,
        manifest,
        venue="bybit",
        start_ms=start_ms,
        end_ms=start_ms + MS_PER_HOUR,
        bars_by_symbol=bars,
    )

    assert funnel.height > 0
    assert set(funnel["source_key"].to_list()) == expected_keys
    assert funnel["source_key"].n_unique() == funnel.height
    assert funnel["barebones_accepted"].all()
    structural = validate_decision_funnel(
        funnel,
        required_gate_orders={"continuous": CONTINUOUS_REQUIRED_GATES},
    )
    assert structural["rows"] == funnel.height

    labels = _build_path_labels(funnel, bars)
    label_structure = validate_path_labels(labels, funnel)
    assert label_structure["rows"] == funnel.height
