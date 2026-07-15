"""Create a disjoint, empty filesystem epoch from a stopped-namespace seal.

Creation source-reopens the exact stopped natural epoch, requires its registered
fleet to remain inactive, and derives the candidate, freeze, and eleven old
root identities from that seal. Verification reopens the same seal without
requiring units to remain stopped, so it is safe after activation. This module
does not edit environment files, start units, deploy software, or grant
execution authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .deterministic_serialization import canonical_json
from .stopped_natural_epoch import (
    OLD_ROOT_ROLES,
    load_stopped_natural_epoch_seal,
)


SCHEMA_VERSION = 1
KIND = "fresh_deploy_epoch"
VALIDATOR = "fresh_deploy_epoch_v1"
MANIFEST_NAME = "fresh-deploy-epoch.json"
EXECUTION_AUTHORIZATION = "not_granted"
LIMITATIONS = (
    "stopped_epoch_seal_is_integrity_evidence_not_filesystem_immutability",
    "late_environment_map_does_not_prove_unit_dropins_or_runtime_consumption",
    "fresh_roots_do_not_grant_deploy_or_execution_authority",
)

ROOT_RELATIVE_PATHS: Mapping[str, str] = {
    "demo_account": "demo-account",
    "demo_inbox": "demo-inbox",
    "demo_capture": "demo-capture",
    "paper_account": "paper-account",
    "paper_inbox": "paper-inbox",
    "paper_capture": "paper-capture",
    "long_demo": "long-demo",
    "long_paper": "long-paper",
    "continuous_demo": "continuous-demo",
    "continuous_paper": "continuous-paper",
}


@dataclass(frozen=True, slots=True)
class PathIdentity:
    path: str
    kind: str
    device: int
    inode: int
    mode: int
    uid: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _full_commit(value: object) -> str:
    candidate = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("candidate commit must be a full lowercase Git commit")
    return candidate


def _freeze_id(value: object) -> str:
    freeze_id = str(value or "")
    if not re.fullmatch(r"natural-cutover-[0-9a-f]{64}", freeze_id):
        raise ValueError("freeze id must be a canonical natural-cutover identity")
    return freeze_id


def _absolute_without_symlink_components(
    path: str | Path,
    *,
    label: str,
    require_exists: bool,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    current = Path(candidate.anchor)
    parts = candidate.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_exists or index != len(parts) - 1:
                raise ValueError(f"{label} is unavailable: {candidate}") from None
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} must not traverse a symbolic link")
    if require_exists:
        return candidate.resolve(strict=True)
    return candidate.resolve(strict=False)


def _private_file_snapshot(path: str | Path, *, label: str) -> StableFileSnapshot:
    source = _absolute_without_symlink_components(path, label=label, require_exists=True)
    snapshot = read_stable_file(
        source,
        label=label,
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
    )
    if snapshot.nlink != 1:
        raise ValueError(f"{label} must be verifier-owned with exact mode 0600")
    return snapshot


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


def _private_file_identity(path: str | Path, *, label: str) -> FileIdentity:
    return _identity_from_snapshot(_private_file_snapshot(path, label=label))


def _path_identity(path: Path, *, label: str) -> PathIdentity:
    resolved = _absolute_without_symlink_components(path, label=label, require_exists=True)
    metadata = resolved.stat()
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        raise ValueError(f"{label} must be a regular file or directory")
    return PathIdentity(
        path=str(resolved),
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _late_environment(root_paths: Mapping[str, str]) -> dict[str, dict[str, str]]:
    demo_route = {
        "ACCOUNT_EXECUTION_ROOT": root_paths["demo_account"],
        "ACCOUNT_INTENT_INBOX_ROOT": root_paths["demo_inbox"],
    }
    paper_route = {
        "ACCOUNT_EXECUTION_ROOT": root_paths["paper_account"],
        "ACCOUNT_INTENT_INBOX_ROOT": root_paths["paper_inbox"],
    }
    natural_epoch_clear = {
        "NATURAL_EVIDENCE_REQUIRED": "0",
        "NATURAL_RUN_CONFIG": "",
        "STRATEGY_TARGET_CAPTURE_PATH": "",
        "CANDIDATE_UNIVERSE_FILE": "",
    }
    return {
        "liquidity-migration-account-execution.service": {
            **demo_route,
            "ACCOUNT_CAPTURE_ROOT": root_paths["demo_capture"],
        },
        "liquidity-migration-account-paper-execution.service": {
            **paper_route,
            "ACCOUNT_PAPER_CAPTURE_ROOT": root_paths["paper_capture"],
        },
        "liquidity-migration-bybit-long-demo.service": {
            **demo_route,
            **natural_epoch_clear,
            "DATA_ROOT": root_paths["long_demo"],
        },
        "liquidity-migration-bybit-long-paper.service": {
            **paper_route,
            "DATA_ROOT": root_paths["long_paper"],
        },
        "liquidity-migration-bybit-continuous-demo.service": {
            **demo_route,
            **natural_epoch_clear,
            "DATA_ROOT": root_paths["continuous_demo"],
        },
        "liquidity-migration-bybit-continuous-paper.service": {
            **paper_route,
            "DATA_ROOT": root_paths["continuous_paper"],
        },
        "liquidity-migration-continuous-hedge.service": {
            **demo_route,
            "PRIMARY_ROOT": root_paths["continuous_demo"],
        },
        # These auxiliary units consume the same roots through their checked-in
        # environment-backed launch surfaces. Deployment integration must still
        # require matching late files before either unit is started.
        "liquidity-migration-continuous-rmom-refresh.service": {
            "CONTINUOUS_DEMO_DATA_ROOT": root_paths["continuous_demo"],
            "CONTINUOUS_PAPER_DATA_ROOT": root_paths["continuous_paper"],
        },
        "liquidity-migration-demo-liveness.service": {
            "ACCOUNT_CAPTURE_ROOT": root_paths["demo_capture"],
            "ACCOUNT_EXECUTION_ROOT": root_paths["demo_account"],
            "ACCOUNT_PAPER_CAPTURE_ROOT": root_paths["paper_capture"],
            "ACCOUNT_PAPER_EXECUTION_ROOT": root_paths["paper_account"],
            "CONTINUOUS_DEMO_DATA_ROOT": root_paths["continuous_demo"],
            "CONTINUOUS_PAPER_DATA_ROOT": root_paths["continuous_paper"],
            "LONG_DEMO_DATA_ROOT": root_paths["long_demo"],
            "LONG_PAPER_DATA_ROOT": root_paths["long_paper"],
        },
    }


def _exclusive_write(path: Path, payload: Mapping[str, Any]) -> None:
    data = canonical_json(dict(payload)) + b"\n"
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("fresh deploy manifest write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _stopped_epoch_binding(
    path: str | Path,
    *,
    require_currently_stopped: bool,
    systemctl_bin: str = "systemctl",
) -> tuple[FileIdentity, dict[str, Any], str, str, int, list[PathIdentity]]:
    """Source-reopen a stopped seal and derive its exact fresh-epoch binding."""

    seal_snapshot = _private_file_snapshot(path, label="stopped epoch seal")
    seal_before = _identity_from_snapshot(seal_snapshot)
    stopped = load_stopped_natural_epoch_seal(
        seal_before.path,
        require_currently_stopped=require_currently_stopped,
        systemctl_bin=systemctl_bin,
        snapshot=seal_snapshot,
    )
    seal_after_snapshot = _private_file_snapshot(path, label="stopped epoch seal")
    seal_after = _identity_from_snapshot(seal_after_snapshot)
    if seal_after != seal_before or seal_after_snapshot.data != seal_snapshot.data:
        raise RuntimeError("stopped epoch seal changed while it was source-reopened")

    identity = _mapping(stopped.get("identity"), label="stopped epoch identity")
    candidate = _full_commit(identity.get("candidate_commit"))
    clean_freeze_id = _freeze_id(identity.get("freeze_id"))
    stopped_created = int(stopped.get("created_ts_ns") or 0)
    if stopped_created <= 0:
        raise ValueError("stopped epoch seal has an invalid creation time")

    trees = _mapping(stopped.get("source_trees"), label="stopped epoch source trees")
    if set(trees) != set(OLD_ROOT_ROLES):
        raise ValueError("stopped epoch seal does not bind the exact eleven old roots")
    old_identities: list[PathIdentity] = []
    for role in OLD_ROOT_ROLES:
        tree = _mapping(trees[role], label=f"stopped epoch {role} tree")
        expected = _path_identity_from_payload(tree.get("root_identity"), label=f"stopped epoch {role} root")
        actual = _path_identity(Path(expected.path), label=f"old sealed {role} root")
        if actual != expected:
            raise ValueError(f"stopped epoch {role} root identity changed")
        old_identities.append(expected)

    sealed = _mapping(stopped.get("sealed_namespace"), label="stopped epoch sealed namespace")
    registered_roots = sealed.get("required_old_mutable_roots")
    expected_roots = [{"role": role, "path": old_identities[index].path} for index, role in enumerate(OLD_ROOT_ROLES)]
    if registered_roots != expected_roots:
        raise ValueError("stopped epoch root registry differs from its source trees")
    return (
        seal_after,
        stopped,
        candidate,
        clean_freeze_id,
        stopped_created,
        old_identities,
    )


def create_fresh_deploy_epoch(
    *,
    stopped_seal_path: str | Path,
    epoch_parent: str | Path,
    systemctl_bin: str = "systemctl",
    created_ts_ns: int | None = None,
) -> Path:
    """Create ten empty roots from one source-reopened, currently stopped seal."""

    (
        seal,
        stopped,
        candidate,
        clean_freeze_id,
        stopped_created,
        old_identities,
    ) = _stopped_epoch_binding(
        stopped_seal_path,
        require_currently_stopped=True,
        systemctl_bin=systemctl_bin,
    )
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= 0:
        raise ValueError("fresh deploy epoch creation time must be positive")
    if created <= stopped_created:
        raise ValueError("fresh deploy epoch must be created after the stopped seal")

    parent = _absolute_without_symlink_components(epoch_parent, label="fresh deploy epoch parent", require_exists=False)
    if os.path.lexists(parent):
        raise FileExistsError(f"fresh deploy epoch parent already exists: {parent}")
    old_paths = [Path(identity.path) for identity in old_identities]
    if any(_paths_overlap(parent, old_path) for old_path in old_paths):
        raise ValueError("fresh deploy epoch cannot contain or nest inside an old sealed path")

    created_parent = False
    try:
        os.mkdir(parent, 0o700)
        created_parent = True
        os.chmod(parent, 0o700)
        roots: dict[str, PathIdentity] = {}
        for role, relative in ROOT_RELATIVE_PATHS.items():
            root = parent / relative
            os.mkdir(root, 0o700)
            os.chmod(root, 0o700)
            identity = _path_identity(root, label=f"fresh {role} root")
            if identity.mode != 0o700 or identity.uid != os.geteuid():
                raise RuntimeError(f"fresh {role} root did not retain mode 0700")
            roots[role] = identity
        root_paths = {role: identity.path for role, identity in roots.items()}
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "validator": VALIDATOR,
            "created_ts_ns": created,
            "epoch_id": f"fresh-deploy-{candidate[:12]}-{created}",
            "candidate_commit": candidate,
            "freeze_id": clean_freeze_id,
            "stopped_epoch_seal": seal.to_dict(),
            "epoch_parent": str(parent),
            "old_sealed_paths": [identity.to_dict() for identity in old_identities],
            "old_sealed_paths_sha256": hashlib.sha256(
                canonical_json({"paths": [identity.to_dict() for identity in old_identities]})
            ).hexdigest(),
            "roots": {role: identity.to_dict() for role, identity in roots.items()},
            "late_environment": _late_environment(root_paths),
            "execution_authorization": EXECUTION_AUTHORIZATION,
            "limitations": list(LIMITATIONS),
            "artifact_sha256": "",
        }
        payload["artifact_sha256"] = _self_hash(payload)
        manifest = parent / MANIFEST_NAME
        _exclusive_write(manifest, payload)
        directory = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        (
            final_seal,
            final_stopped,
            final_candidate,
            final_freeze_id,
            final_stopped_created,
            final_old_identities,
        ) = _stopped_epoch_binding(
            stopped_seal_path,
            require_currently_stopped=True,
            systemctl_bin=systemctl_bin,
        )
        if (
            final_seal != seal
            or final_stopped.get("artifact_sha256") != stopped.get("artifact_sha256")
            or final_candidate != candidate
            or final_freeze_id != clean_freeze_id
            or final_stopped_created != stopped_created
            or final_old_identities != old_identities
        ):
            raise RuntimeError("stopped epoch binding changed during fresh-root creation")
        load_fresh_deploy_epoch(manifest, require_empty_roots=True)
        return manifest
    except BaseException:
        if created_parent:
            shutil.rmtree(parent, ignore_errors=True)
        raise


def _strict_json(data: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in values:
            if key in output:
                raise ValueError(f"fresh deploy manifest repeats JSON key {key!r}")
            output[key] = value
        return output

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("fresh deploy manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("fresh deploy manifest must be a JSON object")
    return value


def _path_identity_from_payload(value: object, *, label: str) -> PathIdentity:
    if not isinstance(value, Mapping) or set(value) != {"path", "kind", "device", "inode", "mode", "uid"}:
        raise ValueError(f"{label} identity is malformed")
    identity = PathIdentity(
        path=str(value.get("path") or ""),
        kind=str(value.get("kind") or ""),
        device=int(value.get("device") or 0),
        inode=int(value.get("inode") or 0),
        mode=int(value.get("mode") or 0),
        uid=int(cast(int, value.get("uid"))) if value.get("uid") is not None else -1,
    )
    if identity.kind not in {"file", "directory"}:
        raise ValueError(f"{label} identity has an invalid kind")
    return identity


def _file_identity_from_payload(value: object, *, label: str) -> FileIdentity:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "size_bytes",
        "sha256",
        "device",
        "inode",
        "mtime_ns",
        "mode",
        "uid",
    }:
        raise ValueError(f"{label} identity is malformed")
    identity = FileIdentity(
        path=str(value.get("path") or ""),
        size_bytes=int(value.get("size_bytes") or 0),
        sha256=str(value.get("sha256") or ""),
        device=int(value.get("device") or 0),
        inode=int(value.get("inode") or 0),
        mtime_ns=int(value.get("mtime_ns") or 0),
        mode=int(value.get("mode") or 0),
        uid=int(cast(int, value.get("uid"))) if value.get("uid") is not None else -1,
    )
    if (
        not Path(identity.path).is_absolute()
        or identity.size_bytes < 0
        or not re.fullmatch(r"[0-9a-f]{64}", identity.sha256)
        or identity.device <= 0
        or identity.inode <= 0
        or identity.mtime_ns <= 0
        or identity.mode != 0o600
        or identity.uid < 0
    ):
        raise ValueError(f"{label} identity is invalid")
    return identity


def load_fresh_deploy_epoch(
    path: str | Path,
    *,
    require_empty_roots: bool = False,
    snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Reopen the manifest, stopped seal, old namespace, and fresh roots."""

    if snapshot is None:
        manifest_snapshot = _private_file_snapshot(
            path,
            label="fresh deploy manifest",
        )
    else:
        manifest_snapshot = snapshot
        if manifest_snapshot.path != Path(path).expanduser().absolute():
            raise ValueError("fresh deploy manifest snapshot path differs")
        if (
            manifest_snapshot.mode != 0o600
            or manifest_snapshot.uid != os.geteuid()
            or manifest_snapshot.nlink != 1
        ):
            raise ValueError(
                "fresh deploy manifest must be verifier-owned with exact mode 0600"
            )
    manifest_identity = _identity_from_snapshot(manifest_snapshot)
    payload = _strict_json(manifest_snapshot.data)
    expected_fields = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "epoch_id",
        "candidate_commit",
        "freeze_id",
        "stopped_epoch_seal",
        "epoch_parent",
        "old_sealed_paths",
        "old_sealed_paths_sha256",
        "roots",
        "late_environment",
        "execution_authorization",
        "limitations",
        "artifact_sha256",
    }
    if set(payload) != expected_fields:
        raise ValueError("fresh deploy manifest has unexpected or missing fields")
    candidate = _full_commit(payload.get("candidate_commit"))
    clean_freeze_id = _freeze_id(payload.get("freeze_id"))
    created = int(payload.get("created_ts_ns") or 0)
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != KIND
        or payload.get("validator") != VALIDATOR
        or payload.get("epoch_id") != f"fresh-deploy-{candidate[:12]}-{created}"
        or payload.get("execution_authorization") != EXECUTION_AUTHORIZATION
        or payload.get("limitations") != list(LIMITATIONS)
        or payload.get("artifact_sha256") != _self_hash(payload)
        or created <= 0
    ):
        raise ValueError("fresh deploy manifest identity or self-hash is invalid")

    seal_expected = _file_identity_from_payload(payload.get("stopped_epoch_seal"), label="stopped epoch seal")
    (
        seal_actual,
        _stopped,
        stopped_candidate,
        stopped_freeze_id,
        stopped_created,
        stopped_old_identities,
    ) = _stopped_epoch_binding(
        seal_expected.path,
        require_currently_stopped=False,
    )
    if seal_actual != seal_expected:
        raise ValueError("stopped epoch seal changed after fresh-root creation")
    if candidate != stopped_candidate or clean_freeze_id != stopped_freeze_id:
        raise ValueError("fresh deploy identity differs from its stopped epoch seal")
    if created <= stopped_created:
        raise ValueError("fresh deploy epoch was not created after its stopped seal")

    parent = _absolute_without_symlink_components(
        str(payload.get("epoch_parent") or ""),
        label="fresh deploy epoch parent",
        require_exists=True,
    )
    if Path(manifest_identity.path) != parent / MANIFEST_NAME:
        raise ValueError("fresh deploy manifest is not at its epoch's canonical path")
    parent_metadata = parent.stat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != os.geteuid()
    ):
        raise ValueError("fresh deploy epoch parent must remain verifier-owned mode 0700")

    raw_old = payload.get("old_sealed_paths")
    if not isinstance(raw_old, list) or not raw_old:
        raise ValueError("fresh deploy manifest lacks old sealed paths")
    old_identities = [
        _path_identity_from_payload(value, label=f"old sealed path {index}") for index, value in enumerate(raw_old)
    ]
    if old_identities != stopped_old_identities:
        raise ValueError("fresh deploy old sealed paths differ from the stopped epoch seal")
    if len({identity.path for identity in old_identities}) != len(old_identities):
        raise ValueError("fresh deploy manifest repeats an old sealed path")
    if len({(identity.device, identity.inode) for identity in old_identities}) != len(old_identities):
        raise ValueError("fresh deploy manifest aliases old sealed path inodes")
    if (
        payload.get("old_sealed_paths_sha256")
        != hashlib.sha256(canonical_json({"paths": [identity.to_dict() for identity in old_identities]})).hexdigest()
    ):
        raise ValueError("fresh deploy old sealed path-set hash changed")
    raw_roots = payload.get("roots")
    if not isinstance(raw_roots, Mapping) or set(raw_roots) != set(ROOT_RELATIVE_PATHS):
        raise ValueError("fresh deploy manifest must contain exactly ten registered roots")
    roots: dict[str, PathIdentity] = {}
    for role, relative in ROOT_RELATIVE_PATHS.items():
        expected = _path_identity_from_payload(raw_roots[role], label=f"fresh {role} root")
        actual = _path_identity(Path(expected.path), label=f"fresh {role} root")
        if actual != expected or actual.kind != "directory" or actual.mode != 0o700:
            raise ValueError(f"fresh {role} root identity or mode changed")
        if Path(actual.path) != parent / relative:
            raise ValueError(f"fresh {role} root is not at its registered path")
        if require_empty_roots and any(Path(actual.path).iterdir()):
            raise ValueError(f"fresh {role} root is not empty")
        roots[role] = actual
    root_paths = [Path(identity.path) for identity in roots.values()]
    if len({(identity.device, identity.inode) for identity in roots.values()}) != len(roots):
        raise ValueError("fresh deploy roots alias the same inode")
    if any(_paths_overlap(left, right) for index, left in enumerate(root_paths) for right in root_paths[index + 1 :]):
        raise ValueError("fresh deploy roots alias or nest each other")
    old_paths = [Path(identity.path) for identity in old_identities]
    if any(_paths_overlap(parent, old) for old in old_paths):
        raise ValueError("fresh deploy epoch overlaps an old sealed path")
    if any(_paths_overlap(root, old) for root in root_paths for old in old_paths):
        raise ValueError("fresh deploy root overlaps an old sealed path")
    root_values = {role: identity.path for role, identity in roots.items()}
    if payload.get("late_environment") != _late_environment(root_values):
        raise ValueError("fresh deploy late environment map changed")
    # Re-read the manifest after validating every named source/path.
    final_snapshot = _private_file_snapshot(path, label="fresh deploy manifest")
    if (
        _identity_from_snapshot(final_snapshot) != manifest_identity
        or final_snapshot.data != manifest_snapshot.data
    ):
        raise RuntimeError("fresh deploy manifest changed while it was validated")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify a fresh deploy epoch from a stopped seal")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--stopped-seal", type=Path, required=True)
    create.add_argument("--epoch-parent", type=Path, required=True)
    create.add_argument("--systemctl-bin", default="systemctl")
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--require-empty-roots", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            output = create_fresh_deploy_epoch(
                stopped_seal_path=args.stopped_seal,
                epoch_parent=args.epoch_parent,
                systemctl_bin=args.systemctl_bin,
            )
            print(str(output))
            return 0
        receipt = load_fresh_deploy_epoch(
            args.manifest,
            require_empty_roots=bool(args.require_empty_roots),
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "artifact_sha256": receipt["artifact_sha256"],
                    "candidate_commit": receipt["candidate_commit"],
                    "freeze_id": receipt["freeze_id"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"fresh deploy epoch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXECUTION_AUTHORIZATION",
    "KIND",
    "LIMITATIONS",
    "MANIFEST_NAME",
    "ROOT_RELATIVE_PATHS",
    "VALIDATOR",
    "create_fresh_deploy_epoch",
    "load_fresh_deploy_epoch",
    "main",
]
