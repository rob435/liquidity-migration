"""Pin the signal-research harness — Change 4 from the 2026-05-27 plan.

Causality is the only thing that matters in this module: if a feature for
(symbol, date=D) ever uses data from after D's EOD-close, the IC results
from Phase 5 are meaningless. These tests pin:

  * forward returns match the entry+1h fill-model exactly (D's signal trades
    at D+1's first-bar close; exit N days later at D+1+N's first-bar close)
  * each feature is causal at its EOD — explicitly tested on a synthetic
    fixture where a future-only price spike would be detectable if leaked
  * cross-sectional ranks are per-day and dense
  * IC computation recovers a planted edge with the expected sign
  * IC computation does NOT show a spurious edge on uncorrelated noise
  * combined-portfolio weights are signed and top-decile-correct
  * registry has all 20 features the plan listed
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.signal_harness import (
    FEATURE_REGISTRY,
    FeatureContext,
    ICReport,
    _aggregate_daily_klines,
    _attach_daily_returns,
    _attach_forward_returns,
    build_combined_signal_portfolio,
    build_feature_panel,
    compute_univariate_ic,
    resolve_feature_specs,
)

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


# ============================================================================
# Synthetic data fixture
# ============================================================================


def _date_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _make_hourly_klines(
    symbols: list[str],
    start_date: str,
    days: int,
    *,
    seed: int = 0,
    price_paths: dict[str, list[float]] | None = None,
) -> pl.DataFrame:
    """Synthetic hourly klines with deterministic per-symbol price paths.

    If ``price_paths[sym]`` is provided, it must have ``days * 24 + 1`` floats
    representing the close after each hour (the first value is the open of
    hour 0 of day 0). Otherwise a flat-then-drift path is generated.
    """
    import random
    rng = random.Random(seed)
    start_ms = _date_ms(start_date)
    rows: list[dict] = []
    for symbol in symbols:
        # Resolve a price path per symbol. Length = days * 24 hourly bars.
        if price_paths and symbol in price_paths:
            closes = price_paths[symbol]
            if len(closes) < days * 24:
                raise ValueError(f"price_paths[{symbol}] needs >= {days*24} entries")
        else:
            base = 100.0 + rng.random() * 10
            closes = [base + 0.05 * i for i in range(days * 24)]
        for d in range(days):
            day_start = start_ms + d * MS_PER_DAY
            for h in range(24):
                ts = day_start + h * MS_PER_HOUR
                bar_close = closes[d * 24 + h]
                bar_open = closes[d * 24 + h - 1] if (d * 24 + h - 1) >= 0 else bar_close
                rows.append({
                    "ts_ms": ts,
                    "symbol": symbol,
                    "open": bar_open,
                    "high": max(bar_open, bar_close) * 1.001,
                    "low": min(bar_open, bar_close) * 0.999,
                    "close": bar_close,
                    "volume_base": 100.0 + rng.random() * 50,
                    "turnover_quote": (100.0 + rng.random() * 50) * bar_close,
                    "source": "test",
                    "date": (
                        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    ),
                })
    return pl.DataFrame(rows)


# ============================================================================
# Daily aggregation
# ============================================================================


def test_aggregate_daily_klines_emits_one_row_per_symbol_date() -> None:
    hourly = _make_hourly_klines(["AAA", "BBB"], "2025-01-01", days=3, seed=1)
    daily = _aggregate_daily_klines(hourly)
    # 3 days × 2 symbols
    assert daily.height == 6
    # Daily close == close of the LAST hourly bar of the day
    aaa_day0 = daily.filter((pl.col("symbol") == "AAA") & (pl.col("date") == "2025-01-01"))
    expected_last_close = hourly.filter(
        (pl.col("symbol") == "AAA") & (pl.col("date") == "2025-01-01")
    ).sort("ts_ms")["close"][-1]
    assert aaa_day0["close"][0] == pytest.approx(expected_last_close)
    # first_bar_close == close of the FIRST hourly bar of the day
    expected_first_close = hourly.filter(
        (pl.col("symbol") == "AAA") & (pl.col("date") == "2025-01-01")
    ).sort("ts_ms")["close"][0]
    assert aaa_day0["first_bar_close"][0] == pytest.approx(expected_first_close)


def test_aggregate_daily_klines_sums_turnover_and_volume() -> None:
    hourly = _make_hourly_klines(["AAA"], "2025-01-01", days=1, seed=2)
    daily = _aggregate_daily_klines(hourly)
    assert daily.height == 1
    assert daily["volume_base"][0] == pytest.approx(hourly["volume_base"].sum())
    assert daily["turnover_quote"][0] == pytest.approx(hourly["turnover_quote"].sum())


# ============================================================================
# Forward returns (the causality contract)
# ============================================================================


def test_forward_returns_use_entry_plus_one_hour_close() -> None:
    """fwd_ret_3d for decision date D = close[first bar of D+1+3] / close[first bar of D+1] - 1.

    Build a hand-rigged path where the first-bar close on each day is exactly
    the day index plus 10, so forward returns are deterministic and easy to
    check by hand."""
    days = 12
    # closes[hour_index] — we only care about the FIRST bar of each day.
    # First bar of day d is hour_index d*24+0. Set that bar's close to (d+10).
    # All other hours within the day get the same value so daily aggregates
    # are unambiguous.
    closes: list[float] = []
    for d in range(days):
        for _h in range(24):
            closes.append(float(d + 10))
    klines = _make_hourly_klines(["AAA"], "2025-01-01", days=days, seed=3, price_paths={"AAA": closes})
    daily = _aggregate_daily_klines(klines)
    fwd = _attach_forward_returns(daily, horizons=(1, 3, 7))
    # Day 0 = decision date 2025-01-01. entry_close = first bar of D+1 = closes[24] = 11.
    # fwd_ret_1d = closes[48]/closes[24] - 1 = 12/11 - 1
    # fwd_ret_3d = closes[96]/closes[24] - 1 = 14/11 - 1
    # fwd_ret_7d = closes[192]/closes[24] - 1 = 18/11 - 1
    day0 = fwd.filter(pl.col("ts_ms") == _date_ms("2025-01-01")).to_dicts()[0]
    assert day0["fwd_ret_1d"] == pytest.approx(12.0 / 11.0 - 1.0)
    assert day0["fwd_ret_3d"] == pytest.approx(14.0 / 11.0 - 1.0)
    assert day0["fwd_ret_7d"] == pytest.approx(18.0 / 11.0 - 1.0)


def test_forward_returns_are_null_past_window_end() -> None:
    """Rows whose horizon extends past the data window get null forward
    returns. IC code drops nulls; portfolio code skips them."""
    days = 5
    klines = _make_hourly_klines(["AAA"], "2025-01-01", days=days, seed=4)
    daily = _aggregate_daily_klines(klines)
    fwd = _attach_forward_returns(daily, horizons=(7,))
    # 5 days of data → fwd_ret_7d requires close at D+8 (= day 0+8 = day 8, OOB).
    assert fwd["fwd_ret_7d"].null_count() == fwd.height


# ============================================================================
# Causality (the bug we will not repeat)
# ============================================================================


def _make_deterministic_klines(
    *,
    symbols: list[str],
    start_date: str,
    days: int,
    price_paths: dict[str, list[float]],
    turnover_paths: dict[str, list[float]] | None = None,
) -> pl.DataFrame:
    """Hand-rigged hourly klines with NO RNG. Each symbol's hourly close path
    comes from ``price_paths[symbol]`` (must have days*24 entries). Optional
    ``turnover_paths`` per symbol; defaults to 1000 per hour for every symbol
    so cross-sectional rank features have no per-symbol noise."""
    start_ms = _date_ms(start_date)
    rows: list[dict] = []
    for symbol in symbols:
        closes = price_paths[symbol]
        if len(closes) < days * 24:
            raise ValueError(f"price_paths[{symbol}] needs >= {days*24} entries")
        turnovers = (turnover_paths or {}).get(symbol, [1000.0] * (days * 24))
        for d in range(days):
            day_start = start_ms + d * MS_PER_DAY
            for h in range(24):
                idx = d * 24 + h
                ts = day_start + h * MS_PER_HOUR
                bar_close = closes[idx]
                bar_open = closes[idx - 1] if idx > 0 else bar_close
                rows.append({
                    "ts_ms": ts,
                    "symbol": symbol,
                    "open": bar_open,
                    "high": max(bar_open, bar_close),
                    "low": min(bar_open, bar_close),
                    "close": bar_close,
                    "volume_base": 1.0,
                    "turnover_quote": turnovers[idx],
                    "source": "test",
                    "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                })
    return pl.DataFrame(rows)


def _features_for_klines(klines: pl.DataFrame) -> dict[str, pl.DataFrame]:
    daily = _aggregate_daily_klines(klines)
    returns = _attach_daily_returns(daily)
    ctx = FeatureContext(
        daily_klines=daily,
        daily_returns=returns,
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out: dict[str, pl.DataFrame] = {}
    for name, spec in FEATURE_REGISTRY.items():
        if name in {"funding_rate_z", "funding_rate_delta_7d", "oi_delta_7d", "oi_to_adv", "premium_index_z"}:
            continue  # those features rely on datasets we left empty here
        feat = spec.builder(ctx)
        if not feat.is_empty():
            out[name] = feat
    return out


def test_no_feature_can_see_a_future_price_spike() -> None:
    """Causality contract: plant a +50% price spike on day 10's first bar in
    universe A only. Universe B is identical except the spike isn't there.
    For SPIKE on any date D < spike_day, the feature value in A must equal
    the feature value in B — bit-identical input up to D's EOD means
    bit-identical output, otherwise the feature is reading from the future.

    Comparing same-symbol-across-universes (rather than cross-symbol within
    one universe) avoids false positives from ordinal tie-breaking in
    rank-based features like ``liquidity_rank``."""
    days = 15
    spike_day = 10
    flat = [100.0] * (days * 24)
    spiked = list(flat)
    spiked[spike_day * 24] = 150.0  # +50% on the first bar of day 10
    klines_with_spike = _make_deterministic_klines(
        symbols=["AAA", "SPIKE"],
        start_date="2025-01-01",
        days=days,
        price_paths={"AAA": flat, "SPIKE": spiked},
    )
    klines_without_spike = _make_deterministic_klines(
        symbols=["AAA", "SPIKE"],
        start_date="2025-01-01",
        days=days,
        price_paths={"AAA": flat, "SPIKE": flat},
    )
    feats_a = _features_for_klines(klines_with_spike)
    feats_b = _features_for_klines(klines_without_spike)

    leakers: list[tuple[str, str, object, object]] = []
    for name in feats_a:
        a = feats_a[name].filter(pl.col("symbol") == "SPIKE").sort("ts_ms")
        b = feats_b[name].filter(pl.col("symbol") == "SPIKE").sort("ts_ms")
        a_rows = a.to_dicts()
        b_rows = b.to_dicts()
        for row_a, row_b in zip(a_rows, b_rows):
            ts = row_a["ts_ms"]
            day_index = (ts - _date_ms("2025-01-01")) // MS_PER_DAY
            if day_index >= spike_day:
                continue  # at-or-after the spike day, divergence is expected
            v_a = row_a[name]
            v_b = row_b[name]
            label = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if v_a is None and v_b is None:
                continue
            if v_a is None or v_b is None:
                leakers.append((name, label, v_a, v_b))
                continue
            if not math.isclose(float(v_a), float(v_b), abs_tol=1e-9):
                leakers.append((name, label, v_a, v_b))
    assert not leakers, (
        f"Future-bleed detected in features: {leakers}. "
        "A feature for date D must use ONLY data observable at D's EOD close."
    )


# ============================================================================
# Individual feature spot-checks
# ============================================================================


def test_close_location_1d_endpoints() -> None:
    """close_location_1d = 1.0 when close==high, 0.0 when close==low."""
    daily = pl.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"],
        "ts_ms": [_date_ms("2025-01-01")] * 3,
        "date": ["2025-01-01"] * 3,
        "open": [100.0] * 3,
        "close": [110.0, 90.0, 100.0],
        "first_bar_close": [100.0] * 3,
        "high": [110.0, 105.0, 100.0],
        "low": [95.0, 90.0, 100.0],
        "volume_base": [0.0] * 3,
        "turnover_quote": [0.0] * 3,
    })
    ctx = FeatureContext(
        daily_klines=daily,
        daily_returns=daily.with_columns(pl.lit(0.0).alias("ret_1d")),
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    feat = FEATURE_REGISTRY["close_location_1d"].builder(ctx)
    by_sym = {row["symbol"]: row["close_location_1d"] for row in feat.to_dicts()}
    assert by_sym["AAA"] == pytest.approx(1.0)  # close at high
    assert by_sym["BBB"] == pytest.approx(0.0)  # close at low
    assert by_sym["CCC"] == pytest.approx(0.5)  # degenerate (high == low)


def test_liquidity_rank_orders_by_trailing_turnover() -> None:
    """liquidity_rank: 1 = highest turnover (7d trailing mean)."""
    days = 10
    # AAA has constant high turnover; BBB has constant low turnover.
    rows: list[dict] = []
    for d in range(days):
        rows.append({
            "symbol": "AAA",
            "ts_ms": _date_ms("2025-01-01") + d * MS_PER_DAY,
            "date": "2025-01-0" + str(d + 1) if d < 9 else "2025-01-10",
            "open": 100.0, "close": 100.0, "first_bar_close": 100.0, "high": 100.0, "low": 100.0,
            "volume_base": 0.0,
            "turnover_quote": 1_000_000.0,
        })
        rows.append({
            "symbol": "BBB",
            "ts_ms": _date_ms("2025-01-01") + d * MS_PER_DAY,
            "date": "2025-01-0" + str(d + 1) if d < 9 else "2025-01-10",
            "open": 100.0, "close": 100.0, "first_bar_close": 100.0, "high": 100.0, "low": 100.0,
            "volume_base": 0.0,
            "turnover_quote": 1.0,
        })
    daily = pl.DataFrame(rows)
    ctx = FeatureContext(
        daily_klines=daily,
        daily_returns=daily.with_columns(pl.lit(0.0).alias("ret_1d")),
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    feat = FEATURE_REGISTRY["liquidity_rank"].builder(ctx)
    # Final day: AAA rank=1, BBB rank=2 (1 = highest liquidity).
    last_day = feat.filter(pl.col("ts_ms") == _date_ms("2025-01-01") + (days - 1) * MS_PER_DAY)
    by_sym = {row["symbol"]: row["liquidity_rank"] for row in last_day.to_dicts()}
    assert by_sym["AAA"] == 1
    assert by_sym["BBB"] == 2


# ============================================================================
# Univariate IC
# ============================================================================


def _planted_edge_panel() -> pl.DataFrame:
    """Build a panel where feature1 IS forward return up to noise — IC ~ 1.
    feature2 is iid noise — IC ~ 0 in expectation.

    Sized so the IC stats stabilize: 120 days × 100 symbols ≈ 12k observations,
    enough to put the noise feature's mean IC inside +/- 0.01 with high
    probability under the null."""
    import random
    rng = random.Random(42)
    rows: list[dict] = []
    n_days = 120
    n_symbols = 100
    for d in range(n_days):
        ts = _date_ms("2025-01-01") + d * MS_PER_DAY
        per_day_fwd = [rng.gauss(0.0, 0.05) for _ in range(n_symbols)]
        for s in range(n_symbols):
            fwd = per_day_fwd[s]
            feature1 = fwd + rng.gauss(0.0, 0.005)  # high IC by construction
            feature2 = rng.gauss(0.0, 1.0)  # noise feature
            rows.append({
                "symbol": f"S{s:02d}",
                "ts_ms": ts,
                "feature1": feature1,
                "feature2": feature2,
                "fwd_ret_3d": fwd,
            })
    return pl.DataFrame(rows)


def test_compute_univariate_ic_recovers_planted_positive_edge() -> None:
    panel = _planted_edge_panel()
    report = compute_univariate_ic(panel, feature="feature1", target="fwd_ret_3d")
    # Construction: feature1 ≈ fwd_ret_3d → IC should be very high (>0.9)
    assert isinstance(report, ICReport)
    assert report.mean_ic > 0.9
    assert report.t_stat > 5.0
    assert report.sub_period_sign_consistent is True


def test_compute_univariate_ic_does_not_survive_phase_5_rule_on_noise() -> None:
    """The Phase 5 conjunctive survival rule is:
        |mean_ic| >= 0.03 AND sub_period_sign_consistent AND |t_stat| >= 3.
    A noise feature must FAIL the rule (the whole point of the rule is to
    reject noise). With 120d × 100 symbols, mean_ic for noise is well within
    +/- 0.02 in expectation; we check the conjunctive rule rather than the
    individual stats so the test isn't flaky on a particular sample."""
    panel = _planted_edge_panel()
    report = compute_univariate_ic(panel, feature="feature2", target="fwd_ret_3d")
    survives = (
        abs(report.mean_ic) >= 0.03
        and report.sub_period_sign_consistent
        and abs(report.t_stat) >= 3.0
    )
    assert not survives, (
        f"Noise feature falsely survived Phase 5 rule: mean_ic={report.mean_ic:.4f}, "
        f"t={report.t_stat:.2f}, sub_period_sign_consistent={report.sub_period_sign_consistent}"
    )


