"""Tests for the execution-grade continuous-fade engine (liquidity_migration.continuous_events).

Covers: the size/ADV impact cost model, fresh-entry gap + decile + liquid filters, the
concurrency/cooldown gate, funding-to-exit sign, and an end-to-end run on a synthetic full-PIT
root (panel build -> trades -> compounding equity -> artifacts).
"""
from __future__ import annotations

import dataclasses

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from liquidity_migration.account_kernel import AccountRiskPolicy
from liquidity_migration.continuous_events import (
    BTC_EXACT_MONTH_DAYS,
    BTC_TREND_MODE_HOURLY_30D,
    BTC_TREND_MODE_HOURLY_EXACT_MONTH,
    BTC_TREND_MODE_SMART_MONTH,
    ContinuousEventConfig,
    _additive_summary,
    _apply_entry_order,
    _assert_funding_one_per_settlement,
    _assert_rmom_covers_window,
    _btc_hourly_month_returns,
    _btc_smart_month_value,
    _btc_trend_returns,
    _btc_trend_return_lookup,
    derive_funding_interval_min,
    _daily_pnl_metrics,
    _fresh_entries,
    _listing_ts_by_symbol,
    _market_daily_returns,
    _panel_cache_path,
    _panel_cache_stale,
    _resolve_end_ms,
    _round_trip_bps,
    _run_trades,
    build_continuous_panel,
    cross_sectional_decile,
    run_continuous_event_research,
)
from liquidity_migration.storage import write_dataset
from liquidity_migration.execution_adapters import ExecutionTwinConfig, LatencyProfile
from liquidity_migration.historical_account_replay import (
    HistoricalAccountSession,
    synthetic_historical_rules_for_symbols,
)
from liquidity_migration.trade_lifecycle import _indexed_price_bars_by_symbol


def test_panel_cache_stale_invalidates_when_rmom_is_newer(tmp_path) -> None:
    """The deciled-panel cache is keyed only on rmom_quantile, so it MUST be invalidated when the
    underlying data is refreshed (new klines → rebuilt residual_momentum.parquet). Else it silently
    serves a panel truncated to the old data end (the 2026-06-03 'continuous curve stuck at May-27
    after a fresh rebuild' bug). Pins: cache older than rmom → stale; newer → fresh; rmom absent → stale."""
    import os
    cache = tmp_path / "_continuous_engine_panel_rmom33.parquet"
    rmom = tmp_path / "residual_momentum.parquet"
    cache.write_bytes(b"x")
    rmom.write_bytes(b"y")
    os.utime(cache, (1000, 1000))
    os.utime(rmom, (2000, 2000))   # rmom newer than cache
    assert _panel_cache_stale(cache, rmom) is True                 # → rebuild
    os.utime(cache, (3000, 3000))                                  # cache now newer than rmom
    assert _panel_cache_stale(cache, rmom) is False                # → reuse
    rmom.unlink()
    assert _panel_cache_stale(cache, rmom) is True                 # rmom missing → rebuild (safe)


def test_panel_cache_path_is_keyed_by_feature_set(tmp_path) -> None:
    """Feature-set sweeps must not share the same cached decile panel."""
    a = ContinuousEventConfig(rmom_quantile=0.33, feature_set=("rv_168h", "max_ret168"))
    b = ContinuousEventConfig(rmom_quantile=0.33, feature_set=("rv_168h", "vov"))
    assert _panel_cache_path(tmp_path, a, end_ms=0) != _panel_cache_path(tmp_path, b, end_ms=0)
    assert "_continuous_engine_panel_v4_" in _panel_cache_path(tmp_path, a, end_ms=0).name


def test_panel_cache_path_is_keyed_by_date_window(tmp_path) -> None:
    """Short smoke runs must not poison full-window research caches."""
    a = ContinuousEventConfig(start_date="2023-04-01", end_date="2023-05-01")
    b = ContinuousEventConfig(start_date="2023-04-01", end_date="2026-05-28")
    assert _panel_cache_path(tmp_path, a, end_ms=0) != _panel_cache_path(tmp_path, b, end_ms=0)


# --------------------------------------------------------------------------- cost model


def test_round_trip_bps_flat_override_bypasses_model() -> None:
    cfg = ContinuousEventConfig(flat_round_trip_bps=30.0, impact_coef_bps=999.0)
    assert _round_trip_bps(cfg, turnover_quote=1.0) == 30.0


def test_round_trip_bps_base_when_no_impact() -> None:
    cfg = ContinuousEventConfig(impact_coef_bps=0.0, taker_fee_bps=5.5, spread_bps=2.5)
    # 2*(taker+spread) = 2*8 = 16 bps, no impact term
    assert _round_trip_bps(cfg, turnover_quote=1_000_000.0) == pytest.approx(16.0)


def test_round_trip_bps_impact_rises_with_size_and_falls_with_liquidity() -> None:
    small = ContinuousEventConfig(deploy_capital_usd=1_000_000.0)
    big = ContinuousEventConfig(deploy_capital_usd=10_000_000.0)
    # bigger book -> more participation -> more impact -> higher cost (same ADV)
    assert _round_trip_bps(big, 1_000_000.0) > _round_trip_bps(small, 1_000_000.0)
    # more liquid name (higher ADV) -> less participation -> lower cost (same book)
    assert _round_trip_bps(small, 5_000_000.0) < _round_trip_bps(small, 500_000.0)


def test_round_trip_bps_uses_scaled_trade_notional_for_impact() -> None:
    cfg = ContinuousEventConfig(deploy_capital_usd=1_000_000.0, impact_coef_bps=50.0)
    base = _round_trip_bps(cfg, 1_000_000.0, notional_weight=0.02)
    scaled = _round_trip_bps(cfg, 1_000_000.0, notional_weight=0.08)
    assert scaled > base


# --------------------------------------------------------------------------- fresh entries


def test_fresh_entries_applies_decile_liquidity_and_gap() -> None:
    h = MS_PER_HOUR
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "B", "C"],
            # A: t0 fresh, t0+1h continuation (not fresh), t0+5h fresh again (gap>1h)
            "ts_ms": [0, h, 5 * h, 0, 0],
            "decile": [9, 9, 9, 9, 8],          # C is decile 8 -> excluded
            "composite": [0.9, 0.9, 0.9, 0.9, 0.5],
            "turnover_quote": [1e6, 1e6, 1e6, 1e3, 1e6],  # B below the 500k liquid gate
        }
    )
    cfg = ContinuousEventConfig(decile=9, liq_turnover_min=500_000.0)
    fresh = _fresh_entries(panel, cfg)
    got = sorted((r["symbol"], int(r["ts_ms"])) for r in fresh.to_dicts())
    # A at t0 and t0+5h are fresh; A at t0+1h is a continuation; B illiquid; C wrong decile
    assert got == [("A", 0), ("A", 5 * h)]


def test_fresh_entries_illiquid_hours_in_spell_do_not_create_new_spells() -> None:
    """Regression: 'fresh' must be computed on the FULL D9 timeline, then liquid-filtered.

    A name in D9 continuously for hours 0..3 with hours 1-2 illiquid is ONE spell -> one fresh
    entry at hour 0. Filtering liquid first would split it into two spurious fresh entries
    (hour 0 and hour 3), the ~2x inflation bug.
    """
    h = MS_PER_HOUR
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "A"],
            "ts_ms": [0, h, 2 * h, 3 * h],
            "decile": [9, 9, 9, 9],
            "composite": [0.9, 0.9, 0.9, 0.9],
            "turnover_quote": [1e6, 1e3, 1e3, 1e6],  # liquid, illiquid, illiquid, liquid
        }
    )
    fresh = _fresh_entries(panel, ContinuousEventConfig(decile=9, liq_turnover_min=500_000.0))
    got = sorted((r["symbol"], int(r["ts_ms"])) for r in fresh.to_dicts())
    assert got == [("A", 0)]  # ONE spell; hour 3 is a continuation, not a new fresh entry


def test_fresh_entries_state_exit_uses_hysteresis_hold_band_without_creating_d8_entries() -> None:
    """Live exits enter on fresh D9, then hold through D8 when exit_decile_buffer=1.

    The state spell_end must extend through the D8 wobble band so the backtest covers only
    once the name drops below D8; D8 itself must not create a new entry.
    """
    h = MS_PER_HOUR
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A", "A", "B"],
            "ts_ms": [0, h, 2 * h, 3 * h, h],
            "decile": [9, 8, 8, 7, 8],
            "composite": [0.9, 0.8, 0.8, 0.4, 0.8],
            "turnover_quote": [1e6, 1e6, 1e6, 1e6, 1e6],
        }
    )
    cfg = ContinuousEventConfig(
        decile=9,
        exit_mode="state",
        exit_decile_buffer=1,
        liq_turnover_min=500_000.0,
    )
    fresh = _fresh_entries(panel, cfg)
    assert fresh.select("symbol", "ts_ms", "spell_end_ts").to_dicts() == [
        {"symbol": "A", "ts_ms": 0, "spell_end_ts": 2 * h}
    ]


def test_fresh_entries_entry_event_trigger_can_fire_inside_existing_decile_spell() -> None:
    """Event-trigger mode is not the old always-on decile spell gate.

    If a name is already in D9 for hours 0..2 but the actual hourly catalyst happens at hour 2,
    the entry should fire at hour 2. That is the event-driven behavior: don't enter merely because
    D9 exists; enter when a fresh catalyst appears.
    """
    h = MS_PER_HOUR
    panel = pl.DataFrame(
        {
            "symbol": ["A", "A", "A"],
            "ts_ms": [0, h, 2 * h],
            "decile": [9, 9, 9],
            "composite": [0.9, 0.9, 0.9],
            "turnover_quote": [1e6, 1e6, 1e6],
            "ret1": [0.01, 0.02, 0.11],
            "max_ret168": [0.01, 0.02, 0.11],
            "prior6_ret1_max": [None, 0.01, 0.02],
            "giveback_from_prior6_high": [None, 0.0, 0.0],
            "turnover_spike_168h": [1.0, 1.0, 1.0],
        }
    )
    cfg = ContinuousEventConfig(decile=9, liq_turnover_min=500_000.0, entry_event_trigger="fresh_pop10")
    fresh = _fresh_entries(panel, cfg)
    got = sorted((r["symbol"], int(r["ts_ms"])) for r in fresh.to_dicts())
    assert got == [("A", 2 * h)]


