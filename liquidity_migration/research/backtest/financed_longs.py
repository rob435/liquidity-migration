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
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from liquidity_migration.rules.carry_hold import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLongsError,
    _signal_frame,
    carry_hold_weights,
    daily_grid,
    top_n_universe,
)
from liquidity_migration.rules.carry_contract import (
    DAY_MS,
    CarryDecision,
    DecisionInput as CarryDecisionInput,
    Holding as CarryHolding,
    PresettlementObservation,
    PriorState as CarryPriorState,
    SettledFundingObservation,
    SizingAnchorRequest,
    StrategyConfig as CarryStrategyConfig,
    decide as decide_carry,
)
from liquidity_migration.strategy.presettlement_events import CarryPresettlementEvent
from liquidity_migration.rules.engine_targets import parse_target_book_bytes

#: Renames that make Binance the traded venue. One implementation, two venues:
#: the replication arms must not be two different code paths.
BINANCE_VIEW = {
    "bn_close": "by_close",
    "bn_turnover_quote": "by_turnover_quote",
    "bn_funding": "by_funding",
    "bn_funding_age_h": "by_funding_age_h",
}


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
    weights: pl.DataFrame,
    universe: pl.DataFrame,
    hourly_view: pl.DataFrame,
    cfg: CarryHoldConfig,
    *,
    replay_settings: CarryReplaySettings,
    presettlement_events: tuple[CarryPresettlementEvent, ...] = (),
) -> tuple[pl.DataFrame, dict[str, object]]:
    """Replay the live CARRY reducer and score its hourly settled clock.

    The cross-venue panel can reconstruct the settled-print fallback exactly.
    A v7 pre-settlement fire additionally needs a typed running-rate observation
    with its fire-time mark.  When none are supplied the output remains a
    bounded settled-clock diagnostic, not live parity.
    """

    decision_times = sorted(int(value) for value in universe["bar_ts_ms"].unique().to_list())
    if not decision_times:
        raise FinancedLongsError("CARRY contract replay has no decision bars")
    raw_by_day: dict[int, dict[str, float]] = {value: {} for value in decision_times}
    for row in weights.select("bar_ts_ms", "symbol", "w").iter_rows(named=True):
        raw_by_day[int(row["bar_ts_ms"])][str(row["symbol"])] = float(row["w"])
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
    contract_config = CarryStrategyConfig(
        profile_name=replay_settings.source_profile,
        accepted_book_sources=(),
        exit_bp=float(cfg.exit_bp),
        early_exit_enabled=True,
        presettlement_exit_enabled=True,
        notional_multiplier=replay_settings.notional_multiplier,
        entry_leverage=replay_settings.entry_leverage,
        stop_loss_fraction=replay_settings.stop_loss_fraction,
        max_new_entries_per_cycle=replay_settings.max_new_entries_per_cycle,
        capital_reference_usdt=replay_settings.capital_reference_usdt,
    )
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
        if raw_trail is not None and math.isfinite(float(raw_trail)):
            trail_by_day[int(trail_row["bar_ts_ms"])][str(trail_row["symbol"])] = float(
                raw_trail
            )

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
        if event.decision_ts_ms not in decisions:
            raise FinancedLongsError("CARRY pre-settlement tape decision is outside the replay")
    observation_index = 0
    prior = CarryPriorState()
    active_decision: CarryDecision | None = None
    active_weights: dict[str, float] = {}
    active_day: int | None = None
    gross_by_day = {ts: 0.0 for ts in decision_times}
    turnover_by_day = {ts: 0.0 for ts in decision_times}
    settled_fire_count = 0
    presettlement_fire_count = 0
    drop_fire_count = 0
    anchor_request_count = 0
    entry_cap_deferral_count = 0
    resize_mark_missing_count = 0
    max_active_names = 0

    def reduce_at(
        now_ms: int,
        *,
        decision: CarryDecision,
        upcoming: CarryDecision | None = None,
        settled: tuple[SettledFundingObservation, ...] = (),
        presettlement: tuple[PresettlementObservation, ...] = (),
        sizing_anchor_requests: tuple[SizingAnchorRequest, ...] = (),
        marks: Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        nonlocal prior, settled_fire_count, presettlement_fire_count, drop_fire_count
        nonlocal entry_cap_deferral_count, resize_mark_missing_count, max_active_names
        mark_by_symbol = marks or {}
        holdings = tuple(
            CarryHolding(
                symbol=symbol,
                side="long",
                qty=(
                    weight
                    * replay_settings.equity_usdt
                    * replay_settings.notional_multiplier
                    / mark_by_symbol[symbol]
                    if symbol in mark_by_symbol
                    else weight
                    * replay_settings.equity_usdt
                    * replay_settings.notional_multiplier
                ),
                entry_px=mark_by_symbol.get(symbol, 1.0),
                mark_px=mark_by_symbol.get(symbol),
            )
            for symbol, weight in sorted(active_weights.items())
        )
        output = decide_carry(
            CarryDecisionInput(
                now_ms=now_ms,
                decision=decision,
                upcoming_decision=upcoming,
                holdings=holdings,
                trail_by_symbol=tuple(
                    sorted(trail_by_day[decision.decision_ts_ms].items())
                ),
                equity_usdt=replay_settings.equity_usdt,
                sizing_anchor_requests=sizing_anchor_requests,
                settled_funding=settled,
                presettlement=presettlement,
            ),
            prior,
            contract_config,
        )
        prior = output.next_state
        settled_fire_count += len(output.settled_exit_fires)
        presettlement_fire_count += len(output.presettlement_fires)
        drop_fire_count += len(output.drop_exit_fires)
        entry_cap_deferral_count += output.summary.entry_cap_deferrals
        resize_mark_missing_count += output.summary.resize_mark_missing_skips
        if output.target_book_text is None:
            return dict(active_weights)
        parsed = parse_target_book_bytes(output.target_book_text.encode())
        if output.sizing_equity_usdt is None:
            raise FinancedLongsError("CARRY contract replay wrote targets without sizing equity")
        notional_base = output.sizing_equity_usdt * replay_settings.notional_multiplier
        replayed = {
            target.symbol: target.notional_usdt / notional_base
            for target in parsed.targets
        }
        max_active_names = max(max_active_names, len(replayed))
        return replayed

    def charge_transition(day: int, before: Mapping[str, float], after: Mapping[str, float]) -> None:
        turnover_by_day[day] += sum(
            abs(float(after.get(symbol, 0.0)) - float(before.get(symbol, 0.0)))
            for symbol in set(before) | set(after)
        )

    batch_ts: int | None = None
    batch: list[dict[str, object]] = []

    def consume_hour(ts: int, rows: list[dict[str, object]]) -> None:
        nonlocal active_decision, active_weights, active_day, observation_index
        nonlocal anchor_request_count
        hour_marks: dict[str, float] = {}
        for hour_row in rows:
            mark_px = _finite_float(hour_row["by_close"])
            if mark_px is not None and mark_px > 0.0:
                hour_marks[str(hour_row["symbol"])] = mark_px
        if ts in decisions:
            next_decision = decisions[ts]
            if (
                active_decision is not None
                and active_day is not None
                and next_decision.decision_ts_ms == active_decision.decision_ts_ms + DAY_MS
            ):
                before_drop = dict(active_weights)
                active_weights = reduce_at(
                    ts,
                    decision=active_decision,
                    upcoming=next_decision,
                    sizing_anchor_requests=(
                        SizingAnchorRequest(
                            decision_ts_ms=next_decision.decision_ts_ms,
                            equity_usdt=replay_settings.equity_usdt,
                        ),
                    ),
                    marks=hour_marks,
                )
                anchor_request_count += 1
                charge_transition(active_day, before_drop, active_weights)
            before = dict(active_weights)
            active_decision = next_decision
            active_day = ts
            active_weights = reduce_at(ts, decision=active_decision, marks=hour_marks)
            charge_transition(ts, before, active_weights)
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
            before = dict(active_weights)
            active_weights = reduce_at(
                ts,
                decision=active_decision,
                settled=settled,
                marks=hour_marks,
            )
            charge_transition(active_day, before, active_weights)

        in_hour_events: list[CarryPresettlementEvent] = []
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
        weights_before_presettle = dict(active_weights)
        if in_hour:
            before = dict(active_weights)
            active_weights = reduce_at(
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
            charge_transition(active_day, before, active_weights)
        removed = set(weights_before_presettle) - set(active_weights)
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
        turnover_by_day[active_day] += sum(active_weights.values())

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
        "settled_exit_fires": settled_fire_count,
        "presettlement_observations": len(replay_events),
        "presettlement_exit_fires": presettlement_fire_count,
        "drop_exit_fires": drop_fire_count,
        "sizing_anchor_requests": anchor_request_count,
        "entry_cap_deferrals": entry_cap_deferral_count,
        "resize_mark_missing_skips": resize_mark_missing_count,
        "max_active_names": max_active_names,
        "pre_settlement_clock": (
            "typed_event_replay" if replay_events else "missing_typed_running_rate_observations"
        ),
        "max_new_entries_per_cycle": replay_settings.max_new_entries_per_cycle,
        "admission_trail": "trail_fund_24h",
        "live_parity": False,
        "boundary": (
            "Hourly closes reconstruct the settled-print fallback. Exact v7 pre-settlement returns "
            "require typed running-rate observations with fire-time marks; queue, fills, and intrahour "
            "paths remain outside this diagnostic."
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


def score_carry_hold(panel: pl.DataFrame, cfg: CarryHoldConfig) -> dict[str, Any]:
    view = venue_view(panel, cfg.venue)
    grid = daily_grid(prepare(view))
    universe = top_n_universe(grid, cfg.universe_top_n)
    weights = carry_hold_weights(universe, cfg)
    scores = daily_scores(weights, universe, cfg.fee_side_bp)
    out: dict[str, Any] = {"config_id": cfg.config_id, "venue": cfg.venue}
    out.update(summarize(scores, cfg))
    return out


def config_scores(panel: pl.DataFrame, config_path: str | Path) -> tuple[pl.DataFrame, pl.DataFrame, str, str]:
    """Daily score rows for a registered carry-hold config JSON.

    Returns ``(scores, venue_view_frame, config_id, venue)``. Only the
    carry-hold rule shape (a ``rule.state`` block) remains; the leaders and
    spread shapes were deleted 2026-08-19 by operator override.
    """
    payload: dict[str, Any] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    rule = payload.get("rule") or {}
    if "state" in rule:
        carry = CarryHoldConfig.from_json(config_path)
        view = venue_view(panel, carry.venue)
        universe = top_n_universe(daily_grid(prepare(view)), carry.universe_top_n)
        weights = carry_hold_weights(universe, carry)
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
    presettlement_events: tuple[CarryPresettlementEvent, ...] = (),
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
        universe = top_n_universe(daily_grid(prepare(view)), carry.universe_top_n)
        weights = carry_hold_weights(universe, carry)
        scores, contract_replay = live_contract_scores(
            weights,
            universe,
            view,
            carry,
            replay_settings=replay_settings,
            presettlement_events=presettlement_events,
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
            else "SIMULATION ON SEEN DATA - opinion, not evidence. Corrected settlement-exact scorer; "
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
    }
    if contract_replay is not None:
        payload["contract_replay"] = contract_replay
    (out / f"{config_id}_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload
