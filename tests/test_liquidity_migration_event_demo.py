from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from liquidity_migration.cli import build_parser
from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo import (
    DEMO_RELAXED_STRATEGY_ID,
    DEMO_STRATEGY_PROFILES,
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    PENDING_ORDER_GUARD_MS,
    _build_demo_features,
    _collect_private_snapshots,
    _demo_event_config,
    _demo_feature_cache_fingerprint,
    _demo_feature_cache_paths,
    _demo_instruments,
    _demo_kline_fetch_ranges,
    _execute_entries,
    _execute_exits,
    _execute_risk_exits,
    _filter_live_open_exit_orders,
    _download_recent_1h_klines,
    _limit_chase_price,
    _live_open_order_symbols,
    _maybe_notify,
    _prune_cycle_reports,
    _reconcile_pending_order_fills,
    decode_entry_order_link_id,
    _required_universe_rank_end,
    _refresh_positions_and_orders,
    _submit_reduce_only_exit,
    _telegram_notification_reason,
    _terminalize_stale_pending_entry_orders,
    _validate_demo_config,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_telegram_status_message,
    order_quantity_for_notional,
    plan_demo_exits,
    plan_risk_exits,
    plan_stop_repairs,
    run_event_demo_cycle,
    run_event_risk_cycle,
    select_demo_entry_candidates,
    summarize_position_pnl,
    target_initial_margin_pct_equity,
    target_order_notional_pct_equity,
    wallet_equity_usdt,
    warm_demo_kline_cache,
)
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.volume_events import EventScenario, VolumeEventResearchConfig


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


def test_event_demo_cli_defaults_to_frequent_demo_forward_cycle() -> None:
    args = build_parser().parse_args(["event-demo-cycle"])

    assert args.command == "event-demo-cycle"
    assert args.lookback_days == 45
    assert args.universe_rank_end == 400
    assert args.universe_max_symbols == 400
    assert args.universe_min_turnover_24h == 0.0
    assert args.max_order_notional_pct_equity == 0.0
    assert args.max_entry_lag_minutes == 360
    assert args.max_new_entries_per_cycle == 5
    assert args.entry_leverage == 2.0
    assert args.order_fill_confirm_seconds == 2.0
    assert args.order_fill_poll_interval_seconds == 0.2
    assert args.submit_orders is False
    assert args.confirm_demo_orders is False


def test_event_risk_cli_defaults_to_fast_market_watchdog() -> None:
    args = build_parser().parse_args(["event-risk-cycle"])

    assert args.command == "event-risk-cycle"
    assert args.exit_order_mode == "market"
    assert args.limit_chase_attempts == 3
    assert args.limit_chase_wait_seconds == 0.15
    assert args.stop_tolerance_bps == 1.0
    assert args.loop is False
    assert args.quiet_loop is False
    assert args.interval_seconds == 0.25
    assert args.max_cycles == 0
    assert args.submit_orders is False
    assert args.confirm_demo_orders is False


def test_event_ws_risk_cli_defaults_to_ws_then_rest_demo_path() -> None:
    args = build_parser().parse_args(["event-risk-ws"])

    assert args.command == "event-risk-ws"
    assert args.order_submit_mode == "ws_then_rest"
    assert args.no_rest_fallback is False
    assert args.rest_reconcile_seconds == 30.0
    assert args.heartbeat_seconds == 10.0
    assert args.pending_exit_guard_seconds == 120.0
    assert args.exit_untracked_positions is False
    assert args.adopt_untracked_positions is True
    assert args.adopt_stop_loss_pct == 0.12
    assert args.adopt_take_profit_pct == 0.21
    assert args.adopt_hold_days == 3.0
    assert args.fast_execution_stream is False
    assert args.submit_orders is False
    assert args.confirm_demo_orders is False


def test_event_demo_default_sizing_matches_backtest_weight() -> None:
    assert target_order_notional_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == pytest.approx(1.0 / 5.0)
    assert target_initial_margin_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == pytest.approx(
        1.0 / 5.0 / 2.0
    )
    assert (
        target_order_notional_pct_equity(
            EventDemoCycleConfig(max_order_notional_pct_equity=0.10),
            VolumeEventResearchConfig(),
        )
        == 0.10
    )


def test_demo_kline_cache_avoids_refetching_complete_window(tmp_path: Path) -> None:
    market = FakeKlineMarket()

    first, first_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )

    assert first.height == 6
    assert first_stats["fetch_symbols"] == 2
    assert first_stats["fetched_rows"] == 6
    assert market.calls == [
        ("AAAUSDT", "60", 0, 2 * MS_PER_HOUR),
        ("BBBUSDT", "60", 0, 2 * MS_PER_HOUR),
    ]
    cached = read_dataset(tmp_path, "event_demo_klines_1h")
    assert cached.height == 6
    assert read_dataset(tmp_path, "klines_1h").is_empty()

    second, second_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )

    assert second.height == 6
    assert second_stats["cache_rows"] == 6
    assert second_stats["cache_symbols"] == 2
    assert second_stats["fetch_symbols"] == 0
    assert second_stats["fetched_rows"] == 0


def test_demo_kline_cache_fetches_only_new_hour(tmp_path: Path) -> None:
    market = FakeKlineMarket()

    initial, _ = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )
    assert initial.height == 2

    market.calls.clear()
    updated, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )

    assert market.calls == [("AAAUSDT", "60", 2 * MS_PER_HOUR, 2 * MS_PER_HOUR)]
    assert updated.height == 3
    assert stats["cache_rows"] == 2
    assert stats["fetch_symbols"] == 1
    assert stats["fetched_rows"] == 1
    assert read_dataset(tmp_path, "event_demo_klines_1h").height == 3


def test_demo_kline_fetch_ranges_uses_latest_bar_per_symbol() -> None:
    cached = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": 0},
            {"symbol": "AAAUSDT", "ts_ms": 2 * MS_PER_HOUR},
            {"symbol": "BBBUSDT", "ts_ms": 0},
            {"symbol": "CCCUSDT", "ts_ms": 3 * MS_PER_HOUR},
        ]
    )

    ranges = _demo_kline_fetch_ranges(
        ["AAAUSDT", "BBBUSDT", "DDDUSDT"],
        cached,
        start_ms=0,
        end_ms=3 * MS_PER_HOUR,
    )

    assert ranges == {
        "AAAUSDT": (3 * MS_PER_HOUR, 3 * MS_PER_HOUR),
        "BBBUSDT": (MS_PER_HOUR, 3 * MS_PER_HOUR),
        "DDDUSDT": (0, 3 * MS_PER_HOUR),
    }


def test_demo_kline_compact_cache_serves_repeat_window(tmp_path: Path) -> None:
    cached_rows = []
    for symbol in ("AAAUSDT", "BBBUSDT"):
        for ts_ms in (0, MS_PER_HOUR, 2 * MS_PER_HOUR):
            cached_rows.append(
                {
                    "symbol": symbol,
                    "ts_ms": ts_ms,
                    "open": 100.0,
                    "high": 110.0,
                    "low": 90.0,
                    "close": 105.0,
                    "volume": 1.5,
                    "turnover": 157.5,
                }
            )
    write_dataset(pl.DataFrame(cached_rows), tmp_path, "event_demo_klines_1h")

    first, first_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )
    shutil.rmtree(tmp_path / "event_demo_klines_1h")

    second, second_stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
    )

    assert first.height == 6
    assert first_stats["fetch_symbols"] == 0
    assert second.height == 6
    assert second_stats["cache_rows"] == 6
    assert second_stats["fetch_symbols"] == 0


def test_download_recent_1h_klines_uses_store_fast_path(tmp_path: Path) -> None:
    """With a fully-covering kline_store, REST is never called and the output
    is sourced entirely from the store."""
    from liquidity_migration.kline_store import KlineStore

    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        ts = hour * MS_PER_HOUR
        for symbol in ("AAAUSDT", "BBBUSDT"):
            store.add_bar(
                symbol,
                {
                    "start": ts,
                    "open": "100", "high": "110", "low": "90", "close": "105",
                    "volume": "1.5", "turnover": "157.5",
                },
                confirmed=True,
            )

    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),  # REST must NOT be called
        cache_root=tmp_path,
        kline_store=store,
    )
    assert output.height == 6
    assert stats["store_rows"] == 6
    assert stats["store_symbols"] == 2
    assert stats["fetch_symbols"] == 0
    assert stats["fetched_rows"] == 0


def test_download_recent_1h_klines_store_full_coverage_skips_disk_cache(tmp_path: Path) -> None:
    """When the WS store fully covers the universe at end_ms, the cycle
    must skip the on-disk parquet cache read entirely. Reading the full
    dataset costs 5-10s on a populated cache; the store serves the same
    in <50ms. Asserted by writing a SENTINEL row to the disk cache that
    would corrupt the output if read — the fast path must skip it."""
    from liquidity_migration.kline_store import KlineStore
    from liquidity_migration.storage import write_dataset

    # Disk cache holds a sentinel row that would surface if read.
    sentinel = pl.DataFrame([{
        "symbol": "AAAUSDT", "ts_ms": 999 * MS_PER_HOUR,
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume_base": 0.0, "turnover_quote": 0.0, "source": "DISK_SENTINEL",
    }])
    write_dataset(sentinel, tmp_path, "event_demo_klines_1h")

    # Store has the FULL universe covered at end_ms.
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        ts = hour * MS_PER_HOUR
        for symbol in ("AAAUSDT", "BBBUSDT"):
            store.add_bar(
                symbol,
                {"start": ts, "open": "100", "high": "110", "low": "90",
                 "close": "105", "volume": "1", "turnover": "1"},
                confirmed=True,
            )

    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=FailingKlineMarket(),
        cache_root=tmp_path,
        kline_store=store,
    )
    assert output.height == 6
    # Disk cache stat shows 0 — we didn't read it.
    assert stats["cache_rows"] == 0
    assert stats["cache_symbols"] == 0
    assert stats["store_rows"] == 6
    # Sentinel never made it into the output.
    assert "DISK_SENTINEL" not in output["source"].to_list()


def test_download_recent_1h_klines_falls_back_to_rest_for_uncovered_symbols(tmp_path: Path) -> None:
    """Hybrid path: store covers one symbol, REST fills the other."""
    from liquidity_migration.kline_store import KlineStore

    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    for hour in range(3):
        store.add_bar(
            "AAAUSDT",
            {
                "start": hour * MS_PER_HOUR,
                "open": "1", "high": "1", "low": "1", "close": "1",
                "volume": "1", "turnover": "1",
            },
            confirmed=True,
        )

    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT", "BBBUSDT"],
        start_ms=0,
        end_ms=2 * MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        kline_store=store,
    )
    # BBBUSDT only — AAAUSDT was served from the store.
    fetched_symbols = sorted({call[0] for call in market.calls})
    assert fetched_symbols == ["BBBUSDT"]
    # Output has bars for both symbols.
    assert output.height == 6
    assert sorted(output["symbol"].unique().to_list()) == ["AAAUSDT", "BBBUSDT"]
    assert stats["store_rows"] == 3
    assert stats["store_symbols"] == 1
    assert stats["fetch_symbols"] == 1
    assert stats["fetched_rows"] >= 3


def test_download_recent_1h_klines_ignores_store_failure_gracefully(tmp_path: Path) -> None:
    """A broken kline_store must never break the cycle — REST takes over."""

    class _BrokenStore:
        def symbols_with_coverage_through(self, ts_ms):
            raise RuntimeError("store offline")

        def get_klines(self, symbols, *, start_ms, end_ms):  # pragma: no cover
            raise AssertionError("should not be called after coverage failure")

    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
        kline_store=_BrokenStore(),
    )
    assert output.height >= 1
    assert stats["fetched_rows"] >= 1


def test_download_recent_1h_klines_without_store_keeps_legacy_behavior(tmp_path: Path) -> None:
    """Pre-existing call site (no kline_store) must behave identically to
    before: cache + REST path, no new stats blow-up."""
    market = FakeKlineMarket()
    output, stats = _download_recent_1h_klines(
        ["AAAUSDT"],
        start_ms=0,
        end_ms=MS_PER_HOUR,
        config=ResearchConfig(data_root=tmp_path),
        workers=1,
        market_client=market,
        cache_root=tmp_path,
    )
    assert output.height == 2
    assert stats["fetched_rows"] == 2
    # Store-related stat keys are present but zero when no store is wired.
    assert stats["store_rows"] == 0
    assert stats["store_symbols"] == 0


