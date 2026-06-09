"""Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

These assert that the deployed-profile accessors resolve to the exact values the live
sleeves run, so the equity-curve tool (and anyone asking "what's deployed?") can trust
them and a silent drift fails CI. If a profile legitimately changes on deploy, update
its factory AND the expected value here in the same change.

Two promoted sleeves only: short + long. The continuous-fade sleeve was de-promoted
(2026-06-05, invalidated look-ahead; live sleeve OFF) and removed from the registry.
"""
from __future__ import annotations

from pathlib import Path

from liquidity_migration import promoted


def test_short_profile_is_drop_all_4_age300_ff6() -> None:
    cfg = promoted.short_profile()
    # age300 + ff6 stacked on drop_all_4 (max_active=12, rank ceiling disabled).
    assert cfg.liquidity_migration_pit_age_days_min == 300
    assert cfg.failed_fade_exit_hours == 6
    assert cfg.failed_fade_loss_pct == 0.04
    assert cfg.max_active_symbols == 12
    assert cfg.universe_rank_max == 99999
    assert cfg.liquidity_migration_residual_momentum_max == 10.0  # inactive sentinel; no rmom gate.
    # btc_trend_gate=uptrend (2026-06-04 operator-directed demo deploy): the deployed fade
    # only takes entries when BTC's causal 30d trend is positive (risk-on).
    assert cfg.btc_trend_gate == "uptrend"


def test_long_profile_is_v11a() -> None:
    cfg = promoted.long_profile()
    assert cfg.universe_size == 50          # div promotion
    assert cfg.fc_min_day_return == 0.15    # canonical v11a


def test_windowing_sets_dates_on_all_sleeves() -> None:
    for fn in (promoted.short_profile, promoted.long_profile):
        cfg = fn(start="2024-01-01", end="2025-01-01")
        assert cfg.start_date == "2024-01-01"
        assert cfg.end_date == "2025-01-01"


def test_registry_covers_two_sleeves() -> None:
    assert set(promoted.PROFILES) == {"short", "long"}


def test_continuous_overlay_candidate_is_documented_but_not_promoted() -> None:
    cfg = promoted.CONTINUOUS_OVERLAY_OPERATING_CANDIDATE
    assert cfg.name == "fresh_pop15_pop25_cap22_13_binance_pnl_m50"
    assert cfg.active_overlay_caps == {"bybit": 0.22, "binance": 0.13}
    assert cfg.venue_scales == {"bybit": 1.8, "binance": 5.0}
    assert cfg.binance_active_primary_pnl_gate == -0.50
    assert "not promoted" in cfg.status
    assert Path(cfg.research_note).exists()
    assert "continuous" not in promoted.PROFILES


def test_continuous_performance_frontier_is_not_promoted() -> None:
    cfg = promoted.CONTINUOUS_OVERLAY_HIGHEST_RETURN_CANDIDATE
    assert cfg.name == promoted.CONTINUOUS_OVERLAY_OPERATING_CANDIDATE.name
    assert cfg.active_overlay_caps == {"bybit": 0.22, "binance": 0.13}
    assert cfg.binance_active_primary_pnl_gate == -0.50
    assert Path(cfg.research_note).exists()
    assert "continuous" not in promoted.PROFILES


def test_continuous_standalone_return_candidate_is_not_promoted() -> None:
    cfg = promoted.CONTINUOUS_STANDALONE_RETURN_CANDIDATE
    assert cfg.name == "q25_liq1m_maxret168_btcoff_turn4_pop4_h24"
    assert cfg.params["entry_event_trigger"] == "turn4_pop4"
    assert cfg.params["btc_trend_gate"] == "off"
    assert cfg.params["round_trip_cost_multiplier"] == 1.0
    assert "not promoted" in cfg.status
    assert Path(cfg.research_note).exists()
    assert "continuous" not in promoted.PROFILES


def test_continuous_rebalance_candidate_is_not_promoted() -> None:
    cfg = promoted.CONTINUOUS_REBALANCE_BASE_CANDIDATE
    assert cfg.name == "q25_liq500k_btcup_turn4_pop4_decomp_rebalance_w90_tv25_max4_dd4_trend180_hurdle2"
    assert cfg.portfolio_rule["accounting"] == "decomposed_daily_rebalance"
    assert cfg.portfolio_rule["target_daily_vol"] == 0.025
    assert cfg.portfolio_rule["max_scale"] == 4.0
    assert cfg.portfolio_rule["drawdown_half_threshold"] == -0.04
    assert cfg.portfolio_rule["strategy_momentum_window_days"] == 180
    assert cfg.portfolio_rule["strategy_momentum_min_return"] == 0.02
    assert cfg.portfolio_rule["strategy_momentum_negative_scale"] == 0.0
    # Artifact roots are external research-data pointers (operator data box), not repo
    # files — assert they're wired, not that they exist on whatever host runs the suite
    # (those absolute paths only resolve on the Windows data box).
    assert cfg.base_artifact_root and cfg.cost_2x_artifact_root and cfg.robustness_artifact_root
    assert "not promoted" in cfg.status
    assert Path(cfg.research_note).exists()
    assert "continuous" not in promoted.PROFILES


def test_continuous_merged_rebalance_candidate_is_not_promoted() -> None:
    cfg = promoted.CONTINUOUS_REBALANCE_MERGED_SIGNAL_CANDIDATE
    assert cfg.name == "q25_liq500k_btcup_turn3_pop3_age240_tp10_crowd2_decomp_rebalance_w90_tv25_max4_dd4"
    assert cfg.portfolio_rule["accounting"] == "decomposed_daily_rebalance"
    assert cfg.portfolio_rule["entry_event_trigger"] == "turn3_pop3"
    assert cfg.portfolio_rule["age_days_min"] == 240
    assert cfg.portfolio_rule["take_profit_pct"] == 0.10
    assert cfg.portfolio_rule["entry_crowding_max_fresh"] == 2
    assert cfg.portfolio_rule["strategy_momentum_window_days"] == 0
    # See note above: artifact roots are external data-box pointers, not repo files.
    assert cfg.base_artifact_root and cfg.cost_2x_artifact_root and cfg.robustness_artifact_root
    assert "not promoted" in cfg.status
    assert Path(cfg.research_note).exists()
    assert "continuous" not in promoted.PROFILES
