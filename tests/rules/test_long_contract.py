from __future__ import annotations

import math

import pytest

from liquidity_migration.core._common import date_ms, exact_duration_ms
from liquidity_migration.rules.long_contract import (
    ConfigLayer,
    DecisionAction,
    DecisionInput,
    PriorState,
    decide,
    resolve_strategy_config,
)
from liquidity_migration.rules.long_native import long_v12_profile


def _signal_row(*, ts_ms: int, symbol: str = "BTCUSDT") -> dict[str, object]:
    return {
        "ts_ms": ts_ms,
        "symbol": symbol,
        "close": 100.0,
        "in_universe": True,
        "regime_on": True,
        "eth_regime_on": True,
        "today_volume_rank": 1,
        "log_return": math.log1p(0.20),
        "pump_3d_log": 0.10,
        "pump_7d_log": 0.20,
        "sigma_daily_30d": 0.05,
        "close_location": 0.85,
        "close_loc_3d": 0.70,
        "close_loc_7d": 0.70,
        "atr_14d_pct": 0.05,
        "realized_vol": 0.60,
        "btc_rv_30": 0.60,
    }


def test_effective_config_records_the_winning_source_per_field() -> None:
    config = resolve_strategy_config(
        "v12",
        layers=(
            ConfigLayer(
                source="operational_profile",
                detail="sha256:abc",
                values={
                    "notional_multiplier": 6.0,
                    "entry_leverage": 10.0,
                    "max_new_entries_per_cycle": 5,
                },
            ),
            ConfigLayer(
                source="higher_priority_layer",
                detail="explicit test override",
                values={"notional_multiplier": 7.0},
            ),
            ConfigLayer(
                source="research_cost_model",
                values={"round_trip_cost_bps": 42.0},
            ),
        ),
    )

    assert config.rule == long_v12_profile()
    assert config.notional_multiplier == pytest.approx(7.0)
    assert config.round_trip_cost_bps == pytest.approx(42.0)
    provenance = config.provenance_by_field()
    assert provenance["notional_multiplier"] == {
        "source": "higher_priority_layer",
        "detail": "explicit test override",
    }
    assert provenance["entry_leverage"] == {
        "source": "operational_profile",
        "detail": "sha256:abc",
    }
    assert provenance["resize_floor_fraction"]["source"] == "engine_plan_rules_fleet"


def test_flat_signal_decides_exact_live_entry_without_take_profit() -> None:
    signal_ts_ms = date_ms("2027-01-04")
    decision_ts_ms = signal_ts_ms + exact_duration_ms(hours=1)
    config = resolve_strategy_config(
        "v12",
        layers=(ConfigLayer(source="test", values={"notional_multiplier": 7.0}),),
    )

    output = decide(
        DecisionInput(
            decision_ts_ms=decision_ts_ms,
            symbol="BTCUSDT",
            signal_ts_ms=signal_ts_ms,
            signal_close=100.0,
            market_price=98.9,
            equity_usdt=1_000.0,
            feature_row=_signal_row(ts_ms=signal_ts_ms),
        ),
        PriorState(),
        config,
    )

    assert output.action is DecisionAction.ENTER
    assert output.reason == "sniper_retrace"
    assert output.position_weight == pytest.approx(0.5)
    assert output.target_fraction_of_equity == pytest.approx(0.35)
    assert output.target_notional_usdt == pytest.approx(350.0)
    assert output.stop_loss_fraction == pytest.approx(0.15)
    assert output.stop_decay_after_ms == exact_duration_ms(hours=48)
    assert output.decayed_stop_loss_fraction == pytest.approx(0.075)
    assert output.max_hold_duration_ms == exact_duration_ms(days=3)
    assert output.entry_valid_until_ms == decision_ts_ms + exact_duration_ms(hours=1)
    assert "take_profit" not in output.as_json_dict()


