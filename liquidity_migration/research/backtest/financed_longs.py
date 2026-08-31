"""Executable form of the ``lane2_carry_hold`` configs.

Reads a cross-venue panel (``scripts/data/build_cross_venue_panel.py``) and produces
daily score rows for carry-hold — a per-name hysteresis state machine on the
settled funding rate: enter LONG when funding prints below ``-enter_bp``, stay
while it stays below ``-exit_bp``. Payment is funding received plus squeeze
pressure on crowded shorts; measured attribution ~3.4 units funding per -1 unit
price. (The financed-leaders and funding-spread expressions this module also
carried were deleted 2026-08-19 by operator override, with their configs.)

Accounting conventions shared with the rest of the research surface:

* Decisions on a fixed 24h grid of hourly-close bars; entry at the decision
  close (``execution_delay_ms=0`` on top of bar completion). Entry delays were
  free at v1 registration but are not on v4 — every fill-delay arm measured
  2026-08-03 is flat-to-negative (research_findings §2, settlement-instant
  timing).
* Funding accrues settlement-exact (``carry_hold.settlement_exact_funding``).
* Costs are measured one-way turnover x the measured per-side fee, not a flat
  round trip per period.
* Per-name weight cap plus a total gross cap; uncapped, gross trebles during
  cascades.
"""

from __future__ import annotations

import datetime as dt
import copy
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import polars as pl

from liquidity_migration.rules.carry_hold import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLongsError,
    _signal_frame,
    daily_grid,
    prepare_decision,
    top_n_universe,
)
from liquidity_migration.rules.carry_models import (
    DAY_MS,
    CarryDecision,
    PresettlementObservation,
    SettledFundingObservation,
)
from liquidity_migration.rules.rust_strategy_contract import (
    RustStrategyContract,
    load_rendered_native_config,
    rust_carry_research_weights as carry_hold_weights,
)

#: Renames that make Binance the traded venue. One implementation, two venues:
#: the replication arms must not be two different code paths.
BINANCE_VIEW = {
    "bn_close": "by_close",
    "bn_turnover_quote": "by_turnover_quote",
    "bn_funding": "by_funding",
    "bn_funding_age_h": "by_funding_age_h",
}

_CARRY_NATIVE_REPLAY_DAYS = 90
_CARRY_FEATURE_FLOAT_FIELDS = (
    "by_close",
    "by_turnover_quote",
    "by_funding",
    "by_funding_age_h",
    "adv24",
    "trail_fund_24h",
    "momentum",
    "ret_3d",
    "vol_30d_daily",
    "dtrail_2d",
    "crowd_persistence",
    "turn_growth_3d",
    "d_tt_ls_3d",
)
CarryDecisionSource = Literal["rust_signal_batch", "supplied_reference_fixture"]


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


@dataclass(frozen=True, slots=True)
class CarryReplaySettings:
    """Effective live settings required by the reducer replay."""

    environment: str
    source_profile: str
    notional_multiplier: float
    entry_leverage: float
    stop_loss_fraction: float
    max_new_entries_per_cycle: int
    capital_reference_usdt: float = 0.0
    equity_usdt: float = 1_000_000.0
    idle_cycle_ms: int = 60_000

    def __post_init__(self) -> None:
        if type(self.idle_cycle_ms) is not int or self.idle_cycle_ms <= 0:
            raise ValueError("CARRY replay idle cycle must be a positive integer")


@dataclass(frozen=True, slots=True)
class _ReplayHolding:
    qty: float
    entry_px: float
    mark_px: float


class CarryPresettlementReplayEvent(Protocol):
    """Typed event fields the reducer replay reads, without runtime I/O."""

    @property
    def environment(self) -> str: ...

    @property
    def source_profile(self) -> str: ...

    @property
    def source_config_id(self) -> str: ...

    @property
    def decision_ts_ms(self) -> int: ...

    @property
    def fired_ts_ms(self) -> int: ...

    @property
    def settlement_ts_ms(self) -> int: ...

    @property
    def symbol(self) -> str: ...

    @property
    def running_rate(self) -> float: ...

    @property
    def mark_px(self) -> float | None: ...

    @property
    def carry_side(self) -> str | None: ...

    @property
    def carry_qty(self) -> float | None: ...

    @property
    def carry_avg_entry_px(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class _ReducerReplayStep:
    weights: dict[str, float]
    oneway_turnover: float
    entry_cap_deferrals: int
    effective_decision: CarryDecision


class _StrategyContract(Protocol):
    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


def _empty_native_carry_state() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scorer": {
            "by_symbol": {},
            "last_decision_ts_ms": 0,
            "first_replay_ts_ms": 0,
            "last_weights": {},
            "last_universe_size": 0,
        },
        "sizing_anchors": {},
        "fired_exits": {},
        "desired_targets": {},
        "refused_entries": [],
        "entry_retry_after_ms": {},
        "last_publication_decision_ts_ms": 0,
        "current_decision": None,
    }


def _native_carry_replay_config(
    cfg: CarryHoldConfig,
    replay_settings: CarryReplaySettings,
    *,
    native_scorer: bool,
) -> dict[str, Any]:
    """Build the lifecycle config from the renderer-owned native contract."""

    native = copy.deepcopy(
        load_rendered_native_config(
            realm=replay_settings.environment,
            sleeve="carry",
        )
    )
    native.update(
        {
            "profile_name": replay_settings.source_profile,
            "environment": replay_settings.environment,
            "entries_enabled": True,
            "exit_bp": float(cfg.exit_bp),
            "early_exit_enabled": True,
            "presettlement_exit_enabled": True,
            "notional_multiplier": replay_settings.notional_multiplier,
            "entry_leverage": replay_settings.entry_leverage,
            "stop_loss_fraction": replay_settings.stop_loss_fraction,
            "max_new_entries_per_cycle": replay_settings.max_new_entries_per_cycle,
            "capital_reference_usdt": replay_settings.capital_reference_usdt,
        }
    )
    if not native_scorer:
        # Lifecycle fixtures inject an already-decided book. Their source id
        # still binds durable CARRY fire ids, but does not claim scorer parity.
        native["rule"]["config_id"] = cfg.config_id
        native["rule"]["exit_bp"] = float(cfg.exit_bp)
    return native


