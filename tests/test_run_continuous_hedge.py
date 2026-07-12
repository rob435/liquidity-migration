from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import polars as pl
import pytest

import scripts.run_continuous_hedge as hedge_runner
from liquidity_migration.continuous_hedge_manager import HedgeDecision, HedgeDecision2F
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan


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


def test_live_book_state_unstamped_row_borrows_known_equity(monkeypatch, tmp_path) -> None:
    """Solo sweep 2026-06-12: every row's equity_usdt is a snapshot of the SAME
    netted account, so a legacy row missing it (pre-round-3 snipe fills, adopted
    rows) borrows the median of the known ones — ONE un-stamped row previously
    flipped the whole book to unknown and BLOCKED the armed hedge. A row with
    unknown NOTIONAL still blocks (truly unmeasured exposure)."""
    monkeypatch.setattr(
        hedge_runner,
        "read_dataset",
        lambda root, dataset: pl.DataFrame(
            [
                {"status": "open", "side": "short", "notional_usdt": 1_000.0, "equity_usdt": 10_000.0},
                {"status": "open", "side": "short", "notional_usdt": 2_000.0, "equity_usdt": 10_000.0},
                # legacy zero-equity row: borrows the 10k median -> 0.05
                {"status": "open", "side": "short", "notional_usdt": 500.0, "equity_usdt": 0.0},
            ]
        ),
    )
    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")
    assert state.gross_short_frac_known is True
    assert state.gross_short_frac_source == "notional_over_equity"
    assert abs(state.gross_short_frac - 0.35) < 1e-12

    # Zero NOTIONAL is not borrowable — exposure unmeasured -> still blocked.
    monkeypatch.setattr(
        hedge_runner,
        "read_dataset",
        lambda root, dataset: pl.DataFrame(
            [
                {"status": "open", "side": "short", "notional_usdt": 1_000.0, "equity_usdt": 10_000.0},
                {"status": "open", "side": "short", "notional_usdt": 0.0, "equity_usdt": 10_000.0},
            ]
        ),
    )
    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")
    assert state.gross_short_frac_known is False
    assert state.gross_short_frac_source == "partial_notional_over_equity"


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


def _setup_runner(
    monkeypatch,
    tmp_path,
    *,
    warm_eth: list[float | None] | None = None,
    warmstart_last: date | None = None,
    book_state: "hedge_runner.LiveBookState | None" = None,
    argv: list[str] | None = None,
) -> None:
    """Common main() seams: warm-start, book state, hedge-qty reads, env, argv."""
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45
    eth = [None] * 90 if warm_eth is None else warm_eth

    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)  # no .cache/ws_klines under tmp
    monkeypatch.setattr(hedge_runner, "load_warmstart_2f", lambda path: (unit, btc, eth))
    monkeypatch.setattr(
        hedge_runner, "_warmstart_last_date", lambda path: warmstart_last or date.today()
    )
    state = book_state or hedge_runner.LiveBookState({}, 0.5, True, "test")
    monkeypatch.setattr(
        hedge_runner, "_live_book_state", lambda root, dataset, cycles_dataset=None: state
    )
    monkeypatch.setattr(
        hedge_runner, "_current_hedge_qty", lambda root, dataset, symbol=None: 0.0
    )
    monkeypatch.setenv("CONFIRM_DEMO_ORDERS", "1")
    monkeypatch.delenv("REAL_MONEY", raising=False)
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("HEDGE_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py", *(argv or [])])


def _resize_plan(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.003348,
    reduce_only: bool = False,
    delta_notional: float = 334.8,
    reason: str = "hedge_increase",
) -> ContinuousRebalanceResizePlan:
    return ContinuousRebalanceResizePlan(
        trade_id="hedge", symbol=symbol, side=side, reduce_only=reduce_only, qty=qty,
        current_notional_usdt=0.0, target_notional_usdt=abs(delta_notional),
        delta_notional_usdt=delta_notional, reason=reason,
    )


def _single_decision(plan: ContinuousRebalanceResizePlan | None) -> HedgeDecision:
    return HedgeDecision(
        beta_window_days=90, hedge_ratio_equity_frac=0.05, target_notional_usdt=500.0,
        current_notional_usdt=0.0, n_obs=90, plan=plan,
    )


def _decision_2f(
    plan_btc: ContinuousRebalanceResizePlan | None,
    plan_eth: ContinuousRebalanceResizePlan | None,
) -> HedgeDecision2F:
    return HedgeDecision2F(
        beta_window_days=90, ratio_btc=0.03, ratio_eth=0.02,
        target_btc_usdt=300.0, target_eth_usdt=200.0, n_obs_joint=90,
        plan_btc=plan_btc, plan_eth=plan_eth, fell_back_to_btc=False,
    )


