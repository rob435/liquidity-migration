"""Weekly kill-criteria evaluation for the deployed sleeves.

Executable form of ``docs/preregistration/sleeve_kill_criteria_2026-07-20.md``:
read-only over the verified canonical journal, computes K1 (drawdown), K2
(dead run), and K3 (insufficient sample) per sleeve group and prints
trip/no-trip. It has no operational authority — a trip is executed by the
operator as the registered five-line note + sleeve toggle + change point.

Attribution: per-sleeve realized P&L joins component close rows
(``exit_pnl_key`` from the canonical trade projection) to journal PNL rows.
PNL rows not yet attributed to any component (account-netted reductions) are
reported separately; while their magnitude is material the K1 read is
labelled provisional rather than silently wrong.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Sequence

import polars as pl

from .account_contracts import AccountEvent, AccountEventType

EPOCH_START_UTC = dt.datetime(2026, 7, 19, 14, 0, tzinfo=dt.timezone.utc)
EPOCH_DAY90_UTC = dt.datetime(2026, 10, 17, 14, 0, tzinfo=dt.timezone.utc)

# Sleeve groups per the registration: CONTINUOUS is judged with its hedge.
SLEEVE_GROUPS: dict[str, tuple[str, ...]] = {
    "continuous": ("continuous", "hedge"),
    "long": ("long",),
}
K1_DRAWDOWN_LIMIT_USDT = {"continuous": -500.0, "long": -400.0}
K2_MIN_FORWARD_DAYS = {"continuous": 60, "long": 60}
K2_MIN_ROUND_TRIPS = {"continuous": 30, "long": 40}
K3_MIN_ROUND_TRIPS_AT_DAY90 = {"continuous": 30, "long": 15}

# The K1 read is provisional while unattributed netted-reduction P&L exceeds
# this share of the tightest drawdown limit.
UNATTRIBUTED_PROVISIONAL_FRACTION = 0.10

# R3c protection-premium accounting (tail-risk program P1.4): a PNL row is
# attributed to a native-stop close when its symbol matches and its exchange
# timestamp lies within this window of a verified_native_stop fill.
NATIVE_STOP_MATCH_WINDOW_NS = 180 * 1_000_000_000


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
    """R3c: native-stop realized cost as an explicit insurance-premium line.

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
            "R3c insurance-premium line: realized P&L of venue-native protection"
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
) -> dict[str, Any]:
    """Evaluate K1/K2/K3 for each sleeve group from pre-assembled inputs."""

    pnl_by_key = _pnl_rows_by_key(pnl_events)
    epoch_start_ns = int(EPOCH_START_UTC.timestamp() * 1e9)
    forward_days = max((now_utc - EPOCH_START_UTC).total_seconds() / 86_400.0, 0.0)
    day90_reached = now_utc >= EPOCH_DAY90_UTC

    attributed_keys: set[str] = set()
    report: dict[str, Any] = {
        "checked_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_start_utc": EPOCH_START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_day90_utc": EPOCH_DAY90_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "forward_days": round(forward_days, 2),
        "sleeves": {},
    }
    tripped: list[str] = []

    for group, _sleeves in SLEEVE_GROUPS.items():
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
                if pnl_row["ts_ns"] >= epoch_start_ns:
                    series.append((pnl_row["ts_ns"], pnl_row["net_pnl_usdt"]))
                    closed_in_epoch += 1
        cumulative_net = sum(net for _ts, net in series)
        drawdown = _peak_to_trough(series)
        k1_limit = K1_DRAWDOWN_LIMIT_USDT[group]
        k1_tripped = drawdown < k1_limit
        k2_applicable = (
            day90_reached
            and forward_days >= K2_MIN_FORWARD_DAYS[group]
            and closed_in_epoch >= K2_MIN_ROUND_TRIPS[group]
        )
        k2_tripped = k2_applicable and cumulative_net <= 0.0
        k3_tripped = day90_reached and closed_in_epoch < K3_MIN_ROUND_TRIPS_AT_DAY90[group]
        if k1_tripped:
            tripped.append(f"{group}:K1")
        if k2_tripped:
            tripped.append(f"{group}:K2")
        if k3_tripped:
            tripped.append(f"{group}:K3")
        report["sleeves"][group] = {
            "attributed_round_trips_in_epoch": closed_in_epoch,
            "cumulative_net_usdt": round(cumulative_net, 2),
            "peak_to_trough_usdt": round(drawdown, 2),
            "k1": {"limit_usdt": k1_limit, "tripped": k1_tripped},
            "k2": {
                "evaluates": "now" if day90_reached else EPOCH_DAY90_UTC.strftime("%Y-%m-%d"),
                "min_round_trips": K2_MIN_ROUND_TRIPS[group],
                "tripped": k2_tripped,
            },
            "k3": {
                "evaluates": "now" if day90_reached else EPOCH_DAY90_UTC.strftime("%Y-%m-%d"),
                "min_round_trips_at_day90": K3_MIN_ROUND_TRIPS_AT_DAY90[group],
                "tripped": k3_tripped,
            },
        }

    unattributed = [
        row for key, row in pnl_by_key.items()
        if key not in attributed_keys and row["ts_ns"] >= epoch_start_ns
    ]
    unattributed_net = sum(row["net_pnl_usdt"] for row in unattributed)
    tightest_limit = min(abs(value) for value in K1_DRAWDOWN_LIMIT_USDT.values())
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
    now_utc: dt.datetime | None = None,
) -> dict[str, Any]:
    """Assemble inputs from one verified account root and evaluate."""

    from .account_kernel import read_account_journal
    from .account_strategy_state import canonical_strategy_trade_rows

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
    return evaluate_kill_criteria(
        pnl_events=events,
        trades_by_group=trades_by_group,
        now_utc=now_utc or dt.datetime.now(tz=dt.timezone.utc),
    )
