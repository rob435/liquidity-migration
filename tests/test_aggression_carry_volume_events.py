from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.volume_events import (
    _attach_event_archive_membership,
    _event_decay_exit_hit,
    _event_filter,
    _full_pit_universe_error,
    VolumeEventResearchConfig,
    _add_rank_fraction,
    _monthly_returns,
    _promotion_fields,
    _validate_event_config,
    run_volume_event_research,
)


def test_add_rank_fraction_scales_cross_section_to_zero_one() -> None:
    frame = pl.DataFrame(
        [
            {"ts_ms": 1, "symbol": "A", "score": 10.0},
            {"ts_ms": 1, "symbol": "B", "score": 20.0},
            {"ts_ms": 1, "symbol": "C", "score": 30.0},
        ]
    )

    ranked = _add_rank_fraction(frame, "score", "score_rank_frac").sort("symbol")

    assert ranked["score_rank_frac"].to_list() == pytest.approx([0.0, 0.5, 1.0])


def test_volume_event_research_writes_reports_on_fixture(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    payload = run_volume_event_research(
        tmp_path,
        event_config=VolumeEventResearchConfig(
            event_types=("fresh_volume_spike",),
            thresholds=(0.5,),
            hold_days=(1,),
            side_hypotheses=("continuation",),
            stop_loss_pcts=(0.0,),
            cost_multipliers=(1.0,),
            max_active_symbols=4,
            cooldown_days=0,
            require_full_pit_universe=False,
        ),
    )

    assert payload["rows"]["scenarios"] == 1
    assert (tmp_path / "reports" / "volume_event_research" / "volume_event_research_report.md").exists()
    assert (tmp_path / "reports" / "volume_event_research" / "volume_event_scenario_summary.csv").exists()


def test_volume_event_research_requires_full_pit_by_default(tmp_path: Path) -> None:
    generate_fixture_data(tmp_path)

    with pytest.raises(RuntimeError, match="requires full PIT archive membership"):
        run_volume_event_research(
            tmp_path,
            event_config=VolumeEventResearchConfig(
                event_types=("fresh_volume_spike",),
                thresholds=(0.5,),
                hold_days=(1,),
                side_hypotheses=("continuation",),
                stop_loss_pcts=(0.0,),
                cost_multipliers=(1.0,),
            ),
        )


def test_full_pit_universe_error_reports_missing_symbols() -> None:
    features = pl.DataFrame([{"symbol": "AAAUSDT", "ts_ms": 1}])
    manifest = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "date": "2024-01-01"},
            {"symbol": "BBBUSDT", "date": "2024-01-01"},
        ]
    )

    message = _full_pit_universe_error(features, manifest)

    assert "missing_symbols=1" in message
    assert "BBBUSDT" in message


def test_volume_event_config_validates_new_research_knobs() -> None:
    with pytest.raises(ValueError, match="entry_delay_hours"):
        _validate_event_config(VolumeEventResearchConfig(entry_delay_hours=-1))

    with pytest.raises(ValueError, match="rank_exit_threshold"):
        _validate_event_config(VolumeEventResearchConfig(rank_exit_threshold=0.0))

    with pytest.raises(ValueError, match="tail_rank_min"):
        _validate_event_config(VolumeEventResearchConfig(tail_rank_min=200, tail_rank_max=100))

    with pytest.raises(ValueError, match="liquidity_migration_rank_improvement_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_rank_improvement_min=-1))

    with pytest.raises(ValueError, match="dryup_prior_volume_rank_max"):
        _validate_event_config(VolumeEventResearchConfig(dryup_prior_volume_rank_max=1.5))


def test_volume_event_promotion_requires_all_splits_positive() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": -0.01, "max_drawdown": -0.05, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.12, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
    )

    assert fields["promotion_gate_pass"] is False
    assert "positive_split_fail" in fields["promotion_reason"]


