from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from liquidity_migration.risk_model import (
    _FACTOR_COLUMNS,
    build_factor_panel,
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


def _write_klines_root(root: Path, *, symbols: list[str], days: int, seed: int = 11) -> None:
    """Minimal synthetic klines_1h root (storage layout: date=YYYY-MM-DD/part.parquet)."""
    rng = random.Random(seed)
    rows = []
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
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
    kdir = root / "klines_1h"
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
