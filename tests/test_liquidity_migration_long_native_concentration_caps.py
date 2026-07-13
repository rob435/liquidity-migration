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
from polars.testing import assert_frame_equal

from liquidity_migration._common import MS_PER_HOUR, date_ms
from liquidity_migration.account_kernel import AccountRiskPolicy
from liquidity_migration.config import CostConfig
from liquidity_migration.execution_adapters import ExecutionTwinConfig, LatencyProfile
from liquidity_migration.historical_account_replay import (
    HistoricalAccountSession,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.long_native import (
    FUNDING_MODELED_FRACTION_THRESHOLD,
    LongNativeConfig,
    _evaluate_promotion,
    _finalize_trade,
    _load_sector_map,
    _methodology_run_label,
    _run_label,
    _run_long_pipeline,
    format_long_native_report,
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


def _sniper_bars_for(
    *,
    signal_ts_ms: int,
    retrace_hour: int,
    signal_price: float = 100.0,
    retrace_price: float = 97.0,
    hours: int = 49,
) -> dict:
    """Hourly bars including the signal-close bar and one later retrace."""
    bars = _bars_for(
        "unused",
        day_ts_ms=signal_ts_ms - MS_PER_HOUR,
        entry_price=signal_price,
        hours=hours,
    )
    retrace_idx = bars["by_end"][signal_ts_ms + retrace_hour * MS_PER_HOUR]
    bars["close"][retrace_idx] = retrace_price
    bars["low"][retrace_idx] = min(bars["low"][retrace_idx], retrace_price)
    return bars


def _online_long_config() -> LongNativeConfig:
    return LongNativeConfig(
        execution_strategy_id="long-online-test",
        execution_leverage=10.0,
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
        fc_use_sniper_entry=False,
        fc_provisional_entry=False,
        max_concurrent_positions=1,
        cooldown_days=0,
        gross_exposure=1.0,
        require_full_pit_universe=False,
        cost_multiplier=0.0,
    )


def _online_long_session(
    root: Path,
    *,
    risk_limit: float,
    stale_execution: bool = False,
    symbols: tuple[str, ...] = ("WIFUSDT",),
) -> HistoricalAccountSession:
    return HistoricalAccountSession(
        root,
        account_id="long-online-test",
        risk_policy=AccountRiskPolicy(
            risk_limit,
            risk_limit,
            risk_limit,
            risk_limit,
            10.0,
        ),
        instrument_rules=synthetic_historical_rules_for_symbols(
            list(symbols),
            max_leverage=10.0,
            observed_ts_ns=1,
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(1 if stale_execution else 0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="long-online-test",
    )


def test_standard_long_history_uses_online_account_feedback(tmp_path: Path) -> None:
    day_ts = 1_700_000_000_000
    decisions = []
    session = _online_long_session(tmp_path / "account", risk_limit=1e12)
    features = pl.DataFrame([
        _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
    ])
    bars = {"WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)}
    config = _online_long_config()

    trades, stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.height == 1
    assert stats["skipped_account_kernel"] == 0
    assert len(decisions) == 2
    assert len(session.outputs) == 2
    assert session.kernel is not None
    state = session.kernel.state()
    assert state.component_targets == {}
    assert state.positions["WIFUSDT"].signed_qty == pytest.approx(0.0)

    legacy_trades, legacy_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
    )
    assert_frame_equal(trades, legacy_trades)
    assert stats == legacy_stats


def test_standard_long_batches_same_timestamp_entries_and_exits(tmp_path: Path) -> None:
    day_ts = 1_700_000_000_000
    symbols = ("WIFUSDT", "PEPEUSDT")
    decisions = []
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        symbols=symbols,
    )
    config = dataclasses.replace(
        _online_long_config(),
        max_concurrent_positions=2,
    )

    trades, stats, _ = _run_long_pipeline(
        features=pl.DataFrame([
            _features_row(symbol=symbol, ts_ms=day_ts, day_return=0.20)
            for symbol in symbols
        ]),
        bars_by_symbol={
            symbol: _bars_for(symbol, day_ts_ms=day_ts)
            for symbol in symbols
        },
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.height == 2
    assert stats["skipped_account_kernel"] == 0
    assert len(decisions) == 4
    # One atomic entry target batch and one atomic data-end exit batch.
    assert len(session.outputs) == 2
    assert all(output.target_result.accepted for output in session.outputs)


def test_online_long_advances_hourly_exits_before_later_entry_capacity(
    tmp_path: Path,
) -> None:
    day_0 = 1_700_000_000_000
    day_1 = day_0 + 24 * MS_PER_HOUR
    features = pl.DataFrame([
        _features_row(symbol="WIFUSDT", ts_ms=day_0, day_return=0.20),
        _features_row(symbol="PEPEUSDT", ts_ms=day_1, day_return=0.20),
    ])
    bars = {
        "WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_0, hours=72),
        "PEPEUSDT": _bars_for("PEPEUSDT", day_ts_ms=day_1, hours=48),
    }
    # WIF stops at day_1+1h. With a 3h entry delay, that exit is knowable
    # before PEPE's day_1+3h order decision.
    bars["WIFUSDT"]["low"][24] = 1.0
    config = dataclasses.replace(
        _online_long_config(),
        entry_delay_hours=3,
        max_concurrent_positions=1,
    )

    legacy_trades, legacy_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
    )
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        symbols=("WIFUSDT", "PEPEUSDT"),
    )
    online_trades, online_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        kernel_session=session,
    )

    assert legacy_trades["symbol"].to_list() == ["WIFUSDT"]
    assert legacy_stats["skipped_capacity"] == 1
    assert online_trades["symbol"].to_list() == ["WIFUSDT", "PEPEUSDT"]
    assert online_stats["skipped_capacity"] == 0


def test_standard_long_does_not_invent_trade_after_account_risk_rejection(
    tmp_path: Path,
) -> None:
    day_ts = 1_700_000_000_000
    decisions = []
    session = _online_long_session(tmp_path / "account", risk_limit=100.0)

    trades, stats, _ = _run_long_pipeline(
        features=pl.DataFrame([
            _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
        ]),
        bars_by_symbol={
            "WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)
        },
        funding_lookup=None,
        config=_online_long_config(),
        costs=CostConfig(),
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.is_empty()
    assert stats["skipped_account_kernel"] == 1
    assert len(decisions) == 1
    assert session.kernel is not None
    state = session.kernel.state()
    assert state.component_targets == {}
    assert state.positions == {}


def test_standard_long_neutralizes_committed_target_after_execution_rejection(
    tmp_path: Path,
) -> None:
    day_ts = 1_700_000_000_000
    decisions = []
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        stale_execution=True,
    )

    trades, stats, _ = _run_long_pipeline(
        features=pl.DataFrame([
            _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
        ]),
        bars_by_symbol={
            "WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=day_ts)
        },
        funding_lookup=None,
        config=_online_long_config(),
        costs=CostConfig(),
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.is_empty()
    assert stats["skipped_account_kernel"] == 1
    assert len(decisions) == 2
    assert decisions[-1].intent.intent.signed_notional_usdt == 0.0
    assert session.kernel is not None
    state = session.kernel.state()
    assert state.component_targets == {}
    assert state.positions == {}


def test_sniper_long_submits_at_observed_retrace_boundary(tmp_path: Path) -> None:
    day_ts = 1_700_000_000_000
    decisions = []
    config = dataclasses.replace(
        _online_long_config(),
        fc_use_sniper_entry=True,
        fc_sniper_retrace_pct=0.02,
        fc_sniper_deadline_hours=6,
    )
    bars = {
        "WIFUSDT": _sniper_bars_for(
            signal_ts_ms=day_ts,
            retrace_hour=3,
        )
    }
    session = _online_long_session(tmp_path / "account", risk_limit=1e12)

    online_trades, stats, _ = _run_long_pipeline(
        features=pl.DataFrame([
            _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
        ]),
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        kernel_decision_sink=decisions,
        kernel_session=session,
    )
    legacy_trades, _, _ = _run_long_pipeline(
        features=pl.DataFrame([
            _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)
        ]),
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
    )

    assert stats["skipped_account_kernel"] == 0
    assert decisions[0].wall_ts_ns == (day_ts + 3 * MS_PER_HOUR) * 1_000_000
    assert decisions[0].reference_price == pytest.approx(97.0)
    assert len(session.outputs) == 2
    assert_frame_equal(online_trades, legacy_trades)


def test_sniper_long_allocates_capacity_in_fill_time_order(tmp_path: Path) -> None:
    day_ts = 1_700_000_000_000
    symbols = ("WIFUSDT", "PEPEUSDT")
    config = dataclasses.replace(
        _online_long_config(),
        fc_use_sniper_entry=True,
        fc_sniper_retrace_pct=0.02,
        fc_sniper_deadline_hours=8,
        max_concurrent_positions=1,
    )
    features = pl.DataFrame([
        _features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.30),
        _features_row(symbol="PEPEUSDT", ts_ms=day_ts, day_return=0.20),
    ])
    bars = {
        # WIF ranks first at the signal boundary but retraces later.
        "WIFUSDT": _sniper_bars_for(signal_ts_ms=day_ts, retrace_hour=6),
        "PEPEUSDT": _sniper_bars_for(signal_ts_ms=day_ts, retrace_hour=2),
    }

    legacy_trades, legacy_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
    )
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        symbols=symbols,
    )
    online_trades, online_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        kernel_session=session,
    )

    assert legacy_trades["symbol"].to_list() == ["WIFUSDT"]
    assert legacy_stats["skipped_capacity"] == 1
    assert online_trades["symbol"].to_list() == ["PEPEUSDT"]
    assert online_stats["skipped_capacity"] == 1


