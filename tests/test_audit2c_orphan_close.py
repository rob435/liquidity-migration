"""audit2c [4]/[5]: orphan-close leg selection must not mis-attribute a sibling
sleeve's earlier same-side close on the shared netted demo account."""

from __future__ import annotations

import pytest

from liquidity_migration.event_demo_exits import _orphan_close_pnl_from_records


def _trade(**kw):
    base = {
        "symbol": "AAAUSDT", "side": "short", "entry_price": 10.0, "entry_ts_ms": 1000,
        "qty": "100", "exit_order_id": "", "notional_usdt": 1000.0, "equity_usdt": 10000.0,
    }
    base.update(kw)
    return base


def _leg(price, size, fee, oid, t):
    return {"symbol": "AAAUSDT", "side": "Buy", "avgExitPrice": str(price),
            "closedSize": str(size), "execFee": str(fee), "orderId": oid, "createdTime": str(t)}


def test_orphan_close_prefers_our_recent_close_over_earlier_sibling() -> None:
    # exit_order_id unknown; a SIBLING's same-side close is EARLIER than ours.
    # Old earliest-first picked the sibling (12.0); the fix prefers our most-recent close.
    records = [_leg(12.0, 100, 0.5, "sibling", 2000), _leg(9.0, 100, 0.3, "ours", 3000)]
    bf = _orphan_close_pnl_from_records(_trade(), now_ms=5000, records=records)
    assert bf["exit_price"] == pytest.approx(9.0)       # ours, NOT the sibling's 12.0
    assert bf["exit_order_id"] == "ours"
    assert bf["exit_fee_usdt"] == pytest.approx(0.3)    # only our leg's fee
    assert bf["gross_trade_return"] == pytest.approx(0.10)  # short (10-9)/10


def test_orphan_close_known_exit_order_id_wins_even_when_sibling_is_later() -> None:
    records = [_leg(9.0, 100, 0.3, "ours", 2000), _leg(12.0, 100, 0.5, "sibling", 3000)]
    bf = _orphan_close_pnl_from_records(_trade(exit_order_id="ours"), now_ms=5000, records=records)
    assert bf["exit_price"] == pytest.approx(9.0)
    assert bf["exit_order_id"] == "ours"


def test_orphan_close_own_multileg_still_aggregates_in_full() -> None:
    # Our own 2-leg close (sums to qty) must still aggregate fully (no regression).
    records = [_leg(96.0, 3, 0.3, "leg-1", 1_700_000_040_000),
               _leg(94.0, 1, 0.1, "leg-2", 1_700_000_050_000)]
    bf = _orphan_close_pnl_from_records(_trade(qty="4", entry_price=100.0),
                                        now_ms=1_700_000_100_000, records=records)
    assert bf["exit_price"] == pytest.approx(95.5)      # (3*96 + 1*94) / 4
    assert bf["exit_fee_usdt"] == pytest.approx(0.4)    # both legs summed
    assert bf["exit_order_id"] == "leg-2"               # latest leg completes the close
    assert bf["closed_at_ms"] == 1_700_000_050_000