def test_fresh_entries_entry_max_ret168_gate_filters_explosive_pumps() -> None:
    panel = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts_ms": [0, 0],
            "decile": [9, 9],
            "composite": [0.9, 0.9],
            "turnover_quote": [1e6, 1e6],
            "max_ret168": [0.08, 0.25],
        }
    )
    cfg = ContinuousEventConfig(decile=9, liq_turnover_min=500_000.0, entry_max_ret168_max=0.20)
    fresh = _fresh_entries(panel, cfg)
    got = sorted((r["symbol"], int(r["ts_ms"])) for r in fresh.to_dicts())
    assert got == [("A", 0)]


# --------------------------------------------------------------------------- concurrency / cooldown / funding


def _grid_klines(symbols: list[str], n_bars: int, *, price: float = 100.0) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        for i in range(n_bars):
            p = price  # flat price -> zero gross return, isolates the gate/funding mechanics
            rows.append({"ts_ms": i * MS_PER_HOUR, "symbol": sym,
                         "open": p, "high": p, "low": p, "close": p})
    return pl.DataFrame(rows)


def test_btc_trend_returns_are_prior_30d_excluding_current_day() -> None:
    rows = []
    for day in range(36):
        rows.append(
            {
                "ts_ms": day * MS_PER_DAY,
                "symbol": "BTCUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0 * (1.01 ** day),
            }
        )
        rows.append(
            {
                "ts_ms": day * MS_PER_DAY,
                "symbol": "A",
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
            }
        )
    trend = _btc_trend_returns(pl.DataFrame(rows))
    assert 30 * MS_PER_DAY not in trend
    assert trend[31 * MS_PER_DAY] == pytest.approx(0.30)


def test_btc_trend_returns_support_configured_lookback() -> None:
    rows = []
    for day in range(8):
        rows.append(
            {
                "ts_ms": day * MS_PER_DAY,
                "symbol": "BTCUSDT",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0 * (1.02 ** day),
            }
        )

    trend = _btc_trend_returns(pl.DataFrame(rows), lookback_days=3)

    assert 3 * MS_PER_DAY not in trend
    assert trend[4 * MS_PER_DAY] == pytest.approx(0.06)


def test_btc_hourly_month_returns_use_exact_cutoff_not_30d() -> None:
    anchor = 731 * MS_PER_HOUR
    rows = [
        {"ts_ms": 0, "symbol": "BTCUSDT", "close": 100.0},
        {"ts_ms": 11 * MS_PER_HOUR, "symbol": "BTCUSDT", "close": 200.0},
        {"ts_ms": anchor, "symbol": "BTCUSDT", "close": 300.0},
    ]

    exact_month = _btc_hourly_month_returns(
        pl.DataFrame(rows),
        lookback_ms=exact_duration_ms(days=BTC_EXACT_MONTH_DAYS),
    )
    thirty_days = _btc_trend_return_lookup(
        pl.DataFrame(rows),
        mode=BTC_TREND_MODE_HOURLY_30D,
        lookback_days=30,
    )

    assert exact_month[anchor] == pytest.approx(2.0)
    assert thirty_days[anchor] == pytest.approx(0.5)


def test_btc_hourly_month_returns_fail_closed_across_source_gap() -> None:
    anchor = 731 * MS_PER_HOUR
    rows = [
        {"ts_ms": 0, "symbol": "BTCUSDT", "close": 100.0},
        {"ts_ms": anchor, "symbol": "BTCUSDT", "close": 300.0},
    ]

    trend = _btc_hourly_month_returns(
        pl.DataFrame(rows),
        lookback_ms=exact_duration_ms(days=30),
    )

    assert anchor not in trend


def test_btc_smart_month_score_tolerates_small_leg_disagreement() -> None:
    assert _btc_smart_month_value(0.02, -0.005, tolerance=0.01) > 0.0
    assert _btc_smart_month_value(0.02, -0.05, tolerance=0.01) <= 0.0


def test_btc_trend_return_lookup_smart_month_combines_hourly_and_daily() -> None:
    rows = []
    for hour in range(0, 32 * 24):
        day = hour // 24
        close = 100.0
        if hour == 36:
            close = 100.0
        elif day == 1 and hour % 24 == 23:
            close = 98.0
        elif hour == 31 * 24:
            close = 101.0
        rows.append({"ts_ms": hour * MS_PER_HOUR, "symbol": "BTCUSDT", "close": close})

    trend = _btc_trend_return_lookup(
        pl.DataFrame(rows),
        mode=BTC_TREND_MODE_SMART_MONTH,
        lookback_days=30,
        month_days=BTC_EXACT_MONTH_DAYS,
        smart_tolerance=0.01,
    )

    assert trend[31 * MS_PER_DAY] > 0.0


def test_run_trades_respects_max_active_cap() -> None:
    syms = [f"S{i}" for i in range(6)]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 40))
    # all 6 fire fresh at the same signal ts -> with max_active=2 only 2 can open
    entries = pl.DataFrame(
        {"symbol": syms, "ts_ms": [0] * 6, "composite": [0.9] * 6, "turnover_quote": [1e6] * 6}
    )
    cfg = ContinuousEventConfig(max_active=2, hold_hours=10, entry_delay_hours=1, use_funding=False)
    trades, skips = _run_trades(entries, bars, None, cfg)
    assert trades.height == 2
    assert skips["skipped_capacity"] == 4


def test_run_trades_keeps_execution_leverage_separate_from_notional_weight() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]}
    )
    decisions = []
    cfg = ContinuousEventConfig(
        execution_strategy_id="continuous-parity-test",
        execution_leverage=10.0,
        gross_exposure=0.5,
        max_active=1,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
    )
    trades, _ = _run_trades(entries, bars, None, cfg, kernel_decision_sink=decisions)
    assert trades.height == 1
    assert len(decisions) == 2
    entry = decisions[0].intent.intent
    assert entry.strategy_id == "continuous-parity-test"
    assert entry.leverage == 10.0
    assert entry.signed_notional_usdt == pytest.approx(-0.5 * cfg.deploy_capital_usd)
    assert decisions[1].intent.intent.leverage == 10.0


def test_run_trades_does_not_invent_position_after_account_kernel_rejection(tmp_path) -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]}
    )
    config = ContinuousEventConfig(
        execution_strategy_id="continuous-feedback-test",
        execution_leverage=10.0,
        gross_exposure=0.5,
        max_active=1,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
    )
    session = HistoricalAccountSession(
        tmp_path / "account",
        account_id="continuous-feedback-test",
        risk_policy=AccountRiskPolicy(100.0, 100.0, 100.0, 100.0, 10.0),
        instrument_rules=synthetic_historical_rules_for_symbols(
            ["A"], max_leverage=10.0, observed_ts_ns=1
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(0, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="continuous-feedback-test",
    )
    decisions = []
    tape = []
    trades, skips = _run_trades(
        entries,
        bars,
        None,
        config,
        candidate_sink=tape,
        kernel_decision_sink=decisions,
        kernel_session=session,
    )
    assert trades.is_empty()
    assert skips["skipped_account_kernel"] == 1
    assert [row["reason"] for row in tape] == ["account_kernel_rejection"]
    assert any("component_gross_limit" in key for key in tape[0]["account_rejection_keys"])
    assert len(decisions) == 1
    assert session.kernel is not None
    assert session.kernel.state().positions == {}


def test_run_trades_neutralizes_target_after_execution_rejection(tmp_path) -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]}
    )
    config = ContinuousEventConfig(
        execution_strategy_id="continuous-execution-reject-test",
        execution_leverage=10.0,
        gross_exposure=0.5,
        max_active=1,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
    )
    session = HistoricalAccountSession(
        tmp_path / "account",
        account_id="continuous-execution-reject-test",
        risk_policy=AccountRiskPolicy(1e12, 1e12, 1e12, 1e12, 10.0),
        instrument_rules=synthetic_historical_rules_for_symbols(
            ["A"], max_leverage=10.0, observed_ts_ns=1
        ),
        execution_config=ExecutionTwinConfig(
            fee_bps=0.0,
            latency=LatencyProfile(1, 0, 0),
            max_decision_age_ns=0,
        ),
        id_seed="continuous-execution-reject-test",
    )
    decisions = []
    tape = []

    trades, skips = _run_trades(
        entries,
        bars,
        None,
        config,
        candidate_sink=tape,
        kernel_decision_sink=decisions,
        kernel_session=session,
    )

    assert trades.is_empty()
    assert skips["skipped_account_kernel"] == 1
    assert [row["reason"] for row in tape] == ["account_kernel_rejection"]
    assert any(
        "stale_decision" in key for key in tape[0]["account_rejection_keys"]
    ), tape[0]["account_rejection_keys"]
    # Entry target plus an explicit zero replacement: the venue rejection
    # cannot leave a stale desired target that reopens on an unrelated cycle.
    assert len(decisions) == 2
    assert decisions[-1].intent.intent.signed_notional_usdt == 0.0
    assert session.kernel is not None
    state = session.kernel.state()
    assert state.component_targets == {}
    assert state.positions == {}


