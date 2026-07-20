from __future__ import annotations

import pytest

from liquidity_migration.account_contracts import AccountEvent
from liquidity_migration.execution_cost_model import (
    build_fill_cost_rows,
    cost_model_summary,
)


def _event(event_type: str, *, sequence: int, correlation_id: str, symbol: str, payload: dict) -> AccountEvent:
    return AccountEvent(
        schema_version=2,
        event_id=f"id-{sequence}",
        sequence=sequence,
        event_type=event_type,
        correlation_id=correlation_id,
        causation_id="",
        account_id="test",
        sleeve="continuous",
        symbol=symbol,
        wall_ts_ns=1_000 + sequence,
        monotonic_ns=sequence,
        payload=payload,
        prev_event_hash="0" * 64,
        state_hash="0" * 64,
        event_hash="0" * 64,
    )


def _world() -> tuple[list[AccountEvent], dict, dict]:
    events = [
        _event(
            "market_input_ref",
            sequence=1,
            correlation_id="batch-1",
            symbol="XUSDT",
            payload={"input_key": "ctx-1"},
        ),
        _event(
            "order_command",
            sequence=2,
            correlation_id="batch-1",
            symbol="XUSDT",
            payload={"command_id": "c1"},
        ),
        _event(
            "fill",
            sequence=3,
            correlation_id="batch-1",
            symbol="XUSDT",
            payload={
                "command_id": "c1",
                "execution_id": "e1",
                "signed_qty": 2.0,
                "price": 101.0,
                "exchange_ts_ns": 5_000,
            },
        ),
    ]
    contexts = {"ctx-1": {"bids": [[99.0, 5.0]], "asks": [[101.0, 5.0]]}}
    observations = {
        ("e1", 1_000_000_000): {
            "markout_status": "observed_healthy",
            "markout_midpoint": 100.5,
        }
    }
    return events, contexts, observations


def test_buy_fill_decomposition_is_m0_anchored() -> None:
    events, contexts, observations = _world()
    rows = build_fill_cost_rows(events, contexts, observations)
    assert rows.height == 1
    row = rows.to_dicts()[0]
    assert row["side"] == "buy"
    assert row["decision_mid"] == pytest.approx(100.0)
    assert row["quoted_spread_bps"] == pytest.approx(200.0)
    # buy at 101 against M0=100: paid 200 bps effective
    assert row["effective_spread_bps"] == pytest.approx(200.0)
    # mid moved to 100.5 by 1s: 100 bps impact, 100 bps realized (maker share)
    assert row["price_impact_1s_bps"] == pytest.approx(100.0)
    assert row["realized_spread_1s_bps"] == pytest.approx(100.0)
    # identity: effective = impact + realized at every observed horizon
    assert row["effective_spread_bps"] == pytest.approx(
        row["price_impact_1s_bps"] + row["realized_spread_1s_bps"]
    )
    # unobserved horizons stay null instead of polluting averages
    assert row["price_impact_5m_bps"] is None
    assert row["realized_spread_5m_bps"] is None


def test_sell_fill_flips_the_sign_convention() -> None:
    events, contexts, observations = _world()
    events[2] = _event(
        "fill",
        sequence=3,
        correlation_id="batch-1",
        symbol="XUSDT",
        payload={
            "command_id": "c1",
            "execution_id": "e1",
            "signed_qty": -2.0,
            "price": 99.0,
            "exchange_ts_ns": 5_000,
        },
    )
    row = build_fill_cost_rows(events, contexts, observations).to_dicts()[0]
    assert row["side"] == "sell"
    # sell at 99 against M0=100 also costs 200 bps effective
    assert row["effective_spread_bps"] == pytest.approx(200.0)
    # mid rising to 100.5 after a sell is favorable: negative impact
    assert row["price_impact_1s_bps"] == pytest.approx(-100.0)
    assert row["realized_spread_1s_bps"] == pytest.approx(300.0)


def test_fill_without_decision_context_is_excluded_not_averaged() -> None:
    events, _contexts, observations = _world()
    rows = build_fill_cost_rows(events, {}, observations)
    assert rows.is_empty()
    summary = cost_model_summary(rows, total_fill_events=1)
    assert summary["fills_measured"] == 0
    assert summary["fills_total"] == 1
    assert summary["coverage"] == 0.0


def test_summary_weights_by_notional_and_counts_horizon_coverage() -> None:
    events, contexts, observations = _world()
    rows = build_fill_cost_rows(events, contexts, observations)
    summary = cost_model_summary(rows, total_fill_events=1)
    assert summary["fills_measured"] == 1
    assert summary["coverage"] == 1.0
    assert summary["effective_spread_bps_weighted"] == pytest.approx(200.0)
    assert summary["price_impact_1s_bps_weighted"] == pytest.approx(100.0)
    assert summary["observed_1s"] == 1
    assert summary["observed_5m"] == 0
    assert summary["price_impact_5m_bps_weighted"] is None