SUBMIT_ARGS = ["--submit", "--btc-price", "100000", "--equity-usdt", "10000"]


def test_submit_uses_central_real_money_guard(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=SUBMIT_ARGS)
    monkeypatch.setenv("REAL_MONEY", "YES")

    assert hedge_runner.main() == 1  # blocked armed run fails the oneshot -> watchdog pages
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_blocked_order_submit_guard"
    assert "REAL_MONEY=true" in out["error"]


def test_missing_btc_price_is_surfaced_not_silent(monkeypatch, tmp_path, capsys) -> None:
    """No kline store + no --btc-price -> plan is None; the status must SAY the
    input was dead instead of reading as a healthy dry_run_ok no-op."""
    _setup_runner(monkeypatch, tmp_path, argv=["--equity-usdt", "10000"])

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
        "trade_id": trade_id, "strategy_id": "continuous_hedge_v2", "symbol": "BTCUSDT",
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
        "trade_id": "hedge-lm-en-ca-BTC-t72ncw", "strategy_id": "continuous_hedge_v2",
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


# ---------------------------------------------------------------------------
# Flat-book detection via the cycles dataset (audit 2026-06-12: a rebuilt book
# with ZERO trade rows ever read as "unknown" 0.5 and the armed hedge would
# have BOUGHT against a flat book the day staleness unblocked)
# ---------------------------------------------------------------------------


def test_live_book_state_flat_when_cycles_present_but_no_trades(monkeypatch, tmp_path) -> None:
    def fake_read_dataset(root, dataset):
        if dataset == "continuous_fade_demo_cycles":
            return pl.DataFrame([{"cycle_id": "c-1", "ts_ms": 1}])
        return pl.DataFrame()

    monkeypatch.setattr(hedge_runner, "read_dataset", fake_read_dataset)

    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")

    assert state.gross_short_frac == 0.0
    assert state.gross_short_frac_known is True
    assert state.gross_short_frac_source == "ledger_present_no_rows"


def test_live_book_state_stays_unknown_when_cycles_missing_or_empty(monkeypatch, tmp_path) -> None:
    def missing_cycles(root, dataset):
        if dataset == "continuous_fade_demo_cycles":
            raise FileNotFoundError(dataset)
        return pl.DataFrame()

    monkeypatch.setattr(hedge_runner, "read_dataset", missing_cycles)
    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")
    assert state.gross_short_frac_known is False
    assert state.gross_short_frac == 0.5
    assert state.gross_short_frac_source == "ledger_empty_or_missing_status"

    monkeypatch.setattr(hedge_runner, "read_dataset", lambda root, dataset: pl.DataFrame())
    state = hedge_runner._live_book_state(tmp_path, "continuous_fade_demo_trades")
    assert state.gross_short_frac_known is False
    assert state.gross_short_frac_source == "ledger_empty_or_missing_status"


# ---------------------------------------------------------------------------
# Armed-run blocking + exit-code paging contract (audit 2026-06-12: every
# blocked armed run exited 0, so the watchdog never paged)
# ---------------------------------------------------------------------------


def test_submit_blocked_when_book_state_unknown(monkeypatch, tmp_path, capsys) -> None:
    """Never submit orders sized off the 0.5 'unknown' default."""
    _setup_runner(
        monkeypatch, tmp_path, argv=SUBMIT_ARGS,
        book_state=hedge_runner.LiveBookState({}, 0.5, False, "unknown"),
    )
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision",
        lambda cfg, **kw: _single_decision(_resize_plan()),
    )
    submit_calls: list = []
    monkeypatch.setattr(hedge_runner, "_submit_plan", lambda *a, **k: submit_calls.append(a))

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_blocked_book_state_unknown"
    assert submit_calls == []


def test_submit_blocked_when_eth_price_unavailable_in_2f_mode(monkeypatch, tmp_path, capsys) -> None:
    """2f mode used to silently drop the ETH leg when its price feed was dead."""
    _setup_runner(monkeypatch, tmp_path, warm_eth=[0.001] * 90, argv=SUBMIT_ARGS)  # no --eth-price
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision_2f",
        lambda cfg, **kw: _decision_2f(_resize_plan(), None),
    )
    submit_calls: list = []
    monkeypatch.setattr(hedge_runner, "_submit_plan", lambda *a, **k: submit_calls.append(a))

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["hedge_mode"] == "2f"
    assert out["eth_price"] == 0.0
    assert out["status"] == "submit_blocked_eth_price_unavailable"
    assert submit_calls == []


