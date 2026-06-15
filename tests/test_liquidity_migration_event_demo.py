"""audit2c eventdemo_mark — build_position_pnl_snapshot must never mark at liqPrice.

A missing markPrice used to fall back to the LIQUIDATION price as the mark,
producing a wildly-wrong (report/telemetry only) PnL snapshot. The corrected
behavior prefers markPrice -> lastPrice -> avgPrice, and leaves the mark and
unrealized fields null when no genuine mark is available rather than
substituting liqPrice.
"""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from liquidity_migration import bybit, event_demo
from liquidity_migration.event_demo import (
    EventRiskCycleConfig,
    _execute_risk_exits,
    _orphan_close_pnl_from_records,
    build_position_pnl_snapshot,
    summarize_position_pnl,
)


def test_missing_mark_does_not_use_liqprice() -> None:
    # markPrice absent, liqPrice present (and far from avg, as a liq price is).
    # Old code marked at liqPrice=50 => mark_price 50.0 (wildly wrong); the fix
    # falls back to avgPrice (the next legitimate price), never liqPrice.
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "size": "10",
                "avgPrice": "100",
                "liqPrice": "50",
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    # The defect: never adopt liqPrice as the mark.
    assert row["mark_price"] != 50.0
    # markPrice/lastPrice absent -> mark falls back to avgPrice, not liqPrice.
    assert row["mark_price"] == 100.0
    # avgPrice is reported as the entry, untouched by the mark fix.
    assert row["avg_price"] == 100.0


def test_no_mark_or_avg_leaves_fields_null() -> None:
    # None of markPrice / lastPrice / avgPrice present, only liqPrice. Old code
    # marked at liqPrice=50; the fix leaves the mark/unrealized fields null.
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Buy",
                "size": "10",
                "liqPrice": "50",
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["mark_price"] is None
    assert row["unrealized_pnl_usdt"] is None
    assert row["pnl_pct"] is None


def test_lastprice_used_when_markprice_missing() -> None:
    # markPrice missing but lastPrice present -> mark falls back to lastPrice,
    # NOT liqPrice (old code would have returned liqPrice=50).
    rows = build_position_pnl_snapshot(
        [
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "10",
                "avgPrice": "100",
                "lastPrice": "95",
                "liqPrice": "50",
                "positionValue": "950",
                "unrealisedPnl": "50",
            }
        ]
    )

    assert rows[0]["mark_price"] == 95.0
    assert rows[0]["unrealized_pnl_usdt"] == 50.0


def test_summary_tolerates_null_mark_rows() -> None:
    # A null-mark row must not crash the summary aggregation or the sort key.
    rows = build_position_pnl_snapshot(
        [
            {"symbol": "NOMARKUSDT", "side": "Buy", "size": "10", "liqPrice": "50"},
            {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "10",
                "avgPrice": "100",
                "markPrice": "95",
                "positionValue": "950",
                "unrealisedPnl": "50",
            },
        ]
    )

    summary = summarize_position_pnl(rows)
    assert summary["positions"] == 2
    # The null-mark row contributes 0 to the totals; only the marked row counts.
    assert summary["unrealized_pnl_usdt"] == 50.0


# ---------------------------------------------------------------------------
# audit b07 code-quality-3: _pending_order_refs deleted; re-exported guards still import
# (relocated from tests/test_audit_fix_b07.py)
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
# audit b07 telegram-alert-2: run_event_risk_cycle sends Telegram OUTSIDE the ledger lock
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


