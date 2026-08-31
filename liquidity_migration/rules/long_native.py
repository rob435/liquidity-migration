"""Registered LONG decision rule: the FC-v11a/v12 profiles, features, and signal.

Production surface for the LONG sleeve, the policy sizing profiles, and the
CLI. The historical equity engine that replays this rule lives in
``liquidity_migration/research/backtest/long_native.py``.
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core._common import calendar_roll, calendar_shift
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

    execution_strategy_id: str
    start_date: str
    end_date: str

    universe_size: int
    universe_volume_window_days: int
    min_listing_history_days: int
    exclude_symbols: tuple[str, ...]
    regime_symbol: str
    regime_sma_days: int

    fc_min_day_return: float
    fc_top_volume_rank_max: int
    fc_min_close_location: float
    fc_max_hold_days: int
    fc_max_atr_pct: float
    fc_atr_stop_mult: float
    fc_sigma_mult: float
    fc_sniper_retrace_pct: float
    fc_sniper_deadline_hours: int
    weekend_size_mult: float
    fc_close_loc_multi_day: float

    # Tighten the stop to N x ATR once a position is this many hours old. Zero
    # disables, so v11a is unchanged. Pairs with a wide `fc_atr_stop_mult`:
    # ATR-14d is a two-week average and this signal only fires when a name moved
    # 2.5 sigma TODAY, so a narrow stop sits inside the noise of the very move
    # that triggered the entry. Give the trade room through that move, then stop
    # giving it room once it has had two days and gone nowhere.
    fc_stop_time_decay_hours: int
    fc_stop_time_decay_atr_mult: float

    max_concurrent_positions: int
    cooldown_days: int
    entry_delay_hours: int
    gross_exposure: float
    vol_estimate_window_days: int
    vol_floor_annual: float
    max_position_weight: float
    vol_target_annual: float
    vol_target_min_scale: float
    vol_target_max_scale: float
    cost_multiplier: float


LONG_STRATEGY_PROFILE_CHOICES = ("v11a", "v12")
_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
LONG_REGISTERED_RULE_PATHS = {
    name: _CONFIGS_DIR / f"long_native_{name}.json" for name in LONG_STRATEGY_PROFILE_CHOICES
}


def _registered_long_profiles() -> dict[str, LongNativeConfig]:
    expected_fields = set(LongNativeConfig.__dataclass_fields__)
    integer_fields = {
        "universe_size",
        "universe_volume_window_days",
        "min_listing_history_days",
        "regime_sma_days",
        "fc_top_volume_rank_max",
        "fc_max_hold_days",
        "fc_sniper_deadline_hours",
        "fc_stop_time_decay_hours",
        "max_concurrent_positions",
        "cooldown_days",
        "entry_delay_hours",
        "vol_estimate_window_days",
    }
    text_fields = {"execution_strategy_id", "start_date", "end_date", "regime_symbol"}
    loaded: dict[str, LongNativeConfig] = {}
    for name in LONG_STRATEGY_PROFILE_CHOICES:
        payload = json.loads(LONG_REGISTERED_RULE_PATHS[name].read_bytes())
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "profile_name", "rule"}:
            raise ValueError("LONG registered rule has unexpected or missing fields")
        if (
            payload["schema_version"] != 1
            or payload["kind"] != "liquidity_migration_long_native_rule"
            or payload["profile_name"] != name
        ):
            raise ValueError("unsupported LONG registered rule identity")
        row = payload["rule"]
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise ValueError(f"LONG registered profile {name!r} has unexpected or missing fields")
        if any(type(row[field]) is not int for field in integer_fields):
            raise ValueError(f"LONG registered profile {name!r} has a non-integer field")
        if any(not isinstance(row[field], str) for field in text_fields):
            raise ValueError(f"LONG registered profile {name!r} has a non-text field")
        excluded = row["exclude_symbols"]
        if (
            not isinstance(excluded, list)
            or any(not isinstance(symbol, str) or not symbol for symbol in excluded)
            or excluded != sorted(set(excluded))
        ):
            raise ValueError(f"LONG registered profile {name!r} has invalid excluded symbols")
        values = dict(row)
        values["exclude_symbols"] = tuple(excluded)
        for field in expected_fields - integer_fields - text_fields - {"exclude_symbols"}:
            value = values[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"LONG registered profile {name!r} has an invalid number")
            values[field] = float(value)
        loaded[name] = LongNativeConfig(**values)
    if loaded["v11a"].execution_strategy_id != LONG_V11A_DIV_WEEKEND_VOL_STRATEGY_ID:
        raise ValueError("LONG v11a registered identity disagrees with attribution")
    if loaded["v12"].execution_strategy_id != LONG_V12_WIDE_STOP_STRATEGY_ID:
        raise ValueError("LONG v12 registered identity disagrees with attribution")
    return loaded


def long_v11a_profile() -> LongNativeConfig:
    """Return the deployed LONG strategy profile."""

    return _registered_long_profiles()["v11a"]


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

    return _registered_long_profiles()["v12"]


# Each selector maps to exactly one registered profile. Unknown values fail
# instead of silently changing the persisted strategy identity.
def resolve_long_strategy_profile(name: str) -> LongNativeConfig:
    """Resolve a runtime profile selector to its registered LONG config.

    Deliberately strict: an unknown selector raises instead of defaulting, so a
    typo in native config cannot silently run the wrong execution identity.
    """

    normalized = str(name).strip().lower()
    if normalized == "v11a":
        return long_v11a_profile()
    if normalized == "v12":
        return long_v12_profile()
    raise ValueError(
        f"unknown LONG strategy profile {name!r}; supported: {', '.join(LONG_STRATEGY_PROFILE_CHOICES)}"
    )


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
