"""Regression tests for audit bucket b07.

Each test pins a finding from the 2026-06-14 audit (ids in the docstrings). They
are written to FAIL on the original bug and PASS on the committed fix:

  hedge-2/hedge-3  scripts/run_continuous_hedge.py  — book the ACTUAL venue fill
                   (partial / venue-capped), never the requested qty at the
                   planner's implied price.
  hedge-5          scripts/run_continuous_hedge.py  — a stale warm-start blocks
                   only the risk-INCREASING legs; a reduce-only trim in the SAME
                   mixed run still submits.
  realmoney-safety-2  scripts/run_continuous_hedge.py — the resolved demo flag is
                   threaded into BybitPrivateClient, never a hardcoded True.
  reports-charts-3/4/5  liquidity_migration/volume_events_charts.py — y-axis
                   floor follows negative data; "Trades" header only with a real
                   trades column; legend multiples read over the common window.
  code-quality-3   liquidity_migration/event_demo.py — _pending_order_refs is
                   gone, the re-exported guard constants stay importable.
  test-gaps-6      liquidity_migration/momentum_signals.py — _attach_residual_
                   momentum is gone; the two genuinely-used helpers remain.
  telegram-alert-2  liquidity_migration/event_demo.py — run_event_risk_cycle
                   sends Telegram OUTSIDE the ledger lock.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import polars as pl

import scripts.run_continuous_hedge as hedge_runner
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan


# ---------------------------------------------------------------------------
# hedge-2 / hedge-3: _submit_plan books the ACTUAL venue fill
# ---------------------------------------------------------------------------


def _resize_plan(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.5,
    reduce_only: bool = False,
    delta_notional: float = 50_000.0,
    reason: str = "hedge_increase",
) -> ContinuousRebalanceResizePlan:
    return ContinuousRebalanceResizePlan(
        trade_id="hedge", symbol=symbol, side=side, reduce_only=reduce_only, qty=qty,
        current_notional_usdt=0.0, target_notional_usdt=abs(delta_notional),
        delta_notional_usdt=delta_notional, reason=reason,
    )


def _wire_submit_seams(monkeypatch, *, executions: list[dict], filters: dict | None = None):
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
    placed, written, _ = _wire_submit_seams(
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
    placed, written, reduces = _wire_submit_seams(
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
    _placed, written, _ = _wire_submit_seams(
        monkeypatch,
        executions=[{"execQty": "0.5", "execPrice": "100000", "execValue": "50000", "execFee": "0", "execTime": "1"}],
    )
    result = hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000,
    )
    assert result["fill_source"] == "venue"
    cfg = hedge_runner.ContinuousHedgeConfig()
    order_row = {dataset: df.to_dicts()[0] for dataset, df in written}[cfg.orders_dataset]
    assert order_row["status"] == "filled"
    assert abs(order_row["filled_qty"] - 0.5) < 1e-9


def test_submit_plan_falls_back_when_fill_unreadable(monkeypatch, tmp_path) -> None:
    """If the venue execution read fails/returns empty, the order is still accepted
    (we hold an orderId), so the position we asked for must keep being tracked — the
    fill is booked at the requested qty + implied price but flagged read_failed so the
    operator can see it is unverified (no silent position drop)."""
    placed, written, _ = _wire_submit_seams(monkeypatch, executions=[])  # empty venue view
    result = hedge_runner._submit_plan(
        _resize_plan(qty=0.5, delta_notional=50_000.0),
        hedge_runner.ContinuousHedgeConfig(), tmp_path, tmp_path, now_ms=1_700_000_000_000,
    )
    assert result["fill_source"] == "read_failed"
    assert abs(result["qty"] - 0.5) < 1e-9  # tracks the requested qty, not dropped


# ---------------------------------------------------------------------------
# realmoney-safety-2: the resolved demo flag is threaded into the client
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
# hedge-5: mixed stale run submits the reduce-only leg, blocks only the add leg
# ---------------------------------------------------------------------------


def _setup_runner(
    monkeypatch, tmp_path, *, warmstart_last: date, argv: list[str],
    warm_eth: list[float | None] | None = None,
) -> None:
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45
    # 2f mode needs >=60 non-None eth obs; default leaves single-leg mode.
    eth: list[float | None] = [None] * 90 if warm_eth is None else warm_eth
    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)
    monkeypatch.setattr(hedge_runner, "load_warmstart_2f", lambda path: (unit, btc, eth))
    monkeypatch.setattr(hedge_runner, "_warmstart_last_date", lambda path: warmstart_last)
    monkeypatch.setattr(
        hedge_runner, "_live_book_state",
        lambda root, dataset, cycles_dataset=None: hedge_runner.LiveBookState({}, 0.5, True, "test"),
    )
    monkeypatch.setattr(hedge_runner, "_current_hedge_qty", lambda root, dataset, symbol=None: 0.0)
    monkeypatch.setenv("CONFIRM_DEMO_ORDERS", "1")
    monkeypatch.delenv("REAL_MONEY", raising=False)
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("HEDGE_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py", *argv])


def _decision_2f(plan_btc, plan_eth):
    from liquidity_migration.continuous_hedge_manager import HedgeDecision2F

    return HedgeDecision2F(
        beta_window_days=90, ratio_btc=0.03, ratio_eth=0.02,
        target_btc_usdt=300.0, target_eth_usdt=200.0, n_obs_joint=90,
        plan_btc=plan_btc, plan_eth=plan_eth, fell_back_to_btc=False,
    )


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


# ---------------------------------------------------------------------------
# reports-charts-3: y-axis floor follows the data when it goes negative
# ---------------------------------------------------------------------------


def test_nice_axis_floor_follows_negative_data() -> None:
    """reports-charts-3: a levered (e.g. 4x) equity curve blowing through zero must
    not have its y-floor pinned at 0 — that draws the wipeout below the plot floor,
    hiding it. When min < 0 the floor follows the data."""
    from liquidity_migration.volume_events_charts import _nice_axis

    low, _high, ticks = _nice_axis(-0.27, 2.0, target_ticks=12)
    assert low < 0.0  # the floor descended below zero to show the blow-through
    assert low <= -0.27  # the worst point is inside the plot, not clipped at the axis
    assert min(ticks) <= -0.27


def test_nice_axis_still_clamps_floor_at_zero_for_nonnegative_data() -> None:
    """The common case (a $1-normalised curve that never drops below 0) keeps the
    floor clamped at 0 — the fix is scoped to negative data only, so a non-negative
    series whose padded floor would dip below 0 is still pinned at 0 (the axis never
    invents negative territory the curve never visited)."""
    from liquidity_migration.volume_events_charts import _nice_axis

    # min near zero: the 5% pad would push floor_candidate below 0; the clamp holds it at 0.
    low, _high, ticks = _nice_axis(0.02, 1.5, target_ticks=12)
    assert low == 0.0
    assert min(ticks) >= 0.0
    # a higher non-negative curve also never floors below 0.
    low2, _h2, _t2 = _nice_axis(1.0, 2.0, target_ticks=12)
    assert low2 >= 0.0


# ---------------------------------------------------------------------------
# reports-charts-4: "Trades" header only when a real trades column is present
# ---------------------------------------------------------------------------


def test_monthly_table_labels_days_when_no_trades_column() -> None:
    """reports-charts-4: a monthly frame carrying only month+strategy_return has NO
    trade counts. The rows must NOT claim "Trades: 0" for every month — the count is
    derived from per-month equity DAYS and the caller labels it "Days"."""
    from liquidity_migration.volume_events_charts import _has_columns, _monthly_table_rows

    monthly = pl.DataFrame(
        {"month": ["2025-01", "2025-02"], "strategy_return": [0.05, -0.02]}
    )
    equity = pl.DataFrame(
        {"date": ["2025-01-10", "2025-01-20", "2025-02-05"], "basket_return": [0.01, 0.0, -0.01]}
    )
    # the caller's count_label gate: "Trades" only with a real trades column
    has_real_monthly = (
        not monthly.is_empty() and _has_columns(monthly, "month", "strategy_return", "trades")
    )
    assert has_real_monthly is False  # -> caller renders "Days", not "Trades"

    rows = {r["month"]: r for r in _monthly_table_rows(equity=equity, monthly=monthly)}
    # real returns kept; counts are equity DAYS per month (2 in Jan, 1 in Feb), not 0
    assert abs(rows["2025-01"]["return"] - 0.05) < 1e-12
    assert rows["2025-01"]["count"] == 2
    assert rows["2025-02"]["count"] == 1


def test_monthly_table_labels_trades_when_trades_column_present() -> None:
    """Companion: a frame WITH a trades column keeps the honest "Trades" path."""
    from liquidity_migration.volume_events_charts import _has_columns, _monthly_table_rows

    monthly = pl.DataFrame(
        {"month": ["2025-01"], "strategy_return": [0.05], "trades": [7]}
    )
    assert _has_columns(monthly, "month", "strategy_return", "trades") is True
    rows = _monthly_table_rows(equity=pl.DataFrame(), monthly=monthly)
    assert rows[0]["count"] == 7


# ---------------------------------------------------------------------------
# reports-charts-5: legend multiples read over the common (earliest-end) window
# ---------------------------------------------------------------------------


def test_chart_final_values_uses_common_end_window() -> None:
    """reports-charts-5: when BTC ends before the (flat-extended) strategy curve, the
    legend multiple for EACH series must be read at the last date COMMON to all series
    — not each series' own last point, which compared spans of different length."""
    from liquidity_migration.volume_events_charts import _chart_final_values

    series = [
        {"name": "Strategy", "points": [
            {"date": "2025-01-01", "value": 1.0},
            {"date": "2025-02-01", "value": 1.5},
            {"date": "2025-03-01", "value": 2.0},  # flat-extended past BTC
        ]},
        {"name": "BTC", "points": [
            {"date": "2025-01-01", "value": 1.0},
            {"date": "2025-02-01", "value": 1.2},  # BTC ends here
        ]},
    ]
    finals = _chart_final_values(series)
    # common end is 2025-02-01: strategy read THERE (1.5), not at its 2025-03 end (2.0)
    assert abs(finals["Strategy"] - 1.5) < 1e-9
    assert abs(finals["BTC"] - 1.2) < 1e-9


