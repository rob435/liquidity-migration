"""Liquidation-collector tests (P3 2026-06-10): parsers + day-rotating writer."""

from __future__ import annotations

import json
import time
from pathlib import Path

from liquidity_migration.liquidation_collector import (
    MAX_CONNECTION_AGE_SECONDS,
    JsonlDayWriter,
    connection_expired,
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


def test_writer_rotates_by_utc_day_and_venue(tmp_path) -> None:
    w = JsonlDayWriter(tmp_path)
    day1 = 1_765_000_000_000
    day2 = day1 + 86_400_000
    w.write([
        {"recv_ms": day1, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day1},
        {"recv_ms": day2, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day2},
    ])
    files = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jsonl"))
    assert len(files) == 2
    assert all(f.startswith("bybit/") for f in files)
    assert w.written == 2
    # appends accumulate (no truncation)
    w.write([{"recv_ms": day1, "venue": "bybit", "symbol": "A", "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": day1}])
    day1_path = tmp_path / files[0]
    day2_path = tmp_path / files[1]
    day1_lines = [json.loads(x) for x in day1_path.read_text(encoding="utf-8").splitlines() if x]
    day2_lines = [json.loads(x) for x in day2_path.read_text(encoding="utf-8").splitlines() if x]
    assert [row["recv_ms"] for row in day1_lines] == [day1, day1]
    assert [row["recv_ms"] for row in day2_lines] == [day2]


def test_writer_counts_rows_per_venue(tmp_path) -> None:
    """The alive heartbeat reports explicit per-venue counts for the Bybit leg."""
    from liquidity_migration.liquidation_collector import JsonlDayWriter

    writer = JsonlDayWriter(tmp_path)
    writer.write(
        [
            {"venue": "bybit", "recv_ms": 1_781_100_000_000, "symbol": "AUSDT"},
            {"venue": "bybit", "recv_ms": 1_781_100_000_000, "symbol": "BUSDT"},
        ]
    )
    assert writer.written == 2
    assert writer.written_by_venue == {"bybit": 2}


def test_connection_expired_age_check() -> None:
    """Max-connection-age seam for new-listing pickup: the bybit leg's symbol
    list is fetched once per connection, so a connection older than the cap
    must be flagged expired (the message/heartbeat path then closes it and the
    reconnect loop re-fetches the universe and resubscribes)."""
    assert MAX_CONNECTION_AGE_SECONDS == 24 * 60 * 60
    opened = 1_000.0
    assert not connection_expired(opened, now_monotonic=opened)
    assert not connection_expired(opened, now_monotonic=opened + MAX_CONNECTION_AGE_SECONDS - 1)
    assert connection_expired(opened, now_monotonic=opened + MAX_CONNECTION_AGE_SECONDS)
    assert connection_expired(opened, now_monotonic=opened + MAX_CONNECTION_AGE_SECONDS + 1)
    # custom cap (and the boundary is inclusive at exactly max age)
    assert connection_expired(0.0, now_monotonic=5.0, max_age_seconds=5.0)
    assert not connection_expired(0.0, now_monotonic=4.9, max_age_seconds=5.0)
    # default clock: a just-opened connection is not expired
    assert not connection_expired(time.monotonic())


# --------------------------------------------------------------------------- #
# audit bucket b05 (liquidation-collector-4/5/6 writer + parser fixes;
# folded from test_audit_fix_b05.py)
# --------------------------------------------------------------------------- #
def _liq():
    import liquidity_migration.liquidation_collector as lc
    return lc


def test_writer_per_line_partial_failure_counts_only_unlanded(tmp_path, monkeypatch) -> None:
    """liquidation-collector-4: a disk-full OSError partway through a batch must leave
    every already-written line intact (no torn line) and count ONLY the rows that did
    not land — not the whole batch."""
    lc = _liq()
    w = lc.JsonlDayWriter(tmp_path)

    rows = [
        {"recv_ms": 1_765_000_000_000, "venue": "bybit", "symbol": f"S{i}",
         "side": "Buy", "qty": 1.0, "price": 2.0, "ts_ms": 1}
        for i in range(5)
    ]

    real_open = Path.open

    class _FailAfter:
        """A file wrapper that raises OSError on the 3rd write (disk full mid-batch)."""

        def __init__(self, fh):
            self._fh = fh
            self._n = 0

        def write(self, data):
            self._n += 1
            if self._n == 3:
                raise OSError("No space left on device")
            return self._fh.write(data)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

    def fake_open(self, *args, **kwargs):
        return _FailAfter(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fake_open)
    w.write(rows)
    monkeypatch.undo()

    # 2 lines landed before the 3rd write raised; 3 did not.
    assert w.written == 2
    assert w.dropped == 3
    assert w.written_by_venue.get("bybit") == 2
    # Every persisted line is a complete JSON record (no torn trailing line).
    f = next((tmp_path / "bybit").glob("*.jsonl"))
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # must not raise


def test_bybit_on_message_writes_before_age_cap_close() -> None:
    """liquidation-collector-5: the bybit on_message must parse-and-write the in-hand
    frame BEFORE the 24h connection-age close, so the rollover frame's rows are not
    discarded. Assert the ordering within the on_message body in the source."""
    import inspect
    lc = _liq()
    src = inspect.getsource(lc._run_bybit)
    # Isolate the on_message body (from its def to the next nested def, on_pong).
    om_start = src.index("def on_message(")
    om_end = src.index("def on_pong(", om_start)
    on_message = src[om_start:om_end]
    # The write must precede the expiry check WITHIN on_message.
    write_idx = on_message.index("writer.write(parse_bybit_event")
    close_idx = on_message.index("_close_if_expired(ws)", write_idx)
    assert close_idx > write_idx, "on_message must write the frame before the age-cap close"
    # The OLD bug — a guard-return at the very top before any parse/write — must be gone:
    # on_message must not return early on expiry before having written the frame.
    first_line = on_message.split("\n", 1)[1].lstrip()
    assert not first_line.startswith("if _close_if_expired(ws):"), \
        "on_message must not short-circuit on expiry before writing the frame"


def test_parse_drops_zero_and_negative_price_rows() -> None:
    """liquidation-collector-6: a row with price<=0 (missing/garbage price) is dropped
    the same way zero-qty rows are, so notional aggregations are never polluted."""
    lc = _liq()
    recv = 1_765_000_000_000

    # Bybit: zero price dropped, positive price kept.
    assert lc.parse_bybit_event(
        {"topic": "allLiquidation.X", "data": {"s": "X", "S": "Buy", "v": "1", "p": "0"}}, recv) == []
    kept = lc.parse_bybit_event(
        {"topic": "allLiquidation.X", "data": {"s": "X", "S": "Buy", "v": "1", "p": "2.5"}}, recv)
    assert len(kept) == 1 and kept[0]["price"] == 2.5


def test_module_documents_per_venue_side_price_schema() -> None:
    """liquidation-collector-6: the module docstring documents the side/price schema."""
    lc = _liq()
    doc = lc.__doc__ or ""
    assert "Row schema" in doc
    assert "Buy" in doc
    assert "binance" not in doc.lower()
    assert "LIQUIDATED order" in doc or "liquidated" in doc.lower()
    assert "price > 0" in doc
