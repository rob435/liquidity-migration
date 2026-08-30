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

Manifest validation requires at least 20 unique, aligned hour keys and one valid
OHLC row for a claimed symbol/day. Leading rows may keep prices null before a
midday listing; they state that the price was not yet known and do not count as
observed or executable bars. Validation rejects a partition whose declared date
disagrees with its timestamps.
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
| target capture `event_ts_ns` | LONG daemon event time bound to exact target bytes, never a fill clock or publication timestamp |

A request timestamp cannot start protection decay or maximum hold. An
account-global, manual, inherited, or shared position cannot start a sleeve's
fill clock. The Rust heartbeat exposes a configured sleeve name only when the
fill ledger has one owner and its signed quantity matches the venue quantity.

## Durable strategy artifacts

Every producer's absolute target publication is:

1. render and strictly parse deterministic bytes;
2. durably create `.target-book-objects/<sha256>.json`;
3. atomically replace the engine-visible target path.

After LONG or CARRY publishes through the strategy host, the hosted evidence
path appends a hash-chained target capture for the same bytes. Exodus uses its
own daemon instead: it records the current path, immutable-object path, and
content hash in its `exodus_cycles` row and writes no target-capture tape.

No file means no new decision. An empty target array is an explicit flat
sleeve. A stale or malformed account heartbeat blocks additions and resizes;
CARRY may only remove already-expired requests from a previously verified
book.

LONG requested-book state, CARRY sizing anchors, CARRY early-exit state, and
Exodus open-short state use strict schemas and durable atomic replacement.
Corrupt state is unknown state and fails closed without advancing the active
book.

CARRY's pre-settlement handoff is a typed, hash-chained JSONL event tape. Each
event freezes the realm, source profile and config identity, decision/fire and
settlement times, symbol, and running rate. When available, it also freezes the
mark and exact CARRY-attributed side, quantity, and average entry; those fields
are null when the fire lacks complete holding or mark evidence. Its semantic
ID makes append idempotent. CARRY durably appends the event before it applies
the exit mask. Venue/account parsing, pure planning, tape publication, state
transition, and private-state persistence are separate phases. Exodus verifies
the whole chain and owns a separate state root
and book. Accepted, expired, and incomplete handoffs are terminally recorded
once. Health, symbol-state, and compatibility blocks remain unconsumed and can
be reconsidered by a later cycle. An incomplete holding or mark is consumed as
blocked rather than guessed.

The LONG cross-language replay fixture is recorded test input, not market
evidence. Each hash-chained strategy event carries the typed decision input,
prior state, effective-config identity, quote, instrument rule, and account
snapshot. Python verifies the tape and produces the decision, live state, and
exact target-book bytes. Rust verifies the same tape and produces the recorded
planner, risk, and WAL events.

The hourly LONG runner is diagnostic. The minute live-physics runner reads
point-in-time hourly signals plus candidate-window `klines_1m` and
`mark_price_1m`. Mark price drives the live-equivalent entry and stop tests and
the position value charged at funding; traded price drives the crossing fill,
position accounting, and target resize. Separate receipts report hashes and
missing symbol-days/minutes for both minute streams. Because OHLC minutes do
not reveal the path inside a minute, the report is an execution bound rather
than tick or fill parity.
Its PIT receipt grades the causal input window, not every partition stored in
the root. For the current rule that window begins 90 calendar days before the
signal start. Because each daily source bar is timestamped at the following
midnight, it ends before `signal_end_exclusive - 1 day`. The same report keeps
the whole-root coverage result as a separate informational receipt.
Each run also writes `long_live_physics_source_snapshot.json`: the exact UTF-8
source bytes reachable from the research runner and live LONG producer roots,
plus the Python, Polars, and NumPy runtime versions. The report records that
snapshot's file count and SHA-256, so a dirty worktree does not hide which code
produced the artifact.

Download a candidate-window mark tape through the same partition writer as the
trade tape:

```bash
.venv/bin/python scripts/data/download_bybit_klines_1m.py \
  --price-stream mark --windows-file WINDOWS.csv
```

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