def test_compute_univariate_ic_raises_on_unknown_feature_or_target() -> None:
    panel = _planted_edge_panel()
    with pytest.raises(KeyError):
        compute_univariate_ic(panel, feature="missing", target="fwd_ret_3d")
    with pytest.raises(KeyError):
        compute_univariate_ic(panel, feature="feature1", target="fwd_ret_99d")


# ============================================================================
# Combined portfolio
# ============================================================================


def _portfolio_panel() -> pl.DataFrame:
    rows: list[dict] = []
    n_days = 5
    n_symbols = 20
    for d in range(n_days):
        ts = _date_ms("2025-01-01") + d * MS_PER_DAY
        for s in range(n_symbols):
            rows.append({
                "symbol": f"S{s:02d}",
                "ts_ms": ts,
                "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "feature_a": float(s),  # monotone in symbol id
                "realized_vol_7d": 0.5,  # constant so sizing is constant
                "fwd_ret_3d": 0.0,
            })
    return pl.DataFrame(rows)


def test_build_combined_signal_portfolio_assigns_top_decile_shorts() -> None:
    panel = _portfolio_panel()
    out = build_combined_signal_portfolio(
        panel,
        surviving_features=["feature_a"],
        weighting="equal",
        top_decile=0.20,  # 20% of 20 symbols/day = 4 shorts + 4 longs
        vol_target_per_name=0.01,
        forward_horizon=3,
    )
    # Per day: 4 shorts (lowest feature_a → most-negative Z), 4 longs (highest), 12 flats
    by_side_per_day = (
        out.group_by(["ts_ms", "position_side"]).agg(pl.len().alias("n"))
    )
    assert by_side_per_day.filter(pl.col("position_side") == "short")["n"].max() == 4
    assert by_side_per_day.filter(pl.col("position_side") == "long")["n"].max() == 4
    assert by_side_per_day.filter(pl.col("position_side") == "flat")["n"].max() == 12

    # Short weights are negative; long weights are positive; flat is zero.
    shorts = out.filter(pl.col("position_side") == "short")
    longs = out.filter(pl.col("position_side") == "long")
    assert (shorts["weight"] < 0.0).all()
    assert (longs["weight"] > 0.0).all()
    assert (out.filter(pl.col("position_side") == "flat")["weight"] == 0.0).all()