def test_resolve_ticker_snapshot_prefers_fresh_cache() -> None:
    """When the ticker cache is seeded + fresh, _resolve_ticker_snapshot
    returns the cache snapshot and never touches REST."""
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])

    class _FailingPublic:
        def get_tickers(self):
            raise AssertionError("REST must not be called when cache is fresh")

    rows, source = _resolve_ticker_snapshot(
        _FailingPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "ws_cache"
    assert rows[0]["symbol"] == "BTCUSDT"


def test_resolve_ticker_snapshot_falls_back_to_rest_when_unseeded() -> None:
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()  # never seeded

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "RESTUSDT", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "RESTUSDT"


def test_resolve_ticker_snapshot_falls_back_when_cache_stale() -> None:
    """An old seed (stale) must trigger REST fallback even if the cache has
    rows. Critical for safety: trading on a stale price snapshot is worse
    than waiting one REST roundtrip."""
    import time as _time
    from liquidity_migration.event_demo import _resolve_ticker_snapshot
    from liquidity_migration.ws_state_cache import TickerCache

    cache = TickerCache()
    cache.seed([{"symbol": "BTCUSDT", "lastPrice": "30000"}])
    # Force last_event timestamp to be ancient.
    cache._stats.last_event_monotonic = _time.monotonic() - 1000.0

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "FRESHUSDT", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=cache, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "FRESHUSDT"


def test_resolve_ticker_snapshot_with_no_cache_uses_rest() -> None:
    from liquidity_migration.event_demo import _resolve_ticker_snapshot

    class _RestPublic:
        def get_tickers(self):
            return [{"symbol": "X", "lastPrice": "1"}]

    rows, source = _resolve_ticker_snapshot(
        _RestPublic(), ticker_cache=None, state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert rows[0]["symbol"] == "X"


def test_resolve_private_snapshot_prefers_fresh_cache() -> None:
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    cache.seed(
        equity_usdt=12_500.0,
        positions=[{"symbol": "BTCUSDT", "size": "1.0"}],
        open_orders=[],
    )

    class _FailingClient:
        def get_positions(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

        def get_open_orders(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

        def get_wallet_balance(self, **kwargs):
            raise AssertionError("REST must not be called when cache is fresh")

    snap, source = _resolve_private_snapshot(
        _FailingClient(),
        EventDemoCycleConfig(),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "ws_cache"
    assert snap["equity_usdt"] == 12_500.0
    assert snap["raw_positions"][0]["symbol"] == "BTCUSDT"
    assert snap["raw_open_orders"] == []


def test_resolve_private_snapshot_falls_back_to_rest_when_cache_stale() -> None:
    import time as _time
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    cache.seed(equity_usdt=10_000.0)
    cache._stats.last_event_monotonic = _time.monotonic() - 1000.0

    # trading_client=None hits the neutral REST snapshot path.
    snap, source = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    # REST neutral snapshot returns the fallback equity, not the cached 10_000.
    assert snap["equity_usdt"] == 5_000.0


def test_resolve_private_snapshot_falls_back_to_rest_when_cache_unseeded() -> None:
    from liquidity_migration.event_demo import EventDemoCycleConfig, _resolve_private_snapshot
    from liquidity_migration.ws_state_cache import PrivateStateCache

    cache = PrivateStateCache()
    snap, source = _resolve_private_snapshot(
        None,
        EventDemoCycleConfig(fallback_equity_usdt=5_000.0),
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )
    assert source == "rest"
    assert snap["equity_usdt"] == 5_000.0


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


def test_build_demo_features_cache_returns_identical_frame_on_hit(tmp_path: Path) -> None:
    """The feature build is a pure function of (klines, universe). With a
    cache_root, an unchanged input must serve a parquet cache hit identical to
    a fresh recompute — this is what lets 59 of every 60 demo cycles skip the
    whole feature pipeline."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()

    fresh = _build_demo_features(klines, universe)
    cold = _build_demo_features(klines, universe, cache_root=tmp_path)  # miss -> compute + write
    parquet_path, metadata_path = _demo_feature_cache_paths(tmp_path)
    assert parquet_path.exists() and metadata_path.exists()

    warm = _build_demo_features(klines, universe, cache_root=tmp_path)  # hit -> parquet read
    assert not fresh.is_empty()
    assert warm.equals(fresh)
    assert cold.equals(fresh)


def test_build_demo_features_cache_misses_when_a_bar_is_appended(tmp_path: Path) -> None:
    """A new closed bar must change the fingerprint so the cache recomputes —
    a stale feature frame would silently freeze the entry signal."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()
    _build_demo_features(klines, universe, cache_root=tmp_path)

    next_bar = klines.filter(pl.col("symbol") == "SYM00USDT").tail(1).with_columns(
        pl.col("ts_ms") + MS_PER_HOUR
    )
    grown = pl.concat([klines, next_bar])
    assert _demo_feature_cache_fingerprint(grown, universe) != _demo_feature_cache_fingerprint(klines, universe)

    recomputed = _build_demo_features(grown, universe, cache_root=tmp_path)
    assert recomputed.equals(_build_demo_features(grown, universe))


def test_build_demo_features_cache_survives_subday_age_drift(tmp_path: Path) -> None:
    """listing_age_days creeps up every cycle — it is (now - launch_time)/day.
    The cache fingerprint must key on whole-day ages, so an otherwise-unchanged
    universe still hits across cycles. Without this the feature cache misses
    100% of the time in production (the bug live telemetry caught)."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()  # whole-number listing_age_days
    drifted = universe.with_columns(
        (pl.col("listing_age_days").cast(pl.Float64) + 0.37).alias("listing_age_days")
    )
    assert _demo_feature_cache_fingerprint(klines, universe) == _demo_feature_cache_fingerprint(klines, drifted)

    fresh = _build_demo_features(klines, universe)
    _build_demo_features(klines, universe, cache_root=tmp_path)  # miss -> compute + write
    parquet_path, _ = _demo_feature_cache_paths(tmp_path)
    written_at = parquet_path.stat().st_mtime_ns

    warm = _build_demo_features(klines, drifted, cache_root=tmp_path)  # must HIT despite drift
    assert parquet_path.stat().st_mtime_ns == written_at, "cache rewritten — fingerprint missed on sub-day drift"
    assert warm.equals(fresh)


def test_build_demo_features_without_cache_root_writes_nothing(tmp_path: Path) -> None:
    """cache_root=None (the default, used by tests and any non-cycle caller)
    must never touch disk."""
    klines = _feature_cache_klines()
    universe = _feature_cache_universe()
    _build_demo_features(klines, universe)
    assert not (tmp_path / ".cache" / "event_demo_features").exists()


def test_event_demo_cycles_dataset_is_date_partitioned(tmp_path: Path) -> None:
    """event_demo_cycles is append-only telemetry written every cycle. It must
    be date-partitioned so the per-cycle write stays bounded to the current
    day's rows instead of read+rewriting the whole (unbounded) dataset — and it
    must still round-trip cleanly through read_dataset for the tribunal."""
    day_ms = 24 * 60 * 60 * 1000
    day1 = 1_700_000_000_000
    day2 = day1 + day_ms
    rows = [
        {"cycle_id": "c1", "ts_ms": day1, "mode": "submit"},
        {"cycle_id": "c2", "ts_ms": day1 + 60_000, "mode": "submit"},
        {"cycle_id": "c3", "ts_ms": day2, "mode": "submit"},
    ]
    for row in rows:
        write_dataset(pl.DataFrame([row]), tmp_path, "event_demo_cycles", partition_by=("date",))

    date_parts = sorted(p.name for p in (tmp_path / "event_demo_cycles").glob("date=*"))
    assert len(date_parts) == 2, f"expected one partition per day, got {date_parts}"

    loaded = read_dataset(tmp_path, "event_demo_cycles")
    assert sorted(loaded["cycle_id"].to_list()) == ["c1", "c2", "c3"]


class _RecordingInstrumentsMarket:
    """Public market client that counts get_instruments_info calls so tests can
    prove the TTL cache suppresses repeat fetches."""

    def __init__(self) -> None:
        self.instrument_calls = 0

    def get_instruments_info(self) -> list[dict[str, str]]:
        self.instrument_calls += 1
        return [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}]


def test_demo_instruments_cache_serves_within_ttl(tmp_path: Path) -> None:
    """get_instruments_info is a large REST call but contract specs change ~daily.
    A second cycle inside the TTL must serve the cached frame, not refetch."""
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    first = _demo_instruments(market, cache_root=tmp_path, now_ms=now)
    assert market.instrument_calls == 1
    assert first["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]

    second = _demo_instruments(market, cache_root=tmp_path, now_ms=now + 59 * 60 * 1000)
    assert market.instrument_calls == 1, "within-TTL cycle must not refetch instruments"
    assert second.equals(first)


def test_demo_instruments_cache_refetches_after_ttl(tmp_path: Path) -> None:
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    _demo_instruments(market, cache_root=tmp_path, now_ms=now)
    _demo_instruments(market, cache_root=tmp_path, now_ms=now + 61 * 60 * 1000)
    assert market.instrument_calls == 2, "a cycle past the TTL must refetch instruments"


def test_demo_instruments_falls_back_to_stale_cache_on_fetch_error(tmp_path: Path) -> None:
    """A transient instruments-endpoint outage must not fail the whole cycle —
    contract specs barely change, so a stale cache is safe to reuse."""
    market = _RecordingInstrumentsMarket()
    now = 1_700_000_000_000
    cached = _demo_instruments(market, cache_root=tmp_path, now_ms=now)

    class _BrokenInstrumentsMarket:
        def get_instruments_info(self) -> list[dict[str, str]]:
            raise RuntimeError("bybit instruments endpoint down")

    served = _demo_instruments(_BrokenInstrumentsMarket(), cache_root=tmp_path, now_ms=now + 2 * 60 * 60 * 1000)
    assert served.equals(cached)


def test_warm_demo_kline_cache_populates_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """warm_demo_kline_cache pre-fetches the universe's 1h klines into the same
    event_demo_klines_1h cache a cycle reads — so the post-bar-close cycle finds
    the cache warm and skips the per-symbol REST burst."""
    monkeypatch.setattr(
        "liquidity_migration.event_demo._build_demo_universe",
        lambda *args, **kwargs: pl.DataFrame({"symbol": ["AAAUSDT", "BBBUSDT"]}),
    )

    class _WarmMarket:
        def get_instruments_info(self) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}]

        def get_tickers(self) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT", "markPrice": "100", "lastPrice": "100"}]

        def get_klines(self, symbol: str, interval: str, start: int, end: int) -> list[list[str]]:
            return [
                [str(ts_ms), "100", "110", "90", "105", "1.5", "157.5"]
                for ts_ms in range(start, end + 1, MS_PER_HOUR)
            ]

    stats = warm_demo_kline_cache(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(lookback_days=1, workers=1),
        market_client=_WarmMarket(),
        now_ms=100 * MS_PER_HOUR,
    )
    assert stats["symbols"] == 2
    cached = read_dataset(tmp_path, "event_demo_klines_1h")
    assert not cached.is_empty()
    assert set(cached["symbol"].to_list()) == {"AAAUSDT", "BBBUSDT"}


def test_warm_demo_kline_cache_handles_empty_universe(tmp_path: Path) -> None:
    """An empty universe (no tradable symbols) must yield a zero-stats no-op,
    not an error — the warmer runs unattended on a background thread."""
    stats = warm_demo_kline_cache(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(),
        market_client=MinimalEventMarket(),
        now_ms=100 * MS_PER_HOUR,
    )
    assert stats == {"symbols": 0, "fetch_symbols": 0, "fetched_rows": 0, "cache_rows": 0}


def test_collect_private_snapshots_neutral_without_client() -> None:
    """With no trading client the snapshot must be the same neutral result the
    old serial path produced: fallback equity, empty orders/positions, no errors."""
    snapshot = _collect_private_snapshots(None, EventDemoCycleConfig(fallback_equity_usdt=12_345.0))
    assert snapshot["equity_usdt"] == 12_345.0
    assert snapshot["raw_open_orders"] == []
    assert snapshot["raw_positions"] == []
    assert snapshot["wallet_error"] == ""
    assert snapshot["open_order_error"] == ""
    assert snapshot["position_error"] == ""


def test_collect_private_snapshots_gathers_all_three_from_client() -> None:
    """The concurrent fan-out must still return each endpoint's data correctly."""

    class _FakeClient:
        def get_wallet_balance(self, **_kwargs: object) -> dict[str, object]:
            return {"list": [{"totalEquity": "8000", "coin": [{"coin": "USDT", "equity": "8000"}]}]}

        def get_open_orders(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"symbol": "AAAUSDT", "orderLinkId": "lm-en-1"}]

        def get_positions(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"symbol": "BBBUSDT", "size": "3"}]

    snapshot = _collect_private_snapshots(_FakeClient(), EventDemoCycleConfig())
    assert snapshot["equity_usdt"] == 8000.0
    assert snapshot["raw_open_orders"] == [{"symbol": "AAAUSDT", "orderLinkId": "lm-en-1"}]
    assert snapshot["raw_positions"] == [{"symbol": "BBBUSDT", "size": "3"}]
    assert snapshot["wallet_error"] == ""


def test_refresh_positions_and_orders_returns_both_results() -> None:
    """The post-trade refetch runs positions + open orders concurrently; with no
    client both come back as the neutral empty result."""
    (positions, position_error), (orders, open_order_error) = _refresh_positions_and_orders(
        None, settle_coin="USDT"
    )
    assert positions == [] and position_error == ""
    assert orders == [] and open_order_error == ""


def test_event_demo_cycle_skips_entry_when_live_position_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-live-position",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "100",
                "positionValue": "100",
            }
        ]
    )
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["entry_candidates"] == 0
    assert payload["cycle"]["skipped_live_position_entry"] == 1
    assert payload["cycle"]["skipped_position_snapshot_error"] == 0
    assert payload["cycle"]["bybit_positions"] == 1
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()


