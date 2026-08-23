#!/usr/bin/env python3
"""Sweep resting-quote policies over a recorded tape and say which one is cheapest.

Every arm is replayed against the same tape through
``liquidity_migration.research.execution.quote_lab.shadow``, which respects
queue position and reports two fill bounds (conservative: cancels happen
behind us; optimistic: ahead). No orders are placed and no venue is touched,
so a run is deterministic and repeatable.

The comparison is **paired at the decision instant**: every attempt yields
both what resting cost and what crossing would have cost on the same book, so
the difference carries none of the variance of two separately sampled arms.

Cost, per side, positive when it costs us:

- rested and filled: the resting price against the decision mid, plus the
  maker fee. A rest that fills earns the half-spread, so this is normally
  negative before fees.
- rested and missed: crossing at the *terminal* quote plus the taker fee —
  the drift while waiting is what a miss actually costs.
- crossed: crossing at the *decision* quote plus the taker fee.

Fees are per symbol. Bybit bills 11.0 bp on some contracts against 5.5, and a
sweep that assumed one rate would rank the arms wrong on the other.

Usage:
  python scripts/research/sweep_quote_arms.py --tape DIR --ticks ticks.json \
      --output receipt.json
"""

from __future__ import annotations

import argparse
import io
import json
import math
import statistics as st
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from liquidity_migration.research.execution.quote_lab.shadow import (  # noqa: E402
    ShadowOutcome,
    ShadowPolicy,
    run_shadow_attempts,
)

# What the venue bills, per symbol, when it is not the usual rate.
TAKER_FEE_BP = 5.5
MAKER_FEE_BP = 2.0
DEAR_CONTRACTS = {"CAPUSDT": (11.0, 4.0), "BMTUSDT": (11.0, 4.0)}

# Only the fields the replay reads. The tape carries a dozen more per record
# and keeping them costs gigabytes on a symbol with a busy book.
_KEEP = (
    "kind",
    "symbol",
    "local_receive_ts_ns",
    "bids",
    "asks",
    "price",
    "qty",
    "side",
    # The mirror marks a book unhealthy on either of these and refuses deltas
    # until the next clean snapshot. Dropping them would silently fill against
    # a book that had already lost its place.
    "sequence_gap",
    "restart_snapshot",
)
_WANTED_KINDS = frozenset({"orderbook_snapshot", "orderbook_delta", "public_trade"})


def fees_for(symbol: str) -> tuple[float, float]:
    return DEAR_CONTRACTS.get(symbol, (TAKER_FEE_BP, MAKER_FEE_BP))


def _lines(segment: Path) -> Iterator[str]:
    """Tape lines, through the zstd binary rather than a python decoder.

    The repository pins its dependencies exactly and `zstandard` is not among
    them; adding a pin to read an archive is a worse trade than a pipe.
    """

    if segment.suffix != ".zst":
        with segment.open("r", encoding="utf-8") as plain:
            yield from plain
        return
    with subprocess.Popen(
        ["zstd", "-dcq", str(segment)], stdout=subprocess.PIPE
    ) as proc:
        assert proc.stdout is not None
        yield from io.TextIOWrapper(proc.stdout, encoding="utf-8")


def read_tape(symbol_dir: Path, *, max_records: int) -> list[dict[str, Any]]:
    """One symbol's tape from its first book snapshot, trimmed to what the
    replay reads.

    Starting at the snapshot rather than at the first byte is what makes the
    record budget mean anything: a recorder that attached mid-stream writes
    deltas the mirror refuses to apply, and a tape read from the top can spend
    its whole budget on a book that is never healthy.
    """

    out: list[dict[str, Any]] = []
    started = False
    segments = sorted(symbol_dir.glob("segment-*.jsonl.zst")) or sorted(
        symbol_dir.glob("segment-*.jsonl")
    )
    for segment in segments:
        for line in _lines(segment):
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record.get("kind")
            if kind not in _WANTED_KINDS:
                continue
            if not started:
                if kind != "orderbook_snapshot":
                    continue
                started = True
            out.append({k: record[k] for k in _KEEP if k in record})
            if len(out) >= max_records:
                return out
    return out


def px_cost_bp(side: str, mid: float, price: float) -> float:
    signed = (price - mid) / mid
    return (signed if side == "Buy" else -signed) * 1e4


def graded(outcome: ShadowOutcome, *, optimistic: bool, symbol: str) -> tuple[float, float] | None:
    """(what resting cost, what crossing would have cost) for one attempt."""

    taker_fee, maker_fee = fees_for(symbol)
    bid, ask = outcome.decision_bid, outcome.decision_ask
    if not (bid > 0.0 and ask > bid):
        return None
    mid = (bid + ask) / 2.0
    crossed = px_cost_bp(outcome.side, mid, ask if outcome.side == "Buy" else bid) + taker_fee

    filled = outcome.filled_optimistic if optimistic else outcome.filled_conservative
    if filled:
        if not outcome.placed_prices:
            return None
        rested = px_cost_bp(outcome.side, mid, outcome.placed_prices[-1]) + maker_fee
        return rested, crossed
    if outcome.terminal_bid is None or outcome.terminal_ask is None:
        return None
    if not (outcome.terminal_bid > 0.0 and outcome.terminal_ask > outcome.terminal_bid):
        return None
    away = outcome.terminal_ask if outcome.side == "Buy" else outcome.terminal_bid
    return px_cost_bp(outcome.side, mid, away) + taker_fee, crossed