# ---------------------------------------------------------------------------
# code-quality-3: _pending_order_refs deleted; re-exported guards still import
# ---------------------------------------------------------------------------


def test_pending_order_refs_is_gone() -> None:
    """code-quality-3: the orphaned _pending_order_refs (duplicated the live
    pending-order guard, could drift) is deleted."""
    import liquidity_migration.event_demo as ed

    assert not hasattr(ed, "_pending_order_refs")


def test_pending_order_guard_constants_still_importable_from_event_demo() -> None:
    """The pending-order guard constants are a re-export contract other modules/tests
    import from event_demo — deleting _pending_order_refs must NOT break them (the
    import is kept as a deliberate re-export, not pruned as 'unused')."""
    from liquidity_migration.event_demo import PENDING_ORDER_GUARD_MS, PENDING_ORDER_STATUSES

    assert PENDING_ORDER_GUARD_MS > 0
    assert isinstance(PENDING_ORDER_STATUSES, (set, frozenset, tuple, list))
    assert len(PENDING_ORDER_STATUSES) > 0


# ---------------------------------------------------------------------------
# test-gaps-6: dead _attach_residual_momentum removed; real helpers remain
# ---------------------------------------------------------------------------


def test_residual_momentum_dead_join_is_removed() -> None:
    """test-gaps-6: _attach_residual_momentum (orphaned SHORT-engine code whose
    docstring cited a non-existent pinning test) is deleted; the two genuinely-used
    helpers (daily_bars, add_returns_and_age) stay."""
    import liquidity_migration.momentum_signals as ms

    assert not hasattr(ms, "_attach_residual_momentum")
    assert hasattr(ms, "daily_bars")
    assert hasattr(ms, "add_returns_and_age")


