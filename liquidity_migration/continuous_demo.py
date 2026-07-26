"""CONTINUOUS signal planner and account-target producer.

The sleeve owns causal market features, selection, sizing, and desired component
targets. The account owner alone owns venue orders, fills, positions, P&L,
protection, and operator notifications. The backtest is a prior; forward demo
and paper evidence are separate claims.

The active ensemble enters from the confirmed-bar +1h path. The decile pipeline is the shared
`continuous_events.compute_continuous_decile_panel`.

The sole `continuous_ensemble_v2` profile uses inverse-vol component entry
sizing, a fill-anchored account-owned TP, and a fill-anchored 24h max hold.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence, cast

import numpy as np
import polars as pl

from ._common import (
    MS_PER_DAY,
    MS_PER_HOUR,
    coerce_int,
    exact_duration_ms,
    finite_float,
)
from .account_intent_client import publish_exit_first_target_requests
from .account_candidate_universe import (
    load_candidate_universe,
    require_scheduled_retirements_flat,
)
from .account_route import require_account_route
from .account_service import RequestedIntent, SleeveAdapterKind
from .account_strategy_state import (
    CanonicalReductionEvent,
    canonical_adverse_reduction_events,
    target_reservation_rows,
)
from .artifact_snapshot import read_stable_file
from .bybit_market_data import BybitMarketData
from .config import DEFAULT_EXCLUDED_SYMBOLS, ResearchConfig
from .continuous_btc_risk import (
    BTC_RISK_EVIDENCE_METADATA_KEY,
    BTC_RISK_MIN_PRIOR,
    CTRL_BTC_RISK_70_90_35_ID,
    BtcRiskLiveSizer,
    btc_context_by_day,
    normalize_btc_risk_decision_evidence,
)
from .continuous_cycle_status import (
    ContinuousCycleStatus,
    write_continuous_cycle_status,
)
from .continuous_events import (
    FEATURES,
    _btc_trend_returns,
    _entry_event_expr,
    compute_continuous_decile_panel,
    cross_sectional_decile,
    per_symbol_timeseries_features,
    require_stable_residual_momentum,
)
from .continuous_identity import continuous_trade_id
from .continuous_profile import (
    CONTINUOUS_PROFILE_ID,
    CONTINUOUS_RUNTIME_COMPONENTS as CONTINUOUS_COMPONENTS,
)
from .deterministic_serialization import canonical_json
from .event_demo_data import (
    _float,
    _kline_window,
    _download_recent_1h_klines,
    _price_lookup_from_tickers_and_klines,
    _resolve_cycle_universe,
    _utc_now_ms,
)
from .execution_environment import (
    ExecutionEnvironment,
    account_id_for_environment,
    execution_environment,
)
from .storage import exclusive_file_lock, write_dataset
from .strategy_planning import (
    account_owner_equity_or_error,
    sleeve_planning_snapshot,
    suppress_target_intents,
)
from .strategy_targets import component_target_intent, exit_target_intents
from .strategy_funnel import DecisionFunnelObserver
from .strategy_target_replay import PublishedTargetCyclePayload

CONTINUOUS_STRATEGY_ID = "continuous_fade_v2"
CONTINUOUS_V2_PROFILE = CONTINUOUS_PROFILE_ID
BTC_TREND_SYMBOL = "BTCUSDT"
CONTINUOUS_DEMO_PROFILES = (CONTINUOUS_V2_PROFILE,)
CONTINUOUS_ENTRY_OBSERVABILITY_SCHEMA_VERSION = 2
CONTINUOUS_FEATURE_IDENTITY_SCHEMA_VERSION = 1
CONTINUOUS_RMOM_IDENTITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ResidualMomentumSnapshot:
    """Descriptor-bound RMOM table plus the exact source-file identity."""

    table: pl.DataFrame
    source_path: str
    source_sha256: str
    source_size_bytes: int
    source_mtime_ns: int


@dataclass(frozen=True, slots=True)
class ContinuousEntrySelectionObservation:
    """Counterfactual selection facts that never grant entry authority."""

    candidates: tuple[dict[str, Any], ...]
    funnel_rows: tuple[dict[str, Any], ...]
    qualified_opportunities: tuple[dict[str, str], ...]
    admitted_opportunities: tuple[dict[str, str], ...]
    selection_rejections: tuple[dict[str, str], ...]
    skipped_same_signal_reentry: int
    observed_same_signal_reentry: int
    observed_entry_cooldown: int = 0
    observed_crowding: int = 0
    funding_admission_rejected: int = 0
    funding_admission_unknown_admitted: int = 0
    observer_error: str = ""


@dataclass(frozen=True, slots=True)
class ContinuousDemoCycleConfig:
    """Continuous demo/paper target-producer configuration."""

    # --- code-defined active selection ---
    decile: int = 9  # short the top composite decile
    # rmom-LOW gate: keep the lowest-residual-momentum third within each ts.
    rmom_quantile: float = 0.33
    feature_set: tuple[str, ...] = FEATURES
    liq_turnover_min: float = 500_000.0  # liquid gate: signal-bar hourly turnover_quote (USD)
    # --- live state window ---
    lookback_days: int = 45  # confirmed-1h history pulled from the store for the features
    # --- execution / book ---
    max_active: int = 25
    max_new_entries_per_cycle: int = 5
    max_hold_hours: int = 48  # force-exit cap if a name never leaves the decile
    # Research parity: the engine that produced the active profile's equity
    # evidence skips any fresh entry within hold_ms of the symbol's last entry
    # (cooldown_ms = hold_ms) and skips ALL of a component's fresh signals when
    # more than entry_crowding_max_fresh share one signal_ts.  The live sleeve
    # mirrors both; the validated record contains no faster re-entries and no
    # crowded-hour books.
    entry_reentry_cooldown_enabled: bool = True
    entry_crowding_max_fresh: int = 2  # 0 disables the crowding gate
    # The active profile selects from a confirmed close and enters one hour later.
    entry_confirm_delay_hours: int = 1
    entry_event_trigger: str = "none"  # opt-in confirmed-hour event gate: none | fresh_pop25 | popX_gbY | ...
    # The live profile is the uptrend-gated object. Keep the CLI/config
    # default aligned with the runner + systemd units; explicit overrides remain
    # available for diagnostics.
    btc_trend_gate: str = "uptrend"  # off | uptrend | downtrend; causal prior-30d BTC return gate.
    btc_trend_lookback_days: int = 30
    # Portfolio circuit breaker (correlated-squeeze defense): PAUSE new entries when the sleeve has had
    # >= entry_pause_after_adverse_exits adverse covers (stop_approach / failed_fade / any net-negative
    # cover — the footprint of a market-wide alt melt-up squeezing many shorts at once) within the last
    # entry_pause_window_minutes. Stateless (recomputed from the ledger each cycle), so the pause lifts
    # automatically as the cluster ages out of the window. NEVER adds risk — it only stops opening fresh
    # shorts into a squeeze. The current w24/n8 setting is a deliberate
    # de-risking choice. Set entry_pause_after_adverse_exits=0 to disable.
    entry_pause_after_adverse_exits: int = 8
    entry_pause_window_minutes: int = 1440
    entry_leverage: float = 2.0
    per_position_notional_pct_equity: float = 2.0  # base % before component, inverse-vol and rebalance scales
    sizing_mode: str = "flat"  # "flat" | "inverse_vol" (target_vol_per_name / rv_168h)
    target_vol_per_name: float = 0.02  # inverse-vol per-name hourly-vol target
    vol_weight_clamp: float = 3.0  # inverse-vol multiplier clamp [1/clamp, clamp]
    # Stateful accepted-decision BTC-risk entry-size overlay.
    entry_btc_risk_sizing_enabled: bool = False
    entry_btc_risk_arm_id: str = CTRL_BTC_RISK_70_90_35_ID
    entry_btc_risk_low: float = 0.70
    entry_btc_risk_high: float = 0.90
    entry_btc_risk_tail_mult: float = 0.35
    entry_btc_risk_min_prior: int = BTC_RISK_MIN_PRIOR
    # Explicit demo/paper scale multiplier, recorded in configs and ledgers.
    notional_multiplier: float = 1.0
    # SHA-256 of the shared operational profile when runtime sizing came from
    # that profile. Empty is retained for isolated diagnostics/tests.
    operational_profile_sha256: str = ""
    workers: int = 8
    # 0/0 keeps the full resolved universe; the signal's liquidity gate filters it.
    universe_rank_end: int = 0
    universe_max_symbols: int = 0
    universe_min_turnover_24h: float = 0.0
    # --- environment / wiring ---
    # No default is intentional. Runtime callers must name exactly one target
    # owner; the producer never carries an order-submission boolean.
    execution_environment: str = ""
    # Demo and paper both publish immutable DesiredTarget batches; only the
    # selected account service may mutate venue or canonical state.
    account_intent_inbox_root: str | None = None
    # Canonical accepted-target journal used as the planning read model.  It is
    # inseparable from the inbox route to prevent split-brain open positions.
    account_execution_root: str | None = None
    # Optional frozen candidate population, enforced before signal selection in
    # demo and paper alike.
    candidate_universe_file: str = ""
    strategy_profile: str = CONTINUOUS_V2_PROFILE
    # --- continuous_ensemble_v2 active component book ---
    # (name, entry_event_trigger|"none", age_days_min, take_profit_pct, weight,
    # stop_loss_pct, funding_min_at_entry|None). Non-empty => the cycle selects
    # entries PER COMPONENT (each with its own event trigger, age floor,
    # settled-funding admission floor, account-owned TP and declared wide stop
    # backstop) and sizes each entry by weight x the base per-position notional.
    ensemble_components: tuple[tuple[str, str, int, float, float, float, float | None], ...] = (
        CONTINUOUS_COMPONENTS
    )
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS
    # --- WS kline stream (same shape as the other sleeves) ---
    ws_klines_enabled: bool = True
    # Follow ANOTHER root's flushed WS kline snapshot (read-only) instead of running a
    # second WS pool — the paper shadow co-located with the demo sleeve sets this to the
    # demo root so both sleeves share one market-data plane AND decide on identical
    # signal inputs: the rmom gate is read from the followed root too (it is derived
    # purely from that root's klines). Empty = run the sleeve's own pool (default).
    klines_follow_root: str = ""
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = 45
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


def continuous_cycles_dataset(config: ContinuousDemoCycleConfig) -> str:
    """Cycle-heartbeat dataset for the continuous demo or paper planner."""
    if execution_environment(config.execution_environment) is ExecutionEnvironment.PAPER:
        return "continuous_fade_paper_cycles"
    return "continuous_fade_demo_cycles"


def _continuous_base_notional_pct_equity(config: ContinuousDemoCycleConfig) -> float:
    """Effective pre-component entry notional as a percentage of equity.

    ``notional_multiplier`` is pure exposure scale.  Exchange
    ``entry_leverage`` only changes initial margin; it does not change order
    quantity, so the two controls must remain explicit and independent.
    """
    return float(config.per_position_notional_pct_equity) * float(config.notional_multiplier)


def build_live_continuous_state(
    klines_recent: pl.DataFrame,
    current_prices: dict[str, float],
    rmom: pl.DataFrame,
    *,
    now_ts_ms: int,
    config: ContinuousDemoCycleConfig,
) -> pl.DataFrame:
    """Live cross-sectional decile at `now`, using confirmed 1h bars + the live ticker price.

    `klines_recent`: confirmed hourly bars from the WS kline store (cols incl. ts_ms, symbol, close,
    turnover_quote). `current_prices`: live last/mark price per symbol from the ticker cache. `rmom`:
    the daily residual-momentum table renamed to [symbol, day_ts, residual_momentum].

    Appends a synthetic in-progress ("current") bar per symbol at the current hour slot with
    close = live price (turnover carried from the last confirmed bar for the liquid gate), runs the
    SHARED verified decile pipeline, and returns the latest-bar rows:
    [symbol, decile, composite, turnover_quote]. This is the live analog of one backtest timestamp."""
    if klines_recent.is_empty() or not current_prices:
        return _empty_live_state()
    cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
    k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if k.is_empty():
        return _empty_live_state()
    last_turn = k.sort("ts_ms").group_by("symbol").agg(pl.col("turnover_quote").last().alias("lt"))
    prices = pl.DataFrame(
        {"symbol": list(current_prices.keys()), "close": [float(v) for v in current_prices.values()]}
    ).filter(pl.col("close") > 0)
    cur = (
        prices.join(last_turn, on="symbol", how="inner")  # only symbols with confirmed history
        .with_columns(pl.lit(cur_ts).alias("ts_ms"), pl.col("lt").alias("turnover_quote"))
        .select("ts_ms", "symbol", "close", "turnover_quote")
    )
    if cur.is_empty():
        return _empty_live_state()
    combined = pl.concat([k, cur], how="vertical_relaxed")
    panel = compute_continuous_decile_panel(
        combined,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
        feature_set=config.feature_set,
    )
    return _select_live_state_columns(panel.filter(pl.col("ts_ms") == cur_ts))


def build_confirmed_entry_state(
    klines_recent: pl.DataFrame,
    rmom: pl.DataFrame,
    *,
    now_ts_ms: int,
    config: ContinuousDemoCycleConfig,
    funnel_observer: DecisionFunnelObserver | None = None,
) -> pl.DataFrame:
    """Return the entry decile from the configured confirmed deciding bar.

    No synthetic in-progress bar is included. Entries use this state; exits use
    the tick-updated state.
    """
    delay = max(1, config.entry_confirm_delay_hours)
    if klines_recent.is_empty():
        return _empty_live_state()
    cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
    # deciding bar starts at ts_d, closes at ts_d+1h; entry is +delay h after that close, so the most
    # recent eligible deciding bar has ts_d = cur_ts - (1+delay)h (its +delay-after-close <= cur_ts <= now).
    deciding_ts = cur_ts - exact_duration_ms(hours=1 + delay)
    k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
    if config.exclude_symbols:
        k = k.filter(~pl.col("symbol").is_in(list(config.exclude_symbols)))
    if k.is_empty():
        return _empty_live_state()
    panel = compute_continuous_decile_panel(
        k,
        rmom,
        rmom_quantile=config.rmom_quantile,
        start_ms=0,
        feature_set=config.feature_set,
        funnel_observer=funnel_observer,
        funnel_venue="bybit",
        funnel_signal_ts_ms=deciding_ts,
    )
    return _select_live_state_columns(panel.filter(pl.col("ts_ms") == deciding_ts))


def _empty_live_state() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": pl.Series([], dtype=pl.String),
            "decile": pl.Series([], dtype=pl.Int64),
            "composite": pl.Series([], dtype=pl.Float64),
            "turnover_quote": pl.Series([], dtype=pl.Float64),
            "rv_168h": pl.Series([], dtype=pl.Float64),
            "ret1": pl.Series([], dtype=pl.Float64),
            "max_ret168": pl.Series([], dtype=pl.Float64),
            "prior6_ret1_max": pl.Series([], dtype=pl.Float64),
            "giveback_from_prior6_high": pl.Series([], dtype=pl.Float64),
            "turnover_spike_168h": pl.Series([], dtype=pl.Float64),
        }
    )


def _select_live_state_columns(panel: pl.DataFrame) -> pl.DataFrame:
    base = ["symbol", "decile", "composite", "turnover_quote"]
    event_cols = [
        "rv_168h",
        "ret1",
        "max_ret168",
        "prior6_ret1_max",
        "giveback_from_prior6_high",
        "turnover_spike_168h",
    ]
    return panel.select(base + [c for c in event_cols if c in panel.columns])


def _sha256_canonical(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _entry_feature_contract(config: ContinuousDemoCycleConfig) -> dict[str, Any]:
    """Return the explicit signal/admission contract behind the live state."""

    return {
        "schema_version": CONTINUOUS_FEATURE_IDENTITY_SCHEMA_VERSION,
        "strategy_profile": config.strategy_profile,
        "decile": int(config.decile),
        "rmom_quantile": float(config.rmom_quantile),
        "feature_set": list(config.feature_set),
        "liq_turnover_min": float(config.liq_turnover_min),
        "lookback_days": int(config.lookback_days),
        "entry_confirm_delay_hours": int(config.entry_confirm_delay_hours),
        "ensemble_components": [list(component) for component in config.ensemble_components],
        "exclude_symbols": list(config.exclude_symbols),
        "max_active": int(config.max_active),
        "max_new_entries_per_cycle": int(config.max_new_entries_per_cycle),
        "btc_trend_gate": config.btc_trend_gate,
        "btc_trend_lookback_days": int(config.btc_trend_lookback_days),
    }


def _entry_feature_identity_payload_fields(
    entry_state: pl.DataFrame,
    *,
    signal_ts_ms: int,
    config: ContinuousDemoCycleConfig,
) -> dict[str, Any]:
    """Hash the exact cross-sectional feature state consumed by admission."""

    columns = list(entry_state.columns)
    ordered = entry_state.sort("symbol") if "symbol" in columns else entry_state
    material = {
        "schema_version": CONTINUOUS_FEATURE_IDENTITY_SCHEMA_VERSION,
        "kind": "continuous_entry_feature_state",
        "signal_ts_ms": int(signal_ts_ms),
        "columns": columns,
        "rows": ordered.select(columns).to_dicts() if columns else [],
    }
    contract = _entry_feature_contract(config)
    return {
        "entry_feature_identity_schema_version": CONTINUOUS_FEATURE_IDENTITY_SCHEMA_VERSION,
        "entry_feature_signal_ts_ms": int(signal_ts_ms),
        "entry_feature_state_rows": entry_state.height,
        "entry_feature_state_columns_json": json.dumps(columns, separators=(",", ":")),
        "entry_feature_state_sha256": _sha256_canonical(material),
        "entry_feature_contract_sha256": _sha256_canonical(contract),
    }


def _rmom_identity_payload_fields(
    snapshot: ResidualMomentumSnapshot | None,
    *,
    signal_day_ts_ms: int,
) -> dict[str, Any]:
    """Persist both the full stable RMOM source and exact signal-day slice."""

    base: dict[str, Any] = {
        "rmom_identity_schema_version": CONTINUOUS_RMOM_IDENTITY_SCHEMA_VERSION,
        "rmom_source_path": "",
        "rmom_source_sha256": "",
        "rmom_source_size_bytes": 0,
        "rmom_source_mtime_ns": 0,
        "rmom_signal_day_ts_ms": int(signal_day_ts_ms),
        "rmom_signal_day_rows": 0,
        "rmom_signal_day_sha256": "",
    }
    if snapshot is None:
        return base
    signal_rows = (
        snapshot.table.filter(pl.col("day_ts") == int(signal_day_ts_ms))
        .select("symbol", "day_ts", "residual_momentum")
        .sort("symbol")
    )
    material = {
        "schema_version": CONTINUOUS_RMOM_IDENTITY_SCHEMA_VERSION,
        "kind": "continuous_signal_day_residual_momentum",
        "signal_day_ts_ms": int(signal_day_ts_ms),
        "columns": list(signal_rows.columns),
        "rows": signal_rows.to_dicts(),
    }
    base.update(
        {
            "rmom_source_path": snapshot.source_path,
            "rmom_source_sha256": snapshot.source_sha256,
            "rmom_source_size_bytes": snapshot.source_size_bytes,
            "rmom_source_mtime_ns": snapshot.source_mtime_ns,
            "rmom_signal_day_rows": signal_rows.height,
            "rmom_signal_day_sha256": _sha256_canonical(material),
        }
    )
    return base


class LivePanelCache:
    """Tier 2 — within-hour static-feature cache for the live continuous decile.

    `build_live_continuous_state` re-runs the FULL trailing-window feature pipeline (rv_168h, vov,
    dist_low, xsret7/3) over the whole ~45d×N-symbol history on every wake. But those windows are
    trailing aggregates over CONFIRMED hourly bars — they do not change between bar closes; only the
    in-progress ("current") bar's live-price term moves intra-hour. This cache computes the heavy
    per-symbol confirmed-bar part ONCE per bar close (`_refresh`) and, on each wake, computes only
    the current bar's features from the cached carry + the live price and re-ranks the cross-section
    (`_live_panel`). That turns the per-wake cost from a full O(N×history) recompute into an
    O(N) carry-update + O(N log N) re-rank, which is what makes sub-minute triggers affordable.

    EQUIVALENCE (the repo's `np.allclose` gate): the cross-sectional half is the SHARED
    `cross_sectional_decile`, and the per-symbol current-bar feature values are computed to reproduce
    exactly what `per_symbol_timeseries_features` produces for the appended synthetic bar — so
    `state(...)` is `np.allclose` on `composite` and exact on `decile`/membership vs
    `build_live_continuous_state(...)`. `tests/test_liquidity_migration_continuous_demo.py` pins it
    (incl. a young-symbol and a gapped-symbol case). The full recompute remains the always-available
    fallback in the cycle, so a cache bug degrades to "slower", never "wrong".
    """

    __slots__ = ("_rmom_quantile", "_feature_set", "_exclude", "_cur_ts", "_sig", "_carry")

    def __init__(
        self,
        *,
        rmom_quantile: float = 0.5,
        feature_set: tuple[str, ...] = FEATURES,
        exclude_symbols: tuple[str, ...] = (),
    ) -> None:
        self._rmom_quantile = float(rmom_quantile)
        self._feature_set = tuple(feature_set)
        self._exclude = tuple(exclude_symbols)
        self._cur_ts: int | None = None
        self._sig: tuple[int, int, int, int] | None = None
        self._carry: dict[str, dict[str, Any]] = {}

    def _confirmed_signature(
        self,
        klines_recent: pl.DataFrame,
        cur_ts: int,
        config: ContinuousDemoCycleConfig,
    ) -> tuple[int, int, int, int]:
        """A cheap, collision-resistant content fingerprint of the confirmed bars feeding the carry.

        It catches NOT JUST the hour rolling over but also a confirmed bar being BACKFILLED or
        RE-DELIVERED with corrected values mid-hour (Bybit re-pushes a just-closed bar after late
        trades; ``KlineStore.add_bar`` overwrites it in place) — without that, the carry would silently
        go stale within the hour.

        pit-engine-3: the previous fingerprint was ``(row_count, max_ts, close_sum, turnover_sum)``. A
        value-preserving multi-symbol backfill that conserved BOTH float sums (e.g. one symbol's close
        corrected +X while another's is -X, same count/max_ts) left the fingerprint unchanged → the
        refresh was skipped and the live decile reranked off a stale per-symbol carry, with no exception
        so the cycle's full-recompute fallback never fired. We now fold an order-independent hash of
        each row's ``(symbol, ts_ms, close, turnover_quote)`` tuple into the signature (a wrapping SUM
        and a wrapping SUM-OF-SQUARES of polars' per-row 64-bit hashes — two different-degree
        reductions), so a per-symbol value change moves the fingerprint even when the cross-sectional
        float sums are preserved. Order-independent because the bar set has no inherent order. Still an
        O(N) scan (no rolling), far cheaper than ``_refresh``."""
        exclude = self._exclude or config.exclude_symbols
        lf = klines_recent.lazy().select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
        if exclude:
            lf = lf.filter(~pl.col("symbol").is_in(list(exclude)))
        confirmed = lf.collect()
        if confirmed.is_empty():
            return (0, 0, 0, 0)
        row_hashes = confirmed.select("symbol", "ts_ms", "close", "turnover_quote").hash_rows()
        row_count = confirmed.height
        max_ts = coerce_int(confirmed.get_column("ts_ms").max())
        mod = 1 << 64
        # Two order-independent reductions of the per-row 64-bit hashes: a linear SUM and a
        # quadratic SUM-OF-SQUARES. Both move when any row's (symbol, ts_ms, close, turnover_quote)
        # changes; using two different-degree polynomials means a backfill whose per-row hash deltas
        # happened to cancel in the linear sum (mod 2^64) is overwhelmingly unlikely to ALSO cancel in
        # the quadratic sum, so the pair is collision-resistant where a plain float-sum was not.
        hashes = row_hashes.to_list()
        hash_sum = sum(hashes) % mod
        hash_sq_sum = sum((h * h) % mod for h in hashes) % mod
        return (row_count, max_ts, hash_sum, hash_sq_sum)

    def state(
        self,
        klines_recent: pl.DataFrame,
        current_prices: dict[str, float],
        rmom: pl.DataFrame,
        *,
        now_ts_ms: int,
        config: ContinuousDemoCycleConfig,
    ) -> pl.DataFrame:
        """Drop-in replacement for `build_live_continuous_state`: re-run the heavy `_refresh` only when
        the confirmed-bar content changes (hour roll-over OR a backfilled/re-delivered bar — detected
        by the cheap signature), then compute the live decile from the carry + live prices."""
        if klines_recent.is_empty() or not current_prices:
            return _empty_live_state()
        cur_ts = (int(now_ts_ms) // MS_PER_HOUR) * MS_PER_HOUR
        sig = self._confirmed_signature(klines_recent, cur_ts, config)
        if self._cur_ts != cur_ts or self._sig != sig or not self._carry:
            self._refresh(klines_recent, cur_ts, config)
            self._sig = sig
        return self._live_panel(current_prices, rmom, cur_ts, config)

    def _refresh(self, klines_recent: pl.DataFrame, cur_ts: int, config: ContinuousDemoCycleConfig) -> None:
        """HEAVY (once per bar close): compute the per-symbol confirmed-bar carry needed to
        reconstruct the synthetic current bar's features from a live price. `cur_ts` is the current
        hour slot; the confirmed bars are everything strictly before it (matches
        `build_live_continuous_state`)."""
        self._cur_ts = cur_ts
        self._carry = {}
        k = klines_recent.select("ts_ms", "symbol", "close", "turnover_quote").filter(pl.col("ts_ms") < cur_ts)
        exclude = self._exclude or config.exclude_symbols
        if exclude:
            k = k.filter(~pl.col("symbol").is_in(list(exclude)))
        if k.is_empty():
            return
        feats = per_symbol_timeseries_features(k).sort(["symbol", "ts_ms"])
        grouped = feats.group_by("symbol", maintain_order=True).agg(
            pl.col("close").alias("_close"),
            pl.col("ret1").alias("_ret1"),
            pl.col("rv_168h").alias("_rv"),
            pl.col("turnover_quote").last().alias("_turn"),
        )
        for row in grouped.iter_rows(named=True):
            closes = np.asarray(row["_close"], dtype=float)
            n = int(closes.size)
            if n == 0:
                continue
            ret1 = np.array([np.nan if v is None else float(v) for v in row["_ret1"]], dtype=float)
            rv = np.array([np.nan if v is None else float(v) for v in row["_rv"]], dtype=float)
            self._carry[str(row["symbol"])] = {
                "n": n,
                "prev_close": float(closes[-1]),
                # positional shift(72)/shift(168) at the synthetic bar (index n) == confirmed[n-72/-168]
                "close_72": float(closes[n - 72]) if n >= 72 else None,
                "close_168": float(closes[n - 168]) if n >= 168 else None,
                # rolling 720 window at the synthetic bar = last <=719 confirmed closes + the live bar
                "close_win": closes[max(0, n - 719) : n],
                # rolling 168 ret1 window = last <=167 confirmed ret1 + the live ret1
                "ret1_win": ret1[max(0, n - 167) : n],
                # rolling 720 rv_168h window = last <=719 confirmed rv_168h + the live rv_168h
                "rv_win": rv[max(0, n - 719) : n],
                "turnover": float(row["_turn"]) if row["_turn"] is not None else 0.0,
            }

    def _live_panel(
        self,
        current_prices: dict[str, float],
        rmom: pl.DataFrame,
        cur_ts: int,
        config: ContinuousDemoCycleConfig,
    ) -> pl.DataFrame:
        """CHEAP (per wake): per-symbol current-bar features from cached carry + live price, then the
        SHARED cross-sectional decile. Mirrors the synthetic-bar arithmetic of
        `per_symbol_timeseries_features` exactly (rolling_std ddof=1, min_samples = non-null count,
        the `max>min` dist_low guard, positional return offsets)."""
        rows: list[dict[str, Any]] = []
        for symbol, c in self._carry.items():
            price = current_prices.get(symbol)
            if price is None or price <= 0.0:
                continue
            price = float(price)
            prev = c["prev_close"]
            if prev <= 0.0:
                continue
            ret1_cur = price / prev - 1.0
            # rv_168h: rolling_std(window=168, min_samples=48) over the confirmed tail + the live ret1
            win = c["ret1_win"]
            win = win[~np.isnan(win)] if win.size else win
            rv_vals = np.concatenate([win, np.array([ret1_cur])]) if win.size else np.array([ret1_cur])
            rv_cur: float | None = float(np.std(rv_vals, ddof=1)) if rv_vals.size >= 48 else None
            max_ret168_cur: float | None = float(np.max(rv_vals)) if rv_vals.size >= 48 else None
            # min720/max720: rolling 720 min/max over confirmed close tail + the live close
            cw = c["close_win"]
            count = int(cw.size) + 1
            if count >= 168:
                mn: float | None = min(float(cw.min()), price) if cw.size else price
                mx: float | None = max(float(cw.max()), price) if cw.size else price
            else:
                mn = mx = None
            dist_low = (price - mn) / (mx - mn) if (mn is not None and mx is not None and mx > mn) else None
            # vov: rolling_std(window=720, min_samples=168) over confirmed rv tail + the live rv
            if rv_cur is None:
                vov_cur: float | None = None
            else:
                rw = c["rv_win"]
                rw = rw[~np.isnan(rw)] if rw.size else rw
                vov_vals = np.concatenate([rw, np.array([rv_cur])]) if rw.size else np.array([rv_cur])
                vov_cur = float(np.std(vov_vals, ddof=1)) if vov_vals.size >= 168 else None
            ret72_cur = (price / c["close_72"] - 1.0) if c["close_72"] else None
            ret168_cur = (price / c["close_168"] - 1.0) if c["close_168"] else None
            rows.append(
                {
                    "symbol": symbol,
                    "ts_ms": cur_ts,
                    "turnover_quote": c["turnover"],
                    "rv_168h": rv_cur,
                    "max_ret168": max_ret168_cur,
                    "vov": vov_cur,
                    "dist_low": dist_low,
                    "ret72": ret72_cur,
                    "ret168": ret168_cur,
                }
            )
        if not rows:
            return _empty_live_state()
        frame = pl.DataFrame(
            rows,
            schema={
                "symbol": pl.String,
                "ts_ms": pl.Int64,
                "turnover_quote": pl.Float64,
                "rv_168h": pl.Float64,
                "max_ret168": pl.Float64,
                "vov": pl.Float64,
                "dist_low": pl.Float64,
                "ret72": pl.Float64,
                "ret168": pl.Float64,
            },
        )
        panel = cross_sectional_decile(
            frame,
            rmom,
            rmom_quantile=self._rmom_quantile,
            feature_set=self._feature_set,
        )
        return panel.select("symbol", "decile", "composite", "turnover_quote", "rv_168h")


def select_continuous_entries(
    live_state: pl.DataFrame,
    *,
    held_symbols: set[str],
    cooldown_symbols: set[str],
    open_count: int,
    config: ContinuousDemoCycleConfig,
    eligible_symbols: set[str] | None = None,
    prior_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fresh shorts: names currently in the top decile + liquid + not held + not in cooldown (+ age-
    eligible if `eligible_symbols` is supplied — the inherited fresh-listing-squeezer floor).

    Holding a position IS the 'still in the spell' state, so 'not held' implements fresh-entry; the
    exit (left-decile) is planned separately. Ranked by composite desc, capped by capacity.

    `prior_symbols` (already traded this signal window) must be excluded BEFORE the capacity
    truncation: a closed same-signal trade otherwise re-occupies a top composite slot every cycle
    and silently starves fresh names ranked just below the cutoff."""
    if live_state.is_empty():
        return []
    room = max(0, config.max_active - open_count)
    if room <= 0:
        return []
    cand = live_state.filter(
        (pl.col("decile") == config.decile) & (pl.col("turnover_quote") >= config.liq_turnover_min)
    )
    if config.entry_event_trigger != "none":
        cand = cand.filter(_entry_event_expr(config.entry_event_trigger))
    if held_symbols:
        cand = cand.filter(~pl.col("symbol").is_in(list(held_symbols)))
    if cooldown_symbols:
        cand = cand.filter(~pl.col("symbol").is_in(list(cooldown_symbols)))
    if prior_symbols:
        cand = cand.filter(~pl.col("symbol").is_in(list(prior_symbols)))
    if eligible_symbols is not None:
        cand = cand.filter(pl.col("symbol").is_in(list(eligible_symbols)))
    return cand.sort("composite", descending=True).head(min(config.max_new_entries_per_cycle, room)).to_dicts()


def plan_continuous_exits(
    open_trades: list[dict[str, Any]],
    *,
    now_ms: int,
    config: ContinuousDemoCycleConfig,
) -> list[dict[str, Any]]:
    """Return the sole active sleeve exit: the fill-anchored max-hold target.

    ``canonical_strategy_trade_rows`` owns the lifecycle clock. A row without
    an attributable first fill has no valid deadline and is deliberately held
    out of this planner instead of falling back to TARGET acceptance time.
    Take-profit exits are account-owned and evaluated from execution anchors.
    """
    if not open_trades or config.max_hold_hours <= 0:
        return []
    duration_ms = exact_duration_ms(hours=config.max_hold_hours)
    exits: list[dict[str, Any]] = []
    for trade in open_trades:
        entry_ts_ms = int(trade.get("entry_ts_ms") or 0)
        if entry_ts_ms <= 0:
            continue
        max_hold_deadline_ts_ms = int(trade.get("max_hold_deadline_ts_ms") or entry_ts_ms + duration_ms)
        if now_ms < max_hold_deadline_ts_ms:
            continue
        exits.append({**trade, "exit_reason": "max_hold", "exit_trigger_ts_ms": now_ms})
    return exits


def _recent_adverse_reduction_count(
    adverse_events: Sequence[CanonicalReductionEvent],
    *,
    now_ms: int,
    window_minutes: int,
) -> int:
    """Count journal-confirmed adverse reduction batches in the causal window.

    Each ``pnl_key`` is one account/symbol reduction fact. Component rows are
    ownership context only, so a multi-component close is never counted more
    than once. Local receive time is the causal availability clock.
    """

    if window_minutes <= 0:
        return 0
    cutoff_ns = (int(now_ms) - exact_duration_ms(minutes=window_minutes)) * 1_000_000
    now_ns = int(now_ms) * 1_000_000
    return sum(cutoff_ns <= int(event.local_receive_ts_ns) <= now_ns for event in adverse_events)


def entry_circuit_breaker_tripped(
    adverse_events: Sequence[CanonicalReductionEvent],
    *,
    now_ms: int,
    config: ContinuousDemoCycleConfig,
) -> tuple[bool, int]:
    """Pause entries after enough distinct adverse account reductions."""
    if config.entry_pause_after_adverse_exits <= 0:
        return False, 0
    count = _recent_adverse_reduction_count(
        adverse_events,
        now_ms=now_ms,
        window_minutes=config.entry_pause_window_minutes,
    )
    return count >= config.entry_pause_after_adverse_exits, count


_ALL_COMPONENTS = "*"


def _component_suffix(
    trade_id: str,
    *,
    strategy_id: str,
    symbol: str,
    signal_ts_ms: int | None,
) -> str:
    """Component name embedded in a canonical trade id; '' when unattributable."""

    if signal_ts_ms is None:
        return ""
    base = f"{continuous_trade_id(strategy_id, symbol, int(signal_ts_ms))}-"
    if trade_id.startswith(base) and len(trade_id) > len(base):
        return trade_id[len(base):]
    return ""


def _component_scope_map(
    frame: pl.DataFrame,
    *,
    strategy_id: str,
) -> dict[str, set[str]]:
    """symbol -> owning components; the ``*`` wildcard blocks every component.

    The research books run independently per component, so admission guards
    must not let one component's lifecycle blanket-block its siblings.  A row
    whose trade id cannot be attributed to exactly one component (legacy id,
    foreign producer) fails safe to the wildcard: unattributable lifecycles
    can only narrow admission, never widen it.
    """

    scope: dict[str, set[str]] = {}
    if frame.is_empty() or "symbol" not in frame.columns:
        return scope
    has_identity = {"trade_id", "signal_ts_ms"} <= set(frame.columns)
    columns = ["symbol"]
    if has_identity:
        columns += ["trade_id", "signal_ts_ms"]
    if "strategy_id" in frame.columns:
        columns.append("strategy_id")
    for row in frame.select(columns).to_dicts():
        symbol = str(row["symbol"])
        component = ""
        if has_identity:
            signal_value = row.get("signal_ts_ms")
            component = _component_suffix(
                str(row.get("trade_id") or ""),
                strategy_id=str(row.get("strategy_id") or strategy_id),
                symbol=symbol,
                signal_ts_ms=(None if signal_value is None else int(signal_value)),
            )
        scope.setdefault(symbol, set()).add(component or _ALL_COMPONENTS)
    return scope


def _symbols_blocking_component(
    component: str,
    scope: dict[str, set[str]],
) -> set[str]:
    return {
        symbol
        for symbol, components in scope.items()
        if _ALL_COMPONENTS in components or component in components
    }


def _continuous_cooldown_components(
    all_trades: pl.DataFrame,
    *,
    now_ms: int,
    strategy_id: str,
    config: ContinuousDemoCycleConfig,
) -> dict[str, set[str]]:
    """(symbol -> components) inside the entry-anchored re-entry cooldown.

    Research parity: the engine pins ``cooldown_ms = hold_ms`` per symbol per
    component book, so the validated equity evidence contains no re-entry
    within ``max_hold_hours`` of that component's last entry — including after
    a fast take-profit close — while a sibling book may still trade the
    symbol.  Derived from canonical entry timestamps; rows without an
    attributed entry carry no cooldown clock (they are reserved instead).
    """

    if not config.entry_reentry_cooldown_enabled or config.max_hold_hours <= 0:
        return {}
    if all_trades.is_empty() or not {"symbol", "entry_ts_ms"} <= set(all_trades.columns):
        return {}
    frame = all_trades
    if "strategy_id" in frame.columns:
        frame = frame.filter(pl.col("strategy_id") == strategy_id)
    cutoff = int(now_ms) - exact_duration_ms(hours=config.max_hold_hours)
    frame = frame.filter(
        pl.col("entry_ts_ms").cast(pl.Int64, strict=False).fill_null(0) > cutoff
    )
    return _component_scope_map(frame, strategy_id=strategy_id)


def _prior_continuous_signal_entries(
    all_trades: pl.DataFrame,
    *,
    signal_ts: int,
    strategy_id: str,
) -> dict[str, set[str]]:
    """(symbol -> components) that already own an entry for this signal window."""

    if all_trades.is_empty() or not {"symbol", "signal_ts_ms"} <= set(all_trades.columns):
        return {}
    frame = all_trades.filter(pl.col("signal_ts_ms") == signal_ts)
    if "strategy_id" in frame.columns:
        frame = frame.filter(pl.col("strategy_id") == strategy_id)
    return _component_scope_map(frame, strategy_id=strategy_id)


def _continuous_entry_candidates_with_signal_metadata(
    picks: list[dict[str, Any]],
    *,
    signal_ts: int,
    strategy_id: str,
    price_by_symbol: dict[str, float],
    prior_symbols: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Attach signal metadata and enforce one entry per component/signal window.

    ``prior_symbols`` must already be scoped to the admitting component: the
    guard is per component book (research parity), so a sibling component
    completing a capacity-truncated stack is legal and must not be skipped
    here.
    """
    candidates: list[dict[str, Any]] = []
    skipped_same_signal_reentry = 0
    for c in picks:
        sym = str(c["symbol"])
        if sym in prior_symbols:
            skipped_same_signal_reentry += 1
            continue
        candidates.append(
            {
                **c,
                "signal_ts_ms": signal_ts,
                "live_price": price_by_symbol.get(sym, 0.0),
                "trade_id": continuous_trade_id(strategy_id, sym, signal_ts),
            }
        )
    return candidates, skipped_same_signal_reentry


class _CycleSettledFundingProbe:
    """Last settled funding print at-or-before one cycle's admission cutoff.

    Reads the venue funding-HISTORY endpoint (realized settlements only — never
    the ticker's predicted next rate) and memoizes per symbol for the cycle.
    Any fetch error, missing client method, or absent history returns ``None``:
    the admission fails open and the cycle counts the unknown admit, matching
    the engine's unknown-admits semantics.
    """

    def __init__(
        self,
        market_client: Any,
        *,
        cutoff_ts_ms: int,
        lookback_ms: int = 3 * MS_PER_DAY,
    ) -> None:
        self._client = market_client
        self._cutoff_ts_ms = int(cutoff_ts_ms)
        self._lookback_ms = int(lookback_ms)
        self._rates: dict[str, float | None] = {}

    def _fetch(self, symbol: str) -> float | None:
        fetch = getattr(self._client, "get_funding_history", None)
        if fetch is None:
            return None
        try:
            rows = fetch(symbol, self._cutoff_ts_ms - self._lookback_ms, self._cutoff_ts_ms)
        except Exception:  # noqa: BLE001 - admission fails open and is counted
            _logger.exception("settled-funding probe failed for %s; admitting as unknown", symbol)
            return None
        best_ts: int | None = None
        best_rate: float | None = None
        for row in rows or []:
            try:
                ts = int(row["fundingRateTimestamp"])
                rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts <= self._cutoff_ts_ms and math.isfinite(rate) and (best_ts is None or ts > best_ts):
                best_ts = ts
                best_rate = rate
        return best_rate

    def rates_for(self, symbols: Sequence[str]) -> dict[str, float | None]:
        for symbol in symbols:
            key = str(symbol)
            if key not in self._rates:
                self._rates[key] = self._fetch(key)
        return {str(symbol): self._rates[str(symbol)] for symbol in symbols}


def _entry_stage_frames(
    entry_state: pl.DataFrame,
    *,
    component_config: ContinuousDemoCycleConfig,
    eligible_symbols: set[str] | None,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    if entry_state.is_empty():
        empty = entry_state
        return empty, empty, empty, empty
    d9 = entry_state.filter(pl.col("decile") == component_config.decile)
    liquid = d9.filter(pl.col("turnover_quote") >= component_config.liq_turnover_min)
    event = liquid
    if component_config.entry_event_trigger != "none":
        event = event.filter(_entry_event_expr(component_config.entry_event_trigger))
    age = event
    if eligible_symbols is not None:
        age = age.filter(pl.col("symbol").is_in(sorted(eligible_symbols)))
    return d9, liquid, event, age


def _component_funnel_state(
    entry_state: pl.DataFrame,
    *,
    universe: pl.DataFrame,
    klines: pl.DataFrame,
    reserved_symbols: set[str],
    cooldown_symbols: set[str],
    prior_symbols: set[str],
    trigger: str,
    age_days: int,
    funding_min_at_entry: float | None,
    settled_funding_probe: "_CycleSettledFundingProbe | None",
    now_ms: int,
    config: ContinuousDemoCycleConfig,
) -> tuple[
    ContinuousDemoCycleConfig,
    set[str] | None,
    pl.DataFrame,
    pl.DataFrame,
    pl.DataFrame,
    pl.DataFrame,
    pl.DataFrame,
    pl.DataFrame,
    pl.DataFrame,
    int,
    int,
    dict[str, float | None],
    int,
    int,
]:
    component_config = replace(config, entry_event_trigger=trigger)
    eligible = _continuous_age_eligible_symbols(
        universe,
        klines,
        age_days_min=age_days,
        now_ms=now_ms,
    )
    d9, liquid, event, age = _entry_stage_frames(
        entry_state,
        component_config=component_config,
        eligible_symbols=eligible,
    )
    # Settled-funding admission runs before the lifecycle guards so the
    # crowding count below sees only admitted candidates — the engine applies
    # the same filter on its fresh-entries frame before its crowd counts.
    funded = age
    funding_rates: dict[str, float | None] = {}
    funding_rejected = 0
    funding_unknown_admitted = 0
    if funding_min_at_entry is not None and not age.is_empty():
        symbols = [str(symbol) for symbol in age["symbol"].to_list()]
        if settled_funding_probe is not None:
            funding_rates = settled_funding_probe.rates_for(sorted(set(symbols)))
        floor = float(funding_min_at_entry)
        admitted: list[str] = []
        for symbol in symbols:
            rate = funding_rates.get(symbol)
            if rate is None:
                funding_unknown_admitted += 1
                admitted.append(symbol)
            elif rate >= floor:
                admitted.append(symbol)
            else:
                funding_rejected += 1
        if funding_rejected:
            funded = age.filter(pl.col("symbol").is_in(admitted))
    fresh = funded
    if not fresh.is_empty() and reserved_symbols:
        fresh = fresh.filter(~pl.col("symbol").is_in(sorted(reserved_symbols)))
    reserved_count = funded.height - fresh.height
    cooled = fresh
    if not cooled.is_empty() and cooldown_symbols:
        cooled = cooled.filter(~pl.col("symbol").is_in(sorted(cooldown_symbols)))
    cooldown_count = fresh.height - cooled.height
    same_signal = (
        cooled.filter(pl.col("symbol").is_in(sorted(prior_symbols)))
        if not cooled.is_empty() and prior_symbols
        else cooled.head(0)
    )
    available = (
        cooled.filter(~pl.col("symbol").is_in(sorted(prior_symbols)))
        if not cooled.is_empty() and prior_symbols
        else cooled
    )
    if not available.is_empty():
        available = available.sort("composite", descending=True)
    return (
        component_config,
        eligible,
        d9,
        liquid,
        event,
        age,
        funded,
        same_signal,
        available,
        reserved_count,
        cooldown_count,
        funding_rates,
        funding_rejected,
        funding_unknown_admitted,
    )


def _observe_continuous_component_selection(
    entry_state: pl.DataFrame,
    *,
    universe: pl.DataFrame,
    klines: pl.DataFrame,
    reserved_components: dict[str, set[str]],
    cooldown_components: dict[str, set[str]],
    reservations_count: int,
    entry_capacity: int,
    all_trades: pl.DataFrame,
    signal_ts: int,
    strategy_id: str,
    price_by_symbol: dict[str, float],
    now_ms: int,
    config: ContinuousDemoCycleConfig,
    active_entries_enabled: bool,
    settled_funding_probe: "_CycleSettledFundingProbe | None" = None,
) -> ContinuousEntrySelectionObservation:
    """Evaluate the entry funnel without bypassing any admission control.

    The candidate loop intentionally calls the same selector and metadata
    helper as the historical active path.  Continuing after capacity is full is
    telemetry-only; those later rows never enter ``candidates``.

    Reservation, cooldown, and same-signal guards are scoped per component
    (research parity: independent component books), so a sibling component can
    complete a capacity-truncated stack in a later cycle of the same window.
    """

    candidates: list[dict[str, Any]] = []
    funnel_rows: list[dict[str, Any]] = []
    qualified: list[dict[str, str]] = []
    admitted: list[dict[str, str]] = []
    selection_rejections: list[dict[str, str]] = []
    skipped_same_signal_reentry = 0
    observed_same_signal_reentry = 0
    observed_entry_cooldown = 0
    observed_crowding = 0
    funding_admission_rejected = 0
    funding_admission_unknown_admitted = 0
    observer_errors: list[str] = []
    prior_entries = _prior_continuous_signal_entries(
        all_trades,
        signal_ts=signal_ts,
        strategy_id=strategy_id,
    )

    for (
        component,
        trigger,
        age_days,
        take_profit_pct,
        component_weight,
        stop_loss_pct,
        funding_min_at_entry,
    ) in config.ensemble_components:
        component_is_authoritative = active_entries_enabled and len(candidates) < entry_capacity
        reserved_symbols = _symbols_blocking_component(component, reserved_components)
        cooldown_symbols = _symbols_blocking_component(component, cooldown_components)
        prior_symbols = _symbols_blocking_component(component, prior_entries)
        try:
            (
                component_config,
                eligible,
                d9,
                liquid,
                event,
                age,
                funded,
                same_signal,
                available,
                reserved_count,
                cooldown_count,
                funding_rates,
                funding_rejected,
                funding_unknown,
            ) = _component_funnel_state(
                entry_state,
                universe=universe,
                klines=klines,
                reserved_symbols=reserved_symbols,
                cooldown_symbols=cooldown_symbols,
                prior_symbols=prior_symbols,
                trigger=trigger,
                age_days=age_days,
                funding_min_at_entry=funding_min_at_entry,
                settled_funding_probe=settled_funding_probe,
                now_ms=now_ms,
                config=config,
            )
        except Exception as exc:
            if component_is_authoritative:
                raise
            observer_errors.append(f"{component}:{type(exc).__name__}:{exc}"[:300])
            funnel_rows.append(
                {
                    "component": component,
                    "d9": 0,
                    "liquidity": 0,
                    "event": 0,
                    "age": 0,
                    "funding": 0,
                    "available": 0,
                    "capacity": 0,
                    "reserved": 0,
                    "same_signal_reentry": 0,
                }
            )
            continue
        observed_same_signal_reentry += same_signal.height
        observed_entry_cooldown += cooldown_count
        funding_admission_rejected += funding_rejected
        funding_admission_unknown_admitted += funding_unknown
        # Research parity (entry_crowding_max_fresh): more fresh qualifying
        # signals than the cap at one signal_ts skips the component's ENTIRE
        # stack for the window, exactly like the equity-evidence engine — the
        # engine counts crowding on its funding-admitted fresh entries, so the
        # funded frame is the counting base here.
        component_crowded = 0 < config.entry_crowding_max_fresh < funded.height
        if component_crowded:
            observed_crowding += funded.height
        for row in funded.to_dicts():
            symbol = str(row["symbol"])
            opportunity = {"component": component, "symbol": symbol}
            qualified.append(opportunity)
            if component_crowded:
                selection_rejections.append(
                    {**opportunity, "first_rejection_reason": "crowding"}
                )
            elif symbol in reserved_symbols:
                selection_rejections.append(
                    {**opportunity, "first_rejection_reason": "already_reserved"}
                )
            elif symbol in cooldown_symbols:
                selection_rejections.append(
                    {**opportunity, "first_rejection_reason": "entry_cooldown"}
                )
            elif symbol in prior_symbols:
                selection_rejections.append(
                    {**opportunity, "first_rejection_reason": "same_signal_reentry"}
                )

        component_capacity_count = 0
        if not component_crowded and len(candidates) < entry_capacity:
            picks_eligible = eligible
            if funding_min_at_entry is not None:
                # The selector re-derives from entry_state, so the settled-funding
                # admission must constrain it the same way the funnel was cut.
                picks_eligible = {str(symbol) for symbol in funded["symbol"].to_list()}
            picks = select_continuous_entries(
                entry_state,
                held_symbols=reserved_symbols,
                cooldown_symbols=reserved_symbols | cooldown_symbols,
                open_count=reservations_count + len(candidates),
                config=component_config,
                eligible_symbols=picks_eligible,
                prior_symbols=prior_symbols,
            )
            component_candidates, skipped = _continuous_entry_candidates_with_signal_metadata(
                picks,
                signal_ts=signal_ts,
                strategy_id=strategy_id,
                price_by_symbol=price_by_symbol,
                prior_symbols=prior_symbols,
            )
            skipped_same_signal_reentry += skipped
            for candidate in component_candidates:
                if len(candidates) >= entry_capacity:
                    break
                symbol = str(candidate["symbol"])
                candidates.append(
                    {
                        **candidate,
                        "component": component,
                        "component_weight": component_weight,
                        "take_profit_pct": take_profit_pct,
                        "stop_loss_pct": stop_loss_pct,
                        "trade_id": f"{candidate['trade_id']}-{component}",
                        **(
                            {
                                "funding_min_at_entry": float(funding_min_at_entry),
                                "funding_rate_at_admission": funding_rates.get(symbol),
                                "funding_admission_unknown": funding_rates.get(symbol) is None,
                            }
                            if funding_min_at_entry is not None
                            else {}
                        ),
                    }
                )
                admitted.append({"component": component, "symbol": symbol})
                component_capacity_count += 1
        funnel_rows.append(
            {
                "component": component,
                "d9": d9.height,
                "liquidity": liquid.height,
                "event": event.height,
                "age": age.height,
                "funding": funded.height,
                "available": available.height,
                "capacity": component_capacity_count,
                "reserved": reserved_count,
                "same_signal_reentry": same_signal.height,
            }
        )

    return ContinuousEntrySelectionObservation(
        candidates=tuple(candidates),
        funnel_rows=tuple(funnel_rows),
        qualified_opportunities=tuple(qualified),
        admitted_opportunities=tuple(admitted),
        selection_rejections=tuple(selection_rejections),
        skipped_same_signal_reentry=skipped_same_signal_reentry,
        observed_same_signal_reentry=observed_same_signal_reentry,
        observed_entry_cooldown=observed_entry_cooldown,
        observed_crowding=observed_crowding,
        funding_admission_rejected=funding_admission_rejected,
        funding_admission_unknown_admitted=funding_admission_unknown_admitted,
        observer_error=" · ".join(observer_errors)[:500],
    )


def _preselection_entry_gate_reason(
    *,
    entry_health_ok: bool,
    entry_paused: bool,
    entry_capacity: int,
    btc_trend_gate_allows_entry: bool,
) -> str:
    """Mirror the existing active gate order exactly."""

    if not entry_health_ok:
        return "account_execution_health"
    if entry_paused:
        return "entry_pause"
    if entry_capacity <= 0:
        return "capacity"
    if not btc_trend_gate_allows_entry:
        return "btc_trend_gate"
    return ""


def _qualified_block_reasons(
    observation: ContinuousEntrySelectionObservation,
    *,
    preselection_reason: str,
    btc_risk_reason: str,
) -> dict[tuple[str, str], str]:
    admitted = {(row["component"], row["symbol"]) for row in observation.admitted_opportunities}
    reasons = {
        (row["component"], row["symbol"]): row["first_rejection_reason"]
        for row in observation.selection_rejections
    }
    for row in observation.qualified_opportunities:
        key = (row["component"], row["symbol"])
        if preselection_reason:
            reasons[key] = preselection_reason
        elif key in reasons:
            continue
        elif key not in admitted:
            reasons[key] = "capacity"
        elif btc_risk_reason:
            reasons[key] = btc_risk_reason
    return reasons


def _blocked_rows_from_reasons(
    observation: ContinuousEntrySelectionObservation,
    reasons: dict[tuple[str, str], str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for opportunity in observation.qualified_opportunities:
        key = (opportunity["component"], opportunity["symbol"])
        reason = reasons.get(key, "")
        if not reason or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "component": key[0],
                "symbol": key[1],
                "first_rejection_reason": reason,
            }
        )
    return rows


def _first_entry_rejection_reason(
    observation: ContinuousEntrySelectionObservation,
    *,
    preselection_reason: str,
    btc_risk_reason: str,
    raw_entry_intent_count: int,
    unresolved_suppressions: int,
    terminal_suppressions: int,
    publication_error_count: int,
    published_entry_count: int,
    blocked_rows: Sequence[dict[str, str]],
) -> str:
    if observation.observer_error and not observation.funnel_rows:
        return preselection_reason or "entry_observer_error"
    if preselection_reason:
        # A cycle-level gate (health, pause, capacity, BTC trend) disabled
        # entries before any admission ran.  _qualified_block_reasons names the
        # same gate for every qualified symbol; both channels must agree or an
        # operator chases funnel-stage plumbing that never ran.
        return preselection_reason
    totals = {
        field: sum(int(row[field]) for row in observation.funnel_rows)
        for field in ("d9", "liquidity", "event", "age", "available", "capacity", "reserved", "same_signal_reentry")
    }
    if totals["d9"] == 0:
        return "d9"
    if totals["liquidity"] == 0:
        return "liquidity"
    if totals["event"] == 0:
        return "event"
    if totals["age"] == 0:
        return "age"
    if totals["available"] == 0:
        if observation.observed_crowding > 0:
            return "crowding"
        if totals["reserved"] > 0:
            return "already_reserved"
        if observation.observed_entry_cooldown > 0:
            return "entry_cooldown"
        if totals["same_signal_reentry"] > 0:
            return "same_signal_reentry"
        return "capacity"
    if totals["capacity"] == 0:
        if observation.observed_crowding > 0:
            return "crowding"
        if observation.skipped_same_signal_reentry > 0:
            return "same_signal_reentry"
        return "capacity"
    if btc_risk_reason:
        return btc_risk_reason
    if raw_entry_intent_count == 0:
        return "target_intent_validation"
    if unresolved_suppressions > 0 and published_entry_count == 0:
        return "unresolved_account_target"
    if terminal_suppressions > 0 and published_entry_count == 0:
        return "terminal_entry_attempt"
    if publication_error_count > 0 and published_entry_count == 0:
        return "target_publication"
    if blocked_rows:
        return blocked_rows[0]["first_rejection_reason"]
    return ""


def continuous_strategy_id(config: ContinuousDemoCycleConfig) -> str:
    environment = execution_environment(config.execution_environment)
    return CONTINUOUS_STRATEGY_ID + ("_paper" if environment is ExecutionEnvironment.PAPER else "")


def continuous_managed_strategy_ids(config: ContinuousDemoCycleConfig) -> tuple[str, ...]:
    suffix = "_paper" if execution_environment(config.execution_environment) is ExecutionEnvironment.PAPER else ""
    return (CONTINUOUS_STRATEGY_ID + suffix,)


def apply_continuous_demo_profile(config: ContinuousDemoCycleConfig) -> ContinuousDemoCycleConfig:
    """Resolve the active demo/paper profile into explicit knobs.

    It uses the shared code-defined component book (single funding-gated
    turn3_pop3 cell since 2026-07-26), inverse-vol sizing, TP12, and a
    24-hour maximum hold.
    """
    if config.strategy_profile != CONTINUOUS_V2_PROFILE:
        return config
    return replace(
        config,
        rmom_quantile=0.25,
        feature_set=("max_ret168",),
        liq_turnover_min=500_000.0,
        max_hold_hours=24,
        entry_confirm_delay_hours=1,
        entry_event_trigger="none",
        # btc_trend_gate is NOT pinned here: it is the single-source-of-truth
        # CLI/env knob (`--btc-trend-gate` / `BTC_TREND_GATE`), so the deploy
        # layer controls it (units pin `uptrend`; runner defaults `uptrend`).
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        # After 50 prior accepted decisions, multiply all
        # component entries for a `(symbol, signal_ts)` by 0.35 when the causal
        # V0 BTC-risk score is in [0.70, 0.90).
        entry_btc_risk_sizing_enabled=True,
        entry_btc_risk_arm_id=CTRL_BTC_RISK_70_90_35_ID,
        entry_btc_risk_low=0.70,
        entry_btc_risk_high=0.90,
        entry_btc_risk_tail_mult=0.35,
        entry_btc_risk_min_prior=BTC_RISK_MIN_PRIOR,
        ensemble_components=CONTINUOUS_COMPONENTS,
    )


def _btc_trend_gate_value(
    klines: pl.DataFrame,
    *,
    signal_ts_ms: int,
    config: ContinuousDemoCycleConfig | None = None,
    trend_lookup: dict[int, float] | None = None,
) -> float | None:
    cfg = config or ContinuousDemoCycleConfig()
    lookup = (
        trend_lookup
        if trend_lookup is not None
        else _btc_trend_returns(
            klines,
            lookback_days=max(int(cfg.btc_trend_lookback_days), 1),
        )
    )
    signal_day = (int(signal_ts_ms) // MS_PER_DAY) * MS_PER_DAY
    return lookup.get(signal_day)


def _btc_trend_gate_allows_value(gate: str, trend: float | None) -> bool:
    if gate == "off":
        return True
    if gate not in ("uptrend", "downtrend"):
        raise ValueError(f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {gate!r}")
    if trend is None:
        return False
    return trend > 0.0 if gate == "uptrend" else trend <= 0.0


def _btc_rows_from_klines(klines: pl.DataFrame) -> pl.DataFrame:
    if klines.is_empty() or "symbol" not in klines.columns:
        return pl.DataFrame()
    return klines.filter(pl.col("symbol") == BTC_TREND_SYMBOL)


def _btc_trend_kline_stats(klines: pl.DataFrame) -> dict[str, int]:
    btc = _btc_rows_from_klines(klines)
    if btc.is_empty() or "ts_ms" not in btc.columns:
        return {"btc_rows": 0, "btc_max_ts_ms": 0}
    return {
        "btc_rows": btc.height,
        "btc_max_ts_ms": int(btc.select(pl.col("ts_ms").max()).item() or 0),
    }


def _load_btc_trend_gate_klines(
    base_klines: pl.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
    config: ResearchConfig,
    market_client: Any | None,
    cache_root: Path | None,
    kline_store: Any | None,
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Return BTC-only klines for the regime gate without altering the tradable universe."""
    stats: dict[str, int] = {
        "cache_rows": 0,
        "cache_symbols": 0,
        "fetch_symbols": 0,
        "fetched_rows": 0,
        "output_rows": 0,
        "store_rows": 0,
        "store_symbols": 0,
        "store_max_ts_ms": 0,
        "error": 0,
    }
    try:
        btc_klines, loaded_stats = _download_recent_1h_klines(
            [BTC_TREND_SYMBOL],
            start_ms=start_ms,
            end_ms=end_ms,
            config=config,
            workers=1,
            market_client=market_client,
            cache_root=cache_root,
            kline_store=kline_store,
            write_compact_cache=False,
        )
        stats.update({str(k): int(v) for k, v in loaded_stats.items()})
    except Exception:  # noqa: BLE001 - the gate must fail closed, not crash the cycle
        _logger.exception("BTC trend gate kline refresh failed; falling back to already-loaded klines")
        stats["error"] = 1
        btc_klines = _btc_rows_from_klines(base_klines)
    if btc_klines.is_empty():
        btc_klines = _btc_rows_from_klines(base_klines)
    stats.update(_btc_trend_kline_stats(btc_klines))
    return btc_klines, stats


def _continuous_vol_weight_multiplier(config: ContinuousDemoCycleConfig, entry_vol: Any) -> float:
    if config.sizing_mode == "flat":
        return 1.0
    if config.sizing_mode != "inverse_vol":
        raise ValueError(f"unknown continuous sizing_mode {config.sizing_mode!r}")
    rv = _finite_or_none(entry_vol)
    if rv is None or rv <= 0.0:
        return 1.0
    clamp = max(float(config.vol_weight_clamp), 1.0)
    return min(max(float(config.target_vol_per_name) / rv, 1.0 / clamp), clamp)


def _build_btc_risk_sizer(config: ContinuousDemoCycleConfig, root: Path) -> BtcRiskLiveSizer | None:
    if not getattr(config, "entry_btc_risk_sizing_enabled", False):
        return None
    return BtcRiskLiveSizer(
        Path(root) / "btc_risk_sizing_state.parquet",
        low=float(config.entry_btc_risk_low),
        high=float(config.entry_btc_risk_high),
        tail_mult=float(config.entry_btc_risk_tail_mult),
        min_prior=int(config.entry_btc_risk_min_prior),
        arm_id=str(config.entry_btc_risk_arm_id),
    )


def _btc_risk_policy(config: ContinuousDemoCycleConfig) -> dict[str, float | int]:
    return {
        "low": float(config.entry_btc_risk_low),
        "high": float(config.entry_btc_risk_high),
        "tail_mult": float(config.entry_btc_risk_tail_mult),
        "min_prior": int(config.entry_btc_risk_min_prior),
    }


def _continuous_btc_risk_multiplier(config: ContinuousDemoCycleConfig, cand: dict[str, Any]) -> float:
    if not getattr(config, "entry_btc_risk_sizing_enabled", False):
        return 1.0
    m = _finite_or_none(cand.get("btc_risk_stack_mult"))
    return float(m) if (m is not None and m > 0.0) else 1.0


def _apply_btc_risk_sizing(
    candidates: list[dict[str, Any]],
    *,
    config: ContinuousDemoCycleConfig,
    root: Path,
    btc_klines: pl.DataFrame,
    accepted_target_rows: pl.DataFrame | None = None,
    unresolved_entry_requests: int = 0,
    accepted_state_authority: bool = True,
    synchronization_error: str = "",
    btc_context: dict[int, dict[str, float | None]] | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "enabled": bool(getattr(config, "entry_btc_risk_sizing_enabled", False)),
        "arm_id": getattr(config, "entry_btc_risk_arm_id", ""),
        "candidate_rows": len(candidates),
        "scored": 0,
        "duplicates": 0,
        "tail_selected": 0,
        "warmup": 0,
        "state_rows": 0,
        "accepted_ingested": 0,
        "accepted_duplicates": 0,
        "accepted_ignored": 0,
        "accepted_authoritative_rows": 0,
        "accepted_orphaned_dropped": 0,
        "unresolved_entry_requests": int(unresolved_entry_requests),
        "entry_blocked": False,
        "blocking_reason": "",
        "error": 0,
    }
    if not stats["enabled"]:
        return stats
    if not accepted_state_authority:
        stats["entry_blocked"] = True
        stats["blocking_reason"] = "account_accepted_state_unavailable"
        return stats
    if synchronization_error:
        stats["error"] = 1
        stats["entry_blocked"] = True
        stats["blocking_reason"] = f"account_state_sync_failed:{synchronization_error}"[:500]
        return stats
    try:
        sizer = _build_btc_risk_sizer(config, root)
        if sizer is None:
            raise RuntimeError("BTC-risk sizer unexpectedly disabled")
        ingestion = sizer.reconcile_authoritative_accepted_decisions(
            () if accepted_target_rows is None else accepted_target_rows.to_dicts()
        )
        stats["accepted_ingested"] = ingestion["ingested"]
        stats["accepted_duplicates"] = ingestion["duplicates"]
        stats["accepted_ignored"] = ingestion["ignored"]
        stats["accepted_authoritative_rows"] = ingestion["authoritative_rows"]
        stats["state_rows"] = sizer.rows
        orphaned_dropped = int(ingestion.get("orphaned_dropped", 0))
        stats["accepted_orphaned_dropped"] = orphaned_dropped
        if orphaned_dropped:
            _logger.warning(
                "BTC-risk state rebased onto authoritative evidence; dropped %d prior-epoch decisions",
                orphaned_dropped,
            )
    except Exception as exc:  # noqa: BLE001 - malformed accepted state must fail closed
        _logger.exception("BTC-risk accepted-state synchronization failed; blocking new entries")
        stats["error"] = 1
        stats["entry_blocked"] = True
        stats["blocking_reason"] = f"accepted_state_invalid:{type(exc).__name__}:{exc}"[:500]
        return stats
    if unresolved_entry_requests > 0:
        stats["entry_blocked"] = True
        stats["blocking_reason"] = "unresolved_account_entry_request"
        return stats
    if not candidates:
        return stats
    try:
        lookup, score_stats = sizer.score_decisions(
            candidates,
            btc_context=(
                btc_context
                if btc_context is not None
                else btc_context_by_day(btc_klines)
            ),
        )
    except Exception as exc:  # noqa: BLE001 - stale/base sizing is not a safe fallback
        _logger.exception("BTC-risk sizing failed; blocking new entries")
        stats["error"] = 1
        stats["entry_blocked"] = True
        stats["blocking_reason"] = f"candidate_scoring_failed:{type(exc).__name__}:{exc}"[:500]
        return stats
    stats.update(score_stats)
    multipliers: list[float] = []
    scores: list[float] = []
    for cand in candidates:
        symbol = str(cand.get("symbol") or "").upper()
        signal_ts = int(cand.get("signal_ts_ms") or cand.get("entry_signal_ts_ms") or 0)
        row = lookup.get((symbol, signal_ts))
        if row is None:
            cand["btc_risk_stack_mult"] = 1.0
            continue
        cand["btc_risk_arm_id"] = stats["arm_id"]
        cand["btc_risk_score"] = row.get("btc_risk_score")
        cand["btc_risk_stack_mult"] = row.get("stack_mult", 1.0)
        cand["btc_risk_score_warmup"] = bool(row.get("score_warmup"))
        cand["btc_risk_tail_selected"] = bool(row.get("tail_selected"))
        cand["btc_trend_30d"] = row.get("btc_trend_30d")
        cand["btc_return_7d"] = row.get("btc_return_7d")
        cand["btc_vol_30d"] = row.get("btc_vol_30d")
        cand["btc_trend_delta_7d"] = row.get("btc_trend_delta_7d")
        cand[BTC_RISK_EVIDENCE_METADATA_KEY] = row["decision_evidence"]
        mult = _finite_or_none(cand.get("btc_risk_stack_mult"))
        score = _finite_or_none(cand.get("btc_risk_score"))
        if mult is not None:
            multipliers.append(mult)
        if score is not None:
            scores.append(score)
    stats["candidate_tail_rows"] = sum(1 for cand in candidates if bool(cand.get("btc_risk_tail_selected")))
    stats["min_stack_mult"] = min(multipliers) if multipliers else 1.0
    stats["max_stack_mult"] = max(multipliers) if multipliers else 1.0
    stats["mean_btc_risk_score"] = (sum(scores) / len(scores)) if scores else None
    return stats


def _btc_trend_gate_payload_fields(
    *,
    config: ContinuousDemoCycleConfig,
    allows_entry: bool,
    trend_value: float | None,
    kline_stats: dict[str, Any],
) -> dict[str, Any]:
    return {
        "btc_trend_gate": config.btc_trend_gate,
        "btc_trend_gate_mode": "daily_prior",
        "btc_trend_gate_lookback_days": int(config.btc_trend_lookback_days),
        "btc_trend_gate_lookback_duration_ms": (
            int(config.btc_trend_lookback_days) * MS_PER_DAY if config.btc_trend_gate != "off" else None
        ),
        "btc_trend_gate_allows_entry": allows_entry,
        "btc_trend_gate_value": trend_value,
        "btc_trend_gate_btc_rows": kline_stats.get("btc_rows", 0),
        "btc_trend_gate_btc_max_ts_ms": kline_stats.get("btc_max_ts_ms", 0),
        "btc_trend_gate_kline_store_rows": kline_stats.get("store_rows", 0),
        "btc_trend_gate_kline_cache_rows": kline_stats.get("cache_rows", 0),
        "btc_trend_gate_kline_fetch_symbols": kline_stats.get("fetch_symbols", 0),
        "btc_trend_gate_kline_fetched_rows": kline_stats.get("fetched_rows", 0),
        "btc_trend_gate_kline_error": kline_stats.get("error", 0),
    }


def _btc_risk_sizing_payload_fields(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "btc_risk_sizing_enabled": stats.get("enabled", False),
        "btc_risk_sizing_arm_id": stats.get("arm_id", ""),
        "btc_risk_sizing_candidate_rows": stats.get("candidate_rows", 0),
        "btc_risk_sizing_scored": stats.get("scored", 0),
        "btc_risk_sizing_duplicates": stats.get("duplicates", 0),
        "btc_risk_sizing_tail_selected": stats.get("tail_selected", 0),
        "btc_risk_sizing_warmup": stats.get("warmup", 0),
        "btc_risk_sizing_state_rows": stats.get("state_rows", 0),
        "btc_risk_sizing_accepted_ingested": stats.get("accepted_ingested", 0),
        "btc_risk_sizing_accepted_duplicates": stats.get("accepted_duplicates", 0),
        "btc_risk_sizing_accepted_ignored": stats.get("accepted_ignored", 0),
        "btc_risk_sizing_accepted_authoritative_rows": stats.get("accepted_authoritative_rows", 0),
        # A rebase that drops prior-epoch decisions must be journaled per
        # cycle, not only logged: state_rows collapsing with no recorded cause
        # is exactly the unreconstructable mutation incident reviews fight.
        "btc_risk_sizing_accepted_orphaned_dropped": stats.get("accepted_orphaned_dropped", 0),
        "btc_risk_sizing_unresolved_entry_requests": stats.get("unresolved_entry_requests", 0),
        "btc_risk_sizing_entry_blocked": stats.get("entry_blocked", False),
        "btc_risk_sizing_blocking_reason": stats.get("blocking_reason", ""),
        "btc_risk_sizing_min_stack_mult": stats.get("min_stack_mult", 1.0),
        "btc_risk_sizing_max_stack_mult": stats.get("max_stack_mult", 1.0),
        "btc_risk_sizing_mean_score": stats.get("mean_btc_risk_score"),
        "btc_risk_sizing_error": stats.get("error", 0),
    }


def _payload_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rmom_freshness_payload_fields(rmom: pl.DataFrame | None, *, current_day_ts: int) -> dict[str, Any]:
    rmom_day_max = (
        rmom["day_ts"].max() if (rmom is not None and not rmom.is_empty() and "day_ts" in rmom.columns) else None
    )
    max_rmom_day_ts = int(cast(int, rmom_day_max)) if rmom_day_max is not None else 0
    return {
        "rmom_present": rmom is not None,
        "max_rmom_day_ts": max_rmom_day_ts,
        # 0-floored: the refresh seeds a today/tomorrow row (the daily-rollover guard)
        # so the gate day can lead cur_day -> a raw diff would read negative.
        "rmom_stale_days": max(0, (current_day_ts - max_rmom_day_ts) // MS_PER_DAY) if max_rmom_day_ts else None,
    }


_logger = logging.getLogger(__name__)


def _open_continuous_trades(all_trades: pl.DataFrame, strategy_id: str | tuple[str, ...]) -> pl.DataFrame:
    if all_trades.is_empty():
        return all_trades
    strategy_ids = (strategy_id,) if isinstance(strategy_id, str) else tuple(strategy_id)
    return all_trades.filter((pl.col("status") == "open") & (pl.col("strategy_id").is_in(list(strategy_ids))))


def _continuous_target_reservations(
    all_trades: pl.DataFrame,
    strategy_id: str | tuple[str, ...],
) -> pl.DataFrame:
    """Rows reserving continuous admission/capacity, including pending targets.

    This is intentionally separate from ``_open_continuous_trades``: an
    unresolved accepted target suppresses replacement proposals but is not a
    confirmed position for exits, P&L, rebalance marks, or reporting.
    """

    reserved = target_reservation_rows(all_trades)
    if reserved.is_empty():
        return reserved
    strategy_ids = (strategy_id,) if isinstance(strategy_id, str) else tuple(strategy_id)
    return reserved.filter(pl.col("strategy_id").is_in(list(strategy_ids)))


def _finite_or_none(value: Any) -> float | None:
    """NaN/inf-guarded float coercion, None on missing/invalid (default=None variant).

    code-quality-5: routes through the canonical ``_common.finite_float`` so a
    future tightening of the NaN/inf policy lives in one place.
    ``finite_float(value, default=None)`` is behavior-preserving:
    None/unparseable -> None (old: float(None) raised TypeError -> None), NaN/+-inf -> None."""
    return finite_float(value, default=None)


def _continuous_exit_target_intents(
    exits: list[dict[str, Any]],
    all_trades: pl.DataFrame,
    *,
    strategy_id: str,
    now_ms: int,
    default_leverage: float,
) -> list[RequestedIntent]:
    """Translate strategy exits to replacement zero targets without venue I/O."""

    return exit_target_intents(
        exits,
        all_trades,
        adapter_kind=SleeveAdapterKind.CONTINUOUS,
        strategy_id=strategy_id,
        now_ms=now_ms,
        default_leverage=default_leverage,
        source="continuous_target_adapter",
        default_reason="continuous_exit",
        extra_metadata=lambda plan, trade: {
            "exit_trigger_ts_ms": int(_float(plan.get("exit_trigger_ts_ms")) or now_ms),
        },
    )


def _continuous_entry_target_intents(
    candidates: list[dict[str, Any]],
    *,
    demo: ContinuousDemoCycleConfig,
    equity_usdt: float,
    order_notional_frac: float,
    price_by_symbol: dict[str, float],
    now_ms: int,
    strategy_id: str,
) -> list[RequestedIntent]:
    """Build raw short targets; demo venue filters stay in the account kernel."""

    intents: list[RequestedIntent] = []
    for candidate in candidates:
        trade_id = str(candidate.get("trade_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        price = price_by_symbol.get(symbol, _float(candidate.get("live_price")))
        if not trade_id or not symbol or price <= 0.0:
            continue
        component_weight = _float(candidate.get("component_weight"))
        if component_weight <= 0.0:
            raise ValueError("continuous entry candidate requires a positive component_weight")
        vol_weight_multiplier = _continuous_vol_weight_multiplier(demo, candidate.get("rv_168h"))
        btc_risk_multiplier = _continuous_btc_risk_multiplier(demo, candidate)
        btc_risk_evidence: dict[str, Any] | None = None
        if demo.entry_btc_risk_sizing_enabled:
            btc_risk_evidence = normalize_btc_risk_decision_evidence(
                candidate.get(BTC_RISK_EVIDENCE_METADATA_KEY),
                expected_arm_id=demo.entry_btc_risk_arm_id,
                expected_policy=_btc_risk_policy(demo),
            )
            signal_ts_ms = int(candidate.get("signal_ts_ms") or candidate.get("entry_signal_ts_ms") or 0)
            if (
                btc_risk_evidence["symbol"] != symbol
                or btc_risk_evidence["signal_ts_ms"] != signal_ts_ms
                or btc_risk_evidence["decision_key"] != f"{symbol}|{signal_ts_ms}"
            ):
                raise ValueError("BTC-risk evidence identity does not match continuous entry candidate")
            if not math.isclose(
                btc_risk_multiplier,
                float(btc_risk_evidence["result"]["stack_mult"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("BTC-risk multiplier conflicts with decision evidence")
        target_notional = (
            equity_usdt * order_notional_frac * component_weight * vol_weight_multiplier * btc_risk_multiplier
        )
        take_profit_pct = _float(candidate.get("take_profit_pct"))
        stop_loss_pct = _float(candidate.get("stop_loss_pct")) or 0.0
        max_hold_duration_ms = exact_duration_ms(hours=demo.max_hold_hours) if demo.max_hold_hours > 0 else 0
        intents.append(
            component_target_intent(
                adapter_kind=SleeveAdapterKind.CONTINUOUS,
                action="entry",
                decision_ts_ms=now_ms,
                strategy_id=strategy_id,
                component_id=trade_id,
                symbol=symbol,
                signed_notional_usdt=-target_notional,
                leverage=demo.entry_leverage,
                reason=str(candidate.get("entry_reason") or "continuous_entry"),
                metadata={
                    "source": "continuous_target_adapter",
                    "decision_reference_price": price,
                    "take_profit_pct": take_profit_pct,
                    # Declared wide backstop: the account places the venue stop
                    # here instead of its 2% disaster fallback (§16.3 parity).
                    **({"stop_loss_pct": stop_loss_pct} if stop_loss_pct > 0.0 else {}),
                    "max_hold_duration_ms": max_hold_duration_ms,
                    "signal_ts_ms": int(candidate.get("signal_ts_ms") or 0),
                    "signal_valid_until_ms": (((int(now_ms) // MS_PER_HOUR) + 1) * MS_PER_HOUR),
                    "component_weight": component_weight,
                    "vol_weight_multiplier": vol_weight_multiplier,
                    "btc_risk_multiplier": btc_risk_multiplier,
                    "raw_target_notional_usdt": target_notional,
                    "quantity_authority": "account_kernel_demo_rules",
                    **({BTC_RISK_EVIDENCE_METADATA_KEY: btc_risk_evidence} if btc_risk_evidence is not None else {}),
                    # Settled-funding admission evidence: the last settled print
                    # observed at the decision cutoff (null = unknown-admitted),
                    # journaled so the forward record can revisit unknown-admits.
                    **(
                        {
                            "funding_min_at_entry": _float(candidate.get("funding_min_at_entry")),
                            "funding_rate_at_admission": candidate.get("funding_rate_at_admission"),
                            "funding_admission_unknown": bool(candidate.get("funding_admission_unknown")),
                        }
                        if candidate.get("funding_min_at_entry") is not None
                        else {}
                    ),
                },
            )
        )
    return intents


def _load_rmom_snapshot(root: Path) -> ResidualMomentumSnapshot | None:
    path = root / "residual_momentum.parquet"
    if not path.exists():
        return None
    try:
        snapshot = read_stable_file(
            path,
            label="CONTINUOUS residual-momentum source",
            reject_empty=True,
            require_single_link=True,
        )
        table = pl.read_parquet(io.BytesIO(snapshot.data))
        table = require_stable_residual_momentum(table, source=path)
        return ResidualMomentumSnapshot(
            table=table.rename({"ts_ms": "day_ts"}),
            source_path=str(snapshot.path),
            source_sha256=snapshot.sha256,
            source_size_bytes=snapshot.size,
            source_mtime_ns=snapshot.mtime_ns,
        )
    except Exception as exc:  # noqa: BLE001 - a corrupt rmom file must DEGRADE, not crash the cycle
        # A partially-written/corrupt parquet (mid-write crash, disk issue) or a
        # schema drift (missing ts_ms) would otherwise raise out of the cycle,
        # writing NO cycle row -- so the persisted max_rmom_day_ts telemetry never
        # updates and the watchdog cannot tell "cycle crashing on a bad rmom file"
        # from "merely quiet". Degrade to rmom-absent (the watchdog already pages
        # on the silent-blackout that produces), keeping the staleness guard
        # authoritative even on a corrupted file.
        _logger.warning("continuous: failed to load rmom table %s: %s; degrading to rmom-absent", path, exc)
        return None


def _load_rmom_table(root: Path) -> pl.DataFrame | None:
    """Compatibility reader returning only the validated stable rows."""

    snapshot = _load_rmom_snapshot(root)
    return snapshot.table if snapshot is not None else None


def _signal_source_root(config: ContinuousDemoCycleConfig, own_root: Path) -> Path:
    """Return the market-data leader root that owns the causal RMOM table."""

    followed = str(config.klines_follow_root or "").strip()
    return Path(followed).expanduser() if followed else Path(own_root).expanduser()


def format_continuous_demo_cycle_summary(payload: dict[str, Any]) -> str:
    """Render one concise target-producer status line for stdout/journald."""
    sizing = ""
    if payload.get("notional_multiplier") is not None or payload.get("entry_leverage") is not None:
        sizing = (
            f"sizing={_payload_float(payload.get('notional_multiplier')):g}x_notional/"
            f"{_payload_float(payload.get('entry_leverage')):g}x_leverage "
        )
    gate_state = "open" if payload.get("btc_trend_gate_allows_entry") else "BLOCKED"
    return (
        "continuous target producer "
        f"id={payload.get('cycle_id', '')} mode={payload.get('mode')} "
        f"symbols={payload.get('universe_symbols')} "
        f"rmom={'present' if payload.get('rmom_present') else 'MISSING'} "
        f"max_rmom_day_ts={payload.get('max_rmom_day_ts')} stale_days={payload.get('rmom_stale_days')} "
        f"d9={payload.get('live_d9_symbols')} cand={payload.get('candidates')} "
        f"entries={payload.get('entries')} exits={payload.get('exits')} open={payload.get('open_positions')} "
        f"{sizing}"
        f"equity=${_payload_float(payload.get('equity_usdt')):,.2f} paused={payload.get('entry_paused')} "
        f"btc_gate={gate_state}/{payload.get('btc_trend_gate', 'unknown')} "
        f"first_rejection={payload.get('entry_first_rejection_reason', '') or 'none'} "
        f"same_signal_reentry_skips={payload.get('skipped_same_signal_reentry', 0)}"
    )


def _validate_continuous_demo_config(config: ContinuousDemoCycleConfig) -> None:
    """Validate target routing, paper/demo separation, and strategy invariants.

    Operational demo and paper modes require their canonical account owner;
    direct sleeve order authority is retired.
    """
    execution_environment(config.execution_environment)
    if config.strategy_profile not in CONTINUOUS_DEMO_PROFILES:
        raise ValueError(f"unknown continuous strategy_profile {config.strategy_profile!r}")
    if config.btc_trend_gate not in ("off", "uptrend", "downtrend"):
        raise ValueError(f"btc_trend_gate must be 'off', 'uptrend', or 'downtrend'; got {config.btc_trend_gate!r}")
    if config.btc_trend_gate != "off" and config.btc_trend_lookback_days < 1:
        raise ValueError("btc_trend_lookback_days must be >= 1 when btc_trend_gate is active")
    if config.sizing_mode not in ("flat", "inverse_vol"):
        raise ValueError(f"sizing_mode must be 'flat' or 'inverse_vol'; got {config.sizing_mode!r}")
    if not np.isfinite(config.notional_multiplier) or config.notional_multiplier <= 0.0:
        raise ValueError("notional_multiplier must be positive")
    if not np.isfinite(config.per_position_notional_pct_equity) or config.per_position_notional_pct_equity <= 0.0:
        raise ValueError("per_position_notional_pct_equity must be positive")
    if not np.isfinite(config.entry_leverage) or config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if config.sizing_mode == "inverse_vol":
        if config.target_vol_per_name <= 0.0:
            raise ValueError("target_vol_per_name must be positive for inverse_vol sizing")
        if config.vol_weight_clamp < 1.0:
            raise ValueError("vol_weight_clamp must be >= 1.0 for inverse_vol sizing")
    if not config.feature_set:
        raise ValueError("feature_set must contain at least one causal feature")
    if config.entry_crowding_max_fresh < 0:
        raise ValueError("entry_crowding_max_fresh must be >= 0 (0 disables the crowding gate)")
    if not config.ensemble_components:
        raise ValueError("continuous profile requires component entries")
    has_account_inbox = bool(str(config.account_intent_inbox_root or "").strip())
    has_account_execution_root = bool(str(config.account_execution_root or "").strip())
    if has_account_inbox != has_account_execution_root:
        raise ValueError("account_intent_inbox_root and account_execution_root must be configured together")
    if config.entry_event_trigger != "none":
        if config.entry_confirm_delay_hours <= 0:
            raise ValueError("entry_event_trigger requires confirmed-bar entry timing")
        # Parse/validate now, before any order path or cycle work.
        _entry_event_expr(config.entry_event_trigger)
    # Validate every component trigger before cycle resources or target publication.
    for comp in config.ensemble_components:
        comp_trigger = comp[1]
        if comp_trigger != "none":
            if config.entry_confirm_delay_hours <= 0:
                raise ValueError("ensemble component entry_event_trigger requires confirmed-bar entry timing")
            _entry_event_expr(comp_trigger)
        # Declared stop is the venue backstop the account will place; a value
        # outside (0, 1) would be rejected downstream by the kernel's
        # provisional-stop contract, so fail at startup instead.
        comp_stop = float(comp[5])
        if not 0.0 < comp_stop < 1.0:
            raise ValueError("ensemble component stop_loss_pct must be a fraction in (0, 1)")
        comp_funding_min = comp[6]
        if comp_funding_min is not None and not np.isfinite(float(comp_funding_min)):
            raise ValueError("ensemble component funding_min_at_entry must be None or finite")
    if not has_account_inbox:
        raise ValueError(
            "operational demo/paper mode requires account_intent_inbox_root and "
            "account_execution_root; direct sleeve order authority is retired"
        )


def _continuous_age_eligible_symbols(
    universe: pl.DataFrame,
    klines: pl.DataFrame,
    *,
    age_days_min: int,
    now_ms: int,
) -> set[str] | None:
    """Symbols old enough to trade under the fresh-listing-squeezer floor.

    ``None`` means no age gate (floor <= 0). Prefers the AUTHORITATIVE per-cycle
    universe listing age (``listing_age_days`` = (now - Bybit v5 launchTime)/day),
    so the
    continuous sleeve no longer infers age from its rolling kline cache: a
    genuinely old coin whose cache only reaches back a few weeks would otherwise
    be wrongly gated out (the live-vs-PIT age definition debt). A null/unknown
    launch age is treated as ineligible (conservative, matches the universe-build
    age filter). Falls back to the kline-cache first bar only when the universe
    carries no listing age — correct only when the cache spans >= age_days_min days.
    """
    if age_days_min <= 0:
        return None
    if not universe.is_empty() and "listing_age_days" in universe.columns:
        eligible = universe.filter(
            pl.col("listing_age_days").is_not_null() & (pl.col("listing_age_days") >= float(age_days_min))
        )
        return set(eligible["symbol"].to_list())
    if not klines.is_empty():
        _logger.warning(
            "continuous age gate falling back to kline-cache first bar (universe has no "
            "listing_age_days); only correct when the cache spans >= %d days",
            age_days_min,
        )
        age_min_ms = exact_duration_ms(days=age_days_min)
        firsts = klines.group_by("symbol").agg(pl.col("ts_ms").min().alias("first"))
        return set(firsts.filter((now_ms - pl.col("first")) >= age_min_ms)["symbol"].to_list())
    return None


def run_continuous_demo_cycle(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    demo_config: ContinuousDemoCycleConfig | None = None,
    market_client: Any | None = None,
    now_ms: int | None = None,
    kline_store: Any | None = None,
    ticker_cache: Any | None = None,
    state_cache_stale_seconds: float = 120.0,
    panel_cache: "LivePanelCache | None" = None,
    funnel_observer: DecisionFunnelObserver | None = None,
) -> PublishedTargetCyclePayload:
    """Plan one CONT cycle and publish immutable account targets.

    This process has no private venue client, order executor, fill model, trade
    ledger, or notification authority. Planning reads the account owner's
    deterministic journal; accepted targets are reservations, and only
    journal-confirmed fills are open positions. Exits are fill-anchored
    max-hold targets. Take-profit protection is owned by the account service.
    """

    demo = apply_continuous_demo_profile(demo_config or ContinuousDemoCycleConfig())
    _validate_continuous_demo_config(demo)
    environment = execution_environment(demo.execution_environment).value
    account_route = require_account_route(
        account_id=account_id_for_environment(environment),
        environment=environment,
        account_root=Path(str(demo.account_execution_root)).expanduser(),
        inbox_root=Path(str(demo.account_intent_inbox_root)).expanduser(),
    )
    strategy_id = continuous_strategy_id(demo)
    managed_strategy_ids = continuous_managed_strategy_ids(demo)
    cycles_dataset = continuous_cycles_dataset(demo)
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = int(now_ms if now_ms is not None else _utc_now_ms())
    current_day_ts = (cycle_now_ms // MS_PER_DAY) * MS_PER_DAY
    cycle_id = f"continuous-target-{strategy_id}-{cycle_now_ms}"

    with exclusive_file_lock(root / ".locks" / "continuous_demo_cycle.lock", stale_seconds=900):
        public = market_client or BybitMarketData(
            category=config.exchange.category,
            testnet=config.exchange.testnet,
        )
        candidate_universe = (
            load_candidate_universe(demo.candidate_universe_file) if demo.candidate_universe_file else None
        )
        (
            universe,
            symbols,
            tickers,
            ticker_source,
            candidate_reconciliation,
        ) = _resolve_cycle_universe(
            public=public,
            demo=demo,
            config=config,
            root=root,
            cycle_now_ms=cycle_now_ms,
            ticker_cache=ticker_cache,
            state_cache_stale_seconds=state_cache_stale_seconds,
            frozen_candidate_universe=candidate_universe,
        )
        if candidate_reconciliation is not None:
            require_scheduled_retirements_flat(
                candidate_reconciliation,
                route=account_route,
                context="CONT cycle",
            )

        equity_usdt, account_owner_health_error = account_owner_equity_or_error(
            account_route,
            environment=environment,
        )

        start_ms, end_ms = _kline_window(cycle_now_ms, lookback_days=demo.lookback_days)
        klines, kline_stats = _download_recent_1h_klines(
            symbols,
            start_ms=start_ms,
            end_ms=end_ms,
            config=config,
            workers=demo.workers,
            market_client=public,
            cache_root=root,
            kline_store=kline_store,
        )
        btc_gate_klines = klines
        btc_gate_kline_stats = _btc_trend_kline_stats(klines)
        if demo.btc_trend_gate != "off" or demo.entry_btc_risk_sizing_enabled:
            btc_gate_klines, btc_gate_kline_stats = _load_btc_trend_gate_klines(
                klines,
                start_ms=start_ms,
                end_ms=end_ms,
                config=config,
                market_client=public,
                cache_root=root,
                kline_store=kline_store,
            )

        rmom_snapshot = _load_rmom_snapshot(_signal_source_root(demo, root))
        rmom = rmom_snapshot.table if rmom_snapshot is not None else None
        price_by_symbol = _price_lookup_from_tickers_and_klines(tickers, klines)
        live_state = pl.DataFrame()
        if rmom is not None and not klines.is_empty():
            if panel_cache is not None:
                try:
                    live_state = panel_cache.state(
                        klines,
                        price_by_symbol,
                        rmom,
                        now_ts_ms=cycle_now_ms,
                        config=demo,
                    )
                except Exception:  # noqa: BLE001 - cache is an optional speedup
                    _logger.exception("live panel cache failed; using full recompute")
                    live_state = build_live_continuous_state(
                        klines,
                        price_by_symbol,
                        rmom,
                        now_ts_ms=cycle_now_ms,
                        config=demo,
                    )
            else:
                live_state = build_live_continuous_state(
                    klines,
                    price_by_symbol,
                    rmom,
                    now_ts_ms=cycle_now_ms,
                    config=demo,
                )

        entry_state = live_state
        if demo.entry_confirm_delay_hours > 0 and rmom is not None and not klines.is_empty():
            entry_state = build_confirmed_entry_state(
                klines,
                rmom,
                now_ts_ms=cycle_now_ms,
                config=demo,
                funnel_observer=funnel_observer,
            )

        planning = sleeve_planning_snapshot(
            account_route,
            sleeve=SleeveAdapterKind.CONTINUOUS,
            strategy_ids=managed_strategy_ids,
        )
        target_publisher = planning.publisher
        unresolved_targets = planning.unresolved_targets
        canonical_trades = planning.canonical_trades
        terminal_entry_attempts = planning.terminal_entry_attempts

        open_trades = _open_continuous_trades(canonical_trades, managed_strategy_ids)
        reservations = _continuous_target_reservations(
            canonical_trades,
            managed_strategy_ids,
        )
        held_symbols = set(open_trades.get_column("symbol").to_list()) if not open_trades.is_empty() else set()
        reserved_components = _component_scope_map(reservations, strategy_id=strategy_id)
        cooldown_components = _continuous_cooldown_components(
            canonical_trades,
            now_ms=cycle_now_ms,
            strategy_id=strategy_id,
            config=demo,
        )

        exit_plans = plan_continuous_exits(
            open_trades.to_dicts(),
            now_ms=cycle_now_ms,
            config=demo,
        )
        exit_target_intents = _continuous_exit_target_intents(
            exit_plans,
            canonical_trades,
            strategy_id=strategy_id,
            now_ms=cycle_now_ms,
            default_leverage=demo.entry_leverage,
        )

        adverse_reductions = canonical_adverse_reduction_events(
            account_route.account_path,
            sleeve=SleeveAdapterKind.CONTINUOUS.value,
            strategy_ids=managed_strategy_ids,
        )
        entry_paused, recent_adverse = entry_circuit_breaker_tripped(
            adverse_reductions,
            now_ms=cycle_now_ms,
            config=demo,
        )
        current_hour_ts = (cycle_now_ms // MS_PER_HOUR) * MS_PER_HOUR
        signal_ts = (
            current_hour_ts - exact_duration_ms(hours=1 + demo.entry_confirm_delay_hours)
            if demo.entry_confirm_delay_hours > 0
            else current_hour_ts
        )
        btc_trend_gate_value = _btc_trend_gate_value(
            btc_gate_klines,
            signal_ts_ms=signal_ts,
            config=demo,
        )
        btc_trend_gate_allows_entry = _btc_trend_gate_allows_value(
            demo.btc_trend_gate,
            btc_trend_gate_value,
        )
        entry_health_ok = not account_owner_health_error and equity_usdt > 0.0
        entry_capacity = max(
            0,
            min(
                demo.max_new_entries_per_cycle,
                demo.max_active - reservations.height,
            ),
        )
        preselection_reason = _preselection_entry_gate_reason(
            entry_health_ok=entry_health_ok,
            entry_paused=entry_paused,
            entry_capacity=entry_capacity,
            btc_trend_gate_allows_entry=btc_trend_gate_allows_entry,
        )
        settled_funding_probe = (
            _CycleSettledFundingProbe(
                public,
                # Admission cutoff = the signal bar's close (decision time),
                # matching the engine's last-settled-print-at-or-before join.
                cutoff_ts_ms=signal_ts + MS_PER_HOUR,
            )
            if any(component[6] is not None for component in demo.ensemble_components)
            else None
        )
        try:
            selection_observation = _observe_continuous_component_selection(
                entry_state,
                universe=universe,
                klines=klines,
                reserved_components=reserved_components,
                cooldown_components=cooldown_components,
                reservations_count=reservations.height,
                entry_capacity=entry_capacity,
                all_trades=canonical_trades,
                signal_ts=signal_ts,
                strategy_id=strategy_id,
                price_by_symbol=price_by_symbol,
                now_ms=cycle_now_ms,
                config=demo,
                active_entries_enabled=not preselection_reason,
                settled_funding_probe=settled_funding_probe,
            )
        except Exception as exc:
            # The observer path must never turn a pre-existing hard block into a
            # cycle failure.  When entries are enabled, preserve the legacy
            # behavior and surface selection failures normally.
            if not preselection_reason:
                raise
            observer_error = f"{type(exc).__name__}: {exc}"[:500]
            _logger.exception(
                "CONTINUOUS entry-funnel observation failed behind %s",
                preselection_reason,
            )
            selection_observation = ContinuousEntrySelectionObservation(
                candidates=(),
                funnel_rows=(),
                qualified_opportunities=(),
                admitted_opportunities=(),
                selection_rejections=(),
                skipped_same_signal_reentry=0,
                observed_same_signal_reentry=0,
                observer_error=observer_error,
            )
        candidates = (
            [dict(candidate) for candidate in selection_observation.candidates] if not preselection_reason else []
        )
        skipped_same_signal_reentry = (
            selection_observation.skipped_same_signal_reentry if not preselection_reason else 0
        )

        btc_risk_sizing_stats = _apply_btc_risk_sizing(
            candidates,
            config=demo,
            root=root,
            btc_klines=btc_gate_klines,
            accepted_target_rows=canonical_trades,
            unresolved_entry_requests=unresolved_targets.entry_request_count,
            accepted_state_authority=True,
        )
        btc_risk_reason = ""
        if bool(btc_risk_sizing_stats["entry_blocked"]):
            btc_risk_reason = "btc_risk_sizing"
            candidates = []

        raw_entry_target_intents = _continuous_entry_target_intents(
            candidates,
            demo=demo,
            equity_usdt=equity_usdt,
            order_notional_frac=_continuous_base_notional_pct_equity(demo) / 100.0,
            price_by_symbol=price_by_symbol,
            now_ms=cycle_now_ms,
            strategy_id=strategy_id,
        )
        entry_target_intents = list(raw_entry_target_intents)
        qualified_block_reasons = _qualified_block_reasons(
            selection_observation,
            preselection_reason=preselection_reason,
            btc_risk_reason=btc_risk_reason,
        )
        candidate_opportunity_by_trade_id = {
            str(candidate.get("trade_id") or ""): (
                str(candidate.get("component") or ""),
                str(candidate.get("symbol") or ""),
            )
            for candidate in selection_observation.candidates
        }
        raw_entry_component_ids = {
            item.intent.component_id for item in raw_entry_target_intents
        }
        if not preselection_reason and not btc_risk_reason:
            for trade_id, candidate_opportunity in candidate_opportunity_by_trade_id.items():
                if trade_id and trade_id not in raw_entry_component_ids:
                    qualified_block_reasons[candidate_opportunity] = "target_intent_validation"

        def _attribute_entry_suppression(item: RequestedIntent, reason: str) -> None:
            suppressed_opportunity = candidate_opportunity_by_trade_id.get(
                item.intent.component_id
            )
            if suppressed_opportunity is not None:
                qualified_block_reasons[suppressed_opportunity] = reason

        suppression = suppress_target_intents(
            exit_intents=exit_target_intents,
            entry_intents=entry_target_intents,
            unresolved_target_keys=unresolved_targets.target_keys,
            terminal_entry_attempts=terminal_entry_attempts,
            on_entry_suppression=_attribute_entry_suppression,
        )
        exit_target_intents = suppression.exit_intents
        entry_target_intents = suppression.entry_intents
        unresolved_exit_suppressions = suppression.unresolved_exit_suppressions
        unresolved_entry_suppressions = suppression.unresolved_entry_suppressions
        terminal_entry_attempt_suppressions = suppression.terminal_entry_attempt_suppressions
        simultaneous_exit_suppressions = suppression.simultaneous_exit_suppressions

        target_keys = [item.intent.target_key for item in [*exit_target_intents, *entry_target_intents]]
        if len(set(target_keys)) != len(target_keys):
            raise RuntimeError("continuous planner proposed duplicate component target keys")
        publication = publish_exit_first_target_requests(
            target_publisher,
            batch_prefix=f"continuous-target/{strategy_id}/{cycle_now_ms}",
            exit_intents=exit_target_intents,
            entry_intents=entry_target_intents,
            created_ts_ns=cycle_now_ms * 1_000_000,
            independent_entry_requests=True,
        )
        published_exit_target_count = len(publication.exit_requests)
        published_entry_target_count = len(publication.entry_requests)
        entry_opportunity_by_target_key = {
            item.intent.target_key: candidate_opportunity_by_trade_id.get(item.intent.component_id)
            for item in raw_entry_target_intents
        }
        for error in publication.errors:
            publication_opportunity = entry_opportunity_by_target_key.get(
                error.target_key
            )
            if publication_opportunity is not None:
                qualified_block_reasons[publication_opportunity] = "target_publication"
        entry_publication_error_count = sum(
            entry_opportunity_by_target_key.get(error.target_key) is not None for error in publication.errors
        )
        qualified_but_blocked = _blocked_rows_from_reasons(
            selection_observation,
            qualified_block_reasons,
        )
        entry_first_rejection_reason = _first_entry_rejection_reason(
            selection_observation,
            preselection_reason=preselection_reason,
            btc_risk_reason=btc_risk_reason,
            raw_entry_intent_count=len(raw_entry_target_intents),
            unresolved_suppressions=unresolved_entry_suppressions,
            terminal_suppressions=terminal_entry_attempt_suppressions,
            publication_error_count=entry_publication_error_count,
            published_entry_count=published_entry_target_count,
            blocked_rows=qualified_but_blocked,
        )
        identity_error = ""
        try:
            feature_identity_fields = _entry_feature_identity_payload_fields(
                entry_state,
                signal_ts_ms=signal_ts,
                config=demo,
            )
            rmom_identity_fields = _rmom_identity_payload_fields(
                rmom_snapshot,
                signal_day_ts_ms=(signal_ts // MS_PER_DAY) * MS_PER_DAY,
            )
        except Exception as exc:  # noqa: BLE001 - identity is observer-only
            identity_error = f"{type(exc).__name__}: {exc}"[:500]
            _logger.exception("CONTINUOUS feature/RMOM identity projection failed")
            feature_identity_fields = {
                "entry_feature_identity_schema_version": CONTINUOUS_FEATURE_IDENTITY_SCHEMA_VERSION,
                "entry_feature_signal_ts_ms": signal_ts,
                "entry_feature_state_rows": entry_state.height,
                "entry_feature_state_columns_json": "[]",
                "entry_feature_state_sha256": "",
                "entry_feature_contract_sha256": "",
            }
            rmom_identity_fields = _rmom_identity_payload_fields(
                None,
                signal_day_ts_ms=(signal_ts // MS_PER_DAY) * MS_PER_DAY,
            )
        funnel_totals = {
            field: sum(int(row[field]) for row in selection_observation.funnel_rows)
            for field in ("d9", "liquidity", "event", "age", "funding", "available", "capacity")
        }
        account_target_requests = {
            "exit_request_ids": list(publication.exit_request_ids),
            "exit_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "target_key": item.request.intents[0].intent.target_key,
                }
                for item in publication.exit_requests
            ],
            "entry_request_ids": list(publication.entry_request_ids),
            "entry_requests": [
                {
                    "request_id": item.request.request_id,
                    "batch_id": item.request.batch_id,
                    "target_key": item.request.intents[0].intent.target_key,
                }
                for item in publication.entry_requests
            ],
            "publication_errors": [
                {
                    "stage": error.stage,
                    "target_key": error.target_key,
                    "error_type": error.error_type,
                    "message": error.message,
                }
                for error in publication.errors
            ],
        }
        live_d9 = int(live_state.filter(pl.col("decile") == demo.decile).height) if not live_state.is_empty() else 0
        entry_health_reason = "account_owner_health_unavailable" if not entry_health_ok else ""
        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": "continuous",
            "mode": f"{environment}_target",
            "strategy_id": strategy_id,
            "strategy_profile": demo.strategy_profile,
            "candidate_universe_artifact_sha256": (candidate_universe.artifact_sha256 if candidate_universe else ""),
            "temporarily_ineligible_candidates_json": json.dumps(
                candidate_reconciliation.temporarily_ineligible_rows() if candidate_reconciliation is not None else [],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "scheduled_candidate_retirements_json": json.dumps(
                candidate_reconciliation.retirement_rows() if candidate_reconciliation is not None else [],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "operational_profile_sha256": demo.operational_profile_sha256,
            "feature_set": ",".join(demo.feature_set),
            "entry_leverage": demo.entry_leverage,
            "per_position_notional_pct_equity": demo.per_position_notional_pct_equity,
            "notional_multiplier": demo.notional_multiplier,
            "effective_base_notional_pct_equity": _continuous_base_notional_pct_equity(demo),
            "universe_symbols": len(symbols),
            "ticker_source": ticker_source,
            "kline_store_rows": kline_stats.get("store_rows", 0),
            "live_d9_symbols": live_d9,
            "open_positions": len(held_symbols),
            "open_components": open_trades.height,
            "target_reservations": reservations.height,
            # Position events and P&L are emitted by the account notification
            # projector, never inferred from strategy publication receipts.
            "entries": 0,
            "exits": 0,
            "candidates": len(candidates),
            "planned_exits": len(exit_plans),
            "equity_usdt": equity_usdt,
            "entry_targets_queued": published_entry_target_count,
            "exit_targets_queued": published_exit_target_count,
            "target_intents_queued": (published_entry_target_count + published_exit_target_count),
            "account_target_route": True,
            "account_target_exit_request_ids": list(publication.exit_request_ids),
            "account_target_entry_request_ids": list(publication.entry_request_ids),
            "account_target_publication_error_count": len(publication.errors),
            "account_target_requests_json": json.dumps(
                account_target_requests,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "unresolved_exit_target_suppressions": unresolved_exit_suppressions,
            "unresolved_entry_target_suppressions": unresolved_entry_suppressions,
            "terminal_entry_attempt_suppressions": terminal_entry_attempt_suppressions,
            "simultaneous_exit_entry_suppressions": simultaneous_exit_suppressions,
            "account_owner_health_error": account_owner_health_error,
            "wallet_error": account_owner_health_error,
            "entry_risk_health_ok": entry_health_ok,
            "entry_risk_health_reasons": entry_health_reason,
            "entry_risk_health_authority": "account_service",
            "entry_risk_health_snapshot_source": f"account_owner_health:{environment}",
            "entry_signal_ts_ms": signal_ts,
            "entry_paused": entry_paused,
            "recent_adverse_exits": recent_adverse,
            "skipped_same_signal_reentry": skipped_same_signal_reentry,
            "entry_observability_schema_version": CONTINUOUS_ENTRY_OBSERVABILITY_SCHEMA_VERSION,
            "entry_observability_scope": "observer_only_no_admission_authority",
            "entry_funnel_d9": funnel_totals["d9"],
            "entry_funnel_liquidity": funnel_totals["liquidity"],
            "entry_funnel_event": funnel_totals["event"],
            "entry_funnel_age": funnel_totals["age"],
            "entry_funnel_funding": funnel_totals["funding"],
            "entry_funnel_available": funnel_totals["available"],
            "entry_funnel_capacity": funnel_totals["capacity"],
            "funding_admission_rejected": selection_observation.funding_admission_rejected,
            "funding_admission_unknown_admitted": (
                selection_observation.funding_admission_unknown_admitted
            ),
            "entry_funnel_json": json.dumps(
                selection_observation.funnel_rows,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "entry_preselection_rejection_reason": preselection_reason,
            "entry_first_rejection_reason": entry_first_rejection_reason,
            "entry_observer_error": selection_observation.observer_error,
            "entry_identity_error": identity_error,
            "observed_same_signal_reentry": selection_observation.observed_same_signal_reentry,
            "qualified_but_blocked_count": len(qualified_but_blocked),
            "qualified_but_blocked_symbols": ",".join(sorted({row["symbol"] for row in qualified_but_blocked})),
            "qualified_but_blocked_json": json.dumps(
                qualified_but_blocked,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        payload.update(feature_identity_fields)
        payload.update(rmom_identity_fields)
        payload.update(
            _rmom_freshness_payload_fields(
                rmom,
                current_day_ts=current_day_ts,
            )
        )
        payload.update(
            _btc_trend_gate_payload_fields(
                config=demo,
                allows_entry=btc_trend_gate_allows_entry,
                trend_value=btc_trend_gate_value,
                kline_stats=btc_gate_kline_stats,
            )
        )
        payload.update(_btc_risk_sizing_payload_fields(btc_risk_sizing_stats))
        write_dataset(
            pl.DataFrame([payload], infer_schema_length=None),
            root,
            cycles_dataset,
            partition_by=(),
        )
        try:
            write_continuous_cycle_status(
                root,
                ContinuousCycleStatus.from_cycle_payload(payload),
            )
        except Exception:  # noqa: BLE001 - projection loss never changes targets
            _logger.exception("CONTINUOUS notification projection failed after durable cycle write")
    return PublishedTargetCyclePayload(
        payload,
        publication=publication,
        route=target_publisher.route,
    )
