"""Regression tests for audit bucket b11.

Each test pins a finding from the 2026-06-14 audit (ids in the docstrings),
written to FAIL on the original bug and PASS on the committed fix:

  code-quality-2   liquidity_migration/trade_lifecycle.py — the dead
                   _filter_universe is gone.
  cost-funding-3   liquidity_migration/trade_lifecycle.py — the backtest summary
                   surfaces a notional-weighted funding_modeled_fraction so a
                   downstream gate can tell a coverage-edge "partial" book from a
                   half-uncharged one.
  cost-funding-4   liquidity_migration/trade_lifecycle.py — the entry-notional
                   funding approximation is explicitly documented at the charge
                   site (no longer a silent approximation).
  metrics-4        liquidity_migration/trade_lifecycle.py — _worst_volume_day_return
                   SUMS same-exit-day baskets to match build_equity_curve, instead
                   of compounding them.
  long-sleeve-1    liquidity_migration/long_native_event_demo.py — the live entry
                   path applies the deployed weekend 1.5x size tilt identically to
                   the backtest, keyed on the entry time.
  long-sleeve-3    liquidity_migration/long_native_event_demo.py — the per-trade
                   live net_return is NET of venue fees, matching the backtest.
  ratelimit-rest-4 liquidity_migration/long_native_event_demo.py — the rate-limit
                   docstring no longer claims cross-sleeve starvation protection
                   for the erased short sleeve.
  pit-data-4       liquidity_migration/archive_manifest.py — a narrow manifest
                   rebuild UNIONs with the persisted manifest, so it augments
                   rather than destructively replacing date partitions.
  pit-data-7       liquidity_migration/archive_manifest.py — the scrape 1m/1h
                   download paths SKIP v5-listing sentinel rows instead of burning
                   the retry budget and recording a 'failed'.
  w4-w5-stages-2   scripts/w5_continuous_stage1_score_entry.py — A0-must-reproduce-
                   Stage-0 is a load-bearing gate: no arm advances unless wiring is
                   OK on every venue.
  w4-w5-stages-3   scripts/w5_continuous_stage1_score_entry.py — the A0/Stage-0
                   equity reconciliation treats a one-sided NaN as a mismatch, not
                   "close".
"""

from __future__ import annotations

import inspect
import math

import polars as pl
import pytest

import liquidity_migration.archive_manifest as am
import liquidity_migration.trade_lifecycle as tl
import scripts.w5_continuous_stage1_score_entry as w5_stage1
from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.long_native_event_demo import (
    _execute_long_exits,
    _select_long_entry_candidates,
    _v11a_long_native_config,
)
from liquidity_migration.storage import read_dataset, write_dataset


# ---------------------------------------------------------------------------
# code-quality-2: dead _filter_universe removed
# ---------------------------------------------------------------------------
def test_filter_universe_dead_function_removed() -> None:
    assert not hasattr(tl, "_filter_universe"), (
        "_filter_universe had zero call sites and re-implemented stale universe "
        "filtering; it must stay deleted (code-quality-2)."
    )


# ---------------------------------------------------------------------------
# metrics-4: worst_day_return SUMS same-day baskets (matches build_equity_curve)
# ---------------------------------------------------------------------------
def test_worst_volume_day_return_sums_multi_basket_day_matching_equity_curve() -> None:
    baskets = pl.DataFrame(
        {
            "exit_date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "basket_return": [0.10, -0.20, 0.05],
        }
    )
    worst = tl._worst_volume_day_return(baskets)
    # Additive same-day combination: 0.10 + -0.20 = -0.10. The COMPOUND value
    # (1.10 * 0.80 - 1 = -0.12) is the original bug — a different day-return
    # definition than build_equity_curve uses for total_return/max_drawdown.
    assert worst == pytest.approx(-0.10)
    assert worst != pytest.approx(-0.12)


