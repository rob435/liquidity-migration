from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from aggression_carry.cli import build_parser
from aggression_carry.config import ResearchConfig
from aggression_carry.event_demo import (
    OBSERVE_DEMO_STRATEGY_ID,
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    PENDING_ORDER_GUARD_MS,
    _demo_event_config,
    _execute_entries,
    _execute_exits,
    _execute_risk_exits,
    _filter_live_open_exit_orders,
    _download_recent_1h_klines,
    _limit_chase_price,
    _live_open_order_symbols,
    _maybe_notify,
    _reconcile_pending_order_fills,
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
)
from aggression_carry.storage import read_dataset, write_dataset
from aggression_carry.volume_features import MS_PER_HOUR
from aggression_carry.volume_events import EventScenario, VolumeEventResearchConfig


class FakeRiskClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, str]] | None = None,
        fill_market_orders: bool = False,
        fill_order_prefixes: tuple[str, ...] = ("agc-lm-",),
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
        "aggression_carry.event_demo._build_demo_universe",
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
        "aggression_carry.event_demo._download_recent_1h_klines",
        lambda *args, **kwargs: (
            pl.DataFrame([{"symbol": "AAAUSDT", "ts_ms": 1_700_000_000_000, "close": 100.0}]),
            {"cache_rows": 0, "cache_symbols": 0, "fetch_symbols": 0, "fetched_rows": 0, "output_rows": 1},
        ),
    )
    monkeypatch.setattr(
        "aggression_carry.event_demo._build_demo_features",
        lambda klines, universe=None: pl.DataFrame([{"symbol": "AAAUSDT"}]),
    )
    monkeypatch.setattr(
        "aggression_carry.event_demo.select_demo_entry_candidates",
        lambda *args, **kwargs: ([candidate], {}),
    )


def test_event_demo_cli_defaults_to_frequent_demo_forward_cycle() -> None:
    args = build_parser().parse_args(["event-demo-cycle"])

    assert args.command == "event-demo-cycle"
    assert args.lookback_days == 45
    assert args.universe_rank_end == 220
    assert args.universe_max_symbols == 220
    assert args.max_order_notional_pct_equity == 0.0
    assert args.max_entry_lag_minutes == 15
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
    assert args.exit_untracked_positions is True
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
        fill_order_prefixes=("agc-ex-",),
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
                "orderLinkId": "agc-en-existing",
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
                "orderLinkId": "agc-ex-existing",
                "orderStatus": "New",
                "reduceOnly": True,
            },
            {
                "symbol": "CCCUSDT",
                "orderLinkId": "agc-ex-filled",
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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",))

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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
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
        fill_order_prefixes=("agc-en-",),
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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
    )

    assert [row["trade_id"] for row in rows] == ["t2"]
    assert [row["symbol"] for row in orders] == ["AAAUSDT", "BBBUSDT"]
    assert orders[0]["status"] == "failed"
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
        strategy_id=OBSERVE_DEMO_STRATEGY_ID,
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

    monkeypatch.setattr("aggression_carry.event_demo.send_telegram_message", fake_send)
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
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-ex-",), fill_qty="0.4"),
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
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-ex-",)),
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
                "order_link_id": "agc-en-pending",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",))

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
                "order_link_id": "agc-en-pending",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",))

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
                "order_link_id": "agc-en-pending",
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
    assert order_updates[0]["order_link_id"] == "agc-en-pending"
    assert "fill reconciliation failed" in order_updates[0]["error"]
    assert "history unavailable" in order_updates[0]["error"]


def test_stale_pending_order_fill_is_not_polled_forever() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "agc-en-stale",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",))

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
                "order_link_id": "agc-en-stale-live",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",))

    trades, order_updates = _reconcile_pending_order_fills(
        orders,
        pl.DataFrame(),
        trading_client=client,
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=1_700_000_000_000 + 16 * 60_000,
        live_position_symbols={"AAAUSDT"},
    )

    assert client.trade_history_calls == ["agc-en-stale-live"]
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
                "order_link_id": "agc-en-stale-flat",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS - 1,
                "trade_id": "t-flat",
                "symbol": "AAAUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "agc-en-fresh",
                "ts_ms": now_ms - PENDING_ORDER_GUARD_MS,
                "trade_id": "t-fresh",
                "symbol": "BBBUSDT",
                "side": "Sell",
                "qty": "1",
                "reduce_only": False,
                "status": "submitted_unconfirmed",
            },
            {
                "order_link_id": "agc-ex-stale",
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

    assert [row["order_link_id"] for row in updates] == ["agc-en-stale-flat"]
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
                    "order_link_id": "agc-en-stale-flat",
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
    monkeypatch.setattr("aggression_carry.event_demo.select_demo_entry_candidates", lambda *args, **kwargs: ([], {}))
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
    assert orders.filter(pl.col("order_link_id") == "agc-en-stale-flat").select("status").item() == "expired_unconfirmed"
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
                    "order_link_id": "agc-en-stale-live",
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
    monkeypatch.setattr("aggression_carry.event_demo.select_demo_entry_candidates", lambda *args, **kwargs: ([], {}))
    client = FakeRiskClient(
        fill_market_orders=True,
        fill_order_prefixes=("agc-en-",),
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
    assert client.trade_history_calls == ["agc-en-stale-live"]
    assert client.orders == []
    assert trade["status"] == "open"
    assert trade["qty"] == "1"
    assert orders.filter(pl.col("order_link_id") == "agc-en-stale-live").select("status").item() == "filled"
    assert payload["cycle"]["pending_entry_fills_reconciled"] == 1
    assert payload["cycle"]["stale_pending_entry_orders_terminalized"] == 0


def test_pending_exit_fill_reconciles_to_closed_trade() -> None:
    orders = pl.DataFrame(
        [
            {
                "order_link_id": "agc-ex-pending",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-ex-",))

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
                "order_link_id": "agc-ex-pending",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-ex-",), fill_qty="0.4")

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
                "order_link_id": "agc-en-pending",
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
    client = FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-en-",), fill_qty="0.7")

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
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-rx-",), fill_qty="0.4"),
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
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-lc-",), fill_qty="0.4"),
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
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("agc-rx-",)),
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


def test_observe_profile_lowers_gates_for_more_demo_trades() -> None:
    strategy = _demo_event_config(VolumeEventResearchConfig(), profile="observe")

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


def test_observe_profile_requires_wide_forward_universe() -> None:
    with pytest.raises(ValueError, match="rank 260"):
        _validate_demo_config(
            EventDemoCycleConfig(strategy_profile="observe", universe_rank_end=220, universe_max_symbols=220)
        )


def test_event_demo_rejects_non_positive_entry_leverage() -> None:
    with pytest.raises(ValueError, match="entry_leverage"):
        _validate_demo_config(EventDemoCycleConfig(entry_leverage=0.0))
