#!/usr/bin/env python3
"""Generate demo-verified instrument rules from small PostOnly order probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_owner_lease import AccountOwnerLease  # noqa: E402
from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    resolve_private_credentials,
    validate_demo_order_permission,
)
from liquidity_migration.demo_rule_probe import probe_demo_instrument_rule  # noqa: E402
from liquidity_migration.deterministic_serialization import canonical_json  # noqa: E402


def _active_positions(client: Any) -> list[dict[str, Any]]:
    return [
        row
        for row in client.get_positions(settle_coin="USDT")
        if abs(float(row.get("size") or 0.0)) > 0.0
    ]


def _open_orders_all_kinds(client: Any) -> list[dict[str, Any]]:
    """Read and deduplicate both the default and explicit conditional view."""

    grouped: dict[str, dict[str, Any]] = {}
    for source, rows in (
        ("all-kinds", client.get_open_orders(settle_coin="USDT")),
        (
            "conditional",
            client.get_open_orders(settle_coin="USDT", order_filter="StopOrder"),
        ),
    ):
        for index, row in enumerate(rows):
            identity = str(
                row.get("orderId")
                or row.get("orderLinkId")
                or f"{source}:{index}"
            )
            grouped[identity] = dict(row)
    return list(grouped.values())


def _require_flat(client: Any) -> None:
    positions = _active_positions(client)
    orders = _open_orders_all_kinds(client)
    if positions or orders:
        raise RuntimeError(
            "demo rule probe requires an entirely flat account with no open orders "
            f"(positions={len(positions)}, orders={len(orders)})"
        )


def _cleanup_probe_state(client: Any) -> None:
    """Cancel only probe orders and flatten only fills created from a flat start."""

    for row in _open_orders_all_kinds(client):
        link = str(row.get("orderLinkId") or "")
        symbol = str(row.get("symbol") or "")
        if symbol and link.startswith("lm-demo-rule-"):
            try:
                client.cancel_order(symbol=symbol, order_link_id=link)
            except Exception:  # noqa: BLE001 - final flatness audit surfaces failure
                pass
    for index, row in enumerate(_active_positions(client), start=1):
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "")
        qty = str(row.get("size") or "")
        if not symbol or not qty or side not in {"Buy", "Sell"}:
            continue
        client.place_order(
            symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Market",
            qty=qty,
            reduceOnly=True,
            orderLinkId=f"lm-rule-flat-{time.time_ns():x}-{index}"[-36:],
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _active_positions(client) and not [
            row
            for row in _open_orders_all_kinds(client)
            if str(row.get("orderLinkId") or "").startswith("lm-demo-rule-")
        ]:
            return
        time.sleep(0.1)


def _symbols(values: Iterable[str]) -> list[str]:
    return sorted({part.strip().upper() for value in values for part in value.split(",") if part.strip()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", action="append", required=True, help="symbol or comma-separated symbols")
    parser.add_argument("--output", required=True, help="demo-rules.json artifact path")
    parser.add_argument("--account-root", default="data/bybit-account-execution")
    parser.add_argument("--owner-lock", default="")
    parser.add_argument("--max-probe-notional-usdt", type=float, default=20.0)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--confirm-demo-probe", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_demo_probe:
        parser.error("--confirm-demo-probe is required because this submits/cancels demo orders")
    if args.max_probe_notional_usdt <= 0.0 or args.leverage <= 0.0:
        parser.error("probe notional and leverage must be positive")
    symbols = _symbols(args.symbols)
    if not symbols:
        parser.error("at least one symbol is required")

    validate_demo_order_permission(confirm_demo_orders=True)
    api_key, api_secret, demo = resolve_private_credentials()
    if not demo:
        raise RuntimeError("rule probe refuses REAL_MONEY/mainnet credentials")
    if not api_key or not api_secret:
        raise RuntimeError("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
    lease = AccountOwnerLease(
        args.owner_lock
        or str(Path(args.account_root).expanduser() / "account_execution_owner.lock")
    )
    lease.acquire()
    client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=True,
        api_key=api_key,
        api_secret=api_secret,
        account_execution_owner=True,
    )
    _require_flat(client)
    observed_ts_ns = time.time_ns()
    instrument_rows = {
        str(row.get("symbol") or "").upper(): row
        for row in client.get_instruments_info()
    }
    rules: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    try:
        for symbol in symbols:
            instrument = instrument_rows.get(symbol)
            if instrument is None:
                raise RuntimeError(f"{symbol}: absent from api-demo instruments-info")
            tickers = client.get_tickers(symbol=symbol)
            if not tickers:
                raise RuntimeError(f"{symbol}: api-demo ticker is unavailable")
            rule, receipt = probe_demo_instrument_rule(
                client,
                instrument_row=instrument,
                ticker_row=tickers[0],
                observed_ts_ns=observed_ts_ns,
                max_probe_notional_usdt=args.max_probe_notional_usdt,
                leverage=args.leverage,
            )
            rules[symbol] = asdict(rule)
            evidence[symbol] = receipt.to_dict()
    finally:
        _cleanup_probe_state(client)
    _require_flat(client)

    payload = {
        "schema_version": 2,
        "environment": "demo",
        "verified_ts_ns": observed_ts_ns,
        "method": "api-demo instruments-info plus accepted/cancelled PostOnly order-create binary search",
        "minimum_semantics": (
            "min_notional is the smallest accepted qty-step notional at probe_price; "
            "it is a conservative upper bound on the hidden threshold"
        ),
        "max_probe_notional_usdt": args.max_probe_notional_usdt,
        "official_references": [
            "https://bybit-exchange.github.io/docs/v5/demo",
            "https://bybit-exchange.github.io/docs/v5/order/create-order",
            "https://bybit-exchange.github.io/docs/v5/error",
        ],
        "rules": rules,
        "evidence": evidence,
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(json.dumps({
        "status": "bybit_demo_rules_verified",
        "symbols": symbols,
        "output": str(output),
        "observed_min_notional": {
            symbol: rules[symbol]["min_notional"] for symbol in symbols
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
