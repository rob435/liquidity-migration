"""Authorize one exact clean commit for bounded demo/paper operation only.

This does not assert replay, alpha, promotion, or real-money evidence. The
demo-operational profile authorizes the demo fleet without a paper twin; the
operational profile adds the explicitly uncalibrated paper integration fleet.
Both profiles disable bulk raw persistence while retaining decision-time market
data.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .candidate_rule_coverage import build_candidate_rule_coverage
from .deterministic_serialization import canonical_json
from .execution_adapters import INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE
from .systemd_environment import parse_systemd_environment_bytes


SCHEMA_VERSION = 2
KIND = "account_execution_operational_runtime_authorization"
VALIDATOR = "account_execution_operational_runtime_authorization_v2"
DEFAULT_RECEIPT = Path(
    "/etc/liquidity-migration/account-execution-operational-ready"
)
OWNER_ACKNOWLEDGEMENT = "AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION"
DEMO_OPERATIONAL_PROFILE = "demo-operational"
OPERATIONAL_PROFILE = "operational"
DEMO_OPERATIONAL_AUTHORIZED_UNITS = (
    "liquidity-migration-account-execution.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-demo-liveness.service",
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
PAPER_RUNTIME_UNITS = frozenset(
    {
        "liquidity-migration-account-paper-execution.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-bybit-long-paper.service",
    }
)
_EXCHANGE_CREDENTIAL_ENVIRONMENT_KEYS = (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
    "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET",
)
REQUIRED_ENVIRONMENT_PATHS = (
    Path("/etc/liquidity-migration/account-execution.env"),
    Path("/etc/liquidity-migration/account-paper-execution.env"),
    Path("/etc/liquidity-migration/bybit-demo.env"),
    Path("/etc/liquidity-migration/sleeves.resolved.env"),
)
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNIT = re.compile(r"liquidity-migration-[a-z0-9-]+\.service")
_FALSE_VALUES = {"", "0", "false", "no", "off"}
PAPER_RUNTIME_USER = "liquidity-migration-paper"
PAPER_RUNTIME_GROUP = "liquidity-migration-paper"
_GROUP_READABLE_ENVIRONMENTS = {
    "account-paper-execution.env",
    "sleeves.resolved.env",
}
_INPUT_KEYS: dict[str, tuple[str, ...]] = {
    "account-execution.env": (
        "ACCOUNT_SYMBOLS_FILE",
        "ACCOUNT_DEMO_RULES_FILE",
        "ACCOUNT_RISK_POLICY_FILE",
    ),
    "account-paper-execution.env": (
        "ACCOUNT_SYMBOLS_FILE",
        "ACCOUNT_DEMO_RULES_FILE",
        "ACCOUNT_RISK_POLICY_FILE",
    ),
}


def _git_output(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def require_clean_authorized_checkout(
    repo_root: str | Path,
    authorized_commit: str,
) -> str:
    """Require the exact clean Git tree bound into an operational receipt."""

    root = Path(repo_root).expanduser().resolve(strict=True)
    commit = str(authorized_commit or "")
    if not _FULL_COMMIT.fullmatch(commit):
        raise ValueError(
            "authorized_commit must be a full 40-character lowercase Git commit"
        )
    head = _git_output(root, "rev-parse", "HEAD").lower()
    if head != commit:
        raise ValueError(
            f"checkout HEAD {head} does not equal authorized commit {commit}"
        )
    if _git_output(root, "status", "--porcelain"):
        raise ValueError("authorized checkout is dirty")
    return head


_CANDIDATE_UNIVERSE_KEY = "CANDIDATE_UNIVERSE_FILE"
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
    DEMO_OPERATIONAL_PROFILE: (
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
_PROFILE_FIELDS: dict[str, dict[str, Any]] = {
    DEMO_OPERATIONAL_PROFILE: {
        "scope": "demo_operational_only_no_paper_no_real_money",
        "raw_market_persistence": "disabled",
        "research_evidence_status": "not_claimed_or_authorized",
        "authorized_units": DEMO_OPERATIONAL_AUTHORIZED_UNITS,
        "demo_raw_value": "0",
        "liveness_scope": "demo",
        "paper_execution_model_scope": "not_applicable_no_paper",
    },
    OPERATIONAL_PROFILE: {
        "scope": "demo_paper_operational_only_no_real_money",
        "raw_market_persistence": "disabled",
        "research_evidence_status": "not_claimed_or_authorized",
        "authorized_units": AUTHORIZED_UNITS,
        "demo_raw_value": "0",
        "liveness_scope": "demo-paper",
        "paper_execution_model_scope": INTEGRATION_ONLY_EXECUTION_MODEL_SCOPE,
    },
}


def _profile_fields(profile: str) -> Mapping[str, Any]:
    try:
        return _PROFILE_FIELDS[profile]
    except KeyError as exc:
        raise ValueError(f"unsupported operational authorization profile: {profile}") from exc


def _paper_user_id() -> int:
    try:
        return int(pwd.getpwnam(PAPER_RUNTIME_USER).pw_uid)
    except KeyError as exc:
        raise ValueError(
            f"paper runtime user is not provisioned: {PAPER_RUNTIME_USER}"
        ) from exc


def _paper_group_id() -> int:
    try:
        return int(grp.getgrnam(PAPER_RUNTIME_GROUP).gr_gid)
    except KeyError as exc:
        raise ValueError(
            f"paper runtime group is not provisioned: {PAPER_RUNTIME_GROUP}"
        ) from exc


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
        "gid": snapshot.metadata.st_gid,
        "nlink": snapshot.nlink,
    }


def _directory_identity(
    value: str,
    *,
    label: str,
    required_uid: int | None = None,
    require_private: bool = False,
) -> tuple[Path, dict[str, Any]]:
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
    owner_uid = os.geteuid() if required_uid is None else required_uid
    if before.st_uid != owner_uid:
        raise ValueError(f"{label} must be owned by uid {owner_uid}")
    if require_private and stat.S_IMODE(before.st_mode) & 0o077:
        raise ValueError(f"{label} must not grant group or other permissions")
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
    snapshot = read_stable_file(
        candidate,
        label=label,
        reject_empty=True,
        require_owner=False,
    )
    if snapshot.mode not in {0o600, 0o640}:
        raise ValueError(f"{label} must have mode 0600 or 0640: {candidate}")
    if snapshot.uid not in {0, os.geteuid()}:
        raise ValueError(f"{label} must be owned by root or the current user")
    if snapshot.mode == 0o640 and snapshot.metadata.st_gid != _paper_group_id():
        raise ValueError(f"{label} group-readable mode is bound to the wrong group")
    return snapshot


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


def _parse_environment_snapshots(
    profile: str,
    *,
    names: Sequence[str] | None = None,
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
    if names is not None:
        unknown = sorted(set(names) - set(required_names))
        if unknown:
            raise ValueError(
                "runtime environment selection is outside the operational profile: "
                + ", ".join(unknown)
            )
        required_names = tuple(names)
    for name in required_names:
        path = paths_by_name[name]
        group_readable = name in _GROUP_READABLE_ENVIRONMENTS
        snapshot = read_stable_file(
            path,
            label=f"operational environment {path.name}",
            reject_empty=True,
            require_mode=0o640 if group_readable else 0o600,
            require_owner=not group_readable,
        )
        if group_readable:
            if snapshot.uid not in {0, os.geteuid()}:
                raise ValueError(
                    f"operational environment {path.name} must be root-owned"
                )
            if snapshot.metadata.st_gid != _paper_group_id():
                raise ValueError(
                    f"operational environment {path.name} has the wrong runtime group"
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
    expected_liveness_scope = profile_fields["liveness_scope"]
    if (
        expected_liveness_scope is not None
        and demo.get("ACCOUNT_LIVENESS_SCOPE") != expected_liveness_scope
    ):
        raise ValueError(
            "demo environment must set "
            f"ACCOUNT_LIVENESS_SCOPE={expected_liveness_scope} for profile {profile}"
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
        if any(paper.get(key) for key in _EXCHANGE_CREDENTIAL_ENVIRONMENT_KEYS):
            raise ValueError("paper operational environment must not contain exchange credentials")
    if not credentials.get("BYBIT_DEMO_API_KEY") or not credentials.get(
        "BYBIT_DEMO_API_SECRET"
    ):
        raise ValueError("demo credentials are missing")
    if any(credentials.get(key) for key in ("BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET")):
        raise ValueError("operational credential file must not contain mainnet credentials")
    if credentials.get("REAL_MONEY", "").strip().lower() not in _FALSE_VALUES:
        raise ValueError("operational credential file does not explicitly disable REAL_MONEY")
    for key in (
        "LONG_SLEEVE",
        "CONTINUOUS_SLEEVE",
        "CONTINUOUS_PAPER_SLEEVE",
        "CONTINUOUS_HEDGE_TIMER",
    ):
        if sleeves.get(key, "").strip().lower() not in {"on", "off"}:
            raise ValueError(f"resolved sleeve environment has invalid {key}")
    if (
        profile == DEMO_OPERATIONAL_PROFILE
        and sleeves.get("CONTINUOUS_PAPER_SLEEVE", "").strip().lower() != "off"
    ):
        raise ValueError(
            "demo-operational profile requires CONTINUOUS_PAPER_SLEEVE=off"
        )

    roots: list[Path] = []
    root_identities: dict[str, dict[str, Any]] = {}
    root_filenames = (
        ("account-execution.env",)
        if profile == DEMO_OPERATIONAL_PROFILE
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
                required_uid=(
                    _paper_user_id()
                    if filename == "account-paper-execution.env"
                    else os.geteuid()
                ),
                require_private=(filename == "account-paper-execution.env"),
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
        if profile == DEMO_OPERATIONAL_PROFILE
        else tuple(_INPUT_KEYS)
    )
    for filename in input_filenames:
        input_keys = _INPUT_KEYS[filename]
        input_keys = (*input_keys, _CANDIDATE_UNIVERSE_KEY)
        environment = values[filename]
        for key in input_keys:
            raw = environment.get(key, "")
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError(f"{filename} {key} must be absolute")
            is_paper_input = filename == "account-paper-execution.env"
            snapshot = read_stable_file(
                path,
                label=f"operational input {filename} {key}",
                reject_empty=True,
                require_mode=0o600,
                require_owner=not is_paper_input,
            )
            if is_paper_input and snapshot.uid != _paper_user_id():
                raise ValueError(
                    f"operational input {filename} {key} must be owned by "
                    f"{PAPER_RUNTIME_USER}"
                )
            inputs[f"{filename}:{key}"] = snapshot
    demo_candidate_path = Path(
        demo.get(_CANDIDATE_UNIVERSE_KEY, "")
    ).expanduser()
    demo_symbols_path = Path(demo.get("ACCOUNT_SYMBOLS_FILE", "")).expanduser()
    if demo_candidate_path != demo_symbols_path:
        raise ValueError(
            "demo operational candidate universe must also be the owner symbols file"
        )
    candidate_snapshot = inputs[
        f"account-execution.env:{_CANDIDATE_UNIVERSE_KEY}"
    ]
    rules_snapshot = inputs["account-execution.env:ACCOUNT_DEMO_RULES_FILE"]
    build_candidate_rule_coverage(
        demo_candidate_path,
        rules_snapshot.path,
        candidate_snapshot=candidate_snapshot,
        demo_rules_snapshot=rules_snapshot,
    )
    if profile == OPERATIONAL_PROFILE:
        assert paper is not None
        for key in (*_INPUT_KEYS["account-execution.env"], _CANDIDATE_UNIVERSE_KEY):
            demo_path = Path(demo.get(key, "")).expanduser()
            paper_path = Path(paper.get(key, "")).expanduser()
            if paper_path == demo_path:
                raise ValueError(
                    f"paper operational input {key} must be an isolated mirror"
                )
            demo_snapshot = inputs[f"account-execution.env:{key}"]
            paper_snapshot = inputs[f"account-paper-execution.env:{key}"]
            if (
                paper_snapshot.data != demo_snapshot.data
                or paper_snapshot.sha256 != demo_snapshot.sha256
            ):
                raise ValueError(
                    f"paper operational input {key} differs from its validated demo source"
                )
    return root_identities, inputs


def _validate_paper_runtime_environments(
    values: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, StableFileSnapshot]]:
    """Revalidate only state a paper process is allowed to read.

    Full-profile issuance still validates and binds the demo account and its
    credential file. A paper process has no reason to reopen either one after
    issuance: doing so defeats the credential boundary even when systemd later
    removes the variables from its inherited environment.
    """

    paper = values["account-paper-execution.env"]
    sleeves = values["sleeves.resolved.env"]
    if paper.get("ACCOUNT_PAPER_KERNEL_REQUIRED") != "1":
        raise ValueError("paper operational environment does not require the account kernel")
    if paper.get("ACCOUNT_RAW_MARKET_PERSISTENCE") != "0":
        raise ValueError(
            "paper operational environment must set ACCOUNT_RAW_MARKET_PERSISTENCE=0"
        )
    if any(paper.get(key) for key in _EXCHANGE_CREDENTIAL_ENVIRONMENT_KEYS):
        raise ValueError("paper operational environment must not contain exchange credentials")
    for key in (
        "LONG_SLEEVE",
        "CONTINUOUS_SLEEVE",
        "CONTINUOUS_PAPER_SLEEVE",
        "CONTINUOUS_HEDGE_TIMER",
    ):
        if sleeves.get(key, "").strip().lower() not in {"on", "off"}:
            raise ValueError(f"resolved sleeve environment has invalid {key}")

    root_identities: dict[str, dict[str, Any]] = {}
    for key in _ROOT_KEYS["account-paper-execution.env"]:
        _root, identity = _directory_identity(
            paper.get(key, ""),
            label=f"account-paper-execution.env {key}",
            required_uid=_paper_user_id(),
            require_private=True,
        )
        root_identities[f"account-paper-execution.env:{key}"] = identity

    inputs: dict[str, StableFileSnapshot] = {}
    input_keys = (*_INPUT_KEYS["account-paper-execution.env"], _CANDIDATE_UNIVERSE_KEY)
    for key in input_keys:
        path = Path(paper.get(key, "")).expanduser()
        if not path.is_absolute():
            raise ValueError(f"account-paper-execution.env {key} must be absolute")
        snapshot = read_stable_file(
            path,
            label=f"operational input account-paper-execution.env {key}",
            reject_empty=True,
            require_mode=0o600,
            require_owner=False,
        )
        if snapshot.uid != _paper_user_id():
            raise ValueError(
                f"operational input account-paper-execution.env {key} must be owned "
                f"by {PAPER_RUNTIME_USER}"
            )
        inputs[f"account-paper-execution.env:{key}"] = snapshot
    candidate_path = Path(paper.get(_CANDIDATE_UNIVERSE_KEY, "")).expanduser()
    symbols_path = Path(paper.get("ACCOUNT_SYMBOLS_FILE", "")).expanduser()
    if candidate_path != symbols_path:
        raise ValueError(
            "paper operational candidate universe must also be the owner symbols file"
        )
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
        "paper_execution_model_scope",
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
    expected_mode = 0o640 if profile == OPERATIONAL_PROFILE else 0o600
    if (
        snapshot.mode != expected_mode
        or payload.get("schema_version") != SCHEMA_VERSION
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
        or payload.get("paper_execution_model_scope")
        != profile_fields["paper_execution_model_scope"]
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
        "paper_execution_model_scope": profile_fields[
            "paper_execution_model_scope"
        ],
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
            receipt_mode = 0o640 if profile == OPERATIONAL_PROFILE else 0o600
            os.fchmod(descriptor, receipt_mode)
            if profile == OPERATIONAL_PROFILE:
                os.fchown(descriptor, -1, _paper_group_id())
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
    paper_runtime = unit in PAPER_RUNTIME_UNITS
    if paper_runtime:
        environment_snapshots, values = _parse_environment_snapshots(
            profile,
            names=("account-paper-execution.env", "sleeves.resolved.env"),
        )
        root_identities, input_snapshots = _validate_paper_runtime_environments(
            values
        )
    else:
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
    if paper_runtime:
        if not set(environment_snapshots).issubset(environment_payload):
            raise ValueError("paper runtime authorization environment set changed")
        if not set(input_snapshots).issubset(input_payload):
            raise ValueError("paper runtime authorization input set changed")
        if any(root_payload.get(name) != identity for name, identity in root_identities.items()):
            raise ValueError("paper operational runtime root changed after authorization")
    else:
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
    if paper_runtime and any(
        os.environ.get(key)
        for key in ("BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET")
    ):
        raise ValueError("paper runtime environment contains demo credentials")
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
    expected_liveness_scope = profile_fields["liveness_scope"]
    if (
        unit == "liquidity-migration-demo-liveness.service"
        and os.environ.get("ACCOUNT_LIVENESS_SCOPE") != expected_liveness_scope
    ):
        raise ValueError(
            "liveness runtime did not inherit the authorized account scope"
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
        choices=(
            DEMO_OPERATIONAL_PROFILE,
            OPERATIONAL_PROFILE,
        ),
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
    "DEMO_OPERATIONAL_AUTHORIZED_UNITS",
    "DEMO_OPERATIONAL_PROFILE",
    "DEFAULT_RECEIPT",
    "OPERATIONAL_PROFILE",
    "OWNER_ACKNOWLEDGEMENT",
    "issue_operational_authorization",
    "verify_operational_authorization",
]