def test_event_demo_cycle_skips_entry_when_live_position_in_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the WS-fed private cache shows a live position, the cycle MUST
    skip the entry even though the trading client is set up to claim no
    position. This is the cache-driven equivalent of the REST-snapshot
    skip test — proves the cache is the source of truth on the hot path."""
    from liquidity_migration.ws_state_cache import PrivateStateCache

    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-cache-live-position",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    # Trading client reports NO position via REST — would let the entry through.
    # But the CACHE shows a live AAAUSDT position, which must take precedence
    # because it is fresh + seeded.
    client = FakeRiskClient(positions=[])
    cache = PrivateStateCache()
    cache.seed(
        equity_usdt=10_000.0,
        positions=[{
            "symbol": "AAAUSDT", "side": "Sell", "size": "1",
            "avgPrice": "100", "markPrice": "100", "positionValue": "100",
        }],
        open_orders=[],
    )
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
        private_state_cache=cache,
        state_cache_stale_seconds=60.0,
    )

    # Entry skipped — cache reported a live position, even though REST client
    # would have claimed none.
    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["skipped_live_position_entry"] == 1
    assert payload["cycle"]["bybit_positions"] == 1
    # Telemetry confirms we used the cache.
    assert payload["data_sources"]["private_snapshot_source"] == "ws_cache"


def test_event_demo_cycle_skips_entries_when_position_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-position-error",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(fail_positions=True)
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["entry_candidates"] == 0
    assert payload["cycle"]["skipped_live_position_entry"] == 0
    assert payload["cycle"]["skipped_position_snapshot_error"] == 1
    assert payload["cycle"]["position_report_error"] == "positions unavailable"
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()


def test_event_demo_cycle_does_not_crash_when_reconcile_position_snapshot_fails_with_open_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t-existing",
                    "symbol": "AAAUSDT",
                    "side": "short",
                    "status": "open",
                    "entry_ts_ms": now_ms - 2 * MS_PER_HOUR,
                    "planned_exit_ts_ms": now_ms + 24 * MS_PER_HOUR,
                    "qty": "1",
                    "entry_price": 100.0,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    candidate = {
        "trade_id": "t-position-error",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(fail_positions=True)
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    trades = read_dataset(tmp_path, "event_demo_trades")
    assert client.orders == []
    assert trades.filter(pl.col("trade_id") == "t-existing").select("status").item() == "open"
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["skipped_position_snapshot_error"] == 1
    assert payload["cycle"]["position_report_error"] == "positions unavailable"


def test_event_demo_cycle_skips_entries_when_wallet_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-wallet-error",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(fail_wallet=True)
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["entry_candidates"] == 0
    assert payload["cycle"]["skipped_wallet_snapshot_error"] == 1
    assert payload["cycle"]["position_report_error"] == "wallet equity unavailable: wallet unavailable"
    assert payload["cycle"]["equity_usdt"] == 10_000.0
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()


def test_event_demo_cycle_still_exits_when_wallet_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t-existing",
                    "symbol": "AAAUSDT",
                    "side": "short",
                    "status": "open",
                    "entry_ts_ms": now_ms - 4 * 24 * MS_PER_HOUR,
                    "planned_exit_ts_ms": now_ms - MS_PER_HOUR,
                    "qty": "1",
                    "entry_price": 100.0,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    candidate = {
        "trade_id": "t-wallet-error-entry",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "100",
                "positionValue": "100",
                "unrealisedPnl": "0",
            }
        ],
        fill_market_orders=True,
        fill_order_prefixes=("lm-ex-",),
        fail_wallet=True,
    )
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    trades = read_dataset(tmp_path, "event_demo_trades")
    orders = read_dataset(tmp_path, "event_demo_orders")
    assert len(client.orders) == 1
    assert client.orders[0]["reduceOnly"] is True
    assert payload["cycle"]["exits_executed"] == 1
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["skipped_wallet_snapshot_error"] == 1
    assert payload["cycle"]["position_report_error"] == "wallet equity unavailable: wallet unavailable"
    assert trades.filter(pl.col("trade_id") == "t-existing").select("status").item() == "closed"
    assert orders.filter(pl.col("trade_id") == "t-existing").select("status").item() == "filled"


def test_event_demo_cycle_skips_entry_when_live_open_entry_order_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-live-open-order",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(
        open_orders=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "orderLinkId": "lm-en-existing",
                "orderStatus": "New",
                "reduceOnly": False,
            }
        ]
    )
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["entry_candidates"] == 0
    assert payload["cycle"]["bybit_open_orders"] == 1
    assert payload["cycle"]["bybit_entry_open_orders"] == 1
    assert payload["cycle"]["skipped_live_open_entry_order"] == 1
    assert payload["cycle"]["skipped_open_order_snapshot_error"] == 0
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()


def test_event_demo_cycle_skips_entries_when_open_order_snapshot_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    candidate = {
        "trade_id": "t-open-order-error",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    client = FakeRiskClient(fail_open_orders=True)
    _patch_minimal_event_cycle(monkeypatch, candidate)

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    assert client.orders == []
    assert payload["cycle"]["entries_executed"] == 0
    assert payload["cycle"]["entry_candidates"] == 0
    assert payload["cycle"]["skipped_live_open_entry_order"] == 0
    assert payload["cycle"]["skipped_open_order_snapshot_error"] == 1
    assert payload["cycle"]["position_report_error"] == "open orders unavailable"
    assert read_dataset(tmp_path, "event_demo_orders").is_empty()


def test_live_open_exit_order_filter_only_blocks_own_reduce_only_order() -> None:
    live_exit_symbols = _live_open_order_symbols(
        [
            {
                "symbol": "AAAUSDT",
                "orderLinkId": "manual-reduce",
                "orderStatus": "New",
                "reduceOnly": True,
            },
            {
                "symbol": "BBBUSDT",
                "orderLinkId": "lm-ex-existing",
                "orderStatus": "New",
                "reduceOnly": True,
            },
            {
                "symbol": "CCCUSDT",
                "orderLinkId": "lm-ex-filled",
                "orderStatus": "Filled",
                "reduceOnly": True,
            },
        ],
        reduce_only=True,
    )
    exits = [
        {"trade_id": "t1", "symbol": "AAAUSDT"},
        {"trade_id": "t2", "symbol": "BBBUSDT"},
    ]

    kept, skipped = _filter_live_open_exit_orders(exits, live_exit_symbols)

    assert live_exit_symbols == {"BBBUSDT"}
    assert kept == [{"trade_id": "t1", "symbol": "AAAUSDT"}]
    assert skipped == 1


def test_execute_entries_sizes_notional_before_leverage_margin() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows[0]["qty"] == "20"
    assert rows[0]["notional_usdt"] == 2_000.0
    assert rows[0]["entry_leverage"] == 2.0
    assert rows[0]["initial_margin_usdt"] == 1_000.0
    assert rows[0]["initial_margin_pct_equity"] == 0.10
    assert orders[0]["notional_usdt"] == 2_000.0
    assert orders[0]["initial_margin_usdt"] == 1_000.0


def test_execute_entry_attaches_native_stop_and_requires_fill_confirmation() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient()

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"
    assert client.stop_updates == []


def test_execute_entry_records_only_confirmed_fill() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows[0]["qty"] == "1"
    assert rows[0]["entry_price"] == 100.5
    assert rows[0]["stop_price"] == 112.6
    assert rows[0]["take_profit_price"] == 80.4
    assert rows[0]["entry_stop_update_status"] == "submitted"
    assert orders[0]["status"] == "partial"
    assert orders[0]["notional_usdt"] == 100.5
    assert orders[0]["stop_price"] == 112.6
    assert orders[0]["take_profit_price"] == 80.4
    assert orders[0]["stop_loss_pct"] == 0.12
    assert orders[0]["take_profit_pct"] == 0.20
    assert orders[0]["entry_stop_update_status"] == "submitted"
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"
    assert client.stop_updates == [{"symbol": "AAAUSDT", "stop_loss": "112.6", "take_profit": "80.4"}]


def test_execute_entry_records_leverage_error_without_raising() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fail_leverage_symbols={"AAAUSDT"})

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert len(orders) == 1
    assert orders[0]["status"] == "failed"
    assert orders[0]["submit_mode"] == "error"
    assert orders[0]["order_id"] == ""
    assert orders[0]["notional_usdt"] == 0.0
    assert orders[0]["initial_margin_usdt"] == 0.0
    assert orders[0]["stop_price"] == 112.0
    assert orders[0]["take_profit_price"] == 80.0
    assert orders[0]["stop_loss_pct"] == 0.12
    assert orders[0]["take_profit_pct"] == 0.20
    assert "set_leverage failed" in str(orders[0]["error"])
    assert "leverage rejected" in str(orders[0]["error"])
    assert client.orders == []


def test_execute_entry_records_order_error_and_continues() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        },
        {
            "trade_id": "t2",
            "symbol": "BBBUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_060_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        },
    ]
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-en-",),
        fill_qty="1",
        fill_price="100",
        fail_order_symbols={"AAAUSDT"},
    )

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.01,
        price_by_symbol={"AAAUSDT": 100.0, "BBBUSDT": 100.0},
        contract_by_symbol={
            "AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0},
            "BBBUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0},
        },
        now_ms=1_700_000_120_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert [row["trade_id"] for row in rows] == ["t2"]
    assert [row["symbol"] for row in orders] == ["AAAUSDT", "BBBUSDT"]
    # A place_order exception is ledgered submitted_unconfirmed (a pending
    # status) so reconciliation can adopt a lost-response fill -- not "failed".
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "error"
    assert "place_order failed" in str(orders[0]["error"])
    assert "order rejected" in str(orders[0]["error"])
    assert orders[1]["status"] == "filled"
    assert orders[1]["submit_mode"] == "submitted"
    assert orders[1]["error"] == ""
    assert rows[0]["entry_order_id"] == "order-1"


def test_execute_entry_fill_confirmation_error_leaves_pending_order_for_reconcile() -> None:
    candidates = [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
    ]
    client = FakeRiskClient(fail_trade_history=True)

    rows, orders = _execute_entries(
        candidates,
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )

    assert rows == []
    assert len(orders) == 1
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "submitted"
    assert orders[0]["order_id"] == "order-1"
    assert orders[0]["notional_usdt"] == 0.0
    assert orders[0]["qty"] == "20"
    assert "fill confirmation failed" in str(orders[0]["error"])
    assert "history unavailable" in str(orders[0]["error"])
    assert client.orders[0]["stopLoss"] == "112"
    assert client.orders[0]["takeProfit"] == "80"


def test_wallet_equity_usdt_prefers_total_equity_then_coin_equity() -> None:
    assert wallet_equity_usdt({"list": [{"totalEquity": "1234.5", "coin": []}]}) == 1234.5
    assert (
        wallet_equity_usdt(
            {
                "list": [
                    {
                        "totalEquity": "0",
                        "coin": [{"coin": "USDT", "equity": "321.25", "walletBalance": "300"}],
                    }
                ]
            }
        )
        == 321.25
    )


def test_bybit_position_snapshot_reports_unrealized_pnl() -> None:
    positions = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "10",
                "avgPrice": "100",
                "markPrice": "95",
                "positionValue": "950",
                "unrealisedPnl": "50",
                "leverage": "1",
            },
            {"symbol": "EMPTYUSDT", "side": "Buy", "size": "0"},
        ]
    )
    summary = summarize_position_pnl(positions)

    assert positions == [
        {
            "symbol": "AAAUSDT",
            "side": "short",
            "qty": 10.0,
            "avg_price": 100.0,
            "mark_price": 95.0,
            "position_value_usdt": 950.0,
            "unrealized_pnl_usdt": 50.0,
            "pnl_pct": 50.0 / 950.0,
            "leverage": 1.0,
        }
    ]
    assert summary["positions"] == 1
    assert summary["unrealized_pnl_usdt"] == 50.0


def test_ledger_position_snapshot_marks_short_pnl_from_current_price() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "10",
                "entry_price": 100.0,
            }
        ]
    )

    positions = build_ledger_position_pnl_snapshot(open_trades, {"AAAUSDT": 95.0})

    assert positions[0]["unrealized_pnl_usdt"] == 50.0
    assert positions[0]["position_value_usdt"] == 950.0


def test_telegram_status_message_includes_positions_and_pnl() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": 10_000.0,
            "entries_executed": 1,
            "entry_candidates": 1,
            "exits_executed": 0,
            "exit_candidates": 0,
            "position_report_error": "",
        },
        "bybit_position_summary": {
            "positions": 1,
            "position_value_usdt": 950.0,
            "unrealized_pnl_usdt": 50.0,
            "pnl_pct": 50.0 / 950.0,
        },
        "bybit_positions": [
            {
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": 10.0,
                "avg_price": 100.0,
                "mark_price": 95.0,
                "position_value_usdt": 950.0,
                "unrealized_pnl_usdt": 50.0,
                "pnl_pct": 50.0 / 950.0,
            }
        ],
        "ledger_position_summary": {
            "positions": 1,
            "position_value_usdt": 950.0,
            "unrealized_pnl_usdt": 50.0,
            "pnl_pct": 50.0 / 950.0,
        },
        "ledger_positions": [],
    }

    text = format_telegram_status_message(payload)

    assert "bybit_positions=1" in text
    assert "uPnL=$50.00" in text
    assert "AAAUSDT short" in text
    assert "reason=entry_executed" in text


def test_telegram_notify_only_for_material_events(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_send(text: str, *, enabled: bool) -> bool:
        sent.append(text)
        return enabled

    monkeypatch.setattr("liquidity_migration.event_demo.send_telegram_message", fake_send)
    quiet_payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": 10_000.0,
            "entries_executed": 0,
            "entry_candidates": 0,
            "exits_executed": 0,
            "exit_candidates": 0,
            "position_report_error": "",
        },
        "bybit_position_summary": {},
        "ledger_position_summary": {},
    }

    assert _telegram_notification_reason(quiet_payload) == ""
    assert _maybe_notify(quiet_payload, enabled=True) == (False, "quiet_no_material_event")
    assert sent == []
    entry_unconfirmed_payload = {
        **quiet_payload,
        "entry_orders": [{"status": "submitted_unconfirmed"}],
    }

    assert _telegram_notification_reason(entry_unconfirmed_payload) == "entry_order_unconfirmed"

    failed_entry_stop_update_payload = {
        **quiet_payload,
        "entry_orders": [{"status": "filled", "entry_stop_update_status": "failed"}],
    }

    assert _telegram_notification_reason(failed_entry_stop_update_payload) == "entry_stop_update_failed"

    failed_entry_order_payload = {
        **quiet_payload,
        "entry_orders": [{"status": "failed", "submit_mode": "error"}],
    }

    assert _telegram_notification_reason(failed_entry_order_payload) == "entry_order_error"

    reconciled_entry_payload = {
        **quiet_payload,
        "cycle": {
            **quiet_payload["cycle"],
            "pending_entry_fills_reconciled": 1,
        },
    }

    assert _telegram_notification_reason(reconciled_entry_payload) == "entry_fill_reconciled"

    reconciled_exit_payload = {
        **quiet_payload,
        "cycle": {
            **quiet_payload["cycle"],
            "pending_exit_fills_reconciled": 1,
        },
    }

    assert _telegram_notification_reason(reconciled_exit_payload) == "exit_fill_reconciled"

    alert_payload = {
        **quiet_payload,
        "cycle": {
            **quiet_payload["cycle"],
            "entries_executed": 1,
            "entry_candidates": 1,
        },
    }

    assert _telegram_notification_reason(alert_payload) == "entry_executed"
    assert _maybe_notify(alert_payload, enabled=True) == (True, "")
    assert len(sent) == 1


def test_order_quantity_for_notional_floors_to_qty_step_and_min_notional() -> None:
    result = order_quantity_for_notional(
        notional_usdt=100.0,
        price=9.9,
        qty_step=0.1,
        min_order_qty=0.1,
        min_notional_value=5.0,
    )

    assert result == ("10.1", 99.99)
    assert (
        order_quantity_for_notional(
            notional_usdt=3.0,
            price=9.9,
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_value=5.0,
        )
        is None
    )


def test_order_quantity_for_notional_caps_by_max_order_qty() -> None:
    """Regression guard for the SUPERUSDT-2026-05-25 rejection.

    Demo cycle sized a 26477-contract market entry on SUPERUSDT while
    Bybit's maxMktOrderQty for the symbol is 21100. The order errored
    with ErrCode 10001 ("number of contracts exceeds maximum limit");
    the ledger row sat in submit_unconfirmed and the paper run still
    "took" the entry, producing a reconciliation gap.

    With max_order_qty supplied, the function must cap at the max
    (floored to qty_step) instead of returning the over-cap value or
    rejecting outright.
    """
    # Without the cap: the request rounds down to 26477 contracts.
    uncapped = order_quantity_for_notional(
        notional_usdt=3287.4951,  # 26477 × 0.12416
        price=0.12416,
        qty_step=1.0,
        min_order_qty=1.0,
    )
    assert uncapped is not None
    assert uncapped[0] == "26477"

    # With the cap (Bybit's actual maxMktOrderQty for SUPERUSDT at the time
    # of the incident): qty floors to 21100.
    capped = order_quantity_for_notional(
        notional_usdt=3287.4951,
        price=0.12416,
        qty_step=1.0,
        min_order_qty=1.0,
        max_order_qty=21100.0,
    )
    assert capped is not None
    assert capped[0] == "21100"
    assert capped[1] == pytest.approx(21100.0 * 0.12416, rel=1e-9)


def test_order_quantity_for_notional_caps_floors_to_qty_step() -> None:
    """max_order_qty may not be step-aligned (e.g. an exchange that
    publishes 100 with a qty_step of 7). The cap must floor to the step
    grid so the order_qty is always a valid multiple of qty_step."""
    capped = order_quantity_for_notional(
        notional_usdt=10_000.0,  # would buy 1000 @ $10 if uncapped
        price=10.0,
        qty_step=7.0,
        max_order_qty=100.0,
    )
    assert capped is not None
    # 100 // 7 * 7 = 98
    assert capped[0] == "98"


def test_order_quantity_for_notional_returns_none_when_cap_below_min() -> None:
    """When max_order_qty < min_order_qty (unusual but possible during
    a venue config change), skip the candidate rather than sending a
    sub-min order Bybit will reject."""
    result = order_quantity_for_notional(
        notional_usdt=1_000_000.0,
        price=1.0,
        qty_step=1.0,
        min_order_qty=100.0,
        max_order_qty=50.0,
    )
    assert result is None


def test_select_demo_entry_candidates_uses_selected_liquidity_migration_filters() -> None:
    signal_ts = 1_700_000_000_000
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.20,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_close_location_min=0.0,
        liquidity_migration_pit_age_days_min=0,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 2.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": False,
            },
            {
                "ts_ms": signal_ts,
                "symbol": "BBBUSDT",
                "dollar_volume_rank_z": 2.5,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 60,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 8_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "tradable_membership_flag": False,
            },
        ]
    )

    candidates, skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + MS_PER_HOUR + 5 * 60_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=180,
        max_new_entries=6,
    )

    assert [row["symbol"] for row in candidates] == ["AAAUSDT"]
    assert candidates[0]["side"] == "short"
    assert candidates[0]["stop_loss_pct"] == 0.12
    assert candidates[0]["take_profit_pct"] == 0.20
    assert skips["not_ready"] == 0


def test_select_demo_entry_candidates_waits_for_quality_squeeze_giveback() -> None:
    signal_ts = 1_700_000_000_000
    hour = MS_PER_HOUR
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.40,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.25,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_close_location": 0.70,
                "pit_age_days": 120.0,
                "tradable_membership_flag": False,
            }
        ]
    )
    bar_dicts = [
        {"bar_end_ts_ms": signal_ts, "high": 101.0, "low": 99.0, "close": 100.0},
        {"bar_end_ts_ms": signal_ts + hour, "high": 101.2, "low": 100.0, "close": 101.1},
        {"bar_end_ts_ms": signal_ts + 2 * hour, "high": 101.6, "low": 101.0, "close": 101.5},
        {"bar_end_ts_ms": signal_ts + 3 * hour, "high": 101.6, "low": 100.9, "close": 101.1},
    ]
    ends = [int(b["bar_end_ts_ms"]) for b in bar_dicts]
    bars: dict[str, dict[str, Any]] = {
        "AAAUSDT": {
            "ts_ms": np.array([end - hour for end in ends], dtype=np.int64),
            "bar_end_ts_ms": np.array(ends, dtype=np.int64),
            "open": np.array([b["close"] for b in bar_dicts], dtype=np.float64),
            "high": np.array([b["high"] for b in bar_dicts], dtype=np.float64),
            "low": np.array([b["low"] for b in bar_dicts], dtype=np.float64),
            "close": np.array([b["close"] for b in bar_dicts], dtype=np.float64),
            "ends": ends,
            "by_end": {end: idx for idx, end in enumerate(ends)},
        }
    }

    pending_candidates, pending_skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 2 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        entry_bars_by_symbol=bars,
    )
    ready_candidates, ready_skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 3 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        entry_bars_by_symbol=bars,
    )

    assert pending_candidates == []
    assert pending_skips["not_ready"] == 1
    assert ready_skips["not_ready"] == 0
    assert len(ready_candidates) == 1
    assert ready_candidates[0]["entry_ready_ts_ms"] == signal_ts + 3 * hour
    assert ready_candidates[0]["entry_rule"] == "quality_squeeze_giveback"
    assert ready_candidates[0]["entry_quality_tier"] == "promoted_quality"
    assert ready_candidates[0]["actual_entry_delay_hours"] == 3.0


def test_select_demo_entry_candidates_builds_entry_bars_from_klines() -> None:
    signal_ts = 1_700_000_000_000
    hour = MS_PER_HOUR
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.40,
        side_hypothesis="reversal",
        hold_days=3,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.25,
    )
    config = VolumeEventResearchConfig(
        require_pit_membership=False,
        require_full_pit_universe=False,
        liquidity_migration_crowding_filter="none",
    )
    features = pl.DataFrame(
        [
            {
                "ts_ms": signal_ts,
                "symbol": "AAAUSDT",
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.85,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 225,
                "turnover_quote": 7_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.02,
                "residual_return_1d": 0.09,
                "market_pct_up_1d": 0.55,
                "signal_day_close_location": 0.70,
                "pit_age_days": 120.0,
                "tradable_membership_flag": False,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": signal_ts - hour, "open": 99.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"symbol": "AAAUSDT", "ts_ms": signal_ts, "open": 100.0, "high": 101.2, "low": 100.0, "close": 101.1},
            {
                "symbol": "AAAUSDT",
                "ts_ms": signal_ts + hour,
                "open": 101.1,
                "high": 101.6,
                "low": 101.0,
                "close": 101.5,
            },
            {
                "symbol": "AAAUSDT",
                "ts_ms": signal_ts + 2 * hour,
                "open": 101.5,
                "high": 101.6,
                "low": 100.9,
                "close": 101.1,
            },
            {"symbol": "ZZZUSDT", "ts_ms": signal_ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
        ]
    )

    candidates, skips = select_demo_entry_candidates(
        features,
        pl.DataFrame(),
        now_ms=signal_ts + 3 * hour + 30_000,
        config=config,
        scenario=scenario,
        max_entry_lag_minutes=240,
        max_new_entries=6,
        klines=klines,
    )

    assert skips["not_ready"] == 0
    assert len(candidates) == 1
    assert candidates[0]["entry_ready_ts_ms"] == signal_ts + 3 * hour
    assert candidates[0]["entry_rule"] == "quality_squeeze_giveback"


def test_plan_demo_exits_detects_rank_decay_before_max_hold() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
    )
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": 1_000,
                "planned_exit_ts_ms": 1_000 + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={("AAAUSDT", 1_000 + MS_PER_HOUR): 0.69},
        klines=pl.DataFrame(),
        price_by_symbol={"AAAUSDT": 99.0},
        now_ms=1_000 + MS_PER_HOUR,
        config=VolumeEventResearchConfig(require_pit_membership=False, require_full_pit_universe=False),
        scenario=scenario,
    )

    assert exits == [
        {
            "trade_id": "t1",
            "symbol": "AAAUSDT",
            "side": "short",
            "qty": "1",
            "exit_reason": "event_decay",
            "exit_trigger_ts_ms": 1_000 + MS_PER_HOUR,
            "planned_exit_price": 99.0,
            "planned_exit_ts_ms": 1_000 + 24 * MS_PER_HOUR,
        }
    ]


def test_plan_demo_exits_detects_take_profit_before_max_hold() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.20,
    )
    entry_ts = 1_700_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": entry_ts,
                "planned_exit_ts_ms": entry_ts + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {
                "ts_ms": entry_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 101.0,
                "low": 79.0,
                "close": 81.0,
            }
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={},
        klines=klines,
        price_by_symbol={"AAAUSDT": 81.0},
        now_ms=entry_ts + MS_PER_HOUR,
        config=VolumeEventResearchConfig(require_pit_membership=False, require_full_pit_universe=False),
        scenario=scenario,
    )

    assert exits[0]["exit_reason"] == "take_profit"
    assert exits[0]["planned_exit_price"] == 80.0


def test_plan_demo_exits_detects_failed_fade_on_completed_bar() -> None:
    scenario = EventScenario(
        event_type="liquidity_migration",
        threshold=0.30,
        side_hypothesis="reversal",
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
        take_profit_pct=0.0,
    )
    entry_ts = 1_700_000_000_000
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "entry_ts_ms": entry_ts,
                "entry_price": 100.0,
                "planned_exit_ts_ms": entry_ts + 24 * MS_PER_HOUR,
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 0.0,
            }
        ]
    )
    klines = pl.DataFrame(
        [
            {"ts_ms": entry_ts, "symbol": "AAAUSDT", "open": 100.0, "high": 101.2, "low": 99.7, "close": 101.0},
            {
                "ts_ms": entry_ts + MS_PER_HOUR,
                "symbol": "AAAUSDT",
                "open": 101.0,
                "high": 103.0,
                "low": 100.5,
                "close": 102.8,
            },
        ]
    )

    exits = plan_demo_exits(
        open_trades,
        rank_lookup={},
        klines=klines,
        price_by_symbol={"AAAUSDT": 102.8},
        now_ms=entry_ts + 2 * MS_PER_HOUR,
        config=VolumeEventResearchConfig(
            require_pit_membership=False,
            require_full_pit_universe=False,
            failed_fade_exit_hours=2,
            failed_fade_min_mfe_pct=0.005,
            failed_fade_loss_pct=0.025,
            failed_fade_close_location_min=0.85,
        ),
        scenario=scenario,
    )

    assert exits[0]["exit_reason"] == "failed_fade"
    assert exits[0]["exit_trigger_ts_ms"] == entry_ts + 2 * MS_PER_HOUR
    assert exits[0]["planned_exit_price"] == 102.8


def test_plan_risk_exits_uses_live_position_price_for_stops() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            },
            {
                "trade_id": "t2",
                "symbol": "BBBUSDT",
                "side": "short",
                "status": "open",
                "qty": "2",
                "stop_price": 112.0,
                "planned_exit_ts_ms": 1_700_000_000_000,
            },
        ]
    )

    exits = plan_risk_exits(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "markPrice": "113"},
            "BBBUSDT": {"symbol": "BBBUSDT", "side": "Sell", "size": "2", "markPrice": "99"},
        },
        price_by_symbol={"AAAUSDT": 113.0, "BBBUSDT": 99.0},
        now_ms=1_700_000_060_000,
    )

    assert [row["exit_reason"] for row in exits] == ["stop_loss", "max_hold"]
    assert exits[0]["qty"] == "1"
    assert exits[0]["planned_exit_price"] == 113.0


def test_plan_stop_repairs_detects_missing_exchange_stop() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )

    repairs = plan_stop_repairs(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "stopLoss": "",
                "takeProfit": "80.0001",
            }
        },
        tolerance_bps=1.0,
    )

    assert len(repairs) == 1
    assert repairs[0]["needs_stop_repair"] is True
    assert repairs[0]["needs_take_profit_repair"] is False


def test_limit_chase_price_crosses_spread_with_tick_rounding() -> None:
    assert _limit_chase_price(bybit_side="Buy", reference_price=100.0, bps=10.0, tick_size=0.1) == 100.1
    assert _limit_chase_price(bybit_side="Sell", reference_price=100.0, bps=10.0, tick_size=0.1) == 99.9


def test_limit_chase_uses_ioc_limits_then_market_fallback() -> None:
    client = FakeRiskClient(fill_market_orders=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert [row["orderType"] for row in client.orders] == ["Limit", "Limit", "Market"]
    assert client.orders[0]["timeInForce"] == "IOC"
    assert client.orders[0]["reduceOnly"] is True
    assert client.orders[0]["price"] == "100.1"
    assert submit["exec_summary"]["qty"] == "1"
    assert [row["status"] for row in submit["order_rows"]] == ["unfilled", "unfilled", "filled"]
    assert submit["order_rows"][-1]["target_qty"] == "1"
    assert submit["order_rows"][-1]["filled_qty"] == "1"


def test_market_risk_exit_history_error_stays_pending() -> None:
    client = FakeRiskClient(fail_trade_history=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="market",
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert submit["order_id"] == "order-1"
    assert submit["exec_summary"]["qty"] == ""
    assert submit["order_rows"][0]["status"] == "submitted_unconfirmed"
    assert "fill confirmation failed" in submit["order_rows"][0]["error"]


def test_limit_chase_history_error_stops_before_fallback() -> None:
    client = FakeRiskClient(fail_trade_history=True)

    submit = _submit_reduce_only_exit(
        symbol="AAAUSDT",
        bybit_side="Buy",
        qty="1",
        trading_client=client,
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_000_000,
        reference_price=100.0,
        tick_size=0.1,
    )

    assert [row["orderType"] for row in client.orders] == ["Limit"]
    assert submit["order_id"] == "order-1"
    assert submit["exec_summary"]["qty"] == ""
    assert submit["order_rows"][0]["status"] == "submitted_unconfirmed"
    assert "fill confirmation failed" in submit["order_rows"][0]["error"]


def test_event_exit_does_not_close_until_fill_confirmed() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["notional_usdt"] == 0.0


def test_event_exit_records_order_error_without_raising() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_order_symbols={"AAAUSDT"}),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "failed"
    assert orders[0]["submit_mode"] == "error"
    assert orders[0]["order_id"] == ""
    assert "place_order failed" in orders[0]["error"]
    assert "order rejected" in orders[0]["error"]


def test_event_exit_fill_confirmation_error_stays_pending() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_trade_history=True),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["submit_mode"] == "submitted"
    assert orders[0]["order_id"] == "order-1"
    assert "fill confirmation failed" in orders[0]["error"]
    assert "history unavailable" in orders[0]["error"]


def test_event_exit_partial_fill_reduces_trade_qty_immediately() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",), fill_qty="0.4"),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.6"
    assert rows[0]["notional_usdt"] == 60.0
    assert rows[0]["partial_exit_reason"] == "max_hold"
    assert rows[0]["partial_exit_qty"] == "0.4"
    assert orders[0]["status"] == "partial"
    assert orders[0]["filled_qty"] == "0.4"


def test_event_exit_closes_after_confirmed_fill() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )

    rows, orders = _execute_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 99.0,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",)),
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
    )

    assert rows[0]["status"] == "closed"
    assert rows[0]["exit_price"] == 100.5
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == "1"


def test_pending_entry_fill_reconciles_to_open_trade() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "open"
    assert trades[0]["qty"] == "1"
    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["stop_price"] == 112.0
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["filled_qty"] == "1"


def test_pending_entry_fill_recomputes_protection_from_confirmed_fill() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.20,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["stop_price"] == 112.6
    assert trades[0]["take_profit_price"] == 80.4
    assert trades[0]["entry_stop_update_status"] == "submitted"
    assert order_updates[0]["stop_price"] == 112.6
    assert order_updates[0]["take_profit_price"] == 80.4
    assert order_updates[0]["entry_stop_update_status"] == "submitted"
    assert client.stop_updates == [{"symbol": "AAAUSDT", "stop_loss": "112.6", "take_profit": "80.4"}]


def test_pending_fill_history_error_keeps_order_pending() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
            }
        ]
    )
    client = FakeRiskClient(fail_trade_history=True)

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades == []
    assert order_updates[0]["status"] == "submitted_unconfirmed"
    assert order_updates[0]["order_link_id"] == "lm-en-pending"
    assert "fill reconciliation failed" in order_updates[0]["error"]
    assert "history unavailable" in order_updates[0]["error"]


def test_stale_pending_order_fill_is_not_polled_forever() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale",
                "ts_ms": 1_700_000_000_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_000_000 + 16 * 60_000,
    )

    assert trades == []
    assert order_updates == []
    assert client.trade_history_calls == []


def test_stale_pending_order_fill_reconciles_when_live_position_exists() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale-live",
                "ts_ms": 1_700_000_000_000,
                "trade_id": "t-live",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-live",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 0.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "trade_side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "equity_usdt": 10_000.0,
                "tick_size": 0.1,
                "qty_step": 0.1,
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "target_qty": "1",
                "filled_qty": "",
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_000_000 + 16 * 60_000,
        live_position_symbols={"AAAUSDT"},
    )

    assert client.trade_history_calls == ["lm-en-stale-live"]
    assert trades[0]["status"] == "open"
    assert trades[0]["trade_id"] == "t-live"
    assert trades[0]["qty"] == "1"
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["filled_qty"] == "1"


def test_stale_pending_entry_terminalizes_only_when_exchange_flat() -> None:
    now_ms = 1_700_000_000_000
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-stale-flat",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                "trade_id": "t-flat",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "lm-en-fresh",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS,
                "trade_id": "t-fresh",
                "symbol": "BBBUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "lm-ex-stale",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                "trade_id": "t-exit",
                "symbol": "CCCUSDT",
                "side": "Buy",
                "qty": "1",
                "reduce_only": True,
                "status": "submitted_unconfirmed",
            },
        ]
    )

    updates = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols=set(),
        live_open_entry_order_symbols=set(),
        now_ms=now_ms,
    )
    blocked_by_position = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols={"AAAUSDT"},
        live_open_entry_order_symbols=set(),
        now_ms=now_ms,
    )
    blocked_by_open_order = _terminalize_stale_pending_entry_orders(
        orders,
        live_position_symbols=set(),
        live_open_entry_order_symbols={"AAAUSDT"},
        now_ms=now_ms,
    )

    assert [row["order_link_id"] for row in updates] == ["lm-en-stale-flat"]
    assert updates[0]["status"] == "expired_unconfirmed"
    assert "flat Bybit position and no open order" in updates[0]["error"]
    assert blocked_by_position == []
    assert blocked_by_open_order == []


def test_event_demo_cycle_terminalizes_stale_pending_entry_when_exchange_flat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-stale-flat",
                    "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                    "trade_id": "t-flat",
                    "symbol": "AAAUSDT",
                    "side": "Sell",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": False,
                    "order_id": "order-flat",
                    "submit_mode": "submitted",
                    "avg_price": 100.0,
                    "notional_usdt": 0.0,
                    "status": "submitted_unconfirmed",
                    "trade_side": "short",
                    "signal_ts_ms": now_ms - MS_PER_HOUR,
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    candidate = {
        "trade_id": "unused",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    _patch_minimal_event_cycle(monkeypatch, candidate)
    monkeypatch.setattr("liquidity_migration.event_demo.select_demo_entry_candidates", lambda *args, **kwargs: ([], {}))
    client = FakeRiskClient()

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    orders = read_dataset(tmp_path, "event_demo_orders")
    assert client.trade_history_calls == []
    assert orders.filter(pl.col("order_link_id") == "lm-en-stale-flat").select("status").item() == "expired_unconfirmed"
    assert payload["cycle"]["stale_pending_entry_orders_terminalized"] == 1


def test_event_demo_cycle_reconciles_stale_pending_entry_when_position_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = 1_700_000_060_000
    write_dataset(
        pl.DataFrame(
            [
                {
                    "order_link_id": "lm-en-stale-live",
                    "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                    "trade_id": "t-live",
                    "symbol": "AAAUSDT",
                    "side": "Sell",
                    "order_type": "Market",
                    "qty": "1",
                    "reduce_only": False,
                    "order_id": "order-live",
                    "submit_mode": "submitted",
                    "avg_price": 100.0,
                    "notional_usdt": 0.0,
                    "target_notional_pct_equity": 0.2,
                    "entry_leverage": 2.0,
                    "initial_margin_usdt": 0.0,
                    "status": "submitted_unconfirmed",
                    "trade_side": "short",
                    "signal_ts_ms": now_ms - MS_PER_HOUR,
                    "equity_usdt": 10_000.0,
                    "tick_size": 0.1,
                    "qty_step": 0.1,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                    "target_qty": "1",
                    "filled_qty": "",
                }
            ]
        ),
        tmp_path,
        "event_demo_orders",
        partition_by=(),
    )
    candidate = {
        "trade_id": "unused",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": now_ms - MS_PER_HOUR,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    _patch_minimal_event_cycle(monkeypatch, candidate)
    monkeypatch.setattr("liquidity_migration.event_demo.select_demo_entry_candidates", lambda *args, **kwargs: ([], {}))
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("lm-en-",),
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100.5",
                "markPrice": "100.5",
                "positionValue": "100.5",
                "unrealisedPnl": "0",
            }
        ],
    )

    payload = run_event_demo_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        demo_config=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        market_client=MinimalEventMarket(),
        private_client=client,
        now_ms=now_ms,
    )

    trades = read_dataset(tmp_path, "event_demo_trades")
    orders = read_dataset(tmp_path, "event_demo_orders")
    trade = trades.filter(pl.col("trade_id") == "t-live").to_dicts()[0]
    assert client.trade_history_calls == ["lm-en-stale-live"]
    assert client.orders == []
    assert trade["status"] == "open"
    assert trade["qty"] == "1"
    assert orders.filter(pl.col("order_link_id") == "lm-en-stale-live").select("status").item() == "filled"
    assert payload["cycle"]["pending_entry_fills_reconciled"] == 1
    assert payload["cycle"]["stale_pending_entry_orders_terminalized"] == 0


def test_pending_exit_fill_reconciles_to_closed_trade() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-ex-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Buy",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": True,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "exit_reason": "time_exit",
                "exit_trigger_ts_ms": 1_700_000_061_000,
                "target_qty": "1",
                "filled_qty": "",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 99.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "closed"
    assert trades[0]["exit_reason"] == "time_exit"
    assert trades[0]["exit_trigger_ts_ms"] == 1_700_000_061_000
    assert trades[0]["exit_price"] == 100.5
    assert order_updates[0]["status"] == "filled"
    assert order_updates[0]["notional_usdt"] == 100.5


def test_pending_exit_partial_fill_reduces_open_trade_qty() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-ex-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Buy",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": True,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 0.0,
                "notional_usdt": 0.0,
                "status": "submitted_unconfirmed",
                "exit_reason": "time_exit",
                "target_qty": "1",
                "filled_qty": "",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 99.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-ex-",), fill_qty="0.4")

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["status"] == "open"
    assert trades[0]["qty"] == "0.6"
    assert trades[0]["partial_exit_reason"] == "time_exit"
    assert order_updates[0]["status"] == "partial"
    assert order_updates[0]["filled_qty"] == "0.4"


def test_pending_entry_additional_fill_updates_open_trade_qty() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "lm-en-pending",
                "ts_ms": 1_700_000_060_000,
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "order_type": "Market",
                "qty": "1",
                "reduce_only": False,
                "order_id": "order-1",
                "submit_mode": "submitted",
                "avg_price": 100.0,
                "notional_usdt": 40.0,
                "target_notional_pct_equity": 0.2,
                "entry_leverage": 2.0,
                "initial_margin_usdt": 20.0,
                "status": "partial",
                "filled_qty": "0.4",
            }
        ]
    )
    trades_df = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "0.4",
                "entry_price": 100.0,
                "notional_usdt": 40.0,
                "equity_usdt": 10_000.0,
                "entry_leverage": 2.0,
            }
        ]
    )
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",), fill_qty="0.7")

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        trades_df,
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_120_000,
    )

    assert trades[0]["qty"] == "0.7"
    assert trades[0]["entry_price"] == 100.5
    assert trades[0]["notional_usdt"] == pytest.approx(70.35)
    assert order_updates[0]["status"] == "partial"
    assert order_updates[0]["filled_qty"] == "0.7"


def test_risk_exit_does_not_close_until_submitted_fill_confirmed() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows == []
    assert orders[0]["status"] == "submitted_unconfirmed"
    assert orders[0]["notional_usdt"] == 0.0


def test_risk_exit_failure_records_auditable_order_context() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fail_order_symbols={"AAAUSDT"}),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows == []
    assert orders[0]["status"] == "failed"
    assert orders[0]["trade_id"] == "t1"
    assert orders[0]["exit_reason"] == "stop_loss"
    assert orders[0]["exit_trigger_ts_ms"] == 1_700_000_000_000
    assert orders[0]["target_qty"] == "1"
    assert orders[0]["filled_qty"] == ""
    assert orders[0]["avg_price"] == 113.0
    assert "order rejected" in orders[0]["error"]


def test_risk_exit_partial_fill_reduces_trade_qty_immediately() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-rx-",), fill_qty="0.4"),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.6"
    assert rows[0]["notional_usdt"] == 60.0
    assert rows[0]["partial_exit_reason"] == "stop_loss"
    assert rows[0]["partial_exit_qty"] == "0.4"
    assert orders[0]["status"] == "partial"
    assert orders[0]["target_qty"] == "1"
    assert orders[0]["filled_qty"] == "0.4"


def test_limit_chase_partial_fills_keep_child_order_quantities() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 100.0,
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-lc-",), fill_qty="0.4"),
        risk=EventRiskCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            exit_order_mode="limit_chase",
            limit_chase_attempts=2,
            limit_chase_wait_seconds=0.0,
        ),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={"AAAUSDT": 0.1},
    )

    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == "0.2"
    assert [order["order_type"] for order in orders] == ["Limit", "Limit", "Market"]
    assert [order["status"] for order in orders] == ["partial", "partial", "fallback_market"]
    assert [order["target_qty"] for order in orders] == ["1", "0.6", "0.2"]
    assert [order["filled_qty"] for order in orders] == ["0.4", "0.4", ""]
    assert orders[0]["notional_usdt"] == pytest.approx(40.2)
    assert orders[1]["notional_usdt"] == pytest.approx(40.2)


def test_risk_exit_records_filled_order_after_confirmed_fill() -> None:
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )

    rows, orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "stop_loss",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 113.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ],
        all_trades,
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-rx-",)),
        risk=EventRiskCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 113.0},
        tick_size_by_symbol={},
    )

    assert rows[0]["status"] == "closed"
    assert orders[0]["status"] == "filled"
    assert orders[0]["filled_qty"] == "1"


def test_run_event_risk_cycle_dry_run_closes_crossed_stop(tmp_path) -> None:
    write_dataset(
        pl.DataFrame(
            [
                {
                    "trade_id": "t1",
                    "symbol": "AAAUSDT",
                    "side": "short",
                    "status": "open",
                    "qty": "1",
                    "entry_price": 100.0,
                    "stop_price": 112.0,
                    "take_profit_price": 80.0,
                    "planned_exit_ts_ms": 1_700_100_000_000,
                }
            ]
        ),
        tmp_path,
        "event_demo_trades",
        partition_by=(),
    )
    client = FakeRiskClient(
        positions=[
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "avgPrice": "100",
                "markPrice": "113",
                "positionValue": "113",
                "unrealisedPnl": "-13",
                "stopLoss": "112",
                "takeProfit": "80",
            }
        ]
    )

    payload = run_event_risk_cycle(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventRiskCycleConfig(record_dry_run=True, repair_stops=False),
        private_client=client,
        now_ms=1_700_000_060_000,
    )

    trades = read_dataset(tmp_path, "event_demo_trades")
    assert payload["cycle"]["exits_executed"] == 1
    assert payload["exits"][0]["status"] == "closed"
    assert trades.filter(pl.col("trade_id") == "t1").select("status").item() == "closed"
    assert (tmp_path / "reports" / "event-risk" / "latest_event_risk_cycle.md").exists()


def test_submit_orders_requires_explicit_confirmation() -> None:
    config = EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=False)

    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        _validate_demo_config(config)


def test_demo_relaxed_profile_lowers_gates_for_more_demo_trades() -> None:
    strategy = _demo_event_config(VolumeEventResearchConfig(), profile="demo_relaxed")

    assert strategy.take_profit_pcts == (0.21,)
    assert strategy.failed_fade_exit_hours == 6
    assert strategy.failed_fade_min_mfe_pct == 0.01
    assert strategy.failed_fade_loss_pct == 0.04
    assert strategy.failed_fade_close_location_min == 0.0
    assert strategy.max_active_symbols == 10
    assert strategy.cooldown_days == 2
    assert strategy.universe_rank_min == 11
    assert strategy.universe_rank_max == 260
    assert strategy.liquidity_migration_rank_improvement_min == 80
    assert strategy.liquidity_migration_turnover_ratio_min == 3.0
    assert strategy.liquidity_migration_day_return_min == -0.03
    assert strategy.liquidity_migration_residual_return_min == 0.03
    assert strategy.liquidity_migration_close_location_min == 0.25
    assert strategy.liquidity_migration_crowding_filter == "union_pathology"


def test_demo_relaxed_profile_requires_wide_forward_universe() -> None:
    # demo_relaxed needs trade_rank_max(260) + rank_improvement_min(80) = 340
    # so prior-week ranks of rocket-symbols are observable.
    with pytest.raises(ValueError, match="rank 340"):
        _validate_demo_config(
            EventDemoCycleConfig(strategy_profile="demo_relaxed", universe_rank_end=220, universe_max_symbols=400)
        )
    with pytest.raises(ValueError, match="340"):
        _validate_demo_config(
            EventDemoCycleConfig(strategy_profile="demo_relaxed", universe_rank_end=400, universe_max_symbols=220)
        )
    # exact minimum passes
    _validate_demo_config(
        EventDemoCycleConfig(strategy_profile="demo_relaxed", universe_rank_end=340, universe_max_symbols=340)
    )


def test_promoted_profile_requires_wide_forward_universe() -> None:
    # Regression for the 2026-05-24 demo-VPS bug: rank_end=220 was passing the
    # validator even though the promoted profile needs trade_rank_max(150) +
    # rank_improvement_min(150) = 300 to observe prior-week ranks of
    # rocket-symbols. With the old narrow universe the forward test could
    # never fire a signal.
    with pytest.raises(ValueError, match="rank 300"):
        _validate_demo_config(
            EventDemoCycleConfig(strategy_profile="promoted", universe_rank_end=220, universe_max_symbols=400)
        )
    with pytest.raises(ValueError, match="300"):
        _validate_demo_config(
            EventDemoCycleConfig(strategy_profile="promoted", universe_rank_end=400, universe_max_symbols=220)
        )
    # exact minimum passes
    _validate_demo_config(
        EventDemoCycleConfig(strategy_profile="promoted", universe_rank_end=300, universe_max_symbols=300)
    )


def test_event_demo_rejects_non_positive_entry_leverage() -> None:
    with pytest.raises(ValueError, match="entry_leverage"):
        _validate_demo_config(EventDemoCycleConfig(entry_leverage=0.0))


def test_default_demo_cycle_config_passes_validator_for_every_profile() -> None:
    """Guard: shipping defaults must satisfy the per-profile minimum at all times.

    If a future change tightens `liquidity_migration_rank_improvement_min` or
    `universe_rank_max` without bumping `EventDemoCycleConfig.universe_rank_end`,
    this test catches the drift before the demo VPS silently produces zero
    signals again.
    """
    for profile in DEMO_STRATEGY_PROFILES:
        config = replace(EventDemoCycleConfig(), strategy_profile=profile)
        # If the dataclass defaults don't cover the per-profile minimum the
        # validator raises here.
        _validate_demo_config(config)


def test_required_universe_rank_end_matches_strategy_math() -> None:
    """The validator's derived requirement must equal the strategy-config math.

    This guards against someone hardcoding a number into the validator that
    drifts from the actual strategy field. Both numbers come from the same
    `VolumeEventResearchConfig + profile overrides`, so they must agree.
    """
    for profile in DEMO_STRATEGY_PROFILES:
        strategy = _demo_event_config(VolumeEventResearchConfig(), profile=profile)
        derived = _required_universe_rank_end(profile)
        expected = strategy.universe_rank_max + strategy.liquidity_migration_rank_improvement_min
        assert derived == expected, (profile, derived, expected)


def test_compute_pipeline_diagnostics_reports_zero_gap_for_synthetic_full_coverage() -> None:
    """Diagnostics report zero coverage gap when the universe spans the required range."""
    from liquidity_migration.event_demo import _compute_pipeline_diagnostics, _selected_scenario
    from liquidity_migration.volume_events import _event_score

    strategy = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    scenario = _selected_scenario(strategy)
    _, score_col = _event_score(scenario.event_type)
    required = strategy.universe_rank_max + strategy.liquidity_migration_rank_improvement_min
    features = pl.DataFrame({
        "symbol": ["S1", "S2", "S3"],
        "ts_ms": [0, 0, 0],
        "prior7_liquidity_rank": [1, required // 2, required],
    })
    diagnostics = _compute_pipeline_diagnostics(features, strategy=strategy, scenario=scenario, score_col=score_col)
    coverage = diagnostics["universe_coverage"]
    assert coverage["required_prior7_rank"] == required
    assert coverage["observed_prior7_rank_max"] == required
    assert coverage["coverage_gap"] == 0


def test_compute_pipeline_diagnostics_reports_gap_when_universe_too_narrow() -> None:
    """Diagnostics flag a non-zero coverage gap exactly when prior7 max < required.

    Regression for the 2026-05-24 silent failure: the live demo had 165 symbols
    and prior7_max=165 while promoted requires 300. The cycle JSON reported
    `entries=0` with no clue why. This test pins the telemetry that exposes
    that exact gap so a future regression is loud, not silent.
    """
    from liquidity_migration.event_demo import _compute_pipeline_diagnostics, _selected_scenario
    from liquidity_migration.volume_events import _event_score

    strategy = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    scenario = _selected_scenario(strategy)
    _, score_col = _event_score(scenario.event_type)
    required = strategy.universe_rank_max + strategy.liquidity_migration_rank_improvement_min
    narrow_observed_max = required - 100
    features = pl.DataFrame({
        "symbol": ["S1", "S2"],
        "ts_ms": [0, 0],
        "prior7_liquidity_rank": [1, narrow_observed_max],
    })
    diagnostics = _compute_pipeline_diagnostics(features, strategy=strategy, scenario=scenario, score_col=score_col)
    coverage = diagnostics["universe_coverage"]
    assert coverage["observed_prior7_rank_max"] == narrow_observed_max
    assert coverage["coverage_gap"] == 100


def test_event_demo_max_active_symbols_override() -> None:
    # 0 keeps the strategy profile's value (promoted = 5); a positive value overrides it.
    promoted = _demo_event_config(VolumeEventResearchConfig(), profile="promoted")
    assert promoted.max_active_symbols == 5
    assert EventDemoCycleConfig().max_active_symbols == 0
    assert EventDemoCycleConfig(max_active_symbols=3).max_active_symbols == 3
    _validate_demo_config(EventDemoCycleConfig(strategy_profile="promoted", max_active_symbols=3))
    with pytest.raises(ValueError, match="max_active_symbols"):
        _validate_demo_config(EventDemoCycleConfig(max_active_symbols=-1))


def test_execute_entries_records_preflight_row_before_place_order(tmp_path: Path) -> None:
    """Risk engine relies on event_demo_orders containing a pending row at the
    instant Bybit fills an entry. If the demo engine writes only the post-fill
    row, the risk engine will treat the brand-new position as untracked and
    close it. This test pins the preflight write order: parquet must contain a
    PENDING_ORDER_STATUSES row keyed by order_link_id BEFORE place_order returns.
    """
    from liquidity_migration.event_demo import PENDING_ORDER_STATUSES, _write_order_rows

    observed_at_place_order: dict[str, pl.DataFrame] = {}

    class PreflightInspectingClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def set_leverage(self, **params: object) -> dict[str, str]:
            return {}

        def place_order(self, **params: object) -> dict[str, str]:
            observed_at_place_order["orders"] = read_dataset(tmp_path, "event_demo_orders")
            self.orders.append(params)
            return {"orderId": "order-1"}

        def get_trade_history(self, **_: object) -> list[dict[str, str]]:
            return []

    client = PreflightInspectingClient()
    candidate = {
        "trade_id": "t-preflight",
        "symbol": "AAAUSDT",
        "side": "short",
        "signal_ts_ms": 1_700_000_000_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.20,
    }
    rows, orders = _execute_entries(
        [candidate],
        trading_client=client,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={
            "AAAUSDT": {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        record_preflight=lambda row: _write_order_rows(
            tmp_path, pl.DataFrame([row], infer_schema_length=None)
        ),
    )

    observed = observed_at_place_order["orders"]
    assert not observed.is_empty(), "preflight row must be in parquet before place_order"
    preflight = observed.filter(pl.col("symbol") == "AAAUSDT").to_dicts()
    assert len(preflight) == 1
    assert preflight[0]["status"] in PENDING_ORDER_STATUSES
    assert preflight[0]["submit_mode"] == "preflight"
    assert preflight[0]["reduce_only"] is False
    assert preflight[0]["trade_id"] == "t-preflight"
    assert preflight[0]["order_link_id"].startswith("lm-en-")

    # Final post-loop write upserts the same order_link_id; status must transition
    # away from preflight so pending_entry_symbols drops it once the open trade
    # row is in event_demo_trades.
    assert len(orders) == 1
    assert orders[0]["order_link_id"] == preflight[0]["order_link_id"]
    assert orders[0]["submit_mode"] in {"submitted", "filled", "partial", "submitted_unconfirmed"}


def test_execute_entries_parallel_path_runs_concurrent_candidates() -> None:
    """With max_concurrent_entries > 1 and a private_client_factory, candidates
    fan out across worker threads instead of running serially. Verify by giving
    each candidate's place_order a 100ms sleep: serial would take >300ms for
    three candidates, parallel must finish in roughly one slot.
    """
    import time as _time

    candidates = [
        {
            "trade_id": f"t-par-{i}",
            "symbol": f"AAA{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]
    price_by_symbol = {c["symbol"]: 100.0 for c in candidates}
    contract_by_symbol = {
        c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
        for c in candidates
    }

    class SlowClient(FakeRiskClient):
        def __init__(self):
            super().__init__(fill_market_orders=True, fill_order_prefixes=("lm-en-",))

        def place_order(self, **params):
            _time.sleep(0.1)
            return super().place_order(**params)

    factory_calls: list[int] = []
    def factory() -> SlowClient:
        factory_calls.append(1)
        return SlowClient()

    started = _time.monotonic()
    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=3,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol=price_by_symbol,
        contract_by_symbol=contract_by_symbol,
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=factory,
    )
    elapsed = _time.monotonic() - started

    assert len(orders) == 3
    assert elapsed < 0.25, (
        f"parallel path should finish under 250ms with 100ms place_order x 3 "
        f"workers; took {elapsed:.3f}s"
    )
    assert len(factory_calls) == 3


def test_execute_entries_falls_back_to_serial_when_submit_orders_off() -> None:
    """If submit_orders=False the parallel path is bypassed regardless of
    max_concurrent_entries (no live execution to fan out)."""
    candidates = [
        {
            "trade_id": f"t-fb-{i}",
            "symbol": f"BBB{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]
    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0, max_concurrent_entries=4),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )
    assert [o["symbol"] for o in orders] == ["BBB0USDT", "BBB1USDT", "BBB2USDT"]
    assert all(o["submit_mode"] == "dry_run" for o in orders)


def test_execute_entries_parallel_records_preflight_for_every_candidate(tmp_path: Path) -> None:
    """The preflight callback must fire once per candidate even on the parallel
    path, and the resulting parquet must contain a preflight row for each
    order_link_id BEFORE place_order returns for that candidate. Pins the
    contract between fix #1 (close-on-open preflight) and speed #1 (parallel).
    """
    import threading as _threading
    from liquidity_migration.event_demo import _write_order_rows

    candidates = [
        {
            "trade_id": f"t-pp-{i}",
            "symbol": f"PRE{i}USDT",
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i in range(3)
    ]

    place_order_started = _threading.Event()

    class PreflightAwareClient:
        def __init__(self) -> None:
            self.orders: list[dict[str, object]] = []

        def set_leverage(self, **_kwargs) -> dict[str, str]:
            return {}

        def place_order(self, **params) -> dict[str, str]:
            place_order_started.set()
            self.orders.append(params)
            return {"orderId": f"order-{params.get('orderLinkId')}"}

        def get_trade_history(self, **_kwargs) -> list[dict[str, str]]:
            return []

    def _record_preflight(row: dict[str, object]) -> None:
        _write_order_rows(tmp_path, pl.DataFrame([row], infer_schema_length=None))

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=3,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        record_preflight=_record_preflight,
        private_client_factory=PreflightAwareClient,
    )

    stored = read_dataset(tmp_path, "event_demo_orders").sort("order_link_id")
    preflights = stored.filter(pl.col("submit_mode") == "preflight").to_dicts()
    # Three preflights written, one per candidate.
    assert len(preflights) == 3
    assert {p["symbol"] for p in preflights} == {"PRE0USDT", "PRE1USDT", "PRE2USDT"}
    assert all(p["status"] in {"submitted", "submitted_unconfirmed", "partial", "fallback_market"} for p in preflights)
    # Final returned orders match candidate order (deterministic).
    assert [o["symbol"] for o in orders] == ["PRE0USDT", "PRE1USDT", "PRE2USDT"]


def test_execute_entries_parallel_isolates_place_order_failure(tmp_path: Path) -> None:
    """One candidate failing place_order must NOT abort the cycle for the others
    when running in parallel. Each ledgered as its own row with its own status.
    """
    candidates = [
        {
            "trade_id": f"t-iso-{i}",
            "symbol": symbol,
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i, symbol in enumerate(["BADUSDT", "OKUSDT"])
    ]

    class SelectiveClient(FakeRiskClient):
        def __init__(self):
            super().__init__(
                fill_market_orders=True,
                fill_order_prefixes=("lm-en-",),
                fail_order_symbols={"BADUSDT"},
            )

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=2,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=SelectiveClient,
    )

    by_symbol = {o["symbol"]: o for o in orders}
    # place_order exception -> submitted_unconfirmed (pending) for reconciliation.
    assert by_symbol["BADUSDT"]["status"] == "submitted_unconfirmed"
    assert "place_order failed" in by_symbol["BADUSDT"]["error"]
    assert by_symbol["OKUSDT"]["status"] in {"filled", "partial", "submitted_unconfirmed"}
    assert by_symbol["OKUSDT"]["error"] == ""


def test_execute_entries_parallel_isolates_set_leverage_failure(tmp_path: Path) -> None:
    """Same isolation but for set_leverage failures: one candidate's leverage
    rejection must not bleed into another worker's path."""
    candidates = [
        {
            "trade_id": f"t-lev-{i}",
            "symbol": symbol,
            "side": "short",
            "signal_ts_ms": 1_700_000_000_000 + i,
            "stop_loss_pct": 0.12,
            "take_profit_pct": 0.20,
        }
        for i, symbol in enumerate(["LEVBADUSDT", "LEVOKUSDT"])
    ]

    class LeverageFlakyClient(FakeRiskClient):
        def __init__(self):
            super().__init__(
                fill_market_orders=True,
                fill_order_prefixes=("lm-en-",),
                fail_leverage_symbols={"LEVBADUSDT"},
            )

    rows, orders = _execute_entries(
        candidates,
        trading_client=None,
        demo=EventDemoCycleConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            order_fill_confirm_seconds=0.0,
            max_concurrent_entries=2,
        ),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={c["symbol"]: 100.0 for c in candidates},
        contract_by_symbol={
            c["symbol"]: {"tick_size": 0.1, "qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}
            for c in candidates
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
        private_client_factory=LeverageFlakyClient,
    )

    by_symbol = {o["symbol"]: o for o in orders}
    assert by_symbol["LEVBADUSDT"]["status"] == "failed"
    assert "set_leverage failed" in by_symbol["LEVBADUSDT"]["error"]
    assert by_symbol["LEVOKUSDT"]["status"] in {"filled", "partial", "submitted_unconfirmed"}


def test_wait_for_execution_summary_fast_window_then_slow_interval() -> None:
    """Until fast_poll_seconds elapses, polls happen every ~fast_poll_interval;
    after, they fall back to poll_interval. Verify by counting calls in the
    fast window and the slow window against a client that never returns a fill.
    """
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary

    call_times: list[float] = []

    class CountingClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            call_times.append(_time.monotonic())
            return []

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        CountingClient(),
        symbol="AAAUSDT",
        order_link_id="lm-test-poll",
        poll_seconds=0.6,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.3,
    )
    elapsed = _time.monotonic() - started

    assert float(summary.get("qty") or 0.0) == 0.0, "no fills -> no qty at deadline"
    # ~0.6 seconds total wallclock.
    assert 0.55 < elapsed < 1.0, f"expected ~0.6s wallclock, got {elapsed:.3f}s"
    # Fast window is 0.3s @ 0.05s = ~6 calls; slow window is 0.3s @ 0.2s = ~2 calls.
    # Allow ±2 jitter for scheduler latency on macOS CI.
    fast_window_calls = sum(1 for t in call_times if t - started < 0.3)
    slow_window_calls = sum(1 for t in call_times if t - started >= 0.3)
    assert 4 <= fast_window_calls <= 8, f"fast window expected 4-8 calls, got {fast_window_calls}"
    assert 1 <= slow_window_calls <= 4, f"slow window expected 1-4 calls, got {slow_window_calls}"


