"""Immutable pre-window evidence manifest for the natural account cutover.

The freeze is deliberately a source manifest, not an execution permission.  It
reopens every receipt it names, binds the clean candidate and the exact natural
window, and independently checks the six-root ``--leave-stopped`` reset.  A
valid freeze can therefore be used by later replay/drift/sufficiency tools
without trusting copied operator summaries.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .account_execution_config import require_registered_demo_rule_max_age_hours
from .account_market_readiness import (
    require_registered_request_market_warmup_timeout,
)
from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .systemd_environment import parse_systemd_environment_bytes
from .deterministic_serialization import canonical_json


SCHEMA_VERSION = 1
KIND = "account_execution_natural_cutover_freeze"
VALIDATOR = "natural_cutover_freeze_v1"
HOUR_NS = 60 * 60 * 1_000_000_000
WINDOW_HOURS = 120
WINDOW_NS = WINDOW_HOURS * HOUR_NS
MAX_CLOCK_AGE_HOURS = 24.0
MAX_INITIAL_CLOCK_DISTANCE_TO_T0_HOURS = 6.0

LOCAL_SUITE_KIND = "account_cutover_local_suite_receipt"
LINUX_CI_KIND = "account_cutover_linux_ci_receipt"
LOCAL_SUITE_VALIDATOR = "account_cutover_local_suite_v1"
LINUX_CI_VALIDATOR = "account_cutover_linux_ci_v1"
GITHUB_PROVENANCE_KIND = "github_actions_run_provenance"
GITHUB_PROVENANCE_VALIDATOR = "github_actions_run_provenance_v1"
WORKFLOW_NAME = "VPS Deploy"
WORKFLOW_PATH = ".github/workflows/vps-deploy.yml"
CI_JOB_NAME = "ci"
VPS_JOB_NAME = "vps"
READ_ONLY_VERIFY_STEP = "Read-only verify"
CANDIDATE_CI_STEP = "Confirm candidate CI-only boundary"
CANDIDATE_CI_EXTERNAL_STEPS = frozenset(
    {
        "Configure SSH key",
        "Verify deploy key fingerprint",
        "Pin VPS host key",
        READ_ONLY_VERIFY_STEP,
    }
)
CI_REQUIRED_STEPS = (
    "Install dependencies",
    "Ruff (lint gate)",
    "Pytest (full suite gate)",
)
MUTATING_WORKFLOW_STEPS = frozenset(
    {
        "Checked deploy",
        "Install current topology without authorization or startup",
        "Wait for SSH recovery then deploy",
    }
)
EXACT_CANDIDATE_CI_EVENT = "workflow_dispatch"
PAPER_OWNER_ROLE = "paper_owner_start_sequence"
DEMO_OWNER_ROLE = "demo_owner_start_sequence"
PAPER_OWNER_CLAIM = (
    "PAPER_OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_PAPER_PRODUCER_START_AND_STOPPED_BEFORE_REPLAY"
)
DEMO_OWNER_CLAIM = "DEMO_OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_DEMO_PRODUCER_START"

EXPECTED_ACCOUNT_IDS = {
    "demo": "bybit-demo-unified",
    "paper": "bybit-paper-unified",
}
ROOT_ENVIRONMENTS = ("demo", "paper")
ROOT_KINDS = ("account", "inbox", "capture")
LIMITATIONS = [
    "self_hash_is_not_a_signature",
    "operator_reviewed_owner_order_is_not_machine_observed_topology",
    "v7_is_training_and_cannot_satisfy_the_natural_holdout",
    "freeze_does_not_authorize_deployment_or_execution",
]
_FIXED_SOURCE_LABELS = frozenset(
    {
        "local_suite",
        "linux_ci",
        "clock",
        "candidate_universe",
        "demo_rules",
        "rule_coverage",
        "calibration",
        "archive_map",
        "baseline_config",
        "stress_config",
        "reset_archive",
        "reset_sha256_sidecar",
        "reset_receipt",
        "paper_owner_first",
        "demo_owner_first",
        "seed",
    }
)


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    label: str
    path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mtime_ns: int
    mode: int
    uid: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _freeze_id(payload: Mapping[str, Any]) -> str:
    material = {**dict(payload), "freeze_id": "", "artifact_sha256": ""}
    return "natural-cutover-" + hashlib.sha256(canonical_json(material)).hexdigest()


def _lower_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return digest


def _full_commit(value: object, *, label: str) -> str:
    commit = str(value or "")
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError(f"{label} must be a full lowercase 40-character Git commit")
    return commit


def _read_identity(
    path: str | Path,
    *,
    label: str,
    require_private: bool = True,
    include_data: bool = True,
    snapshot: StableFileSnapshot | None = None,
) -> tuple[_FileIdentity, bytes]:
    if snapshot is None:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        snapshot = read_stable_file(
            candidate,
            label=label,
            require_mode=0o600 if require_private else None,
            require_owner=True,
            require_single_link=False,
        )
    else:
        resolved = Path(path).expanduser().absolute()
        if snapshot.path != resolved:
            raise ValueError(f"{label} snapshot path differs")
        if require_private and snapshot.mode != 0o600:
            raise ValueError(f"{label} must have exact mode 0600")
        if snapshot.uid != os.geteuid():
            raise ValueError(f"{label} must be owned by the verifier")
    return (
        _FileIdentity(
            label=label,
            path=str(snapshot.path),
            size_bytes=snapshot.size,
            sha256=snapshot.sha256,
            device=snapshot.device,
            inode=snapshot.inode,
            mtime_ns=snapshot.mtime_ns,
            mode=snapshot.mode,
            uid=snapshot.uid,
        ),
        snapshot.data if include_data else b"",
    )


def _strict_json(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _artifact_ref(identity: _FileIdentity, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": identity.path,
        "file_sha256": identity.sha256,
        "artifact_sha256": _lower_sha256(
            payload.get("artifact_sha256"), label=f"{identity.label} artifact hash"
        ),
    }


_IDENTITY_FIELDS = {
    "label",
    "path",
    "size_bytes",
    "sha256",
    "device",
    "inode",
    "mtime_ns",
    "mode",
    "uid",
}


def _identity_from_payload(value: object, *, label: str) -> _FileIdentity:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_FIELDS:
        raise ValueError(f"{label} source identity has unexpected fields")
    if value.get("label") != label:
        raise ValueError(f"{label} source identity has the wrong label")
    identity = _FileIdentity(
        label=label,
        path=str(value.get("path") or ""),
        size_bytes=int(value.get("size_bytes") or 0),
        sha256=_lower_sha256(value.get("sha256"), label=f"{label} file hash"),
        device=int(value.get("device") or 0),
        inode=int(value.get("inode") or 0),
        mtime_ns=int(value.get("mtime_ns") or 0),
        mode=int(value.get("mode") or 0),
        uid=int(value.get("uid") or -1),
    )
    if (
        identity.size_bytes < 0
        or identity.device <= 0
        or identity.inode <= 0
        or identity.mtime_ns <= 0
        or identity.mode != 0o600
        or identity.uid < 0
    ):
        raise ValueError(f"{label} source identity is invalid")
    return identity


def _hash_file_range(data: bytes, *, start: int, end: int) -> str:
    if start < 0 or end < start:
        raise ValueError("log byte range is invalid")
    if end > len(data):
        raise ValueError("local-suite log is shorter than its command range")
    return hashlib.sha256(data[start:end]).hexdigest()


def _resolve_new_output(path: str | Path, *, label: str) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink path")
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError(f"{label} parent must be a non-symlink directory")
    resolved = parent / output.name
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError(f"{label} already exists: {resolved}")
    return resolved


def _outside_repository(path: Path, *, repository_root: Path, label: str) -> None:
    try:
        path.relative_to(repository_root)
    except ValueError:
        return
    raise ValueError(f"{label} must be outside the candidate worktree")


def _candidate_checkout(
    repository_root: str | Path, *, candidate_commit: str
) -> tuple[Path, str, str]:
    root = Path(repository_root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("repository_root must be an absolute non-symlink directory")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository_root must be a directory")
    candidate = _full_commit(candidate_commit, label="candidate commit")
    if _git(root, "rev-parse", "HEAD") != candidate:
        raise ValueError("candidate commit is not the repository HEAD")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate checkout is not clean")
    origin_url = _git(root, "remote", "get-url", "origin")
    if not origin_url:
        raise ValueError("candidate checkout has no origin URL")
    return root, candidate, origin_url


def _python_executable(value: str | Path) -> Path:
    raw = str(value)
    discovered = shutil.which(raw) if not Path(raw).expanduser().is_absolute() else raw
    if not discovered:
        raise ValueError(f"Python executable is unavailable: {raw}")
    # Preserve a venv launcher path. Resolving its final symlink invokes the
    # base interpreter directly and silently drops the venv's site-packages.
    executable = Path(os.path.abspath(Path(discovered).expanduser()))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Python executable is not an executable regular file: {executable}")
    return executable


def verify_local_suite_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_candidate_commit: str | None = None,
    require_passed: bool = True,
) -> dict[str, Any]:
    """Reopen the command log and validate the canonical local full-suite run."""

    payload = dict(receipt)
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "status",
        "candidate_commit",
        "started_ts_ns",
        "finished_ts_ns",
        "repository",
        "python_executable",
        "commands",
        "log_source",
        "gate_passed",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("local-suite receipt has unexpected or missing fields")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != LOCAL_SUITE_KIND
        or payload.get("validator") != LOCAL_SUITE_VALIDATOR
    ):
        raise ValueError("local-suite receipt identity is unsupported")
    candidate = _full_commit(payload.get("candidate_commit"), label="local-suite candidate")
    if expected_candidate_commit is not None and candidate != _full_commit(
        expected_candidate_commit, label="expected local-suite candidate"
    ):
        raise ValueError("local-suite receipt names another candidate commit")
    if payload.get("artifact_sha256") != _self_hash(payload):
        raise ValueError("local-suite receipt self-hash is invalid")
    started = int(payload.get("started_ts_ns") or 0)
    finished = int(payload.get("finished_ts_ns") or 0)
    if started <= 0 or finished < started:
        raise ValueError("local-suite receipt timestamps are invalid")

    repository = payload.get("repository")
    if not isinstance(repository, Mapping) or set(repository) != {
        "root",
        "origin_url",
        "head_before",
        "head_after",
        "clean_before",
        "clean_after",
    }:
        raise ValueError("local-suite repository binding is malformed")
    repository_root = str(repository.get("root") or "")
    head_after = _full_commit(
        repository.get("head_after"), label="local-suite post-run HEAD"
    )
    if (
        not Path(repository_root).is_absolute()
        or not str(repository.get("origin_url") or "").strip()
        or repository.get("head_before") != candidate
        or repository.get("clean_before") is not True
        or type(repository.get("clean_after")) is not bool
    ):
        raise ValueError("local-suite repository binding is invalid")

    executable = str(payload.get("python_executable") or "")
    if not Path(executable).is_absolute():
        raise ValueError("local-suite Python executable must be absolute")
    commands = payload.get("commands")
    if not isinstance(commands, list) or len(commands) != 2:
        raise ValueError("local-suite receipt must contain exact Ruff and pytest commands")
    expected_ids = ("ruff", "pytest")
    expected_argv_prefixes = (
        [executable, "-m", "ruff", "check", "liquidity_migration", "tests", "scripts"],
        [executable, "-m", "pytest", "-q", "--basetemp"],
    )
    prior_end = 0
    prior_finished = started
    all_commands_passed = True
    log_identity = _identity_from_payload(payload.get("log_source"), label="local_suite_log")
    observed_identity, log_data = _read_identity(
        log_identity.path,
        label="local_suite_log",
        require_private=True,
    )
    if observed_identity != log_identity:
        raise ValueError("local-suite command log changed after receipt creation")
    for index, (raw, command_id, expected_prefix) in enumerate(
        zip(commands, expected_ids, expected_argv_prefixes, strict=True)
    ):
        if not isinstance(raw, Mapping) or set(raw) != {
            "command_id",
            "argv",
            "cwd",
            "started_ts_ns",
            "finished_ts_ns",
            "exit_code",
            "log_start_byte",
            "log_end_byte",
            "log_sha256",
        }:
            raise ValueError(f"local-suite command {index} is malformed")
        argv = raw.get("argv")
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise ValueError(f"local-suite command {index} argv is malformed")
        if command_id == "ruff":
            if argv != expected_prefix:
                raise ValueError("local-suite Ruff command differs from the registered command")
        else:
            if len(argv) != 6 or argv[:5] != expected_prefix:
                raise ValueError("local-suite pytest command differs from the registered command")
            basetemp = Path(argv[5])
            expected_tmp_parent = Path(log_identity.path).parent
            expected_name = re.fullmatch(
                rf"pytest-natural-freeze-{re.escape(candidate[:12])}-[1-9][0-9]*-[1-9][0-9]*",
                basetemp.name,
            )
            if (
                not basetemp.is_absolute()
                or basetemp.parent != expected_tmp_parent
                or basetemp.is_relative_to(Path(repository_root))
                or expected_name is None
            ):
                raise ValueError(
                    "local-suite pytest basetemp must be a unique registered path "
                    "beside the external command log"
                )
        command_started = int(raw.get("started_ts_ns") or 0)
        command_finished = int(raw.get("finished_ts_ns") or 0)
        log_start = int(raw.get("log_start_byte") or 0)
        log_end = int(raw.get("log_end_byte") or 0)
        exit_code = raw.get("exit_code")
        if (
            raw.get("command_id") != command_id
            or raw.get("cwd") != repository_root
            or command_started < started
            or command_started < prior_finished
            or command_finished < command_started
            or command_finished > finished
            or type(exit_code) is not int
            or log_start != prior_end
            or log_end < log_start
        ):
            raise ValueError(f"local-suite command {index} metadata is invalid")
        observed_range_hash = _hash_file_range(
            log_data,
            start=log_start,
            end=log_end,
        )
        if raw.get("log_sha256") != observed_range_hash:
            raise ValueError(f"local-suite command {index} log segment hash is invalid")
        prior_end = log_end
        prior_finished = command_finished
        all_commands_passed = all_commands_passed and exit_code == 0
    if prior_end != log_identity.size_bytes:
        raise ValueError("local-suite command ranges do not cover the exact log")
    after_log_identity, _ = _read_identity(
        log_identity.path,
        label="local_suite_log",
        require_private=True,
        include_data=False,
    )
    if after_log_identity != observed_identity:
        raise RuntimeError("local-suite command log changed while it was validated")
    derived_pass = (
        all_commands_passed
        and head_after == candidate
        and repository.get("clean_after") is True
    )
    expected_status = "passed" if derived_pass else "failed"
    if payload.get("status") != expected_status or payload.get("gate_passed") is not derived_pass:
        raise ValueError("local-suite status does not match command and repository results")
    if payload.get("limitations") != [
        "local_process_receipt_is_not_a_remote_attestation",
        "linux_ci_is_an_independent_required_gate",
    ]:
        raise ValueError("local-suite receipt limitations are invalid")
    if require_passed and not derived_pass:
        raise ValueError("local-suite gate has not passed")
    return payload


def load_local_suite_receipt(
    path: str | Path,
    *,
    expected_candidate_commit: str | None = None,
    require_passed: bool = True,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    receipt_identity, data = _read_identity(
        path,
        label="local suite receipt",
        require_private=True,
        snapshot=snapshot,
    )
    payload = verify_local_suite_receipt(
        _strict_json(data, label="local suite receipt"),
        expected_candidate_commit=expected_candidate_commit,
        require_passed=require_passed,
    )
    after, _ = _read_identity(
        receipt_identity.path,
        label="local suite receipt",
        require_private=True,
        include_data=False,
    )
    if after != receipt_identity:
        raise RuntimeError("local-suite receipt changed while it was validated")
    return payload


def write_local_suite_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    payload = verify_local_suite_receipt(receipt, require_passed=False)
    return _atomic_create(path, payload)


def run_local_suite(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    python_executable: str | Path,
    log_path: str | Path,
    output_path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Run the canonical local Ruff/full-pytest gate and write its receipt."""

    root, candidate, origin_url = _candidate_checkout(
        repository_root, candidate_commit=candidate_commit
    )
    executable = _python_executable(python_executable)
    log_output = _resolve_new_output(log_path, label="local-suite log output")
    receipt_output = _resolve_new_output(output_path, label="local-suite receipt output")
    _outside_repository(log_output, repository_root=root, label="local-suite log output")
    _outside_repository(receipt_output, repository_root=root, label="local-suite receipt output")
    started = time.time_ns()
    pytest_basetemp = _resolve_new_output(
        log_output.parent
        / f"pytest-natural-freeze-{candidate[:12]}-{started}-{os.getpid()}",
        label="local-suite pytest basetemp",
    )
    _outside_repository(
        pytest_basetemp,
        repository_root=root,
        label="local-suite pytest basetemp",
    )
    commands = [
        (
            "ruff",
            [
                str(executable),
                "-m",
                "ruff",
                "check",
                "liquidity_migration",
                "tests",
                "scripts",
            ],
        ),
        (
            "pytest",
            [
                str(executable),
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(pytest_basetemp),
            ],
        ),
    ]
    descriptor = os.open(str(log_output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    results: list[dict[str, Any]] = []
    try:
        for command_id, argv in commands:
            command_started = time.time_ns()
            log_start = os.lseek(descriptor, 0, os.SEEK_CUR)
            try:
                completed = subprocess.run(
                    argv,
                    cwd=root,
                    stdout=descriptor,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                exit_code = int(completed.returncode)
            except OSError as exc:
                message = f"unable to execute {command_id}: {exc}\n".encode(
                    "utf-8", errors="replace"
                )
                os.write(descriptor, message)
                exit_code = 127
            command_finished = time.time_ns()
            log_end = os.lseek(descriptor, 0, os.SEEK_CUR)
            os.fsync(descriptor)
            results.append(
                {
                    "command_id": command_id,
                    "argv": argv,
                    "cwd": str(root),
                    "started_ts_ns": command_started,
                    "finished_ts_ns": command_finished,
                    "exit_code": exit_code,
                    "log_start_byte": log_start,
                    "log_end_byte": log_end,
                    "log_sha256": "",
                }
            )
    finally:
        os.close(descriptor)
    log_identity, log_data = _read_identity(
        log_output,
        label="local_suite_log",
        require_private=True,
    )
    for result in results:
        result["log_sha256"] = _hash_file_range(
            log_data,
            start=int(result["log_start_byte"]),
            end=int(result["log_end_byte"]),
        )
    finished = time.time_ns()
    head_after = _git(root, "rev-parse", "HEAD")
    clean_after = not bool(
        _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    )
    gate_passed = (
        head_after == candidate
        and clean_after
        and all(result["exit_code"] == 0 for result in results)
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": LOCAL_SUITE_KIND,
        "validator": LOCAL_SUITE_VALIDATOR,
        "status": "passed" if gate_passed else "failed",
        "candidate_commit": candidate,
        "started_ts_ns": started,
        "finished_ts_ns": finished,
        "repository": {
            "root": str(root),
            "origin_url": origin_url,
            "head_before": candidate,
            "head_after": head_after,
            "clean_before": True,
            "clean_after": clean_after,
        },
        "python_executable": str(executable),
        "commands": results,
        "log_source": log_identity.to_dict(),
        "gate_passed": gate_passed,
        "limitations": [
            "local_process_receipt_is_not_a_remote_attestation",
            "linux_ci_is_an_independent_required_gate",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    output = write_local_suite_receipt(receipt_output, payload)
    return output, payload


def _github_repository_slug(origin_url: str) -> str:
    patterns = (
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        r"git@github\.com:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"ssh://git@github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, origin_url)
        if match:
            return match.group(1)
    raise ValueError("origin must be an exact github.com owner/repository URL")


def _gh_api_json(endpoint: str, *, repository_root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["GH_PROMPT_DISABLED"] = "1"
    gh = shutil.which("gh")
    if gh is not None:
        try:
            result = subprocess.run(
                [gh, "api", "--hostname", "github.com", endpoint],
                cwd=repository_root,
                env=environment,
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        else:
            return _strict_json(
                result.stdout, label=f"GitHub API response {endpoint}"
            )

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "liquidity-migration-cutover-freeze/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/{endpoint}", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            data = response.read(20 * 1024 * 1024 + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ValueError(
            f"cannot fetch GitHub Actions provenance: {endpoint}"
        ) from exc
    if len(data) > 20 * 1024 * 1024:
        raise ValueError(f"GitHub API response is unexpectedly large: {endpoint}")
    return _strict_json(data, label=f"GitHub API response {endpoint}")


def _decode_workflow_content(payload: Mapping[str, Any]) -> bytes:
    if (
        payload.get("type") != "file"
        or payload.get("path") != WORKFLOW_PATH
        or payload.get("encoding") != "base64"
        or not isinstance(payload.get("content"), str)
    ):
        raise ValueError("GitHub workflow-content response is not the registered workflow file")
    try:
        encoded = "".join(str(payload["content"]).splitlines())
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("GitHub workflow content is not valid base64") from exc
    blob_hash = hashlib.sha1(  # noqa: S324 - GitHub Git-blob identity is defined as SHA-1.
        f"blob {len(decoded)}\0".encode() + decoded
    ).hexdigest()
    if payload.get("sha") != blob_hash:
        raise ValueError("GitHub workflow content does not match its Git blob SHA")
    return decoded


def _github_provenance_payload(
    *,
    repository_full_name: str,
    candidate_commit: str,
    run_id: int,
    fetched_ts_ns: int,
    run: Mapping[str, Any],
    jobs: Mapping[str, Any],
    workflow_content: Mapping[str, Any],
) -> dict[str, Any]:
    run_endpoint = f"repos/{repository_full_name}/actions/runs/{run_id}"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": GITHUB_PROVENANCE_KIND,
        "validator": GITHUB_PROVENANCE_VALIDATOR,
        "fetched_ts_ns": fetched_ts_ns,
        "repository_full_name": repository_full_name,
        "candidate_commit": candidate_commit,
        "api_endpoints": {
            "run": run_endpoint,
            "jobs": f"{run_endpoint}/jobs?filter=all&per_page=100",
            "workflow_content": (
                f"repos/{repository_full_name}/contents/{WORKFLOW_PATH}"
                f"?ref={candidate_commit}"
            ),
        },
        "run": dict(run),
        "jobs": dict(jobs),
        "workflow_content": dict(workflow_content),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _verify_github_provenance(
    payload: Mapping[str, Any],
    *,
    expected_candidate_commit: str,
) -> dict[str, Any]:
    provenance = dict(payload)
    if set(provenance) != {
        "schema_version",
        "kind",
        "validator",
        "fetched_ts_ns",
        "repository_full_name",
        "candidate_commit",
        "api_endpoints",
        "run",
        "jobs",
        "workflow_content",
        "artifact_sha256",
    }:
        raise ValueError("GitHub provenance has unexpected or missing fields")
    if (
        provenance.get("schema_version") != 1
        or provenance.get("kind") != GITHUB_PROVENANCE_KIND
        or provenance.get("validator") != GITHUB_PROVENANCE_VALIDATOR
        or provenance.get("artifact_sha256") != _self_hash(provenance)
    ):
        raise ValueError("GitHub provenance identity or self-hash is invalid")
    candidate = _full_commit(
        provenance.get("candidate_commit"), label="GitHub provenance candidate"
    )
    if candidate != _full_commit(
        expected_candidate_commit, label="expected GitHub provenance candidate"
    ):
        raise ValueError("GitHub provenance names another candidate commit")
    fetched = int(provenance.get("fetched_ts_ns") or 0)
    repository = str(provenance.get("repository_full_name") or "")
    if fetched <= 0 or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("GitHub provenance source metadata is invalid")
    run = provenance.get("run")
    jobs = provenance.get("jobs")
    workflow = provenance.get("workflow_content")
    endpoints = provenance.get("api_endpoints")
    if not all(isinstance(value, Mapping) for value in (run, jobs, workflow, endpoints)):
        raise ValueError("GitHub provenance API payloads are malformed")
    run_id = int(cast(Mapping[str, Any], run).get("id") or 0)
    run_endpoint = f"repos/{repository}/actions/runs/{run_id}"
    if dict(cast(Mapping[str, Any], endpoints)) != {
        "run": run_endpoint,
        "jobs": f"{run_endpoint}/jobs?filter=all&per_page=100",
        "workflow_content": f"repos/{repository}/contents/{WORKFLOW_PATH}?ref={candidate}",
    }:
        raise ValueError("GitHub provenance API endpoints do not match the bound run")
    _decode_workflow_content(cast(Mapping[str, Any], workflow))
    return provenance


def _derive_linux_ci_evidence(
    provenance: Mapping[str, Any],
    *,
    candidate_commit: str,
    repository_root: Path,
) -> dict[str, Any]:
    candidate = _full_commit(candidate_commit, label="Linux CI candidate")
    verified = _verify_github_provenance(
        provenance, expected_candidate_commit=candidate
    )
    repository_full_name = str(verified["repository_full_name"])
    run = cast(Mapping[str, Any], verified["run"])
    jobs_payload = cast(Mapping[str, Any], verified["jobs"])
    workflow_payload = cast(Mapping[str, Any], verified["workflow_content"])
    run_id = int(run.get("id") or 0)
    run_attempt = int(run.get("run_attempt") or 0)
    run_url = str(run.get("html_url") or "")
    expected_run_url = (
        f"https://github.com/{repository_full_name}/actions/runs/{run_id}"
    )
    if (
        run_id <= 0
        or run_attempt <= 0
        or run.get("name") != WORKFLOW_NAME
        or run.get("path") != WORKFLOW_PATH
        or run.get("head_sha") != candidate
        or run_url != expected_run_url
    ):
        raise ValueError("GitHub run does not bind the exact candidate workflow identity")
    run_repository = run.get("repository")
    if not isinstance(run_repository, Mapping) or run_repository.get(
        "full_name"
    ) != repository_full_name:
        raise ValueError("GitHub run belongs to another repository")
    event = str(run.get("event") or "")
    if event != EXACT_CANDIDATE_CI_EVENT:
        raise ValueError(
            "Linux CI evidence must come from an exact-head workflow_dispatch; "
            "pull_request tests a synthetic merge SHA and push crosses the deploy boundary"
        )

    raw_jobs = jobs_payload.get("jobs")
    total_count = jobs_payload.get("total_count")
    if (
        not isinstance(raw_jobs, list)
        or type(total_count) is not int
        or total_count != len(raw_jobs)
        or total_count <= 0
    ):
        raise ValueError("GitHub jobs response is incomplete or malformed")
    current_jobs = [
        job
        for job in raw_jobs
        if isinstance(job, Mapping) and int(job.get("run_attempt") or 0) == run_attempt
    ]
    ci_jobs = [job for job in current_jobs if job.get("name") == CI_JOB_NAME]
    if len(ci_jobs) != 1:
        raise ValueError("GitHub run must contain exactly one current-attempt ci job")
    vps_jobs = [job for job in current_jobs if job.get("name") == VPS_JOB_NAME]
    if len(vps_jobs) != 1 or {str(job.get("name") or "") for job in current_jobs} != {
        CI_JOB_NAME,
        VPS_JOB_NAME,
    }:
        raise ValueError("GitHub run must contain exact current-attempt ci and vps jobs")
    ci_job = cast(Mapping[str, Any], ci_jobs[0])
    vps_job = cast(Mapping[str, Any], vps_jobs[0])
    ci_job_id = int(ci_job.get("id") or 0)
    ci_job_url = str(ci_job.get("html_url") or "")
    if (
        ci_job_id <= 0
        or ci_job_url
        != f"https://github.com/{repository_full_name}/actions/runs/{run_id}/job/{ci_job_id}"
        or ci_job.get("head_sha") != candidate
    ):
        raise ValueError("GitHub ci job did not execute the exact candidate commit")
    raw_steps = ci_job.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("GitHub ci job has no source-backed step results")
    required_step_results: dict[str, str] = {}
    for required_name in CI_REQUIRED_STEPS:
        matching = [
            step
            for step in raw_steps
            if isinstance(step, Mapping) and step.get("name") == required_name
        ]
        if len(matching) != 1:
            raise ValueError(f"GitHub ci job lacks exact step {required_name!r}")
        required_step_results[required_name] = str(matching[0].get("conclusion") or "")

    mutation_results: dict[str, list[str]] = {
        name: [] for name in sorted(MUTATING_WORKFLOW_STEPS)
    }
    for job in current_jobs:
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            name = str(step.get("name") or "")
            if name in MUTATING_WORKFLOW_STEPS:
                mutation_results[name].append(str(step.get("conclusion") or ""))
    missing_mutation_steps = [
        name for name, conclusions in mutation_results.items() if len(conclusions) != 1
    ]
    if missing_mutation_steps:
        raise ValueError(
            "workflow-dispatch provenance lacks exact deployment-step results: "
            + ", ".join(missing_mutation_steps)
        )
    mutation_steps_skipped = all(
        conclusion == "skipped"
        for conclusions in mutation_results.values()
        for conclusion in conclusions
    )
    if not mutation_steps_skipped:
        raise ValueError("Linux CI workflow run executed a mutating deployment step")
    vps_steps = vps_job.get("steps")
    if not isinstance(vps_steps, list):
        raise ValueError("workflow-dispatch vps job lacks step results")
    candidate_ci_steps = [
        step
        for step in vps_steps
        if isinstance(step, Mapping) and step.get("name") == CANDIDATE_CI_STEP
    ]
    external_step_results: dict[str, list[str]] = {
        name: [] for name in sorted(CANDIDATE_CI_EXTERNAL_STEPS)
    }
    for step in vps_steps:
        if not isinstance(step, Mapping):
            continue
        name = str(step.get("name") or "")
        if name in external_step_results:
            external_step_results[name].append(str(step.get("conclusion") or ""))
    malformed_external_steps = [
        name for name, conclusions in external_step_results.items() if len(conclusions) != 1
    ]
    if malformed_external_steps:
        raise ValueError(
            "candidate-CI provenance lacks exact external-step results: "
            + ", ".join(malformed_external_steps)
        )
    external_steps_skipped = all(
        conclusion == "skipped"
        for conclusions in external_step_results.values()
        for conclusion in conclusions
    )
    read_only_steps = [
        step
        for step in vps_steps
        if isinstance(step, Mapping) and step.get("name") == READ_ONLY_VERIFY_STEP
    ]
    if (
        len(candidate_ci_steps) != 1
        or candidate_ci_steps[0].get("conclusion") != "success"
        or len(read_only_steps) != 1
        or read_only_steps[0].get("conclusion") != "skipped"
        or not external_steps_skipped
        or vps_job.get("status") != "completed"
        or vps_job.get("conclusion") != "success"
        or vps_job.get("head_sha") != candidate
    ):
        raise ValueError(
            "workflow-dispatch did not complete the exact-candidate CI-only path"
        )

    github_workflow_bytes = _decode_workflow_content(workflow_payload)
    local_workflow = repository_root / WORKFLOW_PATH
    local_identity, local_workflow_bytes = _read_identity(
        local_workflow,
        label="candidate workflow file",
        require_private=False,
    )
    if local_workflow_bytes != github_workflow_bytes:
        raise ValueError("GitHub and local candidate workflow bytes differ")
    workflow_hash = hashlib.sha256(github_workflow_bytes).hexdigest()
    run_passed = run.get("status") == "completed" and run.get("conclusion") == "success"
    ci_passed = (
        ci_job.get("status") == "completed"
        and ci_job.get("conclusion") == "success"
        and all(value == "success" for value in required_step_results.values())
    )
    gate_passed = (
        run_passed and ci_passed and mutation_steps_skipped and external_steps_skipped
    )
    return {
        "repository_full_name": repository_full_name,
        "workflow": {
            "name": WORKFLOW_NAME,
            "path": WORKFLOW_PATH,
            "file_sha256": workflow_hash,
            "github_blob_sha": str(workflow_payload.get("sha") or ""),
            "local_file": {
                "path": local_identity.path,
                "sha256": local_identity.sha256,
                "size_bytes": local_identity.size_bytes,
            },
        },
        "run": {
            "id": run_id,
            "attempt": run_attempt,
            "url": run_url,
            "event": event,
            "head_sha": candidate,
            "status": str(run.get("status") or ""),
            "conclusion": str(run.get("conclusion") or ""),
        },
        "ci_job": {
            "id": ci_job_id,
            "url": ci_job_url,
            "name": CI_JOB_NAME,
            "head_sha": candidate,
            "status": str(ci_job.get("status") or ""),
            "conclusion": str(ci_job.get("conclusion") or ""),
            "required_steps": required_step_results,
        },
        "deployment_safety": {
            "push_event_rejected": True,
            "mutation_steps": mutation_results,
            "all_observed_mutation_steps_skipped": mutation_steps_skipped,
            "external_steps": external_step_results,
            "all_external_steps_skipped": external_steps_skipped,
            "candidate_ci_only_step": {
                "name": CANDIDATE_CI_STEP,
                "conclusion": "success",
            },
        },
        "gate_passed": gate_passed,
    }


def build_linux_ci_receipt(
    *,
    provenance_path: str | Path,
    candidate_commit: str,
    repository_root: str | Path,
) -> dict[str, Any]:
    root = Path(repository_root).expanduser().resolve(strict=True)
    provenance_identity, provenance_data = _read_identity(
        provenance_path, label="github_actions_provenance", require_private=True
    )
    provenance = _strict_json(
        provenance_data, label="GitHub Actions provenance source"
    )
    evidence = _derive_linux_ci_evidence(
        provenance,
        candidate_commit=candidate_commit,
        repository_root=root,
    )
    gate_passed = bool(evidence.pop("gate_passed"))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": LINUX_CI_KIND,
        "validator": LINUX_CI_VALIDATOR,
        "status": "passed" if gate_passed else "failed",
        "candidate_commit": _full_commit(
            candidate_commit, label="Linux CI candidate"
        ),
        "fetched_ts_ns": int(provenance.get("fetched_ts_ns") or 0),
        **evidence,
        "github_provenance": provenance_identity.to_dict(),
        "gate_passed": gate_passed,
        "limitations": [
            "github_api_snapshot_is_tls_sourced_but_not_a_detached_signature",
            "receipt_proves_ci_and_non_deployment_not_runtime_cutover_readiness",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def verify_linux_ci_receipt(
    receipt: Mapping[str, Any],
    *,
    repository_root: str | Path,
    expected_candidate_commit: str | None = None,
    require_passed: bool = True,
) -> dict[str, Any]:
    payload = dict(receipt)
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "status",
        "candidate_commit",
        "fetched_ts_ns",
        "repository_full_name",
        "workflow",
        "run",
        "ci_job",
        "deployment_safety",
        "github_provenance",
        "gate_passed",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("Linux CI receipt has unexpected or missing fields")
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != LINUX_CI_KIND
        or payload.get("validator") != LINUX_CI_VALIDATOR
        or payload.get("artifact_sha256") != _self_hash(payload)
    ):
        raise ValueError("Linux CI receipt identity or self-hash is invalid")
    candidate = _full_commit(payload.get("candidate_commit"), label="Linux CI candidate")
    if expected_candidate_commit is not None and candidate != _full_commit(
        expected_candidate_commit, label="expected Linux CI candidate"
    ):
        raise ValueError("Linux CI receipt names another candidate commit")
    provenance_identity = _identity_from_payload(
        payload.get("github_provenance"), label="github_actions_provenance"
    )
    observed_identity, provenance_data = _read_identity(
        provenance_identity.path,
        label="github_actions_provenance",
        require_private=True,
    )
    if observed_identity != provenance_identity:
        raise ValueError("GitHub Actions provenance changed after receipt creation")
    provenance = _strict_json(
        provenance_data, label="GitHub Actions provenance source"
    )
    evidence = _derive_linux_ci_evidence(
        provenance,
        candidate_commit=candidate,
        repository_root=Path(repository_root).expanduser().resolve(strict=True),
    )
    gate_passed = bool(evidence.pop("gate_passed"))
    for name, expected in evidence.items():
        if payload.get(name) != expected:
            raise ValueError(f"Linux CI {name} no longer matches GitHub provenance")
    if payload.get("fetched_ts_ns") != int(provenance.get("fetched_ts_ns") or 0):
        raise ValueError("Linux CI fetch timestamp differs from GitHub provenance")
    after_provenance, _ = _read_identity(
        provenance_identity.path,
        label="github_actions_provenance",
        require_private=True,
        include_data=False,
    )
    if after_provenance != observed_identity:
        raise RuntimeError("GitHub Actions provenance changed while it was validated")
    expected_status = "passed" if gate_passed else "failed"
    if payload.get("status") != expected_status or payload.get("gate_passed") is not gate_passed:
        raise ValueError("Linux CI status does not match source-backed results")
    if payload.get("limitations") != [
        "github_api_snapshot_is_tls_sourced_but_not_a_detached_signature",
        "receipt_proves_ci_and_non_deployment_not_runtime_cutover_readiness",
    ]:
        raise ValueError("Linux CI receipt limitations are invalid")
    if require_passed and not gate_passed:
        raise ValueError("Linux CI gate has not passed")
    return payload


def load_linux_ci_receipt(
    path: str | Path,
    *,
    repository_root: str | Path,
    expected_candidate_commit: str | None = None,
    require_passed: bool = True,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    identity, data = _read_identity(
        path,
        label="Linux CI receipt",
        require_private=True,
        snapshot=snapshot,
    )
    payload = verify_linux_ci_receipt(
        _strict_json(data, label="Linux CI receipt"),
        repository_root=repository_root,
        expected_candidate_commit=expected_candidate_commit,
        require_passed=require_passed,
    )
    after, _ = _read_identity(
        identity.path,
        label="Linux CI receipt",
        require_private=True,
        include_data=False,
    )
    if after != identity:
        raise RuntimeError("Linux CI receipt changed while it was validated")
    return payload


def write_linux_ci_receipt(
    path: str | Path,
    receipt: Mapping[str, Any],
    *,
    repository_root: str | Path,
) -> Path:
    payload = verify_linux_ci_receipt(
        receipt, repository_root=repository_root, require_passed=False
    )
    return _atomic_create(path, payload)


def capture_linux_ci_receipt(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    run_id: int,
    provenance_path: str | Path,
    output_path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Fetch GitHub REST sources and write an exact-candidate Linux-CI receipt."""

    root, candidate, origin_url = _candidate_checkout(
        repository_root, candidate_commit=candidate_commit
    )
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("GitHub Actions run id must be a positive integer")
    repository_full_name = _github_repository_slug(origin_url)
    provenance_output = _resolve_new_output(
        provenance_path, label="GitHub provenance output"
    )
    receipt_output = _resolve_new_output(output_path, label="Linux CI receipt output")
    _outside_repository(
        provenance_output, repository_root=root, label="GitHub provenance output"
    )
    _outside_repository(
        receipt_output, repository_root=root, label="Linux CI receipt output"
    )
    run_endpoint = f"repos/{repository_full_name}/actions/runs/{run_id}"
    run = _gh_api_json(run_endpoint, repository_root=root)
    jobs = _gh_api_json(
        f"{run_endpoint}/jobs?filter=all&per_page=100", repository_root=root
    )
    workflow = _gh_api_json(
        f"repos/{repository_full_name}/contents/{WORKFLOW_PATH}?ref={candidate}",
        repository_root=root,
    )
    provenance = _github_provenance_payload(
        repository_full_name=repository_full_name,
        candidate_commit=candidate,
        run_id=run_id,
        fetched_ts_ns=time.time_ns(),
        run=run,
        jobs=jobs,
        workflow_content=workflow,
    )
    _atomic_create(provenance_output, provenance)
    receipt = build_linux_ci_receipt(
        provenance_path=provenance_output,
        candidate_commit=candidate,
        repository_root=root,
    )
    output = write_linux_ci_receipt(
        receipt_output, receipt, repository_root=root
    )
    return output, receipt


def _git(repository_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot verify candidate Git state: git {' '.join(arguments)}") from exc
    return result.stdout.strip()


def _repository_binding(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    origin_main_commit: str,
    allow_promoted_main: bool = False,
) -> tuple[Path, str, str]:
    root = Path(repository_root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("repository_root must be an absolute non-symlink directory")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repository_root must be a directory")
    candidate = _full_commit(candidate_commit, label="candidate commit")
    base = _full_commit(origin_main_commit, label="origin/main commit")
    if _git(root, "rev-parse", "HEAD") != candidate:
        raise ValueError("candidate commit is not the repository HEAD")
    observed_main = _git(root, "rev-parse", "refs/remotes/origin/main")
    allowed_main = {base, candidate} if allow_promoted_main else {base}
    if observed_main not in allowed_main:
        raise ValueError(
            "origin/main is neither the declared baseline nor the exact candidate promotion"
        )
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("candidate checkout is not clean")
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, candidate],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("candidate is not a fast-forward descendant of origin/main") from exc
    return root, candidate, base


def _normalize_roots(
    roots: Mapping[str, Mapping[str, str | Path]], *, repository_root: Path
) -> tuple[dict[str, dict[str, str]], list[str]]:
    if set(roots) != set(ROOT_ENVIRONMENTS):
        raise ValueError("roots must contain exact demo and paper environments")
    normalized: dict[str, dict[str, str]] = {}
    relative: list[str] = []
    identities: set[tuple[int, int]] = set()
    resolved_paths: list[Path] = []
    for environment in ROOT_ENVIRONMENTS:
        raw = roots[environment]
        if set(raw) != set(ROOT_KINDS):
            raise ValueError(f"{environment} roots must contain exact account/inbox/capture keys")
        normalized[environment] = {}
        for kind in ROOT_KINDS:
            candidate = Path(raw[kind]).expanduser()
            if not candidate.is_absolute() or candidate.is_symlink():
                raise ValueError(f"{environment} {kind} root must be an absolute non-symlink path")
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"{environment} {kind} root must be a directory")
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
                raise ValueError(f"{environment} {kind} root must be verifier-owned mode 0700")
            try:
                rel = resolved.relative_to(repository_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"{environment} {kind} root must stay in the repository") from exc
            if not rel.startswith("data/"):
                raise ValueError(f"{environment} {kind} root must stay below repository data/")
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in identities:
                raise ValueError("fresh account roots must not alias each other")
            for prior in resolved_paths:
                if resolved in prior.parents or prior in resolved.parents:
                    raise ValueError("fresh account roots must be pairwise disjoint")
            identities.add(identity)
            resolved_paths.append(resolved)
            normalized[environment][kind] = str(resolved)
            relative.append(rel)
    return normalized, relative


def _reset_snapshot(
    *,
    candidate_commit: str,
    roots: Mapping[str, Mapping[str, str]],
    relative_roots: Sequence[str],
    archive_path: Path,
    sidecar_path: Path,
    reset_receipt_path: Path,
    identities: Mapping[str, _FileIdentity],
    data: Mapping[str, bytes],
    snapshots: Mapping[str, StableFileSnapshot],
) -> dict[str, Any]:
    from .account_reset_receipt import load_account_reset_receipt

    sidecar_text = data["reset_sha256_sidecar"].decode("ascii", errors="strict")
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\s]+)\n?", sidecar_text)
    if not match or match.group(2) != archive_path.name:
        raise ValueError("reset SHA-256 sidecar has the wrong format or archive name")
    archive_sha = identities["reset_archive"].sha256
    if match.group(1) != archive_sha:
        raise ValueError("reset archive does not match its SHA-256 sidecar")
    receipt = load_account_reset_receipt(
        reset_receipt_path,
        expected_candidate_commit=candidate_commit,
        expected_roots=roots,
        require_leave_stopped=True,
        require_fresh_roots=False,
        snapshot=snapshots["reset_receipt"],
        archive_snapshot=snapshots["reset_archive"],
        sidecar_snapshot=snapshots["reset_sha256_sidecar"],
    )
    receipt_reset = cast(Mapping[str, Any], receipt["reset"])
    receipt_archive = cast(Mapping[str, Any], receipt["archive"])
    receipt_services = cast(Mapping[str, Any], receipt["services"])
    archive_file = cast(Mapping[str, Any], receipt_archive["file"])
    receipt_sidecar = cast(Mapping[str, Any], receipt_archive["sha256_sidecar"])
    if (
        archive_file.get("path") != str(archive_path)
        or archive_file.get("sha256") != archive_sha
        or archive_file.get("size_bytes") != identities["reset_archive"].size_bytes
        or receipt_sidecar.get("path") != str(sidecar_path)
        or receipt_sidecar.get("sha256")
        != identities["reset_sha256_sidecar"].sha256
    ):
        raise ValueError("structured reset receipt names another archive bundle")
    receipt_relative = cast(
        Mapping[str, Mapping[str, str]],
        receipt_reset["account_epoch_relative_roots"],
    )
    flattened_relative = [
        receipt_relative[environment][kind]
        for environment in ROOT_ENVIRONMENTS
        for kind in ROOT_KINDS
    ]
    if flattened_relative != list(relative_roots):
        raise ValueError("structured reset receipt names another six-root epoch")
    sleeves = cast(Sequence[str], receipt_reset["sleeves"])
    if list(sleeves) != ["long", "continuous", "retire-shared-compat"]:
        raise ValueError("reset did not cover the exact LONG and CONTINUOUS all-sleeve scope")
    boundaries = cast(Mapping[str, str], receipt_reset["boundaries"])
    inactive = cast(Sequence[str], receipt_services["inactive_after"])
    embedded = cast(Mapping[str, Any], receipt_archive["embedded_manifest"])
    return {
        "receipt": _artifact_ref(identities["reset_receipt"], receipt),
        "started_ts_ns": int(receipt["started_ts_ns"]),
        "finished_ts_ns": int(receipt["finished_ts_ns"]),
        "archive": {
            "path": str(archive_path),
            "sha256": archive_sha,
            "size_bytes": identities["reset_archive"].size_bytes,
        },
        "sha256_sidecar": {
            "path": str(sidecar_path),
            "sha256": identities["reset_sha256_sidecar"].sha256,
        },
        "embedded_manifest_sha256": receipt_archive["embedded_manifest_sha256"],
        "ledger_reset_utc": embedded["ledger_reset_utc"],
        "git_head": candidate_commit,
        "sleeves": ["long", "continuous"],
        "legacy_shared_compat_retired": True,
        "leave_stopped": True,
        "fresh_roots_verified_at_reset": receipt_reset["fresh_roots_verified"],
        "account_epoch_roots": list(relative_roots),
        "demo_boundary": boundaries["demo"],
        "paper_boundary": boundaries["paper"],
        "inactive_units": list(inactive),
    }


def _aggregate_files(
    names: Sequence[str], identities: Mapping[str, _FileIdentity]
) -> dict[str, Any]:
    artifacts = {name: identities[name].to_dict() for name in names}
    return {
        "artifacts": artifacts,
        "sha256": hashlib.sha256(canonical_json(artifacts)).hexdigest(),
    }


def _assert_distinct_sources(identities: Mapping[str, _FileIdentity]) -> None:
    seen: dict[tuple[int, int], str] = {}
    for label, identity in identities.items():
        key = (identity.device, identity.inode)
        prior = seen.get(key)
        if prior is not None:
            raise ValueError(f"source artifacts alias each other: {prior} and {label}")
        seen[key] = label


def _environment_assignments(data: bytes, *, label: str) -> dict[str, str]:
    return parse_systemd_environment_bytes(data, label=label)


def _validate_runtime_sources(
    *,
    paths: Mapping[str, Path],
    data: Mapping[str, bytes],
    roots: Mapping[str, Mapping[str, str]],
    candidate_symbols: Sequence[str],
    seed_symbols: set[str],
) -> None:
    from .account_execution_config import load_risk_policy_bytes
    from .execution_calibration_driver import REGISTERED_CALIBRATION_SYMBOLS

    route_labels = {label for label in paths if label.startswith("route:")}
    risk_labels = {label for label in paths if label.startswith("risk:")}
    if route_labels != {"route:demo", "route:paper"}:
        raise ValueError("natural freeze requires exact demo and paper route artifacts")
    if risk_labels != {"risk:demo", "risk:paper"}:
        raise ValueError("natural freeze requires exact demo and paper risk policies")
    if seed_symbols != set(REGISTERED_CALIBRATION_SYMBOLS):
        raise ValueError("natural freeze seed must be the exact registered V7 symbol set")
    if not seed_symbols.issubset(set(candidate_symbols)):
        raise ValueError("natural freeze seed symbols are absent from the candidate universe")

    policies = {
        environment: load_risk_policy_bytes(data[f"risk:{environment}"])
        for environment in ("demo", "paper")
    }
    if policies["demo"] != policies["paper"]:
        raise ValueError("natural demo and paper owners must use the same risk policy")

    route_values = {
        environment: _environment_assignments(
            data[f"route:{environment}"],
            label=f"natural {environment} route environment",
        )
        for environment in ("demo", "paper")
    }
    common_expected = {
        "ACCOUNT_SYMBOLS_FILE": str(paths["candidate_universe"]),
        "ACCOUNT_DEMO_RULES_FILE": str(paths["demo_rules"]),
    }
    environment_expected = {
        "demo": {
            "ACCOUNT_EXECUTION_KERNEL_REQUIRED": "1",
            "ACCOUNT_EXECUTION_ROOT": roots["demo"]["account"],
            "ACCOUNT_INTENT_INBOX_ROOT": roots["demo"]["inbox"],
            "ACCOUNT_CAPTURE_ROOT": roots["demo"]["capture"],
            "ACCOUNT_RAW_MARKET_PERSISTENCE": "1",
            "ACCOUNT_RISK_POLICY_FILE": str(paths["risk:demo"]),
            **common_expected,
        },
        "paper": {
            "ACCOUNT_PAPER_KERNEL_REQUIRED": "1",
            "ACCOUNT_EXECUTION_ROOT": roots["paper"]["account"],
            "ACCOUNT_INTENT_INBOX_ROOT": roots["paper"]["inbox"],
            "ACCOUNT_PAPER_CAPTURE_ROOT": roots["paper"]["capture"],
            "ACCOUNT_RAW_MARKET_PERSISTENCE": "1",
            "ACCOUNT_RISK_POLICY_FILE": str(paths["risk:paper"]),
            "ACCOUNT_TWIN_CALIBRATION_FILE": str(paths["calibration"]),
            "ACCOUNT_TWIN_LATENCY_QUANTILE": "p50",
            "ACCOUNT_TWIN_SLIPPAGE_QUANTILE": "p50",
            **common_expected,
        },
    }
    for environment, expected in environment_expected.items():
        observed = route_values[environment]
        for key, value in expected.items():
            if observed.get(key) != value:
                raise ValueError(
                    f"natural {environment} route {key} does not match the frozen source"
                )
        try:
            require_registered_demo_rule_max_age_hours(
                observed.get("MAX_DEMO_RULE_AGE_HOURS", "168")
            )
        except ValueError as exc:
            raise ValueError(
                f"natural {environment} route weakens the registered rule freshness"
            ) from exc
        try:
            require_registered_request_market_warmup_timeout(
                observed.get("ACCOUNT_REQUEST_MARKET_WARMUP_TIMEOUT_SECONDS", "30")
            )
        except ValueError as exc:
            raise ValueError(
                f"natural {environment} route weakens the registered market warmup timeout"
            ) from exc
        real_money = observed.get("REAL_MONEY")
        if real_money is not None and real_money.strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
        }:
            raise ValueError(
                f"natural {environment} route REAL_MONEY must be unset or explicitly false"
            )
    try:
        disaster_stop_fraction = float(
            route_values["demo"].get("DISASTER_STOP_FRACTION", "")
        )
    except ValueError as exc:
        raise ValueError("natural demo route DISASTER_STOP_FRACTION is invalid") from exc
    if not math.isfinite(disaster_stop_fraction) or not 0.0 < disaster_stop_fraction < 1.0:
        raise ValueError(
            "natural demo route DISASTER_STOP_FRACTION must be strictly between zero and one"
        )
    try:
        paper_equity = float(route_values["paper"].get("PAPER_EQUITY_USDT", ""))
    except ValueError as exc:
        raise ValueError("natural paper route PAPER_EQUITY_USDT is invalid") from exc
    if not math.isfinite(paper_equity) or paper_equity <= 0.0:
        raise ValueError("natural paper route PAPER_EQUITY_USDT must be finite and positive")


def _semantic_artifacts(
    *,
    repository_root: Path,
    paths: Mapping[str, Path],
    data: Mapping[str, bytes],
    identities: Mapping[str, _FileIdentity],
    snapshots: Mapping[str, StableFileSnapshot],
    roots: Mapping[str, Mapping[str, str]],
    candidate_commit: str,
    now_ns: int,
) -> dict[str, dict[str, Any]]:
    from .account_candidate_universe import load_candidate_universe
    from .account_cutover_authority import load_reviewed_evidence
    from .account_execution_config import load_demo_rules
    from .candidate_rule_coverage import load_candidate_rule_coverage
    from .clock_offset_receipt import verify_clock_offset_receipt
    from .execution_twin_calibration import load_calibration_receipt
    from .execution_twin_drift import (
        BASELINE_CONFIG_ROLE,
        STRESS_CONFIG_ROLE,
        load_execution_twin_config_artifact,
        load_v7_archive_source_map,
    )
    from .market_capture import symbols_from_file

    local_suite = load_local_suite_receipt(
        paths["local_suite"],
        expected_candidate_commit=candidate_commit,
        snapshot=snapshots["local_suite"],
    )
    linux_ci = load_linux_ci_receipt(
        paths["linux_ci"],
        repository_root=repository_root,
        expected_candidate_commit=candidate_commit,
        snapshot=snapshots["linux_ci"],
    )
    repository_slug = _github_repository_slug(
        _git(repository_root, "remote", "get-url", "origin")
    )
    local_repository = cast(Mapping[str, Any], local_suite["repository"])
    if (
        _github_repository_slug(str(local_repository["origin_url"]))
        != repository_slug
        or linux_ci.get("repository_full_name") != repository_slug
    ):
        raise ValueError("local-suite/Linux-CI evidence belongs to another repository")
    if (
        int(local_suite["finished_ts_ns"]) > now_ns
        or int(linux_ci["fetched_ts_ns"]) > now_ns
    ):
        raise ValueError("local-suite/Linux-CI evidence postdates freeze creation")
    clock = _strict_json(data["clock"], label="clock-offset receipt")
    verify_clock_offset_receipt(
        clock,
        now_ns=now_ns,
        max_age_hours=MAX_CLOCK_AGE_HOURS,
        require_registered_contract=True,
    )
    candidate = load_candidate_universe(
        paths["candidate_universe"],
        snapshot=snapshots["candidate_universe"],
    )
    _validate_runtime_sources(
        paths=paths,
        data=data,
        roots=roots,
        candidate_symbols=candidate.symbols,
        seed_symbols=symbols_from_file(
            paths["seed"],
            snapshot=snapshots["seed"],
        ),
    )
    demo_rules = _strict_json(data["demo_rules"], label="demo-rule receipt")
    load_demo_rules(
        paths["demo_rules"],
        now_ns=now_ns,
        max_age_seconds=7 * 24 * 60 * 60,
        snapshot=snapshots["demo_rules"],
    )
    coverage = load_candidate_rule_coverage(
        paths["rule_coverage"],
        validation_now_ns=now_ns,
        max_rule_age_seconds=7 * 24 * 60 * 60,
        snapshot=snapshots["rule_coverage"],
        candidate_snapshot=snapshots["candidate_universe"],
        demo_rules_snapshot=snapshots["demo_rules"],
    )
    calibration = load_calibration_receipt(
        paths["calibration"],
        require_registered_requirements=True,
        snapshot=snapshots["calibration"],
    )
    if calibration.get("execution_twin_gate_passed") is not True:
        raise ValueError("V7 calibration gate has not passed")
    archive_map = load_v7_archive_source_map(
        paths["archive_map"],
        calibration_receipt=calibration,
        snapshot=snapshots["archive_map"],
    )
    baseline = load_execution_twin_config_artifact(
        paths["baseline_config"],
        calibration_receipt=calibration,
        expected_role=BASELINE_CONFIG_ROLE,
        snapshot=snapshots["baseline_config"],
    )
    stress = load_execution_twin_config_artifact(
        paths["stress_config"],
        calibration_receipt=calibration,
        expected_role=STRESS_CONFIG_ROLE,
        snapshot=snapshots["stress_config"],
    )
    paper = load_reviewed_evidence(
        paths["paper_owner_first"],
        expected_role=PAPER_OWNER_ROLE,
        snapshot=snapshots["paper_owner_first"],
    )
    demo = load_reviewed_evidence(
        paths["demo_owner_first"],
        expected_role=DEMO_OWNER_ROLE,
        snapshot=snapshots["demo_owner_first"],
    )
    if paper.get("claim") != PAPER_OWNER_CLAIM or demo.get("claim") != DEMO_OWNER_CLAIM:
        raise ValueError("owner-first receipts do not carry the exact registered claims")
    if int(paper.get("reviewed_ts_ns") or 0) > int(demo.get("reviewed_ts_ns") or 0):
        raise ValueError("paper owner-first review must precede demo owner-first review")
    candidate_payload = _strict_json(
        data["candidate_universe"], label="candidate-universe artifact"
    )
    if candidate_payload.get("artifact_sha256") != candidate.artifact_sha256:
        raise ValueError("candidate-universe loader and source disagree")
    paper_ref = _artifact_ref(identities["paper_owner_first"], paper)
    paper_ref["reviewed_ts_ns"] = int(paper["reviewed_ts_ns"])
    demo_ref = _artifact_ref(identities["demo_owner_first"], demo)
    demo_ref["reviewed_ts_ns"] = int(demo["reviewed_ts_ns"])
    local_suite_ref = _artifact_ref(identities["local_suite"], local_suite)
    local_suite_ref["finished_ts_ns"] = int(local_suite["finished_ts_ns"])
    linux_ci_ref = _artifact_ref(identities["linux_ci"], linux_ci)
    linux_ci_ref.update(
        {
            "fetched_ts_ns": int(linux_ci["fetched_ts_ns"]),
            "run_id": int(cast(Mapping[str, Any], linux_ci["run"])["id"]),
            "run_url": str(cast(Mapping[str, Any], linux_ci["run"])["url"]),
        }
    )
    clock_ref = _artifact_ref(identities["clock"], clock)
    clock_ref["observed_ts_ns"] = int(clock["observed_ts_ns"])
    return {
        "local_suite": local_suite_ref,
        "linux_ci": linux_ci_ref,
        "clock": clock_ref,
        "candidate_universe": _artifact_ref(
            identities["candidate_universe"], candidate_payload
        ),
        "demo_rules": _artifact_ref(identities["demo_rules"], demo_rules),
        "rule_coverage": _artifact_ref(identities["rule_coverage"], coverage),
        "calibration": _artifact_ref(identities["calibration"], calibration),
        "archive_map": _artifact_ref(identities["archive_map"], archive_map),
        "baseline_config": _artifact_ref(identities["baseline_config"], baseline),
        "stress_config": _artifact_ref(identities["stress_config"], stress),
        "paper": paper_ref,
        "demo": demo_ref,
    }


def _source_paths(
    *,
    local_suite_path: str | Path,
    linux_ci_path: str | Path,
    clock_offset_path: str | Path,
    candidate_universe_path: str | Path,
    demo_rules_path: str | Path,
    rule_coverage_path: str | Path,
    calibration_path: str | Path,
    archive_map_path: str | Path,
    baseline_config_path: str | Path,
    stress_config_path: str | Path,
    reset_archive_path: str | Path,
    reset_sha256_path: str | Path,
    reset_receipt_path: str | Path,
    paper_owner_first_path: str | Path,
    demo_owner_first_path: str | Path,
    route_paths: Mapping[str, str | Path],
    risk_policy_paths: Mapping[str, str | Path],
    seed_path: str | Path,
) -> dict[str, Path]:
    if set(route_paths) != set(ROOT_ENVIRONMENTS):
        raise ValueError("route_paths must contain exact demo and paper entries")
    if set(risk_policy_paths) != set(ROOT_ENVIRONMENTS):
        raise ValueError("risk_policy_paths must contain exact demo and paper entries")
    paths = {
        "local_suite": Path(local_suite_path),
        "linux_ci": Path(linux_ci_path),
        "clock": Path(clock_offset_path),
        "candidate_universe": Path(candidate_universe_path),
        "demo_rules": Path(demo_rules_path),
        "rule_coverage": Path(rule_coverage_path),
        "calibration": Path(calibration_path),
        "archive_map": Path(archive_map_path),
        "baseline_config": Path(baseline_config_path),
        "stress_config": Path(stress_config_path),
        "reset_archive": Path(reset_archive_path),
        "reset_sha256_sidecar": Path(reset_sha256_path),
        "reset_receipt": Path(reset_receipt_path),
        "paper_owner_first": Path(paper_owner_first_path),
        "demo_owner_first": Path(demo_owner_first_path),
        "seed": Path(seed_path),
    }
    for name, path in route_paths.items():
        if not str(name).strip() or name in paths:
            raise ValueError(f"invalid or duplicate route artifact label {name!r}")
        paths[f"route:{name}"] = Path(path)
    for name, path in risk_policy_paths.items():
        if not str(name).strip() or f"risk:{name}" in paths:
            raise ValueError(f"invalid or colliding risk artifact label {name!r}")
        paths[f"risk:{name}"] = Path(path)
    return paths


def build_natural_cutover_freeze_manifest(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    origin_main_commit: str,
    t0_ns: int,
    t1_ns: int,
    account_ids: Mapping[str, str],
    roots: Mapping[str, Mapping[str, str | Path]],
    local_suite_path: str | Path,
    linux_ci_path: str | Path,
    clock_offset_path: str | Path,
    candidate_universe_path: str | Path,
    demo_rules_path: str | Path,
    rule_coverage_path: str | Path,
    calibration_path: str | Path,
    archive_map_path: str | Path,
    baseline_config_path: str | Path,
    stress_config_path: str | Path,
    reset_archive_path: str | Path,
    reset_sha256_path: str | Path,
    reset_receipt_path: str | Path,
    paper_owner_first_path: str | Path,
    demo_owner_first_path: str | Path,
    route_paths: Mapping[str, str | Path],
    risk_policy_paths: Mapping[str, str | Path],
    seed_path: str | Path,
    created_ts_ns: int | None = None,
    validation_now_ns: int | None = None,
) -> dict[str, Any]:
    """Build and fully validate the immutable pre-window freeze manifest."""

    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    now = created if validation_now_ns is None else int(validation_now_ns)
    if created <= 0 or now <= 0 or created > now:
        raise ValueError("freeze creation/validation timestamps are invalid")
    if type(t0_ns) is not int or type(t1_ns) is not int:
        raise ValueError("T0/T1 must be integer nanosecond timestamps")
    if t0_ns % HOUR_NS or t1_ns - t0_ns != WINDOW_NS or t0_ns <= now:
        raise ValueError("natural window must be a future UTC-hour T0 and exact 120-hour T1")
    repository, candidate, base = _repository_binding(
        repository_root=repository_root,
        candidate_commit=candidate_commit,
        origin_main_commit=origin_main_commit,
    )
    if dict(account_ids) != EXPECTED_ACCOUNT_IDS:
        raise ValueError("account_ids must be the exact registered demo and paper identities")
    normalized_roots, relative_roots = _normalize_roots(roots, repository_root=repository)
    raw_paths = _source_paths(
        local_suite_path=local_suite_path,
        linux_ci_path=linux_ci_path,
        clock_offset_path=clock_offset_path,
        candidate_universe_path=candidate_universe_path,
        demo_rules_path=demo_rules_path,
        rule_coverage_path=rule_coverage_path,
        calibration_path=calibration_path,
        archive_map_path=archive_map_path,
        baseline_config_path=baseline_config_path,
        stress_config_path=stress_config_path,
        reset_archive_path=reset_archive_path,
        reset_sha256_path=reset_sha256_path,
        reset_receipt_path=reset_receipt_path,
        paper_owner_first_path=paper_owner_first_path,
        demo_owner_first_path=demo_owner_first_path,
        route_paths=route_paths,
        risk_policy_paths=risk_policy_paths,
        seed_path=seed_path,
    )
    paths: dict[str, Path] = {}
    identities: dict[str, _FileIdentity] = {}
    source_data: dict[str, bytes] = {}
    source_snapshots: dict[str, StableFileSnapshot] = {}
    for label, raw in raw_paths.items():
        source_snapshot = read_stable_file(
            raw,
            label=label,
            require_mode=0o600,
            require_owner=True,
            require_single_link=False,
        )
        identity, data = _read_identity(
            raw,
            label=label,
            require_private=True,
            include_data=label != "reset_archive",
            snapshot=source_snapshot,
        )
        paths[label] = Path(identity.path)
        identities[label] = identity
        source_data[label] = data
        source_snapshots[label] = source_snapshot
    _assert_distinct_sources(identities)
    semantics = _semantic_artifacts(
        repository_root=repository,
        paths=paths,
        data=source_data,
        identities=identities,
        snapshots=source_snapshots,
        roots=normalized_roots,
        candidate_commit=candidate,
        now_ns=now,
    )
    initial_clock_to_t0_ns = t0_ns - int(semantics["clock"]["observed_ts_ns"])
    if not (
        0 < initial_clock_to_t0_ns
        <= MAX_INITIAL_CLOCK_DISTANCE_TO_T0_HOURS * HOUR_NS
    ):
        raise ValueError(
            "initial clock-offset receipt must be observed at or before freeze "
            "and no more than six hours before T0"
        )
    reset = _reset_snapshot(
        candidate_commit=candidate,
        roots=normalized_roots,
        relative_roots=relative_roots,
        archive_path=paths["reset_archive"],
        sidecar_path=paths["reset_sha256_sidecar"],
        reset_receipt_path=paths["reset_receipt"],
        identities=identities,
        data=source_data,
        snapshots=source_snapshots,
    )
    paper_reviewed = int(semantics["paper"]["reviewed_ts_ns"])
    demo_reviewed = int(semantics["demo"]["reviewed_ts_ns"])
    if not (
        int(reset["finished_ts_ns"]) <= paper_reviewed <= demo_reviewed <= created
    ):
        raise ValueError(
            "reset, paper-owner review, demo-owner review, and freeze creation are out of order"
        )
    after: dict[str, _FileIdentity] = {}
    for label, path in paths.items():
        identity, _ = _read_identity(
            path, label=label, require_private=True, include_data=False
        )
        after[label] = identity
    if after != identities:
        raise RuntimeError("a freeze source changed during semantic validation")

    route_names = sorted(label for label in paths if label.startswith("route:"))
    risk_names = sorted(label for label in paths if label.startswith("risk:"))
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "created_ts_ns": created,
        "freeze_id": "",
        "repository": {
            "root": str(repository),
            "candidate_commit": candidate,
            "origin_main_commit": base,
            "checkout_clean": True,
            "fast_forward_only": True,
            "local_suite": semantics["local_suite"],
            "linux_ci": semantics["linux_ci"],
        },
        "window": {
            "t0_ns": t0_ns,
            "t1_ns": t1_ns,
            "duration_hours": WINDOW_HOURS,
            "interval": "half_open_[t0,t1)",
        },
        "runtime": {
            "account_ids": dict(EXPECTED_ACCOUNT_IDS),
            "roots": normalized_roots,
            "routes": _aggregate_files(route_names, identities),
            "risk_policy": _aggregate_files(risk_names, identities),
            "seed": {
                "path": identities["seed"].path,
                "sha256": identities["seed"].sha256,
                "size_bytes": identities["seed"].size_bytes,
            },
        },
        "v7_training": {
            "calibration": semantics["calibration"],
            "archive_map": semantics["archive_map"],
            "baseline_config": semantics["baseline_config"],
            "stress_config": semantics["stress_config"],
            "counts_toward_natural_holdout": False,
        },
        "clock": {
            "receipt": semantics["clock"],
            "fresh_at_freeze": True,
            "max_age_hours": MAX_CLOCK_AGE_HOURS,
            "initial_to_t0_ns": initial_clock_to_t0_ns,
            "max_initial_to_t0_hours": MAX_INITIAL_CLOCK_DISTANCE_TO_T0_HOURS,
        },
        "population": {
            "candidate_universe": semantics["candidate_universe"],
            "demo_rules": semantics["demo_rules"],
            "rule_coverage": semantics["rule_coverage"],
        },
        "reset": reset,
        "owner_first": {
            "paper": semantics["paper"],
            "demo": semantics["demo"],
            "order": ["paper", "demo"],
            "producer_start_authority": "not_granted_by_review_receipts",
        },
        "gates": {
            "pre_window_freeze_passed": True,
            "execution_authorization": "not_granted",
        },
        "source_files": {label: identity.to_dict() for label, identity in sorted(identities.items())},
        "limitations": LIMITATIONS,
        "artifact_sha256": "",
    }
    payload["freeze_id"] = _freeze_id(payload)
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _atomic_create(path: str | Path, payload: Mapping[str, Any]) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("freeze output must be an absolute non-symlink path")
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("freeze output parent must be a non-symlink directory")
    resolved = parent / output.name
    data = canonical_json(dict(payload)) + b"\n"
    descriptor = os.open(str(resolved), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("freeze write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        resolved.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    directory_fd = os.open(str(parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return resolved


def write_natural_cutover_freeze_manifest(
    path: str | Path, payload: Mapping[str, Any]
) -> Path:
    value = dict(payload)
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != KIND
        or value.get("validator") != VALIDATOR
        or value.get("freeze_id") != _freeze_id(value)
        or value.get("artifact_sha256") != _self_hash(value)
    ):
        raise ValueError("natural cutover freeze manifest is not a valid self-hashed v1 artifact")
    return _atomic_create(path, value)


def _paths_from_payload(payload: Mapping[str, Any]) -> dict[str, Path]:
    sources = payload.get("source_files")
    if not isinstance(sources, Mapping) or not sources:
        raise ValueError("freeze manifest lacks source identities")
    paths: dict[str, Path] = {}
    for label, raw in sources.items():
        if not isinstance(label, str) or not isinstance(raw, Mapping):
            raise ValueError("freeze source identities are malformed")
        expected_fields = {
            "label",
            "path",
            "size_bytes",
            "sha256",
            "device",
            "inode",
            "mtime_ns",
            "mode",
            "uid",
        }
        if set(raw) != expected_fields or raw.get("label") != label:
            raise ValueError(f"freeze source identity {label!r} has invalid fields")
        paths[label] = Path(str(raw.get("path") or ""))
    labels = set(paths)
    route_labels = {label for label in labels if label.startswith("route:")}
    risk_labels = {label for label in labels if label.startswith("risk:")}
    if not _FIXED_SOURCE_LABELS <= labels or not route_labels or not risk_labels:
        raise ValueError("freeze manifest lacks required route/risk/fixed source artifacts")
    if labels != _FIXED_SOURCE_LABELS | route_labels | risk_labels:
        raise ValueError("freeze manifest contains an unregistered source artifact label")
    return paths


def load_natural_cutover_freeze_manifest(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Load, source-reopen, and revalidate a natural cutover freeze."""

    receipt_identity, data = _read_identity(
        path,
        label="natural cutover freeze manifest",
        require_private=True,
        snapshot=snapshot,
    )
    payload = _strict_json(data, label="natural cutover freeze manifest")
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "freeze_id",
        "repository",
        "window",
        "runtime",
        "v7_training",
        "clock",
        "population",
        "reset",
        "owner_first",
        "gates",
        "source_files",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("natural cutover freeze manifest has unexpected or missing fields")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        raise ValueError("unsupported natural cutover freeze manifest identity")
    if payload.get("validator") != VALIDATOR:
        raise ValueError("natural cutover freeze manifest validator is unsupported")
    if payload.get("freeze_id") != _freeze_id(payload) or payload.get("artifact_sha256") != _self_hash(payload):
        raise ValueError("natural cutover freeze manifest self-hash is invalid")
    gates = payload.get("gates")
    if not isinstance(gates, Mapping) or dict(gates) != {
        "pre_window_freeze_passed": True,
        "execution_authorization": "not_granted",
    }:
        raise ValueError("natural cutover freeze cannot grant execution authority")
    repository = cast(Mapping[str, Any], payload.get("repository"))
    window = cast(Mapping[str, Any], payload.get("window"))
    runtime = cast(Mapping[str, Any], payload.get("runtime"))
    candidate = _full_commit(repository.get("candidate_commit"), label="freeze candidate commit")
    base = _full_commit(repository.get("origin_main_commit"), label="freeze origin/main commit")
    root, _, _ = _repository_binding(
        repository_root=str(repository.get("root") or ""),
        candidate_commit=candidate,
        origin_main_commit=base,
        allow_promoted_main=True,
    )
    if set(repository) != {
        "root",
        "candidate_commit",
        "origin_main_commit",
        "checkout_clean",
        "fast_forward_only",
        "local_suite",
        "linux_ci",
    } or repository.get("checkout_clean") is not True or repository.get(
        "fast_forward_only"
    ) is not True:
        raise ValueError("natural cutover freeze repository contract is invalid")
    t0_ns = int(window.get("t0_ns") or 0)
    t1_ns = int(window.get("t1_ns") or 0)
    created = int(payload.get("created_ts_ns") or 0)
    if (
        t0_ns % HOUR_NS
        or t1_ns - t0_ns != WINDOW_NS
        or created <= 0
        or created >= t0_ns
        or window.get("duration_hours") != WINDOW_HOURS
        or window.get("interval") != "half_open_[t0,t1)"
    ):
        raise ValueError("natural cutover freeze window is invalid")
    account_ids = runtime.get("account_ids")
    roots = runtime.get("roots")
    if not isinstance(account_ids, Mapping) or dict(account_ids) != EXPECTED_ACCOUNT_IDS:
        raise ValueError("natural cutover freeze account identities are invalid")
    if not isinstance(roots, Mapping):
        raise ValueError("natural cutover freeze roots are invalid")
    normalized_roots, relative_roots = _normalize_roots(
        cast(Mapping[str, Mapping[str, str | Path]], roots), repository_root=root
    )
    if normalized_roots != dict(roots):
        raise ValueError("natural cutover freeze roots are not canonical")

    paths = _paths_from_payload(payload)
    identities: dict[str, _FileIdentity] = {}
    source_data: dict[str, bytes] = {}
    source_snapshots: dict[str, StableFileSnapshot] = {}
    for label, source_path in paths.items():
        source_snapshot = read_stable_file(
            source_path,
            label=label,
            require_mode=0o600,
            require_owner=True,
            require_single_link=False,
        )
        identity, source_bytes = _read_identity(
            source_path,
            label=label,
            require_private=True,
            include_data=label != "reset_archive",
            snapshot=source_snapshot,
        )
        expected = cast(Mapping[str, Any], cast(Mapping[str, Any], payload["source_files"])[label])
        if identity.to_dict() != dict(expected):
            raise ValueError(f"freeze source {label!r} changed after creation")
        identities[label] = identity
        source_data[label] = source_bytes
        source_snapshots[label] = source_snapshot
    _assert_distinct_sources(identities)
    semantics = _semantic_artifacts(
        repository_root=root,
        paths=paths,
        data=source_data,
        identities=identities,
        snapshots=source_snapshots,
        roots=normalized_roots,
        candidate_commit=candidate,
        now_ns=created,
    )
    initial_clock_to_t0_ns = t0_ns - int(semantics["clock"]["observed_ts_ns"])
    if not (
        0 < initial_clock_to_t0_ns
        <= MAX_INITIAL_CLOCK_DISTANCE_TO_T0_HOURS * HOUR_NS
    ):
        raise ValueError(
            "initial clock-offset receipt no longer satisfies the T0 endpoint contract"
        )
    expected_reset = _reset_snapshot(
        candidate_commit=candidate,
        roots=normalized_roots,
        relative_roots=relative_roots,
        archive_path=paths["reset_archive"],
        sidecar_path=paths["reset_sha256_sidecar"],
        reset_receipt_path=paths["reset_receipt"],
        identities=identities,
        data=source_data,
        snapshots=source_snapshots,
    )
    if payload.get("reset") != expected_reset:
        raise ValueError("reset sources no longer reproduce the frozen reset boundary")
    if not (
        int(expected_reset["finished_ts_ns"])
        <= int(semantics["paper"]["reviewed_ts_ns"])
        <= int(semantics["demo"]["reviewed_ts_ns"])
        <= created
    ):
        raise ValueError(
            "reset, paper-owner review, demo-owner review, and freeze creation are out of order"
        )
    repository_expected = cast(Mapping[str, Any], payload["repository"])
    if repository_expected.get("local_suite") != semantics["local_suite"] or repository_expected.get("linux_ci") != semantics["linux_ci"]:
        raise ValueError("repository validation receipts no longer match the freeze")
    training = cast(Mapping[str, Any], payload["v7_training"])
    for name in ("calibration", "archive_map", "baseline_config", "stress_config"):
        if training.get(name) != semantics[name]:
            raise ValueError(f"V7 {name} no longer matches the freeze")
    population = cast(Mapping[str, Any], payload["population"])
    for name in ("candidate_universe", "demo_rules", "rule_coverage"):
        if population.get(name) != semantics[name]:
            raise ValueError(f"population {name} no longer matches the freeze")
    owner = cast(Mapping[str, Any], payload["owner_first"])
    if owner.get("paper") != semantics["paper"] or owner.get("demo") != semantics["demo"]:
        raise ValueError("owner-first evidence no longer matches the freeze")
    clock = cast(Mapping[str, Any], payload["clock"])
    if clock.get("receipt") != semantics["clock"]:
        raise ValueError("clock receipt no longer matches the freeze")
    after_identity, _ = _read_identity(
        Path(receipt_identity.path),
        label="natural cutover freeze manifest",
        require_private=True,
        include_data=False,
    )
    if after_identity != receipt_identity:
        raise RuntimeError("natural cutover freeze manifest changed while loading")
    route_names = sorted(label for label in paths if label.startswith("route:"))
    risk_names = sorted(label for label in paths if label.startswith("risk:"))
    if set(runtime) != {"account_ids", "roots", "routes", "risk_policy", "seed"}:
        raise ValueError("natural cutover freeze runtime fields are invalid")
    if runtime.get("routes") != _aggregate_files(route_names, identities):
        raise ValueError("route artifacts no longer reproduce the frozen runtime binding")
    if runtime.get("risk_policy") != _aggregate_files(risk_names, identities):
        raise ValueError("risk artifacts no longer reproduce the frozen runtime binding")
    expected_seed = {
        "path": identities["seed"].path,
        "sha256": identities["seed"].sha256,
        "size_bytes": identities["seed"].size_bytes,
    }
    if runtime.get("seed") != expected_seed:
        raise ValueError("seed artifact no longer reproduces the frozen runtime binding")
    if training.get("counts_toward_natural_holdout") is not False:
        raise ValueError("V7 training cannot count toward the natural holdout")
    if (
        clock.get("fresh_at_freeze") is not True
        or clock.get("max_age_hours") != MAX_CLOCK_AGE_HOURS
        or clock.get("initial_to_t0_ns") != initial_clock_to_t0_ns
        or clock.get("max_initial_to_t0_hours")
        != MAX_INITIAL_CLOCK_DISTANCE_TO_T0_HOURS
    ):
        raise ValueError("clock freeze contract is invalid")
    if owner.get("order") != ["paper", "demo"] or owner.get(
        "producer_start_authority"
    ) != "not_granted_by_review_receipts":
        raise ValueError("owner-first ordering contract is invalid")
    if payload.get("limitations") != LIMITATIONS:
        raise ValueError("natural cutover freeze limitations are invalid")
    return payload


def _named_paths(values: Sequence[str], *, label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw in values:
        name, separator, path = raw.partition("=")
        if (
            separator != "="
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or not path
            or name in result
        ):
            raise ValueError(f"{label} must use unique NAME=/absolute/path values")
        result[name] = Path(path)
    if not result:
        raise ValueError(f"at least one {label} is required")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Produce source-backed cutover-suite/CI evidence and the immutable "
            "demo|paper natural-window freeze."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    local = subparsers.add_parser(
        "local-suite",
        help="run canonical local Ruff and full pytest commands into a private receipt",
    )
    local.add_argument("--repository-root", type=Path, required=True)
    local.add_argument("--candidate-commit", required=True)
    local.add_argument("--python", default=sys.executable)
    local.add_argument("--log", type=Path, required=True)
    local.add_argument("--output", type=Path, required=True)

    linux = subparsers.add_parser(
        "linux-ci",
        help="fetch and bind an exact-candidate GitHub Actions CI run",
    )
    linux.add_argument("--repository-root", type=Path, required=True)
    linux.add_argument("--candidate-commit", required=True)
    linux.add_argument("--run-id", type=int, required=True)
    linux.add_argument("--provenance", type=Path, required=True)
    linux.add_argument("--output", type=Path, required=True)

    create = subparsers.add_parser(
        "create",
        help="validate every pre-window source and create the immutable freeze",
    )
    create.add_argument("--repository-root", type=Path, required=True)
    create.add_argument("--candidate-commit", required=True)
    create.add_argument("--origin-main-commit", required=True)
    create.add_argument("--t0-ns", type=int, required=True)
    create.add_argument("--t1-ns", type=int, required=True)
    for environment in ROOT_ENVIRONMENTS:
        for kind in ROOT_KINDS:
            create.add_argument(
                f"--{environment}-{kind}-root", type=Path, required=True
            )
    create.add_argument("--local-suite", type=Path, required=True)
    create.add_argument("--linux-ci", type=Path, required=True)
    create.add_argument("--clock-offset", type=Path, required=True)
    create.add_argument("--candidate-universe", type=Path, required=True)
    create.add_argument("--demo-rules", type=Path, required=True)
    create.add_argument("--rule-coverage", type=Path, required=True)
    create.add_argument("--calibration", type=Path, required=True)
    create.add_argument("--archive-map", type=Path, required=True)
    create.add_argument("--baseline-config", type=Path, required=True)
    create.add_argument("--stress-config", type=Path, required=True)
    create.add_argument("--reset-archive", type=Path, required=True)
    create.add_argument("--reset-sha256", type=Path, required=True)
    create.add_argument("--reset-receipt", type=Path, required=True)
    create.add_argument("--paper-owner-first", type=Path, required=True)
    create.add_argument("--demo-owner-first", type=Path, required=True)
    create.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="NAME=/ABSOLUTE/PATH",
        help="effective route/config artifact; repeat for every registered route",
    )
    create.add_argument(
        "--risk-policy",
        action="append",
        default=[],
        metavar="NAME=/ABSOLUTE/PATH",
        help="effective risk-policy artifact; repeat for every registered policy",
    )
    create.add_argument("--seed", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser(
        "verify", help="reopen every source and verify an existing freeze"
    )
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def _print_summary(*, path: Path, payload: Mapping[str, Any]) -> None:
    summary = {
        "path": str(path),
        "kind": payload.get("kind"),
        "status": payload.get("status"),
        "candidate_commit": payload.get("candidate_commit"),
        "freeze_id": payload.get("freeze_id"),
        "artifact_sha256": payload.get("artifact_sha256"),
    }
    print(json.dumps({key: value for key, value in summary.items() if value is not None}, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "local-suite":
        output, payload = run_local_suite(
            repository_root=args.repository_root,
            candidate_commit=args.candidate_commit,
            python_executable=args.python,
            log_path=args.log,
            output_path=args.output,
        )
        _print_summary(path=output, payload=payload)
        return 0 if payload["gate_passed"] is True else 1
    if args.command == "linux-ci":
        output, payload = capture_linux_ci_receipt(
            repository_root=args.repository_root,
            candidate_commit=args.candidate_commit,
            run_id=args.run_id,
            provenance_path=args.provenance,
            output_path=args.output,
        )
        _print_summary(path=output, payload=payload)
        return 0 if payload["gate_passed"] is True else 1
    if args.command == "verify":
        manifest = load_natural_cutover_freeze_manifest(args.manifest)
        _print_summary(path=args.manifest.resolve(strict=True), payload=manifest)
        return 0
    if args.command != "create":
        raise AssertionError(f"unhandled command: {args.command}")

    repository = args.repository_root.expanduser().resolve(strict=True)
    output = _resolve_new_output(args.output, label="freeze output")
    _outside_repository(output, repository_root=repository, label="freeze output")
    roots = {
        environment: {
            kind: getattr(args, f"{environment}_{kind}_root")
            for kind in ROOT_KINDS
        }
        for environment in ROOT_ENVIRONMENTS
    }
    payload = build_natural_cutover_freeze_manifest(
        repository_root=repository,
        candidate_commit=args.candidate_commit,
        origin_main_commit=args.origin_main_commit,
        t0_ns=args.t0_ns,
        t1_ns=args.t1_ns,
        account_ids=EXPECTED_ACCOUNT_IDS,
        roots=roots,
        local_suite_path=args.local_suite,
        linux_ci_path=args.linux_ci,
        clock_offset_path=args.clock_offset,
        candidate_universe_path=args.candidate_universe,
        demo_rules_path=args.demo_rules,
        rule_coverage_path=args.rule_coverage,
        calibration_path=args.calibration,
        archive_map_path=args.archive_map,
        baseline_config_path=args.baseline_config,
        stress_config_path=args.stress_config,
        reset_archive_path=args.reset_archive,
        reset_sha256_path=args.reset_sha256,
        reset_receipt_path=args.reset_receipt,
        paper_owner_first_path=args.paper_owner_first,
        demo_owner_first_path=args.demo_owner_first,
        route_paths=_named_paths(args.route, label="--route"),
        risk_policy_paths=_named_paths(args.risk_policy, label="--risk-policy"),
        seed_path=args.seed,
    )
    written = write_natural_cutover_freeze_manifest(output, payload)
    loaded = load_natural_cutover_freeze_manifest(written)
    _print_summary(path=written, payload=loaded)
    return 0


__all__ = [
    "build_linux_ci_receipt",
    "build_natural_cutover_freeze_manifest",
    "capture_linux_ci_receipt",
    "load_linux_ci_receipt",
    "load_local_suite_receipt",
    "load_natural_cutover_freeze_manifest",
    "run_local_suite",
    "verify_linux_ci_receipt",
    "verify_local_suite_receipt",
    "write_linux_ci_receipt",
    "write_local_suite_receipt",
    "write_natural_cutover_freeze_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
