"""Liquidation-collector tests (P3 2026-06-10): parsers + day-rotating writer."""

from __future__ import annotations

import json

from liquidity_migration.liquidation_collector import (
    JsonlDayWriter,
    parse_binance_event,
    parse_bybit_event,
)

RECV = 1_765_000_000_000  # 2025-12-06ish UTC


def test_parse_bybit_event_list_and_dict_shapes() -> None:
    msg = {"topic": "allLiquidation.AAAUSDT",
           "data": [{"T": 1, "s": "AAAUSDT", "S": "Buy", "v": "2.5", "p": "1.05"}]}
    rows = parse_bybit_event(msg, RECV)
    assert len(rows) == 1
    r = rows[0]
    assert (r["venue"], r["symbol"], r["side"]) == ("bybit", "AAAUSDT", "Buy")
    assert r["qty"] == 2.5 and r["price"] == 1.05 and r["ts_ms"] == 1 and r["recv_ms"] == RECV
    # dict-shaped data + non-liq frames
    assert parse_bybit_event({"topic": "allLiquidation.X", "data": {"s": "X", "S": "Sell", "v": 1, "p": 2}}, RECV)
    assert parse_bybit_event({"op": "subscribe", "success": True}, RECV) == []
    assert parse_bybit_event({"topic": "tickers.BTCUSDT", "data": []}, RECV) == []


def test_parse_binance_event_single_and_array() -> None:
    e = {"e": "forceOrder", "E": 100,
         "o": {"s": "BBBUSDT", "S": "Sell", "q": "10", "ap": "3.2", "p": "3.1", "T": 99}}
    rows = parse_binance_event(e, RECV)
    assert len(rows) == 1
    r = rows[0]
    assert (r["venue"], r["symbol"], r["side"]) == ("binance", "BBBUSDT", "Sell")
    assert r["qty"] == 10.0 and r["price"] == 3.2 and r["ts_ms"] == 99
    assert parse_binance_event([e, e], RECV) and len(parse_binance_event([e, e], RECV)) == 2
    assert parse_binance_event({"e": "aggTrade"}, RECV) == []
    # falls back to mark/last price p when ap missing; zero-qty dropped
    e2 = {"e": "forceOrder", "o": {"s": "C", "S": "Buy", "q": "1", "p": "5", "T": 7}}
    assert parse_binance_event(e2, RECV)[0]["price"] == 5.0
    assert parse_binance_event({"e": "forceOrder", "o": {"s": "C", "S": "Buy", "q": "0", "p": "5"}}, RECV) == []


def test_writer_rotates_by_utc_day_and_venue(tmp_path) -> None:
    w = JsonlDayWriter(tmp_path)
    day1 = 1_765_000_000_000
    day2 = day1 + 86_400_000
    w.write([
        {"recv_ms": day1, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day1},
        {"recv_ms": day2, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day2},
        {"recv_ms": day1, "venue": "binance", "symbol": "B", "side": "Sell", "qty": 3.0, "price": 4.0, "ts_ms": day1},
    ])
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jsonl"))
    assert len(files) == 3
    assert any(f.startswith("bybit/") for f in files) and any(f.startswith("binance/") for f in files)
    assert w.written == 3
    # appends accumulate (no truncation)
    w.write([{"recv_ms": day1, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day1}])
    byb = [p for p in tmp_path.rglob("*.jsonl") if "bybit" in str(p) and p.stem != ""][0]
    lines = [json.loads(x) for x in byb.read_text(encoding="utf-8").splitlines() if x]
    assert len(lines) >= 2