# --- audit bucket b10 (relocated from test_audit_fix_b10.py) -----------------
# event-demo-core-2: the dry-run risk path keeps a no-price trade OPEN (no
# fabricated 0 close). event-demo-core-3: orphan-close aggregation is capped at
# the trade's own qty so a sibling sleeve's surplus close cannot reprice it.
def test_eventdemocore2_risk_exit_keeps_open_when_no_exit_price() -> None:
    """A max_hold exit on a delisted/no-ticker symbol in the dry-run risk path has
    no resolvable exit price (planned 0.0, no current price). The sibling cycle path
    guards this (BUG-5) and keeps the trade OPEN; the risk path must do the same
    rather than book a fabricated exit_price=0 / 0% return. Before the fix the
    unguarded `if fully_filled:` closed the trade at price 0."""
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "DEADUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
            }
        ]
    )
    rows, _orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "DEADUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 0.0,  # no planned price (delisted)
                "planned_exit_ts_ms": 1_700_000_000_000,
            }
        ],
        all_trades,
        trading_client=None,
        risk=EventRiskCycleConfig(submit_orders=False, record_dry_run=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={},  # no current price either
        tick_size_by_symbol={},
    )
    # No fabricated close: the trade is kept OPEN for retry.
    assert rows == [], "a no-price risk exit must NOT close the trade at price 0"


def test_eventdemocore2_risk_exit_closes_when_price_resolvable() -> None:
    """Control: when a price IS resolvable the dry-run risk path still closes the
    trade (the guard only blocks the price<=0 fabrication)."""
    all_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "entry_price": 100.0,
                "notional_usdt": 1_000.0,
                "equity_usdt": 10_000.0,
            }
        ]
    )
    rows, _orders = _execute_risk_exits(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "qty": "1",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": 1_700_000_000_000,
                "planned_exit_price": 95.0,
                "planned_exit_ts_ms": 1_700_000_000_000,
            }
        ],
        all_trades,
        trading_client=None,
        risk=EventRiskCycleConfig(submit_orders=False, record_dry_run=True),
        now_ms=1_700_000_060_000,
        price_by_symbol={"AAAUSDT": 95.0},
        tick_size_by_symbol={},
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "closed"
    assert rows[0]["exit_price"] == 95.0
    # short return = (100 - 95) / 100 = 0.05
    assert rows[0]["gross_trade_return"] == pytest.approx(0.05)


def test_eventdemocore3_orphan_close_caps_sibling_surplus_legs() -> None:
    """On the SHARED netted account get_closed_pnl is account-wide. If a SIBLING
    sleeve also closed the same side on this symbol since entry_ts_ms, those legs
    are returned here too. The matcher must cap aggregation at THIS trade's qty so
    a sibling's surplus close cannot reprice this leg. Trade qty=1; the venue shows
    our leg (size 1 @ 95) followed by a sibling's leg (size 4 @ 80). The exit must
    price off ONLY our leg (95.0), not the blended (95*1 + 80*4)/5 = 83.0."""
    trade = {
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "qty": "1",
        "entry_price": 100.0,
        "entry_ts_ms": 1_700_000_000_000,
        "notional_usdt": 1_000.0,
        "equity_usdt": 10_000.0,
    }
    records = [
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "95.0", "closedSize": "1",
         "execFee": "0.1", "orderId": "ours", "createdTime": "1700000040000"},
        # Sibling's same-side surplus close AFTER entry_ts_ms — must be excluded.
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "80.0", "closedSize": "4",
         "execFee": "0.4", "orderId": "sibling", "createdTime": "1700000050000"},
    ]
    backfill = _orphan_close_pnl_from_records(trade, now_ms=1_700_000_100_000, records=records)
    assert backfill["exit_price"] == pytest.approx(95.0), "sibling surplus leg must not reprice this trade"
    # Only our leg's fee is counted, not the sibling's.
    assert backfill["exit_fee_usdt"] == pytest.approx(0.1)
    # gross return prices off our 95.0: (100 - 95)/100 = 0.05.
    assert backfill["gross_trade_return"] == pytest.approx(0.05)


