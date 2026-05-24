from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import polars as pl

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.momentum_signals import (
    MomentumSignalsConfig,
    RANKER_SHARPE,
    _clenow_slope_r2,
    add_btc_regime,
    add_coil_release,
    add_funding_overheat,
    add_liquidity_tier,
    add_prior_high,
    add_returns_and_age,
    add_realized_vol,
    add_sma,
    add_true_range_and_atr,
    build_momentum_features,
    daily_bars,
)


START_MS = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _make_klines(
    *,
    symbols: list[str],
    n_hours: int,
    base_prices: list[float] | None = None,
    log_trend_per_day: list[float] | None = None,
    turnover_per_hour: list[float] | None = None,
    start_ms: int = START_MS,
) -> pl.DataFrame:
    """Synthetic 1h klines for `len(symbols)` symbols over `n_hours`.

    Closes follow exp(log_trend_per_day[i] * t_days). High/low ±0.1% from close.
    """
    base_prices = base_prices or [100.0] * len(symbols)
    log_trend_per_day = log_trend_per_day or [0.0] * len(symbols)
    turnover_per_hour = turnover_per_hour or [1_000_000.0] * len(symbols)
    rows: list[dict[str, float | int | str]] = []
    for i, symbol in enumerate(symbols):
        slope = log_trend_per_day[i] / 24.0
        base = base_prices[i]
        for h in range(n_hours):
            ts_ms = start_ms + h * MS_PER_HOUR
            close = base * math.exp(slope * h)
            open_ = base * math.exp(slope * max(h - 1, 0)) if h > 0 else close
            high = max(open_, close) * 1.001
            low = min(open_, close) * 0.999
            rows.append(
                {
                    "ts_ms": ts_ms,
                    "symbol": symbol,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume_base": turnover_per_hour[i] / close,
                    "turnover_quote": turnover_per_hour[i],
                }
            )
    return pl.DataFrame(rows).sort(["symbol", "ts_ms"])


def test_clenow_slope_r2_perfect_trend():
    n = 200
    log_close = 0.01 * np.arange(n)
    close = np.exp(log_close)
    scores = _clenow_slope_r2(close, lookback=90)
    assert np.isnan(scores[:89]).all()
    expected = math.exp(0.01 * 365.0) - 1.0
    np.testing.assert_allclose(scores[89:], expected, rtol=1e-6)


def test_clenow_slope_r2_flat_close_is_nan():
    close = np.ones(120)
    scores = _clenow_slope_r2(close, lookback=90)
    assert np.isnan(scores).all()


def test_clenow_slope_r2_short_input_is_nan():
    close = np.exp(0.01 * np.arange(50))
    scores = _clenow_slope_r2(close, lookback=90)
    assert np.isnan(scores).all()


def test_clenow_slope_r2_downtrend_is_negative():
    close = np.exp(-0.02 * np.arange(150))
    scores = _clenow_slope_r2(close, lookback=90)
    assert (scores[89:] < 0).all()
    expected = math.exp(-0.02 * 365.0) - 1.0
    np.testing.assert_allclose(scores[89:], expected, rtol=1e-6)


def test_daily_bars_resamples_full_hours():
    klines = _make_klines(symbols=["AAA", "BBB"], n_hours=48)
    daily = daily_bars(klines)
    assert daily.height == 4
    assert set(daily["symbol"].unique()) == {"AAA", "BBB"}
    aaa = daily.filter(pl.col("symbol") == "AAA").sort("ts_ms")
    assert aaa["hourly_bars"].to_list() == [24, 24]
    assert aaa["ts_ms"][0] == START_MS + MS_PER_DAY


def test_daily_bars_drops_partial_day():
    klines = _make_klines(symbols=["AAA"], n_hours=30)
    daily = daily_bars(klines, min_hourly_bars=20)
    assert daily.height == 1


