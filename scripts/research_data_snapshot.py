#!/usr/bin/env python3
"""Plan, capture, verify, or reconstruct a prospective research snapshot."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.research_data_snapshot import (  # noqa: E402
    DEFAULT_DATASETS,
    build_snapshot_plan,
    capture_snapshot,
    contract_sha256,
    extract_snapshot,
    parse_date,
    plan_payload,
    verify_snapshot,
)


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _common_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True, help="Mutable source data root.")
    parser.add_argument("--start", required=True, help="Inclusive raw-data date (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="Exclusive raw-data date (YYYY-MM-DD).")
    parser.add_argument(
        "--dataset",
        action="append",
        default=None,
        help="Top-level dataset; repeat to override the registered default set.",
    )


def _plan(args: argparse.Namespace):
    return build_snapshot_plan(
        args.root,
        start=parse_date(args.start),
        end=parse_date(args.end),
        datasets=tuple(args.dataset or DEFAULT_DATASETS),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Inventory paths/sizes without reading outcomes.")
    _common_plan_args(plan)

    capture = subparsers.add_parser("capture", help="Create and fully verify one immutable container.")
    _common_plan_args(capture)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--receipt", type=Path, required=True)
    capture.add_argument("--contract", type=Path, required=True)
    capture.add_argument("--code-commit", default=None, help="Defaults to the current exact Git HEAD.")
    capture.add_argument("--batch-size", type=int, default=500)
    capture.add_argument("--progress-every", type=int, default=10_000)

    verify = subparsers.add_parser("verify", help="Verify container, receipt, and all stored bytes.")
    verify.add_argument("--container", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--logical-only", action="store_true")
    verify.add_argument("--progress-every", type=int, default=10_000)

    extract = subparsers.add_parser("extract", help="Reconstruct into a new run-scoped root.")
    extract.add_argument("--container", type=Path, required=True)
    extract.add_argument("--receipt", type=Path, required=True)
    extract.add_argument("--output-root", type=Path, required=True)
    extract.add_argument("--reconstruction-receipt", type=Path, required=True)
    extract.add_argument("--progress-every", type=int, default=10_000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "plan":
        result = plan_payload(_plan(args))
    elif args.command == "capture":
        plan = _plan(args)
        result = capture_snapshot(
            plan,
            output=args.output,
            receipt_path=args.receipt,
            contract_sha256=contract_sha256(args.contract),
            code_commit=args.code_commit or _git_head(),
            batch_size=args.batch_size,
            progress_every=args.progress_every,
        )
    elif args.command == "verify":
        result = verify_snapshot(
            args.container,
            receipt_path=args.receipt,
            full_content=not args.logical_only,
            progress_every=args.progress_every,
        )
    elif args.command == "extract":
        result = extract_snapshot(
            args.container,
            receipt_path=args.receipt,
            output_root=args.output_root,
            reconstruction_receipt_path=args.reconstruction_receipt,
            progress_every=args.progress_every,
        )
    else:  # pragma: no cover - argparse owns the command set
        raise AssertionError(args.command)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
