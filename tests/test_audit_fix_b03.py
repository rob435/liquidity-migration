"""Regression tests for audit bucket b03.

Each test would FAIL on the original bug and PASS on the fix. Covers:
  - test-gaps-7      : untracked LONG close-side sign (Buy -> Sell) is asserted
  - ws-risk-1        : a failed private-WS rebuild retries instead of latching None
  - ws-risk-2        : a failed public-ticker rebuild retries instead of latching None
  - ws-risk-4        : cap-qty predicate covers an addon-only sibling config
  - ws-risk-5        : the stale-WS watchdog keys off a private-only event clock
  - ws-risk-6        : partial reduce-only fills book realized PnL on the closed chunk
  - ws-risk-7        : _prune_closed_order_state is skipped on a degraded ledger read
  - ws-risk-8        : cold-start adoption uses cached equity, no per-orphan wallet REST
  - code-quality-5   : continuous_rebalance._finite_float delegates to _common.finite_float
  - sizing-rebalance-1: hedge engine matches the live twin on a None-today hedge return
  - decision-rule-2  : a 0-trade venue can never be a candidate, even under `legacy`
  - shadows-2        : a torn arm row is re-armed from the open ledger (self-heal)
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration import continuous_rebalance, ws_risk
from liquidity_migration.config import ResearchConfig
from liquidity_migration.continuous_dynexit_shadow import update_dynexit_shadow
from liquidity_migration.continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousHedgeState,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    compute_continuous_hedge_ratio,
)
from liquidity_migration.storage import read_dataset, write_dataset
from liquidity_migration.ws_risk import (
    EventWebSocketRiskConfig,
    EventWebSocketRiskEngine,
)

MS_PER_DAY = 86_400_000
T0 = 1_680_652_800_000  # 2023-04-05 00:00 UTC


# ---------------------------------------------------------------------------
# Fakes (mirror the ws_risk test suite's minimal doubles)
# ---------------------------------------------------------------------------
class _FakePrivateClient:
    def __init__(self, *, positions: list[dict[str, object]] | None = None) -> None:
        self.positions = positions if positions is not None else []
        self.orders: list[dict[str, object]] = []
        self.wallet_calls = 0

    def get_positions(self, *, settle_coin: str | None = None):
        return self.positions

    def get_open_orders(self, *, symbol: str | None = None, settle_coin: str | None = None):
        return []

    def place_order(self, **params):
        self.orders.append(params)
        return {"orderId": "rest-order-1"}

    def set_trading_stop(self, **params):
        return {}

    def get_wallet_balance(self, *, account_type=None, coin=None):
        self.wallet_calls += 1
        return {"list": [{"coin": [{"coin": "USDT", "equity": "10000", "walletBalance": "10000"}]}]}

    def get_trade_history(self, *, symbol=None, order_link_id=None, limit=50):
        return [{"orderLinkId": order_link_id, "execQty": "1", "execPrice": "113",
                 "execValue": "113", "execFee": "0.01"}]

    def get_order_history(self, *, symbol=None, order_link_id=None, limit=50):
        return []


class _FakePrivateStream:
    def subscribe_positions(self, callback):
        pass

    def subscribe_orders(self, callback):
        pass

    def subscribe_executions(self, callback, *, fast: bool = False):
        pass

    def close(self):
        pass


class _FakePublicStream:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    def subscribe_tickers(self, symbols, callback):
        self.symbols.extend(symbols if isinstance(symbols, list) else [symbols])

    def close(self):
        pass


def _long_position(symbol: str = "AAAUSDT") -> dict[str, object]:
    # A LONG (Buy) position: close_side must be Sell (test-gaps-7).
    return {
        "symbol": symbol,
        "side": "Buy",
        "size": "1",
        "avgPrice": "100",
        "markPrice": "100",
        "positionValue": "100",
        "unrealisedPnl": "0",
        "stopLoss": "88",
        "takeProfit": "120",
    }


# ---------------------------------------------------------------------------
# test-gaps-7 — untracked LONG close-side sign
# ---------------------------------------------------------------------------
def test_untracked_long_position_is_closed_with_a_sell(tmp_path: Path) -> None:
    """test-gaps-7: the Buy->Sell long-close branch of close_side was never asserted.
    An inverted sign would close a LONG with a Buy (increasing exposure). Pin it."""
    private_client = _FakePrivateClient(positions=[_long_position()])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True,
            confirm_demo_orders=True,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
            exit_untracked_positions=True,
            adopt_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )

    engine.bootstrap()

    assert len(private_client.orders) == 1
    assert private_client.orders[0]["reduceOnly"] is True
    # The load-bearing assertion the suite was missing: a LONG closes with a SELL.
    assert private_client.orders[0]["side"] == "Sell"
    stored_orders = read_dataset(tmp_path, "event_demo_orders")
    assert stored_orders.select("side").item() == "Sell"
    assert stored_orders.select("exit_reason").item() == "untracked_position"


# ---------------------------------------------------------------------------
# ws-risk-1 / ws-risk-2 — failed rebuild must retry, not latch None
# ---------------------------------------------------------------------------
class _DeadPrivate:
    def is_connected(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def subscribe_positions(self, cb) -> None:  # noqa: ANN001
        pass

    def subscribe_orders(self, cb) -> None:  # noqa: ANN001
        pass

    def subscribe_executions(self, cb, **k) -> None:  # noqa: ANN001
        pass


class _LivePrivate(_DeadPrivate):
    def is_connected(self) -> bool:
        return True


def test_failed_private_rebuild_retries_instead_of_latching_none(tmp_path: Path, monkeypatch) -> None:
    """ws-risk-1: a rebuild that raises after the old socket is closed leaves
    private_stream=None. The old guard (`stream is None -> return`) then made every
    later on_idle pass a no-op forever (start_streams runs only once). The fix arms a
    pending-rebuild latch so a SUBSEQUENT pass retries and recovers."""
    attempts = {"n": 0}

    def _flaky_build(_config):  # noqa: ANN001, ANN202
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient build failure")
        return _LivePrivate()

    monkeypatch.setattr("liquidity_migration.ws_risk._build_private_stream", _flaky_build)

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(),
        # timeout=0 runs the build inline so the failure is deterministic.
        risk_config=EventWebSocketRiskConfig(
            private_ws_reconnect_seconds=30.0, stream_start_timeout_seconds=0.0
        ),
    )
    engine.private_stream = _DeadPrivate()  # type: ignore[assignment]

    # First reconnect pass: socket down past the bound -> build raises -> None + pending latch.
    engine._maybe_reconnect_private_stream(time.monotonic())  # arms _private_disconnected_since
    engine._private_disconnected_since = time.monotonic() - 999  # past the bound
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert engine.private_stream is None
    assert engine._private_rebuild_pending is True, "a failed rebuild must OWE a retry"

    # A LATER pass must retry the build even though private_stream is None.
    engine._last_private_reconnect_monotonic = time.monotonic() - 999  # clear the cooldown
    engine._private_disconnected_since = time.monotonic() - 999
    engine._maybe_reconnect_private_stream(time.monotonic())
    assert isinstance(engine.private_stream, _LivePrivate), "the next pass must rebuild, not stay blind"
    assert engine._private_rebuild_pending is False
    assert attempts["n"] == 2


def test_failed_public_rebuild_retries_instead_of_latching_none(tmp_path: Path, monkeypatch) -> None:
    """ws-risk-2: same structure as ws-risk-1 for the public ticker stream. A failed
    rebuild after the socket is closed leaves public_stream=None AND clears
    subscribed_symbols; without the pending latch every later pass is a no-op and the
    intrabar price feed silently degrades to the 30s REST mark forever."""
    attempts = {"n": 0}

    class _RebuiltPublic(_FakePublicStream):
        pass

    def _flaky_public(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient public build failure")
        return _RebuiltPublic()

    monkeypatch.setattr("liquidity_migration.ws_risk.BybitPublicTickerStream", _flaky_public)

    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(),
        risk_config=EventWebSocketRiskConfig(
            private_ws_reconnect_seconds=30.0, stream_start_timeout_seconds=0.0
        ),
    )
    engine.public_stream = _FakePublicStream()  # type: ignore[assignment]
    engine.state.subscribed_symbols = {"AAAUSDT"}

    now = time.monotonic()
    # Force a long silence so the public watchdog fires.
    engine._last_ticker_event_monotonic = now - 10_000
    engine._public_stream_built_monotonic = now - 10_000
    engine._maybe_reconnect_public_stream(now)
    assert engine.public_stream is None
    assert engine._public_rebuild_pending is True, "a failed public rebuild must OWE a retry"
    # The symbols to re-subscribe are preserved ACROSS the failed attempt.
    assert "AAAUSDT" in engine._public_resubscribe

    # A later pass retries and recovers, re-subscribing the saved symbol set.
    engine._last_public_reconnect_monotonic = now - 10_000
    engine._maybe_reconnect_public_stream(now)
    assert isinstance(engine.public_stream, _RebuiltPublic)
    assert engine._public_rebuild_pending is False
    assert "AAAUSDT" in engine.public_stream.symbols
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# ws-risk-4 — cap-qty predicate must include the addon root
# ---------------------------------------------------------------------------
def test_cap_qty_predicate_includes_addon_only_sibling(tmp_path: Path) -> None:
    """ws-risk-4: an engine with ONLY continuous_addon configured still has a netted
    sibling (short is always present), so the stop-cap must engage. The old predicate
    (long_root or continuous_root) omitted the addon root -> cap_qty_to_trade=False,
    letting a stop flatten the sibling leg. The fix keys off the owned-ledger count."""
    addon_root = tmp_path / "addon"
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(continuous_addon_data_root=str(addon_root)),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # short + continuous_addon == 2 owned ledgers -> cap engages.
    assert len(engine._sleeve_routes(trades=True)) == 2
    assert (len(engine._sleeve_routes(trades=True)) > 1) is True
    # The old buggy predicate would have been False for this config.
    old_predicate = engine.long_root is not None or engine.continuous_root is not None
    assert old_predicate is False, "the addon-only config exposes the omitted-root bug"

    # A short-only engine has a single owned ledger -> cap must NOT engage.
    short_only = EventWebSocketRiskEngine(
        tmp_path / "short_only",
        config=ResearchConfig(data_root=tmp_path / "short_only"),
        risk_config=EventWebSocketRiskConfig(),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    assert len(short_only._sleeve_routes(trades=True)) == 1
    assert (len(short_only._sleeve_routes(trades=True)) > 1) is False


# ---------------------------------------------------------------------------
# ws-risk-5 — stale-WS watchdog must see a dead PRIVATE stream while tickers flow
# ---------------------------------------------------------------------------
def test_stale_watchdog_fires_on_private_silence_while_tickers_flow(tmp_path: Path) -> None:
    """ws-risk-5: the all-events clock is bumped by ticker traffic too, so a dead
    private stream stayed invisible to the stale-WS watchdog while public tickers kept
    it warm. The fix tracks a private-only event clock; with positions held, private
    silence must force a REST reconcile even when ticker events are fresh."""
    private_client = _FakePrivateClient(positions=[])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False,
            repair_stops=False,
            order_submit_mode="rest",
            rest_reconcile_seconds=0.0,
            heartbeat_seconds=0.0,
            stale_ws_seconds=30.0,
            rest_fallback=True,
        ),
        private_client=private_client,
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # Hold a position so the watchdog expects private-stream traffic.
    engine.state.positions_by_symbol = {"AAAUSDT": _long_position()}

    now = time.monotonic()
    reconciles: list[float] = []
    engine.rest_reconcile = lambda *a, **k: reconciles.append(now)  # type: ignore[assignment]

    # Public ticker traffic keeps the all-events clock FRESH...
    engine.state.last_ws_event_monotonic = now
    # ...but the PRIVATE stream has been silent past the bound.
    engine.state.last_private_ws_event_monotonic = now - 999
    engine.state.last_stale_reconcile_monotonic = now - 999

    engine.reconcile_stale_websocket(now)
    assert reconciles, "private-stream silence must force a REST reconcile even with fresh tickers"
    assert any("private-stream" in e for e in engine.state.errors)


# ---------------------------------------------------------------------------
# ws-risk-6 — partial reduce-only fills book realized PnL on the closed chunk
# ---------------------------------------------------------------------------
def _open_continuous_trade(root: Path) -> None:
    write_dataset(
        pl.DataFrame(
            [{
                "trade_id": "t2", "symbol": "BBBUSDT", "side": "short", "status": "open",
                "qty": "3", "entry_price": 50.0, "equity_usdt": 10_000.0,
                "notional_usdt": 150.0, "stop_price": 56.0, "take_profit_price": 40.0,
                "planned_exit_ts_ms": 9_999_999_999_999,
            }]
        ),
        root,
        "event_demo_trades",
        partition_by=(),
    )


def test_partial_reduce_books_realized_loss_on_the_closed_chunk(tmp_path: Path) -> None:
    """ws-risk-6: the partial (not-fully-filled) branch booked NO realized PnL for the
    closed chunk — the loss was hidden until the residual fully closed, so the
    adverse-exit breaker read it as net 0. The fix accumulates the closed delta's
    realized return on the still-open row, and folds every leg into the final
    close's net_return."""
    _open_continuous_trade(tmp_path)
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=True, confirm_demo_orders=True, repair_stops=False,
            order_submit_mode="rest", rest_fallback=True, exit_untracked_positions=False,
            rest_reconcile_seconds=0.0, heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0, adopt_untracked_positions=False,
        ),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    engine.bootstrap()

    # Partial reduce: close 1 of 3 at 55 (a SHORT loss: entry 50 -> 55 = -10% gross).
    link1 = "lm-ux-c-BBBUSDT-reduce"
    engine._record_orders([{
        "order_link_id": link1, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "1", "target_qty": "1",
        "filled_qty": "", "exit_reason": "rebalance_reduce",
    }])
    engine.state.submitted_link_to_trade_id[link1] = "t2"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link1, filled_qty=1.0, exit_price=55.0, source="execution",
    )

    row = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t2").to_dicts()[0]
    assert row["status"] == "open"
    assert float(row["qty"]) == pytest.approx(2.0)
    # The closed chunk's realized return is now BOOKED on the open row (was absent).
    # gross = (50-55)/50 = -0.10; delta notional = 1*50 = 50; weight = 50/10000 = 0.005.
    assert float(row["partial_exit_gross_return"]) == pytest.approx(-0.10)
    assert float(row["partial_exit_realized_return"]) == pytest.approx(-0.10 * 0.005)
    assert float(row["rebalance_realized_return"]) == pytest.approx(-0.0005)

    # Close the residual 2 at 56 (another loss). The final net_return must fold BOTH
    # legs — not just the last delta — so the multi-leg close no longer understates.
    link2 = "lm-ux-c-BBBUSDT-final"
    engine._record_orders([{
        "order_link_id": link2, "trade_id": "t2", "symbol": "BBBUSDT", "side": "Buy",
        "reduce_only": True, "status": "submitted", "qty": "2", "target_qty": "2",
        "filled_qty": "", "exit_reason": "max_hold",
    }])
    engine.state.submitted_link_to_trade_id[link2] = "t2"
    engine.record_tracked_exit_stream_fill(
        order_link_id=link2, filled_qty=2.0, exit_price=56.0, source="execution",
    )

    closed = read_dataset(tmp_path, "event_demo_trades").filter(pl.col("trade_id") == "t2").to_dicts()[0]
    assert closed["status"] == "closed"
    # Final delta: gross = (50-56)/50 = -0.12; delta notional = 2*50 = 100; weight = 0.01.
    # net_return = prior(-0.0005) + (-0.12 * 0.01) = -0.0005 - 0.0012 = -0.0017.
    assert float(closed["net_return"]) == pytest.approx(-0.0017)
    # The original bug would have booked only the final leg (-0.0012), hiding -0.0005.
    assert float(closed["net_return"]) < -0.0012


