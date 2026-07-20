"""R3a book-level daily loss-budget SHADOW governor.

Log-only evaluation of the registered R3a rule
(`docs/preregistration/r3a_loss_budget_experiment_2026-07-20.md`) over the
verified canonical journal: compute realized UTC-day book P&L, decide
whether the frozen threshold is breached, and report what the governor
WOULD do — block new entries for the remainder of the UTC day — without
acting on anything. The A/B arm assignment (UTC-date ordinal parity) is
computed here so the shadow log carries the exact arm the activated
experiment would use.

Runtime status: implemented + tested + staged only. Nothing imports this on
the live path; activation requires the registered A/B design plus a separate
operator go with a recorded change point.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

from .account_contracts import AccountEvent, AccountEventType

CAPITAL_REFERENCE_USDT = 10_000.0  # the sleeve-kill-criteria capital reference
THRESHOLD_FRACTION = -0.015  # frozen from kill-criteria arithmetic (K1 = -5%/epoch)
HALVE_DIAGNOSTIC_FRACTION = -0.0075  # logged diagnostic only; not an arm


def arm_for_utc_date(day: dt.date) -> str:
    """Frozen A/B assignment: odd UTC-date ordinal -> arm A (governor off),
    even ordinal -> arm B (governor on). Ordinal = proleptic Gregorian day
    number (``datetime.date.toordinal``)."""
    return "B" if day.toordinal() % 2 == 0 else "A"


def _day_bounds_ns(now_utc: dt.datetime) -> tuple[int, int, dt.date]:
    day = now_utc.astimezone(dt.timezone.utc).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    start_ns = int(start.timestamp() * 1e9)
    return start_ns, start_ns + 86_400_000_000_000, day


def evaluate_loss_budget_shadow(
    pnl_events: Sequence[AccountEvent],
    *,
    now_utc: dt.datetime,
    capital_reference_usdt: float = CAPITAL_REFERENCE_USDT,
    threshold_fraction: float = THRESHOLD_FRACTION,
) -> dict[str, Any]:
    """Evaluate the shadow decision for the UTC day containing ``now_utc``.

    Realized day P&L = cumulative ``net_pnl_usdt`` over ALL journal PNL rows
    (component closes, netted reductions, funding settlements) ordered by
    exchange timestamp — the book-level realized cash view. The trigger is
    the FIRST crossing of the threshold; a later recovery does not un-trip
    (entry-side block holds until the next UTC day by design).
    """
    if capital_reference_usdt <= 0.0:
        raise ValueError("capital reference must be positive")
    if threshold_fraction >= 0.0:
        raise ValueError("loss-budget threshold must be negative")
    start_ns, end_ns, day = _day_bounds_ns(now_utc)
    threshold_usdt = threshold_fraction * capital_reference_usdt
    halve_usdt = HALVE_DIAGNOSTIC_FRACTION * capital_reference_usdt

    rows: list[tuple[int, float, str]] = []
    for event in pnl_events:
        if event.event_type != AccountEventType.PNL.value:
            continue
        payload: Mapping[str, Any] = event.payload
        ts_ns = int(payload.get("exchange_ts_ns") or event.wall_ts_ns)
        if not start_ns <= ts_ns < end_ns:
            continue
        rows.append((ts_ns, float(payload.get("net_pnl_usdt") or 0.0), str(payload.get("pnl_key") or "")))
    rows.sort(key=lambda item: item[0])

    cumulative = 0.0
    first_breach_ns: int | None = None
    first_halve_ns: int | None = None
    for ts_ns, net, _key in rows:
        cumulative += net
        if first_halve_ns is None and cumulative <= halve_usdt:
            first_halve_ns = ts_ns
        if first_breach_ns is None and cumulative <= threshold_usdt:
            first_breach_ns = ts_ns
    breached = first_breach_ns is not None
    arm = arm_for_utc_date(day)
    return {
        "utc_day": day.isoformat(),
        "arm": arm,
        "capital_reference_usdt": capital_reference_usdt,
        "threshold_usdt": threshold_usdt,
        "pnl_rows_in_day": len(rows),
        "realized_day_net_usdt": round(cumulative, 8),
        "breached": breached,
        "first_breach_utc": (
            None
            if first_breach_ns is None
            else dt.datetime.fromtimestamp(first_breach_ns / 1e9, tz=dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        ),
        "would_block_new_entries_now": breached,
        "armed_experiment_would_block": breached and arm == "B",
        "halve_diagnostic": {
            "threshold_usdt": halve_usdt,
            "crossed": first_halve_ns is not None,
        },
        "acts_on_anything": False,
    }


def evaluate_for_root(
    account_root: str,
    *,
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read the verified journal of one account root and evaluate (read-only)."""
    from .account_kernel import read_account_journal

    events = read_account_journal(account_root, verify=True)
    return evaluate_loss_budget_shadow(
        events, now_utc=now_utc or dt.datetime.now(tz=dt.timezone.utc)
    )
