from __future__ import annotations

from scripts.research.compare_toxic_quoter import NS, simulate


SYMBOL = "TESTUSDT"


def book(seconds: int, bid: float, ask: float) -> dict[str, object]:
    return {
        "kind": "orderbook_snapshot",
        "symbol": SYMBOL,
        "local_receive_ts_ns": seconds * NS,
        "bids": [[bid, 1.0]],
        "asks": [[ask, 1.0]],
    }


def trade(seconds: int, price: float, qty: float, side: str) -> dict[str, object]:
    return {
        "kind": "public_trade",
        "symbol": SYMBOL,
        "local_receive_ts_ns": seconds * NS,
        "price": price,
        "qty": qty,
        "side": side,
    }


def test_flow_can_only_protect_against_trades_after_the_trigger() -> None:
    result = simulate(
        iter(
            [
                book(1, 99.9, 100.1),
                # This is the trigger. It does not reach the resting ask and
                # may only change what the quoter does afterwards.
                trade(2, 100.05, 10.0, "Buy"),
                # The unchanged quote is picked off here. The pull arm saw
                # the earlier flow and is no longer offering its ask.
                trade(3, 100.08, 10.0, "Buy"),
                book(18, 100.9, 101.1),
            ]
        ),
        SYMBOL,
        0.01,
        30 * NS,
        15 * NS,
    )

    control = (1, "Sell", "fee_corrected")
    protected = (1, "Sell", "directional_w2_pull1")
    assert control in result.filled
    assert result.values[control] < 0.0
    assert protected not in result.filled
    assert result.values[protected] == 0.0
