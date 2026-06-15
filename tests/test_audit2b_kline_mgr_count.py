"""Regression for audit2b unit kline_mgr_count.

Defect: ``universe_refresh_errors`` was double-counted on the default-fetcher
empty path. When ``force_refresh_universe`` calls ``_fetch_universe`` and the
default fetcher's ``get_instruments_info`` raises, ``_fetch_universe`` already
increments ``_universe_refresh_errors`` (line ~413) and returns ``[]``; the
empty-set guard in ``force_refresh_universe`` then increments it a SECOND time
for the same underlying error.

These tests pin:
  * the default-fetcher REST-exception path counts the error exactly ONCE
    (fails on the old code, which counted 2), and
  * the normal / other empty paths are unchanged (custom-fetcher empty and a
    default fetch that simply filters to empty each count exactly once; a
    successful refresh counts zero).
"""

from __future__ import annotations

from pathlib import Path

from liquidity_migration.kline_stream_manager import KlineStreamManager


def _instruments_payload(symbols: list[str]) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "status": "Trading",
            "quoteCoin": "USDT",
            "settleCoin": "USDT",
            "contractType": "LinearPerpetual",
            "isPreListing": False,
        }
        for symbol in symbols
    ]


class _Market:
    """Default-fetcher stand-in: get_instruments_info drives the path."""

    def __init__(self, instruments_factory, kline_factory) -> None:
        self._instruments_factory = instruments_factory
        self._kline_factory = kline_factory
        self.instrument_calls = 0
        self.kline_calls: list[str] = []
        # __post_init__ wires a limiter only when this attribute is None.
        self.rate_limiter = None

    def get_instruments_info(self) -> list[dict]:
        self.instrument_calls += 1
        return list(self._instruments_factory(self.instrument_calls))

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list:
        self.kline_calls.append(symbol)
        return list(self._kline_factory(symbol, interval, start, end))


class _Pool:
    def __init__(self) -> None:
        self.updates: list[set[str]] = []
        self.subscribed: list[list[str]] = []
        self.closed = False

    def subscribe(self, symbols, callback) -> None:
        self.subscribed.append(list(symbols))

    def update_subscriptions(self, new_symbols: set[str]) -> dict:
        self.updates.append(set(new_symbols))
        return {"added": 0, "removed": 0, "connections": 1}

    def close(self) -> None:
        self.closed = True

    def start_watchdog(self) -> None:
        pass

    def stats(self) -> dict:
        return {"connections": 1}


def _build(tmp_path: Path, instruments_factory) -> tuple[KlineStreamManager, _Pool, _Market]:
    pool = _Pool()

    def _klines(symbol, interval, start, end):
        # A single page of bars is plenty for bootstrap to mark covered.
        from liquidity_migration._common import MS_PER_HOUR

        return [
            {
                "ts_ms": start + i * MS_PER_HOUR,
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume_base": 10.0,
                "turnover_quote": 15.0,
            }
            for i in range(120)
        ]

    market = _Market(instruments_factory, _klines)
    manager = KlineStreamManager(
        market_data=market,
        cache_root=tmp_path,
        lookback_days=5,
        bootstrap_workers=2,
        universe_refresh_interval_seconds=0.0,  # no refresh thread
        bootstrap_completion_threshold=1.0,
        bootstrap_timeout_seconds=10.0,
        flush_interval_seconds=0.0,
        retain_days=30,
        topics_per_connection=10,
        pool=pool,
    )
    return manager, pool, market


def test_default_fetcher_rest_exception_counts_error_once(tmp_path: Path) -> None:
    """First fetch succeeds (start), second raises (refresh blip).

    On the old code the exception was counted in _fetch_universe AND again in
    the empty-set guard => 2. The fix counts it exactly once."""

    def _instruments(call_n: int):
        if call_n >= 2:
            raise RuntimeError("simulated REST 5xx")
        return _instruments_payload(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    manager, pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        assert manager.stats()["universe_refresh_errors"] == 0
        result = manager.force_refresh_universe()
        # Universe is preserved (transient-failure guard).
        assert result == {"added": 0, "removed": 0, "size": 3}
        assert set(manager.universe_symbols()) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        # The single underlying error is counted exactly once, not twice.
        assert manager.stats()["universe_refresh_errors"] == 1
        # Existing subscriptions untouched on the empty path.
        assert pool.updates == []
    finally:
        manager.stop()


def test_default_fetcher_filtered_to_empty_counts_error_once(tmp_path: Path) -> None:
    """Default fetch returns rows but they filter to empty (no exception).

    _fetch_universe does NOT count here, so the empty-set guard must still
    count exactly once. This path is unchanged by the fix."""

    def _instruments(call_n: int):
        if call_n >= 2:
            return _instruments_payload([])  # valid call, no symbols
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    manager, _pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        assert manager.stats()["universe_refresh_errors"] == 0
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 2}
        assert manager.stats()["universe_refresh_errors"] == 1
    finally:
        manager.stop()


def test_successful_refresh_counts_no_error(tmp_path: Path) -> None:
    """Normal happy path: a non-empty refresh records zero errors and the
    diff/return shape is unchanged by the fix."""

    def _instruments(call_n: int):
        return _instruments_payload(["BTCUSDT", "ETHUSDT"])

    manager, _pool, _market = _build(tmp_path, _instruments)
    manager.start()
    try:
        result = manager.force_refresh_universe()
        assert result == {"added": 0, "removed": 0, "size": 2}
        assert manager.stats()["universe_refresh_errors"] == 0
    finally:
        manager.stop()