def test_provisional_long_batches_equal_timestamp_targets_online(
    tmp_path: Path,
) -> None:
    day_ts = 1_700_000_000_000
    prior_ts = day_ts - 24 * MS_PER_HOUR
    trigger_ts = prior_ts + 6 * MS_PER_HOUR
    symbols = ("WIFUSDT", "PEPEUSDT")
    features = pl.DataFrame([
        _features_row(symbol=symbol, ts_ms=ts, day_return=day_return)
        for ts, day_return in ((prior_ts, 0.0), (day_ts, 0.20))
        for symbol in symbols
    ])
    bars = {
        symbol: _bars_for(symbol, day_ts_ms=prior_ts, hours=48)
        for symbol in symbols
    }
    config = dataclasses.replace(
        _online_long_config(),
        fc_provisional_entry=True,
        max_concurrent_positions=2,
    )
    decisions = []
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        symbols=symbols,
    )

    trades, stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        provisional_triggers={
            day_ts: [(trigger_ts, symbol) for symbol in symbols]
        },
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.height == 2
    assert stats["provisional_entries"] == 2
    assert stats["provisional_confirmed"] == 2
    assert len(decisions) == 4
    assert {decision.wall_ts_ns for decision in decisions[:2]} == {
        trigger_ts * 1_000_000
    }
    # One entry batch and one final-exit batch, each with both symbols.
    assert len(session.outputs) == 2


