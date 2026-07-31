"""Run the paper target mirror: one fleet decides, both execute.

Reads the demo fleet's hash-chained target-scheduling capture and republishes
each published request onto the paper route. Target-only and venue-free; it does
not touch the demo account journal or inbox.

It runs privileged because the demo capture tape is ``0600 root:root`` and the
paper owner is unprivileged; queued files are handed to the inbox's own uid/gid.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from liquidity_migration.account.account_owner_health import read_account_owner_health
from liquidity_migration.account.account_route import derive_account_route, ensure_account_route
from liquidity_migration.account.account_service import AccountIntentInbox
from liquidity_migration.core.env_flags import explicitly_false_or_unset
from liquidity_migration.core.logging_setup import ensure_default_log_handler
from liquidity_migration.runtime.paper_target_mirror import PaperTargetMirror, PaperTargetMirrorError

_logger = logging.getLogger(__name__)

SCALE_MODES = ("verbatim", "equity_ratio")

_PRIVATE_EXCHANGE_ENVIRONMENT_KEYS = (
    "BYBIT_DEMO_API_KEY",
    "BYBIT_DEMO_API_SECRET",
    "BYBIT_REAL_API_KEY",
    "BYBIT_REAL_API_SECRET",
)


def require_mirror_runtime_isolation(environment=None) -> None:
    import os

    env = os.environ if environment is None else environment
    present = [key for key in _PRIVATE_EXCHANGE_ENVIRONMENT_KEYS if env.get(key)]
    if present:
        raise RuntimeError(
            "paper target mirror received private exchange credentials: " + ", ".join(present)
        )
    if not explicitly_false_or_unset(env.get("REAL_MONEY")):
        raise RuntimeError("paper target mirror requires REAL_MONEY=false or unset")


def resolve_scale(
    *,
    mode: str,
    demo_account_root: str | Path,
    paper_account_root: str | Path,
) -> float:
    """Mirror scale for this poll.

    ``verbatim`` (1.0, the default) is the only setting under which a difference
    between the two books is attributable to execution. ``equity_ratio`` sizes
    paper as a proportional copy of demo at its own account size — right for a
    capacity question, wrong for a fill-model one.
    """

    if mode == "verbatim":
        return 1.0
    if mode != "equity_ratio":
        raise ValueError(f"unknown mirror scale mode {mode!r}")
    demo = read_account_owner_health(demo_account_root)
    paper = read_account_owner_health(paper_account_root)
    if not (demo.equity_usdt > 0.0) or not (paper.equity_usdt > 0.0):
        raise PaperTargetMirrorError(
            "equity_ratio scaling needs a positive equity from both owners"
        )
    return float(paper.equity_usdt) / float(demo.equity_usdt)


def main(argv: list[str] | None = None) -> int:
    ensure_default_log_handler()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-capture-tape", required=True)
    parser.add_argument("--demo-account-root", required=True)
    parser.add_argument("--account-root", required=True, help="Paper account root.")
    parser.add_argument("--inbox-root", required=True, help="Paper intent inbox root.")
    parser.add_argument("--cursor-path", required=True)
    parser.add_argument("--account-id", default="bybit-paper-unified")
    parser.add_argument(
        "--sleeve",
        action="append",
        default=None,
        help="Sleeve to mirror; repeatable. Defaults to carry only.",
    )
    parser.add_argument("--scale-mode", choices=SCALE_MODES, default="verbatim")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0.0:
        parser.error("--poll-seconds must be positive")
    require_mirror_runtime_isolation()

    requested = derive_account_route(
        account_id=args.account_id,
        environment="paper",
        account_root=args.account_root,
        inbox_root=args.inbox_root,
    )
    # No owner lease: the mirror only writes to the inbox, so it must not contend
    # with the owner for the journal lease.
    route = ensure_account_route(
        account_id=requested.account_id,
        environment=requested.environment,
        account_root=requested.account_root,
        inbox_root=requested.inbox_root,
    )
    mirror = PaperTargetMirror(
        tape_path=args.demo_capture_tape,
        route=route,
        inbox=AccountIntentInbox(route),
        sleeves=args.sleeve or ("carry",),
        cursor_path=args.cursor_path,
    )
    _logger.info(
        "paper target mirror started sleeves=%s scale_mode=%s tape=%s",
        ",".join(mirror.sleeves),
        args.scale_mode,
        args.demo_capture_tape,
    )
    try:
        while True:
            try:
                scale = resolve_scale(
                    mode=args.scale_mode,
                    demo_account_root=args.demo_account_root,
                    paper_account_root=args.account_root,
                )
                report = mirror.poll(scale=scale)
            except PaperTargetMirrorError:
                # A tape reset or unreadable cursor must not be retried around:
                # publishing under either replays history onto a live book.
                raise
            except Exception:  # noqa: BLE001 - a transient read must not end the mirror
                _logger.exception("paper target mirror poll failed; retrying")
            else:
                if not report.healthy:
                    _logger.warning("paper target mirror degraded: %s", report.detail)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