# ---------------------------------------------------------------------------
# ws-risk-7 — prune is skipped on a degraded ledger read
# ---------------------------------------------------------------------------
def test_prune_closed_order_state_skipped_on_ledger_read_error(tmp_path: Path) -> None:
    """ws-risk-7: live_trade_ids derives from open_trades, which is incomplete when a
    sibling ledger read raised this pass. Pruning then evicts a still-open sibling
    order's link, and a later fill for it finds no order and is dropped. The fix bails
    out of pruning whenever ledger_read_error is set."""
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(telemetry_log_retention=2),
        private_client=_FakePrivateClient(),
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    # The OLDEST order (idx 0, beyond the retention grace window) belongs to a still-open
    # sibling trade that is INVISIBLE this pass because the sibling ledger read raised, so
    # open_trades is empty and the order looks trade-closed. WITHOUT the guard it would be
    # evicted; a later fill for its link would then find no order and be dropped.
    link = "lm-ux-c-CCC-open-sibling"
    orders = [{"order_link_id": link, "trade_id": "open-sibling", "symbol": "CCCUSDT",
               "status": "submitted", "exit_reason": "rebalance_reduce"}]
    orders += [
        {"order_link_id": f"recent-{i}", "trade_id": f"tx{i}", "symbol": "ZZZUSDT",
         "status": "filled", "exit_reason": "rebalance_reduce"}
        for i in range(5)
    ]
    engine._record_orders(orders)
    engine.state.submitted_link_to_trade_id[link] = "open-sibling"

    # With a ledger_read_error set, the prune MUST be a no-op (fail closed) — even though
    # the oldest order is well beyond the grace window.
    engine.state.ledger_read_error = "continuous:combined: RuntimeError: torn read"
    engine._prune_closed_order_state()
    assert link in engine.state.orders_by_link, "must NOT evict an old sibling order on a degraded read"
    assert engine.state.submitted_link_to_trade_id.get(link) == "open-sibling"
    assert engine.state.orders_evicted == 0

    # Clearing the error re-enables the bounded prune (so it is a guard, not a disable):
    # the oldest order IS now evictable, proving the guard — not a coincidental retention
    # window — is what protected it above.
    engine.state.ledger_read_error = ""
    engine._prune_closed_order_state()
    assert engine.state.orders_evicted > 0
    assert link not in engine.state.orders_by_link, "the unguarded prune evicts the old order"


