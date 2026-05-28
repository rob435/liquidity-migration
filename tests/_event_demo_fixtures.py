"""Shared fakes + helpers for the event_demo test modules.

Extracted from the monolithic test_liquidity_migration_event_demo.py so the
themed test files (…_data, …_entries, …_exits, …) can share one copy.
Imported by basename (`from _event_demo_fixtures import *`); pytest puts
the tests/ dir on sys.path during collection.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR


class FakeRiskClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, str]] | None = None,
        fill_market_orders: bool = False,
        fill_order_prefixes: tuple[str, ...] = ("lm-lm-",),
        fill_qty: str = "1",
        fill_price: str = "100.5",
        fail_leverage_symbols: set[str] | None = None,
        fail_order_symbols: set[str] | None = None,
        fail_history_links: set[str] | None = None,
        fail_trade_history: bool = False,
        fail_positions: bool = False,
        fail_wallet: bool = False,
        open_orders: list[dict[str, object]] | None = None,
        fail_open_orders: bool = False,
    ) -> None:
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.fill_market_orders = fill_market_orders
        self.fill_order_prefixes = fill_order_prefixes
        self.fill_qty = fill_qty
        self.fill_price = fill_price
        self.fail_leverage_symbols = fail_leverage_symbols or set()
        self.fail_order_symbols = fail_order_symbols or set()
        self.fail_history_links = fail_history_links or set()
        self.fail_trade_history = fail_trade_history
        self.fail_positions = fail_positions
        self.fail_wallet = fail_wallet
        self.fail_open_orders = fail_open_orders
        self.orders: list[dict[str, object]] = []
        self.stop_updates: list[dict[str, object]] = []
        self.leverage_updates: list[dict[str, object]] = []
        self.trade_history_calls: list[str | None] = []

    def get_positions(self, *, settle_coin: str | None = None) -> list[dict[str, str]]:
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return self.positions

    def get_wallet_balance(self, *, account_type: str | None = None, coin: str | None = None) -> dict[str, object]:
        if self.fail_wallet:
            raise RuntimeError("wallet unavailable")
        return {"list": [{"totalEquity": "10000"}]}

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = None) -> list[dict[str, object]]:
        if self.fail_open_orders:
            raise RuntimeError("open orders unavailable")
        if symbol:
            return [row for row in self.open_orders if str(row.get("symbol") or "") == symbol]
        return self.open_orders

    def place_order(self, **params: object) -> dict[str, str]:
        if str(params.get("symbol")) in self.fail_order_symbols:
            raise RuntimeError("order rejected")
        self.orders.append(params)
        return {"orderId": f"order-{len(self.orders)}"}

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50) -> list[dict[str, str]]:
        self.trade_history_calls.append(order_link_id)
        if self.fail_trade_history:
            raise RuntimeError("history unavailable")
        if order_link_id in self.fail_history_links:
            raise RuntimeError("history unavailable")
        if self.fill_market_orders and order_link_id and order_link_id.startswith(self.fill_order_prefixes):
            return [
                {
                    "execQty": self.fill_qty,
                    "execPrice": self.fill_price,
                    "execValue": str(float(self.fill_qty) * float(self.fill_price)),
                    "execFee": "0.06",
                }
            ]
        return []

    def set_trading_stop(self, **params: object) -> dict[str, str]:
        self.stop_updates.append(params)
        return {}

    def set_leverage(self, **params: object) -> dict[str, str]:
        if str(params.get("symbol")) in self.fail_leverage_symbols:
            raise RuntimeError("leverage rejected")
        self.leverage_updates.append(params)
        return {}


class FakeKlineMarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list[list[str]]:
        self.calls.append((symbol, interval, start, end))
        return [
            [
                str(ts_ms),
                "100",
                "110",
                "90",
                "105",
                "1.5",
                "157.5",
            ]
            for ts_ms in range(start, end + 1, MS_PER_HOUR)
        ]


class FailingKlineMarket(FakeKlineMarket):
    def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list[list[str]]:
        raise AssertionError(f"unexpected kline fetch for {symbol} {interval} {start} {end}")


class MinimalEventMarket:
    def get_instruments_info(self) -> list[dict[str, str]]:
        return []

    def get_tickers(self) -> list[dict[str, str]]:
        return [{"symbol": "AAAUSDT", "markPrice": "100", "lastPrice": "100"}]

    def stats(self) -> dict[str, int]:
        return {}


def _patch_minimal_event_cycle(monkeypatch: pytest.MonkeyPatch, candidate: dict[str, object]) -> None:
    monkeypatch.setattr(
        "liquidity_migration.event_demo._build_demo_universe",
        lambda *args, **kwargs: pl.DataFrame(
            [
                {
                    "symbol": "AAAUSDT",
                    "tick_size": 0.1,
                    "qty_step": 0.1,
                    "min_order_qty": 0.1,
                    "min_notional_value": 5.0,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "liquidity_migration.event_demo._download_recent_1h_klines",
        lambda *args, **kwargs: (
            pl.DataFrame(
                [
                    {
                        "symbol": "AAAUSDT",
                        "ts_ms": 1_700_000_000_000,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.0,
                    }
                ]
            ),
            {"cache_rows": 0, "cache_symbols": 0, "fetch_symbols": 0, "fetched_rows": 0, "output_rows": 1},
        ),
    )
    monkeypatch.setattr(
        "liquidity_migration.event_demo._build_demo_features",
        lambda klines, universe=None, **kwargs: pl.DataFrame([{"symbol": "AAAUSDT"}]),
    )
    monkeypatch.setattr(
        "liquidity_migration.event_demo.select_demo_entry_candidates",
        lambda *args, **kwargs: ([candidate], {}),
    )


def _feature_cache_klines(symbols: int = 4, days: int = 25) -> pl.DataFrame:
    rows = []
    for s in range(symbols):
        base = 10.0 * (s + 1)
        for bar in range(days * 24):
            ts = bar * MS_PER_HOUR
            px = base * (1.0 + 0.0003 * bar) + (bar % 7) * 0.01
            rows.append(
                {
                    "ts_ms": ts,
                    "symbol": f"SYM{s:02d}USDT",
                    "open": px,
                    "high": px * 1.01,
                    "low": px * 0.99,
                    "close": px,
                    "volume_base": 1_000.0 + bar,
                    "turnover_quote": (1_000.0 + bar) * px,
                    "source": "synthetic",
                }
            )
    return pl.DataFrame(rows)


def _feature_cache_universe(symbols: int = 4) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [f"SYM{s:02d}USDT" for s in range(symbols)],
            "listing_age_days": [120 + s * 30 for s in range(symbols)],
        }
    )


class _RecordingInstrumentsMarket:
    """Public market client that counts get_instruments_info calls so tests can
    prove the TTL cache suppresses repeat fetches."""

    def __init__(self) -> None:
        self.instrument_calls = 0

    def get_instruments_info(self) -> list[dict[str, str]]:
        self.instrument_calls += 1
        return [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}]


def _make_instruments_frame() -> pl.DataFrame:
    """Synthetic Bybit instruments-info frame covering the schema the
    `build_current_universe_table` filter expects. Three USDT-perps, three
    ages, one excluded (BUSDUSDT)."""
    import polars as pl
    return pl.DataFrame({
        "symbol": ["BTCUSDT", "BANUSDT", "NEWUSDT", "BUSDUSDT"],
        "status": ["Trading", "Trading", "Trading", "Trading"],
        "settle_coin": ["USDT", "USDT", "USDT", "USDT"],
        "is_prelisting": [False, False, False, False],
        "contract_type": ["LinearPerpetual"] * 4,
        "launch_time_ms": [
            1_500_000_000_000,
            1_731_919_190_000,  # 2024-11-18 - BAN's real launchTime
            1_779_000_000_000,  # ~5 days before our snapshot
            1_500_000_000_000,
        ],
        # Lot-size / tick-size / etc. columns the universe table propagates.
        "min_order_qty": [0.001, 0.1, 0.1, 1.0],
        "max_order_qty": [100.0, 1000.0, 1000.0, 100.0],
        "max_market_order_qty": [100.0, 1000.0, 1000.0, 100.0],
        "tick_size": [0.1, 0.001, 0.001, 0.01],
        "qty_step": [0.001, 0.1, 0.1, 1.0],
        "min_notional_value": [5.0, 5.0, 5.0, 5.0],
    })


def _make_tickers_frame() -> pl.DataFrame:
    """Synthetic tickers with 24h turnover values. NEWUSDT has lower
    turnover to verify the legacy narrow universe drops it while
    match-the-backtest mode keeps it."""
    import polars as pl
    return pl.DataFrame({
        "symbol": ["BTCUSDT", "BANUSDT", "NEWUSDT", "BUSDUSDT"],
        "turnover_24h": [3.0e9, 2.0e7, 1.0e5, 5.0e6],
        "volume_24h": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "open_interest": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "open_interest_value": [1.0e6, 1.0e4, 1.0e2, 1.0e3],
        "funding_rate": [0.0001] * 4,
    })


def _open_trade_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "status": "open",
        "qty": "1",
        "entry_price": 100.0,
        "entry_ts_ms": 1_700_000_000_000,
        "notional_usdt": 1_000.0,
        "equity_usdt": 10_000.0,
    }
    row.update(overrides)
    return row


class _ClosedPnlClient:
    """Minimal stub exposing only the closed-PnL endpoint the backfill uses."""

    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        raise_on_call: bool = False,
    ) -> None:
        self.records = records or []
        self.raise_on_call = raise_on_call
        self.calls: list[dict[str, Any]] = []

    def get_closed_pnl(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.raise_on_call:
            raise RuntimeError("closed-pnl unavailable")
        symbol = kwargs.get("symbol")
        if symbol:
            return [r for r in self.records if str(r.get("symbol")) == symbol]
        return list(self.records)

