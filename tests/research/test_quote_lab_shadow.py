"""Synthetic-tape tests for the quote lab: book mirror, shadow fills, summary."""

from __future__ import annotations

import json
from typing import Any

import pytest

from liquidity_migration.research.execution.quote_lab.book import BookMirror
from liquidity_migration.research.execution.quote_lab.shadow import (
    ShadowOutcome,
    ShadowPolicy,
    run_shadow_attempts,
)
from liquidity_migration.research.execution.quote_lab.summary import summarize_outcomes

NS = 1_000_000_000
BASE_S = 1_000.0  # synthetic clock start; zero would read as a missing timestamp
SYMBOL = "TESTUSDT"


def ts_ns(ts_s: float) -> int:
    return int((BASE_S + ts_s) * NS)


def snap(
    ts_s: float,
    bids: list[list[float]],
    asks: list[list[float]],
    *,
    gap: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "orderbook_snapshot",
        "symbol": SYMBOL,
        "local_receive_ts_ns": ts_ns(ts_s),
        "bids": bids,
        "asks": asks,
        "sequence_gap": gap,
        "restart_snapshot": restart,
    }


def delta(
    ts_s: float,
    bids: list[list[float]] | None = None,
    asks: list[list[float]] | None = None,
    *,
    gap: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "orderbook_delta",
        "symbol": SYMBOL,
        "local_receive_ts_ns": ts_ns(ts_s),
        "bids": bids or [],
        "asks": asks or [],
        "sequence_gap": gap,
        "restart_snapshot": False,
    }


def trade(ts_s: float, price: float, qty: float, side: str) -> dict[str, Any]:
    return {
        "kind": "public_trade",
        "symbol": SYMBOL,
        "local_receive_ts_ns": ts_ns(ts_s),
        "price": price,
        "qty": qty,
        "side": side,
        "tick_direction": "ZeroMinusTick",
    }


def buy_policy(**overrides: Any) -> ShadowPolicy:
    base: dict[str, Any] = {"side": "Buy", "placement": "join", "tick_size": 0.01}
    base.update(overrides)
    return ShadowPolicy(**base)


class TestBookMirror:
    def test_snapshot_delta_and_health_transitions(self) -> None:
        mirror = BookMirror()
        assert mirror.healthy(SYMBOL) is False
        assert mirror.last_receive_ts_ns(SYMBOL) is None

        # A delta before any snapshot must not build a book.
        mirror.apply(delta(1.0, bids=[[100.0, 5.0]]))
        assert mirror.healthy(SYMBOL) is False
        assert mirror.depth_at(SYMBOL, "Buy", 100.0) == 0.0

        mirror.apply(snap(2.0, [[100.0, 5.0], [99.99, 4.0]], [[100.02, 7.0]]))
        assert mirror.healthy(SYMBOL) is True
        assert mirror.best_bid(SYMBOL) == 100.0
        assert mirror.best_ask(SYMBOL) == 100.02
        assert mirror.depth_at(SYMBOL, "Buy", 100.0) == 5.0
        assert mirror.depth_at(SYMBOL, "Sell", 100.02) == 7.0

        mirror.apply(delta(3.0, bids=[[100.0, 3.0]]))
        assert mirror.depth_at(SYMBOL, "Buy", 100.0) == 3.0

        mirror.apply(delta(4.0, bids=[[100.0, 0.0]]))
        assert mirror.depth_at(SYMBOL, "Buy", 100.0) == 0.0
        assert mirror.best_bid(SYMBOL) == 99.99

        # A gap freezes the book until the next clean snapshot.
        mirror.apply(delta(5.0, bids=[[99.0, 9.0]], gap=True))
        assert mirror.healthy(SYMBOL) is False
        assert mirror.depth_at(SYMBOL, "Buy", 99.0) == 0.0
        mirror.apply(delta(6.0, bids=[[98.0, 1.0]]))
        assert mirror.healthy(SYMBOL) is False
        assert mirror.depth_at(SYMBOL, "Buy", 98.0) == 0.0

        mirror.apply(snap(7.0, [[101.0, 2.0]], [[101.02, 2.0]]))
        assert mirror.healthy(SYMBOL) is True
        assert mirror.best_bid(SYMBOL) == 101.0
        assert mirror.depth_at(SYMBOL, "Buy", 99.99) == 0.0
        assert mirror.last_receive_ts_ns(SYMBOL) == ts_ns(7.0)

    def test_crossed_book_and_restart_snapshot_are_unhealthy(self) -> None:
        mirror = BookMirror()
        mirror.apply(snap(1.0, [[101.0, 1.0]], [[100.5, 1.0]]))
        assert mirror.healthy(SYMBOL) is False

        mirror.apply(snap(2.0, [[100.0, 1.0]], [[100.02, 1.0]], restart=True))
        assert mirror.healthy(SYMBOL) is False
        mirror.apply(snap(3.0, [[100.0, 1.0]], [[100.02, 1.0]]))
        assert mirror.healthy(SYMBOL) is True

    def test_trades_do_not_change_book_but_update_last_trade(self) -> None:
        mirror = BookMirror()
        mirror.apply(snap(1.0, [[100.0, 5.0]], [[100.02, 7.0]]))
        mirror.apply(trade(2.0, 100.0, 1.5, "Sell"))
        assert mirror.depth_at(SYMBOL, "Buy", 100.0) == 5.0
        assert mirror.last_trade(SYMBOL) == (100.0, "Sell", ts_ns(2.0))
        assert mirror.last_receive_ts_ns(SYMBOL) == ts_ns(2.0)


