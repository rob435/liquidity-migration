"""Correctness tests for the reversion_alpha three-layer stack.

The execution simulator is the part most at risk of silent accounting bugs,
so the bulk of these tests pin its behaviour with hand-constructed fixtures
where the right answer is known by inspection.
"""
from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.reversion_alpha import (
    ReversionConfig,
    _regime_multiplier,
    _scan_exit,
    compute_reversion_score,
    construct_target_book,
    prepare_klines,
    simulate,
)

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 24 * MS_PER_HOUR


# --------------------------------------------------------------------------
# Layer 1 — alpha
# --------------------------------------------------------------------------

def test_reversion_score_is_cross_sectional_zscore_mean():
    # Two symbols on one day; one is the extreme pump on every sub-signal.
    rows = []
    for sym, resid, turn_ratio, close_loc, prior_rank, rank in [
        ("HOT", 0.20, 50.0, 0.95, 300, 60),   # extreme on all 4
        ("COLD", 0.01, 1.1, 0.20, 65, 60),    # mild on all 4
    ]:
        rows.append({
            "symbol": sym, "date": "2024-01-01", "ts_ms": 0,
            "daily_return_1d": resid, "residual_return_1d": resid,
            "turnover_quote": turn_ratio, "prior7_turnover_quote_mean": 1.0,
            "signal_day_close_location": close_loc,
            "liquidity_rank": rank, "prior7_liquidity_rank": prior_rank,
            "pit_age_days": 400, "daily_close": 10.0,
            "market_median_return_30d_sum": -0.2,
        })
    feats = pl.DataFrame(rows)
    cfg = ReversionConfig(min_universe_size=2, min_alpha_components=4)
    scored = compute_reversion_score(feats, cfg)
    by = {r["symbol"]: r["reversion_score"] for r in scored.iter_rows(named=True)}
    # polars std() is sample std (ddof=1): two symmetric points z-score to +/- 1/sqrt(2).
    # The pump must score strictly higher and the pair must be symmetric.
    assert by["HOT"] > by["COLD"]
    assert by["HOT"] == pytest.approx(2 ** -0.5, abs=1e-9)
    assert by["COLD"] == pytest.approx(-(2 ** -0.5), abs=1e-9)


def test_reversion_score_requires_min_components():
    # only 2 of 4 sub-signals available -> score is null -> row dropped
    feats = pl.DataFrame([{
        "symbol": "AAA", "date": "2024-01-01", "ts_ms": 0,
        "daily_return_1d": 0.1, "residual_return_1d": 0.1,
        "turnover_quote": 5.0, "prior7_turnover_quote_mean": 1.0,
        "signal_day_close_location": None,
        "liquidity_rank": 50, "prior7_liquidity_rank": None,
        "pit_age_days": 400, "daily_close": 10.0,
        "market_median_return_30d_sum": -0.2,
    }] * 1)
    cfg = ReversionConfig(min_universe_size=1, min_alpha_components=3)
    scored = compute_reversion_score(feats, cfg)
    assert scored.is_empty()


def test_tradable_universe_excludes_immature_and_stable():
    rows = [
        {"symbol": "AAAUSDT", "date": "2024-01-01", "ts_ms": 0, "daily_return_1d": 0.1,
         "residual_return_1d": 0.1, "turnover_quote": 5.0, "prior7_turnover_quote_mean": 1.0,
         "signal_day_close_location": 0.8, "liquidity_rank": 50, "prior7_liquidity_rank": 200,
         "pit_age_days": 30, "daily_close": 10.0, "market_median_return_30d_sum": -0.2},  # too young
        {"symbol": "USDCUSDT", "date": "2024-01-01", "ts_ms": 0, "daily_return_1d": 0.1,
         "residual_return_1d": 0.1, "turnover_quote": 5.0, "prior7_turnover_quote_mean": 1.0,
         "signal_day_close_location": 0.8, "liquidity_rank": 50, "prior7_liquidity_rank": 200,
         "pit_age_days": 400, "daily_close": 10.0, "market_median_return_30d_sum": -0.2},  # stable
    ]
    cfg = ReversionConfig(min_universe_size=1)
    scored = compute_reversion_score(pl.DataFrame(rows), cfg)
    assert scored.is_empty()


# --------------------------------------------------------------------------
# Layer 2 — portfolio construction
# --------------------------------------------------------------------------