def test_build_combined_signal_portfolio_raises_without_vol_column() -> None:
    panel = _portfolio_panel().drop("realized_vol_7d")
    with pytest.raises(KeyError, match="realized_vol_7d"):
        build_combined_signal_portfolio(
            panel,
            surviving_features=["feature_a"],
            weighting="equal",
            forward_horizon=3,
        )


def test_build_combined_signal_portfolio_requires_ic_weights_when_ic_weighted() -> None:
    panel = _portfolio_panel()
    with pytest.raises(ValueError, match="ic_weights"):
        build_combined_signal_portfolio(
            panel,
            surviving_features=["feature_a"],
            weighting="ic_weighted",
            forward_horizon=3,
        )


# ============================================================================
# Registry + resolver
# ============================================================================


def test_feature_registry_has_all_20_features_from_the_plan() -> None:
    expected = {
        "xs_rank_ret_1d", "xs_rank_ret_3d", "xs_rank_ret_7d", "xs_rank_ret_30d",
        "liquidity_rank", "liquidity_rank_delta_7d", "liquidity_rank_delta_30d",
        "turnover_delta_7d", "turnover_delta_30d",
        "funding_rate_z", "funding_rate_delta_7d",
        "oi_delta_7d", "oi_to_adv",
        "premium_index_z",
        "realized_vol_7d", "vol_of_vol_30d",
        "close_location_1d", "range_extension_30d",
        "dist_from_30d_high", "dist_from_30d_low",
    }
    assert set(FEATURE_REGISTRY) == expected


