#!/usr/bin/env python3
"""Passive-vs-taker execution A/B probe on the Bybit demo account (operator-run).

Answers one question with real orders: is the 5.40 bp passive floor mechanically
reachable, or is the measured 15.56 bp taker basis the true cost? Protocol,
metric and sample size are pre-declared in
``liquidity_migration/research/execution/passive_fill_probe.py``; read that first. This probe bounds
the mechanism quickly — only the in-flow A/B grades the flow at signal times.

Operating constraints:
- Demo credentials only, via ``resolve_demo_credentials``.
- ``DemoAccountMutationLease`` refuses to run beside any other mutator,
  including the account owner. Run with the fleet stopped.
- Preflight requires a flat account; every attempt closes its own min-notional
  position before the next begins, and final flatness lands in the receipt.
- Positions are transient, sequential, and bounded at
  ``REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT``.

Usage:
  python scripts/research/probe_passive_fill_ab.py \
      --symbols BTCUSDT,ETHUSDT --demo-rules-file <verified-rules.json> \
      --attempts-per-arm 100 --output <receipt.json> --confirm-demo-probe
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.policy.account_execution_config import (  # noqa: E402
    load_demo_rules,
)
from liquidity_migration.account.account_owner_lease import (  # noqa: E402
    DemoAccountIdentity,
    DemoAccountMutationLease,
)
from liquidity_migration.venue.bybit import (  # noqa: E402
    BybitPrivateClient,
    api_key_allows_order_submit,
    resolve_demo_credentials,
)
from liquidity_migration.marketdata.bybit_market_data import BybitRestRateLimiter  # noqa: E402
from liquidity_migration.core.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.research.execution.passive_fill_probe import (  # noqa: E402
    ARM_POST_ONLY,
    ARM_TAKER,
    REGISTERED_ADVERSE_SELECTION_DELAY_SECONDS,
    REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT,
    REGISTERED_MIN_ATTEMPTS_PER_ARM,
    REGISTERED_POST_ONLY_TIMEOUT_SECONDS,
    TERMINAL_FILLED,
    TERMINAL_REJECTED_WOULD_CROSS,
    TERMINAL_TIMEOUT_CANCELLED,
    AttemptRecord,
    allocate_arm,
    summarize,
)

RECEIPT_KIND = "liquidity_migration_passive_fill_ab_receipt"
RECEIPT_SCHEMA_VERSION = 1
PROBE_LINK_PREFIX = "lm-pfab-"
# The per-side all-in taker cost the research surfaces are priced at — fee AND
# spread. It is the basis the passive arm must beat; it is NOT a fee and must
# never be handed to `itt_cost_bp`, which already charges the spread.
MEASURED_TAKER_ALL_IN_BP_PER_SIDE = 7.78
# What the venue bills a taker, for a symbol whose own fills never reported
# one. Every symbol that did report is priced at what it actually paid.
REGISTERED_TAKER_FEE_BP = 5.5
POLL_SECONDS = 1.0


def _quote(client: Any, symbol: str) -> tuple[float, float]:
    tickers = client.get_tickers(symbol=symbol)
    if not tickers:
        raise RuntimeError(f"{symbol}: ticker unavailable")
    row = tickers[0]
    bid = float(row.get("bid1Price") or 0.0)
    ask = float(row.get("ask1Price") or 0.0)
    if bid <= 0.0 or ask < bid:
        raise RuntimeError(f"{symbol}: malformed quote bid={bid} ask={ask}")
    return bid, ask


def _order_terminal_row(client: Any, *, symbol: str, order_link_id: str) -> dict[str, Any] | None:
    for row in client.get_order_history(symbol=symbol):
        if str(row.get("orderLinkId") or "") == order_link_id:
            return dict(row)
    return None


def _fill_fee_bp(client: Any, *, symbol: str, order_link_id: str) -> tuple[float | None, bool]:
    """Observed execution fee across the order's fills, in bp of executed value."""
    total_fee = 0.0
    total_value = 0.0
    observed = False
    for row in client.get_trade_history(symbol=symbol):
        if str(row.get("orderLinkId") or "") != order_link_id:
            continue
        fee_raw, value_raw = row.get("execFee"), row.get("execValue")
        if fee_raw in (None, "") or value_raw in (None, ""):
            continue
        total_fee += float(fee_raw)
        total_value += float(value_raw)
        observed = True
    if not observed or total_value <= 0.0:
        return None, False
    return total_fee / total_value * 1e4, True


