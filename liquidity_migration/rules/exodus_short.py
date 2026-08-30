"""Registered Exodus-short rule, record codec, timing, and book rendering.

The independent Exodus producer consumes CARRY's durable typed pre-settlement
events and owns its state transitions and publication. Entries cross the
spread; the venue stop is a disaster fence, never a strategy exit:
every stop level tested from +30 bp to +1500 bp lost money against the
time-boxed cover, because these names wick violently while dying.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liquidity_migration.rules.engine_targets import (
    EngineTarget,
    render_target_book,
)

MIN_MS = 60_000

#: Cash-book validity: with no open shorts the book only has to keep saying
#: "hold nothing", and the engine treats an expired book as entry-closed,
#: which for an empty book changes nothing.
_EMPTY_BOOK_VALIDITY_MS = 6 * 60 * 60_000
_ENGINE_ENTRY_CUTOFF_MS = 15 * MIN_MS


class ExodusShortError(ValueError):
    """A registered exodus-short config this code cannot faithfully run."""


@dataclasses.dataclass(frozen=True)
class ExodusShortConfig:
    """Committed exodus-short rule. Field names mirror the JSON."""

    config_id: str
    accepted_source_profile: str
    accepted_source_config_id: str
    cover_minutes_after_settlement: int
    #: Book validity past the settlement. The engine closes entries 15 min
    #: before a book expires, so 20 here means no fill later than S+5 —
    #: past that the ride left is not worth the round trip.
    entry_valid_minutes_after_settlement: int
    stop_loss_fraction: float

    @classmethod
    def from_json(cls, path: str | Path) -> "ExodusShortConfig":
        payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        rule = payload["rule"]
        trigger = rule["trigger"]
        if trigger.get("basis") != "carry_presettle_exit_fire":
            raise ExodusShortError(
                f"unsupported trigger basis {trigger.get('basis')!r}; "
                "only 'carry_presettle_exit_fire' is implemented"
            )
        accepted_source_profile = trigger.get("source_profile")
        accepted_source_config_id = trigger.get("source_config_id")
        if not isinstance(accepted_source_profile, str) or not accepted_source_profile:
            raise ExodusShortError("trigger source_profile must be a non-empty string")
        if not isinstance(accepted_source_config_id, str) or not accepted_source_config_id:
            raise ExodusShortError("trigger source_config_id must be a non-empty string")
        sizing = rule["sizing"]
        if sizing.get("basis") != "carry_position_at_fire":
            raise ExodusShortError(
                f"unsupported sizing basis {sizing.get('basis')!r}; "
                "only 'carry_position_at_fire' is implemented"
            )
        cover = rule["cover"]
        stop = rule["stop"]
        return cls(
            config_id=payload["config_id"],
            accepted_source_profile=accepted_source_profile,
            accepted_source_config_id=accepted_source_config_id,
            cover_minutes_after_settlement=int(cover["minutes_after_settlement"]),
            entry_valid_minutes_after_settlement=int(
                rule["entry"]["valid_minutes_after_settlement"]
            ),
            stop_loss_fraction=float(stop["stop_loss_fraction"]),
        )


@dataclasses.dataclass(frozen=True)
class ExodusShortRecord:
    """One open short. Magnitudes are positive and frozen at the fire.

    ``target_qty`` is absent only on migrated schema-v1 state; those records
    retain their legacy notional-sizing behavior until they cover.
    """

    symbol: str
    notional_usdt: float
    settlement_ts_ms: int
    fired_ts_ms: int
    target_qty: float | None = None

    def cover_ts_ms(self, cfg: ExodusShortConfig) -> int:
        return self.settlement_ts_ms + cfg.cover_minutes_after_settlement * MIN_MS


def records_to_payload(records: list[ExodusShortRecord]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "open": [
            {
                "symbol": r.symbol,
                "notional_usdt": r.notional_usdt,
                "settlement_ts_ms": r.settlement_ts_ms,
                "fired_ts_ms": r.fired_ts_ms,
                "target_qty": r.target_qty,
            }
            for r in sorted(records, key=lambda r: r.symbol)
        ]
    }


def records_from_payload(raw: Any) -> list[ExodusShortRecord]:
    """Strictly decode durable open-short state.

    Corruption is unknown state, not an empty book. Raising leaves the last
    engine-visible target untouched; silently flattening could close exposure
    from a torn local file without a strategy decision.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("exodus state must contain exactly schema_version and open")
    keys = set(raw)
    if keys == {"open"}:
        # The first deployed writer predated explicit schema versions.  Its
        # row shape is exactly schema v1, so preserve those records with their
        # legacy notional sizing instead of mistaking retained state for
        # corruption.  Every other unversioned shape remains invalid.
        schema_version = 1
    elif keys == {"schema_version", "open"}:
        schema_version = raw["schema_version"]
    else:
        raise ValueError("exodus state must contain exactly schema_version and open")
    if schema_version not in {1, 2} or isinstance(schema_version, bool):
        raise ValueError("unsupported exodus state schema_version")
    rows = raw["open"]
    if not isinstance(rows, list):
        raise ValueError("exodus state open must be an array")
    records: list[ExodusShortRecord] = []
    symbols: set[str] = set()
    expected = {"symbol", "notional_usdt", "settlement_ts_ms", "fired_ts_ms"}
    if schema_version == 2:
        expected.add("target_qty")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError(f"exodus state row {index} has an invalid shape")
        symbol = row["symbol"]
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.upper()
            or not symbol.isalnum()
            or symbol in symbols
        ):
            raise ValueError(f"exodus state row {index} has an invalid or duplicate symbol")
        notional = row["notional_usdt"]
        if (
            isinstance(notional, bool)
            or not isinstance(notional, (int, float))
            or not math.isfinite(float(notional))
            or float(notional) <= 0.0
        ):
            raise ValueError(f"exodus state row {index} has invalid notional_usdt")
        target_qty = row.get("target_qty")
        if target_qty is not None and (
            isinstance(target_qty, bool)
            or not isinstance(target_qty, (int, float))
            or not math.isfinite(float(target_qty))
            or float(target_qty) <= 0.0
        ):
            raise ValueError(f"exodus state row {index} has invalid target_qty")
        settlement = row["settlement_ts_ms"]
        fired = row["fired_ts_ms"]
        if (
            isinstance(settlement, bool)
            or not isinstance(settlement, int)
            or settlement <= 0
            or isinstance(fired, bool)
            or not isinstance(fired, int)
            or fired <= 0
        ):
            raise ValueError(f"exodus state row {index} has invalid timestamps")
        symbols.add(symbol)
        records.append(
            ExodusShortRecord(
                symbol=symbol,
                notional_usdt=float(notional),
                settlement_ts_ms=settlement,
                fired_ts_ms=fired,
                target_qty=(float(target_qty) if target_qty is not None else None),
            )
        )
    return sorted(records, key=lambda record: record.symbol)