def test_wait_for_execution_summary_uses_ws_router_when_available() -> None:
    """When an ExecutionEventRouter is supplied AND has a fill for this
    orderLinkId, _wait_for_execution_summary returns within ms — the REST
    get_trade_history path is bypassed entirely on the fast path.
    """
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary
    from liquidity_migration.execution_router import ExecutionEventRouter

    router = ExecutionEventRouter()
    router.on_execution_event(
        {"data": [{"orderLinkId": "lm-en-WSAAA", "execQty": "1", "execPrice": "101", "execValue": "101", "execFee": "0.05"}]}
    )

    rest_calls: list[str | None] = []

    class FailingRestClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            rest_calls.append(order_link_id)
            raise AssertionError("REST must not be hit when WS already has the fill")

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        FailingRestClient(),
        symbol="AAAUSDT",
        order_link_id="lm-en-WSAAA",
        poll_seconds=5.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
        execution_event_router=router,
    )
    elapsed = _time.monotonic() - started

    assert float(summary["qty"] or 0) == 1.0
    assert summary["avg_price"] == 101.0
    assert elapsed < 0.05, f"WS fast-path should return immediately, took {elapsed:.3f}s"
    assert rest_calls == []


def test_wait_for_execution_summary_falls_back_to_rest_when_router_empty() -> None:
    """If the router is supplied but doesn't have a fill within the WS short
    wait, the function falls back to REST polling exactly as it would without
    the router. Guarantees WS is a fast path, never the only path."""
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary
    from liquidity_migration.execution_router import ExecutionEventRouter

    router = ExecutionEventRouter()  # No events delivered

    call_count = {"n": 0}

    class RestFillsAfterTwoCalls:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return [{"execQty": "1", "execPrice": "102", "execValue": "102", "execFee": "0.05"}]
            return []

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        RestFillsAfterTwoCalls(),
        symbol="AAAUSDT",
        order_link_id="lm-en-WSBB",
        poll_seconds=2.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
        execution_event_router=router,
    )
    elapsed = _time.monotonic() - started

    assert float(summary["qty"] or 0) == 1.0
    assert summary["avg_price"] == 102.0
    assert call_count["n"] >= 2
    assert elapsed < 0.5, f"REST fallback should still be reasonably fast, took {elapsed:.3f}s"