def test_add_returns_and_age_sets_first_return_null():
    klines = _make_klines(symbols=["AAA"], n_hours=72, log_trend_per_day=[0.01])
    daily = add_returns_and_age(daily_bars(klines))
    daily_aaa = daily.sort("ts_ms")
    assert daily_aaa["log_return"][0] is None
    assert math.isclose(daily_aaa["log_return"][1], 0.01, rel_tol=1e-6, abs_tol=1e-6)
    assert daily_aaa["symbol_age_days"].to_list() == [1, 2, 3]


def test_add_realized_vol_picks_window():
    n_hours = 24 * 40
    klines = _make_klines(symbols=["AAA"], n_hours=n_hours, log_trend_per_day=[0.005])
    daily = add_returns_and_age(daily_bars(klines))
    daily = add_realized_vol(daily, window_days=30)
    vol = daily.sort("ts_ms")["realized_vol_30d"].to_list()
    # First ~30 rows null (need 30 finite log_returns, and row 0 has a null one).
    assert vol[0] is None
    assert vol[-1] is not None and vol[-1] >= 0.0


def test_add_sma_aligns_with_close():
    klines = _make_klines(symbols=["AAA"], n_hours=24 * 5)
    daily = add_returns_and_age(daily_bars(klines))
    daily = add_sma(daily, window_days=3)
    sma = daily.sort("ts_ms")["sma_3d"].to_list()
    assert sma[0] is None and sma[1] is None
    assert sma[2] is not None


def test_add_prior_high_excludes_today():
    klines = _make_klines(symbols=["AAA"], n_hours=24 * 5, log_trend_per_day=[0.05])
    daily = add_returns_and_age(daily_bars(klines))
    daily = add_prior_high(daily, window_days=2)
    sorted_daily = daily.sort("ts_ms")
    prior = sorted_daily["prior_high_2d"].to_list()
    # day 0, 1 → null (no prior 2-day window). day 2 looks at days 0,1.
    assert prior[0] is None and prior[1] is None
    assert prior[2] is not None
    # day 2's prior_high == max(high of day 0, high of day 1)
    expected = max(sorted_daily["high"][0], sorted_daily["high"][1])
    assert math.isclose(prior[2], expected, rel_tol=1e-6)


def test_atr_present_after_window():
    klines = _make_klines(symbols=["AAA"], n_hours=24 * 8, log_trend_per_day=[0.01])
    daily = add_returns_and_age(daily_bars(klines))
    daily = add_true_range_and_atr(daily, window_days=5)
    atr = daily.sort("ts_ms")["atr_5d"].to_list()
    assert atr[0] is None
    assert atr[-1] is not None and atr[-1] > 0.0


def test_liquidity_tier_selects_top_n_by_median_turnover():
    n_hours = 24 * 200
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    turnover = [1_000_000.0, 2_000_000.0, 500_000.0, 3_000_000.0]
    klines = _make_klines(symbols=symbols, n_hours=n_hours, turnover_per_hour=turnover)
    daily = add_returns_and_age(daily_bars(klines))
    config = MomentumSignalsConfig(
        liquidity_tier_size=2,
        liquidity_volume_window_days=30,
        min_listing_history_days=30,
    )
    daily = add_liquidity_tier(daily, config=config)
    last_day = daily.filter(pl.col("ts_ms") == daily["ts_ms"].max())
    in_tier = last_day.filter(pl.col("in_liquidity_tier"))["symbol"].to_list()
    assert set(in_tier) == {"DDD", "BBB"}


def test_liquidity_tier_age_filter_excludes_young_symbol():
    n_hours = 24 * 100
    klines = _make_klines(symbols=["AAA", "BBB"], n_hours=n_hours)
    daily = add_returns_and_age(daily_bars(klines))
    config = MomentumSignalsConfig(
        liquidity_tier_size=10,
        liquidity_volume_window_days=30,
        min_listing_history_days=200,  # > available history
    )
    daily = add_liquidity_tier(daily, config=config)
    assert not daily["in_liquidity_tier"].any()


