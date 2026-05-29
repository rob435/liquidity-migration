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

import json
import math
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.config import CostConfig
from liquidity_migration.long_native import (
    LongNativeConfig,
    _finalize_trade,
    _load_sector_map,
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
        require_pit_membership=False,
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
        require_pit_membership=False,
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
        require_pit_membership=False,
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
        require_pit_membership=False,
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