def test_wait_for_execution_summary_returns_immediately_on_fill() -> None:
    """A fill landing on the first poll must return without burning the rest of
    the poll budget."""
    import time as _time
    from liquidity_migration.event_demo import _wait_for_execution_summary

    class InstantFillClient:
        def get_trade_history(self, *, symbol, order_link_id, limit=50):
            return [
                {
                    "execQty": "1",
                    "execPrice": "100",
                    "execValue": "100",
                    "execFee": "0.1",
                }
            ]

    started = _time.monotonic()
    summary = _wait_for_execution_summary(
        InstantFillClient(),
        symbol="AAAUSDT",
        order_link_id="lm-test-instant",
        poll_seconds=5.0,
        poll_interval_seconds=0.2,
        fast_poll_interval_seconds=0.05,
        fast_poll_seconds=0.5,
    )
    elapsed = _time.monotonic() - started

    assert float(summary.get("qty") or 0.0) == 1.0
    assert elapsed < 0.05, f"instant fill should return immediately, took {elapsed:.3f}s"


def test_prune_cycle_reports_drops_files_older_than_keep_days(tmp_path: Path) -> None:
    """Old snapshots beyond keep_days must be unlinked; latest pointer kept."""
    now_ms = 1_700_000_000_000
    keep_days = 7
    old_age_seconds = (keep_days + 1) * 86400
    fresh_age_seconds = 3600  # 1 hour old, well within window

    old_file = tmp_path / "long_native_cycle_OLD.json"
    fresh_file = tmp_path / "long_native_cycle_FRESH.json"
    pointer = tmp_path / "latest_long_native_cycle.json"
    for path in (old_file, fresh_file, pointer):
        path.write_text("{}", encoding="utf-8")

    now_s = now_ms / 1000.0
    import os
    os.utime(old_file, (now_s - old_age_seconds, now_s - old_age_seconds))
    os.utime(fresh_file, (now_s - fresh_age_seconds, now_s - fresh_age_seconds))

    _prune_cycle_reports(
        tmp_path, prefix="long_native_cycle_", keep_days=keep_days, now_ms=now_ms,
    )

    assert not old_file.exists()
    assert fresh_file.exists()
    assert pointer.exists(), "latest_*.json pointer must not match the per-cycle prefix"


