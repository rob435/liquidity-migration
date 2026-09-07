from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from liquidity_migration.data.history import import_history, normalize, timestamp_ns

FIXTURES = Path(__file__).parents[1] / "fixtures" / "history"


def mapping() -> dict:
    return json.loads((FIXTURES / "csv-mapping.json").read_text())


def test_actual_bybit_csv_and_parquet_preserve_identical_facts(tmp_path):
    bybit = json.loads((FIXTURES / "bybit-mapping.json").read_text())
    targets = [tmp_path / f"{i}.jsonl" for i in range(3)]
    for source, config, output in zip(
        ["bybit-trades.csv", "bybit-trades.csv", "bybit-trades.parquet"],
        [bybit, mapping(), mapping()], targets, strict=True,
    ):
        assert import_history([FIXTURES / source], config, output) == {"rows": 64, "duplicates": 0}
    assert targets[0].read_bytes() == targets[1].read_bytes() == targets[2].read_bytes()
    events = [json.loads(line) for line in targets[0].read_text().splitlines()[1:]]
    assert [r["recv_ns"] for r in events] == sorted(r["recv_ns"] for r in events)
    assert all(r["recv_ns"] - r["exchange_ts_ns"] == 1_000_000 for r in events)
    assert events[-1]["exchange_ts_ns"] == 1585180700064700000


def test_timestamp_precision_and_invalid_inputs():
    assert timestamp_ns("1585180700.0647", "s") == 1585180700064700000
    assert timestamp_ns("1585180700064700001", "ns") == 1585180700064700001
    for value in ["NaN", "Infinity", "-1", "0.0000000001", str(2**63)]:
        with pytest.raises(ValueError):
            timestamp_ns(value, "s")


def test_duplicates_are_counted_and_conflicts_rejected_without_output(tmp_path):
    source = FIXTURES / "bybit-trades.csv"
    output = tmp_path / "same.jsonl"
    assert import_history([source, source], mapping(), output) == {"rows": 64, "duplicates": 64}
    lines = source.read_text().splitlines()
    row = lines[1].split(",")
    row[4] = "100"
    changed = tmp_path / "changed.csv"
    changed.write_text(lines[0] + "\n" + ",".join(row) + "\n")
    with pytest.raises(ValueError, match="conflicting duplicate identity"):
        import_history([source, changed], mapping(), tmp_path / "conflict.jsonl")
    assert not (tmp_path / "conflict.jsonl").exists()


def test_symbol_and_missing_fields_are_never_guessed():
    import csv
    row = next(csv.DictReader((FIXTURES / "bybit-trades.csv").open()))
    for field, value in [("side", "unknown"), ("symbol", "ETHUSDT"), ("timestamp", None), ("price", "nan"), ("size", "0")]:
        with pytest.raises(ValueError):
            normalize({**row, field: value}, mapping())
    m = mapping()
    m["membership"][0]["end_ns"] = 1
    with pytest.raises(ValueError, match="membership"):
        normalize(row, m)
    m = mapping()
    m["symbols"] = {"XBT": "BTCUSDT"}
    assert normalize({**row, "symbol": "XBT"}, m)["symbol"] == "BTCUSDT"


def test_observed_availability_and_bar_close_are_causal():
    m = copy.deepcopy(mapping())
    m.update(kind="bar", timestamp_unit="ns", delivery={"kind": "observed"})
    m["membership"] = [{"symbol": "BTCUSDT", "start_ns": 0, "end_ns": 10_000}]
    m["columns"] = {k: k for k in ["symbol", "exchange_ts_ns", "recv_ns", "start_ns", "end_ns", "open", "high", "low", "close", "volume"]}
    row = dict(symbol="BTCUSDT", exchange_ts_ns=200, recv_ns=220, start_ns=100, end_ns=200,
               open=10, high=12, low=9, close=11, volume=0)
    assert normalize(row, m)["volume"] == 0
    for field, value in [("recv_ns", 199), ("volume", None), ("exchange_ts_ns", 100), ("low", 11)]:
        with pytest.raises(ValueError):
            normalize({**row, field: value}, m)


def test_missing_ticker_fields_remain_null():
    m = mapping()
    m.update(kind="ticker", timestamp_unit="ns")
    m["membership"] = [{"symbol": "BTCUSDT", "start_ns": 0, "end_ns": 10_000}]
    m["columns"] = {"symbol": "symbol", "exchange_ts_ns": "at", "last_price": "last"}
    row = normalize({"symbol": "BTCUSDT", "at": 100, "last": 10}, m)
    assert row["mark_price"] is None and row["funding_rate"] is None
