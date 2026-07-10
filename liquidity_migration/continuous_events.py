"""Execution-grade backtest for the CONTINUOUS liquidity-migration fade.

Every prior continuous-fade result (P0 -> p1m) is an EXPLORATORY proxy: per-spell
additive PnL, a mid-fill at the SAME close used to rank, a flat 15/30 bps cost with
NO market impact, and no compounding. This module closes those gaps so the continuous
book is measured under the shared execution-grade lifecycle machinery.

It REPRODUCES the proxy SELECTION exactly (so it is auditable against p1d/p1j/p1k):
  - 5 trailing closed-bar features (rv_168h, vov, dist_low, xsret7, xsret3),
  - within-ts composite decile on the rmom-LOW half (causal day-floor lag1 join of
    residual_momentum.parquet),
  - short the top composite decile (D9), fresh spell entry (gap > 1h), liquid gate
    (signal-bar hourly turnover_quote >= threshold).

It runs that selection through the validated execution core
(`_simulate_indexed_trade` + `trade_lifecycle`: stop fills, funding-to-exit, MAE/MFE,
compounding equity, drawdown, Sharpe) and adds the three realism upgrades the proxy
lacked:
  1. HONEST +1h entry -- fill at the bar AFTER the deciding bar's close. The proxy
     filled at the same close it ranked on (execution look-ahead). entry_delay_hours=0
     reproduces the proxy (validation only).
  2. A real round-trip COST = 2*taker + 2*spread + 2*impact, where impact rises with
     size and falls with liquidity: impact_bps = impact_coef_bps * participation^exp,
     participation = position_notional / signal-bar hourly turnover. This is the
     capacity-aware cost the p1c argument + the integrity gate demand.
  3. COMPOUNDING equity + true concurrency (heap of exit-times, max_active cap) +
     per-symbol cooldown, and the full artifact set (ledger, equity curve, splits,
     drawdown, worst-day, config hash, run label).

EXPLORATORY engine: impact coefficients are modeled, not venue-calibrated, and
the selection params come from a heavily multiple-tested research arc.
"""
from __future__ import annotations

import bisect
import hashlib
import heapq
import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, calendar_shift, exact_duration_ms
from .config import DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .daily_feature_panel import _autodetect_dataset_names, _date_str_to_ms, _read_window
from .storage import read_dataset_columns
from .trade_lifecycle import (
    _empty_trades,
    _funding_lookup,
    _indexed_price_bars_by_symbol,
    _simulate_indexed_trade,
    _collapse_interval_min,
    annualized_sharpe,
    derive_funding_interval_min,
    funding_cadence_stats,
)

FEATURES = ("rv_168h", "vov", "dist_low", "xsret7", "xsret3")
BTC_TREND_MODE_DAILY_PRIOR = "daily_prior"
BTC_TREND_MODE_HOURLY_30D = "hourly_30d"
BTC_TREND_MODE_HOURLY_EXACT_MONTH = "hourly_exact_month"
BTC_TREND_MODE_SMART_MONTH = "smart_month"
BTC_TREND_MODES = (
    BTC_TREND_MODE_DAILY_PRIOR,
    BTC_TREND_MODE_HOURLY_30D,
    BTC_TREND_MODE_HOURLY_EXACT_MONTH,
    BTC_TREND_MODE_SMART_MONTH,
)
BTC_EXACT_MONTH_DAYS = 365.25 / 12.0

_RESEARCH_INPUT_CACHE_MAX = 4
_RESEARCH_INPUT_CACHE: OrderedDict[tuple[str, str, str, int], dict[str, Any]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class ContinuousEventConfig:
    """Continuous-fade engine config. See module docstring + the pre-registration."""

    start_date: str = "2023-04-01"
    end_date: str = ""                    # "" = data-driven: clamp to the root's last available day
    #                                       (end-exclusive, so the final full day is included). A
    #                                       fixed past date silently truncated recent data as the
    #                                       calendar advanced (cli-config-5).
    # --- selection (ported from p1d._deciled_panel) ---
    side: str = "short"
    decile: int = 9                       # short the top composite decile
    rmom_quantile: float = 0.5            # rmom-LOW half: keep within-ts rmom rank <= this
    feature_set: tuple[str, ...] = FEATURES  # composite features; all are trailing/causal
    liq_turnover_min: float = 500_000.0   # liquid gate: signal-bar hourly turnover_quote (USD)
    # --- execution ---
    entry_delay_hours: int = 1            # bars AFTER the deciding bar's close (0 = proxy/look-ahead)
    entry_adverse_limit_pct: float = 0.0  # research-only: wait for a better adverse limit; 0=market/close
    entry_adverse_limit_wait_hours: int = 24  # max post-submit hours to wait for adverse limit fill
    exit_mode: str = "fixed"              # "fixed" = hold_hours timer; "state" = hold while in the fade decile
    exit_decile_buffer: int = 0            # state-mode hysteresis: D9/buffer=1 holds while decile >= D8
    hold_hours: int = 12                  # fixed-mode hold horizon
    max_hold_hours: int = 48              # state-mode cap (force exit if the name never leaves the decile)
    rank_exit_threshold: float = 0.0      # 0=off; short exits when composite rank fraction falls below threshold
    cooldown_hours: int = 0               # 0 -> fixed: hold_hours; state: 0 (spell-fresh already dedupes)
    stop_loss_pct: float = 0.0            # 0 -> no stop (proxy parity)
    stop_approach_frac: float = 0.0        # >0 cuts at frac * stop_loss_pct; live uses 0.8 of disaster stop
    take_profit_pct: float = 0.0          # 0 -> no take-profit; short TP exits on favorable downside
    stop_vol_mult: float = 0.0            # >0 -> vol-scaled stop = k * trailing hourly vol (overrides fixed
    #                                       stop_loss_pct per-trade), clamped to [5%,50%]. 0 -> fixed stop.
    stop_fill_mode: str = "bar_extreme_capped"
    stop_slippage_cap_pct: float = 0.10
    # --- sizing ---
    gross_exposure: float = 0.5           # 0.5 / 25 = 2% per name (matches the p1j/p1k proxy)
    max_active: int = 25
    # --- cost model: round_trip = 2*(taker+spread) + 2*impact ---
    taker_fee_bps: float = 5.5
    spread_bps: float = 2.5
    impact_coef_bps: float = 50.0
    impact_exponent: float = 0.5
    deploy_capital_usd: float = 1_000_000.0
    flat_round_trip_bps: float | None = None   # override the cost model (proxy-parity validation)
    round_trip_cost_multiplier: float = 1.0    # stress knob; 1.0 preserves the base cost model
    # --- inherited-from-daily refinements (ablation knobs; default OFF = current baseline) ---
    sizing_mode: str = "flat"             # "flat" (2% each) | "inverse_vol" (size by target_vol/rv, clamped)
    target_vol_per_name: float = 0.02     # inverse_vol: per-name hourly-vol target
    vol_weight_clamp: float = 3.0         # inverse_vol: clamp weight multiplier to [1/clamp, clamp]
    # Risk-objective entry sizing. A 0.001 budget with shock=1.0 caps one
    # symbol's loss under a +100% adverse gap at 0.10% of equity. This changes
    # exposure before entry; it is not a stop or an assumed exit fill.
    entry_disaster_loss_budget_frac: float = 0.0  # 0=off
    entry_disaster_shock_frac: float = 1.0
    # Aggregate live-notional shock budget. Applied independently to each
    # component before ensemble weighting; with component weights summing to 1,
    # the combined book inherits the same cap.
    entry_portfolio_heat_cap_frac: float = 0.0  # 0=off
    age_days_min: int = 0                 # skip symbols younger than this (fresh-listing squeezers)
    entry_max_ret168_max: float = 10.0    # skip entries with trailing 168h max 1h return above this; 10=off
    entry_decel_lookback_h: int = 0       # 0=off; require close[t]/close[t-lookback]-1 <= entry_decel_max_ret
    entry_decel_max_ret: float = 0.0      # fade-started confirmation: recent move must be <= this
    market_min_ret_1d: float = -1.0       # skip entry if equal-weight market 1d return < this (-1 = off)
    btc_trend_gate: str = "off"           # "off" | "uptrend" | "downtrend"
    btc_trend_lookback_days: int = 30     # prior BTC daily returns, excluding the signal day
    btc_trend_mode: str = BTC_TREND_MODE_DAILY_PRIOR
    btc_trend_month_days: float = BTC_EXACT_MONTH_DAYS
    btc_trend_smart_tolerance: float = 0.01
    entry_event_trigger: str = "none"      # hourly catalyst gate; "none" preserves continuous spell entries
    failed_fade_hours: int = 0            # 0=off; cut a fade that hasn't worked after N hours
    failed_fade_loss_pct: float = 0.0
    failed_fade_min_mfe_pct: float = 0.0
    breakeven_arm_pct: float = 0.0        # 0=off; once MFE>=this, exit if it returns to entry
    mfe_giveback_trigger_pct: float = 0.0
    mfe_giveback_retain_pct: float = 0.0
    hash_exit_prob: float = 0.0           # Negative-control per-bar hash exit prob; 0=off
    # Portfolio circuit breaker (correlated-squeeze defense): PAUSE new entries when >= N net-negative
    # exits (the live sleeve's "adverse cover" footprint of a market-wide alt melt-up) have completed
    # within the trailing entry_pause_window_hours. Causal: counts only exits that closed strictly
    # before the candidate entry. 0 = OFF. Mirrors continuous_demo.entry_circuit_breaker_tripped so the
    # engine validates the live knob (net_return<0 is the engine-side proxy for the live adverse set).
    entry_pause_after_adverse_exits: int = 0
    entry_pause_window_hours: int = 24
    entry_crowding_max_fresh: int = 0     # 0=off; skip signal hours with more fresh candidates than this
    entry_skip_external_size_multiplier_lte: float = 0.0  # 0=off; skip entries sized <= threshold by external hook
    # --- funding / splits / universe ---
    use_funding: bool = True
    split_date: str = "2025-06-01"        # early/recent boundary
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS

    @property
    def effective_cooldown_hours(self) -> int:
        if self.cooldown_hours > 0:
            return self.cooldown_hours
        # state-mode entries are one-per-D9-spell and can't double-open, so no extra cooldown;
        # fixed-mode uses hold_hours so a name can't re-enter mid-hold (matches the proxy).
        return 0 if self.exit_mode == "state" else self.hold_hours

    @property
    def notional_weight(self) -> float:
        return self.gross_exposure / max(self.max_active, 1)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]


def _iso_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _panel_cache_stale(cache_path: Path, rmom_path: Path) -> bool:
    """The deciled-panel cache is keyed ONLY on rmom_quantile, so a refresh of the underlying
    data — new klines → a rebuilt residual_momentum.parquet — must invalidate it. Without this
    the cache silently serves a panel truncated to the OLD data end (observed 2026-06-03: the
    continuous equity curve stuck at the prior data tail / May-27 after a fresh rebuild, because
    the Jun-2 cache predated the refreshed klines+rmom). Treat the cache as stale whenever the
    rmom panel is newer than it (a data refresh rebuilds rmom, bumping its mtime); fail safe to
    'stale' if either file can't be stat'd so we rebuild rather than serve a possibly-stale panel."""
    try:
        return (not rmom_path.exists()) or (cache_path.stat().st_mtime < rmom_path.stat().st_mtime)
    except OSError:
        return True


def _feature_tag(feature_set: tuple[str, ...]) -> str:
    raw = "_".join(feature_set) or "none"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _root_max_kline_ms(root: Path) -> int | None:
    """Max kline ``ts_ms`` available under ``root`` (None if no kline data)."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = read_dataset_columns(root, kname, columns=["ts_ms"])
    if k.is_empty() or "ts_ms" not in k.columns:
        return None
    return int(k["ts_ms"].max())


def _listing_ts_by_symbol(root: Path) -> dict[str, int]:
    """Per-symbol AUTHORITATIVE PIT listing timestamp: the first-ever kline ``ts_ms`` under the
    root, read over the FULL dataset independent of any run window.

    The age gate must measure listing age from a symbol's true first bar, not the first bar that
    happens to land inside the padded read window (which is clamped to ``start_ms - pad_back`` and
    makes any older symbol look exactly ``pad_back`` old at the window start). This mirrors the live
    demo's ``universe.listing_age_days`` (Bybit launchTime) using the root's own data, so the
    backtest age floor admits the same symbols near the window edge as the live sleeve does
    (pit-engine-2)."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = read_dataset_columns(root, kname, columns=["symbol", "ts_ms"])
    if k.is_empty() or "symbol" not in k.columns or "ts_ms" not in k.columns:
        return {}
    first = k.group_by("symbol").agg(pl.col("ts_ms").min().alias("first_ts"))
    return {str(s): int(t) for s, t in first.iter_rows()}