def test_prune_cycle_reports_amortizes_via_hourly_sentinel(tmp_path: Path) -> None:
    """Second call within an hour must be a no-op — even if a new old file
    appears, the sentinel skips the scan. Prevents the 5500-stat-syscalls-per-
    cycle waste from re-emerging in the long sleeve."""
    import os
    import time as _time

    real_now_ms = int(_time.time() * 1000)
    stale_s = real_now_ms / 1000.0 - 30 * 86400

    first_old = tmp_path / "long_native_cycle_OLD1.json"
    first_old.write_text("{}", encoding="utf-8")
    os.utime(first_old, (stale_s, stale_s))

    _prune_cycle_reports(
        tmp_path, prefix="long_native_cycle_", keep_days=7, now_ms=real_now_ms,
    )
    assert not first_old.exists()
    sentinel = tmp_path / ".long_native_cycle_prune_sentinel"
    assert sentinel.exists(), "first prune must touch the sentinel"

    second_old = tmp_path / "long_native_cycle_OLD2.json"
    second_old.write_text("{}", encoding="utf-8")
    os.utime(second_old, (stale_s, stale_s))

    _prune_cycle_reports(
        tmp_path, prefix="long_native_cycle_", keep_days=7, now_ms=real_now_ms + 60_000,
    )
    assert second_old.exists(), (
        "second prune within the hour must skip the scan even if new old files appeared"
    )

    # Advance the sentinel mtime backwards by >1h so the gate releases. Cleaner
    # than waiting an hour in a unit test.
    past_mtime = (real_now_ms / 1000.0) - 3601
    os.utime(sentinel, (past_mtime, past_mtime))

    _prune_cycle_reports(
        tmp_path, prefix="long_native_cycle_", keep_days=7, now_ms=real_now_ms + 60_000,
    )
    assert not second_old.exists(), "prune must run again after the hour expires"


