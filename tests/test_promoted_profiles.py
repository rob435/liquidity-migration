"""Pin the active profile registry (liquidity_migration.promoted).

These assertions keep the registry narrow: active LONG and CONTINUOUS profile
accessors only, not a research-candidate manifest archive.
"""
from __future__ import annotations

from pathlib import Path

from liquidity_migration import promoted
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig, apply_continuous_demo_profile
from liquidity_migration.long_native_event_demo import _v11a_long_native_config


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
    assert "CTRL_BTC_RISK_70_90_35" in text
    assert "btc_risk_stack_mult" in text
    assert "w90/tv0.045/max4/ddh=-0.04" in text
    assert "FROZEN_FORWARD_CONFIG" in text
    assert "Reconstruction Boundary" in text
    assert "docs/preregistration/INDEX.md" in text
    assert "stop_approach" in text
    assert "left_decile" in text
    assert "fc_sniper_retrace_pct" in text


def test_promoted_trading_logic_doc_matches_resolved_profile_values() -> None:
    text = Path(promoted.PROMOTED_TRADING_LOGIC_DOC).read_text(encoding="utf-8")

    cont = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(strategy_profile="continuous_ensemble_v2", btc_trend_gate="uptrend")
    )
    assert f"- `BTC_TREND_GATE={cont.btc_trend_gate}`." in text
    assert f"- `rmom_quantile={cont.rmom_quantile}`." in text
    assert f'- `feature_set=("{cont.feature_set[0]}",)`.' in text
    assert f"- `liq_turnover_min={int(cont.liq_turnover_min)}`." in text
    assert f"- Max active shorts: {cont.max_active}." in text
    assert f"- Max new entries per cycle: {cont.max_new_entries_per_cycle}." in text
    assert f"- `ENTRY_LEVERAGE={int(cont.entry_leverage)}`." in text
    assert f"- `PER_POSITION_NOTIONAL_PCT_EQUITY={int(cont.per_position_notional_pct_equity)}`." in text
    assert f"- `SIZING_MODE={cont.sizing_mode}`." in text
    assert f"- `TARGET_VOL_PER_NAME={cont.target_vol_per_name}`." in text
    assert f"- `VOL_WEIGHT_CLAMP={int(cont.vol_weight_clamp)}`." in text
    assert f"`[{cont.entry_btc_risk_low:.2f}, {cont.entry_btc_risk_high:.2f})`" in text
    assert f"`btc_risk_stack_mult={cont.entry_btc_risk_tail_mult}`" in text
    assert f"- `max_hold` force cover after {cont.max_hold_hours} hours." in text
    assert f"- `STOP_LOSS_PCT={int(cont.stop_loss_pct)}`; no venue/server disaster stop." in text
    for name, trigger, age_days, take_profit_pct, weight in cont.ensemble_components:
        pct = int(round(take_profit_pct * 100))
        assert f"| `{name}` | `{trigger}` | {age_days}d | {pct}% | {weight} |" in text

    long_cfg = _v11a_long_native_config()
    assert f"- Universe size {long_cfg.universe_size} by trailing {long_cfg.universe_volume_window_days}-day turnover." in text
    assert f"- Minimum listing history {long_cfg.min_listing_history_days} days." in text
    assert f"- `fc_min_day_return={long_cfg.fc_min_day_return}`." in text
    assert f"- `fc_top_volume_rank_max={long_cfg.fc_top_volume_rank_max}`." in text
    assert f"- `fc_min_close_location={long_cfg.fc_min_close_location}`." in text
    assert f"- `fc_max_atr_pct={long_cfg.fc_max_atr_pct}`." in text
    assert f"- `fc_sigma_mult={long_cfg.fc_sigma_mult}`." in text
    assert f"- `fc_sniper_retrace_pct={long_cfg.fc_sniper_retrace_pct}`." in text
    assert f"- `fc_sniper_deadline_hours={long_cfg.fc_sniper_deadline_hours}`." in text
    assert f"- `fc_sniper_skip_on_no_retrace={long_cfg.fc_sniper_skip_on_no_retrace}`." in text
    assert f"- `gross_exposure={long_cfg.gross_exposure}`." in text
    assert f"- `max_concurrent_positions={long_cfg.max_concurrent_positions}`." in text
    assert f"- Max position weight {long_cfg.max_position_weight:.2f}." in text
    assert f"- Weekend multiplier {long_cfg.weekend_size_mult}." in text
    assert f"- Exit cooldown {long_cfg.cooldown_days} days." in text
    assert f"- ATR stop multiple {long_cfg.fc_atr_stop_mult}." in text
    assert f"- ATR take-profit multiple {long_cfg.fc_atr_tp_mult}." in text
    assert f"- Max hold {long_cfg.fc_max_hold_days} days." in text


def test_long_profile_is_v11a() -> None:
    cfg = promoted.long_profile()
    assert cfg.universe_size == 50          # div promotion
    assert cfg.fc_min_day_return == 0.15    # canonical v11a


def test_windowing_sets_dates_on_all_sleeves() -> None:
    cfg = promoted.long_profile(start="2024-01-01", end="2025-01-01")
    assert cfg.start_date == "2024-01-01"
    assert cfg.end_date == "2025-01-01"


def test_registry_covers_long_and_continuous() -> None:
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
    # the exact deployed object (continuous_ensemble_v2 ensemble + BTC+ETH 2f hedge)
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
    assert promoted.PROFILES["continuous"] is promoted.continuous_profile