def test_regime_multiplier_ramp():
    cfg = ReversionConfig(regime_full_at=-0.15, regime_zero_at=0.10)
    assert _regime_multiplier(-0.30, cfg) == pytest.approx(1.0)   # deep bear -> full
    assert _regime_multiplier(0.20, cfg) == pytest.approx(0.0)    # strong bull -> flat
    mid = _regime_multiplier(-0.025, cfg)                         # midpoint
    assert mid == pytest.approx(0.5, abs=1e-6)
    assert _regime_multiplier(None, cfg) == pytest.approx(1.0)    # missing -> no throttle


def test_target_book_weights_sum_to_regime_scaled_gross():
    # 20 names; top decile = 2 selected; deep-bear regime -> full gross
    rows = []
    for i in range(20):
        rows.append({
            "symbol": f"S{i:02d}", "date": "2024-01-01", "ts_ms": 0,
            "daily_return_1d": 0.05, "residual_return_1d": 0.01 * i,
            "turnover_quote": 2.0 + i, "prior7_turnover_quote_mean": 1.0,
            "signal_day_close_location": 0.5, "liquidity_rank": 60,
            "prior7_liquidity_rank": 60 + i, "pit_age_days": 400, "daily_close": 10.0,
            "market_median_return_30d_sum": -0.30,
        })
    cfg = ReversionConfig(min_universe_size=10, select_top_pct=0.10,
                          base_gross=1.0, per_name_cap=1.0, min_alpha_components=4)
    scored = compute_reversion_score(pl.DataFrame(rows), cfg)
    book = construct_target_book(scored, cfg)
    assert book.height == 2  # top decile of 20
    assert book["target_weight"].sum() == pytest.approx(1.0, abs=1e-9)


def test_target_book_per_name_cap_enforced():
    rows = []
    for i in range(30):
        rows.append({
            "symbol": f"S{i:02d}", "date": "2024-01-01", "ts_ms": 0,
            "daily_return_1d": 0.05, "residual_return_1d": 0.01 * i,
            "turnover_quote": 2.0 + i, "prior7_turnover_quote_mean": 1.0,
            "signal_day_close_location": 0.5, "liquidity_rank": 60,
            "prior7_liquidity_rank": 60 + i, "pit_age_days": 400, "daily_close": 10.0,
            "market_median_return_30d_sum": -0.30,
        })
    cfg = ReversionConfig(min_universe_size=10, select_top_pct=0.20,
                          base_gross=1.0, per_name_cap=0.25, min_alpha_components=4)
    scored = compute_reversion_score(pl.DataFrame(rows), cfg)
    book = construct_target_book(scored, cfg)
    assert book["target_weight"].max() <= 0.25 + 1e-9
    assert book["target_weight"].sum() == pytest.approx(1.0, abs=1e-9)


def test_target_book_regime_zero_skips_day():
    rows = []
    for i in range(20):
        rows.append({
            "symbol": f"S{i:02d}", "date": "2024-01-01", "ts_ms": 0,
            "daily_return_1d": 0.05, "residual_return_1d": 0.01 * i,
            "turnover_quote": 2.0 + i, "prior7_turnover_quote_mean": 1.0,
            "signal_day_close_location": 0.5, "liquidity_rank": 60,
            "prior7_liquidity_rank": 60 + i, "pit_age_days": 400, "daily_close": 10.0,
            "market_median_return_30d_sum": 0.50,  # raging alt bull
        })
    cfg = ReversionConfig(min_universe_size=10, min_alpha_components=4)
    scored = compute_reversion_score(pl.DataFrame(rows), cfg)
    book = construct_target_book(scored, cfg)
    assert book.is_empty()  # regime multiplier 0 -> no book


# --------------------------------------------------------------------------
# Layer 3 — execution simulator
# --------------------------------------------------------------------------

def test_scan_exit_take_profit():
    cfg = ReversionConfig(stop_pct=0.12, take_profit_pct=0.26, max_hold_hours=72)
    bars = [{"ts_ms": i, "open": 100.0, "high": 101.0, "low": 100.0, "close": 100.0}
            for i in range(5)]
    bars[3]["low"] = 70.0  # short take-profit (74) is touched
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg)
    assert reason == "take_profit"
    assert idx == 3
    assert price == pytest.approx(74.0)


def test_scan_exit_stop():
    cfg = ReversionConfig(stop_pct=0.12, take_profit_pct=0.26, max_hold_hours=72)
    bars = [{"ts_ms": i, "open": 100.0, "high": 100.0, "low": 99.0, "close": 100.0}
            for i in range(5)]
    bars[2]["high"] = 120.0  # short stop (112) is touched
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg)
    assert reason == "stop"
    assert idx == 2
    assert price == pytest.approx(112.0)


