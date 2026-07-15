#!/usr/bin/env python3
"""Capture and reconcile final Bybit demo accounting evidence without orders."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_owner_lease import (  # noqa: E402
    DemoAccountIdentity,
    DemoAccountMutationLease,
)
from liquidity_migration.account_venue_accounting import (  # noqa: E402
    VenueAccountingRequirements,
    build_venue_accounting_receipt,
    require_registered_venue_accounting_requirements,
    write_venue_accounting_receipt,
)
from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    resolve_demo_credentials,
)


def _mapping_rows(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise RuntimeError(f"{label} returned malformed rows")
    return [dict(row) for row in value]


def _open_orders_all_kinds(client: Any) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for source, rows in (
        ("all-kinds", client.get_open_orders(settle_coin="USDT")),
        (
            "conditional",
            client.get_open_orders(
                settle_coin="USDT",
                order_filter="StopOrder",
            ),
        ),
    ):
        for index, row in enumerate(
            _mapping_rows(rows, label=f"Bybit {source} open-orders")
        ):
            identity = str(
                row.get("orderId")
                or row.get("orderLinkId")
                or f"{source}:{index}"
            )
            prior = grouped.get(identity)
            if prior is not None and prior != row:
                raise RuntimeError(
                    f"Bybit open-order views disagree for identity {identity!r}"
                )
            grouped[identity] = row
    return [grouped[key] for key in sorted(grouped)]


def _positions(client: Any) -> list[dict[str, Any]]:
    return _mapping_rows(
        client.get_positions(settle_coin="USDT"),
        label="Bybit positions",
    )


def _requirements(args: argparse.Namespace) -> VenueAccountingRequirements:
    return require_registered_venue_accounting_requirements(
        VenueAccountingRequirements(
            min_trade_rows=args.min_trade_rows,
            min_closed_pnl_rows=args.min_closed_pnl_rows,
            min_funding_rows=args.min_funding_rows,
            quantity_abs_tolerance=args.quantity_abs_tolerance,
            price_abs_tolerance=args.price_abs_tolerance,
            amount_abs_tolerance=args.amount_abs_tolerance,
            relative_tolerance=args.relative_tolerance,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--account-id", default="bybit-demo-unified")
    parser.add_argument("--start-time-ms", type=int, required=True)
    parser.add_argument(
        "--end-time-ms",
        type=int,
        default=0,
        help="Fixed inclusive end time; defaults to capture time after the pre-snapshot.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-trade-rows", type=int, default=2)
    parser.add_argument("--min-closed-pnl-rows", type=int, default=1)
    parser.add_argument("--min-funding-rows", type=int, default=1)
    parser.add_argument("--quantity-abs-tolerance", type=float, default=1e-12)
    parser.add_argument("--price-abs-tolerance", type=float, default=1e-8)
    parser.add_argument("--amount-abs-tolerance", type=float, default=1e-8)
    parser.add_argument("--relative-tolerance", type=float, default=1e-9)
    args = parser.parse_args(argv)
    try:
        requirements = _requirements(args)
    except ValueError as exc:
        parser.error(str(exc))

    account_root = Path(args.account_root).expanduser().resolve(strict=True)
    output = Path(args.output).expanduser().resolve()
    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        raise RuntimeError(
            "BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required"
        )
    client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
    )
    identity = DemoAccountIdentity.from_api_key_info(
        api_key=api_key,
        api_key_info=client.get_api_key_information(),
    )
    lease = DemoAccountMutationLease(identity)
    lease.acquire()
    try:
        pre_positions = _positions(client)
        pre_orders = _open_orders_all_kinds(client)
        query_end_ms = args.end_time_ms or time.time_ns() // 1_000_000
        if args.start_time_ms <= 0 or query_end_ms <= args.start_time_ms:
            parser.error("accounting query window must be positive and increasing")
        if query_end_ms - args.start_time_ms > 7 * 24 * 60 * 60 * 1000:
            parser.error("accounting query window cannot exceed seven days")

        closed_pnl = client.get_closed_pnl(
            start_time_ms=args.start_time_ms,
            end_time_ms=query_end_ms,
            limit=100,
            max_pages=50,
            strict=True,
        )
        trades = client.get_account_transactions(
            transaction_type="TRADE",
            start_time_ms=args.start_time_ms,
            end_time_ms=query_end_ms,
            limit=50,
            max_pages=50,
            strict=True,
        )
        settlements = client.get_account_transactions(
            transaction_type="SETTLEMENT",
            start_time_ms=args.start_time_ms,
            end_time_ms=query_end_ms,
            limit=50,
            max_pages=50,
            strict=True,
        )
        post_positions = _positions(client)
        post_orders = _open_orders_all_kinds(client)
        observed_ts_ns = time.time_ns()

        receipt = build_venue_accounting_receipt(
            account_root=account_root,
            expected_account_id=args.account_id,
            query_start_ms=args.start_time_ms,
            query_end_ms=query_end_ms,
            observed_ts_ns=observed_ts_ns,
            closed_pnl_rows=_mapping_rows(closed_pnl, label="Bybit closed PnL"),
            trade_rows=_mapping_rows(trades, label="Bybit TRADE transactions"),
            settlement_rows=_mapping_rows(
                settlements,
                label="Bybit SETTLEMENT transactions",
            ),
            pre_position_rows=pre_positions,
            pre_open_order_rows=pre_orders,
            post_position_rows=post_positions,
            post_open_order_rows=post_orders,
            requirements=requirements,
        )
        write_venue_accounting_receipt(output, receipt)
    finally:
        lease.close()

    passed = bool(receipt["venue_accounting_gate_passed"])
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "environment": "demo",
                "output": str(output),
                "query_window_ms": receipt["query_window_ms"],
                "sample_counts": receipt["sample_counts"],
                "mismatches": receipt["mismatches"],
                "venue_accounting_gate_passed": passed,
                "final_demo_flatness_gate_passed": receipt[
                    "final_demo_flatness_gate_passed"
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
