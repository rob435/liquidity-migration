"""Public market-data fakes shared by the extracted data-plane tests."""

from __future__ import annotations

import polars as pl

from liquidity_migration._common import MS_PER_HOUR


class FakeKlineMarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def get_klines(
        self, symbol: str, interval: str, start: int, end: int
    ) -> list[list[str]]:
        self.calls.append((symbol, interval, start, end))
        return [
            [str(ts_ms), "100", "110", "90", "105", "1.5", "157.5"]
            for ts_ms in range(start, end + 1, MS_PER_HOUR)
        ]


class FailingKlineMarket(FakeKlineMarket):
    def get_klines(
        self, symbol: str, interval: str, start: int, end: int
    ) -> list[list[str]]:
        raise AssertionError(
            f"unexpected kline fetch for {symbol} {interval} {start} {end}"
        )


class _RecordingInstrumentsMarket:
    def __init__(self) -> None:
        self.instrument_calls = 0

    def get_instruments_info(self) -> list[dict[str, str]]:
        self.instrument_calls += 1
        return [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}]


def _make_instruments_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["BTCUSDT", "BANUSDT", "NEWUSDT", "BUSDUSDT"],
        "status": ["Trading"] * 4,
        "settle_coin": ["USDT"] * 4,
        "is_prelisting": [False] * 4,
        "contract_type": ["LinearPerpetual"] * 4,
        "launch_time_ms": [
            1_500_000_000_000,
            1_731_919_190_000,
            1_779_000_000_000,
            1_500_000_000_000,
        ],
        "min_order_qty": [0.001, 0.1, 0.1, 1.0],
        "max_order_qty": [100.0, 1000.0, 1000.0, 100.0],
        "max_market_order_qty": [100.0, 1000.0, 1000.0, 100.0],
        "tick_size": [0.1, 0.001, 0.001, 0.01],
        "qty_step": [0.001, 0.1, 0.1, 1.0],
        "min_notional_value": [5.0] * 4,
    })


def _make_tickers_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["BTCUSDT", "BANUSDT", "NEWUSDT", "BUSDUSDT"],
        "turnover_24h": [3.0e9, 2.0e7, 1.0e5, 5.0e6],
        "volume_24h": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "open_interest": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "open_interest_value": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "funding_rate": [0.0001] * 4,
    })
