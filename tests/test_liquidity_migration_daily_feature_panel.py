"""Pin the daily feature-panel builders.

Causality is the only thing that matters in this module: if a feature for
(symbol, date=D) ever uses data from after D's EOD-close, residual momentum
and risk-model consumers become invalid. These tests pin:

  * forward returns match the entry+1h fill-model exactly (D's signal trades
    at D+1's first-bar close; exit N days later at D+1+N's first-bar close)
  * each feature is causal at its EOD — explicitly tested on a synthetic
    fixture where a future-only price spike would be detectable if leaked
  * cross-sectional ranks are per-day and dense
  * registry has all 20 features the plan listed
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.daily_feature_panel import (
    FEATURE_REGISTRY,
    FeatureContext,
    _aggregate_daily_funding,
    _aggregate_daily_klines,
    _aggregate_daily_open_interest,
    _aggregate_daily_premium,
    _attach_daily_returns,
    _attach_forward_returns,
    _make_turnover_delta,
    _make_xs_rank_ret_Nd,
    build_feature_panel,
    resolve_feature_specs,
)

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


# ============================================================================
# Synthetic data fixture
# ============================================================================


def _date_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _daily_returns_frame(symbol: str, day_indices: list[int], closes: list[float]) -> pl.DataFrame:
    # Provenance: relocated from tests/test_audit_fix_b09.py (audit bucket b09).
    base = _date_ms("2025-01-01")
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(day_indices),
            "ts_ms": [base + d * MS_PER_DAY for d in day_indices],
            "close": closes,
        }
    )


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


def test_forward_returns_calendar_correct_across_missing_day() -> None:
    """M4: a missing calendar day must not be skipped positionally. A row whose
    entry (D+1) or exit (D+1+N) calendar day is absent gets a NULL forward
    return, not a misaligned one. With a positional shift, day-2 below would
    have wrongly produced close[D4]/close[D... ] — a 2-calendar-day return
    mislabeled as fwd_ret_1d."""
    base = _date_ms("2025-01-01")
    present = [0, 1, 2, 4, 5, 6]  # 2025-01-04 (day 3) is MISSING
    daily = pl.DataFrame(
        {
            "symbol": ["AAA"] * len(present),
            "ts_ms": [base + d * MS_PER_DAY for d in present],
            "first_bar_close": [float(d + 10) for d in present],  # close = day_index + 10
        }
    )
    fwd = _attach_forward_returns(daily, horizons=(1,))
    by_day = {(row["ts_ms"] - base) // MS_PER_DAY: row for row in fwd.to_dicts()}
    # Day 0: entry=D1 (11), exit=D2 (12) — both present → valid.
    assert by_day[0]["fwd_ret_1d"] == pytest.approx(12.0 / 11.0 - 1.0)
    # Day 1: entry=D2 (12) present, exit=D3 MISSING → null (was 13/12-1 positionally).
    assert by_day[1]["fwd_ret_1d"] is None
    # Day 2: entry=D3 MISSING → null (positional shift wrongly gave 15/14-1).
    assert by_day[2]["fwd_ret_1d"] is None
    # Day 4: entry=D5 (15), exit=D6 (16) — both present → valid.
    assert by_day[4]["fwd_ret_1d"] == pytest.approx(16.0 / 15.0 - 1.0)


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


def test_autodetect_dataset_names_picks_binance_when_prefixed_subdirs_exist(tmp_path: Path) -> None:
    """Binance roots use binance_usdm_-prefixed dataset dirs; Bybit roots
    use plain names. Phase 5a hit this — dispatch passed default Bybit
    names against the Binance root, the panel silently produced 100%-null
    funding/oi/premium-derived features, and Phase 5b IC returned all NaN
    for those features. The autodetector picks the right convention by
    sniffing which subdirs exist."""
    from liquidity_migration.daily_feature_panel import _autodetect_dataset_names

    # Bybit-shaped root: plain dataset dirs
    (tmp_path / "bybit_like" / "funding").mkdir(parents=True)
    (tmp_path / "bybit_like" / "open_interest").mkdir(parents=True)
    bybit_names = _autodetect_dataset_names(tmp_path / "bybit_like")
    assert bybit_names["funding_dataset"] == "funding"
    assert bybit_names["open_interest_dataset"] == "open_interest"
    assert bybit_names["premium_dataset"] == "premium_index_1h"

    # Binance-shaped root: binance_usdm_-prefixed dirs
    (tmp_path / "binance_like" / "binance_usdm_funding").mkdir(parents=True)
    (tmp_path / "binance_like" / "binance_usdm_open_interest").mkdir(parents=True)
    binance_names = _autodetect_dataset_names(tmp_path / "binance_like")
    assert binance_names["funding_dataset"] == "binance_usdm_funding"
    assert binance_names["open_interest_dataset"] == "binance_usdm_open_interest"
    assert binance_names["premium_dataset"] == "binance_usdm_premium_index_1h"

    # Empty root (neither convention): defaults to Bybit names
    (tmp_path / "empty").mkdir()
    empty_names = _autodetect_dataset_names(tmp_path / "empty")
    assert empty_names["funding_dataset"] == "funding"


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


def test_attach_daily_returns_is_calendar_exact_across_gaps() -> None:
    """ret_1d must be calendar-exact: a symbol with a missing calendar day gets a
    NULL return on the post-gap day, NOT a positional 2-day return mislabeled 1d
    (the gap-blind shift(1) hazard the M4 forward-return join was built to avoid)."""
    _DAY = 86_400_000
    # Day 2 missing for X: present days 0, 1, 3.
    df = pl.DataFrame({
        "symbol": ["X", "X", "X"],
        "ts_ms": [0, _DAY, 3 * _DAY],
        "close": [100.0, 110.0, 121.0],
    })
    out = _attach_daily_returns(df).sort("ts_ms")
    rets = out["ret_1d"].to_list()
    assert rets[0] is None  # no D-1 partner for the first day
    assert abs(rets[1] - 0.10) < 1e-12  # 110/100 - 1
    assert rets[2] is None  # day 3 has no day-2 partner -> null, not 121/110-1


# ============================================================================
# audit2c: daily-aggregation day-key snap
# ============================================================================


def _gap_edge_intraday(value_col: str, *, extra: dict[str, list] | None = None) -> pl.DataFrame:
    """Intraday rows for one symbol/day whose first ts is OFF the 00:00 grid.

    The day's 00:00 bar is missing (a gap edge); the first observation is at
    01:00 UTC. ``min(ts_ms)`` is therefore the day floor + 1h, not the floor.
    """
    day_floor = _date_ms("2025-03-02")
    hours = [1, 2, 23]  # 00:00 bar absent
    data: dict[str, list] = {
        "symbol": ["S00"] * len(hours),
        "ts_ms": [day_floor + h * 3_600_000 for h in hours],
        value_col: [1.0, 2.0, 3.0],
    }
    if extra:
        data.update(extra)
    return pl.DataFrame(data)


def test_funding_day_key_snapped_to_day_floor() -> None:
    funding = _gap_edge_intraday("funding_rate")
    out = _aggregate_daily_funding(funding)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    # sanity: the un-snapped first ts would have been day_floor + 1h
    assert out["ts_ms"][0] != day_floor + 3_600_000
    # aggregation content unaffected by the key snap
    assert out["funding_rate_1d_sum"][0] == 6.0
    assert out["funding_rate_last"][0] == 3.0


def test_funding_daily_sum_uses_raw_dynamic_settlements_not_default_8h_equivalent() -> None:
    day_floor = _date_ms("2025-03-02")
    funding = pl.DataFrame(
        {
            "symbol": ["S00"] * 4,
            "ts_ms": [day_floor + hour * 3_600_000 for hour in range(1, 5)],
            "funding_rate": [-0.005] * 4,
            # This legacy column was derived from stale/default 8h metadata and
            # must not replace the realized per-settlement rates above.
            "funding_rate_8h_equiv": [-0.005, -0.04, -0.04, -0.04],
        }
    )

    out = _aggregate_daily_funding(funding)

    assert out["funding_rate_1d_sum"][0] == pytest.approx(-0.02)
    assert out["funding_rate_last"][0] == pytest.approx(-0.005)


def test_open_interest_day_key_snapped_to_day_floor() -> None:
    oi = _gap_edge_intraday("open_interest")
    out = _aggregate_daily_open_interest(oi)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    assert out["open_interest"][0] == 3.0  # last of the day


def test_premium_day_key_snapped_to_day_floor() -> None:
    premium = _gap_edge_intraday("close")
    out = _aggregate_daily_premium(premium)
    day_floor = _date_ms("2025-03-02")
    assert out["ts_ms"].to_list() == [day_floor]
    assert out["premium_close"][0] == 3.0  # last hourly close


def test_snapped_key_joins_kline_grid() -> None:
    """The snapped daily key joins a kline-grid row keyed at 00:00 (fix #2).

    The kline daily grid keys each day at the 00:00 floor. The un-snapped OI
    key (first intraday ts = floor + 1h) would miss this join; the snapped key
    lands exactly on the grid and the join keeps the day.
    """
    day_floor = _date_ms("2025-03-02")
    oi_daily = _aggregate_daily_open_interest(_gap_edge_intraday("open_interest"))
    kline_grid = pl.DataFrame({"symbol": ["S00"], "ts_ms": [day_floor], "adv_30d": [10.0]})
    joined = oi_daily.join(kline_grid, on=["symbol", "ts_ms"], how="inner")
    assert joined.height == 1
    assert joined["ts_ms"][0] == day_floor


# ============================================================================
# Relocated from tests/test_audit_fix_b09.py (audit bucket b09).
# pit-signals-3 / research-methodology-2 / test-gaps-3 — gap-blind N-day builders
# ============================================================================


def test_xs_rank_ret_Nd_is_calendar_exact_across_a_gap() -> None:
    """A symbol present on days {0,1,2,5,6} then ranked: the day-5/6 rows must NOT
    treat shift(3) as a clean 3-calendar-day return. With the positional bug the
    day-5 row's "ret_3d" used day-2's close (a 3-CALENDAR-day-misaligned span);
    calendar_shift nulls it because there is no row exactly 3 days back.

    Two contiguous control symbols keep the cross-section non-degenerate so the
    rank denominator and ordering are well defined.
    """
    builder = _make_xs_rank_ret_Nd(3)
    # GAPPED symbol: missing days 3 and 4.
    gapped = _daily_returns_frame("GAP", [0, 1, 2, 5, 6], [10.0, 11.0, 12.0, 20.0, 21.0])
    # Contiguous controls present every day 0..6.
    ctrl_days = [0, 1, 2, 3, 4, 5, 6]
    ctrl_a = _daily_returns_frame("AAA", ctrl_days, [100.0 + d for d in ctrl_days])
    ctrl_b = _daily_returns_frame("BBB", ctrl_days, [200.0 - d for d in ctrl_days])
    daily_returns = pl.concat([gapped, ctrl_a, ctrl_b]).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=pl.DataFrame(),
        daily_returns=daily_returns,
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx)
    base = _date_ms("2025-01-01")
    gap_rows = {
        (row["ts_ms"] - base) // MS_PER_DAY: row["xs_rank_ret_3d"]
        for row in out.filter(pl.col("symbol") == "GAP").to_dicts()
    }
    # Day 5 and day 6 have no row EXACTLY 3 calendar days back (days 2 and 3 resp.;
    # day 3 is missing entirely, day 2 is 3 days before day 5 but reached via a gap),
    # so calendar_shift yields null ret_Nd -> null rank. The positional bug would have
    # produced a finite (misaligned) rank for at least one of them.
    assert gap_rows[5] is None
    assert gap_rows[6] is None
    # A contiguous row (day 3, exactly 3 days after day 0) is finite for the controls.
    ctrl_day3 = [
        row["xs_rank_ret_3d"]
        for row in out.filter((pl.col("symbol") == "AAA")).to_dicts()
        if (row["ts_ms"] - base) // MS_PER_DAY == 3
    ]
    assert ctrl_day3 and ctrl_day3[0] is not None


def test_xs_rank_ret_Nd_matches_positional_shift_on_contiguous_data() -> None:
    """Numerical-equivalence gate: on a CONTIGUOUS series calendar_shift(n) is
    byte-identical to the old close/close.shift(n)-1, so the fix moves no number
    on the happy path."""
    builder = _make_xs_rank_ret_Nd(3)
    days = list(range(8))
    a = _daily_returns_frame("AAA", days, [100.0 * (1.0 + 0.01 * d) for d in days])
    b = _daily_returns_frame("BBB", days, [50.0 * (1.0 + 0.02 * d) for d in days])
    daily_returns = pl.concat([a, b]).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=pl.DataFrame(),
        daily_returns=daily_returns,
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx).sort(["symbol", "ts_ms"])
    # Reference: old positional definition + the same cross-sectional rank fraction.
    ref = daily_returns.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1.0).alias("ret_Nd")
    ).with_columns(
        pl.col("ret_Nd").rank(method="average", descending=False).over("ts_ms").alias("_r")
    ).with_columns(
        (pl.col("_r") / pl.col("ret_Nd").count().over("ts_ms")).alias("ref_rank")
    ).sort(["symbol", "ts_ms"])
    got = out["xs_rank_ret_3d"].to_list()
    exp = ref["ref_rank"].to_list()
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert (g is None) == (e is None)
        if g is not None:
            assert g == pytest.approx(e)


def test_turnover_delta_window_shrinks_across_a_gap_not_stretches() -> None:
    """turnover_delta_7d's prior-mean is now calendar-bounded: a gapped symbol's
    prior window covers <=7 CALENDAR days, never positionally back-filled rows from
    >7 days ago. We assert the post-gap row's prior mean uses only in-window days."""
    builder = _make_turnover_delta(7)
    base = _date_ms("2025-01-01")
    # Present days 0,1,2 (turnover 100) then a long gap, relist day 30 (turnover 100).
    present = [0, 1, 2, 30]
    daily_klines = pl.DataFrame(
        {
            "symbol": ["GAP"] * len(present),
            "ts_ms": [base + d * MS_PER_DAY for d in present],
            "turnover_quote": [100.0, 100.0, 100.0, 100.0],
        }
    ).sort(["symbol", "ts_ms"])
    ctx = FeatureContext(
        daily_klines=daily_klines,
        daily_returns=pl.DataFrame(),
        funding_daily=pl.DataFrame(),
        open_interest_daily=pl.DataFrame(),
        premium_daily=pl.DataFrame(),
    )
    out = builder(ctx)
    by_day = {
        (row["ts_ms"] - base) // MS_PER_DAY: row["turnover_delta_7d"]
        for row in out.to_dicts()
    }
    # Day 30's prior-7-CALENDAR-day window (days 23..29) is empty, so prior_mean is
    # null and turnover_delta_7d is null. The positional bug compared against days
    # {0,1,2} (28+ days stale) and produced a finite (0.0) delta.
    assert by_day[30] is None
