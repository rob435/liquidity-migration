from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import polars as pl
import pytest

import liquidity_migration.research.backtest.long_live_physics as long_live_physics
from liquidity_migration.core._common import date_ms, exact_duration_ms
from liquidity_migration.research.backtest.long_live_physics import (
    EvidenceProvenance,
    LivePhysicsAssumptions,
    LivePhysicsCapitalReference,
    capital_reference_after_equity,
    candidate_execution_intervals,
    deadband_threshold_usdt,
    extract_signal_candidates,
    funding_frame_for_candidates,
    format_long_live_physics_report,
    load_candidate_minute_tape,
    load_funding_download_coverage,
    resolve_live_physics_configuration,
    simulate_long_live_physics,
)
from liquidity_migration.rules.long_config import ConfigLayer, resolve_strategy_config
from liquidity_migration.rules.long_native import long_v12_profile


def _signal_row(*, ts_ms: int, symbol: str = "AAAUSDT") -> dict[str, object]:
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


def _config(*, hold_days: int = 1, max_new: int = 5):
    return resolve_strategy_config(
        "v12",
        rule=replace(long_v12_profile(), fc_max_hold_days=hold_days),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={
                    "notional_multiplier": 6.0,
                    "entry_leverage": 5.0,
                    "max_new_entries_per_cycle": max_new,
                },
            ),
        ),
    )


def _physics() -> LivePhysicsAssumptions:
    return LivePhysicsAssumptions(
        initial_equity_usdt=1_000.0,
        evidence=EvidenceProvenance(
            shaped_data="unit fixture",
            graded_data="none; unit fixture",
        ),
    )


def _capital_reference(
    *,
    configured_seed_usdt: float = 100.0,
    account_gross_cap_multiple_reference: float = 5.0,
    account_margin_cap_multiple_reference: float = 1.0,
) -> LivePhysicsCapitalReference:
    return LivePhysicsCapitalReference(
        configured_seed_usdt=configured_seed_usdt,
        tracks_equity=True,
        equity_fraction=1.0,
        floor_usdt=100.0,
        expand_dead_band_fraction=0.05,
        account_gross_cap_multiple_reference=account_gross_cap_multiple_reference,
        account_margin_cap_multiple_reference=account_margin_cap_multiple_reference,
        source="unit fixture",
        source_sha256="a" * 64,
    )


def test_deadband_matches_the_rust_fleet_formula() -> None:
    config = _config()

    assert deadband_threshold_usdt(20.0, config=config, venue_min_notional_usdt=5.0) == pytest.approx(5.0)
    assert deadband_threshold_usdt(200.0, config=config, venue_min_notional_usdt=5.0) == pytest.approx(10.0)
    assert deadband_threshold_usdt(0.0, config=config, venue_min_notional_usdt=0.0) == pytest.approx(1.0)


def test_capital_reference_holds_sub_band_and_exact_boundary_expansion() -> None:
    capital = _capital_reference(configured_seed_usdt=1_000.0)

    assert capital_reference_after_equity(1_000.0, 1_049.0, config=capital) == 1_000.0
    assert capital_reference_after_equity(1_000.0, 1_050.0, config=capital) == 1_000.0


def test_capital_reference_accepts_expansion_strictly_above_the_band() -> None:
    capital = _capital_reference(configured_seed_usdt=1_000.0)

    assert capital_reference_after_equity(1_000.0, 1_050.000_001, config=capital) == pytest.approx(1_050.000_001)


def test_capital_reference_contracts_immediately_outside_close_tolerance() -> None:
    capital = _capital_reference(configured_seed_usdt=1_000.0)

    assert capital_reference_after_equity(1_000.0, 1_000.0 - 5e-10, config=capital) == 1_000.0
    assert capital_reference_after_equity(1_000.0, 999.0, config=capital) == 999.0