# ---------------------------------------------------------------------------
# ws-risk-8 — cold-start adoption never fires a per-orphan wallet REST
# ---------------------------------------------------------------------------
def test_cold_start_adoption_uses_cached_equity_no_wallet_call(tmp_path: Path) -> None:
    """ws-risk-8: _build_adopted_trade stamped equity via
    `_last_equity_usdt or _account_equity_usdt()`. On cold start the cache is 0.0, so a
    blocking get_wallet_balance fired per adopted orphan on the latency-critical
    consumer thread. The fix seeds the cache ONCE before adoption and reads cache-only."""
    def _short(symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "side": "Sell", "size": "1", "avgPrice": "100",
                "markPrice": "100", "positionValue": "100", "unrealisedPnl": "0",
                "stopLoss": "112", "takeProfit": "80"}

    private_client = _FakePrivateClient(positions=[_short("DDDUSDT"), _short("EEEUSDT")])
    engine = EventWebSocketRiskEngine(
        tmp_path,
        config=ResearchConfig(data_root=tmp_path),
        risk_config=EventWebSocketRiskConfig(
            submit_orders=False, repair_stops=False, order_submit_mode="rest",
            rest_reconcile_seconds=0.0, heartbeat_seconds=0.0,
            untracked_position_grace_seconds=0.0,
            adopt_untracked_positions=True, exit_untracked_positions=False,
        ),
        private_client=private_client,
        private_stream=_FakePrivateStream(),
        public_stream=_FakePublicStream(),
    )
    engine.bootstrap()

    # The adopted rows carry a non-zero equity snapshot from the seeded cache.
    stored = read_dataset(tmp_path, "event_demo_trades")
    assert not stored.is_empty()
    assert all(e and float(e) > 0.0 for e in stored.select("equity_usdt").to_series().to_list())

    # The adoption builder reads cache-only -> never a wallet call. _build_adopted_trade
    # would have called get_wallet_balance once PER orphan (2 here) under the bug.
    before = private_client.wallet_calls
    built = engine._build_adopted_trade({"symbol": "FFFUSDT", "side": "Sell", "size": "1", "avgPrice": "100"},
                                        now_ms=ws_risk._now_ms())
    assert built is not None and float(built["equity_usdt"]) > 0.0
    assert private_client.wallet_calls == before, "adoption builder must not fire a wallet REST"