def _require_registered_native_rule(
    cfg: CarryHoldConfig,
    native: Mapping[str, Any],
) -> None:
    """Refuse to label a Python-era rule as the deployed native scorer."""

    rule = native.get("rule")
    if not isinstance(rule, dict):
        raise FinancedLongsError("rendered native CARRY config has no rule")
    expected: dict[str, object] = {
        "config_id": cfg.config_id,
        "universe_top_n": cfg.universe_top_n,
        "enter_bp": cfg.enter_bp,
        "exit_bp": cfg.exit_bp,
        "per_name_cap": cfg.per_name_cap,
        "gross_cap": cfg.gross_cap,
        "depth_ref_bp_per_day": cfg.depth_ref_bp_per_day,
        "depth_floor": cfg.depth_floor,
        "depth_exponent": cfg.depth_exponent,
        "toxic_band_ret3d_lo": (
            cfg.toxic_band_ret3d[0] if cfg.toxic_band_ret3d is not None else None
        ),
        "toxic_band_ret3d_hi": (
            cfg.toxic_band_ret3d[1] if cfg.toxic_band_ret3d is not None else None
        ),
        "min_vol30_daily": cfg.min_vol30_daily,
        "trail_recovery_exit_bp_2d": cfg.trail_recovery_exit_bp_2d,
        "persistence_cut": cfg.persistence_cut if cfg.persistence_window is not None else None,
        "persistence_lo": cfg.persistence_lo if cfg.persistence_window is not None else None,
        "flow_cut": cfg.flow_cut,
        "flow_lo": cfg.flow_lo if cfg.flow_cut is not None else None,
        "whale_cut": cfg.whale_cut,
        "whale_lo": cfg.whale_lo if cfg.whale_cut is not None else None,
    }
    mismatches = [
        name
        for name, expected_value in expected.items()
        if rule.get(name) != expected_value
    ]
    if mismatches:
        joined = ", ".join(sorted(mismatches))
        raise FinancedLongsError(
            f"{cfg.config_id}: registered config differs from the rendered native CARRY rule: "
            f"{joined}"
        )
    if cfg.persistence_window != 20:
        raise FinancedLongsError(
            f"{cfg.config_id}: native CARRY requires the registered 20-settlement "
            "persistence feature contract"
        )


def _carry_feature_rows_by_day(
    signal_grid: pl.DataFrame,
) -> dict[int, list[dict[str, object]]]:
    required = {"symbol", "bar_ts_ms"}
    missing = sorted(required - set(signal_grid.columns))
    if missing:
        raise FinancedLongsError(
            f"native CARRY signal frame lacks required columns: {', '.join(missing)}"
        )
    rows_by_day: dict[int, list[dict[str, object]]] = {}
    seen: set[tuple[int, str]] = set()
    available_fields = tuple(
        field for field in _CARRY_FEATURE_FLOAT_FIELDS if field in signal_grid.columns
    )
    for row in signal_grid.select(
        "symbol", "bar_ts_ms", *available_fields
    ).iter_rows(named=True):
        decision_ts_ms = int(row["bar_ts_ms"])
        symbol = str(row["symbol"])
        if decision_ts_ms <= 0 or decision_ts_ms % DAY_MS != 0:
            raise FinancedLongsError(
                "native CARRY signal rows must be positive UTC day boundaries"
            )
        identity = (decision_ts_ms, symbol)
        if identity in seen:
            raise FinancedLongsError(
                f"native CARRY signal frame repeats {symbol}:{decision_ts_ms}"
            )
        seen.add(identity)
        feature: dict[str, object] = {
            "symbol": symbol,
            "bar_ts_ms": decision_ts_ms,
            "adv_rank": None,
            "in_universe": False,
        }
        feature.update(
            {field: _finite_float(row.get(field)) for field in _CARRY_FEATURE_FLOAT_FIELDS}
        )
        rows_by_day.setdefault(decision_ts_ms, []).append(feature)
    for rows in rows_by_day.values():
        rows.sort(key=lambda row: str(row["symbol"]))
    return rows_by_day


def _placeholder_carry_decision(decision_ts_ms: int) -> CarryDecision:
    return CarryDecision(
        decision_ts_ms=decision_ts_ms,
        weights={},
        universe_size=1,
        replay_days=0,
        gross=0.0,
    )


def _parse_effective_carry_decision(output: Mapping[str, Any]) -> CarryDecision:
    raw = output.get("effective_decision")
    if not isinstance(raw, dict):
        raise RuntimeError("Rust CARRY reducer returned no effective decision")
    if set(raw) != {
        "schema_version",
        "decision_ts_ms",
        "weights",
        "universe_size",
        "replay_days",
        "gross",
    }:
        raise RuntimeError("Rust CARRY reducer returned an invalid effective decision")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or type(raw["decision_ts_ms"]) is not int
        or raw["decision_ts_ms"] <= 0
        or type(raw["universe_size"]) is not int
        or raw["universe_size"] <= 0
        or type(raw["replay_days"]) is not int
        or raw["replay_days"] < 0
        or not isinstance(raw["weights"], dict)
        or isinstance(raw["gross"], bool)
        or not isinstance(raw["gross"], (int, float))
        or not math.isfinite(float(raw["gross"]))
        or float(raw["gross"]) < 0.0
    ):
        raise RuntimeError("Rust CARRY reducer returned an invalid effective decision")
    weights: dict[str, float] = {}
    for symbol, raw_weight in raw["weights"].items():
        if (
            not isinstance(symbol, str)
            or isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or float(raw_weight) <= 0.0
        ):
            raise RuntimeError("Rust CARRY reducer returned an invalid effective decision")
        weights[symbol] = float(raw_weight)
    gross = float(raw["gross"])
    if not math.isclose(sum(weights.values()), gross, rel_tol=1e-12, abs_tol=1e-12):
        raise RuntimeError("Rust CARRY reducer returned an inconsistent decision gross")
    return CarryDecision(
        decision_ts_ms=raw["decision_ts_ms"],
        weights=weights,
        universe_size=raw["universe_size"],
        replay_days=raw["replay_days"],
        gross=float(raw["gross"]),
    )


def _research_instrument_rule() -> dict[str, float]:
    """Fine-grained rule for the historical immediate-fill model."""

    return {
        "tick_size": 1e-8,
        "qty_step": 1e-8,
        "min_qty": 1e-8,
        "min_notional": 1e-9,
    }


def venue_view(panel: pl.DataFrame, venue: str) -> pl.DataFrame:
    """Return the panel with ``venue`` as the traded venue."""
    if venue == "bybit":
        return panel
    if venue != "binance":
        raise FinancedLongsError(f"unknown venue {venue!r}")
    missing = [c for c in BINANCE_VIEW if c not in panel.columns]
    if missing:
        raise FinancedLongsError(f"panel lacks Binance columns: {missing}")
    keep = [c for c in panel.columns if c not in set(BINANCE_VIEW.values())]
    return panel.select(keep).rename(BINANCE_VIEW)


def prepare(panel: pl.DataFrame, momentum_lookback_hours: int = 168) -> pl.DataFrame:
    """Attach adv24, forward 24h net return, momentum, and contiguity."""
    frame = _signal_frame(panel, momentum_lookback_hours)
    frame = frame.filter(
        pl.col("contiguous")
        & pl.col("price_return").is_finite()
        & pl.col("funding_paid").is_finite()
        & pl.col("momentum").is_finite()
    )
    return frame.with_columns(
        (pl.col("price_return") - pl.col("funding_paid")).alias("net_return")
    )