def test_worst_volume_day_return_matches_build_equity_curve_definition() -> None:
    # The worst single-day move of the equity curve must equal worst_day_return.
    baskets = pl.DataFrame(
        {
            "basket_id": ["b1", "b2", "b3"],
            "exit_ts_ms": [
                1_704_067_200_000,  # 2024-01-01
                1_704_067_200_000,  # 2024-01-01 (same day, second basket)
                1_704_153_600_000,  # 2024-01-02
            ],
            "exit_date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "basket_return": [0.10, -0.20, 0.05],
        }
    )
    curve = tl.build_equity_curve(baskets)
    # Per-day equity returns implied by the summed curve.
    eq = curve.sort("ts_ms")["equity"].to_list()
    day_rets = [eq[0] - 1.0] + [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    assert tl._worst_volume_day_return(baskets) == pytest.approx(min(day_rets))


# ---------------------------------------------------------------------------
# cost-funding-3: notional-weighted funding_modeled_fraction surfaced
# ---------------------------------------------------------------------------
def _funding_trades(modes_and_weights: list[tuple[str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "funding_mode": [m for m, _ in modes_and_weights],
            "notional_weight": [w for _, w in modes_and_weights],
        }
    )


def test_funding_modeled_fraction_distinguishes_coverage_edge_from_half_uncharged() -> None:
    # Coverage-edge: one small alt missing, rest modeled -> high fraction.
    edge = _funding_trades([("modeled", 1.0), ("modeled", 1.0), ("missing", 0.05)])
    # Half the book uncharged -> low fraction. funding_mode == 'partial' for both,
    # so the 3-state summary cannot tell them apart; the fraction can.
    half = _funding_trades([("modeled", 1.0), ("missing", 1.0)])
    f_edge = tl._funding_modeled_fraction(edge)
    f_half = tl._funding_modeled_fraction(half)
    assert f_edge == pytest.approx(2.0 / 2.05)
    assert f_half == pytest.approx(0.5)
    assert f_edge > f_half  # the whole point of the metric


def test_funding_modeled_fraction_extremes_and_summary_wiring() -> None:
    assert tl._funding_modeled_fraction(pl.DataFrame()) == 0.0
    all_modeled = _funding_trades([("modeled", 1.0), ("modeled", 2.0)])
    assert tl._funding_modeled_fraction(all_modeled) == pytest.approx(1.0)
    all_missing = _funding_trades([("missing", 1.0), ("missing", 1.0)])
    assert tl._funding_modeled_fraction(all_missing) == pytest.approx(0.0)
    # Empty-payload summary carries the new key so downstream readers never KeyError.
    empty = tl.summarize_trade_backtest(
        pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), config=tl.TradeLifecycleConfig()
    )
    assert empty["funding_modeled_fraction"] == 0.0


# ---------------------------------------------------------------------------
# cost-funding-4: entry-notional funding approximation is documented, not silent
# ---------------------------------------------------------------------------
def test_funding_entry_notional_approximation_is_documented() -> None:
    src = inspect.getsource(tl._simulate_indexed_trade)
    assert "cost-funding-4" in src
    assert "marked notional" in src.lower()


# ---------------------------------------------------------------------------
# long-sleeve-1: live path applies the deployed weekend 1.5x size tilt
# ---------------------------------------------------------------------------
def _fc_signal_features(*, symbol: str, signal_ts_ms: int, signal_close: float = 100.0) -> pl.DataFrame:
    """Minimal feature row that passes detect_pattern_fomo_chase (mirrors the
    long_native_event_demo test fixture)."""
    return pl.DataFrame([
        {
            "ts_ms": signal_ts_ms,
            "symbol": symbol,
            "close": signal_close,
            "in_universe": True,
            "regime_on": True,
            "eth_regime_on": True,
            "today_volume_rank": 5,
            "log_return": math.log(1.0 + 0.20),
            "close_location": 0.85,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.05,
            "pump_3d_log": 0.10,
            "pump_7d_log": 0.20,
            "close_loc_3d": 0.7,
            "close_loc_7d": 0.7,
            "intra_max_Nh_pump_log": 0.0,
            "realized_vol": 0.6,
            "coin_30d_return": 0.5,
            "coin_60d_return": 0.5,
            "coin_fc_sma": None,
            "btc_high_proximity": 0.5,
            "btc_sma_dist": 0.05,
            "vol_vs_30d_median": 2.0,
            "own_pump_quantile_90d": 0.10,
            "own_atr_quantile_90d": 0.10,
            "atr_20d": 5.0,
        }
    ])


# 2023-04-01 12:00Z is a Saturday; 2023-04-05 12:00Z is a Wednesday.
_SAT_NOW_MS = 1_680_350_400_000
_WED_NOW_MS = 1_680_696_000_000


def _one_candidate_weight(now_ms: int) -> float:
    strategy = _v11a_long_native_config()
    assert strategy.weekend_size_mult == 1.5, "v11a profile must carry the 1.5x weekend tilt"
    signal_ts = now_ms - 2 * MS_PER_HOUR  # fresh, same UTC day, retrace fired
    features = _fc_signal_features(symbol="BTCUSDT", signal_ts_ms=signal_ts, signal_close=100.0)
    candidates, _ = _select_long_entry_candidates(
        features=features,
        klines=pl.DataFrame(),
        all_trades=pl.DataFrame(),
        now_ms=now_ms,
        strategy=strategy,
        price_by_symbol={"BTCUSDT": 98.5},  # below the 1% retrace threshold
        max_new_entries=5,
    )
    assert len(candidates) == 1, "expected exactly one FC retrace candidate"
    return float(candidates[0]["position_weight"])


def test_live_weekend_size_tilt_matches_backtest() -> None:
    weekday_weight = _one_candidate_weight(_WED_NOW_MS)
    weekend_weight = _one_candidate_weight(_SAT_NOW_MS)
    assert weekday_weight > 0.0
    # The live Sat entry must be sized 1.5x the live weekday entry, exactly as the
    # backtest sizes Sat/Sun entries (long_native.py weekend_size_mult). Before the
    # fix the live path ignored weekend_size_mult, so both were equal.
    assert weekend_weight == pytest.approx(weekday_weight * 1.5)


# ---------------------------------------------------------------------------
# long-sleeve-3: live per-trade net_return is NET of venue fees
# ---------------------------------------------------------------------------
def test_live_long_exit_net_return_subtracts_fees() -> None:
    from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig

    now = 2_000_000_000_000
    # Trade carries an entry fee; equity 10k, notional 1k (weight 0.1), +10% move.
    all_trades = pl.DataFrame([
        {
            "trade_id": "t1", "sleeve": "long", "symbol": "BTCUSDT", "side": "long",
            "status": "open", "qty": "0.001", "entry_price": 100.0,
            "notional_usdt": 1_000.0, "equity_usdt": 10_000.0,
            "entry_fee_usdt": 0.6,  # taker fee on entry
            "planned_exit_ts_ms": now - MS_PER_HOUR,
        },
    ])
    plan = {"trade_id": "t1", "symbol": "BTCUSDT", "qty": "0.001", "exit_reason": "time_stop"}
    demo = LongNativeDemoCycleConfig(submit_orders=False)  # dry-run -> exit_fee 0

    rows, _ = _execute_long_exits(
        [plan], all_trades, trading_client=None, demo=demo, now_ms=now,
        price_by_symbol={"BTCUSDT": 110.0},  # +10%
    )
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    gross = 0.10 * (1_000.0 / 10_000.0)  # gross_trade_return * notional_weight
    fee_return = (0.6 + 0.0) / 10_000.0
    # net_return must be NET of fees; the original bug recorded the gross value.
    assert float(rows[0]["net_return"]) == pytest.approx(gross - fee_return)
    assert float(rows[0]["net_return"]) < gross


# ---------------------------------------------------------------------------
# ratelimit-rest-4: stale "short never gets starved" claim removed
# ---------------------------------------------------------------------------
def test_rate_limit_docstring_no_longer_overstates_cross_sleeve_protection() -> None:
    from liquidity_migration.long_native_event_demo import (
        _long_demo_private_rest_rate_limit_per_second,
    )

    doc = _long_demo_private_rest_rate_limit_per_second.__doc__ or ""
    assert "ratelimit-rest-4" in doc
    assert "per-IP" in doc  # explicitly states it is NOT a per-IP coordinator
    assert "never gets starved" not in doc  # the stale erased-short claim is gone


# ---------------------------------------------------------------------------
# pit-data-7: scrape download paths SKIP v5-listing sentinel rows
# ---------------------------------------------------------------------------
def test_is_v5_listing_row_matches_sentinel_url_or_source() -> None:
    assert am._is_v5_listing_row({"url": am.V5_LISTING_URL_SENTINEL, "source": "x"})
    assert am._is_v5_listing_row({"url": "x", "source": am.V5_LISTING_SOURCE})
    assert not am._is_v5_listing_row(
        {"url": "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2024-01-01.csv.gz", "source": "scrape"}
    )


def test_scrape_download_skips_v5_listing_without_fetch(tmp_path, monkeypatch) -> None:
    # If the sentinel row were NOT skipped, download_public_trade_archive would be
    # called on the bogus URL and burn the retry budget; assert it is never called.
    def _boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("download_public_trade_archive must not be called for a v5-listing row")

    monkeypatch.setattr(am, "download_public_trade_archive", _boom)
    row = {"symbol": "NEWUSDT", "date": "2024-01-01", "url": am.V5_LISTING_URL_SENTINEL,
           "source": am.V5_LISTING_SOURCE}
    for fn in (am._download_one_archive_kline, am._download_one_archive_hourly_kline):
        result = fn(tmp_path, row, missing_only=False, min_existing_bars=1,
                    discard_archives_after_success=False)
        assert result["status"] == "skipped_v5_listing"
        assert result["status"] != "failed"  # the original bug recorded 'failed'


def test_archive_klines_report_surfaces_skipped_count() -> None:
    payload = {
        "name": "fix", "dataset": "klines_1h", "interval": "1h", "rows": 3, "workers": 1,
        "downloaded": 1, "cached": 0, "empty": 0, "skipped_v5_listing": 2, "failures": 0,
        "archives_deleted": 0, "created_at": "c",
    }
    report = am.format_archive_klines_report(payload)
    assert "| Skipped (v5 listing) | 2 |" in report


# ---------------------------------------------------------------------------
# pit-data-4: a narrow rebuild UNIONs with the persisted manifest (no PIT erasure)
# ---------------------------------------------------------------------------
def _manifest(rows: list[tuple[str, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [s for s, _, _ in rows],
            "date": [d for _, d, _ in rows],
            "url": [u for _, _, u in rows],
            "source": ["scrape"] * len(rows),
        }
    )


def test_union_with_persisted_manifest_retains_other_symbols_on_shared_date(tmp_path) -> None:
    # Persisted manifest covers BTC and ETH on 2024-01-01.
    persisted = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("ETHUSDT", "2024-01-01", "eth.csv.gz"),
    ])
    write_dataset(persisted, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    # A narrow rebuild only covers BTC on the same date.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])
    merged = am._union_with_persisted_manifest(tmp_path, narrow)

    syms_on_date = set(
        merged.filter(pl.col("date") == "2024-01-01")["symbol"].to_list()
    )
    # ETH's PIT membership on 2024-01-01 must survive the narrow rebuild.
    assert syms_on_date == {"BTCUSDT", "ETHUSDT"}


def test_union_with_persisted_manifest_dedupes_and_falls_back(tmp_path) -> None:
    # No prior manifest -> returns the new frame unchanged.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])
    assert am._union_with_persisted_manifest(tmp_path, narrow).height == 1

    # With a prior manifest sharing the same (symbol,date,url), the union dedupes.
    write_dataset(narrow, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)
    again = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),  # duplicate key
        ("SOLUSDT", "2024-01-02", "sol.csv.gz"),  # new
    ])
    merged = am._union_with_persisted_manifest(tmp_path, again)
    keys = sorted(zip(merged["symbol"].to_list(), merged["date"].to_list(), merged["url"].to_list()))
    assert keys == [
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("SOLUSDT", "2024-01-02", "sol.csv.gz"),
    ]


