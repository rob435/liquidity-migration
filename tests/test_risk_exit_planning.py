"""Event-demo planning tests — split from the monolithic test_liquidity_migration_event_demo.py."""

from __future__ import annotations


import polars as pl

from liquidity_migration.event_demo import (
    plan_risk_exits,
    plan_stop_repairs,
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

def test_plan_risk_exits_uses_live_position_price_for_stops() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            },
            {
                "trade_id": "t2",
                "symbol": "BBBUSDT",
                "side": "short",
                "status": "open",
                "qty": "2",
                "stop_price": 112.0,
                "planned_exit_ts_ms": 1_700_000_000_000,
            },
        ]
    )

    exits = plan_risk_exits(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "1", "markPrice": "113"},
            "BBBUSDT": {"symbol": "BBBUSDT", "side": "Sell", "size": "2", "markPrice": "99"},
        },
        price_by_symbol={"AAAUSDT": 113.0, "BBBUSDT": 99.0},
        now_ms=1_700_000_060_000,
    )

    assert [row["exit_reason"] for row in exits] == ["stop_loss", "max_hold"]
    assert exits[0]["qty"] == "1"
    assert exits[0]["planned_exit_price"] == 113.0


def test_plan_stop_repairs_detects_missing_exchange_stop() -> None:
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "stop_price": 112.0,
                "take_profit_price": 80.0,
            }
        ]
    )

    repairs = plan_stop_repairs(
        open_trades,
        position_by_symbol={
            "AAAUSDT": {
                "symbol": "AAAUSDT",
                "side": "Sell",
                "size": "1",
                "stopLoss": "",
                "takeProfit": "80.0001",
            }
        },
        tolerance_bps=1.0,
    )

    assert len(repairs) == 1
    assert repairs[0]["needs_stop_repair"] is True
    assert repairs[0]["needs_take_profit_repair"] is False


def test_plan_risk_exits_caps_qty_to_trade_on_shared_account() -> None:
    # This sleeve's ledger row holds qty=1 of AAAUSDT, but the NETTED venue
    # position is size=3 (this sleeve's 1 + a sibling sleeve's 2). On the shared
    # account a stop on this sleeve must size to 1, not 3 - else the reduce-only
    # flattens the sibling's 2 too.
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
                "planned_exit_ts_ms": 1_700_100_000_000,
            }
        ]
    )
    position_by_symbol = {
        "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "3", "markPrice": "113"},
    }
    price_by_symbol = {"AAAUSDT": 113.0}
    legacy = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol=price_by_symbol,
        now_ms=1_700_000_060_000,
    )
    assert legacy[0]["exit_reason"] == "stop_loss"
    assert legacy[0]["qty"] == "3"
    capped = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol=price_by_symbol,
        now_ms=1_700_000_060_000,
        cap_qty_to_trade=True,
    )
    assert capped[0]["exit_reason"] == "stop_loss"
    assert capped[0]["qty"] == "1"


# --- audit bucket b10 ws-risk-3 (relocated from test_audit_fix_b10.py) -------
# A qty<=0 ledger row must never reduce-only-flatten the netted size in cap mode.
def test_wsrisk3_zero_qty_row_does_not_emit_netted_exit() -> None:
    """A cap-mode (sibling-sleeve configured) open trade whose ledger qty parses to
    0 must NOT fall back to the raw netted position.size when a stop fires — that
    reduce-only would flatten the sibling's leg too (the non-self-healing
    cross-sleeve over-close). Before the fix the else branch preferred the netted
    size and emitted qty='10'; after, it skips entirely."""
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "0",  # schema drift / adopted-hedge / partially-cleared row
                "stop_price": 112.0,
            }
        ]
    )
    position_by_symbol = {
        "AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "10", "markPrice": "113"},
    }
    price_by_symbol = {"AAAUSDT": 113.0}
    capped = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol=price_by_symbol,
        now_ms=1_700_000_060_000,
        cap_qty_to_trade=True,
    )
    assert capped == [], "a qty<=0 ledger row must not flatten the full netted position in cap mode"


def test_wsrisk3_positive_qty_still_caps_to_trade() -> None:
    """Control: a normal qty>0 cap-mode trade still caps at min(trade_qty,
    position_size) (unchanged behaviour)."""
    open_trades = pl.DataFrame(
        [
            {
                "trade_id": "t1",
                "symbol": "AAAUSDT",
                "side": "short",
                "status": "open",
                "qty": "1",
                "stop_price": 112.0,
            }
        ]
    )
    position_by_symbol = {"AAAUSDT": {"symbol": "AAAUSDT", "side": "Sell", "size": "10", "markPrice": "113"}}
    capped = plan_risk_exits(
        open_trades,
        position_by_symbol=position_by_symbol,
        price_by_symbol={"AAAUSDT": 113.0},
        now_ms=1_700_000_060_000,
        cap_qty_to_trade=True,
    )
    assert len(capped) == 1
    assert capped[0]["qty"] == "1"