def test_entry_wait_and_fill_anchored_exit_share_the_same_contract() -> None:
    signal_ts_ms = date_ms("2027-01-04")
    config = resolve_strategy_config("v12")
    waiting = decide(
        DecisionInput(
            decision_ts_ms=signal_ts_ms + exact_duration_ms(hours=2),
            symbol="BTCUSDT",
            signal_ts_ms=signal_ts_ms,
            signal_close=100.0,
            market_price=99.5,
            feature_row=_signal_row(ts_ms=signal_ts_ms),
        ),
        PriorState(),
        config,
    )
    assert waiting.action is DecisionAction.WAIT
    assert waiting.reason == "awaiting_retrace"
    assert waiting.wake_at_or_below == pytest.approx(99.0)

    entry_ts_ms = signal_ts_ms + exact_duration_ms(hours=3)
    prior = PriorState(
        requested=True,
        filled=True,
        entry_ts_ms=entry_ts_ms,
        entry_price=100.0,
        target_notional_usdt=500.0,
        stop_loss_fraction=0.15,
        stop_decay_after_ms=exact_duration_ms(hours=48),
        decayed_stop_loss_fraction=0.075,
        max_hold_deadline_ts_ms=entry_ts_ms + exact_duration_ms(days=3),
        entry_valid_until_ms=entry_ts_ms + exact_duration_ms(hours=1),
    )
    exit_output = decide(
        DecisionInput(
            decision_ts_ms=entry_ts_ms + exact_duration_ms(hours=49),
            symbol="BTCUSDT",
            signal_ts_ms=signal_ts_ms,
            market_price=92.5,
        ),
        prior,
        config,
    )
    assert exit_output.action is DecisionAction.EXIT
    assert exit_output.reason == "decayed_stop_loss"
    assert exit_output.stop_loss_fraction == pytest.approx(0.075)


def test_filled_position_base_stop_is_a_shared_contract_exit() -> None:
    entry_ts_ms = date_ms("2027-01-04")
    output = decide(
        DecisionInput(
            decision_ts_ms=entry_ts_ms + exact_duration_ms(hours=24),
            symbol="BTCUSDT",
            signal_ts_ms=entry_ts_ms - exact_duration_ms(hours=1),
            market_price=90.0,
            observed_low=84.9,
        ),
        PriorState(
            requested=True,
            filled=True,
            entry_ts_ms=entry_ts_ms,
            entry_price=100.0,
            target_notional_usdt=500.0,
            stop_loss_fraction=0.15,
            stop_decay_after_ms=exact_duration_ms(hours=48),
            decayed_stop_loss_fraction=0.075,
            max_hold_deadline_ts_ms=entry_ts_ms + exact_duration_ms(days=3),
        ),
        resolve_strategy_config("v12"),
    )

    assert output.action is DecisionAction.EXIT
    assert output.reason == "stop_loss"
    assert output.stop_loss_fraction == pytest.approx(0.15)


def test_future_signal_is_rejected_without_calling_the_entry_path() -> None:
    signal_ts_ms = date_ms("2027-01-04")
    output = decide(
        DecisionInput(
            decision_ts_ms=signal_ts_ms - 1,
            symbol="BTCUSDT",
            signal_ts_ms=signal_ts_ms,
            signal_close=100.0,
            market_price=99.0,
            feature_row=_signal_row(ts_ms=signal_ts_ms),
        ),
        PriorState(),
        resolve_strategy_config("v12"),
    )

    assert output.action is DecisionAction.REJECT
    assert output.reason == "signal_not_available"


def test_pending_request_expires_without_starting_the_hold_clock() -> None:
    now_ms = date_ms("2027-01-04")
    config = resolve_strategy_config("v12")
    output = decide(
        DecisionInput(decision_ts_ms=now_ms, symbol="BTCUSDT"),
        PriorState(
            requested=True,
            filled=False,
            entry_valid_until_ms=now_ms,
            max_hold_deadline_ts_ms=0,
        ),
        config,
    )
    assert output.action is DecisionAction.EXIT
    assert output.reason == "entry_expired"
