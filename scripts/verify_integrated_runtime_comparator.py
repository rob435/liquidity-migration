#!/usr/bin/env python3
"""Independently verify a final integrated comparator and freeze a compact receipt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration.artifact_snapshot import read_stable_file  # noqa: E402
from liquidity_migration.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.forward_epoch_start import (  # noqa: E402
    build_comparator_verification_receipt,
    load_integrated_comparator_receipt,
    validate_comparator_verification_payload,
    verify_integrated_comparator_files,
)


DEFAULT_COMPARATOR = (
    REPO
    / "reports/prospective-runtime-parity-execution-epoch-2026-07-18"
    / "runtime-parity/integrated-production-comparator/receipt.json"
)
DEFAULT_OUTPUT = DEFAULT_COMPARATOR.parent.parent / "integrated-production-comparator-verification.json"


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_create_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("comparator verification receipt write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name != "nt":
        directory = os.open(
            str(path.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparator-receipt", type=Path, default=DEFAULT_COMPARATOR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    head = _git("rev-parse", "HEAD")
    if _git("status", "--porcelain=v1"):
        raise RuntimeError("integrated comparator verification requires a clean checkout")
    comparator_path = args.comparator_receipt.expanduser().resolve(strict=True)
    payload, summary = load_integrated_comparator_receipt(
        comparator_path,
        expected_commit=head,
    )
    files = verify_integrated_comparator_files(
        payload,
        output_root=comparator_path.parent,
    )
    receipt = build_comparator_verification_receipt(
        created_ts_ns=time.time_ns(),
        comparator_summary=summary,
        file_verification=files,
    )
    output = args.out.expanduser().resolve(strict=False)
    data = canonical_json(receipt) + b"\n"
    _write_create_only(output, data)
    snapshot = read_stable_file(
        output,
        label="integrated comparator verification receipt",
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
        require_single_link=True,
        max_bytes=128 * 1024,
    )
    if snapshot.data != data:
        raise RuntimeError("integrated comparator verification receipt changed after publication")
    reopened = json.loads(snapshot.data)
    validate_comparator_verification_payload(reopened)
    print(
        json.dumps(
            {
                "status": "pass",
                "output": str(snapshot.path),
                "receipt_sha256": snapshot.sha256,
                "comparator_receipt_sha256": summary["sha256"],
                **files,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