def test_resolve_feature_specs_accepts_all_string_list_and_specs() -> None:
    assert len(resolve_feature_specs("all")) == 20
    assert [s.name for s in resolve_feature_specs("xs_rank_ret_1d,funding_rate_z")] == [
        "xs_rank_ret_1d", "funding_rate_z",
    ]
    one = FEATURE_REGISTRY["xs_rank_ret_3d"]
    assert resolve_feature_specs([one]) == [one]
    with pytest.raises(KeyError, match="not_a_feature"):
        resolve_feature_specs(["not_a_feature"])


# ============================================================================
# build_feature_panel end-to-end via tmp data root
# ============================================================================


def _write_fixture_data_root(root: Path, *, days: int = 35) -> None:
    """Write a minimal data root (klines_1h only) to ``root`` so
    build_feature_panel can read it via read_dataset."""
    hourly = _make_hourly_klines(["AAA", "BBB", "CCC"], "2025-01-01", days=days, seed=7)
    # Storage convention: partitioned by date=YYYY-MM-DD, single file 'part.parquet'.
    klines_dir = root / "klines_1h"
    for date_str, group in hourly.group_by("date"):
        partition = klines_dir / f"date={date_str[0]}"
        partition.mkdir(parents=True, exist_ok=True)
        group.write_parquet(partition / "part.parquet")


