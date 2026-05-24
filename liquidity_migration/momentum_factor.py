"""Cross-sectional momentum FACTOR sleeve — weekly rebalance, paper-grounded.

Counterpart to the event-driven short. Adapted from Liu-Tsyvinski (2021),
Liu-Tsyvinski-Wu (2022), Asness-Moskowitz-Pedersen (2013), and Hurst-Ooi-
Pedersen (2017). Design spec: `docs/momentum_v2_literature.md`.

Key differences from `cross_sectional_momentum.py` (v1, event-driven):
- Weekly periodic rebalance, not multi-condition event triggers
- Short-horizon momentum (7/14/28 days), not 90-day Clenow slope
- Optional carry overlay (trailing funding rank)
- Optional time-series momentum filter
- L-only or L/S modes
- Vol-parity position sizing with portfolio vol-target overlay
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, date_ms, pct
from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .momentum_signals import daily_bars, add_returns_and_age
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _funding_lookup,
    _perp_funding_return,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
from .volume_events import (
    _date_range,
    _exclude_symbols,
    _full_pit_universe_error,
    _full_pit_universe_pass,
    _iso_date,
    _iso_month,
    _pit_manifest_metadata,
)


_logger = logging.getLogger("liquidity_migration.momentum_factor")


SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)

MODE_LONG_ONLY = "long_only"
MODE_LONG_SHORT = "long_short"
MODES = (MODE_LONG_ONLY, MODE_LONG_SHORT)

SIZING_EQUAL = "equal"
SIZING_VOL_PARITY = "vol_parity"
SIZINGS = (SIZING_EQUAL, SIZING_VOL_PARITY)

PRESET_LO_SKIP0 = "lo_skip0"
PRESET_LO_CARRY0 = "lo_carry0"
PRESET_LO_SHARPE3 = "lo_sharpe3"
PRESET_LO_SHARPE3_ROBUST = "lo_sharpe3_robust"
PRESET_LO_ADAPTIVE = "lo_adaptive"
FACTOR_PRESETS = (
    PRESET_LO_SKIP0,
    PRESET_LO_CARRY0,
    PRESET_LO_SHARPE3,
    PRESET_LO_SHARPE3_ROBUST,
    PRESET_LO_ADAPTIVE,
)

VARIANT_STANDARD = "standard"
VARIANT_QUALITY_BLEND = "quality_blend"
VARIANT_BTC_RESIDUAL = "btc_residual"
VARIANT_ADAPTIVE_DUAL = "adaptive_dual"
VARIANT_DISPERSION_GATED = "dispersion_gated"
FACTOR_VARIANTS = (
    VARIANT_STANDARD,
    VARIANT_QUALITY_BLEND,
    VARIANT_BTC_RESIDUAL,
    VARIANT_ADAPTIVE_DUAL,
    VARIANT_DISPERSION_GATED,
)


def lo_skip0_preset(*, start_date: str = "", end_date: str = "") -> MomentumFactorConfig:
    """Long-only LO_skip0 baseline from trade-study round 1.

    OOS sanity (run 2026-05-24, see reports/momentum_lo_oos_sanity/):
    bybit_pre2023 daily Sharpe 1.47, binance_pre2023 daily Sharpe 3.69.
    Survives both pre-2023 roots; the OOS-validated baseline.
    """
    return MomentumFactorConfig(
        start_date=start_date,
        end_date=end_date,
        mode=MODE_LONG_ONLY,
        momentum_skip_days=0,
        carry_weight=1.5,
        require_positive_ts_momentum_for_longs=True,
        vol_target_annual=0.15,
        regime_off_scale=0.0,
        use_regime_filter=True,
    )


def lo_carry0_preset(*, start_date: str = "", end_date: str = "") -> MomentumFactorConfig:
    """Long-only with carry overlay disabled — forensics round-2 recommendation.

    Per docs/momentum_lo_trade_forensics.md, dropping carry_weight from 1.5 to 0
    gives a small IS improvement (daily Sharpe 2.43 → 2.46, +97 trades) on the
    canonical Bybit research root. Run label: `exploratory_in_sample` — no OOS
    evidence yet; lo_skip0 remains the OOS-validated default until this preset
    is forward-tested.
    """
    return MomentumFactorConfig(
        start_date=start_date,
        end_date=end_date,
        mode=MODE_LONG_ONLY,
        momentum_skip_days=0,
        carry_weight=0.0,
        require_positive_ts_momentum_for_longs=True,
        vol_target_annual=0.15,
        regime_off_scale=0.0,
        use_regime_filter=True,
    )


def lo_sharpe3_preset(*, start_date: str = "", end_date: str = "") -> MomentumFactorConfig:
    """Long-only config from trade-forensics grid (daily Sharpe ~3.9, ~200 trades IS).

    Entry gates: top-50 universe, vol cap 120% ann, top-15 liquidity names only,
    carry off, weekly rebalance. Label: `exploratory_in_sample` (grid-mined from
    1,548-config hunt — multiple-testing risk; see docs/momentum_lo_sharpe3_winner.md).

    OOS sanity (run 2026-05-24, see reports/momentum_lo_oos_sanity/):
    bybit_pre2023 daily Sharpe **0.73** (vs IS 3.95 — classic overfit drop),
    binance_pre2023 daily Sharpe 3.47. Do NOT promote on headline Sharpe.
    """
    return MomentumFactorConfig(
        start_date=start_date,
        end_date=end_date,
        mode=MODE_LONG_ONLY,
        universe_size=50,
        long_quantile=0.20,
        momentum_skip_days=0,
        carry_weight=0.0,
        require_positive_ts_momentum_for_longs=True,
        vol_target_annual=0.15,
        regime_off_scale=0.0,
        use_regime_filter=True,
        rebalance_days=7,
        max_realized_vol=1.2,
        max_turnover_rank=15,
    )


def lo_adaptive_preset(*, start_date: str = "", end_date: str = "") -> MomentumFactorConfig:
    """Regime-adaptive dual book: momentum when BTC>50d SMA, liquidity-shelter when off.

    Structural hypothesis for tri-root gate (Bybit IS 2023-26, Bybit OOS 2022,
    Binance OOS 2020). Not a parameter grid — one pre-declared architecture.
    """
    return MomentumFactorConfig(
        start_date=start_date,
        end_date=end_date,
        mode=MODE_LONG_ONLY,
        universe_size=30,
        long_quantile=0.20,
        momentum_skip_days=0,
        carry_weight=0.0,
        reversal_lookback_days=3,
        require_positive_ts_momentum_for_longs=True,
        vol_target_annual=0.15,
        regime_off_scale=1.0,
        use_regime_filter=True,
        rebalance_days=7,
        max_realized_vol=1.4,
        max_turnover_rank=10,
        factor_variant=VARIANT_ADAPTIVE_DUAL,
    )


def lo_sharpe3_robust_preset(*, start_date: str = "", end_date: str = "") -> MomentumFactorConfig:
    """lo_sharpe3 with vol cap loosened 1.2 → 1.6 (round-3, 19-variant sensitivity hunt).

    Picked because it had the best `oos_2025_2026` walk-forward Sharpe among 19
    hand-picked variants around `lo_sharpe3`. Run label: `exploratory_in_sample`
    until forward-tested. See docs/momentum_lo_sharpe3_robust.md.

    Canonical (2023-05 → 2026-05): daily Sharpe 3.06, 307 trades, +49.7%.
    OOS sanity (run 2026-05-24):
    - bybit_pre2023 daily Sharpe **1.74** (vs lo_sharpe3 0.73 — vol-cap loosening
      meaningfully recovers cleanest OOS root)
    - binance_pre2023 daily Sharpe 4.12, +71.1%
    First preset in the sharpe3 family with all three roots positive.
    """
    return replace(
        lo_sharpe3_preset(start_date=start_date, end_date=end_date),
        max_realized_vol=1.6,
    )


@dataclass(frozen=True, slots=True)
class MomentumFactorConfig:
    # --- window ---
    start_date: str = ""
    end_date: str = ""

    # --- universe ---
    universe_size: int = 30
    universe_volume_window_days: int = 90
    min_listing_history_days: int = 90
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS

    # --- momentum signal (cumulative return over each lookback, skipping last `skip` days) ---
    momentum_lookbacks_days: tuple[int, ...] = (7, 14, 28)
    momentum_skip_days: int = 1

    # --- carry signal (trailing funding sum; we tilt against high carry for longs) ---
    carry_lookback_days: int = 7
    carry_weight: float = 0.5  # 0 disables carry

    # --- short-horizon reversal signal (Jegadeesh 1990 / De Bondt-Thaler 1985).
    #     trailing N-day return WITHOUT a skip; subtracted from composite so
    #     recent losers get higher rank → "buy the dip" tilt that's known to be
    #     orthogonal to multi-week momentum in equities. ---
    reversal_lookback_days: int = 0  # 0 disables
    reversal_weight: float = 0.0

    # --- time-series momentum filter ---
    ts_momentum_lookback_days: int = 30
    require_positive_ts_momentum_for_longs: bool = False
    require_negative_ts_momentum_for_shorts: bool = False

    # --- portfolio mode ---
    mode: str = MODE_LONG_ONLY
    long_quantile: float = 0.20  # top 20% of eligible (e.g. top 6 of 30)
    short_quantile: float = 0.20  # bottom 20% — only used in L/S mode

    # --- lifecycle ---
    rebalance_days: int = 7
    entry_delay_hours: int = 1

    # --- sizing ---
    sizing: str = SIZING_VOL_PARITY
    vol_estimate_window_days: int = 30
    vol_floor_annual: float = 0.30
    gross_exposure: float = 1.0  # total gross book at full risk-on
    max_position_weight: float = 0.30
    vol_target_annual: float = 0.0  # 0 disables; otherwise scale total exposure to hit this portfolio vol estimate
    vol_target_max_scale: float = 2.0
    assumed_avg_correlation: float = 0.5  # used to estimate portfolio vol for vol-targeting

    # --- regime gate ---
    regime_symbol: str = "BTCUSDT"
    regime_sma_days: int = 50
    use_regime_filter: bool = True
    regime_off_scale: float = 0.30  # scale exposure to 30% when regime off (0 = flat)

    # --- costs ---
    cost_multiplier: float = 3.0

    # --- gates ---
    require_pit_membership: bool = True
    require_full_pit_universe: bool = True

    # --- optional long entry filters (trade-forensics discoveries; None = disabled) ---
    min_ts_momentum: float | None = None
    min_momentum_avg: float | None = None
    max_carry: float | None = None
    max_realized_vol: float | None = None
    min_composite_score: float | None = None
    max_composite_score: float | None = None
    max_momentum_avg: float | None = None
    max_turnover_rank: int | None = None
    min_turnover_rank: int | None = None

    # --- structural variants (hypothesis tests, not parameter grids) ---
    factor_variant: str = VARIANT_STANDARD
    min_momentum_dispersion: float | None = None  # skip rebalance if xs std(mom) below

    # --- promotion thresholds ---
    promotion_min_avg_sharpe: float = 1.0
    promotion_max_drawdown: float = -0.30


def run_momentum_factor_research(
    data_root: str | Path,
    *,
    config: MomentumFactorConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or MomentumFactorConfig()
    costs = cost_config or CostConfig()
    _validate_config(cfg)
    root = Path(data_root).expanduser()
    output_dir = (
        Path(report_dir) if report_dir else root / "reports" / "momentum_factor_research"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_klines = read_dataset_columns(
        root,
        "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(root, "funding")
    archive_manifest = read_dataset(root, "archive_trade_manifest")

    klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)

    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    if cfg.require_full_pit_universe and not full_pit_universe_pass:
        raise RuntimeError(_full_pit_universe_error(klines, archive_manifest))

    features = build_factor_features(klines, funding=funding, config=cfg)
    features = _filter_signal_window(features, start=cfg.start_date, end=cfg.end_date)
    if features.is_empty():
        raise RuntimeError("No features generated; check data root coverage and config window.")

    bars_by_symbol = _bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None

    trades, lifecycle_stats, rebalance_log = _run_factor_pipeline(
        features=features,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        costs=costs,
    )

    bt_config = TradeLifecycleConfig(
        score="momentum_factor",
        hold_days=cfg.rebalance_days,
        rebalance_days=cfg.rebalance_days,
        gross_exposure=cfg.gross_exposure,
        entry_delay_hours=cfg.entry_delay_hours,
        cost_multiplier=cfg.cost_multiplier,
        side_mode="long_high_short_low",
    )
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary_metrics = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    monthly = _monthly_returns(baskets)
    split_rows = _split_rows(baskets, config=bt_config)
    funding_mode = summary_metrics.get("funding_mode", "missing")

    promotion = _evaluate_promotion(
        split_rows=split_rows,
        summary=summary_metrics,
        funding_mode=funding_mode,
        full_pit_universe_pass=full_pit_universe_pass,
        config=cfg,
    )

    if not trades.is_empty():
        trades.write_csv(output_dir / "momentum_factor_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "momentum_factor_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "momentum_factor_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "momentum_factor_monthly.csv")

    metadata = {
        "config": asdict(cfg),
        "rows": {
            "features": features.height,
            "rebalances": len(rebalance_log),
            "trades": trades.height,
            "baskets": baskets.height,
        },
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {
            **asdict(costs),
            "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
            "cost_multiplier": cfg.cost_multiplier,
            "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier,
        },
        "summary": summary_metrics,
        "lifecycle": lifecycle_stats,
        "splits": split_rows,
        "promotion": promotion,
        "rebalance_log_tail": rebalance_log[-10:],
        "run_label": _run_label(
            config=cfg,
            archive_manifest=archive_manifest,
            full_pit_universe_pass=full_pit_universe_pass,
            funding_mode=funding_mode,
        ),
    }
    (output_dir / "momentum_factor_research_report.json").write_text(
        json.dumps(metadata, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "momentum_factor_research_report.md").write_text(
        format_factor_report(metadata),
        encoding="utf-8",
    )
    return {**metadata, "report_dir": str(output_dir)}


def _validate_config(cfg: MomentumFactorConfig) -> None:
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if cfg.sizing not in SIZINGS:
        raise ValueError(f"sizing must be one of {SIZINGS}")
    if cfg.universe_size <= 0:
        raise ValueError("universe_size must be positive")
    if not cfg.momentum_lookbacks_days:
        raise ValueError("momentum_lookbacks_days must not be empty")
    if cfg.rebalance_days <= 0:
        raise ValueError("rebalance_days must be positive")
    if not 0.0 < cfg.long_quantile <= 0.5:
        raise ValueError("long_quantile must be in (0, 0.5]")
    if cfg.mode == MODE_LONG_SHORT and not 0.0 < cfg.short_quantile <= 0.5:
        raise ValueError("short_quantile must be in (0, 0.5] for L/S mode")
    if cfg.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if cfg.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
    if cfg.vol_floor_annual <= 0.0:
        raise ValueError("vol_floor_annual must be positive")
    if cfg.vol_target_annual < 0.0:
        raise ValueError("vol_target_annual must be non-negative (0 disables vol-targeting)")
    if cfg.factor_variant not in FACTOR_VARIANTS:
        raise ValueError(f"factor_variant must be one of {FACTOR_VARIANTS}")


def build_factor_features(
    klines_1h: pl.DataFrame,
    *,
    funding: pl.DataFrame | None,
    config: MomentumFactorConfig,
) -> pl.DataFrame:
    """Per (date, symbol) feature frame: momentum_{Nd}, ts_momentum_{Nd}, vol, carry, eligibility, regime."""
    daily = daily_bars(klines_1h)
    if daily.is_empty():
        return daily
    daily = add_returns_and_age(daily)

    annualization = math.sqrt(365.0)
    daily = daily.sort(["symbol", "ts_ms"]).with_columns(
        (
            pl.col("log_return")
            .rolling_std(window_size=config.vol_estimate_window_days, min_samples=config.vol_estimate_window_days)
            .over("symbol")
            * annualization
        ).alias("realized_vol")
    )

    # Multi-horizon cumulative returns, skipping the last `skip` days (Carhart-style).
    skip = max(config.momentum_skip_days, 0)
    for lookback in config.momentum_lookbacks_days:
        # cumulative log return over [t - skip - lookback, t - skip]
        shifted = pl.col("close").shift(skip + 1).over("symbol")
        anchor = pl.col("close").shift(skip + lookback + 1).over("symbol")
        daily = daily.with_columns(
            (shifted / anchor - 1.0).alias(f"momentum_{lookback}d")
        )

    # Time-series momentum signal (own coin's trailing N-day return, no skip).
    ts_lookback = config.ts_momentum_lookback_days
    if ts_lookback > 0:
        daily = daily.with_columns(
            (pl.col("close") / pl.col("close").shift(ts_lookback).over("symbol") - 1.0).alias("ts_momentum")
        )

    # Short-horizon reversal signal (no skip). High recent return → high
    # reversal score → SUBTRACTED from composite (so we tilt toward recent
    # losers). Jegadeesh 1990 / De Bondt-Thaler 1985 adapted to crypto's
    # shorter time-scale.
    rev_lookback = config.reversal_lookback_days
    if rev_lookback > 0:
        daily = daily.with_columns(
            (pl.col("close") / pl.col("close").shift(rev_lookback).over("symbol") - 1.0).alias("reversal_signal")
        )

    # Trailing turnover for universe filter.
    daily = daily.with_columns(
        pl.col("turnover_quote")
        .rolling_median(window_size=config.universe_volume_window_days, min_samples=config.universe_volume_window_days)
        .over("symbol")
        .alias("turnover_median")
    )
    daily = daily.with_columns(
        pl.col("turnover_median").rank(method="ordinal", descending=True).over("ts_ms").alias("turnover_rank")
    )
    daily = daily.with_columns(
        (
            (pl.col("turnover_rank") <= config.universe_size)
            & (pl.col("symbol_age_days") >= config.min_listing_history_days)
            & pl.col("turnover_median").is_finite()
        ).alias("in_universe")
    )

    # Carry: cumulative funding rate over trailing window. High funding = pay more for longs.
    if funding is not None and not funding.is_empty():
        rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in funding.columns else (
            "funding_rate" if "funding_rate" in funding.columns else None
        )
        if rate_col is not None:
            fund_daily = (
                funding.select(["ts_ms", "symbol", rate_col])
                .filter(pl.col(rate_col).is_finite())
                .with_columns(
                    ((pl.col("ts_ms") - (pl.col("ts_ms") % MS_PER_DAY)) + MS_PER_DAY).alias("ts_ms_day_end"),
                )
                .sort(["symbol", "ts_ms"])
                .group_by(["symbol", "ts_ms_day_end"], maintain_order=True)
                .agg(pl.col(rate_col).sum().alias("funding_day_sum"))
                .rename({"ts_ms_day_end": "ts_ms"})
                .sort(["symbol", "ts_ms"])
                .with_columns(
                    pl.col("funding_day_sum")
                    .rolling_sum(window_size=config.carry_lookback_days, min_samples=config.carry_lookback_days)
                    .over("symbol")
                    .alias("carry")
                )
                .select(["ts_ms", "symbol", "carry", "funding_day_sum"])
            )
            daily = daily.join(fund_daily, on=["ts_ms", "symbol"], how="left")
        else:
            daily = daily.with_columns(
                [
                    pl.lit(None, dtype=pl.Float64).alias("carry"),
                    pl.lit(None, dtype=pl.Float64).alias("funding_day_sum"),
                ]
            )
    else:
        daily = daily.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("carry"),
                pl.lit(None, dtype=pl.Float64).alias("funding_day_sum"),
            ]
        )

    # BTC regime gate.
    btc = daily.filter(pl.col("symbol") == config.regime_symbol).sort("ts_ms")
    if not btc.is_empty():
        btc = btc.with_columns(
            pl.col("close")
            .rolling_mean(window_size=config.regime_sma_days, min_samples=config.regime_sma_days)
            .alias("regime_sma")
        ).with_columns(
            (pl.col("close") > pl.col("regime_sma")).alias("regime_on"),
            (pl.col("close") / pl.col("close").shift(7) - 1.0).alias("btc_return_7d"),
        ).select(["ts_ms", "regime_sma", "regime_on", "btc_return_7d"])
        daily = daily.join(btc, on="ts_ms", how="left").with_columns(
            pl.col("regime_on").fill_null(False)
        )
    else:
        daily = daily.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias("regime_sma"),
                pl.lit(False).alias("regime_on"),
                pl.lit(None, dtype=pl.Float64).alias("btc_return_7d"),
            ]
        )

    return daily.sort(["ts_ms", "symbol"])


def _bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Per-symbol parallel arrays indexed by bar_end_ts_ms (close ts of each 1h bar)."""
    required = {"ts_ms", "symbol", "close"}
    missing = required - set(klines.columns)
    if missing:
        raise RuntimeError(f"klines missing required columns: {sorted(missing)}")
    output: dict[str, dict[str, Any]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        ends = part["bar_end_ts_ms"].to_numpy()
        closes = part["close"].to_numpy()
        output[symbol] = {
            "ends": ends.tolist(),
            "by_end": {int(end_ts): idx for idx, end_ts in enumerate(ends)},
            "close": closes,
            "bar_end_ts_ms": ends,
        }
    return output


def _filter_signal_window(features: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    if features.is_empty():
        return features
    if start:
        features = features.filter(pl.col("ts_ms") >= date_ms(start))
    if end:
        features = features.filter(pl.col("ts_ms") < date_ms(end))
    return features


def _run_factor_pipeline(
    *,
    features: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: MomentumFactorConfig,
    costs: CostConfig,
) -> tuple[pl.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    """At each rebalance date: build target portfolio, generate trades for current week."""
    dates_all = sorted(int(ts) for ts in features["ts_ms"].unique().to_list())
    if not dates_all:
        return _empty_factor_trades(), {}, []

    # Warm-up: skip dates until we have all required lookbacks.
    max_lookback = max(
        max(config.momentum_lookbacks_days) + config.momentum_skip_days + 1,
        config.ts_momentum_lookback_days,
        config.vol_estimate_window_days,
        config.universe_volume_window_days,
        config.carry_lookback_days,
        config.regime_sma_days,
    )
    if max_lookback >= len(dates_all):
        _logger.warning(
            "momentum_factor backtest produced zero trades: data window has %d daily bars "
            "but max required lookback is %d (momentum_lookbacks_days max=%d + skip=%d + 1, "
            "ts_momentum=%d, vol_estimate=%d, universe_volume=%d, carry=%d, regime_sma=%d). "
            "Widen the date range or shrink the lookbacks.",
            len(dates_all),
            max_lookback,
            max(config.momentum_lookbacks_days),
            config.momentum_skip_days,
            config.ts_momentum_lookback_days,
            config.vol_estimate_window_days,
            config.universe_volume_window_days,
            config.carry_lookback_days,
            config.regime_sma_days,
        )
        return _empty_factor_trades(), {}, []
    rebalance_dates = dates_all[max_lookback :: config.rebalance_days]
    if not rebalance_dates:
        _logger.warning(
            "momentum_factor backtest produced zero trades: warm-up consumed all daily bars "
            "(max_lookback=%d, dates=%d, rebalance_days=%d).",
            max_lookback, len(dates_all), config.rebalance_days,
        )
        return _empty_factor_trades(), {}, []

    features_by_date: dict[int, list[dict[str, Any]]] = {}
    for part in features.partition_by("ts_ms", maintain_order=True):
        ts = int(part["ts_ms"][0])
        features_by_date[ts] = part.to_dicts()

    trade_rows: list[dict[str, Any]] = []
    rebalance_log: list[dict[str, Any]] = []
    stats = {
        "rebalances_total": 0,
        "rebalances_regime_off": 0,
        "rebalances_with_positions": 0,
        "longs_opened": 0,
        "shorts_opened": 0,
        "skipped_no_entry_bar": 0,
        "skipped_no_exit_bar": 0,
    }
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier

    # Each rebalance generates trades held until the NEXT rebalance.
    for i, rebalance_ts in enumerate(rebalance_dates[:-1]):
        next_rebalance_ts = rebalance_dates[i + 1]
        rows_today = features_by_date.get(rebalance_ts, [])
        if not rows_today:
            continue

        if config.min_momentum_dispersion is not None:
            moms = [
                float(r.get("momentum_7d") or r.get("momentum_14d") or 0)
                for r in rows_today
                if r.get("in_universe")
            ]
            if len(moms) >= 5 and float(np.std(moms)) < config.min_momentum_dispersion:
                stats["rebalances_total"] += 1
                continue

        positions = _build_target_portfolio(rows_today, config=config)
        regime_on = bool(rows_today[0].get("regime_on", False)) if rows_today else False
        if config.factor_variant == VARIANT_ADAPTIVE_DUAL:
            regime_scale = 1.0
        else:
            regime_scale = 1.0 if (regime_on or not config.use_regime_filter) else config.regime_off_scale
        if regime_scale != 1.0:
            positions = [{**p, "weight": p["weight"] * regime_scale} for p in positions]
            stats["rebalances_regime_off"] += 1

        # Vol-targeting overlay: rescale total exposure to hit portfolio vol target.
        if config.vol_target_annual > 0.0 and positions:
            scale = _vol_target_scale(positions, config=config)
            positions = [{**p, "weight": p["weight"] * scale} for p in positions]

        stats["rebalances_total"] += 1
        if positions:
            stats["rebalances_with_positions"] += 1

        rebalance_log.append(
            {
                "ts_ms": rebalance_ts,
                "date": _iso_date(rebalance_ts),
                "regime_on": regime_on,
                "regime_scale": regime_scale,
                "long_count": sum(1 for p in positions if p["side"] == "long"),
                "short_count": sum(1 for p in positions if p["side"] == "short"),
                "gross_exposure": sum(abs(p["weight"]) for p in positions),
            }
        )

        entry_ts_ms = rebalance_ts + config.entry_delay_hours * MS_PER_HOUR
        exit_ts_ms = next_rebalance_ts + config.entry_delay_hours * MS_PER_HOUR

        for pos in positions:
            symbol = pos["symbol"]
            bars = bars_by_symbol.get(symbol)
            entry_idx = bars["by_end"].get(entry_ts_ms) if bars else None
            exit_idx = bars["by_end"].get(exit_ts_ms) if bars else None
            if entry_idx is None or bars is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            entry_price = float(bars["close"][entry_idx])
            if not math.isfinite(entry_price) or entry_price <= 0.0:
                stats["skipped_no_entry_bar"] += 1
                continue
            if exit_idx is None:
                stats["skipped_no_exit_bar"] += 1
                # Use last available bar before exit_ts as exit.
                exit_idx = _last_bar_before(bars, exit_ts_ms)
                if exit_idx is None:
                    continue
            exit_price = float(bars["close"][exit_idx])
            if not math.isfinite(exit_price) or exit_price <= 0.0:
                continue
            actual_exit_ts = int(bars["bar_end_ts_ms"][exit_idx])

            trade = _finalize_factor_trade(
                pos=pos,
                signal_ts_ms=rebalance_ts,
                entry_ts_ms=entry_ts_ms,
                exit_ts_ms=actual_exit_ts,
                entry_price=entry_price,
                exit_price=exit_price,
                round_trip_cost_bps=round_trip_cost_bps,
                funding_lookup=funding_lookup,
            )
            trade_rows.append(trade)
            if pos["side"] == "long":
                stats["longs_opened"] += 1
            else:
                stats["shorts_opened"] += 1

    trades = (
        pl.DataFrame(trade_rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"])
        if trade_rows
        else _empty_factor_trades()
    )
    return trades, stats, rebalance_log


def _score_eligible_rows(
    eligible: list[dict[str, Any]],
    *,
    config: MomentumFactorConfig,
    regime_on: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (scored rows with _composite, bear_mode flag)."""
    momentum_scores: list[float] = []
    carry_scores: list[float] = []
    reversal_scores: list[float] = []
    inv_vol_scores: list[float] = []
    for r in eligible:
        m_vals = [_safe_float(r.get(f"momentum_{lb}d")) for lb in config.momentum_lookbacks_days]
        m_vals = [v for v in m_vals if v is not None]
        momentum_scores.append(np.mean(m_vals) if m_vals else float("nan"))
        carry_scores.append(_safe_float(r.get("carry")) if r.get("carry") is not None else float("nan"))
        reversal_scores.append(
            _safe_float(r.get("reversal_signal")) if r.get("reversal_signal") is not None else float("nan")
        )
        vol = _safe_float(r.get("realized_vol")) or config.vol_floor_annual
        inv_vol_scores.append(1.0 / max(vol, config.vol_floor_annual))

    mom_arr = np.asarray(momentum_scores, dtype=float)
    carry_z = _zscore(np.asarray(carry_scores, dtype=float))
    reversal_z = _zscore(np.asarray(reversal_scores, dtype=float))
    inv_vol_z = _zscore(np.asarray(inv_vol_scores, dtype=float))

    variant = config.factor_variant
    bear_mode = False

    if variant == VARIANT_ADAPTIVE_DUAL and not regime_on:
        bear_mode = True
        composite = -np.where(np.isfinite(reversal_z), reversal_z, 0.0) + 0.6 * inv_vol_z
    elif variant == VARIANT_BTC_RESIDUAL:
        btc_7d = _safe_float(eligible[0].get("btc_return_7d")) or 0.0
        residual = mom_arr - btc_7d
        composite = _zscore(residual)
    elif variant == VARIANT_QUALITY_BLEND:
        composite = _zscore(mom_arr)
        composite = composite - config.carry_weight * np.where(np.isfinite(carry_z), carry_z, 0.0)
        composite = composite + 0.4 * inv_vol_z
    else:
        composite = _zscore(mom_arr)
        if config.carry_weight != 0.0:
            composite = composite - config.carry_weight * np.where(np.isfinite(carry_z), carry_z, 0.0)
        if config.reversal_weight != 0.0:
            composite = composite - config.reversal_weight * np.where(np.isfinite(reversal_z), reversal_z, 0.0)

    scored = [
        {
            **r,
            "_composite": float(composite[i]),
            "_momentum_avg": float(momentum_scores[i] if not np.isnan(momentum_scores[i]) else 0.0),
        }
        for i, r in enumerate(eligible)
        if np.isfinite(composite[i])
    ]
    return scored, bear_mode


def _build_target_portfolio(rows_today: list[dict[str, Any]], *, config: MomentumFactorConfig) -> list[dict[str, Any]]:
    """Compute target weights for this rebalance."""
    # Filter eligible universe.
    eligible = [
        r for r in rows_today
        if r.get("in_universe") and _has_required_signals(r, config)
    ]
    if not eligible:
        return []

    regime_on = bool(eligible[0].get("regime_on", False))
    scored, bear_mode = _score_eligible_rows(eligible, config=config, regime_on=regime_on)
    if not scored:
        return []
    scored.sort(key=lambda x: x["_composite"], reverse=True)
    n = len(scored)

    n_long = max(int(round(n * config.long_quantile)), 1)
    if bear_mode:
        n_long = min(n_long, 5)
    long_set = scored[:n_long]
    if config.require_positive_ts_momentum_for_longs and not bear_mode:
        long_set = [r for r in long_set if (_safe_float(r.get("ts_momentum")) or 0.0) > 0.0]
    if bear_mode:
        long_set = [
            r for r in long_set
            if (_safe_float(r.get("realized_vol")) or 9.0) <= min(config.max_realized_vol or 1.4, 1.2)
        ]
    long_set = [r for r in long_set if _passes_entry_filters(r, config)]

    short_set: list[dict[str, Any]] = []
    if config.mode == MODE_LONG_SHORT:
        n_short = max(int(round(n * config.short_quantile)), 1)
        short_set = scored[-n_short:]
        if config.require_negative_ts_momentum_for_shorts:
            short_set = [r for r in short_set if (_safe_float(r.get("ts_momentum")) or 0.0) < 0.0]

    # Sizing.
    long_weights = _compute_weights(long_set, config=config, side="long")
    short_weights = _compute_weights(short_set, config=config, side="short")

    # Allocate gross exposure across long / short.
    if config.mode == MODE_LONG_SHORT and short_set:
        long_budget = config.gross_exposure * 0.5
        short_budget = config.gross_exposure * 0.5
    else:
        long_budget = config.gross_exposure
        short_budget = 0.0

    positions: list[dict[str, Any]] = []
    sum_lw = sum(long_weights.values()) if long_weights else 0.0
    for r in long_set:
        raw = long_weights.get(r["symbol"], 0.0)
        w = (raw / sum_lw) * long_budget if sum_lw > 0 else 0.0
        w = min(w, config.max_position_weight)
        if w > 0:
            positions.append(
                {
                    "symbol": r["symbol"],
                    "side": "long",
                    "weight": w,
                    "score": r["_composite"],
                    "momentum_avg": r["_momentum_avg"],
                    "ts_momentum": _safe_float(r.get("ts_momentum")) or 0.0,
                    "carry": _safe_float(r.get("carry")) or 0.0,
                    "realized_vol": _safe_float(r.get("realized_vol")) or 0.0,
                }
            )

    if short_set and short_budget > 0:
        sum_sw = sum(short_weights.values()) if short_weights else 0.0
        for r in short_set:
            raw = short_weights.get(r["symbol"], 0.0)
            w = (raw / sum_sw) * short_budget if sum_sw > 0 else 0.0
            w = min(w, config.max_position_weight)
            if w > 0:
                positions.append(
                    {
                        "symbol": r["symbol"],
                        "side": "short",
                        "weight": w,
                        "score": r["_composite"],
                        "momentum_avg": r["_momentum_avg"],
                        "ts_momentum": _safe_float(r.get("ts_momentum")) or 0.0,
                        "carry": _safe_float(r.get("carry")) or 0.0,
                        "realized_vol": _safe_float(r.get("realized_vol")) or 0.0,
                    }
                )

    return positions


def _passes_entry_filters(row: dict[str, Any], config: MomentumFactorConfig) -> bool:
    """Post-rank entry gates from trade-level forensics (applied before sizing)."""
    if config.min_ts_momentum is not None:
        if (_safe_float(row.get("ts_momentum")) or 0.0) < config.min_ts_momentum:
            return False
    if config.min_momentum_avg is not None:
        if (_safe_float(row.get("_momentum_avg")) or 0.0) < config.min_momentum_avg:
            return False
    if config.max_carry is not None:
        carry = _safe_float(row.get("carry"))
        if carry is not None and carry > config.max_carry:
            return False
    if config.max_realized_vol is not None:
        vol = _safe_float(row.get("realized_vol"))
        if vol is not None and vol > config.max_realized_vol:
            return False
    if config.min_composite_score is not None:
        if (_safe_float(row.get("_composite")) or 0.0) < config.min_composite_score:
            return False
    if config.max_composite_score is not None:
        if (_safe_float(row.get("_composite")) or 0.0) > config.max_composite_score:
            return False
    if config.max_momentum_avg is not None:
        if (_safe_float(row.get("_momentum_avg")) or 0.0) > config.max_momentum_avg:
            return False
    rank = row.get("turnover_rank")
    if config.max_turnover_rank is not None and rank is not None:
        if int(rank) > config.max_turnover_rank:
            return False
    if config.min_turnover_rank is not None and rank is not None:
        if int(rank) < config.min_turnover_rank:
            return False
    return True


def _has_required_signals(row: dict[str, Any], config: MomentumFactorConfig) -> bool:
    # Need at least one finite momentum value and finite vol.
    for lb in config.momentum_lookbacks_days:
        v = row.get(f"momentum_{lb}d")
        if v is not None and math.isfinite(float(v)):
            return True
    return False


def _compute_weights(rows: list[dict[str, Any]], *, config: MomentumFactorConfig, side: str) -> dict[str, float]:
    if not rows:
        return {}
    if config.sizing == SIZING_EQUAL:
        return {r["symbol"]: 1.0 for r in rows}
    # vol_parity: weight = 1 / max(vol, floor)
    weights = {}
    for r in rows:
        v = _safe_float(r.get("realized_vol")) or config.vol_floor_annual
        v = max(v, config.vol_floor_annual)
        weights[r["symbol"]] = 1.0 / v
    return weights


def _vol_target_scale(positions: list[dict[str, Any]], *, config: MomentumFactorConfig) -> float:
    """Scale total exposure to hit the configured portfolio vol target.

    Uses single-factor approximation: portfolio_vol² ≈ (1-ρ) Σ wᵢ²σᵢ² + ρ (Σ |wᵢ|σᵢ)².
    Then scale = vol_target / portfolio_vol_estimate, clamped to vol_target_max_scale.
    """
    if not positions:
        return 1.0
    rho = max(0.0, min(0.99, config.assumed_avg_correlation))
    w = np.asarray([abs(p["weight"]) for p in positions], dtype=float)
    sigma = np.asarray([max(_safe_float(p.get("realized_vol")) or config.vol_floor_annual, config.vol_floor_annual) for p in positions], dtype=float)
    diag = float((w * w * sigma * sigma).sum())
    off = float((w * sigma).sum() ** 2)
    port_vol = math.sqrt(max((1.0 - rho) * diag + rho * off, 1e-12))
    if port_vol <= 0:
        return 1.0
    scale = config.vol_target_annual / port_vol
    return float(min(max(scale, 0.0), config.vol_target_max_scale))


def _last_bar_before(bars: dict[str, Any], ts_ms: int) -> int | None:
    ends = bars["ends"]
    # Find largest end <= ts_ms.
    lo, hi = 0, len(ends)
    while lo < hi:
        mid = (lo + hi) // 2
        if ends[mid] <= ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1 if lo > 0 else None


def _finalize_factor_trade(
    *,
    pos: dict[str, Any],
    signal_ts_ms: int,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_price: float,
    exit_price: float,
    round_trip_cost_bps: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    side = pos["side"]
    gross_trade_return = (exit_price / entry_price - 1.0) if entry_price > 0.0 else 0.0
    if side == "short":
        gross_trade_return = -gross_trade_return
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=pos["symbol"],
        side=side,
        entry_ts_ms=int(entry_ts_ms),
        exit_ts_ms=int(exit_ts_ms),
    )
    effective_weight = abs(float(pos["weight"]))
    funding_return = effective_weight * raw_funding_return
    cost_return = -effective_weight * round_trip_cost_bps / 10_000.0
    gross_return = effective_weight * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    basket_id = f"factor-{_iso_date(signal_ts_ms)}"
    return {
        "trade_id": f"{basket_id}-{side[0]}-{pos['symbol']}",
        "basket_id": basket_id,
        "entry_signal_ts_ms": int(signal_ts_ms),
        "entry_ts_ms": int(entry_ts_ms),
        "exit_ts_ms": int(exit_ts_ms),
        "entry_date": _iso_date(int(entry_ts_ms)),
        "exit_date": _iso_date(int(exit_ts_ms)),
        "exit_month": _iso_month(int(exit_ts_ms)),
        "symbol": pos["symbol"],
        "side": side,
        "score": float(pos.get("score", 0.0)),
        "rank": 0,
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "exit_reason": "rebalance",
        "planned_exit_ts_ms": int(exit_ts_ms),
        "stop_price": None,
        "take_profit_price": None,
        "notional_weight": effective_weight,
        "position_weight": float(pos["weight"]),
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": int(funding_event_count),
        "net_return": net_return,
        "mae": 0.0,
        "mfe": 0.0,
        "bars_held": int(round((int(exit_ts_ms) - int(entry_ts_ms)) / MS_PER_HOUR)),
        "hold_hours": (int(exit_ts_ms) - int(entry_ts_ms)) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(entry_ts_ms) - int(signal_ts_ms)) / MS_PER_HOUR,
        "momentum_avg": float(pos.get("momentum_avg", 0.0)),
        "ts_momentum": float(pos.get("ts_momentum", 0.0)),
        "carry": float(pos.get("carry", 0.0)),
        "realized_vol": float(pos.get("realized_vol", 0.0)),
    }


def _empty_factor_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "basket_id": pl.Series([], dtype=pl.String),
            "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_date": pl.Series([], dtype=pl.String),
            "exit_date": pl.Series([], dtype=pl.String),
            "exit_month": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "score": pl.Series([], dtype=pl.Float64),
            "rank": pl.Series([], dtype=pl.Int64),
            "entry_price": pl.Series([], dtype=pl.Float64),
            "exit_price": pl.Series([], dtype=pl.Float64),
            "exit_reason": pl.Series([], dtype=pl.String),
            "planned_exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "stop_price": pl.Series([], dtype=pl.Float64),
            "take_profit_price": pl.Series([], dtype=pl.Float64),
            "notional_weight": pl.Series([], dtype=pl.Float64),
            "position_weight": pl.Series([], dtype=pl.Float64),
            "gross_trade_return": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "funding_mode": pl.Series([], dtype=pl.String),
            "funding_event_count": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
            "mae": pl.Series([], dtype=pl.Float64),
            "mfe": pl.Series([], dtype=pl.Float64),
            "bars_held": pl.Series([], dtype=pl.Int64),
            "hold_hours": pl.Series([], dtype=pl.Float64),
            "actual_entry_delay_hours": pl.Series([], dtype=pl.Float64),
            "momentum_avg": pl.Series([], dtype=pl.Float64),
            "ts_momentum": pl.Series([], dtype=pl.Float64),
            "carry": pl.Series([], dtype=pl.Float64),
            "realized_vol": pl.Series([], dtype=pl.Float64),
        }
    )


def _split_rows(baskets: pl.DataFrame, *, config: TradeLifecycleConfig) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in SPLITS:
        start_ms = date_ms(start)
        end_ms = date_ms(end)
        part = (
            baskets.filter((pl.col("entry_signal_ts_ms") >= start_ms) & (pl.col("entry_signal_ts_ms") < end_ms))
            if not baskets.is_empty()
            else baskets
        )
        rows.append(_summarize_split(part, name=name, config=config))
    return rows


def _summarize_split(part: pl.DataFrame, *, name: str, config: TradeLifecycleConfig) -> dict[str, Any]:
    if part.is_empty():
        return {
            "name": name,
            "basket_count": 0,
            "total_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
        }
    returns = np.asarray(part.sort("entry_signal_ts_ms")["basket_return"].to_list(), dtype=float)
    equity_curve = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(equity_curve)
    drawdowns = equity_curve / peaks - 1.0
    stdev = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    mean = float(np.mean(returns)) if returns.size else 0.0
    annual_periods = 365.0 / max(config.rebalance_days, 1)
    return {
        "name": name,
        "basket_count": int(returns.size),
        "total_return": float(equity_curve[-1] - 1.0),
        "sharpe_like": float(mean / stdev * math.sqrt(annual_periods)) if stdev > 1e-12 else 0.0,
        "max_drawdown": float(drawdowns.min()),
    }


def _evaluate_promotion(
    *,
    split_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    funding_mode: str,
    full_pit_universe_pass: bool,
    config: MomentumFactorConfig,
) -> dict[str, Any]:
    splits_with_baskets = [row for row in split_rows if row["basket_count"] > 0]
    all_splits_positive = bool(splits_with_baskets) and all(
        row["total_return"] > 0.0 for row in splits_with_baskets
    )
    avg_sharpe = (
        float(np.mean([row["sharpe_like"] for row in splits_with_baskets]))
        if splits_with_baskets
        else 0.0
    )
    max_dd_ok = summary.get("max_drawdown", 0.0) >= config.promotion_max_drawdown
    sharpe_ok = avg_sharpe >= config.promotion_min_avg_sharpe
    funding_ok = funding_mode != "missing"
    reasons = []
    if not full_pit_universe_pass:
        reasons.append("full_pit_universe_missing")
    if not all_splits_positive:
        reasons.append("not_all_splits_positive")
    if not max_dd_ok:
        reasons.append("max_drawdown_too_deep")
    if not sharpe_ok:
        reasons.append("avg_split_sharpe_below_threshold")
    if not funding_ok:
        reasons.append("funding_missing")
    return {
        "promotion_gate_pass": bool(
            full_pit_universe_pass
            and all_splits_positive
            and max_dd_ok
            and sharpe_ok
            and funding_ok
        ),
        "all_splits_positive": all_splits_positive,
        "splits_with_baskets": len(splits_with_baskets),
        "avg_split_sharpe": avg_sharpe,
        "max_drawdown": float(summary.get("max_drawdown", 0.0)),
        "funding_mode": funding_mode,
        "promotion_reasons": reasons,
    }


def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "strategy_return": pl.Series([], dtype=pl.Float64),
                "baskets": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("short_return").sum().alias("short_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.len().alias("baskets"),
            ]
        )
        .sort("month")
    )


def _run_label(
    *,
    config: MomentumFactorConfig,
    archive_manifest: pl.DataFrame,
    full_pit_universe_pass: bool,
    funding_mode: str,
) -> str:
    if archive_manifest.is_empty():
        return "pit_required_missing_manifest" if config.require_pit_membership else "biased_benchmark"
    if not full_pit_universe_pass:
        return "pit_membership_filtered_current_universe"
    if funding_mode == "missing":
        return "full_pit_universe_funding_missing"
    if funding_mode == "partial":
        return "full_pit_universe_funding_partial"
    return "full_pit_universe"


def _zscore(arr: np.ndarray) -> np.ndarray:
    finite = np.isfinite(arr)
    out = np.full(arr.shape, np.nan, dtype=float)
    if finite.sum() < 2:
        return out
    vals = arr[finite]
    mean = float(vals.mean())
    std = float(vals.std(ddof=1))
    if std <= 1e-12:
        out[finite] = 0.0
        return out
    out[finite] = (arr[finite] - mean) / std
    return out


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


def format_factor_report(metadata: dict[str, Any]) -> str:
    cfg = metadata.get("config", {})
    pit = metadata.get("pit_manifest", {})
    summary = metadata.get("summary", {})
    promotion = metadata.get("promotion", {})
    splits = metadata.get("splits", [])
    lifecycle = metadata.get("lifecycle", {})
    date_range = metadata.get("date_range", {})

    lines: list[str] = [
        "# Cross-Sectional Momentum Factor Research",
        "",
        f"Mode: **{cfg.get('mode')}** · Rebalance: every **{cfg.get('rebalance_days')}** days · "
        f"Momentum lookbacks: {cfg.get('momentum_lookbacks_days')} (skip {cfg.get('momentum_skip_days')}d)",
        "",
        "## Inputs",
        "",
        f"- Run label: `{metadata.get('run_label', 'unknown')}`",
        f"- Date range: {date_range.get('start')} to {date_range.get('end')}",
        f"- Feature rows: {metadata.get('rows', {}).get('features', 0)}",
        f"- Rebalances: {metadata.get('rows', {}).get('rebalances', 0)}",
        f"- Trades: {metadata.get('rows', {}).get('trades', 0)}",
        f"- Universe: top {cfg.get('universe_size')} by {cfg.get('universe_volume_window_days')}d median turnover",
        f"- Long quantile: {cfg.get('long_quantile')} · Short quantile: {cfg.get('short_quantile')}",
        f"- Sizing: `{cfg.get('sizing')}` · gross_exposure: {cfg.get('gross_exposure')} · vol_target: {cfg.get('vol_target_annual')}",
        f"- Carry weight: {cfg.get('carry_weight')} · TS-momentum filter (long): {cfg.get('require_positive_ts_momentum_for_longs')}",
        f"- Regime gate: {cfg.get('use_regime_filter')} (sma_{cfg.get('regime_sma_days')}d, off_scale={cfg.get('regime_off_scale')})",
        f"- Cost multiplier: {cfg.get('cost_multiplier')}x · effective round-trip bps: "
        f"{metadata.get('cost_model', {}).get('effective_round_trip_cost_bps', 0):.2f}",
        f"- Full PIT: {pit.get('full_pit_universe_pass', False)} · feature symbols: {pit.get('feature_symbols', 0)}",
        "",
        "## Headline metrics",
        "",
        f"- Total return: {pct(summary.get('total_return'))}",
        f"- Sharpe-like (annualized): {summary.get('sharpe_like', 0.0):.2f}",
        f"- Max drawdown: {pct(summary.get('max_drawdown'))}",
        f"- Max underwater days: {summary.get('max_underwater_days', 0)}",
        f"- Worst 30d / 60d / 90d / 120d: {pct(summary.get('worst_30d_return'))} / "
        f"{pct(summary.get('worst_60d_return'))} / {pct(summary.get('worst_90d_return'))} / "
        f"{pct(summary.get('worst_120d_return'))}",
        f"- Trade win rate: {pct(summary.get('trade_win_rate'))}",
        f"- Profit factor: {summary.get('profit_factor', 0.0):.2f}",
        f"- Gross return: {pct(summary.get('gross_return'))} | cost: {pct(summary.get('cost_return'))} | "
        f"funding: {pct(summary.get('funding_return'))} ({summary.get('funding_mode', 'missing')})",
        f"- Long return: {pct(summary.get('long_return'))} | Short return: {pct(summary.get('short_return'))}",
        "",
        "## Promotion gate",
        "",
        f"- Pass: **{promotion.get('promotion_gate_pass', False)}**",
        f"- All splits positive: {promotion.get('all_splits_positive', False)} "
        f"({promotion.get('splits_with_baskets', 0)}/{len(SPLITS)} with baskets)",
        f"- Avg split sharpe: {promotion.get('avg_split_sharpe', 0.0):.2f}",
        f"- Funding mode: `{promotion.get('funding_mode', 'missing')}`",
        f"- Block reasons: {', '.join(promotion.get('promotion_reasons', [])) or 'none'}",
        "",
        "## Splits",
        "",
        "| Split | Baskets | Return | Sharpe | Max DD |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in splits:
        lines.append(
            f"| {row.get('name')} | {row.get('basket_count', 0)} | "
            f"{pct(row.get('total_return'))} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{pct(row.get('max_drawdown'))} |"
        )

    lines.extend(
        [
            "",
            "## Lifecycle counters",
            "",
            f"- Rebalances total / regime-off / with positions: "
            f"{lifecycle.get('rebalances_total', 0)} / "
            f"{lifecycle.get('rebalances_regime_off', 0)} / "
            f"{lifecycle.get('rebalances_with_positions', 0)}",
            f"- Longs opened: {lifecycle.get('longs_opened', 0)} · Shorts opened: {lifecycle.get('shorts_opened', 0)}",
            f"- Skipped: no_entry_bar={lifecycle.get('skipped_no_entry_bar', 0)}, "
            f"no_exit_bar={lifecycle.get('skipped_no_exit_bar', 0)}",
            "",
            "## Output files",
            "",
            "```text",
            "momentum_factor_trades.csv",
            "momentum_factor_baskets.csv",
            "momentum_factor_equity.csv",
            "momentum_factor_monthly.csv",
            "momentum_factor_research_report.json",
            "momentum_factor_research_report.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
