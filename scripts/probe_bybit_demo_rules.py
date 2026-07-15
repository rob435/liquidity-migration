#!/usr/bin/env python3
"""Generate demo-verified instrument rules from small PostOnly order probes."""

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
from typing import Any, Iterable, Mapping

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_owner_lease import (  # noqa: E402
    DemoAccountIdentity,
    DemoAccountMutationLease,
)
from liquidity_migration.account_candidate_universe import (  # noqa: E402
    CANDIDATE_UNIVERSE_KIND,
    load_candidate_universe,
)
from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    api_key_allows_order_submit,
    resolve_demo_credentials,
    validate_demo_order_permission,
)
from liquidity_migration.bybit_market_data import BybitRestRateLimiter  # noqa: E402
from liquidity_migration.artifact_snapshot import read_stable_file  # noqa: E402
from liquidity_migration.demo_rule_probe import (  # noqa: E402
    DEMO_RULE_PROBE_FAILURE_KIND,
    DEMO_RULE_PROBE_FAILURE_SCHEMA_VERSION,
    DEMO_RULES_KIND,
    DEMO_RULES_SCHEMA_VERSION,
    DemoRuleProbeAttempt,
    REGISTERED_MAX_PRIVATE_REQUESTS_PER_SECOND,
    REGISTERED_MAX_PROBE_NOTIONAL_USDT,
    REGISTERED_PROBE_DISTANCE_BPS,
    probe_demo_instrument_rule,
    require_registered_demo_rule_probe_parameters,
)
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
    snapshot = _flatness_snapshot(client)
    if not snapshot["flat"]:
        raise RuntimeError(
            "demo rule probe requires an entirely flat account with no open orders "
            f"(positions={len(snapshot['positions'])}, orders={len(snapshot['open_orders'])})"
        )


def _flatness_snapshot(client: Any) -> dict[str, Any]:
    positions = [dict(row) for row in _active_positions(client)]
    orders = [dict(row) for row in _open_orders_all_kinds(client)]
    return {
        "observed_ts_ns": time.time_ns(),
        "positions": positions,
        "open_orders": orders,
        "flat": not positions and not orders,
    }