def test_stale_warmstart_blocks_risk_increasing_legs(monkeypatch, tmp_path, capsys) -> None:
    buy = _resize_plan(side="Buy", reduce_only=False)
    _setup_runner(
        monkeypatch, tmp_path, argv=SUBMIT_ARGS,
        warmstart_last=date.today() - timedelta(days=10),
    )
    monkeypatch.setattr(hedge_runner, "compute_hedge_decision", lambda cfg, **kw: _single_decision(buy))
    submit_calls: list = []
    monkeypatch.setattr(hedge_runner, "_submit_plan", lambda *a, **k: submit_calls.append(a))

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["warmstart_stale"] is True
    assert out["status"] == "submit_blocked_stale_warmstart"
    assert [leg["side"] for leg in out["blocked_legs"]] == ["Buy"]
    # No reduce-only sibling leg -> nothing submitted (the add is the only plan).
    assert out["submitted"] == []
    assert submit_calls == []


def test_stale_warmstart_blocks_nonflat_book_even_below_resize_floor(
    monkeypatch, tmp_path, capsys
) -> None:
    """A missing plan must not make stale beta protection look healthy."""
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=SUBMIT_ARGS,
        warmstart_last=date.today() - timedelta(days=10),
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision",
        lambda cfg, **kw: _single_decision(None),
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["gross_short_frac"] > 0
    assert out["plan"] is None
    assert out["status"] == "submit_blocked_stale_warmstart"


def test_stale_warmstart_lets_all_reduce_only_plans_proceed(monkeypatch, tmp_path, capsys) -> None:
    """Trimming/closing a hedge is risk-REDUCING — a stale beta window must not block it."""
    sell = _resize_plan(side="Sell", reduce_only=True, qty=0.003, delta_notional=-300.0, reason="hedge_reduce")
    _setup_runner(
        monkeypatch, tmp_path, argv=SUBMIT_ARGS,
        warmstart_last=date.today() - timedelta(days=10),
    )
    monkeypatch.setattr(hedge_runner, "compute_hedge_decision", lambda cfg, **kw: _single_decision(sell))
    monkeypatch.setattr(
        hedge_runner, "_submit_plan",
        lambda plan, cfg, data_root, primary_root, now_ms: {
            "symbol": plan.symbol, "side": plan.side, "qty": plan.qty,
            "reduce_only": plan.reduce_only, "order_id": "oid-1", "link": "l-1",
        },
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["warmstart_stale"] is True
    assert out["status"] == "submitted"
    assert [leg["side"] for leg in out["submitted"]] == ["Sell"]


def test_submit_failed_exits_nonzero(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=SUBMIT_ARGS)
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision", lambda cfg, **kw: _single_decision(_resize_plan())
    )

    def boom(plan, cfg, data_root, primary_root, now_ms):
        raise RuntimeError("venue down")

    monkeypatch.setattr(hedge_runner, "_submit_plan", boom)

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_failed"
    assert out["submit_errors"][0]["error"] == "venue down"


def test_submit_partial_exits_nonzero(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch, tmp_path, warm_eth=[0.001] * 90,
        argv=[*SUBMIT_ARGS, "--eth-price", "3000"],
    )
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision_2f",
        lambda cfg, **kw: _decision_2f(
            _resize_plan(),
            _resize_plan(symbol="ETHUSDT", qty=0.05, delta_notional=150.0),
        ),
    )

    def one_leg_fails(plan, cfg, data_root, primary_root, now_ms):
        if plan.symbol == "ETHUSDT":
            raise RuntimeError("eth leg rejected")
        return {"symbol": plan.symbol, "side": plan.side, "qty": plan.qty,
                "reduce_only": plan.reduce_only, "order_id": "oid-1", "link": "l-1"}

    monkeypatch.setattr(hedge_runner, "_submit_plan", one_leg_fails)

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_partial"
    assert [leg["symbol"] for leg in out["submitted"]] == ["BTCUSDT"]
    assert [err["symbol"] for err in out["submit_errors"]] == ["ETHUSDT"]