def daily_scores(
    weights: pl.DataFrame, universe: pl.DataFrame, fee_side_bp: float
) -> pl.DataFrame:
    """Daily gross, turnover cost, and net rows, including cash and final liquidation."""
    rets = universe.select("bar_ts_ms", "symbol", "net_return")
    j = weights.join(rets, on=["bar_ts_ms", "symbol"], how="left").with_columns(
        (pl.col("w") * pl.col("net_return").fill_null(0.0)).alias("_pnl")
    )
    gross = j.group_by("bar_ts_ms").agg((pl.col("_pnl").sum() * 1e4).alias("gross_bp"))
    pivot = {
        int(k[0] if isinstance(k, tuple) else k): dict(zip(v["symbol"].to_list(), v["w"].to_list()))
        for k, v in weights.partition_by("bar_ts_ms", as_dict=True).items()
    }
    # Score the whole decision record. Flat bars are cash, and the first flat
    # bar after a hold carries the exit turnover.
    ts_sorted = sorted({int(item) for item in universe["bar_ts_ms"].unique().to_list()})
    prev: dict[str, float] = {}
    rows: dict[str, list] = {"bar_ts_ms": [], "oneway": []}
    for t in ts_sorted:
        cur = pivot.get(t, {})
        rows["bar_ts_ms"].append(t)
        rows["oneway"].append(
            sum(abs(cur.get(s, 0.0) - prev.get(s, 0.0)) for s in set(cur) | set(prev))
        )
        prev = cur
    if prev and rows["oneway"]:
        rows["oneway"][-1] += sum(abs(weight) for weight in prev.values())
    turn = pl.DataFrame(rows, schema={"bar_ts_ms": pl.Int64, "oneway": pl.Float64})
    return (
        turn.join(gross, on="bar_ts_ms", how="left")
        .with_columns(pl.col("gross_bp").fill_null(0.0).alias("gross_bp"))
        .with_columns((pl.col("oneway").fill_null(0.0) * fee_side_bp).alias("cost_bp"))
        .with_columns((pl.col("gross_bp") - pl.col("cost_bp")).alias("net_bp"))
        .select("bar_ts_ms", "gross_bp", "oneway", "cost_bp", "net_bp")
        .sort("bar_ts_ms")
    )


def live_contract_scores(
    weights: pl.DataFrame | None,
    universe: pl.DataFrame,
    hourly_view: pl.DataFrame,
    cfg: CarryHoldConfig,
    *,
    replay_settings: CarryReplaySettings,
    presettlement_events: tuple[CarryPresettlementReplayEvent, ...] = (),
    strategy_contract: _StrategyContract | None = None,
    decision_source: CarryDecisionSource = "supplied_reference_fixture",
) -> tuple[pl.DataFrame, dict[str, object]]:
    """Replay the native CARRY reducer and score its hourly settled clock.

    The cross-venue panel can reconstruct the settled-print fallback exactly.
    A v7 pre-settlement fire additionally needs a typed running-rate observation
    with its fire-time mark.  When none are supplied the output remains a
    bounded settled-clock diagnostic, not live parity. The standard v7 path
    sends causal feature rows through Rust. Supplied weights exist only for
    small supplied lifecycle fixtures.
    """

    if strategy_contract is not None:
        return _live_contract_scores_with_contract(
            weights,
            universe,
            hourly_view,
            cfg,
            replay_settings=replay_settings,
            presettlement_events=presettlement_events,
            strategy_contract=strategy_contract,
            decision_source=decision_source,
        )
    with RustStrategyContract() as contract:
        return _live_contract_scores_with_contract(
            weights,
            universe,
            hourly_view,
            cfg,
            replay_settings=replay_settings,
            presettlement_events=presettlement_events,
            strategy_contract=contract,
            decision_source=decision_source,
        )


