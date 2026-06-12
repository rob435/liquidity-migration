"""Sniper live-wiring tests (S1 Amendment 6 → demo cycle, 2026-06-10).

Covers: placement (rounding, PostOnly limit, stop attached, dry-run vs submit),
fill reconciliation into first-class open trade rows, cancel-on-base-exit, and
base_exited exit planning. Uses a fake trading client (no I/O).
"""

from __future__ import annotations

import polars as pl

from liquidity_migration.continuous_demo import (
    SNIPER_REASON,
    ContinuousDemoCycleConfig,
    _execute_sniper_placements,
    _sniper_round_qty,
    reconcile_continuous_snipes,
)


class FakeClient:
    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.open_orders: list[dict] = []
        self.history: list[dict] = []

    def place_order(self, **params):
        self.placed.append(params)
        return {"orderId": f"oid-{len(self.placed)}"}

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self.cancelled.append((symbol, order_link_id))
        return {}

    def get_open_orders(self, *, symbol: str | None = None, **_kw):
        return [o for o in self.open_orders if symbol is None or o.get("symbol") == symbol]

    def get_order_history(self, *, symbol: str | None = None, order_link_id: str | None = None, **_kw):
        return [h for h in self.history
                if (symbol is None or h.get("symbol") == symbol)
                and (order_link_id is None or h.get("orderLinkId") == order_link_id)]


def _entry_row(symbol="AAAUSDT", trade_id="t1", price=100.0, qty=10.0):
    return {"symbol": symbol, "trade_id": trade_id, "entry_price": price, "qty": qty,
            "notional_usdt": price * qty, "signal_ts_ms": 1_700_000_000_000,
            "tick_size": 0.01, "qty_step": 0.1}


def _cfg(**kw):
    base = dict(sniper_enabled=True, sniper_wick_pct=0.08, sniper_size_frac=0.25,
                stop_loss_pct=0.25, submit_orders=True, confirm_demo_orders=True)
    base.update(kw)
    return ContinuousDemoCycleConfig(**base)


def test_round_qty_floors_to_step() -> None:
    assert _sniper_round_qty(2.5, 0.1) == 2.5
    assert abs(_sniper_round_qty(2.57, 0.1) - 2.5) < 1e-12
    assert _sniper_round_qty(0.04, 0.1) == 0.0
    assert _sniper_round_qty(5.0, 0.0) == 5.0


def test_placement_submits_postonly_limit_with_stop() -> None:
    client = FakeClient()
    demo = _cfg()
    orders = _execute_sniper_placements(
        [_entry_row()], trading_client=client, demo=demo, now_ms=1_700_000_100_000,
        strategy_id="s", price_by_symbol={"AAAUSDT": 100.0})
    assert len(orders) == 1
    o = orders[0]
    assert o["status"] == "resting" and o["submit_mode"] == "submitted"
    assert o["reason"] == SNIPER_REASON and o["base_trade_id"] == "t1"
    assert o["trade_id"] == "t1-snipe"
    assert abs(o["limit_price"] - 108.0) < 1e-9
    p = client.placed[0]
    assert p["orderType"] == "Limit" and p["timeInForce"] == "PostOnly" and p["side"] == "Sell"
    assert p["reduceOnly"] is False
    assert abs(float(p["price"]) - 108.0) < 1e-9
    assert float(p["stopLoss"]) > 108.0  # short disaster stop above the limit
    assert float(p["qty"]) == 2.5  # 0.25 * 10


def test_placement_dry_run_records_without_client() -> None:
    demo = _cfg(submit_orders=False, confirm_demo_orders=False)
    orders = _execute_sniper_placements(
        [_entry_row()], trading_client=None, demo=demo, now_ms=1, strategy_id="s",
        price_by_symbol={})
    assert len(orders) == 1 and orders[0]["submit_mode"] == "dry_run" and orders[0]["status"] == "resting"


def test_placement_disabled_returns_nothing() -> None:
    demo = _cfg(sniper_enabled=False)
    assert _execute_sniper_placements([_entry_row()], trading_client=None, demo=demo,
                                      now_ms=1, strategy_id="s", price_by_symbol={}) == []


def _orders_df(rows):
    return pl.DataFrame(rows, infer_schema_length=None)