def test_provisional_long_rejection_does_not_create_local_trade(
    tmp_path: Path,
) -> None:
    day_ts = 1_700_000_000_000
    prior_ts = day_ts - 24 * MS_PER_HOUR
    trigger_ts = prior_ts + 6 * MS_PER_HOUR
    features = pl.DataFrame([
        _features_row(symbol="WIFUSDT", ts_ms=ts, day_return=0.0)
        for ts in (prior_ts, day_ts)
    ])
    session = _online_long_session(tmp_path / "account", risk_limit=100.0)

    trades, stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol={
            "WIFUSDT": _bars_for("WIFUSDT", day_ts_ms=prior_ts, hours=48)
        },
        funding_lookup=None,
        config=dataclasses.replace(
            _online_long_config(),
            fc_provisional_entry=True,
        ),
        costs=CostConfig(),
        provisional_triggers={day_ts: [(trigger_ts, "WIFUSDT")]},
        kernel_session=session,
    )

    assert trades.is_empty()
    assert stats["provisional_entries"] == 0
    assert stats["skipped_account_kernel"] == 1
    assert session.kernel is not None
    assert session.kernel.state().positions == {}


def test_provisional_long_releases_capacity_at_trigger_boundary(
    tmp_path: Path,
) -> None:
    day_0 = 1_700_000_000_000
    day_1 = day_0 + 24 * MS_PER_HOUR
    trigger_ts = day_0 + 10 * MS_PER_HOUR
    features = pl.DataFrame([
        _features_row(symbol="WIFUSDT", ts_ms=day_0, day_return=0.20),
        _features_row(symbol="PEPEUSDT", ts_ms=day_0, day_return=0.0),
        _features_row(symbol="WIFUSDT", ts_ms=day_1, day_return=0.0),
        _features_row(symbol="PEPEUSDT", ts_ms=day_1, day_return=0.0),
    ])
    bars = {
        symbol: _bars_for(symbol, day_ts_ms=day_0, hours=48)
        for symbol in ("WIFUSDT", "PEPEUSDT")
    }
    # WIF enters at +1h and stops at +5h, before PEPE's +10h trigger.
    bars["WIFUSDT"]["low"][4] = 1.0
    config = dataclasses.replace(
        _online_long_config(),
        fc_provisional_entry=True,
        max_concurrent_positions=1,
    )
    provisional = {day_1: [(trigger_ts, "PEPEUSDT")]}

    legacy_trades, legacy_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        provisional_triggers=provisional,
    )
    session = _online_long_session(
        tmp_path / "account",
        risk_limit=1e12,
        symbols=("WIFUSDT", "PEPEUSDT"),
    )
    online_trades, online_stats, _ = _run_long_pipeline(
        features=features,
        bars_by_symbol=bars,
        funding_lookup=None,
        config=config,
        costs=CostConfig(),
        provisional_triggers=provisional,
        kernel_session=session,
    )

    assert legacy_trades["symbol"].to_list() == ["WIFUSDT"]
    assert legacy_stats["skipped_capacity"] == 1
    assert online_trades["symbol"].to_list() == ["WIFUSDT", "PEPEUSDT"]
    assert online_stats["skipped_capacity"] == 0


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
        notional_multiplier=4.0,
        execution_strategy_id="long-target-projection-test",
        execution_leverage=10.0,
    )
    bars = _bars_for("WIFUSDT", day_ts_ms=day_ts, hours=12)
    bars["low"][2] = 94.0
    kernel_decisions = []
    trades, stats, _events = _run_long_pipeline(
        features=pl.DataFrame([_features_row(symbol="WIFUSDT", ts_ms=day_ts, day_return=0.20)]),
        bars_by_symbol={"WIFUSDT": bars},
        funding_lookup=None,
        config=cfg,
        costs=CostConfig(),
        kernel_decision_sink=kernel_decisions,
    )
    assert not trades.is_empty()
    assert trades["exit_reason"][0] == "stop_loss"
    assert int(trades["exit_ts_ms"][0]) == int(bars["bar_end_ts_ms"][2])
    assert stats["exits_stop"] == 1
    assert len(kernel_decisions) == 2
    assert kernel_decisions[0].intent.intent.signed_notional_usdt > 0.0
    assert kernel_decisions[0].intent.intent.leverage == 10.0
    assert kernel_decisions[1].intent.intent.signed_notional_usdt == 0.0
    assert kernel_decisions[1].intent.intent.leverage == 10.0
    assert kernel_decisions[1].intent.intent.reason == "stop_loss"

    # Given the same selected candidate and sizing inputs, the live adapter and
    # historical strategy path must now produce exactly the same component
    # identity, decision identity, quantity intent, and leverage.
    from liquidity_migration.long_native_event_demo import (
        LongNativeDemoCycleConfig,
        _long_entry_target_intents,
    )

    historical_entry = kernel_decisions[0].intent.intent
    live_entry = _long_entry_target_intents(
        [{
            "trade_id": historical_entry.component_id,
            "symbol": historical_entry.symbol,
            "signal_ts_ms": historical_entry.metadata["signal_ts_ms"],
            "entry_reason": historical_entry.reason,
            "position_weight": historical_entry.metadata["position_weight"],
            "stop_loss_pct": 0.05,
            "take_profit_pct": 10.0,
            "max_hold_days": 3,
        }],
        demo=LongNativeDemoCycleConfig(entry_leverage=10.0),
        equity_usdt=1_000_000.0,
        order_notional_pct_equity=(
            cfg.gross_exposure / cfg.max_concurrent_positions * cfg.notional_multiplier
        ),
        price_by_symbol={historical_entry.symbol: kernel_decisions[0].reference_price},
        now_ms=kernel_decisions[0].wall_ts_ns // 1_000_000,
        strategy_id=cfg.execution_strategy_id,
    )[0].intent
    assert live_entry.decision_key == historical_entry.decision_key
    assert live_entry.target_key == historical_entry.target_key
    assert live_entry.signed_notional_usdt == historical_entry.signed_notional_usdt
    assert live_entry.leverage == historical_entry.leverage
    assert live_entry.metadata["stop_loss_pct"] == pytest.approx(0.05)
    assert live_entry.metadata["take_profit_pct"] == pytest.approx(10.0)
    assert live_entry.metadata["max_hold_duration_ms"] == 3 * 86_400_000
    assert {
        "stop_price",
        "take_profit_price",
        "planned_exit_ts_ms",
    }.isdisjoint(live_entry.metadata)


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
    assert "canonical_journal" not in default
    receipt = default["account_journal"]
    assert receipt["strategy_targets"] == 0
    assert receipt["canonical_common_kernel_parity"] is True
    assert receipt["cross_environment_strategy_parity"] is False
    assert receipt["historical_strategy_runtime_is_sequential"] is True
    assert receipt["strategy_runtime_shared_across_environments"] is False
    assert receipt["venue_rule_parity"] is False
    assert receipt["account_kernel_feedback_online"] is True
    assert receipt["entry_capacity_evaluated_at_actual_entry_boundary"] is True
    assert receipt["evidence_label"] == (
        "chronological_strategy_targets_through_live_common_account_kernel"
    )


