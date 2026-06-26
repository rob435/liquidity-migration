"""Unit tests for the three-way reconcile pairing logic (scripts/reconcile_three_way.py).

Covers the pure entry-key extraction that backs the LONG demo<->backtest<->paper
agreement: window clipping, (symbol, side, signal-day) bucketing, and the missing
/ schema-degenerate fallbacks. The data-download + backtest legs are integration
surfaces and are exercised by running the tool, not here.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reconcile_three_way as tw  # noqa: E402


def _ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp() * 1000)


def test_key_set_buckets_by_symbol_side_signal_day():
    df = pl.DataFrame({
        "symbol": ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
        "signal_ts_ms": [_ms(2026, 6, 10, 9), _ms(2026, 6, 10, 23), _ms(2026, 6, 11, 1)],
        "side": ["LONG", "long", "long"],
    })
    keys = tw._key_set(df, "signal_ts_ms", _ms(2026, 6, 1), _ms(2026, 6, 18))
    assert keys == {
        ("BTCUSDT", "long", "2026-06-10"),
        ("ETHUSDT", "long", "2026-06-10"),
        ("BTCUSDT", "long", "2026-06-11"),
    }


def test_key_set_clips_to_window_half_open():
    df = pl.DataFrame({
        "symbol": ["AAAUSDT", "BBBUSDT", "CCCUSDT"],
        "signal_ts_ms": [_ms(2026, 6, 3, 23), _ms(2026, 6, 4), _ms(2026, 6, 18)],
        "side": ["long", "long", "long"],
    })
    # window [2026-06-04, 2026-06-18): start inclusive, end exclusive.
    keys = tw._key_set(df, "signal_ts_ms", _ms(2026, 6, 4), _ms(2026, 6, 18))
    assert keys == {("BBBUSDT", "long", "2026-06-04")}


def test_key_set_handles_missing_side_and_empty():
    no_side = pl.DataFrame({"symbol": ["XUSDT"], "signal_ts_ms": [_ms(2026, 6, 5)]})
    assert tw._key_set(no_side, "signal_ts_ms", _ms(2026, 6, 1), _ms(2026, 6, 18)) == {
        ("XUSDT", "", "2026-06-05")
    }
    empty = pl.DataFrame({"symbol": [], "signal_ts_ms": []})
    assert tw._key_set(empty, "signal_ts_ms", 0, _ms(2030, 1, 1)) == set()
    # absent signal column -> empty (never raises)
    assert tw._key_set(pl.DataFrame({"symbol": ["X"]}), "signal_ts_ms", 0, 1) == set()


def test_backtest_keys_missing_csv_is_empty(tmp_path):
    assert tw._backtest_keys(tmp_path / "nope.csv", 0, _ms(2030, 1, 1)) == set()


def test_backtest_keys_reads_entry_signal_column(tmp_path):
    csv = tmp_path / "long_native_trades.csv"
    pl.DataFrame({
        "symbol": ["BTCUSDT", "ETHUSDT"],
        "side": ["long", "long"],
        "entry_signal_ts_ms": [_ms(2026, 6, 10), _ms(2026, 6, 30)],  # 2nd outside window
    }).write_csv(csv)
    keys = tw._backtest_keys(csv, _ms(2026, 6, 4), _ms(2026, 6, 18))
    assert keys == {("BTCUSDT", "long", "2026-06-10")}


def test_long_cadence_stats_classifies_near_p95_gap(tmp_path):
    csv = tmp_path / "long_v11a_bybit_refreshed_2026-06-24" / "long" / "long_native_trades.csv"
    csv.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E"],
        "side": ["long"] * 5,
        "entry_ts_ms": [
            _ms(2026, 1, 1),
            _ms(2026, 1, 3),
            _ms(2026, 1, 10),
            _ms(2026, 2, 20),
            _ms(2026, 3, 1),
        ],
    }).write_csv(csv)
    stats = tw._long_cadence_stats(
        csv,
        as_of=dt.date(2026, 4, 9),
        forward_start=dt.date(2026, 3, 15),
    )
    assert stats is not None
    assert stats["venue"] == "bybit"
    assert stats["current_gap_days"] == 39
    assert stats["p95_gap_days"] == 41
    assert stats["max_gap_days"] == 41
    assert stats["trades_since_forward_start"] == 0
    assert stats["status"] == "near_or_above_p95"


def test_format_long_cadence_diagnostic_summarizes_references(tmp_path):
    csv = tmp_path / "long_v11a_binance_refreshed_2026-06-24" / "long" / "long_native_trades.csv"
    csv.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "side": ["long"] * 3,
        "entry_ts_ms": [_ms(2026, 1, 1), _ms(2026, 1, 5), _ms(2026, 1, 9)],
    }).write_csv(csv)
    line = tw._format_long_cadence_diagnostic(
        [csv],
        as_of=dt.date(2026, 1, 20),
        forward_start=dt.date(2026, 1, 10),
    )
    assert line is not None
    assert line.startswith("LONG cadence diagnostic [above_historical_max]:")
    assert "binance: gap=11d" in line
    assert "trades_since_forward_start=0" in line
