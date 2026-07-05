from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ._common import exact_duration_ms


class BinanceDataError(RuntimeError):
    pass


BINANCE_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(slots=True)
class BinanceUSDMData:
    base_url: str = "https://fapi.binance.com"
    retries: int = 3
    retry_sleep_seconds: float = 0.75
    timeout_seconds: float = 15.0
    # Fallback wait on a 429/418 with no usable Retry-After header, and the hard
    # cap on any Retry-After value (server-specified windows can be tens of
    # seconds to minutes; a capped wait keeps an offline build from stalling
    # indefinitely on a hostile/huge header). See ratelimit-rest-6.
    rate_limit_backoff_seconds: float = 30.0
    max_retry_after_seconds: float = 120.0
    calls: int = field(init=False, default=0)
    retry_events: int = field(init=False, default=0)
    error_events: int = field(init=False, default=0)
    last_error: str = field(init=False, default="")

    def get_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/klines", symbol, interval, start, end, limit=limit)

    def get_mark_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/markPriceKlines", symbol, interval, start, end, limit=limit)

    def get_index_price_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/indexPriceKlines", symbol, interval, start, end, limit=limit, pair_param=True)

    def get_premium_index_klines(self, symbol: str, interval: str, start: int, end: int, limit: int = 1500) -> list[list[Any]]:
        return self._paged_kline("/fapi/v1/premiumIndexKlines", symbol, interval, start, end, limit=limit)

    def get_funding_history(self, symbol: str, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        return self._paged_forward(
            "/fapi/v1/fundingRate",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 1000),
            timestamp_key="fundingTime",
            extra_params={},
        )

    def get_open_interest_hist(self, symbol: str, period: str, start: int, end: int, limit: int = 500) -> list[dict[str, Any]]:
        start = _recent_history_start(start, end, days=30)
        start = _ceil_to_period(start, period)
        end = _floor_to_period(end, period)
        if start > end:
            return []
        return self._paged_forward(
            "/futures/data/openInterestHist",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 500),
            timestamp_key="timestamp",
            extra_params={"period": period},
            step_ms=BINANCE_INTERVAL_MS[period],
        )

    def get_taker_buy_sell_volume(self, symbol: str, period: str, start: int, end: int, limit: int = 500) -> list[dict[str, Any]]:
        start = _recent_history_start(start, end, days=30)
        start = _ceil_to_period(start, period)
        end = _floor_to_period(end, period)
        if start > end:
            return []
        return self._paged_forward(
            "/futures/data/takerlongshortRatio",
            symbol=symbol,
            start=start,
            end=end,
            limit=min(limit, 500),
            timestamp_key="timestamp",
            extra_params={"period": period},
            step_ms=BINANCE_INTERVAL_MS[period],
        )

    def _paged_kline(
        self,
        path: str,
        symbol: str,
        interval: str,
        start: int,
        end: int,
        *,
        limit: int,
        pair_param: bool = False,
    ) -> list[list[Any]]:
        page_limit = min(limit, 1500)
        interval_ms = BINANCE_INTERVAL_MS[interval]
        rows_by_ts: dict[int, list[Any]] = {}
        cursor = start
        prev_page_full = False
        while cursor <= end:
            params: dict[str, Any] = {
                "interval": interval,
                "startTime": cursor,
                "endTime": end,
                "limit": page_limit,
            }
            params["pair" if pair_param else "symbol"] = symbol
            batch = self._get(path, params)
            if not isinstance(batch, list):
                raise BinanceDataError(f"Binance {path} returned non-list payload")
            if not batch:
                # Distinguish a benign terminal empty page from a SUSPICIOUS
                # mid-range empty page: the provider returns [] (a rate-limit /
                # load hiccup that does not raise) right after a FULL page while
                # the cursor is still more than one interval short of end_ms.
                # That silently truncates the fetch; the downloader then writes a
                # full-range completeness marker over a permanent gap that is
                # never re-fetched. Raise a transient error so the symbol is
                # retried / recorded failed instead of marked complete. A full
                # prior page is the unambiguous "there is more data" signal, so
                # this never false-positives on a genuinely sparse/short symbol
                # whose last page was partial. (ingestion-1)
                _raise_if_suspicious_empty_page(
                    path, symbol, cursor=cursor, end=end, step_ms=interval_ms, prev_page_full=prev_page_full
                )
                break
            for row in batch:
                ts = int(row[0])
                if start <= ts <= end:
                    rows_by_ts[ts] = row
            prev_page_full = len(batch) >= page_limit
            latest = max(int(row[0]) for row in batch)
            next_cursor = latest + interval_ms
            if next_cursor <= cursor or next_cursor > end:
                break
            cursor = next_cursor
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _paged_forward(
        self,
        path: str,
        *,
        symbol: str,
        start: int,
        end: int,
        limit: int,
        timestamp_key: str,
        extra_params: dict[str, Any],
        step_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        rows_by_ts: dict[int, dict[str, Any]] = {}
        cursor = start
        prev_page_full = False
        while cursor <= end:
            params = {
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end,
                "limit": limit,
                **extra_params,
            }
            batch = self._get(path, params)
            if not isinstance(batch, list):
                raise BinanceDataError(f"Binance {path} returned non-list payload")
            if not batch:
                # See _paged_kline: a mid-range empty page after a FULL page is a
                # silent-truncation signal, not end-of-data. Raise so the funding
                # / open-interest / taker fetch is retried rather than the gap
                # being marked complete. step_ms may be None (e.g. funding, whose
                # cadence is irregular); the prev-page-full guard alone still
                # distinguishes truncation from a genuine terminal empty page.
                _raise_if_suspicious_empty_page(
                    path, symbol, cursor=cursor, end=end, step_ms=step_ms, prev_page_full=prev_page_full
                )
                break
            for row in batch:
                ts = int(row[timestamp_key])
                if start <= ts <= end:
                    rows_by_ts[ts] = row
            prev_page_full = len(batch) >= limit
            latest = max(int(row[timestamp_key]) for row in batch)
            next_cursor = latest + (step_ms or 1)
            if len(batch) < limit or next_cursor <= cursor or next_cursor > end:
                break
            cursor = next_cursor
        return [rows_by_ts[ts] for ts in sorted(rows_by_ts)]

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        url = f"{self.base_url}{path}?{urlencode(params)}"
        for attempt in range(self.retries):
            try:
                self.calls += 1
                request = Request(url, headers={"User-Agent": "liquidity-migration-data-layer/1.0"})
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and "code" in payload and int(payload.get("code") or 0) < 0:
                    raise BinanceDataError(f"Binance {path} failed: {payload}")
                return payload
            except Exception as exc:  # noqa: BLE001 - urllib raises transport-specific errors
                if isinstance(exc, BinanceDataError):
                    self.error_events += 1
                    self.last_error = str(exc)[:500]
                    raise
                self.error_events += 1
                self.last_error = str(exc)[:500]
                if isinstance(exc, HTTPError):
                    # Permanent client errors (e.g. -1121 invalid symbol, -1100
                    # illegal chars) arrive as HTTP 4xx, not as an HTTP-200 JSON
                    # body, so the code<0 check above never sees them. Retrying
                    # burns the whole budget + backoff sleeps on a request that
                    # can never succeed. Fail fast with the parsed Binance error
                    # code, mirroring the 200-path handling. 429 (rate limit) and
                    # 418 (IP ban) are the exception: they ARE transient, so we
                    # honor Retry-After and keep retrying. (ingestion-5)
                    if exc.code not in (418, 429) and 400 <= exc.code < 500:
                        raise BinanceDataError(
                            f"Binance {path} failed with HTTP {exc.code}: {_http_error_detail(exc)}"
                        ) from exc
                last_error = exc
                if attempt + 1 >= self.retries:
                    break
                self.retry_events += 1
                # On 429/418 the server tells us how long to wait; honor it
                # (capped) instead of burning the fixed exponential backoff in a
                # couple of seconds and then dropping the symbol. (ratelimit-rest-6)
                backoff = self.retry_sleep_seconds * (2**attempt)
                if isinstance(exc, HTTPError) and exc.code in (418, 429):
                    backoff = max(backoff, self._retry_after_seconds(exc))
                time.sleep(backoff)
        raise BinanceDataError(f"Binance {path} failed after retries") from last_error

    def _retry_after_seconds(self, exc: HTTPError) -> float:
        """Seconds to wait after a 429/418, from the Retry-After header.

        Falls back to a capped backoff when the header is missing or unparseable,
        and always clamps to ``max_retry_after_seconds`` so a hostile/huge value
        cannot stall an offline build indefinitely.
        """
        raw = None
        try:
            raw = exc.headers.get("Retry-After") if exc.headers is not None else None
        except Exception:  # noqa: BLE001 - header access must never crash the retry path
            raw = None
        seconds = self.rate_limit_backoff_seconds
        if raw is not None:
            try:
                seconds = float(str(raw).strip())
            except (TypeError, ValueError):
                seconds = self.rate_limit_backoff_seconds
        return max(0.0, min(seconds, self.max_retry_after_seconds))

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "retry_events": self.retry_events,
            "error_events": self.error_events,
            "last_error": self.last_error,
        }