def test_risk_caps_use_the_held_reference_not_sub_band_raw_equity() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 98.0,
                "close": 98.0,
            }
        ]
    )
    capital = _capital_reference(
        configured_seed_usdt=1_000.0,
        account_gross_cap_multiple_reference=0.305,
        account_margin_cap_multiple_reference=10.0,
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=_config(),
        capital_reference=capital,
        assumptions=replace(_physics(), initial_equity_usdt=1_040.0),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    assert result.trades.is_empty()
    assert result.metadata["summary"]["risk_entry_blocks"] == 1
    assert result.metadata["capital_reference"]["final_reference_usdt"] == pytest.approx(1_000.0)


def test_research_rule_provenance_matches_the_registered_live_profile() -> None:
    resolved = resolve_live_physics_configuration()
    provenance = resolved.strategy.provenance_by_field()
    capital = resolved.capital_reference

    assert provenance["rule.execution_strategy_id"]["source"] == "registered_profile:v12"
    assert provenance["round_trip_cost_bps"]["source"] == "live_crossing_execution"
    assert resolved.strategy.round_trip_cost_bps == pytest.approx(15.56)
    assert capital.configured_seed_usdt == pytest.approx(100.0)
    assert capital.tracks_equity is True
    assert capital.equity_fraction == pytest.approx(1.0)
    assert capital.floor_usdt == pytest.approx(100.0)
    assert capital.expand_dead_band_fraction == pytest.approx(0.05)
    assert capital.account_gross_cap_multiple_reference == pytest.approx(5.0)
    assert capital.account_margin_cap_multiple_reference == pytest.approx(1.0)
    assert capital.source == resolved.operational_profile_source
    assert capital.source_sha256 == resolved.operational_profile_sha256


def test_retrace_touch_uses_shared_contract_fill_clock_costs_and_exact_funding() -> None:
    signal_ts = date_ms("2027-01-04")
    entry_bar_ts = signal_ts + exact_duration_ms(hours=1)
    entry_ts = entry_bar_ts + exact_duration_ms(minutes=1)
    exit_ts = entry_ts + exact_duration_ms(days=1)
    funding_ts = signal_ts + exact_duration_ms(hours=8)
    minute = pl.DataFrame(
        [
            {
                "ts_ms": entry_bar_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 150.0,
                "low": 98.8,
                "close": 99.0,
            },
            {
                "ts_ms": funding_ts,
                "symbol": "AAAUSDT",
                "open": 99.0,
                "high": 99.0,
                "low": 99.0,
                "close": 99.0,
            },
            {
                "ts_ms": exit_ts,
                "symbol": "AAAUSDT",
                "open": 110.0,
                "high": 111.0,
                "low": 109.0,
                "close": 110.0,
            },
        ]
    )
    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=pl.DataFrame(
            [
                {
                    "ts_ms": funding_ts,
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.001,
                }
            ]
        ),
        config=_config(),
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        funding_coverage={"AAAUSDT": ((signal_ts, exit_ts + 1),)},
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    assert result.trades.height == 1
    trade = result.trades.to_dicts()[0]
    assert trade["entry_ts_ms"] == entry_ts
    assert trade["entry_reason"] == "sniper_retrace"
    assert trade["entry_reference_price"] == pytest.approx(99.0)
    assert trade["exit_ts_ms"] == exit_ts
    assert trade["exit_reason"] == "time_stop"
    assert trade["hold_minutes"] == pytest.approx(1_440.0)
    assert trade["funding_event_count"] == 1
    assert trade["funding_mode"] == "modeled"
    assert trade["resize_count"] == 0
    assert result.mutations.height == 2
    assert result.metadata["execution_evidence"]["no_take_profit"] is True
    assert result.metadata["accounting_reconciliation"]["reconciles"] is True
    assert result.metadata["run_label"] == "minute_execution_bound_lane_1"
    assert result.metadata["equity_scale"]["label"] == "normalized_1000_usdt"
    assert result.metadata["equity_scale"]["venue_balance_verified"] is False
    assert result.metadata["capital_reference"]["configured_seed_usdt"] == pytest.approx(100.0)
    assert result.metadata["capital_reference"]["current_reference_usdt"] == pytest.approx(
        result.metadata["capital_reference"]["final_reference_usdt"]
    )
    assert result.metadata["capital_reference"]["provenance"] == {
        "source": "unit fixture",
        "source_sha256": "a" * 64,
    }
    assert trade["fee_usdt"] > 0.0
    assert trade["slippage_usdt"] > 0.0
    assert trade["funding_usdt"] < 0.0


def test_trade_price_retrace_without_mark_price_retrace_does_not_enter() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    trade_minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
            }
        ]
    )
    mark_minute = trade_minute.with_columns(
        pl.lit(100.0).alias("low"),
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=trade_minute,
        mark_price_bars=mark_minute,
        funding=None,
        config=_config(),
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    assert result.trades.is_empty()
    assert result.metadata["summary"]["entered_candidates"] == 0


def test_mark_price_retrace_enters_at_the_trade_price_stream() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    trade_minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 102.0,
                "low": 100.0,
                "close": 101.25,
            }
        ]
    )
    mark_minute = trade_minute.with_columns(
        pl.lit(98.0).alias("low"),
        pl.lit(100.0).alias("close"),
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=trade_minute,
        mark_price_bars=mark_minute,
        funding=None,
        config=_config(),
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    trade = result.trades.to_dicts()[0]
    assert trade["entry_reason"] == "sniper_retrace"
    assert trade["entry_reference_price"] == pytest.approx(101.25)
    entry_decision = result.decisions.filter(pl.col("action") == "enter").to_dicts()[0]
    assert entry_decision["entry_signal_stream"] == "mark_price"
    assert entry_decision["entry_fill_stream"] == "trade_price"


def test_funding_uses_the_mark_price_stream_not_the_trade_price_stream() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    funding_ts = signal_ts + exact_duration_ms(hours=8)
    config = resolve_strategy_config(
        "v12",
        rule=replace(long_v12_profile(), fc_sniper_deadline_hours=1),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    trade_minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                "ts_ms": funding_ts,
                "symbol": "AAAUSDT",
                "open": 80.0,
                "high": 80.0,
                "low": 80.0,
                "close": 80.0,
            },
        ]
    )
    mark_minute = trade_minute.with_columns(
        pl.when(pl.col("ts_ms") == funding_ts).then(pl.lit(120.0)).otherwise(pl.col("open")).alias("open"),
        pl.when(pl.col("ts_ms") == funding_ts).then(pl.lit(120.0)).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("ts_ms") == funding_ts).then(pl.lit(120.0)).otherwise(pl.col("low")).alias("low"),
        pl.when(pl.col("ts_ms") == funding_ts).then(pl.lit(120.0)).otherwise(pl.col("close")).alias("close"),
    )
    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=trade_minute,
        mark_price_bars=mark_minute,
        funding=pl.DataFrame([{"ts_ms": funding_ts, "symbol": "AAAUSDT", "funding_rate": 0.001}]),
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        funding_coverage={"AAAUSDT": ((signal_ts, funding_ts + 1),)},
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    event = result.funding_events.to_dicts()[0]
    assert event["price_stream"] == "mark_price_1m_open"
    assert event["price_proxy"] == pytest.approx(120.0)
    assert event["funding_usdt"] == pytest.approx(-event["quantity"] * 120.0 * 0.001)