def test_volume_event_promotion_requires_pit_membership() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": 0.08, "max_drawdown": -0.12, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.15, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
        pit_membership_pass=False,
    )

    assert fields["pre_pit_gate_pass"] is True
    assert fields["promotion_gate_pass"] is False
    assert "pit_membership_fail" in fields["promotion_reason"]
    assert "full_pit_universe_fail" in fields["promotion_reason"]


def test_volume_event_promotion_requires_full_pit_universe() -> None:
    fields = _promotion_fields(
        [
            {"name": "train_2023_2024", "total_return": 0.10, "max_drawdown": -0.10, "sharpe_like": 1.0},
            {"name": "validation_2024_2025", "total_return": 0.08, "max_drawdown": -0.12, "sharpe_like": 0.8},
            {"name": "oos_2025_2026", "total_return": 0.03, "max_drawdown": -0.15, "sharpe_like": 0.7},
        ],
        config=VolumeEventResearchConfig(),
        pit_membership_pass=True,
        full_pit_universe_pass=False,
    )

    assert fields["pre_pit_gate_pass"] is True
    assert fields["pit_membership_pass"] is True
    assert fields["promotion_gate_pass"] is False
    assert fields["promotion_reason"] == "full_pit_universe_fail"


def test_attach_event_archive_membership_flags_symbol_dates() -> None:
    features = pl.DataFrame(
        [
            {"symbol": "AAAUSDT", "ts_ms": 1_704_067_200_000},
            {"symbol": "BBBUSDT", "ts_ms": 1_704_067_200_000},
        ]
    )
    manifest = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "date": "2024-01-01",
                "url": "https://public.bybit.com/trading/AAAUSDT/AAAUSDT2024-01-01.csv.gz",
            }
        ]
    )

    joined = _attach_event_archive_membership(features, manifest).sort("symbol")

    assert joined.select(["symbol", "date", "tradable_membership_flag"]).to_dicts() == [
        {"symbol": "AAAUSDT", "date": "2024-01-01", "tradable_membership_flag": True},
        {"symbol": "BBBUSDT", "date": "2024-01-01", "tradable_membership_flag": False},
    ]


def test_event_decay_exit_fires_at_scenario_threshold() -> None:
    assert _event_decay_exit_hit(
        symbol="AAAUSDT",
        bar_end_ts_ms=2,
        rank_lookup={("AAAUSDT", 2): 0.79},
        threshold=0.80,
    )
    assert not _event_decay_exit_hit(
        symbol="AAAUSDT",
        bar_end_ts_ms=2,
        rank_lookup={("AAAUSDT", 2): 0.80},
        threshold=0.80,
    )


