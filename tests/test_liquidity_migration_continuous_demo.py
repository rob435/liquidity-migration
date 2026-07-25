"""Target-only decision contract for the CONTINUOUS demo and paper producer.

These tests cover signal construction, admission, deterministic target planning,
and the strict account-owner publication boundary. Venue orders, fills, P&L,
protection, and notifications belong to the account service and are intentionally
absent from this suite.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.account_intent_client import (
    AccountTargetPublisher,
    ExitFirstPublication,
    publish_exit_first_target_requests,
)
from liquidity_migration.account_route import (
    AccountRoute,
    AccountRouteMismatchError,
    ensure_account_route,
)
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.account_strategy_state import CanonicalReductionEvent
from liquidity_migration.config import ResearchConfig
from liquidity_migration.continuous_demo import (
    CONTINUOUS_DEMO_PROFILES,
    ContinuousDemoCycleConfig,
    LivePanelCache,
    ResidualMomentumSnapshot,
    _btc_risk_sizing_payload_fields,
    _btc_trend_gate_allows_value,
    _btc_trend_gate_payload_fields,
    _continuous_age_eligible_symbols,
    _continuous_base_notional_pct_equity,
    _continuous_btc_risk_multiplier,
    _continuous_entry_candidates_with_signal_metadata,
    _continuous_entry_target_intents,
    _continuous_exit_target_intents,
    _continuous_target_reservations,
    _entry_feature_identity_payload_fields,
    _observe_continuous_component_selection,
    _qualified_block_reasons,
    _blocked_rows_from_reasons,
    _first_entry_rejection_reason,
    _load_rmom_table,
    _load_rmom_snapshot,
    _rmom_identity_payload_fields,
    _open_continuous_trades,
    _payload_float,
    _rmom_freshness_payload_fields,
    _validate_continuous_demo_config,
    apply_continuous_demo_profile,
    build_confirmed_entry_state,
    build_live_continuous_state,
    continuous_cycles_dataset,
    continuous_managed_strategy_ids,
    continuous_strategy_id,
    entry_circuit_breaker_tripped,
    format_continuous_demo_cycle_summary,
    plan_continuous_exits,
    run_continuous_demo_cycle,
    select_continuous_entries,
)
from liquidity_migration.continuous_cycle_status import read_continuous_cycle_status
from liquidity_migration.continuous_events import compute_continuous_decile_panel
from liquidity_migration.strategy_target_replay import PublishedTargetCyclePayload


def _synth(
    n_symbols: int = 26,
    n_bars: int = 320,
    start: int = 1_700_000_000_000,
) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    start -= start % MS_PER_DAY
    rows: list[dict[str, object]] = []
    for symbol_index in range(n_symbols):
        price = 100.0 + symbol_index
        for bar_index in range(n_bars):
            wobble = 1.0 + 0.02 * ((symbol_index * 7 + bar_index * 13) % 11 - 5) / 5.0
            price = max(1.0, price * wobble)
            rows.append(
                {
                    "ts_ms": start + bar_index * MS_PER_HOUR,
                    "symbol": f"S{symbol_index:02d}",
                    "close": price,
                    "turnover_quote": 1_000_000.0,
                }
            )
    klines = pl.DataFrame(rows)
    days = sorted({((start + bar_index * MS_PER_HOUR) // MS_PER_DAY) * MS_PER_DAY for bar_index in range(n_bars)})
    rmom = pl.DataFrame(
        [
            {
                "symbol": f"S{symbol_index:02d}",
                "day_ts": day,
                "residual_momentum": (symbol_index % 13) * 0.001 - 0.006,
            }
            for day in days
            for symbol_index in range(n_symbols)
        ]
    )
    return klines, rmom, start, n_bars


def _dispersed_synth(
    n_symbols: int = 60,
    n_bars: int = 460,
    start: int = 1_700_000_000_000,
) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    """Well-separated feature values plus young and gapped symbols."""
    start -= start % MS_PER_DAY
    rows: list[dict[str, object]] = []
    for symbol_index in range(n_symbols):
        rng = np.random.default_rng(1_000 + symbol_index)
        price = 10.0 + 3.0 * symbol_index
        volatility = 0.015 + 0.05 * (symbol_index / n_symbols)
        drift = -0.002 + 0.00012 * symbol_index
        first_bar = n_bars - 160 if symbol_index >= n_symbols - 3 else 0
        for bar_index in range(first_bar, n_bars):
            if symbol_index == n_symbols - 4 and bar_index % 41 == 0 and bar_index > first_bar + 5:
                continue
            price = max(
                0.5,
                price * (1.0 + drift + rng.normal(0.0, volatility)),
            )
            rows.append(
                {
                    "ts_ms": start + bar_index * MS_PER_HOUR,
                    "symbol": f"S{symbol_index:02d}",
                    "close": price,
                    "turnover_quote": 500_000.0 + 10_000.0 * symbol_index,
                }
            )
    klines = pl.DataFrame(rows)
    days = sorted({((start + bar_index * MS_PER_HOUR) // MS_PER_DAY) * MS_PER_DAY for bar_index in range(n_bars)})
    rmom_rng = np.random.default_rng(42)
    rmom = pl.DataFrame(
        [
            {
                "symbol": f"S{symbol_index:02d}",
                "day_ts": day,
                "residual_momentum": float(rmom_rng.normal(0.0, 0.01)),
            }
            for day in days
            for symbol_index in range(n_symbols)
        ]
    )
    return klines, rmom, start, n_bars


def _route(tmp_path: Path, *, environment: str = "demo") -> AccountRoute:
    return ensure_account_route(
        account_id=("bybit-demo-unified" if environment == "demo" else "bybit-paper-unified"),
        environment=environment,
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )


def _routed_config(
    tmp_path: Path,
    *,
    environment: str = "demo",
    **overrides: Any,
) -> ContinuousDemoCycleConfig:
    values: dict[str, Any] = {
        "execution_environment": environment,
        "account_execution_root": str(tmp_path / "account"),
        "account_intent_inbox_root": str(tmp_path / "inbox"),
    }
    values.update(overrides)
    return ContinuousDemoCycleConfig(**values)


def _decile_map(frame: pl.DataFrame) -> dict[str, int]:
    return {str(row["symbol"]): int(row["decile"]) for row in frame.to_dicts()}


def _composite_map(frame: pl.DataFrame) -> dict[str, float]:
    return {str(row["symbol"]): float(row["composite"]) for row in frame.to_dicts()}


def test_live_state_reproduces_backtest_decile() -> None:
    klines, rmom, start, n_bars = _synth()
    config = ContinuousDemoCycleConfig()
    current_hour = start + (n_bars - 1) * MS_PER_HOUR
    backtest = compute_continuous_decile_panel(
        klines,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
    )
    expected = _decile_map(backtest.filter(pl.col("ts_ms") == current_hour))
    history = klines.filter(pl.col("ts_ms") < current_hour)
    prices = {
        str(row["symbol"]): float(row["close"]) for row in klines.filter(pl.col("ts_ms") == current_hour).to_dicts()
    }

    actual = build_live_continuous_state(
        history,
        prices,
        rmom,
        now_ts_ms=current_hour + 1_800_000,
        config=config,
    )

    assert expected
    assert _decile_map(actual) == expected


def test_confirmed_entry_state_uses_the_delayed_closed_bar() -> None:
    klines, rmom, start, n_bars = _synth()
    config = ContinuousDemoCycleConfig(entry_confirm_delay_hours=1)
    current_hour = start + (n_bars - 1) * MS_PER_HOUR
    now_ms = current_hour + 1_800_000
    deciding_hour = current_hour - 2 * MS_PER_HOUR

    actual = build_confirmed_entry_state(
        klines,
        rmom,
        now_ts_ms=now_ms,
        config=config,
    )
    panel = compute_continuous_decile_panel(
        klines.filter(pl.col("ts_ms") < current_hour),
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
    )
    expected = panel.filter(pl.col("ts_ms") == deciding_hour)

    assert _decile_map(actual) == _decile_map(expected)
    assert {"ret1", "max_ret168", "prior6_ret1_max"} <= set(actual.columns)


def test_selector_respects_signal_liquidity_reservations_and_capacity() -> None:
    state = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D", "E"],
            "decile": [9, 9, 9, 8, 9],
            "composite": [0.95, 0.90, 0.99, 0.50, 0.85],
            "turnover_quote": [1e6, 1e3, 1e6, 1e6, 1e6],
        }
    )
    config = ContinuousDemoCycleConfig(
        decile=9,
        liq_turnover_min=500_000.0,
        max_active=3,
        max_new_entries_per_cycle=2,
    )

    selected = select_continuous_entries(
        state,
        held_symbols={"C"},
        cooldown_symbols=set(),
        open_count=1,
        config=config,
    )

    assert [row["symbol"] for row in selected] == ["A", "E"]
    assert (
        select_continuous_entries(
            state,
            held_symbols=set(),
            cooldown_symbols=set(),
            open_count=3,
            config=config,
        )
        == []
    )


def test_selector_applies_confirmed_event_and_age_gates() -> None:
    state = pl.DataFrame(
        {
            "symbol": ["FRESH", "OLD", "SMALL"],
            "decile": [9, 9, 9],
            "composite": [0.7, 0.9, 0.8],
            "turnover_quote": [1e6, 1e6, 1e6],
            "ret1": [0.26, 0.26, 0.20],
            "max_ret168": [0.26, 0.30, 0.20],
        }
    )
    config = ContinuousDemoCycleConfig(
        entry_event_trigger="fresh_pop25",
        max_new_entries_per_cycle=5,
    )

    selected = select_continuous_entries(
        state,
        held_symbols=set(),
        cooldown_symbols=set(),
        open_count=0,
        config=config,
        eligible_symbols={"FRESH", "OLD"},
    )

    assert [row["symbol"] for row in selected] == ["FRESH"]


def test_entry_funnel_observer_preserves_legacy_candidate_decisions() -> None:
    state = pl.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "decile": [9, 9, 9],
            "composite": [0.9, 0.8, 0.7],
            "turnover_quote": [1_000_000.0, 100_000.0, 900_000.0],
            "rv_168h": [0.01, 0.02, 0.03],
        }
    )
    config = ContinuousDemoCycleConfig(
        max_active=5,
        max_new_entries_per_cycle=2,
        ensemble_components=(
            ("first", "none", 0, 0.12, 0.4, 0.35),
            ("second", "none", 0, 0.12, 0.6, 0.35),
        ),
    )
    signal_ts = 1_700_000_000_000
    prices = {"A": 10.0, "B": 20.0, "C": 30.0}

    legacy: list[dict[str, Any]] = []
    for component, _trigger, _age, take_profit_pct, weight, stop_loss_pct in config.ensemble_components:
        if len(legacy) >= 2:
            break
        picks = select_continuous_entries(
            state,
            held_symbols=set(),
            cooldown_symbols=set(),
            open_count=len(legacy),
            config=config,
            eligible_symbols=None,
        )
        component_candidates, _ = _continuous_entry_candidates_with_signal_metadata(
            picks,
            pl.DataFrame(),
            signal_ts=signal_ts,
            strategy_id="continuous_fade_v2",
            price_by_symbol=prices,
        )
        for candidate in component_candidates:
            if len(legacy) >= 2:
                break
            legacy.append(
                {
                    **candidate,
                    "component": component,
                    "component_weight": weight,
                    "take_profit_pct": take_profit_pct,
                    "stop_loss_pct": stop_loss_pct,
                    "trade_id": f"{candidate['trade_id']}-{component}",
                }
            )

    observed = _observe_continuous_component_selection(
        state,
        universe=pl.DataFrame(),
        klines=pl.DataFrame(),
        reserved_symbols=set(),
        reservations_count=0,
        entry_capacity=2,
        all_trades=pl.DataFrame(),
        signal_ts=signal_ts,
        strategy_id="continuous_fade_v2",
        price_by_symbol=prices,
        now_ms=signal_ts + 2 * MS_PER_HOUR,
        config=config,
        active_entries_enabled=True,
    )

    assert list(observed.candidates) == legacy
    assert observed.funnel_rows == (
        {
            "component": "first",
            "d9": 3,
            "liquidity": 2,
            "event": 2,
            "age": 2,
            "available": 2,
            "capacity": 2,
            "reserved": 0,
            "same_signal_reentry": 0,
        },
        {
            "component": "second",
            "d9": 3,
            "liquidity": 2,
            "event": 2,
            "age": 2,
            "available": 2,
            "capacity": 0,
            "reserved": 0,
            "same_signal_reentry": 0,
        },
    )

    reasons = _qualified_block_reasons(
        observed,
        preselection_reason="btc_trend_gate",
        btc_risk_reason="",
    )
    blocked = _blocked_rows_from_reasons(observed, reasons)
    assert len(blocked) == 4
    assert {row["symbol"] for row in blocked} == {"A", "C"}
    assert {row["first_rejection_reason"] for row in blocked} == {"btc_trend_gate"}
    assert (
        _first_entry_rejection_reason(
            observed,
            preselection_reason="btc_trend_gate",
            btc_risk_reason="",
            raw_entry_intent_count=0,
            unresolved_suppressions=0,
            terminal_suppressions=0,
            publication_error_count=0,
            published_entry_count=0,
            blocked_rows=blocked,
        )
        == "btc_trend_gate"
    )


def test_entry_funnel_names_age_qualified_same_signal_suppression() -> None:
    signal_ts = 1_700_000_000_000
    state = pl.DataFrame(
        {
            "symbol": ["TLMUSDT"],
            "decile": [9],
            "composite": [0.9],
            "turnover_quote": [1_000_000.0],
            "rv_168h": [0.01],
        }
    )
    prior = pl.DataFrame(
        [{
            "trade_id": f"continuous_fade_v2-TLMUSDT-{signal_ts}",
            "strategy_id": "continuous_fade_v2",
            "symbol": "TLMUSDT",
            "status": "closed",
            "signal_ts_ms": signal_ts,
        }]
    )
    observed = _observe_continuous_component_selection(
        state,
        universe=pl.DataFrame(),
        klines=pl.DataFrame(),
        reserved_symbols=set(),
        reservations_count=0,
        entry_capacity=1,
        all_trades=prior,
        signal_ts=signal_ts,
        strategy_id="continuous_fade_v2",
        price_by_symbol={"TLMUSDT": 0.0017},
        now_ms=signal_ts + 2 * MS_PER_HOUR,
        config=ContinuousDemoCycleConfig(
            max_active=1,
            max_new_entries_per_cycle=1,
            ensemble_components=(("p3", "none", 0, 0.12, 1.0, 0.35),),
        ),
        active_entries_enabled=True,
    )

    reasons = _qualified_block_reasons(
        observed,
        preselection_reason="",
        btc_risk_reason="",
    )
    blocked = _blocked_rows_from_reasons(observed, reasons)

    assert observed.funnel_rows[0]["age"] == 1
    assert observed.funnel_rows[0]["capacity"] == 0
    assert blocked == [{
        "component": "p3",
        "symbol": "TLMUSDT",
        "first_rejection_reason": "same_signal_reentry",
    }]


def test_listing_age_is_authoritative_over_the_rolling_kline_cache() -> None:
    now_ms = 1_000 * MS_PER_DAY
    universe = pl.DataFrame(
        {
            "symbol": ["OLD", "YOUNG", "UNKNOWN"],
            "listing_age_days": [400.0, 10.0, None],
        }
    )
    recent_cache = pl.DataFrame(
        {
            "symbol": ["OLD", "YOUNG"],
            "ts_ms": [now_ms - 5 * MS_PER_DAY, now_ms - 60 * MS_PER_DAY],
        }
    )

    eligible = _continuous_age_eligible_symbols(
        universe,
        recent_cache,
        age_days_min=240,
        now_ms=now_ms,
    )

    assert eligible == {"OLD"}
    assert (
        _continuous_age_eligible_symbols(
            universe,
            recent_cache,
            age_days_min=0,
            now_ms=now_ms,
        )
        is None
    )


def test_age_gate_falls_back_to_kline_history_when_listing_age_is_absent() -> None:
    now_ms = 1_000 * MS_PER_DAY
    universe = pl.DataFrame({"symbol": ["OLD", "YOUNG"]})
    klines = pl.DataFrame(
        {
            "symbol": ["OLD", "YOUNG"],
            "ts_ms": [now_ms - 60 * MS_PER_DAY, now_ms - 10 * MS_PER_DAY],
        }
    )

    assert _continuous_age_eligible_symbols(
        universe,
        klines,
        age_days_min=30,
        now_ms=now_ms,
    ) == {"OLD"}


def test_pending_targets_reserve_capacity_but_are_not_open_positions() -> None:
    strategy_id = "continuous_fade_v2"
    rows = pl.DataFrame(
        [
            {
                "trade_id": "filled",
                "strategy_id": strategy_id,
                "symbol": "A",
                "status": "open",
            },
            {
                "trade_id": "pending-entry",
                "strategy_id": strategy_id,
                "symbol": "B",
                "status": "target_pending",
            },
            {
                "trade_id": "pending-close",
                "strategy_id": strategy_id,
                "symbol": "C",
                "status": "target_pending",
            },
            {
                "trade_id": "closed",
                "strategy_id": strategy_id,
                "symbol": "D",
                "status": "closed",
            },
        ]
    )

    reservations = _continuous_target_reservations(rows, strategy_id)
    open_positions = _open_continuous_trades(rows, strategy_id)

    assert reservations["trade_id"].to_list() == [
        "filled",
        "pending-entry",
        "pending-close",
    ]
    assert open_positions["trade_id"].to_list() == ["filled"]


def test_same_signal_entry_is_suppressed() -> None:
    strategy_id = "continuous_fade_v2"
    signal_ts_ms = 1_700_000_000_000
    prior = pl.DataFrame(
        [
            {
                "trade_id": f"{strategy_id}-WIFUSDT-{signal_ts_ms}",
                "strategy_id": strategy_id,
                "symbol": "WIFUSDT",
                "status": "closed",
                "signal_ts_ms": signal_ts_ms,
            }
        ]
    )
    picks = [{"symbol": "WIFUSDT", "decile": 9, "composite": 1.0}]

    blocked, skipped = _continuous_entry_candidates_with_signal_metadata(
        picks,
        prior,
        signal_ts=signal_ts_ms,
        strategy_id=strategy_id,
        price_by_symbol={"WIFUSDT": 100.0},
    )

    assert blocked == []
    assert skipped == 1


def test_max_hold_is_anchored_to_first_fill_not_target_acceptance() -> None:
    config = ContinuousDemoCycleConfig(max_hold_hours=24)
    now_ms = 2_000_000_000_000
    target_only = {
        "trade_id": "target-only",
        "symbol": "A",
        "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
    }
    not_due = {
        "trade_id": "not-due",
        "symbol": "B",
        "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
        "entry_ts_ms": now_ms - 23 * MS_PER_HOUR,
    }
    due = {
        "trade_id": "due",
        "symbol": "C",
        "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
        "entry_ts_ms": now_ms - 24 * MS_PER_HOUR,
    }

    exits = plan_continuous_exits(
        [target_only, not_due, due],
        now_ms=now_ms,
        config=config,
    )

    assert exits == [
        {
            **due,
            "exit_reason": "max_hold",
            "exit_trigger_ts_ms": now_ms,
        }
    ]


def test_explicit_fill_anchored_deadline_is_respected_exactly() -> None:
    config = ContinuousDemoCycleConfig(max_hold_hours=24)
    now_ms = 2_000_000_000_000
    trade = {
        "trade_id": "deadline",
        "symbol": "A",
        "entry_ts_ms": now_ms - MS_PER_HOUR,
        "max_hold_deadline_ts_ms": now_ms,
    }

    assert (
        plan_continuous_exits(
            [trade],
            now_ms=now_ms - 1,
            config=config,
        )
        == []
    )
    assert (
        plan_continuous_exits(
            [trade],
            now_ms=now_ms,
            config=config,
        )[0]["exit_reason"]
        == "max_hold"
    )


def test_target_intents_preserve_component_identity_and_duration_metadata() -> None:
    now_ms = 1_700_000_000_123
    strategy_id = "continuous_fade_v2"
    trade_id = f"{strategy_id}-ABCUSDT-1-p3"
    config = ContinuousDemoCycleConfig(
        entry_leverage=10.0,
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        max_hold_hours=24,
        entry_btc_risk_sizing_enabled=False,
    )
    entries = _continuous_entry_target_intents(
        [
            {
                "trade_id": trade_id,
                "symbol": "ABCUSDT",
                "signal_ts_ms": now_ms - MS_PER_HOUR,
                "entry_reason": "confirmed_decile_entry",
                "component_weight": 0.25,
                "rv_168h": 0.02,
                "take_profit_pct": 0.12,
            }
        ],
        demo=config,
        equity_usdt=10_000.0,
        order_notional_frac=0.20,
        price_by_symbol={"ABCUSDT": 2.0},
        now_ms=now_ms,
        strategy_id=strategy_id,
    )

    assert len(entries) == 1
    entry = entries[0].intent
    assert entries[0].adapter_kind == SleeveAdapterKind.CONTINUOUS
    assert entry.signed_notional_usdt == pytest.approx(-250.0)
    assert entry.leverage == 10.0
    assert entry.component_id == trade_id
    assert entry.metadata["decision_reference_price"] == pytest.approx(2.0)
    assert entry.metadata["take_profit_pct"] == pytest.approx(0.12)
    assert entry.metadata["max_hold_duration_ms"] == 24 * MS_PER_HOUR
    assert entry.metadata["entry_attempt_key"] == (f"entry-attempt/{entry.target_key}")
    assert entry.metadata["quantity_authority"] == "account_kernel_demo_rules"
    # A candidate without a declared stop publishes none, so the account's
    # tighter disaster fallback applies — fail-closed. The active ensemble
    # always declares one (validated at startup); presence is pinned by
    # test_continuous_active_profile_contract.
    assert {
        "take_profit_price",
        "max_hold_ms",
        "max_hold_deadline_ts_ms",
        "stop_loss_pct",
        "stop_price",
    }.isdisjoint(entry.metadata)

    exits = _continuous_exit_target_intents(
        [
            {
                "trade_id": trade_id,
                "symbol": "ABCUSDT",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": now_ms + 24 * MS_PER_HOUR,
            }
        ],
        pl.DataFrame(
            [
                {
                    "trade_id": trade_id,
                    "strategy_id": strategy_id,
                    "symbol": "ABCUSDT",
                    "entry_leverage": 10.0,
                }
            ]
        ),
        strategy_id=strategy_id,
        now_ms=now_ms + 24 * MS_PER_HOUR,
        default_leverage=10.0,
    )
    exit_target = exits[0].intent
    assert exit_target.target_key == entry.target_key
    assert exit_target.signed_notional_usdt == 0.0
    assert exit_target.reason == "max_hold"


def test_account_publication_is_exit_first_and_component_entries_are_independent(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)
    publisher = AccountTargetPublisher(route)
    now_ms = 1_700_000_000_123
    strategy_id = "continuous_fade_v2"
    config = ContinuousDemoCycleConfig(
        entry_btc_risk_sizing_enabled=False,
        sizing_mode="flat",
    )
    candidates = [
        {
            "trade_id": f"new-{component}",
            "symbol": "NEWUSDT",
            "signal_ts_ms": now_ms - MS_PER_HOUR,
            "component_weight": weight,
            "take_profit_pct": 0.12,
        }
        for component, weight in (("p3", 0.4), ("p4p5", 0.6))
    ]
    entry_intents = _continuous_entry_target_intents(
        candidates,
        demo=config,
        equity_usdt=10_000.0,
        order_notional_frac=0.02,
        price_by_symbol={"NEWUSDT": 100.0},
        now_ms=now_ms,
        strategy_id=strategy_id,
    )
    exit_intents = _continuous_exit_target_intents(
        [
            {
                "trade_id": "old-p3",
                "symbol": "OLDUSDT",
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": now_ms,
            }
        ],
        pl.DataFrame(
            [
                {
                    "trade_id": "old-p3",
                    "strategy_id": strategy_id,
                    "symbol": "OLDUSDT",
                    "entry_leverage": 10.0,
                }
            ]
        ),
        strategy_id=strategy_id,
        now_ms=now_ms,
        default_leverage=10.0,
    )

    publication = publish_exit_first_target_requests(
        publisher,
        batch_prefix=f"continuous-target/{strategy_id}/{now_ms}",
        exit_intents=exit_intents,
        entry_intents=entry_intents,
        created_ts_ns=now_ms * 1_000_000,
        independent_entry_requests=True,
    )

    assert publication.errors == ()
    assert len(publication.exit_requests) == 1
    assert len(publication.entry_requests) == 2
    assert all(len(receipt.request.intents) == 1 for receipt in publication.entry_requests)
    assert all(
        receipt.request.route_id == route.route_id
        and receipt.request.account_id == route.account_id
        and receipt.request.environment == route.environment
        for receipt in (
            *publication.exit_requests,
            *publication.entry_requests,
        )
    )
    assert publication.entry_request_ids == tuple(item.request.request_id for item in publication.entry_requests)

    claimed = [publisher.inbox.claim_next() for _ in range(3)]
    requests = [item[1] for item in claimed if item is not None]
    assert len(requests) == 3
    assert requests[0].intents[0].intent.signed_notional_usdt == 0.0
    assert [request.intents[0].intent.component_id for request in requests[1:]] == ["new-p3", "new-p4p5"]


def test_cycle_publishes_exit_and_independent_component_entries_through_one_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import liquidity_migration.continuous_demo as module
    import liquidity_migration.strategy_planning as planning_module

    route = _route(tmp_path / "route")
    candidate_path = (tmp_path / "candidate-universe.json").absolute()
    now_ms = 2_000_000_000_000
    strategy_id = "continuous_fade_v2"
    config = ContinuousDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=route.account_root,
        account_intent_inbox_root=route.inbox_root,
        btc_trend_gate="off",
        entry_btc_risk_sizing_enabled=False,
        sizing_mode="flat",
        max_active=5,
        max_new_entries_per_cycle=5,
        max_hold_hours=24,
        candidate_universe_file=str(candidate_path),
        ensemble_components=(
            ("p3", "none", 0, 0.12, 0.4, 0.35),
            ("p4p5", "none", 0, 0.12, 0.6, 0.35),
        ),
    )
    canonical = pl.DataFrame(
        [
            {
                "trade_id": "old-p3",
                "strategy_id": strategy_id,
                "symbol": "OLDUSDT",
                "status": "open",
                "entry_ts_ms": now_ms - 25 * MS_PER_HOUR,
                "entry_leverage": 10.0,
            }
        ]
    )
    universe = pl.DataFrame({"symbol": ["NEWUSDT"], "listing_age_days": [365.0]})
    tickers = pl.DataFrame(
        {
            "symbol": ["NEWUSDT"],
            "mark_price": [100.0],
            "last_price": [100.0],
        }
    )
    klines = pl.DataFrame(
        {
            "symbol": ["NEWUSDT"],
            "ts_ms": [now_ms - MS_PER_HOUR],
            "close": [100.0],
            "turnover_quote": [1_000_000.0],
        }
    )
    state = pl.DataFrame(
        {
            "symbol": ["NEWUSDT"],
            "decile": [9],
            "composite": [1.0],
            "turnover_quote": [1_000_000.0],
            "rv_168h": [0.01],
        }
    )
    rmom = pl.DataFrame(
        {
            "symbol": ["NEWUSDT"],
            "day_ts": [(now_ms // MS_PER_DAY) * MS_PER_DAY],
            "residual_momentum": [-0.1],
        }
    )
    captured: dict[str, Any] = {}
    owner_health_call: dict[str, Any] = {}
    frozen_candidate = SimpleNamespace(
        path=candidate_path,
        artifact_sha256="a" * 64,
    )
    candidate_loads = 0

    monkeypatch.setattr(module, "apply_continuous_demo_profile", lambda value: value)

    def load_candidate(path: str) -> SimpleNamespace:
        nonlocal candidate_loads
        candidate_loads += 1
        assert Path(path).absolute() == candidate_path
        return frozen_candidate

    monkeypatch.setattr(module, "load_candidate_universe", load_candidate)

    def resolve_universe(
        **kwargs: Any,
    ) -> tuple[pl.DataFrame, list[str], pl.DataFrame, str, None]:
        assert kwargs["frozen_candidate_universe"] is frozen_candidate
        return universe, ["NEWUSDT"], tickers, "fixture", None

    monkeypatch.setattr(
        module,
        "_resolve_cycle_universe",
        resolve_universe,
    )
    monkeypatch.setattr(
        module,
        "_download_recent_1h_klines",
        lambda *_args, **_kwargs: (klines, {"store_rows": klines.height}),
    )
    monkeypatch.setattr(
        module,
        "_load_rmom_snapshot",
        lambda _root: ResidualMomentumSnapshot(
            table=rmom,
            source_path=str(tmp_path / "residual_momentum.parquet"),
            source_sha256="b" * 64,
            source_size_bytes=123,
            source_mtime_ns=456,
        ),
    )
    monkeypatch.setattr(
        module,
        "build_live_continuous_state",
        lambda *_args, **_kwargs: state,
    )
    monkeypatch.setattr(
        module,
        "build_confirmed_entry_state",
        lambda *_args, **_kwargs: state,
    )

    def owner_health(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        owner_health_call.update(kwargs)
        return SimpleNamespace(equity_usdt=10_000.0)

    monkeypatch.setattr(planning_module, "require_recent_account_owner_health", owner_health)
    monkeypatch.setattr(
        planning_module,
        "canonical_strategy_trade_rows",
        lambda *_args, **_kwargs: canonical,
    )
    monkeypatch.setattr(
        planning_module,
        "terminal_entry_attempt_keys",
        lambda *_args, **_kwargs: frozenset(),
    )
    monkeypatch.setattr(
        module,
        "canonical_adverse_reduction_events",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(module, "_btc_trend_gate_value", lambda *_args, **_kwargs: None)

    def capture_publication(
        publisher: AccountTargetPublisher,
        **kwargs: Any,
    ) -> ExitFirstPublication:
        assert publisher.route == route
        captured.update(kwargs)
        return ExitFirstPublication(exit_requests=(), entry_requests=(), errors=())

    monkeypatch.setattr(
        module,
        "publish_exit_first_target_requests",
        capture_publication,
    )

    payload = run_continuous_demo_cycle(
        tmp_path / "producer",
        config=ResearchConfig(),
        demo_config=config,
        now_ms=now_ms,
    )

    assert type(payload) is PublishedTargetCyclePayload
    assert payload.publication == ExitFirstPublication((), (), ())
    assert payload.route == route
    assert captured["independent_entry_requests"] is True
    assert len(captured["exit_intents"]) == 1
    assert captured["exit_intents"][0].intent.signed_notional_usdt == 0.0
    assert len(captured["entry_intents"]) == 2
    assert len({item.intent.target_key for item in captured["entry_intents"]}) == 2
    assert payload["planned_exits"] == 1
    assert payload["candidates"] == 2
    assert payload["account_target_route"] is True
    assert payload["candidate_universe_artifact_sha256"] == "a" * 64
    assert payload["entry_funnel_d9"] == 2
    assert payload["entry_funnel_liquidity"] == 2
    assert payload["entry_funnel_event"] == 2
    assert payload["entry_funnel_age"] == 2
    assert payload["entry_funnel_capacity"] == 2
    assert payload["qualified_but_blocked_count"] == 0
    assert payload["entry_feature_state_rows"] == 1
    assert len(payload["entry_feature_state_sha256"]) == 64
    assert payload["rmom_source_sha256"] == "b" * 64
    status = read_continuous_cycle_status(tmp_path / "producer")
    assert status.cycle_id == payload["cycle_id"]
    assert status.entry_funnel[0]["component"] == "p3"
    assert candidate_loads == 1
    assert "now_ns" not in owner_health_call

    blocked_payload = run_continuous_demo_cycle(
        tmp_path / "blocked-producer",
        config=ResearchConfig(),
        demo_config=replace(config, btc_trend_gate="uptrend"),
        now_ms=now_ms,
    )
    assert blocked_payload["candidates"] == 0
    assert blocked_payload["entry_funnel_capacity"] == 2
    assert blocked_payload["qualified_but_blocked_count"] == 2
    assert blocked_payload["qualified_but_blocked_symbols"] == "NEWUSDT"
    assert blocked_payload["entry_first_rejection_reason"] == "btc_trend_gate"
    assert captured["entry_intents"] == []


def test_cross_wired_account_route_fails_before_cycle_resources(tmp_path: Path) -> None:
    route_a = _route(tmp_path / "a")
    route_b = _route(tmp_path / "b")
    config = ContinuousDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=route_b.account_root,
        account_intent_inbox_root=route_a.inbox_root,
    )

    with pytest.raises(AccountRouteMismatchError):
        run_continuous_demo_cycle(
            tmp_path / "producer",
            config=ResearchConfig(),
            demo_config=config,
            now_ms=2_000_000_000_000,
        )


def test_profile_resolves_only_the_active_target_contract() -> None:
    config = apply_continuous_demo_profile(ContinuousDemoCycleConfig(execution_environment="demo"))

    assert CONTINUOUS_DEMO_PROFILES == ("continuous_ensemble_v2",)
    assert config.rmom_quantile == pytest.approx(0.25)
    assert config.feature_set == ("max_ret168",)
    assert config.max_hold_hours == 24
    assert config.entry_confirm_delay_hours == 1
    assert config.sizing_mode == "inverse_vol"
    assert config.target_vol_per_name == pytest.approx(0.01)
    assert config.vol_weight_clamp == pytest.approx(2.0)
    assert config.ensemble_components == (
        ("p3", "turn3_pop3", 240, 0.12, 0.3333333333333333, 0.35),
        ("p4p3", "turn4_pop3", 240, 0.12, 0.2222222222222222, 0.35),
        ("p4p5", "turn4_pop5", 240, 0.12, 0.4444444444444444, 0.35),
    )
    assert continuous_managed_strategy_ids(config) == ("continuous_fade_v2",)
    assert (
        continuous_strategy_id(ContinuousDemoCycleConfig(execution_environment="paper")) == "continuous_fade_v2_paper"
    )


def test_runtime_validation_requires_exactly_one_target_environment_and_route(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="execution_environment"):
        _validate_continuous_demo_config(ContinuousDemoCycleConfig())
    with pytest.raises(ValueError, match="execution_environment"):
        _validate_continuous_demo_config(_routed_config(tmp_path / "invalid", environment="live"))
    with pytest.raises(ValueError, match="operational demo/paper mode requires"):
        _validate_continuous_demo_config(ContinuousDemoCycleConfig(execution_environment="demo"))
    with pytest.raises(ValueError, match="configured together"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                execution_environment="demo",
                account_execution_root=str(tmp_path / "account-only"),
            )
        )

    _validate_continuous_demo_config(_routed_config(tmp_path / "demo"))
    _validate_continuous_demo_config(_routed_config(tmp_path / "paper", environment="paper"))


def test_runtime_validation_rejects_retired_profile_and_invalid_signal_timing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unknown continuous strategy_profile"):
        _validate_continuous_demo_config(
            _routed_config(
                tmp_path / "profile",
                strategy_profile="retired_continuous_profile",
            )
        )
    with pytest.raises(ValueError, match="confirmed-bar"):
        _validate_continuous_demo_config(
            _routed_config(
                tmp_path / "timing",
                entry_event_trigger="fresh_pop25",
                entry_confirm_delay_hours=0,
            )
        )


def test_notional_multiplier_scales_exposure_and_is_validated(tmp_path: Path) -> None:
    config = _routed_config(
        tmp_path,
        per_position_notional_pct_equity=2.0,
        notional_multiplier=10.0,
        entry_leverage=10.0,
    )

    assert _continuous_base_notional_pct_equity(config) == pytest.approx(20.0)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="notional_multiplier must be positive"):
            _validate_continuous_demo_config(_routed_config(tmp_path / str(bad), notional_multiplier=bad))


def test_cycles_datasets_keep_demo_and_paper_telemetry_separate() -> None:
    demo = continuous_cycles_dataset(ContinuousDemoCycleConfig(execution_environment="demo"))
    paper = continuous_cycles_dataset(ContinuousDemoCycleConfig(execution_environment="paper"))

    assert demo == "continuous_fade_demo_cycles"
    assert paper == "continuous_fade_paper_cycles"


@pytest.mark.parametrize(
    ("gate", "value", "expected"),
    [
        ("off", None, True),
        ("off", -1.0, True),
        ("uptrend", 0.01, True),
        ("uptrend", -0.01, False),
        ("uptrend", None, False),
        ("downtrend", -0.01, True),
        ("downtrend", 0.01, False),
    ],
)
def test_btc_trend_gate_is_fail_closed_when_context_is_required(
    gate: str,
    value: float | None,
    expected: bool,
) -> None:
    assert _btc_trend_gate_allows_value(gate, value) is expected


def test_btc_gate_and_risk_payloads_are_deterministic() -> None:
    gate_fields = _btc_trend_gate_payload_fields(
        config=ContinuousDemoCycleConfig(btc_trend_gate="uptrend"),
        allows_entry=True,
        trend_value=0.12,
        kline_stats={"btc_rows": 42, "btc_max_ts_ms": 123, "fetch_symbols": 1},
    )
    risk_fields = _btc_risk_sizing_payload_fields(
        {
            "enabled": True,
            "arm_id": "btc-risk-v1",
            "candidate_rows": 3,
            "scored": 2,
            "tail_selected": 1,
            "mean_btc_risk_score": 0.4,
        }
    )

    assert gate_fields["btc_trend_gate"] == "uptrend"
    assert gate_fields["btc_trend_gate_lookback_duration_ms"] == 30 * MS_PER_DAY
    assert gate_fields["btc_trend_gate_btc_rows"] == 42
    assert gate_fields["btc_trend_gate_kline_fetch_symbols"] == 1
    assert risk_fields["btc_risk_sizing_enabled"] is True
    assert risk_fields["btc_risk_sizing_candidate_rows"] == 3
    assert risk_fields["btc_risk_sizing_tail_selected"] == 1
    assert risk_fields["btc_risk_sizing_mean_score"] == pytest.approx(0.4)
    assert risk_fields["btc_risk_sizing_entry_blocked"] is False


def test_btc_risk_multiplier_uses_only_a_positive_causal_decision() -> None:
    enabled = ContinuousDemoCycleConfig(entry_btc_risk_sizing_enabled=True)
    disabled = ContinuousDemoCycleConfig(entry_btc_risk_sizing_enabled=False)

    assert _continuous_btc_risk_multiplier(enabled, {"btc_risk_stack_mult": 0.35}) == pytest.approx(0.35)
    assert _continuous_btc_risk_multiplier(enabled, {"btc_risk_stack_mult": float("nan")}) == pytest.approx(1.0)
    assert _continuous_btc_risk_multiplier(disabled, {"btc_risk_stack_mult": 0.35}) == pytest.approx(1.0)


def test_rmom_loader_degrades_on_absent_corrupt_or_unprovenanced_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "residual_momentum.parquet"
    assert _load_rmom_table(tmp_path) is None
    path.write_bytes(b"not parquet")
    assert _load_rmom_table(tmp_path) is None
    pl.DataFrame([{"symbol": "AAA", "ts_ms": MS_PER_DAY, "residual_momentum": -0.1}]).write_parquet(path)
    assert _load_rmom_table(tmp_path) is None


def test_rmom_loader_keeps_only_stable_daily_rows(tmp_path: Path) -> None:
    pl.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": -0.1,
                "is_provisional": False,
            },
            {
                "symbol": "AAA",
                "ts_ms": 2 * MS_PER_DAY,
                "residual_momentum": -0.2,
                "is_provisional": True,
            },
        ]
    ).write_parquet(tmp_path / "residual_momentum.parquet")

    loaded = _load_rmom_table(tmp_path)

    assert loaded is not None
    assert loaded.select(["symbol", "day_ts", "residual_momentum"]).to_dicts() == [
        {"symbol": "AAA", "day_ts": MS_PER_DAY, "residual_momentum": -0.1}
    ]


def test_feature_and_rmom_identities_bind_exact_consumed_rows(tmp_path: Path) -> None:
    path = tmp_path / "residual_momentum.parquet"
    pl.DataFrame(
        [
            {
                "symbol": "BBB",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": -0.2,
                "is_provisional": False,
            },
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": -0.1,
                "is_provisional": False,
            },
        ]
    ).write_parquet(path)
    snapshot = _load_rmom_snapshot(tmp_path)
    assert snapshot is not None
    assert snapshot.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()

    rmom_first = _rmom_identity_payload_fields(
        snapshot,
        signal_day_ts_ms=MS_PER_DAY,
    )
    rmom_second = _rmom_identity_payload_fields(
        snapshot,
        signal_day_ts_ms=MS_PER_DAY,
    )
    assert rmom_first == rmom_second
    assert rmom_first["rmom_signal_day_rows"] == 2
    assert len(rmom_first["rmom_signal_day_sha256"]) == 64

    state = pl.DataFrame(
        {
            "symbol": ["BBB", "AAA"],
            "decile": [8, 9],
            "composite": [0.5, 0.9],
            "turnover_quote": [800_000.0, 900_000.0],
        }
    )
    config = ContinuousDemoCycleConfig()
    identity = _entry_feature_identity_payload_fields(
        state,
        signal_ts_ms=2 * MS_PER_DAY,
        config=config,
    )
    reordered = _entry_feature_identity_payload_fields(
        state.reverse(),
        signal_ts_ms=2 * MS_PER_DAY,
        config=config,
    )
    changed = _entry_feature_identity_payload_fields(
        state.with_columns(
            pl.when(pl.col("symbol") == "AAA")
            .then(pl.col("composite") + 0.01)
            .otherwise(pl.col("composite"))
            .alias("composite")
        ),
        signal_ts_ms=2 * MS_PER_DAY,
        config=config,
    )
    assert identity["entry_feature_state_sha256"] == reordered["entry_feature_state_sha256"]
    assert identity["entry_feature_state_sha256"] != changed["entry_feature_state_sha256"]


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": float("nan"),
                "is_provisional": False,
            }
        ],
        [
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY + 1,
                "residual_momentum": -0.1,
                "is_provisional": False,
            }
        ],
        [
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": -0.1,
                "is_provisional": False,
            },
            {
                "symbol": "AAA",
                "ts_ms": MS_PER_DAY,
                "residual_momentum": -0.2,
                "is_provisional": False,
            },
        ],
    ],
)
def test_rmom_loader_rejects_nonfinite_nondaily_or_duplicate_keys(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> None:
    pl.DataFrame(rows).write_parquet(tmp_path / "residual_momentum.parquet")
    assert _load_rmom_table(tmp_path) is None


def test_rmom_freshness_fields_are_stable_and_floor_future_rows() -> None:
    current_day = 10 * MS_PER_DAY
    future = pl.DataFrame({"day_ts": [current_day + MS_PER_DAY]})

    assert _rmom_freshness_payload_fields(
        future,
        current_day_ts=current_day,
    ) == {
        "rmom_present": True,
        "max_rmom_day_ts": current_day + MS_PER_DAY,
        "rmom_stale_days": 0,
    }
    assert _rmom_freshness_payload_fields(
        None,
        current_day_ts=current_day,
    ) == {
        "rmom_present": False,
        "max_rmom_day_ts": 0,
        "rmom_stale_days": None,
    }


def test_live_panel_cache_matches_full_recompute_and_reuses_carry() -> None:
    klines, rmom, start, n_bars = _dispersed_synth()
    config = ContinuousDemoCycleConfig()
    current_hour = start + (n_bars - 1) * MS_PER_HOUR
    history = klines.filter(pl.col("ts_ms") < current_hour)
    base_prices = {
        str(row["symbol"]): float(row["close"]) for row in klines.filter(pl.col("ts_ms") == current_hour).to_dicts()
    }
    cache = LivePanelCache(
        rmom_quantile=config.rmom_quantile,
        feature_set=config.feature_set,
        exclude_symbols=config.exclude_symbols,
    )

    for ordinal, multiplier in enumerate((1.0, 1.01, 0.98), start=1):
        prices = {symbol: price * multiplier for symbol, price in base_prices.items()}
        now_ms = current_hour + ordinal * 60_000
        cached = cache.state(
            history,
            prices,
            rmom,
            now_ts_ms=now_ms,
            config=config,
        )
        reference = build_live_continuous_state(
            history,
            prices,
            rmom,
            now_ts_ms=now_ms,
            config=config,
        )
        cached_deciles = _decile_map(cached)
        reference_deciles = _decile_map(reference)
        assert {symbol for symbol, decile in cached_deciles.items() if decile == 9} == {
            symbol for symbol, decile in reference_deciles.items() if decile == 9
        }
        assert {symbol for symbol, decile in cached_deciles.items() if decile >= 8} == {
            symbol for symbol, decile in reference_deciles.items() if decile >= 8
        }
        cached_composite = _composite_map(cached)
        reference_composite = _composite_map(reference)
        symbols = sorted(cached_composite)
        assert np.allclose(
            [cached_composite[symbol] for symbol in symbols],
            [reference_composite[symbol] for symbol in symbols],
            atol=1e-9,
            rtol=1e-6,
        )


def test_live_panel_cache_invalidates_on_corrected_confirmed_bar() -> None:
    klines, rmom, start, n_bars = _dispersed_synth()
    config = ContinuousDemoCycleConfig()
    current_hour = start + (n_bars - 1) * MS_PER_HOUR
    history = klines.filter(pl.col("ts_ms") < current_hour)
    prices = {
        str(row["symbol"]): float(row["close"]) for row in klines.filter(pl.col("ts_ms") == current_hour).to_dicts()
    }
    cache = LivePanelCache(
        rmom_quantile=config.rmom_quantile,
        feature_set=config.feature_set,
        exclude_symbols=config.exclude_symbols,
    )
    cache.state(
        history,
        prices,
        rmom,
        now_ts_ms=current_hour + 1,
        config=config,
    )
    last_ts = int(history["ts_ms"].max())
    corrected = history.with_columns(
        pl.when((pl.col("symbol") == "S05") & (pl.col("ts_ms") == last_ts))
        .then(pl.col("close") * 1.05)
        .otherwise(pl.col("close"))
        .alias("close")
    )

    cached = cache.state(
        corrected,
        prices,
        rmom,
        now_ts_ms=current_hour + 60_000,
        config=config,
    )
    reference = build_live_continuous_state(
        corrected,
        prices,
        rmom,
        now_ts_ms=current_hour + 60_000,
        config=config,
    )

    cached_deciles = _decile_map(cached)
    reference_deciles = _decile_map(reference)
    assert {symbol for symbol, decile in cached_deciles.items() if decile == 9} == {
        symbol for symbol, decile in reference_deciles.items() if decile == 9
    }
    assert {symbol for symbol, decile in cached_deciles.items() if decile >= 8} == {
        symbol for symbol, decile in reference_deciles.items() if decile >= 8
    }


def test_summary_formatter_is_deterministic_for_the_flat_cycle_payload() -> None:
    payload = {
        "cycle_id": "continuous-target-1",
        "mode": "demo_target",
        "universe_symbols": 550,
        "rmom_present": True,
        "max_rmom_day_ts": 1_780_444_800_000,
        "rmom_stale_days": 0,
        "live_d9_symbols": 17,
        "candidates": 2,
        "entries": 0,
        "exits": 0,
        "open_positions": 2,
        "equity_usdt": 12_345.6,
        "notional_multiplier": 10.0,
        "entry_leverage": 10.0,
        "entry_paused": False,
        "entry_risk_health_ok": True,
    }

    first = format_continuous_demo_cycle_summary(payload)
    second = format_continuous_demo_cycle_summary(dict(reversed(list(payload.items()))))

    assert first == second
    assert "mode=demo_target" in first
    assert "d9=17" in first
    assert "entries=0 exits=0 open=2" in first
    assert "sizing=10x_notional/10x_leverage" in first
    assert first.startswith("continuous target producer ")
    assert "telegram=" not in first
    assert "risk_health=" not in first
    assert "$0.00" in format_continuous_demo_cycle_summary({"rmom_present": False})
    assert _payload_float("12.5") == pytest.approx(12.5)
    assert _payload_float("bad") == 0.0


def _reduction_event(
    *,
    pnl_key: str,
    local_receive_ts_ns: int,
    component_count: int = 1,
) -> CanonicalReductionEvent:
    target_keys = tuple(f"continuous/continuous_fade_v2/component-{index}/ABCUSDT" for index in range(component_count))
    return CanonicalReductionEvent(
        pnl_key=pnl_key,
        close_key=f"close-{pnl_key}",
        batch_id=f"batch-{pnl_key}",
        symbol="ABCUSDT",
        local_receive_ts_ns=local_receive_ts_ns,
        exchange_ts_ns=local_receive_ts_ns - 1,
        source="venue_closed_pnl",
        accounting_scope="symbol_reduce_batch",
        component_attribution_status="matched",
        all_component_target_keys=target_keys,
        matched_component_target_keys=target_keys,
        component_ids=tuple(f"component-{i}" for i in range(component_count)),
        component_reasons=("max_hold",),
        gross_pnl_usdt=-10.0,
        fee_usdt=-1.0,
        funding_usdt=0.0,
        net_pnl_usdt=-11.0,
        fee_status="final",
        funding_status="final",
        venue_closed_pnl_status="final",
        pnl_finalization_status="final",
        adverse=True,
        adverse_basis="negative_net_pnl",
    )


def test_breaker_counts_canonical_reduction_batches_not_component_rows() -> None:
    now_ms = 2_000_000_000_000
    cutoff_ms = now_ms - 60 * 60_000
    config = ContinuousDemoCycleConfig(
        entry_pause_after_adverse_exits=2,
        entry_pause_window_minutes=60,
    )
    events = (
        _reduction_event(
            pnl_key="grouped-close",
            local_receive_ts_ns=cutoff_ms * 1_000_000,
            component_count=3,
        ),
        _reduction_event(
            pnl_key="second-close",
            local_receive_ts_ns=now_ms * 1_000_000,
        ),
        _reduction_event(
            pnl_key="too-old",
            local_receive_ts_ns=(cutoff_ms - 1) * 1_000_000,
        ),
    )

    tripped, count = entry_circuit_breaker_tripped(
        events,
        now_ms=now_ms,
        config=config,
    )

    assert count == 2
    assert tripped is True