def test_run_archive_manifest_persist_is_non_destructive(tmp_path, monkeypatch) -> None:
    """End-to-end: a narrow rebuild persisted via run_archive_manifest must not
    drop another symbol's date partition (pit-data-4)."""
    # Seed a persisted manifest covering BTC + ETH on a shared date.
    seed = _manifest([
        ("BTCUSDT", "2024-01-01", "btc.csv.gz"),
        ("ETHUSDT", "2024-01-01", "eth.csv.gz"),
    ])
    write_dataset(seed, tmp_path, "archive_trade_manifest", partition_by=("date",), append=False)

    # A narrow rebuild that resolves only BTC. Patch the builder so we drive the
    # persist path deterministically without network.
    narrow = _manifest([("BTCUSDT", "2024-01-01", "btc.csv.gz")])

    def _fake_build(*args, **kwargs):
        return narrow

    monkeypatch.setattr(am, "build_archive_trade_manifest", _fake_build)

    cfg = am.ArchiveManifestConfig(
        name="narrow", symbols=("BTCUSDT",), allow_degraded=True,
    )
    am.run_archive_manifest(tmp_path, config=cfg, report_dir=tmp_path / "reports")

    persisted = read_dataset(tmp_path, "archive_trade_manifest")
    syms_on_date = set(
        persisted.filter(pl.col("date") == "2024-01-01")["symbol"].to_list()
    )
    assert "ETHUSDT" in syms_on_date, "narrow rebuild must NOT erase ETH's PIT membership"
    assert "BTCUSDT" in syms_on_date