def test_build_feature_panel_end_to_end_produces_features_and_forward_returns(tmp_path: Path) -> None:
    _write_fixture_data_root(tmp_path, days=35)
    panel = build_feature_panel(
        tmp_path,
        start="2025-01-15",  # leave 14 days of warm-up so 30d-rolling features have data
        end="2025-02-01",
        feature_specs=["xs_rank_ret_3d", "close_location_1d", "realized_vol_7d", "liquidity_rank"],
        forward_horizons=(1, 3),
    )
    # Required columns
    for col in ("symbol", "ts_ms", "xs_rank_ret_3d", "close_location_1d", "realized_vol_7d",
                "liquidity_rank", "fwd_ret_1d", "fwd_ret_3d"):
        assert col in panel.columns, f"missing column {col}; got {panel.columns}"
    # Every (symbol, date) in the window has a row
    assert panel.height > 0
    assert set(panel["symbol"].unique().to_list()) == {"AAA", "BBB", "CCC"}


def test_build_feature_panel_universe_filter_drops_low_turnover_rows(tmp_path: Path) -> None:
    _write_fixture_data_root(tmp_path, days=20)
    panel_unfiltered = build_feature_panel(
        tmp_path,
        start="2025-01-15",
        end="2025-01-20",
        feature_specs=["close_location_1d"],
        forward_horizons=(1,),
        universe_min_daily_turnover=0.0,
    )
    panel_filtered = build_feature_panel(
        tmp_path,
        start="2025-01-15",
        end="2025-01-20",
        feature_specs=["close_location_1d"],
        forward_horizons=(1,),
        universe_min_daily_turnover=1e18,  # impossibly high — should drop every row
    )
    assert panel_unfiltered.height > 0
    assert panel_filtered.height == 0