def _flatten(client: Any, *, symbol: str, attempt_index: int) -> None:
    """Reduce-only market close of whatever this attempt opened."""
    for row in client.get_positions(settle_coin="USDT"):
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        size = abs(float(row.get("size") or 0.0))
        side = str(row.get("side") or "")
        if size <= 0.0 or side not in {"Buy", "Sell"}:
            continue
        client.place_order(
            symbol=symbol,
            side="Sell" if side == "Buy" else "Buy",
            orderType="Market",
            qty=str(row.get("size")),
            reduceOnly=True,
            orderLinkId=f"{PROBE_LINK_PREFIX}flat-{attempt_index}-{time.time_ns():x}"[-36:],
        )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        open_size = sum(
            abs(float(row.get("size") or 0.0))
            for row in client.get_positions(settle_coin="USDT")
            if str(row.get("symbol") or "").upper() == symbol
        )
        if open_size <= 0.0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"{symbol}: attempt {attempt_index} could not flatten its probe position")


def _probe_qty(rules: Mapping[str, Any], symbol: str, reference_price: float) -> str:
    rule = rules.get(symbol)
    if rule is None:
        raise RuntimeError(f"{symbol}: no verified demo rule; extend the rules receipt first")
    min_notional = float(rule.min_notional)
    qty_step = float(rule.qty_step)
    if min_notional > REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT:
        raise RuntimeError(
            f"{symbol}: verified min notional {min_notional} exceeds the registered "
            f"attempt bound {REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT}"
        )
    steps = math.ceil((min_notional / reference_price) / qty_step)
    qty = steps * qty_step
    if qty * reference_price > REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT:
        raise RuntimeError(f"{symbol}: minimum executable quantity breaches the attempt bound")
    return f"{qty:.12f}".rstrip("0").rstrip(".")