def test_scan_exit_both_touched_takes_stop():
    cfg = ReversionConfig(stop_pct=0.12, take_profit_pct=0.26, max_hold_hours=72)
    bars = [{"ts_ms": i, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
            for i in range(5)]
    bars[1]["high"] = 130.0   # stop in range
    bars[1]["low"] = 60.0     # take-profit also in range
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg)
    assert reason == "stop"   # conservative: adverse fill wins
    assert price == pytest.approx(112.0)


def test_scan_exit_max_hold():
    cfg = ReversionConfig(stop_pct=0.12, take_profit_pct=0.26, max_hold_hours=3)
    bars = [{"ts_ms": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 - i}
            for i in range(10)]
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg)
    assert reason == "max_hold"
    assert idx == 3
    assert price == pytest.approx(bars[3]["close"])


def _hourly_klines(symbol: str, start_day: str, hours: int, *, price_fn) -> pl.DataFrame:
    """Build flat-ish hourly bars; price_fn(i) returns the close for bar i."""
    start_ms = int(pl.Series([start_day]).str.to_datetime().dt.timestamp("ms")[0])
    rows = []
    for i in range(hours):
        c = price_fn(i)
        rows.append({
            "ts_ms": start_ms + i * MS_PER_HOUR, "symbol": symbol,
            "open": c, "high": c * 1.001, "low": c * 0.999, "close": c,
        })
    return pl.DataFrame(rows)


def test_simulate_single_take_profit_trade():
    # signal date 2024-01-01 -> entry one hour after the signal close (hour 1)
    # price flat 100 until hour 30, then crashes so the short take-profit fills.
    def price_fn(i):
        return 100.0 if i < 30 else 70.0
    klines = _hourly_klines("AAAUSDT", "2024-01-01", 120, price_fn=price_fn)
    book = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1,
                          "target_weight": 1.0, "regime_mult": 1.0}])
    cfg = ReversionConfig(cost_round_trip_bps=30.0, take_profit_pct=0.26, stop_pct=0.12)
    res = simulate(book, klines, cfg)
    trades = res["trades"]
    assert trades.height == 1
    t = trades.to_dicts()[0]
    assert t["exit_reason"] == "take_profit"
    assert t["entry_price"] == pytest.approx(100.0)
    # short gross return = (100 - 74)/100 = 0.26 ; net = 0.26 - 0.003
    assert t["gross_return"] == pytest.approx(0.26, abs=1e-9)
    assert t["net_return"] == pytest.approx(0.26 - 0.003, abs=1e-9)


def test_simulate_entry_timing_is_delay_after_signal_close():
    # `date` is stamped at the signal-close moment, so entry is exactly
    # entry_delay_hours after to_datetime(date). Regression for the +23h
    # off-by-one that entered ~25h late instead of 1h.
    klines = _hourly_klines("AAAUSDT", "2024-01-01", 100, price_fn=lambda i: 100.0)
    book = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1,
                          "target_weight": 1.0, "regime_mult": 1.0}])
    day_ms = int(pl.Series(["2024-01-01"]).str.to_datetime().dt.timestamp("ms")[0])

    res1 = simulate(book, klines, ReversionConfig(entry_delay_hours=1))
    assert res1["trades"].to_dicts()[0]["entry_ts_ms"] == day_ms + 1 * MS_PER_HOUR

    # entry_delay_hours=24 reproduces the legacy (pre-fix) ~1-day-late entry
    res24 = simulate(book, klines, ReversionConfig(entry_delay_hours=24))
    assert res24["trades"].to_dicts()[0]["entry_ts_ms"] == day_ms + 24 * MS_PER_HOUR


def test_simulate_prepared_matches_unprepared():
    # prepare_klines() reuse must give bit-identical results to the internal build.
    def price_fn(i):
        return 100.0 if i < 30 else 70.0
    klines = _hourly_klines("AAAUSDT", "2024-01-01", 120, price_fn=price_fn)
    book = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1,
                          "target_weight": 1.0, "regime_mult": 1.0}])
    cfg = ReversionConfig(cost_round_trip_bps=30.0)
    a = simulate(book, klines, cfg)
    b = simulate(book, klines, cfg, prepared=prepare_klines(klines))
    assert a["metrics"] == b["metrics"]
    assert a["trades"].to_dicts() == b["trades"].to_dicts()
    assert a["equity"].to_dicts() == b["equity"].to_dicts()


