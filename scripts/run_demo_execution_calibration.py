#!/usr/bin/env python3
"""Run a preregistered target-only demo execution calibration sequence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.account_kernel import read_account_journal
from liquidity_migration.account_route import require_account_route
from liquidity_migration.account_service_runner import load_demo_rules
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.execution_calibration_driver import (
    CalibrationPlan,
    CalibrationStepResult,
    DemoExecutionCalibrationDriver,
    REGISTERED_CALIBRATION_SYMBOLS,
    REGISTERED_HOLD_SECONDS,
    REGISTERED_LEVERAGE,
    REGISTERED_NOTIONAL_USDT,
    REGISTERED_ROUND_TRIPS_PER_SYMBOL,
    require_quantization_safe_minimum_buffer,
    require_registered_calibration_plan,
)


CAPTURE_MARKER = Path("/etc/liquidity-migration/account-execution-capture-enabled")


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _atomic_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    output = path.expanduser()
    if not output.is_absolute():
        raise ValueError("calibration receipt output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    material = {**dict(payload), "artifact_sha256": ""}
    material["artifact_sha256"] = hashlib.sha256(canonical_json(material)).hexdigest()
    data = canonical_json(material) + b"\n"
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("calibration receipt write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise FileExistsError(
                f"calibration receipt already exists; preserve it: {output}"
            ) from exc
        directory = os.open(str(output.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return output


def _journal_head(account_root: str, account_id: str) -> dict[str, object]:
    events = read_account_journal(account_root, verify=True)
    if events and events[-1].account_id != account_id:
        raise RuntimeError("account journal id does not match calibration account")
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    return {
        "event_count": len(events),
        "sequence": events[-1].sequence if events else 0,
        "state_hash": events[-1].state_hash if events else "0" * 64,
        "normalized_sha256": digest.hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a bounded deterministic demo calibration target tape"
    )
    parser.add_argument("--account-root", required=True)
    parser.add_argument("--inbox-root", required=True)
    parser.add_argument("--account-id", default="bybit-demo-unified")
    parser.add_argument("--demo-rules-file", required=True)
    parser.add_argument("--event-tape", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--symbols", default=",".join(REGISTERED_CALIBRATION_SYMBOLS))
    parser.add_argument(
        "--round-trips-per-symbol", type=int, default=REGISTERED_ROUND_TRIPS_PER_SYMBOL
    )
    parser.add_argument("--notional-usdt", type=float, default=REGISTERED_NOTIONAL_USDT)
    parser.add_argument("--leverage", type=float, default=REGISTERED_LEVERAGE)
    parser.add_argument("--hold-seconds", type=float, default=REGISTERED_HOLD_SECONDS)
    parser.add_argument("--funding-symbol", default="")
    parser.add_argument("--funding-close-not-before-ms", type=int, default=0)
    parser.add_argument("--transition-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-demo-calibration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.confirm_demo_calibration:
        parser.error("--confirm-demo-calibration is required because this emits demo orders")
    if any(os.environ.get(name) for name in (
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
    )):
        parser.error("calibration target producer must not receive private API credentials")
    if os.environ.get("REAL_MONEY", "").strip().lower() in {"1", "true", "yes", "on"}:
        parser.error("calibration target producer refuses REAL_MONEY")
    try:
        marker_stat = CAPTURE_MARKER.lstat()
    except FileNotFoundError:
        parser.error(f"capture marker is missing: {CAPTURE_MARKER}")
    if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != os.geteuid():
        parser.error("capture marker must be a regular file owned by the runner")

    expected_commit = args.expected_commit.lower()
    if len(expected_commit) != 40 or any(character not in "0123456789abcdef" for character in expected_commit):
        parser.error("--expected-commit must be a full lowercase commit id")
    try:
        actual_commit = _git_output("rev-parse", "HEAD")
        dirty = _git_output("status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError) as exc:
        parser.error(f"cannot verify calibration source commit: {exc}")
    if actual_commit != expected_commit or dirty:
        parser.error("calibration requires the exact clean expected commit")

    symbols = tuple(
        value.strip().upper() for value in args.symbols.split(",") if value.strip()
    )
    funding_symbol = args.funding_symbol.strip().upper()
    funding_close_ns = int(args.funding_close_not_before_ms) * 1_000_000
    now_ns = time.time_ns()
    if funding_close_ns and not now_ns < funding_close_ns <= now_ns + 24 * 3_600_000_000_000:
        parser.error("funding close time must be in the next 24 hours")

    try:
        plan = CalibrationPlan(
            plan_id=args.plan_id,
            symbols=symbols,
            round_trips_per_symbol=args.round_trips_per_symbol,
            notional_usdt=args.notional_usdt,
            leverage=args.leverage,
            hold_seconds=args.hold_seconds,
            funding_symbol=funding_symbol,
            funding_close_not_before_ts_ns=funding_close_ns,
        )
        require_registered_calibration_plan(plan)
        rules_path = Path(args.demo_rules_file).expanduser().resolve(strict=True)
        rules_bytes = rules_path.read_bytes()
        rules = load_demo_rules(rules_path, max_age_seconds=7 * 24 * 3_600)
        if rules_path.read_bytes() != rules_bytes:
            raise ValueError("demo-rule receipt changed while calibration was binding it")
        rules_payload = json.loads(rules_bytes)
        if not isinstance(rules_payload, Mapping):
            raise ValueError("demo-rule receipt must be an object")
        missing = sorted(set(symbols) - set(rules))
        if missing:
            raise ValueError(f"calibration symbols lack demo rule receipts: {missing}")
        require_quantization_safe_minimum_buffer(
            plan,
            {symbol: rules[symbol].min_notional for symbol in symbols},
        )
        route = require_account_route(
            account_id=args.account_id,
            environment="demo",
            account_root=args.account_root,
            inbox_root=args.inbox_root,
        )
        route_manifest_paths = (
            Path(route.account_root) / "account_route.json",
            Path(route.inbox_root) / "account_route.json",
        )
        route_manifest_hashes = [
            hashlib.sha256(path.read_bytes()).hexdigest() for path in route_manifest_paths
        ]
        if len(set(route_manifest_hashes)) != 1:
            raise ValueError("account route manifest mirrors differ on disk")
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"demo calibration preflight failed: {exc}", file=sys.stderr)
        return 2

    event_tape = Path(args.event_tape).expanduser()
    output = Path(args.output).expanduser()
    if not event_tape.is_absolute() or not output.is_absolute():
        parser.error("event tape and calibration receipt paths must be absolute")
    if event_tape.exists() and not args.resume:
        parser.error("event tape already exists; --resume is required")
    if output.exists():
        parser.error("calibration receipt path already exists; preserve it and choose a new output")

    started_ns = time.time_ns()
    print(json.dumps({
        "calibration_started": True,
        "plan": plan.to_dict(),
        "plan_hash": plan.plan_hash,
        "expected_target_events": len(plan.steps()),
        "source_commit": actual_commit,
    }, sort_keys=True), flush=True)
    driver = DemoExecutionCalibrationDriver(
        route=route,
        tape_path=event_tape,
        transition_timeout_seconds=args.transition_timeout_seconds,
        progress=lambda message: print(message, flush=True),
    )
    status = "failed"
    error = ""
    results: tuple[CalibrationStepResult, ...] = ()
    try:
        results = driver.run(plan, resume=args.resume)
        status = "passed"
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"demo calibration failed: {error}", file=sys.stderr, flush=True)

    try:
        journal_head = _journal_head(args.account_root, args.account_id)
        final_state = driver.kernel.state()
        account_flat = all(
            abs(value) <= 1e-12 for value in final_state.aggregate_targets.values()
        ) and all(
            abs(position.signed_qty) <= 1e-12
            for position in final_state.positions.values()
        ) and not final_state.working_order_ids and not final_state.component_targets
        receipt_payload = {
            "schema_version": 1,
            "purpose": "bounded_demo_market_order_execution_calibration",
            "status": status,
            "error": error,
            "observed_start_ts_ns": started_ns,
            "observed_end_ts_ns": time.time_ns(),
            "source_commit": actual_commit,
            "plan": plan.to_dict(),
            "plan_hash": plan.plan_hash,
            "account_route": route.to_dict(),
            "account_route_manifest_sha256": route_manifest_hashes[0],
            "demo_rules_file": str(rules_path),
            "demo_rules_file_sha256": hashlib.sha256(rules_bytes).hexdigest(),
            "demo_rules_artifact_sha256": str(rules_payload.get("artifact_sha256") or ""),
            "event_tape_path": str(event_tape.resolve()),
            "event_tape_sha256": hashlib.sha256(
                event_tape.read_bytes() if event_tape.exists() else b""
            ).hexdigest(),
            "event_tape_hash": driver.recorder.tape_hash,
            "step_results": [asdict(result) for result in results],
            "journal_head": journal_head,
            "account_flat_after": account_flat,
            "actual_long_continuous_strategy_parity": False,
            "deployment_authority": False,
        }
        receipt_path = _atomic_receipt(output, receipt_payload)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"cannot write demo calibration receipt: {exc}", file=sys.stderr)
        return 4
    print(json.dumps({
        "output": str(receipt_path),
        "status": status,
        "steps": len(results),
        "account_flat_after": account_flat,
    }, sort_keys=True), flush=True)
    return 0 if status == "passed" and account_flat else 3


if __name__ == "__main__":
    raise SystemExit(main())
