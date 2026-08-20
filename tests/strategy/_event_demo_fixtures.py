"""Public market-data fakes shared by the extracted data-plane tests."""

from __future__ import annotations


from liquidity_migration.core._common import MS_PER_HOUR


class FakeKlineMarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def get_klines(
        self, symbol: str, interval: str, start: int, end: int
    ) -> list[list[str]]:
        self.calls.append((symbol, interval, start, end))
        # ``end`` is EXCLUSIVE, matching BybitMarketData.get_klines — an
        # inclusive fake here would hide a caller that drops the tail bar.
        return [
            [str(ts_ms), "100", "110", "90", "105", "1.5", "157.5"]
            for ts_ms in range(start, end, MS_PER_HOUR)
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