# ---------------------------------------------------------------------------
# code-quality-5 — _finite_float delegates to the canonical helper
# ---------------------------------------------------------------------------
def test_finite_float_delegates_and_keeps_drop_in_contract() -> None:
    """code-quality-5: continuous_rebalance._finite_float must route through the single
    canonical _common.finite_float (so a future NaN/inf policy tightening lands in ONE
    place) while preserving its float (never None), positional-default contract."""
    assert continuous_rebalance.finite_float is not None  # the import is wired
    # NaN / inf / non-numeric all collapse to the default (the finite guard).
    assert continuous_rebalance._finite_float(float("nan")) == 0.0
    assert continuous_rebalance._finite_float(float("inf")) == 0.0
    assert continuous_rebalance._finite_float("not-a-number") == 0.0
    assert continuous_rebalance._finite_float(None) == 0.0
    # Custom positional default is honored and the return is always a float, never None.
    out = continuous_rebalance._finite_float(None, -1.5)
    assert out == -1.5 and isinstance(out, float)
    # Valid finite values pass through unchanged.
    assert continuous_rebalance._finite_float("3.5") == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# sizing-rebalance-1 — engine matches the live twin on a None-today hedge return
# ---------------------------------------------------------------------------
def _hedge_components(raw: list[float]) -> ContinuousRebalanceComponents:
    days = [T0 + i * MS_PER_DAY for i in range(len(raw))]
    raw_by_day = {d: r for d, r in zip(days, raw)}
    return ContinuousRebalanceComponents(
        days=days,
        raw_by_day=raw_by_day,
        gross_by_day=dict(raw_by_day),
        cost_events={},
        funding_by_day={},
        active_gross_start={d: 0.0 for d in days},
        impact_exponent=0.5,
    )