def _resolve_end_ms(root: Path, config: ContinuousEventConfig) -> int:
    """End-exclusive window boundary in ms.

    An explicit ``end_date`` is honored verbatim (frozen/forward runs pin it). An empty
    ``end_date`` is the DATA-DRIVEN default: clamp to the day AFTER the root's last available
    kline (end-exclusive, so the final full day is included), so a default research run never
    silently omits the freshest data as the calendar advances (cli-config-5)."""
    if config.end_date:
        return _date_str_to_ms(config.end_date)
    max_ts = _root_max_kline_ms(root)
    if max_ts is None:
        # No kline data to clamp against; fall back to start so the empty-root path returns empty.
        return _date_str_to_ms(config.start_date)
    return (max_ts // MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY


def _window_tag(config: ContinuousEventConfig, end_ms: int) -> str:
    # When end_date is data-driven (""), bake the RESOLVED end into the cache key so two runs
    # at different data ends never collide on the same cached panel.
    end_part = config.end_date or f"auto{end_ms}"
    return hashlib.sha256(f"{config.start_date}_{end_part}".encode("utf-8")).hexdigest()[:8]


def _exclude_tag(exclude_symbols: Any) -> str:
    """Stable 8-char hash of the exclusion set, order-independent."""
    syms = sorted(str(s) for s in (exclude_symbols or []))
    return hashlib.sha256("|".join(syms).encode("utf-8")).hexdigest()[:8]


def _panel_cache_path(root: Path, config: ContinuousEventConfig, *, end_ms: int) -> Path:
    window_tag = _window_tag(config, end_ms)
    # audit2: fold the exclusion set into the cache key. The panel is built with
    # `config.exclude_symbols` filtered out (build_continuous_panel below), but the
    # old key omitted it — two runs differing ONLY in exclude_symbols collided on the
    # same cached parquet and the second silently reused the first's (wrong-exclusion)
    # panel. The empty-exclusion case (the live/default path) keeps its prior filename
    # byte-for-byte, so no existing cache is invalidated; only non-empty exclusions get
    # a distinct, correct key.
    excl_part = "" if not config.exclude_symbols else f"_excl{_exclude_tag(config.exclude_symbols)}"
    return root / (
        # v4 invalidates panels built before provisional residual-momentum rows
        # were excluded from live/research consumers.
        f"_continuous_engine_panel_v4_rmom{int(round(config.rmom_quantile * 100))}"
        f"_feat{_feature_tag(config.feature_set)}{excl_part}_{window_tag}.parquet"
    )


# Max days the rmom table may lag the klines window's last day before the backtest panel build
# refuses to run. residual_momentum[D] sums residual_return[D-9..D-3] (precompute shift(3)), so a
# freshly rebuilt rmom legitimately trails the newest kline day by a couple of days; beyond that the
# table is STALE and the left-join+null-filter would SILENTLY drop the newest dates from the panel
# (the documented 2026-06-03 truncation). Matches the live watchdog's --max-rmom-stale-days default.
RMOM_COVERAGE_TOLERANCE_DAYS = 2


def validated_stable_residual_momentum(
    table: pl.DataFrame,
    *,
    source: str | Path,
) -> pl.DataFrame:
    """Validate RMOM provenance/keys/values, then return stable rows only.

    Duplicate keys multiply panel rows and NaN/Inf values can corrupt ranks. A
    legacy table without provenance can expose its mutable tail. All three are
    wrong-signal failures, so live callers degrade the raised error to no signal
    while research and reconciliation fail loudly.
    """
    required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
    missing = sorted(required - set(table.columns))
    if missing:
        if "is_provisional" in missing:
            raise RuntimeError(f"{source} lacks boolean is_provisional provenance")
        raise RuntimeError(f"{source} missing residual-momentum columns: {missing}")
    if table.schema["is_provisional"] != pl.Boolean:
        raise RuntimeError(f"{source} lacks boolean is_provisional provenance")
    if table.schema["symbol"] != pl.String:
        raise RuntimeError(f"{source} symbol must be String")
    if table.schema["ts_ms"] != pl.Int64:
        raise RuntimeError(f"{source} ts_ms must be Int64")
    if table.schema["residual_momentum"] not in (pl.Float32, pl.Float64):
        raise RuntimeError(f"{source} residual_momentum must be floating point")
    if table["is_provisional"].null_count() > 0:
        raise RuntimeError(f"{source} contains null is_provisional provenance")
    invalid_keys = table.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("ts_ms").is_null()
        | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid_keys.is_empty():
        raise RuntimeError(f"{source} contains null, blank, or non-daily residual-momentum keys")
    duplicates = table.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(f"{source} has duplicate (symbol,ts_ms) residual-momentum keys")
    invalid = table.filter(
        pl.col("residual_momentum").is_null()
        | (~pl.col("residual_momentum").is_finite())
    )
    if not invalid.is_empty():
        raise RuntimeError(f"{source} contains null or non-finite residual_momentum")
    return table.filter(~pl.col("is_provisional"))


def _assert_rmom_covers_window(
    rmom: pl.DataFrame, klines: pl.DataFrame, *, start_ms: int, root: Path
) -> None:
    """Fail loudly when residual_momentum lags the klines window instead of silently truncating.

    The decile build left-joins rmom on (symbol, day_ts) and filters to non-null rmom, so a
    present-but-stale residual_momentum.parquet drops EVERY symbol on the newest days with no error
    — understating recent exposure/return in decision evidence (equity curves, research panels).
    The live daemon is guarded by max_rmom_day_ts / rmom_stale_days telemetry; this mirrors that
    guard on the backtest path (pit-data-5)."""
    in_window = klines.filter(pl.col("ts_ms") >= start_ms)
    if in_window.is_empty() or rmom.is_empty() or "day_ts" not in rmom.columns:
        return
    klines_max_day = (int(in_window["ts_ms"].max()) // MS_PER_DAY) * MS_PER_DAY
    rmom_max_day = (int(rmom["day_ts"].max()) // MS_PER_DAY) * MS_PER_DAY
    lag_days = (klines_max_day - rmom_max_day) // MS_PER_DAY
    if lag_days > RMOM_COVERAGE_TOLERANCE_DAYS:
        raise RuntimeError(
            f"residual_momentum.parquet is STALE: newest rmom day "
            f"{_iso_day(rmom_max_day)} lags the klines window's last day {_iso_day(klines_max_day)} "
            f"by {lag_days}d (> {RMOM_COVERAGE_TOLERANCE_DAYS}d). The decile join would silently "
            f"drop the newest dates from the panel. Rebuild it: "
            f"POLARS_MAX_THREADS=8 python scripts/precompute_residual_momentum.py --root {root}"
        )


def build_continuous_panel(
    data_root: str | Path, config: ContinuousEventConfig, *, cache: bool = True
) -> pl.DataFrame:
    """Build the deciled rmom-low panel: (symbol, ts_ms, decile, composite, turnover_quote).

    PIT-causal: all 5 features are trailing closed-bar windows on `ts_ms`'s close (known at
    ts_ms+1h); the rmom join is a day-floor lag1 (residual_momentum[D] uses residuals <= D-1).
    This is the engine-owned decile panel builder; it computes realised fills
    downstream rather than carrying a forward-return proxy column.
    """
    root = Path(str(data_root)).expanduser()
    start_ms, end_ms = _date_str_to_ms(config.start_date), _resolve_end_ms(root, config)
    cache_path = _panel_cache_path(root, config, end_ms=end_ms)
    rmom_path = root / "residual_momentum.parquet"
    if cache and cache_path.exists() and not _panel_cache_stale(cache_path, rmom_path):
        return pl.read_parquet(cache_path)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(
        root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )
    if k.is_empty():
        return pl.DataFrame()
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if not rmom_path.exists():
        raise FileNotFoundError(
            f"{rmom_path} missing -- build it first: "
            f"POLARS_MAX_THREADS=8 python scripts/precompute_residual_momentum.py --root {root}"
        )
    rmom = pl.read_parquet(rmom_path)
    # Provisional padded values are causal but can mature when a delayed
    # forward-target residual arrives. They are append telemetry, not an
    # approved trading signal; block them so live and research use only the
    # registered stable shift-3 series.
    rmom = validated_stable_residual_momentum(rmom, source=rmom_path)
    rmom = rmom.rename({"ts_ms": "day_ts"})
    _assert_rmom_covers_window(rmom, k, start_ms=start_ms, root=root)
    panel = compute_continuous_decile_panel(
        k,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=start_ms,
        feature_set=config.feature_set,
    )
    if cache:
        panel.write_parquet(cache_path)
    return panel


def per_symbol_timeseries_features(k: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol trailing-window features (NO cross-section): the rolling parts that depend only
    on a symbol's own closed-bar history (ret1, rv_168h, ret72, ret168, min720, max720, vov,
    dist_low). Split out of `compute_continuous_decile_panel` so the live demo can recompute these
    once per bar close and cache them (see `continuous_demo.LivePanelCache`); the expressions and
    their order are byte-for-byte the original inline version, so behaviour is unchanged."""
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
    k = k.with_columns(
        pl.col("ret1").rolling_std(window_size=168, min_samples=48).over("symbol").alias("rv_168h"),
        # max single-hour return over the trailing week (the MAX / lottery-demand feature, Bali et al.) —
        # always computed but only enters the composite when feature_set includes it, so the default
        # signal (and the live↔backtest equivalence) is unchanged. Research lever for the MAX base.
        pl.col("ret1").rolling_max(window_size=168, min_samples=48).over("symbol").alias("max_ret168"),
        (pl.col("close") / pl.col("close").shift(72).over("symbol") - 1.0).alias("ret72"),
        (pl.col("close") / pl.col("close").shift(168).over("symbol") - 1.0).alias("ret168"),
        pl.col("close").rolling_min(window_size=720, min_samples=168).over("symbol").alias("min720"),
        pl.col("close").rolling_max(window_size=720, min_samples=168).over("symbol").alias("max720"),
    )
    k = k.with_columns(
        pl.col("rv_168h").rolling_std(window_size=720, min_samples=168).over("symbol").alias("vov"),
        pl.when(pl.col("max720") > pl.col("min720"))
        .then((pl.col("close") - pl.col("min720")) / (pl.col("max720") - pl.col("min720")))
        .otherwise(None).alias("dist_low"),
        pl.col("ret1").shift(1).rolling_max(window_size=6, min_samples=1).over("symbol").alias("prior6_ret1_max"),
        pl.col("close").shift(1).rolling_max(window_size=6, min_samples=1).over("symbol").alias("prior6_close_max"),
        pl.col("turnover_quote")
        .shift(1)
        .rolling_mean(window_size=168, min_samples=48)
        .over("symbol")
        .alias("prior168_turnover_mean"),
        pl.col("turnover_quote")
        .shift(1)
        .rolling_std(window_size=168, min_samples=48)
        .over("symbol")
        .alias("prior168_turnover_std"),
        pl.col("turnover_quote")
        .rolling_sum(window_size=24, min_samples=6)
        .over("symbol")
        .alias("turnover_24h"),
    )
    k = k.with_columns(
        (pl.col("close") / pl.col("prior6_close_max") - 1.0).alias("giveback_from_prior6_high"),
        (pl.col("turnover_quote") / pl.col("prior168_turnover_mean")).alias("turnover_spike_168h"),
        pl.when(pl.col("prior168_turnover_std") > 0.0)
        .then((pl.col("turnover_quote") - pl.col("prior168_turnover_mean")) / pl.col("prior168_turnover_std"))
        .otherwise(None)
        .alias("turnover_zscore_168h"),
    )
    return k


def cross_sectional_decile(
    k: pl.DataFrame, rmom: pl.DataFrame, *, rmom_quantile: float = 0.5,
    feature_set: tuple[str, ...] = FEATURES,
) -> pl.DataFrame:
    """Cross-sectional composite -> decile from per-symbol features. `k` must already carry
    [symbol, ts_ms, turnover_quote, rv_168h, vov, dist_low, ret72, ret168]; the backtest passes the
    full multi-ts panel, the live cache assembles a single current-ts frame from cached carry +
    the live price. This is the EXACT tail of the original `compute_continuous_decile_panel`, so
    the live signal that flows through it is provably identical to the verified backtest signal.

    Cross-sectional ops (`xsret*`, `_rr`, `_n_*`, decile) rank WITHIN each `ts_ms` group, so they
    are unaffected by which other timestamps are present — hence computing them here (after the
    backtest's start_ms filter) matches computing them before it (start_ms drops whole timestamps,
    never individual symbols within a surviving timestamp)."""
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    k = k.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    k = k.join(rmom, on=["symbol", "day_ts"], how="left").filter(pl.col("residual_momentum").is_not_null())
    k = k.with_columns(
        (pl.col("ts_ms") + MS_PER_HOUR).alias("signal_bar_close_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("decision_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("feature_ts_ms"),
        pl.col("day_ts").alias("rmom_source_day_ts_ms"),
        pl.col("day_ts").alias("rmom_data_available_ts_ms"),
    )
    k = k.with_columns(
        pl.max_horizontal("feature_ts_ms", "rmom_data_available_ts_ms").alias("data_available_ts_ms")
    )
    # Rank-fraction denominator (len-1) must be clamped to >=1: a ts_ms group that collapses to a
    # single surviving symbol would otherwise divide by 0 -> NaN, and `filter(_rr <= q)` silently
    # drops the lone candidate (NaN <= x is False) instead of ranking it at 0.0. Matches the
    # singleton guard `_continuous_rank_lookup` already uses (max(height-1, 1)). See pit-signals-5.
    _rank_denom = pl.max_horizontal(pl.len().over("ts_ms") - 1, pl.lit(1))
    k = k.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / _rank_denom).alias("_rr")
    ).filter(pl.col("_rr") <= rmom_quantile)
    k = k.with_columns(
        pl.col("_rr").alias("residual_momentum_rank"),
        ((pl.col("turnover_quote").rank().over("ts_ms") - 1) / _rank_denom).alias("liquidity_rank"),
    )
    present = [f for f in feature_set if f in k.columns]
    k = k.with_columns(
        [((pl.col(f).rank().over("ts_ms") - 1) / _rank_denom).alias(f"_n_{f}") for f in present]
    )
    k = k.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    k = k.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )
    cols = [
        "symbol",
        "ts_ms",
        "decile",
        "composite",
        "turnover_quote",
        "signal_bar_close_ts_ms",
        "decision_ts_ms",
        "feature_ts_ms",
        "data_available_ts_ms",
        "rmom_source_day_ts_ms",
        "rmom_data_available_ts_ms",
        "residual_momentum",
        "residual_momentum_rank",
        "liquidity_rank",
    ]
    event_cols = [
        "rv_168h",
        "vov",
        "dist_low",
        "xsret7",
        "xsret3",
        "ret1",
        "max_ret168",
        "prior6_ret1_max",
        "giveback_from_prior6_high",
        "turnover_spike_168h",
        "turnover_24h",
        "turnover_zscore_168h",
    ]
    return k.select(cols + [c for c in event_cols if c in k.columns]).sort(["symbol", "ts_ms"])


def _entry_event_expr(trigger: str) -> pl.Expr:
    if trigger == "none":
        return pl.lit(True)
    if trigger.startswith("fresh_pop"):
        threshold = float(trigger.removeprefix("fresh_pop")) / 100.0
        return (pl.col("ret1") >= threshold) & (pl.col("ret1") >= pl.col("max_ret168") - 1e-12)
    if trigger.startswith("pop") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        pop_min = float(left.removeprefix("pop")) / 100.0
        gb_min = float(right) / 100.0
        return (
            (pl.col("prior6_ret1_max") >= pop_min)
            & (pl.col("ret1") <= 0.0)
            & (pl.col("giveback_from_prior6_high") <= -gb_min)
        )
    if trigger.startswith("turn") and "_pop" in trigger:
        left, right = trigger.split("_pop", 1)
        turn_min = float(left.removeprefix("turn"))
        pop_min = float(right) / 100.0
        return (pl.col("turnover_spike_168h") >= turn_min) & (pl.col("ret1") >= pop_min)
    if trigger.startswith("turn") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        turn_min = float(left.removeprefix("turn"))
        gb_min = float(right) / 100.0
        return (
            (pl.col("turnover_spike_168h") >= turn_min)
            & (pl.col("prior6_ret1_max") >= 0.05)
            & (pl.col("giveback_from_prior6_high") <= -gb_min)
        )
    raise ValueError(f"unknown entry_event_trigger {trigger!r}")


def compute_continuous_decile_panel(
    k: pl.DataFrame, rmom: pl.DataFrame, *, rmom_quantile: float = 0.5, start_ms: int = 0,
    feature_set: tuple[str, ...] = FEATURES,
) -> pl.DataFrame:
    """Shared feature -> composite -> decile pipeline (used by BOTH the backtest panel and the
    live demo state, so the live signal is provably identical to the verified backtest).

    `k`: hourly klines [ts_ms, symbol, close, turnover_quote] (closed bars; the live caller appends
    a synthetic current bar per symbol at `now` with close = live ticker price). `rmom`: the daily
    residual-momentum table [symbol, day_ts, residual_momentum] (day-floored ts). All features are
    trailing closed-bar windows; the rmom join is a causal day-floor lag1. Returns
    [symbol, ts_ms, decile, composite, turnover_quote]. Thin composition of the two halves above."""
    k = per_symbol_timeseries_features(k)
    if start_ms:
        k = k.filter(pl.col("ts_ms") >= start_ms)
    return cross_sectional_decile(k, rmom, rmom_quantile=rmom_quantile, feature_set=feature_set)


def _fresh_entries(panel: pl.DataFrame, config: ContinuousEventConfig) -> pl.DataFrame:
    """Fresh (new D9 spell) entries on the liquid universe, ts-ordered.

    "Fresh" is computed on the FULL target-decile membership timeline (a gap > 1h marks a new
    spell), and the liquid gate is applied AFTER -- matching the proxy. Filtering liquid FIRST
    would let illiquid hours inside one continuous spell open artificial gaps -> spurious fresh
    entries (it inflated the count ~2x in the first cut).

    Live state exits use hysteresis: enter on a fresh D9 catalyst, but keep the planned state
    spell alive while the name remains within the configured hold band (D9/D8 for buffer=1).
    The wider hold-band spell is used ONLY for state-exit timing; it does not create D8 entries.
    """
    if panel.is_empty():
        return panel
    d = panel.filter(pl.col("decile") == config.decile).sort(["symbol", "ts_ms"])
    if config.entry_event_trigger != "none":
        d = d.filter(_entry_event_expr(config.entry_event_trigger))
    d = d.with_columns(
        ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR).fill_null(True).alias("fresh")
    )
    # spell id + the last consecutive in-decile hour of each spell (the STATE-exit timestamp:
    # the name stops being a top-decile fade candidate after spell_end_ts).
    d = d.with_columns(pl.col("fresh").cum_sum().over("symbol").alias("_spell"))
    d = d.with_columns(pl.col("ts_ms").max().over(["symbol", "_spell"]).alias("spell_end_ts"))
    d = d.filter(pl.col("fresh")).filter(pl.col("turnover_quote") >= config.liq_turnover_min)
    if config.exit_mode == "state" and config.exit_decile_buffer > 0 and not d.is_empty():
        hold_min_decile = config.decile - max(0, int(config.exit_decile_buffer))
        hold = panel.filter(pl.col("decile") >= hold_min_decile).sort(["symbol", "ts_ms"])
        hold = hold.with_columns(
            ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR)
            .fill_null(True)
            .alias("_hold_fresh")
        )
        hold = hold.with_columns(pl.col("_hold_fresh").cum_sum().over("symbol").alias("_hold_spell"))
        hold = hold.with_columns(
            pl.col("ts_ms").max().over(["symbol", "_hold_spell"]).alias("hold_band_spell_end_ts")
        )
        d = (
            d.drop("spell_end_ts")
            .join(hold.select("symbol", "ts_ms", "hold_band_spell_end_ts"), on=["symbol", "ts_ms"], how="left")
            .rename({"hold_band_spell_end_ts": "spell_end_ts"})
        )
    if config.entry_max_ret168_max < 10.0:
        if "max_ret168" not in d.columns:
            raise ValueError("entry_max_ret168_max requires max_ret168 in the continuous panel")
        d = d.filter(pl.col("max_ret168") <= config.entry_max_ret168_max)
    keep_cols = [
        "symbol",
        "ts_ms",
        "composite",
        "turnover_quote",
        "spell_end_ts",
        "signal_bar_close_ts_ms",
        "decision_ts_ms",
        "feature_ts_ms",
        "data_available_ts_ms",
        "rmom_source_day_ts_ms",
        "rmom_data_available_ts_ms",
        "residual_momentum",
        "residual_momentum_rank",
        "liquidity_rank",
        "rv_168h",
        "vov",
        "dist_low",
        "xsret7",
        "xsret3",
        "ret1",
        "max_ret168",
        "prior6_ret1_max",
        "giveback_from_prior6_high",
        "turnover_spike_168h",
        "turnover_24h",
        "turnover_zscore_168h",
    ]
    return d.select([c for c in keep_cols if c in d.columns]).sort(["ts_ms", "symbol"])


def _symbol_priority_hash(symbol: str) -> int:
    """Deterministic, market-content-free symbol hash for negative-control entry priority."""
    return int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16) % 1000


def _apply_entry_order(entries: pl.DataFrame, entry_order: str) -> pl.DataFrame:
    """Re-order candidates WITHIN each ``signal_ts`` by an entry-priority score,
    leaving every gate / capacity / cooldown / sizing untouched. Reordering is causal (only same-ts
    candidates ever swap; a later ts can never jump ahead of an earlier one). ``fcfs`` (default)
    reproduces the frozen control's ``(ts_ms, symbol)`` order exactly.

    - ``fcfs``: control order (symbol-alphabetical within ts).
    - ``composite``: highest production composite first within ts (symbol tiebreak).
    - ``symbol_hash``: ascending market-content-free symbol hash (negative control)."""
    if entries.is_empty() or entry_order == "fcfs":
        return entries
    if entry_order == "composite":
        return entries.sort(
            ["ts_ms", "composite", "symbol"], descending=[False, True, False], nulls_last=True
        )
    if entry_order == "symbol_hash":
        syms = entries["symbol"].unique().to_list()
        hmap = pl.DataFrame(
            {"symbol": syms, "_pri_hash": [_symbol_priority_hash(str(s)) for s in syms]}
        )
        return (
            entries.join(hmap, on="symbol", how="left")
            .sort(["ts_ms", "_pri_hash", "symbol"])
            .drop("_pri_hash")
        )
    raise ValueError(f"unknown entry_order {entry_order!r}")


def _continuous_rank_lookup(panel: pl.DataFrame, *, delay_ms: int) -> dict[tuple[str, int], float]:
    """Composite rank by symbol and actionable bar-end timestamp for rank-decay exits."""
    output: dict[tuple[str, int], float] = {}
    if panel.is_empty() or "composite" not in panel.columns:
        return output
    for part in panel.select("symbol", "ts_ms", "composite").drop_nulls().sort(["ts_ms", "symbol"]).partition_by(
        "ts_ms", maintain_order=True
    ):
        values = part.filter(pl.col("composite").is_finite()).sort("composite")
        if values.height < 2:
            continue
        check_ts = int(values["ts_ms"][0]) + delay_ms
        denom = max(values.height - 1, 1)
        for rank, row in enumerate(values.to_dicts()):
            output[(str(row["symbol"]), check_ts)] = rank / denom
    return output


def _round_trip_bps(
    config: ContinuousEventConfig, turnover_quote: float, *, notional_weight: float | None = None
) -> float:
    """Round-trip cost (bps): 2*taker + 2*spread + 2*impact; impact is size/ADV-aware.

    participation = position_notional / signal-bar hourly turnover (the ADV proxy).
    A flat_round_trip_bps override bypasses the model for proxy-parity validation.
    """
    cost_multiplier = max(float(config.round_trip_cost_multiplier), 0.0)
    if config.flat_round_trip_bps is not None:
        return float(config.flat_round_trip_bps) * cost_multiplier
    base = 2.0 * (config.taker_fee_bps + config.spread_bps)
    weight = config.notional_weight if notional_weight is None else float(notional_weight)
    notional = max(weight, 0.0) * config.deploy_capital_usd
    adv = max(float(turnover_quote), 1.0)
    participation = notional / adv
    impact = config.impact_coef_bps * (participation ** config.impact_exponent)
    return (base + 2.0 * impact) * cost_multiplier


def _market_daily_returns(klines: pl.DataFrame) -> dict[int, float]:
    """Equal-weight cross-sectional mean daily return per day-floored ts (the alt-market regime gate).

    The value for day D is D's FULL-day close-to-close return (known only at D's
    final bar). The entry gate consumes it with a one-day causal lag (reads day
    D-1 for an entry on day D), so an intraday entry never reads its own day's
    not-yet-realised return.
    """
    # audit2c: gap-aware 1-day return. A plain shift(1).over("symbol") pairs a symbol's
    # Nth PRESENT day, so across a delist/relist or archive gap it mislabels a multi-day
    # close-to-close move as that day's 1-day return, biasing the equal-weight market
    # regime gate. calendar_shift nulls the return on a post-gap day (it is excluded from
    # the cross-sectional mean) and is byte-identical for a contiguous daily series.
    dc = (
        klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c")).sort(["symbol", "day"])
        .with_columns((pl.col("c") / calendar_shift(pl.col("c"), 1, time_col="day") - 1.0).alias("r"))
        .group_by("day").agg(pl.col("r").mean().alias("mkt")).drop_nulls()
    )
    return {int(r[0]): float(r[1]) for r in dc.iter_rows()}


def _btc_trend_returns(klines: pl.DataFrame, *, lookback_days: int = 30) -> dict[int, float]:
    """Prior-N-day BTC return-sum by day, excluding the current signal day.

    Mirrors ``btc_return_30d`` in the daily event features: the current day is not
    included, so the regime value is known before any same-day continuous signal.
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1; got {lookback_days}")
    if klines.is_empty() or "symbol" not in klines.columns:
        return {}
    btc = (
        klines.filter(pl.col("symbol") == "BTCUSDT")
        .sort("ts_ms")
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by("day")
        .agg(pl.col("close").last().alias("close"))
        .sort("day")
        .with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"))
        .drop_nulls("ret")
    )
    out: dict[int, float] = {}
    days: list[int] = []
    rets: list[float] = []
    lo = 0
    for day, ret in btc.select("day", "ret").iter_rows():
        day_i = int(day)
        while lo < len(days) and days[lo] < day_i - lookback_days * MS_PER_DAY:
            lo += 1
        window = rets[lo:]
        if len(window) >= lookback_days:
            out[day_i] = float(sum(window))
        days.append(day_i)
        rets.append(float(ret))
    return out


def _btc_hourly_month_returns(klines: pl.DataFrame, *, lookback_ms: int) -> dict[int, float]:
    """BTC return keyed by hourly signal bar, using only that confirmed bar close.

    The key is the BTC kline ``ts_ms`` (the hour's bar start). The close is known at
    ``ts_ms + 1h``, which is still before the existing continuous default entry
    submit time. The source close is the latest BTC bar whose timestamp is <= the
    exact cutoff. If the source is more than one hour older than the cutoff, the
    value is treated as unavailable instead of smearing over an archive gap.
    """
    if lookback_ms <= 0:
        raise ValueError(f"lookback_ms must be positive; got {lookback_ms}")
    if klines.is_empty() or "symbol" not in klines.columns:
        return {}
    btc = (
        klines.filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .sort("ts_ms")
        .drop_nulls(["ts_ms", "close"])
    )
    if btc.is_empty():
        return {}
    ts = [int(v) for v in btc["ts_ms"].to_list()]
    closes = [float(v) for v in btc["close"].to_list()]
    out: dict[int, float] = {}
    for idx, anchor_ts in enumerate(ts):
        source_cutoff = int(anchor_ts) - int(lookback_ms)
        source_idx = bisect.bisect_right(ts, source_cutoff) - 1
        if source_idx < 0:
            continue
        source_ts = int(ts[source_idx])
        if source_ts < source_cutoff - MS_PER_HOUR:
            continue
        source_close = float(closes[source_idx])
        anchor_close = float(closes[idx])
        if source_close > 0.0 and np.isfinite(source_close) and np.isfinite(anchor_close):
            out[int(anchor_ts)] = anchor_close / source_close - 1.0
    return out


def _btc_smart_month_value(hourly_month_return: float, daily_prior_return: float, *, tolerance: float) -> float:
    """Signed consensus score for the research smart-month BTC gate.

    Positive means an uptrend gate passes. It tolerates a small disagreement
    between the faster hourly exact-month return and the slower daily prior-N-day
    return, but requires at least one leg to be positive. This is deliberately
    low-capacity; it is a robustness hypothesis, not a classifier.
    """
    tol = max(float(tolerance), 0.0)
    h = float(hourly_month_return)
    d = float(daily_prior_return)
    return max(min(h, d + tol), min(d, h + tol))


def _btc_trend_return_lookup(
    klines: pl.DataFrame,
    *,
    mode: str,
    lookback_days: int,
    month_days: float = BTC_EXACT_MONTH_DAYS,
    smart_tolerance: float = 0.01,
) -> dict[int, float]:
    if mode == BTC_TREND_MODE_DAILY_PRIOR:
        return _btc_trend_returns(klines, lookback_days=lookback_days)
    if mode == BTC_TREND_MODE_HOURLY_30D:
        return _btc_hourly_month_returns(klines, lookback_ms=exact_duration_ms(days=lookback_days))
    if mode == BTC_TREND_MODE_HOURLY_EXACT_MONTH:
        return _btc_hourly_month_returns(klines, lookback_ms=exact_duration_ms(days=month_days))
    if mode == BTC_TREND_MODE_SMART_MONTH:
        hourly = _btc_hourly_month_returns(klines, lookback_ms=exact_duration_ms(days=month_days))
        daily = _btc_trend_returns(klines, lookback_days=lookback_days)
        out: dict[int, float] = {}
        for signal_ts, hourly_value in hourly.items():
            day = (int(signal_ts) // MS_PER_DAY) * MS_PER_DAY
            daily_value = daily.get(day)
            if daily_value is None:
                continue
            out[int(signal_ts)] = _btc_smart_month_value(
                float(hourly_value),
                float(daily_value),
                tolerance=smart_tolerance,
            )
        return out
    raise ValueError(f"btc_trend_mode must be one of {BTC_TREND_MODES}; got {mode!r}")


def _btc_trend_lookup_key(signal_ts_ms: int, *, mode: str) -> int:
    if mode == BTC_TREND_MODE_DAILY_PRIOR:
        return (int(signal_ts_ms) // MS_PER_DAY) * MS_PER_DAY
    return int(signal_ts_ms)


def _btc_trend_lookback_duration_ms(config: ContinuousEventConfig) -> int:
    if config.btc_trend_mode == BTC_TREND_MODE_DAILY_PRIOR:
        return int(config.btc_trend_lookback_days) * MS_PER_DAY
    if config.btc_trend_mode == BTC_TREND_MODE_HOURLY_30D:
        return exact_duration_ms(days=int(config.btc_trend_lookback_days))
    if config.btc_trend_mode in (BTC_TREND_MODE_HOURLY_EXACT_MONTH, BTC_TREND_MODE_SMART_MONTH):
        return exact_duration_ms(days=float(config.btc_trend_month_days))
    raise ValueError(f"btc_trend_mode must be one of {BTC_TREND_MODES}; got {config.btc_trend_mode!r}")


def _btc_trend_metadata(signal_ts_ms: int, config: ContinuousEventConfig) -> dict[str, int | str | None]:
    mode = config.btc_trend_mode
    if mode == BTC_TREND_MODE_DAILY_PRIOR:
        signal_day = (int(signal_ts_ms) // MS_PER_DAY) * MS_PER_DAY
        return {
            "btc_trend_mode": mode,
            "btc_trend_lookback_duration_ms": _btc_trend_lookback_duration_ms(config),
            "btc_trend_source_start_ts_ms": signal_day - int(config.btc_trend_lookback_days) * MS_PER_DAY,
            "btc_trend_source_end_ts_ms": signal_day - MS_PER_DAY,
            "btc_trend_data_available_ts_ms": signal_day,
        }
    duration = _btc_trend_lookback_duration_ms(config)
    return {
        "btc_trend_mode": mode,
        "btc_trend_lookback_duration_ms": duration,
        "btc_trend_source_start_ts_ms": int(signal_ts_ms) - duration,
        "btc_trend_source_end_ts_ms": int(signal_ts_ms),
        "btc_trend_data_available_ts_ms": int(signal_ts_ms) + MS_PER_HOUR,
    }


def _entry_vol(close_arr: "np.ndarray", entry_bar: int, window: int = 168, min_n: int = 48) -> float:
    """Trailing hourly return std ending at entry_bar (the per-name vol for risk-sizing)."""
    lo = max(0, entry_bar - window)
    seg = close_arr[lo:entry_bar + 1]
    if seg.size < min_n + 1:
        return 0.0
    rets = np.diff(seg) / seg[:-1]
    rets = rets[np.isfinite(rets)]
    return float(np.std(rets)) if rets.size >= min_n else 0.0


def _assert_funding_one_per_settlement(
    funding: pl.DataFrame,
    *,
    root: Path,
    interval_by_symbol: dict[str, int] | None = None,
) -> None:
    """Refuse to silently OVER-charge funding from sub-interval SNAPSHOT rows.

    The engine charges funding per distinct settlement (``_funding_lookup``). That is
    correct when the parquet holds one row per (symbol, settlement) — true for the
    venues' funding-history endpoints. A root that ingested sub-interval SNAPSHOT
    rows (e.g. an hourly ticker scrape of an 8h-settling symbol) would, under naive
    exact-stamp dedup, treat each snapshot as a settlement and OVER-charge ~Nx,
    flattering a short book (cost-funding-5).

    Detection is DATA-INTRINSIC and does NOT trust the stored ``funding_interval_min``
    (a stale 8h venue default that false-positived genuine 1h/2h-settling alts — the
    2026-06-15 regression). A symbol is over-sampled only when its funding RATE stays
    constant across several finer-spaced stamps, i.e. the rate changes on a clean,
    strictly coarser multiple of the stamp cadence (``change_gap >= 2*stamp_gap``).
    Genuine sub-8h settlements (rate changes every stamp) are not flagged. See
    :func:`liquidity_migration.trade_lifecycle.funding_cadence_stats`.

    Over-sampling is no longer fatal by itself: ``_funding_lookup`` collapses it to
    one charge per true settlement when given ``interval_by_symbol`` (from
    :func:`derive_funding_interval_min`). This guard raises ONLY if an over-sampled
    symbol is not covered by a collapsing interval — i.e. it would actually be
    mis-charged."""
    stats = funding_cadence_stats(funding)
    if stats.is_empty():
        return
    intervals = interval_by_symbol or {}
    uncorrected: list[tuple[str, int, int]] = []
    for r in stats.iter_rows(named=True):
        stamp_gap = int(r["stamp_gap"])
        collapse = _collapse_interval_min(stamp_gap, r["change_gap"], int(r["n_changes"]))
        # Over-sampled symbols are fine IFF the applied interval collapses them to one
        # charge per true settlement; flag only those left uncorrected (would over-charge).
        if collapse is not None and intervals.get(str(r["symbol"]), 0) < collapse:
            uncorrected.append((str(r["symbol"]), stamp_gap, collapse))
    if not uncorrected:
        return
    uncorrected.sort()
    examples = ", ".join(f"{s} (stamps {sg}min vs settlements {cg}min)" for s, sg, cg in uncorrected[:3])
    raise RuntimeError(
        f"funding dataset under {root} has sub-interval SNAPSHOT rows NOT corrected by an authoritative "
        f"interval: {len(uncorrected)} symbol(s) sample finer than their true settlement cadence "
        f"(e.g. {examples}). Exact-stamp dedup would OVER-charge funding (~Nx) and flatter a short book. "
        f"Pass interval_by_symbol=derive_funding_interval_min(funding) to _funding_lookup, or rebuild "
        f"funding from the funding-history endpoint."
    )


def _build_lifecycle_config(config: ContinuousEventConfig) -> TradeLifecycleConfig:
    """Translate the continuous-event config into the shared lifecycle config.

    The optional exit ladder is off unless the config sets it.
    Pure; lifted verbatim out of `_run_trades` so the setup reads in one place."""
    return TradeLifecycleConfig(
        start_date=config.start_date, end_date=config.end_date,
        hold_days=max(1, round(config.hold_hours / 24)), take_profit_pct=max(config.take_profit_pct, 0.0),
        failed_fade_exit_hours=config.failed_fade_hours,
        failed_fade_loss_pct=config.failed_fade_loss_pct,
        failed_fade_min_mfe_pct=config.failed_fade_min_mfe_pct,
        failed_fade_close_location_min=0.0 if config.failed_fade_hours > 0 else 1.0,
        breakeven_arm_pct=config.breakeven_arm_pct,
        mfe_giveback_trigger_pct=config.mfe_giveback_trigger_pct,
        mfe_giveback_retain_pct=config.mfe_giveback_retain_pct,
        side_mode="long_low_short_high",
        rank_exit_enabled=config.rank_exit_threshold > 0.0,
        rank_exit_threshold=config.rank_exit_threshold,
        hash_exit_prob=config.hash_exit_prob,
    )


def _compute_size_and_stop(
    config: ContinuousEventConfig,
    close_arr: Any,
    entry_bar: int,
    *,
    base_nw: float,
    inverse_vol: bool,
    clamp: float,
    regime_size_mult: float,
    stop_pct: float | None,
) -> tuple[float, float | None]:
    """Per-trade notional weight (flat or inverse-vol, then x regime size mult) and the effective
    stop (k * trailing hourly vol clamped to [0.05, 0.50] when stop_vol_mult>0, else the fixed
    stop). Pure; arithmetic expressions preserved verbatim so float ordering is identical to the
    prior inline form."""
    nw = base_nw
    if inverse_vol:
        rv = _entry_vol(close_arr, int(entry_bar))
        mult = min(max(config.target_vol_per_name / rv, 1.0 / clamp), clamp) if rv > 0 else 1.0
        nw = base_nw * mult
    nw *= regime_size_mult
    trade_stop = stop_pct
    if config.stop_vol_mult > 0.0:
        sv = _entry_vol(close_arr, int(entry_bar))
        trade_stop = min(max(config.stop_vol_mult * sv, 0.05), 0.50) if sv > 0 else stop_pct
    return nw, trade_stop


def _plan_exit(
    *,
    state_mode: bool,
    spell_end: int,
    entry_bar_end: int,
    delay_ms: int,
    max_hold_ms: int,
    hold_ms: int,
) -> int:
    """Planned exit ts. State exit: cover when the name has left D9. spell_end is the last in-decile
    hour (bar START), so spell_end + delay_ms (= +2h at entry_delay=1) is the close of the FIRST bar
    the name is out of D9 — the same close at which "left-decile" becomes known. This close-on-
    detection fill is INTENTIONAL and causal (deciding at a bar's close and filling at that close is
    the convention every early-exit path in _simulate_indexed_trade uses for stop/TP/rank). It is
    mildly asymmetric with the +1h entry gap, but the live book exits FASTER (tick-driven
    left_decile / protective covers), so the backtest exit is not optimistic relative to live
    (pit-engine-4). Clamped to >= entry+1h so a same-bar spell still holds at least one bar, and
    capped at max_hold. Fixed-timer mode just holds hold_ms."""
    if state_mode:
        planned_exit = min(int(spell_end) + delay_ms, entry_bar_end + max_hold_ms)
        return max(planned_exit, entry_bar_end + MS_PER_HOUR)
    return entry_bar_end + hold_ms


def _resolve_entry_fill(
    config: ContinuousEventConfig,
    bars: dict[str, Any],
    *,
    side: str,
    order_submit_bar: int,
    order_submit_ts_ms: int,
) -> dict[str, Any] | None:
    """Resolve the executable entry bar/price for the configured entry style.

    Default continuous execution fills at the order-submit bar close. The
    research-only adverse-limit mode submits after that close and therefore
    scans only later bars; this avoids using an earlier same-bar high/low that a
    live order could not have interacted with.
    """
    close_arr = bars["close"]
    ref_price = float(close_arr[order_submit_bar])
    if ref_price <= 0.0:
        return None
    adverse_pct = float(config.entry_adverse_limit_pct or 0.0)
    if adverse_pct <= 0.0:
        return {
            "entry_bar": int(order_submit_bar),
            "entry_bar_end_ts_ms": int(order_submit_ts_ms),
            "entry_price_override": None,
            "entry_reference_price": ref_price,
            "entry_limit_price": None,
            "entry_fill_mode": "bar_close",
            "fill_window_start_ts_ms": int(order_submit_ts_ms),
            "fill_window_end_ts_ms": int(order_submit_ts_ms),
        }
    wait_ms = exact_duration_ms(hours=max(config.entry_adverse_limit_wait_hours, 0))
    if wait_ms <= 0:
        return None
    limit_price = ref_price * (1.0 + adverse_pct) if side == "short" else ref_price * (1.0 - adverse_pct)
    bar_end_ts_arr = bars["bar_end_ts_ms"]
    high_arr = bars["high"]
    low_arr = bars["low"]
    start_idx = bisect.bisect_right(bar_end_ts_arr, int(order_submit_ts_ms))
    end_idx = bisect.bisect_right(bar_end_ts_arr, int(order_submit_ts_ms) + wait_ms)
    for idx in range(start_idx, end_idx):
        if side == "short":
            touched = float(high_arr[idx]) >= limit_price
        else:
            touched = float(low_arr[idx]) <= limit_price
        if touched:
            fill_ts = int(bar_end_ts_arr[idx])
            return {
                "entry_bar": int(idx),
                "entry_bar_end_ts_ms": fill_ts,
                "entry_price_override": float(limit_price),
                "entry_reference_price": ref_price,
                "entry_limit_price": float(limit_price),
                "entry_fill_mode": "adverse_limit",
                "fill_window_start_ts_ms": int(order_submit_ts_ms),
                "fill_window_end_ts_ms": fill_ts,
            }
    return None


def _run_trades(
    entries: pl.DataFrame,
    symbol_bars: dict[str, Any],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: ContinuousEventConfig,
    market_daily: dict[int, float] | None = None,
    btc_trend_daily: dict[int, float] | None = None,
    rank_lookup: dict[tuple[str, int], float] | None = None,
    candidate_sink: list[dict[str, Any]] | None = None,
    size_mult_lookup: dict[tuple[str, int], float] | None = None,
    admission_lookup: dict[tuple[str, int], bool] | None = None,
    listing_ts_by_symbol: dict[str, int] | None = None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Walk fresh entries in ts order; apply concurrency + cooldown + the inherited selection gates
    (age / fade-deceleration / market-context), size by the chosen rule, and simulate each via the
    shared `_simulate_indexed_trade` path (identical fills/funding/exit semantics).

    `candidate_sink` (default None) is the candidate-tape audit hook: when a list is supplied, every
    candidate fed into this loop appends one decision row (selected OR the exact rejection reason,
    in engine order) so the FULL eligible candidate set — not just executed trades — is recoverable
    from the same code that makes the live decision. When it is None the loop is byte-identical to
    the pre-hook engine (no work, no output change), so existing callers are unaffected.

    Returns (trades, skip-counts)."""
    if entries.is_empty():
        return _empty_trades(), {}
    # Optional exit ladder (off unless the config sets it).
    lifecycle = _build_lifecycle_config(config)
    base_nw = config.notional_weight
    inverse_vol = config.sizing_mode == "inverse_vol"
    clamp = max(config.vol_weight_clamp, 1.0)
    cooldown_ms = exact_duration_ms(hours=config.effective_cooldown_hours)
    hold_ms = exact_duration_ms(hours=config.hold_hours)
    max_hold_ms = exact_duration_ms(hours=config.max_hold_hours)
    delay_ms = exact_duration_ms(hours=1 + config.entry_delay_hours)
    decel_h = config.entry_decel_lookback_h
    age_min_ms = exact_duration_ms(days=config.age_days_min)
    state_mode = config.exit_mode == "state"
    stop_pct = config.stop_loss_pct if config.stop_loss_pct > 0.0 else None
    pause_window_ms = exact_duration_ms(hours=config.entry_pause_window_hours)
    breaker_on = config.entry_pause_after_adverse_exits > 0 and pause_window_ms > 0
    crowding_on = config.entry_crowding_max_fresh > 0
    external_size_skip_lte = float(config.entry_skip_external_size_multiplier_lte)
    if external_size_skip_lte < 0.0:
        raise ValueError(
            "entry_skip_external_size_multiplier_lte must be >= 0; "
            f"got {external_size_skip_lte}"
        )
    external_size_skip_on = external_size_skip_lte > 0.0 and size_mult_lookup is not None
    disaster_budget = float(config.entry_disaster_loss_budget_frac)
    disaster_shock = float(config.entry_disaster_shock_frac)
    portfolio_heat_cap = float(config.entry_portfolio_heat_cap_frac)
    if disaster_budget < 0.0 or portfolio_heat_cap < 0.0 or disaster_shock < 0.0:
        raise ValueError("disaster budget, portfolio heat cap, and shock fraction must be non-negative")
    if (disaster_budget > 0.0 or portfolio_heat_cap > 0.0) and disaster_shock <= 0.0:
        raise ValueError("entry_disaster_shock_frac must be positive when a risk budget is enabled")
    disaster_budget_on = disaster_budget > 0.0
    portfolio_heat_on = portfolio_heat_cap > 0.0
    signal_counts: dict[int, int] = {}
    if crowding_on:
        for ts in entries["ts_ms"].to_list():
            ts_i = int(ts)
            signal_counts[ts_i] = signal_counts.get(ts_i, 0) + 1
    adverse_exit_ts: list[int] = []   # ASCENDING net-negative exit timestamps (circuit-breaker, causal)
    active: list[int] = []          # min-heap of actual exit timestamps of open positions
    active_notional: list[tuple[int, float]] = []  # min-heap of (exit_ts, abs non-hedge notional)
    last_entry: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    btc_gate = config.btc_trend_gate
    if btc_gate not in ("off", "uptrend", "downtrend"):
        raise ValueError(
            f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {btc_gate!r}"
        )
    btc_mode = str(config.btc_trend_mode)
    if btc_mode not in BTC_TREND_MODES:
        raise ValueError(f"btc_trend_mode must be one of {BTC_TREND_MODES}; got {btc_mode!r}")
    btc_lookback_days = int(config.btc_trend_lookback_days)
    if btc_gate != "off" and btc_lookback_days < 1:
        raise ValueError(f"btc_trend_lookback_days must be >= 1; got {btc_lookback_days}")
    btc_lookback_duration_ms = _btc_trend_lookback_duration_ms(config) if btc_gate != "off" else None
    skipped_capacity = skipped_cooldown = skipped_no_bar = skipped_gate = skipped_breaker = skipped_btc_trend = 0
    skipped_admission = 0
    skipped_crowding = 0
    skipped_external_size_multiplier = 0
    skipped_entry_limit_unfilled = 0
    skipped_portfolio_heat = 0
    syms = entries["symbol"].to_list()
    tss = entries["ts_ms"].to_list()
    comps = entries["composite"].to_list()
    turns = entries["turnover_quote"].to_list()
    spell_ends = entries["spell_end_ts"].to_list() if "spell_end_ts" in entries.columns else tss
    entry_meta_rows = entries.to_dicts()
    stop_approach_on = (
        stop_pct is not None
        and config.stop_approach_frac > 0.0
        and config.stop_loss_pct > 0.0
        and config.stop_vol_mult <= 0.0
    )
    stop_approach_pct = (
        min(float(config.stop_loss_pct), float(config.stop_loss_pct) * float(config.stop_approach_frac))
        if stop_approach_on else None
    )
    def _emit(
        reason: str,
        *,
        entry_bar_end: int | None = None,
        active_count: int | None = None,
        regime_trend: float | None = None,
        regime_size_mult: float | None = None,
        notional_weight: float | None = None,
        entry_volatility: float | None = None,
        inverse_vol_multiplier: float | None = None,
        external_size_multiplier: float | None = None,
        pre_risk_notional_weight: float | None = None,
        disaster_notional_cap: float | None = None,
        portfolio_heat_before_frac: float | None = None,
        portfolio_heat_after_frac: float | None = None,
        risk_size_clamped: bool | None = None,
        exit_ts_ms: int | None = None,
        order_submit_ts_ms: int | None = None,
        fill_window_start_ts_ms: int | None = None,
        fill_window_end_ts_ms: int | None = None,
        entry_reference_price: float | None = None,
        entry_limit_price: float | None = None,
        entry_fill_mode: str | None = None,
    ) -> None:
        if candidate_sink is None:
            return
        btc_meta = _btc_trend_metadata(int(sig_ts), config) if btc_gate != "off" else {}

        def _meta_int(name: str) -> int | None:
            value = cand_meta.get(name)
            return int(value) if value is not None else None

        def _meta_float(name: str) -> float | None:
            value = cand_meta.get(name)
            return float(value) if value is not None else None

        order_ts = order_submit_ts_ms if order_submit_ts_ms is not None else entry_bar_end
        fill_start_ts = (
            fill_window_start_ts_ms
            if fill_window_start_ts_ms is not None
            else entry_bar_end if entry_bar_end is not None else order_ts
        )
        fill_end_ts = (
            fill_window_end_ts_ms
            if fill_window_end_ts_ms is not None
            else entry_bar_end if entry_bar_end is not None else order_ts
        )
        candidate_sink.append(
            {
                "symbol": sym,
                "signal_ts_ms": int(sig_ts),
                "signal_bar_close_ts_ms": _meta_int("signal_bar_close_ts_ms"),
                "decision_ts_ms": _meta_int("decision_ts_ms"),
                "feature_ts_ms": _meta_int("feature_ts_ms"),
                "data_available_ts_ms": _meta_int("data_available_ts_ms"),
                "rmom_source_day_ts_ms": _meta_int("rmom_source_day_ts_ms"),
                "rmom_data_available_ts_ms": _meta_int("rmom_data_available_ts_ms"),
                "btc_trend_lookback_days": btc_lookback_days if btc_gate != "off" else None,
                "btc_trend_mode": btc_meta.get("btc_trend_mode") if btc_gate != "off" else None,
                "btc_trend_lookback_duration_ms": btc_lookback_duration_ms,
                "btc_trend_source_start_ts_ms": btc_meta.get("btc_trend_source_start_ts_ms"),
                "btc_trend_source_end_ts_ms": btc_meta.get("btc_trend_source_end_ts_ms"),
                "btc_trend_data_available_ts_ms": btc_meta.get("btc_trend_data_available_ts_ms"),
                "order_submit_ts_ms": int(order_ts) if order_ts is not None else None,
                "fill_window_start_ts_ms": int(fill_start_ts) if fill_start_ts is not None else None,
                "fill_window_end_ts_ms": int(fill_end_ts) if fill_end_ts is not None else None,
                "composite": float(comp) if comp is not None else None,
                "residual_momentum_value": _meta_float("residual_momentum"),
                "residual_momentum_rank": _meta_float("residual_momentum_rank"),
                "feature_rv_168h": _meta_float("rv_168h"),
                "feature_vov": _meta_float("vov"),
                "feature_dist_low": _meta_float("dist_low"),
                "feature_xsret7": _meta_float("xsret7"),
                "feature_xsret3": _meta_float("xsret3"),
                "feature_ret1": _meta_float("ret1"),
                "feature_max_ret168": _meta_float("max_ret168"),
                "feature_prior6_ret1_max": _meta_float("prior6_ret1_max"),
                "feature_giveback_from_prior6_high": _meta_float("giveback_from_prior6_high"),
                "feature_turnover_spike_168h": _meta_float("turnover_spike_168h"),
                "turnover_quote": float(turn) if turn is not None else None,
                "liquidity_value": float(turn) if turn is not None else None,
                "liquidity_rank": _meta_float("liquidity_rank"),
                "volume_1h_quote": float(turn) if turn is not None else None,
                "volume_24h_quote": _meta_float("turnover_24h"),
                "volume_zscore": _meta_float("turnover_zscore_168h"),
                "spell_end_ts_ms": int(spell_end) if spell_end is not None else None,
                "entry_bar_end_ts_ms": int(entry_bar_end) if entry_bar_end is not None else None,
                "entry_reference_price": entry_reference_price,
                "entry_limit_price": entry_limit_price,
                "entry_fill_mode": entry_fill_mode,
                "crowding_count": int(signal_counts.get(int(sig_ts), 0)) if crowding_on else None,
                "active_count": active_count,
                "btc_trend_gate": btc_gate,
                "regime_trend": regime_trend,
                "regime_size_mult": regime_size_mult,
                "sizing_mode": config.sizing_mode,
                "base_notional_weight": base_nw,
                "entry_volatility": entry_volatility,
                "inverse_vol_multiplier": inverse_vol_multiplier,
                "external_size_multiplier": external_size_multiplier,
                "pre_risk_notional_weight": pre_risk_notional_weight,
                "disaster_notional_cap": disaster_notional_cap,
                "portfolio_heat_before_frac": portfolio_heat_before_frac,
                "portfolio_heat_after_frac": portfolio_heat_after_frac,
                "risk_size_clamped": risk_size_clamped,
                "notional_weight": notional_weight,
                "exit_ts_ms": exit_ts_ms,
                "selected": reason == "selected",
                "reason": reason,
            }
        )

    for idx, (sym, sig_ts, comp, turn, spell_end) in enumerate(zip(syms, tss, comps, turns, spell_ends)):
        cand_meta = entry_meta_rows[idx]
        bars = symbol_bars.get(sym)
        if bars is None:
            _emit("no_bar_symbol")
            skipped_no_bar += 1
            continue
        order_submit_ts = int(sig_ts) + delay_ms
        cand_trend: float | None = None
        while active and active[0] <= order_submit_ts:
            heapq.heappop(active)
        while active_notional and active_notional[0][0] <= order_submit_ts:
            heapq.heappop(active_notional)
        if crowding_on and signal_counts.get(int(sig_ts), 0) > config.entry_crowding_max_fresh:
            _emit("crowding", order_submit_ts_ms=order_submit_ts, active_count=len(active))
            skipped_crowding += 1
            continue
        # Circuit breaker: pause this entry if too many net-negative covers have CLOSED in the trailing
        # window (a correlated alt-squeeze). Causal — adverse_exit_ts holds only exits already simulated,
        # and the [entry_bar_end-window, entry_bar_end) slice counts only those that closed before now.
        if breaker_on:
            lo = bisect.bisect_left(adverse_exit_ts, order_submit_ts - pause_window_ms)
            hi = bisect.bisect_left(adverse_exit_ts, order_submit_ts)
            if hi - lo >= config.entry_pause_after_adverse_exits:
                _emit("breaker", order_submit_ts_ms=order_submit_ts, active_count=len(active))
                skipped_breaker += 1
                continue
        if sym in last_entry and order_submit_ts - last_entry[sym] < cooldown_ms:
            _emit("cooldown", order_submit_ts_ms=order_submit_ts, active_count=len(active))
            skipped_cooldown += 1
            continue
        if len(active) >= config.max_active:
            _emit("capacity", order_submit_ts_ms=order_submit_ts, active_count=len(active))
            skipped_capacity += 1
            continue
        order_submit_bar = bars["by_end"].get(order_submit_ts)
        if order_submit_bar is None:
            _emit("no_bar_entry", order_submit_ts_ms=order_submit_ts, active_count=len(active))
            skipped_no_bar += 1
            continue
        close_arr = bars["close"]
        # --- inherited selection gates (squeeze-defense) ---
        if age_min_ms > 0:
            # Age against the symbol's AUTHORITATIVE PIT listing (first-ever bar under the root),
            # NOT the first bar of the padded read window. The window's first bar is clamped to
            # start_ms - pad_back, so a symbol genuinely listed long before the run would otherwise
            # appear exactly pad_back old at the window start and be wrongly age-gated near every
            # backtest edge — diverging from the live demo, which ages off universe.listing_age_days
            # (Bybit launchTime). Falls back to the loaded first bar only when no listing is known.
            listing_ts = (listing_ts_by_symbol or {}).get(sym)
            if listing_ts is None:
                listing_ts = int(bars["bar_end_ts_ms"][0])
            if (order_submit_ts - int(listing_ts)) < age_min_ms:
                _emit("age", order_submit_ts_ms=order_submit_ts, active_count=len(active))
                skipped_gate += 1
                continue
        regime_size_mult = 1.0
        if btc_gate != "off":
            trend = (btc_trend_daily or {}).get(_btc_trend_lookup_key(int(sig_ts), mode=btc_mode))
            if trend is None:
                _emit("btc_trend_unknown", order_submit_ts_ms=order_submit_ts, active_count=len(active))
                skipped_btc_trend += 1
                continue
            cand_trend = float(trend)
            if btc_gate == "uptrend" and trend <= 0.0:
                _emit("btc_trend", order_submit_ts_ms=order_submit_ts, active_count=len(active), regime_trend=cand_trend)
                skipped_btc_trend += 1
                continue
            if btc_gate == "downtrend" and trend > 0.0:
                _emit("btc_trend", order_submit_ts_ms=order_submit_ts, active_count=len(active), regime_trend=cand_trend)
                skipped_btc_trend += 1
                continue
        if decel_h > 0 and order_submit_bar - decel_h >= 0:
            base_px = float(close_arr[order_submit_bar - decel_h])
            recent_ret = (float(close_arr[order_submit_bar]) / base_px - 1.0) if base_px > 0 else 0.0
            if recent_ret > config.entry_decel_max_ret:   # still ripping up -> not a confirmed fade
                _emit("decel", order_submit_ts_ms=order_submit_ts, active_count=len(active), regime_trend=cand_trend)
                skipped_gate += 1
                continue
        if config.market_min_ret_1d > -1.0 and market_daily is not None:
            # Causal: read the PRIOR completed day's market return, NOT the entry
            # day's own full-day close-to-close return (which only realises at the
            # entry day's final bar and is future data at an intraday entry).
            # Mirrors the current-day exclusion in _btc_trend_returns.
            entry_day = (order_submit_ts // MS_PER_DAY) * MS_PER_DAY
            mkt = market_daily.get(entry_day - MS_PER_DAY)
            if mkt is not None and mkt < config.market_min_ret_1d:   # short into a weak tape = squeeze risk
                _emit("market", order_submit_ts_ms=order_submit_ts, active_count=len(active), regime_trend=cand_trend)
                skipped_gate += 1
                continue
        if admission_lookup is not None and not bool(admission_lookup.get((sym, int(sig_ts)), False)):
            _emit("admission", order_submit_ts_ms=order_submit_ts, active_count=len(active), regime_trend=cand_trend)
            skipped_admission += 1
            continue
        external_size_multiplier = 1.0
        if size_mult_lookup is not None:
            external_size_multiplier = float(size_mult_lookup.get((sym, int(sig_ts)), 1.0))
        if external_size_skip_on and external_size_multiplier <= external_size_skip_lte:
            _emit(
                "external_size_multiplier",
                order_submit_ts_ms=order_submit_ts,
                active_count=len(active),
                regime_trend=cand_trend,
                regime_size_mult=regime_size_mult,
                external_size_multiplier=external_size_multiplier,
            )
            skipped_external_size_multiplier += 1
            continue
        fill = _resolve_entry_fill(
            config,
            bars,
            side=config.side,
            order_submit_bar=int(order_submit_bar),
            order_submit_ts_ms=order_submit_ts,
        )
        if fill is None:
            ref_price = float(close_arr[order_submit_bar]) if int(order_submit_bar) < len(close_arr) else None
            limit_price = (
                ref_price * (1.0 + float(config.entry_adverse_limit_pct))
                if ref_price is not None and config.side == "short"
                else ref_price * (1.0 - float(config.entry_adverse_limit_pct)) if ref_price is not None else None
            )
            _emit(
                "entry_limit_unfilled",
                order_submit_ts_ms=order_submit_ts,
                fill_window_start_ts_ms=order_submit_ts,
                fill_window_end_ts_ms=order_submit_ts
                + exact_duration_ms(hours=max(config.entry_adverse_limit_wait_hours, 0)),
                active_count=len(active),
                regime_trend=cand_trend,
                entry_reference_price=ref_price,
                entry_limit_price=limit_price,
                entry_fill_mode="adverse_limit",
            )
            skipped_entry_limit_unfilled += 1
            continue
        entry_bar = int(fill["entry_bar"])
        entry_bar_end = int(fill["entry_bar_end_ts_ms"])
        # --- sizing + stop + exit planning (verbatim logic, extracted to helpers) ---
        entry_volatility = _entry_vol(close_arr, int(order_submit_bar))
        inverse_vol_multiplier = (
            min(max(config.target_vol_per_name / entry_volatility, 1.0 / clamp), clamp)
            if inverse_vol and entry_volatility > 0 else 1.0
        )
        nw, trade_stop = _compute_size_and_stop(
            config, close_arr, int(order_submit_bar),
            base_nw=base_nw, inverse_vol=inverse_vol, clamp=clamp,
            regime_size_mult=regime_size_mult, stop_pct=stop_approach_pct if stop_approach_on else stop_pct,
        )
        # Per-entry sizing hook (default None -> byte-identical): a causal,
        # gross-neutral notional multiplier keyed by (symbol, signal_ts). Applied AFTER all
        # selection gates, so entries/breadth/exits are unchanged; resize/impact cost is
        # recomputed at the new size by _round_trip_bps below. trade_stop is independent of nw,
        # so applying the multiplier after _compute_size_and_stop is numerically identical.
        nw *= external_size_multiplier
        pre_risk_nw = nw
        disaster_notional_cap: float | None = None
        risk_size_clamped = False
        if disaster_budget_on:
            disaster_notional_cap = disaster_budget / disaster_shock
            if nw > disaster_notional_cap:
                nw = disaster_notional_cap
                risk_size_clamped = True
        portfolio_heat_before_frac: float | None = None
        portfolio_heat_after_frac: float | None = None
        if portfolio_heat_on:
            active_notional_weight = sum(weight for _exit, weight in active_notional)
            portfolio_heat_before_frac = active_notional_weight * disaster_shock
            remaining_notional_weight = max(
                (portfolio_heat_cap / disaster_shock) - active_notional_weight,
                0.0,
            )
            if remaining_notional_weight <= 1e-15:
                _emit(
                    "portfolio_heat",
                    order_submit_ts_ms=order_submit_ts,
                    active_count=len(active),
                    regime_trend=cand_trend,
                    regime_size_mult=regime_size_mult,
                    external_size_multiplier=external_size_multiplier,
                    pre_risk_notional_weight=pre_risk_nw,
                    disaster_notional_cap=disaster_notional_cap,
                    portfolio_heat_before_frac=portfolio_heat_before_frac,
                    portfolio_heat_after_frac=portfolio_heat_before_frac,
                    risk_size_clamped=risk_size_clamped,
                )
                skipped_portfolio_heat += 1
                continue
            if nw > remaining_notional_weight:
                nw = remaining_notional_weight
                risk_size_clamped = True
            portfolio_heat_after_frac = (active_notional_weight + nw) * disaster_shock
        planned_exit = _plan_exit(
            state_mode=state_mode, spell_end=int(spell_end), entry_bar_end=entry_bar_end,
            delay_ms=delay_ms, max_hold_ms=max_hold_ms, hold_ms=hold_ms,
        )
        round_trip = _round_trip_bps(config, turn, notional_weight=nw)
        trade = _simulate_indexed_trade(
            symbol=sym, side=config.side, score=float(comp) if comp is not None else 0.0,
            rank=int(config.decile), basket_id=_iso_day(entry_bar_end), signal_ts_ms=int(sig_ts),
            entry_bar=int(entry_bar), symbol_bars=bars, planned_exit_ts_ms=planned_exit,
            notional_weight=nw, position_weight=1.0, config=lifecycle,
            round_trip_cost_bps=round_trip, stop_pct=trade_stop,
            rank_lookup=rank_lookup or {}, event_decay_threshold=0.0,
            funding_lookup=funding_lookup if config.use_funding else None,
            stop_fill_mode=config.stop_fill_mode, stop_slippage_cap_pct=config.stop_slippage_cap_pct,
            entry_price_override=fill.get("entry_price_override"),
        )
        trade_rows = [] if trade is None else [trade]
        if not trade_rows:
            _emit(
                "no_fill",
                entry_bar_end=entry_bar_end,
                order_submit_ts_ms=order_submit_ts,
                fill_window_start_ts_ms=fill.get("fill_window_start_ts_ms"),
                fill_window_end_ts_ms=fill.get("fill_window_end_ts_ms"),
                active_count=len(active),
                entry_reference_price=fill.get("entry_reference_price"),
                entry_limit_price=fill.get("entry_limit_price"),
                entry_fill_mode=fill.get("entry_fill_mode"),
            )
            skipped_no_bar += 1
            continue
        if stop_approach_on and trade.get("exit_reason") == "stop_loss":
            trade["exit_reason"] = "stop_approach"
        if (
            state_mode
            and trade.get("exit_reason") == "max_hold"
            and planned_exit < entry_bar_end + max_hold_ms
            and int(trade.get("exit_ts_ms") or 0) == int(planned_exit)
        ):
            trade["exit_reason"] = "left_decile"
        if disaster_budget_on or portfolio_heat_on:
            trade.update({
                "pre_risk_notional_weight": pre_risk_nw,
                "entry_disaster_notional_cap": disaster_notional_cap,
                "entry_disaster_shock_frac": disaster_shock,
                "entry_portfolio_heat_before_frac": portfolio_heat_before_frac,
                "entry_portfolio_heat_after_frac": portfolio_heat_after_frac,
                "entry_risk_size_clamped": risk_size_clamped,
            })
        rows.append(trade)
        exit_ts = int(trade["exit_ts_ms"])
        _emit(
            "selected",
            entry_bar_end=entry_bar_end,
            order_submit_ts_ms=order_submit_ts,
            fill_window_start_ts_ms=fill.get("fill_window_start_ts_ms"),
            fill_window_end_ts_ms=fill.get("fill_window_end_ts_ms"),
            active_count=len(active),
            regime_trend=cand_trend,
            regime_size_mult=regime_size_mult,
            notional_weight=nw,
            entry_volatility=entry_volatility,
            inverse_vol_multiplier=inverse_vol_multiplier,
            external_size_multiplier=external_size_multiplier,
            pre_risk_notional_weight=pre_risk_nw,
            disaster_notional_cap=disaster_notional_cap,
            portfolio_heat_before_frac=portfolio_heat_before_frac,
            portfolio_heat_after_frac=portfolio_heat_after_frac,
            risk_size_clamped=risk_size_clamped,
            entry_reference_price=fill.get("entry_reference_price"),
            entry_limit_price=fill.get("entry_limit_price"),
            entry_fill_mode=fill.get("entry_fill_mode"),
            exit_ts_ms=exit_ts,
        )
        heapq.heappush(active, exit_ts)
        heapq.heappush(active_notional, (exit_ts, abs(float(trade.get("notional_weight") or 0.0))))
        last_entry[sym] = order_submit_ts
        if breaker_on:
            for trade in trade_rows:
                if float(trade.get("net_return") or 0.0) < 0.0:
                    bisect.insort(adverse_exit_ts, int(trade["exit_ts_ms"]))
    skips = {
        "skipped_capacity": skipped_capacity,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_bar": skipped_no_bar,
        "skipped_gate": skipped_gate,
        "skipped_breaker": skipped_breaker,
        "skipped_btc_trend": skipped_btc_trend,
        "skipped_crowding": skipped_crowding,
        "skipped_admission": skipped_admission,
        "skipped_external_size_multiplier": skipped_external_size_multiplier,
        "skipped_entry_limit_unfilled": skipped_entry_limit_unfilled,
        "skipped_portfolio_heat": skipped_portfolio_heat,
    }
    if not rows:
        return _empty_trades(), skips
    return pl.DataFrame(rows), skips


def _additive_equity(trades: pl.DataFrame) -> pl.DataFrame:
    """Fixed-capital ADDITIVE daily equity (NOT compounding).

    For a capacity-capped book whose impact is sized off a FIXED deploy capital, additive PnL is
    the consistent accounting -- compounding implies a growing book that would face growing impact
    the fixed-capital model does not charge. `net_return` is already the fraction-of-capital PnL of
    each trade (notional_weight applied); sum it onto its exit day, then cumulative-SUM across days.
    Schema matches build_equity_curve so the same plot/CSV helpers work; equity = 1 + cum PnL,
    drawdown = equity - running-max (<=0, in return units)."""
    if trades.is_empty():
        return pl.DataFrame(
            {"ts_ms": pl.Series([], dtype=pl.Int64), "equity": pl.Series([], dtype=pl.Float64),
             "drawdown": pl.Series([], dtype=pl.Float64), "basket_return": pl.Series([], dtype=pl.Float64)}
        )
    daily = (
        trades.with_columns(((pl.col("exit_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("ts_ms"))
        .group_by("ts_ms").agg(pl.col("net_return").sum().alias("basket_return")).sort("ts_ms")
    )
    return daily.with_columns(
        (1.0 + pl.col("basket_return").cum_sum()).alias("equity")
    ).with_columns(
        (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")
    ).select("ts_ms", "equity", "drawdown", "basket_return")


def _additive_summary(trades: pl.DataFrame, config: ContinuousEventConfig) -> dict[str, Any]:
    equity = _additive_equity(trades)
    # Headline DD/MAR/Sharpe/return metrics come from the shared `_daily_pnl_metrics` helper
    # (single source of truth — see code-quality-6: a divergent second copy would silently
    # report a different number for the same sleeve). Only the additive-specific split is local.
    base = _daily_pnl_metrics(equity)
    if equity.is_empty():
        return {"n_trades": 0, **base}
    pnl = equity["basket_return"].to_list()
    split_ms = _date_str_to_ms(config.split_date)
    funding_modes = set(str(m) for m in trades["funding_mode"].to_list()) if "funding_mode" in trades.columns else set()
    fmode = "missing" if (not funding_modes or funding_modes == {"missing"}) else (
        "modeled" if funding_modes == {"modeled"} else "partial")
    # Compounding reference from the lifecycle accounting; shown for transparency, NOT headline.
    comp = float((pl.Series([p + 1.0 for p in pnl]).cum_prod()[-1]) - 1.0) if pnl else 0.0
    return {
        "n_trades": int(trades.height),
        **base,
        "win_rate": float((trades["net_return"] > 0.0).mean()),
        "gross_return": float(trades["gross_return"].sum()),
        "cost_return": float(trades["cost_return"].sum()),
        "funding_return": float(trades["funding_return"].sum()),
        "funding_mode": fmode,
        "early_return": float(trades.filter(pl.col("entry_ts_ms") < split_ms)["net_return"].sum()),
        "recent_return": float(trades.filter(pl.col("entry_ts_ms") >= split_ms)["net_return"].sum()),
        "compounding_total_return_ref": comp,
    }


def _daily_pnl_metrics(equity: pl.DataFrame) -> dict[str, Any]:
    """MAR/Sharpe/DD from any daily PnL series (cols: ts_ms, equity, drawdown, basket_return)."""
    if equity.is_empty():
        return {"total_return": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0, "mar": None,
                "sharpe_like": 0.0, "worst_day_return": 0.0}
    pnl = equity["basket_return"].to_list()
    total = float(equity["equity"][-1] - 1.0)
    maxdd = float(equity["drawdown"].min())
    ts = equity["ts_ms"]
    years = max((int(ts[-1]) - int(ts[0])) / (365.25 * MS_PER_DAY), 1e-9)
    ann = total / years
    # metrics-3: one shared Sharpe convention (ddof=1, sqrt(365.25)) across all reports.
    sharpe = annualized_sharpe(pnl)
    return {"total_return": total, "annualized_return": ann, "max_drawdown": maxdd,
            "mar": (ann / abs(maxdd)) if abs(maxdd) > 1e-9 else None,
            "sharpe_like": sharpe, "worst_day_return": float(min(pnl)) if pnl else 0.0}


def _portfolio_mtm_equity(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Daily portfolio MARK-TO-MARKET equity (additive, fixed-capital).

    Realized-PnL-at-exit (`_additive_equity`) only books a trade's whole PnL on its exit day, so a
    day where 25 still-open alt shorts all move against you shows nothing until they exit. This marks
    every OPEN position to the daily close, distributing each trade's gross PnL across the calendar
    days it is held; cost is booked on the entry day, funding on the exit day. Concurrent correlated
    moves therefore aggregate into the daily series → a real portfolio drawdown. Gross daily marks
    telescope to the trade's realized gross, so total return is unchanged; only the PATH (and thus DD
    and Sharpe) differ.

    LEDGER-DAY SEMANTICS ARE LOAD-BEARING — do NOT calendar-fill this series. The persisted
    `continuous_mtm_equity.csv` is the validated input of the ensemble rebalance pipeline
    (`continuous_rebalance` vol/beta/momentum windows are defined over trailing LEDGER rows, and
    `apply_rebalance_rule` hedges every input row). Zero-filling flat days dilutes the w90 vol
    window (re-levering the whole validated path) and fabricates hedge PnL on days the book has
    zero exposure — observed 2026-06-12: bybit deployed-ensemble total return shifted 103%→87%
    from exactly this. Flat-tail/gap presentation belongs to the chart layer
    (`_extend_equity_flat_for_chart`, `_step_fill_daily`, monthly gap-fill)."""
    if trades.is_empty() or klines.is_empty():
        return _additive_equity(trades)  # empty-safe schema
    dc = (
        klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by(["symbol", "day"]).agg(pl.col("close").last().alias("c"))
    )
    close = {(r[0], r[1]): r[2] for r in dc.iter_rows()}
    daily: dict[int, float] = {}
    for t in trades.select(
        "symbol", "side", "entry_ts_ms", "exit_ts_ms", "entry_price", "exit_price",
        "notional_weight", "cost_return", "funding_return",
    ).iter_rows():
        s, side, e_ts, x_ts, p0, px, w, cost_r, fund_r = t
        if p0 is None or p0 <= 0:
            continue
        sign = -1.0 if side == "short" else 1.0
        e_day = (int(e_ts) // MS_PER_DAY) * MS_PER_DAY
        x_day = (int(x_ts) // MS_PER_DAY) * MS_PER_DAY
        prev = float(p0)
        d = e_day
        while d <= x_day:
            curr = float(px) if d == x_day else float(close.get((s, d), prev))
            daily[d] = daily.get(d, 0.0) + w * sign * (curr - prev) / float(p0)  # gross MTM increment
            prev = curr
            d += MS_PER_DAY
        daily[e_day] = daily.get(e_day, 0.0) + float(cost_r)      # cost on entry day
        daily[x_day] = daily.get(x_day, 0.0) + float(fund_r)      # funding on exit day
    if not daily:
        return _additive_equity(trades)
    days = sorted(daily)
    return pl.DataFrame({"ts_ms": days, "basket_return": [daily[d] for d in days]}).with_columns(
        (1.0 + pl.col("basket_return").cum_sum()).alias("equity")
    ).with_columns(
        (pl.col("equity") - pl.col("equity").cum_max()).alias("drawdown")
    ).select("ts_ms", "equity", "drawdown", "basket_return")


def _extend_equity_flat_for_chart(equity: pl.DataFrame, *, through_ts_ms: int) -> pl.DataFrame:
    """CHART-ONLY flat-tail extension: append zero-return days (equity/drawdown carried) through
    ``through_ts_ms`` so a book that goes flat near the data end renders as a flat line to the
    boundary (and the final months appear on the axis) instead of the curve silently truncating
    at the last exit. Never persist this shape — the stored mtm CSV must keep ledger-day rows
    (see `_portfolio_mtm_equity`)."""
    if equity.is_empty():
        return equity
    last_ts = int(equity["ts_ms"].max())
    if through_ts_ms <= last_ts:
        return equity
    tail = equity.sort("ts_ms").tail(1)
    last_equity = float(tail["equity"][0])
    last_dd = float(tail["drawdown"][0])
    n_days = (through_ts_ms - last_ts) // MS_PER_DAY
    pad = pl.DataFrame(
        {
            "ts_ms": [last_ts + (i + 1) * MS_PER_DAY for i in range(n_days)],
            "equity": [last_equity] * n_days,
            "drawdown": [last_dd] * n_days,
            "basket_return": [0.0] * n_days,
        }
    ).select(equity.columns)
    return pl.concat([equity, pad]).sort("ts_ms")


def _split_metrics(trades: pl.DataFrame, config: ContinuousEventConfig) -> dict[str, dict[str, Any]]:
    """Additive (fixed-capital) summaries: full + early/recent (split on entry ts)."""
    if trades.is_empty():
        return {}
    split_ms = _date_str_to_ms(config.split_date)
    out: dict[str, dict[str, Any]] = {"full": _additive_summary(trades, config)}
    early = trades.filter(pl.col("entry_ts_ms") < split_ms)
    recent = trades.filter(pl.col("entry_ts_ms") >= split_ms)
    if not early.is_empty():
        out["early"] = _additive_summary(early, config)
    if not recent.is_empty():
        out["recent"] = _additive_summary(recent, config)
    return out


def _write_equity_png(equity: pl.DataFrame, path: Path, *, title: str) -> None:
    if equity.is_empty():
        return
    try:
        import matplotlib
    except ImportError:
        # matplotlib is an optional charting dependency (not in install_requires — the
        # canonical *_equity_btc.png uses Pillow). A research-only PNG must never fail the run.
        return
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    eq = equity.sort("ts_ms")
    xs = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in eq["ts_ms"].to_list()]
    fig, (ax, axd) = plt.subplots(2, 1, figsize=(13, 7), sharex=True, height_ratios=[3, 1])
    ax.plot(xs, [(v - 1.0) * 100 for v in eq["equity"].to_list()], color="#c0392b", lw=1.5)
    ax.set_ylabel("cumulative return (%)")
    ax.set_title(title)
    ax.grid(alpha=0.25)
    ax.text(0.99, 0.02, "EXPLORATORY engine — NOT promotion evidence", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=7, color="grey", style="italic")
    axd.fill_between(xs, [d * 100 for d in eq["drawdown"].to_list()], 0.0, color="#2c3e50", alpha=0.5)
    axd.set_ylabel("drawdown (%)")
    axd.grid(alpha=0.25)
    axd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _research_input_cache_key(
    root: Path, config: ContinuousEventConfig, entry_order: str, end_ms: int
) -> tuple[str, str, str, int]:
    return (str(root.resolve()), config.config_hash(), entry_order, int(end_ms))


def _cache_research_inputs(cache_key: tuple[str, str, str, int], payload: dict[str, Any]) -> None:
    _RESEARCH_INPUT_CACHE[cache_key] = payload
    _RESEARCH_INPUT_CACHE.move_to_end(cache_key)
    while len(_RESEARCH_INPUT_CACHE) > _RESEARCH_INPUT_CACHE_MAX:
        _RESEARCH_INPUT_CACHE.popitem(last=False)


def _prepare_research_inputs(
    root: Path,
    config: ContinuousEventConfig,
    entry_order: str,
) -> dict[str, Any]:
    start_ms, end_ms = _date_str_to_ms(config.start_date), _resolve_end_ms(root, config)
    cache_key = _research_input_cache_key(root, config, entry_order, end_ms)
    cached = _RESEARCH_INPUT_CACHE.get(cache_key)
    if cached is not None:
        _RESEARCH_INPUT_CACHE.move_to_end(cache_key)
        return cached

    panel = build_continuous_panel(root, config)
    entries = _fresh_entries(panel, config) if not panel.is_empty() else panel
    if not entries.is_empty():
        entries = _apply_entry_order(entries, entry_order)

    kname = _autodetect_dataset_names(root)["klines_dataset"]
    pad_fwd = exact_duration_ms(
        hours=config.hold_hours
        + config.entry_delay_hours
        + max(config.entry_adverse_limit_wait_hours, 0)
        + 4
    )
    # audit2c: also reserve >=2 warmup days when the equal-weight market gate is on, so the
    # gate's one-day-lagged daily market return is available from the window's first day
    # instead of failing OPEN (allowing entries) for the first ~2 days.
    btc_trend_lookback_days = max(int(config.btc_trend_lookback_days), 1)
    pad_back = max(
        exact_duration_ms(days=max(config.age_days_min, 0)),
        exact_duration_ms(days=2) if config.market_min_ret_1d > -1.0 else 0,
    )
    if config.btc_trend_gate != "off":
        mode = str(config.btc_trend_mode)
        if mode == BTC_TREND_MODE_DAILY_PRIOR:
            pad_back = max(pad_back, exact_duration_ms(days=btc_trend_lookback_days + 1))
        elif mode == BTC_TREND_MODE_HOURLY_30D:
            pad_back = max(pad_back, exact_duration_ms(days=btc_trend_lookback_days) + MS_PER_HOUR)
        elif mode == BTC_TREND_MODE_HOURLY_EXACT_MONTH:
            pad_back = max(pad_back, exact_duration_ms(days=config.btc_trend_month_days) + MS_PER_HOUR)
        elif mode == BTC_TREND_MODE_SMART_MONTH:
            pad_back = max(
                pad_back,
                exact_duration_ms(days=config.btc_trend_month_days) + MS_PER_HOUR,
                exact_duration_ms(days=btc_trend_lookback_days + 1),
            )
        else:
            raise ValueError(f"btc_trend_mode must be one of {BTC_TREND_MODES}; got {mode!r}")
    klines = _read_window(
        root, kname, start_ms=start_ms - pad_back, end_ms=end_ms + pad_fwd,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
    )
    if config.exclude_symbols and not klines.is_empty():
        klines = klines.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines) if not klines.is_empty() else {}

    funding_lookup = None
    if config.use_funding:
        fname = _autodetect_dataset_names(root)["funding_dataset"]
        funding = _read_window(root, fname, start_ms=start_ms - 10 * MS_PER_DAY, end_ms=end_ms + pad_fwd)
        # Derive each symbol's TRUE settlement interval from the realized rate-change
        # cadence (not the stale stored funding_interval_min) so genuine sub-8h alts
        # are charged every settlement and any real SNAPSHOT over-sampling collapses.
        funding_intervals = derive_funding_interval_min(funding)
        _assert_funding_one_per_settlement(funding, root=root, interval_by_symbol=funding_intervals)
        funding_lookup = _funding_lookup(funding, interval_by_symbol=funding_intervals)

    market_daily = None
    if config.market_min_ret_1d > -1.0 and not klines.is_empty():
        market_daily = _market_daily_returns(klines)

    btc_trend_daily = None
    if config.btc_trend_gate != "off" and not klines.is_empty():
        btc_trend_daily = _btc_trend_return_lookup(
            klines,
            mode=str(config.btc_trend_mode),
            lookback_days=btc_trend_lookback_days,
            month_days=float(config.btc_trend_month_days),
            smart_tolerance=float(config.btc_trend_smart_tolerance),
        )

    rank_lookup = None
    if config.rank_exit_threshold > 0.0 and not panel.is_empty():
        rank_lookup = _continuous_rank_lookup(
            panel,
            delay_ms=exact_duration_ms(hours=1 + config.entry_delay_hours),
        )

    # Authoritative per-symbol PIT listing (first-ever bar under the root), read independently of the
    # run window so the age gate does not infer listing from the clamped window start (pit-engine-2).
    listing_ts_by_symbol = _listing_ts_by_symbol(root) if config.age_days_min > 0 else None

    payload = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "panel": panel,
        "entries": entries,
        "klines": klines,
        "symbol_bars": symbol_bars,
        "funding_lookup": funding_lookup,
        "market_daily": market_daily,
        "btc_trend_daily": btc_trend_daily,
        "rank_lookup": rank_lookup,
        "listing_ts_by_symbol": listing_ts_by_symbol,
    }
    _cache_research_inputs(cache_key, payload)
    return payload


def run_continuous_event_research(
    data_root: str | Path,
    *,
    config: ContinuousEventConfig | None = None,
    report_dir: str | Path | None = None,
    candidate_tape_path: str | Path | None = None,
    entry_order: str = "fcfs",
    size_mult_lookup: dict[tuple[str, int], float] | None = None,
    admission_lookup: dict[tuple[str, int], bool] | None = None,
) -> dict[str, Any]:
    """Run the execution-grade continuous-fade backtest and (optionally) write artifacts.

    When `candidate_tape_path` is set, the full eligible candidate set (selected + rejected,
    with the exact engine reason) is written to that parquet for candidate-tape reconstruction. The
    extra emission is purely additive: with `candidate_tape_path=None` the run is unchanged.

    `entry_order` re-orders candidates WITHIN each signal timestamp by an
    entry-priority score before the unchanged selection loop; `"fcfs"` (default) reproduces the
    frozen control exactly. See `_apply_entry_order`.

    `admission_lookup` is a research-only gate keyed by ``(symbol, signal_ts_ms)``. ``None``
    preserves the control engine; when supplied, missing/false keys are rejected before sizing.
    """
    config = config or ContinuousEventConfig()
    root = Path(str(data_root)).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")

    inputs = _prepare_research_inputs(root, config, entry_order)
    end_ms = int(inputs["end_ms"])
    entries = inputs["entries"]
    klines = inputs["klines"]
    symbol_bars = inputs["symbol_bars"]
    funding_lookup = inputs["funding_lookup"]
    market_daily = inputs["market_daily"]
    btc_trend_daily = inputs["btc_trend_daily"]
    rank_lookup = inputs["rank_lookup"]
    listing_ts_by_symbol = inputs["listing_ts_by_symbol"]

    candidate_sink: list[dict[str, Any]] | None = [] if candidate_tape_path is not None else None
    if not entries.is_empty() and symbol_bars:
        trades, skips = _run_trades(
            entries, symbol_bars, funding_lookup, config, market_daily, btc_trend_daily, rank_lookup,
            candidate_sink=candidate_sink, size_mult_lookup=size_mult_lookup,
            admission_lookup=admission_lookup,
            listing_ts_by_symbol=listing_ts_by_symbol,
        )
    else:
        trades, skips = _empty_trades(), {}

    equity = _additive_equity(trades)
    splits = _split_metrics(trades, config)
    mtm_equity = _portfolio_mtm_equity(trades, klines)
    mtm = _daily_pnl_metrics(mtm_equity)

    funding_mode = splits.get("full", {}).get("funding_mode", "missing")
    run_label = "exploratory"  # engine-grade fills but modeled impact + research-tuned selection

    payload: dict[str, Any] = {
        "config": asdict(config),
        "config_hash": config.config_hash(),
        "data_root": str(root),
        "run_label": run_label,
        "n_fresh_entries": int(entries.height) if not entries.is_empty() else 0,
        "n_trades": int(trades.height),
        "skips": skips,
        "funding_mode": funding_mode,
        "metrics": splits,                 # realized-PnL-at-exit (additive, fixed-capital)
        "metrics_mtm": mtm,                # portfolio mark-to-market (correlated-DD aware)
    }

    if candidate_tape_path is not None:
        tape_path = Path(str(candidate_tape_path)).expanduser()
        tape_path.parent.mkdir(parents=True, exist_ok=True)
        tape_schema = {
            "symbol": pl.Utf8,
            "signal_ts_ms": pl.Int64,
            "signal_bar_close_ts_ms": pl.Int64,
            "decision_ts_ms": pl.Int64,
            "feature_ts_ms": pl.Int64,
            "data_available_ts_ms": pl.Int64,
            "rmom_source_day_ts_ms": pl.Int64,
            "rmom_data_available_ts_ms": pl.Int64,
            "btc_trend_source_start_ts_ms": pl.Int64,
            "btc_trend_lookback_days": pl.Int64,
            "btc_trend_mode": pl.Utf8,
            "btc_trend_lookback_duration_ms": pl.Int64,
            "btc_trend_source_end_ts_ms": pl.Int64,
            "btc_trend_data_available_ts_ms": pl.Int64,
            "order_submit_ts_ms": pl.Int64,
            "fill_window_start_ts_ms": pl.Int64,
            "fill_window_end_ts_ms": pl.Int64,
            "composite": pl.Float64,
            "residual_momentum_value": pl.Float64,
            "residual_momentum_rank": pl.Float64,
            "feature_rv_168h": pl.Float64,
            "feature_vov": pl.Float64,
            "feature_dist_low": pl.Float64,
            "feature_xsret7": pl.Float64,
            "feature_xsret3": pl.Float64,
            "feature_ret1": pl.Float64,
            "feature_max_ret168": pl.Float64,
            "feature_prior6_ret1_max": pl.Float64,
            "feature_giveback_from_prior6_high": pl.Float64,
            "feature_turnover_spike_168h": pl.Float64,
            "turnover_quote": pl.Float64,
            "liquidity_value": pl.Float64,
            "liquidity_rank": pl.Float64,
            "volume_1h_quote": pl.Float64,
            "volume_24h_quote": pl.Float64,
            "volume_zscore": pl.Float64,
            "spell_end_ts_ms": pl.Int64,
            "entry_bar_end_ts_ms": pl.Int64,
            "entry_reference_price": pl.Float64,
            "entry_limit_price": pl.Float64,
            "entry_fill_mode": pl.Utf8,
            "crowding_count": pl.Int64,
            "active_count": pl.Int64,
            "btc_trend_gate": pl.Utf8,
            "regime_trend": pl.Float64,
            "regime_size_mult": pl.Float64,
            "sizing_mode": pl.Utf8,
            "base_notional_weight": pl.Float64,
            "entry_volatility": pl.Float64,
            "inverse_vol_multiplier": pl.Float64,
            "external_size_multiplier": pl.Float64,
            "pre_risk_notional_weight": pl.Float64,
            "disaster_notional_cap": pl.Float64,
            "portfolio_heat_before_frac": pl.Float64,
            "portfolio_heat_after_frac": pl.Float64,
            "risk_size_clamped": pl.Boolean,
            "notional_weight": pl.Float64,
            "exit_ts_ms": pl.Int64,
            "selected": pl.Boolean,
            "reason": pl.Utf8,
        }
        tape_df = (
            pl.DataFrame(candidate_sink).cast(tape_schema, strict=False)
            if candidate_sink
            else pl.DataFrame(schema=tape_schema)
        )
        tape_df.write_parquet(tape_path)
        payload["candidate_tape_path"] = str(tape_path)
        payload["n_candidates"] = int(tape_df.height)
        payload["n_candidates_selected"] = int(tape_df.filter(pl.col("selected")).height) if tape_df.height else 0

    if report_dir is not None:
        out_dir = Path(str(report_dir)).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Always write the ledger CSVs — even empty ones (a legitimately flat component):
        # the forward-replay orchestrator/scout loads these by path, so a missing CSV on a
        # zero-trade component used to hard-fail the whole venue (audit-iter4/5 deferred).
        trades.write_csv(out_dir / "continuous_trades.csv")
        equity.write_csv(out_dir / "continuous_equity.csv")
        mtm_equity.write_csv(out_dir / "continuous_mtm_equity.csv")
        if not trades.is_empty():
            # Charts render an extended copy so a book that goes flat near the end (e.g. the
            # BTC-trend gate blocking all entries) draws as a flat line through the data
            # boundary; the persisted CSVs above keep the validated ledger-day shape.
            chart_boundary = end_ms - MS_PER_DAY
            if not klines.is_empty():
                chart_boundary = min(chart_boundary, (int(klines["ts_ms"].max()) // MS_PER_DAY) * MS_PER_DAY)
            mtm_chart = _extend_equity_flat_for_chart(mtm_equity, through_ts_ms=chart_boundary)
            _write_equity_png(
                mtm_chart, out_dir / "continuous_mtm_equity.png",
                title=(f"PORTFOLIO MARK-TO-MARKET — {config.side} D{config.decile} | hold {config.hold_hours}h "
                       f"({config.exit_mode}) | MAR {mtm.get('mar')} DD {abs(mtm.get('max_drawdown') or 0)*100:.1f}%  [EXPLORATORY]"),
            )
            _write_equity_png(
                equity, out_dir / "continuous_equity.png",
                title=(f"continuous fade {config.side} D{config.decile} | hold {config.hold_hours}h | "
                       f"liq>=${int(config.liq_turnover_min/1000)}k | stop {config.stop_loss_pct:.0%} | "
                       f"MAR {splits.get('full', {}).get('mar')}  [EXPLORATORY]"),
            )
            # Canonical strategy-vs-BTC PNG (same renderer + monthly-return table as the short/long
            # sleeves) so the continuous curve is visually comparable; equity_curves.sh prefers the
            # *_equity_btc.png. Marked EXPLORATORY (research-tuned selection + modeled impact).
            try:
                from .volume_events_charts import _write_equity_benchmark_chart
                _date = pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
                eq_dated = mtm_chart.with_columns(_date)
                btc_klines = (
                    klines.filter(pl.col("symbol") == "BTCUSDT").with_columns(_date)
                    if not klines.is_empty() else klines
                )
                # Real per-month aggregation: trade counts by ENTRY month (not equity rows — the
                # shared renderer's None-fallback counts pl.len() of daily marks ≈ ~30/month) +
                # MTM monthly return. Without this the table shows ~days, not the ~hundreds of
                # trades/month a high-turnover book actually opens.
                trades_m = (
                    trades.with_columns(pl.col("entry_date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
                    .group_by("month").agg(pl.len().alias("trades"))
                )
                monthly_df = (
                    eq_dated.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
                    .group_by("month")
                    .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
                    .join(trades_m, on="month", how="left")
                    .with_columns(pl.col("trades").fill_null(0))
                    .sort("month")
                )
                if not eq_dated.is_empty() and not btc_klines.is_empty():
                    _write_equity_benchmark_chart(
                        out_dir, root=root, equity=eq_dated, raw_klines=btc_klines, monthly=monthly_df,
                        png_name="continuous_equity_btc.png",
                        title=(f"Continuous-fade {config.side} D{config.decile} vs BTC | hold "
                               f"{config.hold_hours}h ({config.exit_mode}) | MTM-MAR {mtm.get('mar')} "
                               f"DD {abs(mtm.get('max_drawdown') or 0)*100:.1f}%  [EXPLORATORY]"),
                    )
            except Exception:  # noqa: BLE001 - chart failure must not fail the run
                pass
        (out_dir / "continuous_report.json").write_text(json.dumps(payload, indent=2, default=str))
        payload["report_dir"] = str(out_dir)

    return payload