def _resting_order_row(link="lnk1", symbol="AAAUSDT", base="t1", submitted=True):
    return {"order_link_id": link, "ts_ms": 1, "trade_id": f"{base}-snipe", "strategy_id": "s",
            "symbol": symbol, "side": "Sell", "order_type": "Limit", "qty": "2.5",
            "reduce_only": False, "order_id": "oid-1",
            "submit_mode": "submitted" if submitted else "dry_run", "status": "resting",
            "signal_ts_ms": 1, "tick_size": 0.01, "qty_step": 0.1, "stop_price": 135.0,
            "stop_loss_pct": 0.25, "sleeve": "continuous", "reason": SNIPER_REASON,
            "limit_price": 108.0, "base_trade_id": base}


def _trades_df(base_open=True, with_snipe=False, snipe_open=True):
    rows = [{"trade_id": "t1", "status": "open" if base_open else "closed", "symbol": "AAAUSDT",
             "equity_usdt": 10_000.0}]
    if with_snipe:
        rows.append({"trade_id": "t1-snipe", "status": "open" if snipe_open else "closed",
                     "symbol": "AAAUSDT", "base_trade_id": "t1"})
    return pl.DataFrame(rows, infer_schema_length=None)


def test_reconcile_fill_creates_open_trade_row() -> None:
    client = FakeClient()
    client.open_orders = []  # no longer resting on venue
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "2.5",
                       "avgPrice": "108.0", "updatedTime": "1700000200000",
                       "orderStatus": "Filled"}]
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert len(fills) == 1
    f = fills[0]
    assert f["trade_id"] == "t1-snipe" and f["status"] == "open" and f["side"] == "short"
    assert f["entry_price"] == 108.0 and f["base_trade_id"] == "t1"
    # REGRESSION (audit 2026-06-12 round 3): snipe fill rows must inherit the
    # base trade's equity — a hardcoded 0.0 zeroed snipe net_return and blocked
    # the armed hedge's book-state resolution while any snipe was open.
    assert f["equity_usdt"] == 10_000.0
    assert any(u["status"] == "filled" for u in updates)
    assert exits == []


def test_reconcile_nonterminal_partial_fill_is_not_booked() -> None:
    """REGRESSION (audit 2026-06-12): a PartiallyFilled order missing from a torn
    open-orders snapshot must NOT be booked/terminalized off the partial — the fill
    branch requires positive terminal orderStatus evidence, exactly like the cancel
    branch. Otherwise the still-resting remainder becomes a ghost (never cancelled
    at base exit; later fills unledgered until position reconcile)."""
    client = FakeClient()
    client.open_orders = []  # torn/empty snapshot while the order actually still rests
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "1.0",
                       "avgPrice": "108.0", "updatedTime": "1700000200000",
                       "orderStatus": "PartiallyFilled"}]
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert fills == [] and updates == [] and exits == []
    # PartiallyFilledCanceled IS terminal: the partial books as the trade row.
    client.history[0]["orderStatus"] = "PartiallyFilledCanceled"
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert len(fills) == 1 and fills[0]["trade_id"] == "t1-snipe"
    assert any(u["status"] == "filled" for u in updates)


def test_recover_snipe_trade_id_from_link_round_trips_component_and_seq() -> None:
    """Round 3: a snipe link carries no base component / re-entry seq, but its
    -x{crc} suffix is crc32(trade_id) — adoption can recover the EXACT live
    trade_id by enumerating the known component tags."""
    from liquidity_migration.continuous_demo import (
        _continuous_sniper_link_prefix,
        _continuous_suborder_link_id,
        _continuous_trade_id,
        recover_snipe_trade_id_from_link,
    )

    demo = _cfg()
    strategy_id = "continuous_fade_ensemble_v1"
    sig = 1_765_400_000_000
    for component in ("p3", "p4p3", "p4p5", "tp14", ""):
        for seq in (0, 1):
            base = _continuous_trade_id(strategy_id, "WIFUSDT", sig, seq)
            trade_id = (f"{base}-{component}-snipe" if component else f"{base}-snipe")
            link = _continuous_suborder_link_id(
                _continuous_sniper_link_prefix(demo), symbol="WIFUSDT",
                signal_ts_ms=sig, trade_id=trade_id,
            )
            assert recover_snipe_trade_id_from_link(
                link, strategy_id=strategy_id, symbol="WIFUSDT", signal_ts_ms=sig,
            ) == trade_id, (component, seq)
    # no -x suffix -> no recovery claim
    assert recover_snipe_trade_id_from_link(
        "lm-en-cs-WIFUSDT-abc123", strategy_id=strategy_id, symbol="WIFUSDT", signal_ts_ms=sig,
    ) is None


