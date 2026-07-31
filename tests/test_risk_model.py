from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal

from liquidity_migration.daily_feature_panel import _aggregate_daily_klines
from liquidity_migration.risk_model import (
    _FACTOR_COLUMNS,
    build_factor_panel,
    build_factor_panel_from_daily,
    compute_btc_beta,
    fit_factor_returns,
)

_DAY = 86_400_000


def _daily_returns(symbol_to_rets: dict[str, list[float]]) -> pl.DataFrame:
    rows = []
    for sym, rets in symbol_to_rets.items():
        for i, r in enumerate(rets):
            rows.append({"symbol": sym, "ts_ms": i * _DAY, "ret_1d": r})
    return pl.DataFrame(rows)


def test_btc_beta_recovers_known_slope() -> None:
    # ALT = 1.5 * BTC exactly => rolling OLS beta ~ 1.5; FLAT = 0 => beta ~ 0.
    rng = random.Random(0)
    btc = [rng.uniform(-0.05, 0.05) for _ in range(80)]
    alt = [1.5 * b for b in btc]
    flat = [0.0 for _ in btc]
    out = compute_btc_beta(_daily_returns({"BTCUSDT": btc, "ALT": alt, "FLAT": flat}), window=60, min_periods=30)

    last = out.filter(pl.col("ts_ms") == 79 * _DAY)
    alt_beta = last.filter(pl.col("symbol") == "ALT")["btc_beta"][0]
    flat_beta = last.filter(pl.col("symbol") == "FLAT")["btc_beta"][0]
    assert alt_beta is not None and abs(alt_beta - 1.5) < 1e-6, alt_beta
    assert flat_beta is not None and abs(flat_beta) < 1e-6, flat_beta

    # Warm-up: a row before min_periods is null.
    early = out.filter((pl.col("symbol") == "ALT") & (pl.col("ts_ms") == 5 * _DAY))
    assert early["btc_beta"][0] is None


def test_btc_beta_no_btc_in_panel_returns_nulls() -> None:
    out = compute_btc_beta(_daily_returns({"ALT": [0.01, -0.02, 0.03] * 20}), window=60, min_periods=30)
    assert out["btc_beta"].is_null().all()


def test_btc_beta_empty_input() -> None:
    out = compute_btc_beta(pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "ret_1d": pl.Float64}))
    assert out.is_empty()
    assert set(out.columns) == {"symbol", "ts_ms", "btc_beta"}


def _write_klines_root(
    root: Path, *, symbols: list[str], days: int, seed: int = 11,
    dataset: str = "klines_1h", base: datetime | None = None,
) -> None:
    """Minimal synthetic kline root (storage layout: <dataset>/date=YYYY-MM-DD/part.parquet)."""
    rng = random.Random(seed)
    rows = []
    base = base or datetime(2025, 1, 1, tzinfo=timezone.utc)
    for sym in symbols:
        price = 100.0
        for d in range(days):
            for h in range(24):
                ts = base + timedelta(days=d, hours=h)
                o = price
                price *= 1 + rng.uniform(-0.02, 0.02)
                c = price
                rows.append({
                    "ts_ms": int(ts.timestamp() * 1000), "symbol": sym,
                    "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998,
                    "close": c, "volume_base": 1000.0, "turnover_quote": 1000.0 * c,
                    "date": ts.strftime("%Y-%m-%d"),
                })
    df = pl.DataFrame(rows)
    kdir = root / dataset
    for key, group in df.group_by("date"):
        part = kdir / f"date={key[0]}"
        part.mkdir(parents=True, exist_ok=True)
        group.write_parquet(part / "part.parquet")


def test_build_factor_panel_attaches_all_factor_columns(tmp_path: Path) -> None:
    _write_klines_root(tmp_path, symbols=["BTCUSDT", "AAA", "BBB"], days=40)
    panel = build_factor_panel(tmp_path, start="2025-01-10", end="2025-02-08")
    assert panel.height > 0
    for col in ["symbol", "ts_ms", "date", *_FACTOR_COLUMNS]:
        assert col in panel.columns, f"missing {col}; got {panel.columns}"
    assert set(panel["symbol"].unique().to_list()) <= {"BTCUSDT", "AAA", "BBB"}


