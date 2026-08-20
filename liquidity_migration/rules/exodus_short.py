"""The exodus short: ride the fall AFTER a dying crowd-fee print settles.

Measured 2026-08-20 (docs/research/research_findings.md §the exodus short):
when the carry sleeve's pre-settlement exit fires — the running rate says
the deep funding print is dying — the name's price keeps falling for about
an hour past the settlement. Shorting the exact position carry abandons,
at the fire, and covering 60 minutes after the settlement earned +95 bp
mean (+50 median) per event net of the 15.6 bp round trip, on all 1,112
historical book fires, with the 18 real premature fires (the rule fired
but the print stayed deep; a short pays that print, and the worst squeezed
-945 bp) charged at their measured walk-forward rate. The edge lives in
2025-26; 2024 loses slightly and 2021-23 is a wash — a regime trade on the
modern farmer crowd, priced as such in the registered config.

This module owns the sleeve's whole decision surface: a record per fired
name (notional frozen at fire, settlement time), the cover clock, and the
rendered book the engine follows. The carry producer opens records at its
own fire site — the trigger IS carry's fire, so there is no second signal
machine. Entries cross the spread; the venue stop is a disaster fence
(0.35, the same posture as carry's declared stop), never a strategy exit:
every stop level tested from +30 bp to +1500 bp lost money against the
time-boxed cover, because these names wick violently while dying.
"""

from __future__ import annotations

import dataclasses
import json
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


class ExodusShortError(ValueError):
    """A registered exodus-short config this code cannot faithfully run."""


@dataclasses.dataclass(frozen=True)
class ExodusShortConfig:
    """Committed exodus-short rule. Field names mirror the JSON."""

    config_id: str
    cover_minutes_after_settlement: int
    #: Book validity past the settlement. The engine closes entries 15 min
    #: before a book expires, so 20 here means no fill later than S+5 —
    #: past that the ride left is not worth the round trip.
    entry_valid_minutes_after_settlement: int
    stop_loss_fraction: float
    entry_leverage: float

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
        sizing = rule["sizing"]
        if sizing.get("basis") != "carry_notional_at_fire":
            raise ExodusShortError(
                f"unsupported sizing basis {sizing.get('basis')!r}; "
                "only 'carry_notional_at_fire' is implemented"
            )
        cover = rule["cover"]
        stop = rule["stop"]
        return cls(
            config_id=payload["config_id"],
            cover_minutes_after_settlement=int(cover["minutes_after_settlement"]),
            entry_valid_minutes_after_settlement=int(
                rule["entry"]["valid_minutes_after_settlement"]
            ),
            stop_loss_fraction=float(stop["stop_loss_fraction"]),
            entry_leverage=float(rule["entry"]["leverage"]),
        )


@dataclasses.dataclass(frozen=True)
class ExodusShortRecord:
    """One open short. ``notional_usdt`` is the positive magnitude, frozen
    at fire so covers never need an equity read; the book renders it
    negative."""

    symbol: str
    notional_usdt: float
    settlement_ts_ms: int
    fired_ts_ms: int

    def cover_ts_ms(self, cfg: ExodusShortConfig) -> int:
        return self.settlement_ts_ms + cfg.cover_minutes_after_settlement * MIN_MS


def records_to_payload(records: list[ExodusShortRecord]) -> dict[str, Any]:
    return {
        "open": [
            {
                "symbol": r.symbol,
                "notional_usdt": r.notional_usdt,
                "settlement_ts_ms": r.settlement_ts_ms,
                "fired_ts_ms": r.fired_ts_ms,
            }
            for r in sorted(records, key=lambda r: r.symbol)
        ]
    }


def records_from_payload(raw: Any) -> list[ExodusShortRecord]:
    """A torn or half-written state file reads as empty, which the book
    turns into covers — losing state closes shorts, never strands them."""
    records: list[ExodusShortRecord] = []
    try:
        for row in raw.get("open", []):
            records.append(
                ExodusShortRecord(
                    symbol=str(row["symbol"]),
                    notional_usdt=float(row["notional_usdt"]),
                    settlement_ts_ms=int(row["settlement_ts_ms"]),
                    fired_ts_ms=int(row["fired_ts_ms"]),
                )
            )
    except Exception:  # noqa: BLE001 - unreadable state fails toward flat
        return []
    return records


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
) -> str:
    """The absolute short book: every open record, negative, with the fence
    stop. Validity runs to the latest settlement plus the entry window, so
    the engine stops opening once the ride is spent but the book keeps
    standing as the hold instruction until the cover book replaces it."""
    targets = [
        EngineTarget(
            symbol=r.symbol,
            notional_usdt=-abs(r.notional_usdt),
            stop_loss_fraction=cfg.stop_loss_fraction,
            leverage=cfg.entry_leverage,
        )
        for r in records
    ]
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