def _rebalance_rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(
        realized_vol_window_days=90, target_daily_vol=0.045, max_scale=4.0,
        drawdown_half_threshold=-0.04, drawdown_zero_threshold=None,
        resize_cost_bps=10.0, strategy_momentum_window_days=0,
    )


def _hedge_rule() -> ContinuousHedgeRule:
    return ContinuousHedgeRule(beta_window_days=10, beta_min_obs=5, hedge_cap=2.0, cost_bps=5.0)


def test_hedge_engine_holds_position_on_none_today_like_the_live_twin() -> None:
    """sizing-rebalance-1: when the most-recent hedge return is None (a data-gap day),
    the backtest engine used to report hedge_ratio=0.0 (and charge a phantom
    close+reopen) while the parity-tested live twin holds a fully-sized hedge. The fix
    sizes the engine from beta UNCONDITIONALLY (matching the twin) and only gates the
    realized PnL contribution on the day's value."""
    n = 30
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h_vals = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [-0.5 * x + 0.001 for x in h_vals]
    comp = _hedge_components(raw)
    hr = _hedge_rule()

    # Drop the MOST-RECENT hedge return (None today) — a real, supported live input.
    h_with_gap = {d: v for d, v in zip(days, h_vals)}
    h_with_gap.pop(days[-1])

    df = apply_rebalance_rule(comp, _rebalance_rule(), hr, h_with_gap, {})
    engine_ratio_last = float(df["hedge_ratio"][-1])

    # The live twin sizes from the prior series alone (no 'today' gate).
    h_list = [h_with_gap.get(d) for d in days]
    twin_last = compute_continuous_hedge_ratio(
        ContinuousHedgeState(
            prior_raw_returns=tuple(raw[: n - 1]),
            prior_hedge_returns=tuple(h_list[: n - 1]),
        ),
        hr,
        target_scale=float(df["scale"][-1]),
    )
    # The engine now HOLDS the hedge on the gap day, matching the twin (was 0.0).
    assert engine_ratio_last > 0.0, "engine must hold the hedge on a None-today gap day"
    assert math.isclose(engine_ratio_last, twin_last, rel_tol=0, abs_tol=1e-12)
    # No realized hedge_return is booked for the missing day (contribution gated).
    assert float(df["hedge_return"][-1]) == 0.0