def paired(diffs: list[float]) -> dict[str, float | None]:
    if len(diffs) < 2:
        return {"n": len(diffs), "mean": None, "t": None}
    mean = st.fmean(diffs)
    se = st.stdev(diffs) / math.sqrt(len(diffs))
    return {"n": len(diffs), "mean": mean, "t": (mean / se) if se > 0 else None}


ARMS: list[dict[str, Any]] = [
    {"placement": placement, "chase_ticks": chase, "timeout_seconds": timeout}
    for placement in ("join", "improve")
    for chase in (0, 1, 2, 4)
    for timeout in (60.0, 120.0, 180.0)
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", required=True, help="directory of per-symbol tape folders")
    parser.add_argument("--ticks", required=True, help="json map of symbol -> tick size")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-records-per-symbol", type=int, default=400_000)
    parser.add_argument("--attempt-interval-seconds", type=float, default=30.0)
    parser.add_argument("--max-reprices", type=int, default=4)
    args = parser.parse_args(argv)

    ticks: dict[str, float] = json.loads(Path(args.ticks).read_text())
    tape_root = Path(args.tape)
    symbol_dirs = sorted(d for d in tape_root.iterdir() if d.is_dir() and d.name in ticks)
    if not symbol_dirs:
        raise SystemExit(f"no tape folders under {tape_root} match the tick map")

    # arm key -> bound -> list of paired differences, and the raw components
    diffs: dict[str, dict[str, list[float]]] = {}
    rested_costs: dict[str, dict[str, list[float]]] = {}
    crossed_costs: dict[str, dict[str, list[float]]] = {}
    fills: dict[str, dict[str, list[int]]] = {}
    per_symbol: dict[str, dict[str, dict[str, float | None]]] = {}

    for symbol_dir in symbol_dirs:
        symbol = symbol_dir.name
        records = read_tape(symbol_dir, max_records=args.max_records_per_symbol)
        print(f"{symbol}: {len(records)} records", file=sys.stderr, flush=True)
        if not records:
            continue
        for arm in ARMS:
            key = f"{arm['placement']}-c{arm['chase_ticks']}-t{arm['timeout_seconds']:.0f}"
            policy = ShadowPolicy(
                side="Buy",
                placement=arm["placement"],
                tick_size=ticks[symbol],
                timeout_seconds=arm["timeout_seconds"],
                chase_ticks=arm["chase_ticks"],
                max_reprices=args.max_reprices,
            )
            outcomes = run_shadow_attempts(
                records,
                policy,
                args.attempt_interval_seconds,
                tick_size=ticks[symbol],
                sides=("Buy", "Sell"),
            )
            for bound, optimistic in (("conservative", False), ("optimistic", True)):
                pairs = [
                    g for o in outcomes if (g := graded(o, optimistic=optimistic, symbol=symbol))
                ]
                if not pairs:
                    continue
                d = diffs.setdefault(key, {}).setdefault(bound, [])
                d.extend(rest - cross for rest, cross in pairs)
                rested_costs.setdefault(key, {}).setdefault(bound, []).extend(r for r, _ in pairs)
                crossed_costs.setdefault(key, {}).setdefault(bound, []).extend(c for _, c in pairs)
                got = [
                    int(o.filled_optimistic if optimistic else o.filled_conservative)
                    for o in outcomes
                ]
                fills.setdefault(key, {}).setdefault(bound, []).extend(got)
                if bound == "conservative":
                    per_symbol.setdefault(symbol, {})[key] = paired(
                        [rest - cross for rest, cross in pairs]
                    )

    table = []
    for key in sorted(diffs):
        row: dict[str, Any] = {"arm": key}
        for bound in ("conservative", "optimistic"):
            if bound not in diffs[key]:
                continue
            row[bound] = paired(diffs[key][bound])
            row[bound]["rest_bp"] = st.fmean(rested_costs[key][bound])
            row[bound]["cross_bp"] = st.fmean(crossed_costs[key][bound])
            row[bound]["fill_rate"] = st.fmean(fills[key][bound])
        table.append(row)

    payload = {
        "kind": "liquidity_migration_quote_arm_sweep",
        "tape": str(tape_root),
        "symbols": [d.name for d in symbol_dirs],
        "attempt_interval_seconds": args.attempt_interval_seconds,
        "max_reprices": args.max_reprices,
        "max_records_per_symbol": args.max_records_per_symbol,
        "fees": {"taker_bp": TAKER_FEE_BP, "maker_bp": MAKER_FEE_BP, "dear": DEAR_CONTRACTS},
        "arms": table,
        "per_symbol_conservative": per_symbol,
    }
    Path(args.output).write_text(json.dumps(payload, indent=1, sort_keys=True))
    best = min(
        (r for r in table if r.get("conservative", {}).get("mean") is not None),
        key=lambda r: r["conservative"]["mean"],
        default=None,
    )
    if best:
        c = best["conservative"]
        print(
            f"cheapest (conservative): {best['arm']}  "
            f"rest {c['rest_bp']:.2f} vs cross {c['cross_bp']:.2f}  "
            f"diff {c['mean']:+.2f} bp  t {c['t']:+.2f}  fill {100 * c['fill_rate']:.0f}%  n {c['n']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