def _run_attempt(
    client: Any,
    *,
    rules: Mapping[str, Any],
    symbol: str,
    attempt_index: int,
    salt: str,
) -> AttemptRecord:
    side = "Sell"  # registered probe side; every deployed/researched book enters short
    arm = allocate_arm(salt=salt, symbol=symbol, attempt_index=attempt_index)
    bid, ask = _quote(client, symbol)
    qty = _probe_qty(rules, symbol, ask)
    link = f"{PROBE_LINK_PREFIX}{arm[:2]}-{attempt_index}-{time.time_ns():x}"[-36:]
    decision_ts_ns = time.time_ns()
    metadata: dict[str, Any] = {"order_link_id": link, "qty": qty}

    if arm == ARM_TAKER:
        client.place_order(
            symbol=symbol, side=side, orderType="Market", qty=qty, orderLinkId=link
        )
        terminal_state, fill_price = TERMINAL_FILLED, None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and fill_price is None:
            row = _order_terminal_row(client, symbol=symbol, order_link_id=link)
            if row is not None and float(row.get("cumExecQty") or 0.0) > 0.0:
                fill_price = float(row.get("avgPrice") or 0.0)
                break
            time.sleep(POLL_SECONDS)
        if not fill_price:
            raise RuntimeError(f"{symbol}: taker probe {link} never reported a fill")
        terminal_ts_ns = time.time_ns()
    else:
        try:
            client.place_order(
                symbol=symbol,
                side=side,
                orderType="Limit",
                price=f"{ask:.12f}".rstrip("0").rstrip("."),
                qty=qty,
                timeInForce="PostOnly",
                orderLinkId=link,
            )
        except Exception as exc:  # noqa: BLE001 - a would-cross reject is a passive failure
            metadata["reject_error"] = str(exc)[:500]
            terminal_ts_ns = time.time_ns()
            terminal_bid, terminal_ask = _quote(client, symbol)
            return AttemptRecord(
                symbol=symbol,
                arm=arm,
                side=side,
                attempt_index=attempt_index,
                decision_ts_ns=decision_ts_ns,
                decision_bid=bid,
                decision_ask=ask,
                terminal_state=TERMINAL_REJECTED_WOULD_CROSS,
                terminal_ts_ns=terminal_ts_ns,
                terminal_bid=terminal_bid,
                terminal_ask=terminal_ask,
                metadata=metadata,
            )
        terminal_state, fill_price = TERMINAL_TIMEOUT_CANCELLED, None
        deadline = time.monotonic() + REGISTERED_POST_ONLY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            row = _order_terminal_row(client, symbol=symbol, order_link_id=link)
            if row is not None:
                status = str(row.get("orderStatus") or "")
                executed = float(row.get("cumExecQty") or 0.0)
                if executed > 0.0:
                    terminal_state = TERMINAL_FILLED
                    fill_price = float(row.get("avgPrice") or 0.0)
                    break
                if status in {"Cancelled", "Rejected", "Deactivated"}:
                    # PostOnly that would cross reports Cancelled post-create.
                    terminal_state = TERMINAL_REJECTED_WOULD_CROSS
                    break
            time.sleep(POLL_SECONDS)
        if terminal_state == TERMINAL_TIMEOUT_CANCELLED:
            try:
                client.cancel_order(symbol=symbol, order_link_id=link)
            except Exception as exc:  # noqa: BLE001 - a cancel race can mean it just filled
                metadata["cancel_error"] = str(exc)[:500]
            row = _order_terminal_row(client, symbol=symbol, order_link_id=link)
            if row is not None and float(row.get("cumExecQty") or 0.0) > 0.0:
                terminal_state = TERMINAL_FILLED
                fill_price = float(row.get("avgPrice") or 0.0)
        terminal_ts_ns = time.time_ns()

    terminal_bid, terminal_ask = _quote(client, symbol)
    fill_fee_bp: float | None = None
    fee_observed = False
    post_delay_mid: float | None = None
    if terminal_state == TERMINAL_FILLED:
        fill_fee_bp, fee_observed = _fill_fee_bp(client, symbol=symbol, order_link_id=link)
        time.sleep(REGISTERED_ADVERSE_SELECTION_DELAY_SECONDS)
        delay_bid, delay_ask = _quote(client, symbol)
        post_delay_mid = 0.5 * (delay_bid + delay_ask)
        _flatten(client, symbol=symbol, attempt_index=attempt_index)
    return AttemptRecord(
        symbol=symbol,
        arm=arm,
        side=side,
        attempt_index=attempt_index,
        decision_ts_ns=decision_ts_ns,
        decision_bid=bid,
        decision_ask=ask,
        terminal_state=terminal_state,
        terminal_ts_ns=terminal_ts_ns,
        fill_price=fill_price,
        fill_fee_bp=fill_fee_bp,
        fee_observed=fee_observed,
        terminal_bid=terminal_bid,
        terminal_ask=terminal_ask,
        post_delay_mid=post_delay_mid,
        metadata=metadata,
    )


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


