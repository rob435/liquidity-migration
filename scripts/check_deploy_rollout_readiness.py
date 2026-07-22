#!/usr/bin/env python3
"""Prove a demo account is flat and coherent before a stopped rollout."""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.account_contracts import AccountEventType  # noqa: E402
from liquidity_migration.account_kernel import (  # noqa: E402
    read_account_journal,
    reduce_account_events,
)
from liquidity_migration.account_owner_health import (  # noqa: E402
    require_recent_account_owner_health,
)
from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    resolve_demo_credentials,
)


MAX_EVIDENCE_AGE_NS = 60_000_000_000
QUANTITY_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class RolloutReadiness:
    journal_sequence: int
    local_positions: int
    aggregate_targets: int
    working_orders: int
    venue_positions: int
    venue_orders: int


def _amount(row: Mapping[str, Any]) -> float:
    for key in ("size", "qty", "positionAmt"):
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Bybit position has invalid {key}: {raw!r}") from exc
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"Bybit position has invalid {key}: {raw!r}")
        return value
    raise ValueError("Bybit position is missing a finite non-negative size")


def _order_identity(row: Mapping[str, Any], *, fallback: str) -> str:
    return str(row.get("orderId") or row.get("orderLinkId") or fallback)


def require_rollout_readiness(
    *,
    account_root: str | Path,
    head_binding: str,
    client: Any,
    now_ns: int | None = None,
) -> RolloutReadiness:
    """Require one flat local/direct-venue snapshot or raise fail-closed."""

    if head_binding not in {
        "exact",
        "allow_behind",
        "none",
        "stopped-maintenance",
    }:
        raise ValueError(
            "head binding must be exact, allow_behind, none, or stopped-maintenance"
        )
    if not bool(getattr(client, "demo", False)):
        raise ValueError("rollout readiness refuses a non-demo venue client")
    root = Path(account_root).expanduser()
    observed_now_ns = time.time_ns() if now_ns is None else int(now_ns)
    events = read_account_journal(root, verify=True)
    state = reduce_account_events(events)
    problems: list[str] = []

    local_positions = {
        symbol: position.signed_qty
        for symbol, position in state.positions.items()
        if abs(position.signed_qty) > QUANTITY_TOLERANCE
    }
    local_targets = {
        symbol: float(quantity)
        for symbol, quantity in state.aggregate_targets.items()
        if abs(float(quantity)) > QUANTITY_TOLERANCE
    }
    working_symbols = sorted(state.working_symbols(tolerance=QUANTITY_TOLERANCE))
    if local_positions:
        problems.append(f"canonical positions are non-flat: {local_positions}")
    if local_targets:
        problems.append(f"canonical aggregate targets are non-flat: {local_targets}")
    if working_symbols:
        problems.append(f"canonical working orders remain: {working_symbols}")

    snapshots = [
        event
        for event in events
        if event.event_type == AccountEventType.VENUE_SNAPSHOT.value
    ]
    if not snapshots:
        problems.append("canonical journal has no venue reconciliation snapshot")
    else:
        latest = max(
            snapshots,
            key=lambda event: int(
                event.payload.get("local_receive_ts_ns") or event.wall_ts_ns
            ),
        )
        observed_ns = int(
            latest.payload.get("local_receive_ts_ns") or latest.wall_ts_ns
        )
        age_ns = observed_now_ns - observed_ns
        if head_binding != "stopped-maintenance" and (
            age_ns < 0 or age_ns > MAX_EVIDENCE_AGE_NS
        ):
            problems.append(
                f"latest venue reconciliation snapshot is not fresh: age_ns={age_ns}"
            )
        if not bool(latest.payload.get("healthy")) or latest.payload.get("mismatches"):
            problems.append(
                "latest venue reconciliation snapshot is unhealthy: "
                f"{latest.payload.get('mismatches')}"
            )
        if any(
            abs(float(value)) > QUANTITY_TOLERANCE
            for value in (latest.payload.get("venue_positions") or {}).values()
        ):
            problems.append("latest authenticated venue snapshot is non-flat")
        if any(
            abs(float(value)) > QUANTITY_TOLERANCE
            for value in (
                latest.payload.get("reconstructed_positions") or {}
            ).values()
        ):
            problems.append("latest venue snapshot reconstruction is non-flat")
        metadata = latest.payload.get("metadata") or {}
        ownership = (
            metadata.get("venue_order_ownership") or {}
            if isinstance(metadata, Mapping)
            else {}
        )
        if (
            not isinstance(ownership, Mapping)
            or ownership.get("status") != "verified"
            or int(ownership.get("unique_orders_observed") or 0) != 0
        ):
            problems.append(
                "latest venue order inventory is not empty and verified: "
                f"{ownership}"
            )

    if head_binding in {"exact", "allow_behind"}:
        require_recent_account_owner_health(
            root,
            environment="demo",
            max_age_ns=MAX_EVIDENCE_AGE_NS,
            now_ns=observed_now_ns,
            head_binding=head_binding,
        )

    venue_positions = [
        row
        for row in client.get_positions(settle_coin="USDT")
        if _amount(row) > 0.0
    ]
    all_orders = list(client.get_open_orders(settle_coin="USDT"))
    conditional_orders = list(
        client.get_open_orders(settle_coin="USDT", order_filter="StopOrder")
    )
    order_identities = {
        _order_identity(row, fallback=f"row:{index}")
        for index, row in enumerate([*all_orders, *conditional_orders])
    }
    if venue_positions:
        summary = [
            f"{row.get('symbol', '?')}:{row.get('side', '?')}:{_amount(row):g}"
            for row in venue_positions[:20]
        ]
        problems.append(f"authenticated Bybit positions are non-flat: {summary}")
    if order_identities:
        problems.append(
            "authenticated Bybit open-order inventory is non-empty: "
            f"count={len(order_identities)}"
        )

    if problems:
        raise RuntimeError("; ".join(problems))
    return RolloutReadiness(
        journal_sequence=state.events_applied,
        local_positions=0,
        aggregate_targets=0,
        working_orders=0,
        venue_positions=0,
        venue_orders=0,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument(
        "--head-binding",
        choices=("exact", "allow_behind", "none", "stopped-maintenance"),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        api_key, api_secret = resolve_demo_credentials()
        if not api_key or not api_secret:
            raise RuntimeError("demo credentials are unavailable")
        readiness = require_rollout_readiness(
            account_root=args.account_root,
            head_binding=args.head_binding,
            client=BybitPrivateClient(
                api_key=api_key,
                api_secret=api_secret,
                demo=True,
            ),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rollout-not-ready: {exc}", file=sys.stderr)
        return 1
    print(
        "rollout-flat-ok "
        f"journal_sequence={readiness.journal_sequence} "
        "positions=0 targets=0 working_orders=0 venue_positions=0 venue_orders=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