def test_tail_liquidity_jump_requires_pit_membership_flag() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "AAAUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.0,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 100,
                "prior7_liquidity_rank": 130,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": False,
            },
            {
                "symbol": "BBBUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.1,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 100,
                "prior7_liquidity_rank": 130,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    filtered = _event_filter(
        frame,
        "tail_liquidity_jump",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(),
    )

    assert filtered["symbol"].to_list() == ["BBBUSDT"]


def test_creative_event_filters_select_distinct_pit_events() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "ABSUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.0,
                "volume_change_1d_z_rank_frac": 0.95,
                "prior_volume_change_1d_z_rank_frac": 0.40,
                "prior7_volume_persistence_rank_max": 0.25,
                "prior7_abs_daily_return_mean": 0.010,
                "dollar_volume_rank_z": 1.0,
                "dollar_volume_rank_z_rank_frac": 0.70,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 40,
                "prior7_liquidity_rank": 115,
                "daily_return_1d": 0.006,
                "daily_return_rank_frac": 0.55,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "DRYUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 2.8,
                "volume_change_1d_z_rank_frac": 0.92,
                "prior_volume_change_1d_z_rank_frac": 0.30,
                "prior7_volume_persistence_rank_max": 0.20,
                "prior7_abs_daily_return_mean": 0.008,
                "dollar_volume_rank_z": 0.8,
                "dollar_volume_rank_z_rank_frac": 0.65,
                "prior7_dollar_volume_rank_z_rank_frac": 0.15,
                "liquidity_rank": 140,
                "prior7_liquidity_rank": 145,
                "daily_return_1d": 0.025,
                "daily_return_rank_frac": 0.70,
                "turnover_quote": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "MIGUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 0.5,
                "volume_change_1d_z_rank_frac": 0.55,
                "prior_volume_change_1d_z_rank_frac": 0.45,
                "prior7_volume_persistence_rank_max": 0.80,
                "prior7_abs_daily_return_mean": 0.040,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.97,
                "prior7_dollar_volume_rank_z_rank_frac": 0.10,
                "liquidity_rank": 35,
                "prior7_liquidity_rank": 100,
                "daily_return_1d": 0.018,
                "daily_return_rank_frac": 0.62,
                "turnover_quote": 2_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "SELLUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.4,
                "volume_change_1d_z_rank_frac": 0.99,
                "prior_volume_change_1d_z_rank_frac": 0.35,
                "prior7_volume_persistence_rank_max": 0.60,
                "prior7_abs_daily_return_mean": 0.030,
                "dollar_volume_rank_z": 1.2,
                "dollar_volume_rank_z_rank_frac": 0.75,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 90,
                "prior7_liquidity_rank": 95,
                "daily_return_1d": -0.070,
                "daily_return_rank_frac": 0.03,
                "turnover_quote": 1_500_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOISEUSDT",
                "ts_ms": 1,
                "volume_change_1d_z": 3.2,
                "volume_change_1d_z_rank_frac": 0.96,
                "prior_volume_change_1d_z_rank_frac": 0.91,
                "prior7_volume_persistence_rank_max": 0.85,
                "prior7_abs_daily_return_mean": 0.070,
                "dollar_volume_rank_z": 2.8,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.95,
                "liquidity_rank": 120,
                "prior7_liquidity_rank": 130,
                "daily_return_1d": 0.055,
                "daily_return_rank_frac": 0.98,
                "turnover_quote": 1_500_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    config = VolumeEventResearchConfig(
        liquidity_migration_rank_improvement_min=50,
        absorption_max_abs_day_return=0.015,
        dryup_prior_volume_rank_max=0.35,
        dryup_prior_abs_day_return_max=0.02,
        selloff_exhaustion_min_abs_day_return=0.05,
    )

    absorption = _event_filter(
        frame,
        "volume_absorption",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    dryup = _event_filter(
        frame,
        "dryup_reacceleration",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=config,
    )
    selloff = _event_filter(
        frame,
        "selloff_exhaustion",
        score_col="volume_change_1d_z",
        rank_col="volume_change_1d_z_rank_frac",
        top_cut=0.80,
        config=config,
    )

    assert absorption["symbol"].to_list() == ["ABSUSDT"]
    assert dryup["symbol"].to_list() == ["ABSUSDT", "DRYUSDT"]
    assert migration["symbol"].to_list() == ["MIGUSDT"]
    assert selloff["symbol"].to_list() == ["SELLUSDT"]


def test_monthly_returns_are_written_from_baskets() -> None:
    baskets = pl.DataFrame(
        [
            {
                "exit_ts_ms": 1_704_067_200_000,
                "basket_return": 0.10,
                "long_return": 0.10,
                "short_return": 0.0,
                "cost_return": -0.001,
                "funding_return": 0.0,
                "trades": 1,
            },
            {
                "exit_ts_ms": 1_704_153_600_000,
                "basket_return": -0.05,
                "long_return": -0.05,
                "short_return": 0.0,
                "cost_return": -0.001,
                "funding_return": 0.0,
                "trades": 2,
            },
        ]
    )

    monthly = _monthly_returns(baskets)

    assert monthly["month"].to_list() == ["2024-01"]
    assert monthly["strategy_return"].to_list()[0] == pytest.approx(0.045)
    assert monthly["trades"].to_list()[0] == 3
