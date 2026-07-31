"""Refactor-parity contract for the sole ``continuous_ensemble_v2`` profile: deleting
retired CONT paths must preserve confirmed entry timing, component targets, causal size
multipliers, max-hold sleeve exits, and account-target identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.account_service import SleeveAdapterKind
from liquidity_migration.continuous_btc_risk import (
    BTC_RISK_COMPONENTS,
    BTC_RISK_EVIDENCE_METADATA_KEY,
    BtcRiskLiveSizer,
)
from liquidity_migration.continuous_demo import (
    PRIOR_BAR_COLUMN_PREFIX,
    CONTINUOUS_DEMO_PROFILES,
    CONTINUOUS_V2_PROFILE,
    ContinuousDemoCycleConfig,
    _continuous_btc_risk_multiplier,
    _continuous_entry_target_intents,
    _continuous_exit_target_intents,
    _continuous_vol_weight_multiplier,
    _validate_continuous_demo_config,
    apply_continuous_demo_profile,
    build_confirmed_entry_state,
    plan_continuous_exits,
)
from liquidity_migration.continuous_events import compute_continuous_decile_panel


ACTIVE_COMPONENTS = (
    # Single turn3_pop3 cell with the settled-funding >= 0 admission
    # (docs/research_findings.md).
    ("p3", "turn3_pop3", 240, 0.12, 1.0, 0.35, 0.0),
)
STRATEGY_ID = "continuous_fade_v2"
SYMBOL = "ABCUSDT"


def _active_profile() -> ContinuousDemoCycleConfig:
    return apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            strategy_profile=CONTINUOUS_V2_PROFILE,
            btc_trend_gate="uptrend",
        )
    )


def _routed_profile(
    tmp_path: Path,
    *,
    environment: str,
) -> ContinuousDemoCycleConfig:
    return apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            strategy_profile=CONTINUOUS_V2_PROFILE,
            btc_trend_gate="uptrend",
            execution_environment=environment,
            account_execution_root=str(tmp_path / "account"),
            account_intent_inbox_root=str(tmp_path / "inbox"),
        )
    )


def _confirmed_panel_fixture(
    *,
    n_symbols: int = 26,
    n_bars: int = 220,
) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    start = 1_700_000_000_000
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
    current_hour = start + (n_bars - 1) * MS_PER_HOUR
    return klines, rmom, current_hour, current_hour + 1_800_000


def _accepted_tail_evidence(
    state_path: Path,
    *,
    profile: ContinuousDemoCycleConfig,
    signal_ts_ms: int,
) -> dict[str, Any]:
    sizer = BtcRiskLiveSizer(
        state_path,
        low=profile.entry_btc_risk_low,
        high=profile.entry_btc_risk_high,
        tail_mult=profile.entry_btc_risk_tail_mult,
        min_prior=profile.entry_btc_risk_min_prior,
        arm_id=profile.entry_btc_risk_arm_id,
    )
    prior_values = {
        "btc_trend_30d": 0.0,
        "btc_return_7d": 0.0,
        "btc_vol_30d": 0.1,
        "btc_trend_delta_7d": 0.0,
    }
    prior_decisions = [
        {"symbol": f"H{index:02d}USDT", "signal_ts_ms": (index + 1) * MS_PER_DAY}
        for index in range(profile.entry_btc_risk_min_prior)
    ]
    prior_context = {int(row["signal_ts_ms"]): dict(prior_values) for row in prior_decisions}
    lookup, stats = sizer.score_decisions(
        prior_decisions,
        btc_context=prior_context,
    )
    assert stats["scored"] == profile.entry_btc_risk_min_prior
    sizer.reconcile_authoritative_accepted_decisions(
        [
            {
                BTC_RISK_EVIDENCE_METADATA_KEY: lookup[(str(row["symbol"]), int(row["signal_ts_ms"]))][
                    "decision_evidence"
                ]
            }
            for row in prior_decisions
        ]
    )
    tail_values = {
        "btc_trend_30d": -1.0,
        "btc_return_7d": -1.0,
        "btc_vol_30d": 1.0,
        "btc_trend_delta_7d": 1.0,
    }
    tail_lookup, tail_stats = sizer.score_decisions(
        [{"symbol": SYMBOL, "signal_ts_ms": signal_ts_ms}],
        btc_context={
            (signal_ts_ms // MS_PER_DAY) * MS_PER_DAY: tail_values,
        },
    )
    assert tail_stats["scored"] == 1
    evidence = tail_lookup[(SYMBOL, signal_ts_ms)]["decision_evidence"]
    assert evidence["result"]["prior_decision_count"] == profile.entry_btc_risk_min_prior
    assert evidence["result"]["btc_risk_score"] == pytest.approx(0.75)
    assert evidence["result"]["stack_mult"] == pytest.approx(0.35)
    return evidence


def test_only_active_profile_resolves_exact_component_decisions(
    tmp_path: Path,
) -> None:
    assert CONTINUOUS_DEMO_PROFILES == (CONTINUOUS_V2_PROFILE,)
    profile = _active_profile()

    assert profile.ensemble_components == ACTIVE_COMPONENTS
    assert sum(component[4] for component in profile.ensemble_components) == pytest.approx(1.0)
    _validate_continuous_demo_config(_routed_profile(tmp_path / "demo", environment="demo"))
    _validate_continuous_demo_config(_routed_profile(tmp_path / "paper", environment="paper"))
    with pytest.raises(ValueError, match="unknown continuous strategy_profile"):
        _validate_continuous_demo_config(
            ContinuousDemoCycleConfig(
                strategy_profile="retired_continuous_profile",
                execution_environment="demo",
                account_execution_root=str(tmp_path / "retired-account"),
                account_intent_inbox_root=str(tmp_path / "retired-inbox"),
            )
        )


def test_runtime_validation_requires_explicit_demo_or_paper_target_route(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="execution_environment"):
        _validate_continuous_demo_config(_active_profile())
    with pytest.raises(ValueError, match="operational demo/paper mode requires"):
        _validate_continuous_demo_config(
            apply_continuous_demo_profile(
                ContinuousDemoCycleConfig(
                    strategy_profile=CONTINUOUS_V2_PROFILE,
                    execution_environment="demo",
                )
            )
        )
    with pytest.raises(ValueError, match="configured together"):
        _validate_continuous_demo_config(
            apply_continuous_demo_profile(
                ContinuousDemoCycleConfig(
                    strategy_profile=CONTINUOUS_V2_PROFILE,
                    execution_environment="demo",
                    account_execution_root=str(tmp_path / "account-only"),
                )
            )
        )


def test_active_profile_uses_confirmed_bar_one_hour_after_close() -> None:
    profile = _active_profile()
    klines, rmom, current_hour, now_ms = _confirmed_panel_fixture()

    actual = build_confirmed_entry_state(
        klines,
        rmom,
        now_ts_ms=now_ms,
        config=profile,
    ).sort("symbol")
    confirmed_panel = compute_continuous_decile_panel(
        klines.filter(pl.col("ts_ms") < current_hour),
        rmom,
        rmom_quantile=profile.rmom_quantile,
        start_ms=0,
        feature_set=profile.feature_set,
    )
    deciding_bar = current_hour - 2 * MS_PER_HOUR
    # The prior confirmed bar travels alongside the deciding bar so the live
    # crowding gate can reproduce the engine's fresh-entrant rule;
    # the deciding-bar contract itself is the unprefixed columns.
    deciding_columns = [
        column for column in actual.columns if not column.startswith(PRIOR_BAR_COLUMN_PREFIX)
    ]
    deciding = actual.select(deciding_columns)
    expected = confirmed_panel.filter(pl.col("ts_ms") == deciding_bar).select(deciding_columns).sort("symbol")
    one_bar_too_recent = (
        confirmed_panel.filter(pl.col("ts_ms") == current_hour - MS_PER_HOUR)
        .select(deciding_columns)
        .sort("symbol")
    )

    assert not actual.is_empty()
    assert deciding.equals(expected)
    assert not deciding.equals(one_bar_too_recent)

    prior_bar = (
        confirmed_panel.filter(pl.col("ts_ms") == deciding_bar - MS_PER_HOUR)
        .select(["symbol", "decile"])
        .sort("symbol")
    )
    carried = (
        actual.select(["symbol", f"{PRIOR_BAR_COLUMN_PREFIX}decile"])
        .rename({f"{PRIOR_BAR_COLUMN_PREFIX}decile": "decile"})
        .drop_nulls()
        .sort("symbol")
    )
    assert carried.equals(prior_bar.filter(pl.col("symbol").is_in(carried["symbol"].to_list())))


@pytest.mark.parametrize(
    ("entry_vol", "expected"),
    [
        (None, 1.0),
        (0.0, 1.0),
        (0.001, 2.0),
        (0.01, 1.0),
        (0.02, 0.5),
        (0.10, 0.5),
    ],
)
def test_active_inverse_vol_multiplier_contract(
    entry_vol: float | None,
    expected: float,
) -> None:
    assert _continuous_vol_weight_multiplier(_active_profile(), entry_vol) == pytest.approx(expected)


def test_active_component_targets_preserve_sizing_identity_and_tp(
    tmp_path: Path,
) -> None:
    profile = _active_profile()
    signal_ts_ms = 60 * MS_PER_DAY
    now_ms = signal_ts_ms + 2 * MS_PER_HOUR + 12_345
    evidence = _accepted_tail_evidence(
        tmp_path / "btc-risk.parquet",
        profile=profile,
        signal_ts_ms=signal_ts_ms,
    )
    assert set(evidence["raw_values"]) == set(BTC_RISK_COMPONENTS)
    candidates = [
        {
            "trade_id": f"{STRATEGY_ID}-{SYMBOL}-{signal_ts_ms}-{tag}",
            "symbol": SYMBOL,
            "signal_ts_ms": signal_ts_ms,
            "entry_reason": "confirmed_decile_entry",
            "component": tag,
            "component_weight": weight,
            "take_profit_pct": take_profit_pct,
            "stop_loss_pct": stop_loss_pct,
            "funding_min_at_entry": funding_min,
            "funding_rate_at_admission": 0.0001,
            "funding_admission_unknown": False,
            "rv_168h": 0.02,
            "btc_risk_stack_mult": 0.35,
            BTC_RISK_EVIDENCE_METADATA_KEY: evidence,
        }
        for tag, _trigger, _age_days, take_profit_pct, weight, stop_loss_pct, funding_min in ACTIVE_COMPONENTS
    ]

    requested = _continuous_entry_target_intents(
        candidates,
        demo=profile,
        equity_usdt=10_000.0,
        order_notional_frac=0.02,
        price_by_symbol={SYMBOL: 100.0},
        now_ms=now_ms,
        strategy_id=STRATEGY_ID,
    )

    assert len(requested) == len(ACTIVE_COMPONENTS)
    for requested_intent, component in zip(requested, ACTIVE_COMPONENTS):
        tag, _trigger, _age_days, take_profit_pct, weight, stop_loss_pct, funding_min = component
        trade_id = f"{STRATEGY_ID}-{SYMBOL}-{signal_ts_ms}-{tag}"
        target = requested_intent.intent
        expected_notional = 10_000.0 * 0.02 * weight * 0.5 * 0.35
        expected_target_key = f"continuous/{STRATEGY_ID}/{trade_id}/{SYMBOL}"

        assert requested_intent.adapter_kind == SleeveAdapterKind.CONTINUOUS
        assert target.decision_key == (f"continuous-target/{STRATEGY_ID}/{now_ms}/entry/{trade_id}")
        assert target.target_key == expected_target_key
        assert target.strategy_id == STRATEGY_ID
        assert target.component_id == trade_id
        assert target.symbol == SYMBOL
        assert target.signed_notional_usdt == pytest.approx(-expected_notional)
        assert target.leverage == profile.entry_leverage
        assert target.reason == "confirmed_decile_entry"
        assert target.metadata["entry_attempt_key"] == f"entry-attempt/{expected_target_key}"
        assert target.metadata["signal_ts_ms"] == signal_ts_ms
        assert target.metadata["signal_valid_until_ms"] == signal_ts_ms + 3 * MS_PER_HOUR
        assert target.metadata["component_weight"] == pytest.approx(weight)
        assert target.metadata["vol_weight_multiplier"] == pytest.approx(0.5)
        assert target.metadata["btc_risk_multiplier"] == pytest.approx(0.35)
        assert target.metadata["raw_target_notional_usdt"] == pytest.approx(expected_notional)
        assert target.metadata["take_profit_pct"] == pytest.approx(take_profit_pct)
        # Declared wide backstop (§16.3/§20): published as a fraction so the
        # account places the venue stop here instead of its disaster fallback.
        assert target.metadata["stop_loss_pct"] == pytest.approx(stop_loss_pct)
        # Settled-funding admission evidence is journaled with the entry.
        assert target.metadata["funding_min_at_entry"] == pytest.approx(funding_min)
        assert target.metadata["funding_rate_at_admission"] == pytest.approx(0.0001)
        assert target.metadata["funding_admission_unknown"] is False
        assert target.metadata["max_hold_duration_ms"] == 24 * MS_PER_HOUR
        assert target.metadata["decision_reference_price"] == pytest.approx(100.0)
        # Price-level fields stay account-owned: only fractions are published.
        assert {
            "take_profit_price",
            "max_hold_deadline_ts_ms",
            "max_hold_ms",
            "stop_price",
        }.isdisjoint(target.metadata)
        assert target.metadata["quantity_authority"] == "account_kernel_demo_rules"
        assert target.metadata[BTC_RISK_EVIDENCE_METADATA_KEY] == evidence

    first_target = requested[0].intent
    exit_now_ms = now_ms + 24 * MS_PER_HOUR
    exit_request = _continuous_exit_target_intents(
        [
            {
                "trade_id": first_target.component_id,
                "symbol": SYMBOL,
                "exit_reason": "max_hold",
                "exit_trigger_ts_ms": exit_now_ms,
            }
        ],
        pl.DataFrame(
            [
                {
                    "trade_id": first_target.component_id,
                    "strategy_id": STRATEGY_ID,
                    "symbol": SYMBOL,
                    "entry_leverage": profile.entry_leverage,
                }
            ]
        ),
        strategy_id=STRATEGY_ID,
        now_ms=exit_now_ms,
        default_leverage=profile.entry_leverage,
    )[0]
    exit_target = exit_request.intent
    assert exit_request.adapter_kind == SleeveAdapterKind.CONTINUOUS
    assert exit_target.target_key == first_target.target_key
    assert exit_target.component_id == first_target.component_id
    assert exit_target.signed_notional_usdt == 0.0
    assert exit_target.reason == "max_hold"
    assert exit_target.metadata["source"] == "continuous_target_adapter"
    assert exit_target.metadata["owner_sleeve"] == "continuous"
    assert exit_target.metadata["prior_trade_id"] == first_target.component_id
    assert exit_target.metadata["exit_trigger_ts_ms"] == exit_now_ms


def test_active_btc_size_evidence_mismatch_fails_closed(tmp_path: Path) -> None:
    profile = _active_profile()
    signal_ts_ms = 60 * MS_PER_DAY
    evidence = _accepted_tail_evidence(
        tmp_path / "btc-risk-mismatch.parquet",
        profile=profile,
        signal_ts_ms=signal_ts_ms,
    )
    candidate = {
        "trade_id": f"{STRATEGY_ID}-{SYMBOL}-{signal_ts_ms}-p3",
        "symbol": SYMBOL,
        "signal_ts_ms": signal_ts_ms,
        "component_weight": ACTIVE_COMPONENTS[0][4],
        "take_profit_pct": ACTIVE_COMPONENTS[0][3],
        "rv_168h": 0.02,
        "btc_risk_stack_mult": 1.0,
        BTC_RISK_EVIDENCE_METADATA_KEY: evidence,
    }

    assert _continuous_btc_risk_multiplier(profile, candidate) == 1.0
    with pytest.raises(ValueError, match="multiplier conflicts with decision evidence"):
        _continuous_entry_target_intents(
            [candidate],
            demo=profile,
            equity_usdt=10_000.0,
            order_notional_frac=0.02,
            price_by_symbol={SYMBOL: 100.0},
            now_ms=signal_ts_ms + 2 * MS_PER_HOUR,
            strategy_id=STRATEGY_ID,
        )


def test_active_strategy_does_not_exit_before_24h() -> None:
    profile = _active_profile()
    now_ms = 2_000_000_000_000
    entry_ts_ms = now_ms - 23 * MS_PER_HOUR
    trade = {
        "trade_id": "trade-before-max-hold",
        "symbol": SYMBOL,
        "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
        "entry_ts_ms": entry_ts_ms,
    }

    exits = plan_continuous_exits(
        [trade],
        now_ms=now_ms,
        config=profile,
    )

    assert exits == []


def test_target_acceptance_without_fill_has_no_max_hold_deadline() -> None:
    profile = _active_profile()
    now_ms = 2_000_000_000_000

    exits = plan_continuous_exits(
        [
            {
                "trade_id": "target-only",
                "symbol": SYMBOL,
                "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
            }
        ],
        now_ms=now_ms,
        config=profile,
    )

    assert exits == []


def test_active_strategy_exits_at_exact_24h_for_max_hold_only() -> None:
    profile = _active_profile()
    now_ms = 2_000_000_000_000
    trade = {
        "trade_id": "trade-max-hold",
        "symbol": SYMBOL,
        "entry_target_ts_ms": now_ms - 48 * MS_PER_HOUR,
        "entry_ts_ms": now_ms - 24 * MS_PER_HOUR,
    }

    exits = plan_continuous_exits(
        [trade],
        now_ms=now_ms,
        config=profile,
    )

    assert exits == [
        {
            **trade,
            "exit_reason": "max_hold",
            "exit_trigger_ts_ms": now_ms,
        }
    ]
