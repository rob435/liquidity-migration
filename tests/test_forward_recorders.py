"""P0.3 forward-recorder tests (code staged only; nothing installs it)."""

from __future__ import annotations

import json
from pathlib import Path

from liquidity_migration.forward_recorders import (
    DailyPartitionWriter,
    ForwardRecorderService,
    coverage_receipt,
    normalize_liquidation_frame,
    summarize_orderbook,
)

TS = 1_784_500_000_000  # 2026-07-19 UTC region


def liq_frame(*items: dict) -> dict:
    return {"topic": "allLiquidation.BTCUSDT", "ts": TS, "data": list(items)}


class TestNormalizeLiquidation:
    def test_happy_path(self) -> None:
        rows, malformed = normalize_liquidation_frame(
            liq_frame({"T": TS, "s": "btcusdt", "S": "Buy", "v": "0.5", "p": "64000"}),
            received_ts_ms=TS + 5,
        )
        assert malformed == 0 and len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "BTCUSDT"
        assert row["notional_quote"] == 32000.0
        assert row["received_ts_ms"] == TS + 5

    def test_malformed_entries_counted_not_raised(self) -> None:
        rows, malformed = normalize_liquidation_frame(
            liq_frame(
                {"T": TS, "s": "AUSDT", "S": "Sell", "v": "1", "p": "2"},
                {"T": TS, "s": "BUSDT", "S": "Sell", "v": "oops", "p": "2"},
                {"missing": "fields"},
                {"T": TS, "s": "CUSDT", "S": "Sell", "v": "-1", "p": "2"},
            ),
            received_ts_ms=TS,
        )
        assert len(rows) == 1 and malformed == 3

    def test_structurally_invalid_frame(self) -> None:
        rows, malformed = normalize_liquidation_frame({"data": "nope"}, received_ts_ms=TS)
        assert rows == [] and malformed == 1


class TestSummarizeOrderbook:
    def test_depth_bands_and_spread(self) -> None:
        row = summarize_orderbook(
            bids=[(99.9, 10.0), (99.5, 20.0), (98.0, 50.0)],
            asks=[(100.1, 8.0), (100.5, 25.0), (102.0, 40.0)],
            symbol="AUSDT", venue_ts_ms=TS, received_ts_ms=TS + 1,
        )
        assert row is not None
        assert abs(row["mid"] - 100.0) < 1e-9
        assert abs(row["spread_bps"] - 20.0) < 1e-6
        # 10bps band = [99.9, 100.1]: only the touch levels
        assert abs(row["bid_quote_10bps"] - 99.9 * 10.0) < 1e-6
        assert abs(row["ask_quote_10bps"] - 100.1 * 8.0) < 1e-6
        # 100bps band = [99.0, 101.0]: adds the 99.5 and 100.5 levels
        assert abs(row["bid_quote_100bps"] - (99.9 * 10 + 99.5 * 20)) < 1e-6
        assert abs(row["ask_quote_100bps"] - (100.1 * 8 + 100.5 * 25)) < 1e-6
        assert -1.0 <= row["imbalance_100bps"] <= 1.0

    def test_crossed_or_missorted_books_are_uncountable(self) -> None:
        assert summarize_orderbook([(101.0, 1.0)], [(100.0, 1.0)], symbol="A", venue_ts_ms=TS, received_ts_ms=TS) is None
        assert summarize_orderbook([(99.0, 1.0), (99.5, 1.0)], [(100.0, 1.0)], symbol="A", venue_ts_ms=TS, received_ts_ms=TS) is None
        assert summarize_orderbook([], [(100.0, 1.0)], symbol="A", venue_ts_ms=TS, received_ts_ms=TS) is None


class TestWriterAndReceipts:
    def test_partitioned_append_and_receipt(self, tmp_path: Path) -> None:
        writer = DailyPartitionWriter(tmp_path, "liquidations")
        day1 = TS
        day2 = TS + 86_400_000
        writer.add([{"venue_ts_ms": day1, "symbol": "A"}, {"venue_ts_ms": day2, "symbol": "B"}], malformed=2)
        assert writer.flush() == 2
        writer.add([{"venue_ts_ms": day1, "symbol": "C"}])
        assert writer.flush() == 1
        receipt = json.loads((tmp_path / "liquidations" / "_coverage_receipt.json").read_text())
        assert receipt["rows"] == 3 and receipt["days"] == 2
        assert receipt["recorder_session"]["malformed_this_session"] == 2
        cov = coverage_receipt(tmp_path, "liquidations")
        assert cov["gap_days"] == []
        assert cov["rows_per_day"]["max"] == 2

    def test_gap_days_are_reported(self, tmp_path: Path) -> None:
        writer = DailyPartitionWriter(tmp_path, "l2_summaries")
        writer.add([{"venue_ts_ms": TS}, {"venue_ts_ms": TS + 2 * 86_400_000}])
        writer.flush()
        cov = coverage_receipt(tmp_path, "l2_summaries")
        assert len(cov["gap_days"]) == 1


class FakeSocket:
    def __init__(self) -> None:
        self.liq_cb = None
        self.book_cb = None

    def all_liquidation_stream(self, symbols, callback) -> None:  # noqa: ANN001
        self.liq_cb = callback

    def orderbook_stream(self, depth, symbols, callback) -> None:  # noqa: ANN001
        self.book_cb = callback


class TestService:
    def test_end_to_end_offline(self, tmp_path: Path) -> None:
        fake = FakeSocket()
        clock = {"now": TS}
        service = ForwardRecorderService(
            tmp_path, ["BTCUSDT"], websocket_factory=lambda: fake,
            l2_summary_interval_s=60.0, clock_ms=lambda: clock["now"],
        )
        service.start()
        assert fake.liq_cb is not None and fake.book_cb is not None
        fake.liq_cb(liq_frame({"T": TS, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "64000"}))
        book = {"ts": TS, "data": {"s": "BTCUSDT", "b": [["99.9", "1"]], "a": [["100.1", "2"]]}}
        fake.book_cb(book)
        clock["now"] = TS + 30_000
        fake.book_cb(book)  # inside the cadence window -> suppressed
        clock["now"] = TS + 61_000
        fake.book_cb(book)  # past the cadence -> second summary
        result = service.flush()
        assert result["liquidation_rows"] == 1
        assert result["l2_summary_rows"] == 2
        assert result["uncountable_books"] == 0
        assert (tmp_path / "liquidations" / "date=2026-07-19" / "records.jsonl").exists() or any(
            (tmp_path / "liquidations").glob("date=*/records.jsonl")
        )

    def test_bad_books_counted(self, tmp_path: Path) -> None:
        fake = FakeSocket()
        service = ForwardRecorderService(
            tmp_path, ["AUSDT"], websocket_factory=lambda: fake, clock_ms=lambda: TS,
        )
        service.start()
        fake.book_cb({"ts": TS, "data": {"s": "AUSDT", "b": [["101", "1"]], "a": [["100", "1"]]}})
        assert service.flush()["uncountable_books"] == 1
