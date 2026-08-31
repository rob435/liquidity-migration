# Data and time boundaries

Research datasets, Rust signal state, and execution evidence are different
authorities. They must never be substituted for one another.

## Roots

The configured `DATA_ROOT` is the Python research root. It contains public
market inputs, cached features, reports, and source-format fixtures used by
replay or stopped-state takeover. It contains no venue credentials and is not
a live reducer, position, or P&L authority.

Research refreshes write run-scoped artifacts beneath
`reports/research-refresh/<run-id>/`:

- `manifest.json` freezes code, configuration, roots, and date window;
- `events.jsonl` records append-only step starts, failures, and completions;
- `logs/` contains append-only command output;
- `summary.<sha-prefix>.json` is an immutable content-addressed result card;
- `backtests/<venue>/` contains the selected sleeve reports.

Each Rust signal worker has its own state directory for normalized public
history, its checkpoint, pending publication transaction, and heartbeat. It
publishes immutable observations to the realm signal spool. Each Rust engine
has a separate state directory for the execution WAL, reducer checkpoints and
events, reconciliation state, account heartbeat, and closed trades. Python
reads stable observer artifacts and never parses the WAL on a live decision
path.

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

Python research timestamps are Unix milliseconds and end in `_ms`. A kline
`ts_ms` is its open; its close becomes actionable only after the full interval.
Funding `ts_ms` is the settlement instant and can be joined only backward
as-of.

Rust monotonic clocks are nanoseconds and end in `_ns`; their origin is local
to a process unless a field explicitly says `wall`. Venue timestamps and local
receive timestamps in Rust WAL records are the execution-latency evidence.
Target publication time and filesystem modification time are not.

The live signal and LONG clocks are deliberately separate:

| Field | Meaning |
| --- | --- |
| `observed_wall_ts_ms` | Public-data/source time represented by a normalized worker observation |
| `available_wall_ts_ms` | First time the complete observation was available; never earlier than its observed time |
| `feature_ts_ms` | Closed feature boundary carried in a LONG signal batch |
| `decision_ts_ms` | Wall-clock decision time at which the native reducer applies the batch and lifecycle clocks |
| `entry_valid_until_ms` | Original interval in which the engine may enter; later observations do not extend it |
| `entry_ts_ms` | First engine observation of a uniquely LONG-attributed venue holding; zero before that evidence |
| `max_hold_duration_ms` | Duration frozen with the accepted request |
| `max_hold_deadline_ts_ms` | Attributed entry observation plus the frozen duration; zero before attribution |

A request timestamp cannot start protection decay or maximum hold. An
account-global, manual, inherited, or shared position cannot start a sleeve's
fill clock. The Rust heartbeat exposes a configured sleeve name only when the
fill ledger has one owner and its signed quantity matches the venue quantity.

## Durable strategy artifacts

The signal worker commits normalized history, exact observation bytes, output
sequences, and its next checkpoint as one local pending transaction. Restore
finishes that transaction before acquisition resumes. The engine accepts only
the immutable sequence-numbered observation envelope, writes it to the WAL,
then wakes the addressed native reducer.

LONG, CARRY, and Exodus live state is strategy-owned checkpoint data in the
engine WAL. CARRY hands a pre-settlement fire to Exodus as an engine-owned
durable strategy event. The event freezes source and destination strategy IDs,
its semantic ID, decision/fire/settlement times, symbol, rate, mark, and exact
CARRY-attributed holding facts. The engine makes the event durable before it
becomes visible to Exodus; replay and deduplication use that same record.

Python reducer checkpoints and event tapes remain strict source formats for
research fixtures and stopped-runtime takeover. Active native directional
slots read only WAL-backed signals, events, and checkpoints.

The LONG and CARRY native replay fixtures are recorded test input, not market
evidence. Their typed inputs and prior state are replayed through the native
Rust reducers by the persistent `strategy_contract` adapter and compared with
the recorded expected decisions. A mismatch is code drift, not a performance
result.

The Exodus fixture is consumed by the native Rust contract replay and reducer
tests. It records prior, staged, and final state, fixed event ordering, and
exact outputs over entry, restart, and cover cycles. It proves the decision
contract only; it does not reconstruct the registration economics or prove an
order filled.

The standard historical CARRY panel can reconstruct settled funding prints but
does not contain the venue ticker's pre-settlement running rate or its exact
fire-time mark. Without separately supplied typed observations, the shared
reducer therefore produces a settled-clock diagnostic and labels the
pre-settlement clock missing. It never substitutes the next settled print or
an hourly close and calls that live parity.

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
source bytes reachable from the research runner and Python reference-model
roots, plus the Python, Polars, and NumPy runtime versions. The report records
that snapshot's file count and SHA-256, so a dirty worktree does not hide which
code produced the artifact.

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
