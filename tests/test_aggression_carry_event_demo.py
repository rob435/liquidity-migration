from __future__ import annotations

import polars as pl
import pytest

from aggression_carry.cli import build_parser
from aggression_carry.config import ResearchConfig
from aggression_carry.event_demo import (
    EventDemoCycleConfig,
    EventRiskCycleConfig,
    _execute_entries,
    _execute_exits,
    _execute_risk_exits,
    _limit_chase_price,
    _maybe_notify,
    _reconcile_pending_order_fills,
    _submit_reduce_only_exit,
    _telegram_notification_reason,
    _validate_demo_config,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_telegram_status_message,
    order_quantity_for_notional,
    plan_demo_exits,
    plan_risk_exits,
    plan_stop_repairs,
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
    ) -> None:
        self.positions = positions or []
        self.fill_market_orders = fill_market_orders
        self.fill_order_prefixes = fill_order_prefixes
        self.fill_qty = fill_qty
        self.fill_price = fill_price
        self.orders: list[dict[str, object]] = []
        self.stop_updates: list[dict[str, object]] = []
        self.leverage_updates: list[dict[str, object]] = []
        self.trade_history_calls: list[str | None] = []

    def get_positions(self, *, settle_coin: str | None = None) -> list[dict[str, str]]:
        return self.positions

    def place_order(self, **params: object) -> dict[str, str]:
        self.orders.append(params)
        return {"orderId": f"order-{len(self.orders)}"}

    def get_trade_history(self, *, symbol: str | None = None, order_link_id: str | None = None, limit: int = 50) -> list[dict[str, str]]:
        self.trade_history_calls.append(order_link_id)
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
        self.leverage_updates.append(params)
        return {}


def test_event_demo_cli_defaults_to_frequent_demo_forward_cycle() -> None:
    args = build_parser().parse_args(["event-demo-cycle"])

    assert args.command == "event-demo-cycle"
    assert args.lookback_days == 45
    assert args.universe_rank_end == 220
    assert args.universe_max_symbols == 220
    assert args.max_order_notional_pct_equity == 0.0
    assert args.max_entry_lag_minutes == 15
    assert args.max_new_entries_per_cycle == 6
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
    assert args.fast_execution_stream is False
    assert args.submit_orders is False
    assert args.confirm_demo_orders is False


def test_event_demo_default_sizing_matches_backtest_weight() -> None:
    assert target_order_notional_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == 1.25 / 6.0
    assert target_initial_margin_pct_equity(EventDemoCycleConfig(), VolumeEventResearchConfig()) == pytest.approx(
        1.25 / 6.0 / 2.0
    )
    assert (
        target_order_notional_pct_equity(
            EventDemoCycleConfig(max_order_notional_pct_equity=0.10),
            VolumeEventResearchConfig(),
        )
        == 0.10
    )


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
    )

    assert rows[0]["qty"] == "1"
    assert rows[0]["entry_price"] == 100.5
    assert orders[0]["status"] == "partial"
    assert orders[0]["notional_usdt"] == 100.5


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
    assert submit["order_rows"][-1]["status"] == "fallback_market"


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


def test_event_demo_rejects_non_positive_entry_leverage() -> None:
    with pytest.raises(ValueError, match="entry_leverage"):
        _validate_demo_config(EventDemoCycleConfig(entry_leverage=0.0))