def _live_contract_scores_with_contract(
    weights: pl.DataFrame | None,
    universe: pl.DataFrame,
    hourly_view: pl.DataFrame,
    cfg: CarryHoldConfig,
    *,
    replay_settings: CarryReplaySettings,
    presettlement_events: tuple[CarryPresettlementReplayEvent, ...],
    strategy_contract: _StrategyContract,
    decision_source: CarryDecisionSource,
) -> tuple[pl.DataFrame, dict[str, object]]:
    if decision_source not in {"rust_signal_batch", "supplied_reference_fixture"}:
        raise FinancedLongsError(f"unknown CARRY decision source {decision_source!r}")
    if decision_source == "rust_signal_batch" and weights is not None:
        raise FinancedLongsError("native CARRY replay refuses externally supplied daily weights")
    if decision_source == "supplied_reference_fixture" and weights is None:
        raise FinancedLongsError("CARRY lifecycle fixture replay requires supplied daily weights")

    all_decision_times = sorted(
        int(value) for value in universe["bar_ts_ms"].unique().to_list()
    )
    signal_rows_by_day: dict[int, list[dict[str, object]]] = {}
    if decision_source == "rust_signal_batch":
        signal_rows_by_day = _carry_feature_rows_by_day(universe)
        if not all_decision_times:
            raise FinancedLongsError("native CARRY replay has no feature bars")
        first_signal_ts = all_decision_times[0]
        decision_times = [
            ts
            for ts in all_decision_times
            if ts - first_signal_ts >= _CARRY_NATIVE_REPLAY_DAYS * DAY_MS
        ]
    else:
        decision_times = all_decision_times
    if not decision_times:
        raise FinancedLongsError(
            "CARRY contract replay has no decision bars after the configured 90-day replay window"
        )
    decisions: dict[int, CarryDecision] = {}
    if weights is not None:
        raw_by_day: dict[int, dict[str, float]] = {value: {} for value in decision_times}
        for row in weights.select("bar_ts_ms", "symbol", "w").iter_rows(named=True):
            ts = int(row["bar_ts_ms"])
            if ts in raw_by_day:
                raw_by_day[ts][str(row["symbol"])] = float(row["w"])
        universe_size = {
            int(key[0] if isinstance(key, tuple) else key): frame.height
            for key, frame in universe.partition_by("bar_ts_ms", as_dict=True).items()
        }
        decisions = {
            ts: CarryDecision(
                decision_ts_ms=ts,
                weights=raw_by_day[ts],
                universe_size=universe_size[ts],
                replay_days=0,
                gross=sum(raw_by_day[ts].values()),
            )
            for ts in decision_times
        }
    contract_config = _native_carry_replay_config(
        cfg,
        replay_settings,
        native_scorer=decision_source == "rust_signal_batch",
    )
    if decision_source == "rust_signal_batch":
        _require_registered_native_rule(cfg, contract_config)
    first_ts = decision_times[0]
    last_ts = decision_times[-1]
    hourly = (
        hourly_view.filter(
            (pl.col("bar_ts_ms") >= first_ts)
            & (pl.col("bar_ts_ms") < last_ts + 24 * HOUR_MS)
        )
        .sort(["symbol", "bar_ts_ms"])
        .with_columns(
            pl.col("bar_ts_ms").shift(-1).over("symbol").alias("_next_ts"),
            pl.col("by_close").shift(-1).over("symbol").alias("_next_close"),
            pl.col("by_funding").shift(-1).over("symbol").alias("_next_funding"),
            pl.col("by_funding_age_h").shift(1).over("symbol").alias("_prev_age"),
            pl.col("by_funding_age_h").shift(-1).over("symbol").alias("_next_age"),
        )
        .filter(pl.col("_next_ts") == pl.col("bar_ts_ms") + HOUR_MS)
        .with_columns(
            (
                (pl.col("by_funding_age_h") < 0.5)
                | (pl.col("by_funding_age_h") < pl.col("_prev_age"))
            )
            .fill_null(False)
            .alias("_settles_now"),
            (
                (pl.col("_next_age") < 0.5)
                | (pl.col("_next_age") < pl.col("by_funding_age_h"))
            )
            .fill_null(False)
            .alias("_settles_next"),
        )
        .sort(["bar_ts_ms", "symbol"])
    )
    trail_by_day: dict[int, dict[str, float]] = {value: {} for value in decision_times}
    if "trail_fund_24h" not in universe.columns:
        raise FinancedLongsError("CARRY contract replay lacks live admission trail_fund_24h")
    for trail_row in universe.select(
        "bar_ts_ms", "symbol", "trail_fund_24h"
    ).iter_rows(named=True):
        raw_trail = trail_row["trail_fund_24h"]
        ts = int(trail_row["bar_ts_ms"])
        if (
            ts in trail_by_day
            and raw_trail is not None
            and math.isfinite(float(raw_trail))
        ):
            trail_by_day[ts][str(trail_row["symbol"])] = float(raw_trail)

    replay_events = sorted(
        presettlement_events,
        key=lambda event: (event.fired_ts_ms, event.symbol),
    )
    for event in replay_events:
        if event.environment != replay_settings.environment:
            raise FinancedLongsError("CARRY pre-settlement tape environment does not match replay")
        if event.source_profile != replay_settings.source_profile:
            raise FinancedLongsError("CARRY pre-settlement tape profile does not match replay")
        if event.source_config_id != cfg.config_id:
            raise FinancedLongsError("CARRY pre-settlement tape config does not match replay")
        if event.decision_ts_ms not in decision_times:
            raise FinancedLongsError("CARRY pre-settlement tape decision is outside the replay")
    observation_index = 0
    prior = _empty_native_carry_state()
    checkpoint_fingerprint: str | None = None
    active_decision: CarryDecision | None = None
    active_weights: dict[str, float] = {}
    active_holdings: dict[str, _ReplayHolding] = {}
    active_notional_base = replay_settings.equity_usdt * replay_settings.notional_multiplier
    active_day: int | None = None
    pending_entry_deferrals = 0
    next_cadence_wake_ms: int | None = None
    gross_by_day = {ts: 0.0 for ts in decision_times}
    turnover_by_day = {ts: 0.0 for ts in decision_times}
    settled_fire_count = 0
    presettlement_fire_count = 0
    drop_fire_count = 0
    anchor_request_count = 0
    entry_cap_deferral_count = 0
    planned_resize_count = 0
    resize_mark_missing_count = 0
    max_active_names = 0
    cadence_wake_count = 0
    hourly_mark_wake_count = 0
    unpriced_execution_skip_count = 0

    def signal_batch(
        decision_ts_ms: int,
        *,
        upcoming_ts_ms: int | None = None,
    ) -> dict[str, object]:
        replay_start = decision_ts_ms - _CARRY_NATIVE_REPLAY_DAYS * DAY_MS
        rows = [
            row
            for ts in all_decision_times
            if replay_start <= ts <= decision_ts_ms
            for row in signal_rows_by_day[ts]
        ]
        if not rows:
            raise FinancedLongsError(
                f"native CARRY replay has no signal rows for {decision_ts_ms}"
            )
        upcoming_rows = (
            signal_rows_by_day.get(upcoming_ts_ms, [])
            if upcoming_ts_ms is not None
            else []
        )
        return {
            "schema_version": 1,
            "decision_ts_ms": decision_ts_ms,
            "rows": rows,
            "upcoming_rows": upcoming_rows,
            "settled_funding": [],
            "presettlement": [],
            "marks": [],
            "rejections": [],
        }

    def marked_weights(
        marks: Mapping[str, float],
        *,
        notional_base: float,
    ) -> dict[str, float]:
        return {
            symbol: holding.qty * marks[symbol] / notional_base
            for symbol, holding in sorted(active_holdings.items())
            if symbol in marks
        }

    def reduce_at(
        now_ms: int,
        *,
        decision: CarryDecision,
        upcoming: CarryDecision | None = None,
        settled: tuple[SettledFundingObservation, ...] = (),
        presettlement: tuple[PresettlementObservation, ...] = (),
        upcoming_sizing_equity_usdt: float | None = None,
        marks: Mapping[str, float] | None = None,
        native_signal_batch: dict[str, object] | None = None,
    ) -> _ReducerReplayStep:
        nonlocal prior, settled_fire_count, presettlement_fire_count, drop_fire_count
        nonlocal entry_cap_deferral_count, planned_resize_count
        nonlocal resize_mark_missing_count, max_active_names
        nonlocal active_notional_base, unpriced_execution_skip_count
        nonlocal checkpoint_fingerprint
        mark_by_symbol = marks or {}
        for symbol, mark in mark_by_symbol.items():
            holding = active_holdings.get(symbol)
            if holding is not None:
                active_holdings[symbol] = _ReplayHolding(
                    qty=holding.qty,
                    entry_px=holding.entry_px,
                    mark_px=mark,
                )
        held = {
            symbol: {
                "qty": holding.qty,
                "side": "Buy",
                "px": holding.mark_px,
                "entry_px": holding.entry_px,
                "stop_px": holding.entry_px
                * (1.0 - replay_settings.stop_loss_fraction),
            }
            for symbol, holding in sorted(active_holdings.items())
        }
        symbols = (
            set(held)
            | set(mark_by_symbol)
            | set(decision.weights)
            | (set(upcoming.weights) if upcoming is not None else set())
        )
        instrument_rule = _research_instrument_rule()
        output = strategy_contract.request(
            {
                "schema_version": 1,
                "operation": "carry_reduce",
                "config": contract_config,
                "input": {
                    "now_ms": now_ms,
                    "decision": decision.as_json_dict(),
                    "upcoming_decision": (
                        upcoming.as_json_dict() if upcoming is not None else None
                    ),
                    "settled_funding": [
                        {
                            "symbol": row.symbol,
                            "settlement_ts_ms": row.settlement_ts_ms,
                            "rate": row.rate,
                        }
                        for row in settled
                    ],
                    "presettlement": [
                        {
                            "symbol": row.symbol,
                            "observed_ts_ms": row.observed_ts_ms,
                            "settlement_ts_ms": row.settlement_ts_ms,
                            "running_rate": row.running_rate,
                            "mark_px": row.mark_px,
                        }
                        for row in presettlement
                    ],
                    "durable_fires": [],
                    "trail_by_symbol": dict(
                        sorted(trail_by_day.get(decision.decision_ts_ms, {}).items())
                    ),
                    "entry_blockers": {},
                    "account_healthy": True,
                    "equity_usdt": replay_settings.equity_usdt,
                    "upcoming_sizing_equity_usdt": upcoming_sizing_equity_usdt,
                    "facts": {
                        "held": held,
                        "prices": dict(sorted(mark_by_symbol.items())),
                        "rules": {
                            symbol: instrument_rule for symbol in sorted(symbols)
                        },
                    },
                    "owned_working_symbols": [],
                    "owned_opening_order_ids": {},
                    "checkpoint_fingerprint": checkpoint_fingerprint,
                    "signal_receipt": None,
                },
                "prior": prior,
                "signal_batch": native_signal_batch,
            }
        )
        raw_state = output.get("next_state")
        summary = output.get("summary")
        execution = output.get("execution")
        if not isinstance(raw_state, dict) or not isinstance(summary, dict):
            raise RuntimeError("Rust CARRY reducer returned an invalid state or summary")
        if not isinstance(execution, dict) or not isinstance(execution.get("effects"), list):
            raise RuntimeError("Rust CARRY reducer returned no ordered effects")
        effective_decision = _parse_effective_carry_decision(output)
        if effective_decision.decision_ts_ms != decision.decision_ts_ms:
            raise RuntimeError("Rust CARRY reducer changed the requested decision timestamp")
        effects = execution["effects"]
        checkpoints = [effect for effect in effects if effect.get("kind") == "persist_checkpoint"]
        if len(checkpoints) != 1:
            raise RuntimeError("Rust CARRY reducer did not return one whole-sleeve checkpoint")
        checkpoint_fingerprint = checkpoints[0].get("config_fingerprint")
        if not isinstance(checkpoint_fingerprint, str):
            raise RuntimeError("Rust CARRY checkpoint has no decision fingerprint")
        prior = raw_state
        settled_fires = output.get("settled_exit_fires")
        presettlement_fires = output.get("presettlement_fires")
        drop_fires = output.get("drop_exit_fires")
        if not isinstance(settled_fires, list):
            raise RuntimeError("Rust CARRY reducer returned invalid settled fires")
        if not isinstance(presettlement_fires, list):
            raise RuntimeError("Rust CARRY reducer returned invalid pre-settlement fires")
        if not isinstance(drop_fires, list):
            raise RuntimeError("Rust CARRY reducer returned invalid fire lists")
        settled_fire_count += len(settled_fires)
        presettlement_fire_count += len(presettlement_fires)
        drop_fire_count += len(drop_fires)
        deferrals = int(summary["entry_cap_deferrals"])
        entry_cap_deferral_count += deferrals
        planned_resize_count += int(summary["planned_resizes"])
        resize_mark_missing_count += int(summary["resize_mark_missing_skips"])
        anchors = raw_state.get("sizing_anchors")
        target_rows = raw_state.get("desired_targets")
        if not isinstance(anchors, dict) or not isinstance(target_rows, dict):
            raise RuntimeError("Rust CARRY reducer returned invalid target state")
        anchor = anchors.get(str(decision.decision_ts_ms))
        if not isinstance(anchor, (int, float)) or isinstance(anchor, bool):
            raise FinancedLongsError("Rust CARRY reducer wrote targets without sizing equity")
        sizing_equity = float(anchor)
        if replay_settings.capital_reference_usdt > 0.0:
            sizing_equity = min(sizing_equity, replay_settings.capital_reference_usdt)
        notional_base = sizing_equity * replay_settings.notional_multiplier
        active_notional_base = notional_base
        target_by_symbol = dict(target_rows)
        oneway_turnover = 0.0
        for symbol in sorted(set(active_holdings) - set(target_by_symbol)):
            exiting_holding = active_holdings.pop(symbol)
            exit_mark = mark_by_symbol.get(symbol)
            if exit_mark is None:
                unpriced_execution_skip_count += 1
                active_holdings[symbol] = exiting_holding
                continue
            oneway_turnover += abs(exiting_holding.qty * exit_mark) / notional_base
        for symbol, target in sorted(target_by_symbol.items()):
            if not isinstance(target, dict):
                raise RuntimeError("Rust CARRY reducer returned an invalid target")
            target_mark = mark_by_symbol.get(symbol)
            if target_mark is None:
                unpriced_execution_skip_count += 1
                continue
            target_notional = float(target["notional_usdt"])
            target_qty = target_notional / target_mark
            standing_holding = active_holdings.get(symbol)
            if standing_holding is None:
                if abs(target_notional) < float(
                    contract_config["execution"]["entry_floor_usdt"]
                ):
                    continue
                active_holdings[symbol] = _ReplayHolding(
                    qty=target_qty,
                    entry_px=target_mark,
                    mark_px=target_mark,
                )
                oneway_turnover += abs(target_notional) / notional_base
                continue
            standing_notional = standing_holding.qty * target_mark
            resize_floor = max(
                float(contract_config["execution"]["resize_floor_usdt"]),
                float(contract_config["execution"]["resize_floor_fraction"])
                * abs(standing_notional),
            )
            delta = target_notional - standing_notional
            if abs(delta) <= resize_floor:
                continue
            entry_px = standing_holding.entry_px
            if target_qty > standing_holding.qty:
                added_qty = target_qty - standing_holding.qty
                entry_px = (
                    standing_holding.qty * standing_holding.entry_px
                    + added_qty * target_mark
                ) / target_qty
            active_holdings[symbol] = _ReplayHolding(
                qty=target_qty,
                entry_px=entry_px,
                mark_px=target_mark,
            )
            oneway_turnover += abs(delta) / notional_base
        replayed = marked_weights(mark_by_symbol, notional_base=notional_base)
        max_active_names = max(max_active_names, len(active_holdings))
        return _ReducerReplayStep(
            weights=replayed,
            oneway_turnover=oneway_turnover,
            entry_cap_deferrals=deferrals,
            effective_decision=effective_decision,
        )

    batch_ts: int | None = None
    batch: list[dict[str, object]] = []

    def consume_hour(ts: int, rows: list[dict[str, object]]) -> None:
        nonlocal active_decision, active_weights, active_day, observation_index
        nonlocal anchor_request_count, cadence_wake_count
        nonlocal hourly_mark_wake_count
        nonlocal pending_entry_deferrals, next_cadence_wake_ms
        reduced_at_hour_boundary = False
        hour_marks: dict[str, float] = {}
        for hour_row in rows:
            mark_px = _finite_float(hour_row["by_close"])
            if mark_px is not None and mark_px > 0.0:
                hour_marks[str(hour_row["symbol"])] = mark_px
        active_weights = marked_weights(
            hour_marks,
            notional_base=active_notional_base,
        )
        if ts in decision_times:
            if decision_source == "rust_signal_batch":
                if (
                    active_decision is not None
                    and active_day is not None
                    and ts == active_decision.decision_ts_ms + DAY_MS
                ):
                    drop_step = reduce_at(
                        ts,
                        decision=active_decision,
                        upcoming_sizing_equity_usdt=replay_settings.equity_usdt,
                        marks=hour_marks,
                        native_signal_batch=signal_batch(
                            active_decision.decision_ts_ms,
                            upcoming_ts_ms=ts,
                        ),
                    )
                    anchor_request_count += 1
                    active_weights = drop_step.weights
                    turnover_by_day[active_day] += drop_step.oneway_turnover
                active_day = ts
                decision_step = reduce_at(
                    ts,
                    decision=_placeholder_carry_decision(ts),
                    marks=hour_marks,
                    native_signal_batch=signal_batch(ts),
                )
                active_decision = decision_step.effective_decision
            else:
                next_decision = decisions[ts]
                if (
                    active_decision is not None
                    and active_day is not None
                    and next_decision.decision_ts_ms
                    == active_decision.decision_ts_ms + DAY_MS
                ):
                    drop_step = reduce_at(
                        ts,
                        decision=active_decision,
                        upcoming=next_decision,
                        upcoming_sizing_equity_usdt=replay_settings.equity_usdt,
                        marks=hour_marks,
                    )
                    anchor_request_count += 1
                    active_weights = drop_step.weights
                    turnover_by_day[active_day] += drop_step.oneway_turnover
                active_decision = next_decision
                active_day = ts
                decision_step = reduce_at(ts, decision=active_decision, marks=hour_marks)
            active_weights = decision_step.weights
            turnover_by_day[ts] += decision_step.oneway_turnover
            pending_entry_deferrals = decision_step.entry_cap_deferrals
            next_cadence_wake_ms = (
                ts + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
            reduced_at_hour_boundary = True
        if active_decision is None or active_day is None:
            return

        settled_rows: list[SettledFundingObservation] = []
        for hour_row in rows:
            funding_rate = _finite_float(hour_row["by_funding"])
            if bool(hour_row["_settles_now"]) and funding_rate is not None:
                settled_rows.append(
                    SettledFundingObservation(
                        symbol=str(hour_row["symbol"]),
                        settlement_ts_ms=ts,
                        rate=funding_rate,
                    )
                )
        settled = tuple(settled_rows)
        if settled and ts > active_decision.decision_ts_ms:
            settled_step = reduce_at(
                ts,
                decision=active_decision,
                settled=settled,
                marks=hour_marks,
            )
            active_weights = settled_step.weights
            turnover_by_day[active_day] += settled_step.oneway_turnover
            pending_entry_deferrals = settled_step.entry_cap_deferrals
            next_cadence_wake_ms = (
                ts + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
            reduced_at_hour_boundary = True

        if not reduced_at_hour_boundary:
            hourly_step = reduce_at(
                ts,
                decision=active_decision,
                marks=hour_marks,
            )
            active_weights = hourly_step.weights
            turnover_by_day[active_day] += hourly_step.oneway_turnover
            pending_entry_deferrals = hourly_step.entry_cap_deferrals
            next_cadence_wake_ms = (
                ts + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
            hourly_mark_wake_count += 1

        in_hour_events: list[CarryPresettlementReplayEvent] = []
        while (
            observation_index < len(replay_events)
            and replay_events[observation_index].fired_ts_ms < ts
        ):
            observation_index += 1
        while (
            observation_index < len(replay_events)
            and replay_events[observation_index].fired_ts_ms < ts + HOUR_MS
        ):
            event = replay_events[observation_index]
            if event.fired_ts_ms >= ts and event.decision_ts_ms == active_decision.decision_ts_ms:
                in_hour_events.append(event)
            observation_index += 1
        in_hour = tuple(
            PresettlementObservation(
                symbol=event.symbol,
                observed_ts_ms=event.fired_ts_ms,
                settlement_ts_ms=event.settlement_ts_ms,
                running_rate=event.running_rate,
                mark_px=event.mark_px,
                carry_side=event.carry_side,
                carry_qty=event.carry_qty,
                carry_avg_entry_px=event.carry_avg_entry_px,
            )
            for event in in_hour_events
        )
        cadence_boundary_ms = (
            min(row.observed_ts_ms for row in in_hour)
            if in_hour
            else ts + HOUR_MS
        )
        while (
            pending_entry_deferrals
            and next_cadence_wake_ms is not None
            and next_cadence_wake_ms < cadence_boundary_ms
        ):
            cadence_step = reduce_at(
                next_cadence_wake_ms,
                decision=active_decision,
                marks=hour_marks,
            )
            active_weights = cadence_step.weights
            turnover_by_day[active_day] += cadence_step.oneway_turnover
            pending_entry_deferrals = cadence_step.entry_cap_deferrals
            cadence_wake_count += 1
            next_cadence_wake_ms = (
                next_cadence_wake_ms + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
        weights_before_presettle = dict(active_weights)
        holdings_before_presettle = set(active_holdings)
        if in_hour:
            presettlement_step = reduce_at(
                max(row.observed_ts_ms for row in in_hour),
                decision=active_decision,
                presettlement=in_hour,
                marks={
                    **hour_marks,
                    **{
                        row.symbol: row.mark_px
                        for row in in_hour
                        if row.mark_px is not None
                    },
                },
            )
            active_weights = presettlement_step.weights
            turnover_by_day[active_day] += presettlement_step.oneway_turnover
            pending_entry_deferrals = presettlement_step.entry_cap_deferrals
            next_cadence_wake_ms = (
                max(row.observed_ts_ms for row in in_hour)
                + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
        while (
            pending_entry_deferrals
            and next_cadence_wake_ms is not None
            and next_cadence_wake_ms < ts + HOUR_MS
        ):
            cadence_step = reduce_at(
                next_cadence_wake_ms,
                decision=active_decision,
                marks=hour_marks,
            )
            active_weights = cadence_step.weights
            turnover_by_day[active_day] += cadence_step.oneway_turnover
            pending_entry_deferrals = cadence_step.entry_cap_deferrals
            cadence_wake_count += 1
            next_cadence_wake_ms = (
                next_cadence_wake_ms + replay_settings.idle_cycle_ms
                if pending_entry_deferrals
                else None
            )
        removed = holdings_before_presettle - set(active_holdings)
        marks = {row.symbol: row.mark_px for row in in_hour if row.symbol in removed}
        if any(marks[symbol] is None for symbol in removed):
            missing = ",".join(sorted(symbol for symbol in removed if marks.get(symbol) is None))
            raise FinancedLongsError(
                f"CARRY pre-settlement replay cannot price fire-time exits without marks: {missing}"
            )

        for hour_row in rows:
            symbol = str(hour_row["symbol"])
            weight = weights_before_presettle.get(symbol, 0.0)
            if weight == 0.0:
                continue
            close = _finite_float(hour_row["by_close"])
            if close is None:
                raise FinancedLongsError(f"CARRY contract replay lacks a close for {symbol}:{ts}")
            if symbol in removed:
                exit_mark = marks.get(symbol)
                if exit_mark is None:
                    raise FinancedLongsError(
                        f"CARRY pre-settlement replay cannot price {symbol}:{ts}"
                    )
                end_price = exit_mark
                funding_paid = 0.0
            else:
                next_close = _finite_float(hour_row["_next_close"])
                if next_close is None:
                    raise FinancedLongsError(
                        f"CARRY contract replay lacks the next close for {symbol}:{ts}"
                    )
                end_price = next_close
                next_funding = _finite_float(hour_row["_next_funding"])
                funding_paid = (
                    next_funding
                    if bool(hour_row["_settles_next"])
                    and next_funding is not None
                    else 0.0
                )
            gross_by_day[active_day] += weight * ((end_price / close - 1.0) - funding_paid)

    for row in hourly.iter_rows(named=True):
        ts = int(row["bar_ts_ms"])
        if batch_ts is None:
            batch_ts = ts
        if ts != batch_ts:
            consume_hour(batch_ts, batch)
            batch_ts = ts
            batch = []
        batch.append(row)
    if batch_ts is not None:
        consume_hour(batch_ts, batch)
    if active_day is not None:
        turnover_by_day[active_day] += sum(abs(weight) for weight in active_weights.values())

    rows = []
    for ts in decision_times:
        gross_bp = gross_by_day[ts] * 1e4
        oneway = turnover_by_day[ts]
        cost_bp = oneway * cfg.fee_side_bp
        rows.append(
            {
                "bar_ts_ms": ts,
                "gross_bp": gross_bp,
                "oneway": oneway,
                "cost_bp": cost_bp,
                "net_bp": gross_bp - cost_bp,
            }
        )
    diagnostics: dict[str, object] = {
        "schema_version": 1,
        "mode": "live_carry_contract_hourly",
        "decision_authority": (
            "rust_carry_native"
            if decision_source == "rust_signal_batch"
            else "supplied_reference_fixture"
        ),
        "daily_scorer_authority": (
            "rust_carry_native"
            if decision_source == "rust_signal_batch"
            else "supplied_reference_fixture"
        ),
        "last_effective_universe_size": (
            active_decision.universe_size
            if decision_source == "rust_signal_batch" and active_decision is not None
            else None
        ),
        "signal_replay_days": (
            _CARRY_NATIVE_REPLAY_DAYS
            if decision_source == "rust_signal_batch"
            else None
        ),
        "contract_transport": "one_persistent_jsonl_process",
        "settled_exit_fires": settled_fire_count,
        "presettlement_observations": len(replay_events),
        "presettlement_exit_fires": presettlement_fire_count,
        "drop_exit_fires": drop_fire_count,
        "sizing_anchor_requests": anchor_request_count,
        "entry_cap_deferrals": entry_cap_deferral_count,
        "planned_resizes": planned_resize_count,
        "resize_mark_missing_skips": resize_mark_missing_count,
        "unpriced_execution_skips": unpriced_execution_skip_count,
        "max_active_names": max_active_names,
        "pre_settlement_clock": (
            "typed_event_replay" if replay_events else "missing_typed_running_rate_observations"
        ),
        "max_new_entries_per_cycle": replay_settings.max_new_entries_per_cycle,
        "admission_trail": "trail_fund_24h",
        "idle_cycle_ms": replay_settings.idle_cycle_ms,
        "idle_cadence_wakes": cadence_wake_count,
        "hourly_mark_wakes": hourly_mark_wake_count,
        "holding_state": "carried_quantity_entry_and_current_mark",
        "execution_model": "modeled_immediate_target_fill_at_observed_hourly_mark",
        "live_parity": False,
        "boundary": (
            "Rust owns the v7 daily scorer and lifecycle reducer. The reducer wakes on the configured "
            "idle cadence and carries modeled quantities through the Rust $1/5% resize deadband. "
            "Hourly marks approximate the quote-driven follower and "
            "target fills are assumed at those marks; venue queue, event-driven fill wakes, "
            "quantization, and intrahour prices are not reconstructed. Exact "
            "v7 pre-settlement returns require typed running-rate observations with fire-time marks."
            if decision_source == "rust_signal_batch"
            else "The daily decision is a supplied lifecycle fixture. Rust owns every lifecycle "
            "transition and target effect, but this mode is not a daily-scorer parity claim."
        ),
    }
    return pl.DataFrame(rows), diagnostics


def volatility_scale(
    net_bp: np.ndarray, *, target_annual: float, lookback_days: int, max_leverage: float
) -> np.ndarray:
    """Leverage per day from volatility measured strictly before that day."""
    returns = np.asarray(net_bp, dtype=float) / 1e4
    scale = np.zeros(len(returns))
    for i in range(lookback_days, len(returns)):
        realized = returns[i - lookback_days : i].std(ddof=1) * math.sqrt(365.0)
        if realized > 0:
            scale[i] = min(target_annual / realized, max_leverage)
    return scale


def summarize(scores: pl.DataFrame, cfg: CarryHoldConfig) -> dict[str, float]:
    """Scoring recipe: raw and vol-targeted Sharpe, compounded return/drawdown."""
    net = scores["net_bp"].to_numpy()
    if len(net) < 2:
        return {"days": float(len(net))}
    raw = net / 1e4
    sd = raw.std(ddof=1)
    lev = volatility_scale(
        net,
        target_annual=cfg.vol_target_annual,
        lookback_days=cfg.vol_lookback_days,
        max_leverage=cfg.max_leverage,
    )
    extra_cost = np.abs(np.diff(lev, prepend=0.0)) * (cfg.fee_side_bp / 1e4)
    scaled = lev * raw - extra_cost
    ssd = scaled.std(ddof=1)
    eq_raw = np.cumprod(1.0 + raw)
    eq_vt = np.cumprod(1.0 + scaled)
    return {
        "days": float(len(net)),
        "mean_net_bp_per_day": float(net.mean()),
        "sharpe_raw": float(raw.mean() / sd * math.sqrt(365.0)) if sd > 0 else 0.0,
        "sharpe_vol_targeted": float(scaled.mean() / ssd * math.sqrt(365.0)) if ssd > 0 else 0.0,
        "total_return_raw_pct": float((eq_raw[-1] - 1.0) * 100.0),
        "total_return_vt_pct": float((eq_vt[-1] - 1.0) * 100.0),
        "max_drawdown_vt_pct": float(
            np.max(1.0 - eq_vt / np.maximum.accumulate(np.maximum(eq_vt, 1e-12))) * 100.0
        ),
        "worst_day_vt_pct": float(scaled.min() * 100.0),
        "mean_oneway_turnover": float(scores["oneway"].mean() or 0.0),  # type: ignore[arg-type]
    }


def score_carry_hold(
    panel: pl.DataFrame,
    cfg: CarryHoldConfig,
    *,
    strategy_contract: _StrategyContract | None = None,
) -> dict[str, Any]:
    view = venue_view(panel, cfg.venue)
    grid = daily_grid(prepare(view))
    universe = top_n_universe(grid, cfg.universe_top_n)
    weights = carry_hold_weights(
        universe,
        cfg,
        strategy_contract=strategy_contract,
    )
    scores = daily_scores(weights, universe, cfg.fee_side_bp)
    out: dict[str, Any] = {"config_id": cfg.config_id, "venue": cfg.venue}
    out.update(summarize(scores, cfg))
    return out


def config_scores(
    panel: pl.DataFrame,
    config_path: str | Path,
    *,
    strategy_contract: _StrategyContract | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame, str, str]:
    """Return offline scores for a registered carry-hold rule.

    Python constructs the causal feature grid. Rust applies the registered
    hysteresis and sizing rule. This research path does not replay the full
    live lifecycle and makes no live-parity claim.
    """
    payload: dict[str, Any] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rule = payload.get("rule") or {}
    if "state" in rule:
        carry = CarryHoldConfig.from_json(config_path)
        view = venue_view(panel, carry.venue)
        universe = top_n_universe(daily_grid(prepare(view)), carry.universe_top_n)
        weights = carry_hold_weights(
            universe,
            carry,
            strategy_contract=strategy_contract,
        )
        return daily_scores(weights, universe, carry.fee_side_bp), view, carry.config_id, carry.venue
    raise FinancedLongsError(f"unrecognized financed-longs rule shape in {config_path}")


def research_equity_chart(
    panel: pl.DataFrame,
    config_path: str | Path,
    output_dir: str | Path,
    *,
    start: str,
    end: str,
    replay_mode: str = "registered_daily",
    replay_settings: CarryReplaySettings | None = None,
    presettlement_events: tuple[CarryPresettlementReplayEvent, ...] = (),
) -> dict[str, Any]:
    """Render a registered financed-longs config through the standard equity
    chart renderer, labelled as research.

    ``end`` is exclusive; the daily series is the settlement-exact scorer's
    full-calendar record clipped to ``[start, end)`` and compounded at native
    raw-book size (no presentation leverage).
    """
    from liquidity_migration.research.backtest.volume_events_charts import _write_equity_benchmark_chart

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    contract_replay: dict[str, object] | None = None
    if replay_mode == "registered_daily":
        scores, view, config_id, venue = config_scores(panel, config_path)
    elif replay_mode == "live_contract":
        if replay_settings is None:
            raise FinancedLongsError(
                "live CARRY contract replay requires effective execution settings"
            )
        carry = CarryHoldConfig.from_json(config_path)
        view = venue_view(panel, carry.venue)
        signal_grid = daily_grid(prepare_decision(view))
        scores, contract_replay = live_contract_scores(
            None,
            signal_grid,
            view,
            carry,
            replay_settings=replay_settings,
            presettlement_events=presettlement_events,
            decision_source="rust_signal_batch",
        )
        config_id = carry.config_id
        venue = carry.venue
    else:
        raise FinancedLongsError(f"unknown CARRY equity replay mode {replay_mode!r}")
    start_ms = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC).timestamp() * 1000)
    end_ms = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC).timestamp() * 1000)
    window = scores.filter((pl.col("bar_ts_ms") >= start_ms) & (pl.col("bar_ts_ms") < end_ms)).sort("bar_ts_ms")
    if window.height < 2:
        raise FinancedLongsError(f"{config_id}: fewer than 2 scored days in [{start}, {end})")

    returns = window["net_bp"].to_numpy() / 1e4
    equity_values = np.cumprod(1.0 + returns)
    days = [
        dt.datetime.fromtimestamp(int(ts) / 1000, dt.UTC).date().isoformat()
        for ts in window["bar_ts_ms"].to_list()
    ]
    equity = pl.DataFrame({"date": days, "equity": equity_values})
    equity.write_csv(out / f"{config_id}_daily_equity.csv")

    years = max((dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days, 1) / 365.25
    total = float(equity_values[-1] - 1.0)
    annualized = float(equity_values[-1] ** (1.0 / years) - 1.0)
    drawdown = float((equity_values / np.maximum.accumulate(equity_values) - 1.0).min())
    deviation = float(returns.std(ddof=1))
    metrics = {
        "total_return_pct": total * 100.0,
        "annualized_pct": annualized * 100.0,
        "max_drawdown_pct": drawdown * 100.0,
        "worst_day_pct": float(returns.min()) * 100.0,
        "sharpe_daily_ann": float(returns.mean() / deviation * math.sqrt(365.0)) if deviation > 0 else 0.0,
        "mar": (annualized / abs(drawdown)) if drawdown < 0 else None,
        "years": years,
    }

    raw_klines = (
        view.filter(pl.col("symbol") == "BTCUSDT")
        .select("symbol", "bar_ts_ms", "by_close")
        .rename({"bar_ts_ms": "ts_ms", "by_close": "close"})
        .with_columns(
            pl.from_epoch("ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
    )
    chart = _write_equity_benchmark_chart(
        out,
        equity=equity,
        raw_klines=raw_klines,
        monthly=None,
        png_name=f"{config_id}_equity_btc.png",
        title=f"RESEARCH {config_id} [{venue}] - registered Lane-2 config",
        subtitle=(
            (
                "SIMULATION ON SEEN DATA - opinion, not evidence. Live CARRY reducer replay on the "
                "hourly settled clock; typed pre-settlement observations are applied when supplied. "
            )
            if replay_mode == "live_contract"
            else "SIMULATION ON SEEN DATA - opinion, not evidence. Rust registered-rule scorer; "
        )
        + f"native raw-book size (no presentation leverage); window {start} -> {end} (end exclusive).",
        step=False,
        strategy_name=config_id,
        metrics=metrics,
    )
    run_label = (
        f"{config_id}_research_seen_data_live_contract_replay"
        if replay_mode == "live_contract"
        else f"{config_id}_research_seen_data_corrected_scorer"
    )
    payload = {
        "run_label": run_label,
        "summary": {
            "total_return": total,
            "max_drawdown": drawdown,
            "sharpe_like": metrics["sharpe_daily_ann"],
            "mar": metrics["mar"],
        },
        "metrics": metrics,
        "png": chart.get("png"),
        "config_id": config_id,
        "venue": venue,
        "decision_authority": (
            "rust_carry_native"
            if replay_mode == "live_contract"
            else "rust_registered_rule"
        ),
    }
    if contract_replay is not None:
        payload["contract_replay"] = contract_replay
    (out / f"{config_id}_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload
