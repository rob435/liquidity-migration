from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from aggression_carry.ingestion import generate_fixture_data
from aggression_carry.volume_events import (
    _attach_event_archive_membership,
    _event_decay_exit_hit,
    _event_filter,
    _execution_ordered_events,
    _float_or_nan,
    _full_pit_universe_error,
    _stop_pressure_active,
    VolumeEventResearchConfig,
    _add_rank_fraction,
    _monthly_returns,
    _promotion_fields,
    _scenario_side,
    _validate_event_config,
    _write_equity_benchmark_chart,
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


def test_float_or_nan_handles_missing_context_values() -> None:
    assert _float_or_nan(None) != _float_or_nan(None)
    assert _float_or_nan("bad") != _float_or_nan("bad")
    assert _float_or_nan("0.25") == pytest.approx(0.25)


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
    assert "best_equity_chart" in payload


def test_equity_benchmark_chart_writes_overlays_without_annotations(tmp_path: Path) -> None:
    from PIL import Image

    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    equity = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "equity": 1.0, "drawdown": 0.0, "basket_return": 0.0},
            {"ts_ms": 2, "date": "2024-01-02", "equity": 0.9, "drawdown": -0.10, "basket_return": -0.10},
            {"ts_ms": 3, "date": "2024-01-03", "equity": 1.08, "drawdown": 0.0, "basket_return": 0.20},
            {"ts_ms": 4, "date": "2024-01-04", "equity": 1.0, "drawdown": -0.074, "basket_return": -0.074},
            {"ts_ms": 5, "date": "2024-01-05", "equity": 1.14, "drawdown": 0.0, "basket_return": 0.14},
        ]
    )
    raw_klines = pl.DataFrame(
        [
            {"ts_ms": 1, "date": "2024-01-01", "symbol": "BTCUSDT", "close": 100.0},
            {"ts_ms": 2, "date": "2024-01-02", "symbol": "BTCUSDT", "close": 110.0},
            {"ts_ms": 3, "date": "2024-01-03", "symbol": "BTCUSDT", "close": 120.0},
            {"ts_ms": 4, "date": "2024-01-04", "symbol": "BTCUSDT", "close": 115.0},
            {"ts_ms": 5, "date": "2024-01-05", "symbol": "BTCUSDT", "close": 122.0},
        ]
    )

    chart = _write_equity_benchmark_chart(output_dir, root=tmp_path, equity=equity, raw_klines=raw_klines)

    assert Path(chart["png"]).exists()
    assert Path(chart["png"]).name == "volume_event_best_equity_btc.png"
    with Image.open(chart["png"]) as image:
        assert image.size == (1600, 940)
    assert not (output_dir / "volume_event_best_equity_btc_spy.png").exists()
    assert not (output_dir / "volume_event_best_equity_btc_spy.svg").exists()
    assert not (output_dir / "volume_event_best_equity_benchmarks.csv").exists()
    assert not (output_dir / "volume_event_best_equity_annotations.csv").exists()
    assert chart["series"]["strategy"] == 5
    assert chart["series"]["btc"] == 5
    assert "spy" not in chart["series"]
    assert "spy_status" not in chart
    assert chart["annotations"] == []


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


def test_event_filter_excludes_default_stable_and_peg_symbols() -> None:
    features = pl.DataFrame(
        [
            {
                "ts_ms": 1,
                "symbol": "AAAUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 40,
            },
            {
                "ts_ms": 1,
                "symbol": "XRPUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 41,
            },
            {
                "ts_ms": 1,
                "symbol": "USDCUSDT",
                "score": 1.0,
                "score_rank_frac": 0.9,
                "prior_score_rank_frac": 0.1,
                "turnover_quote": 10_000_000.0,
                "liquidity_rank": 42,
            },
        ]
    )

    filtered = _event_filter(
        features,
        "fresh_volume_spike",
        score_col="score",
        rank_col="score_rank_frac",
        top_cut=0.7,
        config=VolumeEventResearchConfig(require_pit_membership=False),
    )

    assert filtered["symbol"].to_list() == ["AAAUSDT", "XRPUSDT"]