def test_all_legs_skipped_is_no_action_and_exits_zero(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=SUBMIT_ARGS)
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision",
        lambda cfg, **kw: _single_decision(_resize_plan(qty=0.0004, delta_notional=40.0)),
    )
    monkeypatch.setattr(
        hedge_runner, "_submit_plan",
        lambda plan, cfg, data_root, primary_root, now_ms: {
            "symbol": plan.symbol, "side": plan.side, "qty": 0.0,
            "planned_qty": plan.qty, "reduce_only": plan.reduce_only,
            "skipped": "below_min_qty",
        },
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_no_action"
    assert out["submitted"] == []
    assert [leg["skipped"] for leg in out["skipped"]] == ["below_min_qty"]


def test_no_warmstart_fails_only_armed_runs(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=SUBMIT_ARGS)
    monkeypatch.setattr(hedge_runner, "load_warmstart_2f", lambda path: ([], [], []))

    assert hedge_runner.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "no_warmstart"

    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py"])  # dry-run
    assert hedge_runner.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "no_warmstart"


# ---------------------------------------------------------------------------
# Venue qty-step / min-filter alignment in _submit_plan (audit 2026-06-12:
# planned 0.003348 BTC vs qtyStep 0.001 -> guaranteed venue reject)
# ---------------------------------------------------------------------------


def _wire_submit_seams(monkeypatch, *, filters: dict[str, float]):
    """Stub credentials/client/instrument-filters/ledger writes around _submit_plan.

    Returns (placed_orders, written_datasets, reduce_calls) capture lists.
    """
    import liquidity_migration.bybit as bybit_mod

    placed: list[dict] = []
    written: list[tuple[str, pl.DataFrame]] = []
    reduces: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def place_order(self, **params):
            placed.append(params)
            return {"orderId": "oid-1"}

    monkeypatch.setattr(bybit_mod, "resolve_private_credentials", lambda: ("k", "s", True))
    monkeypatch.setattr(bybit_mod, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(
        hedge_runner,
        "_read_actual_fill",
        lambda client, *, symbol, order_link_id, requested_qty: (
            requested_qty,
            100_000.0,
            "venue",
        ),
    )
    monkeypatch.setattr(hedge_runner, "_instrument_filters", lambda symbol, root: dict(filters))
    monkeypatch.setattr(
        hedge_runner, "write_dataset",
        lambda df, root, dataset, **kw: written.append((dataset, df)),
    )
    monkeypatch.setattr(
        hedge_runner, "_apply_hedge_reduce_to_trades",
        lambda data_root, cfg, **kw: reduces.append(kw),
    )
    return placed, written, reduces


def test_submit_plan_floors_qty_to_step_everywhere(monkeypatch, tmp_path) -> None:
    placed, written, _ = _wire_submit_seams(
        monkeypatch, filters={"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0}
    )
    cfg = hedge_runner.ContinuousHedgeConfig()
    plan = _resize_plan(qty=0.003348, delta_notional=334.8)  # implied price 100k

    result = hedge_runner._submit_plan(plan, cfg, tmp_path, tmp_path, now_ms=1_700_000_000_000)

    assert result["qty"] == 0.003
    assert "skipped" not in result
    assert placed[0]["qty"] == "0.003"
    rows_by_dataset = {dataset: df.to_dicts()[0] for dataset, df in written}
    order_row = rows_by_dataset[cfg.orders_dataset]
    assert order_row["qty"] == 0.003
    assert order_row["filled_qty"] == 0.003
    assert order_row["target_qty"] == 0.003
    trade_row = rows_by_dataset[cfg.trades_dataset]
    assert trade_row["qty"] == 0.003
    assert abs(trade_row["entry_price"] - 100_000.0) < 1e-6  # implied price, float-division noise


def test_submit_plan_skips_below_min_qty_without_submitting(monkeypatch, tmp_path) -> None:
    placed, written, _ = _wire_submit_seams(
        monkeypatch, filters={"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0}
    )
    plan = _resize_plan(qty=0.0004, delta_notional=40.0)  # floors to 0.0

    result = hedge_runner._submit_plan(
        plan, hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000
    )

    assert result["skipped"] == "below_min_qty"
    assert result["planned_qty"] == 0.0004
    assert placed == []
    assert written == []


def test_submit_plan_skips_below_min_notional_for_risk_increasing_legs(monkeypatch, tmp_path) -> None:
    placed, written, _ = _wire_submit_seams(
        monkeypatch, filters={"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0}
    )
    # implied price 1000 -> floored qty 0.001 -> notional 1.0 < minNotional 5.0
    plan = _resize_plan(qty=0.0015, delta_notional=1.5)

    result = hedge_runner._submit_plan(
        plan, hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000
    )

    assert result["skipped"] == "below_min_notional"
    assert placed == []
    assert written == []


def test_submit_plan_reduce_only_is_exempt_from_min_notional(monkeypatch, tmp_path) -> None:
    """Bybit semantics: reduce-only legs bypass minNotional (still step/minQty bound)."""
    placed, written, reduces = _wire_submit_seams(
        monkeypatch, filters={"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0}
    )
    plan = _resize_plan(
        side="Sell", reduce_only=True, qty=0.0015, delta_notional=-1.5, reason="hedge_reduce"
    )

    result = hedge_runner._submit_plan(
        plan, hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000
    )

    assert "skipped" not in result
    assert result["qty"] == 0.001
    assert placed[0]["qty"] == "0.001"
    assert placed[0]["reduceOnly"] is True
    assert reduces and abs(reduces[0]["sold_qty"] - 0.001) < 1e-12


# ---------------------------------------------------------------------------
# Warm-start eth alignment (audit 2026-06-12: the old _load_warmstart_eth used a
# different row-skip rule than load_warmstart — a non-numeric unit_ret desynced
# the eth column by one)
# ---------------------------------------------------------------------------


def test_warmstart_loader_keeps_eth_aligned_on_malformed_unit_ret(tmp_path) -> None:
    csv_path = tmp_path / "warmstart.csv"
    csv_path.write_text(
        "date,unit_ret,btc_ret,eth_ret\n"
        "2026-01-01,0.001,0.01,0.02\n"
        "2026-01-02,n/a,0.011,0.03\n"  # malformed unit_ret: whole row must drop
        "2026-01-03,0.002,0.012,0.04\n",
        encoding="utf-8",
    )

    unit, btc, eth = hedge_runner.load_warmstart_2f(csv_path)

    assert unit == [0.001, 0.002]
    assert btc == [0.01, 0.012]
    assert eth == [0.02, 0.04]  # 0.03 dropped WITH its unit row — no off-by-one
    # the divergent private loader is gone; the manager's 2f loader is the only path
    assert not hasattr(hedge_runner, "_load_warmstart_eth")


def test_armed_run_blocked_when_wallet_equity_unavailable(monkeypatch, tmp_path, capsys) -> None:
    """ROUND 4: an armed run with no --equity-usdt override must size off the
    LIVE wallet, never the $10k fallback constant — a failed wallet read blocks
    the run (nonzero exit -> watchdog pages) instead of silently under/over-
    hedging a wallet of unknown size."""
    _setup_runner(monkeypatch, tmp_path, argv=["--submit", "--btc-price", "100000"])
    monkeypatch.setattr(
        hedge_runner, "_live_wallet_equity_usdt",
        lambda: (_ for _ in ()).throw(RuntimeError("wallet read down")),
    )
    monkeypatch.setattr(hedge_runner, "compute_hedge_decision", lambda *a, **k: _single_decision(None))

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "submit_blocked_equity_unavailable"
    assert out["equity_source"] == "fallback"
    assert "wallet" in out["error"]


def test_armed_run_sizes_off_live_wallet_equity(monkeypatch, tmp_path, capsys) -> None:
    """ROUND 4 companion: with the wallet readable, the armed run sizes off the
    live equity (and says so), not the fallback constant."""
    _setup_runner(monkeypatch, tmp_path, argv=["--submit", "--btc-price", "100000"])
    monkeypatch.setattr(hedge_runner, "_live_wallet_equity_usdt", lambda: 5_432.0)
    monkeypatch.setattr(hedge_runner, "compute_hedge_decision", lambda *a, **k: _single_decision(None))

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "submit_no_action"
    assert out["equity_usdt"] == 5_432.0
    assert out["equity_source"] == "wallet"


def test_dry_run_keeps_fallback_equity_without_wallet_read(monkeypatch, tmp_path, capsys) -> None:
    """Dry-run behavior is unchanged: no wallet read, fallback equity."""
    _setup_runner(monkeypatch, tmp_path, argv=["--btc-price", "100000"])

    def _boom() -> float:
        raise AssertionError("dry-run must not read the wallet")

    monkeypatch.setattr(hedge_runner, "_live_wallet_equity_usdt", _boom)
    monkeypatch.setattr(hedge_runner, "compute_hedge_decision", lambda *a, **k: _single_decision(None))

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["equity_source"] == "fallback"


# ---------------------------------------------------------------------------
# audit2c: hedge runner robustness — warmstart staleness uses the MAX date.
# ---------------------------------------------------------------------------


def test_warmstart_last_date_returns_max_not_last_in_file(tmp_path) -> None:
    # Final row is OLDER than an earlier row (non-monotonic / out-of-order append).
    p = tmp_path / "ws.csv"
    p.write_text(
        "date,btc_ret,eth_ret\n2026-06-10,0.01,0.01\n2026-06-12,0.02,0.02\n2026-06-09,0.0,0.0\n",
        encoding="utf-8",
    )
    assert hedge_runner._warmstart_last_date(p) == date(2026, 6, 12)  # max, not the last row 06-09


def test_warmstart_last_date_ordered_file_unchanged(tmp_path) -> None:
    p = tmp_path / "ws.csv"
    p.write_text(
        "date,btc_ret,eth_ret\n2026-06-10,0.01,0.01\n2026-06-11,0.02,0.02\n2026-06-12,0.0,0.0\n",
        encoding="utf-8",
    )
    assert hedge_runner._warmstart_last_date(p) == date(2026, 6, 12)


def test_warmstart_freshness_prefers_validated_data_boundary(tmp_path) -> None:
    p = tmp_path / "warm.csv"
    p.write_text(
        "date,unit_ret,btc_ret,eth_ret,data_through_date,source_summary_sha256\n"
        "2026-06-12,0,0,0,2026-07-09,abc\n",
        encoding="utf-8",
    )
    assert hedge_runner._warmstart_last_date(p) == date(2026, 7, 9)


# ---------------------------------------------------------------------------
# audit b07 hedge-2 / hedge-3: _submit_plan books the ACTUAL venue fill
# (relocated from tests/test_audit_fix_b07.py — pins the actual-fill booking,
# the resolved-demo-flag threading, and the mixed-stale-run reduce/add split)
# ---------------------------------------------------------------------------


def _wire_submit_seams_b07(
    monkeypatch,
    *,
    executions: list[dict],
    order_history: list[dict] | None = None,
    filters: dict | None = None,
):
    """Stub the credentials/client/instrument-filters/ledger writes around
    _submit_plan.

    ``_read_actual_fill`` reads the realized fill via the demo engine's
    ``_wait_for_execution_summary``. We patch THAT (it is imported locally inside
    _read_actual_fill from event_demo, so patching event_demo.* is picked up) to
    return the summary that the real ``_execution_summary`` would build from
    ``executions`` — deterministic and instant (no 3s venue poll). Empty
    ``executions`` -> summary qty 0 -> _read_actual_fill's read_failed fallback."""
    import liquidity_migration.bybit as bybit_mod
    import liquidity_migration.event_demo as ed

    placed: list[dict] = []
    written: list[tuple[str, pl.DataFrame]] = []
    reduces: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **params):
            placed.append(params)
            return {"orderId": "oid-1"}

        def get_order_history(self, **_params):
            return list(order_history or [])

    monkeypatch.setattr(bybit_mod, "resolve_private_credentials", lambda: ("k", "s", True))
    monkeypatch.setattr(bybit_mod, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(
        ed, "_wait_for_execution_summary",
        lambda client, **kw: ed._execution_summary(list(executions)),
    )
    monkeypatch.setattr(
        hedge_runner, "_instrument_filters",
        lambda symbol, root: dict(filters or {"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0}),
    )
    monkeypatch.setattr(
        hedge_runner, "write_dataset",
        lambda df, root, dataset, **kw: written.append((dataset, df)),
    )
    monkeypatch.setattr(
        hedge_runner, "_apply_hedge_reduce_to_trades",
        lambda data_root, cfg, **kw: reduces.append(kw),
    )
    return placed, written, reduces


def test_submit_plan_books_partial_buy_fill_not_requested_qty(monkeypatch, tmp_path) -> None:
    """hedge-3: a market BUY that fills SHORT of the requested qty must book the
    venue's actual filled_qty (and avg fill price), not the requested qty at the
    planner's implied price. The original code wrote filled_qty=qty unconditionally
    -> the next day's resize was computed against a hedge that never fully existed."""
    placed, written, _ = _wire_submit_seams_b07(
        monkeypatch,
        # requested 0.5 BTC, only 0.3 filled at avg 101_000 (slippage above implied)
        executions=[{"execQty": "0.3", "execPrice": "101000", "execValue": "30300", "execFee": "0", "execTime": "1"}],
    )
    plan = _resize_plan(qty=0.5, delta_notional=50_000.0)  # implied price 100k

    result = hedge_runner._submit_plan(
        plan, hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000
    )

    assert result["fill_source"] == "venue"
    assert abs(result["qty"] - 0.3) < 1e-9          # booked the ACTUAL fill
    assert abs(result["requested_qty"] - 0.5) < 1e-9
    rows = {dataset: df.to_dicts()[0] for dataset, df in written}
    cfg = hedge_runner.ContinuousHedgeConfig()
    order_row = rows[cfg.orders_dataset]
    assert abs(order_row["filled_qty"] - 0.3) < 1e-9
    # short fill -> NOT terminal "filled" (so ws_risk's reconciler can delta-add a remainder)
    assert order_row["status"] == "partial"
    trade_row = rows[cfg.trades_dataset]
    assert abs(trade_row["qty"] - 0.3) < 1e-9       # trade row sized off the real fill
    assert abs(trade_row["entry_price"] - 101_000.0) < 1.0  # venue avg, not implied 100k


def test_submit_plan_books_venue_capped_reduce_fill(monkeypatch, tmp_path) -> None:
    """hedge-2: a reduce-only SELL venue-capped by cross-sleeve netting (a same-symbol
    fade short on the shared one-way account) fills less than requested. The reduce
    booked against the trade rows must be the ACTUAL filled qty, not the requested
    qty — otherwise the ledger over-states the reduction and the planner re-BUYs a
    hedge that partly still exists (over-hedge accumulation)."""
    placed, written, reduces = _wire_submit_seams_b07(
        monkeypatch,
        # requested reduce of 0.5, venue only let 0.2 through (capped by the net)
        executions=[{"execQty": "0.2", "execPrice": "99000", "execValue": "19800", "execFee": "0", "execTime": "1"}],
    )
    plan = _resize_plan(
        side="Sell", reduce_only=True, qty=0.5, delta_notional=-50_000.0, reason="hedge_reduce",
    )

    result = hedge_runner._submit_plan(
        plan, hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000
    )

    assert result["fill_source"] == "venue"
    assert abs(result["qty"] - 0.2) < 1e-9
    # the reduce booked against trade rows is the ACTUAL filled qty, never the requested 0.5
    assert reduces and abs(reduces[0]["sold_qty"] - 0.2) < 1e-9
    assert abs(reduces[0]["exit_price"] - 99_000.0) < 1.0


def test_submit_plan_full_fill_is_terminal_filled(monkeypatch, tmp_path) -> None:
    """A fully-filled BUY keeps the terminal status='filled' booking (the deliberate
    2026-06-11 anti-double-booking design) — only short fills become 'partial'."""
    _placed, written, _ = _wire_submit_seams_b07(
        monkeypatch,
        executions=[{"execQty": "0.5", "execPrice": "100000", "execValue": "50000", "execFee": "0", "execTime": "1"}],
    )
    result = hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000,
    )
    assert result["fill_source"] == "venue"
    cfg = hedge_runner.ContinuousHedgeConfig()
    assert written[0][0] == cfg.orders_dataset
    assert written[0][1].to_dicts()[0]["status"] == "submitted_unconfirmed"
    order_row = {dataset: df.to_dicts()[0] for dataset, df in written}[cfg.orders_dataset]
    assert order_row["status"] == "filled"
    assert abs(order_row["filled_qty"] - 0.5) < 1e-9


def test_submit_plan_falls_back_when_fill_unreadable(monkeypatch, tmp_path) -> None:
    """If the venue execution read fails/returns empty, the order is still accepted
    (we hold an orderId), so the position we asked for must keep being tracked — the
    fill is booked at the requested qty + implied price but flagged read_failed so the
    operator can see it is unverified (no silent position drop)."""
    placed, written, _ = _wire_submit_seams_b07(monkeypatch, executions=[])  # empty venue view
    result = hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000,
    )
    assert result["fill_source"] == "read_failed"
    assert abs(result["qty"] - 0.5) < 1e-9  # tracks the requested qty, not dropped
    cfg = hedge_runner.ContinuousHedgeConfig()
    rows = [df.to_dicts()[0] for dataset, df in written if dataset == cfg.orders_dataset]
    assert [row["status"] for row in rows] == ["submitted_unconfirmed", "submitted_unconfirmed"]
    assert rows[-1]["filled_qty"] == 0.0
    trade = [df.to_dicts()[0] for dataset, df in written if dataset == cfg.trades_dataset][-1]
    assert trade["provisional_entry_fill"] is True
    assert trade["entry_price_source"] == "planned_fallback"


def test_submit_plan_recovers_fill_from_order_history_when_execution_stream_lags(
    monkeypatch, tmp_path
) -> None:
    """The live demo can expose a terminal order row before get_executions.

    That is still authoritative venue fill evidence and must beat the provisional
    requested-qty/implied-price fallback.
    """
    _placed, written, _ = _wire_submit_seams_b07(
        monkeypatch,
        executions=[],
        order_history=[
            {
                "orderLinkId": "lm-en-ca-BTC-s44we8",
                "orderStatus": "Filled",
                "cumExecQty": "0.5",
                "avgPrice": "101250",
            }
        ],
    )
    monkeypatch.setattr(
        hedge_runner,
        "hedge_order_link_id",
        lambda now_ms, symbol="BTCUSDT": "lm-en-ca-BTC-s44we8",
    )

    result = hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(),
        tmp_path,
        tmp_path,
        now_ms=1_700_000_000_000,
    )

    assert result["fill_source"] == "order_history"
    assert result["qty"] == pytest.approx(0.5)
    cfg = hedge_runner.ContinuousHedgeConfig()
    order = [df.to_dicts()[0] for dataset, df in written if dataset == cfg.orders_dataset][-1]
    trade = [df.to_dicts()[0] for dataset, df in written if dataset == cfg.trades_dataset][-1]
    assert order["status"] == "filled"
    assert order["fill_source"] == "order_history"
    assert trade["entry_price"] == pytest.approx(101_250.0)
    assert trade["provisional_entry_fill"] is False


# ---------------------------------------------------------------------------
# audit b07 realmoney-safety-2: the resolved demo flag is threaded into the client
# ---------------------------------------------------------------------------


class _TruthyFlag:
    """A truthy, identity-distinct stand-in for the resolved demo flag — `is True`
    would also hold for the inlined literal, so we forward a UNIQUE object and assert
    identity to prove the resolved flag is threaded, not a hardcoded True."""

    def __bool__(self) -> bool:
        return True


def test_submit_plan_threads_resolved_demo_flag_not_literal_true(monkeypatch, tmp_path) -> None:
    """The BybitPrivateClient in _submit_plan must be constructed with the demo flag
    RESOLVED by resolve_private_credentials(), not a hardcoded literal True. A
    hardcoded True would silently mismatch the endpoint against the credential set if
    the surrounding REAL_MONEY guard ever changed."""
    import liquidity_migration.bybit as bybit_mod
    import liquidity_migration.event_demo as ed

    constructed: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def place_order(self, **params):
            return {"orderId": "oid-1"}

    sentinel_demo = _TruthyFlag()
    monkeypatch.setattr(bybit_mod, "resolve_private_credentials", lambda: ("k", "s", sentinel_demo))
    monkeypatch.setattr(bybit_mod, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(
        ed, "_wait_for_execution_summary",
        lambda client, **kw: {"qty": "0.5", "avg_price": 100_000.0},
    )
    monkeypatch.setattr(
        hedge_runner, "_instrument_filters",
        lambda symbol, root: {"qty_step": 0.001, "min_order_qty": 0.001, "min_notional_value": 5.0},
    )
    monkeypatch.setattr(hedge_runner, "write_dataset", lambda *a, **k: None)
    monkeypatch.setattr(hedge_runner, "_apply_hedge_reduce_to_trades", lambda *a, **k: None)

    hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000,
    )
    assert constructed and constructed[0]["demo"] is sentinel_demo


def test_live_wallet_equity_threads_resolved_demo_flag(monkeypatch) -> None:
    """The equity-read client path threads the resolved demo flag too (line 443)."""
    import liquidity_migration.bybit as bybit_mod
    import liquidity_migration.event_demo as ed

    constructed: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def get_wallet_balance(self):
            return {}

    sentinel_demo = _TruthyFlag()
    monkeypatch.setattr(bybit_mod, "resolve_private_credentials", lambda: ("k", "s", sentinel_demo))
    monkeypatch.setattr(bybit_mod, "BybitPrivateClient", FakeClient)
    monkeypatch.setattr(ed, "wallet_equity_usdt", lambda payload: 1234.0)

    assert hedge_runner._live_wallet_equity_usdt() == 1234.0
    assert constructed and constructed[0]["demo"] is sentinel_demo


# ---------------------------------------------------------------------------
# audit b07 hedge-5: mixed stale run submits the reduce-only leg, blocks the add
# ---------------------------------------------------------------------------


def test_stale_warmstart_mixed_run_submits_reduce_blocks_add(monkeypatch, tmp_path, capsys) -> None:
    """hedge-5: in a MIXED run under a stale warm-start (one add leg + one reduce-only
    leg) the risk-REDUCING reduce-only Sell must still submit; only the risk-increasing
    add leg is blocked. The original terminal block rejected the whole run, holding a
    risk-reducing trim hostage to a sibling add leg. The blocked add must still page
    (nonzero exit via submit_partial_blocked_stale_warmstart)."""
    add_btc = _resize_plan(symbol="BTCUSDT", side="Buy", reduce_only=False, qty=0.5)
    reduce_eth = _resize_plan(
        symbol="ETHUSDT", side="Sell", reduce_only=True, qty=0.05,
        delta_notional=-150.0, reason="hedge_reduce",
    )
    _setup_runner(
        monkeypatch, tmp_path,
        warmstart_last=date.today() - timedelta(days=10),
        argv=["--submit", "--btc-price", "100000", "--eth-price", "3000", "--equity-usdt", "10000"],
        warm_eth=[0.001] * 90,  # >=60 non-None obs -> 2f mode (two legs in one run)
    )
    monkeypatch.setattr(
        hedge_runner, "compute_hedge_decision_2f", lambda cfg, **kw: _decision_2f(add_btc, reduce_eth)
    )
    submitted_plans: list = []
    monkeypatch.setattr(
        hedge_runner, "_submit_plan",
        lambda plan, cfg, data_root, primary_root, now_ms: (
            submitted_plans.append(plan)
            or {"symbol": plan.symbol, "side": plan.side, "qty": plan.qty,
                "reduce_only": plan.reduce_only, "order_id": "oid-1", "link": "l-1"}
        ),
    )

    rc = hedge_runner.main()
    out = json.loads(capsys.readouterr().out)

    assert out["warmstart_stale"] is True
    # only the reduce-only leg was actually submitted
    assert [p.symbol for p in submitted_plans] == ["ETHUSDT"]
    assert [leg["symbol"] for leg in out["submitted"]] == ["ETHUSDT"]
    # the add leg is reported blocked, NOT submitted
    assert [leg["symbol"] for leg in out["blocked_legs"]] == ["BTCUSDT"]
    # a trim went through but an add was blocked -> partial-blocked status that PAGES
    assert out["status"] == "submit_partial_blocked_stale_warmstart"
    assert rc == 1
