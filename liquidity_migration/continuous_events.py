"""Historical equity engine for the active continuous short-fade profile."""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms
from .account_contracts import AccountRiskPolicy
from .account_kernel import verify_account_journal
from .account_service import SleeveAdapterKind
from .config import DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .continuous_profile import (
    CONTINUOUS_EQUITY_EVIDENCE_LABEL,
    CONTINUOUS_HISTORY_START_DATE,
    CONTINUOUS_PROFILE_ID,
    CONTINUOUS_PROFILE_REVISION,
)
from .daily_feature_panel import _autodetect_dataset_names, _date_str_to_ms, _read_window
from .storage import read_dataset_columns
from .volume_events_pit import filter_klines_to_pit_membership
from .trade_lifecycle import (
    _IndexedTradeState,
    _empty_trades,
    _funding_lookup,
    _indexed_price_bars_by_symbol,
    annualized_sharpe,
)
from .execution_adapters import ExecutionTwinConfig, LatencyProfile
from .historical_account_replay import (
    HistoricalAccountSession,
    HistoricalTargetDecision,
    neutralize_rejected_entry_decisions,
    submit_historical_decisions,
    synthetic_historical_rules_for_symbols,
)
from .strategy_targets import component_target_intent
from .strategy_funnel import (
    DecisionFunnelObserver,
    finalize_funnel_row,
    gate_state,
    observe_funnel_rows_safely,
)

FEATURES = ("rv_168h", "vov", "dist_low", "xsret7", "xsret3")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContinuousEventConfig:
    """Inputs that still vary in the active continuous equity path."""

    profile_id: str = CONTINUOUS_PROFILE_ID
    profile_revision: str = CONTINUOUS_PROFILE_REVISION
    component_key: str = ""
    execution_strategy_id: str = ""
    execution_leverage: float = 1.0
    start_date: str = CONTINUOUS_HISTORY_START_DATE
    end_date: str = ""
    side: str = "short"
    decile: int = 9
    rmom_quantile: float = 0.25
    feature_set: tuple[str, ...] = ("max_ret168",)
    liq_turnover_min: float = 500_000.0
    entry_delay_hours: int = 1
    hold_hours: int = 24
    take_profit_pct: float = 0.12
    # Declared wide backstop, mirrored from the active profile so the modeled
    # exit rule matches the venue stop. Zero disables it.
    stop_loss_pct: float = 0.35
    gross_exposure: float = 0.5
    max_active: int = 25
    taker_fee_bps: float = 5.5
    spread_bps: float = 2.5
    impact_coef_bps: float = 50.0
    impact_exponent: float = 0.5
    deploy_capital_usd: float = 1_000_000.0
    sizing_mode: str = "inverse_vol"
    target_vol_per_name: float = 0.01
    vol_weight_clamp: float = 2.0
    age_days_min: int = 240
    btc_trend_gate: str = "uptrend"
    btc_trend_lookback_days: int = 30
    entry_event_trigger: str = "none"
    entry_crowding_max_fresh: int = 2
    use_funding: bool = True
    # Entry-admission floor on the candidate's last SETTLED funding print at the
    # signal-bar close: admit only when rate >= floor; None disables it.
    # Settled history only, never a predicted rate. A candidate with no settled
    # print admits and is counted as an unknown-admit.
    funding_min_at_entry: float | None = None
    split_date: str = "2025-06-01"
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS

    @property
    def notional_weight(self) -> float:
        return self.gross_exposure / max(self.max_active, 1)

    def config_hash(self) -> str:
        material = asdict(self)
        # Execution routing and margin do not alter the historical signal.
        material.pop("execution_strategy_id", None)
        material.pop("execution_leverage", None)
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]

    @property
    def kernel_strategy_id(self) -> str:
        return self.execution_strategy_id.strip() or f"continuous_events_{self.config_hash()}"


def _iso_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _panel_cache_stale(cache_path: Path, rmom_path: Path) -> bool:
    """Invalidate a panel when its RMOM input is missing or newer."""
    try:
        return (not rmom_path.exists()) or (cache_path.stat().st_mtime < rmom_path.stat().st_mtime)
    except OSError:
        return True


def _feature_tag(feature_set: tuple[str, ...]) -> str:
    raw = "_".join(feature_set) or "none"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _listing_ts_by_symbol(root: Path) -> dict[str, int]:
    """Return each symbol's first kline across the full root for the age gate."""
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = read_dataset_columns(root, kname, columns=["symbol", "ts_ms"])
    if k.is_empty() or "symbol" not in k.columns or "ts_ms" not in k.columns:
        return {}
    first = k.group_by("symbol").agg(pl.col("ts_ms").min().alias("first_ts"))
    return {str(s): int(t) for s, t in first.iter_rows()}


def _window_ms(config: ContinuousEventConfig) -> tuple[int, int]:
    if not config.end_date:
        raise ValueError("continuous equity requires an explicit end_date")
    start_ms = _date_str_to_ms(config.start_date)
    end_ms = _date_str_to_ms(config.end_date)
    if end_ms <= start_ms:
        raise ValueError("continuous equity end_date must be after start_date")
    return start_ms, end_ms


def _window_tag(config: ContinuousEventConfig) -> str:
    return hashlib.sha256(
        f"{config.start_date}_{config.end_date}".encode("utf-8")
    ).hexdigest()[:8]


def _exclude_tag(exclude_symbols: Any) -> str:
    """Stable 8-char hash of the exclusion set, order-independent."""
    syms = sorted(str(s) for s in (exclude_symbols or []))
    return hashlib.sha256("|".join(syms).encode("utf-8")).hexdigest()[:8]


def _panel_cache_path(
    root: Path,
    config: ContinuousEventConfig,
    *,
    require_pit_membership: bool = False,
    residual_momentum_path: Path | None = None,
) -> Path:
    window_tag = _window_tag(config)
    # Exclusions are material because the panel is ranked cross-sectionally.
    excl_part = "" if not config.exclude_symbols else f"_excl{_exclude_tag(config.exclude_symbols)}"
    pit_part = "_pit" if require_pit_membership else ""
    rmom_part = ""
    if residual_momentum_path is not None:
        rmom_part = "_rmom" + hashlib.sha256(
            str(residual_momentum_path.resolve()).encode("utf-8")
        ).hexdigest()[:8]
    return root / (
        f"_continuous_engine_panel_v4_rmom{int(round(config.rmom_quantile * 100))}"
        f"_feat{_feature_tag(config.feature_set)}{excl_part}{pit_part}{rmom_part}_{window_tag}.parquet"
    )


