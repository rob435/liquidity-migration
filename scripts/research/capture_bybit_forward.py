#!/usr/bin/env python3
"""The Bybit market recorder lives in `market_tape`; this path keeps its old command line working.

`python -m market_tape record --config deploy/capture/bybit-linear.toml --root ROOT`
is the same recorder driven by a config file. This entry point translates the
old flags into that config so an older unit line still starts the new code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_tape.config import (  # noqa: E402
    CaptureConfig,
    Feed,
    StorageSettings,
    Tier,
    Universe,
    VenueSettings,
    validate_symbols,
)
from market_tape.record import run  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--symbols-file", type=Path)
    result.add_argument("--symbols", nargs="*", default=())
    result.add_argument("--wide-universe", choices=("linear-usdt",), default=None)
    result.add_argument("--deep-funding-bp", type=float, default=10.0)
    result.add_argument("--depth", type=int, default=50)
    result.add_argument("--segment-max-mb", type=float, default=64.0)
    result.add_argument("--fsync-every-records", type=int, default=1_000)
    result.add_argument("--retention-days", type=int, default=30)
    result.add_argument("--max-disk-gb", type=float, default=60.0)
    result.add_argument("--min-free-disk-gb", type=float, default=25.0)
    result.add_argument("--queue-frames", type=int, default=32_768)
    result.add_argument("--topics-per-connection", type=int, default=150)
    result.add_argument("--status-interval-seconds", type=float, default=30.0)
    result.add_argument("--ws-url", default=None)
    result.add_argument("--rest-base", default=None)
    return result


def config_from_args(args: argparse.Namespace) -> CaptureConfig:
    deep_feeds = (Feed("book", str(args.depth)), Feed("book", "1"), Feed("trades"), Feed("ticker"), Feed("liquidations"))
    wide_feeds = (Feed("book", "1"), Feed("trades"), Feed("ticker"), Feed("liquidations"))
    if args.symbols_file is not None:
        deep_universe = Universe("file", path=args.symbols_file.resolve())
    else:
        symbols = validate_symbols(args.symbols)
        if not symbols:
            raise SystemExit("capture needs --symbols-file or --symbols")
        deep_universe = Universe("symbols", symbols=symbols)
    tiers = [Tier("deep", deep_feeds, deep_universe)]
    if args.wide_universe is not None:
        if args.deep_funding_bp > 0:
            tiers.append(
                Tier(
                    "crowded",
                    (Feed("book", str(args.depth)),),
                    Universe(
                        "funding_below",
                        threshold_bp=args.deep_funding_bp,
                        sticky_days=2,
                        quote="USDT",
                        exclude_tiers=("deep",),
                    ),
                )
            )
        tiers.append(Tier("wide", wide_feeds, Universe("listed", quote="USDT", exclude_tiers=("deep",))))
    return CaptureConfig(
        venue=VenueSettings("bybit", "linear", ws_url=args.ws_url, rest_url=args.rest_base),
        storage=StorageSettings(
            root=args.root,
            segment_max_mb=args.segment_max_mb,
            fsync_every_records=args.fsync_every_records,
            retention_days=args.retention_days,
            max_disk_gb=args.max_disk_gb,
            min_free_disk_gb=args.min_free_disk_gb,
            queue_frames=args.queue_frames,
            status_interval_seconds=args.status_interval_seconds,
        ),
        tiers=tuple(tiers),
        topics_per_connection=args.topics_per_connection,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for name in (
        "segment_max_mb",
        "fsync_every_records",
        "retention_days",
        "max_disk_gb",
        "min_free_disk_gb",
        "queue_frames",
        "topics_per_connection",
        "status_interval_seconds",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    return run(config_from_args(args), root=args.root)


if __name__ == "__main__":
    raise SystemExit(main())