def test_cross_sectional_rank_normalized_within_eligible():
    n_hours = 24 * 200
    symbols = ["AAA", "BBB", "CCC"]
    klines = _make_klines(
        symbols=symbols,
        n_hours=n_hours,
        log_trend_per_day=[0.001, 0.005, 0.010],
        turnover_per_hour=[1_000_000.0, 1_000_000.0, 1_000_000.0],
    )
    config = MomentumSignalsConfig(
        liquidity_tier_size=10,
        liquidity_volume_window_days=30,
        min_listing_history_days=30,
        ranker_lookback_days=90,
    )
    features = build_momentum_features(klines, config=config)
    last_day = features.filter(pl.col("ts_ms") == features["ts_ms"].max()).sort("rank_norm")
    eligible = last_day.filter(pl.col("rank_norm").is_not_null()).sort("rank_norm")
    # Best ranker should be CCC (strongest trend); worst AAA.
    assert eligible["symbol"].to_list() == ["AAA", "BBB", "CCC"]
    assert math.isclose(eligible["rank_norm"][0], 0.0)
    assert math.isclose(eligible["rank_norm"][-1], 1.0)


def test_btc_regime_propagates_per_date():
    n_hours = 24 * 250
    klines = _make_klines(
        symbols=["BTCUSDT", "AAA"],
        n_hours=n_hours,
        log_trend_per_day=[0.01, -0.01],
    )
    daily = add_returns_and_age(daily_bars(klines))
    config = MomentumSignalsConfig(sma_regime_days=50, regime_symbol="BTCUSDT")
    daily = add_btc_regime(daily, config=config)
    last_day = daily.filter(pl.col("ts_ms") == daily["ts_ms"].max())
    # BTC is uptrending — all rows on the last day should have regime_on True.
    assert last_day["regime_on"].all()


def test_btc_regime_off_when_btc_under_sma():
    n_hours = 24 * 250
    klines = _make_klines(
        symbols=["BTCUSDT", "AAA"],
        n_hours=n_hours,
        log_trend_per_day=[-0.01, 0.0],
    )
    daily = add_returns_and_age(daily_bars(klines))
    config = MomentumSignalsConfig(sma_regime_days=50, regime_symbol="BTCUSDT")
    daily = add_btc_regime(daily, config=config)
    last_day = daily.filter(pl.col("ts_ms") == daily["ts_ms"].max())
    assert not last_day["regime_on"].any()


def test_funding_overheat_fires_on_spike():
    n_days = 200
    n_hours = 24 * n_days
    klines = _make_klines(symbols=["AAA"], n_hours=n_hours)
    daily = add_returns_and_age(daily_bars(klines))
    # Build synthetic funding: low rate everywhere, then a single spike on day 150.
    funding_rows: list[dict[str, float | int | str]] = []
    for d in range(n_days):
        for h in (0, 8, 16):
            ts_ms = START_MS + d * MS_PER_DAY + h * MS_PER_HOUR
            rate = 0.0001 if d != 150 else 0.05
            funding_rows.append(
                {"ts_ms": ts_ms, "symbol": "AAA", "funding_rate_8h_equiv": rate}
            )
    funding = pl.DataFrame(funding_rows)
    config = MomentumSignalsConfig(
        funding_overheat_window_days=90, funding_overheat_percentile=0.95
    )
    daily = add_funding_overheat(daily, funding, config=config)
    spike_day_ms = START_MS + 150 * MS_PER_DAY + MS_PER_DAY  # day-end of day 150
    row = daily.filter((pl.col("symbol") == "AAA") & (pl.col("ts_ms") == spike_day_ms))
    assert row["funding_overheat"][0] is True


def test_funding_overheat_returns_false_when_funding_missing():
    klines = _make_klines(symbols=["AAA"], n_hours=24 * 5)
    daily = add_returns_and_age(daily_bars(klines))
    config = MomentumSignalsConfig()
    daily_no_fund = add_funding_overheat(daily, None, config=config)
    assert not daily_no_fund["funding_overheat"].any()
    daily_empty_fund = add_funding_overheat(daily, pl.DataFrame(), config=config)
    assert not daily_empty_fund["funding_overheat"].any()


