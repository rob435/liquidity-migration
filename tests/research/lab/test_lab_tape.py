from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.lab.tape import best_lag, lead_lag, log_returns, tape_bars

FIXTURE = Path(__file__).resolve().parents[2] / "market_tape" / "fixtures" / "host" / "bybit-linear"
SECOND = 1_000_000_000


def _bars(venue: str, symbol: str, prices: list[float | None], *, start: int = 0) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "venue": [venue] * len(prices),
            "symbol": [symbol] * len(prices),
            "bucket_start_ns": [start + index * SECOND for index in range(len(prices))],
            "mid": prices,
        },
        schema={"venue": pl.String, "symbol": pl.String, "bucket_start_ns": pl.Int64, "mid": pl.Float64},
    )


def test_log_returns_carry_the_price_forward_inside_a_symbol() -> None:
    bars = _bars("bybit", "BTCUSDT", [100.0, None, 110.0, 110.0])
    returns = log_returns(bars)
    assert returns["ret"].to_list() == pytest.approx([0.0, math.log(1.1), 0.0])
    assert returns["bucket_start_ns"].to_list() == [SECOND, 2 * SECOND, 3 * SECOND]


def test_lead_lag_finds_the_venue_that_moves_first() -> None:
    rng = np.random.default_rng(7)
    steps = rng.normal(0.0, 1e-3, 400)
    leader = 100.0 * np.exp(np.cumsum(steps))
    # The follower prints the leader's price two seconds later.
    follower = np.concatenate([[leader[0], leader[0]], leader[:-2]])
    first = _bars("bybit", "BTCUSDT", leader.tolist())
    second = _bars("binance", "BTCUSDT", follower.tolist())
    table = lead_lag(first, second, max_lag=5)
    assert table.height == 11
    best = best_lag(table)
    assert best["symbol"].to_list() == ["BTCUSDT"]
    assert best["lag"].to_list() == [2]
    assert best["corr"][0] > 0.99
    reverse = best_lag(lead_lag(second, first, max_lag=5))
    assert reverse["lag"].to_list() == [-2]


def test_lead_lag_maps_symbols_and_skips_names_only_one_venue_has() -> None:
    prices = [100.0, 101.0, 100.5, 102.0, 101.5, 103.0]
    first = pl.concat([_bars("bybit", "1000SHIBUSDT", prices), _bars("bybit", "ONLYUSDT", prices)])
    second = _bars("binance", "SHIB1000USDT", prices)
    table = lead_lag(first, second, max_lag=1, symbol_map={"SHIB1000USDT": "1000SHIBUSDT"})
    assert table["symbol"].unique().to_list() == ["1000SHIBUSDT"]
    at_zero = table.filter(pl.col("lag") == 0)["corr"][0]
    assert at_zero > 0.999


def test_lead_lag_on_a_single_bucket_or_empty_frames_is_explicit() -> None:
    empty = lead_lag(_bars("bybit", "BTCUSDT", []), _bars("binance", "BTCUSDT", []))
    assert empty.is_empty() and empty.columns == ["symbol", "lag", "corr", "n"]
    assert best_lag(empty).is_empty()


def test_tape_bars_reads_the_fixture_hour() -> None:
    bars = tape_bars(str(FIXTURE), start_hour="2026-08-30T00", end_hour="2026-08-30T01", interval_seconds=1, symbols=["BTCUSDT"])
    assert bars["symbol"].unique().to_list() == ["BTCUSDT"]
    assert bars.height > 0
    assert {"bucket_start_ns", "mid", "trades", "volume"} <= set(bars.columns)
