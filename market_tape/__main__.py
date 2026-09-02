"""`python -m market_tape`: record a venue, ship the archives, read them back.

```text
record      --config PATH [--root PATH]          run a recorder until SIGTERM
check       --config PATH                        validate a config and print the static tiers
pack        (see `pack --help`)                  pack finished hours and upload them
hours       SOURCE                               list the hours a source holds
rows        SOURCE --hours FROM[..TO] [...]      print rows as JSON lines
bars        SOURCE --hours FROM[..TO] --interval 60 --out bars.parquet
book        SOURCE --hour H --symbol S [--at NS] the rebuilt book at a moment
```

SOURCE is a recorder root on a host, a directory of hour archives laid out
like the Drive (`YYYY/MM/DD/<day>T<HH>Z.tar`), or `rclone:<remote:path>` to
read the Drive itself through a local cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _hours_argument(text: str) -> tuple[str, str]:
    start, separator, end = text.partition("..")
    return (start, end) if separator else (start, start)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market_tape", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record", help="run a recorder")
    record.add_argument("--config", type=Path, required=True)
    record.add_argument("--root", type=Path, default=None, help="storage root; overrides storage.root")

    check = commands.add_parser("check", help="validate a capture config without touching the network")
    check.add_argument("--config", type=Path, required=True)

    commands.add_parser("pack", help="pack finished hours and upload them", add_help=False)

    hours = commands.add_parser("hours", help="list the hours a source holds")
    hours.add_argument("source")
    hours.add_argument("--cache", type=Path, default=None, help="local cache for archives read from rclone")

    rows = commands.add_parser("rows", help="print rows as JSON lines")
    rows.add_argument("source")
    rows.add_argument("--hours", required=True, help="FROM[..TO], hours as YYYY-MM-DDTHH, TO exclusive")
    rows.add_argument("--symbols", nargs="*", default=None)
    rows.add_argument("--kinds", nargs="*", default=None)
    rows.add_argument("--limit", type=int, default=None)
    rows.add_argument("--cache", type=Path, default=None)

    bars = commands.add_parser("bars", help="fixed-interval bars from trades, books, tickers, liquidations")
    bars.add_argument("source")
    bars.add_argument("--hours", required=True)
    bars.add_argument("--interval", type=float, default=60.0, help="bar length in seconds")
    bars.add_argument("--symbols", nargs="*", default=None)
    bars.add_argument("--out", type=Path, required=True, help=".parquet or .csv")
    bars.add_argument("--cache", type=Path, default=None)

    book = commands.add_parser("book", help="the rebuilt book at one moment")
    book.add_argument("source")
    book.add_argument("--hour", required=True)
    book.add_argument("--symbol", required=True)
    book.add_argument("--at", type=int, default=None, help="local receive nanoseconds; default: end of the hour")
    book.add_argument("--depth", type=int, default=None, help="which book to rebuild (1, 50, 1000...); default: the first depth seen")
    book.add_argument("--levels", type=int, default=5)
    book.add_argument("--cache", type=Path, default=None)

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "pack":
        from market_tape.pack import main as pack_main

        return pack_main(argv[1:])
    args = parser.parse_args(argv)

    if args.command == "record":
        from market_tape.config import load_config
        from market_tape.record import run

        return run(load_config(args.config), root=args.root)

    if args.command == "check":
        from market_tape.config import load_config
        from market_tape.venues import adapter_for

        config = load_config(args.config)
        adapter = adapter_for(config.venue.name, market=config.venue.market, ws_url=config.venue.ws_url, rest_url=config.venue.rest_url)
        for tier in config.tiers:
            adapter.validate_feeds(tier.feeds)
            print(f"tier {tier.name}: universe={tier.universe.kind} feeds={' '.join(feed.text for feed in tier.feeds)}")
        print(f"venue={config.venue.name} market={config.venue.market} ws={adapter.ws_url} rest={adapter.rest_url}")
        print(f"root={config.storage.root} snapshots={config.snapshot_cadence} topics_per_connection={config.topics_per_connection}")
        return 0

    from market_tape.load import hour_range, iter_rows, open_source

    source = open_source(args.source, cache_dir=args.cache)
    if args.command == "hours":
        for hour in source.hours():
            print(hour)
        return 0

    if args.command == "rows":
        start, end = _hours_argument(args.hours)
        count = 0
        for row in iter_rows(source, hour_range(start, end), symbols=args.symbols, kinds=args.kinds, typed=False):
            sys.stdout.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            count += 1
            if args.limit is not None and count >= args.limit:
                break
        return 0

    if args.command == "bars":
        from market_tape.bars import build_bars

        start, end = _hours_argument(args.hours)
        frame = build_bars(iter_rows(source, hour_range(start, end), symbols=args.symbols), interval_seconds=args.interval)
        if args.out.suffix == ".csv":
            frame.write_csv(args.out)
        else:
            frame.write_parquet(args.out)
        print(f"wrote {frame.height} bars for {frame['symbol'].n_unique()} symbols to {args.out}")
        return 0

    if args.command == "book":
        from market_tape.book import Book

        state = Book()
        depth = args.depth
        for row in iter_rows(source, [args.hour], symbols=[args.symbol], kinds=["orderbook_snapshot", "orderbook_delta"]):
            if args.at is not None and row.local_receive_ts_ns > args.at:
                break
            if depth is None:
                depth = row.depth
            if row.depth != depth:
                continue
            state.apply(row)
        print(json.dumps(state.describe(levels=args.levels), indent=2, sort_keys=True))
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