def test_reconcile_cancels_resting_snipe_when_base_closed() -> None:
    client = FakeClient()
    client.open_orders = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT"}]  # still resting
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=False), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=2)
    assert fills == []
    assert client.cancelled == [("AAAUSDT", "lnk1")]
    # REGRESSION (audit 2026-06-12 round 3): the cancel ack must NOT terminalize
    # the row — a PostOnly wick order can be PartiallyFilled before the base
    # exits, and a terminal "cancelled" here permanently unbooked the executed
    # leg. The row stays resting; history resolution terminalizes next cycle.
    assert all(u["status"] != "cancelled" for u in updates)
    assert any(u["status"] == "resting" and u["submit_mode"] == "cancel" for u in updates)

    # Next cycle: the venue reports the terminal truth — a partial fill before
    # our cancel. The executed leg books as a first-class trade row.
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "1.0",
                       "avgPrice": "108.0", "updatedTime": "1700000200000",
                       "orderStatus": "PartiallyFilledCanceled"}]
    fills, updates, _exits = reconcile_continuous_snipes(
        _trades_df(base_open=False), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=3)
    assert len(fills) == 1 and fills[0]["trade_id"] == "t1-snipe"
    assert fills[0]["qty"] == "1"
    assert any(u["status"] == "filled" for u in updates)


def test_reconcile_cancel_with_zero_fill_converges_to_cancelled() -> None:
    client = FakeClient()
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "0",
                       "avgPrice": "0", "updatedTime": "1700000200000",
                       "orderStatus": "Cancelled"}]
    fills, updates, _exits = reconcile_continuous_snipes(
        _trades_df(base_open=False), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=3)
    assert fills == []
    assert any(u["status"] == "cancelled" for u in updates)


def test_reconcile_keeps_resting_snipe_while_base_open() -> None:
    client = FakeClient()
    client.open_orders = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT"}]
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=2)
    assert fills == [] and updates == [] and exits == [] and client.cancelled == []


def test_reconcile_plans_base_exited_for_filled_snipe() -> None:
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=False, with_snipe=True),
        _orders_df([{**_resting_order_row(), "status": "filled"}]),
        trading_client=FakeClient(), demo=_cfg(), now_ms=3)
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "base_exited" and exits[0]["trade_id"] == "t1-snipe"


def test_reconcile_no_duplicate_fill_row() -> None:
    client = FakeClient()
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "2.5",
                       "avgPrice": "108.0", "updatedTime": "1700000200000"}]
    fills, _u, _e = reconcile_continuous_snipes(
        _trades_df(base_open=True, with_snipe=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=4)
    assert fills == []  # trade row already exists


def test_reconcile_dry_run_retires_with_base() -> None:
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=False), _orders_df([_resting_order_row(submitted=False)]),
        trading_client=None, demo=_cfg(submit_orders=False, confirm_demo_orders=False), now_ms=5)
    assert fills == [] and len(updates) == 1 and updates[0]["status"] == "cancelled"


def test_reconcile_empty_history_leaves_snipe_unresolved() -> None:
    """REGRESSION (audit 2026-06-11, empty-fetch-as-deletion class): an order absent
    from a (possibly degraded/empty) get_open_orders snapshot with NO history row must
    NOT be terminalized — terminal 'cancelled' is permanent (_sniper_order_state), so a
    still-resting PostOnly order became an unmanaged ghost: never cancelled, and its
    later fill produced no trade row. No evidence -> retry next cycle."""
    client = FakeClient()
    client.open_orders = []  # link missing from the snapshot
    client.history = []      # ...and the venue has no history row yet (lag)
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert fills == [] and updates == [] and exits == []


def test_reconcile_nonterminal_history_status_is_not_cancelled() -> None:
    client = FakeClient()
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "0",
                       "avgPrice": "0", "orderStatus": "New",
                       "updatedTime": "1700000200000"}]
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert fills == [] and updates == []


def test_reconcile_terminal_cancelled_history_terminalizes() -> None:
    client = FakeClient()
    client.open_orders = []
    client.history = [{"orderLinkId": "lnk1", "symbol": "AAAUSDT", "cumExecQty": "0",
                       "avgPrice": "0", "orderStatus": "Cancelled",
                       "updatedTime": "1700000200000"}]
    fills, updates, exits = reconcile_continuous_snipes(
        _trades_df(base_open=True), _orders_df([_resting_order_row()]),
        trading_client=client, demo=_cfg(), now_ms=1_700_000_300_000)
    assert fills == []
    assert any(u["status"] == "cancelled" for u in updates)
