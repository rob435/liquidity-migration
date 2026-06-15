"""audit2c eventdemo_mark — build_position_pnl_snapshot must never mark at liqPrice.

A missing markPrice used to fall back to the LIQUIDATION price as the mark,
producing a wildly-wrong (report/telemetry only) PnL snapshot. The corrected
behavior prefers markPrice -> lastPrice -> avgPrice, and leaves the mark and
unrealized fields null when no genuine mark is available rather than
substituting liqPrice.
"""

from __future__ import annotations

from liquidity_migration.event_demo import (
    build_position_pnl_snapshot,
    summarize_position_pnl,
)


def test_missing_mark_does_not_use_liqprice() -> None:
    # markPrice absent, liqPrice present (and far from avg, as a liq price is).
    # Old code marked at liqPrice=50 => mark_price 50.0 (wildly wrong); the fix
    # falls back to avgPrice (the next legitimate price), never liqPrice.
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "size": "10",
                "avgPrice": "100",
                "liqPrice": "50",
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    # The defect: never adopt liqPrice as the mark.
    assert row["mark_price"] != 50.0
    # markPrice/lastPrice absent -> mark falls back to avgPrice, not liqPrice.
    assert row["mark_price"] == 100.0
    # avgPrice is reported as the entry, untouched by the mark fix.
    assert row["avg_price"] == 100.0


def test_no_mark_or_avg_leaves_fields_null() -> None:
    # None of markPrice / lastPrice / avgPrice present, only liqPrice. Old code
    # marked at liqPrice=50; the fix leaves the mark/unrealized fields null.
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "size": "10",
                "liqPrice": "50",
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["mark_price"] is None
    assert row["unrealized_pnl_usdt"] is None
    assert row["pnl_pct"] is None


def test_lastprice_used_when_markprice_missing() -> None:
    # markPrice missing but lastPrice present -> mark falls back to lastPrice,
    # NOT liqPrice (old code would have returned liqPrice=50).
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "10",
                "avgPrice": "100",
                "lastPrice": "95",
                "liqPrice": "50",
                "positionValue": "950",
                "unrealisedPnl": "50",
            }
        ]
    )

    assert rows[0]["mark_price"] == 95.0
    assert rows[0]["unrealized_pnl_usdt"] == 50.0


def test_summary_tolerates_null_mark_rows() -> None:
    # A null-mark row must not crash the summary aggregation or the sort key.
    rows = build_position_pnl_snapshot(
        [
            {"symbol": "NOMARKUSDT", "side": "Buy", "size": "10", "liqPrice": "50"},
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "10",
                "avgPrice": "100",
                "markPrice": "95",
                "positionValue": "950",
                "unrealisedPnl": "50",
            },
        ]
    )

    summary = summarize_position_pnl(rows)
    assert summary["positions"] == 2
    # The null-mark row contributes 0 to the totals; only the marked row counts.
    assert summary["unrealized_pnl_usdt"] == 50.0
