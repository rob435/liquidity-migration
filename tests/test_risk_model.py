from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

from liquidity_migration.risk_model import (
    _FACTOR_COLUMNS,
    build_factor_panel,
    compute_btc_beta,
    decompose_strategy_pnl,
    fit_factor_returns,
    residual_variance_capture,
)

_DAY = 86_400_000


def _factor_panel(n_days: int, n_syms: int, *, signal_factor: str | None = None, seed: int = 0) -> pl.DataFrame:
    """Synthetic factor-exposure panel with all _FACTOR_COLUMNS + fwd_ret_1d.

    ``signal_factor=None`` => target is pure noise (factors carry no information);
    otherwise target = 0.3 * that factor + noise (a real, detectable relation)."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        ts = d * _DAY
        for s in range(n_syms):
            vals = {c: float(rng.normal()) for c in _FACTOR_COLUMNS}
            vals["liquidity_rank"] = float(s + 1)
            tgt = (0.3 * vals[signal_factor] if signal_factor else 0.0) + float(rng.normal() * 0.02)
            rows.append({"symbol": f"S{s}", "ts_ms": ts, **vals, "fwd_ret_1d": tgt})
    return pl.DataFrame(rows)


def test_residual_variance_capture_noise_model_does_not_capture() -> None:
    # The OLD check (residual_std < raw_std) is an in-sample R^2>=0 tautology that
    # passes EVERY noise panel. The permutation null instead has a ~5% false-positive
    # rate BY CONSTRUCTION, so assert the rate across many seeds is low (a broken /
    # tautological gate would flag capture on every noise panel).
    flags = []
    for s in range(20):
        panel = _factor_panel(45, 25, signal_factor=None, seed=100 + s)
        vc = residual_variance_capture(panel, n_permutations=50, seed=s)
        assert vc["residual_std_over_raw"] < 1.0  # in-sample tautology still holds
        flags.append(vc["captures_real_variance"])
    # Expected ~1/20 false positives; <=5 is a safe bound that still catches a gate
    # that calls every noise model "real" (the bug A1 fixes). P(false fail) ~ 3e-4.
    assert sum(flags) <= 5, f"noise false-positive rate too high: {sum(flags)}/20"


def test_residual_variance_capture_real_signal_is_detected() -> None:
    # A genuine factor->return relation must be detected on every realization.
    for s in range(5):
        panel = _factor_panel(45, 25, signal_factor="btc_beta", seed=200 + s)
        vc = residual_variance_capture(panel, n_permutations=50, seed=s)
        assert vc["captures_real_variance"] is True
        assert vc["p_value"] < 0.05
        assert vc["residual_std_over_raw"] < vc["null_ratio_p05"]  # beats the null's best


def test_decompose_strategy_pnl_snaps_offgrid_entry_to_daily_grid() -> None:
    # Engine ledger entries are +1h off the 00:00-UTC panel grid; they must still
    # resolve (pre-fix they missed every lookup -> all-null -> inflated residual).
    panel = _factor_panel(40, 20, signal_factor="btc_beta", seed=3)
    fr, _resid = fit_factor_returns(panel)
    loadings = panel.select(["symbol", "ts_ms", *_FACTOR_COLUMNS])
    trades = pl.DataFrame({
        "symbol": ["S1", "S2"],
        "entry_ts_ms": [10 * _DAY + 3_600_000, 11 * _DAY + 3_600_000],
        "hold_days": [2, 2],
        "realized_return": [0.05, -0.03],
    })
    dec = decompose_strategy_pnl(trades, loadings, fr)
    assert dec["resolved_fraction"] == 1.0
    assert dec["n_unresolved"] == 0
    assert dec["per_trade"]["explained"].drop_nulls().len() == 2


def test_decompose_strategy_pnl_no_factor_returns_is_null_not_zero() -> None:
    # Exposure resolves but no factor-return rows over the hold -> null (NOT 0.0,
    # which would mis-book the whole realized return as residual alpha).
    panel = _factor_panel(40, 20, signal_factor="btc_beta", seed=4)
    loadings = panel.select(["symbol", "ts_ms", *_FACTOR_COLUMNS])
    empty_fr = pl.DataFrame(schema={"ts_ms": pl.Int64, "factor": pl.String, "factor_return": pl.Float64})
    trades = pl.DataFrame({"symbol": ["S1"], "entry_ts_ms": [10 * _DAY], "hold_days": [2], "realized_return": [0.05]})
    dec = decompose_strategy_pnl(trades, loadings, empty_fr)
    assert dec["n_unresolved"] == 1
    assert dec["per_trade"]["explained"][0] is None
    assert dec["per_trade"]["residual"][0] is None


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


def test_decompose_strategy_pnl_splits_explained_and_residual() -> None:
    # 1 factor f1; trade A entered ts=0, hold 2d, exposure 2.0, realized 0.10.
    # factor returns: day0=0.01, day1=0.02 -> cum 0.03 -> explained 2.0*0.03=0.06 -> residual 0.04.
    loadings = pl.DataFrame([{"symbol": "A", "ts_ms": 0, "f1": 2.0}])
    fr = pl.DataFrame([
        {"ts_ms": 0, "factor": "f1", "factor_return": 0.01},
        {"ts_ms": _DAY, "factor": "f1", "factor_return": 0.02},
    ])
    trades = pl.DataFrame([{"symbol": "A", "entry_ts_ms": 0, "hold_days": 2, "realized_return": 0.10}])
    out = decompose_strategy_pnl(trades, loadings, fr, factor_cols=["f1"])
    row = out["per_trade"].row(0, named=True)
    assert abs(row["explained"] - 0.06) < 1e-9, row
    assert abs(row["residual"] - 0.04) < 1e-9, row
    assert out["n_trades"] == 1


def test_decompose_strategy_pnl_missing_exposure_is_null() -> None:
    loadings = pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "f1": pl.Float64})
    fr = pl.DataFrame(schema={"ts_ms": pl.Int64, "factor": pl.String, "factor_return": pl.Float64})
    trades = pl.DataFrame([{"symbol": "A", "entry_ts_ms": 0, "hold_days": 2, "realized_return": 0.1}])
    out = decompose_strategy_pnl(trades, loadings, fr, factor_cols=["f1"])
    assert out["per_trade"].row(0, named=True)["explained"] is None
    assert out["residual_sharpe"] == 0.0
