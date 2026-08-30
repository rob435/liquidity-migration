"""Registered LONG decision rule: the FC-v11a/v12 profiles, features, and signal.

Production surface for the LONG sleeve, the policy sizing profiles, and the
CLI. The historical equity engine that replays this rule lives in
``liquidity_migration/research/backtest/long_native.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import polars as pl

from liquidity_migration.core._common import calendar_roll, calendar_shift
from liquidity_migration.core.config import DEFAULT_EXCLUDED_SYMBOLS
from liquidity_migration.rules.long_identity import (
    LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID,
    LONG_V12_WIDE_STOP_STRATEGY_ID,
)
from liquidity_migration.rules.momentum_signals import add_returns_and_age, daily_bars


@dataclass(frozen=True, slots=True)
class LongNativeConfig:
    """The sole supported LONG strategy: FC-v11a div/weekend/vol.

    This is deliberately not a research parameter surface. Fields remain only
    when the active demo runtime or standard equity runner consumes them.
    """

    execution_strategy_id: str = LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID
    start_date: str = ""
    end_date: str = ""

    universe_size: int = 50
    universe_volume_window_days: int = 90
    min_listing_history_days: int = 30
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    regime_symbol: str = "BTCUSDT"
    regime_sma_days: int = 30

    fc_min_day_return: float = 0.15
    fc_top_volume_rank_max: int = 10
    fc_min_close_location: float = 0.70
    fc_max_hold_days: int = 3
    fc_max_atr_pct: float = 0.12
    fc_atr_stop_mult: float = 1.5
    fc_sigma_mult: float = 2.5
    fc_sniper_retrace_pct: float = 0.01
    fc_sniper_deadline_hours: int = 6
    weekend_size_mult: float = 1.5
    fc_close_loc_multi_day: float = 0.6

    # Tighten the stop to N x ATR once a position is this many hours old. Zero
    # disables, so v11a is unchanged. Pairs with a wide `fc_atr_stop_mult`:
    # ATR-14d is a two-week average and this signal only fires when a name moved
    # 2.5 sigma TODAY, so a narrow stop sits inside the noise of the very move
    # that triggered the entry. Give the trade room through that move, then stop
    # giving it room once it has had two days and gone nowhere.
    fc_stop_time_decay_hours: int = 0
    fc_stop_time_decay_atr_mult: float = 0.0

    max_concurrent_positions: int = 10
    cooldown_days: int = 7
    entry_delay_hours: int = 1
    gross_exposure: float = 1.0
    vol_estimate_window_days: int = 30
    vol_floor_annual: float = 0.30
    max_position_weight: float = 0.30
    vol_target_annual: float = 0.60
    vol_target_min_scale: float = 0.30
    vol_target_max_scale: float = 1.25
    cost_multiplier: float = 3.0


def long_v11a_profile() -> LongNativeConfig:
    """Return the deployed LONG strategy profile."""

    return LongNativeConfig()


def long_v12_profile() -> LongNativeConfig:
    """v11a with the stop widened early and tightened late.

    Every other v11a rule was ablated on the real engine and kept: the volume
    rank, the BTC-and-ETH regime gate, the 2.5 sigma trigger family, the 7-day
    cooldown, the 3-day hold, the 1%/6h retrace entry and the
    top-50 universe all lose Sharpe when loosened. The stop was the one number
    that was wrong.

    A 1.5xATR stop is measured off a two-week average on a name that moved 2.5
    sigma today, so it sits inside the noise of the move that triggered the
    entry: 67 of 294 trades stopped out. Widening it to 3xATR for the first two
    days and then tightening to 1.5xATR keeps the room where it is needed and
    takes it back from trades that have gone nowhere.

    Measured against v11a over 2021-04..2026-07 (paired daily difference
    +0.48 bp/day, t 3.27): total 38.5% -> 51.6%, daily Sharpe 1.24 -> 1.49,
    worst dip -4.4% -> -3.9%, better or equal in all six calendar years, and
    LESS concentrated (best 20 trades carry 62% of P&L against 78%).

    Lane-1 evidence: simulated on data that also shaped the choice. The forward
    record starts at the commit that registers this profile.
    """

    return LongNativeConfig(
        execution_strategy_id=LONG_V12_WIDE_STOP_STRATEGY_ID,
        fc_atr_stop_mult=3.0,
        fc_stop_time_decay_hours=48,
        fc_stop_time_decay_atr_mult=1.5,
    )


# Runtime selector names accepted by the CLI/daemon. Each maps to exactly one
# registered profile; anything else fails at argument parsing or resolution.
LONG_STRATEGY_PROFILE_CHOICES = ("v11a", "v12")


def resolve_long_strategy_profile(name: str) -> LongNativeConfig:
    """Resolve a runtime profile selector to its registered LONG config.

    Deliberately strict: an unknown selector raises instead of defaulting, so a
    typo in deploy wiring cannot silently publish targets under the wrong
    persisted execution identity.
    """

    normalized = str(name).strip().lower()
    if normalized == "v11a":
        return long_v11a_profile()
    if normalized == "v12":
        return long_v12_profile()
    raise ValueError(
        f"unknown LONG strategy profile {name!r}; supported: {', '.join(LONG_STRATEGY_PROFILE_CHOICES)}"
    )


def _vol_target_scale(config: "LongNativeConfig", btc_rv: float | None) -> float:
    """Active v11a BTC-vol book scalar, shared by equity and runtime."""

    rv = btc_rv or config.vol_target_annual  # None/0.0 -> target (scale 1.0); mirrors backtest
    vt = config.vol_target_annual / max(rv, 1e-6)
    return max(config.vol_target_min_scale, min(config.vol_target_max_scale, vt))


def build_long_features(klines_1h: pl.DataFrame, *, config: LongNativeConfig) -> pl.DataFrame:
    """Build only the features consumed by the registered FC-v11a profile."""

    daily = daily_bars(klines_1h)
    return build_long_features_from_daily(daily, config=config)


def build_long_features_from_daily(
    daily: pl.DataFrame,
    *,
    config: LongNativeConfig,
) -> pl.DataFrame:
    """Build the canonical LONG feature set from PIT-filtered daily bars."""

    if daily.is_empty():
        return daily
    daily = add_returns_and_age(daily).sort(["symbol", "ts_ms"])
    annualization = math.sqrt(365.0)
    daily = daily.with_columns(
        [
            pl.when((pl.col("high") - pl.col("low")) > 1e-12)
            .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .otherwise(0.5)
            .alias("close_location"),
            (
                _cal_roll(
                    pl.col("log_return"),
                    "std",
                    config.vol_estimate_window_days,
                    min_samples=config.vol_estimate_window_days,
                ).over("symbol")
                * annualization
            ).alias("realized_vol"),
            _cal_roll(
                pl.col("turnover_quote"),
                "median",
                config.universe_volume_window_days,
                min_samples=config.universe_volume_window_days,
            )
            .over("symbol")
            .alias("turnover_median_90d"),
            (pl.col("close") / calendar_shift(pl.col("close"), 3)).log().alias("pump_3d_log"),
            (pl.col("close") / calendar_shift(pl.col("close"), 7)).log().alias("pump_7d_log"),
            _cal_roll(pl.col("high"), "max", 3, min_samples=3).over("symbol").alias("high_3d"),
            _cal_roll(pl.col("low"), "min", 3, min_samples=3).over("symbol").alias("low_3d"),
            _cal_roll(pl.col("high"), "max", 7, min_samples=7).over("symbol").alias("high_7d"),
            _cal_roll(pl.col("low"), "min", 7, min_samples=7).over("symbol").alias("low_7d"),
            pl.max_horizontal(
                [
                    pl.col("high") - pl.col("low"),
                    (pl.col("high") - calendar_shift(pl.col("close"), 1)).abs(),
                    (pl.col("low") - calendar_shift(pl.col("close"), 1)).abs(),
                ]
            ).alias("true_range"),
        ]
    )
    daily = daily.with_columns(
        [
            (pl.col("realized_vol") / math.sqrt(365.0)).alias("sigma_daily_30d"),
            pl.when((pl.col("high_3d") - pl.col("low_3d")) > 1e-12)
            .then((pl.col("close") - pl.col("low_3d")) / (pl.col("high_3d") - pl.col("low_3d")))
            .otherwise(0.5)
            .alias("close_loc_3d"),
            pl.when((pl.col("high_7d") - pl.col("low_7d")) > 1e-12)
            .then((pl.col("close") - pl.col("low_7d")) / (pl.col("high_7d") - pl.col("low_7d")))
            .otherwise(0.5)
            .alias("close_loc_7d"),
            _cal_roll(pl.col("true_range"), "mean", 14, min_samples=7).over("symbol").alias("atr_14d"),
        ]
    ).with_columns((pl.col("atr_14d") / pl.col("close")).alias("atr_14d_pct"))

    daily = daily.with_columns(
        pl.col("turnover_quote").rank(method="ordinal", descending=True).over("ts_ms").alias("today_volume_rank")
    )
    daily = daily.with_columns(
        pl.col("turnover_median_90d").rank(method="ordinal", descending=True).over("ts_ms").alias("universe_rank")
    ).with_columns(
        (
            (pl.col("universe_rank") <= config.universe_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col("turnover_median_90d").is_finite()
        ).alias("in_universe")
    )

    btc = daily.filter(pl.col("symbol") == config.regime_symbol).sort("ts_ms")
    if not btc.is_empty():
        btc = (
            btc.with_columns(
                [
                    _cal_roll(
                        pl.col("close"),
                        "mean",
                        config.regime_sma_days,
                        min_samples=config.regime_sma_days,
                    ).alias("regime_sma"),
                    (_cal_roll(pl.col("log_return"), "std", 30, min_samples=20) * math.sqrt(365.0)).alias("btc_rv_30"),
                ]
            )
            .with_columns((pl.col("close") > pl.col("regime_sma")).alias("regime_on"))
            .select(["ts_ms", "regime_on", "btc_rv_30"])
        )
        daily = daily.join(btc, on="ts_ms", how="left").with_columns(
            [
                pl.col("regime_on").fill_null(False),
                pl.col("btc_rv_30").fill_null(0.8),
            ]
        )
    else:
        daily = daily.with_columns(
            [
                pl.lit(False).alias("regime_on"),
                pl.lit(0.8, dtype=pl.Float64).alias("btc_rv_30"),
            ]
        )

    eth = daily.filter(pl.col("symbol") == "ETHUSDT").sort("ts_ms")
    if not eth.is_empty():
        eth = (
            eth.with_columns(
                _cal_roll(
                    pl.col("close"),
                    "mean",
                    config.regime_sma_days,
                    min_samples=config.regime_sma_days,
                ).alias("eth_sma")
            )
            .with_columns((pl.col("close") > pl.col("eth_sma")).alias("eth_regime_on"))
            .select(["ts_ms", "eth_regime_on"])
        )
        daily = daily.join(eth, on="ts_ms", how="left").with_columns(pl.col("eth_regime_on").fill_null(False))
    else:
        daily = daily.with_columns(pl.lit(False).alias("eth_regime_on"))

    return daily.sort(["ts_ms", "symbol"])


def detect_pattern_fomo_chase(row: dict[str, Any], cfg: LongNativeConfig) -> bool:
    """Return whether a closed daily row satisfies the active FC-v11a signal."""

    if not row.get("in_universe") or not row.get("regime_on") or not row.get("eth_regime_on"):
        return False
    today_rank = _safe_float(row.get("today_volume_rank"))
    if today_rank is None or today_rank > cfg.fc_top_volume_rank_max:
        return False
    if _safe_float(row.get("log_return")) is None:
        return False
    pump = long_pump_family(row, cfg)
    close_location = _safe_float(row.get("close_location"))
    close_loc_3d = _safe_float(row.get("close_loc_3d"))
    close_loc_7d = _safe_float(row.get("close_loc_7d"))
    trigger_1d = bool(pump["trigger_1d"]) and (
        close_location is not None and close_location >= cfg.fc_min_close_location
    )
    trigger_3d = bool(pump["trigger_3d"]) and (
        close_loc_3d is not None and close_loc_3d >= cfg.fc_close_loc_multi_day
    )
    trigger_7d = bool(pump["trigger_7d"]) and (
        close_loc_7d is not None and close_loc_7d >= cfg.fc_close_loc_multi_day
    )
    if not (trigger_1d or trigger_3d or trigger_7d):
        return False
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    return atr_pct is not None and 0.0 < atr_pct <= cfg.fc_max_atr_pct


def long_pump_family(row: dict[str, Any], cfg: LongNativeConfig) -> dict[str, Any]:
    """Return the causal FC pump family before active alpha filters.

    The diagnostic source population uses these exact magnitude thresholds but
    records regime, universe/rank, close-location, and ATR constraints later.
    ``detect_pattern_fomo_chase`` consumes the same flags, preventing a second
    implementation of the active pump arithmetic.
    """

    sigma_d = _safe_float(row.get("sigma_daily_30d"))
    threshold_1d = (
        cfg.fc_sigma_mult * sigma_d
        if sigma_d is not None and sigma_d > 0.0
        else math.log1p(cfg.fc_min_day_return)
    )
    thresholds = {
        "1d": threshold_1d,
        "3d": threshold_1d * math.sqrt(3),
        "7d": threshold_1d * math.sqrt(7),
    }
    values = {
        "1d": _safe_float(row.get("log_return")),
        "3d": _safe_float(row.get("pump_3d_log")),
        "7d": _safe_float(row.get("pump_7d_log")),
    }
    triggers = {
        horizon: value is not None and value >= thresholds[horizon]
        for horizon, value in values.items()
    }
    ratios = [
        value / thresholds[horizon]
        for horizon, value in values.items()
        if value is not None and thresholds[horizon] > 0.0
    ]
    return {
        "threshold_1d": thresholds["1d"],
        "threshold_3d": thresholds["3d"],
        "threshold_7d": thresholds["7d"],
        "trigger_1d": triggers["1d"],
        "trigger_3d": triggers["3d"],
        "trigger_7d": triggers["7d"],
        "trigger_any": any(triggers.values()),
        "source_strength": max(ratios) if ratios else None,
    }


def _fc_stop_fraction(row: dict[str, Any], cfg: LongNativeConfig) -> float:
    atr_pct = _safe_float(row.get("atr_14d_pct"))
    if atr_pct is None or atr_pct <= 0.0:
        raise ValueError("active FC-v11a entry requires positive atr_14d_pct")
    return atr_pct * cfg.fc_atr_stop_mult


def _classify_entry(
    row: dict[str, Any], cfg: LongNativeConfig
) -> tuple[str | None, float, int]:
    if not detect_pattern_fomo_chase(row, cfg):
        return None, 0.0, 0
    return "fomo_chase", _fc_stop_fraction(row, cfg), cfg.fc_max_hold_days


def _cal_roll(
    expr: pl.Expr,
    agg: str,
    n_days: int,
    *,
    shifted: bool = False,
    min_samples: int | None = None,
    **kwargs: Any,
) -> pl.Expr:
    return calendar_roll(
        expr,
        agg,
        n_days,
        shifted=shifted,
        min_samples=n_days if min_samples is None else min_samples,
        **kwargs,
    )


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f