def test_daily_factor_owner_matches_data_root_builder(tmp_path: Path) -> None:
    _write_klines_root(tmp_path, symbols=["BTCUSDT", "AAA", "BBB"], days=40)
    hourly = pl.read_parquet(sorted((tmp_path / "klines_1h").glob("**/*.parquet")))
    daily = _aggregate_daily_klines(hourly)

    from_root = build_factor_panel(tmp_path, start="2025-01-10", end="2025-02-08")
    from_daily = build_factor_panel_from_daily(
        daily,
        start="2025-01-10",
        end="2025-02-08",
    )

    assert_frame_equal(from_daily, from_root)


def test_build_factor_panel_honours_klines_dataset_override(tmp_path: Path) -> None:
    # Live demo/paper roots store WS klines under event_demo_klines_1h, NOT klines_1h. The
    # autodetect always returns klines_1h, so without the override the read is empty (the
    # continuous zero-signal blackout); the override must read the live store.
    _write_klines_root(tmp_path, symbols=["BTCUSDT", "AAA", "BBB"], days=40, dataset="event_demo_klines_1h")
    assert build_factor_panel(tmp_path, start="2025-01-10", end="2025-02-08").is_empty()  # autodetect -> klines_1h (absent)
    panel = build_factor_panel(
        tmp_path, start="2025-01-10", end="2025-02-08", klines_dataset="event_demo_klines_1h")
    assert panel.height > 0
    for col in ["symbol", "ts_ms", "date", *_FACTOR_COLUMNS]:
        assert col in panel.columns, f"missing {col}; got {panel.columns}"


def test_fit_factor_returns_recovers_known_loadings() -> None:
    # y = 0.01 + 2.0*f1 + 0.5*f2 exactly (no noise) => OLS recovers slopes, residual ~ 0.
    rng = random.Random(3)
    rows = []
    for ts in range(60):
        for s in range(30):
            f1 = rng.uniform(-1.0, 1.0)
            f2 = rng.uniform(-1.0, 1.0)
            rows.append({
                "symbol": f"S{s}", "ts_ms": ts * _DAY, "f1": f1, "f2": f2,
                "fwd_ret_1d": 0.01 + 2.0 * f1 + 0.5 * f2,
            })
    fr, resid = fit_factor_returns(pl.DataFrame(rows), factor_cols=["f1", "f2"])
    day = fr.filter(pl.col("ts_ms") == 30 * _DAY)
    assert abs(day.filter(pl.col("factor") == "f1")["factor_return"][0] - 2.0) < 1e-6
    assert abs(day.filter(pl.col("factor") == "f2")["factor_return"][0] - 0.5) < 1e-6
    assert resid["residual_return"].abs().max() < 1e-6


def test_fit_factor_returns_skips_thin_days_and_handles_empty() -> None:
    thin = pl.DataFrame([{"symbol": "A", "ts_ms": 0, "f1": 1.0, "fwd_ret_1d": 0.5}])
    fr, resid = fit_factor_returns(thin, factor_cols=["f1"])  # need=3 obs > 1 -> skipped
    assert fr.is_empty() and resid.is_empty()
    fr2, resid2 = fit_factor_returns(pl.DataFrame(), factor_cols=["f1"])
    assert fr2.is_empty() and resid2.is_empty()