def test_hedge_engine_unchanged_on_fully_populated_series() -> None:
    """sizing-rebalance-1 guard: the fix must be numerically identical to the prior
    behaviour when every day has a hedge return (the parity-tested contiguous case)."""
    n = 30
    days = [T0 + i * MS_PER_DAY for i in range(n)]
    h_vals = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    raw = [-0.5 * x + 0.001 for x in h_vals]
    comp = _hedge_components(raw)
    hr = _hedge_rule()
    h = {d: v for d, v in zip(days, h_vals)}

    df = apply_rebalance_rule(comp, _rebalance_rule(), hr, h, {})
    h_list = [h.get(d) for d in days]
    for i in range(n):
        twin = compute_continuous_hedge_ratio(
            ContinuousHedgeState(
                prior_raw_returns=tuple(raw[:i]),
                prior_hedge_returns=tuple(h_list[:i]),
            ),
            hr,
            target_scale=float(df["scale"][i]),
        )
        assert math.isclose(twin, float(df["hedge_ratio"][i]), rel_tol=0, abs_tol=1e-12), i


# ---------------------------------------------------------------------------
# decision-rule-2 — a 0-trade venue can never be a candidate
# ---------------------------------------------------------------------------
def test_legacy_rule_rejects_zero_trade_binance_cell() -> None:
    """decision-rule-2: with `--rule legacy`, min_trades_binance defaults to 0, so the
    soft floor (`0 < 0`) never fires and a 0-trade Binance cell was rubber-stamped a
    candidate on the Bybit numbers alone. The fix blocks any venue with 0 executed
    trades regardless of preset (STATE.md non-negotiable #3: both venues matter)."""
    from scripts.apply_decision_rule import CellMetrics, evaluate_cell

    def _m(*, sharpe: float, dd: float, ret: float, trades: int) -> CellMetrics:
        return CellMetrics(
            cell_id="c", venue="x", sharpe_like=sharpe, max_drawdown=dd,
            total_return=ret, trades=trades,
        )

    # Control with real drawdown/return on both venues.
    control = {
        "bybit": _m(sharpe=1.0, dd=-0.30, ret=1.0, trades=400),
        "binance": _m(sharpe=1.0, dd=-0.30, ret=1.0, trades=400),
    }
    # Cell: bybit clears the legacy bar (Δsharpe +0.6, DD unchanged, positive return,
    # 350 trades); binance is PRESENT but executed ZERO trades.
    cell = {
        "bybit": _m(sharpe=1.6, dd=-0.30, ret=1.5, trades=350),
        "binance": _m(sharpe=1.6, dd=-0.30, ret=1.5, trades=0),
    }
    verdict = evaluate_cell(
        "cell-degenerate", cell, control,
        sharpe_delta_min=0.5, dd_delta_pp_max=-5.0,  # legacy preset values
        min_trades_bybit=30, min_trades_binance=0,   # the buggy legacy floor
    )
    assert verdict.verdict != "candidate", "a 0-trade Binance venue must never be a candidate"
    assert any("0 trades" in r for r in verdict.reasons)

    # Sanity: the same cell with real trades on BOTH venues still passes the legacy bar
    # (the guard targets only the degenerate 0-trade case, not legitimate cells).
    good = {
        "bybit": _m(sharpe=1.6, dd=-0.30, ret=1.5, trades=350),
        "binance": _m(sharpe=1.6, dd=-0.30, ret=1.5, trades=350),
    }
    ok = evaluate_cell(
        "cell-good", good, control,
        sharpe_delta_min=0.5, dd_delta_pp_max=-5.0,
        min_trades_bybit=30, min_trades_binance=0,
    )
    assert ok.verdict == "candidate"