def test_funding_is_not_charged_without_an_exact_mark_price_bar() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    funding_ts = signal_ts + exact_duration_ms(hours=8)
    config = resolve_strategy_config(
        "v12",
        rule=replace(long_v12_profile(), fc_sniper_deadline_hours=1),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    trade_minute = pl.DataFrame(
        [
            {
                "ts_ms": ts_ms,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            }
            for ts_ms in (first_check, funding_ts)
        ]
    )
    mark_minute = trade_minute.filter(pl.col("ts_ms") == first_check)

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=trade_minute,
        mark_price_bars=mark_minute,
        funding=pl.DataFrame([{"ts_ms": funding_ts, "symbol": "AAAUSDT", "funding_rate": 0.001}]),
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        funding_coverage={"AAAUSDT": ((signal_ts, funding_ts + 1),)},
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    assert result.funding_events.is_empty()
    assert result.metadata["rows"]["funding_settlements_without_exact_mark_price_bar"] == 1


def test_fixed_target_resizes_only_after_strict_deadband_crossing() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=1,
            fc_sniper_deadline_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={
                    "notional_multiplier": 6.0,
                    "entry_leverage": 5.0,
                },
            ),
        ),
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            # 5% is exactly the standing-notional floor and does not resize.
            {
                "ts_ms": first_check + exact_duration_ms(minutes=1),
                "symbol": "AAAUSDT",
                "open": 105.0,
                "high": 105.0,
                "low": 105.0,
                "close": 105.0,
            },
            # The next open clears the strict `>` threshold and trims to target.
            {
                "ts_ms": first_check + exact_duration_ms(minutes=2),
                "symbol": "AAAUSDT",
                "open": 106.0,
                "high": 106.0,
                "low": 106.0,
                "close": 106.0,
            },
        ]
    )
    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    resize_rows = result.mutations.filter(pl.col("reason").str.starts_with("deadband_resize"))
    assert resize_rows.height == 1
    assert resize_rows["reference_price"].to_list() == [106.0]
    assert result.trades["resize_count"].to_list() == [1]
    assert result.trades["exit_reason"].to_list() == ["data_end"]


