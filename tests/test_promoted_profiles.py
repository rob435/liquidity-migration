"""Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

These assert that the deployed-profile accessors resolve to the exact values the live
sleeves run, so the equity-curve tool (and anyone asking "what's deployed?") can trust
them and a silent drift fails CI. If a profile legitimately changes on deploy, update
its factory AND the expected value here in the same change.
"""
from __future__ import annotations

from liquidity_migration import promoted


def test_short_profile_is_drop_all_4_age300_ff6() -> None:
    cfg = promoted.short_profile()
    # age300 + ff6 stacked on drop_all_4 (max_active=12, rank ceiling disabled).
    assert cfg.liquidity_migration_pit_age_days_min == 300
    assert cfg.failed_fade_exit_hours == 6
    assert cfg.failed_fade_loss_pct == 0.04
    assert cfg.max_active_symbols == 12
    assert cfg.universe_rank_max == 99999
    # btc_trend_gate=uptrend (2026-06-04 operator-directed demo deploy): the deployed fade
    # only takes entries when BTC's causal 30d trend is positive (risk-on).
    assert cfg.btc_trend_gate == "uptrend"


def test_long_profile_is_v11a() -> None:
    cfg = promoted.long_profile()
    assert cfg.universe_size == 50          # div promotion
    assert cfg.fc_min_day_return == 0.15    # canonical v11a


def test_continuous_profile_matches_live_sleeve() -> None:
    cfg = promoted.continuous_profile()
    assert cfg.side == "short"
    assert cfg.decile == 9
    assert cfg.rmom_quantile == 0.33
    assert cfg.entry_delay_hours == 1       # the validated +1h confirmed entry
    assert cfg.exit_mode == "state"         # cover on leaving the decile
    assert cfg.stop_loss_pct == 0.25        # server-side disaster stop
    assert cfg.liq_turnover_min == 500_000.0


def test_windowing_sets_dates_on_all_sleeves() -> None:
    for fn in (promoted.short_profile, promoted.long_profile, promoted.continuous_profile):
        cfg = fn(start="2024-01-01", end="2025-01-01")
        assert cfg.start_date == "2024-01-01"
        assert cfg.end_date == "2025-01-01"


def test_registry_covers_three_sleeves() -> None:
    assert set(promoted.PROFILES) == {"short", "long", "continuous"}