def test_simulate_capacity_blocks_extra_entries():
    # three names all signalled the same day, max_active=2 -> only 2 enter
    frames = []
    for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
        frames.append(_hourly_klines(sym, "2024-01-01", 120, price_fn=lambda i: 100.0))
    klines = pl.concat(frames)
    book = pl.DataFrame([
        {"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1, "target_weight": 0.33, "regime_mult": 1.0},
        {"date": "2024-01-01", "symbol": "BBBUSDT", "rank": 2, "target_weight": 0.33, "regime_mult": 1.0},
        {"date": "2024-01-01", "symbol": "CCCUSDT", "rank": 3, "target_weight": 0.33, "regime_mult": 1.0},
    ])
    cfg = ReversionConfig(max_active=2)
    res = simulate(book, klines, cfg)
    assert res["trades"].height == 2
    # the two admitted are the highest-ranked
    assert set(res["trades"]["symbol"].to_list()) == {"AAAUSDT", "BBBUSDT"}


def test_simulate_cooldown_blocks_reentry():
    klines = _hourly_klines("AAAUSDT", "2024-01-01", 24 * 14, price_fn=lambda i: 100.0)
    # same symbol signalled on two dates 3 days apart; cooldown 5d -> 2nd blocked
    book = pl.DataFrame([
        {"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1, "target_weight": 1.0, "regime_mult": 1.0},
        {"date": "2024-01-05", "symbol": "AAAUSDT", "rank": 1, "target_weight": 1.0, "regime_mult": 1.0},
    ])
    cfg = ReversionConfig(max_active=5, cooldown_days=5, max_hold_hours=24)
    res = simulate(book, klines, cfg)
    assert res["trades"].height == 1
    assert res["trades"].to_dicts()[0]["entry_date"] == "2024-01-01"


def test_simulate_equity_compounds_from_net_returns():
    # one clean take-profit trade at full weight -> equity end ~= 1 + net_return
    def price_fn(i):
        return 100.0 if i < 30 else 70.0
    klines = _hourly_klines("AAAUSDT", "2024-01-01", 120, price_fn=price_fn)
    book = pl.DataFrame([{"date": "2024-01-01", "symbol": "AAAUSDT", "rank": 1,
                          "target_weight": 1.0, "regime_mult": 1.0}])
    cfg = ReversionConfig(cost_round_trip_bps=30.0)
    res = simulate(book, klines, cfg)
    equity = res["equity"]
    assert not equity.is_empty()
    # full-weight single trade: terminal equity within rounding of 1 + net_return
    net = res["trades"].to_dicts()[0]["net_return"]
    assert equity["equity"][-1] == pytest.approx(1.0 + net, rel=1e-6)


# --------------------------------------------------------------------------
# WS-1 — adverse stop-fill stress
# --------------------------------------------------------------------------

def test_scan_exit_bar_extreme_fills_at_bar_high():
    # A bar gaps far above the stop trigger. Default mode fills at the trigger
    # (112); the adverse-fill stress fills at the bar high (130) — a worse
    # short loss, which is the point of the stress.
    bars = [{"ts_ms": i, "open": 100.0, "high": 100.0, "low": 99.0, "close": 100.0}
            for i in range(5)]
    bars[2]["high"] = 130.0  # stop trigger is 112; bar high is 130

    cfg_stop = ReversionConfig(stop_pct=0.12, stop_fill_mode="stop")
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg_stop)
    assert (idx, reason) == (2, "stop")
    assert price == pytest.approx(112.0)

    cfg_adverse = ReversionConfig(stop_pct=0.12, stop_fill_mode="bar_extreme")
    idx, price, reason = _scan_exit(bars, 0, 100.0, cfg_adverse)
    assert (idx, reason) == (2, "stop")
    assert price == pytest.approx(130.0)   # adverse: filled at the bar high


# --------------------------------------------------------------------------
# WS-3 — hard regime gate
# --------------------------------------------------------------------------

def test_regime_multiplier_hard_gate():
    cfg = ReversionConfig(regime_hard_threshold=-0.05)
    assert _regime_multiplier(-0.10, cfg) == pytest.approx(1.0)   # bear -> trade
    assert _regime_multiplier(-0.05, cfg) == pytest.approx(1.0)   # boundary inclusive
    assert _regime_multiplier(-0.04, cfg) == pytest.approx(0.0)   # not bear -> flat
    assert _regime_multiplier(0.20, cfg) == pytest.approx(0.0)    # bull -> flat
    # the hard gate overrides the continuous ramp endpoints entirely
    cfg2 = ReversionConfig(regime_hard_threshold=-0.05,
                           regime_full_at=-0.15, regime_zero_at=0.10)
    assert _regime_multiplier(-0.08, cfg2) == pytest.approx(1.0)