def test_decayed_stop_does_not_reach_back_into_the_minute_before_it_arms() -> None:
    signal_ts = date_ms("2027-01-04")
    entry_ts = signal_ts + exact_duration_ms(hours=1)
    arm_ts = entry_ts + exact_duration_ms(hours=1)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=1,
            fc_sniper_deadline_hours=1,
            fc_stop_time_decay_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": entry_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                # The 7.5% stop arms only when this minute ends. Its low may
                # not be tested against a rule that did not exist yet.
                "ts_ms": arm_ts - exact_duration_ms(minutes=1),
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 92.0,
                "close": 100.0,
            },
            {
                "ts_ms": arm_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 92.0,
                "close": 100.0,
            },
        ]
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    trade = result.trades.to_dicts()[0]
    assert trade["exit_reason"] == "decayed_stop_loss"
    assert trade["exit_ts_ms"] == arm_ts + exact_duration_ms(minutes=1)
    decision = result.decisions.filter(pl.col("reason") == "decayed_stop_loss").to_dicts()[0]
    assert decision["decision_ts_ms"] == arm_ts
    assert decision["observation_end_ts_ms"] == arm_ts + exact_duration_ms(minutes=1)


def test_entry_throttle_has_one_budget_across_open_and_touch_in_a_minute() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    features = pl.DataFrame([_signal_row(ts_ms=signal_ts, symbol=f"A{index}USDT") for index in range(6)])
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": f"A{index}USDT",
                "open": 98.0 if index < 5 else 100.0,
                "high": 99.0 if index < 5 else 100.0,
                "low": 98.0,
                "close": 98.0 if index < 5 else 100.0,
            }
            for index in range(6)
        ]
    )
    result = simulate_long_live_physics(
        features=features,
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=_config(max_new=5),
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    entry_rows = result.mutations.filter(pl.col("reason").str.starts_with("entry:"))
    assert entry_rows.filter(pl.col("ts_ms") == first_check).height == 5
    assert entry_rows.filter(pl.col("ts_ms") == first_check + exact_duration_ms(minutes=1)).is_empty()
    assert result.metadata["summary"]["entered_candidates"] == 5
    assert "one shared" in result.metadata["execution_evidence"]["entry_throttle"]


def test_one_entry_wave_uses_one_equity_snapshot_for_every_candidate() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    symbols = ("AAAUSDT", "BBBUSDT")
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": symbol,
                "open": 98.0,
                "high": 99.0,
                "low": 98.0,
                "close": 98.0,
            }
            for symbol in symbols
        ]
    )
    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts, symbol=symbol) for symbol in symbols]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=_config(),
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    entries = result.mutations.filter(pl.col("reason").str.starts_with("entry:"))
    assert entries.height == 2
    notionals = entries["target_notional_usdt"].to_list()
    assert notionals[0] == pytest.approx(notionals[1])


def test_human_report_marks_tainted_performance_as_diagnostic() -> None:
    report = format_long_live_physics_report(
        {
            "run_label": "minute_execution_diagnostic_tainted",
            "tainted": True,
            "taint_reasons": [
                "candidate_minute_rows_missing",
                "candidate_mark_price_minute_rows_missing",
            ],
            "pit_manifest": {"full_pit_universe_pass": False},
            "minute_tape": {"complete": False, "missing_minutes": 1},
            "mark_price_minute_tape": {
                "complete": False,
                "missing_minutes": 1,
            },
            "identities": {
                "source_snapshot_file": long_live_physics.SOURCE_SNAPSHOT_NAME,
                "source_snapshot_sha256": "ab" * 32,
                "source_snapshot_files": 7,
            },
            "summary": {"total_return": 12.34},
        }
    )

    assert "TAINTED DIAGNOSTIC" in report
    assert "do not use the performance numbers" in report
    assert "candidate_minute_rows_missing" in report
    assert "candidate_mark_price_minute_rows_missing" in report
    assert "Causal-input point-in-time universe coverage: `False`" in report
    assert "MarkPrice trigger" in report
    assert "Mark-price minute tape complete: False" in report
    assert "Exact source snapshot:" in report
    assert "7 files" in report
    assert "ab" * 32 in report


