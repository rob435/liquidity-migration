"""Weekly kill-criteria evaluation for the deployed sleeves.

Executable form of ``docs/preregistration/sleeve_kill_criteria_2026-07-20.md``
(LONG/CONTINUOUS) and ``docs/preregistration/carry_sleeve_kill_criteria_2026-07-29.md``
(CARRY): read-only over the verified canonical journal, computes K1
(drawdown), K2 (dead run), and K3 (insufficient sample) per sleeve group and
prints trip/no-trip. It has no operational authority — a trip is executed by
the operator as the registered five-line note + sleeve toggle + change point.

CARRY registers the expressible subset only: K1 (journal drawdown vs 30% of
capital reference x carry notional multiplier) and the forward-days +
cumulative-net core of K2 are computed here; K2's deployed-share (>= 30% of
days) and consecutiveness conditions, K3 (funding received minus price bleed
over any 90-day deployed stretch), and K4 (fewer than 25 deployed days in 180
calendar days) need the venue funding ledger and position history beside the
PNL journal, so they are checked manually at the weekly cadence (see
``MANUAL_CRITERIA``). An uncompleted manual check is "unknown", and unknown
safety-critical state fails closed.

Attribution: per-sleeve realized P&L joins component close rows
(``exit_pnl_key`` from the canonical trade projection) to journal PNL rows.
PNL rows not yet attributed to any component (account-netted reductions) are
reported separately; while their magnitude is material the K1 read is
labelled provisional rather than silently wrong.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from .account_contracts import AccountEvent, AccountEventType

EPOCH_START_UTC = dt.datetime(2026, 7, 19, 14, 0, tzinfo=dt.timezone.utc)
EPOCH_DAY90_UTC = dt.datetime(2026, 10, 17, 14, 0, tzinfo=dt.timezone.utc)

# CARRY forward clock: the 2026-07-29 registration starts the carry window at
# the CARRY deployment change point, not at the 2026-07-19 LONG/CONTINUOUS
# epoch. The rollout receipt's exact commit time lands after this code is
# registered, so the registered calendar day is the executable constant and
# the weekly manual check pins the precise change point.
CARRY_EPOCH_START_UTC = dt.datetime(2026, 7, 29, 0, 0, tzinfo=dt.timezone.utc)
GROUP_EPOCH_START_UTC: dict[str, dt.datetime] = {"carry": CARRY_EPOCH_START_UTC}

# Sleeve groups per the registrations: CONTINUOUS is judged with its hedge;
# CARRY stands alone.
SLEEVE_GROUPS: dict[str, tuple[str, ...]] = {
    "continuous": ("continuous", "hedge"),
    "long": ("long",),
    "carry": ("carry",),
}

# The registration states K1 as a percentage of the operational capital
# reference and prints its evaluation at the then-current 10,000 USDT reference
# ("-500 USDT (-5% of capital reference)", "-400 USDT (-4% of capital
# reference)"). The percentage is the registered quantity; the absolutes were
# only its arithmetic at one reference. Commit 58c3432 scaled the profile 25x
# without touching them, which silently redefined K1 from ~10% of maximum
# deployed gross to ~0.4% -- at 250k equity a single routine 1.5-ATR stop-out
# (~1,100 USDT) exceeded the LONG limit, making a false trip near-certain
# (2026-07-27 audit H3). Deriving the limit from the committed profile keeps the
# registered meaning across any future sizing change; see the amendment note in
# docs/preregistration/sleeve_kill_criteria_2026-07-20.md.
REGISTERED_CAPITAL_REFERENCE_USDT = 10_000.0
# CARRY K1 (2026-07-29 registration): forward drawdown > 30% of the
# operational capital reference x the carry notional multiplier, from the
# forward peak on the canonical journal. Same encoding as the 2026-07-20
# criteria — a registered fraction of the committed reference — with the
# multiplier applied through ``k1_drawdown_limits``.
K1_DRAWDOWN_LIMIT_FRACTION = {"continuous": -0.05, "long": -0.04, "carry": -0.30}
# CARRY K2 (dead run): 120 consecutive forward days with the book deployed on
# >= 30% of days and cumulative net <= 0. The executable subset is the
# 120-forward-day clock (from the carry epoch) + cumulative net <= 0 with at
# least one attributed round trip; deployed-share and consecutiveness are
# manual (see MANUAL_CRITERIA).
K2_MIN_FORWARD_DAYS = {"continuous": 60, "long": 60, "carry": 120}
K2_MIN_ROUND_TRIPS = {"continuous": 30, "long": 40, "carry": 1}
# CARRY has no day-90 sample criterion: its insufficient-sample clause (K4:
# fewer than 25 deployed days in 180 calendar days) is stated in deployed
# days, which the PNL journal alone cannot count, so it is manual-only and
# the group deliberately has NO entry here.
K3_MIN_ROUND_TRIPS_AT_DAY90 = {"continuous": 30, "long": 15}
# LONG only: "fewer than 30 by day 180 retires the demo sleeve for capacity
# reasons, independent of sign".
K3_EXTENSION_DAYS = 90
K3_MIN_ROUND_TRIPS_AT_EXTENSION = {"long": 30}
# CONTINUOUS only: K2/K3 are gated on epoch day 90. LONG K2 evaluates "once 40
# completed round trips exist (whenever that occurs)" -- gating it on day 90
# too let a dead LONG run keep trading for up to two extra months (audit H2).
# CARRY has NO epoch-day-90 gate; its K2 rides the forward-day clock alone.
K2_REQUIRES_DAY90 = {"continuous": True, "long": False, "carry": False}
K2_REQUIRES_MIN_FORWARD_DAYS = {"continuous": False, "long": False, "carry": True}

# Registered clauses the shared journal evaluator cannot express; checked
# manually at the weekly cadence per
# docs/preregistration/carry_sleeve_kill_criteria_2026-07-29.md. An
# uncompleted manual check is "unknown", and unknown fails closed.
MANUAL_CRITERIA: dict[str, tuple[str, ...]] = {
    "carry": (
        "K2 deployed-share: >= 30% of days deployed across the 120-day window,"
        " and the window's day-consecutiveness",
        "K3 mechanism break: funding received minus price bleed (carry_hold.md"
        " s2 attribution) negative over any 90-day deployed stretch",
        "K4 insufficient sample: fewer than 25 deployed days in 180 calendar"
        " days -> no verdict; keep accruing, do not promote",
    ),
}


# The K1 read is provisional while unattributed netted-reduction P&L exceeds
# this share of the tightest drawdown limit.
UNATTRIBUTED_PROVISIONAL_FRACTION = 0.10

# Protection-premium accounting: a PNL row is attributed to a native-stop
# close when its symbol matches and its exchange timestamp lies within this
# window of a verified_native_stop fill.
NATIVE_STOP_MATCH_WINDOW_NS = 180 * 1_000_000_000


def k1_drawdown_limits(
    capital_reference_usdt: float,
    *,
    carry_notional_multiplier: float = 1.0,
) -> dict[str, float]:
    """Registered K1 drawdown limits at a given operational capital reference.

    The carry registration states its 30% against the capital reference
    scaled by the committed carry ``notional_multiplier`` (the sleeve's
    deployed book size), so a sizing change carries the registered meaning
    exactly as the audit-H3 amendment did for the 2026-07-20 criteria.
    """

    if not capital_reference_usdt > 0.0:
        raise ValueError("capital_reference_usdt must be positive")
    if not carry_notional_multiplier > 0.0:
        raise ValueError("carry_notional_multiplier must be positive")
    limits = {
        group: fraction * float(capital_reference_usdt)
        for group, fraction in K1_DRAWDOWN_LIMIT_FRACTION.items()
    }
    limits["carry"] *= float(carry_notional_multiplier)
    return limits


def _native_stop_fills(events: Sequence[AccountEvent]) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != AccountEventType.FILL.value:
            continue
        payload = event.payload
        if str(payload.get("external_execution_origin") or "") != "verified_native_stop":
            continue
        fills.append(
            {
                "symbol": str(event.symbol or payload.get("symbol") or "").upper(),
                "ts_ns": int(payload.get("exchange_ts_ns") or event.wall_ts_ns),
                "fee_usdt": float(payload.get("fee_usdt") or 0.0),
                "execution_id": str(payload.get("execution_id") or ""),
            }
        )
    return fills


def protection_premium_section(
    events: Sequence[AccountEvent],
    *,
    epoch_start_ns: int,
) -> dict[str, Any]:
    """Report native-stop realized cost as an explicit insurance-premium line.

    The venue's native stops are an operational seatbelt research never owned;
    this section attributes their realized P&L so the safety/alpha boundary
    stays visible in the weekly forward record. Attribution: PNL rows matched
    by symbol within ±3 minutes of a verified_native_stop fill (the fill and
    its closed-PnL row are separate journal producers); unmatched fills are
    reported and flag the line provisional rather than silently wrong.
    """
    fills = [f for f in _native_stop_fills(events) if f["ts_ns"] >= epoch_start_ns]
    pnl_rows: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != AccountEventType.PNL.value:
            continue
        payload = event.payload
        ts_ns = int(payload.get("exchange_ts_ns") or event.wall_ts_ns)
        if ts_ns < epoch_start_ns:
            continue
        pnl_rows.append(
            {
                "symbol": str(event.symbol or payload.get("symbol") or "").upper(),
                "ts_ns": ts_ns,
                "net_pnl_usdt": float(payload.get("net_pnl_usdt") or 0.0),
                "pnl_key": str(payload.get("pnl_key") or ""),
            }
        )
    matched_keys: set[str] = set()
    matched_net = 0.0
    unmatched_fills = 0
    for fill in fills:
        candidates = [
            row
            for row in pnl_rows
            if row["symbol"] == fill["symbol"]
            and abs(row["ts_ns"] - fill["ts_ns"]) <= NATIVE_STOP_MATCH_WINDOW_NS
            and row["pnl_key"] not in matched_keys
        ]
        if not candidates:
            unmatched_fills += 1
            continue
        best = min(candidates, key=lambda row: abs(row["ts_ns"] - fill["ts_ns"]))
        matched_keys.add(best["pnl_key"])
        matched_net += best["net_pnl_usdt"]
    return {
        "native_stop_fills_in_epoch": len(fills),
        "symbols": sorted({f["symbol"] for f in fills}),
        "fill_fees_usdt": round(sum(f["fee_usdt"] for f in fills), 8),
        "matched_pnl_rows": len(matched_keys),
        "realized_premium_net_usdt": round(matched_net, 8),
        "unmatched_fills": unmatched_fills,
        "provisional": unmatched_fills > 0,
        "note": (
            "Insurance-premium line: realized P&L of venue-native protection"
            " closes (matched by symbol within ±3 min); negative = premium paid"
            " for the operational seatbelt"
        ),
    }


def _pnl_rows_by_key(events: Sequence[AccountEvent]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type != AccountEventType.PNL.value:
            continue
        payload = event.payload
        key = str(payload.get("pnl_key") or "")
        if key:
            rows[key] = {
                "ts_ns": int(payload.get("exchange_ts_ns") or event.wall_ts_ns),
                "net_pnl_usdt": float(payload.get("net_pnl_usdt") or 0.0),
            }
    return rows


def _peak_to_trough(series: list[tuple[int, float]]) -> float:
    """Worst running-peak-to-value drop of the cumulative sum (<= 0)."""

    running = 0.0
    peak = 0.0
    worst = 0.0
    for _ts, net in sorted(series, key=lambda pair: pair[0]):
        running += net
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def evaluate_kill_criteria(
    *,
    pnl_events: Sequence[AccountEvent],
    trades_by_group: Mapping[str, pl.DataFrame],
    now_utc: dt.datetime,
    capital_reference_usdt: float,
    carry_notional_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Evaluate K1/K2/K3 for each sleeve group from pre-assembled inputs.

    ``capital_reference_usdt`` is the committed operational profile's capital
    reference; K1 limits are the registered percentages of it (the carry limit
    additionally scales by the committed carry ``notional_multiplier``).
    """

    pnl_by_key = _pnl_rows_by_key(pnl_events)
    epoch_start_ns = int(EPOCH_START_UTC.timestamp() * 1e9)
    forward_days = max((now_utc - EPOCH_START_UTC).total_seconds() / 86_400.0, 0.0)
    day90_reached = now_utc >= EPOCH_DAY90_UTC
    extension_reached = forward_days >= 90 + K3_EXTENSION_DAYS
    limits = k1_drawdown_limits(
        capital_reference_usdt,
        carry_notional_multiplier=carry_notional_multiplier,
    )

    attributed_keys: set[str] = set()
    report: dict[str, Any] = {
        "checked_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_start_utc": EPOCH_START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_day90_utc": EPOCH_DAY90_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forward_days": round(forward_days, 2),
        "capital_reference_usdt": round(float(capital_reference_usdt), 2),
        "carry_notional_multiplier": round(float(carry_notional_multiplier), 6),
        "k1_limit_fraction_of_capital_reference": dict(K1_DRAWDOWN_LIMIT_FRACTION),
        "sleeves": {},
    }
    tripped: list[str] = []

    for group, _sleeves in SLEEVE_GROUPS.items():
        # CARRY's forward clock starts at its own deployment epoch; the other
        # groups keep the shared 2026-07-19 epoch (identical behavior).
        group_epoch_start = GROUP_EPOCH_START_UTC.get(group, EPOCH_START_UTC)
        group_epoch_start_ns = int(group_epoch_start.timestamp() * 1e9)
        group_forward_days = max(
            (now_utc - group_epoch_start).total_seconds() / 86_400.0, 0.0
        )
        trades = trades_by_group.get(group, pl.DataFrame())
        series: list[tuple[int, float]] = []
        closed_in_epoch = 0
        if not trades.is_empty():
            for row in trades.iter_rows(named=True):
                key = str(row.get("exit_pnl_key") or "")
                if not key:
                    continue
                pnl_row = pnl_by_key.get(key)
                if pnl_row is None:
                    continue
                attributed_keys.add(key)
                if pnl_row["ts_ns"] >= group_epoch_start_ns:
                    series.append((pnl_row["ts_ns"], pnl_row["net_pnl_usdt"]))
                    closed_in_epoch += 1
        cumulative_net = sum(net for _ts, net in series)
        drawdown = _peak_to_trough(series)
        k1_limit = limits[group]
        k1_tripped = drawdown < k1_limit
        # CONTINUOUS K2 is registered "at epoch day 90"; LONG K2 is registered
        # "once 40 completed round trips exist (whenever that occurs)"; CARRY
        # K2 is registered on 120 forward days from the carry change point.
        k2_applicable = closed_in_epoch >= K2_MIN_ROUND_TRIPS[group]
        if K2_REQUIRES_DAY90[group]:
            k2_applicable = (
                k2_applicable and day90_reached and forward_days >= K2_MIN_FORWARD_DAYS[group]
            )
        elif K2_REQUIRES_MIN_FORWARD_DAYS[group]:
            k2_applicable = (
                k2_applicable and group_forward_days >= K2_MIN_FORWARD_DAYS[group]
            )
        expectancy_per_trade = cumulative_net / closed_in_epoch if closed_in_epoch else 0.0
        k2_tripped = k2_applicable and cumulative_net <= 0.0
        k3_day90_minimum = K3_MIN_ROUND_TRIPS_AT_DAY90.get(group)
        k3_tripped = bool(
            k3_day90_minimum is not None
            and day90_reached
            and closed_in_epoch < k3_day90_minimum
        )
        k3_extension_minimum = K3_MIN_ROUND_TRIPS_AT_EXTENSION.get(group)
        k3_extension_tripped = bool(
            k3_extension_minimum is not None
            and extension_reached
            and closed_in_epoch < k3_extension_minimum
        )
        if k1_tripped:
            tripped.append(f"{group}:K1")
        if k2_tripped:
            tripped.append(f"{group}:K2")
        if k3_tripped:
            tripped.append(f"{group}:K3")
        if k3_extension_tripped:
            tripped.append(f"{group}:K3-extension")
        section: dict[str, Any] = {
            "attributed_round_trips_in_epoch": closed_in_epoch,
            "cumulative_net_usdt": round(cumulative_net, 2),
            "expectancy_per_trade_usdt": round(expectancy_per_trade, 4),
            "peak_to_trough_usdt": round(drawdown, 2),
            "epoch_start_utc": group_epoch_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forward_days": round(group_forward_days, 2),
            "k1": {
                "limit_usdt": round(k1_limit, 2),
                "limit_fraction_of_capital_reference": K1_DRAWDOWN_LIMIT_FRACTION[group],
                "tripped": k1_tripped,
            },
            "k2": {
                "evaluates": (
                    "now"
                    if k2_applicable
                    else (
                        EPOCH_DAY90_UTC.strftime("%Y-%m-%d")
                        if K2_REQUIRES_DAY90[group]
                        else (
                            f"at {K2_MIN_FORWARD_DAYS[group]} forward days"
                            if K2_REQUIRES_MIN_FORWARD_DAYS[group]
                            else f"at {K2_MIN_ROUND_TRIPS[group]} round trips"
                        )
                    )
                ),
                "requires_day90": K2_REQUIRES_DAY90[group],
                "min_round_trips": K2_MIN_ROUND_TRIPS[group],
                "applicable": k2_applicable,
                "tripped": k2_tripped,
            },
            "k3": {
                "evaluates": (
                    "manual_only"
                    if k3_day90_minimum is None
                    else ("now" if day90_reached else EPOCH_DAY90_UTC.strftime("%Y-%m-%d"))
                ),
                "min_round_trips_at_day90": k3_day90_minimum,
                "tripped": k3_tripped,
                "extension_min_round_trips": k3_extension_minimum,
                "extension_tripped": k3_extension_tripped,
            },
        }
        manual = MANUAL_CRITERIA.get(group)
        if manual:
            section["manual_criteria"] = list(manual)
        report["sleeves"][group] = section

    unattributed = [
        row for key, row in pnl_by_key.items()
        if key not in attributed_keys and row["ts_ns"] >= epoch_start_ns
    ]
    unattributed_net = sum(row["net_pnl_usdt"] for row in unattributed)
    tightest_limit = min(abs(value) for value in limits.values())
    provisional = abs(unattributed_net) > UNATTRIBUTED_PROVISIONAL_FRACTION * tightest_limit
    report["unattributed"] = {
        "pnl_rows_in_epoch": len(unattributed),
        "net_usdt": round(unattributed_net, 2),
        "k1_read_provisional": provisional,
        "note": (
            "netted-reduction P&L not yet attributed to a component; a "
            "material amount makes the per-sleeve K1 read provisional"
        ),
    }
    report["protection_premium"] = protection_premium_section(
        pnl_events, epoch_start_ns=epoch_start_ns
    )
    report["tripped"] = tripped
    report["verdict"] = ("TRIP: " + ", ".join(tripped)) if tripped else "NO TRIP"
    return report