def test_run_trades_optional_admission_lookup_blocks_entries() -> None:
    syms = ["A", "B"]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 40))
    entries = pl.DataFrame(
        {
            "symbol": syms,
            "ts_ms": [0, 0],
            "composite": [0.9, 0.9],
            "turnover_quote": [1e6, 1e6],
        }
    )
    cfg = ContinuousEventConfig(max_active=5, hold_hours=10, entry_delay_hours=1, use_funding=False)
    control, _ = _run_trades(entries, bars, None, cfg)
    admitted, skips = _run_trades(
        entries,
        bars,
        None,
        cfg,
        admission_lookup={("A", 0): False, ("B", 0): True},
    )
    assert control.height == 2
    assert admitted.height == 1
    assert admitted["symbol"].to_list() == ["B"]
    assert skips["skipped_admission"] == 1


def test_run_trades_can_skip_external_size_multiplier_before_capacity_consumption() -> None:
    syms = ["A", "B", "C"]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 40))
    entries = pl.DataFrame(
        {
            "symbol": syms,
            "ts_ms": [0, 0, 0],
            "composite": [0.9, 0.8, 0.7],
            "turnover_quote": [1e6, 1e6, 1e6],
        }
    )
    cfg = ContinuousEventConfig(
        max_active=2,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
        entry_skip_external_size_multiplier_lte=0.35,
    )
    tape: list[dict[str, object]] = []

    trades, skips = _run_trades(
        entries,
        bars,
        None,
        cfg,
        size_mult_lookup={("A", 0): 0.35, ("B", 0): 1.0, ("C", 0): 1.0},
        candidate_sink=tape,
    )

    assert trades.height == 2
    assert trades["symbol"].to_list() == ["B", "C"]
    assert skips["skipped_external_size_multiplier"] == 1
    assert skips["skipped_capacity"] == 0
    assert tape[0]["selected"] is False
    assert tape[0]["reason"] == "external_size_multiplier"
    assert tape[0]["external_size_multiplier"] == pytest.approx(0.35)


def test_run_trades_disaster_budget_caps_ex_ante_notional() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    entries = pl.DataFrame({
        "symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]
    })
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
        gross_exposure=0.5,
        entry_disaster_loss_budget_frac=0.001,
        entry_disaster_shock_frac=1.0,
    )
    tape: list[dict[str, object]] = []
    trades, skips = _run_trades(entries, bars, None, cfg, candidate_sink=tape)

    assert trades.height == 1
    assert trades["pre_risk_notional_weight"][0] == pytest.approx(0.1)  # .5 / max_active=5
    assert trades["notional_weight"][0] == pytest.approx(0.001)
    assert trades["entry_risk_size_clamped"][0] is True
    assert skips["skipped_portfolio_heat"] == 0
    assert tape[0]["disaster_notional_cap"] == pytest.approx(0.001)
    assert tape[0]["risk_size_clamped"] is True


def test_run_trades_portfolio_heat_partially_sizes_then_blocks() -> None:
    syms = ["A", "B", "C"]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 40))
    entries = pl.DataFrame({
        "symbol": syms,
        "ts_ms": [0, 0, 0],
        "composite": [0.9, 0.8, 0.7],
        "turnover_quote": [1e6, 1e6, 1e6],
    })
    cfg = ContinuousEventConfig(
        max_active=25,
        hold_hours=10,
        entry_delay_hours=1,
        use_funding=False,
        gross_exposure=0.5,
        entry_portfolio_heat_cap_frac=0.03,
        entry_disaster_shock_frac=1.0,
    )
    tape: list[dict[str, object]] = []
    trades, skips = _run_trades(entries, bars, None, cfg, candidate_sink=tape)

    assert trades.height == 2
    assert trades["notional_weight"].sum() == pytest.approx(0.03)
    assert trades["notional_weight"].to_list() == pytest.approx([0.02, 0.01])
    assert skips["skipped_portfolio_heat"] == 1
    assert [row["reason"] for row in tape] == ["selected", "selected", "portfolio_heat"]
    assert tape[1]["portfolio_heat_after_frac"] == pytest.approx(0.03)


def test_run_trades_default_risk_budget_is_schema_and_value_noop() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    entries = pl.DataFrame({
        "symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]
    })
    cfg = ContinuousEventConfig(max_active=25, hold_hours=10, entry_delay_hours=1, use_funding=False)
    trades, _ = _run_trades(entries, bars, None, cfg)
    assert trades["notional_weight"][0] == pytest.approx(cfg.notional_weight)
    assert "entry_risk_size_clamped" not in trades.columns


def test_run_trades_adverse_limit_fills_after_submit_bar_only() -> None:
    h = MS_PER_HOUR
    rows = []
    for i in range(8):
        rows.append(
            {
                "ts_ms": i * h,
                "symbol": "A",
                "open": 100.0,
                # The order is submitted at 1h. This same bar's high must not
                # fill a post-close limit; the 3h bar is the first valid touch.
                "high": 102.0 if i == 0 else 101.2 if i == 2 else 100.0,
                "low": 88.0 if i == 3 else 99.0,
                "close": 100.0,
            }
        )
    bars = _indexed_price_bars_by_symbol(pl.DataFrame(rows))
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=4,
        entry_delay_hours=0,
        entry_adverse_limit_pct=0.01,
        entry_adverse_limit_wait_hours=4,
        take_profit_pct=0.10,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    tape: list[dict[str, object]] = []

    trades, skips = _run_trades(entries, bars, None, cfg, candidate_sink=tape)

    assert skips["skipped_entry_limit_unfilled"] == 0
    assert trades.height == 1
    assert trades["entry_ts_ms"][0] == 3 * h
    assert trades["entry_price"][0] == pytest.approx(101.0)
    assert trades["exit_reason"][0] == "take_profit"
    assert tape[0]["order_submit_ts_ms"] == h
    assert tape[0]["fill_window_start_ts_ms"] == h
    assert tape[0]["fill_window_end_ts_ms"] == 3 * h
    assert tape[0]["entry_bar_end_ts_ms"] == 3 * h
    assert tape[0]["entry_limit_price"] == pytest.approx(101.0)
    assert tape[0]["entry_fill_mode"] == "adverse_limit"


def test_run_trades_adverse_limit_unfilled_rejects_candidate() -> None:
    h = MS_PER_HOUR
    rows = [
        {
            "ts_ms": i * h,
            "symbol": "A",
            "open": 100.0,
            "high": 100.5,
            "low": 99.0,
            "close": 100.0,
        }
        for i in range(8)
    ]
    bars = _indexed_price_bars_by_symbol(pl.DataFrame(rows))
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=4,
        entry_delay_hours=0,
        entry_adverse_limit_pct=0.01,
        entry_adverse_limit_wait_hours=3,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    tape: list[dict[str, object]] = []

    trades, skips = _run_trades(entries, bars, None, cfg, candidate_sink=tape)

    assert trades.height == 0
    assert skips["skipped_entry_limit_unfilled"] == 1
    assert tape[0]["selected"] is False
    assert tape[0]["reason"] == "entry_limit_unfilled"
    assert tape[0]["order_submit_ts_ms"] == h
    assert tape[0]["fill_window_end_ts_ms"] == 4 * h


def test_run_trades_take_profit_exits_short_before_timer() -> None:
    rows = []
    for i in range(12):
        close = 100.0
        low = 100.0
        if i == 3:
            low = 94.0
            close = 96.0
        rows.append(
            {
                "ts_ms": i * MS_PER_HOUR,
                "symbol": "A",
                "open": 100.0,
                "high": 100.0,
                "low": low,
                "close": close,
            }
        )
    bars = _indexed_price_bars_by_symbol(pl.DataFrame(rows))
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=10,
        entry_delay_hours=0,
        take_profit_pct=0.05,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, _ = _run_trades(entries, bars, None, cfg)
    assert trades.height == 1
    assert trades["exit_reason"][0] == "take_profit"
    assert trades["exit_price"][0] == pytest.approx(95.0)


def test_run_trades_rank_exit_cuts_short_when_composite_rank_decays() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 20))
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    rank_lookup = {("A", i * MS_PER_HOUR): 0.5 for i in range(20)}
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=10,
        entry_delay_hours=0,
        rank_exit_threshold=0.8,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, _ = _run_trades(entries, bars, None, cfg, rank_lookup=rank_lookup)
    assert trades.height == 1
    assert trades["exit_reason"][0] == "rank_exit"
    assert trades["hold_hours"][0] < 10.0


def test_run_trades_entry_crowding_skips_overcrowded_signal_hours() -> None:
    syms = ["A", "B", "C", "D"]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 20))
    entries = pl.DataFrame(
        {
            "symbol": syms,
            "ts_ms": [0, 0, 0, 5 * MS_PER_HOUR],
            "composite": [0.9] * 4,
            "turnover_quote": [1e6] * 4,
        }
    )
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=2,
        entry_delay_hours=0,
        entry_crowding_max_fresh=2,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, skips = _run_trades(entries, bars, None, cfg)
    assert trades.height == 1
    assert trades["symbol"][0] == "D"
    assert skips["skipped_crowding"] == 3


def test_run_trades_btc_trend_gate_uses_signal_day() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A", "B"], 60))
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts_ms": [0, MS_PER_DAY],
            "composite": [0.9, 0.9],
            "turnover_quote": [1e6, 1e6],
        }
    )
    btc_trend = {0: 0.10, MS_PER_DAY: -0.10}
    cfg = ContinuousEventConfig(
        btc_trend_gate="uptrend",
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
    )
    trades, skips = _run_trades(entries, bars, None, cfg, btc_trend_daily=btc_trend)
    assert trades.height == 1
    assert trades["symbol"][0] == "A"
    assert skips["skipped_btc_trend"] == 1