def test_data_end_close_uses_last_loaded_minute_not_a_later_funding_event() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=1,
            fc_sniper_deadline_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={
                    "notional_multiplier": 6.0,
                    "entry_leverage": 5.0,
                },
            ),
        ),
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 102.0,
                "low": 99.5,
                "close": 101.0,
            }
        ]
    )
    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=pl.DataFrame(
            [
                {
                    "ts_ms": first_check + exact_duration_ms(hours=8),
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.001,
                }
            ]
        ),
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        funding_coverage={"AAAUSDT": ((signal_ts, signal_ts + exact_duration_ms(days=2)),)},
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    trade = result.trades.to_dicts()[0]
    assert trade["exit_reason"] == "data_end"
    assert trade["exit_ts_ms"] == first_check + exact_duration_ms(minutes=1)
    assert trade["exit_reference_price"] == pytest.approx(101.0)
    assert result.funding_events.is_empty()
    assert result.metadata["rows"]["funding_settlements_without_exact_mark_price_bar"] == 1


def test_base_stop_gap_uses_contract_exit_and_minute_open_fill() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=3,
            fc_sniper_deadline_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                "ts_ms": first_check + exact_duration_ms(minutes=1),
                "symbol": "AAAUSDT",
                "open": 80.0,
                "high": 81.0,
                "low": 79.0,
                "close": 80.0,
            },
        ]
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    trade = result.trades.to_dicts()[0]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_ts_ms"] == first_check + exact_duration_ms(minutes=1)
    assert trade["exit_reference_price"] == pytest.approx(80.0)
    stop_decision = result.decisions.filter(pl.col("reason") == "stop_loss").to_dicts()[0]
    assert stop_decision["phase"] == "minute_open"
    assert stop_decision["stop_loss_fraction"] == pytest.approx(0.15)


def test_decayed_stop_touch_uses_contract_exit_and_stop_price_fill() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    stop_bar_ts = first_check + exact_duration_ms(hours=48)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=3,
            fc_sniper_deadline_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                "ts_ms": stop_bar_ts,
                "symbol": "AAAUSDT",
                "open": 99.0,
                "high": 99.0,
                "low": 90.0,
                "close": 95.0,
            },
        ]
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=3),
    )

    trade = result.trades.to_dicts()[0]
    expected_stop = 100.0 * (1.0 + _physics().slippage_bps / 10_000.0) * (1.0 - 0.075)
    assert trade["exit_reason"] == "decayed_stop_loss"
    assert trade["exit_ts_ms"] == stop_bar_ts + exact_duration_ms(minutes=1)
    assert trade["exit_reference_price"] == pytest.approx(expected_stop)
    stop_decision = result.decisions.filter(pl.col("reason") == "decayed_stop_loss").to_dicts()[0]
    assert stop_decision["phase"] == "minute_low"
    assert stop_decision["stop_loss_fraction"] == pytest.approx(0.075)


