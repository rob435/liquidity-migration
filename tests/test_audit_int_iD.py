"""Cross-file completion regression tests for audit bucket iD.

ingestion-1: an empty mid-pagination batch must NOT silently truncate a
``_paged_time_range`` fetch. The Bybit funding-rate / open-interest paths walk
``endTime`` backwards; if the provider returns an empty list *after* a full
page (a transient hiccup that returns ``[]`` instead of raising), the old code
``break``-ed and returned only the rows fetched before the hole. Downstream,
``_download_symbol_dataset`` sees ``frame.height > 0`` and writes the
FULL-requested-range completeness marker, producing a permanent silent gap in
funding / open_interest that ``_marked_complete`` never re-fetches. The fix
raises ``BybitDataError`` on a mid-range empty page so the symbol-range is
retried rather than marked complete, while a *first-page* empty (genuine
"no data in range") still returns cleanly.
"""

from __future__ import annotations

import pytest

from liquidity_migration import bybit


def _make_market_data(monkeypatch, responses_by_end_time):
    """Build a BybitMarketData whose FakeHTTP serves canned funding/OI pages.

    ``responses_by_end_time`` maps the per-request ``endTime`` (the backwards
    pagination cursor) to the ``result.list`` payload to return for that call.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def _serve(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            return {"retCode": 0, "result": {"list": responses_by_end_time[end_time]}}

        # Both funding-history and open-interest route through _paged_time_range.
        def get_funding_rate_history(self, **params):
            return self._serve(**params)

        def get_open_interest(self, **params):
            return self._serve(**params)

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    # retries=1 keeps the test fast and deterministic; the guard fires before
    # any retry/backoff because BybitDataError(non-rate-limit) raises immediately.
    return bybit.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)


def _funding_row(ts: int) -> dict[str, str]:
    return {"fundingRateTimestamp": str(ts), "fundingRate": "0.0001"}


def test_mid_range_empty_page_raises_instead_of_truncating(monkeypatch) -> None:
    """A full page followed by an empty page mid-range must raise, not truncate.

    limit=2, range [0, 10]. First request (endTime=10) returns a FULL page
    (ts 9, 8) so pagination continues with endTime = 8 - 1 = 7. The second
    request (endTime=7) returns [] -- a transient hole. Old behaviour: break and
    return only [8, 9]. New behaviour: raise BybitDataError so the fetch fails
    and the downloader retries rather than writing a full-range marker.
    """
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page -> keep paginating
        7: [],  # transient empty mid-range -> must NOT silently truncate
    }
    client = _make_market_data(monkeypatch, responses)

    with pytest.raises(bybit.BybitDataError) as excinfo:
        client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert "mid-range" in str(excinfo.value)
    # Both pages were actually requested before the guard fired.
    assert len(client._client.calls) == 2
    assert int(client._client.calls[1]["endTime"]) == 7


def test_first_page_empty_returns_cleanly_no_data_in_range(monkeypatch) -> None:
    """A genuinely empty range (first page empty) returns [] without raising."""
    responses = {10: []}
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert rows == []
    # Only one request was made; no spurious retry/extra pagination.
    assert len(client._client.calls) == 1


def test_full_then_short_page_completes_normally(monkeypatch) -> None:
    """The happy multi-page path is unchanged: a full page then a short
    (< limit) page terminates cleanly and returns the union, ascending by ts."""
    responses = {
        10: [_funding_row(9), _funding_row(8)],  # full page (len == limit)
        7: [_funding_row(5)],  # short page (len < limit) -> natural end of data
    }
    client = _make_market_data(monkeypatch, responses)

    rows = client.get_funding_history("BTCUSDT", start=0, end=10, limit=2)

    assert [int(r["fundingRateTimestamp"]) for r in rows] == [5, 8, 9]
    assert len(client._client.calls) == 2


def test_open_interest_mid_range_empty_also_guarded(monkeypatch) -> None:
    """open_interest shares _paged_time_range, so the same guard must apply.

    Its timestamp key is "timestamp"; reproduce the full-then-empty hole.
    """

    class FakeHTTP:
        def __init__(self, *, testnet: bool):
            self.testnet = testnet
            self.calls: list[dict] = []

        def get_open_interest(self, **params):
            self.calls.append(params)
            end_time = int(params["endTime"])
            pages = {
                10: [
                    {"timestamp": "9", "openInterest": "1"},
                    {"timestamp": "8", "openInterest": "2"},
                ],
                7: [],  # transient mid-range empty
            }
            return {"retCode": 0, "result": {"list": pages[end_time]}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    client = bybit.BybitMarketData(testnet=True, retries=1, retry_sleep_seconds=0.0)

    with pytest.raises(bybit.BybitDataError):
        client.get_open_interest("BTCUSDT", "5min", start=0, end=10, limit=2)
