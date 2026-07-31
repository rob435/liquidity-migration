"""Compare what the demo and paper fleets are actually trying to hold.

Reads both fleets' target-scheduling capture tapes, reduces each to the book
that fleet last asked for, and reports any symbol only one side wants or any
notional differing by more than the tolerance.

Two properties shape the comparison:

* The tapes record publication, not acceptance, so a target risk rejected still
  appears. The question here is whether the two fleets *decided* the same thing;
  acceptance differences belong to the twin calibration.
* A mirrored fleet may be legitimately scaled. Mirrored intents carry
  ``mirror_scale`` in their metadata and are normalised by it, so a deliberately
  smaller paper book is not a thousand disagreements. A verbatim mirror declares
  1.0 and the normalisation is a no-op.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from liquidity_migration.strategy.strategy_target_replay import (
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture,
)

#: Unscaled fleets should agree exactly; the tolerance covers the scaled-mirror
#: case, where a float multiply and a slightly different equity read diverge.
DEFAULT_RELATIVE_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class TargetRow:
    sleeve: str
    target_key: str
    symbol: str
    signed_notional_usdt: float
    created_ts_ns: int
    batch_id: str
    mirror_scale: float | None
    target_weight: float | None


@dataclass(frozen=True, slots=True)
class SymbolDisagreement:
    kind: str  # only_in_demo | only_in_paper | notional
    sleeve: str
    symbol: str
    target_key: str
    demo_notional_usdt: float
    paper_notional_usdt: float
    relative_difference: float

    def describe(self) -> str:
        if self.kind == "only_in_demo":
            return f"{self.symbol} held by demo ({self.demo_notional_usdt:,.0f}) but not paper"
        if self.kind == "only_in_paper":
            return f"{self.symbol} held by paper ({self.paper_notional_usdt:,.0f}) but not demo"
        unit = "qty" if self.kind == "quantity" else "usdt"
        return (
            f"{self.symbol} {unit} demo {self.demo_notional_usdt:,.0f} vs paper "
            f"{self.paper_notional_usdt:,.0f} ({self.relative_difference * 100:.1f}%)"
        )


@dataclass(frozen=True, slots=True)
class AgreementReport:
    agree: bool
    sleeves: tuple[str, ...]
    demo_symbols: tuple[str, ...]
    paper_symbols: tuple[str, ...]
    disagreements: tuple[SymbolDisagreement, ...] = ()
    applied_scale: float = 1.0
    unreadable: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def detail(self, *, max_items: int = 4) -> str:
        if self.unreadable:
            return self.unreadable
        if self.agree:
            return f"demo and paper agree on {len(self.demo_symbols)} symbol(s)"
        shown = [item.describe() for item in self.disagreements[:max_items]]
        remaining = len(self.disagreements) - len(shown)
        if remaining > 0:
            shown.append(f"+{remaining} more")
        return "; ".join(shown)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def target_rows(
    events: Iterable[TargetSchedulingCaptureEvent],
    *,
    sleeves: frozenset[str],
) -> list[TargetRow]:
    """Every published component target, in tape order."""

    rows: list[TargetRow] = []
    for event in events:
        if event.sleeve not in sleeves:
            continue
        for captured in sorted(event.requests, key=lambda item: item.publication_order):
            request = captured.request
            for item in request.intents:
                intent = item.intent
                metadata = intent.metadata or {}
                rows.append(
                    TargetRow(
                        sleeve=event.sleeve,
                        target_key=intent.target_key,
                        symbol=intent.symbol.upper(),
                        signed_notional_usdt=float(intent.signed_notional_usdt),
                        created_ts_ns=int(request.created_ts_ns),
                        batch_id=request.batch_id,
                        mirror_scale=_number(metadata.get("mirror_scale")),
                        target_weight=_number(metadata.get("target_weight")),
                    )
                )
    return rows


def latest_book(rows: Iterable[TargetRow]) -> dict[str, TargetRow]:
    """The last target published for each key, keeping only live exposure.

    A zero is how a fleet says "flat": it supersedes an earlier non-zero target
    and then drops out of the book rather than counting as a held symbol.
    """

    latest: dict[str, TargetRow] = {}
    for row in rows:
        seen = latest.get(row.target_key)
        if seen is None or (row.created_ts_ns, row.batch_id) >= (seen.created_ts_ns, seen.batch_id):
            latest[row.target_key] = row
    return {
        key: row for key, row in latest.items() if row.signed_notional_usdt != 0.0
    }


def _declared_scale(rows: Iterable[TargetRow]) -> float:
    """The scale the paper fleet says it is mirroring at.

    A fleet mid-change can carry two scales; the newest wins, and a fleet that
    declares none (it decided for itself) is unscaled.
    """

    scale = 1.0
    newest = -1
    for row in rows:
        if row.mirror_scale is None or not (row.mirror_scale > 0.0):
            continue
        if row.created_ts_ns > newest:
            newest = row.created_ts_ns
            scale = row.mirror_scale
    return scale


def compare_books(
    demo: Mapping[str, TargetRow],
    paper: Mapping[str, TargetRow],
    *,
    sleeves: Iterable[str],
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
    applied_scale: float = 1.0,
) -> AgreementReport:
    if not (applied_scale > 0.0):
        raise ValueError("applied scale must be positive")
    disagreements: list[SymbolDisagreement] = []
    for key in sorted(set(demo) | set(paper)):
        demo_row = demo.get(key)
        paper_row = paper.get(key)
        demo_notional = demo_row.signed_notional_usdt if demo_row else 0.0
        paper_notional = paper_row.signed_notional_usdt if paper_row else 0.0
        row = demo_row or paper_row
        assert row is not None  # one of the two produced the key
        if demo_row is None or paper_row is None:
            disagreements.append(
                SymbolDisagreement(
                    kind="only_in_paper" if demo_row is None else "only_in_demo",
                    sleeve=row.sleeve,
                    symbol=row.symbol,
                    target_key=key,
                    demo_notional_usdt=demo_notional,
                    paper_notional_usdt=paper_notional,
                    relative_difference=1.0,
                )
            )
            continue
        # Undo the mirror's declared scale before comparing, so a deliberately
        # smaller paper book is not reported as a fleet-wide disagreement.
        normalised_paper = paper_notional / applied_scale
        denominator = max(abs(demo_notional), abs(normalised_paper))
        relative = (
            abs(demo_notional - normalised_paper) / denominator if denominator > 0.0 else 0.0
        )
        if relative > relative_tolerance:
            disagreements.append(
                SymbolDisagreement(
                    kind="notional",
                    sleeve=row.sleeve,
                    symbol=row.symbol,
                    target_key=key,
                    demo_notional_usdt=demo_notional,
                    paper_notional_usdt=paper_notional,
                    relative_difference=relative,
                )
            )
    return AgreementReport(
        agree=not disagreements,
        sleeves=tuple(sorted(sleeves)),
        demo_symbols=tuple(sorted({row.symbol for row in demo.values()})),
        paper_symbols=tuple(sorted({row.symbol for row in paper.values()})),
        disagreements=tuple(disagreements),
        applied_scale=applied_scale,
    )


def accepted_quantities(
    state: Any,
    *,
    sleeves: frozenset[str],
) -> dict[str, tuple[str, float]]:
    """``target_key -> (symbol, accepted signed quantity)`` from a journal state.

    The tape says what a fleet asked for in USDT; this says what its account
    owner accepted in venue units. Only the second exposes a basis difference:
    the same notional accepted at a different reference price is a different
    quantity, and stays so until the book is rebuilt.
    """

    accepted: dict[str, tuple[str, float]] = {}
    for target_key, payload in state.component_targets.items():
        sleeve = str(target_key).split("/", 1)[0].lower()
        if sleeve not in sleeves:
            continue
        symbol = str(payload.get("symbol") or "").upper()
        quantity = _number(payload.get("signed_qty"))
        if not symbol or quantity is None or quantity == 0.0:
            continue
        accepted[str(target_key)] = (symbol, quantity)
    return accepted


def compare_accepted_quantities(
    demo: Mapping[str, tuple[str, float]],
    paper: Mapping[str, tuple[str, float]],
    *,
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
    applied_scale: float = 1.0,
) -> tuple[SymbolDisagreement, ...]:
    disagreements: list[SymbolDisagreement] = []
    for key in sorted(set(demo) | set(paper)):
        demo_row = demo.get(key)
        paper_row = paper.get(key)
        sleeve = str(key).split("/", 1)[0].lower()
        if demo_row is None or paper_row is None:
            known = demo_row or paper_row
            assert known is not None
            disagreements.append(
                SymbolDisagreement(
                    kind="only_in_paper" if demo_row is None else "only_in_demo",
                    sleeve=sleeve,
                    symbol=known[0],
                    target_key=key,
                    demo_notional_usdt=demo_row[1] if demo_row else 0.0,
                    paper_notional_usdt=paper_row[1] if paper_row else 0.0,
                    relative_difference=1.0,
                )
            )
            continue
        normalised = paper_row[1] / applied_scale
        denominator = max(abs(demo_row[1]), abs(normalised))
        relative = abs(demo_row[1] - normalised) / denominator if denominator > 0.0 else 0.0
        if relative > relative_tolerance:
            disagreements.append(
                SymbolDisagreement(
                    kind="quantity",
                    sleeve=sleeve,
                    symbol=demo_row[0],
                    target_key=key,
                    demo_notional_usdt=demo_row[1],
                    paper_notional_usdt=paper_row[1],
                    relative_difference=relative,
                )
            )
    return tuple(disagreements)


def evaluate_demo_paper_agreement(
    *,
    demo_tape: str | Path,
    paper_tape: str | Path,
    sleeves: Iterable[str],
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
    demo_account_root: str | Path | None = None,
    paper_account_root: str | Path | None = None,
    demo_account_id: str = "bybit-demo-unified",
    paper_account_id: str = "bybit-paper-unified",
) -> AgreementReport:
    """Report whether the two fleets want, and hold, the same book.

    Supplying both account roots adds the accepted-quantity comparison; it is
    optional so a caller without journal read access still gets the tape answer.
    """

    scoped = frozenset(str(name).strip().lower() for name in sleeves)
    if not scoped:
        raise ValueError("demo/paper agreement needs at least one sleeve")
    try:
        demo_events, _ = load_target_scheduling_capture(demo_tape)
        paper_events, _ = load_target_scheduling_capture(paper_tape)
    except (OSError, ValueError) as exc:
        # An unverifiable tape is not agreement and must not read as agreement.
        return AgreementReport(
            agree=False,
            sleeves=tuple(sorted(scoped)),
            demo_symbols=(),
            paper_symbols=(),
            unreadable=f"target capture unreadable: {type(exc).__name__}: {exc}"[:300],
        )
    demo_rows = target_rows(demo_events, sleeves=scoped)
    paper_rows = target_rows(paper_events, sleeves=scoped)
    scale = _declared_scale(paper_rows)
    report = compare_books(
        latest_book(demo_rows),
        latest_book(paper_rows),
        sleeves=scoped,
        relative_tolerance=relative_tolerance,
        applied_scale=scale,
    )
    if demo_account_root is None or paper_account_root is None:
        return report
    from liquidity_migration.account.account_kernel import AccountExecutionKernel

    try:
        demo_state = AccountExecutionKernel(
            demo_account_root, account_id=demo_account_id
        )._state_ref()
        paper_state = AccountExecutionKernel(
            paper_account_root, account_id=paper_account_id
        )._state_ref()
    except Exception as exc:  # noqa: BLE001 - an unreadable journal is not agreement
        return AgreementReport(
            agree=False,
            sleeves=report.sleeves,
            demo_symbols=report.demo_symbols,
            paper_symbols=report.paper_symbols,
            disagreements=report.disagreements,
            applied_scale=scale,
            unreadable=f"account journal unreadable: {type(exc).__name__}: {exc}"[:300],
        )
    quantity_disagreements = compare_accepted_quantities(
        accepted_quantities(demo_state, sleeves=scoped),
        accepted_quantities(paper_state, sleeves=scoped),
        relative_tolerance=relative_tolerance,
        applied_scale=scale,
    )
    combined = report.disagreements + quantity_disagreements
    return AgreementReport(
        agree=not combined,
        sleeves=report.sleeves,
        demo_symbols=report.demo_symbols,
        paper_symbols=report.paper_symbols,
        disagreements=combined,
        applied_scale=scale,
    )
