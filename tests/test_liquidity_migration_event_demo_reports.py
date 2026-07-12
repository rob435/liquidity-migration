"""Event-demo reports tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations


import polars as pl
import pytest

from liquidity_migration.event_demo import (
    _maybe_notify,
    _telegram_notification_reason,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_position_loss_alert,
    format_telegram_status_message,
    position_loss_alert_levels,
    position_loss_alerts,
    summarize_position_pnl,
    wallet_equity_usdt,
)

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
            "stop_price": 0.0,
            "take_profit_price": 0.0,
            "liquidation_price": 0.0,
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


def test_ledger_position_snapshot_nets_component_rows_like_bybit() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "continuous_fade_v2-ICNTUSDT-1-p3",
                "symbol": "ICNTUSDT",
                "side": "short",
                "status": "open",
                "qty": "188",
                "entry_price": 0.1779,
                "component": "p3",
            },
            {
                "trade_id": "continuous_fade_v2-ICNTUSDT-1-p4p5",
                "symbol": "ICNTUSDT",
                "side": "short",
                "status": "open",
                "qty": "125",
                "entry_price": 0.1779,
                "component": "p4p5",
            },
        ]
    )

    positions = build_ledger_position_pnl_snapshot(
        open_trades,
        {"ICNTUSDT": 0.1790},
        position_by_symbol={"ICNTUSDT": {"markPrice": "0.1795"}},
    )
    summary = summarize_position_pnl(positions)

    assert len(positions) == 1
    assert positions[0]["symbol"] == "ICNTUSDT"
    assert positions[0]["side"] == "short"
    assert positions[0]["qty"] == pytest.approx(313.0)
    assert positions[0]["avg_price"] == pytest.approx(0.1779)
    assert positions[0]["mark_price"] == pytest.approx(0.1795)
    assert positions[0]["position_value_usdt"] == pytest.approx(0.1795 * 313.0)
    assert positions[0]["unrealized_pnl_usdt"] == pytest.approx((0.1779 - 0.1795) * 313.0)
    assert positions[0]["ledger_rows"] == 2
    assert summary["positions"] == 1


def test_ledger_position_snapshot_uses_weighted_average_entry_for_netted_rows() -> None:
    open_trades = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "side": "Sell", "qty": "2", "entry_price": 100.0, "status": "open"},
            {"symbol": "AAAUSDT", "side": "Sell", "qty": "1", "entry_price": 103.0, "status": "open"},
        ]
    )

    positions = build_ledger_position_pnl_snapshot(open_trades, {"AAAUSDT": 99.0})

    assert len(positions) == 1
    assert positions[0]["side"] == "short"
    assert positions[0]["qty"] == pytest.approx(3.0)
    assert positions[0]["avg_price"] == pytest.approx(101.0)
    assert positions[0]["unrealized_pnl_usdt"] == pytest.approx(6.0)


def test_telegram_event_message_does_not_dump_component_ledger_rows() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_783_275_129_129,
            "mode": "ws_risk_submit",
            "equity_usdt": 10_038.23,
            "entries_executed": 0,
            "entry_candidates": 0,
            "exits_executed": 0,
            "exit_candidates": 4,
            "pending_order_fills_reconciled": 0,
            "position_report_error": "",
        },
        "bybit_position_summary": {
            "positions": 1,
            "position_value_usdt": 56.18,
            "unrealized_pnl_usdt": -0.50,
            "pnl_pct": -0.0089,
        },
        "bybit_positions": [
            {
                "symbol": "ICNTUSDT",
                "side": "short",
                "qty": 313.0,
                "avg_price": 0.1779,
                "mark_price": 0.1795,
                "position_value_usdt": 56.18,
                "unrealized_pnl_usdt": -0.50,
                "pnl_pct": -0.0089,
            }
        ],
        "ledger_position_summary": {
            "positions": 1,
            "position_value_usdt": 56.18,
            "unrealized_pnl_usdt": -0.50,
            "pnl_pct": -0.0089,
        },
        "ledger_positions": [
            {
                "symbol": "ICNTUSDT",
                "side": "short",
                "qty": 313.0,
                "avg_price": 0.1779,
                "mark_price": 0.1795,
                "position_value_usdt": 56.18,
                "unrealized_pnl_usdt": -0.50,
                "pnl_pct": -0.0089,
                "ledger_rows": 2,
            }
        ],
    }

    text = format_telegram_status_message(payload)

    assert "Account now: 1 open" in text
    assert "exposure $56.18" in text
    assert "Ledger positions" not in text
    assert "qty=188" not in text
    assert "qty=125" not in text


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
        "entries": [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": 10.0,
                "entry_price": 100.0,
                "notional_usdt": 1_000.0,
                "take_profit_price": 88.0,
                "stop_price": 0.0,
            }
        ],
    }

    text = format_telegram_status_message(payload)

    assert "Position opened" in text
    assert "AAAUSDT SHORT" in text
    assert "Opened 10 @ $100" in text
    assert "Account now: 1 open" in text
    assert "uPnL +$50.00" in text


def test_telegram_exit_message_names_take_profit_and_realized_pnl() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "ws_risk_submit",
            "entries_executed": 0,
            "exits_executed": 1,
            "entry_candidates": 0,
            "exit_candidates": 1,
            "position_report_error": "",
        },
        "exits": [
            {
                "trade_id": "bus-short-1",
                "symbol": "BUSDT",
                "side": "short",
                "status": "closed",
                "qty": 100.0,
                "entry_price": 1.0,
                "exit_price": 0.88,
                "entry_fee_usdt": 0.02,
                "exit_fee_usdt": 0.02,
                "exit_reason": "take_profit",
            }
        ],
        "bybit_position_summary": {
            "positions": 0,
            "position_value_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "pnl_pct": 0.0,
        },
        "bybit_positions": [],
    }

    text = format_telegram_status_message(payload)

    assert "Position closed" in text
    assert "BUSDT SHORT · TAKE PROFIT" in text
    assert "Realised P&L +$11.96 (+11.96% of entry exposure)" in text
    assert "Account now: 0 open" in text


def test_telegram_exit_message_discloses_price_inferred_tp_cause() -> None:
    payload = {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "exits_executed": 0,
            "pending_exit_fills_reconciled": 1,
            "position_report_error": "",
        },
        "pending_fill_reconciliations": [
            {
                "trade_id": "tp-inferred",
                "symbol": "BUSDT",
                "side": "short",
                "status": "closed",
                "qty": 100.0,
                "entry_price": 1.0,
                "exit_price": 0.79,
                "exit_reason": "take_profit_level_reached",
                "exit_reason_source": "exit_price_vs_ledger_take_profit",
                "venue_closed_pnl_allocated_usdt": 20.5,
            }
        ],
        "bybit_position_summary": {
            "positions": 0,
            "position_value_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "pnl_pct": 0.0,
        },
        "bybit_positions": [],
    }

    text = format_telegram_status_message(payload)

    assert "TAKE-PROFIT LEVEL REACHED (ORDER TYPE UNCONFIRMED)" in text
    assert "Realised P&L +$20.50" in text
    assert "Close cause inferred from exit price; venue order type was unavailable." in text


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


def test_build_ledger_position_pnl_snapshot_prefers_position_markprice() -> None:
    """P1-3 (2026-05-27): when an open Bybit position is supplied alongside
    the ticker price-by-symbol dict, the ledger uPnL must use the position's
    own ``markPrice`` so the ledger uPnL matches the venue's position uPnL.

    Without this, a ticker-cache mark of 110 vs a position-payload mark of
    115 (the live divergence shape observed on TRUSTUSDT) silently drifted
    the ledger uPnL ~4% off the Bybit-reported uPnL."""
    from liquidity_migration.event_demo import build_ledger_position_pnl_snapshot

    open_trades = pl.DataFrame(
        [
            {
                "symbol": "TRUSTUSDT",
                "side": "short",
                "qty": 100.0,
                "entry_price": 120.0,
                "status": "open",
            }
        ]
    )
    # Ticker mark (e.g., 1m kline close on a thin alt) trails the venue's
    # position markPrice by ~4%, producing the user-reported drift.
    price_by_symbol = {"TRUSTUSDT": 110.0}
    position_by_symbol = {"TRUSTUSDT": {"symbol": "TRUSTUSDT", "markPrice": "115.0"}}

    # Without position_by_symbol: uPnL uses ticker mark 110 → (120-110)*100 = 1000.
    rows_ticker = build_ledger_position_pnl_snapshot(open_trades, price_by_symbol)
    assert rows_ticker[0]["unrealized_pnl_usdt"] == pytest.approx(1000.0)

    # With position_by_symbol: uPnL uses position mark 115 → (120-115)*100 = 500.
    rows_position = build_ledger_position_pnl_snapshot(
        open_trades, price_by_symbol, position_by_symbol=position_by_symbol
    )
    assert rows_position[0]["unrealized_pnl_usdt"] == pytest.approx(500.0)
    assert rows_position[0]["mark_price"] == pytest.approx(115.0)


def test_build_ledger_position_pnl_snapshot_falls_back_to_ticker_when_no_position() -> None:
    """Symbols without an open position dict (e.g., long-tail symbols the
    risk engine isn't watching) still fall back to the ticker mark — the
    position-mark preference is per-symbol, not all-or-nothing."""
    from liquidity_migration.event_demo import build_ledger_position_pnl_snapshot

    open_trades = pl.DataFrame(
        [
            {
                "symbol": "TRUSTUSDT",
                "side": "short",
                "qty": 100.0,
                "entry_price": 120.0,
                "status": "open",
            }
        ]
    )
    price_by_symbol = {"TRUSTUSDT": 110.0}
    # Position dict for a DIFFERENT symbol — TRUSTUSDT falls through.
    position_by_symbol = {"OTHERUSDT": {"symbol": "OTHERUSDT", "markPrice": "200.0"}}

    rows = build_ledger_position_pnl_snapshot(
        open_trades, price_by_symbol, position_by_symbol=position_by_symbol
    )
    assert rows[0]["mark_price"] == pytest.approx(110.0)


# --- relocated from test_audit_fix_b04.py (audit bucket b04) -----------------
# reports-charts-1: a wallet-read outage is surfaced, not masked. (The formatters
# format_telegram_status_message / _telegram_notification_reason are defined in
# event_demo_reports and re-exported via event_demo, imported above.)


def _status_payload(*, wallet_error: str, equity: float) -> dict:
    return {
        "cycle": {
            "ts_ms": 1_700_000_000_000,
            "mode": "submit",
            "equity_usdt": equity,
            "wallet_error": wallet_error,
            "entries_executed": 0,
            "entry_candidates": 0,
            "exits_executed": 0,
            "exit_candidates": 0,
            "position_report_error": "",
        },
        "bybit_position_summary": {},
        "ledger_position_summary": {},
    }


def test_wallet_error_tags_fallback_equity_and_is_surfaced() -> None:
    payload = _status_payload(wallet_error="wallet equity unavailable: timeout", equity=10_000.0)
    text = format_telegram_status_message(payload)
    # The fallback equity must NOT print as a clean read.
    assert "wallet check unavailable" in text
    assert "Wallet could not be verified: wallet equity unavailable: timeout" in text
    assert "Equity: $10,000.00" not in text


def test_wallet_error_triggers_a_notification() -> None:
    payload = _status_payload(wallet_error="wallet equity unavailable: 403", equity=10_000.0)
    assert _telegram_notification_reason(payload) == "wallet_error"


def test_clean_wallet_read_is_not_tagged_or_notified() -> None:
    payload = _status_payload(wallet_error="", equity=12_345.0)
    text = format_telegram_status_message(payload)
    assert "FALLBACK" not in text
    assert "wallet_error" not in text
    assert _telegram_notification_reason(payload) == ""


def test_position_loss_alerts_use_deepest_crossed_band_per_position() -> None:
    payload = {
        "cycle": {"ts_ms": 1_700_000_000_000, "position_report_error": ""},
        "bybit_positions": [
            {
                "symbol": "BUSDT",
                "side": "short",
                "qty": 100.0,
                "avg_price": 1.0,
                "mark_price": 1.12,
                "position_value_usdt": 112.0,
                "unrealized_pnl_usdt": -12.0,
                "pnl_pct": -12.0 / 112.0,
                "liquidation_price": 1.8,
                "take_profit_price": 0.88,
                "stop_price": 0.0,
            }
        ],
    }

    alerts = position_loss_alerts(payload, levels=(0.05, 0.10, 0.20))

    assert len(alerts) == 1
    assert alerts[0].threshold == pytest.approx(0.10)
    assert alerts[0].next_threshold == pytest.approx(0.20)
    text = format_position_loss_alert(alerts[0], now_ms=1_700_000_000_000)
    assert "BUSDT SHORT" in text
    assert "-$12.00" in text
    assert "First alert past -10.00%" in text
    assert "next only past -20.00%" in text
    assert "Protection: TP $0.88 · no venue stop" in text


def test_position_loss_configuration_fails_back_instead_of_disabling_alerts() -> None:
    assert position_loss_alert_levels("garbage") == (0.05, 0.10, 0.20, 0.40)
    assert position_loss_alert_levels("0.20,0.05,0.10") == (0.05, 0.10, 0.20)


def test_position_loss_alerts_require_a_verified_position_snapshot() -> None:
    payload = {
        "cycle": {"position_report_error": "REST timeout"},
        "bybit_positions": [
            {"symbol": "BUSDT", "side": "short", "pnl_pct": -0.50},
        ],
    }

    assert position_loss_alerts(payload) == []
