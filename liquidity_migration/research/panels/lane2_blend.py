"""Executable form of ``configs/lane2_premium_momentum_blend_v1.json``: panel in,
daily score row out.

Two modelling choices change the answer and are load-bearing:

* Funding is charged at settlements, not pro rata. It is a discrete cash event
  at 00/08/16 UTC; pro-rating inverts which signal carries the book.
* Each leg is averaged separately and then differenced (via
  :mod:`liquidity_migration.research.panels.cross_section`). Pooling the tails, or an uncentred
  ``rank/len`` percentile, gives them different name counts and makes a
  market-neutral book directional.

The volatility scale for day ``t`` uses returns strictly before ``t``, kept
explicit rather than folded into a rolling helper.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.research.panels.cross_section import MEASURED_ROUND_TRIP_BP, long_short, top_by

HOUR_MS = 3_600_000
#: Bybit settles funding every 8 hours. A joined rate younger than one hour marks
#: the settlement itself; anything older is the same rate carried forward and must
#: not be charged twice.
FRESH_FUNDING_MAX_AGE_H = 1.0

REQUIRED_COLUMNS = (
    "symbol",
    "bar_ts_ms",
    "by_close",
    "by_turnover_quote",
    "by_funding",
    "by_funding_age_h",
    "premium_diff_bp",
)


@dataclasses.dataclass(frozen=True)
class BlendConfig:
    """The committed rule. Field names mirror the JSON so drift is visible."""

    config_id: str
    universe_top_n: int
    hold_hours: int
    momentum_lookback_hours: int
    decile_cut: float
    premium_weight: float
    momentum_weight: float
    maker_round_trip_bp: float
    measured_round_trip_bp: float
    vol_target_annual: float
    vol_lookback_days: int
    max_leverage: float

    @property
    def cost_basis_bp(self) -> float:
        """Default cost basis: the measured round trip, not the registered 4 bp
        maker assumption. ``maker_round_trip_bp`` is retained so the
        as-registered number stays reproducible."""
        return self.measured_round_trip_bp

    @classmethod
    def from_json(cls, path: str | Path) -> "BlendConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        signals = {s["name"]: s for s in rule["signals"]}
        return cls(
            config_id=payload["config_id"],
            universe_top_n=int(rule["universe"]["top_n"]),
            hold_hours=int(rule["entries"]["hold_hours"]),
            momentum_lookback_hours=168,
            decile_cut=float(rule["book"]["decile_cut"]),
            premium_weight=float(signals["premium_diff_bp"]["weight"]),
            momentum_weight=float(signals["momentum_1w"]["weight"]),
            maker_round_trip_bp=float(payload["cost_model"]["maker_round_trip_bp"]),
            measured_round_trip_bp=float(
                payload["cost_model"].get("MEASURED_round_trip_bp", MEASURED_ROUND_TRIP_BP)
            ),
            vol_target_annual=float(rule["risk"]["vol_target_annual"]),
            vol_lookback_days=int(rule["risk"]["vol_lookback_days"]),
            max_leverage=float(rule["risk"]["max_leverage"]),
        )


def settlement_exact_funding(hold_hours: int) -> pl.Expr:
    """Funding a LONG pays over ``(t, t + hold_hours]``.

    Only settlements landing inside the window are charged. ``rolling_sum(h)``
    evaluated at ``t + h`` covers rows ``t+1 .. t+h``, which is exactly the open
    interval we want, so the shift is by a full ``h`` rather than ``h - 1``.
    """
    age = pl.col("by_funding_age_h")
    # A settlement bar has age ~0, or an age DROP versus the prior bar when the
    # settlement bar itself is missing. The half-window keeps the next bar's
    # float-epsilon age (0.9999999999999999) from being charged a second time.
    is_settlement = (age < FRESH_FUNDING_MAX_AGE_H / 2.0) | (
        age < age.shift(1).over("symbol")
    ).fill_null(False)
    fresh = pl.when(is_settlement).then(pl.col("by_funding")).otherwise(0.0)
    # Shift inside the window, or each symbol's tail reads the next symbol's sums.
    return fresh.rolling_sum(hold_hours).shift(-hold_hours).over("symbol")


def prepare(panel: pl.DataFrame, cfg: BlendConfig) -> pl.DataFrame:
    """Attach forward return, settlement-exact funding, momentum, and liquidity."""
    missing = [c for c in REQUIRED_COLUMNS if c not in panel.columns]
    if missing:
        raise ValueError(f"panel is missing required columns: {missing}")

    h = cfg.hold_hours
    close = pl.col("by_close")
    frame = panel.filter(close > 0).sort(["symbol", "bar_ts_ms"])
    frame = frame.with_columns(
        [
            pl.col("by_turnover_quote").rolling_sum(24).over("symbol").alias("adv24"),
            settlement_exact_funding(h).alias("funding_paid"),
        ]
    )
    frame = frame.with_columns(
        [
            (close.shift(-h).over("symbol") / close - 1.0).alias("price_return"),
            (close / close.shift(cfg.momentum_lookback_hours).over("symbol") - 1.0).alias("momentum_1w"),
            # A contiguous forward window: without this a gap in the panel would
            # silently become a longer, unearned holding period.
            (pl.col("bar_ts_ms").shift(-h).over("symbol") - pl.col("bar_ts_ms") == h * HOUR_MS).alias("contiguous"),
        ]
    )
    frame = frame.filter(
        pl.col("contiguous")
        & pl.col("price_return").is_finite()
        & pl.col("momentum_1w").is_finite()
        & pl.col("funding_paid").is_finite()
    )
    return frame.with_columns((pl.col("price_return") - pl.col("funding_paid")).alias("net_return"))


def daily_book(
    prepared: pl.DataFrame, cfg: BlendConfig, *, cost_bp: float | None = None
) -> pl.DataFrame:
    """One row per decision day: the blended net book return in bp.

    Entries are sampled every ``hold_hours`` from the first observation so the
    holding windows never overlap; overlapping entries leave the mean unbiased
    but badly inflate its t-statistic.

    ``cost_bp`` defaults to :attr:`BlendConfig.cost_basis_bp` — the measured
    round trip. Pass ``cfg.maker_round_trip_bp`` to reproduce the as-registered
    4 bp figures.
    """
    universe = top_by(prepared, "adv24", cfg.universe_top_n)
    if universe.height == 0:
        return pl.DataFrame({"bar_ts_ms": [], "ret_bp": []})
    origin = int(universe["bar_ts_ms"].min())  # type: ignore[arg-type]  # polars min() is Any-typed
    universe = universe.with_columns(
        ((pl.col("bar_ts_ms") - origin) // HOUR_MS).alias("_offset")
    ).filter(pl.col("_offset") % cfg.hold_hours == 0)

    premium = long_short(
        universe, signal="premium_diff_bp", ret="net_return", sign=+1, cut=cfg.decile_cut
    ).rename({"ret_bp": "premium_bp"})
    momentum = long_short(
        universe, signal="momentum_1w", ret="net_return", sign=-1, cut=cfg.decile_cut
    ).rename({"ret_bp": "momentum_bp"})

    charged = cfg.cost_basis_bp if cost_bp is None else float(cost_bp)
    joined = premium.join(momentum, on="bar_ts_ms", how="inner").sort("bar_ts_ms")
    gross = cfg.premium_weight * pl.col("premium_bp") + cfg.momentum_weight * pl.col("momentum_bp")
    return joined.with_columns((gross - charged).alias("ret_bp"))


def volatility_scale(ret_bp: np.ndarray, cfg: BlendConfig) -> np.ndarray:
    """Leverage per day from volatility measured strictly before that day.

    Days without a full lookback get scale 0, not an unscaled position.
    """
    returns = np.asarray(ret_bp, dtype=float) / 1e4
    scale = np.zeros(len(returns))
    for i in range(cfg.vol_lookback_days, len(returns)):
        prior = returns[i - cfg.vol_lookback_days : i]
        realized = prior.std(ddof=1) * np.sqrt(365.0)
        if realized > 0:
            scale[i] = min(cfg.vol_target_annual / realized, cfg.max_leverage)
    return scale


def summarize(ret_bp: np.ndarray, cfg: BlendConfig) -> dict[str, float]:
    """Scoring recipe from the config: mean bp, Sharpe, compounded drawdown."""
    raw = np.asarray(ret_bp, dtype=float)
    if len(raw) < 2:
        return {"days": float(len(raw))}
    scaled = raw * volatility_scale(raw, cfg)
    returns = scaled / 1e4
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(np.maximum(equity, 1e-12))
    std = returns.std(ddof=1)
    return {
        "days": float(len(raw)),
        "mean_net_bp_per_day": float(raw.mean()),
        "sharpe_raw": float(raw.mean() / raw.std(ddof=1) * np.sqrt(365.0)) if raw.std(ddof=1) > 0 else 0.0,
        "sharpe_vol_targeted": float(returns.mean() / std * np.sqrt(365.0)) if std > 0 else 0.0,
        "compounded_max_drawdown_pct": float(np.max(1.0 - equity / peak) * 100.0),
        "worst_day_pct": float(raw.min() / 100.0),
        "hit_rate_pct": float((raw > 0).mean() * 100.0),
    }


def score(
    panel: pl.DataFrame, cfg: BlendConfig, *, cost_bp: float | None = None
) -> dict[str, Any]:
    """Full pass: panel in, scoring row out. The charged cost basis is echoed in
    the result so downstream tables are unambiguous about it."""
    charged = cfg.cost_basis_bp if cost_bp is None else float(cost_bp)
    book = daily_book(prepare(panel, cfg), cfg, cost_bp=charged)
    result: dict[str, Any] = {"config_id": cfg.config_id, "cost_basis_bp": charged}
    result.update(summarize(book["ret_bp"].to_numpy(), cfg))
    return result
