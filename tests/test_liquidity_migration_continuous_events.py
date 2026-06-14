"""Tests for the execution-grade continuous-fade engine (liquidity_migration.continuous_events).

Covers: the size/ADV impact cost model, fresh-entry gap + decile + liquid filters, the
concurrency/cooldown gate, funding-to-exit sign, and an end-to-end run on a synthetic full-PIT
root (panel build -> trades -> compounding equity -> artifacts).
"""
from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.continuous_events import (
    ContinuousEventConfig,
    _btc_trend_returns,
    _fresh_entries,
    _panel_cache_path,
    _panel_cache_stale,
    _round_trip_bps,
    _run_trades,
    build_continuous_panel,
    run_continuous_event_research,
)
from liquidity_migration.storage import write_dataset
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
    assert trades["hold_hours"][0] < 10.0


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


def test_run_trades_uptrend_capped_gate_blocks_euphoria_and_downtrend() -> None:
    """V1 (E2 receipt): on iff 0 < trend <= cap — euphoria AND non-uptrend both skip."""
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A", "B", "C"], 100))
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B", "C"],
            "ts_ms": [0, MS_PER_DAY, 2 * MS_PER_DAY],
            "composite": [0.9] * 3,
            "turnover_quote": [1e6] * 3,
        }
    )
    btc_trend = {0: 0.10, MS_PER_DAY: 0.30, 2 * MS_PER_DAY: -0.05}
    cfg = ContinuousEventConfig(
        btc_trend_gate="uptrend_capped",
        btc_trend_euphoria_cap=0.20,
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, skips = _run_trades(entries, bars, None, cfg, btc_trend_daily=btc_trend)
    assert trades.height == 1
    assert trades["symbol"][0] == "A"          # in-band uptrend trades
    assert skips["skipped_btc_trend"] == 2     # euphoria + downtrend both blocked


def test_run_trades_soft3_gate_quarter_sizes_top_quintile_in_downtrend() -> None:
    """V2 (E2 receipt): euphoria off; in-band full size; downtrend = size_frac x notional,
    top-composite-quintile candidates only (within the same signal-ts pool)."""
    bars = _indexed_price_bars_by_symbol(_grid_klines(["A", "B", "C", "D"], 100))
    entries = pl.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "ts_ms": [0, MS_PER_DAY, 2 * MS_PER_DAY, 2 * MS_PER_DAY],
            "composite": [0.9, 0.9, 0.9, 0.5],
            "turnover_quote": [1e6] * 4,
        }
    )
    btc_trend = {0: 0.10, MS_PER_DAY: 0.30, 2 * MS_PER_DAY: -0.05}
    cfg = ContinuousEventConfig(
        btc_trend_gate="soft3",
        btc_trend_euphoria_cap=0.20,
        btc_soft3_size_frac=0.25,
        max_active=5,
        hold_hours=1,
        entry_delay_hours=1,
        use_funding=False,
        flat_round_trip_bps=0.0,
    )
    trades, skips = _run_trades(entries, bars, None, cfg, btc_trend_daily=btc_trend)
    assert trades.height == 2
    by_sym = {t["symbol"]: t for t in trades.to_dicts()}
    assert set(by_sym) == {"A", "C"}           # B = euphoria off; D = below the ts-pool quintile
    assert skips["skipped_btc_trend"] == 1
    assert skips["skipped_soft3_quintile"] == 1
    base_nw = cfg.notional_weight
    assert abs(by_sym["A"]["notional_weight"] - base_nw) < 1e-12          # in-band: full size
    assert abs(by_sym["C"]["notional_weight"] - 0.25 * base_nw) < 1e-12   # downtrend: quarter size


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
    assert trades["exit_reason"][0] == "max_hold"  # planned (state) exit, no stop


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
    rmom_rows = [{"symbol": f"S{s:02d}", "ts_ms": d, "residual_momentum": (s % 13) * 0.001 - 0.006}
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


def test_end_to_end_run_produces_trades_equity_and_artifacts(tmp_path) -> None:
    root, start, n_bars = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY),     # past feature warm-up, well inside the data
        end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, entry_delay_hours=1, max_active=10, use_funding=True,
        split_date=_iso(start + 16 * MS_PER_DAY),
    )
    payload = run_continuous_event_research(root, config=cfg, report_dir=tmp_path / "rep")
    assert payload["run_label"] == "exploratory"
    assert payload["config_hash"]
    # The synthetic cross-section should yield a populated D9 and some fresh trades.
    assert payload["n_trades"] >= 1
    assert "full" in payload["metrics"]
    full = payload["metrics"]["full"]
    assert {"total_return", "max_drawdown", "sharpe_like", "mar"} <= set(full)
    # artifacts written
    assert (tmp_path / "rep" / "continuous_report.json").exists()
    assert (tmp_path / "rep" / "continuous_trades.csv").exists()
    assert (tmp_path / "rep" / "continuous_equity.png").exists()


def test_end_to_end_btc_trend_gate_passes_computed_trend_to_trade_walker(tmp_path) -> None:
    root, start, _ = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=1200, include_btc=True)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 36 * MS_PER_DAY),
        end_date=_iso(start + 45 * MS_PER_DAY),
        hold_hours=6,
        entry_delay_hours=1,
        max_active=10,
        use_funding=False,
        btc_trend_gate="uptrend",
        split_date=_iso(start + 40 * MS_PER_DAY),
    )
    payload = run_continuous_event_research(root, config=cfg, report_dir=tmp_path / "btc_gate_rep")
    assert payload["n_fresh_entries"] > 0
    assert payload["n_trades"] > 0
    assert payload["skips"]["skipped_btc_trend"] < payload["n_fresh_entries"]


def test_end_to_end_age_gate_loads_enough_history_for_age_test(tmp_path) -> None:
    root, start, _ = _build_synthetic_root(tmp_path, n_symbols=26, n_bars=2200)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 70 * MS_PER_DAY),
        end_date=_iso(start + 82 * MS_PER_DAY),
        hold_hours=6,
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