# ---------------------------------------------------------------------------
# telegram-alert-2: run_event_risk_cycle sends Telegram OUTSIDE the ledger lock
# ---------------------------------------------------------------------------


def test_run_event_risk_cycle_notifies_outside_ledger_lock() -> None:
    """telegram-alert-2: the blocking Telegram send must run AFTER the
    event_demo_ledger.lock is released — a slow RTT inside the lock would stall any
    co-located ledger writer (the lock-held-I/O class the live sleeves eradicated).

    Source-structure invariant (deterministic, no full-cycle run): in
    run_event_risk_cycle the `_maybe_notify(...)` call must be DEDENTED below the
    `with exclusive_file_lock(...)` block — i.e. at a shallower indentation than the
    body inside the `with`. The original bug had _maybe_notify (and `return payload`)
    nested inside the with-block; the fix moves them out. We assert the structural
    position rather than re-running the heavy cycle."""
    import inspect
    import textwrap

    import liquidity_migration.event_demo as ed

    src = textwrap.dedent(inspect.getsource(ed.run_event_risk_cycle))
    lines = src.splitlines()

    def _indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    with_idx = next(
        i for i, ln in enumerate(lines)
        if "with exclusive_file_lock(" in ln and "event_demo_ledger.lock" in ln
    )
    with_indent = _indent(lines[with_idx])
    notify_idx = next(i for i, ln in enumerate(lines) if "_maybe_notify(" in ln)
    return_idx = next(
        i for i, ln in enumerate(lines)
        if ln.strip() == "return payload" and i > notify_idx
    )

    # the notify and the function's return must sit at (or below) the `with` header's
    # own indentation — NOT nested one level deeper inside the with-block body.
    assert _indent(lines[notify_idx]) <= with_indent, (
        "_maybe_notify must run OUTSIDE the event_demo_ledger.lock (telegram-alert-2)"
    )
    assert _indent(lines[return_idx]) <= with_indent
    # and it must come AFTER the with-block (textually below the lock acquisition).
    assert notify_idx > with_idx
