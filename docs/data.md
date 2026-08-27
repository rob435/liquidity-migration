# Data and time boundaries

Research datasets, live strategy state, and execution evidence are different
authorities. They must never be substituted for one another.

## Roots

The configured `DATA_ROOT` contains public market inputs, cached feature
material, strategy event tapes, cycle receipts, retirement registries, and
durable sleeve state. It contains no venue credentials and is not position or
P&L authority.

Research refreshes write run-scoped artifacts beneath
`reports/research-refresh/<run-id>/`:

- `manifest.json` freezes code, configuration, roots, and date window;
- `events.jsonl` records append-only step starts, failures, and completions;
- `logs/` contains append-only command output;
- `summary.<sha-prefix>.json` is an immutable content-addressed result card;
- `backtests/<venue>/` contains the selected sleeve reports.

The Rust state directory contains the execution WAL, reconciliation state, and
the account heartbeat. Only the Rust engine mutates that state or the venue.
Python reads the heartbeat through a stable-file snapshot and never parses the
WAL on a live decision path.

## Dataset families

Canonical public datasets are partitioned beneath their venue roots. The main
families are hourly/minute klines, funding, premium/mark/index price, open
interest, taker flow, positioning ratios, membership manifests, and raw trade
archives. Coverage varies by venue, symbol, and time; directory existence is
not evidence of a complete panel.

Use:

```bash
python -m liquidity_migration --data-root ROOT coverage
```

Before a claim, check the exact symbol/date intersection the calculation reads.
Count recursively: some datasets are keyed by `date=/symbol=/`, while others
are keyed directly by symbol. A non-recursive glob can report a populated root
as empty.

## Point-in-time membership

Universe membership is data, not a present-day symbol lookup. Historical
replay reads the membership manifest valid for each decision timestamp and
joins features backward in time. Delisted symbols remain in earlier frames;
new listings cannot leak into dates before their first eligible observation.

Manifest validation requires enough hourly bars for a claimed symbol/day and
rejects a partition whose declared date disagrees with its timestamps.
Cross-venue research is bounded by the verified intersection of both venues'
membership and data coverage.

## Timestamps

Python research and strategy timestamps are Unix milliseconds and end in
`_ms`. A kline `ts_ms` is its open; its close becomes actionable only after the
full interval. Funding `ts_ms` is the settlement instant and can be joined only
backward as-of.

Rust monotonic clocks are nanoseconds and end in `_ns`; their origin is local
to a process unless a field explicitly says `wall`. Venue timestamps and local
receive timestamps in Rust WAL records are the execution-latency evidence.
Target publication time and filesystem modification time are not.

For LONG, these clocks are deliberately separate:

| Field | Meaning |
| --- | --- |
| `signal_ts_ms` | Closed market-data boundary that caused the decision |
| request validity | Original interval in which the engine may enter; later cycles do not extend it |
| `entered_ts_ms` | First cycle observing a uniquely LONG-attributed venue holding; zero before that evidence |
| `max_hold_duration_ms` | Duration frozen with the request |
| `max_hold_deadline_ts_ms` | Attributed entry observation plus the frozen duration; zero before attribution |
| target evidence `activated_at_ns` | Durable local activation of exact target bytes, never a fill clock |

A request timestamp cannot start protection decay or maximum hold. An
account-global, manual, inherited, or shared position cannot start a sleeve's
fill clock. The Rust heartbeat exposes a configured sleeve name only when the
fill ledger has one owner and its signed quantity matches the venue quantity.

## Durable strategy artifacts

Absolute target publication is:

1. render and strictly parse deterministic bytes;
2. durably create `.target-book-objects/<sha256>.json`;
3. atomically replace the engine-visible target path;
4. append a hash-chained activation receipt for the same bytes.

No file means no new decision. An empty target array is an explicit flat
sleeve. A stale or malformed account heartbeat blocks additions and resizes;
CARRY may only remove already-expired requests from a previously verified
book.

LONG requested-book state, CARRY sizing anchors, CARRY early-exit state, and
Exodus open-short state use strict schemas and durable atomic replacement.
Corrupt state is unknown state and fails closed without advancing the active
book.

## Refresh workflow

Plan before mutation, then run with an explicit end date:

```bash
scripts/ops.sh research-refresh plan --end YYYY-MM-DD
scripts/ops.sh research-refresh run --end YYYY-MM-DD
```

A run ID may resume only a step whose exact command fingerprint succeeded and
whose expected artifacts still exist. Changed roots, windows, source commits,
or configuration are rejected under the same ID. Stable-overlap mismatches in
derived features fail closed; an explicit full rewrite is a visible recovery
choice, not an implicit fallback.

Research is offline: it has no private venue API, order route, or promotion
authority. Demo and funded execution quality comes from Rust WAL/fill/account
artifacts under the evidence policy in
[`research/governance.md`](research/governance.md).
