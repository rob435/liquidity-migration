from __future__ import annotations

import json
import tomllib
from pathlib import Path

from scripts.research.compare_toxic_quoter import NS, simulate


SYMBOL = "TESTUSDT"
ROOT = Path(__file__).resolve().parents[2]


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


def test_registered_rule_and_disabled_runtime_block_are_one_mapping() -> None:
    registered = json.loads(
        (ROOT / "configs" / "lane2_toxic_flow_quoter_v1.json").read_text()
    )["rule"]
    flow = registered["flow"]
    assert set(registered) == {
        "symbol",
        "quote_notional_usdt",
        "max_position_usdt",
        "half_spread_bps",
        "requote_bps",
        "skew_bps",
        "stop_loss_fraction",
        "maker_fee_bps",
        "min_edge_bps",
        "volatility_multiplier",
        "book_lean_bps",
        "signal_half_life_ms",
        "queue_reprice_edge_bps",
        "flow",
    }
    assert set(flow) == {
        "fast_half_life_ms",
        "slow_half_life_ms",
        "fast_weight",
        "slow_weight",
        "response_bps",
        "max_widen_bps",
        "pull_score",
        "near_depth_bps",
        "volatility_depth_multiplier",
        "max_score",
    }

    runtime = tomllib.loads(
        (ROOT / "deploy" / "engine.mainnet.toml.template").read_text()
    )
    maker = next(
        block for block in runtime["strategy"] if block.get("sleeve") == "maker_canary"
    )
    expected = {
        "symbols": [registered["symbol"]],
        "qty_usdt": registered["quote_notional_usdt"],
        "max_position_usdt": registered["max_position_usdt"],
        "half_spread_bps": registered["half_spread_bps"],
        "requote_bps": registered["requote_bps"],
        "skew_bps": registered["skew_bps"],
        "stop_loss_fraction": registered["stop_loss_fraction"],
        "maker_fee_bps": registered["maker_fee_bps"],
        "min_edge_bps": registered["min_edge_bps"],
        "volatility_multiplier": registered["volatility_multiplier"],
        "book_lean_bps": registered["book_lean_bps"],
        "signal_half_life_ms": registered["signal_half_life_ms"],
        "queue_reprice_edge_bps": registered["queue_reprice_edge_bps"],
        "flow_fast_half_life_ms": flow["fast_half_life_ms"],
        "flow_slow_half_life_ms": flow["slow_half_life_ms"],
        "flow_fast_weight": flow["fast_weight"],
        "flow_slow_weight": flow["slow_weight"],
        "flow_response_bps": flow["response_bps"],
        "flow_max_widen_bps": flow["max_widen_bps"],
        "flow_pull_score": flow["pull_score"],
        "flow_depth_bps": flow["near_depth_bps"],
        "flow_volatility_depth_multiplier": flow[
            "volatility_depth_multiplier"
        ],
        "flow_max_score": flow["max_score"],
        "toxicity_bps": 0.0,
        "trade_lean_bps": 0.0,
    }
    actual = {
        key: maker.get(key, 0.0 if key in {"toxicity_bps", "trade_lean_bps"} else None)
        for key in expected
    }
    assert actual == expected
    assert maker["name"] == "quoter"
    assert maker["quote_enabled"] is False
