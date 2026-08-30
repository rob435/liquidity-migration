"""Pure typed Exodus decision contract shared by live and research.

The contract owns only decisions and deterministic serialization. Event-tape
I/O, engine-account reads, durable state, and target publication stay in the
strategy producer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Mapping
from typing import Any

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.exodus_short import (
    MIN_MS,
    ExodusShortConfig,
    ExodusShortRecord,
    next_cover_deadline_ts_ms,
    records_from_payload,
    records_to_payload,
    render_exodus_book,
    split_due_covers,
)


EXODUS_BOOK_SOURCE = "exodus_short"
EXODUS_STATE_SCHEMA_VERSION = 4
EXODUS_DECISION_APPLICATION_ORDER = (
    "persist_staged_state",
    "publish_target_book_bytes",
    "persist_final_state_after_conclusive_flat",
)


def carry_presettlement_event_id(
    *,
    environment: str,
    source_config_id: str,
    decision_ts_ms: int,
    settlement_ts_ms: int,
    symbol: str,
) -> str:
    """Return the durable semantic identity shared by CARRY and Exodus."""

    identity = {
        "schema_version": 1,
        "environment": environment,
        "source_config_id": source_config_id,
        "decision_ts_ms": decision_ts_ms,
        "settlement_ts_ms": settlement_ts_ms,
        "symbol": symbol,
    }
    return "carry-presettlement-" + hashlib.sha256(canonical_json(identity)).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusDecisionConfig:
    """Resolved fields that can change one Exodus decision."""

    profile_name: str
    rule: ExodusShortConfig
    environment: str
    entry_leverage: float

    def __post_init__(self) -> None:
        if not self.profile_name.strip():
            raise ValueError("Exodus profile name is required")
        if not self.environment.strip():
            raise ValueError("Exodus environment is required")
        if not math.isfinite(self.entry_leverage) or self.entry_leverage <= 0.0:
            raise ValueError("Exodus entry leverage must be positive and finite")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "rule": dataclasses.asdict(self.rule),
            "environment": self.environment,
            "entry_leverage": self.entry_leverage,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusTrigger:
    """The exact CARRY handoff fields the Exodus reducer consumes."""

    event_id: str
    environment: str
    source_profile: str
    source_config_id: str
    decision_ts_ms: int
    fired_ts_ms: int
    settlement_ts_ms: int
    symbol: str
    mark_px: float | None
    carry_side: str | None
    carry_qty: float | None

    def __post_init__(self) -> None:
        expected_event_id = carry_presettlement_event_id(
            environment=self.environment,
            source_config_id=self.source_config_id,
            decision_ts_ms=self.decision_ts_ms,
            settlement_ts_ms=self.settlement_ts_ms,
            symbol=self.symbol,
        )
        if self.event_id != expected_event_id:
            raise ValueError("Exodus trigger event id is invalid")
        if not self.environment.strip() or not self.source_profile.strip() or not self.source_config_id.strip():
            raise ValueError("Exodus trigger source identity is incomplete")
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("Exodus trigger symbol must be uppercase alphanumeric")
        if any(
            type(value) is not int or value <= 0
            for value in (self.decision_ts_ms, self.fired_ts_ms, self.settlement_ts_ms)
        ):
            raise ValueError("Exodus trigger timestamps must be positive integers")
        if self.settlement_ts_ms <= self.fired_ts_ms:
            raise ValueError("Exodus trigger settlement must follow its fire")
        if self.carry_side not in {None, "long", "short"}:
            raise ValueError("Exodus trigger carry side is invalid")
        for label, value in (("mark_px", self.mark_px), ("carry_qty", self.carry_qty)):
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise ValueError(f"Exodus trigger {label} must be null or positive and finite")
        if self.carry_side is None and self.carry_qty is not None:
            raise ValueError("Exodus trigger cannot carry quantity without a side")
        if self.carry_side is not None and self.carry_qty is None:
            raise ValueError("Exodus trigger holding is incomplete")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExodusTrigger":
        expected = {
            "event_id",
            "environment",
            "source_profile",
            "source_config_id",
            "decision_ts_ms",
            "fired_ts_ms",
            "settlement_ts_ms",
            "symbol",
            "mark_px",
            "carry_side",
            "carry_qty",
        }
        if set(payload) != expected:
            raise ValueError("Exodus trigger has unexpected or missing fields")
        return cls(
            event_id=str(payload["event_id"]),
            environment=str(payload["environment"]),
            source_profile=str(payload["source_profile"]),
            source_config_id=str(payload["source_config_id"]),
            decision_ts_ms=payload["decision_ts_ms"],
            fired_ts_ms=payload["fired_ts_ms"],
            settlement_ts_ms=payload["settlement_ts_ms"],
            symbol=str(payload["symbol"]),
            mark_px=payload["mark_px"],
            carry_side=(None if payload["carry_side"] is None else str(payload["carry_side"])),
            carry_qty=payload["carry_qty"],
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusState:
    """Durable state owned only by the Exodus producer."""

    open_records: tuple[ExodusShortRecord, ...] = ()
    consumed_event_ids: tuple[str, ...] = ()
    entry_closed_ts_ms_by_symbol: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        symbols = [record.symbol for record in self.open_records]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("Exodus open state must be uniquely sorted by symbol")
        if tuple(sorted(self.consumed_event_ids)) != self.consumed_event_ids:
            raise ValueError("Exodus consumed event ids must be uniquely sorted")
        if len(set(self.consumed_event_ids)) != len(self.consumed_event_ids):
            raise ValueError("Exodus consumed event ids contain duplicates")
        if any(not value.startswith("carry-presettlement-") for value in self.consumed_event_ids):
            raise ValueError("Exodus state contains an invalid consumed event id")
        closed_symbols = [symbol for symbol, _ts_ms in self.entry_closed_ts_ms_by_symbol]
        if closed_symbols != sorted(closed_symbols) or len(closed_symbols) != len(set(closed_symbols)):
            raise ValueError("Exodus closed-entry state must be uniquely sorted")
        if any(
            symbol not in symbols or type(ts_ms) is not int or ts_ms <= 0
            for symbol, ts_ms in self.entry_closed_ts_ms_by_symbol
        ):
            raise ValueError("Exodus closed-entry state has an invalid row")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXODUS_STATE_SCHEMA_VERSION,
            "consumed_event_ids": list(self.consumed_event_ids),
            "entry_closed_ts_ms_by_symbol": {symbol: ts_ms for symbol, ts_ms in self.entry_closed_ts_ms_by_symbol},
            "open": records_to_payload(list(self.open_records))["open"],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExodusState":
        if set(payload) in ({"open"}, {"schema_version", "open"}):
            records = records_from_payload(payload)
            return cls(open_records=tuple(records))
        if set(payload) == {"schema_version", "consumed_event_ids", "open"}:
            if payload["schema_version"] != 3:
                raise ValueError("unsupported Exodus producer state schema")
            closed_payload: Mapping[str, Any] = {}
        elif set(payload) == {
            "schema_version",
            "consumed_event_ids",
            "entry_closed_ts_ms_by_symbol",
            "open",
        }:
            if payload["schema_version"] != EXODUS_STATE_SCHEMA_VERSION:
                raise ValueError("unsupported Exodus producer state schema")
            raw_closed = payload["entry_closed_ts_ms_by_symbol"]
            if not isinstance(raw_closed, Mapping):
                raise ValueError("Exodus entry_closed_ts_ms_by_symbol must be an object")
            closed_payload = raw_closed
        else:
            raise ValueError("Exodus state has unexpected or missing fields")
        consumed = payload["consumed_event_ids"]
        if not isinstance(consumed, list) or any(not isinstance(row, str) for row in consumed):
            raise ValueError("Exodus consumed_event_ids must be an array of strings")
        records = records_from_payload({"schema_version": 2, "open": payload["open"]})
        return cls(
            open_records=tuple(records),
            consumed_event_ids=tuple(consumed),
            entry_closed_ts_ms_by_symbol=tuple(
                sorted(
                    ((str(symbol), ts_ms) for symbol, ts_ms in closed_payload.items()),
                    key=lambda row: row[0],
                )
            ),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusDecisionInput:
    now_ms: int
    events: tuple[ExodusTrigger, ...]
    held_symbols: frozenset[str] | None
    working_entry_symbols: frozenset[str] | None
    held_positions: Mapping[str, tuple[str, float, float]] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusDecisionOutput:
    staged_state: ExodusState
    final_state: ExodusState
    active_records: tuple[ExodusShortRecord, ...]
    cover_records: tuple[ExodusShortRecord, ...]
    opened_event_ids: tuple[str, ...]
    opened_symbols: tuple[str, ...]
    covered_symbols: tuple[str, ...]
    entry_closed_symbols: tuple[str, ...]
    retired_symbols: tuple[str, ...]
    blocked_events: tuple[tuple[str, str], ...]
    target_book_bytes: bytes
    next_cover_ts_ms: int | None


def decide_exodus(
    decision_input: ExodusDecisionInput,
    prior_state: ExodusState,
    config: ExodusDecisionConfig,
) -> ExodusDecisionOutput:
    """Return one pure Exodus state transition and exact target book."""

    if type(decision_input.now_ms) is not int or decision_input.now_ms <= 0:
        raise ValueError("Exodus decision clock must be a positive integer")
    active, covers = split_due_covers(
        list(prior_state.open_records),
        now_ms=decision_input.now_ms,
        cfg=config.rule,
    )
    closed_entries = dict(prior_state.entry_closed_ts_ms_by_symbol)
    retired_symbols: list[str] = []
    retired_records: list[ExodusShortRecord] = []
    newly_closed_entries: list[str] = []
    if (
        decision_input.held_symbols is not None
        and decision_input.working_entry_symbols is not None
        and decision_input.held_positions is not None
    ):
        retained: list[ExodusShortRecord] = []
        for record in active:
            symbol = record.symbol
            if (
                symbol in closed_entries
                and symbol not in decision_input.held_symbols
                and symbol not in decision_input.working_entry_symbols
            ):
                retired_records.append(record)
                retired_symbols.append(symbol)
                continue
            holding = decision_input.held_positions.get(symbol)
            if holding is not None and symbol not in decision_input.working_entry_symbols:
                side, qty, _entry_px = holding
                target_reached = side == "short" and (
                    record.target_qty is None or qty >= record.target_qty * (1.0 - 1e-9)
                )
                if target_reached and symbol not in closed_entries:
                    closed_entries[symbol] = decision_input.now_ms
                    newly_closed_entries.append(symbol)
            retained.append(record)
        active = retained
    active_symbols = {record.symbol for record in active}
    zero_records = sorted(covers + retired_records, key=lambda record: record.symbol)
    cover_symbols = {record.symbol for record in zero_records}
    consumed = set(prior_state.consumed_event_ids)
    opened_ids: list[str] = []
    opened_symbols: list[str] = []
    blocked: list[tuple[str, str]] = []

    for event in decision_input.events:
        if event.event_id in consumed:
            continue
        if event.environment != config.environment:
            blocked.append((event.event_id, "environment_mismatch"))
            continue
        if (
            event.source_profile != config.rule.accepted_source_profile
            or event.source_config_id != config.rule.accepted_source_config_id
        ):
            blocked.append((event.event_id, "incompatible_source"))
            continue
        if decision_input.held_symbols is None or decision_input.working_entry_symbols is None:
            blocked.append((event.event_id, "engine_account_health_unavailable"))
            continue
        if event.symbol in cover_symbols:
            blocked.append((event.event_id, "symbol_cover_pending"))
            continue
        if event.symbol in active_symbols:
            blocked.append((event.event_id, "symbol_already_open"))
            continue
        entry_deadline_ms = event.settlement_ts_ms + (config.rule.entry_valid_minutes_after_settlement - 15) * MIN_MS
        if decision_input.now_ms >= entry_deadline_ms:
            consumed.add(event.event_id)
            blocked.append((event.event_id, "entry_deadline_passed"))
            continue
        if event.carry_side != "long" or event.carry_qty is None or event.mark_px is None:
            consumed.add(event.event_id)
            blocked.append((event.event_id, "no_exact_carry_long"))
            continue
        record = ExodusShortRecord(
            symbol=event.symbol,
            notional_usdt=event.carry_qty * event.mark_px,
            settlement_ts_ms=event.settlement_ts_ms,
            fired_ts_ms=event.fired_ts_ms,
            target_qty=event.carry_qty,
        )
        active.append(record)
        active_symbols.add(event.symbol)
        consumed.add(event.event_id)
        opened_ids.append(event.event_id)
        opened_symbols.append(event.symbol)

    active = sorted(active, key=lambda record: record.symbol)
    covers = sorted(covers, key=lambda record: record.symbol)
    staged_records = sorted(active + zero_records, key=lambda record: record.symbol)
    consumed_ids = tuple(sorted(consumed))
    staged_symbols = {record.symbol for record in staged_records}
    staged_closed = tuple(
        sorted((symbol, ts_ms) for symbol, ts_ms in closed_entries.items() if symbol in staged_symbols)
    )
    staged_state = ExodusState(tuple(staged_records), consumed_ids, staged_closed)

    pending_covers = [
        record
        for record in zero_records
        if decision_input.held_symbols is None
        or decision_input.working_entry_symbols is None
        or record.symbol in decision_input.held_symbols
        or record.symbol in decision_input.working_entry_symbols
    ]
    final_records = tuple(sorted(active + pending_covers, key=lambda record: record.symbol))
    final_symbols = {record.symbol for record in final_records}
    final_state = ExodusState(
        final_records,
        consumed_ids,
        tuple((symbol, ts_ms) for symbol, ts_ms in staged_closed if symbol in final_symbols),
    )
    book = render_exodus_book(
        active,
        cfg=config.rule,
        now_ms=decision_input.now_ms,
        source=EXODUS_BOOK_SOURCE,
        entry_leverage=config.entry_leverage,
        cover_records=zero_records,
        entry_closed_ts_ms_by_symbol=dict(staged_closed),
    ).encode("utf-8")
    return ExodusDecisionOutput(
        staged_state=staged_state,
        final_state=final_state,
        active_records=tuple(active),
        cover_records=tuple(covers),
        opened_event_ids=tuple(opened_ids),
        opened_symbols=tuple(opened_symbols),
        covered_symbols=tuple(record.symbol for record in covers),
        entry_closed_symbols=tuple(sorted(newly_closed_entries)),
        retired_symbols=tuple(sorted(retired_symbols)),
        blocked_events=tuple(blocked),
        target_book_bytes=book,
        next_cover_ts_ms=next_cover_deadline_ts_ms(active, config.rule),
    )
