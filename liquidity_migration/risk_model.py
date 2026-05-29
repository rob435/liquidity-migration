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

import numpy as np
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

# The 5 R4 factors that already exist as signal_harness builders (reused as-is via
# build_feature_panel). realized_vol_7d is additionally cross-sectionally ranked
# below ("realized vol regime"). BTC-beta is computed separately. xs_rank_ret_3d
# was DROPPED by the R4 validation (2026-05-29): sign-inconsistent factor-return
# Sharpe across venues (-0.47 bybit / +0.50 binance) => criterion-1 failure, not a
# stable priced factor. See docs/preregistration/round2/r4-risk-model-verdict.md.
_REUSED_FACTOR_SPECS = [
    "xs_rank_ret_30d",     # XS 30d momentum
    "realized_vol_7d",     # -> realized_vol_rank (vol regime)
    "funding_rate_z",      # funding-rate exposure
    "liquidity_rank",      # liquidity tier (log-ADV rank)
    "premium_index_z",     # mark-index premium
]

_FACTOR_COLUMNS = [
    "btc_beta", "xs_rank_ret_30d", "realized_vol_rank",
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

    Attaches 6 factor exposures: the 5 reused signal_harness factors (via
    ``build_feature_panel``) + ``btc_beta``. ``realized_vol_7d`` is converted to
    its cross-sectional rank (``realized_vol_rank``). The R4 validation
    (2026-05-29) pruned ``xs_rank_ret_3d`` (sign-inconsistent factor return across
    venues) and deferred the alt-season factor — 6 stable, sign-consistent factors
    meet the plan's 5-6 target. See r4-risk-model-verdict.md.
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
    if "fwd_ret_1d" in feat.columns:  # regression target for fit_factor_returns
        keep.append("fwd_ret_1d")
    return feat.select(keep).sort(["ts_ms", "symbol"])


def fit_factor_returns(
    panel: pl.DataFrame,
    *,
    factor_cols: list[str] | None = None,
    target_col: str = "fwd_ret_1d",
    min_obs_per_day: int | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Per-day cross-sectional OLS of realized return on factor exposures.

    For each ``ts_ms``, regress ``target_col`` (the realized forward return) on the
    factor-loading columns (+ an intercept) across that day's eligible symbols.

    Returns ``(factor_returns, residuals)``:
      * ``factor_returns``: ``(ts_ms, factor, factor_return)`` — the OLS slope per
        factor per day (the factor's realized return that day).
      * ``residuals``: ``(symbol, ts_ms, residual_return)`` — ``y - X @ beta``, the
        return NOT explained by factor exposure = candidate alpha. A strategy
        cell's residual Sharpe (Tier-3 gate) is computed from these.

    Days with fewer than ``min_obs_per_day`` eligible symbols (default
    ``len(factor_cols) + 2``) are skipped; rows with any null factor/target are
    dropped per day. PIT-clean: factor loadings are as-of decision_ts; the target
    is the strictly-forward return.

    FORWARD-SURVIVORSHIP NOTE: a symbol's terminal trading day before a
    delist/data-gap has a null ``target_col`` (no strictly-forward return) and is
    therefore dropped from that day's regression. This is unavoidable — you cannot
    regress on a return that does not exist — but it means each daily factor-return
    is estimated only over symbols that survived to the next day. The R4 validation
    run surfaces the dropped-row count so this exposure is visible rather than
    silent; treat factor returns near heavy-delisting windows with that caveat.
    """
    cols = list(factor_cols) if factor_cols is not None else list(_FACTOR_COLUMNS)
    f_schema = {"ts_ms": pl.Int64, "factor": pl.String, "factor_return": pl.Float64}
    r_schema = {"symbol": pl.String, "ts_ms": pl.Int64, "residual_return": pl.Float64}
    if panel.is_empty() or target_col not in panel.columns:
        return pl.DataFrame(schema=f_schema), pl.DataFrame(schema=r_schema)
    present = [c for c in cols if c in panel.columns]
    if not present:
        return pl.DataFrame(schema=f_schema), pl.DataFrame(schema=r_schema)
    need = min_obs_per_day if min_obs_per_day is not None else len(present) + 2

    factor_records: list[dict] = []
    resid_records: list[dict] = []
    for key, day in panel.group_by("ts_ms"):
        ts = int(key[0])
        sub = day.drop_nulls(subset=[*present, target_col])
        if sub.height < need:
            continue
        x = np.column_stack([np.ones(sub.height), sub.select(present).to_numpy()])
        y = sub[target_col].to_numpy()
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        resid = y - x @ beta
        for i, fc in enumerate(present):
            factor_records.append({"ts_ms": ts, "factor": fc, "factor_return": float(beta[i + 1])})
        for sym, rr in zip(sub["symbol"].to_list(), resid.tolist()):
            resid_records.append({"symbol": sym, "ts_ms": ts, "residual_return": float(rr)})

    factor_returns = pl.DataFrame(factor_records, schema=f_schema) if factor_records else pl.DataFrame(schema=f_schema)
    residuals = pl.DataFrame(resid_records, schema=r_schema) if resid_records else pl.DataFrame(schema=r_schema)
    return factor_returns, residuals


def residual_variance_capture(
    panel: pl.DataFrame,
    *,
    factor_cols: list[str] | None = None,
    target_col: str = "fwd_ret_1d",
    min_obs_per_day: int | None = None,
    n_permutations: int = 200,
    seed: int = 0,
) -> dict:
    """Honest test of whether the factor model captures REAL forward-return variance.

    The naive ``residual_std < raw_std`` check is an in-sample tautology: per-day
    OLS *with an intercept* guarantees R^2 >= 0, so residual variance is
    mechanically <= target variance regardless of whether any factor carries
    forward-predictive information — the comparison passes even for a pure-noise
    factor model. This builds a permutation NULL instead: for each cross-section
    the target is shuffled across symbols (destroying any factor->return relation
    while preserving that day's return distribution), the residual std is
    recomputed, and the real residual std is compared against the null
    distribution. The model captures real variance only if the actual residual std
    is below what chance shuffling achieves (``p_value < 0.05``), i.e. it reduces
    variance by MORE than fitting the same number of parameters to noise does.

    Both ``raw_std`` and ``residual_std`` are over the IDENTICAL surviving
    (symbol, ts_ms) rows used in the regression — fixing the prior whole-panel
    (raw) vs regression-subset (residual) population mismatch. ``raw_std`` is
    invariant to the within-day shuffle, so the null shares one denominator.
    """
    cols = list(factor_cols) if factor_cols is not None else list(_FACTOR_COLUMNS)
    present = [c for c in cols if c in panel.columns]
    empty = {
        "raw_std": 0.0, "residual_std": 0.0, "residual_std_over_raw": None,
        "null_ratio_p05": None, "p_value": None, "captures_real_variance": False,
        "n_obs": 0, "n_days": 0, "n_permutations": int(max(0, n_permutations)),
    }
    if panel.is_empty() or target_col not in panel.columns or not present:
        return empty
    need = min_obs_per_day if min_obs_per_day is not None else len(present) + 2

    # Precompute per day: the design matrix X (+ intercept) and its pseudo-inverse.
    # X is FIXED across permutations (only y is shuffled), so caching pinv(X) turns
    # each null fit into two cheap matmuls instead of a fresh lstsq.
    day_ops: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for _key, day in panel.group_by("ts_ms"):
        sub = day.drop_nulls(subset=[*present, target_col])
        if sub.height < need:
            continue
        x = np.column_stack([np.ones(sub.height), sub.select(present).to_numpy()])
        y = sub[target_col].to_numpy()
        day_ops.append((x, np.linalg.pinv(x), y))
    if not day_ops:
        return empty

    def _resid_std(shuffle: bool, rng: np.random.Generator) -> float:
        chunks = [
            (yy := (rng.permutation(y) if shuffle else y)) - x @ (pinv @ yy)
            for x, pinv, y in day_ops
        ]
        return float(np.concatenate(chunks).std())

    raw = np.concatenate([y for _x, _p, y in day_ops])
    raw_std = float(raw.std())
    rng = np.random.default_rng(seed)
    real_res_std = _resid_std(False, rng)
    n_perm = max(0, int(n_permutations))
    null = np.array([_resid_std(True, rng) for _ in range(n_perm)]) if n_perm else np.array([])
    p_value = float(np.mean(null <= real_res_std)) if null.size else None
    null_p05 = float(np.percentile(null, 5)) if null.size else None
    return {
        "raw_std": raw_std,
        "residual_std": real_res_std,
        "residual_std_over_raw": (real_res_std / raw_std) if raw_std > 0 else None,
        "null_ratio_p05": (null_p05 / raw_std) if (null_p05 is not None and raw_std > 0) else None,
        "p_value": p_value,
        "captures_real_variance": bool(p_value is not None and p_value < 0.05),
        "n_obs": int(raw.size),
        "n_days": len(day_ops),
        "n_permutations": n_perm,
    }


def decompose_strategy_pnl(
    trades: pl.DataFrame,
    factor_loadings: pl.DataFrame,
    factor_returns: pl.DataFrame,
    *,
    factor_cols: list[str] | None = None,
) -> dict:
    """Decompose each trade's realized return into factor-explained + residual.

    Inputs:
      * ``trades``: ``symbol, entry_ts_ms, hold_days, realized_return`` (the
        normalized per-trade ledger; the caller adapts the engine's trade ledger).
      * ``factor_loadings``: ``symbol, ts_ms, <factor_cols>`` (``build_factor_panel``).
      * ``factor_returns``: ``ts_ms, factor, factor_return`` (``fit_factor_returns``).

    For each trade: ``entry_ts_ms`` is SNAPPED to the 00:00-UTC daily grid the
    factor panel is keyed on (the engine ledger carries a +1h-delayed, bar-end
    entry that would otherwise miss every exact-ts lookup); exposure = the factor
    loadings at (symbol, snapped-entry); the cumulative factor return over the hold
    = sum of each factor's daily return over the ``hold_days`` daily steps; explained
    = exposure · cum_factor_return; residual = realized_return - explained (the part
    NOT explained by factor exposure = candidate alpha). Trades whose snapped entry
    has no loading row, OR whose hold spans no day with a factor-return row, get
    null explained/residual (NOT explained=0.0 — that would mis-book the entire
    realized return as residual alpha and silently inflate the residual Sharpe).

    Returns a dict: ``per_trade`` (symbol, entry_ts_ms, realized_return, explained,
    residual), ``n_trades``, ``mean_residual``, ``residual_sharpe`` =
    mean(residual)/std(residual) over trades (PER-TRADE, un-annualized; the Tier-3
    gate annualizes by sqrt(trades/yr) with the cell's actual trade span),
    ``n_unresolved`` (trades that could not be decomposed -> null), and
    ``resolved_fraction`` (decomposed trades / n_trades — a low value means the
    loadings/returns grids do not line up with the trade ledger and the residual
    Sharpe is not trustworthy).
    """
    cols = list(factor_cols) if factor_cols is not None else list(_FACTOR_COLUMNS)
    present = [c for c in cols if c in factor_loadings.columns]
    pt_schema = {
        "symbol": pl.String, "entry_ts_ms": pl.Int64, "realized_return": pl.Float64,
        "explained": pl.Float64, "residual": pl.Float64,
    }
    if trades.is_empty() or not present:
        return {"per_trade": pl.DataFrame(schema=pt_schema), "n_trades": 0, "mean_residual": 0.0, "residual_sharpe": 0.0}

    load_map: dict[tuple, dict] = {
        (row["symbol"], row["ts_ms"]): {f: row.get(f) for f in present}
        for row in factor_loadings.iter_rows(named=True)
    }
    fr_map: dict[int, dict] = {}
    for row in factor_returns.iter_rows(named=True):
        fr_map.setdefault(row["ts_ms"], {})[row["factor"]] = row["factor_return"]

    records: list[dict] = []
    n_unresolved = 0
    for t in trades.iter_rows(named=True):
        sym, ets, hd = t["symbol"], int(t["entry_ts_ms"]), int(t["hold_days"])
        realized = float(t["realized_return"])
        # Snap to the 00:00-UTC daily grid (build_factor_panel keys ts_ms to the
        # day's first hourly bar start; the engine ledger entry is +1h/bar-end).
        grid = (ets // MS_PER_DAY) * MS_PER_DAY
        exposure = load_map.get((sym, grid))
        if exposure is None:
            n_unresolved += 1
            records.append({"symbol": sym, "entry_ts_ms": ets, "realized_return": realized, "explained": None, "residual": None})
            continue
        present_days = [grid + k * MS_PER_DAY for k in range(max(hd, 1)) if (grid + k * MS_PER_DAY) in fr_map]
        if not present_days:
            n_unresolved += 1
            records.append({"symbol": sym, "entry_ts_ms": ets, "realized_return": realized, "explained": None, "residual": None})
            continue
        explained = 0.0
        for f in present:
            ef = exposure.get(f)
            if ef is None:
                continue
            cum = sum(fr_map[d].get(f) or 0.0 for d in present_days)
            explained += float(ef) * cum
        records.append({"symbol": sym, "entry_ts_ms": ets, "realized_return": realized, "explained": explained, "residual": realized - explained})

    per_trade = pl.DataFrame(records, schema=pt_schema)
    resid = per_trade["residual"].drop_nulls().to_numpy()
    if resid.size >= 2 and float(resid.std(ddof=1)) > 0.0:
        residual_sharpe = float(resid.mean() / resid.std(ddof=1))
    else:
        residual_sharpe = 0.0
    return {
        "per_trade": per_trade,
        "n_trades": int(per_trade.height),
        "mean_residual": float(resid.mean()) if resid.size else 0.0,
        "residual_sharpe": residual_sharpe,
        "n_unresolved": int(n_unresolved),
        "resolved_fraction": (resid.size / per_trade.height) if per_trade.height else 0.0,
    }
