"""Backwards pagination must survive a history that ends on a page boundary.

The bug this pins: a symbol listed mid-window whose history is an exact
multiple of the page limit gets one legitimately empty page after the last
full one, and the old guard read that as a mid-range hole and aborted — which
took the whole threaded backfill down with it (`downloaders` has no per-symbol
catch). An empty page after a full page is now re-asked before it is believed:
confirmed empty is end-of-history; a transient empty that turns into data on
the retry keeps the fetch going.
"""

from __future__ import annotations

from typing import Any

from liquidity_migration.marketdata.bybit_market_data import BybitMarketData


class _ScriptedFunding:
    """Answers get_funding_rate_history from a fixed page script."""

    def __init__(self, pages: list[list[int]]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[int, int]] = []

    def get_funding_rate_history(self, **params: Any) -> dict[str, Any]:
        self.calls.append((params["startTime"], params["endTime"]))
        rows = self.pages.pop(0) if self.pages else []
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "fundingRateTimestamp": str(ts),
                        "fundingRate": "0.0001",
                        "symbol": params["symbol"],
                    }
                    for ts in rows
                ]
            },
        }


def _market_data(pages: list[list[int]]) -> tuple[BybitMarketData, _ScriptedFunding]:
    md = BybitMarketData()
    fake = _ScriptedFunding(pages)
    md._client = fake
    return md, fake


def test_history_ending_exactly_on_a_page_boundary_completes() -> None:
    # Page 1 is full (limit=2) and stops above start; the next window is
    # legitimately empty because the symbol listed at ts=500. Confirmed empty
    # (three identical answers) is end-of-history, not a fault.
    md, fake = _market_data([[600, 500], [], [], []])
    rows = md.get_funding_history("NEWUSDT", start=0, end=1000, limit=2)
    assert [int(r["fundingRateTimestamp"]) for r in rows] == [500, 600]
    # One page, then the empty window asked three times in confirmation.
    assert fake.calls == [(0, 999), (0, 499), (0, 499), (0, 499)]


def test_transient_empty_page_is_retried_and_the_fetch_continues() -> None:
    md, _ = _market_data([[600, 500], [], [400]])
    rows = md.get_funding_history("NEWUSDT", start=0, end=1000, limit=2)
    assert [int(r["fundingRateTimestamp"]) for r in rows] == [400, 500, 600]