def test_run_trades_hourly_btc_trend_mode_keys_signal_hour() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A", "B"], 60))
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B"],
            "ts_ms": [0, MS_PER_HOUR],
            "composite": [0.9, 0.9],
            "turnover_quote": [1e6, 1e6],
        }
    )
    cfg = ContinuousEventConfig(
        btc_trend_gate="uptrend",
        btc_trend_mode=BTC_TREND_MODE_HOURLY_EXACT_MONTH,
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
    )

    trades, skips = _run_trades(entries, bars, None, cfg, btc_trend_daily={0: -0.10, MS_PER_HOUR: 0.10})

    assert trades.height == 1
    assert trades["symbol"][0] == "B"
    assert skips["skipped_btc_trend"] == 1


def test_candidate_tape_records_configured_btc_trend_lookback() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 80))
    entries = pl.DataFrame(
        {
            "symbol": ["A"],
            "ts_ms": [10 * MS_PER_DAY],
            "composite": [0.9],
            "turnover_quote": [1e6],
        }
    )
    cfg = ContinuousEventConfig(
        btc_trend_gate="uptrend",
        btc_trend_lookback_days=5,
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
    )
    sink: list[dict[str, object]] = []

    _run_trades(entries, bars, None, cfg, btc_trend_daily={10 * MS_PER_DAY: 0.10}, candidate_sink=sink)

    assert sink[0]["btc_trend_lookback_days"] == 5
    assert sink[0]["btc_trend_mode"] == "daily_prior"
    assert sink[0]["btc_trend_lookback_duration_ms"] == 5 * MS_PER_DAY
    assert sink[0]["btc_trend_source_start_ts_ms"] == 5 * MS_PER_DAY
    assert sink[0]["btc_trend_source_end_ts_ms"] == 9 * MS_PER_DAY


def test_run_trades_market_gate_reads_prior_day_not_entry_day() -> None:
    """The market-context gate must key on the PRIOR completed day's market
    return, never the entry day's own full-day (future) close-to-close return.

    Poison-future check: a single entry on day 1 (signal ts = MS_PER_DAY). When
    the prior day (0) is good and the entry day (1) is bad, a causal gate lets the
    entry through; a look-ahead gate keyed on the entry day would block it. The
    converse (prior bad, entry good) must block — proving the gate still works,
    just lagged."""
    entries = pl.DataFrame(
        {
            "symbol": ["A"],
            "ts_ms": [MS_PER_DAY],
            "composite": [0.9],
            "turnover_quote": [1e6],
        }
    )
    cfg = ContinuousEventConfig(
        market_min_ret_1d=-0.10,
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    # Prior day (0) good, entry day (1) bad -> causal gate PASSES (look-ahead would block).
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 60))
    trades_pass, _ = _run_trades(
        entries, bars, None, cfg, market_daily={0: 0.05, MS_PER_DAY: -0.50}
    )
    assert trades_pass.height == 1, "causal gate must read the good prior day, not the bad entry day"

    # Prior day (0) bad, entry day (1) good -> causal gate BLOCKS (look-ahead would pass).
    bars2 = _indexed_price_bars_by_symbol(_grid_klines(["A"], 60))
    trades_block, _ = _run_trades(
        entries, bars2, None, cfg, market_daily={0: -0.50, MS_PER_DAY: 0.05}
    )
    assert trades_block.height == 0, "causal gate must block on the bad prior day"


def test_state_exit_holds_only_while_in_decile() -> None:
    """state mode: a name in D9 for hours 0..2 then gone must exit ~at the spell end (+1h),
    NOT run a fixed 12h timer. The fresh entry carries spell_end_ts = last in-decile hour."""
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 60))
    # fresh entry at sig_ts=0; the D9 spell's last hour is 2h (spell_end_ts = 2h)
    entries = pl.DataFrame(
        {"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6],
         "spell_end_ts": [2 * MS_PER_HOUR]}
    )
    cfg = ContinuousEventConfig(exit_mode="state", entry_delay_hours=1, max_hold_hours=48,
                                hold_hours=12, use_funding=False, max_active=5)
    trades, _ = _run_trades(entries, bars, None, cfg)
    assert trades.height == 1
    # entry bar ends at sig_ts + (1+1)h = 2h; state exit = spell_end(2h) + 2h = 4h -> hold ~2h, NOT 12h
    assert trades["hold_hours"][0] <= 4.0
    assert trades["exit_reason"][0] == "left_decile"  # planned state exit, no stop


def test_run_trades_stop_approach_cuts_before_server_stop_and_labels_reason() -> None:
    rows = []
    for i in range(8):
        high = 100.0
        close = 100.0
        if i == 3:
            high = 120.0
            close = 120.0
        rows.append(
            {
                "ts_ms": i * MS_PER_HOUR,
                "symbol": "A",
                "open": 100.0,
                "high": high,
                "low": 100.0,
                "close": close,
            }
        )
    bars = _indexed_price_bars_by_symbol(pl.DataFrame(rows))
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    cfg = ContinuousEventConfig(
        max_active=5,
        hold_hours=6,
        entry_delay_hours=0,
        stop_loss_pct=0.25,
        stop_approach_frac=0.8,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, _ = _run_trades(entries, bars, None, cfg)
    assert trades.height == 1
    assert trades["exit_reason"][0] == "stop_approach"
    assert trades["exit_price"][0] == pytest.approx(120.0)


def test_run_trades_cooldown_blocks_reentry() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 60))
    # same symbol fires fresh at t=0 and again 3h later; cooldown = hold = 10h blocks the 2nd
    entries = pl.DataFrame(
        {"symbol": ["A", "A"], "ts_ms": [0, 3 * MS_PER_HOUR], "composite": [0.9, 0.9],
         "turnover_quote": [1e6, 1e6]}
    )
    cfg = ContinuousEventConfig(max_active=5, hold_hours=10, entry_delay_hours=1, use_funding=False)
    trades, skips = _run_trades(entries, bars, None, cfg)
    assert trades.height == 1
    assert skips["skipped_cooldown"] == 1


def test_run_trades_circuit_breaker_pauses_after_adverse_cluster() -> None:
    """Engine circuit breaker: flat prices make every cover net-negative (cost only) = 'adverse', so
    once N=3 adverse exits sit in the 24h window, later entries pause. Causal: only exits that closed
    before the candidate entry count. Regression for the knob the cb1 sweep validated (verdict: cuts
    DD but not a robust cross-venue MAR win, so default OFF)."""
    syms = [f"S{i}" for i in range(10)]
    bars = _indexed_price_bars_by_symbol(_grid_klines(syms, 60))
    # staggered 2h apart, hold 1h, +1h fill -> S_i exits at (i+1)*2h, before later entries fire
    entries = pl.DataFrame({
        "symbol": syms, "ts_ms": [i * 2 * MS_PER_HOUR for i in range(10)],
        "composite": [0.9] * 10, "turnover_quote": [1e6] * 10,
    })
    common = dict(entry_delay_hours=0, hold_hours=1, use_funding=False, max_active=25)
    off, skips_off = _run_trades(entries, bars, None, ContinuousEventConfig(**common))
    assert off.height == 10 and skips_off["skipped_breaker"] == 0       # breaker OFF: all 10 open
    on, skips_on = _run_trades(
        entries, bars, None,
        ContinuousEventConfig(**common, entry_pause_after_adverse_exits=3, entry_pause_window_hours=24))
    # S0/S1/S2 open (cluster not yet at 3); from S3 on, the 3 adverse exits (2h/4h/6h) stay in-window -> paused
    assert on.height == 3 and skips_on["skipped_breaker"] == 7


def test_portfolio_mtm_marks_open_positions_and_aggregates_correlated_days() -> None:
    """MTM must distribute each trade's PnL across the days it's open and aggregate concurrent
    correlated moves — two shorts both rising on the same days produce correlated negative days,
    and a real drawdown, that realized-at-exit would hide until exit."""
    from liquidity_migration.continuous_events import _portfolio_mtm_equity

    d = MS_PER_DAY
    rows = []
    for sym, prices in [("A", [100.0, 110.0, 121.0]), ("B", [50.0, 55.0, 60.5])]:  # both +10%/day -> shorts lose
        for i, p in enumerate(prices):
            rows.append({"ts_ms": i * d, "symbol": sym, "open": p, "high": p, "low": p, "close": p})
    klines = pl.DataFrame(rows)
    trades = pl.DataFrame({
        "symbol": ["A", "B"], "side": ["short", "short"],
        "entry_ts_ms": [0, 0], "exit_ts_ms": [2 * d, 2 * d],
        "entry_price": [100.0, 50.0], "exit_price": [121.0, 60.5],
        "notional_weight": [0.02, 0.02], "cost_return": [0.0, 0.0], "funding_return": [0.0, 0.0],
    })
    eq = _portfolio_mtm_equity(trades, klines)
    pnl = eq["basket_return"].to_list()
    assert len(pnl) >= 2
    assert all(p < 0 for p in pnl[-2:])     # both held days are correlated losses
    assert eq["drawdown"].min() < 0.0       # a real portfolio drawdown shows up


