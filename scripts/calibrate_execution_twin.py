#!/usr/bin/env python3
"""Create a verified execution-twin calibration receipt from demo tapes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from liquidity_migration.execution_twin_calibration import (
    CalibrationRequirements,
    calibrate_execution_twin,
    write_calibration_receipt,
)


def _clock_offset(path: str, *, now_ns: int, max_age_hours: float) -> tuple[int | None, str]:
    if not path:
        return None, ""
    resolved = Path(path).expanduser()
    data = resolved.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("clock-offset receipt must be a JSON object")
    if not str(value.get("source") or "").strip():
        raise ValueError("clock-offset receipt requires a source")
    observed_ns = int(value.get("observed_ts_ns") or 0)
    if observed_ns <= 0 or observed_ns > now_ns:
        raise ValueError("clock-offset receipt has an invalid observation time")
    if now_ns - observed_ns > max_age_hours * 3_600_000_000_000:
        raise ValueError("clock-offset receipt is stale")
    correction = int(value["local_minus_exchange_ns"])
    return correction, hashlib.sha256(data).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate the market-order twin from verified Bybit demo tapes"
    )
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--market-capture-root", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--clock-offset-receipt", default="")
    parser.add_argument("--max-clock-offset-age-hours", type=float, default=24.0)
    parser.add_argument("--min-feed-samples", type=int, default=5_000)
    parser.add_argument("--min-target-events", type=int, default=30)
    parser.add_argument("--min-order-commands", type=int, default=30)
    parser.add_argument("--min-request-ack-samples", type=int, default=30)
    parser.add_argument("--min-filled-orders", type=int, default=30)
    parser.add_argument("--min-pnl-events", type=int, default=10)
    parser.add_argument("--min-symbols", type=int, default=3)
    parser.add_argument("--min-context-link-ratio", type=float, default=0.95)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_clock_offset_age_hours <= 0.0:
        parser.error("--max-clock-offset-age-hours must be positive")
    now_ns = time.time_ns()
    try:
        correction_ns, correction_hash = _clock_offset(
            args.clock_offset_receipt,
            now_ns=now_ns,
            max_age_hours=args.max_clock_offset_age_hours,
        )
        requirements = CalibrationRequirements(
            min_feed_samples=args.min_feed_samples,
            min_target_events=args.min_target_events,
            min_order_commands=args.min_order_commands,
            min_request_ack_samples=args.min_request_ack_samples,
            min_filled_orders=args.min_filled_orders,
            min_pnl_events=args.min_pnl_events,
            min_symbols=args.min_symbols,
            min_context_link_ratio=args.min_context_link_ratio,
        )
        receipt = calibrate_execution_twin(
            account_root=args.account_root,
            market_capture_root=args.market_capture_root,
            expected_account_id=args.account_id,
            observed_ts_ns=now_ns,
            local_minus_exchange_ns=correction_ns,
            clock_offset_receipt_sha256=correction_hash,
            requirements=requirements,
        )
        output = write_calibration_receipt(args.output, receipt)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"execution-twin calibration failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(output),
        "artifact_sha256": receipt["artifact_sha256"],
        "sample_counts": receipt["sample_counts"],
        "sample_gate": receipt["sample_gate"],
        "execution_twin_gate_passed": receipt["execution_twin_gate_passed"],
    }, sort_keys=True))
    return 0 if receipt["execution_twin_gate_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