def test_decode_entry_order_link_id_roundtrips_short_signal_ts() -> None:
    """An orderLinkId produced by _order_link_id must decode back to the
    same signal_ts_ms (within 1s — base36 encoding drops sub-second
    resolution). This is the round-trip guarantee that the rebuild-safe
    adoption recovery in ws_risk relies on."""
    from liquidity_migration.event_demo import _order_link_id
    signal_ts_ms = 1_779_667_200_000
    link = _order_link_id("en", symbol="SUPERUSDT", signal_ts_ms=signal_ts_ms)
    decoded = decode_entry_order_link_id(link)
    assert decoded == ("short", 1_779_667_200_000)


def test_decode_entry_order_link_id_roundtrips_long_signal_ts() -> None:
    """Long-sleeve entry links carry an extra '-l' segment between 'en' and
    the symbol base. Decoder must recognize both sleeves so a recovered long
    position rebuilds with the long_native strategy_id, not the short one."""
    from liquidity_migration.long_native_event_demo import _long_order_link_id, LONG_ENTRY_LINK_PREFIX
    signal_ts_ms = 1_779_667_200_000
    link = _long_order_link_id(LONG_ENTRY_LINK_PREFIX, symbol="ETHUSDT", signal_ts_ms=signal_ts_ms)
    decoded = decode_entry_order_link_id(link)
    assert decoded == ("long", 1_779_667_200_000)