def test_eventdemocore3_orphan_close_aggregates_own_multi_leg() -> None:
    """Control: this trade's OWN multi-leg close (legs summing to the trade qty)
    is still fully aggregated — the cap only excludes legs BEYOND the ledgered qty."""
    trade = {
        "trade_id": "t1",
        "symbol": "AAAUSDT",
        "side": "short",
        "qty": "4",
        "entry_price": 100.0,
        "entry_ts_ms": 1_700_000_000_000,
        "notional_usdt": 1_000.0,
        "equity_usdt": 10_000.0,
    }
    records = [
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "96.0", "closedSize": "3",
         "execFee": "0.3", "orderId": "leg-1", "createdTime": "1700000040000"},
        {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": "94.0", "closedSize": "1",
         "execFee": "0.1", "orderId": "leg-2", "createdTime": "1700000050000"},
    ]
    backfill = _orphan_close_pnl_from_records(trade, now_ms=1_700_000_100_000, records=records)
    # qty-weighted across BOTH legs: (3*96 + 1*94)/4 = 95.5
    assert backfill["exit_price"] == pytest.approx(95.5)
    assert backfill["exit_fee_usdt"] == pytest.approx(0.4)
    assert backfill["orphan_close_legs"] == 2


# --------------------------------------------------------------------------
# realmoney-safety-1 (relocated from audit bucket iF): the new signing-layer
# confirm_real_money guard must NOT break the demo submit path. event_demo's
# private client factory builds a working demo client and submission fires
# normally (guard is a no-op for demo).
# --------------------------------------------------------------------------


def _stub_config() -> SimpleNamespace:
    """Minimal stand-in for the bits of ResearchConfig that _build_private_client
    reads (config.exchange.category / .testnet)."""
    return SimpleNamespace(exchange=SimpleNamespace(category="linear", testnet=False))


def test_build_private_client_demo_submits_normally(monkeypatch) -> None:
    """realmoney-safety-1 (VERIFY-ONLY): event_demo._build_private_client resolves
    demo credentials and builds a demo client. With self.demo=True the signing
    guard (`not self.demo and not self.confirm_real_money`) is a no-op, so
    place_order/cancel_order fire normally -- the demo book is unaffected by the
    new real-money fail-safe."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

        def cancel_order(self, **_params):
            return {"retCode": 0, "result": {"orderId": "demo-ok"}}

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    # Resolve to the demo account (the default; assert it explicitly here).
    monkeypatch.setattr(
        event_demo, "resolve_private_credentials", lambda: ("demo-key", "demo-secret", True)
    )

    client = event_demo._build_private_client(_stub_config())
    assert client.demo is True
    assert client.confirm_real_money is False  # never opted into real money

    # The new guard must NOT raise on a demo client: the submit goes through.
    # place_order/cancel_order return payload.get("result", {}) — the unwrapped result.
    result = client.place_order(
        symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="demo-1",
    )
    assert result["orderId"] == "demo-ok"
    cancelled = client.cancel_order(symbol="BTCUSDT", order_link_id="demo-1")
    assert cancelled["orderId"] == "demo-ok"


def test_build_private_client_keeps_real_money_blocked(monkeypatch) -> None:
    """realmoney-safety-1: the factory threads `demo` straight from the resolved
    flag and never opts into real money. If credentials ever resolve to
    real-money (demo=False) the constructed client still has confirm_real_money
    False, so the signing guard blocks the submit -- the extra safety layer holds
    and real money stays blocked (we do NOT thread an opt-in here)."""

    class FakeHTTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def place_order(self, **_params):  # pragma: no cover - must be blocked
            raise AssertionError("real-money submit should have been blocked")

    monkeypatch.setattr(bybit, "HTTP", FakeHTTP)
    monkeypatch.setattr(
        event_demo, "resolve_private_credentials", lambda: ("real-key", "real-secret", False)
    )

    client = event_demo._build_private_client(_stub_config())
    assert client.demo is False
    assert client.confirm_real_money is False  # factory never enables real money
    with pytest.raises(RuntimeError, match="REAL_MONEY"):
        client.place_order(
            symbol="BTCUSDT", side="Buy", orderType="Market", qty="1", orderLinkId="rm-1",
        )