# ---------------------------------------------------------------------------
# shadows-2 — a torn arm row is re-armed from the open ledger
# ---------------------------------------------------------------------------
def _shadow_klines(symbol: str, signal_ts: int) -> pl.DataFrame:
    # Enough hourly closes for compute_shadow_anchor (signal close + 24h + 1h earlier),
    # plus forward bars whose lows never touch the dynamic target (so the shadow stays
    # armed, not exited).
    rows = []
    for h in range(-25, 6):
        ts = signal_ts + h * 3_600_000
        rows.append({"symbol": symbol, "ts_ms": ts, "close": 100.0, "low": 99.0, "high": 101.0})
    return pl.DataFrame(rows)


def test_torn_arm_is_recovered_from_open_ledger(tmp_path: Path) -> None:
    """shadows-2: an arm whose JSONL line was torn by a mid-write crash is no longer
    'fresh' next cycle (it is already open in the ledger), so the fresh-only arm loop
    would never re-arm it and the shadow observation was lost forever. The fix scans
    the open ledger trades and re-arms any not yet armed/exited from the row itself."""
    signal_ts = T0 + 30 * 3_600_000
    klines = _shadow_klines("BBBUSDT", signal_ts)
    open_trades = pl.DataFrame([{
        "trade_id": "tlost", "symbol": "BBBUSDT", "status": "open",
        "entry_price": 100.0, "signal_ts_ms": signal_ts, "entry_ts_ms": signal_ts,
    }])

    # The trade is ALREADY open in the ledger but is NOT in fresh_entries (its arm row
    # was torn away on a prior cycle and replay dropped the truncated line).
    stats = update_dynexit_shadow(
        tmp_path, all_trades=open_trades, fresh_entries=[], klines=klines, now_ms=signal_ts + 4 * 3_600_000,
    )
    assert stats["armed"] == 1, "an open ledger trade with no arm row must be re-armed (self-heal)"

    jsonl = (tmp_path / "continuous_dynexit_shadow.jsonl").read_text(encoding="utf-8")
    assert '"event":"arm"' in jsonl and '"trade_id":"tlost"' in jsonl

    # Idempotent: a second sweep with the trade now armed must NOT double-arm it.
    stats2 = update_dynexit_shadow(
        tmp_path, all_trades=open_trades, fresh_entries=[], klines=klines, now_ms=signal_ts + 5 * 3_600_000,
    )
    assert stats2["armed"] == 0, "re-arming must be idempotent across cycles"