def test_decode_entry_order_link_id_returns_none_for_unknown_patterns() -> None:
    """Hand-placed orders, risk-side exits (lm-ux-*), and legacy formats
    must NOT decode — the caller relies on None to mean 'fall back to the
    adopted-* lossy path' rather than synthesizing a wrong signal_ts."""
    assert decode_entry_order_link_id("") is None
    assert decode_entry_order_link_id("lm-ux-SUPER-abc123") is None  # exit link, not entry
    assert decode_entry_order_link_id("lm-en-SUPER") is None  # missing ts
    assert decode_entry_order_link_id("lm-en-SUPER-abc-xyz-extra") is None  # too many parts
    assert decode_entry_order_link_id("manual-order-id") is None
    assert decode_entry_order_link_id("lm-en-l-SUPER-not_base36!") is None  # invalid base36


def test_execute_entries_preflight_skipped_when_no_callback() -> None:
    """Callers that don't pass record_preflight (e.g. dry-run unit tests) must
    behave exactly as before — no parquet writes attempted, no exceptions."""
    rows, orders = _execute_entries(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "signal_ts_ms": 1_700_000_000_000,
                "stop_loss_pct": 0.12,
                "take_profit_pct": 0.20,
            }
        ],
        trading_client=None,
        demo=EventDemoCycleConfig(entry_leverage=2.0),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.20,
        price_by_symbol={"AAAUSDT": 100.0},
        contract_by_symbol={"AAAUSDT": {"qty_step": 0.1, "min_order_qty": 0.1, "min_notional_value": 5.0}},
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )
    assert orders[0]["submit_mode"] == "dry_run"
    assert rows[0]["submit_mode"] == "dry_run"