def test_portfolio_mtm_preserves_ledger_day_semantics() -> None:
    """The persisted MTM series is the validated input of the ensemble rebalance pipeline,
    whose vol/beta/momentum windows are defined over trailing LEDGER rows and whose hedge
    sizes every input row. It must therefore keep ledger-day shape: NO calendar zero-fill
    between positions and NO tail past the last position day. (Calendar-filling re-levered
    the validated deployed book 103%->87% and fabricated hedge PnL on flat days,
    observed 2026-06-12.) Flat-day presentation is chart-layer-only."""
    from liquidity_migration.continuous_events import _portfolio_mtm_equity

    d = MS_PER_DAY
    rows = [
        {"ts_ms": i * d, "symbol": "A", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
        for i in range(9)  # klines cover days 0..8, well past the last exit
    ]
    klines = pl.DataFrame(rows)
    trades = pl.DataFrame({
        "symbol": ["A", "A"], "side": ["short", "short"],
        "entry_ts_ms": [0, 5 * d], "exit_ts_ms": [1 * d, 6 * d],
        "entry_price": [100.0, 100.0], "exit_price": [100.0, 100.0],
        "notional_weight": [0.02, 0.02], "cost_return": [0.0, 0.0], "funding_return": [0.0, 0.0],
    })
    eq = _portfolio_mtm_equity(trades, klines)
    # only position days appear: days 0-1 (first trade) and 5-6 (second); no gap fill, no tail
    assert eq["ts_ms"].to_list() == [0, 1 * d, 5 * d, 6 * d]


def test_extend_equity_flat_for_chart_pads_zero_days_to_boundary() -> None:
    """The chart-only helper carries equity/drawdown flat through the data boundary so a
    gate-blocked flat spell renders as a flat line (and its months reach the axis) without
    ever touching the persisted ledger-day series."""
    from liquidity_migration.continuous_events import _extend_equity_flat_for_chart

    d = MS_PER_DAY
    eq = pl.DataFrame({
        "ts_ms": [0, 1 * d, 2 * d],
        "equity": [1.0, 1.01, 1.005],
        "drawdown": [0.0, 0.0, -0.005],
        "basket_return": [0.0, 0.01, -0.005],
    })
    out = _extend_equity_flat_for_chart(eq, through_ts_ms=6 * d)
    assert out["ts_ms"].to_list() == [i * d for i in range(7)]
    tail = out.filter(pl.col("ts_ms") > 2 * d)
    assert tail["basket_return"].to_list() == [0.0] * 4
    assert set(tail["equity"].to_list()) == {1.005}
    assert set(tail["drawdown"].to_list()) == {-0.005}
    # no-op when the boundary is not past the series end
    same = _extend_equity_flat_for_chart(eq, through_ts_ms=2 * d)
    assert same["ts_ms"].to_list() == eq["ts_ms"].to_list()


def test_run_trades_funding_to_exit_credits_short() -> None:
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A"], 40))
    from liquidity_migration.trade_lifecycle import _funding_lookup

    # positive funding_rate every hour -> a SHORT receives funding (positive funding_return)
    fund = pl.DataFrame(
        {"ts_ms": [i * MS_PER_HOUR for i in range(40)], "symbol": ["A"] * 40,
         "funding_rate": [0.0005] * 40, "funding_interval_min": [60] * 40}
    )
    lookup = _funding_lookup(fund)
    entries = pl.DataFrame({"symbol": ["A"], "ts_ms": [0], "composite": [0.9], "turnover_quote": [1e6]})
    cfg = ContinuousEventConfig(max_active=5, hold_hours=10, entry_delay_hours=1, use_funding=True,
                                flat_round_trip_bps=0.0)
    trades, _ = _run_trades(entries, bars, lookup, cfg)
    assert trades.height == 1
    assert trades["funding_return"][0] > 0.0  # short is paid when funding_rate > 0
    assert trades["side"][0] == "short"


# --------------------------------------------------------------------------- end-to-end on a synthetic root


def _build_synthetic_root(tmp_path, *, n_symbols: int = 26, n_bars: int = 500, include_btc: bool = False):
    """A synthetic full-PIT root: klines_1h + funding + residual_momentum.parquet.

    Enough symbols (so the rmom-low half still deciles up to 9) and enough bars (so the 720h
    vov window warms up). Prices drift mildly per-symbol so the within-ts cross-section is
    non-degenerate; a deterministic seed keeps it reproducible without Math.random.
    """
    root = tmp_path / "synth_full_pit"
    root.mkdir()
    start = 1_700_000_000_000  # fixed epoch ms, midnight-aligned enough for the day-floor join
    start -= start % MS_PER_DAY
    rows = []
    for s in range(n_symbols):
        p = 100.0 + s
        for i in range(n_bars):
            # deterministic pseudo-noise (no RNG): a per-(symbol,bar) sine wobble
            wob = 1.0 + 0.02 * ((s * 7 + i * 13) % 11 - 5) / 5.0
            p = max(1.0, p * wob)
            ts = start + i * MS_PER_HOUR
            rows.append({"ts_ms": ts, "symbol": f"S{s:02d}", "open": p, "high": p * 1.01,
                         "low": p * 0.99, "close": p, "volume_base": 1000.0, "turnover_quote": 1_000_000.0})
    if include_btc:
        p = 20_000.0
        for i in range(n_bars):
            p *= 1.0005
            ts = start + i * MS_PER_HOUR
            rows.append({
                "ts_ms": ts,
                "symbol": "BTCUSDT",
                "open": p,
                "high": p * 1.001,
                "low": p * 0.999,
                "close": p,
                "volume_base": 1000.0,
                "turnover_quote": 100_000_000.0,
            })
    klines = pl.DataFrame(rows)
    write_dataset(klines, root, "klines_1h")

    # Funding history is one row per settlement (8h here, the canonical full-PIT shape) — NOT one
    # row per hourly kline. A faithful one-per-settlement fixture also matches what the engine's
    # snapshot-scrape guard (_assert_funding_one_per_settlement) expects of a real root.
    fund = (
        klines.select("ts_ms", "symbol")
        .filter((pl.col("ts_ms") // MS_PER_HOUR) % 8 == 0)
        .with_columns(
            pl.lit(0.0001).alias("funding_rate"), pl.lit(480).alias("funding_interval_min")
        )
    )
    write_dataset(fund, root, "funding")

    # residual_momentum: one row per (symbol, daily-floored ts). Spread values so the rmom-low
    # half is well-defined within each day.
    days = sorted({(start + i * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for i in range(n_bars)})
    rmom_rows = [{"symbol": f"S{s:02d}", "ts_ms": d, "residual_momentum": (s % 13) * 0.001 - 0.006,
                  "is_provisional": False}
                 for d in days for s in range(n_symbols)]
    pl.DataFrame(rmom_rows).write_parquet(root / "residual_momentum.parquet")
    return root, start, n_bars


def _iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def test_build_continuous_panel_has_deciles(tmp_path) -> None:
    root, start, n_bars = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY))
    panel = build_continuous_panel(root, cfg, cache=False)
    assert set(panel.columns) >= {"symbol", "ts_ms", "decile", "composite", "turnover_quote"}
    assert not panel.is_empty()
    assert 9 in set(panel["decile"].to_list())  # top decile is populated


def _matplotlib_available() -> bool:
    """True if the optional matplotlib charting dependency can be imported (mirrors the
    renderer's own guard so the PNG assertion only fires when a PNG can actually be drawn)."""
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


def test_end_to_end_run_produces_trades_equity_and_artifacts(tmp_path) -> None:
    root, start, n_bars = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY),     # past feature warm-up, well inside the data
        end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, cooldown_hours=24, entry_delay_hours=1, max_active=10, use_funding=True,
        split_date=_iso(start + 16 * MS_PER_DAY),
    )
    payload = run_continuous_event_research(root, config=cfg, report_dir=tmp_path / "rep")
    assert payload["run_label"] == "exploratory"
    assert payload["config_hash"]
    # The synthetic cross-section should yield a populated D9 and some fresh trades.
    assert payload["n_trades"] >= 1
    assert "canonical_journal" not in payload
    assert payload["canonical_common_kernel_parity"] is True
    assert payload["cross_environment_strategy_parity"] is False
    assert payload["account_journal"]["strategy_targets"] == payload["n_trades"] * 2
    assert payload["account_journal"]["evidence_label"] == (
        "chronological_strategy_targets_through_live_common_account_kernel"
    )
    assert payload["account_journal"]["historical_strategy_runtime_is_sequential"] is True
    assert payload["account_journal"]["account_kernel_feedback_online"] is True
    assert payload["account_journal"]["same_timestamp_strategy_batching"] is True
    assert payload["account_journal"]["events"] > 0
    assert payload["account_journal"]["fills"] == payload["n_trades"] * 2
    assert payload["account_journal"]["strategy_runtime_shared_across_environments"] is False
    assert "full" in payload["metrics"]
    full = payload["metrics"]["full"]
    assert {"total_return", "max_drawdown", "sharpe_like", "mar"} <= set(full)
    # artifacts written
    assert (tmp_path / "rep" / "continuous_report.json").exists()
    assert (tmp_path / "rep" / "continuous_trades.csv").exists()
    # The equity PNG is best-effort: matplotlib is an optional charting dependency
    # (not in install_requires), so only assert the file when it's importable. Use the
    # same import path the renderer uses so test and production agree on availability.
    if _matplotlib_available():
        assert (tmp_path / "rep" / "continuous_equity.png").exists()


def test_end_to_end_btc_trend_gate_passes_computed_trend_to_trade_walker(tmp_path) -> None:
    root, start, _ = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=1200, include_btc=True)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 36 * MS_PER_DAY),
        end_date=_iso(start + 45 * MS_PER_DAY),
        hold_hours=6,
        cooldown_hours=24,
        entry_delay_hours=1,
        max_active=10,
        use_funding=False,
        btc_trend_gate="uptrend",
        split_date=_iso(start + 40 * MS_PER_DAY),
    )
    tape_path = tmp_path / "btc_gate_tape.parquet"
    payload = run_continuous_event_research(
        root,
        config=cfg,
        report_dir=tmp_path / "btc_gate_rep",
        candidate_tape_path=tape_path,
    )
    assert payload["n_fresh_entries"] > 0
    assert payload["n_trades"] > 0
    assert payload["skips"]["skipped_btc_trend"] < payload["n_fresh_entries"]
    tape = pl.read_parquet(tape_path)
    assert tape["btc_trend_source_start_ts_ms"].is_not_null().all()
    assert tape["btc_trend_lookback_days"].is_not_null().all()
    assert tape["btc_trend_source_end_ts_ms"].is_not_null().all()
    assert tape["btc_trend_data_available_ts_ms"].is_not_null().all()
    assert (tape["btc_trend_source_end_ts_ms"] < tape["decision_ts_ms"]).all()
    assert (tape["btc_trend_data_available_ts_ms"] <= tape["decision_ts_ms"]).all()


