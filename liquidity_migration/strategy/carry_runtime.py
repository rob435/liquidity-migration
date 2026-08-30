"""CARRY runtime adapters around the pure lifecycle reducer.

This module gathers typed reducer inputs and applies reducer effects.  The
commit order is fixed: durable Exodus handoff events, CARRY reducer state,
then the exact target-book bytes.  ``publish_target_book`` creates and checks
the immutable content-addressed object before replacing the active path.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import polars as pl

from liquidity_migration.rules.carry_contract import (
    DecisionOutput,
    Holding,
    PresettlementFire,
    PresettlementObservation,
    PriorState,
    PublicationAction,
    SettledFundingObservation,
    StrategyConfig,
)
from liquidity_migration.rules.carry_hold import CarryHoldConfig
from liquidity_migration.rules.engine_targets import PublishedTargetBook, publish_target_book
from liquidity_migration.strategy.presettlement_events import (
    CarryPresettlementEvent,
    append_carry_presettlement_event,
    load_carry_presettlement_events,
)
from liquidity_migration.strategy.strategy_event_clock import JsonlStrategyEventTape


class CarryStateAdapter(Protocol):
    def persist_reducer_state(self, *, exit_state_path: Path, state: PriorState) -> None: ...

    def note_reducer_sizing(
        self,
        *,
        decision_ts_ms: int,
        sizing_equity_usdt: float | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CarryCommitResult:
    publication_events: tuple[CarryPresettlementEvent, ...]
    target_book: PublishedTargetBook | None


def carry_reducer_clock_ms(
    *,
    cycle_started_ms: int,
    after_inputs_ms: int,
    presettlement: tuple[PresettlementObservation, ...],
) -> int:
    """Use the post-read clock and reject observations dated beyond it."""

    reducer_now_ms = max(int(cycle_started_ms), int(after_inputs_ms))
    future = sorted(
        row.symbol
        for row in presettlement
        if row.observed_ts_ms > reducer_now_ms
    )
    if future:
        raise ValueError(
            "CARRY pre-settlement observations are later than the post-read clock: "
            + ",".join(future)
        )
    return reducer_now_ms


def carry_strategy_config(
    *,
    profile_name: str,
    compatibility_source: str,
    rule: CarryHoldConfig,
    early_exit_enabled: bool,
    presettlement_exit_enabled: bool,
    notional_multiplier: float,
    entry_leverage: float,
    stop_loss_fraction: float,
    max_new_entries_per_cycle: int,
    capital_reference_usdt: float,
) -> StrategyConfig:
    """Build the typed effective reducer config from resolved runtime values."""

    return StrategyConfig(
        profile_name=profile_name,
        accepted_book_sources=(compatibility_source,),
        exit_bp=float(rule.exit_bp),
        early_exit_enabled=early_exit_enabled,
        presettlement_exit_enabled=presettlement_exit_enabled,
        notional_multiplier=float(notional_multiplier),
        entry_leverage=float(entry_leverage),
        stop_loss_fraction=float(stop_loss_fraction),
        max_new_entries_per_cycle=int(max_new_entries_per_cycle),
        capital_reference_usdt=float(capital_reference_usdt),
    )


def carry_holdings(
    standing_rows: dict[str, tuple[str, float, float]],
    *,
    mark_px_by_symbol: Mapping[str, float] | None = None,
) -> tuple[Holding, ...]:
    marks = mark_px_by_symbol or {}
    return tuple(
        Holding(
            symbol=symbol,
            side=str(side).lower(),
            qty=abs(float(qty)),
            entry_px=float(entry_px),
            mark_px=(float(marks[symbol]) if symbol in marks else None),
        )
        for symbol, (side, qty, entry_px) in sorted(standing_rows.items())
        if float(qty) != 0.0 and float(entry_px) > 0.0
    )


def settled_funding_observations(
    funding: pl.DataFrame | None,
    *,
    decision_ts_ms: int,
    now_ms: int,
) -> tuple[SettledFundingObservation, ...]:
    if funding is None or funding.is_empty():
        return ()
    frame = (
        funding.filter(
            (pl.col("funding_ts_ms") > int(decision_ts_ms))
            & (pl.col("funding_ts_ms") <= int(now_ms))
            & pl.col("funding_rate").is_not_null()
        )
        .select("symbol", "funding_ts_ms", "funding_rate")
        .sort(["funding_ts_ms", "symbol"])
    )
    return tuple(
        SettledFundingObservation(
            symbol=str(symbol),
            settlement_ts_ms=int(settlement_ts_ms),
            rate=float(rate),
        )
        for symbol, settlement_ts_ms, rate in frame.iter_rows()
    )


def carry_presettlement_observation(
    *,
    symbol: str,
    observed_ts_ms: int,
    settlement_ts_ms: int,
    running_rate: float,
    mark_px: float | None,
    carry_side: str | None,
    carry_qty: float | None,
    carry_avg_entry_px: float | None,
) -> PresettlementObservation:
    return PresettlementObservation(
        symbol=symbol,
        observed_ts_ms=observed_ts_ms,
        settlement_ts_ms=settlement_ts_ms,
        running_rate=running_rate,
        mark_px=mark_px,
        carry_side=carry_side,
        carry_qty=carry_qty,
        carry_avg_entry_px=carry_avg_entry_px,
    )


def durable_presettlement_fire(event: CarryPresettlementEvent) -> PresettlementFire:
    return PresettlementFire(
        decision_ts_ms=event.decision_ts_ms,
        symbol=event.symbol,
        observed_ts_ms=event.fired_ts_ms,
        settlement_ts_ms=event.settlement_ts_ms,
        running_rate=event.running_rate,
        mark_px=event.mark_px,
        carry_side=event.carry_side,
        carry_qty=event.carry_qty,
        carry_avg_entry_px=event.carry_avg_entry_px,
    )


def load_durable_presettlement_events(path: Path) -> tuple[CarryPresettlementEvent, ...]:
    """Repair a private crash tail, sync it, then return the verified tape."""

    if not path.exists():
        return ()
    tape = JsonlStrategyEventTape(path)
    tape.ensure_durable()
    return load_carry_presettlement_events(path)


def in_scope_presettlement_events(
    events: tuple[CarryPresettlementEvent, ...],
    *,
    environment: str,
    source_profile: str,
    source_config_id: str,
) -> tuple[CarryPresettlementEvent, ...]:
    """Keep only durable handoffs owned by this exact CARRY deployment."""

    return tuple(
        event
        for event in events
        if event.environment == environment
        and event.source_profile == source_profile
        and event.source_config_id == source_config_id
    )


def presettlement_event_from_fire(
    fire: PresettlementFire,
    *,
    environment: str,
    source_profile: str,
    source_config_id: str,
) -> CarryPresettlementEvent:
    return CarryPresettlementEvent(
        environment=environment,
        source_profile=source_profile,
        source_config_id=source_config_id,
        decision_ts_ms=fire.decision_ts_ms,
        fired_ts_ms=fire.observed_ts_ms,
        settlement_ts_ms=fire.settlement_ts_ms,
        symbol=fire.symbol,
        running_rate=fire.running_rate,
        mark_px=fire.mark_px,
        carry_side=fire.carry_side,
        carry_qty=fire.carry_qty,
        carry_avg_entry_px=fire.carry_avg_entry_px,
    )


def commit_carry_output(
    output: DecisionOutput,
    *,
    state: CarryStateAdapter,
    decision_ts_ms: int,
    exit_state_path: Path,
    presettlement_event_path: Path,
    target_book_path: Path,
    environment: str,
    source_profile: str,
    source_config_id: str,
) -> CarryCommitResult:
    """Apply one reducer output in its durable write-ahead order."""

    events = tuple(
        sorted(
            (
                presettlement_event_from_fire(
                    fire,
                    environment=environment,
                    source_profile=source_profile,
                    source_config_id=source_config_id,
                )
                for fire in output.presettlement_fires
            ),
            key=lambda event: event.to_strategy_event().order_key,
        )
    )
    for event in events:
        append_carry_presettlement_event(presettlement_event_path, event)

    state.persist_reducer_state(exit_state_path=exit_state_path, state=output.next_state)
    state.note_reducer_sizing(
        decision_ts_ms=decision_ts_ms,
        sizing_equity_usdt=output.sizing_equity_usdt,
    )

    target_book: PublishedTargetBook | None = None
    if output.action is PublicationAction.WRITE:
        if output.target_book_text is None:
            raise RuntimeError("CARRY reducer requested publication without target bytes")
        target_book = publish_target_book(target_book_path, output.target_book_text)
    elif output.target_book_text is not None:
        raise RuntimeError("CARRY reducer returned target bytes for a hold decision")
    return CarryCommitResult(publication_events=events, target_book=target_book)
