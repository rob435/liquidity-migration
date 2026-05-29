"""R6 — per-name, per-bar cost model (Round 2).

Pre-reg: docs/preregistration/round2/integrated-strategy-program.md sub-phase R6.

Replaces the single flat ``cost_multiplier`` with a regression-calibrated surface
that varies predicted round-trip cost (bps) by name liquidity (size/ADV), volatility
regime, entry spread, time of day, and hold-period funding:

    predicted_cost_bps = alpha
                       + b_size    * (size_usd / ADV_30d)
                       + b_vol     * realized_vol_7d
                       + b_spread  * spread_proxy_bps
                       + b_hour    * hour_norm
                       + b_funding * (funding_rate * hold_hours / 8)

``alpha`` captures the base round-trip (taker fees + half-spread + slippage floor);
``b_*`` are calibrated PER VENUE (Bybit and Binance differ structurally) from matched
demo/paper-shadow execution data — see ``fit_cost_model``.

Built incrementally + test-gated. THIS commit lands the model surface + the OLS
fit/predict core (synthetic-data unit tests). Ledger recosting (legacy-flat vs model
side-by-side) and the ``--cost-model {flat,model}`` engine flag follow.

**Calibration is data-gated:** fitting ``b_*`` needs ≥30d of matched demo/paper-shadow
trades, which live on the deployment VPS, not the research box. Until calibrated,
``DEFAULT_PARAMS`` degrades to a flat ``alpha`` = 15 bps (the M2-hardened 100%-taker
round-trip, == ``CostConfig.base_entry_exit_cost_bps`` at ``maker_fill_probability=0``)
with all betas 0 — so ``--cost-model model`` is **never cheaper** than deployed taker
execution (no optimistic-fill bias; backtesting-errors #6/#7/#22).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import polars as pl

# Ordered regression features (design-matrix columns after the intercept). A ledger
# missing a column contributes 0 to that term, so a partial ledger still predicts on
# the terms it has.
COST_FEATURE_COLUMNS = [
    "size_adv_ratio",    # size_usd / ADV_30d           (market impact)
    "realized_vol_7d",   # 7d realized vol              (vol-regime spread widening)
    "spread_proxy_bps",  # entry half-spread proxy (bps)
    "hour_norm",         # hour-of-day / 23  in [0,1]   (liquidity-by-time)
    "funding_term",      # funding_rate * hold_hours / 8 (carry over the hold)
]

_FEATURE_TO_BETA = {
    "size_adv_ratio": "b_size",
    "realized_vol_7d": "b_vol",
    "spread_proxy_bps": "b_spread",
    "hour_norm": "b_hour",
    "funding_term": "b_funding",
}

# Deployed 100%-taker round-trip floor (bps): 2*(taker_fee 5.5 + taker_slippage 2.0)
# = 15 bps, == CostConfig.base_entry_exit_cost_bps at maker_fill_probability=0.0.
TAKER_ROUND_TRIP_BPS = 15.0


@dataclass(frozen=True)
class CostModelParams:
    """Per-venue fitted cost surface (the calibrated regression coefficients).

    ``cost_floor_bps`` is the conservative application floor: ``predict_cost_bps``
    never returns below it (a backtest must not assume cheaper-than-deployed fills).
    Default = the taker round-trip; lower it ONLY once passive execution (R12 sniper /
    limit-chase exit) is validated and the maker rebate is real.
    """

    alpha: float
    b_size: float = 0.0
    b_vol: float = 0.0
    b_spread: float = 0.0
    b_hour: float = 0.0
    b_funding: float = 0.0
    venue: str = ""
    cost_floor_bps: float = TAKER_ROUND_TRIP_BPS

    def coef_vector(self) -> np.ndarray:
        """[alpha, b_size, b_vol, b_spread, b_hour, b_funding] — design-matrix order."""
        return np.array(
            [self.alpha, self.b_size, self.b_vol, self.b_spread, self.b_hour, self.b_funding],
            dtype=float,
        )


# Uncalibrated default: flat at the taker round-trip floor (all betas 0). With these,
# `--cost-model model` == the hardened flat cost — a SAFE (never cheaper) stand-in
# until per-venue calibration from live demo/paper-shadow lands.
DEFAULT_PARAMS: dict[str, CostModelParams] = {
    "bybit": CostModelParams(alpha=TAKER_ROUND_TRIP_BPS, venue="bybit"),
    "binance": CostModelParams(alpha=TAKER_ROUND_TRIP_BPS, venue="binance"),
}


def predict_cost_bps(features: pl.DataFrame, params: CostModelParams) -> pl.Series:
    """Predicted round-trip cost (bps) per row via the functional form, floored.

    ``features`` should contain (a subset of) ``COST_FEATURE_COLUMNS``; absent or null
    cells contribute 0 to their term. The result is clamped to
    ``>= params.cost_floor_bps`` — cost can never beat the deployed round-trip.
    Returns a ``predicted_cost_bps`` Series aligned to ``features`` rows.
    """
    if features.is_empty():
        return pl.Series("predicted_cost_bps", [], dtype=pl.Float64)
    expr = pl.lit(float(params.alpha))
    betas = {
        "size_adv_ratio": params.b_size, "realized_vol_7d": params.b_vol,
        "spread_proxy_bps": params.b_spread, "hour_norm": params.b_hour,
        "funding_term": params.b_funding,
    }
    for col, b in betas.items():
        if b != 0.0 and col in features.columns:
            expr = expr + float(b) * pl.col(col).fill_null(0.0)
    # with_columns (not select) so a pure-literal expr broadcasts to frame height.
    out = features.with_columns(expr.alias("_pcb")).select(
        pl.col("_pcb").clip(lower_bound=float(params.cost_floor_bps)).alias("predicted_cost_bps")
    )
    return out["predicted_cost_bps"]


def fit_cost_model(
    observations: pl.DataFrame,
    *,
    venue: str = "",
    target_col: str = "realized_cost_bps",
    cost_floor_bps: float = TAKER_ROUND_TRIP_BPS,
) -> tuple[CostModelParams, dict]:
    """OLS of realized round-trip cost (bps) on the cost features (+ an intercept).

    ``observations`` carries ``target_col`` (the realized cost, e.g.
    ``(paper_exec - demo_exec) + funding_diff`` per matched trade, in bps) plus the
    ``COST_FEATURE_COLUMNS`` present. Features absent from the frame are excluded from
    the design (their beta stays 0). Returns ``(params, diagnostics)`` where
    diagnostics has ``n_obs, features, status, r2, pred_vs_realized_corr, rmse_bps``.

    The fit is UNfloored (it estimates the true surface); the floor is applied only at
    ``predict_cost_bps`` time. With too few observations (< n_features + 2) it returns
    the venue ``DEFAULT_PARAMS`` (flat taker floor) and ``status="insufficient_obs"`` —
    so an under-powered calibration degrades safely instead of overfitting noise.
    """
    present = [c for c in COST_FEATURE_COLUMNS if c in observations.columns]
    diag: dict = {"venue": venue, "features": present}
    if observations.is_empty() or target_col not in observations.columns:
        diag.update({"n_obs": 0, "status": "no_data"})
        return replace(DEFAULT_PARAMS.get(venue, CostModelParams(alpha=cost_floor_bps)), venue=venue, cost_floor_bps=cost_floor_bps), diag

    sub = observations.drop_nulls(subset=[target_col, *present])
    n = sub.height
    diag["n_obs"] = n
    if n < len(present) + 2:
        diag["status"] = "insufficient_obs"
        return replace(DEFAULT_PARAMS.get(venue, CostModelParams(alpha=cost_floor_bps)), venue=venue, cost_floor_bps=cost_floor_bps), diag

    x = np.column_stack([np.ones(n), sub.select(present).to_numpy()]) if present else np.ones((n, 1))
    y = sub[target_col].to_numpy().astype(float)
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    pred = x @ beta
    resid = y - pred
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    corr = (
        float(np.corrcoef(pred, y)[0, 1])
        if n >= 2 and pred.std() > 0 and y.std() > 0 else 0.0
    )
    kwargs = {_FEATURE_TO_BETA[c]: float(beta[i + 1]) for i, c in enumerate(present)}
    params = CostModelParams(alpha=float(beta[0]), venue=venue, cost_floor_bps=cost_floor_bps, **kwargs)
    diag.update({
        "status": "fitted", "r2": r2, "pred_vs_realized_corr": corr,
        "rmse_bps": float(np.sqrt((resid ** 2).mean())),
    })
    return params, diag


_RECOST_COLUMNS = ["model_cost_bps", "model_cost_return", "model_net_return", "legacy_cost_bps"]


def recost_trades(
    trades: pl.DataFrame,
    params: CostModelParams,
    *,
    weight_col: str = "notional_weight",
    gross_col: str = "gross_return",
    funding_col: str = "funding_return",
    legacy_cost_col: str = "cost_return",
) -> pl.DataFrame:
    """Recost a backtest trade ledger under the R6 model, alongside the legacy flat cost.

    The engine ledger (``volume_events`` trade rows) carries ``notional_weight``
    (= |effective_weight|), ``gross_return``, ``funding_return``, ``cost_return`` (the
    applied legacy flat cost leg), and ``net_return``. This keeps gross + funding fixed
    and swaps ONLY the cost leg for the model's per-trade prediction:

        model_cost_bps   = predict_cost_bps(trade_features, params)   (round-trip, floored)
        model_cost_return = -notional_weight * model_cost_bps / 1e4
        model_net_return  = gross_return + model_cost_return + funding_return

    Cost features (``COST_FEATURE_COLUMNS``) absent from ``trades`` contribute 0, so with
    ``DEFAULT_PARAMS`` this reduces to the flat 15 bps taker round-trip — a safe identity
    until per-venue calibration lands. ``legacy_cost_bps`` is recovered from the existing
    ``cost_return`` for a side-by-side bps comparison. The original columns are retained.
    """
    if trades.is_empty():
        return pl.DataFrame(schema={**trades.schema, **{c: pl.Float64 for c in _RECOST_COLUMNS}})

    out = trades.with_columns(predict_cost_bps(trades, params).alias("model_cost_bps"))
    weight = pl.col(weight_col).abs()
    out = out.with_columns(
        (-weight * pl.col("model_cost_bps") / 10_000.0).alias("model_cost_return")
    )
    funding = pl.col(funding_col).fill_null(0.0) if funding_col in out.columns else pl.lit(0.0)
    out = out.with_columns(
        (pl.col(gross_col) + pl.col("model_cost_return") + funding).alias("model_net_return")
    )
    if legacy_cost_col in out.columns:
        out = out.with_columns(
            pl.when(weight > 0).then(-pl.col(legacy_cost_col) / weight * 10_000.0).otherwise(None).alias("legacy_cost_bps")
        )
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Float64).alias("legacy_cost_bps"))
    return out


def summarize_recosting(recosted: pl.DataFrame, *, legacy_net_col: str = "net_return") -> dict:
    """Aggregate the legacy-vs-model cost comparison for a ``recost_trades`` output.

    Returns ``n_trades`` and per-cell means/totals: mean model & legacy cost (bps),
    total model & legacy net return, and ``mean_net_return_delta`` (model − legacy,
    negative = the model charges more than the legacy flat cost on this cell).
    """
    empty = {
        "n_trades": 0, "mean_model_cost_bps": None, "mean_legacy_cost_bps": None,
        "total_model_net_return": 0.0, "total_legacy_net_return": 0.0, "mean_net_return_delta": 0.0,
    }
    if recosted.is_empty() or "model_net_return" not in recosted.columns:
        return empty
    agg = recosted.select(
        pl.len().alias("n_trades"),
        pl.col("model_cost_bps").mean().alias("mean_model_cost_bps"),
        pl.col("legacy_cost_bps").mean().alias("mean_legacy_cost_bps"),
        pl.col("model_net_return").sum().alias("total_model_net_return"),
        pl.col(legacy_net_col).sum().alias("total_legacy_net_return"),
        (pl.col("model_net_return") - pl.col(legacy_net_col)).mean().alias("mean_net_return_delta"),
    ).row(0, named=True)
    return {k: (int(v) if k == "n_trades" else (float(v) if v is not None else None)) for k, v in agg.items()}