def _write_private_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("passive-fill receipt artifact_sha256 is invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve(strict=True)


def _flatness(client: Any) -> dict[str, Any]:
    positions = [
        dict(row)
        for row in client.get_positions(settle_coin="USDT")
        if abs(float(row.get("size") or 0.0)) > 0.0
    ]
    orders = [dict(row) for row in client.get_open_orders(settle_coin="USDT")]
    return {"positions": positions, "open_orders": orders, "flat": not positions and not orders}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated probe symbols")
    parser.add_argument("--demo-rules-file", required=True, help="verified demo-rules receipt")
    parser.add_argument("--output", required=True, help="receipt path (must not exist)")
    parser.add_argument("--attempts-per-arm", type=int, default=REGISTERED_MIN_ATTEMPTS_PER_ARM)
    parser.add_argument("--salt", default="passive-fill-ab-v1", help="arm-allocation salt")
    parser.add_argument("--max-private-requests-per-second", type=int, default=5)
    parser.add_argument("--confirm-demo-probe", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_demo_probe:
        parser.error("--confirm-demo-probe is required because this submits demo orders")
    if args.attempts_per_arm <= 0:
        parser.error("--attempts-per-arm must be positive")
    symbols = sorted({part.strip().upper() for part in args.symbols.split(",") if part.strip()})
    if not symbols:
        parser.error("at least one symbol is required")
    output = Path(args.output).expanduser()
    if os.path.lexists(output):
        raise SystemExit(f"output already exists; preserve it and choose a new path: {output}")

    api_key, api_secret = resolve_demo_credentials()
    if not api_key or not api_secret:
        raise SystemExit("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
    limiter = BybitRestRateLimiter(
        max_requests=args.max_private_requests_per_second, per_seconds=1.0
    )
    bootstrap = BybitPrivateClient(
        category="linear", testnet=False, demo=True,
        api_key=api_key, api_secret=api_secret, rate_limiter=limiter,
    )
    api_key_info = bootstrap.get_api_key_information()
    allowed, reason = api_key_allows_order_submit(api_key_info)
    if not allowed:
        raise SystemExit(f"Bybit demo API key cannot submit probe orders: {reason}")
    identity = DemoAccountIdentity.from_api_key_info(api_key=api_key, api_key_info=api_key_info)
    lease = DemoAccountMutationLease(identity)
    lease.acquire()
    records: list[AttemptRecord] = []
    try:
        client = BybitPrivateClient(
            category="linear", testnet=False, demo=True,
            api_key=api_key, api_secret=api_secret,
            rate_limiter=limiter, mutation_lease=lease,
        )
        rules = load_demo_rules(args.demo_rules_file)
        preflight = _flatness(client)
        if not preflight["flat"]:
            raise SystemExit(
                "passive-fill A/B requires a flat account with no open orders; "
                "stop the fleet and flatten first"
            )
        total = 2 * args.attempts_per_arm
        for attempt_index in range(total):
            symbol = symbols[attempt_index % len(symbols)]
            record = _run_attempt(
                client,
                rules=rules,
                symbol=symbol,
                attempt_index=attempt_index,
                salt=args.salt,
            )
            records.append(record)
            print(
                f"passive-fill-ab-progress attempt={attempt_index + 1}/{total} "
                f"symbol={symbol} arm={record.arm} terminal={record.terminal_state}",
                file=sys.stderr,
                flush=True,
            )
        final = _flatness(client)
        summary = summarize(records, taker_fee_bp=REGISTERED_TAKER_FEE_BP)
        payload: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": RECEIPT_KIND,
            "environment": "demo",
            "verified_ts_ns": time.time_ns(),
            "salt": args.salt,
            "symbols": symbols,
            "attempts_per_arm_requested": args.attempts_per_arm,
            "preflight_flatness": preflight,
            "final_flatness": final,
            "summary": summary,
            "attempts": [asdict(record) for record in records],
            "artifact_sha256": "",
        }
        payload["artifact_sha256"] = _self_hash(payload)
        written = _write_private_receipt(output, payload)
        print(json.dumps({
            "status": "passive_fill_ab_complete",
            "output": str(written),
            "post_only_fill_rate": summary["arms"][ARM_POST_ONLY]["fill_rate"],
            "itt_diff_bp_post_only_minus_taker": summary.get("itt_diff_bp_post_only_minus_taker"),
            "kill_low_fill_rate": summary["kill_low_fill_rate"],
            "kill_no_cost_edge": summary["kill_no_cost_edge"],
        }, sort_keys=True))
        return 0
    except BaseException as exc:
        failure = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "kind": RECEIPT_KIND + "_failure",
            "environment": "demo",
            "failed_ts_ns": time.time_ns(),
            "error": {"type": type(exc).__name__, "message": str(exc)[:4000]},
            "partial_attempts": [asdict(record) for record in records],
            "artifact_sha256": "",
        }
        failure["artifact_sha256"] = _self_hash(failure)
        failure_path = output.with_name(f"{output.name}.failed-{time.time_ns()}.json")
        _write_private_receipt(failure_path, failure)
        print(
            json.dumps({
                "status": "passive_fill_ab_failed",
                "failure_receipt": str(failure_path),
                "error": str(exc)[:1000],
            }, sort_keys=True),
            file=sys.stderr,
        )
        raise
    finally:
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