def _http_error_detail(exc: HTTPError) -> str:
    """Best-effort detail string from an HTTPError body (the Binance error JSON).

    Binance returns ``{"code": -1121, "msg": "Invalid symbol."}`` in the 4xx body;
    surfacing it makes the fast-fail error actionable. Reading the body must never
    itself raise (the connection may already be closed).
    """
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - body may be unreadable; fall back to reason
        body = ""
    detail = body.strip() or (exc.reason if isinstance(exc.reason, str) else str(exc.reason))
    return str(detail)[:500]


def _raise_if_suspicious_empty_page(
    path: str,
    symbol: str,
    *,
    cursor: int,
    end: int,
    step_ms: int | None,
    prev_page_full: bool,
) -> None:
    """Raise on a mid-range empty page that indicates silent truncation.

    A page that comes back ``[]`` is only a benign end-of-range terminator when
    the previous page was NOT full. A full previous page means the provider
    capped the response at the page limit and there is definitely more data
    after it; a subsequent empty page is then a transient hiccup (the provider
    returned ``[]`` instead of raising), which would otherwise truncate the
    fetch and let the downloader mark a permanent coverage gap as complete.

    When ``step_ms`` is known we additionally require the cursor to be more than
    one step short of ``end`` (a full page whose successor genuinely lands at the
    end of the range is legitimate). When ``step_ms`` is None (irregular cadence,
    e.g. funding) the full-prior-page signal alone is used.
    """
    if not prev_page_full:
        return
    if step_ms is not None and cursor > end - step_ms:
        # The next bar would land beyond the requested end anyway — nothing was
        # truncated.
        return
    raise BinanceDataError(
        f"Binance {path} returned an empty page for {symbol} at startTime={cursor} "
        f"(end={end}) immediately after a full page: suspected transient truncation, "
        "refusing to mark an incomplete range complete."
    )


def _recent_history_start(start: int, end: int, *, days: int) -> int:
    window_ms = exact_duration_ms(days=days)
    latest_available_start = int(time.time() * 1000) - window_ms
    return max(start, end - window_ms + 1, latest_available_start)


def _ceil_to_period(value: int, period: str) -> int:
    step = BINANCE_INTERVAL_MS[period]
    return value if value % step == 0 else ((value // step) + 1) * step


def _floor_to_period(value: int, period: str) -> int:
    step = BINANCE_INTERVAL_MS[period]
    return (value // step) * step
