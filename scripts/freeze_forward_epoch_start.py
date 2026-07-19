#!/usr/bin/env python3
"""Freeze the create-only start boundary for the prospective 90-day epoch."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.artifact_snapshot import read_stable_file  # noqa: E402
from liquidity_migration.deterministic_serialization import canonical_json, json_safe  # noqa: E402
from liquidity_migration.forward_epoch_start import (  # noqa: E402
    EXPECTED_PERSISTENT_UNITS,
    FORWARD_EPOCH_ID,
    build_start_receipt,
    contract_identities,
    load_comparator_verification_receipt,
    load_integrated_comparator_receipt,
    logical_name_hash,
    validate_start_receipt_bytes,
)


DEFAULT_AUTHORIZATION = Path("/etc/liquidity-migration/account-execution-operational-ready")
DEFAULT_MACHINE_ID = Path("/etc/machine-id")
DEFAULT_DEMO_ENV = Path("/etc/liquidity-migration/account-execution.env")
DEFAULT_PAPER_ENV = Path("/etc/liquidity-migration/account-paper-execution.env")
DEFAULT_SYSTEMCTL = Path("/usr/bin/systemctl")
PAPER_GROUP = "liquidity-migration-paper"
PAPER_USER = "liquidity-migration-paper"
TARGET_CAPTURE_FILENAME = "strategy-targets.jsonl"
LEGACY_TARGET_CAPTURE_FILENAME = "strategy_target_scheduling_capture.jsonl"
STRATEGY_EVENT_TAPE_FILENAME = "strategy_event_tape.jsonl"
MAX_OWNER_AGE_NS = 30_000_000_000
MAX_PRODUCER_AGE_NS = 600_000_000_000
_SEGMENT = re.compile(r"segment-[0-9]{6}\.jsonl")
_ATTEMPT_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def _default_receipt(repo: Path) -> Path:
    return repo / "reports" / FORWARD_EPOCH_ID / "forward" / "start" / "receipt.json"


def _default_comparator(repo: Path) -> Path:
    return Path("/var/lib/liquidity-migration/research-evidence/integrated-production-comparator-receipt.json")


def _default_comparator_verification(repo: Path) -> Path:
    return Path("/var/lib/liquidity-migration/research-evidence/integrated-production-comparator-verification.json")


def _strict_directory(
    path: str | Path,
    *,
    label: str,
    expected_uid: int | None = None,
    require_owner_writable: bool = False,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be absolute")
    metadata = candidate.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a real directory")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise ValueError(f"{label} has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ValueError(f"{label} cannot be writable by group or other")
    if require_owner_writable and not stat.S_IMODE(metadata.st_mode) & stat.S_IWUSR:
        raise ValueError(f"{label} must be writable by its owner")
    return candidate.resolve(strict=True)


def _stable_private_snapshot(
    path: Path,
    *,
    label: str,
    expected_uid: int,
    allow_missing: bool = False,
) -> Any | None:
    for attempt in range(8):
        try:
            snapshot = read_stable_file(
                path,
                label=label,
                require_mode=0o600,
                require_owner=False,
                require_single_link=True,
            )
        except ValueError:
            if allow_missing and not path.exists() and not path.is_symlink():
                return None
            raise
        except RuntimeError:
            if attempt == 7:
                raise
            continue
        if snapshot.uid != expected_uid:
            raise ValueError(f"{label} has the wrong owner")
        return snapshot
    raise AssertionError("unreachable stable-snapshot retry")


def _systemd_unit_state(systemctl: Path, unit: str) -> dict[str, Any]:
    properties = (
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "NRestarts",
        "InvocationID",
        "ActiveEnterTimestampMonotonic",
    )
    command = [str(systemctl), "show", unit, "--no-pager"]
    command.extend(f"--property={name}" for name in properties)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode != 0:
        raise RuntimeError(f"systemd cannot inspect {unit}: {completed.stderr.strip()}")
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            if key in values:
                raise RuntimeError(f"systemd repeated {key} for {unit}")
            values[key] = value
    if set(values) != set(properties):
        raise RuntimeError(f"systemd returned incomplete state for {unit}")
    try:
        restarts = int(values["NRestarts"])
        entered = int(values["ActiveEnterTimestampMonotonic"])
    except ValueError as exc:
        raise RuntimeError(f"systemd returned invalid counters for {unit}") from exc
    if (
        values["LoadState"] != "loaded"
        or values["ActiveState"] != "active"
        or values["SubState"] != "running"
        or values["UnitFileState"] != "enabled"
        or restarts != 0
        or entered <= 0
    ):
        raise RuntimeError(f"systemd unit is not one clean running generation: {unit}")
    from liquidity_migration.account_owner_health import validate_systemd_invocation_id

    invocation = validate_systemd_invocation_id(
        values["InvocationID"],
        label=f"systemd invocation for {unit}",
    )
    return {
        **values,
        "NRestarts": restarts,
        "ActiveEnterTimestampMonotonic": entered,
        "InvocationID": invocation,
    }


def _systemd_snapshot(systemctl: Path) -> dict[str, dict[str, Any]]:
    return {unit: _systemd_unit_state(systemctl, unit) for unit in EXPECTED_PERSISTENT_UNITS}


def _authorization_summary(
    *,
    receipt_path: Path,
    repo: Path,
    machine_id: Path,
) -> dict[str, Any]:
    from liquidity_migration.operational_runtime_authority import (
        verify_operational_authorization,
    )

    payload = verify_operational_authorization(
        receipt_path=receipt_path,
        repo_root=repo,
        machine_id_path=machine_id,
    )
    if payload.get("profile") != "operational":
        raise ValueError("forward epoch requires the demo/paper operational profile")
    snapshot = read_stable_file(
        receipt_path,
        label="operational authorization receipt",
        reject_empty=True,
        require_mode=0o640,
        require_owner=True,
        require_single_link=True,
        max_bytes=1024 * 1024,
    )
    return {
        **json_safe(payload),
        "receipt_path": str(snapshot.path),
        "receipt_bytes": snapshot.size,
        "receipt_sha256": snapshot.sha256,
    }


def _account_owner_boundary(
    *,
    environment: str,
    account_root: Path,
    inbox_root: Path,
    capture_root: Path,
    invocation_id: str,
    owner_uid: int,
    now_ns: int,
) -> dict[str, Any]:
    from liquidity_migration.account_owner_health import (
        require_recent_account_owner_health,
    )
    from liquidity_migration.account_route import require_account_route
    from liquidity_migration.account_owner_readiness import latest_market_readiness
    from liquidity_migration.execution_environment import account_id_for_environment

    account_id = account_id_for_environment(environment)
    route = require_account_route(
        account_id=account_id,
        environment=environment,
        account_root=account_root,
        inbox_root=inbox_root,
        expected_owner_uid=owner_uid,
    )
    health = require_recent_account_owner_health(
        account_root,
        environment=environment,
        max_age_ns=MAX_OWNER_AGE_NS,
        now_ns=now_ns,
        expected_account_id=account_id,
        expected_invocation_id=invocation_id,
    )
    market = latest_market_readiness(
        capture_root,
        expected_invocation_id=invocation_id,
        expected_owner_uid=owner_uid,
    )
    market_ts_ns = market.oldest_required_receive_ts_ns
    if market_ts_ns is None:
        raise RuntimeError(f"{environment} owner has no complete required-book timestamp")
    market_age_ns = now_ns - market_ts_ns
    if market_age_ns < 0 or market_age_ns > MAX_OWNER_AGE_NS:
        raise RuntimeError(f"{environment} owner market readiness is stale: {market_age_ns}")
    if market.raw_market_persistence_enabled:
        raise RuntimeError(f"{environment} owner unexpectedly persists raw market frames")
    return {
        "environment": environment,
        "account_id": account_id,
        "route_id": route.route_id,
        "owner_invocation_id": invocation_id,
        "health": health.to_dict(),
        "market_readiness": market.to_dict(),
        "market_age_ns": market_age_ns,
    }


def _nonzero(value: Any, *, tolerance: float = 1e-12) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and abs(number) > tolerance


def _account_journal_boundary(root: Path) -> dict[str, Any]:
    from liquidity_migration.account_kernel import (
        GENESIS_HASH,
        read_account_journal,
        reduce_account_events,
    )

    events = read_account_journal(root, verify=True)
    state = reduce_account_events(events)
    positions = {
        symbol: position.signed_qty
        for symbol, position in sorted(state.positions.items())
        if _nonzero(position.signed_qty)
    }
    working_orders = [
        {
            "command_id": command_id,
            "symbol": state.orders[command_id].symbol,
            "remaining_signed_qty": state.orders[command_id].remaining_signed_qty,
        }
        for command_id in sorted(state.working_order_ids)
        if _nonzero(state.orders[command_id].remaining_signed_qty)
    ]
    aggregates = {symbol: target for symbol, target in sorted(state.aggregate_targets.items()) if _nonzero(target)}
    component_keys = sorted(state.component_targets)
    processed_batches = sorted(state.processed_batches)
    event_types = collections.Counter(event.event_type for event in events)
    last = events[-1] if events else None
    terminal_state_hash = state.state_hash()
    if last is not None and terminal_state_hash != last.state_hash:
        raise RuntimeError("reduced account state does not match the journal head")
    return {
        "root": str(root),
        "verified": True,
        "events": len(events),
        "event_type_counts": dict(sorted(event_types.items())),
        "last_sequence": 0 if last is None else last.sequence,
        "last_event_hash": GENESIS_HASH if last is None else last.event_hash,
        "journal_state_hash": GENESIS_HASH if last is None else last.state_hash,
        "reduced_state_hash": terminal_state_hash,
        "nonzero_positions": positions,
        "nonzero_position_count": len(positions),
        "working_orders": working_orders,
        "working_order_count": len(working_orders),
        "nonzero_aggregate_targets": aggregates,
        "nonzero_aggregate_target_count": len(aggregates),
        "nonzero_component_target_count": len(component_keys),
        "nonzero_component_target_keys_sha256": logical_name_hash(component_keys),
        "processed_batch_count": len(processed_batches),
        "processed_batch_ids_sha256": logical_name_hash(processed_batches),
    }


def _tape_boundary(
    path: Path,
    *,
    label: str,
    expected_uid: int,
    parser: Callable[[bytes], tuple[Sequence[Any], str]],
    event_identity: Callable[[Any], Mapping[str, Any]],
) -> dict[str, Any]:
    parent = _strict_directory(
        path.parent,
        label=f"{label} parent",
        expected_uid=expected_uid,
        require_owner_writable=True,
    )
    if path.parent.resolve(strict=True) != parent:
        raise ValueError(f"{label} parent changed while resolving")
    snapshot = _stable_private_snapshot(
        path,
        label=label,
        expected_uid=expected_uid,
        allow_missing=True,
    )
    data = b"" if snapshot is None else snapshot.data
    events, chain_hash = parser(data)
    first = None if not events else dict(event_identity(events[0]))
    last = None if not events else dict(event_identity(events[-1]))
    return {
        "path": str(path),
        "exists": snapshot is not None,
        "verified": True,
        "bytes": len(data),
        "prefix_sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(events),
        "chain_hash": chain_hash,
        "first": first,
        "last": last,
        "device": None if snapshot is None else snapshot.device,
        "inode": None if snapshot is None else snapshot.inode,
    }


def _strategy_tape_boundary(path: Path, *, expected_uid: int, label: str) -> dict[str, Any]:
    from liquidity_migration.strategy_event_clock import load_strategy_event_tape_bytes

    return _tape_boundary(
        path,
        label=label,
        expected_uid=expected_uid,
        parser=load_strategy_event_tape_bytes,
        event_identity=lambda event: {
            "event_id": event.event_id,
            "event_ts_ns": event.event_ts_ns,
            "event_kind": event.kind,
        },
    )


def _target_tape_boundary(path: Path, *, expected_uid: int, label: str) -> dict[str, Any]:
    from liquidity_migration.strategy_target_replay import (
        load_target_scheduling_capture_bytes,
    )

    return _tape_boundary(
        path,
        label=label,
        expected_uid=expected_uid,
        parser=load_target_scheduling_capture_bytes,
        event_identity=lambda event: {
            "capture_event_id": event.capture_event_id,
            "source_event_id": event.source_event.event_id,
            "source_event_ts_ns": event.source_event.event_ts_ns,
            "environment": event.source_environment,
            "sleeve": event.sleeve,
        },
    )


def _producer_health_boundary(
    root: Path,
    *,
    expected_uid: int,
    expected_invocation_id: str,
    expected_environment: str,
    expected_sleeve: str,
    now_ns: int,
) -> dict[str, Any]:
    from liquidity_migration.strategy_cycle_health import (
        read_strategy_cycle_health,
        strategy_cycle_health_path,
    )

    path = strategy_cycle_health_path(root)
    metadata = path.lstat()
    if metadata.st_uid != expected_uid:
        raise ValueError(f"strategy-cycle health has the wrong owner: {path}")
    health = read_strategy_cycle_health(root)
    if (
        health.invocation_id != expected_invocation_id
        or health.environment != expected_environment
        or health.sleeve != expected_sleeve
    ):
        raise RuntimeError(f"strategy-cycle health does not match current producer: {root}")
    age_ns = now_ns - health.completed_ts_ns
    if age_ns < 0 or age_ns > MAX_PRODUCER_AGE_NS:
        raise RuntimeError(f"strategy-cycle health is stale for {root}: {age_ns}")
    return {**health.to_dict(), "path": str(path), "age_ns": age_ns}


def _queue_inventory(root: Path, *, expected_uid: int) -> dict[str, Any]:
    states = ("pending", "processing", "completed", "failed")

    def read_once() -> dict[str, list[str]]:
        output: dict[str, list[str]] = {}
        seen: set[str] = set()
        for state_name in states:
            directory = _strict_directory(
                root / state_name,
                label=f"account intent {state_name} directory",
                expected_uid=expected_uid,
            )
            names: list[str] = []
            for path in sorted(directory.iterdir(), key=lambda value: value.name):
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"account intent entry is not a regular file: {path}")
                if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise ValueError(f"account intent entry is not private: {path}")
                if path.name in seen:
                    raise RuntimeError(f"account intent filename is present in multiple states: {path.name}")
                seen.add(path.name)
                names.append(path.name)
            output[state_name] = names
        return output

    previous = read_once()
    for _attempt in range(8):
        current = read_once()
        if current == previous:
            return {
                "root": str(root),
                "states": {
                    key: {
                        "count": len(names),
                        "filenames": names,
                        "filenames_sha256": logical_name_hash(names),
                    }
                    for key, names in current.items()
                },
            }
        previous = current
    raise RuntimeError(f"account intent queue did not stabilize: {root}")


def _market_capture_inventory(root: Path, *, expected_uid: int) -> dict[str, Any]:
    canonical_root = _strict_directory(
        root,
        label="account market capture root",
        expected_uid=expected_uid,
    )
    files: list[dict[str, Any]] = []
    for directory, directory_names, filenames in os.walk(canonical_root, followlinks=False):
        current = Path(directory)
        for name in list(directory_names):
            metadata = (current / name).lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"market capture contains a symlink directory: {current / name}")
        for name in filenames:
            path = current / name
            relative = path.relative_to(canonical_root)
            if len(relative.parts) != 3 or not _SEGMENT.fullmatch(name):
                continue
            snapshot = _stable_private_snapshot(
                path,
                label=f"market capture segment {relative.as_posix()}",
                expected_uid=expected_uid,
            )
            assert snapshot is not None
            files.append(
                {
                    "path": relative.as_posix(),
                    "bytes": snapshot.size,
                    "prefix_sha256": snapshot.sha256,
                    "device": snapshot.device,
                    "inode": snapshot.inode,
                }
            )
    files.sort(key=lambda value: str(value["path"]))
    if len({str(value["path"]) for value in files}) != len(files):
        raise RuntimeError("market capture inventory contains duplicate paths")
    sidecars: dict[str, dict[str, Any]] = {}
    for name in (
        "account_owner_capture_readiness.json",
        "account_owner_market_readiness.json",
    ):
        path = canonical_root / name
        snapshot = _stable_private_snapshot(
            path,
            label=f"market capture sidecar {name}",
            expected_uid=expected_uid,
            allow_missing=True,
        )
        sidecars[name] = {
            "exists": snapshot is not None,
            "bytes": 0 if snapshot is None else snapshot.size,
            "sha256": hashlib.sha256(b"").hexdigest() if snapshot is None else snapshot.sha256,
        }
    return {
        "root": str(canonical_root),
        "segments": len(files),
        "bytes": sum(int(value["bytes"]) for value in files),
        "logical_prefix_sha256": hashlib.sha256(canonical_json({"files": files})).hexdigest(),
        "files": files,
        "sidecars": sidecars,
    }


def _prepare_output_parent(output: Path, *, repo: Path) -> None:
    expected = _default_receipt(repo)
    if output != expected:
        attempts = expected.parent / "attempts"
        try:
            relative = output.relative_to(attempts)
        except ValueError as exc:
            raise ValueError(
                f"forward start receipt must be {expected} or one named attempt under {attempts}"
            ) from exc
        if (
            len(relative.parts) != 2
            or relative.name != "receipt.json"
            or not _ATTEMPT_ID.fullmatch(relative.parts[0])
        ):
            raise ValueError("forward start receipt attempt path is invalid")
    current = repo
    for part in output.parent.relative_to(repo).parts:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"forward receipt parent is not a real directory: {current}")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError(f"forward receipt parent is not issuer-controlled: {current}")


def _analysis_boundary(repo: Path) -> dict[str, dict[str, Any]]:
    root = repo / "reports" / FORWARD_EPOCH_ID / "forward"
    output: dict[str, dict[str, Any]] = {}
    for name in ("analysis", "structural", "tca"):
        path = root / name
        exists = path.exists() or path.is_symlink()
        if exists:
            raise FileExistsError(f"pre-start forward analysis path already exists: {path}")
        output[name] = {"path": str(path), "exists": False}
    return output


def _load_runtime_environment(
    *,
    demo_env_path: Path,
    paper_env_path: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    from liquidity_migration.systemd_environment import (
        load_group_systemd_environment,
        load_private_systemd_environment,
    )

    demo = load_private_systemd_environment(demo_env_path)
    paper = load_group_systemd_environment(paper_env_path, group_name=PAPER_GROUP)
    required_demo = (
        "ACCOUNT_EXECUTION_ROOT",
        "ACCOUNT_INTENT_INBOX_ROOT",
        "ACCOUNT_CAPTURE_ROOT",
        "STRATEGY_TARGET_CAPTURE_PATH",
    )
    required_paper = (
        "ACCOUNT_EXECUTION_ROOT",
        "ACCOUNT_INTENT_INBOX_ROOT",
        "ACCOUNT_PAPER_CAPTURE_ROOT",
        "STRATEGY_TARGET_CAPTURE_PATH",
    )
    for label, values, required in (
        ("demo", demo, required_demo),
        ("paper", paper, required_paper),
    ):
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise ValueError(f"{label} environment is missing {', '.join(missing)}")
    expected_demo = Path(demo["ACCOUNT_CAPTURE_ROOT"]) / TARGET_CAPTURE_FILENAME
    expected_paper = Path(paper["ACCOUNT_PAPER_CAPTURE_ROOT"]) / TARGET_CAPTURE_FILENAME
    if Path(demo["STRATEGY_TARGET_CAPTURE_PATH"]) != expected_demo:
        raise ValueError("demo strategy target capture path is not capture-root-bound")
    if Path(paper["STRATEGY_TARGET_CAPTURE_PATH"]) != expected_paper:
        raise ValueError("paper strategy target capture path is not capture-root-bound")
    return demo, paper


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("/opt/liquidity-migration"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--authorization-receipt", type=Path, default=DEFAULT_AUTHORIZATION)
    parser.add_argument("--machine-id", type=Path, default=DEFAULT_MACHINE_ID)
    parser.add_argument("--demo-environment-file", type=Path, default=DEFAULT_DEMO_ENV)
    parser.add_argument("--paper-environment-file", type=Path, default=DEFAULT_PAPER_ENV)
    parser.add_argument("--systemctl", type=Path, default=DEFAULT_SYSTEMCTL)
    parser.add_argument("--comparator-receipt", type=Path)
    parser.add_argument("--comparator-verification-receipt", type=Path)
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid"):
        raise RuntimeError("forward start collection requires the installed Linux host")
    if os.geteuid() != 0:
        raise RuntimeError("forward start collection must run as root")

    import pwd

    args = _parser().parse_args(argv)
    repo = args.repo_root.expanduser().resolve(strict=True)
    output = _default_receipt(repo) if args.receipt is None else args.receipt.expanduser().resolve(strict=False)
    comparator_path = (
        _default_comparator(repo)
        if args.comparator_receipt is None
        else args.comparator_receipt.expanduser().resolve(strict=True)
    )
    verification_path = (
        _default_comparator_verification(repo)
        if args.comparator_verification_receipt is None
        else args.comparator_verification_receipt.expanduser().resolve(strict=True)
    )
    _analysis_boundary(repo)
    _prepare_output_parent(output, repo=repo)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"forward start receipt is create-only: {output}")

    authorization = _authorization_summary(
        receipt_path=args.authorization_receipt,
        repo=repo,
        machine_id=args.machine_id,
    )
    commit = str(authorization["authorized_commit"])
    comparator_payload, comparator = load_integrated_comparator_receipt(
        comparator_path,
        expected_commit=commit,
        require_mode=0o600,
        require_owner=True,
    )
    del comparator_payload
    comparator_verification = load_comparator_verification_receipt(
        verification_path,
        require_mode=0o600,
        require_owner=True,
    )
    attested = comparator_verification["comparator"]
    if not isinstance(attested, Mapping) or any(
        attested.get(key) != comparator.get(key)
        for key in ("sha256", "receipt_payload_sha256", "code_commit", "status")
    ):
        raise ValueError("comparator verification receipt does not bind the transferred comparator")
    comparator["verification_receipt"] = comparator_verification

    demo_env, paper_env = _load_runtime_environment(
        demo_env_path=args.demo_environment_file,
        paper_env_path=args.paper_environment_file,
    )
    paper_uid = pwd.getpwnam(PAPER_USER).pw_uid
    demo_uid = 0
    demo_account = _strict_directory(
        demo_env["ACCOUNT_EXECUTION_ROOT"],
        label="demo account root",
        expected_uid=demo_uid,
    )
    demo_inbox = _strict_directory(
        demo_env["ACCOUNT_INTENT_INBOX_ROOT"],
        label="demo inbox root",
        expected_uid=demo_uid,
    )
    demo_capture = _strict_directory(
        demo_env["ACCOUNT_CAPTURE_ROOT"],
        label="demo capture root",
        expected_uid=demo_uid,
    )
    paper_account = _strict_directory(
        paper_env["ACCOUNT_EXECUTION_ROOT"],
        label="paper account root",
        expected_uid=paper_uid,
    )
    paper_inbox = _strict_directory(
        paper_env["ACCOUNT_INTENT_INBOX_ROOT"],
        label="paper inbox root",
        expected_uid=paper_uid,
    )
    paper_capture = _strict_directory(
        paper_env["ACCOUNT_PAPER_CAPTURE_ROOT"],
        label="paper capture root",
        expected_uid=paper_uid,
    )
    roots = {demo_account, demo_inbox, demo_capture, paper_account, paper_inbox, paper_capture}
    if len(roots) != 6:
        raise ValueError("demo and paper account, inbox, and capture roots must be distinct")

    strategy_roots = {
        "demo_long": repo / "data/bybit-long-demo-event",
        "demo_continuous": repo / "data/bybit-continuous-demo-event",
        "paper_long": repo / "data/bybit-long-paper-event",
        "paper_continuous": repo / "data/bybit-continuous-paper-event",
    }
    strategy_uids = {
        "demo_long": demo_uid,
        "demo_continuous": demo_uid,
        "paper_long": paper_uid,
        "paper_continuous": paper_uid,
    }
    for key, root in strategy_roots.items():
        strategy_roots[key] = _strict_directory(
            root,
            label=f"{key} strategy root",
            expected_uid=strategy_uids[key],
        )

    units = _systemd_snapshot(args.systemctl)
    now_ns = time.time_ns()
    owner_readiness = {
        "demo": _account_owner_boundary(
            environment="demo",
            account_root=demo_account,
            inbox_root=demo_inbox,
            capture_root=demo_capture,
            invocation_id=units["liquidity-migration-account-execution.service"]["InvocationID"],
            owner_uid=demo_uid,
            now_ns=now_ns,
        ),
        "paper": _account_owner_boundary(
            environment="paper",
            account_root=paper_account,
            inbox_root=paper_inbox,
            capture_root=paper_capture,
            invocation_id=units["liquidity-migration-account-paper-execution.service"]["InvocationID"],
            owner_uid=paper_uid,
            now_ns=now_ns,
        ),
    }
    journals = {
        "demo": _account_journal_boundary(demo_account),
        "paper": _account_journal_boundary(paper_account),
    }
    producer_units = {
        "demo_long": "liquidity-migration-bybit-long-demo.service",
        "demo_continuous": "liquidity-migration-bybit-continuous-demo.service",
        "paper_long": "liquidity-migration-bybit-long-paper.service",
        "paper_continuous": "liquidity-migration-bybit-continuous-paper.service",
    }
    producer_health: dict[str, dict[str, Any]] = {}
    for key, unit in producer_units.items():
        environment, sleeve = key.split("_", 1)
        producer_health[key] = _producer_health_boundary(
            strategy_roots[key],
            expected_uid=strategy_uids[key],
            expected_invocation_id=units[unit]["InvocationID"],
            expected_environment=environment,
            expected_sleeve=sleeve,
            now_ns=now_ns,
        )
    strategy_tapes = {
        key: _strategy_tape_boundary(
            root / STRATEGY_EVENT_TAPE_FILENAME,
            expected_uid=strategy_uids[key],
            label=f"{key} strategy event tape",
        )
        for key, root in strategy_roots.items()
    }
    target_tapes = {
        "demo": _target_tape_boundary(
            Path(demo_env["STRATEGY_TARGET_CAPTURE_PATH"]),
            expected_uid=demo_uid,
            label="demo shared strategy target tape",
        ),
        "paper": _target_tape_boundary(
            Path(paper_env["STRATEGY_TARGET_CAPTURE_PATH"]),
            expected_uid=paper_uid,
            label="paper shared strategy target tape",
        ),
    }
    legacy_tapes = {
        key: _target_tape_boundary(
            root / LEGACY_TARGET_CAPTURE_FILENAME,
            expected_uid=strategy_uids[key],
            label=f"{key} legacy strategy target tape",
        )
        for key, root in strategy_roots.items()
    }
    market_captures = {
        "demo": _market_capture_inventory(demo_capture, expected_uid=demo_uid),
        "paper": _market_capture_inventory(paper_capture, expected_uid=paper_uid),
    }
    queues = {
        "demo": _queue_inventory(demo_inbox, expected_uid=demo_uid),
        "paper": _queue_inventory(paper_inbox, expected_uid=paper_uid),
    }
    analysis = _analysis_boundary(repo)
    collected_ts_ns = time.time_ns()
    receipt = build_start_receipt(
        collected_ts_ns=collected_ts_ns,
        installed_commit=commit,
        contracts=contract_identities(repo),
        operational_authorization=authorization,
        integrated_comparator=comparator,
        systemd_units=units,
        account_owner_readiness=owner_readiness,
        account_journals=journals,
        producer_cycle_health=producer_health,
        strategy_event_tapes=strategy_tapes,
        target_capture_tapes=target_tapes,
        legacy_target_capture_tapes=legacy_tapes,
        market_capture_prefixes=market_captures,
        intent_queues=queues,
        analysis_boundary=analysis,
    )
    data = canonical_json(receipt) + b"\n"

    from liquidity_migration.operational_runtime_authority import (
        verify_operational_authorization,
    )
    from liquidity_migration.private_receipt_publication import publish_private_receipt

    initial_invocations = {unit: values["InvocationID"] for unit, values in units.items()}

    def validate_uncommitted(snapshot: Any) -> None:
        if snapshot.data != data:
            raise RuntimeError("forward start receipt changed during publication")
        validate_start_receipt_bytes(snapshot.data)

    def revalidate_sources() -> None:
        current_authorization = verify_operational_authorization(
            receipt_path=args.authorization_receipt,
            repo_root=repo,
            machine_id_path=args.machine_id,
        )
        if current_authorization.get("artifact_sha256") != authorization.get("artifact_sha256"):
            raise RuntimeError("operational authorization changed during start publication")
        current_units = _systemd_snapshot(args.systemctl)
        if {unit: values["InvocationID"] for unit, values in current_units.items()} != initial_invocations:
            raise RuntimeError("a runtime generation changed during start publication")
        _analysis_boundary(repo)
        if time.time_ns() >= int(receipt["start_ts_ns"]):
            raise RuntimeError("forward start boundary passed before receipt publication")

    publish_private_receipt(
        output,
        data,
        label="forward execution epoch start receipt",
        staging_prefix=".forward-start-stage-",
        committed_mode=0o600,
        committed_gid=None,
        max_bytes=16 * 1024 * 1024,
        validate_uncommitted=validate_uncommitted,
        revalidate_sources=revalidate_sources,
        forbidden_roots=sorted(str(root) for root in roots),
    )
    reopened = read_stable_file(
        output,
        label="forward execution epoch start receipt",
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
        max_bytes=16 * 1024 * 1024,
    )
    observed = validate_start_receipt_bytes(reopened.data)
    if reopened.data != data or time.time_ns() >= int(observed["start_ts_ns"]):
        raise RuntimeError("forward start receipt failed its post-publication boundary check")
    print(
        json.dumps(
            {
                "status": observed["status"],
                "receipt": str(reopened.path),
                "receipt_sha256": reopened.sha256,
                "artifact_sha256": observed["artifact_sha256"],
                "installed_commit": commit,
                "start_at_utc": observed["start_at_utc"],
                "calibration_end_at_utc": observed["calibration_end_at_utc"],
                "epoch_end_at_utc": observed["epoch_end_at_utc"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"forward epoch start failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