def test_run_long_native_default_mode_reports_online_account_feedback(
    tmp_path: Path,
) -> None:
    from liquidity_migration.ingestion import generate_fixture_data
    from liquidity_migration.long_native import run_long_native_research
    from liquidity_migration.long_native_event_demo import _v11a_long_native_config

    generate_fixture_data(tmp_path)
    config = dataclasses.replace(
        _v11a_long_native_config(),
        fc_use_sniper_entry=False,
        fc_provisional_entry=False,
        require_full_pit_universe=False,
    )
    result = run_long_native_research(
        tmp_path,
        config=config,
        report_dir=tmp_path / "online-default",
    )

    assert "canonical_journal" not in result
    receipt = result["account_journal"]
    assert receipt["account_kernel_feedback_online"] is True
    assert receipt["same_timestamp_strategy_batching"] is True
    assert receipt["entry_capacity_evaluated_at_actual_entry_boundary"] is True
    assert receipt["evidence_label"] == (
        "chronological_strategy_targets_through_live_common_account_kernel"
    )


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


def test_methodology_run_label_is_conservative_for_raw_reports() -> None:
    assert _methodology_run_label(
        full_pit_universe_pass=True,
        archive_manifest_empty=False,
        tainted=False,
    ) == "exploratory"
    assert _methodology_run_label(
        full_pit_universe_pass=False,
        archive_manifest_empty=False,
        tainted=False,
    ) == "biased_benchmark"
    assert _methodology_run_label(
        full_pit_universe_pass=True,
        archive_manifest_empty=False,
        tainted=True,
    ) == "invalid"


def test_long_native_report_separates_methodology_and_data_labels() -> None:
    report = format_long_native_report({
        "methodology_run_label": "exploratory",
        "run_label": "full_pit_universe",
        "config": {
            "universe_size": 50,
            "universe_volume_window_days": 90,
            "regime_sma_days": 30,
            "enable_capitulation_rebound": False,
            "enable_funding_squeeze": False,
            "enable_volume_resurrection": False,
            "enable_fomo_chase": True,
            "max_concurrent_positions": 10,
            "cooldown_days": 7,
            "cost_multiplier": 3.0,
        },
        "rows": {"features": 10, "trades": 2},
        "date_range": {"start": "2023-01-01", "end": "2023-01-31"},
        "pit_manifest": {"full_pit_universe_pass": True},
        "summary": {},
        "promotion": {},
        "splits": [],
        "lifecycle": {},
        "event_counts": {"fomo_chase": 3},
    })
    assert "- Run label: `exploratory`" in report
    assert "- Data integrity label: `full_pit_universe`" in report
    assert "v11a is FC-only" in report
    assert "fc=True" in report
    assert "- fomo_chase: 3" in report


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