def _load_precompute_module():
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "data" / "precompute_residual_momentum.py"
    scripts = str(path.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("precompute_residual_momentum", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_precompute_residual_momentum_reaches_today(tmp_path: Path) -> None:
    # The live continuous decile exact-joins residual_momentum on TODAY's day_ts, but residual_return
    # only completes ~2 days late -> without the trailing pad the table stops ~2 days back and the live
    # gate is silently empty (the zero-signal blackout). Klines run through a known last day;
    # `end` = that day + 1 ("tomorrow"), matching the live daily refresh.
    mod = _load_precompute_module()
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    days = 50
    last_day = base + timedelta(days=days - 1)
    end = (last_day + timedelta(days=1)).strftime("%Y-%m-%d")  # exclusive end = "tomorrow" UTC
    # Enough cross-section that the per-day common4 regression is over-determined (4 factors + intercept).
    symbols = ["BTCUSDT", *[f"S{i:02d}USDT" for i in range(20)]]
    _write_klines_root(tmp_path, symbols=symbols, days=days, dataset="event_demo_klines_1h", base=base)
    n = mod.precompute(tmp_path, start="2025-01-02", end=end, klines_dataset="event_demo_klines_1h")
    assert n > 0
    sig = pl.read_parquet(tmp_path / "residual_momentum.parquet")
    assert {"symbol", "ts_ms", "residual_momentum"} <= set(sig.columns)
    assert sig["residual_momentum"].is_finite().all()
    today_floor = (int(last_day.timestamp() * 1000) // _DAY) * _DAY
    # Blackout guard: the table must carry a row at "today" (the live join's day_ts), not stop ~2 days back.
    assert sig.filter(pl.col("ts_ms") == today_floor).height > 0, (
        f"no residual_momentum row for today {today_floor}; max={sig['ts_ms'].max()} -> live gate blackout")


def _daily_returns_dayidx(symbol_to_rets: dict[str, list[tuple[int, float]]]) -> pl.DataFrame:
    """symbol -> list of (day_index, ret_1d). Day index keys the 00:00-UTC grid."""
    rows = []
    for sym, pairs in symbol_to_rets.items():
        for d, r in pairs:
            rows.append({"symbol": sym, "ts_ms": d * _DAY, "ret_1d": r})
    return pl.DataFrame(rows)


def test_btc_beta_contiguous_matches_known_slope() -> None:
    # Numerical-equivalence guard: on a CONTIGUOUS series the calendar window must give
    # the same exact OLS slope the row-based window did (ALT = 1.5 * BTC -> beta 1.5).
    rng = random.Random(1)
    btc = [(d, rng.uniform(-0.05, 0.05)) for d in range(80)]
    alt = [(d, 1.5 * r) for d, r in btc]
    out = compute_btc_beta(_daily_returns_dayidx({"BTCUSDT": btc, "ALT": alt}), window=60, min_periods=30)
    last = out.filter((pl.col("symbol") == "ALT") & (pl.col("ts_ms") == 79 * _DAY))["btc_beta"][0]
    assert last is not None and abs(last - 1.5) < 1e-9, last


def test_btc_beta_gap_does_not_stretch_window_past_calendar_span() -> None:
    """A gapped symbol must not get a beta spanning more than ``window`` CALENDAR days.
    A row-based rolling window stitches pre-gap rows onto a far-later day and yields a
    stale beta; with the calendar window that day sees fewer than ``min_periods``
    calendar-recent rows and is correctly null.
    """
    # BTC present every day so a partner exists; ALT present only on days 0..29, then 200.
    btc = [(d, 0.01 if d % 2 else -0.01) for d in range(201)]
    alt_days = list(range(30)) + [200]
    alt = [(d, 0.015 if d % 2 else -0.015) for d in alt_days]
    out = compute_btc_beta(_daily_returns_dayidx({"BTCUSDT": btc, "ALT": alt}), window=60, min_periods=30)
    beta_200 = out.filter((pl.col("symbol") == "ALT") & (pl.col("ts_ms") == 200 * _DAY))["btc_beta"][0]
    # Only 1 calendar-recent ALT row within the trailing 60 days of day 200 (day 200
    # itself) -> below min_periods -> null. A row-based window would have reached back
    # to the pre-gap block and produced a (stale) non-null beta.
    assert beta_200 is None, beta_200