def _cleanup_probe_state(client: Any) -> dict[str, Any]:
    """Cancel probe orders, recover probe fills, and report every exposure seen.

    The caller starts from proved flatness, so any position observed here was
    created during this probe attempt. Recovery is mandatory for safety but it
    does not rescue the evidence gate: any observed position makes the attempt
    fail. The structured report is retained in both passed and failed receipts.
    """

    started_ts_ns = time.time_ns()
    open_orders_observed = [dict(row) for row in _open_orders_all_kinds(client)]
    cancellation_attempts: list[dict[str, Any]] = []
    cleanup_errors: list[str] = []
    for row in open_orders_observed:
        link = str(row.get("orderLinkId") or "")
        symbol = str(row.get("symbol") or "")
        if symbol and link.startswith("lm-demo-rule-"):
            try:
                ack = client.cancel_order(symbol=symbol, order_link_id=link)
            except Exception as exc:  # noqa: BLE001 - retain and fail the attempt
                message = f"cancel {symbol}/{link}: {exc}"
                cleanup_errors.append(message)
                cancellation_attempts.append({
                    "symbol": symbol,
                    "order_id": str(row.get("orderId") or ""),
                    "order_link_id": link,
                    "status": "failed",
                    "error": str(exc)[:1000],
                })
            else:
                cancellation_attempts.append({
                    "symbol": symbol,
                    "order_id": str(row.get("orderId") or ""),
                    "order_link_id": link,
                    "status": "acknowledged",
                    "ack_order_id": str((ack or {}).get("orderId") or "")
                    if isinstance(ack, Mapping)
                    else "",
                    "ack_order_link_id": str((ack or {}).get("orderLinkId") or "")
                    if isinstance(ack, Mapping)
                    else "",
                })
    probe_positions = [dict(row) for row in _active_positions(client)]
    recovery_attempts: list[dict[str, Any]] = []
    for index, row in enumerate(probe_positions, start=1):
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "")
        qty = str(row.get("size") or "")
        if not symbol or not qty or side not in {"Buy", "Sell"}:
            message = f"cannot recover malformed probe position row {index}"
            cleanup_errors.append(message)
            recovery_attempts.append({"status": "failed", "error": message, "position": row})
            continue
        recovery_link = f"lm-rule-flat-{time.time_ns():x}-{index}"[-36:]
        try:
            ack = client.place_order(
                symbol=symbol,
                side="Sell" if side == "Buy" else "Buy",
                orderType="Market",
                qty=qty,
                reduceOnly=True,
                orderLinkId=recovery_link,
            )
        except Exception as exc:  # noqa: BLE001 - retain and fail the attempt
            message = f"flatten {symbol}/{recovery_link}: {exc}"
            cleanup_errors.append(message)
            recovery_attempts.append({
                "symbol": symbol,
                "order_link_id": recovery_link,
                "status": "failed",
                "error": str(exc)[:1000],
            })
        else:
            recovery_attempts.append({
                "symbol": symbol,
                "order_link_id": recovery_link,
                "status": "acknowledged",
                "ack_order_id": str((ack or {}).get("orderId") or "")
                if isinstance(ack, Mapping)
                else "",
                "ack_order_link_id": str((ack or {}).get("orderLinkId") or "")
                if isinstance(ack, Mapping)
                else "",
            })
    deadline = time.monotonic() + 5.0
    final_snapshot = _flatness_snapshot(client)
    while time.monotonic() < deadline:
        final_snapshot = _flatness_snapshot(client)
        if final_snapshot["flat"]:
            break
        time.sleep(0.1)
    return {
        "started_ts_ns": started_ts_ns,
        "completed_ts_ns": time.time_ns(),
        "status": "passed"
        if (
            final_snapshot["flat"]
            and not open_orders_observed
            and not probe_positions
            and not cleanup_errors
        )
        else "failed",
        "open_orders_observed": open_orders_observed,
        "cancellation_attempts": cancellation_attempts,
        "positions_observed": probe_positions,
        "recovery_attempts": recovery_attempts,
        "errors": cleanup_errors,
        "final_flatness": final_snapshot,
    }


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _write_private_receipt(
    path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("probe receipt artifact_sha256 is invalid")
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(
        output,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    created = True
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        directory_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output.resolve(strict=True)


def _write_failure_receipt(
    output_path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    output = Path(output_path).expanduser()
    attempted_ts_ns = int(payload.get("attempted_ts_ns") or time.time_ns())
    for suffix in range(1000):
        marker = f"{attempted_ts_ns}" + (f"-{suffix}" if suffix else "")
        failure_path = output.with_name(f"{output.name}.failed-{marker}.json")
        try:
            return _write_private_receipt(failure_path, payload)
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a unique demo-rule failure receipt path")


def _symbols(values: Iterable[str]) -> list[str]:
    return sorted({part.strip().upper() for value in values for part in value.split(",") if part.strip()})


def _symbols_from_file(path: str | Path) -> tuple[list[str], dict[str, Any]]:
    candidate = Path(path).expanduser()
    snapshot = read_stable_file(
        candidate,
        label="symbol source",
        reject_empty=True,
        require_single_link=False,
    )
    resolved = snapshot.path
    data = snapshot.data
    if not data:
        raise ValueError("symbol source must not be empty")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("symbol source must be UTF-8") from exc
    self_hash_verified = False
    if text.lstrip().startswith(("[", "{")):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("symbol source contains invalid JSON") from exc
        if isinstance(value, dict):
            if value.get("kind") == CANDIDATE_UNIVERSE_KIND:
                frozen = load_candidate_universe(resolved, snapshot=snapshot)
                return list(frozen.symbols), {
                    "kind": "candidate_universe_artifact",
                    "path": str(resolved),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "artifact_sha256": frozen.artifact_sha256,
                    "artifact_self_hash_verified": True,
                }
            raw_symbols = value.get("symbols")
            observed_self_hash = value.get("artifact_sha256")
            if observed_self_hash is not None:
                if not isinstance(observed_self_hash, str):
                    raise ValueError("symbol source artifact_sha256 must be a string")
                expected_self_hash = hashlib.sha256(
                    canonical_json({**value, "artifact_sha256": ""})
                ).hexdigest()
                if observed_self_hash != expected_self_hash:
                    raise ValueError("symbol source artifact_sha256 is invalid")
                self_hash_verified = True
        else:
            raw_symbols = value
        if not isinstance(raw_symbols, list) or any(type(item) is not str for item in raw_symbols):
            raise ValueError("symbol JSON must be a string list or an object with a string-list 'symbols'")
        symbols = _symbols(raw_symbols)
    else:
        symbols = _symbols(text.splitlines())
    if not symbols:
        raise ValueError("symbol source contains no symbols")
    return symbols, {
        "kind": "file",
        "path": str(resolved),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "artifact_self_hash_verified": self_hash_verified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    symbol_group = parser.add_mutually_exclusive_group(required=True)
    symbol_group.add_argument("--symbols", action="append", help="symbol or comma-separated symbols")
    symbol_group.add_argument(
        "--symbols-file",
        help="immutable JSON/newline candidate-universe source; JSON objects use the 'symbols' field",
    )
    parser.add_argument("--output", required=True, help="demo-rules.json artifact path")
    parser.add_argument(
        "--max-probe-notional-usdt",
        type=float,
        default=REGISTERED_MAX_PROBE_NOTIONAL_USDT,
    )
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument(
        "--probe-distance-bps",
        type=float,
        default=REGISTERED_PROBE_DISTANCE_BPS,
        help="place PostOnly buy probes this far below the current bid (default: 100 bps)",
    )
    parser.add_argument(
        "--max-private-requests-per-second",
        type=int,
        default=REGISTERED_MAX_PRIVATE_REQUESTS_PER_SECOND,
        help="shared rate ceiling for authenticated demo reads/mutations (default: 5)",
    )
    parser.add_argument("--confirm-demo-probe", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_demo_probe:
        parser.error("--confirm-demo-probe is required because this submits/cancels demo orders")
    if not math.isfinite(args.leverage) or args.leverage <= 0.0 or args.leverage > 10.0:
        parser.error("--leverage must be positive and cannot exceed registered 10x")
    try:
        require_registered_demo_rule_probe_parameters(
            max_probe_notional_usdt=args.max_probe_notional_usdt,
            probe_distance_bps=args.probe_distance_bps,
            max_private_requests_per_second=args.max_private_requests_per_second,
        )
    except ValueError as exc:
        parser.error(str(exc))
    attempted_ts_ns = time.time_ns()
    output = Path(args.output).expanduser()
    symbols: list[str] = []
    symbol_source: dict[str, Any] = {"status": "not_resolved"}
    client: Any | None = None
    lease: DemoAccountMutationLease | None = None
    preflight_flatness: dict[str, Any] = {"status": "not_run"}
    cleanup: dict[str, Any] = {"status": "not_run"}
    final_flatness: dict[str, Any] = {"status": "not_run"}
    account_identity: dict[str, Any] = {"status": "not_resolved"}
    preflight_proved_flat = False
    observed_ts_ns = 0
    rules: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    partial_attempts: dict[str, list[DemoRuleProbeAttempt]] = {}
    try:
        if os.path.lexists(output):
            raise FileExistsError(
                f"demo-rule output already exists; preserve it and choose a new path: {output}"
            )
        if args.symbols_file:
            symbols, symbol_source = _symbols_from_file(args.symbols_file)
        else:
            symbols = _symbols(args.symbols or ())
            symbol_source = {"kind": "inline", "symbols": symbols}
        if not symbols:
            raise ValueError("at least one symbol is required")
        validate_demo_order_permission(confirm_demo_orders=True)
        api_key, api_secret = resolve_demo_credentials()
        if not api_key or not api_secret:
            raise RuntimeError("BYBIT_DEMO_API_KEY and BYBIT_DEMO_API_SECRET are required")
        limiter = BybitRestRateLimiter(
            max_requests=args.max_private_requests_per_second,
            per_seconds=1.0,
        )
        credential_client = BybitPrivateClient(
            category="linear",
            testnet=False,
            demo=True,
            api_key=api_key,
            api_secret=api_secret,
            rate_limiter=limiter,
        )
        api_key_info = credential_client.get_api_key_information()
        allowed, reason = api_key_allows_order_submit(api_key_info)
        if not allowed:
            raise RuntimeError(f"Bybit demo API key cannot submit probe orders: {reason}")
        identity = DemoAccountIdentity.from_api_key_info(
            api_key=api_key,
            api_key_info=api_key_info,
        )
        account_identity = {
            "status": "authenticated",
            "environment": identity.environment,
            "user_id": identity.user_id,
            "api_key_sha256": identity.api_key_sha256,
        }
        lease = DemoAccountMutationLease(identity)
        lease.acquire()
        client = BybitPrivateClient(
            category="linear",
            testnet=False,
            demo=True,
            api_key=api_key,
            api_secret=api_secret,
            rate_limiter=limiter,
            mutation_lease=lease,
        )
        preflight_flatness = _flatness_snapshot(client)
        if not preflight_flatness["flat"]:
            raise RuntimeError(
                "demo rule probe requires an entirely flat account with no open orders "
                f"(positions={len(preflight_flatness['positions'])}, "
                f"orders={len(preflight_flatness['open_orders'])})"
            )
        preflight_proved_flat = True
        observed_ts_ns = time.time_ns()
        instrument_rows = {
            str(row.get("symbol") or "").upper(): row
            for row in client.get_instruments_info()
        }
        for symbol in symbols:
            instrument = instrument_rows.get(symbol)
            if instrument is None:
                raise RuntimeError(f"{symbol}: absent from api-demo instruments-info")
            tickers = client.get_tickers(symbol=symbol)
            if not tickers:
                raise RuntimeError(f"{symbol}: api-demo ticker is unavailable")
            symbol_attempts: list[DemoRuleProbeAttempt] = []
            partial_attempts[symbol] = symbol_attempts
            rule, receipt = probe_demo_instrument_rule(
                client,
                instrument_row=instrument,
                ticker_row=tickers[0],
                observed_ts_ns=observed_ts_ns,
                max_probe_notional_usdt=args.max_probe_notional_usdt,
                leverage=args.leverage,
                probe_distance_bps=args.probe_distance_bps,
                attempt_sink=symbol_attempts,
            )
            rules[symbol] = asdict(rule)
            evidence[symbol] = receipt.to_dict()

        cleanup = _cleanup_probe_state(client)
        final_flatness = _flatness_snapshot(client)
        if cleanup.get("status") != "passed":
            filled_symbols = sorted({
                str(row.get("symbol") or "UNKNOWN").upper()
                for row in cleanup.get("positions_observed", [])
                if isinstance(row, Mapping)
            })
            raise RuntimeError(
                "demo rule probe cleanup failed or observed a fill/position; "
                "no passed rule receipt will be written "
                f"(symbols={','.join(filled_symbols)}, errors={cleanup.get('errors', [])!r})"
            )
        if not final_flatness.get("flat"):
            raise RuntimeError("demo rule probe final flatness verification failed")

        payload = {
            "schema_version": DEMO_RULES_SCHEMA_VERSION,
            "kind": DEMO_RULES_KIND,
            "status": "passed",
            "environment": "demo",
            "verified_ts_ns": observed_ts_ns,
            "method": (
                "api-demo instruments-info plus terminal Cancelled/zero-fill/"
                "empty-trade-history PostOnly binary search"
            ),
            "minimum_semantics": (
                "min_notional is the smallest terminally Cancelled, zero-fill, "
                "empty-trade-history qty-step notional at probe_price; it is a "
                "conservative upper bound on the hidden threshold"
            ),
            "max_probe_notional_usdt": args.max_probe_notional_usdt,
            "probe_distance_bps": args.probe_distance_bps,
            "max_private_requests_per_second": args.max_private_requests_per_second,
            "symbol_source": symbol_source,
            "official_references": [
                "https://bybit-exchange.github.io/docs/v5/demo",
                "https://bybit-exchange.github.io/docs/v5/order/create-order",
                "https://bybit-exchange.github.io/docs/v5/error",
            ],
            "account_identity": account_identity,
            "preflight_flatness": preflight_flatness,
            "cleanup": cleanup,
            "final_flatness": final_flatness,
            "rules": rules,
            "evidence": evidence,
            "artifact_sha256": "",
        }
        payload["artifact_sha256"] = _self_hash(payload)
        written = _write_private_receipt(output, payload)
        print(json.dumps({
            "status": "bybit_demo_rules_verified",
            "symbols": symbols,
            "output": str(written),
            "observed_min_notional": {
                symbol: rules[symbol]["min_notional"] for symbol in symbols
            },
        }, sort_keys=True))
        return 0
    except BaseException as exc:
        if client is not None and preflight_proved_flat and cleanup.get("status") == "not_run":
            try:
                cleanup = _cleanup_probe_state(client)
            except BaseException as cleanup_exc:  # noqa: BLE001 - receipt must retain cleanup failure
                cleanup = {
                    "status": "failed",
                    "error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"[:2000],
                }
        if client is not None:
            try:
                final_flatness = _flatness_snapshot(client)
            except BaseException as flatness_exc:  # noqa: BLE001 - receipt must retain uncertainty
                final_flatness = {
                    "status": "failed",
                    "flat": False,
                    "error": f"{type(flatness_exc).__name__}: {flatness_exc}"[:2000],
                }
        failure_payload: dict[str, Any] = {
            "schema_version": DEMO_RULE_PROBE_FAILURE_SCHEMA_VERSION,
            "kind": DEMO_RULE_PROBE_FAILURE_KIND,
            "status": "failed",
            "environment": "demo",
            "attempted_ts_ns": attempted_ts_ns,
            "failed_ts_ns": time.time_ns(),
            "observed_ts_ns": observed_ts_ns,
            "requested_output_path": str(output.resolve()),
            "symbols": symbols,
            "symbol_source": symbol_source,
            "account_identity": account_identity,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc)[:4000],
            },
            "preflight_flatness": preflight_flatness,
            "cleanup": cleanup,
            "final_flatness": final_flatness,
            "partial_rules": rules,
            "partial_evidence": evidence,
            "partial_attempts": {
                symbol: [asdict(attempt) for attempt in attempts]
                for symbol, attempts in sorted(partial_attempts.items())
            },
            "artifact_sha256": "",
        }
        failure_payload["artifact_sha256"] = _self_hash(failure_payload)
        failure_path = _write_failure_receipt(output, failure_payload)
        print(
            json.dumps({
                "status": "bybit_demo_rule_probe_failed",
                "failure_receipt": str(failure_path),
                "error": str(exc)[:1000],
            }, sort_keys=True),
            file=sys.stderr,
        )
        raise
    finally:
        if lease is not None:
            lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
