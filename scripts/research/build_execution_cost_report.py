#!/usr/bin/env python3
"""Build the measured per-fill execution-cost report from one account root.

Per-fill effective spread, price impact, and realized spread anchored to the
decision-time midpoint, plus a notional-weighted summary. The decomposition
lives in liquidity_migration/execution_cost_model.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.account_kernel import read_account_journal  # noqa: E402
from liquidity_migration.account_contracts import AccountEventType  # noqa: E402
from liquidity_migration.execution_cost_model import (  # noqa: E402
    build_fill_cost_rows,
    cost_model_summary,
)
from liquidity_migration.trade_diagnostics import (  # noqa: E402
    load_post_fill_markouts,
    load_required_book_contexts,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-root", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="Optional parquet output for the per-fill rows.")
    args = parser.parse_args(argv)

    events = read_account_journal(args.account_root.expanduser().resolve(), verify=True)
    contexts, _lookup = load_required_book_contexts(args.capture_root.expanduser().resolve(), events)
    _schedules, observations, _markout_lookup = load_post_fill_markouts(
        args.capture_root.expanduser().resolve(), events
    )
    rows = build_fill_cost_rows(events, contexts, observations)
    total_fills = sum(1 for event in events if event.event_type == AccountEventType.FILL.value)
    summary = cost_model_summary(rows, total_fill_events=total_fills)
    if args.out is not None:
        out = args.out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        rows.write_parquet(out, compression="zstd", statistics=True)
        summary["rows_parquet"] = str(out)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
