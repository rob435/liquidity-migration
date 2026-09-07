"""Normalize external history for the Rust execution backtest, without strategy logic."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from liquidity_migration.data.archive import _public_trade_text_handle

BYBIT_TRADE_COLUMNS = {
    "exchange_ts_ns": "timestamp", "symbol": "symbol", "price": "price",
    "qty": "size", "side": "side", "trade_id": "trdMatchID",
}


def timestamp_ns(value: Any, unit: str) -> int:
    factor = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}[unit]
    number = Decimal(str(value)) * factor
    if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 2**63 - 1:
        raise ValueError(f"timestamp is not an exact supported nanosecond value: {value!r}")
    return int(number)


def input_rows(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
            yield from batch.to_pylist()
    else:
        with _public_trade_text_handle(path) as handle:
            yield from csv.DictReader(handle)


def normalize(raw: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    columns = mapping["columns"]
    def get(name: str, *, optional: bool = False) -> Any:
        value = raw.get(columns.get(name, ""))
        if value is None or value == "":
            if optional:
                return None
            raise ValueError(f"missing required {name} (column {columns.get(name)!r})")
        return value
    def number(name: str, *, optional: bool = False) -> float | None:
        value = get(name, optional=optional)
        if value is None:
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"nonfinite {name}")
        return parsed
    symbols = mapping["symbols"]
    native = str(get("symbol"))
    if native not in symbols:
        raise ValueError(f"unmapped symbol {native!r}")
    symbol = symbols[native]
    unit = mapping["timestamp_unit"]
    kind = mapping["kind"]
    exchange = timestamp_ns(get("exchange_ts_ns"), unit)
    delivery = mapping["delivery"]
    if delivery["kind"] == "observed":
        at = timestamp_ns(get("recv_ns"), mapping.get("receive_timestamp_unit", unit))
    elif delivery["kind"] == "exchange_plus_delay":
        delay = delivery["delay_ns"]
        if not isinstance(delay, int) or delay < 0:
            raise ValueError("delay_ns must be a nonnegative integer")
        at = exchange + delay
    else:
        raise ValueError("delivery requires observed or exchange_plus_delay")
    if at < exchange or at > 2**63 - 1:
        raise ValueError("availability precedes exchange time or overflows the clock")
    row: dict[str, Any] = {"kind": kind, "venue": mapping["venue"], "symbol": symbol,
                           "exchange_ts_ns": exchange, "recv_ns": at}
    if not any(m["symbol"] == symbol and m["start_ns"] <= exchange < m["end_ns"] for m in mapping["membership"]):
        raise ValueError(f"{symbol} at {exchange} is outside declared membership")
    if kind == "trade":
        side = str(get("side"))
        sides = mapping.get("sides", {"Buy": True, "Sell": False})
        if side not in sides or not isinstance(sides[side], bool):
            raise ValueError(f"unmapped aggressor side {side!r}")
        row.update(price=number("price"), qty=number("qty"), buyer_aggressor=sides[side], trade_id=str(get("trade_id")))
        if row["price"] <= 0 or row["qty"] <= 0:
            raise ValueError("trade price and quantity must be positive")
    elif kind == "bar":
        row.update({key: number(key) for key in ("open", "high", "low", "close", "volume")})
        row["start_ns"] = timestamp_ns(get("start_ns"), unit)
        row["end_ns"] = timestamp_ns(get("end_ns"), unit)
        if not (row["start_ns"] < row["end_ns"] == exchange <= at) or row["volume"] < 0:
            raise ValueError("bar exchange timestamp must be its exclusive close; availability follows close")
        if not (0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]):
            raise ValueError("inconsistent OHLC prices")
    elif kind == "ticker":
        row.update({key: number(key, optional=True) for key in ("last_price", "mark_price", "index_price", "funding_rate")})
        next_time = get("next_funding_time_ms", optional=True)
        row["next_funding_time_ms"] = None if next_time is None else timestamp_ns(next_time, "ms") // 1_000_000
        if any(row[k] is not None and row[k] <= 0 for k in ("last_price", "mark_price", "index_price", "next_funding_time_ms")):
            raise ValueError("invalid ticker price or funding boundary")
    elif kind == "book":
        # Input rows are already reconstructed snapshots. Venue delta chaining
        # belongs in a venue decoder, never in a field-name mapping.
        row.update(valid=True, depth=int(get("depth")), update_id=int(get("update_id")), cross_sequence=int(get("cross_sequence")))
        for key in ("bids", "asks"):
            value = get(key)
            value = json.loads(value) if isinstance(value, str) else value
            row[key] = [{"px": float(level[0]), "qty": float(level[1])} for level in value]
    else:
        raise ValueError(f"unsupported event kind {kind!r}")
    return row


def import_history(paths: list[Path], mapping: dict[str, Any], output: Path) -> dict[str, int]:
    if mapping.get("source") == "bybit_archive":
        if mapping["kind"] != "trade" or mapping["venue"] != "bybit-linear":
            raise ValueError("Bybit archive decoding requires trade / bybit-linear")
        mapping = {**mapping, "columns": BYBIT_TRADE_COLUMNS, "timestamp_unit": "s"}
    header = {"schema": "historical_v1", "venue": mapping["venue"],
              "delivery": json.dumps(mapping["delivery"], sort_keys=True),
              "channels": [mapping["kind"]], "membership": mapping["membership"],
              "instrument_assumption": mapping["instrument_assumption"]}
    if not header["venue"] or not header["instrument_assumption"].strip():
        raise ValueError("declare venue and historical instrument assumption")
    if output.exists():
        raise FileExistsError(output)
    count = duplicates = 0
    # Disk sorting retains equal-time input order and avoids a day-sized Python
    # duplicate set. Conflicting trade identities fail instead of picking a copy.
    with tempfile.TemporaryDirectory(prefix="history-import-", dir=output.parent) as scratch:
        db = sqlite3.connect(str(Path(scratch) / "sort.sqlite"))
        try:
            db.execute("CREATE TABLE rows (ordinal INTEGER PRIMARY KEY, at INTEGER, identity TEXT UNIQUE, payload TEXT)")
            for path in paths:
                for line, raw in enumerate(input_rows(path), 2):
                    try:
                        row = normalize(raw, mapping)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ValueError(f"{path}:{line}: {exc}") from exc
                    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    identity = json.dumps([row["venue"], row["symbol"], row.get("trade_id")]) if row["kind"] == "trade" else payload
                    prior = db.execute("SELECT payload FROM rows WHERE identity=?", (identity,)).fetchone()
                    if prior:
                        if prior[0] != payload:
                            raise ValueError(f"{path}:{line}: conflicting duplicate identity {identity}")
                        duplicates += 1
                    else:
                        db.execute("INSERT INTO rows(at,identity,payload) VALUES(?,?,?)", (row["recv_ns"], identity, payload))
                        count += 1
            db.commit()
            staged = Path(scratch) / "history.jsonl"
            with staged.open("w") as handle:
                handle.write(json.dumps(header, sort_keys=True) + "\n")
                for (payload,) in db.execute("SELECT payload FROM rows ORDER BY at,ordinal"):
                    handle.write(payload + "\n")
            # Exclusive creation also protects the original if another importer
            # selected the same output while this one was sorting.
            with output.open("x") as dest, staged.open() as src:
                import shutil
                shutil.copyfileobj(src, dest)
        finally:
            db.close()
    return {"rows": count, "duplicates": duplicates}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(import_history(args.input, json.loads(args.mapping.read_text()), args.output)))


if __name__ == "__main__":
    main()