@pytest.mark.parametrize(
    ("trade_low", "mark_low", "expected_exit", "expected_reference"),
    [
        (80.0, 100.0, "data_end", 100.0),
        (95.0, 80.0, "stop_loss", 95.0),
    ],
)
def test_stop_trigger_uses_mark_low_and_trade_bar_for_the_fill(
    trade_low: float,
    mark_low: float,
    expected_exit: str,
    expected_reference: float,
) -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    stop_bar_ts = first_check + exact_duration_ms(minutes=1)
    config = resolve_strategy_config(
        "v12",
        rule=replace(
            long_v12_profile(),
            fc_max_hold_days=3,
            fc_sniper_deadline_hours=1,
        ),
        layers=(
            ConfigLayer(
                source="test_operational_profile",
                values={"notional_multiplier": 6.0, "entry_leverage": 5.0},
            ),
        ),
    )
    trade_minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                "ts_ms": stop_bar_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 101.0,
                "low": trade_low,
                "close": 100.0,
            },
        ]
    )
    mark_minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
            },
            {
                "ts_ms": stop_bar_ts,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": mark_low,
                "close": 100.0,
            },
        ]
    )

    result = simulate_long_live_physics(
        features=pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        minute_bars=trade_minute,
        mark_price_bars=mark_minute,
        funding=None,
        config=config,
        capital_reference=_capital_reference(),
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    trade = result.trades.to_dicts()[0]
    assert trade["exit_reason"] == expected_exit
    assert trade["exit_reference_price"] == pytest.approx(expected_reference)
    stop_rows = result.decisions.filter(pl.col("reason") == "stop_loss")
    assert stop_rows.height == (1 if expected_exit == "stop_loss" else 0)
    if expected_exit == "stop_loss":
        assert stop_rows["stop_trigger_stream"].to_list() == ["mark_price"]


def test_long_only_account_capacity_blocks_a_risk_increasing_entry() -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    features = pl.DataFrame(
        [
            _signal_row(ts_ms=signal_ts, symbol="AAAUSDT"),
            _signal_row(ts_ms=signal_ts, symbol="BBBUSDT"),
        ]
    )
    minute = pl.DataFrame(
        [
            {
                "ts_ms": first_check + offset_ms,
                "symbol": symbol,
                "open": 98.0,
                "high": 99.0,
                "low": 98.0,
                "close": 98.0,
            }
            for offset_ms in (0, exact_duration_ms(minutes=1))
            for symbol in ("AAAUSDT", "BBBUSDT")
        ]
    )
    capital_reference = _capital_reference(
        account_gross_cap_multiple_reference=0.5,
        account_margin_cap_multiple_reference=10.0,
    )
    result = simulate_long_live_physics(
        features=features,
        minute_bars=minute,
        mark_price_bars=minute,
        funding=None,
        config=_config(),
        capital_reference=capital_reference,
        assumptions=_physics(),
        signal_start_ms=signal_ts,
        signal_end_ms=signal_ts + exact_duration_ms(days=1),
    )

    assert result.metadata["summary"]["entered_candidates"] == 1
    assert result.metadata["summary"]["risk_entry_blocks"] == 1
    assert result.metadata["entry_status_counts"]["risk_refused"] == 1


def test_candidate_tape_loader_hashes_only_reachable_symbol_days(tmp_path) -> None:
    signal_ts = date_ms("2027-01-04")
    first_check = signal_ts + exact_duration_ms(hours=1)
    candidates = extract_signal_candidates(pl.DataFrame([_signal_row(ts_ms=signal_ts)]), config=_config())
    partition = tmp_path / "klines_1m" / "date=2027-01-04" / "symbol=AAAUSDT"
    partition.mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            }
        ]
    ).write_parquet(partition / "part.parquet")
    mark_partition = tmp_path / "mark_price_1m" / "date=2027-01-04" / "symbol=AAAUSDT"
    mark_partition.mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "AAAUSDT",
                "open": 100.5,
                "high": 101.5,
                "low": 99.5,
                "close": 100.5,
            }
        ]
    ).write_parquet(mark_partition / "part.parquet")
    # An unrelated partition must not enter the content identity.
    unrelated = tmp_path / "klines_1m" / "date=2027-01-04" / "symbol=ZZZUSDT"
    unrelated.mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "ts_ms": first_check,
                "symbol": "ZZZUSDT",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
            }
        ]
    ).write_parquet(unrelated / "part.parquet")

    tape, receipt = load_candidate_minute_tape(
        tmp_path,
        candidates,
        dataset="klines_1m",
    )
    mark_tape, mark_receipt = load_candidate_minute_tape(
        tmp_path,
        candidates,
        dataset="mark_price_1m",
    )

    assert tape["symbol"].to_list() == ["AAAUSDT"]
    assert mark_tape["close"].to_list() == [100.5]
    assert receipt.dataset == "klines_1m"
    assert mark_receipt.dataset == "mark_price_1m"
    assert receipt.selected_files == 1
    assert mark_receipt.selected_files == 1
    assert receipt.missing_symbol_days > 0
    assert mark_receipt.missing_symbol_days > 0
    assert receipt.missing_minutes > 0
    assert mark_receipt.missing_minutes > 0
    assert len(receipt.selected_file_sha256) == 64
    assert len(mark_receipt.selected_file_sha256) == 64
    assert receipt.selected_file_sha256 != mark_receipt.selected_file_sha256


def test_mark_loader_repairs_the_exact_bybit_sub_bp_high_source_defect(tmp_path) -> None:
    raw_ts_ms = 1_634_539_980_000
    signal_ts_ms = raw_ts_ms - exact_duration_ms(hours=1)
    candidates = extract_signal_candidates(
        pl.DataFrame([_signal_row(ts_ms=signal_ts_ms, symbol="MATICUSDT")]),
        config=_config(),
    )
    partition = tmp_path / "mark_price_1m" / "date=2021-10-18" / "symbol=MATICUSDT"
    partition.mkdir(parents=True)
    raw = {
        "ts_ms": raw_ts_ms,
        "symbol": "MATICUSDT",
        "open": 1.596,
        "high": 1.5959,
        "low": 1.5898,
        "close": 1.5898,
    }
    pl.DataFrame([raw]).write_parquet(partition / "part.parquet")

    tape, receipt = load_candidate_minute_tape(
        tmp_path,
        candidates,
        dataset="mark_price_1m",
    )

    repaired = tape.to_dicts()[0]
    assert repaired == {**raw, "high": 1.596}
    assert receipt.source_high_repair_count == 1
    assert receipt.source_high_repair_max_gap_bps == pytest.approx((1.596 - 1.5959) / 1.596 * 10_000.0)
    source = receipt.source_high_repair_raw_sample[0]
    assert source.ts_ms == raw_ts_ms
    assert source.symbol == "MATICUSDT"
    assert source.raw_open == pytest.approx(1.596)
    assert source.raw_high == pytest.approx(1.5959)
    assert source.raw_low == pytest.approx(1.5898)
    assert source.raw_close == pytest.approx(1.5898)
    assert source.repaired_high == pytest.approx(1.596)

    report = format_long_live_physics_report(
        {
            "mark_price_minute_tape": {
                **asdict(receipt),
                "complete": receipt.complete,
            }
        }
    )
    assert "Mark-price source high repairs: count=1" in report
    assert '"raw_high":1.5959' in report
    assert '"symbol":"MATICUSDT"' in report