def split_due_covers(
    records: list[ExodusShortRecord],
    *,
    now_ms: int,
    cfg: ExodusShortConfig,
) -> tuple[list[ExodusShortRecord], list[ExodusShortRecord]]:
    """(still open, due to cover). Due names simply leave the next book;
    the engine reads absence as the exit and crosses."""
    kept = [r for r in records if now_ms < r.cover_ts_ms(cfg)]
    covered = [r for r in records if now_ms >= r.cover_ts_ms(cfg)]
    return kept, covered


def next_cover_deadline_ts_ms(
    records: list[ExodusShortRecord], cfg: ExodusShortConfig
) -> int | None:
    if not records:
        return None
    return min(r.cover_ts_ms(cfg) for r in records)


def render_exodus_book(
    records: list[ExodusShortRecord],
    *,
    cfg: ExodusShortConfig,
    now_ms: int,
    source: str,
    entry_leverage: float,
    cover_records: list[ExodusShortRecord] | None = None,
    entry_closed_ts_ms_by_symbol: Mapping[str, int] | None = None,
) -> str:
    """The absolute short book: every open record, negative, with the fence
    stop. Book validity runs to the latest settlement while each target has
    its own S+5 entry deadline. The book therefore keeps standing as a hold
    instruction without letting a later record extend an older one's entry.

    ``cover_records`` are explicit zero targets. Naming a due dynamic symbol
    lets a fresh follower find and close it after an engine restart.

    ``entry_leverage`` is the resolved deployment dial from the operational
    profile. Leverage changes margin usage, never the measured economics."""
    closed_entries = dict(entry_closed_ts_ms_by_symbol or {})
    targets = [
        EngineTarget(
            symbol=r.symbol,
            notional_usdt=-abs(r.notional_usdt),
            stop_loss_fraction=cfg.stop_loss_fraction,
            leverage=entry_leverage,
            entry_valid_until_ms=(
                min(
                    r.settlement_ts_ms
                    + cfg.entry_valid_minutes_after_settlement * MIN_MS
                    - _ENGINE_ENTRY_CUTOFF_MS,
                    closed_entries.get(r.symbol, 2**63 - 1),
                )
            ),
            target_qty=(-abs(r.target_qty) if r.target_qty is not None else None),
        )
        for r in records
    ]
    targets.extend(
        EngineTarget(
            symbol=r.symbol,
            notional_usdt=0.0,
            stop_loss_fraction=cfg.stop_loss_fraction,
            leverage=entry_leverage,
        )
        for r in (cover_records or [])
    )
    if records:
        valid_until_ms = (
            max(r.settlement_ts_ms for r in records)
            + cfg.entry_valid_minutes_after_settlement * MIN_MS
        )
        valid_until_ms = max(valid_until_ms, now_ms + MIN_MS)
    else:
        valid_until_ms = now_ms + _EMPTY_BOOK_VALIDITY_MS
    return render_target_book(
        source=source,
        decision_ts_ms=now_ms,
        valid_until_ms=valid_until_ms,
        targets=targets,
    )