# ---------------------------------------------------------------------------
# w4-w5-stages-3: A0/Stage-0 equity reconciliation treats one-sided NaN as mismatch
# ---------------------------------------------------------------------------
def test_equity_allclose_one_sided_nan_is_mismatch() -> None:
    # Exactly one side NaN at a row is a genuine mismatch, NOT "close".
    assert not w5_stage1._equity_allclose([1.0, float("nan"), 3.0], [1.0, 2.0, 3.0])
    assert not w5_stage1._equity_allclose([1.0, 2.0, 3.0], [1.0, float("nan"), 3.0])


def test_equity_allclose_matching_nan_positions_pass() -> None:
    # Both NaN at the same row + finite rows within tol -> close.
    assert w5_stage1._equity_allclose([1.0, float("nan"), 3.0], [1.0, float("nan"), 3.0])
    assert w5_stage1._equity_allclose([1.0, 2.0], [1.0, 2.0 + 1e-12])
    assert not w5_stage1._equity_allclose([1.0, 2.0], [1.0, 2.1])
    assert not w5_stage1._equity_allclose([1.0, 2.0, 3.0], [1.0, 2.0])  # length mismatch


# ---------------------------------------------------------------------------
# w4-w5-stages-2: A0-wiring is a load-bearing gate
# ---------------------------------------------------------------------------
def test_a0_wiring_ok_requires_every_venue_to_reproduce_stage0() -> None:
    venues = ["bybit", "binance"]
    good = {
        "bybit": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
    }
    assert w5_stage1._a0_wiring_ok(good, venues)

    # Missing Stage 0 ledger on one venue -> wiring fails (the original silent gap).
    missing = dict(good)
    missing["binance"] = {"available": False}
    assert not w5_stage1._a0_wiring_ok(missing, venues)

    # A0 drifted (allclose False) on one venue -> wiring fails.
    drift = {
        "bybit": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": False},
    }
    assert not w5_stage1._a0_wiring_ok(drift, venues)

    # Row-count mismatch -> wiring fails.
    rows_bad = {
        "bybit": {"available": True, "rows_match": False, "equity_allclose_1e-9": True},
        "binance": {"available": True, "rows_match": True, "equity_allclose_1e-9": True},
    }
    assert not w5_stage1._a0_wiring_ok(rows_bad, venues)

    # A venue absent from the reconcile dict -> wiring fails (cannot prove wiring).
    assert not w5_stage1._a0_wiring_ok({"bybit": good["bybit"]}, venues)
    assert not w5_stage1._a0_wiring_ok(good, [])  # no venues is not a pass