def test_high_source_repair_is_mark_only_bounded_and_never_changes_low() -> None:
    raw = {
        "ts_ms": 1_634_539_980_000,
        "symbol": "MATICUSDT",
        "open": 1.596,
        "high": 1.5959,
        "low": 1.5898,
        "close": 1.5898,
    }

    with pytest.raises(ValueError, match="invalid high ordering"):
        long_live_physics._canonical_minute_bars(
            pl.DataFrame([raw]),
            dataset="klines_1m",
        )
    with pytest.raises(ValueError, match="exceeds the 1 bp"):
        long_live_physics._canonical_minute_bars(
            pl.DataFrame([{**raw, "high": 1.5950}]),
            dataset="mark_price_1m",
        )
    with pytest.raises(ValueError, match="invalid low ordering"):
        long_live_physics._canonical_minute_bars(
            pl.DataFrame([{**raw, "high": 1.596, "low": 1.5900}]),
            dataset="mark_price_1m",
        )


def test_missing_mark_price_minutes_taint_the_research_result() -> None:
    complete_trade = long_live_physics.MinuteTapeReceipt(
        dataset="klines_1m",
        requested_intervals=1,
        requested_symbol_days=1,
        selected_files=1,
        selected_file_sha256="a" * 64,
        rows=1,
        missing_symbol_days=0,
        missing_minutes=0,
    )
    incomplete_mark = replace(
        complete_trade,
        dataset="mark_price_1m",
        selected_file_sha256="b" * 64,
        missing_minutes=1,
        missing_minute_sample=("2027-01-04T01:00:00+00:00/AAAUSDT",),
    )
    funding_receipt = long_live_physics.FundingTapeReceipt(
        required_intervals=1,
        covered_intervals=1,
        selected_markers=1,
        selected_marker_sha256="c" * 64,
        missing_intervals=0,
    )

    reasons = long_live_physics._taint_reasons(
        full_pit_universe_pass=True,
        minute_receipt=complete_trade,
        mark_minute_receipt=incomplete_mark,
        funding_receipt=funding_receipt,
        trades=pl.DataFrame(),
        funding_settlements_without_exact_mark_price_bar=0,
    )

    assert reasons == ["candidate_mark_price_minute_rows_missing"]


def test_missing_mark_price_bar_at_funding_settlement_taints_the_result() -> None:
    complete_trade = long_live_physics.MinuteTapeReceipt(
        dataset="klines_1m",
        requested_intervals=1,
        requested_symbol_days=1,
        selected_files=1,
        selected_file_sha256="a" * 64,
        rows=1,
        missing_symbol_days=0,
        missing_minutes=0,
    )
    complete_mark = replace(
        complete_trade,
        dataset="mark_price_1m",
        selected_file_sha256="b" * 64,
    )
    complete_funding = long_live_physics.FundingTapeReceipt(
        required_intervals=1,
        covered_intervals=1,
        selected_markers=1,
        selected_marker_sha256="c" * 64,
        missing_intervals=0,
    )

    reasons = long_live_physics._taint_reasons(
        full_pit_universe_pass=True,
        minute_receipt=complete_trade,
        mark_minute_receipt=complete_mark,
        funding_receipt=complete_funding,
        trades=pl.DataFrame(),
        funding_settlements_without_exact_mark_price_bar=1,
    )

    assert reasons == ["funding_settlement_mark_price_bar_missing"]