class TestShadowFills:
    def test_queue_consumption_fills_after_queue_clears(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            trade(1.0, 100.0, 6.0, "Sell"),
            trade(2.0, 100.0, 4.0, "Sell"),
            delta(12.0),
            delta(32.0),
            delta(62.0),
            delta(302.0),
        ]
        outcomes = run_shadow_attempts(records, buy_policy(), 1000.0)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.filled_conservative is True
        assert outcome.filled_optimistic is True
        assert outcome.traded_through is False
        assert outcome.time_to_fill_s_conservative == pytest.approx(2.0)
        assert outcome.time_to_fill_s_optimistic == pytest.approx(2.0)
        assert outcome.queue_ahead_at_placement == 10.0
        assert outcome.placed_prices == [100.0]
        assert outcome.terminal_reason == "filled"
        assert outcome.decision_spread_bp == pytest.approx(0.02 / 100.01 * 1e4)
        # The book never moved, so every horizon shows zero drift.
        assert outcome.adverse_markout_bp_10s == pytest.approx(0.0)
        assert outcome.adverse_markout_bp_300s == pytest.approx(0.0)

    def test_partial_queue_consumption_does_not_fill(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            trade(1.0, 100.0, 6.0, "Sell"),
            delta(125.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        assert outcome.filled_conservative is False
        assert outcome.filled_optimistic is False
        assert outcome.terminal_reason == "timeout"

    def test_cancel_shrink_fills_optimistic_but_not_conservative(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            delta(1.0, bids=[[100.0, 15.0]]),  # size arrives behind us
            delta(2.0, bids=[[100.0, 9.0]]),  # 6 leaves with no trades: cancels
            trade(3.0, 100.0, 5.0, "Sell"),
            delta(130.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        # Optimistic queue: 10 - 6 cancelled ahead = 4, then 5 trades -> filled.
        # Conservative queue: clamped to displayed 9, then 5 trades -> 4 left.
        assert outcome.filled_optimistic is True
        assert outcome.filled_conservative is False
        assert outcome.time_to_fill_s_optimistic == pytest.approx(3.0)
        assert outcome.time_to_fill_s_conservative is None
        assert outcome.terminal_reason == "timeout"
        assert outcome.terminal_ts_ns == ts_ns(120.0)

    def test_level_disappearing_clamps_queue_to_zero(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0], [99.99, 5.0]], [[100.02, 5.0]]),
            delta(1.0, bids=[[100.0, 0.0]]),  # whole level gone, no trades
            trade(2.0, 100.0, 0.5, "Sell"),  # first trade at our price fills us
            delta(125.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        assert outcome.filled_conservative is True
        assert outcome.filled_optimistic is True
        assert outcome.time_to_fill_s_conservative == pytest.approx(2.0)
        assert outcome.traded_through is False

    def test_traded_through_fills_both_models(self) -> None:
        records = [
            snap(0.0, [[100.0, 50.0]], [[100.02, 5.0]]),
            trade(1.0, 99.98, 1.0, "Sell"),
            delta(125.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        assert outcome.traded_through is True
        assert outcome.filled_conservative is True
        assert outcome.filled_optimistic is True
        assert outcome.time_to_fill_s_conservative == pytest.approx(1.0)
        assert outcome.terminal_reason == "filled"

    def test_timeout_expires_unfilled_with_no_markouts_past_tape_end(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            delta(125.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        assert outcome.filled_conservative is False
        assert outcome.filled_optimistic is False
        assert outcome.terminal_reason == "timeout"
        assert outcome.terminal_ts_ns == ts_ns(120.0)
        assert outcome.terminal_bid == 100.0
        assert outcome.adverse_markout_bp_10s is None
        assert outcome.adverse_markout_bp_30s is None
        assert outcome.adverse_markout_bp_60s is None
        assert outcome.adverse_markout_bp_300s is None

    def test_chase_reprices_at_new_touch_and_resets_queue(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            delta(1.0, bids=[[100.03, 7.0]], asks=[[100.02, 0.0], [100.05, 5.0]]),
            trade(2.0, 100.03, 7.0, "Sell"),
            delta(130.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(chase_ticks=2), 1000.0)[0]
        assert outcome.reprices == 1
        assert outcome.placed_prices == [100.0, 100.03]
        # The 7.0 trade exactly clears the reset queue at the new level.
        assert outcome.filled_conservative is True
        assert outcome.filled_optimistic is True
        assert outcome.time_to_fill_s_conservative == pytest.approx(2.0)
        assert outcome.queue_ahead_at_placement == 10.0

    def test_chase_budget_exhaustion_ends_the_attempt(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            delta(1.0, bids=[[100.03, 7.0]], asks=[[100.02, 0.0], [100.05, 5.0]]),
            delta(130.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(max_reprices=0), 1000.0)[0]
        assert outcome.terminal_reason == "chase_exhausted"
        assert outcome.terminal_ts_ns == ts_ns(1.0)
        assert outcome.filled_conservative is False
        assert outcome.filled_optimistic is False
        assert outcome.reprices == 0

    def test_market_moving_through_resting_price_counts_as_traded_through(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            # The ask falls to our bid without a trade print: a real resting
            # order would have been in the way.
            delta(1.0, bids=[[100.0, 0.0], [99.9, 1.0]], asks=[[100.02, 0.0], [100.0, 3.0]]),
            delta(130.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        assert outcome.traded_through is True
        assert outcome.filled_conservative is True
        assert outcome.terminal_reason == "filled"

    def test_markout_sign_positive_is_adverse_for_buy(self) -> None:
        records = [
            snap(0.0, [[100.0, 1.0]], [[100.02, 1.0]]),
            trade(1.0, 100.0, 2.0, "Sell"),
            delta(11.5, bids=[[100.0, 0.0], [99.0, 1.0]], asks=[[100.02, 0.0], [99.02, 1.0]]),
        ]
        outcome = run_shadow_attempts(records, buy_policy(), 1000.0)[0]
        expected = (100.01 - 99.01) / 100.01 * 1e4
        assert outcome.adverse_markout_bp_10s == pytest.approx(expected)

    def test_markout_sign_negative_is_favorable_for_sell(self) -> None:
        records = [
            snap(0.0, [[100.0, 1.0]], [[100.02, 1.0]]),
            trade(1.0, 100.02, 2.0, "Buy"),
            delta(11.5, bids=[[100.0, 0.0], [99.0, 1.0]], asks=[[100.02, 0.0], [99.02, 1.0]]),
        ]
        outcome = run_shadow_attempts(records, buy_policy(side="Sell"), 1000.0)[0]
        assert outcome.side == "Sell"
        expected = (100.01 - 99.01) / 100.01 * 1e4
        assert outcome.adverse_markout_bp_10s == pytest.approx(-expected)

    def test_both_sides_run_interleaved_in_one_pass(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.02, 5.0]]),
            trade(1.0, 100.0, 6.0, "Sell"),
            trade(2.0, 100.0, 4.0, "Sell"),
            delta(130.0),
        ]
        outcomes = run_shadow_attempts(records, buy_policy(), 1000.0, sides=("Buy", "Sell"))
        assert [outcome.side for outcome in outcomes] == ["Buy", "Sell"]
        buy, sell = outcomes
        assert buy.filled_conservative is True
        assert sell.filled_conservative is False
        assert sell.terminal_reason == "timeout"

    def test_improve_places_one_tick_inside_with_empty_queue(self) -> None:
        records = [
            snap(0.0, [[100.0, 10.0]], [[100.03, 5.0]]),
            trade(1.0, 100.01, 1.0, "Sell"),
            delta(130.0),
        ]
        outcome = run_shadow_attempts(records, buy_policy(placement="improve"), 1000.0)[0]
        assert outcome.placed_prices == [pytest.approx(100.01)]
        assert outcome.queue_ahead_at_placement == 0.0
        # Empty level: the first trade at our price fills us.
        assert outcome.filled_conservative is True
        assert outcome.time_to_fill_s_conservative == pytest.approx(1.0)


class TestSummary:
    def _outcome(self, **overrides: Any) -> ShadowOutcome:
        base: dict[str, Any] = {
            "symbol": "T",
            "side": "Buy",
            "decision_ts_ns": 1,
            "decision_bid": 100.0,
            "decision_ask": 100.02,
            "placed_prices": [100.0],
            "filled_conservative": True,
            "filled_optimistic": True,
            "time_to_fill_s_conservative": 5.0,
            "time_to_fill_s_optimistic": 3.0,
            "traded_through": False,
            "reprices": 0,
            "terminal_ts_ns": 2,
            "terminal_bid": 100.0,
            "terminal_ask": 100.02,
            "terminal_reason": "filled",
            "decision_spread_bp": 2.0,
            "queue_ahead_at_placement": 10.0,
        }
        base.update(overrides)
        return ShadowOutcome(**base)

    def test_aggregation_math_and_fill_conditioning(self) -> None:
        filled = self._outcome(adverse_markout_bp_10s=3.0)
        unfilled = self._outcome(
            filled_conservative=False,
            filled_optimistic=False,
            time_to_fill_s_conservative=None,
            time_to_fill_s_optimistic=None,
            terminal_reason="timeout",
            decision_spread_bp=4.0,
            # Present but must be excluded: markouts are conditioned on a fill.
            adverse_markout_bp_10s=99.0,
        )
        rows = summarize_outcomes([filled, unfilled], taker_fee_bp=5.5)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "T"
        assert row["side"] == "Buy"
        assert row["attempts"] == 2
        assert row["fill_rate_conservative"] == pytest.approx(0.5)
        assert row["fill_rate_optimistic"] == pytest.approx(0.5)
        assert row["traded_through_rate"] == 0.0
        assert row["median_time_to_fill_s_conservative"] == pytest.approx(5.0)
        assert row["median_time_to_fill_s_optimistic"] == pytest.approx(3.0)
        assert row["mean_decision_spread_bp"] == pytest.approx(3.0)
        assert row["half_spread_bp"] == pytest.approx(1.5)
        assert row["taker_cost_bp"] == pytest.approx(7.0)
        assert row["mean_adverse_markout_bp"]["10s"] == pytest.approx(3.0)
        assert row["mean_adverse_markout_bp"]["30s"] is None
        assert row["maker_edge_bp"]["10s"] == pytest.approx(1.5 - 3.0)
        assert row["maker_edge_bp"]["30s"] is None
        json.dumps(rows)

    def test_groups_split_by_symbol_and_side(self) -> None:
        rows = summarize_outcomes(
            [
                self._outcome(),
                self._outcome(side="Sell"),
                self._outcome(symbol="A"),
            ]
        )
        assert [(row["symbol"], row["side"]) for row in rows] == [("A", "Buy"), ("T", "Buy"), ("T", "Sell")]
        assert all(row["attempts"] == 1 for row in rows)
        assert all(row["taker_fee_bp"] == 5.5 for row in rows)
