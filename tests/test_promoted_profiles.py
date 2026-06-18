"""Pin the promoted profile registry (liquidity_migration.promoted).

These assert that the deployed-profile accessors resolve to the exact values the live
sleeves run, so the equity-curve tool (and anyone asking "what's deployed?") can trust
them and a silent drift fails CI. If a profile legitimately changes on deploy, update
its factory AND the expected value here in the same change.

Two promoted-in-code sleeves: LONG and CONTINUOUS. The daily-short sleeve was
ERASED 2026-06-11 (operator order); the continuous-fade sleeve was RE-ADDED
2026-06-15 by explicit operator override, demo/paper only. This module must stay
a registry, not a research-candidate manifest archive.
"""
from __future__ import annotations

from pathlib import Path

from liquidity_migration import promoted


def test_promoted_trading_logic_doc_exists_and_names_lifecycles() -> None:
    path = Path(promoted.PROMOTED_TRADING_LOGIC_DOC)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Continuous Exit Logic" in text
    assert "Long-Native v11a Sleeve" in text
    assert "STRATEGY_PROFILE=continuous_ensemble_v2" in text
    assert "BTC_TREND_GATE=uptrend" in text
    assert "STOP_LOSS_PCT=0" in text
    assert "SIZING_MODE=inverse_vol" in text
    assert "TARGET_VOL_PER_NAME=0.01" in text
    assert "VOL_WEIGHT_CLAMP=2" in text
    assert "w90/tv0.045/max4/ddh=-0.04" in text
    assert "FROZEN_FORWARD_CONFIG" in text
    assert "2026-06-18 Full Live-Config Backtest Receipt" in text
    assert "2026-06-18 Exit-Cause Ablation Receipt" in text
    assert "2026-06-18 InvVol + Max4 Promotion Receipt" in text
    assert "stop_approach" in text
    assert "left_decile" in text
    assert "fc_sniper_retrace_pct" in text


def test_long_profile_is_v11a() -> None:
    cfg = promoted.long_profile()
    assert cfg.universe_size == 50          # div promotion
    assert cfg.fc_min_day_return == 0.15    # canonical v11a


def test_windowing_sets_dates_on_all_sleeves() -> None:
    cfg = promoted.long_profile(start="2024-01-01", end="2025-01-01")
    assert cfg.start_date == "2024-01-01"
    assert cfg.end_date == "2025-01-01"


def test_registry_covers_long_and_continuous() -> None:
    # CONTINUOUS re-added 2026-06-15 by operator override (demo/paper only).
    assert set(promoted.PROFILES) == {"long", "continuous"}


def test_registry_does_not_export_research_candidate_manifests() -> None:
    stale = [
        name
        for name in dir(promoted)
        if name.startswith("CONTINUOUS_") and name.endswith("_CANDIDATE")
    ]
    assert stale == []


def test_continuous_profile_is_deployed_book_with_regime_hedge() -> None:
    cfg = promoted.continuous_profile()
    # the exact deployed object (winner_base ensemble + BTC+ETH 2f hedge)
    assert cfg["object"] == "continuous_winner_uptrend_ensemble_btc_hedged"
    assert cfg["hedge"]["instrument"] == "BTCUSDT"
    assert cfg["hedge"]["instrument2"] == "ETHUSDT"
    assert cfg["entry_sizing"]["mode"] == "inverse_vol"
    assert cfg["entry_sizing"]["target_vol_per_name"] == 0.01
    assert cfg["entry_sizing"]["vol_weight_clamp"] == 2.0
    assert cfg["rebalance"]["target_daily_vol"] == 0.045
    assert cfg["rebalance"]["max_scale"] == 4.0
    assert cfg["rebalance"]["strategy_momentum_window_days"] == 0
    # the BTC-vol regime-hedge overlay is embedded
    assert cfg["hedge"]["regime"]["lam"] == 0.5
    # promoted by operator override -> it IS in PROFILES now
    assert promoted.PROFILES["continuous"] is promoted.continuous_profile