def evaluate_kill_criteria_for_root(
    account_root: str,
    *,
    operational_profile_path: str | Path,
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    """Assemble inputs from one verified account root and evaluate.

    The K1 limits are the registered percentages of the *committed* operational
    profile's capital reference, so a sizing change carries the registered
    meaning instead of silently redefining the criterion (audit H3). The profile
    path is required: guessing it, or falling back to the original 10,000 USDT
    reference, would reintroduce exactly the silent-disagreement failure.
    """

    from .account_kernel import read_account_journal
    from .account_strategy_state import canonical_strategy_trade_rows
    from .operational_profile import load_operational_profile

    profile = load_operational_profile(operational_profile_path)
    events = read_account_journal(account_root, verify=True)
    trades_by_group: dict[str, pl.DataFrame] = {}
    for group, sleeves in SLEEVE_GROUPS.items():
        frames = [
            canonical_strategy_trade_rows(account_root, sleeve=sleeve, account_events=events)
            for sleeve in sleeves
        ]
        frames = [frame for frame in frames if not frame.is_empty()]
        trades_by_group[group] = (
            pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
        )
    report = evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group=trades_by_group,
        now_utc=now_utc or dt.datetime.now(tz=dt.timezone.utc),
        capital_reference_usdt=profile.capital_reference_usdt,
        carry_notional_multiplier=profile.carry.notional_multiplier,
    )
    report["operational_profile"] = str(profile.source_path)
    return report
