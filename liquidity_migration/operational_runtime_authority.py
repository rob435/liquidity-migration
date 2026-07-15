"""Authorize one exact clean commit for demo/paper operation only.

This is intentionally separate from the natural-tape cutover authority.  It
does not assert replay, drift, alpha, promotion, or real-money evidence.  The
calibration profile can authorize only the demo owner with bounded raw capture;
the operational profile requires the passing twin receipt and disables bulk
raw persistence while retaining live L2 and exact decision books.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .account_cutover_authority import require_clean_authorized_checkout
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .execution_twin_calibration import load_calibration_receipt
from .systemd_environment import parse_systemd_environment_bytes


SCHEMA_VERSION = 1
KIND = "account_execution_operational_runtime_authorization"
VALIDATOR = "account_execution_operational_runtime_authorization_v1"
DEFAULT_RECEIPT = Path(
    "/etc/liquidity-migration/account-execution-operational-ready"
)
OWNER_ACKNOWLEDGEMENT = "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION"
CALIBRATION_PROFILE = "calibration"
OPERATIONAL_PROFILE = "operational"
CALIBRATION_AUTHORIZED_UNITS = (
    "liquidity-migration-account-execution.service",
)
AUTHORIZED_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-demo-liveness.service",
)
REQUIRED_ENVIRONMENT_PATHS = (
    Path("/etc/liquidity-migration/account-execution.env"),
    Path("/etc/liquidity-migration/account-paper-execution.env"),
    Path("/etc/liquidity-migration/bybit-demo.env"),
    Path("/etc/liquidity-migration/sleeves.resolved.env"),
)
FORBIDDEN_OVERRIDE_PATHS = (
    Path("/etc/liquidity-migration/natural-run.env"),
    Path("/etc/liquidity-migration/fresh-deploy"),
)
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNIT = re.compile(r"liquidity-migration-[a-z0-9-]+\.service")
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_INPUT_KEYS = {
    "account-execution.env": (
        "ACCOUNT_SYMBOLS_FILE",
        "ACCOUNT_DEMO_RULES_FILE",
        "ACCOUNT_RISK_POLICY_FILE",
    ),
    "account-paper-execution.env": (
        "ACCOUNT_SYMBOLS_FILE",
        "ACCOUNT_DEMO_RULES_FILE",
        "ACCOUNT_RISK_POLICY_FILE",
        "ACCOUNT_TWIN_CALIBRATION_FILE",
    ),
}
_ROOT_KEYS = {
    "account-execution.env": (
        "ACCOUNT_EXECUTION_ROOT",
        "ACCOUNT_INTENT_INBOX_ROOT",
        "ACCOUNT_CAPTURE_ROOT",
    ),
    "account-paper-execution.env": (
        "ACCOUNT_EXECUTION_ROOT",
        "ACCOUNT_INTENT_INBOX_ROOT",
        "ACCOUNT_PAPER_CAPTURE_ROOT",
    ),
}
_PROFILE_ENVIRONMENT_NAMES = {
    CALIBRATION_PROFILE: (
        "account-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    ),
    OPERATIONAL_PROFILE: (
        "account-execution.env",
        "account-paper-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    ),
}
_PROFILE_FIELDS = {
    CALIBRATION_PROFILE: {
        "scope": "registered_demo_calibration_only_no_real_money",
        "raw_market_persistence": "enabled_for_registered_demo_calibration",
        "research_evidence_status": "forward_calibration_result_not_claimed",
        "authorized_units": CALIBRATION_AUTHORIZED_UNITS,
        "demo_raw_value": "1",
    },
    OPERATIONAL_PROFILE: {
        "scope": "demo_paper_operational_only_no_real_money",
        "raw_market_persistence": "disabled",
        "research_evidence_status": (
            "natural_replay_not_claimed_not_required_for_demo_paper_operation"
        ),
        "authorized_units": AUTHORIZED_UNITS,
        "demo_raw_value": "0",
    },
}


def _profile_fields(profile: str) -> Mapping[str, Any]:
    try:
        return _PROFILE_FIELDS[profile]
    except KeyError as exc:
        raise ValueError(f"unsupported operational authorization profile: {profile}") from exc


def _self_hash(payload: Mapping[str, Any]) -> str:
    material = {**dict(payload), "artifact_sha256": ""}
    return hashlib.sha256(canonical_json(material)).hexdigest()


def _identity(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "mtime_ns": snapshot.mtime_ns,
        "mode": snapshot.mode,
        "uid": snapshot.uid,
        "nlink": snapshot.nlink,
    }


def _directory_identity(value: str, *, label: str) -> tuple[Path, dict[str, Any]]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    lexical = Path(os.path.abspath(path))
    try:
        before = lexical.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {lexical}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"{label} must be a real directory")
    if before.st_uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the current user")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lexical, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely: {lexical}") from exc
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
    if any(getattr(before, field) != getattr(opened, field) for field in fields):
        raise RuntimeError(f"{label} changed while it was opened: {lexical}")
    try:
        after = lexical.lstat()
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} changed while it was inspected: {lexical}") from exc
    if any(getattr(opened, field) != getattr(after, field) for field in fields):
        raise RuntimeError(f"{label} changed while it was inspected: {lexical}")
    if resolved != lexical:
        raise ValueError(f"{label} must not traverse symbolic links")
    return lexical, {
        "path": str(lexical),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "mode": stat.S_IMODE(opened.st_mode),
        "uid": opened.st_uid,
        "gid": opened.st_gid,
    }


def _read_private(path: str | Path, *, label: str) -> StableFileSnapshot:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    return read_stable_file(
        candidate,
        label=label,
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
    )


def _machine_fingerprint(machine_id_path: str | Path) -> str:
    snapshot = read_stable_file(
        machine_id_path,
        label="machine id",
        reject_empty=True,
        require_single_link=False,
    )
    if snapshot.size > 4096:
        raise ValueError("machine id is unexpectedly large")
    return hashlib.sha256(snapshot.data.strip()).hexdigest()


def _assert_no_override_paths() -> None:
    for path in FORBIDDEN_OVERRIDE_PATHS:
        if not os.path.lexists(path):
            continue
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            continue
        raise ValueError(
            f"operational runtime refuses natural/fresh override path: {path}"
        )


def _parse_environment_snapshots(
    profile: str,
) -> tuple[
    dict[str, StableFileSnapshot],
    dict[str, dict[str, str]],
]:
    snapshots: dict[str, StableFileSnapshot] = {}
    values: dict[str, dict[str, str]] = {}
    paths_by_name = {path.name: path for path in REQUIRED_ENVIRONMENT_PATHS}
    if set(paths_by_name) != {
        "account-execution.env",
        "account-paper-execution.env",
        "bybit-demo.env",
        "sleeves.resolved.env",
    }:
        raise ValueError("operational environment path set is invalid")
    try:
        required_names = _PROFILE_ENVIRONMENT_NAMES[profile]
    except KeyError as exc:
        raise ValueError(f"unsupported operational authorization profile: {profile}") from exc
    for name in required_names:
        path = paths_by_name[name]
        required_mode = 0o644 if path.name == "sleeves.resolved.env" else 0o600
        snapshot = read_stable_file(
            path,
            label=f"operational environment {path.name}",
            reject_empty=True,
            require_mode=required_mode,
            require_owner=True,
        )
        snapshots[path.name] = snapshot
        values[path.name] = parse_systemd_environment_bytes(
            snapshot.data,
            label=f"operational environment {path.name}",
        )
    return snapshots, values


def _validate_environments(
    values: Mapping[str, Mapping[str, str]],
    *,
    profile: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, StableFileSnapshot]]:
    profile_fields = _profile_fields(profile)
    demo = values["account-execution.env"]
    credentials = values["bybit-demo.env"]
    sleeves = values["sleeves.resolved.env"]

    if demo.get("ACCOUNT_EXECUTION_KERNEL_REQUIRED") != "1":
        raise ValueError("demo operational environment does not require the account kernel")
    expected_demo_raw = str(profile_fields["demo_raw_value"])
    if demo.get("ACCOUNT_RAW_MARKET_PERSISTENCE") != expected_demo_raw:
        raise ValueError(
            "demo environment must set "
            f"ACCOUNT_RAW_MARKET_PERSISTENCE={expected_demo_raw} for profile {profile}"
        )
    paper: Mapping[str, str] | None = None
    if profile == OPERATIONAL_PROFILE:
        paper = values["account-paper-execution.env"]
        if paper.get("ACCOUNT_PAPER_KERNEL_REQUIRED") != "1":
            raise ValueError(
                "paper operational environment does not require the account kernel"
            )
        if paper.get("ACCOUNT_RAW_MARKET_PERSISTENCE") != "0":
            raise ValueError(
                "paper operational environment must set "
                "ACCOUNT_RAW_MARKET_PERSISTENCE=0"
            )
    if not credentials.get("BYBIT_DEMO_API_KEY") or not credentials.get(
        "BYBIT_DEMO_API_SECRET"
    ):
        raise ValueError("demo credentials are missing")
    if any(credentials.get(key) for key in ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")):
        raise ValueError("operational credential file must not contain mainnet credentials")
    if credentials.get("REAL_MONEY", "").strip().lower() not in _FALSE_VALUES:
        raise ValueError("operational credential file does not explicitly disable REAL_MONEY")
    for key in ("LONG_SLEEVE", "CONTINUOUS_SLEEVE", "CONTINUOUS_PAPER_SLEEVE"):
        if sleeves.get(key, "").strip().lower() not in {"on", "off"}:
            raise ValueError(f"resolved sleeve environment has invalid {key}")

    roots: list[Path] = []
    root_identities: dict[str, dict[str, Any]] = {}
    root_filenames = (
        ("account-execution.env",)
        if profile == CALIBRATION_PROFILE
        else tuple(_ROOT_KEYS)
    )
    for filename in root_filenames:
        keys = _ROOT_KEYS[filename]
        environment = values[filename]
        for key in keys:
            raw = environment.get(key, "")
            root, identity = _directory_identity(
                raw,
                label=f"{filename} {key}",
            )
            roots.append(root)
            root_identities[f"{filename}:{key}"] = identity
    if len(set(roots)) != len(roots):
        raise ValueError("operational account, inbox, and market roots must be distinct")
    for index, left in enumerate(roots):
        for right in roots[index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ValueError("operational roots must not contain one another")

    inputs: dict[str, StableFileSnapshot] = {}
    input_filenames = (
        ("account-execution.env",)
        if profile == CALIBRATION_PROFILE
        else tuple(_INPUT_KEYS)
    )
    for filename in input_filenames:
        keys = _INPUT_KEYS[filename]
        environment = values[filename]
        for key in keys:
            raw = environment.get(key, "")
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{filename} {key} must be absolute")
            snapshot = read_stable_file(
                path,
                label=f"operational input {filename} {key}",
                reject_empty=True,
                require_owner=True,
            )
            inputs[f"{filename}:{key}"] = snapshot
    if profile == OPERATIONAL_PROFILE:
        assert paper is not None
        calibration_path = Path(paper["ACCOUNT_TWIN_CALIBRATION_FILE"])
        calibration = load_calibration_receipt(
            calibration_path,
            snapshot=inputs[
                "account-paper-execution.env:ACCOUNT_TWIN_CALIBRATION_FILE"
            ],
        )
        if calibration.get("execution_twin_gate_passed") is not True:
            raise ValueError("paper execution-twin calibration has not passed")
    return root_identities, inputs


def _validate_identity(
    payload: Mapping[str, Any],
    snapshot: StableFileSnapshot,
    *,
    label: str,
) -> None:
    expected = _identity(snapshot)
    if dict(payload) != expected:
        raise ValueError(f"{label} changed after operational authorization")


def _load_receipt(path: str | Path) -> tuple[StableFileSnapshot, dict[str, Any]]:
    snapshot = _read_private(path, label="operational runtime authorization")
    try:
        payload = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("operational runtime authorization is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("operational runtime authorization must contain an object")
    if canonical_json(payload) + b"\n" != snapshot.data:
        raise ValueError("operational runtime authorization is not canonical JSON")
    expected = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "authorized_commit",
        "repo_root",
        "machine_fingerprint_sha256",
        "authorization_reference",
        "profile",
        "scope",
        "raw_market_persistence",
        "research_evidence_status",
        "authorized_units",
        "environment_files",
        "runtime_roots",
        "runtime_inputs",
        "artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("operational runtime authorization fields are invalid")
    profile = str(payload.get("profile") or "")
    try:
        profile_fields = _profile_fields(profile)
    except ValueError as exc:
        raise ValueError("operational runtime authorization is invalid") from exc
    units = payload.get("authorized_units")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or int(payload.get("created_ts_ns") or 0) <= 0
        or not _FULL_COMMIT.fullmatch(str(payload.get("authorized_commit") or ""))
        or not Path(str(payload.get("repo_root") or "")).is_absolute()
        or not _SHA256.fullmatch(str(payload.get("machine_fingerprint_sha256") or ""))
        or not str(payload.get("authorization_reference") or "").strip()
        or payload.get("scope") != profile_fields["scope"]
        or payload.get("raw_market_persistence")
        != profile_fields["raw_market_persistence"]
        or payload.get("research_evidence_status")
        != profile_fields["research_evidence_status"]
        or units != list(profile_fields["authorized_units"])
        or any(not _UNIT.fullmatch(str(unit)) for unit in units or ())
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("operational runtime authorization is invalid")
    return snapshot, payload


def issue_operational_authorization(
    *,
    receipt_path: str | Path,
    expected_commit: str,
    repo_root: str | Path,
    machine_id_path: str | Path,
    authorization_reference: str,
    owner_acknowledgement: str,
    profile: str = OPERATIONAL_PROFILE,
) -> dict[str, Any]:
    if owner_acknowledgement != OWNER_ACKNOWLEDGEMENT:
        raise ValueError("exact demo/paper-only owner acknowledgement is required")
    if not authorization_reference.strip() or len(authorization_reference) > 500:
        raise ValueError("authorization reference must be non-empty and at most 500 characters")
    candidate = expected_commit.lower()
    if not _FULL_COMMIT.fullmatch(candidate):
        raise ValueError("expected commit must be a full lowercase Git commit")
    root = Path(repo_root).expanduser().resolve(strict=True)
    require_clean_authorized_checkout(root, candidate)
    _assert_no_override_paths()
    profile_fields = _profile_fields(profile)
    environment_snapshots, values = _parse_environment_snapshots(profile)
    root_identities, input_snapshots = _validate_environments(
        values,
        profile=profile,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "created_ts_ns": time.time_ns(),
        "authorized_commit": candidate,
        "repo_root": str(root),
        "machine_fingerprint_sha256": _machine_fingerprint(machine_id_path),
        "authorization_reference": authorization_reference,
        "profile": profile,
        "scope": profile_fields["scope"],
        "raw_market_persistence": profile_fields["raw_market_persistence"],
        "research_evidence_status": profile_fields["research_evidence_status"],
        "authorized_units": list(profile_fields["authorized_units"]),
        "environment_files": {
            name: _identity(snapshot)
            for name, snapshot in sorted(environment_snapshots.items())
        },
        "runtime_roots": dict(sorted(root_identities.items())),
        "runtime_inputs": {
            name: _identity(snapshot)
            for name, snapshot in sorted(input_snapshots.items())
        },
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    output = Path(receipt_path).expanduser()
    if not output.is_absolute():
        raise ValueError("operational authorization path must be absolute")
    parent = output.parent.resolve(strict=True)
    if os.path.lexists(output):
        raise FileExistsError(f"operational authorization already exists: {output}")
    descriptor = os.open(
        output,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            os.fchmod(descriptor, 0o600)
            data = canonical_json(payload) + b"\n"
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError(
                        "operational authorization write made no progress"
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _load_receipt(output)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return payload


def verify_operational_authorization(
    *,
    receipt_path: str | Path,
    repo_root: str | Path,
    machine_id_path: str | Path,
    unit: str | None = None,
) -> dict[str, Any]:
    _snapshot, payload = _load_receipt(receipt_path)
    root = Path(repo_root).expanduser().resolve(strict=True)
    if root != Path(str(payload["repo_root"])).resolve(strict=True):
        raise ValueError("operational authorization belongs to another checkout")
    require_clean_authorized_checkout(root, str(payload["authorized_commit"]))
    if payload["machine_fingerprint_sha256"] != _machine_fingerprint(machine_id_path):
        raise ValueError("operational authorization belongs to another machine")
    profile = str(payload["profile"])
    profile_fields = _profile_fields(profile)
    authorized_units = tuple(str(value) for value in payload["authorized_units"])
    if unit is not None and unit not in authorized_units:
        raise ValueError(
            f"unit is not authorized for operational profile {profile}: {unit}"
        )
    _assert_no_override_paths()
    environment_snapshots, values = _parse_environment_snapshots(profile)
    root_identities, input_snapshots = _validate_environments(
        values,
        profile=profile,
    )
    environment_payload = payload.get("environment_files")
    root_payload = payload.get("runtime_roots")
    input_payload = payload.get("runtime_inputs")
    if (
        not isinstance(environment_payload, Mapping)
        or not isinstance(root_payload, Mapping)
        or not isinstance(input_payload, Mapping)
    ):
        raise ValueError("operational authorization input identities are malformed")
    if set(environment_payload) != set(environment_snapshots):
        raise ValueError("operational authorization environment set changed")
    if set(input_payload) != set(input_snapshots):
        raise ValueError("operational authorization runtime input set changed")
    if dict(root_payload) != root_identities:
        raise ValueError("operational runtime root changed after authorization")
    for name, snapshot in environment_snapshots.items():
        identity = environment_payload.get(name)
        if not isinstance(identity, Mapping):
            raise ValueError(f"operational environment identity is malformed: {name}")
        _validate_identity(identity, snapshot, label=f"operational environment {name}")
    for name, snapshot in input_snapshots.items():
        identity = input_payload.get(name)
        if not isinstance(identity, Mapping):
            raise ValueError(f"operational input identity is malformed: {name}")
        _validate_identity(identity, snapshot, label=f"operational input {name}")
    if os.environ.get("REAL_MONEY", "").strip().lower() not in _FALSE_VALUES:
        raise ValueError("runtime environment enables or ambiguously sets REAL_MONEY")
    if os.environ.get("BYBIT_REAL_API_KEY") or os.environ.get("BYBIT_REAL_API_SECRET"):
        raise ValueError("runtime environment contains mainnet credentials")
    expected_runtime_raw: str | None = None
    if unit == "liquidity-migration-account-execution.service":
        expected_runtime_raw = str(profile_fields["demo_raw_value"])
    elif unit == "liquidity-migration-account-paper-execution.service":
        expected_runtime_raw = "0"
    if (
        expected_runtime_raw is not None
        and os.environ.get("ACCOUNT_RAW_MARKET_PERSISTENCE")
        != expected_runtime_raw
    ):
        raise ValueError(
            "account owner runtime did not inherit the authorized raw persistence mode"
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    issue.add_argument("--expected-commit", required=True)
    issue.add_argument("--repo-root", type=Path, required=True)
    issue.add_argument("--machine-id-path", type=Path, default=Path("/etc/machine-id"))
    issue.add_argument("--authorization-reference", required=True)
    issue.add_argument("--owner-acknowledgement", required=True)
    issue.add_argument(
        "--profile",
        choices=(CALIBRATION_PROFILE, OPERATIONAL_PROFILE),
        default=OPERATIONAL_PROFILE,
    )
    for name in ("verify", "verify-runtime"):
        command = commands.add_parser(name)
        command.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
        command.add_argument("--repo-root", type=Path, required=True)
        command.add_argument(
            "--machine-id-path", type=Path, default=Path("/etc/machine-id")
        )
        if name == "verify-runtime":
            command.add_argument("--unit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "issue":
            result = issue_operational_authorization(
                receipt_path=args.receipt,
                expected_commit=args.expected_commit,
                repo_root=args.repo_root,
                machine_id_path=args.machine_id_path,
                authorization_reference=args.authorization_reference,
                owner_acknowledgement=args.owner_acknowledgement,
                profile=args.profile,
            )
        else:
            result = verify_operational_authorization(
                receipt_path=args.receipt,
                repo_root=args.repo_root,
                machine_id_path=args.machine_id_path,
                unit=args.unit if args.command == "verify-runtime" else None,
            )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"operational runtime authorization failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTHORIZED_UNITS",
    "CALIBRATION_AUTHORIZED_UNITS",
    "CALIBRATION_PROFILE",
    "DEFAULT_RECEIPT",
    "OPERATIONAL_PROFILE",
    "OWNER_ACKNOWLEDGEMENT",
    "issue_operational_authorization",
    "verify_operational_authorization",
]
