"""Tests for B.3 concentration caps in LongNativeConfig + _run_long_pipeline.

We do not require the full kline+feature pipeline to test the cap logic itself;
the caps are simple counters over open_positions plus a sector lookup. We
exercise them by:

  1. Unit-testing `_load_sector_map` directly (valid/invalid/missing JSON).
  2. Running `_run_long_pipeline` with synthetic features that all qualify as
     FOMO_CHASE on the same day, plus synthetic kline bars, and asserting
     that the cap counters bind.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR, date_ms
from liquidity_migration.config import CostConfig
from liquidity_migration.long_native import (
    FUNDING_MODELED_FRACTION_THRESHOLD,
    LongNativeConfig,
    _evaluate_promotion,
    _finalize_trade,
    _load_sector_map,
    _run_label,
    _run_long_pipeline,
)


def test_finalize_trade_notional_multiplier_scales_gross() -> None:
    """H1: notional_multiplier scales per-position gross (mirrors the live
    daemon's --notional-multiplier). Default 1.0 is unchanged; 10x scales gross
    10x so the long sleeve can be validated at the gross it actually deploys."""
    pos = {
        "entry_price": 100.0, "position_weight": 1.0, "symbol": "AAA",
        "entry_ts_ms": 1_000_000, "entry_signal_ts_ms": 1_000_000 - MS_PER_HOUR,
        "basket_id": "b1", "planned_exit_ts_ms": 2_000_000,
        "stop_price": 90.0, "take_profit_price": 130.0, "pattern": "fomo_chase",
    }
    kw = dict(
        exit_ts_ms=2_000_000, exit_price=110.0, reason="take_profit",
        notional_weight=0.2, round_trip_cost_bps=0.0, funding_lookup={},
    )
    base = _finalize_trade(pos, **kw)  # default multiplier 1.0 — historical gross
    scaled = _finalize_trade(pos, **kw, notional_multiplier=10.0)
    # gross_trade_return = 110/100 - 1 = 0.10; effective_weight(1x)=0.2 -> 0.02
    assert base["gross_return"] == pytest.approx(0.02)
    assert base["notional_weight"] == pytest.approx(0.2)
    # 10x -> effective_weight 2.0 -> gross 0.20
    assert scaled["gross_return"] == pytest.approx(0.20)
    assert scaled["gross_return"] == pytest.approx(base["gross_return"] * 10.0)
    # The raw price move is unchanged — only the sizing scales.
    assert scaled["gross_trade_return"] == base["gross_trade_return"]


def _features_row(*, symbol: str, ts_ms: int, day_return: float = 0.20) -> dict:
    """Build a feature row that the FOMO_CHASE pattern accepts."""
    return {
        "symbol": symbol,
        "ts_ms": ts_ms,
        "date": "2025-06-15",
        "in_universe": True,
        "regime_on": True,
        "eth_regime_on": True,
        "log_return": math.log1p(day_return),
        "return_1d": day_return,
        "close_location": 0.95,
        "close": 100.0,
        "today_volume_rank": 3,
        "volume_rank": 5,
        "vol_vs_30d_median": 1.5,
        "coin_30d_return": 0.05,
        "coin_60d_return": 0.05,
        "realized_vol": 0.6,
        "sigma_daily_30d": 0.03,
        "atr_20d": 0.02,
        "atr_14d_pct": 0.02,
        "coin_fc_sma": None,
        "btc_sma_dist": 0.03,
        "btc_above_sma": True,
        "eth_above_50sma": True,
        # multi-day pump features
        "pump_3d_log": math.log1p(day_return),
        "pump_7d_log": math.log1p(day_return),
        "close_loc_3d": 0.9,
        "close_loc_7d": 0.9,
        "intra_max_Nh_pump_log": math.log1p(day_return),
        "p95_pump_90d": math.log1p(0.04),
        "atr_p_quantile_90d": 0.03,
        # pattern-specific flags (used by other detectors but harmless here)
        "is_top_rank": True,
        "btc_high_proximity": 0.5,
        "btc_rv_30": 0.6,
        "own_pump_quantile_90d": 0.05,
        "own_atr_quantile_90d": 0.05,
    }


def _bars_for(symbol: str, *, day_ts_ms: int, entry_price: float = 100.0,
              hours: int = 48) -> dict:
    """Build a minimal bars_by_symbol entry for a single symbol that has a bar
    starting at day_ts_ms+1h (entry_delay_hours=1) and stays flat after that.
    """
    bar_ends = [day_ts_ms + (i + 1) * MS_PER_HOUR for i in range(hours)]
    ends_arr = np.asarray(bar_ends, dtype=np.int64)
    closes = np.full(hours, entry_price, dtype=np.float64)
    return {
        "ends": bar_ends,
        "by_end": {int(e): i for i, e in enumerate(bar_ends)},
        "bar_end_ts_ms": ends_arr,
        "open": closes.copy(),
        "high": closes * 1.005,
        "low": closes * 0.995,
        "close": closes,
    }


def test_load_sector_map_returns_empty_when_path_none() -> None:
    assert _load_sector_map(None) == {}


def test_load_sector_map_returns_empty_when_path_empty() -> None:
    assert _load_sector_map("") == {}


def test_load_sector_map_reads_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text(json.dumps({"WIFUSDT": "meme", "ETHUSDT": "core_l1"}))
    out = _load_sector_map(str(p))
    assert out == {"WIFUSDT": "meme", "ETHUSDT": "core_l1"}


def test_load_sector_map_uppercases_keys(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text(json.dumps({"wifusdt": "meme"}))
    assert _load_sector_map(str(p)) == {"WIFUSDT": "meme"}


def test_load_sector_map_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        _load_sector_map(str(tmp_path / "nope.json"))


def test_load_sector_map_raises_on_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text("{not valid json}")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        _load_sector_map(str(p))


def test_load_sector_map_raises_on_non_object_json(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text(json.dumps(["WIFUSDT", "meme"]))
    with pytest.raises(RuntimeError, match="JSON object"):
        _load_sector_map(str(p))


def test_load_sector_map_raises_on_non_string_values(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text(json.dumps({"WIFUSDT": 1}))
    with pytest.raises(RuntimeError, match="strings"):
        _load_sector_map(str(p))


def test_default_sector_map_loads_and_includes_expected_keys() -> None:
    cfg_path = Path(__file__).resolve().parent.parent / "configs" / "sector_map.json"
    out = _load_sector_map(str(cfg_path))
    assert out["WIFUSDT"] == "meme"
    assert out["ETHUSDT"] == "core_l1"
    assert out["1000PEPEUSDT"] == "meme"
    # at least 3 meme coins in the default map so the cap-of-2 test below is meaningful
    meme_count = sum(1 for v in out.values() if v == "meme")
    assert meme_count >= 3


def test_sector_cap_blocks_third_meme_when_cap_is_two(tmp_path: Path) -> None:
    sector_path = tmp_path / "sectors.json"
    sector_path.write_text(json.dumps({
        "WIFUSDT": "meme",
        "PEPEUSDT": "meme",
        "DOGEUSDT": "meme",
        "ETHUSDT": "core_l1",
    }))
    # Bypass the FOMO_CHASE detection complexity by enabling all three patterns
    # with very loose thresholds — use the existing FC trigger which only needs
    # log_return >= log1p(fc_min_day_return).
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,  # any 5% day qualifies
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=5,
        max_per_sector_concurrent=2,
        max_per_symbol_concurrent=1,
        sector_map_path=str(sector_path),
        cooldown_days=0,
        gross_exposure=1.0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    symbols = ["WIFUSDT", "PEPEUSDT", "DOGEUSDT", "ETHUSDT"]
    rows = [_features_row(symbol=s, ts_ms=day_ts, day_return=0.20) for s in symbols]
    features = pl.DataFrame(rows)
    bars = {s: _bars_for(s, day_ts_ms=day_ts) for s in symbols}
    trades, stats, events = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    # Of WIF/PEPE/DOGE the 3rd meme attempt must be sector-capped. ETH is core_l1
    # and is allowed.
    assert stats["skipped_sector_cap"] >= 1, (
        f"expected at least one skipped_sector_cap, got stats={stats}"
    )


def test_no_sector_cap_when_disabled(tmp_path: Path) -> None:
    sector_path = tmp_path / "sectors.json"
    sector_path.write_text(json.dumps({"WIFUSDT": "meme", "PEPEUSDT": "meme"}))
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=5,
        max_per_sector_concurrent=0,  # disabled
        sector_map_path=str(sector_path),
        cooldown_days=0,
        gross_exposure=1.0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    rows = [
        _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20),
        _features_row(symbol="PEPEUSDT", ts_ms=day_ts, day_return=0.20),
    ]
    features = pl.DataFrame(rows)
    bars = {s: _bars_for(s, day_ts_ms=day_ts) for s in ("WIFUSDT", "PEPEUSDT")}
    _trades, stats, _events = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert stats["skipped_sector_cap"] == 0


def test_max_per_symbol_weight_caps_position_weight(tmp_path: Path) -> None:
    """A high gross_exposure with few concurrent slots produces large
    per-position weight; max_per_symbol_weight clamps it."""
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=2,
        gross_exposure=4.0,  # 2.0 per slot
        max_per_symbol_weight=0.10,  # clamp at 10% of gross
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    rows = [_features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)]
    features = pl.DataFrame(rows)
    bars = {"WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)}
    trades, _stats, _events = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    if trades.is_empty():
        pytest.skip("FOMO_CHASE entry was filtered by another gate; cap test inapplicable")
    notional_weight = cfg.gross_exposure / cfg.max_concurrent_positions  # 2.0
    pos_weight = float(trades["position_weight"][0])
    effective = notional_weight * pos_weight
    assert effective <= cfg.max_per_symbol_weight + 1e-9, (
        f"effective gross share {effective} exceeds cap {cfg.max_per_symbol_weight}"
    )


def test_max_per_symbol_weight_disabled_passes_through() -> None:
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=2,
        gross_exposure=4.0,
        max_per_symbol_weight=0.0,  # disabled
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    rows = [_features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)]
    features = pl.DataFrame(rows)
    bars = {"WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)}
    trades, _stats, _events = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    # No assertion about effective_gross; the disabled flag should simply not crash.
    # If an entry fired, position_weight should be a finite > 0 value.
    if not trades.is_empty():
        pw = float(trades["position_weight"][0])
        assert math.isfinite(pw) and pw > 0


def test_equal_sizing_uses_equal_position_weight() -> None:
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=1,
        gross_exposure=1.0,
        sizing="equal",
        max_position_weight=0.01,
        max_per_symbol_weight=0.0,
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    row = _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
    row["realized_vol"] = 10.0
    trades, _stats, _events = _run_long_pipeline(
        features=pl.DataFrame([row]),
        bars_by_symbol={"WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert not trades.is_empty()
    assert float(trades["position_weight"][0]) == pytest.approx(1.0)


def test_symbol_weight_cap_binds_after_vol_target_scale() -> None:
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        max_concurrent_positions=1,
        gross_exposure=1.0,
        sizing="equal",
        enable_vol_target=True,
        vol_target_annual=1.0,
        vol_target_min_scale=0.1,
        vol_target_max_scale=10.0,
        max_per_symbol_weight=0.10,
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    day_ts = 1_700_000_000_000
    row = _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
    row["btc_rv_30"] = 0.10
    trades, _stats, _events = _run_long_pipeline(
        features=pl.DataFrame([row]),
        bars_by_symbol={"WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert not trades.is_empty()
    effective_gross = (cfg.gross_exposure / cfg.max_concurrent_positions) * float(trades["position_weight"][0])
    assert effective_gross == pytest.approx(cfg.max_per_symbol_weight)


def test_sniper_fallthrough_skips_when_deadline_bar_missing() -> None:
    signal_ts = 1_700_000_000_000 + MS_PER_HOUR
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        fc_use_sniper_entry=True,
        fc_sniper_retrace_pct=0.50,
        fc_sniper_deadline_hours=6,
        fc_sniper_skip_on_no_retrace=False,
        max_concurrent_positions=1,
        max_per_symbol_weight=0.0,
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    bars = _bars_for("WIFUSDT", day_ts_ms=signal_ts - MS_PER_HOUR, hours=6)
    trades, stats, _events = _run_long_pipeline(
        features=pl.DataFrame([_features_row(symbol="WIFUSDT", ts_ms=signal_ts, day_return=0.20)]),
        bars_by_symbol={"WIFUSDT": bars},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert trades.is_empty()
    assert stats["skipped_no_entry_bar"] == 1


def test_final_exit_scan_catches_stop_when_no_later_feature_rows() -> None:
    day_ts = 1_700_000_000_000
    cfg = LongNativeConfig(
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        fc_stop_pct=0.05,
        fc_take_profit_pct=10.0,
        max_concurrent_positions=1,
        max_per_symbol_weight=0.0,
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    bars = _bars_for("WIFUSDT", day_ts_ms=day_ts, hours=12)
    bars["low"][2] = 94.0
    trades, stats, _events = _run_long_pipeline(
        features=pl.DataFrame([_features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)]),
        bars_by_symbol={"WIFUSDT": bars},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert not trades.is_empty()
    assert trades["exit_reason"][0] == "stop_loss"
    assert int(trades["exit_ts_ms"][0]) == int(bars["bar_end_ts_ms"][2])
    assert stats["exits_stop"] == 1


def test_windowed_force_close_uses_configured_end_date() -> None:
    start = "2025-01-01"
    end = "2025-01-02"
    day_ts = date_ms(start)
    cfg = LongNativeConfig(
        start_date=start,
        end_date=end,
        enable_capitulation_rebound=False,
        enable_volume_resurrection=False,
        enable_funding_squeeze=False,
        enable_fomo_chase=True,
        fc_min_day_return=0.05,
        fc_eth_regime_required=False,
        fc_btc_regime_required=False,
        fc_min_close_location=0.0,
        fc_top_volume_rank_max=10,
        fc_max_atr_pct=1.0,
        fc_use_sigma_threshold=False,
        fc_stop_pct=0.50,
        fc_take_profit_pct=10.0,
        max_concurrent_positions=1,
        max_per_symbol_weight=0.0,
        max_per_sector_concurrent=0,
        cooldown_days=0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )
    bars = _bars_for("WIFUSDT", day_ts_ms=day_ts, hours=48)
    end_idx = bars["by_end"][date_ms(end)]
    bars["close"][end_idx] = 110.0
    bars["high"][end_idx] = 111.0
    bars["low"][end_idx] = 109.0
    bars["close"][end_idx + 1:] = 200.0
    bars["high"][end_idx + 1:] = 201.0
    bars["low"][end_idx + 1:] = 199.0
    trades, _stats, _events = _run_long_pipeline(
        features=pl.DataFrame([_features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)]),
        bars_by_symbol={"WIFUSDT": bars},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
    )
    assert not trades.is_empty()
    assert int(trades["exit_ts_ms"][0]) == date_ms(end)
    assert float(trades["exit_price"][0]) == pytest.approx(110.0)
    assert float(trades["gross_trade_return"][0]) < 0.20


def test_evaluate_promotion_reports_avg_split_sharpe() -> None:
    promo = _evaluate_promotion(
        splits=[
            {"basket_count": 2, "total_return": 0.10, "sharpe_like": 1.0},
            {"basket_count": 0, "total_return": 0.0, "sharpe_like": 99.0},
            {"basket_count": 3, "total_return": 0.20, "sharpe_like": 2.0},
        ],
        summary={"sharpe_like": 3.0, "max_drawdown": -0.10},
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["avg_split_sharpe"] == pytest.approx(1.5)


def test_run_long_native_research_precomputed_inputs_are_equivalent(tmp_path) -> None:
    """LON-6: the sweep hoist (precomputed_inputs) must yield a result identical to the
    default build-internally path — the read + feature panel are entry-param-independent,
    so reusing them across sweep cells is provably equivalent (the gate the verifier required)."""
    from liquidity_migration.ingestion import generate_fixture_data
    from liquidity_migration.long_native import build_long_research_inputs, run_long_native_research
    from liquidity_migration.long_native_event_demo import _v11a_long_native_config

    generate_fixture_data(tmp_path)
    cfg = dataclasses.replace(_v11a_long_native_config(), require_full_pit_universe=False)

    default = run_long_native_research(tmp_path, config=cfg, report_dir=tmp_path / "default")
    inputs = build_long_research_inputs(tmp_path, config=cfg)
    precomp = run_long_native_research(
        tmp_path, config=cfg, report_dir=tmp_path / "precomp", precomputed_inputs=inputs,
    )
    assert default["summary"] == precomp["summary"]
    assert default["rows"] == precomp["rows"]
    assert default["splits"] == precomp["splits"]
    assert default["run_label"] == precomp["run_label"]


# ============================================================================
# Relocated from tests/test_audit_fix_b09.py (audit bucket b09, long-sleeve-5).
# The weekend tilt's two backtest sites must route through the shared helper.
# ============================================================================


def test_long_native_weekend_tilt_uses_shared_helper() -> None:
    import liquidity_migration.long_native as ln

    src = inspect.getsource(ln)
    assert "is_weekend_ms" in src
    # Neither backtest site may keep the inline weekday formula.
    assert "((int(trig_ts) // MS_PER_DAY) + 3) % 7 >= 5" not in src
    assert "((int(entry_ts_ms) // MS_PER_DAY) + 3) % 7 >= 5" not in src


# --------------------------------------------------------------------------- #
# cost-funding-3: funding-coverage gate-tightening
# (relocated from tests/test_audit_int_iG.py)
# --------------------------------------------------------------------------- #

def _passing_summary(**overrides):
    """A summary that clears every OTHER gate (Sharpe, DD) so the funding-coverage
    check is the only thing under test."""
    base = {"sharpe_like": 1.5, "max_drawdown": -0.10, "funding_modeled_fraction": 1.0}
    base.update(overrides)
    return base


def test_promotion_fails_when_funding_coverage_below_threshold() -> None:
    """A 'partial' book where a large slice of notional was charged ZERO funding
    (fraction below threshold) must NOT pass the promotion gate."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.50),
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is False
    assert "funding_coverage_below_threshold" in promo["promotion_reasons"]
    # Distinct from the all-missing case: do not double-report funding_missing.
    assert "funding_missing" not in promo["promotion_reasons"]
    assert promo["funding_modeled_fraction"] == pytest.approx(0.50)
    assert promo["funding_coverage_threshold"] == pytest.approx(FUNDING_MODELED_FRACTION_THRESHOLD)


def test_promotion_passes_at_coverage_edge() -> None:
    """A coverage-edge 'partial' (one funding-free alt, fraction >= threshold) is
    still acceptable and must pass."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.97),
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is True
    assert "funding_coverage_below_threshold" not in promo["promotion_reasons"]


def test_promotion_all_missing_still_fails_as_funding_missing() -> None:
    """The pre-existing all-missing failure path is preserved and reported as
    funding_missing (not the new coverage reason)."""
    promo = _evaluate_promotion(
        splits=[],
        summary=_passing_summary(funding_modeled_fraction=0.0),
        funding_mode="missing",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is False
    assert "funding_missing" in promo["promotion_reasons"]
    assert "funding_coverage_below_threshold" not in promo["promotion_reasons"]


def test_promotion_absent_fraction_defaults_to_full_coverage() -> None:
    """Backward-compat: an older summary WITHOUT funding_modeled_fraction must not
    invent a new failure (defaults to 1.0 == full coverage)."""
    promo = _evaluate_promotion(
        splits=[],
        summary={"sharpe_like": 1.5, "max_drawdown": -0.10},  # no fraction key
        funding_mode="partial",
        full_pit_universe_pass=True,
    )
    assert promo["promotion_gate_pass"] is True
    assert promo["funding_modeled_fraction"] == pytest.approx(1.0)


def test_run_label_down_labels_low_coverage_partial() -> None:
    """The single 'partial' label is split: a low-coverage partial down-labels to a
    distinct run label so an auditor can tell it apart from a coverage-edge partial."""
    low = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
        funding_modeled_fraction=0.50,
    )
    assert low == "full_pit_universe_funding_coverage_low"

    edge = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
        funding_modeled_fraction=0.99,
    )
    assert edge == "full_pit_universe_funding_partial"


def test_run_label_partial_default_fraction_is_backward_compatible() -> None:
    """Callers that do not pass funding_modeled_fraction keep the prior partial label
    (default 1.0 == coverage OK)."""
    label = _run_label(
        full_pit_universe_pass=True,
        funding_mode="partial",
        archive_manifest_empty=False,
    )
    assert label == "full_pit_universe_funding_partial"


# --------------------------------------------------------------------------- #
# pit-data-1: the inert require_pit_membership flag is fully removed
# (relocated from tests/test_audit_int_iG.py)
# --------------------------------------------------------------------------- #

def test_require_pit_membership_field_is_removed() -> None:
    field_names = {f.name for f in dataclasses.fields(LongNativeConfig)}
    assert "require_pit_membership" not in field_names
    # The live universe-completeness gate remains.
    assert "require_full_pit_universe" in field_names


def test_long_native_config_rejects_removed_flag() -> None:
    with pytest.raises(TypeError):
        LongNativeConfig(require_pit_membership=False)  # type: ignore[call-arg]


def test_require_full_pit_universe_still_constructs() -> None:
    cfg = LongNativeConfig(require_full_pit_universe=False)
    assert cfg.require_full_pit_universe is False
    cfg_default = LongNativeConfig()
    assert cfg_default.require_full_pit_universe is True
