from __future__ import annotations

import polars as pl
import pytest

from aggression_carry.cli import build_parser
from aggression_carry.event_demo import (
    EventDemoCycleConfig,
    _validate_demo_config,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_telegram_status_message,
    order_quantity_for_notional,
    plan_demo_exits,
    select_demo_entry_candidates,
    summarize_position_pnl,
    target_order_notional_pct_equity,
    wallet_equity_usdt,
)
from aggression_carry.volume_features import MS_PER_HOUR
from aggression_carry.volume_events import EventScenario, VolumeEventResearchConfig


def test_event_demo_cli_defaults_to_frequent_demo_forward_cycle() -> None:
    args = build_parser().parse_args(["event-demo-cycle"])

    assert args.command == "event-demo-cycle"
    assert args.lookback_days == 45
    assert args.universe_rank_end == 220
    assert args.universe_max_symbols == 220
    assert args.max_order_notional_pct_equity == 0.0
    assert args.max_entry_lag_minutes == 15
    assert args.max_new_entries_per_cycle == 6
    assert args.submit_orders is False
    assert args.confirm_demo_orders is False


def test_event_demo_default_sizing_matches_backtest_weight() -> None:
    assert target_order_notional_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == 1.0 / 6.0
    assert (
        target_order_notional_pct_equity(
            EventDemoCycleConfig(max_order_notional_pct_equity=0.10),
            VolumeEventResearchConfig(),
        )
        == 0.10
    )


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
        hold_days=1,
        stop_loss_pct=0.12,
        cost_multiplier=3.0,
    )
    config = VolumeEventResearchConfig(require_pit_membership=False, require_full_pit_universe=False)
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


def test_submit_orders_requires_explicit_confirmation() -> None:
    config = EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=False)

    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        _validate_demo_config(config)
