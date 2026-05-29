"""R4 — risk-factor model for crypto-perp returns (Round 2).

Pre-reg: docs/preregistration/round2/integrated-strategy-program.md sub-phase R4.

Builds a per-(date, symbol) factor-exposure panel so every Round-2 strategy can be
evaluated on RESIDUAL alpha (the return NOT explained by exposure to known
systematic factors). Reuses signal_harness's daily-aggregation + cross-sectional
helpers (6 of the 8 factors already exist there as builders); BTC-beta and the
alt-season factor are new here, plus the cross-sectional factor-return regression
+ residualization.

Built incrementally + test-gated. THIS commit lands the panel scaffolding + the
BTC-beta factor (the most load-bearing new factor) + its unit test. Subsequent
commits add the remaining factors, `fit_factor_returns`, `compute_residual_returns`,
`decompose_strategy_pnl`, and the `risk-model` CLI.

All factor exposures are causal at each row's end-of-day-close decision_ts —
rolling windows look strictly backward; validated by the R4 validation run.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from liquidity_migration.signal_harness import (
    _aggregate_daily_klines,
    _attach_daily_returns,
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
    _xs_rank,
    build_feature_panel,
)

# The 6 R4 factors that already exist as signal_harness builders (reused as-is via
# build_feature_panel). realized_vol_7d is additionally cross-sectionally ranked
# below ("realized vol regime"). BTC-beta + alt-season are computed separately.
_REUSED_FACTOR_SPECS = [
    "xs_rank_ret_3d",      # XS 3d momentum
    "xs_rank_ret_30d",     # XS 30d momentum
    "realized_vol_7d",     # -> realized_vol_rank (vol regime)
    "funding_rate_z",      # funding-rate exposure
    "liquidity_rank",      # liquidity tier (log-ADV rank)
    "premium_index_z",     # mark-index premium
]

_FACTOR_COLUMNS = [
    "btc_beta", "xs_rank_ret_3d", "xs_rank_ret_30d", "realized_vol_rank",
    "funding_rate_z", "liquidity_rank", "premium_index_z",
]

MS_PER_DAY = 86_400_000
BTC_BETA_WINDOW = 60      # trailing trading-day window for the rolling OLS beta
BTC_BETA_MIN_PERIODS = 30


def compute_btc_beta(
    daily_returns: pl.DataFrame,
    *,
    btc_symbol: str = "BTCUSDT",
    window: int = BTC_BETA_WINDOW,
    min_periods: int = BTC_BETA_MIN_PERIODS,
) -> pl.DataFrame:
    """Rolling-window OLS beta of each symbol's daily return on BTC's daily return.

    ``daily_returns`` has columns ``symbol, ts_ms, ret_1d`` (the
    ``signal_harness._attach_daily_returns`` output). Returns ``symbol, ts_ms,
    btc_beta``: at each (symbol, ts_ms) ``btc_beta`` is the OLS slope over the
    trailing ``window`` rows (with at least ``min_periods``), computed causally via
    the rolling-moment identity beta = Cov(x, y) / Var(y) with
    Cov = E[xy] - E[x]E[y], Var = E[y^2] - E[y]^2, y = BTC return. Warm-up rows
    (< ``min_periods``) and degenerate Var(y)≈0 rows are null. BTC's own beta is 1
    by construction but is left to the cross-section (not special-cased).
    """
    if daily_returns.is_empty() or "ret_1d" not in daily_returns.columns:
        return pl.DataFrame(schema={"symbol": pl.String, "ts_ms": pl.Int64, "btc_beta": pl.Float64})
    btc = (
        daily_returns.filter(pl.col("symbol") == btc_symbol)
        .select("ts_ms", pl.col("ret_1d").alias("_btc_ret"))
        .unique(subset="ts_ms")
    )
    if btc.is_empty():
        # No BTC in the panel -> beta undefined; emit nulls (caller decides).
        return daily_returns.select(
            "symbol", "ts_ms", pl.lit(None, dtype=pl.Float64).alias("btc_beta")
        )
    df = (
        daily_returns.join(btc, on="ts_ms", how="inner")
        .filter(pl.col("ret_1d").is_not_null() & pl.col("_btc_ret").is_not_null())
        .sort(["symbol", "ts_ms"])
        .with_columns(
            (pl.col("ret_1d") * pl.col("_btc_ret")).alias("_xy"),
            (pl.col("_btc_ret") * pl.col("_btc_ret")).alias("_yy"),
        )
    )
    df = df.with_columns(
        pl.col("ret_1d").rolling_mean(window_size=window, min_samples=min_periods).over("symbol").alias("_ex"),
        pl.col("_btc_ret").rolling_mean(window_size=window, min_samples=min_periods).over("symbol").alias("_ey"),
        pl.col("_xy").rolling_mean(window_size=window, min_samples=min_periods).over("symbol").alias("_exy"),
        pl.col("_yy").rolling_mean(window_size=window, min_samples=min_periods).over("symbol").alias("_eyy"),
    )
    var_y = pl.col("_eyy") - pl.col("_ey") * pl.col("_ey")
    cov_xy = pl.col("_exy") - pl.col("_ex") * pl.col("_ey")
    return df.with_columns(
        pl.when(var_y.abs() > 1e-12).then(cov_xy / var_y).otherwise(None).alias("btc_beta")
    ).select("symbol", "ts_ms", "btc_beta")


def build_factor_panel(
    data_root: str | Path,
    *,
    start: str,
    end: str,
    btc_symbol: str = "BTCUSDT",
) -> pl.DataFrame:
    """Build the per-(symbol, ts_ms, date) factor-exposure panel, full-PIT.

    Reads the venue's klines from ``data_root`` (a ``*_full_pit`` root => the full
    delisted-inclusive PIT universe => PIT-clean cross-sections), aggregates to
    daily bars, and attaches factor exposures. Pads 90d back so the rolling-60
    betas warm up; the returned panel covers [start, end).

    Attaches 7 factor exposures: the 6 reused signal_harness factors (via
    ``build_feature_panel``) + ``btc_beta``. ``realized_vol_7d`` is converted to
    its cross-sectional rank (``realized_vol_rank``). The 8th planned factor
    (alt-season) is deferred — 7 factors already meets the plan's 5-6 stable
    target; add it later only if R4 validation calls for it.
    """
    feat = build_feature_panel(
        data_root, start=start, end=end,
        feature_specs=",".join(_REUSED_FACTOR_SPECS), forward_horizons=(1,),
    )
    if feat.is_empty():
        return pl.DataFrame()
    # "Realized vol regime" = cross-sectional rank of 7d realized vol (per the plan).
    feat = _xs_rank(feat, "realized_vol_7d", out_col="realized_vol_rank")

    # BTC-beta needs the backward daily-return series, which build_feature_panel
    # does not expose -> a lightweight klines-only second read (padded for warm-up).
    start_ms = _date_str_to_ms(start)
    end_ms = _date_str_to_ms(end)
    klines_name = _autodetect_dataset_names(data_root)["klines_dataset"]
    klines_1h = _read_window(
        data_root, klines_name, start_ms=start_ms - 90 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote", "date"],
    )
    if klines_1h.is_empty():
        feat = feat.with_columns(pl.lit(None, dtype=pl.Float64).alias("btc_beta"))
    else:
        daily_returns = _attach_daily_returns(_aggregate_daily_klines(klines_1h))
        feat = feat.join(compute_btc_beta(daily_returns, btc_symbol=btc_symbol), on=["symbol", "ts_ms"], how="left")

    keep = ["symbol", "ts_ms", "date"] + [c for c in _FACTOR_COLUMNS if c in feat.columns]
    return feat.select(keep).sort(["ts_ms", "symbol"])