def test_end_to_end_age_gate_loads_enough_history_for_age_test(tmp_path) -> None:
    root, start, _ = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=2200)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 70 * MS_PER_DAY),
        end_date=_iso(start + 82 * MS_PER_DAY),
        hold_hours=6,
        cooldown_hours=24,
        entry_delay_hours=1,
        max_active=10,
        use_funding=False,
        age_days_min=60,
        split_date=_iso(start + 76 * MS_PER_DAY),
    )
    payload = run_continuous_event_research(root, config=cfg, report_dir=tmp_path / "age_gate_rep")
    assert payload["n_fresh_entries"] > 0
    assert payload["n_trades"] > 0
    assert payload["skips"]["skipped_gate"] < payload["n_fresh_entries"]


# --------------------------------------------------------------------------- market-regime gate
# audit2c: the equal-weight market-regime gate computes gap-aware daily returns. A symbol with a
# missing calendar day must NOT contribute a multi-day return mislabelled as that day's 1-day
# return to the cross-sectional mean.

D0, D1, D2 = 0, MS_PER_DAY, 2 * MS_PER_DAY


def _bar(symbol, day_ts, close):
    return {"ts_ms": day_ts + 3600_000, "symbol": symbol, "close": close}


def test_market_daily_returns_excludes_gap_day() -> None:
    klines = pl.DataFrame([
        # Symbol A: contiguous days 0,1,2 -> +10% each day.
        _bar("A", D0, 100.0), _bar("A", D1, 110.0), _bar("A", D2, 121.0),
        # Symbol B: GAP at day 1 (present on day 0 and day 2 only).
        _bar("B", D0, 100.0), _bar("B", D2, 120.0),
    ])
    out = _market_daily_returns(klines)
    # Day 1: only A has a return (B has no day-1 bar).
    assert out[D1] == pytest.approx(0.10)
    # Day 2: A = +10%; B's return is gap-aware NULL (no calendar-consecutive day-1
    # predecessor), so B is EXCLUDED — the mean is 0.10, NOT mean(0.10, 0.20)=0.15 that
    # the old gap-blind shift(1) would have produced.
    assert out[D2] == pytest.approx(0.10)


def test_market_daily_returns_contiguous_unchanged() -> None:
    klines = pl.DataFrame([
        _bar("A", D0, 100.0), _bar("A", D1, 110.0),
        _bar("B", D0, 200.0), _bar("B", D1, 210.0),
    ])
    out = _market_daily_returns(klines)
    # A +10%, B +5% -> mean 7.5% (byte-identical to the plain-shift result on contiguous data).
    assert out[D1] == pytest.approx(0.075)


# ---------------------------------------------------------------------------
# audit2
# [11] continuous_events panel cache key must include exclude_symbols.
# ---------------------------------------------------------------------------

_T0 = 1_700_000_000_000  # arbitrary end_ms timestamp int for cache-path keying


def test_panel_cache_key_distinguishes_exclude_symbols(tmp_path) -> None:
    base = ContinuousEventConfig()
    cfg_a = dataclasses.replace(base, exclude_symbols=("BTCUSDT",))
    cfg_b = dataclasses.replace(base, exclude_symbols=("ETHUSDT",))
    cfg_none = dataclasses.replace(base, exclude_symbols=())
    pa = _panel_cache_path(tmp_path, cfg_a, end_ms=_T0)
    pb = _panel_cache_path(tmp_path, cfg_b, end_ms=_T0)
    pnone = _panel_cache_path(tmp_path, cfg_none, end_ms=_T0)
    assert pa != pb  # different exclusion sets must not collide (was the bug)
    assert pa != pnone and pb != pnone
    # the empty-exclusion (live/default) filename must NOT carry an _excl tag, so no
    # existing cache is invalidated.
    assert "_excl" not in pnone.name


def test_panel_cache_key_exclude_order_independent(tmp_path) -> None:
    base = ContinuousEventConfig()
    p1 = _panel_cache_path(tmp_path, dataclasses.replace(base, exclude_symbols=("AUSDT", "BUSDT")), end_ms=_T0)
    p2 = _panel_cache_path(tmp_path, dataclasses.replace(base, exclude_symbols=("BUSDT", "AUSDT")), end_ms=_T0)
    assert p1 == p2