# residual_momentum[D] sums residual_return[D-9..D-3], so a fresh table may
# trail klines briefly. Beyond this bound the panel join would drop recent days.
RMOM_COVERAGE_TOLERANCE_DAYS = 2


def require_stable_residual_momentum(
    table: pl.DataFrame,
    *,
    source: str | Path,
) -> pl.DataFrame:
    """Require valid RMOM provenance, keys, and values; return stable rows.

    Duplicate keys multiply panel rows, invalid values corrupt ranks, and rows
    without provisional-state provenance cannot be safely filtered.
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
    """Fail loudly when residual_momentum lags the klines window.

    The decile build left-joins rmom on (symbol, day_ts) and keeps non-null rmom,
    so a stale table drops every symbol on recent days and understates exposure
    and return. Runtime has an equivalent freshness guard.
    """
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
            f"POLARS_MAX_THREADS=8 python scripts/data/precompute_residual_momentum.py --root {root}"
        )


def build_continuous_panel(
    data_root: str | Path,
    config: ContinuousEventConfig,
    *,
    cache: bool = True,
    require_pit_membership: bool = False,
    residual_momentum_path: str | Path | None = None,
) -> pl.DataFrame:
    """Build the deciled rmom-low panel: (symbol, ts_ms, decile, composite, turnover_quote).

    PIT-causal: features are trailing closed-bar windows on `ts_ms`'s close (known at
    ts_ms+1h); the rmom join is a day-floor lag1 (residual_momentum[D] uses residuals <= D-1).
    This is the engine-owned decile panel builder; it computes realised fills
    downstream rather than carrying a forward-return proxy column.

    Decision-grade historical callers set ``require_pit_membership=True``.
    That filters raw bars before per-symbol features and cross-sectional ranks.
    Operational current-universe roots retain the default because they do not
    necessarily carry a historical archive manifest.
    """
    root = Path(str(data_root)).expanduser()
    start_ms, end_ms = _window_ms(config)
    rmom_path = (
        Path(residual_momentum_path).expanduser().resolve()
        if residual_momentum_path is not None
        else root / "residual_momentum.parquet"
    )
    cache_path = _panel_cache_path(
        root,
        config,
        require_pit_membership=require_pit_membership,
        residual_momentum_path=(rmom_path if residual_momentum_path is not None else None),
    )
    if cache and cache_path.exists() and not _panel_cache_stale(cache_path, rmom_path):
        return pl.read_parquet(cache_path)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(
        root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )
    if k.is_empty():
        return pl.DataFrame()
    if require_pit_membership:
        archive_manifest = read_dataset_columns(
            root,
            "archive_trade_manifest",
            columns=["date", "symbol"],
        )
        k, _pit_filter_receipt = filter_klines_to_pit_membership(k, archive_manifest)
        if k.is_empty():
            return pl.DataFrame()
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if not rmom_path.exists():
        raise FileNotFoundError(
            f"{rmom_path} missing -- build it first: "
            f"POLARS_MAX_THREADS=8 python scripts/data/precompute_residual_momentum.py --root {root}"
        )
    rmom = pl.read_parquet(rmom_path)
    # Provisional rows can change when delayed residuals arrive.
    rmom = require_stable_residual_momentum(rmom, source=rmom_path)
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
    """Per-symbol trailing-window features (NO cross-section): the rolling parts
    that depend only on a symbol's own closed-bar history (ret1, rv_168h, ret72,
    ret168, min720, max720, vov, dist_low). Split out of
    `compute_continuous_decile_panel` so the live demo can recompute them once
    per bar close and cache them (see `continuous_demo.LivePanelCache`)."""
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
    k = k.with_columns(
        pl.col("ret1").rolling_std(window_size=168, min_samples=48).over("symbol").alias("rv_168h"),
        # Max single-hour return over the trailing week (the MAX /
        # lottery-demand feature). Always computed, but only enters the
        # composite when feature_set includes it.
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
    funnel_observer: DecisionFunnelObserver | None = None,
    funnel_venue: str = "unknown",
    funnel_signal_ts_ms: int | None = None,
) -> pl.DataFrame:
    """Cross-sectional composite -> decile from per-symbol features. `k` must
    already carry [symbol, ts_ms, turnover_quote, rv_168h, vov, dist_low, ret72,
    ret168]; the backtest passes the full multi-ts panel while the live cache
    assembles a single current-ts frame from cached carry plus the live price,
    so both run the identical tail.

    Cross-sectional ops (`xsret*`, `_rr`, `_n_*`, decile) rank WITHIN each
    `ts_ms` group, so computing them here (after the backtest's start_ms filter)
    matches computing them before it: start_ms drops whole timestamps, never
    individual symbols inside a surviving one."""
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    if funnel_observer is not None:
        try:
            source_panel = continuous_source_decile_panel(
                k,
                rmom,
                feature_set=("max_ret168",),
            )
            if funnel_signal_ts_ms is not None:
                source_panel = source_panel.filter(pl.col("ts_ms") == funnel_signal_ts_ms)
            source_rows: list[dict[str, Any]] = []
            for row in source_panel.filter(pl.col("source_decile") == 9).to_dicts():
                signal_ts_ms = int(row["ts_ms"])
                source_rows.append(
                    finalize_funnel_row(
                        {
                            **row,
                            "sleeve": "continuous",
                            "venue": funnel_venue,
                            "signal_ts_ms": signal_ts_ms,
                            "feature_ts_ms": int(row["feature_ts_ms"]),
                            "data_available_ts_ms": int(row["data_available_ts_ms"]),
                            "decision_ts_ms": int(row["decision_ts_ms"]),
                            "entry_ts_ms": None,
                            "evaluation_ts_ms": int(row["decision_ts_ms"]),
                            "component_scope": "shared_pre_component",
                            "gate_pit_tradable": "not_applicable",
                            "gate_history_floor": "missing",
                            "gate_source_decile_9": gate_state(row["source_decile"] == 9),
                            "gate_liquidity_floor": gate_state(
                                float(row["turnover_quote"]) >= 500_000.0
                            ),
                            "gate_one_hour_confirmation": "not_applicable",
                        },
                        required_gate_order=(
                            "pit_tradable",
                            "history_floor",
                            "source_decile_9",
                            "liquidity_floor",
                            "one_hour_confirmation",
                        ),
                    )
                )
            observe_funnel_rows_safely(funnel_observer, source_rows)
        except Exception:  # noqa: BLE001 - observer projection cannot change active ranks
            _LOGGER.exception("continuous pre-gate funnel projection failed")
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
    # Clamp the rank denominator so a one-symbol timestamp ranks at zero.
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


def continuous_source_decile_panel(
    k: pl.DataFrame,
    rmom: pl.DataFrame,
    *,
    feature_set: tuple[str, ...] = ("max_ret168",),
) -> pl.DataFrame:
    """Rank the barebones source before active RMOM/component filters.

    ``k`` already carries causal per-symbol features. Residual momentum is a
    left-joined characteristic and never removes a source row here.
    """

    present = [feature for feature in feature_set if feature in k.columns]
    if not present:
        raise ValueError("continuous source panel requires at least one selected feature")
    source = k.filter(
        pl.all_horizontal(
            [pl.col(feature).is_not_null() & pl.col(feature).is_finite() for feature in present]
        )
    )
    source = source.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    rmom_columns = [
        column
        for column in ("symbol", "day_ts", "residual_momentum")
        if column in rmom.columns
    ]
    if set(rmom_columns) == {"symbol", "day_ts", "residual_momentum"}:
        source = source.join(
            rmom.select(rmom_columns).unique(["symbol", "day_ts"]),
            on=["symbol", "day_ts"],
            how="left",
        )
    else:
        source = source.with_columns(pl.lit(None, dtype=pl.Float64).alias("residual_momentum"))
    rank_denom = pl.max_horizontal(pl.len().over("ts_ms") - 1, pl.lit(1))
    source = source.with_columns(
        (pl.col("ts_ms") + MS_PER_HOUR).alias("signal_bar_close_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("decision_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("feature_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("data_available_ts_ms"),
        ((pl.col("turnover_quote").rank().over("ts_ms") - 1) / rank_denom).alias("liquidity_rank"),
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / rank_denom).alias(
            "residual_momentum_rank"
        ),
        *[
            ((pl.col(feature).rank().over("ts_ms") - 1) / rank_denom).alias(f"_source_n_{feature}")
            for feature in present
        ],
    )
    source = source.with_columns(
        pl.mean_horizontal([pl.col(f"_source_n_{feature}") for feature in present]).alias(
            "source_composite"
        )
    )
    source = source.with_columns(
        (
            ((pl.col("source_composite").rank().over("ts_ms") - 1) * 10)
            // pl.len().over("ts_ms")
        )
        .clip(0, 9)
        .alias("source_decile")
    )
    keep = [
        "symbol",
        "ts_ms",
        "source_decile",
        "source_composite",
        "turnover_quote",
        "signal_bar_close_ts_ms",
        "decision_ts_ms",
        "feature_ts_ms",
        "data_available_ts_ms",
        "residual_momentum",
        "residual_momentum_rank",
        "liquidity_rank",
        *present,
        "rv_168h",
        "vov",
        "dist_low",
        "ret1",
        "prior6_ret1_max",
        "giveback_from_prior6_high",
        "turnover_spike_168h",
        "turnover_24h",
        "turnover_zscore_168h",
    ]
    selected = list(dict.fromkeys(column for column in keep if column in source.columns))
    return source.select(selected).sort(
        ["ts_ms", "symbol"]
    )


def _entry_event_expr(trigger: str, *, column_prefix: str = "") -> pl.Expr:
    """Confirmed-hour event gate.

    ``column_prefix`` evaluates the same trigger against a differently-prefixed
    copy of the feature columns; the live sleeve uses it to test the *previous*
    confirmed bar when reproducing the engine's fresh-entrant rule.
    """

    def column(name: str) -> pl.Expr:
        return pl.col(f"{column_prefix}{name}")

    if trigger == "none":
        return pl.lit(True)
    if trigger.startswith("fresh_pop"):
        threshold = float(trigger.removeprefix("fresh_pop")) / 100.0
        return (column("ret1") >= threshold) & (column("ret1") >= column("max_ret168") - 1e-12)
    if trigger.startswith("pop") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        pop_min = float(left.removeprefix("pop")) / 100.0
        gb_min = float(right) / 100.0
        return (
            (column("prior6_ret1_max") >= pop_min)
            & (column("ret1") <= 0.0)
            & (column("giveback_from_prior6_high") <= -gb_min)
        )
    if trigger.startswith("turn") and "_pop" in trigger:
        left, right = trigger.split("_pop", 1)
        turn_min = float(left.removeprefix("turn"))
        pop_min = float(right) / 100.0
        return (column("turnover_spike_168h") >= turn_min) & (column("ret1") >= pop_min)
    if trigger.startswith("turn") and "_gb" in trigger:
        left, right = trigger.split("_gb", 1)
        turn_min = float(left.removeprefix("turn"))
        gb_min = float(right) / 100.0
        return (
            (column("turnover_spike_168h") >= turn_min)
            & (column("prior6_ret1_max") >= 0.05)
            & (column("giveback_from_prior6_high") <= -gb_min)
        )
    raise ValueError(f"unknown entry_event_trigger {trigger!r}")


def compute_continuous_decile_panel(
    k: pl.DataFrame, rmom: pl.DataFrame, *, rmom_quantile: float = 0.5, start_ms: int = 0,
    feature_set: tuple[str, ...] = FEATURES,
    funnel_observer: DecisionFunnelObserver | None = None,
    funnel_venue: str = "unknown",
    funnel_signal_ts_ms: int | None = None,
) -> pl.DataFrame:
    """Shared feature -> composite -> decile pipeline, used by both the backtest
    panel and the live demo state.

    `k`: hourly klines [ts_ms, symbol, close, turnover_quote] on closed bars (the
    live caller appends a synthetic current bar per symbol with close = live
    ticker price). `rmom`: the daily residual-momentum table
    [symbol, day_ts, residual_momentum]. All features are trailing closed-bar
    windows and the rmom join is a causal day-floor lag1. Returns
    [symbol, ts_ms, decile, composite, turnover_quote]."""
    k = per_symbol_timeseries_features(k)
    if start_ms:
        k = k.filter(pl.col("ts_ms") >= start_ms)
    return cross_sectional_decile(
        k,
        rmom,
        rmom_quantile=rmom_quantile,
        feature_set=feature_set,
        funnel_observer=funnel_observer,
        funnel_venue=funnel_venue,
        funnel_signal_ts_ms=funnel_signal_ts_ms,
    )


def _fresh_entries(panel: pl.DataFrame, config: ContinuousEventConfig) -> pl.DataFrame:
    """Fresh target-decile entries on the liquid universe, ordered by signal time."""
    if panel.is_empty():
        return panel
    d = panel.filter(pl.col("decile") == config.decile).sort(["symbol", "ts_ms"])
    if config.entry_event_trigger != "none":
        d = d.filter(_entry_event_expr(config.entry_event_trigger))
    d = d.with_columns(
        ((pl.col("ts_ms") - pl.col("ts_ms").shift(1).over("symbol")) > MS_PER_HOUR).fill_null(True).alias("fresh")
    )
    d = d.filter(pl.col("fresh")).filter(pl.col("turnover_quote") >= config.liq_turnover_min)
    keep_cols = [
        "symbol",
        "ts_ms",
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


def _funding_admission_filter(
    entries: pl.DataFrame,
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: ContinuousEventConfig,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Admit fresh entries whose last settled funding print clears the floor.

    The rate is the last settlement at-or-before each entry's decision timestamp
    (signal-bar close, ``ts_ms + 1h``) -- the same backward as-of semantics as
    the cross-venue panel join. Entries with no settled print admit and are
    counted. Runs BEFORE ``_run_trades`` so crowding, cooldown, and capacity see
    only admitted candidates.
    """
    counters = {"checked": 0, "rejected": 0, "unknown_admitted": 0}
    if config.funding_min_at_entry is None or entries.is_empty():
        return entries, counters
    floor = float(config.funding_min_at_entry)
    keep: list[bool] = []
    for sym, sig_ts in entries.select("symbol", "ts_ms").iter_rows():
        counters["checked"] += 1
        cutoff = int(sig_ts) + MS_PER_HOUR
        rate: float | None = None
        series = (funding_lookup or {}).get(str(sym))
        if series is not None:
            idx = bisect.bisect_right(series["events_ts"], cutoff) - 1
            if idx >= 0:
                rate = float(series["events_rate"][idx])
        if rate is None:
            counters["unknown_admitted"] += 1
            keep.append(True)
        elif rate >= floor:
            keep.append(True)
        else:
            counters["rejected"] += 1
            keep.append(False)
    return entries.filter(pl.Series(keep)), counters


def _round_trip_bps(
    config: ContinuousEventConfig, turnover_quote: float, *, notional_weight: float | None = None
) -> float:
    """Capacity-aware round-trip cost in basis points."""
    base = 2.0 * (config.taker_fee_bps + config.spread_bps)
    weight = config.notional_weight if notional_weight is None else float(notional_weight)
    notional = max(weight, 0.0) * config.deploy_capital_usd
    adv = max(float(turnover_quote), 1.0)
    participation = notional / adv
    impact = config.impact_coef_bps * (participation ** config.impact_exponent)
    return base + 2.0 * impact


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


def _entry_vol(close_arr: "np.ndarray", entry_bar: int, window: int = 168, min_n: int = 48) -> float:
    """Trailing hourly return std ending at entry_bar (the per-name vol for risk-sizing)."""
    lo = max(0, entry_bar - window)
    seg = close_arr[lo:entry_bar + 1]
    if seg.size < min_n + 1:
        return 0.0
    rets = np.diff(seg) / seg[:-1]
    rets = rets[np.isfinite(rets)]
    return float(np.std(rets)) if rets.size >= min_n else 0.0


def _build_lifecycle_config(config: ContinuousEventConfig) -> TradeLifecycleConfig:
    """Translate active continuous settings into the shared lifecycle config."""
    return TradeLifecycleConfig(
        start_date=config.start_date, end_date=config.end_date,
        hold_days=max(1, round(config.hold_hours / 24)), take_profit_pct=max(config.take_profit_pct, 0.0),
        stop_loss_pct=max(config.stop_loss_pct, 0.0),
        side_mode="long_low_short_high",
    )


def _notional_weight(
    config: ContinuousEventConfig,
    close_arr: Any,
    entry_bar: int,
) -> float:
    """Active flat or inverse-vol entry weight."""
    if config.sizing_mode == "flat":
        return config.notional_weight
    if config.sizing_mode == "inverse_vol":
        rv = _entry_vol(close_arr, int(entry_bar))
        clamp = max(config.vol_weight_clamp, 1.0)
        mult = min(max(config.target_vol_per_name / rv, 1.0 / clamp), clamp) if rv > 0 else 1.0
        return config.notional_weight * mult
    raise ValueError(f"unsupported continuous sizing_mode {config.sizing_mode!r}")


@dataclass(slots=True)
class _ContinuousHistoricalOpen:
    """One accepted CONTINUOUS position advanced only by timestamped bars."""

    state: _IndexedTradeState
    bars: dict[str, Any]
    next_bar_index: int
    end_bar_index: int
    component_id: str
    last_mark_price: float


def _run_trades(
    entries: pl.DataFrame,
    symbol_bars: dict[str, Any],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: ContinuousEventConfig,
    btc_trend_daily: dict[int, float] | None = None,
    listing_ts_by_symbol: dict[str, int] | None = None,
    kernel_decision_sink: list[HistoricalTargetDecision] | None = None,
    kernel_session: HistoricalAccountSession | None = None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Run active candidates chronologically through sizing and account feedback."""
    if not np.isfinite(config.execution_leverage) or config.execution_leverage <= 0.0:
        raise ValueError("continuous execution_leverage must be finite and positive")
    if entries.is_empty():
        return _empty_trades(), {}
    lifecycle = _build_lifecycle_config(config)
    hold_ms = exact_duration_ms(hours=config.hold_hours)
    cooldown_ms = hold_ms
    delay_ms = exact_duration_ms(hours=1 + config.entry_delay_hours)
    age_min_ms = exact_duration_ms(days=config.age_days_min)
    crowding_on = config.entry_crowding_max_fresh > 0
    signal_counts: dict[int, int] = {}
    if crowding_on:
        for ts in entries["ts_ms"].to_list():
            ts_i = int(ts)
            signal_counts[ts_i] = signal_counts.get(ts_i, 0) + 1
    open_positions: dict[str, _ContinuousHistoricalOpen] = {}
    last_entry: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    btc_gate = config.btc_trend_gate
    if btc_gate not in ("off", "uptrend", "downtrend"):
        raise ValueError(
            f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {btc_gate!r}"
        )
    btc_lookback_days = int(config.btc_trend_lookback_days)
    if btc_gate != "off" and btc_lookback_days < 1:
        raise ValueError(f"btc_trend_lookback_days must be >= 1; got {btc_lookback_days}")
    skipped_capacity = skipped_cooldown = skipped_no_bar = skipped_gate = skipped_btc_trend = 0
    skipped_crowding = 0
    skipped_account_kernel = 0
    pending_entry_ts_ms: int | None = None
    pending_entry_decisions: list[HistoricalTargetDecision] = []
    pending_entry_position_keys: list[str] = []
    pending_prior_last_entry: dict[str, int | None] = {}
    last_advanced_ts_ms: int | None = None
    syms = entries["symbol"].to_list()
    tss = entries["ts_ms"].to_list()
    comps = entries["composite"].to_list()
    turns = entries["turnover_quote"].to_list()
    def _submit_kernel_decisions(
        decisions: list[HistoricalTargetDecision],
    ) -> tuple[bool, tuple[str, ...], bool]:
        return submit_historical_decisions(
            decisions,
            kernel_session=kernel_session,
            kernel_decision_sink=kernel_decision_sink,
            equity_usdt=config.deploy_capital_usd,
            batch_prefix="continuous-chronological",
            market_prices_for=lambda decision_symbols: {
                position.state.symbol: position.last_mark_price
                for position in open_positions.values()
                if position.state.symbol.upper() not in decision_symbols
            },
        )

    def _cancel_committed_entries_after_execution_rejection() -> None:
        assert kernel_session is not None
        neutralize_rejected_entry_decisions(
            pending_entry_decisions,
            source="continuous_events_execution_rejection_compensation",
            error_label="historical account kernel could not neutralize rejected entries",
            submit=_submit_kernel_decisions,
        )

    def _flush_pending_entries() -> None:
        nonlocal pending_entry_ts_ms, skipped_account_kernel
        if not pending_entry_decisions:
            return
        accepted, rejection_keys, target_committed = _submit_kernel_decisions(
            pending_entry_decisions
        )
        if not accepted:
            if target_committed:
                _cancel_committed_entries_after_execution_rejection()
            skipped_account_kernel += len(pending_entry_decisions)
            for position_key in pending_entry_position_keys:
                open_positions.pop(position_key, None)
            for symbol, prior in pending_prior_last_entry.items():
                if prior is None:
                    last_entry.pop(symbol, None)
                else:
                    last_entry[symbol] = prior
        pending_entry_ts_ms = None
        pending_entry_decisions.clear()
        pending_entry_position_keys.clear()
        pending_prior_last_entry.clear()

    def _materialize_open(position: _ContinuousHistoricalOpen) -> dict[str, Any]:
        return position.state.to_trade()

    def _exit_decision(
        position: _ContinuousHistoricalOpen,
        trade: dict[str, Any],
    ) -> HistoricalTargetDecision:
        exit_ts_ms = int(trade["exit_ts_ms"])
        return HistoricalTargetDecision(
            wall_ts_ns=exit_ts_ms * 1_000_000,
            reference_price=float(trade["exit_price"]),
            intent=component_target_intent(
                adapter_kind=SleeveAdapterKind.CONTINUOUS,
                action="exit",
                decision_ts_ms=exit_ts_ms,
                strategy_id=config.kernel_strategy_id,
                component_id=position.component_id,
                symbol=str(trade["symbol"]),
                signed_notional_usdt=0.0,
                leverage=config.execution_leverage,
                reason=str(trade["exit_reason"]),
                metadata={
                    "source": "continuous_events_chronological_target",
                    "owner_sleeve": "continuous",
                    "prior_trade_id": position.component_id,
                },
            ),
        )

    def _record_finalized(
        position: _ContinuousHistoricalOpen,
        trade: dict[str, Any],
    ) -> None:
        rows.append(trade)

    def _advance_positions(through_ts_ms: int) -> None:
        closed_keys: list[str] = []
        closed: list[tuple[str, _ContinuousHistoricalOpen, dict[str, Any]]] = []
        for key, position in sorted(
            open_positions.items(),
            key=lambda item: (item[1].state.entry_ts_ms, item[0]),
        ):
            state = position.state
            bars = position.bars
            while position.next_bar_index < position.end_bar_index and not state.closed:
                bar_index = position.next_bar_index
                bar_end_ts_ms = int(bars["bar_end_ts_ms"][bar_index])
                if bar_end_ts_ms > through_ts_ms:
                    break
                position.next_bar_index += 1
                position.last_mark_price = float(bars["close"][bar_index])
                state.on_bar(
                    high=float(bars["high"][bar_index]),
                    low=float(bars["low"][bar_index]),
                    close=float(bars["close"][bar_index]),
                    bar_end_ts_ms=bar_end_ts_ms,
                )
            if (
                not state.closed
                and position.next_bar_index >= position.end_bar_index
                and position.end_bar_index > 0
            ):
                last_index = position.end_bar_index - 1
                boundary_ts_ms = int(bars["bar_end_ts_ms"][last_index])
                # Every eligible bar is consumed. Keep the ``data_end`` exit at
                # the final observable bar but finalize it now, while account
                # decisions are still chronological: deferring it to the end of
                # the replay leaves a delisted symbol open for months and then
                # submits a stale exit after newer cycles.
                state.close_at_boundary(
                    close=float(bars["close"][last_index]),
                    bar_end_ts_ms=boundary_ts_ms,
                )
            if state.closed:
                closed_keys.append(key)
                closed.append((key, position, _materialize_open(position)))
        grouped: dict[int, list[tuple[_ContinuousHistoricalOpen, dict[str, Any]]]] = {}
        for _key, position, trade in closed:
            grouped.setdefault(int(trade["exit_ts_ms"]), []).append((position, trade))
        for _exit_ts, items in sorted(grouped.items()):
            if kernel_decision_sink is not None or kernel_session is not None:
                accepted, rejection_keys, _ = _submit_kernel_decisions([
                    _exit_decision(position, trade) for position, trade in items
                ])
                if not accepted:
                    raise RuntimeError(
                        "historical account kernel rejected a strategy exit batch: "
                        + ", ".join(rejection_keys)
                    )
            for position, trade in items:
                _record_finalized(position, trade)
        for key in closed_keys:
            del open_positions[key]

    for idx, (sym, sig_ts, comp, turn) in enumerate(zip(syms, tss, comps, turns)):
        order_submit_ts = int(sig_ts) + delay_ms
        if pending_entry_ts_ms is not None and pending_entry_ts_ms != order_submit_ts:
            _flush_pending_entries()
        if last_advanced_ts_ms != order_submit_ts:
            _advance_positions(order_submit_ts)
            last_advanced_ts_ms = order_submit_ts
        bars = symbol_bars.get(sym)
        if bars is None:
            skipped_no_bar += 1
            continue
        if crowding_on and signal_counts.get(int(sig_ts), 0) > config.entry_crowding_max_fresh:
            skipped_crowding += 1
            continue
        if sym in last_entry and order_submit_ts - last_entry[sym] < cooldown_ms:
            skipped_cooldown += 1
            continue
        if len(open_positions) >= config.max_active:
            skipped_capacity += 1
            continue
        order_submit_bar = bars["by_end"].get(order_submit_ts)
        if order_submit_bar is None:
            skipped_no_bar += 1
            continue
        close_arr = bars["close"]
        if age_min_ms > 0:
            listing_ts = (listing_ts_by_symbol or {}).get(sym)
            if listing_ts is None:
                listing_ts = int(bars["bar_end_ts_ms"][0])
            if (order_submit_ts - int(listing_ts)) < age_min_ms:
                skipped_gate += 1
                continue
        if btc_gate != "off":
            signal_day = (int(sig_ts) // MS_PER_DAY) * MS_PER_DAY
            trend = (btc_trend_daily or {}).get(signal_day)
            if trend is None:
                skipped_btc_trend += 1
                continue
            if btc_gate == "uptrend" and trend <= 0.0:
                skipped_btc_trend += 1
                continue
            if btc_gate == "downtrend" and trend > 0.0:
                skipped_btc_trend += 1
                continue
        entry_bar = int(order_submit_bar)
        entry_bar_end = order_submit_ts
        nw = _notional_weight(config, close_arr, entry_bar)
        planned_exit = entry_bar_end + hold_ms
        round_trip = _round_trip_bps(config, turn, notional_weight=nw)
        entry_price = float(close_arr[entry_bar])
        next_bar_index = bisect.bisect_right(bars["ends"], entry_bar_end)
        end_bar_index = bisect.bisect_right(bars["ends"], planned_exit)
        if entry_price <= 0.0 or next_bar_index >= end_bar_index:
            skipped_no_bar += 1
            continue
        component_id = f"{_iso_day(entry_bar_end)}-{config.side[0]}-{sym}"
        state = _IndexedTradeState(
            symbol=sym,
            side=config.side,
            score=float(comp) if comp is not None else 0.0,
            rank=int(config.decile),
            basket_id=_iso_day(entry_bar_end),
            signal_ts_ms=int(sig_ts),
            entry_ts_ms=entry_bar_end,
            entry_price=entry_price,
            planned_exit_ts_ms=planned_exit,
            notional_weight=nw,
            position_weight=1.0,
            config=lifecycle,
            round_trip_cost_bps=round_trip,
            stop_price=(
                None
                if lifecycle.stop_loss_pct <= 0.0
                else entry_price
                * (
                    1.0 - lifecycle.stop_loss_pct
                    if config.side == "long"
                    else 1.0 + lifecycle.stop_loss_pct
                )
            ),
            take_profit_price=(
                None
                if lifecycle.take_profit_pct <= 0.0
                else entry_price
                * (
                    1.0 + lifecycle.take_profit_pct
                    if config.side == "long"
                    else 1.0 - lifecycle.take_profit_pct
                )
            ),
            rank_lookup={},
            event_decay_threshold=0.0,
            funding_lookup=funding_lookup if config.use_funding else None,
            stop_fill_mode="bar_extreme_capped",
            stop_slippage_cap_pct=0.10,
        )
        entry_decision: HistoricalTargetDecision | None = None
        if kernel_decision_sink is not None or kernel_session is not None:
            strategy_id = config.kernel_strategy_id
            signed_notional = abs(nw) * config.deploy_capital_usd
            if config.side == "short":
                signed_notional *= -1.0
            entry_decision = HistoricalTargetDecision(
                wall_ts_ns=entry_bar_end * 1_000_000,
                reference_price=entry_price,
                intent=component_target_intent(
                    adapter_kind=SleeveAdapterKind.CONTINUOUS,
                    action="entry",
                    decision_ts_ms=entry_bar_end,
                    strategy_id=strategy_id,
                    component_id=component_id,
                    symbol=sym,
                    signed_notional_usdt=signed_notional,
                    leverage=config.execution_leverage,
                    reason="historical_continuous_entry",
                    metadata={
                        "source": "continuous_events_chronological_target",
                        "signal_ts_ms": int(sig_ts),
                        "signal_valid_until_ms": entry_bar_end + MS_PER_HOUR,
                        "take_profit_pct": float(lifecycle.take_profit_pct),
                        **(
                            {"stop_loss_pct": float(lifecycle.stop_loss_pct)}
                            if lifecycle.stop_loss_pct > 0.0
                            else {}
                        ),
                        "max_hold_duration_ms": hold_ms,
                        "notional_weight": nw,
                    },
                ),
            )
        position_key = f"{component_id}/{int(sig_ts)}/{idx}"
        open_positions[position_key] = _ContinuousHistoricalOpen(
            state=state,
            bars=bars,
            next_bar_index=next_bar_index,
            end_bar_index=end_bar_index,
            component_id=component_id,
            last_mark_price=entry_price,
        )
        if entry_decision is not None:
            if kernel_session is not None:
                if pending_entry_ts_ms is None:
                    pending_entry_ts_ms = entry_bar_end
                elif pending_entry_ts_ms != entry_bar_end:
                    raise RuntimeError("entry decision batching crossed a wall timestamp")
                pending_entry_decisions.append(entry_decision)
                pending_entry_position_keys.append(position_key)
                if sym not in pending_prior_last_entry:
                    pending_prior_last_entry[sym] = last_entry.get(sym)
            else:
                _submit_kernel_decisions([entry_decision])
        last_entry[sym] = order_submit_ts
    _flush_pending_entries()
    _advance_positions(2**63 - 1)
    skips = {
        "skipped_capacity": skipped_capacity,
        "skipped_cooldown": skipped_cooldown,
        "skipped_no_bar": skipped_no_bar,
        "skipped_gate": skipped_gate,
        "skipped_btc_trend": skipped_btc_trend,
        "skipped_crowding": skipped_crowding,
        "skipped_account_kernel": skipped_account_kernel,
    }
    if not rows:
        return _empty_trades(), skips
    return pl.DataFrame(rows).sort(["entry_ts_ms", "symbol", "trade_id"]), skips


def _additive_equity(trades: pl.DataFrame) -> pl.DataFrame:
    """Fixed-capital ADDITIVE daily equity (NOT compounding).

    For a capacity-capped book whose impact is sized off a FIXED deploy capital,
    additive PnL is the consistent accounting: compounding implies a growing book
    facing growing impact the fixed-capital model does not charge. `net_return`
    is already each trade's fraction-of-capital PnL, summed onto its exit day and
    then cumulative-summed. Schema matches build_equity_curve; equity = 1 + cum
    PnL, drawdown = equity - running-max (<=0, in return units)."""
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
    # Headline DD/MAR/Sharpe/return metrics come from the shared
    # `_daily_pnl_metrics`; only the additive-specific split is local.
    base = _daily_pnl_metrics(equity)
    if equity.is_empty():
        return {"n_trades": 0, **base}
    pnl = equity["basket_return"].to_list()
    split_ms = _date_str_to_ms(config.split_date)
    funding_modes = set(str(m) for m in trades["funding_mode"].to_list()) if "funding_mode" in trades.columns else set()
    fmode = "missing" if (not funding_modes or funding_modes == {"missing"}) else (
        "modeled" if funding_modes == {"modeled"} else "partial")
    # Compounding reference from the lifecycle accounting, not the headline.
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
    # Shared report convention: sample standard deviation, annualized over 365.25 days.
    sharpe = annualized_sharpe(pnl)
    return {"total_return": total, "annualized_return": ann, "max_drawdown": maxdd,
            "mar": (ann / abs(maxdd)) if abs(maxdd) > 1e-9 else None,
            "sharpe_like": sharpe, "worst_day_return": float(min(pnl)) if pnl else 0.0}


def _portfolio_mtm_equity(trades: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    """Daily portfolio MARK-TO-MARKET equity (additive, fixed-capital).

    `_additive_equity` books a trade's whole PnL on its exit day, so a day where
    25 still-open shorts all move against you shows nothing until they exit. This
    marks every OPEN position to the daily close, spreading gross PnL across the
    held days, with cost on the entry day and funding on the exit day, so
    concurrent correlated moves aggregate into a real portfolio drawdown. Gross
    daily marks telescope to the realized gross, so total return is unchanged and
    only the PATH (hence DD and Sharpe) differs.

    Do not calendar-fill this series: rebalance windows are defined over ledger
    rows and the hedge applies to every input row, so inserting flat days changes
    volatility scaling and fabricates hedge PnL at zero exposure. Flat-tail
    presentation belongs to the chart layer."""
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
    """CHART-ONLY flat-tail extension: append zero-return days through
    ``through_ts_ms`` so a book that goes flat near the data end renders to the
    boundary instead of truncating at the last exit. Never persist this shape;
    the stored mtm CSV keeps ledger-day rows (see `_portfolio_mtm_equity`)."""
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
        # The shared benchmark renderer still writes the standard chart.
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
    ax.text(0.99, 0.02, "DESCRIPTIVE HISTORICAL EQUITY", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=7, color="grey", style="italic")
    axd.fill_between(xs, [d * 100 for d in eq["drawdown"].to_list()], 0.0, color="#2c3e50", alpha=0.5)
    axd.set_ylabel("drawdown (%)")
    axd.grid(alpha=0.25)
    axd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _prepare_inputs(
    root: Path,
    config: ContinuousEventConfig,
) -> dict[str, Any]:
    start_ms, end_ms = _window_ms(config)
    panel = build_continuous_panel(root, config)
    entries = _fresh_entries(panel, config) if not panel.is_empty() else panel

    kname = _autodetect_dataset_names(root)["klines_dataset"]
    pad_fwd = exact_duration_ms(
        hours=config.hold_hours + config.entry_delay_hours + 4
    )
    btc_trend_lookback_days = max(int(config.btc_trend_lookback_days), 1)
    pad_back = exact_duration_ms(days=max(config.age_days_min, 0))
    if config.btc_trend_gate != "off":
        pad_back = max(pad_back, exact_duration_ms(days=btc_trend_lookback_days + 1))
    klines = _read_window(
        root, kname, start_ms=start_ms - pad_back, end_ms=end_ms + pad_fwd,
        columns=["ts_ms", "symbol", "open", "high", "low", "close"],
    )
    if config.exclude_symbols and not klines.is_empty():
        klines = klines.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    symbol_bars = _indexed_price_bars_by_symbol(klines) if not klines.is_empty() else {}

    funding_lookup = None
    if config.use_funding or config.funding_min_at_entry is not None:
        fname = _autodetect_dataset_names(root)["funding_dataset"]
        funding = _read_window(root, fname, start_ms=start_ms - 10 * MS_PER_DAY, end_ms=end_ms + pad_fwd)
        # Each distinct timestamp in a venue funding-history dataset is a
        # realized settlement; exact stamps preserve cadence changes during
        # stressed funding regimes.
        funding_lookup = _funding_lookup(funding)
    entries, funding_admission = _funding_admission_filter(entries, funding_lookup, config)

    btc_trend_daily = None
    if config.btc_trend_gate != "off" and not klines.is_empty():
        btc_trend_daily = _btc_trend_returns(klines, lookback_days=btc_trend_lookback_days)

    # Per-symbol PIT listing (first-ever bar under the root), read independently
    # of the run window so the age gate cannot infer listing from the clamped
    # window start.
    listing_ts_by_symbol = _listing_ts_by_symbol(root) if config.age_days_min > 0 else None

    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "panel": panel,
        "entries": entries,
        "klines": klines,
        "symbol_bars": symbol_bars,
        "funding_lookup": funding_lookup,
        "funding_admission": funding_admission,
        "btc_trend_daily": btc_trend_daily,
        "listing_ts_by_symbol": listing_ts_by_symbol,
    }


def run_continuous_equity_component(
    data_root: str | Path,
    *,
    config: ContinuousEventConfig,
    report_dir: str | Path,
) -> dict[str, Any]:
    """Run one active continuous historical component and write its artifacts."""
    root = Path(str(data_root)).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")
    out_dir = Path(str(report_dir)).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = _prepare_inputs(root, config)
    end_ms = int(inputs["end_ms"])
    entries = inputs["entries"]
    klines = inputs["klines"]
    symbol_bars = inputs["symbol_bars"]
    funding_lookup = inputs["funding_lookup"]
    funding_admission = inputs["funding_admission"]
    btc_trend_daily = inputs["btc_trend_daily"]
    listing_ts_by_symbol = inputs["listing_ts_by_symbol"]

    lifecycle_strategy_id = config.kernel_strategy_id
    replay_policy = AccountRiskPolicy(
        max_component_gross_notional_usdt=config.deploy_capital_usd * 10.0,
        max_account_gross_notional_usdt=config.deploy_capital_usd * 100.0,
        max_symbol_notional_usdt=config.deploy_capital_usd * 10.0,
        max_initial_margin_usdt=config.deploy_capital_usd * 100.0,
        max_leverage=config.execution_leverage,
    )
    execution_config = ExecutionTwinConfig(
        fee_bps=config.taker_fee_bps,
        latency=LatencyProfile(0, 0, 0),
        max_decision_age_ns=0,
    )
    lifecycle_root = out_dir / "common_kernel_execution"
    lifecycle_root.mkdir(parents=True, exist_ok=True)
    observed_ts_ns = max(
        1,
        min(
            (
                int(bars["bar_end_ts_ms"][0]) * 1_000_000
                for bars in symbol_bars.values()
                if len(bars.get("bar_end_ts_ms", ())) > 0
            ),
            default=1,
        ),
    )
    online_session = HistoricalAccountSession(
        lifecycle_root,
        account_id=lifecycle_strategy_id,
        risk_policy=replay_policy,
        instrument_rules=synthetic_historical_rules_for_symbols(
            list(symbol_bars),
            max_leverage=config.execution_leverage,
            observed_ts_ns=observed_ts_ns,
        ),
        execution_config=execution_config,
        id_seed=f"{lifecycle_strategy_id}:historical",
    )

    kernel_decisions: list[HistoricalTargetDecision] = []
    if not entries.is_empty() and symbol_bars:
        trades, skips = _run_trades(
            entries, symbol_bars, funding_lookup, config, btc_trend_daily,
            listing_ts_by_symbol=listing_ts_by_symbol,
            kernel_decision_sink=kernel_decisions,
            kernel_session=online_session,
        )
    else:
        trades, skips = _empty_trades(), {}

    equity = _additive_equity(trades)
    splits = _split_metrics(trades, config)
    mtm_equity = _portfolio_mtm_equity(trades, klines)
    mtm = _daily_pnl_metrics(mtm_equity)

    funding_mode = splits.get("full", {}).get("funding_mode", "missing")
    run_label = CONTINUOUS_EQUITY_EVIDENCE_LABEL

    if config.funding_min_at_entry is not None:
        skips = {**skips, "skipped_funding_admission": funding_admission["rejected"]}
    payload: dict[str, Any] = {
        "config": asdict(config),
        "config_hash": config.config_hash(),
        "data_root": str(root),
        "run_label": run_label,
        "n_fresh_entries": int(entries.height) if not entries.is_empty() else 0,
        "n_trades": int(trades.height),
        "skips": skips,
        "funding_admission": {
            "floor": config.funding_min_at_entry,
            **funding_admission,
        },
        "funding_mode": funding_mode,
        "metrics": splits,                 # realized-PnL-at-exit (additive, fixed-capital)
        "metrics_mtm": mtm,                # portfolio mark-to-market (correlated-DD aware)
    }

    lifecycle_receipt = verify_account_journal(lifecycle_root)
    lifecycle_receipt.update({
        "batches": len(online_session.outputs),
        "strategy_targets": len(kernel_decisions),
        "final_state_hash": online_session.final_state_hash,
        "evidence_label": "chronological_strategy_targets_through_common_account_kernel",
        "historical_strategy_runtime_is_sequential": True,
        "account_kernel_feedback_online": True,
        "same_timestamp_strategy_batching": True,
        "strategy_runtime_shared_across_environments": False,
        "market_tape_shared_across_environments": False,
        "venue_rule_parity": False,
    })

    payload["account_journal"] = lifecycle_receipt
    payload["cross_environment_strategy_parity"] = False

    # Empty component ledgers are valid inputs to the equity combiner.
    trades.write_csv(out_dir / "continuous_trades.csv")
    equity.write_csv(out_dir / "continuous_equity.csv")
    mtm_equity.write_csv(out_dir / "continuous_mtm_equity.csv")
    if not trades.is_empty():
        # Charts render an extended copy so a book that goes flat near the end
        # draws as a flat line through the data boundary; the CSVs above keep
        # the ledger-day shape.
        chart_boundary = end_ms - MS_PER_DAY
        if not klines.is_empty():
            chart_boundary = min(chart_boundary, (int(klines["ts_ms"].max()) // MS_PER_DAY) * MS_PER_DAY)
        mtm_chart = _extend_equity_flat_for_chart(mtm_equity, through_ts_ms=chart_boundary)
        _write_equity_png(
            mtm_chart, out_dir / "continuous_mtm_equity.png",
            title=(f"PORTFOLIO MARK-TO-MARKET — {config.side} D{config.decile} | hold {config.hold_hours}h "
                   f"| MAR {mtm.get('mar')} DD {abs(mtm.get('max_drawdown') or 0)*100:.1f}%  [DESCRIPTIVE]"),
        )
        _write_equity_png(
            equity, out_dir / "continuous_equity.png",
            title=(f"continuous fade {config.side} D{config.decile} | hold {config.hold_hours}h | "
                   f"liq>=${int(config.liq_turnover_min/1000)}k | "
                   f"MAR {splits.get('full', {}).get('mar')}  [DESCRIPTIVE]"),
        )
        # Use the shared strategy-vs-BTC renderer and monthly-return table.
        try:
            from .volume_events_charts import _write_equity_benchmark_chart
            _date = pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
            eq_dated = mtm_chart.with_columns(_date)
            btc_klines = (
                klines.filter(pl.col("symbol") == "BTCUSDT").with_columns(_date)
                if not klines.is_empty() else klines
            )
            # Trade counts by ENTRY month plus MTM monthly return. The shared
            # renderer's fallback counts daily marks (~30/month) instead of the
            # hundreds of trades a high-turnover book opens.
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
                    out_dir, equity=eq_dated, raw_klines=btc_klines, monthly=monthly_df,
                    png_name="continuous_equity_btc.png",
                        title=(f"Continuous-fade {config.side} D{config.decile} vs BTC | hold "
                               f"{config.hold_hours}h | MTM-MAR {mtm.get('mar')} "
                               f"DD {abs(mtm.get('max_drawdown') or 0)*100:.1f}%  [DESCRIPTIVE]"),
                )
        except Exception:  # noqa: BLE001 - chart failure must not fail the run
            pass
    (out_dir / "continuous_report.json").write_text(json.dumps(payload, indent=2, default=str))
    payload["report_dir"] = str(out_dir)

    return payload