def test_coil_release_fires_on_first_expansion_after_compression():
    # Build a synthetic daily series where realized_vol_30d compresses below
    # realized_vol_90d for 10 days, then expands above.
    n_days = 200
    # Use a high-vol regime for first 60 days, low-vol for days 60..150, high-vol after.
    # That makes 30d vol cross under 90d after day ~150 of low-vol, then back over.
    # For simplicity, we directly construct daily bars with controlled log_returns.
    daily_rows = []
    log_close = 0.0
    for d in range(n_days):
        if d < 100:
            ret = 0.04 * (1 if d % 2 == 0 else -1)  # high vol
        elif 100 <= d < 150:
            ret = 0.005 * (1 if d % 2 == 0 else -1)  # low vol (compression)
        else:
            ret = 0.05 * (1 if d % 2 == 0 else -1)  # high vol again (release)
        log_close += ret
        close = math.exp(log_close)
        daily_rows.append(
            {
                "ts_ms": START_MS + (d + 1) * MS_PER_DAY,
                "date": "",
                "symbol": "AAA",
                "open": close,
                "high": close * 1.001,
                "low": close * 0.999,
                "close": close,
                "hourly_bars": 24,
                "volume_base": 1.0,
                "turnover_quote": 1.0,
            }
        )
    daily = pl.DataFrame(daily_rows)
    daily = add_returns_and_age(daily)
    daily = add_realized_vol(daily, window_days=30)
    daily = add_realized_vol(daily, window_days=90)
    config = MomentumSignalsConfig(
        vol_short_window_days=30,
        vol_long_window_days=90,
        coil_release_min_compress_days=5,
    )
    daily = add_coil_release(daily, config=config)
    fires = daily.filter(pl.col("coil_release_event"))
    # Must fire at least once during the high-vol expansion (day >= 150).
    assert fires.height >= 1
    fire_days = fires["ts_ms"].to_list()
    earliest_fire = min(fire_days)
    expansion_start_ms = START_MS + 150 * MS_PER_DAY
    assert earliest_fire >= expansion_start_ms


def test_build_momentum_features_smoke():
    n_hours = 24 * 250
    symbols = ["BTCUSDT", "AAA", "BBB"]
    klines = _make_klines(
        symbols=symbols,
        n_hours=n_hours,
        log_trend_per_day=[0.005, 0.01, -0.005],
        turnover_per_hour=[5_000_000.0, 2_000_000.0, 1_000_000.0],
    )
    config = MomentumSignalsConfig(
        liquidity_tier_size=5,
        liquidity_volume_window_days=30,
        min_listing_history_days=30,
        ranker_lookback_days=90,
        sma_regime_days=50,
    )
    features = build_momentum_features(klines, funding=None, config=config)
    required_cols = {
        "ts_ms",
        "date",
        "symbol",
        "close",
        "log_return",
        "symbol_age_days",
        "in_liquidity_tier",
        "realized_vol_30d",
        "realized_vol_90d",
        "sma_100d",
        "prior_high_60d",
        "atr_30d",
        "clenow_score",
        "rank_norm",
        "regime_on",
        "funding_overheat",
        "coil_release_event",
    }
    assert required_cols.issubset(set(features.columns))
    # Ranker is finite for some non-BTC rows at the end.
    last_day = features.filter(pl.col("ts_ms") == features["ts_ms"].max())
    assert last_day.filter(pl.col("clenow_score").is_finite()).height >= 1


def test_sharpe_ranker_runs_end_to_end():
    n_hours = 24 * 200
    klines = _make_klines(
        symbols=["AAA", "BBB"],
        n_hours=n_hours,
        log_trend_per_day=[0.01, 0.005],
    )
    config = MomentumSignalsConfig(
        ranker=RANKER_SHARPE,
        ranker_lookback_days=60,
        liquidity_tier_size=5,
        liquidity_volume_window_days=30,
        min_listing_history_days=30,
    )
    features = build_momentum_features(klines, config=config)
    assert "sharpe_score" in features.columns
    last_day = features.filter(pl.col("ts_ms") == features["ts_ms"].max())
    assert last_day.filter(pl.col("sharpe_score").is_finite()).height >= 1
