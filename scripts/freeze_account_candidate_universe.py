#!/usr/bin/env python3
"""Freeze the pre-signal LONG/CONT population from public Bybit demo data."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_candidate_universe import (  # noqa: E402
    build_candidate_universe_artifact,
    write_candidate_universe,
)
from liquidity_migration.bybit_market_data import (  # noqa: E402
    BybitMarketData,
    BybitRestRateLimiter,
)
from liquidity_migration.continuous_demo import (  # noqa: E402
    ContinuousDemoCycleConfig,
    apply_continuous_demo_profile,
)
from liquidity_migration.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--long-universe-superset-size", type=int, default=120)
    parser.add_argument(
        "--max-public-requests-per-second",
        type=int,
        default=5,
        help="shared public api-demo rate ceiling (default: 5)",
    )
    args = parser.parse_args(argv)
    if args.long_universe_superset_size <= 0:
        parser.error("--long-universe-superset-size must be positive")
    if args.max_public_requests_per_second <= 0:
        parser.error("--max-public-requests-per-second must be positive")

    long_config = replace(
        LongNativeDemoCycleConfig(),
        universe_superset_size=args.long_universe_superset_size,
    )
    continuous_config = apply_continuous_demo_profile(ContinuousDemoCycleConfig())
    limiter = BybitRestRateLimiter(
        max_requests=args.max_public_requests_per_second,
        per_seconds=1.0,
    )
    market = BybitMarketData(demo=True, rate_limiter=limiter)
    started_ts_ns = time.time_ns()
    instrument_rows = market.get_instruments_info(require_complete=True)
    ticker_rows = market.get_tickers()
    completed_ts_ns = time.time_ns()
    payload = build_candidate_universe_artifact(
        instrument_rows,
        ticker_rows,
        snapshot_ts_ns=completed_ts_ns,
        long_config=long_config,
        continuous_config=continuous_config,
    )
    # The two endpoint calls cannot be literally simultaneous. Bind their
    # bounded acquisition interval before recomputing the artifact self-hash.
    payload["snapshot_started_ts_ns"] = started_ts_ns
    payload["snapshot_completed_ts_ns"] = completed_ts_ns
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    output = write_candidate_universe(args.output, payload)
    print(json.dumps({
        "status": "candidate_universe_frozen",
        "path": str(output),
        "symbols": payload["symbol_count"],
        "artifact_sha256": payload["artifact_sha256"],
        "snapshot_started_ts_ns": started_ts_ns,
        "snapshot_completed_ts_ns": completed_ts_ns,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
