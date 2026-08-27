#!/usr/bin/env python3
"""Prove a demo account is flat before a stopped rollout.

The evidence is the venue's own answer, plus the engine's heartbeat for the
phases where the engine is supposed to be running. Until 2026-08-18 this gate
also read the Python account owner's journal and health file; that owner was
deleted on 2026-08-14, both files froze with it, and a gate that reads a
frozen file can never clear — the same could-never-clear class the fleet
watchdog shed on 2026-08-17. It failed closed: every strict rollout would
have blocked on a dead component's last words.

What each phase proves now:

- ``exact`` / ``allow_behind`` (producers or the whole fleet still running):
  the engine heartbeat is recent, for the demo realm, and names an empty
  holdings list — the component that OWNS the account is alive and agrees it
  holds nothing. Then the venue is read directly.
- ``none`` / ``stopped-maintenance`` (everything stopped): the heartbeat is
  legitimately frozen, so only the venue is read. The venue read is the
  direct proof of flatness and runs in every phase.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.runtime.engine_account_health import (  # noqa: E402
    require_recent_engine_account,
)
from liquidity_migration.core.venue_realm import client_venue_realm  # noqa: E402
from liquidity_migration.venue.bybit import (  # noqa: E402
    BybitAccountReader,
    resolve_demo_credentials,
)


MAX_EVIDENCE_AGE_NS = 60_000_000_000


@dataclass(frozen=True, slots=True)
class RolloutReadiness:
    #: Whether this phase demanded (and got) a fresh engine account reading.
    engine_checked: bool
    venue_positions: int
    venue_orders: int


def _amount(row: Mapping[str, Any]) -> float:
    try:
        return abs(float(row.get("size") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _order_identity(row: Mapping[str, Any], *, fallback: str) -> str:
    for key in ("orderId", "orderLinkId"):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return fallback


def require_rollout_readiness(
    *,
    head_binding: str,
    client: Any,
    now_ns: int | None = None,
    heartbeat_path: str | Path | None = None,
) -> RolloutReadiness:
    """Require one flat direct-venue reading, or raise.

    Under ``exact``/``allow_behind`` the engine's own account reading must
    also be recent and empty — a running fleet whose account owner cannot
    say "flat" is not provably flat, whatever a point-in-time venue read
    happens to show.
    """
    if head_binding not in {
        "exact",
        "allow_behind",
        "none",
        "stopped-maintenance",
    }:
        raise ValueError(
            "head binding must be exact, allow_behind, none, or stopped-maintenance"
        )
    client_venue_realm(client, what="rollout readiness")
    problems: list[str] = []

    engine_checked = head_binding in {"exact", "allow_behind"}
    if engine_checked:
        try:
            reading = require_recent_engine_account(
                "demo",
                max_age_ns=MAX_EVIDENCE_AGE_NS,
                now_ns=now_ns,
                path=heartbeat_path,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"engine account reading is not usable: {exc}")
        else:
            if reading.held_symbols is None:
                problems.append(
                    "the engine heartbeat does not say what is held; an engine too "
                    "old to publish positions cannot prove the account flat"
                )
            elif reading.held_symbols:
                problems.append(
                    f"the engine says the account still holds {sorted(reading.held_symbols)}"
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
        engine_checked=engine_checked,
        venue_positions=0,
        venue_orders=0,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-root",
        type=Path,
        required=False,
        help=(
            "Accepted for the deploy's sake and unused: the journal it names "
            "froze when the Python account owner was deleted (2026-08-14)."
        ),
    )
    parser.add_argument(
        "--head-binding",
        choices=("exact", "allow_behind", "none", "stopped-maintenance"),
        required=True,
    )
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        required=False,
        help=(
            "Engine heartbeat to read for the running phases. Defaults to the "
            "demo realm's path, or the ENGINE_ACCOUNT_HEARTBEAT_FILE override."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        api_key, api_secret = resolve_demo_credentials()
        if not api_key or not api_secret:
            raise RuntimeError("demo credentials are unavailable")
        readiness = require_rollout_readiness(
            head_binding=args.head_binding,
            client=BybitAccountReader(
                api_key=api_key,
                api_secret=api_secret,
                demo=True,
            ),
            heartbeat_path=args.heartbeat_file,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rollout-not-ready: {exc}", file=sys.stderr)
        return 1
    print(
        "rollout-flat-ok "
        f"engine_checked={str(readiness.engine_checked).lower()} "
        "venue_positions=0 venue_orders=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
