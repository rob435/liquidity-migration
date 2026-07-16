#!/usr/bin/env python3
"""Build a read-only command-level trade diagnostic artifact pair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from liquidity_migration.account_kernel import read_account_journal
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.trade_diagnostics import (
    build_execution_diagnostics,
    build_trade_diagnostic_manifest,
    load_required_book_contexts,
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("diagnostic manifest write made no progress")
        offset += written


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        _write_all(descriptor, canonical_json(payload) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="permit an exploratory dirty-source build; dirty state remains in the manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    account_root = args.account_root.expanduser().resolve()
    capture_root = args.capture_root.expanduser().resolve()
    out = args.out.expanduser().resolve()
    dirty = bool(_git("status", "--porcelain=v1"))
    if dirty and not args.allow_dirty:
        raise RuntimeError("trade diagnostics require a clean source tree; use --allow-dirty only for exploration")
    if out.exists():
        raise FileExistsError(f"diagnostic output already exists: {out}")

    events = read_account_journal(account_root, verify=True)
    contexts, lookup = load_required_book_contexts(capture_root, events)
    diagnostics = build_execution_diagnostics(events, contexts)
    commit = _git("rev-parse", "HEAD")
    manifest = build_trade_diagnostic_manifest(
        events=events,
        book_contexts=contexts,
        diagnostics=diagnostics,
        code_commit=commit,
        account_root=str(account_root),
        capture_root=str(capture_root),
        context_lookup=lookup,
    )
    manifest["source"]["git_dirty"] = dirty

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    staging.mkdir(mode=0o700)
    try:
        parquet = staging / "execution_tca.parquet"
        diagnostics.write_parquet(parquet, compression="zstd", statistics=True)
        os.chmod(parquet, 0o600)
        manifest["files"] = {
            "execution_tca.parquet": {
                "bytes": parquet.stat().st_size,
                "sha256": _sha256(parquet),
            }
        }
        manifest.pop("manifest_payload_sha256", None)
        manifest["manifest_payload_sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
        _write_manifest(staging / "manifest.json", manifest)
        directory = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.replace(staging, out)
        parent = os.open(out.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
