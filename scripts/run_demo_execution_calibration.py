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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_kernel import (  # noqa: E402
    read_account_journal,
    reduce_account_events,
)
from liquidity_migration.account_route import require_account_route  # noqa: E402
from liquidity_migration.account_execution_config import load_demo_rules  # noqa: E402
from liquidity_migration.artifact_snapshot import (  # noqa: E402
    StableFileSnapshot,
    read_stable_file,
)
from liquidity_migration.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.execution_calibration_driver import (  # noqa: E402
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
from liquidity_migration.strategy_event_clock import (  # noqa: E402
    load_strategy_event_tape_bytes,
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


def _snapshot_identity(
    snapshot: StableFileSnapshot | None,
) -> tuple[object, ...] | None:
    if snapshot is None:
        return None
    return (
        str(snapshot.path),
        snapshot.device,
        snapshot.inode,
        snapshot.metadata.st_mode,
        snapshot.uid,
        snapshot.nlink,
        snapshot.size,
        snapshot.mtime_ns,
        snapshot.metadata.st_ctime_ns,
        snapshot.sha256,
    )


def _event_tape_snapshot(path: Path) -> StableFileSnapshot | None:
    if not os.path.lexists(path):
        return None
    return read_stable_file(
        path,
        label="demo calibration strategy event tape",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )


def _journal_head(account_root: str, account_id: str) -> tuple[dict[str, object], bool]:
    events = read_account_journal(account_root, verify=True)
    if events and events[-1].account_id != account_id:
        raise RuntimeError("account journal id does not match calibration account")
    digest = hashlib.sha256()
    for event in events:
        digest.update(canonical_json(event.to_dict()))
        digest.update(b"\n")
    state = reduce_account_events(events)
    flat = (
        all(abs(value) <= 1e-12 for value in state.aggregate_targets.values())
        and all(
            abs(position.signed_qty) <= 1e-12
            for position in state.positions.values()
        )
        and not state.working_order_ids
        and not state.component_targets
    )
    return ({
        "event_count": len(events),
        "sequence": events[-1].sequence if events else 0,
        "state_hash": events[-1].state_hash if events else "0" * 64,
        "normalized_sha256": digest.hexdigest(),
    }, flat)


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
    real_money = os.environ.get("REAL_MONEY")
    if real_money is not None and real_money.strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }:
        parser.error("calibration target producer requires REAL_MONEY unset or explicitly false")
    try:
        marker_stat = CAPTURE_MARKER.lstat()
    except FileNotFoundError:
        parser.error(f"capture marker is missing: {CAPTURE_MARKER}")
    if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != os.geteuid():
        parser.error("capture marker must be a regular file owned by the runner")
    capture_marker_identity = (
        marker_stat.st_dev,
        marker_stat.st_ino,
        marker_stat.st_mode,
        marker_stat.st_uid,
        marker_stat.st_nlink,
        marker_stat.st_size,
        marker_stat.st_mtime_ns,
        marker_stat.st_ctime_ns,
    )

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
        rules_snapshot = read_stable_file(
            rules_path,
            label="demo calibration rule receipt",
            require_mode=0o600,
            require_owner=True,
            require_single_link=True,
        )
        rules_bytes = rules_snapshot.data
        rules = load_demo_rules(
            rules_path,
            max_age_seconds=7 * 24 * 3_600,
            snapshot=rules_snapshot,
        )
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
        route_manifest_snapshots = tuple(
            read_stable_file(
                path,
                label="demo calibration account route manifest",
                require_mode=0o600,
                require_owner=True,
                require_single_link=True,
            )
            for path in route_manifest_paths
        )
        route_manifest_hashes = [snapshot.sha256 for snapshot in route_manifest_snapshots]
        if len(set(route_manifest_hashes)) != 1:
            raise ValueError("account route manifest mirrors differ on disk")
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"demo calibration preflight failed: {exc}", file=sys.stderr)
        return 2

    event_tape = Path(args.event_tape).expanduser()
    output = Path(args.output).expanduser()
    if not event_tape.is_absolute() or not output.is_absolute():
        parser.error("event tape and calibration receipt paths must be absolute")
    if os.path.lexists(event_tape):
        parser.error("event tape already exists; preserve the failed attempt and register a new epoch")
    if os.path.lexists(output):
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
        results = driver.run(plan, resume=False)
        status = "passed"
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"demo calibration failed: {error}", file=sys.stderr, flush=True)

    try:
        journal_head, account_flat = _journal_head(args.account_root, args.account_id)
        event_tape_snapshot = _event_tape_snapshot(event_tape)
        event_tape_bytes = (
            b"" if event_tape_snapshot is None else event_tape_snapshot.data
        )
        _events, event_tape_hash = load_strategy_event_tape_bytes(
            event_tape_bytes
        )
        if event_tape_hash != driver.recorder.tape_hash:
            raise RuntimeError("demo calibration event tape differs from the recorder head")
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
            "event_tape_sha256": hashlib.sha256(event_tape_bytes).hexdigest(),
            "event_tape_hash": event_tape_hash,
            "step_results": [asdict(result) for result in results],
            "journal_head": journal_head,
            "account_flat_after": account_flat,
            "actual_long_continuous_strategy_parity": False,
            "deployment_authority": False,
        }
        final_commit = _git_output("rev-parse", "HEAD")
        final_dirty = _git_output("status", "--porcelain", "--untracked-files=all")
        final_rules_snapshot = read_stable_file(
            rules_path,
            label="demo calibration rule receipt",
            require_mode=0o600,
            require_owner=True,
            require_single_link=True,
        )
        final_route_snapshots = tuple(
            read_stable_file(
                path,
                label="demo calibration account route manifest",
                require_mode=0o600,
                require_owner=True,
                require_single_link=True,
            )
            for path in route_manifest_paths
        )
        final_event_tape_snapshot = _event_tape_snapshot(event_tape)
        final_marker_stat = CAPTURE_MARKER.lstat()
        final_capture_marker_identity = (
            final_marker_stat.st_dev,
            final_marker_stat.st_ino,
            final_marker_stat.st_mode,
            final_marker_stat.st_uid,
            final_marker_stat.st_nlink,
            final_marker_stat.st_size,
            final_marker_stat.st_mtime_ns,
            final_marker_stat.st_ctime_ns,
        )
        final_journal_head, final_account_flat = _journal_head(
            args.account_root,
            args.account_id,
        )
        if (
            final_commit != actual_commit
            or final_dirty
            or final_capture_marker_identity != capture_marker_identity
            or _snapshot_identity(final_rules_snapshot)
            != _snapshot_identity(rules_snapshot)
            or tuple(map(_snapshot_identity, final_route_snapshots))
            != tuple(map(_snapshot_identity, route_manifest_snapshots))
            or _snapshot_identity(final_event_tape_snapshot)
            != _snapshot_identity(event_tape_snapshot)
            or final_journal_head != journal_head
            or final_account_flat is not account_flat
        ):
            raise RuntimeError("demo calibration source or final account boundary changed before receipt publication")
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
