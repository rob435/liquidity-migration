"""Event-demo reports tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations


import polars as pl
import pytest

from liquidity_migration.event_demo import (
    _maybe_notify,
    _telegram_notification_reason,
    build_ledger_position_pnl_snapshot,
    build_position_pnl_snapshot,
    format_telegram_status_message,
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

