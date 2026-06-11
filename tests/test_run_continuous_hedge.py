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


# ---------------------------------------------------------------------------
# Hedge ledger booking (audit 2026-06-11: armed BUYs double-booked through
# ws_risk's pending-fill reconciler; reduce-only SELLs never touched the
# trade rows, so the planner re-sold a phantom hedge daily)
# ---------------------------------------------------------------------------


def _open_hedge_row(trade_id: str, qty: float, entry_ts_ms: int) -> dict:
    return {
        "trade_id": trade_id, "strategy_id": "continuous_hedge_v1", "symbol": "BTCUSDT",
        "side": "long", "sleeve": "continuous_addon", "status": "open",
        "ts_ms": entry_ts_ms, "entry_ts_ms": entry_ts_ms, "opened_at_ms": entry_ts_ms,
        "updated_at_ms": entry_ts_ms, "entry_price": 100_000.0, "qty": qty,
        "notional_usdt": 100_000.0 * qty, "stop_price": 0.0, "take_profit_price": 0.0,
        "planned_exit_ts_ms": 0,
    }


def test_hedge_reduce_books_against_open_trade_rows_oldest_first(monkeypatch, tmp_path) -> None:
    ledger = pl.DataFrame([
        _open_hedge_row("hedge-a", 0.3, 1_700_000_000_000),
        _open_hedge_row("hedge-b", 0.4, 1_700_000_100_000),
    ], infer_schema_length=None)
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(hedge_runner, "read_dataset", lambda root, dataset: ledger)
    monkeypatch.setattr(
        hedge_runner, "write_dataset",
        lambda df, root, dataset, **kw: written.append(df),
    )
    cfg = hedge_runner.ContinuousHedgeConfig()
    hedge_runner._apply_hedge_reduce_to_trades(
        tmp_path, cfg, symbol="BTCUSDT", sold_qty=0.5, exit_price=99_000.0,
        now_ms=1_700_000_200_000,
    )
    assert len(written) == 1
    rows = {r["trade_id"]: r for r in written[0].to_dicts()}
    # oldest row fully consumed -> closed with the reduce exit stamp
    assert rows["hedge-a"]["status"] == "closed"
    assert rows["hedge-a"]["exit_reason"] == "hedge_reduce"
    assert rows["hedge-a"]["exit_price"] == 99_000.0
    # remainder (0.5 - 0.3 = 0.2) partially reduces the second row: 0.4 -> 0.2
    assert rows["hedge-b"]["status"] == "open"
    assert abs(rows["hedge-b"]["qty"] - 0.2) < 1e-9
    assert rows["hedge-b"]["updated_at_ms"] == 1_700_000_200_000


def test_hedge_reduce_with_no_open_rows_is_a_noop(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hedge_runner, "read_dataset", lambda root, dataset: pl.DataFrame())
    written: list[pl.DataFrame] = []
    monkeypatch.setattr(
        hedge_runner, "write_dataset",
        lambda df, root, dataset, **kw: written.append(df),
    )
    hedge_runner._apply_hedge_reduce_to_trades(
        tmp_path, hedge_runner.ContinuousHedgeConfig(), symbol="BTCUSDT",
        sold_qty=0.5, exit_price=99_000.0, now_ms=1_700_000_200_000,
    )
    assert written == []


def test_hedge_buy_order_row_is_terminal_for_ws_risk_reconciler() -> None:
    """REGRESSION (audit 2026-06-11): the runner books the market fill itself, so its
    order row must read status='filled' with filled_qty set — a 'submitted' row with
    no filled_qty made ws_risk's pending-fill reconciler delta-add the FULL venue
    fill onto the runner-booked trade row (qty doubled on every armed BUY)."""
    from liquidity_migration.event_demo import EventDemoCycleConfig
    from liquidity_migration.event_demo_exits import _reconcile_pending_order_fills
    import sys as _sys
    _sys.path.insert(0, "tests")
    from _event_demo_fixtures import FakeRiskClient

    now = 1_700_000_000_000
    order_row = {
        "order_link_id": "lm-en-ca-BTC-t72ncw", "ts_ms": now,
        "trade_id": "hedge-lm-en-ca-BTC-t72ncw", "strategy_id": "continuous_hedge_v1",
        "symbol": "BTCUSDT", "side": "Buy", "order_type": "Market", "qty": 0.5,
        "reduce_only": False, "order_id": "oid-1", "submit_mode": "submitted",
        "status": "filled", "filled_qty": 0.5, "target_qty": 0.5,
        "trade_side": "long", "sleeve": "continuous_addon",
        "notional_usdt": 50_000.0, "reason": "hedge_add", "updated_at_ms": now,
    }
    trade_row = _open_hedge_row("hedge-lm-en-ca-BTC-t72ncw", 0.5, now)
    trades, order_updates = _reconcile_pending_order_fills(
        pl.DataFrame([order_row], infer_schema_length=None),
        pl.DataFrame([trade_row], infer_schema_length=None),
        trading_client=FakeRiskClient(fill_market_orders=True, fill_order_prefixes=("lm-en-",)),
        demo=EventDemoCycleConfig(submit_orders=True, confirm_demo_orders=True),
        now_ms=now + 120_000,
    )
    # terminal order row -> the reconciler must not touch the runner-booked trade
    assert trades == []
    assert order_updates == []
