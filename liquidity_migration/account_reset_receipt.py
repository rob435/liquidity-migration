"""Source-reopening receipt for the demo/paper account epoch reset.

The shell reset remains the operational transaction because it owns systemd,
the demo-account lease, and the archive/remove/rebuild sequence.  This module
turns only a fully completed ``--execute --leave-stopped`` run into a durable
machine-checkable artifact.  Failed or interrupted resets never receive a
``passed`` receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json


SCHEMA_VERSION = 1
KIND = "demo_paper_account_epoch_reset"
VALIDATOR = "account_reset_receipt_v1"
ROOT_ENVIRONMENTS = ("demo", "paper")
ROOT_KINDS = ("account", "inbox", "capture")
MANAGED_UNITS = (
    "liquidity-migration-demo-liveness.timer",
    "liquidity-migration-continuous-hedge.timer",
    "liquidity-migration-continuous-rmom-refresh.timer",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-continuous-hedge.service",
    "liquidity-migration-continuous-rmom-refresh.service",
    "liquidity-migration-demo-liveness.service",
    "liquidity-migration-account-execution.service",
    "liquidity-migration-account-paper-execution.service",
)
DEMO_BOUNDARY = "venue_verified_flat_positions_0_open_orders_0"
PAPER_BOUNDARY = "archived_deterministic_epoch_not_carried_forward"
LIMITATIONS = (
    "systemd_inactivity_is_a_local_point_in_time_check_not_a_remote_attestation",
    "posix_mode_restrictions_are_not_worm_storage_or_a_signature",
    "receipt_establishes_an_epoch_boundary_not_execution_or_deploy_authority",
)
_IDENTITY_FIELDS = {
    "path",
    "size_bytes",
    "sha256",
    "device",
    "inode",
    "mtime_ns",
    "mode",
    "uid",
}
_SINGLETON_MANIFEST_KEYS = (
    "ledger_reset_utc",
    "git_head",
    "sleeves",
    "include_reports",
    "include_caches",
    "leave_stopped",
    "env_file",
    "account_env_file",
    "paper_account_env_file",
    "demo_account_lease_path",
    "demo_boundary",
    "paper_boundary",
    "active_before",
)
_REPEATED_MANIFEST_KEYS = (
    "account_epoch_target",
    "target",
    "preserved_risk_state",
)


@dataclass(frozen=True, slots=True)
class FileIdentity:
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
    return hashlib.sha256(canonical_json({**dict(payload), "artifact_sha256": ""})).hexdigest()


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


def _repository(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("repository root must be an absolute non-symlink directory")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("repository root is unavailable") from exc
    if not root.is_dir():
        raise ValueError("repository root must be a directory")
    return root


def _git_head(repository_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot resolve reset candidate Git HEAD") from exc
    return _full_commit(completed.stdout.strip(), label="reset candidate Git HEAD")


def _private_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    return read_stable_file(
        path,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
    )


def _identity_from_snapshot(snapshot: StableFileSnapshot) -> FileIdentity:
    return FileIdentity(
        path=str(snapshot.path),
        size_bytes=snapshot.size,
        sha256=snapshot.sha256,
        device=snapshot.device,
        inode=snapshot.inode,
        mtime_ns=snapshot.mtime_ns,
        mode=snapshot.mode,
        uid=snapshot.uid,
    )


def _file_identity(path: str | Path, *, label: str) -> FileIdentity:
    return _identity_from_snapshot(_private_snapshot(path, label=label))


def _identity_from_payload(value: object, *, label: str) -> FileIdentity:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_FIELDS:
        raise ValueError(f"{label} identity has unexpected fields")
    uid_value = value.get("uid")
    identity = FileIdentity(
        path=str(value.get("path") or ""),
        size_bytes=int(value.get("size_bytes") or 0),
        sha256=_lower_sha256(value.get("sha256"), label=f"{label} hash"),
        device=int(value.get("device") or 0),
        inode=int(value.get("inode") or 0),
        mtime_ns=int(value.get("mtime_ns") or 0),
        mode=int(value.get("mode") or 0),
        uid=int(cast(int | str, uid_value)) if uid_value is not None else -1,
    )
    if (
        not Path(identity.path).is_absolute()
        or identity.size_bytes <= 0
        or identity.device <= 0
        or identity.inode <= 0
        or identity.mtime_ns <= 0
        or identity.mode != 0o600
        or identity.uid < 0
    ):
        raise ValueError(f"{label} identity is invalid")
    return identity


def _normalize_roots(
    roots: Mapping[str, Mapping[str, str | Path]], *, repository_root: Path
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    if set(roots) != set(ROOT_ENVIRONMENTS):
        raise ValueError("account epoch roots must contain exact demo and paper keys")
    absolute: dict[str, dict[str, str]] = {}
    relative: dict[str, dict[str, str]] = {}
    seen: list[Path] = []
    for environment in ROOT_ENVIRONMENTS:
        environment_roots = roots[environment]
        if set(environment_roots) != set(ROOT_KINDS):
            raise ValueError(f"{environment} account epoch roots must contain account/inbox/capture")
        absolute[environment] = {}
        relative[environment] = {}
        for kind in ROOT_KINDS:
            raw = Path(environment_roots[kind]).expanduser()
            candidate = raw if raw.is_absolute() else repository_root / raw
            resolved = candidate.resolve(strict=False)
            try:
                rel = resolved.relative_to(repository_root)
                rel.relative_to("data")
            except ValueError as exc:
                raise ValueError(f"{environment} {kind} root must remain below repository data/") from exc
            if any(resolved == prior or resolved in prior.parents or prior in resolved.parents for prior in seen):
                raise ValueError("account epoch roots must be pairwise disjoint")
            seen.append(resolved)
            absolute[environment][kind] = str(resolved)
            relative[environment][kind] = rel.as_posix()
    return absolute, relative


def _verify_fresh_roots(roots: Mapping[str, Mapping[str, str]]) -> None:
    for environment in ROOT_ENVIRONMENTS:
        for kind in ROOT_KINDS:
            path = Path(roots[environment][kind])
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ValueError(f"fresh {environment} {kind} root is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"fresh {environment} {kind} root is not a directory")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ValueError(f"fresh {environment} {kind} root must have mode 0700")
            if metadata.st_uid != os.geteuid():
                raise ValueError(f"fresh {environment} {kind} root has another owner")
            try:
                next(path.iterdir())
            except StopIteration:
                continue
            raise ValueError(f"fresh {environment} {kind} root is not empty")


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reset archive manifest is not UTF-8") from exc
    singletons: dict[str, str] = {}
    repeated: dict[str, list[str]] = {key: [] for key in _REPEATED_MANIFEST_KEYS}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or "=" not in line:
            raise ValueError(f"reset archive manifest line {number} is malformed")
        key, value = line.split("=", 1)
        if key in repeated:
            repeated[key].append(value)
        elif key in _SINGLETON_MANIFEST_KEYS:
            if key in singletons:
                raise ValueError(f"reset archive manifest repeats {key}")
            singletons[key] = value
        else:
            raise ValueError(f"reset archive manifest has unknown key {key!r}")
    missing = set(_SINGLETON_MANIFEST_KEYS) - set(singletons)
    if missing:
        raise ValueError("reset archive manifest lacks required keys: " + ", ".join(sorted(missing)))
    stamp = singletons["ledger_reset_utc"]
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp) is None:
        raise ValueError("reset archive manifest timestamp is invalid")

    def flag(key: str) -> bool:
        raw = singletons[key]
        if raw not in {"0", "1"}:
            raise ValueError(f"reset archive manifest {key} must be 0 or 1")
        return raw == "1"

    sleeves = singletons["sleeves"].split()
    if not sleeves or len(sleeves) != len(set(sleeves)):
        raise ValueError("reset archive manifest sleeves are invalid")
    active_before = singletons["active_before"].split()
    if len(active_before) != len(set(active_before)):
        raise ValueError("reset archive manifest repeats active units")
    for key, values in repeated.items():
        if len(values) != len(set(values)):
            raise ValueError(f"reset archive manifest repeats {key}")
    return {
        "ledger_reset_utc": stamp,
        "git_head": _full_commit(singletons["git_head"], label="reset manifest Git head"),
        "sleeves": sleeves,
        "include_reports": flag("include_reports"),
        "include_caches": flag("include_caches"),
        "leave_stopped": flag("leave_stopped"),
        "env_file": singletons["env_file"],
        "account_env_file": singletons["account_env_file"],
        "paper_account_env_file": singletons["paper_account_env_file"],
        "demo_account_lease_path": singletons["demo_account_lease_path"],
        "demo_boundary": singletons["demo_boundary"],
        "paper_boundary": singletons["paper_boundary"],
        "active_before": active_before,
        "account_epoch_targets": repeated["account_epoch_target"],
        "archived_targets": repeated["target"],
        "preserved_risk_state": repeated["preserved_risk_state"],
    }


def _archive_bundle(
    archive_path: str | Path,
    sidecar_path: str | Path,
    *,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> tuple[FileIdentity, FileIdentity, str, dict[str, Any], dict[str, bool]]:
    if archive_snapshot is None:
        archive_snapshot = _private_snapshot(archive_path, label="reset archive")
    elif archive_snapshot.path != Path(archive_path).expanduser().absolute():
        raise ValueError("reset archive snapshot path differs")
    if sidecar_snapshot is None:
        sidecar_snapshot = _private_snapshot(
            sidecar_path,
            label="reset SHA-256 sidecar",
        )
    elif sidecar_snapshot.path != Path(sidecar_path).expanduser().absolute():
        raise ValueError("reset SHA-256 sidecar snapshot path differs")
    for label, source in (
        ("reset archive", archive_snapshot),
        ("reset SHA-256 sidecar", sidecar_snapshot),
    ):
        if source.mode != 0o600 or source.uid != os.geteuid() or source.nlink != 1:
            raise ValueError(f"{label} must be verifier-owned mode 0600 and not hard-linked")
    archive = _identity_from_snapshot(archive_snapshot)
    sidecar = _identity_from_snapshot(sidecar_snapshot)
    try:
        sidecar_text = sidecar_snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("reset SHA-256 sidecar is unreadable") from exc
    expected_sidecar = f"{archive.sha256}  {Path(archive.path).name}\n"
    if sidecar_text != expected_sidecar:
        raise ValueError("reset SHA-256 sidecar does not exactly bind the archive")
    try:
        with tarfile.open(
            fileobj=io.BytesIO(archive_snapshot.data),
            mode="r:gz",
        ) as handle:
            manifest_members = [member for member in handle.getmembers() if member.name == "ledger-reset-manifest.txt"]
            if len(manifest_members) != 1:
                raise ValueError("reset archive must contain one exact embedded manifest")
            member = manifest_members[0]
            if not member.isfile() or member.issym() or member.islnk() or member.size > 1024 * 1024:
                raise ValueError("reset archive manifest member is unsafe")
            extracted = handle.extractfile(member)
            if extracted is None:
                raise ValueError("reset archive manifest cannot be read")
            manifest_data = extracted.read()
            member_names = [value.name.rstrip("/") for value in handle.getmembers()]
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("reset archive is not a readable gzip tar") from exc
    observed_archive = _file_identity(archive.path, label="reset archive")
    observed_sidecar = _file_identity(sidecar.path, label="reset SHA-256 sidecar")
    if observed_archive != archive or observed_sidecar != sidecar:
        raise RuntimeError("reset archive bundle changed while it was verified")
    manifest = _parse_manifest(manifest_data)
    presence = {
        target: any(name == target or name.startswith(f"{target}/") for name in member_names)
        for target in manifest["account_epoch_targets"]
    }
    return (
        archive,
        sidecar,
        hashlib.sha256(manifest_data).hexdigest(),
        manifest,
        presence,
    )


def _validate_sequences(
    *,
    sleeves: Sequence[str],
    managed_units: Sequence[str],
    active_before: Sequence[str],
    inactive_after: Sequence[str],
    leave_stopped: bool,
) -> tuple[list[str], list[str], list[str], list[str]]:
    sleeve_list = list(sleeves)
    managed = list(managed_units)
    active = list(active_before)
    inactive = list(inactive_after)
    if not sleeve_list or len(sleeve_list) != len(set(sleeve_list)):
        raise ValueError("reset sleeves must be nonempty and unique")
    if tuple(sleeve_list) not in {
        ("long",),
        ("continuous",),
        ("long", "continuous", "retire-shared-compat"),
    }:
        raise ValueError("reset sleeves are not one canonical reset selection")
    if tuple(managed) != MANAGED_UNITS:
        raise ValueError("reset receipt requires the exact managed-unit order")
    if len(active) != len(set(active)) or any(unit not in managed for unit in active):
        raise ValueError("active-before units are invalid")
    if active != [unit for unit in managed if unit in set(active)]:
        raise ValueError("active-before units are not in managed-unit order")
    if leave_stopped:
        if inactive != managed:
            raise ValueError("leave-stopped reset must verify every managed unit inactive")
    elif inactive:
        raise ValueError("non-leave-stopped reset cannot claim all units inactive")
    return sleeve_list, managed, active, inactive


def build_account_reset_receipt(
    *,
    repository_root: str | Path,
    candidate_commit: str,
    started_ts_ns: int,
    finished_ts_ns: int,
    sleeves: Sequence[str],
    include_reports: bool,
    include_caches: bool,
    leave_stopped: bool,
    account_epoch_roots: Mapping[str, Mapping[str, str | Path]],
    managed_units: Sequence[str],
    active_before: Sequence[str],
    inactive_after: Sequence[str],
    archive_path: str | Path,
    sha256_sidecar_path: str | Path,
) -> dict[str, Any]:
    """Build a passed receipt after the reset has completed every safety check."""

    repository = _repository(repository_root)
    candidate = _full_commit(candidate_commit, label="reset candidate commit")
    if _git_head(repository) != candidate:
        raise ValueError("repository HEAD changed during the account reset")
    started = int(started_ts_ns)
    finished = int(finished_ts_ns)
    if started <= 0 or finished < started:
        raise ValueError("reset receipt timestamps are invalid")
    absolute_roots, relative_roots = _normalize_roots(account_epoch_roots, repository_root=repository)
    _verify_fresh_roots(absolute_roots)
    sleeve_list, managed, active, inactive = _validate_sequences(
        sleeves=sleeves,
        managed_units=managed_units,
        active_before=active_before,
        inactive_after=inactive_after,
        leave_stopped=leave_stopped,
    )
    archive, sidecar, manifest_hash, manifest, presence = _archive_bundle(archive_path, sha256_sidecar_path)
    expected_targets = [relative_roots[environment][kind] for environment in ROOT_ENVIRONMENTS for kind in ROOT_KINDS]
    if manifest["git_head"] != candidate:
        raise ValueError("reset archive manifest names another candidate commit")
    if manifest["sleeves"] != sleeve_list:
        raise ValueError("reset archive manifest names another sleeve selection")
    if manifest["include_reports"] is not bool(include_reports) or manifest["include_caches"] is not bool(
        include_caches
    ):
        raise ValueError("reset archive manifest option flags differ")
    if manifest["leave_stopped"] is not bool(leave_stopped):
        raise ValueError("reset archive manifest leave-stopped flag differs")
    if manifest["account_epoch_targets"] != expected_targets:
        raise ValueError("reset archive manifest names another account epoch root set")
    if manifest["active_before"] != active:
        raise ValueError("reset archive manifest active-before units differ")
    if manifest["demo_boundary"] != DEMO_BOUNDARY or manifest["paper_boundary"] != PAPER_BOUNDARY:
        raise ValueError("reset archive manifest boundary facts differ")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "status": "passed",
        "started_ts_ns": started,
        "finished_ts_ns": finished,
        "repository": {
            "root": str(repository),
            "candidate_commit": candidate,
        },
        "reset": {
            "mode": "execute",
            "sleeves": sleeve_list,
            "include_reports": bool(include_reports),
            "include_caches": bool(include_caches),
            "leave_stopped": bool(leave_stopped),
            "boundaries": {
                "demo": DEMO_BOUNDARY,
                "paper": PAPER_BOUNDARY,
            },
            "account_epoch_roots": absolute_roots,
            "account_epoch_relative_roots": relative_roots,
            "fresh_roots_verified": True,
        },
        "services": {
            "managed_units": managed,
            "active_before": active,
            "inactive_after": inactive,
            "restored_active_after": [] if leave_stopped else active,
            "all_managed_units_stopped_verified": bool(leave_stopped),
        },
        "archive": {
            "file": archive.to_dict(),
            "sha256_sidecar": sidecar.to_dict(),
            "embedded_manifest_sha256": manifest_hash,
            "embedded_manifest": manifest,
            "archived_account_epoch_presence": presence,
        },
        "execution_authorization": "not_granted",
        "limitations": list(LIMITATIONS),
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def _roots_from_payload(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("reset receipt account epoch roots are malformed")
    output: dict[str, dict[str, str]] = {}
    if set(value) != set(ROOT_ENVIRONMENTS):
        raise ValueError("reset receipt account epoch environments differ")
    for environment in ROOT_ENVIRONMENTS:
        raw = value[environment]
        if not isinstance(raw, Mapping) or set(raw) != set(ROOT_KINDS):
            raise ValueError(f"reset receipt {environment} roots are malformed")
        output[environment] = {kind: str(raw.get(kind) or "") for kind in ROOT_KINDS}
    return output


def verify_account_reset_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_candidate_commit: str | None = None,
    expected_roots: Mapping[str, Mapping[str, str | Path]] | None = None,
    require_leave_stopped: bool = False,
    require_fresh_roots: bool = False,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    payload = dict(receipt)
    expected_top = {
        "schema_version",
        "kind",
        "validator",
        "status",
        "started_ts_ns",
        "finished_ts_ns",
        "repository",
        "reset",
        "services",
        "archive",
        "execution_authorization",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_top:
        raise ValueError("reset receipt has unexpected or missing fields")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or payload.get("status") != "passed"
    ):
        raise ValueError("reset receipt identity or status is invalid")
    if payload.get("artifact_sha256") != _self_hash(payload):
        raise ValueError("reset receipt self-hash is invalid")
    if payload.get("execution_authorization") != "not_granted":
        raise ValueError("reset receipt cannot grant execution authority")
    if payload.get("limitations") != list(LIMITATIONS):
        raise ValueError("reset receipt limitations differ")
    started = int(payload.get("started_ts_ns") or 0)
    finished = int(payload.get("finished_ts_ns") or 0)
    if started <= 0 or finished < started:
        raise ValueError("reset receipt timestamps are invalid")

    repository = payload.get("repository")
    if not isinstance(repository, Mapping) or set(repository) != {
        "root",
        "candidate_commit",
    }:
        raise ValueError("reset receipt repository binding is malformed")
    repository_root = _repository(str(repository.get("root") or ""))
    candidate = _full_commit(repository.get("candidate_commit"), label="reset receipt candidate")
    if expected_candidate_commit is not None and candidate != _full_commit(
        expected_candidate_commit, label="expected reset candidate"
    ):
        raise ValueError("reset receipt names another candidate commit")

    reset = payload.get("reset")
    expected_reset_keys = {
        "mode",
        "sleeves",
        "include_reports",
        "include_caches",
        "leave_stopped",
        "boundaries",
        "account_epoch_roots",
        "account_epoch_relative_roots",
        "fresh_roots_verified",
    }
    if not isinstance(reset, Mapping) or set(reset) != expected_reset_keys:
        raise ValueError("reset receipt operation binding is malformed")
    if reset.get("mode") != "execute" or reset.get("fresh_roots_verified") is not True:
        raise ValueError("reset receipt does not describe a completed execute reset")
    for flag in ("include_reports", "include_caches", "leave_stopped"):
        if type(reset.get(flag)) is not bool:
            raise ValueError(f"reset receipt {flag} must be boolean")
    sleeves = reset.get("sleeves")
    if not isinstance(sleeves, list) or any(not isinstance(item, str) for item in sleeves):
        raise ValueError("reset receipt sleeves are malformed")
    boundaries = reset.get("boundaries")
    if boundaries != {"demo": DEMO_BOUNDARY, "paper": PAPER_BOUNDARY}:
        raise ValueError("reset receipt boundary facts differ")
    roots = _roots_from_payload(reset.get("account_epoch_roots"))
    normalized_roots, relative_roots = _normalize_roots(roots, repository_root=repository_root)
    if roots != normalized_roots or reset.get("account_epoch_relative_roots") != relative_roots:
        raise ValueError("reset receipt root identities do not normalize")
    if expected_roots is not None:
        expected_absolute, expected_relative = _normalize_roots(expected_roots, repository_root=repository_root)
        if roots != expected_absolute or relative_roots != expected_relative:
            raise ValueError("reset receipt names another account epoch root set")
    if require_fresh_roots:
        _verify_fresh_roots(roots)

    services = payload.get("services")
    expected_service_keys = {
        "managed_units",
        "active_before",
        "inactive_after",
        "restored_active_after",
        "all_managed_units_stopped_verified",
    }
    if not isinstance(services, Mapping) or set(services) != expected_service_keys:
        raise ValueError("reset receipt service state is malformed")
    managed = services.get("managed_units")
    active = services.get("active_before")
    inactive = services.get("inactive_after")
    restored = services.get("restored_active_after")
    if not all(isinstance(value, list) for value in (managed, active, inactive, restored)):
        raise ValueError("reset receipt service lists are malformed")
    managed_list = cast(list[Any], managed)
    active_list = cast(list[Any], active)
    inactive_list = cast(list[Any], inactive)
    restored_list = cast(list[Any], restored)
    if any(
        not isinstance(item, str)
        for values in (managed_list, active_list, inactive_list, restored_list)
        for item in values
    ):
        raise ValueError("reset receipt service list entries must be strings")
    typed_managed = cast(list[str], managed_list)
    typed_active = cast(list[str], active_list)
    typed_inactive = cast(list[str], inactive_list)
    typed_restored = cast(list[str], restored_list)
    leave_stopped = bool(reset.get("leave_stopped"))
    _validate_sequences(
        sleeves=sleeves,
        managed_units=typed_managed,
        active_before=typed_active,
        inactive_after=typed_inactive,
        leave_stopped=leave_stopped,
    )
    expected_restored = [] if leave_stopped else typed_active
    if typed_restored != expected_restored or services.get("all_managed_units_stopped_verified") is not leave_stopped:
        raise ValueError("reset receipt aggregate service state is inconsistent")
    if require_leave_stopped and not leave_stopped:
        raise ValueError("reset receipt did not leave every managed unit stopped")

    archive = payload.get("archive")
    expected_archive_keys = {
        "file",
        "sha256_sidecar",
        "embedded_manifest_sha256",
        "embedded_manifest",
        "archived_account_epoch_presence",
    }
    if not isinstance(archive, Mapping) or set(archive) != expected_archive_keys:
        raise ValueError("reset receipt archive binding is malformed")
    archive_identity = _identity_from_payload(archive.get("file"), label="reset archive")
    sidecar_identity = _identity_from_payload(archive.get("sha256_sidecar"), label="reset SHA-256 sidecar")
    (
        observed_archive,
        observed_sidecar,
        manifest_hash,
        manifest,
        presence,
    ) = _archive_bundle(
        archive_identity.path,
        sidecar_identity.path,
        archive_snapshot=archive_snapshot,
        sidecar_snapshot=sidecar_snapshot,
    )
    if observed_archive != archive_identity or observed_sidecar != sidecar_identity:
        raise ValueError("reset archive bundle changed after receipt creation")
    if (
        archive.get("embedded_manifest_sha256") != manifest_hash
        or archive.get("embedded_manifest") != manifest
        or archive.get("archived_account_epoch_presence") != presence
    ):
        raise ValueError("reset archive manifest changed after receipt creation")
    expected_targets = [relative_roots[environment][kind] for environment in ROOT_ENVIRONMENTS for kind in ROOT_KINDS]
    if (
        manifest["git_head"] != candidate
        or manifest["sleeves"] != sleeves
        or manifest["include_reports"] is not reset.get("include_reports")
        or manifest["include_caches"] is not reset.get("include_caches")
        or manifest["leave_stopped"] is not leave_stopped
        or manifest["active_before"] != typed_active
        or manifest["account_epoch_targets"] != expected_targets
        or manifest["demo_boundary"] != DEMO_BOUNDARY
        or manifest["paper_boundary"] != PAPER_BOUNDARY
    ):
        raise ValueError("reset receipt does not reproduce from its archive manifest")
    return payload


def load_account_reset_receipt(
    path: str | Path,
    *,
    expected_candidate_commit: str | None = None,
    expected_roots: Mapping[str, Mapping[str, str | Path]] | None = None,
    require_leave_stopped: bool = False,
    require_fresh_roots: bool = False,
    snapshot: StableFileSnapshot | None = None,
    archive_snapshot: StableFileSnapshot | None = None,
    sidecar_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _private_snapshot(path, label="account reset receipt")
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("account reset receipt snapshot path differs")
    if snapshot.mode != 0o600 or snapshot.uid != os.geteuid():
        raise ValueError("account reset receipt must be verifier-owned mode 0600")
    try:
        value = json.loads(
            snapshot.data,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"account reset receipt contains non-finite token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("account reset receipt is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("account reset receipt must be a JSON object")
    payload = verify_account_reset_receipt(
        value,
        expected_candidate_commit=expected_candidate_commit,
        expected_roots=expected_roots,
        require_leave_stopped=require_leave_stopped,
        require_fresh_roots=require_fresh_roots,
        archive_snapshot=archive_snapshot,
        sidecar_snapshot=sidecar_snapshot,
    )
    return payload


def validate_account_reset_receipt_output(path: str | Path, *, forbidden_roots: Sequence[str | Path] = ()) -> Path:
    output = Path(path).expanduser()
    if not output.is_absolute() or output.is_symlink():
        raise ValueError("account reset receipt output must be an absolute non-symlink path")
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("account reset receipt parent must already exist") from exc
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("account reset receipt parent must be a non-symlink directory")
    resolved = parent / output.name
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError(f"account reset receipt already exists: {resolved}")
    for raw in forbidden_roots:
        root = Path(raw).expanduser().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            raise ValueError("account reset receipt output cannot be inside a reset root")
    return resolved


def write_account_reset_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    payload = verify_account_reset_receipt(receipt)
    reset = payload["reset"]
    roots = _roots_from_payload(reset["account_epoch_roots"])
    output = validate_account_reset_receipt_output(
        path,
        forbidden_roots=[roots[environment][kind] for environment in ROOT_ENVIRONMENTS for kind in ROOT_KINDS],
    )
    data = canonical_json(payload) + b"\n"
    created = False
    try:
        descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("account reset receipt write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(str(output.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        load_account_reset_receipt(
            output,
            expected_candidate_commit=payload["repository"]["candidate_commit"],
            expected_roots=roots,
            require_leave_stopped=bool(reset["leave_stopped"]),
            require_fresh_roots=True,
        )
    except BaseException:
        if created:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            else:
                try:
                    directory_fd = os.open(str(output.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        raise
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or preflight a source-reopening account reset receipt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--forbidden-root", action="append", default=[])

    create = subparsers.add_parser("create")
    create.add_argument("--repository-root", required=True)
    create.add_argument("--candidate-commit", required=True)
    create.add_argument("--started-ts-ns", required=True, type=int)
    create.add_argument("--archive", required=True)
    create.add_argument("--sha256-sidecar", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--sleeve", action="append", required=True)
    create.add_argument("--include-reports", action="store_true")
    create.add_argument("--include-caches", action="store_true")
    create.add_argument("--leave-stopped", action="store_true")
    create.add_argument("--demo-account-root", required=True)
    create.add_argument("--demo-inbox-root", required=True)
    create.add_argument("--demo-capture-root", required=True)
    create.add_argument("--paper-account-root", required=True)
    create.add_argument("--paper-inbox-root", required=True)
    create.add_argument("--paper-capture-root", required=True)
    create.add_argument("--managed-unit", action="append", required=True)
    create.add_argument("--active-before-unit", action="append", default=[])
    create.add_argument("--inactive-after-unit", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            output = validate_account_reset_receipt_output(args.output, forbidden_roots=args.forbidden_root)
            print(json.dumps({"output": str(output), "available": True}, sort_keys=True))
            return 0
        roots = {
            "demo": {
                "account": args.demo_account_root,
                "inbox": args.demo_inbox_root,
                "capture": args.demo_capture_root,
            },
            "paper": {
                "account": args.paper_account_root,
                "inbox": args.paper_inbox_root,
                "capture": args.paper_capture_root,
            },
        }
        payload = build_account_reset_receipt(
            repository_root=args.repository_root,
            candidate_commit=args.candidate_commit,
            started_ts_ns=args.started_ts_ns,
            finished_ts_ns=time.time_ns(),
            sleeves=args.sleeve,
            include_reports=args.include_reports,
            include_caches=args.include_caches,
            leave_stopped=args.leave_stopped,
            account_epoch_roots=roots,
            managed_units=args.managed_unit,
            active_before=args.active_before_unit,
            inactive_after=args.inactive_after_unit,
            archive_path=args.archive,
            sha256_sidecar_path=args.sha256_sidecar,
        )
        output = write_account_reset_receipt(args.output, payload)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "artifact_sha256": payload["artifact_sha256"],
                    "status": payload["status"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