# ---------------------------------------------------------------------------
# Relocated from tests/test_audit_fix_b02.py (audit bucket b02 regressions).
# Each test targets one audit finding and would FAIL on the pre-fix code.
# ---------------------------------------------------------------------------
def _build_root(tmp_path, *, n_symbols: int = 26, n_bars: int = 720, funding_8h: bool = True):
    """Minimal synthetic full-PIT root: klines_1h + funding + residual_momentum.parquet."""
    root = tmp_path / "root"
    root.mkdir()
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    rows = []
    for s in range(n_symbols):
        p = 100.0 + s
        for i in range(n_bars):
            wob = 1.0 + 0.02 * ((s * 7 + i * 13) % 11 - 5) / 5.0
            p = max(1.0, p * wob)
            rows.append(
                {"ts_ms": start + i * MS_PER_HOUR, "symbol": f"S{s:02d}", "open": p,
                 "high": p * 1.01, "low": p * 0.99, "close": p, "volume_base": 1000.0,
                 "turnover_quote": 1_000_000.0}
            )
    klines = pl.DataFrame(rows)
    write_dataset(klines, root, "klines_1h")

    fund = klines.select("ts_ms", "symbol")
    if funding_8h:
        fund = fund.filter((pl.col("ts_ms") // MS_PER_HOUR) % 8 == 0)
        fund = fund.with_columns(
            pl.lit(0.0001).alias("funding_rate"), pl.lit(480).alias("funding_interval_min")
        )
    else:
        # Realistic hourly SNAPSHOT scrape of an 8h-settling symbol: the rate is
        # constant within each 8h settlement block and changes across blocks, so
        # naive exact-stamp dedup would over-charge ~8x. The engine must collapse it
        # via the derived true interval (480), not over-charge or reject.
        fund = fund.with_columns(
            (0.0001 + 0.00001 * ((pl.col("ts_ms") // (8 * MS_PER_HOUR)) % 7)).alias("funding_rate"),
            pl.lit(480).alias("funding_interval_min"),
        )
    write_dataset(fund, root, "funding")

    days = sorted({(start + i * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for i in range(n_bars)})
    rmom_rows = [
        {"symbol": f"S{s:02d}", "ts_ms": d, "residual_momentum": (s % 13) * 0.001 - 0.006,
         "is_provisional": False}
        for d in days
        for s in range(n_symbols)
    ]
    pl.DataFrame(rmom_rows).write_parquet(root / "residual_momentum.parquet")
    return root, start, n_bars


def _iso_to_ms(date_str: str) -> int:
    from liquidity_migration.daily_feature_panel import _date_str_to_ms

    return _date_str_to_ms(date_str)


# cli-config-5: continuous-events --end empty default is data-driven, not a stale fixed date
def test_continuous_events_end_date_default_is_empty_data_driven() -> None:
    """The dataclass default must NOT be a hardcoded past date (was 2026-05-28, which silently
    truncated the freshest weeks as the calendar advanced). An empty default signals data-driven."""
    assert ContinuousEventConfig().end_date == ""


def test_resolve_end_ms_clamps_to_day_after_last_kline_when_default(tmp_path) -> None:
    """Empty end_date -> clamp to the day AFTER the root's last kline (end-exclusive, so the final
    full day is included). A fixed past default would have stopped earlier and dropped recent days."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=4, n_bars=200)
    cfg = ContinuousEventConfig(start_date=_iso(start), end_date="")
    last_ts = start + (n_bars - 1) * MS_PER_HOUR
    expected = (last_ts // MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY
    assert _resolve_end_ms(root, cfg) == expected
    # An explicit end_date is still honored verbatim (frozen/forward runs pin it).
    cfg_explicit = ContinuousEventConfig(start_date=_iso(start), end_date=_iso(start + 3 * MS_PER_DAY))
    assert _resolve_end_ms(root, cfg_explicit) == _iso_to_ms(_iso(start + 3 * MS_PER_DAY))


# pit-data-5: stale residual_momentum.parquet must fail loudly, not silently truncate
def test_assert_rmom_covers_window_raises_when_rmom_lags(tmp_path) -> None:
    """A present-but-stale rmom (lagging the klines window beyond tolerance) must raise rather than
    let the left-join+null-filter silently drop the newest dates from the panel."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    klines = pl.DataFrame(
        {"ts_ms": [start + d * MS_PER_DAY for d in range(20)], "symbol": ["A"] * 20}
    )
    # rmom stops 10 days before the klines window end -> well beyond the 2d tolerance.
    rmom = pl.DataFrame(
        {"symbol": ["A"] * 10, "day_ts": [start + d * MS_PER_DAY for d in range(10)],
         "residual_momentum": [0.0] * 10}
    )
    with pytest.raises(RuntimeError, match="STALE"):
        _assert_rmom_covers_window(rmom, klines, start_ms=start, root=tmp_path)


def test_assert_rmom_covers_window_allows_small_lag(tmp_path) -> None:
    """A freshly rebuilt rmom legitimately trails the newest kline day by a couple of days
    (precompute shift(3)); within tolerance it must NOT raise."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    klines = pl.DataFrame(
        {"ts_ms": [start + d * MS_PER_DAY for d in range(20)], "symbol": ["A"] * 20}
    )
    rmom = pl.DataFrame(
        {"symbol": ["A"] * 18, "day_ts": [start + d * MS_PER_DAY for d in range(18)],
         "residual_momentum": [0.0] * 18}
    )  # lags by 1 day, within the 2d tolerance
    _assert_rmom_covers_window(rmom, klines, start_ms=start, root=tmp_path)  # no raise


def test_build_continuous_panel_raises_on_stale_rmom(tmp_path) -> None:
    """End-to-end: a root whose rmom is stale relative to klines must error in the panel build
    instead of returning a date-truncated panel (the 2026-06-03 silent-truncation hazard)."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=8, n_bars=720)
    # Truncate residual_momentum to the first 10 days only, then build a window that needs more.
    rmom = pl.read_parquet(root / "residual_momentum.parquet")
    keep_max = start + 9 * MS_PER_DAY
    rmom.filter(pl.col("ts_ms") <= keep_max).write_parquet(root / "residual_momentum.parquet")
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY)
    )

    with pytest.raises(RuntimeError, match="STALE"):
        build_continuous_panel(root, cfg, cache=False)


def test_build_continuous_panel_rejects_legacy_rmom_without_provenance(tmp_path) -> None:
    root, start, _ = _build_root(tmp_path, n_symbols=8, n_bars=240)
    path = root / "residual_momentum.parquet"
    pl.read_parquet(path).drop("is_provisional").write_parquet(path)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY),
        end_date=_iso(start + 9 * MS_PER_DAY),
    )

    with pytest.raises(RuntimeError, match="is_provisional provenance"):
        build_continuous_panel(root, cfg, cache=False)


# pit-signals-5: singleton cross-section must rank the lone candidate at 0, not drop it
def _decile_input_one_symbol_on_a_day() -> pl.DataFrame:
    """k with the feature columns cross_sectional_decile needs; one symbol on one ts_ms group."""
    return pl.DataFrame(
        {
            "symbol": ["LONE"],
            "ts_ms": [3 * MS_PER_HOUR],
            "turnover_quote": [1_000_000.0],
            "ret168": [0.01],
            "ret72": [0.0],
            "rv_168h": [0.02],
            "vov": [0.001],
            "dist_low": [0.5],
        }
    )


def test_cross_sectional_decile_keeps_singleton_group() -> None:
    """A ts_ms group that collapses to ONE surviving symbol must still emit it (ranked 0), not be
    dropped by a 0/0=NaN rank denominator. Pre-fix the (len-1)=0 division yielded NaN and
    filter(_rr <= q) silently dropped the lone candidate."""
    k = _decile_input_one_symbol_on_a_day()
    day = (int(k["ts_ms"][0]) // MS_PER_DAY) * MS_PER_DAY
    rmom = pl.DataFrame({"symbol": ["LONE"], "day_ts": [day], "residual_momentum": [-0.005]})
    out = cross_sectional_decile(k, rmom, rmom_quantile=0.5)
    assert out.height == 1
    assert out["symbol"][0] == "LONE"
    assert out["composite"][0] == pytest.approx(0.0)  # lone candidate ranks at 0, not NaN


# code-quality-6: _additive_summary delegates headline metrics to _daily_pnl_metrics
def _toy_trades() -> pl.DataFrame:
    base = {
        "gross_return": 0.02, "cost_return": -0.001, "funding_return": 0.0005,
        "funding_mode": "modeled", "net_return": 0.0195, "entry_ts_ms": MS_PER_DAY,
    }
    rows = []
    for d in range(1, 6):
        r = dict(base)
        r["exit_ts_ms"] = d * MS_PER_DAY
        r["entry_ts_ms"] = (d - 1) * MS_PER_DAY + MS_PER_HOUR
        r["net_return"] = 0.01 if d % 2 else -0.005
        r["gross_return"] = r["net_return"] + 0.001
        rows.append(r)
    return pl.DataFrame(rows)


def test_additive_summary_headline_metrics_match_shared_helper() -> None:
    """The headline DD/MAR/Sharpe/return block in _additive_summary must come from the single
    _daily_pnl_metrics helper (no divergent second copy). Compare against the helper applied to
    the same equity frame."""
    from liquidity_migration.continuous_events import _additive_equity

    trades = _toy_trades()
    cfg = ContinuousEventConfig(split_date=_iso(2 * MS_PER_DAY + MS_PER_HOUR))
    summary = _additive_summary(trades, cfg)
    expected = _daily_pnl_metrics(_additive_equity(trades))
    for key in ("total_return", "annualized_return", "max_drawdown", "mar", "sharpe_like",
                "worst_day_return"):
        assert summary[key] == expected[key], key


def test_additive_summary_empty_trades_returns_zeroed_headline() -> None:
    """Empty-trades path still returns the same headline keys (no KeyError, no divergent shape)."""
    summary = _additive_summary(pl.DataFrame(schema={
        "gross_return": pl.Float64, "cost_return": pl.Float64, "funding_return": pl.Float64,
        "net_return": pl.Float64, "entry_ts_ms": pl.Int64, "exit_ts_ms": pl.Int64,
    }), ContinuousEventConfig())
    assert summary["n_trades"] == 0
    assert summary["total_return"] == 0.0
    assert summary["mar"] is None


# pit-engine-2: backtest age gate uses authoritative PIT listing, not the clamped window start
def test_listing_ts_by_symbol_reads_first_ever_bar(tmp_path) -> None:
    root, start, n_bars = _build_root(tmp_path, n_symbols=3, n_bars=50)
    listing = _listing_ts_by_symbol(root)
    assert listing["S00"] == start  # first-ever bar, independent of any window


def test_age_gate_uses_listing_not_window_start() -> None:
    """A symbol whose authoritative listing is far older than the loaded window start must NOT be
    age-gated, even though the window's first loaded bar is recent. Pre-fix the gate measured age
    from bars[...][0] (the clamped window start) and wrongly skipped old symbols near the edge."""
    # The loaded window's first bar is at ts 0; the symbol's TRUE listing is 100 days earlier.
    bars = _indexed_price_bars_by_symbol(_grid_klines(["OLD"], 60))
    true_listing = -100 * MS_PER_DAY  # listed long before the loaded window
    entries = pl.DataFrame(
        {"symbol": ["OLD"], "ts_ms": [2 * MS_PER_HOUR], "composite": [0.9],
         "turnover_quote": [1e6], "spell_end_ts": [2 * MS_PER_HOUR]}
    )
    cfg = ContinuousEventConfig(
        age_days_min=30, max_active=5, hold_hours=1, entry_delay_hours=1,
        use_funding=False, flat_round_trip_bps=0.0,
    )
    # With authoritative listing 100d old -> NOT age-gated -> one trade.
    trades_pass, _ = _run_trades(
        entries, bars, None, cfg, listing_ts_by_symbol={"OLD": true_listing}
    )
    assert trades_pass.height == 1, "old symbol must clear the age floor on its true listing"

    # Control: without a listing source, the gate falls back to the window's first bar (ts 0),
    # which is < 30d old at the entry -> skipped. This is the pre-fix (wrong-for-old-symbols)
    # behaviour, retained only as the no-listing fallback.
    trades_fallback, _ = _run_trades(entries, bars, None, cfg, listing_ts_by_symbol=None)
    assert trades_fallback.height == 0


# candidate_sink / candidate_tape and _apply_entry_order invariants
def test_apply_entry_order_fcfs_is_identity() -> None:
    """fcfs must be a no-op reorder (reproduces the frozen control's (ts, symbol) order)."""
    entries = pl.DataFrame(
        {"symbol": ["B", "A", "C"], "ts_ms": [0, 0, 0], "composite": [0.1, 0.9, 0.5],
         "turnover_quote": [1e6, 1e6, 1e6], "spell_end_ts": [0, 0, 0]}
    )
    out = _apply_entry_order(entries, "fcfs")
    assert out["symbol"].to_list() == ["B", "A", "C"]  # untouched


def test_apply_entry_order_is_causal_within_ts() -> None:
    """Reordering only swaps SAME-ts candidates; a later ts can never jump ahead of an earlier one."""
    entries = pl.DataFrame(
        {"symbol": ["A", "B", "C", "D"], "ts_ms": [0, 0, MS_PER_HOUR, MS_PER_HOUR],
         "composite": [0.1, 0.9, 0.2, 0.8], "turnover_quote": [1e6] * 4,
         "spell_end_ts": [0, 0, MS_PER_HOUR, MS_PER_HOUR]}
    )
    out = _apply_entry_order(entries, "composite")
    ts = out["ts_ms"].to_list()
    assert ts == sorted(ts), "timestamps must stay non-decreasing (causal)"
    # within ts=0, highest composite first: B before A
    first_ts = out.filter(pl.col("ts_ms") == 0)["symbol"].to_list()
    assert first_ts == ["B", "A"]


def test_candidate_tape_selected_rows_match_executed_trades(tmp_path) -> None:
    """The candidate-tape audit contract: selected rows must equal executed trades
    (same-code reconstruction). Pre-existing finding had NO positive test for this."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, cooldown_hours=24, entry_delay_hours=1, max_active=10, use_funding=False,
    )
    tape_path = tmp_path / "tape.parquet"
    payload = run_continuous_event_research(root, config=cfg, candidate_tape_path=tape_path)
    tape = pl.read_parquet(tape_path)
    assert tape.height >= 1
    required = {
        "signal_bar_close_ts_ms",
        "decision_ts_ms",
        "feature_ts_ms",
        "data_available_ts_ms",
        "rmom_source_day_ts_ms",
        "rmom_data_available_ts_ms",
        "order_submit_ts_ms",
        "fill_window_start_ts_ms",
        "fill_window_end_ts_ms",
        "residual_momentum_value",
        "residual_momentum_rank",
        "feature_rv_168h",
        "feature_vov",
        "feature_dist_low",
        "feature_xsret7",
        "feature_xsret3",
        "liquidity_value",
        "liquidity_rank",
        "volume_24h_quote",
        "volume_zscore",
        "sizing_mode",
        "base_notional_weight",
        "entry_volatility",
        "inverse_vol_multiplier",
        "external_size_multiplier",
    }
    assert required <= set(tape.columns)
    assert (tape["signal_bar_close_ts_ms"] == tape["decision_ts_ms"]).all()
    assert (tape["feature_ts_ms"] <= tape["decision_ts_ms"]).all()
    assert (tape["data_available_ts_ms"] <= tape["decision_ts_ms"]).all()
    assert (tape["rmom_data_available_ts_ms"] <= tape["decision_ts_ms"]).all()
    assert (tape["order_submit_ts_ms"] >= tape["decision_ts_ms"]).all()
    assert (tape["fill_window_start_ts_ms"] == tape["order_submit_ts_ms"]).all()
    assert (tape["fill_window_end_ts_ms"] == tape["order_submit_ts_ms"]).all()
    selected = tape.filter(pl.col("selected"))
    assert selected.height == payload["n_trades"] == payload["n_candidates_selected"]
    assert selected["notional_weight"].is_not_null().all()
    assert selected["entry_volatility"].is_not_null().all()
    # Every non-selected row carries a concrete rejection reason (never the "selected" sentinel).
    rejected = tape.filter(~pl.col("selected"))
    assert (rejected["reason"] != "selected").all()


def test_candidate_tape_none_path_is_additive(tmp_path) -> None:
    """With candidate_tape_path=None the run must be unchanged (n_trades identical) — the audit
    hook is purely additive."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, cooldown_hours=24, entry_delay_hours=1, max_active=10, use_funding=False,
    )
    no_tape = run_continuous_event_research(root, config=cfg)
    tape_path = tmp_path / "t.parquet"
    with_tape = run_continuous_event_research(root, config=cfg, candidate_tape_path=tape_path)
    assert no_tape["n_trades"] == with_tape["n_trades"]


def test_entry_order_composite_preserves_trade_set_when_capacity_non_binding(tmp_path) -> None:
    """When max_active does NOT bind, reordering candidates within a ts must yield the SAME executed
    trade SET as fcfs (only the order changed, not which names trade). A reorder that dropped or
    duplicated candidates would break the constant-breadth claim."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    common = dict(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, cooldown_hours=24, entry_delay_hours=1, max_active=10_000,
        use_funding=False,  # capacity non-binding
    )
    fcfs = run_continuous_event_research(root, config=ContinuousEventConfig(**common), entry_order="fcfs")
    comp = run_continuous_event_research(root, config=ContinuousEventConfig(**common), entry_order="composite")
    assert fcfs["n_trades"] == comp["n_trades"]


# cost-funding-5: a realistic SNAPSHOT scrape (rate constant within the true settlement
# interval, changing across settlements) would over-charge a short book under naive
# exact-stamp dedup. Detection is now DATA-INTRINSIC (rate-change cadence, not the stale
# declared funding_interval_min). Uncorrected -> raise; corrected by the derived interval
# -> collapsed; genuine sub-8h settlements -> NEVER flagged (the 2026-06-15 false-positive).
def test_assert_funding_one_per_settlement_rejects_uncorrected_hourly_snapshot(tmp_path) -> None:
    """A realistic hourly snapshot of an 8h-settling symbol (rate constant within each 8h
    block) must raise when no collapsing interval is supplied — exact-stamp dedup would
    over-charge ~8x."""
    hour = MS_PER_HOUR
    # 6 settlements x 8 hourly snapshots each; rate changes only across the 8h blocks.
    rates = [0.001 + 0.0001 * (h // 8) for h in range(48)]
    funding = pl.DataFrame(
        {"symbol": ["AAA"] * 48, "ts_ms": [h * hour for h in range(48)],
         "funding_rate": rates, "funding_interval_min": [480] * 48}
    )
    with pytest.raises(RuntimeError, match="SNAPSHOT"):
        _assert_funding_one_per_settlement(funding, root=tmp_path)
    # ...but with the derived collapsing interval applied, it is corrected -> no raise.
    intervals = derive_funding_interval_min(funding)
    assert intervals["AAA"] == 480  # the true settlement interval, recovered from rate changes
    _assert_funding_one_per_settlement(funding, root=tmp_path, interval_by_symbol=intervals)


def test_assert_funding_does_not_flag_genuine_sub_8h_settlements(tmp_path) -> None:
    """Genuine sub-8h alts (rate changes every settlement) with the venue's stale declared
    8h interval must NOT be flagged — this was the 2026-06-15 false-positive that blocked
    the bybit continuous backtest. BERA settles every 2h; rate changes every stamp."""
    hour = MS_PER_HOUR
    bera = pl.DataFrame(
        {"symbol": ["BERA"] * 24, "ts_ms": [h * 2 * hour for h in range(24)],
         "funding_rate": [0.0001 * i for i in range(24)], "funding_interval_min": [480] * 24}
    )
    _assert_funding_one_per_settlement(bera, root=tmp_path)  # no raise (genuine 2h)
    assert derive_funding_interval_min(bera)["BERA"] == 120


def test_assert_funding_one_per_settlement_accepts_one_per_settlement(tmp_path) -> None:
    """One row per 8h settlement (declared 8h) is the canonical shape — must NOT raise. A real
    4h-settling alt whose row WRONGLY declares 8h must also pass (it is legitimate
    one-per-settlement data; bucketing by the stored interval was the 4h-undercount bug)."""
    hour = MS_PER_HOUR
    ok_8h = pl.DataFrame(
        {"symbol": ["AAA"] * 12, "ts_ms": [h * hour for h in range(0, 96, 8)],
         "funding_rate": [0.001 * i for i in range(12)], "funding_interval_min": [480] * 12}
    )
    _assert_funding_one_per_settlement(ok_8h, root=tmp_path)  # no raise
    real_4h_wrong_declared = pl.DataFrame(
        {"symbol": ["BBB"] * 24, "ts_ms": [h * hour for h in range(0, 96, 4)],
         "funding_rate": [0.001 * i for i in range(24)], "funding_interval_min": [480] * 24}  # settles 4h
    )
    _assert_funding_one_per_settlement(real_4h_wrong_declared, root=tmp_path)  # no raise


def test_funding_research_run_self_corrects_snapshot_root(tmp_path) -> None:
    """End-to-end: a root whose funding is a realistic hourly snapshot scrape must NOT crash
    the research run — the engine derives the true settlement interval and collapses the
    snapshots to one charge per settlement (no over-charge), rather than rejecting."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720, funding_8h=False)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 20 * MS_PER_DAY),
        hold_hours=6, cooldown_hours=24, entry_delay_hours=1, max_active=10, use_funding=True,
    )
    result = run_continuous_event_research(root, config=cfg)  # completes, self-corrected
    assert result is not None


