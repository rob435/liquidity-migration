from __future__ import annotations

import json
import sys
from datetime import date

import polars as pl

import scripts.run_continuous_hedge as hedge_runner


def test_live_book_state_uses_notional_over_equity(monkeypatch, tmp_path) -> None:
    def fake_read_dataset(root, dataset):
        return pl.DataFrame(
            [
                {"status": "open", "side": "short", "notional_usdt": 1_000.0, "equity_usdt": 10_000.0},
                {"status": "open", "side": "Sell", "notional_usdt": 2_000.0, "equity_usdt": 10_000.0},
                {"status": "open", "side": "long", "notional_usdt": 9_000.0, "equity_usdt": 10_000.0},
            ]
        )

    monkeypatch.setattr(hedge_runner, "read_dataset", fake_read_dataset)

    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")

    assert state.gross_short_frac_known is True
    assert state.gross_short_frac_source == "notional_over_equity"
    assert abs(state.gross_short_frac - 0.3) < 1e-12


def test_live_book_state_distinguishes_flat_from_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        hedge_runner,
        "read_dataset",
        lambda root, dataset: pl.DataFrame([{"status": "closed", "notional_usdt": 1_000.0, "equity_usdt": 10_000.0}]),
    )

    flat = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")

    assert flat.gross_short_frac_known is True
    assert flat.gross_short_frac == 0.0
    assert flat.gross_short_frac_source == "flat"

    monkeypatch.setattr(
        hedge_runner,
        "read_dataset",
        lambda root, dataset: pl.DataFrame([{"status": "open", "side": "short", "notional_usdt": 1_000.0}]),
    )

    unknown = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")

    assert unknown.gross_short_frac_known is False
    assert unknown.gross_short_frac == 0.5
    assert unknown.gross_short_frac_source == "unknown"


def test_current_hedge_qty_reads_open_btc_long_from_hedge_ledger(monkeypatch, tmp_path) -> None:
    def fake_read_dataset(root, dataset):
        return pl.DataFrame(
            [
                {"status": "open", "symbol": "BTCUSDT", "side": "long", "qty": "0.02"},
                {"status": "open", "symbol": "BTCUSDT", "side": "Buy", "qty": "0.03"},
                {"status": "closed", "symbol": "BTCUSDT", "side": "long", "qty": "1"},
                {"status": "open", "symbol": "ETHUSDT", "side": "long", "qty": "5"},
                {"status": "open", "symbol": "BTCUSDT", "side": "short", "qty": "0.5"},
            ]
        )

    monkeypatch.setattr(hedge_runner, "read_dataset", fake_read_dataset)

    assert abs(hedge_runner._current_hedge_qty(tmp_path, "continuous_fade_demo_trades") - 0.05) < 1e-12


def test_submit_uses_central_real_money_guard(monkeypatch, tmp_path, capsys) -> None:
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45

    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)
    monkeypatch.setattr(hedge_runner, "load_warmstart", lambda path: (unit, btc))
    monkeypatch.setattr(hedge_runner, "_warmstart_last_date", lambda path: date.today())
    monkeypatch.setattr(
        hedge_runner,
        "_live_book_state",
        lambda root, dataset: hedge_runner.LiveBookState({}, 0.5, True, "test"),
    )
    monkeypatch.setattr(hedge_runner, "_current_hedge_qty", lambda root, dataset: 0.0)
    monkeypatch.setenv("CONFIRM_DEMO_ORDERS", "1")
    monkeypatch.setenv("REAL_MONEY", "YES")
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_continuous_hedge.py",
            "--submit",
            "--btc-price",
            "100000",
            "--equity-usdt",
            "10000",
        ],
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_blocked_order_submit_guard"
    assert "REAL_MONEY=true" in out["error"]


def test_missing_btc_price_is_surfaced_not_silent(monkeypatch, tmp_path, capsys) -> None:
    """No kline store + no --btc-price -> plan is None; the status must SAY the
    input was dead instead of reading as a healthy dry_run_ok no-op."""
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45

    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)  # no .cache/ws_klines under tmp
    monkeypatch.setattr(hedge_runner, "load_warmstart", lambda path: (unit, btc))
    monkeypatch.setattr(hedge_runner, "_warmstart_last_date", lambda path: date.today())
    monkeypatch.setattr(
        hedge_runner,
        "_live_book_state",
        lambda root, dataset: hedge_runner.LiveBookState({}, 0.5, True, "test"),
    )
    monkeypatch.setattr(hedge_runner, "_current_hedge_qty", lambda root, dataset: 0.0)
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py", "--equity-usdt", "10000"])

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["btc_price"] == 0.0
    assert out["plan"] is None
    assert out["status"] == "dry_run_btc_price_unavailable"
