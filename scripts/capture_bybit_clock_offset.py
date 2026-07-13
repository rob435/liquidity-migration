#!/usr/bin/env python3
"""Capture an independently sampled Bybit public clock-offset receipt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from liquidity_migration.clock_offset_receipt import (
    CLOCK_OFFSET_ENDPOINT,
    REGISTERED_MAX_ERROR_NS,
    REGISTERED_MAX_RTT_NS,
    REGISTERED_SAMPLE_COUNT,
    REGISTERED_SELECTED_COUNT,
    capture_clock_offset,
    write_clock_offset_receipt,
)


def _ntp_synchronized() -> bool:
    result = subprocess.run(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "yes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture low-RTT public Bybit server-time clock evidence"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--endpoint", default=CLOCK_OFFSET_ENDPOINT)
    parser.add_argument("--samples", type=int, default=REGISTERED_SAMPLE_COUNT)
    parser.add_argument("--selected-samples", type=int, default=REGISTERED_SELECTED_COUNT)
    parser.add_argument("--interval-seconds", type=float, default=0.05)
    parser.add_argument("--max-rtt-ms", type=float, default=REGISTERED_MAX_RTT_NS / 1_000_000)
    parser.add_argument("--max-error-ms", type=float, default=REGISTERED_MAX_ERROR_NS / 1_000_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if any(os.environ.get(name) for name in (
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
    )):
        parser.error("clock-offset capture must run without private API credentials")
    if args.max_rtt_ms <= 0.0 or args.max_error_ms <= 0.0:
        parser.error("clock-offset bounds must be positive")
    registered = (
        args.endpoint == CLOCK_OFFSET_ENDPOINT
        and args.samples == REGISTERED_SAMPLE_COUNT
        and args.selected_samples == REGISTERED_SELECTED_COUNT
        and int(args.max_rtt_ms * 1_000_000) == REGISTERED_MAX_RTT_NS
        and int(args.max_error_ms * 1_000_000) == REGISTERED_MAX_ERROR_NS
    )
    if not registered:
        parser.error("clock-offset parameters must match the preregistered 21/5 demo contract")

    request = urllib.request.Request(
        args.endpoint,
        headers={"User-Agent": "liquidity-migration-clock-offset-v1"},
        method="GET",
    )

    def request_once() -> bytes:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - fixed HTTPS default
            return response.read()

    try:
        receipt = capture_clock_offset(
            request_once=request_once,
            ntp_synchronized=_ntp_synchronized(),
            endpoint=args.endpoint,
            sample_count=args.samples,
            selected_count=args.selected_samples,
            interval_seconds=args.interval_seconds,
            max_rtt_ns=int(args.max_rtt_ms * 1_000_000),
            max_error_ns=int(args.max_error_ms * 1_000_000),
        )
        output = write_clock_offset_receipt(Path(args.output), receipt)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        print(f"clock-offset capture failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(output),
        "artifact_sha256": receipt["artifact_sha256"],
        "local_minus_exchange_ns": receipt["local_minus_exchange_ns"],
        "estimated_max_error_ns": receipt["estimated_max_error_ns"],
        "clock_offset_gate_passed": receipt["clock_offset_gate_passed"],
    }, sort_keys=True))
    return 0 if receipt["clock_offset_gate_passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
