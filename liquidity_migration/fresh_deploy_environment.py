"""Materialize and verify exact systemd EnvironmentFile bytes for a fresh epoch.

The evidence artifact records a per-unit variable map.  Systemd cannot consume
that JSON directly, so checked deployment needs one deterministic translation
rather than hand-maintained overrides.  This module writes no unit drop-ins and
starts no service; it only publishes the nine owner-only environment files and
a source-bound receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Mapping, cast

from .artifact_snapshot import StableFileSnapshot, read_stable_file, rename_noreplace
from .deterministic_serialization import canonical_json
from .fresh_deploy_epoch import load_fresh_deploy_epoch


SCHEMA_VERSION = 1
KIND = "fresh_deploy_environment_materialization"
VALIDATOR = "fresh_deploy_environment_materialization_v1"
RECEIPT_NAME = "environment-materialization.json"
DEFAULT_OUTPUT_DIRECTORY = Path("/etc/liquidity-migration/fresh-deploy")
_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
_UNIT_PATTERN = re.compile(r"liquidity-migration-[a-z0-9-]+\.service")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _self_hash(payload: Mapping[str, Any]) -> str:
    return _sha256(canonical_json({**dict(payload), "artifact_sha256": ""}))


def _absolute_no_symlink(path: str | Path, *, label: str, require_exists: bool) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    current = Path(candidate.anchor)
    for index, part in enumerate(candidate.parts[1:]):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_exists or index != len(candidate.parts[1:]) - 1:
                raise ValueError(f"{label} is unavailable: {candidate}") from None
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} must not traverse a symbolic link")
    return candidate.resolve(strict=require_exists)


def _quote_environment_value(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("fresh-deploy environment values cannot contain control lines")
    # systemd EnvironmentFile double-quoted values preserve spaces and literal
    # dollar signs. Escape only the two characters with special meaning here.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _refuse_evidence_path_overlap(output: Path, manifest: Mapping[str, Any]) -> None:
    protected: list[Path] = [Path(str(manifest.get("epoch_parent") or ""))]
    roots = manifest.get("roots")
    old_paths = manifest.get("old_sealed_paths")
    if not isinstance(roots, Mapping) or not isinstance(old_paths, list):
        raise ValueError("fresh-deploy manifest path namespace is malformed")
    for value in roots.values():
        if not isinstance(value, Mapping):
            raise ValueError("fresh-deploy root identity is malformed")
        protected.append(Path(str(value.get("path") or "")))
    for value in old_paths:
        if not isinstance(value, Mapping):
            raise ValueError("fresh-deploy old-path identity is malformed")
        protected.append(Path(str(value.get("path") or "")))
    if any(not path.is_absolute() for path in protected):
        raise ValueError("fresh-deploy manifest contains a non-absolute protected path")
    if any(_paths_overlap(output, path) for path in protected):
        raise ValueError("fresh-deploy environment directory overlaps an evidence or runtime path")


def render_unit_environment(
    *,
    unit: str,
    environment: Mapping[str, str],
    epoch_artifact_sha256: str,
) -> bytes:
    """Render canonical bytes for one registered unit."""

    if not _UNIT_PATTERN.fullmatch(unit):
        raise ValueError(f"invalid fresh-deploy systemd unit name: {unit!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", epoch_artifact_sha256):
        raise ValueError("fresh-deploy epoch artifact hash is invalid")
    if not environment:
        raise ValueError(f"fresh-deploy environment for {unit} is empty")
    lines = [
        "# Generated from a source-reopened fresh_deploy_epoch; do not edit.",
        f"# fresh_deploy_epoch_artifact_sha256={epoch_artifact_sha256}",
    ]
    for key in sorted(environment):
        value = environment[key]
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"fresh-deploy environment key is invalid: {key!r}")
        if not isinstance(value, str):
            raise ValueError(f"fresh-deploy environment value for {key} must be a string")
        lines.append(f"{key}={_quote_environment_value(value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def expected_environment_files(manifest: Mapping[str, Any]) -> dict[str, bytes]:
    raw = manifest.get("late_environment")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("fresh-deploy manifest has no late environment map")
    artifact = str(manifest.get("artifact_sha256") or "")
    output: dict[str, bytes] = {}
    for unit, value in sorted(raw.items()):
        if not isinstance(unit, str) or not isinstance(value, Mapping):
            raise ValueError("fresh-deploy late environment map is malformed")
        environment: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("fresh-deploy late environment entry is malformed")
            environment[key] = item
        output[f"{unit}.env"] = render_unit_environment(
            unit=unit,
            environment=environment,
            epoch_artifact_sha256=artifact,
        )
    return output


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        offset = 0
        view = memoryview(data)
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("fresh-deploy environment write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)


def _manifest_identity(snapshot: StableFileSnapshot) -> dict[str, Any]:
    return {
        "path": str(snapshot.path),
        "size_bytes": snapshot.size,
        "sha256": snapshot.sha256,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "mtime_ns": snapshot.mtime_ns,
        "mode": snapshot.mode,
        "uid": snapshot.uid,
    }


def _receipt_payload(
    *,
    manifest_snapshot: StableFileSnapshot,
    manifest: Mapping[str, Any],
    output_directory: Path,
    files: Mapping[str, bytes],
    created_ts_ns: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "validator": VALIDATOR,
        "created_ts_ns": created_ts_ns,
        "fresh_deploy_epoch": {
            **_manifest_identity(manifest_snapshot),
            "artifact_sha256": str(manifest.get("artifact_sha256") or ""),
        },
        "output_directory": str(output_directory),
        "files": {
            name: {"size_bytes": len(data), "sha256": _sha256(data), "mode": 0o600}
            for name, data in sorted(files.items())
        },
        "execution_authorization": "not_granted",
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def materialize_fresh_deploy_environment(
    *,
    manifest_path: str | Path,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    require_empty_roots: bool = True,
    created_ts_ns: int | None = None,
) -> Path:
    """Publish the complete environment directory atomically, or verify reuse."""

    source = _absolute_no_symlink(
        manifest_path, label="fresh-deploy manifest", require_exists=True
    )
    manifest_snapshot = read_stable_file(
        source,
        label="fresh-deploy manifest",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )
    manifest = load_fresh_deploy_epoch(
        source,
        require_empty_roots=require_empty_roots,
        snapshot=manifest_snapshot,
    )
    output = _absolute_no_symlink(
        output_directory, label="fresh-deploy environment directory", require_exists=False
    )
    _refuse_evidence_path_overlap(output, manifest)
    files = expected_environment_files(manifest)
    created = time.time_ns() if created_ts_ns is None else int(created_ts_ns)
    if created <= int(manifest.get("created_ts_ns") or 0):
        raise ValueError("fresh-deploy environment must be created after its epoch manifest")
    receipt = _receipt_payload(
        manifest_snapshot=manifest_snapshot,
        manifest=manifest,
        output_directory=output,
        files=files,
        created_ts_ns=created,
    )
    if output.exists():
        verify_fresh_deploy_environment(
            manifest_path=source,
            output_directory=output,
            require_empty_roots=require_empty_roots,
        )
        return output / RECEIPT_NAME

    parent = _absolute_no_symlink(
        output.parent, label="fresh-deploy environment parent", require_exists=True
    )
    temporary = parent / f".{output.name}.{os.getpid()}.{created}.tmp"
    if os.path.lexists(temporary):
        raise FileExistsError(f"fresh-deploy environment temporary path exists: {temporary}")
    try:
        os.mkdir(temporary, 0o700)
        os.chmod(temporary, 0o700)
        for name, data in sorted(files.items()):
            _write_exclusive(temporary / name, data)
        _write_exclusive(temporary / RECEIPT_NAME, canonical_json(receipt) + b"\n")
        descriptor = os.open(str(temporary), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        rename_noreplace(
            temporary,
            output,
            label="fresh-deploy environment directory",
        )
        descriptor = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verify_fresh_deploy_environment(
        manifest_path=source,
        output_directory=output,
        require_empty_roots=require_empty_roots,
    )
    return output / RECEIPT_NAME


def _strict_json(data: bytes, *, path: Path) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in values:
            if key in output:
                raise ValueError(f"fresh-deploy environment receipt repeats JSON key {key!r}")
            output[key] = value
        return output

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read fresh-deploy environment receipt: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("fresh-deploy environment receipt must contain one object")
    return value


def verify_fresh_deploy_environment(
    *,
    manifest_path: str | Path,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    require_empty_roots: bool = False,
    manifest_snapshot: StableFileSnapshot | None = None,
) -> dict[str, Any]:
    """Recompute and verify every fragment byte and its source-bound receipt."""

    source = _absolute_no_symlink(
        manifest_path, label="fresh-deploy manifest", require_exists=True
    )
    if manifest_snapshot is None:
        manifest_snapshot = read_stable_file(
            source,
            label="fresh-deploy manifest",
            require_mode=0o600,
            require_owner=True,
            require_single_link=True,
        )
    elif manifest_snapshot.path != source:
        raise ValueError("fresh-deploy manifest snapshot path differs")
    manifest = load_fresh_deploy_epoch(
        source,
        require_empty_roots=require_empty_roots,
        snapshot=manifest_snapshot,
    )
    output = _absolute_no_symlink(
        output_directory, label="fresh-deploy environment directory", require_exists=True
    )
    _refuse_evidence_path_overlap(output, manifest)
    metadata = output.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise ValueError("fresh-deploy environment directory must be owner-owned mode 0700")
    expected_files = expected_environment_files(manifest)
    expected_names = set(expected_files) | {RECEIPT_NAME}
    observed_names = {item.name for item in output.iterdir()}
    if observed_names != expected_names:
        raise ValueError("fresh-deploy environment directory has unexpected or missing files")
    for name, expected in expected_files.items():
        path = output / name
        fragment = read_stable_file(
            path,
            label=f"fresh-deploy environment fragment {name}",
            require_mode=0o600,
            require_owner=True,
            require_single_link=False,
        )
        if (
            fragment.mode != 0o600
            or fragment.uid != os.geteuid()
            or fragment.data != expected
        ):
            raise ValueError(f"fresh-deploy environment fragment changed: {name}")
    receipt_path = output / RECEIPT_NAME
    receipt_snapshot = read_stable_file(
        receipt_path,
        label="fresh-deploy environment receipt",
        require_mode=0o600,
        require_owner=True,
        require_single_link=False,
    )
    receipt = _strict_json(receipt_snapshot.data, path=receipt_path)
    expected_fields = {
        "schema_version",
        "kind",
        "validator",
        "created_ts_ns",
        "fresh_deploy_epoch",
        "output_directory",
        "files",
        "execution_authorization",
        "artifact_sha256",
    }
    if set(receipt) != expected_fields:
        raise ValueError("fresh-deploy environment receipt has unexpected or missing fields")
    source_identity_value = receipt.get("fresh_deploy_epoch")
    if not isinstance(source_identity_value, Mapping):
        raise ValueError("fresh-deploy environment receipt lacks its source identity")
    source_identity = cast(Mapping[str, Any], source_identity_value)
    expected_source = {
        **_manifest_identity(manifest_snapshot),
        "artifact_sha256": str(manifest.get("artifact_sha256") or ""),
    }
    expected_file_receipts = {
        name: {"size_bytes": len(data), "sha256": _sha256(data), "mode": 0o600}
        for name, data in sorted(expected_files.items())
    }
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("kind") != KIND
        or receipt.get("validator") != VALIDATOR
        or int(receipt.get("created_ts_ns") or 0) <= 0
        or dict(source_identity) != expected_source
        or receipt.get("output_directory") != str(output)
        or receipt.get("files") != expected_file_receipts
        or receipt.get("execution_authorization") != "not_granted"
        or receipt.get("artifact_sha256") != _self_hash(receipt)
    ):
        raise ValueError("fresh-deploy environment receipt is invalid")
    final_manifest = read_stable_file(
        source,
        label="fresh-deploy manifest",
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
    )
    if final_manifest != manifest_snapshot:
        raise RuntimeError("fresh-deploy manifest changed during environment verification")
    if {item.name for item in output.iterdir()} != expected_names:
        raise RuntimeError("fresh-deploy environment directory changed during verification")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize or verify exact fresh-deploy systemd environment files"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    materialize = commands.add_parser(
        "materialize",
        help="publish the environment files while all fresh roots are still empty",
    )
    materialize.add_argument("--manifest", type=Path, required=True)
    materialize.add_argument(
        "--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY
    )
    verify = commands.add_parser("verify", help="source-reopen the installed files")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument(
        "--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY
    )
    verify.add_argument(
        "--require-empty-roots",
        action="store_true",
        help="pre-start check; omit after the owners have populated the fresh roots",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "materialize":
        receipt_path = materialize_fresh_deploy_environment(
            manifest_path=args.manifest,
            output_directory=args.output_directory,
            require_empty_roots=True,
        )
        print(str(receipt_path))
        return 0
    receipt = verify_fresh_deploy_environment(
        manifest_path=args.manifest,
        output_directory=args.output_directory,
        require_empty_roots=bool(args.require_empty_roots),
    )
    print(
        json.dumps(
            {
                "artifact_sha256": receipt["artifact_sha256"],
                "output_directory": receipt["output_directory"],
                "status": "verified",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OUTPUT_DIRECTORY",
    "RECEIPT_NAME",
    "expected_environment_files",
    "materialize_fresh_deploy_environment",
    "render_unit_environment",
    "verify_fresh_deploy_environment",
]
