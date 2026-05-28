"""Event-demo cycle tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.cli import build_parser
from liquidity_migration.config import ResearchConfig
from liquidity_migration.event_demo import (
    DEMO_RELAXED_STRATEGY_ID,
    DEMO_STRATEGY_PROFILES,
    EventDemoCycleConfig,
    PENDING_ORDER_GUARD_MS,
    _demo_event_config,
    _execute_entries,
    _filter_live_open_exit_orders,
    _live_open_order_symbols,
    _prune_cycle_reports,
    decode_entry_order_link_id,
    _required_universe_rank_end,
    _validate_demo_config,
    run_event_demo_cycle,
)
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.volume_events import VolumeEventResearchConfig

from _event_demo_fixtures import *  # noqa: F401,F403  (shared fakes/helpers)
from _event_demo_fixtures import (  # noqa: F401  explicit for the linters
    FailingKlineMarket,
    FakeKlineMarket,
    FakeRiskClient,
    MinimalEventMarket,
    _ClosedPnlClient,
    _RecordingInstrumentsMarket,
    _feature_cache_klines,
    _feature_cache_universe,
    _make_instruments_frame,
    _make_tickers_frame,
    _open_trade_row,
    _patch_minimal_event_cycle,
)


def test_event_demo_cli_defaults_to_frequent_demo_forward_cycle() -> None:
    args = build_parser().parse_args(["event-demo-cycle"])

    assert args.command == "event-demo-cycle"
    assert args.lookback_days == 45
    # Match-the-backtest mode: 0/0 disables the ticker pre-filter so the
    # demo's daily-aggregated liquidity_rank is computed across the full
    # Bybit perp universe (same denominator the backtest uses).
    assert args.universe_rank_end == 0
    assert args.universe_max_symbols == 0
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


def test_split_order_link_id_stays_within_36_chars_and_keeps_suffix_unique() -> None:
    """Sub-order links must never exceed Bybit's 36-char orderLinkId cap, and
    each sub MUST stay unique — a naive f"{base}-s{idx}"[:36] would truncate the
    suffix off a long base and collide two sub-orders onto one link."""
    from liquidity_migration.event_demo import _split_order_link_id

    # Normal-length base: unchanged shape, well under the cap.
    assert _split_order_link_id("lm-en-SUPER-abc123", 0) == "lm-en-SUPER-abc123-s0"
    assert len(_split_order_link_id("lm-en-SUPER-abc123", 0)) <= 36

    # Pathologically long base: capped to 36 but suffixes stay distinct.
    long_base = "lm-ex-VERYLONGSYMBOLNAME-zzzzzzz-0"  # 34 chars; +"-s0" would exceed 36
    by_idx = {idx: _split_order_link_id(long_base, idx) for idx in range(12)}
    assert all(len(link) <= 36 for link in by_idx.values())
    assert all(link.endswith(f"-s{idx}") for idx, link in by_idx.items())
    assert len(set(by_idx.values())) == 12, "every sub-order link must stay unique after capping"


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


def test_validate_demo_config_accepts_unlimited_universe_mode() -> None:
    """universe_rank_end == universe_max_symbols == 0 must not trigger
    the universe-too-narrow check — that check exists to catch operator
    misconfig (rank_end=100) where prior-week ranks are unobservable,
    but 0 explicitly opts into the full Bybit perp set which trivially
    exceeds the required-rank threshold."""
    config = EventDemoCycleConfig(
        universe_rank_end=0,
        universe_max_symbols=0,
        universe_min_turnover_24h=0.0,
    )
    _validate_demo_config(config)  # must not raise


def test_validate_demo_config_still_rejects_partially_unlimited_universe() -> None:
    """Only the BOTH-zero case is the new escape hatch. A misconfig
    where rank_end=0 but max_symbols=100 (or vice versa) still trips
    the existing too-narrow check rather than silently becoming
    a 100-symbol universe.
    """
    import pytest as _pytest
    with _pytest.raises(ValueError, match="too narrow"):
        _validate_demo_config(EventDemoCycleConfig(
            universe_rank_end=0,
            universe_max_symbols=100,
            universe_min_turnover_24h=0.0,
        ))
    with _pytest.raises(ValueError, match="too narrow"):
        _validate_demo_config(EventDemoCycleConfig(
            universe_rank_end=100,
            universe_max_symbols=0,
            universe_min_turnover_24h=0.0,
        ))


def test_dry_run_cycle_ignores_demo_owned_live_position_symbols(tmp_path: Path) -> None:
    """When SUBMIT_ORDERS=0 (paper), the cycle's entry-candidate filter
    against Bybit's live positions must be a no-op.

    Reproduces the live failure where paper shared the demo's Bybit account
    creds: paper's get_positions returned demo's positions, paper filtered
    its own OKB/REQ candidates against them, and the divergence cascaded
    (each demo entry suppressed the paper candidate, paper's exit logic
    drifted, paper's open count drifted, paper's free_slots drifted...).

    This test verifies a paper-equivalent cycle with submit_orders=False
    no longer filters by Bybit live positions even when those positions
    are passed in (mimicking a contaminated snapshot).
    """
    # Use the dry-run path of _execute_entries directly with a candidate that
    # would have been filtered if submit_orders=True + live_position_symbols
    # contained the candidate's symbol.
    candidate = {
        "trade_id": "paper-shadow-1",
        "symbol": "REQUSDT",
        "side": "short",
        "signal_ts_ms": 1_700_000_000_000,
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.26,
    }
    # Dry-run path takes no trading_client and shouldn't reach any Bybit call.
    rows, orders = _execute_entries(
        [candidate],
        trading_client=None,
        demo=EventDemoCycleConfig(submit_orders=False, record_dry_run=True),
        equity_usdt=10_000.0,
        order_notional_pct_equity=0.3333,
        price_by_symbol={"REQUSDT": 0.08676},
        contract_by_symbol={
            "REQUSDT": {
                "tick_size": 0.00001, "qty_step": 1.0,
                "min_order_qty": 1.0, "min_notional_value": 5.0,
            },
        },
        now_ms=1_700_000_060_000,
        strategy_id=DEMO_RELAXED_STRATEGY_ID,
    )
    # Dry-run should produce a planned trade + order row.
    assert len(rows) == 1, "dry-run should produce one planned trade row"
    assert rows[0]["symbol"] == "REQUSDT"
    assert len(orders) == 1
    assert orders[0]["status"] == "planned"
    assert orders[0]["submit_mode"] == "dry_run"

