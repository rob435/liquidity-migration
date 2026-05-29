from __future__ import annotations

import random

import polars as pl

from liquidity_migration.risk_model import compute_btc_beta

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