# test-gaps-4: live continuous rmom day-floor join is day-D <- rmom[D] (no off-by-one)
def test_decile_join_attaches_decision_day_rmom_not_next_day() -> None:
    """The day-floor join must attach residual_momentum[D] to a day-D panel row, NEVER rmom[D+1].
    A one-day misalignment would pull a future rmom onto the decision day (a live look-ahead).

    Construction: two symbols, two days, one ts_ms each. rmom designates the LOW symbol per day —
    A low on day0, B low on day1. With rmom_quantile=0.5 the low symbol survives. If the join were
    off-by-one, day1 would keep A (rmom[day0]'s low) instead of B (rmom[day1]'s low)."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    day0, day1 = start, start + MS_PER_DAY
    ts0, ts1 = day0 + 3 * MS_PER_HOUR, day1 + 3 * MS_PER_HOUR
    feat = {"ret168": 0.0, "ret72": 0.0, "rv_168h": 0.01, "vov": 0.001, "dist_low": 0.5}
    k = pl.DataFrame(
        [
            {"symbol": "A", "ts_ms": ts0, "turnover_quote": 1e6, **feat},
            {"symbol": "B", "ts_ms": ts0, "turnover_quote": 1e6, **feat},
            {"symbol": "A", "ts_ms": ts1, "turnover_quote": 1e6, **feat},
            {"symbol": "B", "ts_ms": ts1, "turnover_quote": 1e6, **feat},
        ]
    )
    # Per-day-VARYING rmom: A is the low (gets selected) on day0; B is the low on day1.
    rmom = pl.DataFrame(
        [
            {"symbol": "A", "day_ts": day0, "residual_momentum": -0.01},  # low on day0
            {"symbol": "B", "day_ts": day0, "residual_momentum": 0.01},
            {"symbol": "A", "day_ts": day1, "residual_momentum": 0.01},
            {"symbol": "B", "day_ts": day1, "residual_momentum": -0.01},  # low on day1
        ]
    )
    out = cross_sectional_decile(k, rmom, rmom_quantile=0.5)
    sel_day0 = set(out.filter(pl.col("ts_ms") == ts0)["symbol"].to_list())
    sel_day1 = set(out.filter(pl.col("ts_ms") == ts1)["symbol"].to_list())
    assert sel_day0 == {"A"}, "day0 must select the symbol rmom[day0] marks low"
    assert sel_day1 == {"B"}, "day1 must select rmom[day1]'s low, NOT rmom[day0]'s (off-by-one)"