def test_volume_event_config_validates_new_research_knobs() -> None:
    with pytest.raises(ValueError, match="entry_delay_hours"):
        _validate_event_config(VolumeEventResearchConfig(entry_delay_hours=-1))

    with pytest.raises(ValueError, match="rank_exit_threshold"):
        _validate_event_config(VolumeEventResearchConfig(rank_exit_threshold=0.0))

    with pytest.raises(ValueError, match="tail_rank_min"):
        _validate_event_config(VolumeEventResearchConfig(tail_rank_min=200, tail_rank_max=100))

    with pytest.raises(ValueError, match="liquidity_migration_rank_improvement_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_rank_improvement_min=-1))

    with pytest.raises(ValueError, match="liquidity_migration_turnover_ratio_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_turnover_ratio_min=-1.0))

    with pytest.raises(ValueError, match="liquidity_migration_prior_rank_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_prior_rank_min=-1))

    with pytest.raises(ValueError, match="liquidity_migration_current_rank_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_current_rank_max=-1))

    with pytest.raises(ValueError, match="liquidity_migration_event_rank_fraction_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_event_rank_fraction_max=1.1))

    with pytest.raises(ValueError, match="liquidity_migration_score_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_score_max=-0.1))
    with pytest.raises(ValueError, match="liquidity_migration_day_return_min"):
        _validate_event_config(
            VolumeEventResearchConfig(liquidity_migration_day_return_min=0.2, liquidity_migration_day_return_max=0.1)
        )
    with pytest.raises(ValueError, match="liquidity_migration_market_pct_up_max"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_market_pct_up_max=1.1))
    with pytest.raises(ValueError, match="liquidity_migration_hot_market_day_return_min"):
        _validate_event_config(VolumeEventResearchConfig(liquidity_migration_hot_market_day_return_min=-0.1))

    with pytest.raises(ValueError, match="market_median_return_1d_min"):
        _validate_event_config(
            VolumeEventResearchConfig(market_median_return_1d_min=0.05, market_median_return_1d_max=0.01)
        )

    with pytest.raises(ValueError, match="market_pct_up_1d_max"):
        _validate_event_config(VolumeEventResearchConfig(market_pct_up_1d_max=1.1))

    with pytest.raises(ValueError, match="btc_return_1d_min"):
        _validate_event_config(VolumeEventResearchConfig(btc_return_1d_min=0.1, btc_return_1d_max=0.0))

    with pytest.raises(ValueError, match="stop_pressure_window_days"):
        _validate_event_config(VolumeEventResearchConfig(stop_pressure_window_days=-1))

    with pytest.raises(ValueError, match="stop_pressure_stop_count"):
        _validate_event_config(VolumeEventResearchConfig(stop_pressure_stop_count=-1))

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


def test_execution_order_prioritizes_strongest_same_timestamp_events() -> None:
    events = pl.DataFrame(
        [
            {"ts_ms": 1, "symbol": "AAAUSDT", "event_rank": 3},
            {"ts_ms": 1, "symbol": "BBBUSDT", "event_rank": 1},
            {"ts_ms": 1, "symbol": "CCCUSDT", "event_rank": 2},
            {"ts_ms": 2, "symbol": "DDDUSDT", "event_rank": 1},
        ]
    )

    ordered = _execution_ordered_events(events)

    assert ordered["symbol"].to_list() == ["BBBUSDT", "CCCUSDT", "AAAUSDT", "DDDUSDT"]


def test_stop_pressure_throttle_uses_only_realized_recent_stops() -> None:
    config = VolumeEventResearchConfig(stop_pressure_window_days=7, stop_pressure_stop_count=2)
    signal_ts_ms = 10 * 24 * 60 * 60 * 1000
    recent_stop = signal_ts_ms - 2 * 24 * 60 * 60 * 1000
    old_stop = signal_ts_ms - 9 * 24 * 60 * 60 * 1000
    future_stop = signal_ts_ms + 1

    assert not _stop_pressure_active([recent_stop, old_stop, future_stop], signal_ts_ms=signal_ts_ms, config=config)
    assert _stop_pressure_active([recent_stop, signal_ts_ms, future_stop], signal_ts_ms=signal_ts_ms, config=config)


def test_selloff_exhaustion_side_hypotheses_are_directional() -> None:
    assert _scenario_side("volume_exhaustion", "continuation") == "long"
    assert _scenario_side("volume_exhaustion", "reversal") == "short"
    assert _scenario_side("selloff_exhaustion", "continuation") == "short"
    assert _scenario_side("selloff_exhaustion", "reversal") == "long"


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
        liquidity_migration_turnover_ratio_min=0.0,
        liquidity_migration_event_rank_fraction_max=0.0,
        liquidity_migration_market_pct_up_max=1.0,
        liquidity_migration_hot_market_day_return_min=10.0,
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


def test_liquidity_migration_quality_controls_require_rank_and_turnover_expansion() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.95,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 50,
                "prior7_liquidity_rank": 220,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOGROWTHUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 45,
                "prior7_liquidity_rank": 210,
                "turnover_quote": 1_500_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "ALREADYLIQUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.2,
                "dollar_volume_rank_z_rank_frac": 0.97,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 30,
                "prior7_liquidity_rank": 110,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "NOTCURRENTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.3,
                "dollar_volume_rank_z_rank_frac": 0.98,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 90,
                "prior7_liquidity_rank": 260,
                "turnover_quote": 4_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.0,
            liquidity_migration_prior_rank_min=150,
            liquidity_migration_current_rank_max=80,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_liquidity_migration_can_require_positive_event_day_return() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.22,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOFLATUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "daily_return_1d": 0.05,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_day_return_min=0.20,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


def test_liquidity_migration_market_gate_allows_hot_coin_exception() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "COOLMKTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.0,
                "dollar_volume_rank_z_rank_frac": 0.86,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 80,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.55,
                "daily_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "BLOWOFFUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.1,
                "dollar_volume_rank_z_rank_frac": 0.87,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 75,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.85,
                "daily_return_1d": 0.18,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "HOTFLATUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 3.2,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 70,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_pct_up_1d": 0.85,
                "daily_return_1d": 0.06,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_market_pct_up_max=0.60,
            liquidity_migration_hot_market_day_return_min=0.15,
        ),
    )

    assert migration["symbol"].to_list() == ["COOLMKTUSDT", "BLOWOFFUSDT"]


def test_liquidity_migration_overheated_and_regime_filters_are_pit_safe() -> None:
    frame = pl.DataFrame(
        [
            {
                "symbol": "GOODUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.8,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 70,
                "prior7_liquidity_rank": 240,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOORANKEDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.7,
                "dollar_volume_rank_z_rank_frac": 0.96,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 65,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOSCOREDUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 2.7,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 60,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.01,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
            {
                "symbol": "TOOHOTUSDT",
                "ts_ms": 1,
                "dollar_volume_rank_z": 1.8,
                "dollar_volume_rank_z_rank_frac": 0.88,
                "prior7_dollar_volume_rank_z_rank_frac": 0.20,
                "liquidity_rank": 55,
                "prior7_liquidity_rank": 230,
                "turnover_quote": 3_000_000.0,
                "prior7_turnover_quote_mean": 1_000_000.0,
                "market_median_return_1d": 0.04,
                "market_pct_up_1d": 0.55,
                "btc_return_1d": 0.02,
                "tradable_membership_flag": True,
            },
        ]
    )

    migration = _event_filter(
        frame,
        "liquidity_migration",
        score_col="dollar_volume_rank_z",
        rank_col="dollar_volume_rank_z_rank_frac",
        top_cut=0.80,
        config=VolumeEventResearchConfig(
            liquidity_migration_rank_improvement_min=100,
            liquidity_migration_turnover_ratio_min=2.0,
            liquidity_migration_event_rank_fraction_max=0.90,
            liquidity_migration_score_max=2.0,
            liquidity_migration_market_pct_up_max=1.0,
            liquidity_migration_hot_market_day_return_min=10.0,
            market_median_return_1d_max=0.03,
            market_pct_up_1d_max=0.70,
            btc_return_1d_max=0.05,
        ),
    )

    assert migration["symbol"].to_list() == ["GOODUSDT"]


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
