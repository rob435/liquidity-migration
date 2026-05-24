from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl


EXIT_RANK_DECAY = "rank_decay"
EXIT_TREND_BREAK = "trend_break"
EXIT_VOL_SHOCK = "vol_shock"
EXIT_REGIME_BREAK = "regime_break"
EXIT_UNIVERSE_DEMOTION = "universe_demotion"
EXIT_TRAILING_ATR = "trailing_atr"
EXIT_DATA_END = "data_end"

EXIT_REASONS = (
    EXIT_RANK_DECAY,
    EXIT_TREND_BREAK,
    EXIT_VOL_SHOCK,
    EXIT_REGIME_BREAK,
    EXIT_UNIVERSE_DEMOTION,
    EXIT_TRAILING_ATR,
    EXIT_DATA_END,
)


@dataclass(frozen=True, slots=True)
class MomentumEventsConfig:
    rank_entry_min_norm: float = 0.75  # top quartile (rank_norm: 0 worst, 1 best)
    rank_exit_max_norm: float = 0.50  # below median triggers exit
    breakout_window_days: int = 60
    sma_trend_break_days: int = 100
    atr_window_days: int = 30
    vol_shock_multiple: float = 3.0
    trailing_atr_multiple: float = 4.0
    require_regime_on_entry: bool = True
    require_funding_not_overheated: bool = True
    require_coil_release: bool = True
    require_breakout: bool = True


def detect_entry_events(features: pl.DataFrame, *, config: MomentumEventsConfig | None = None) -> pl.DataFrame:
    """All entry conditions firing on the same daily bar.

    Returned rows are the (date, symbol) candidates whose entries should be
    placed 1h after the daily close. Callers still apply capacity, active, and
    cooldown gates downstream.
    """
    cfg = config or MomentumEventsConfig()
    if features.is_empty():
        return features
    breakout_col = f"prior_high_{cfg.breakout_window_days}d"
    required = {"rank_norm", "close", "in_liquidity_tier"}
    if cfg.require_coil_release:
        required.add("coil_release_event")
    if cfg.require_breakout:
        required.add(breakout_col)
    missing = required - set(features.columns)
    if missing:
        raise RuntimeError(f"features missing required columns for entry detection: {sorted(missing)}")
    predicate = (
        (pl.col("rank_norm") >= cfg.rank_entry_min_norm)
        & pl.col("in_liquidity_tier")
    )
    if cfg.require_coil_release:
        predicate = predicate & pl.col("coil_release_event")
    if cfg.require_breakout:
        predicate = predicate & (pl.col("close") > pl.col(breakout_col))
    if cfg.require_regime_on_entry and "regime_on" in features.columns:
        predicate = predicate & pl.col("regime_on")
    if cfg.require_funding_not_overheated and "funding_overheat" in features.columns:
        predicate = predicate & (~pl.col("funding_overheat"))
    return features.filter(predicate).sort(["ts_ms", "symbol"])


def exit_reason_for_position(
    today: dict[str, Any],
    *,
    high_water_close: float,
    config: MomentumEventsConfig | None = None,
) -> str | None:
    """First exit reason that fires given today's per-symbol feature row.

    Order is intentional: regime break first (kills the whole sleeve), then
    universe demotion (mechanical exit), then signal-quality exits (trend
    break, rank decay), then volatility / trailing-stop exits. None means hold.
    """
    cfg = config or MomentumEventsConfig()
    close = _safe_float(today.get("close"))
    if close is None:
        return EXIT_DATA_END

    regime_on = today.get("regime_on")
    if regime_on is False:
        return EXIT_REGIME_BREAK

    in_tier = today.get("in_liquidity_tier")
    if in_tier is False:
        return EXIT_UNIVERSE_DEMOTION

    sma_trend = _safe_float(today.get(f"sma_{cfg.sma_trend_break_days}d"))
    if sma_trend is not None and close < sma_trend:
        return EXIT_TREND_BREAK

    rank_norm = _safe_float(today.get("rank_norm"))
    if rank_norm is not None and rank_norm < cfg.rank_exit_max_norm:
        return EXIT_RANK_DECAY

    abs_ret_median = _safe_float(today.get(f"abs_return_median_{cfg.atr_window_days}d"))
    log_return = _safe_float(today.get("log_return"))
    if (
        abs_ret_median is not None
        and abs_ret_median > 0.0
        and log_return is not None
        and abs(log_return) > cfg.vol_shock_multiple * abs_ret_median
    ):
        return EXIT_VOL_SHOCK

    atr = _safe_float(today.get(f"atr_{cfg.atr_window_days}d"))
    if atr is not None and atr > 0.0:
        threshold = high_water_close - cfg.trailing_atr_multiple * atr
        if close < threshold:
            return EXIT_TRAILING_ATR

    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