def test_funding_selection_keeps_candidate_interval_end_exclusive() -> None:
    signal_ts = date_ms("2027-01-04")
    candidates = extract_signal_candidates(pl.DataFrame([_signal_row(ts_ms=signal_ts)]), config=_config())
    start, end = candidate_execution_intervals(candidates)["AAAUSDT"][0]

    funding, _, _ = funding_frame_for_candidates(
        {
            "AAAUSDT": {
                "events_ts": [start, start + exact_duration_ms(hours=8), end],
                "events_rate": [0.001, 0.002, 0.003],
                "start_ts_ms": start,
                "end_ts_ms": end,
            }
        },
        candidates,
    )

    assert funding["ts_ms"].to_list() == [
        start,
        start + exact_duration_ms(hours=8),
    ]


def test_funding_download_markers_prove_candidate_execution_windows(tmp_path) -> None:
    signal_ts = date_ms("2027-01-04")
    candidates = extract_signal_candidates(
        pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        config=_config(),
    )
    start, end = candidate_execution_intervals(candidates)["AAAUSDT"][0]
    marker = tmp_path / "_download_markers" / "funding" / f"AAAUSDT_{start - 1}_{end + 1}.done"
    marker.parent.mkdir(parents=True)
    marker.write_text("completed funding fetch\n", encoding="utf-8")

    coverage, receipt = load_funding_download_coverage(tmp_path, candidates)

    assert coverage == {"AAAUSDT": ((start, end),)}
    assert receipt.complete is True
    assert receipt.required_intervals == 1
    assert receipt.covered_intervals == 1
    assert receipt.selected_markers == 1
    assert len(receipt.selected_marker_sha256) == 64


def test_funding_download_coverage_reports_a_missing_candidate_window(tmp_path) -> None:
    signal_ts = date_ms("2027-01-04")
    candidates = extract_signal_candidates(
        pl.DataFrame([_signal_row(ts_ms=signal_ts)]),
        config=_config(),
    )

    coverage, receipt = load_funding_download_coverage(tmp_path, candidates)

    assert coverage == {}
    assert receipt.complete is False
    assert receipt.required_intervals == 1
    assert receipt.covered_intervals == 0
    assert receipt.missing_intervals == 1
    assert receipt.missing_interval_sample[0].startswith("AAAUSDT:")


def test_report_writer_truncates_stale_csvs_when_the_new_run_is_empty(tmp_path) -> None:
    filenames = (
        "long_live_physics_trades.csv",
        "long_live_physics_mutations.csv",
        "long_live_physics_funding.csv",
        "long_live_physics_daily_equity.csv",
        "long_live_physics_decisions.csv",
    )
    for filename in filenames:
        (tmp_path / filename).write_text("stale,data\n1,2\n", encoding="utf-8")
    empty = pl.DataFrame()
    result = long_live_physics.LivePhysicsResult(
        trades=empty,
        mutations=empty,
        funding_events=empty,
        daily_equity=empty,
        decisions=empty,
        metadata={},
    )

    snapshot = b'{"snapshot":true}\n'
    long_live_physics._write_result(
        tmp_path,
        result,
        {},
        source_snapshot_bytes=snapshot,
    )

    for filename in filenames:
        assert "stale" not in (tmp_path / filename).read_text(encoding="utf-8")
    assert (tmp_path / long_live_physics.SOURCE_SNAPSHOT_NAME).read_bytes() == snapshot


def test_source_snapshot_freezes_the_research_and_live_local_closure() -> None:
    repo = Path(__file__).resolve().parents[3]

    first, identity = long_live_physics._source_snapshot(repo)
    second, second_identity = long_live_physics._source_snapshot(repo)

    assert first == second
    assert identity == second_identity
    assert identity["source_snapshot_sha256"] == hashlib.sha256(first).hexdigest()
    payload = json.loads(first)
    files = {row["path"]: row for row in payload["files"]}
    for root in long_live_physics._SOURCE_SNAPSHOT_ROOTS:
        assert root in files
    assert "liquidity_migration/core/operational_profile.py" in files
    assert "liquidity_migration/policy/operational_profile.py" not in files
    for row in files.values():
        assert row["sha256"] == hashlib.sha256(row["content_utf8"].encode("utf-8")).hexdigest()


def test_git_identity_ignores_foreign_repository_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(long_live_physics.__file__).resolve().parents[3]
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected_dirty = bool(
        subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    foreign = tmp_path / "foreign"
    subprocess.run(["git", "init", "-q", str(foreign)], check=True)

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(foreign / ".git"))

    identity = long_live_physics._git_identity()

    assert identity["git_commit"] == expected
    assert identity["git_dirty"] is expected_dirty
