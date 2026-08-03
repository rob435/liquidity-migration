# 2026-08-03 latency and architecture audit, pass 2

Owner question: "What other baggage and latency/architecture issues are
affecting the systems?" This is the full second pass after the same-day
latency/efficiency program (`a1058e9`+`3b15ba5`).

**Method.** Live measurement on the VPS (per-unit CPU counters, per-thread
`/proc` deltas over 60 s, 5 s syscall profiles of the hottest threads, storage
inventory, journal-event histograms over the current epoch and the archived
12-day epoch, order-lifecycle latency chains) plus three parallel read-only
code audits (producers; account owner and journal kernel; storage, deploy, and
baggage). Every load-bearing claim below was re-derived from source by the
session itself; findings taken from an agent without an independent check say
so. Code references are against `main @ a52b35e` unless dated otherwise.

**One defect was fixed during the audit** (finding 1); everything else is
reported only. No capital-preservation control was touched, and none of the
proposals below is implemented — the owner picks.

---

## What was measured (steady state, 17:23–17:35 UTC, fleet at `95497d1`)

| Unit | CPU | Memory | Notes |
| --- | --- | --- | --- |
| carry producer | ~29% of a core | 768 MB / 1408M cap | hottest thread ~21% |
| LONG producer | ~26% of a core | 519 MB / 1024M cap | hottest thread ~21% |
| account owner | ~8.8% of a core | 71 MB | two threads ~2.9 + 2.2 CPU-s/min |
| watchdog | 0.9–1.0 s CPU per 3-min run | oneshot | after the same-day slimming |

Host: load ~1.0 on the VPS, 2.8 GB RAM available, disk 36% used, journald
422 MB. Cycle cadence: carry on a strict 60 s grid; LONG drifts (+2 s/cycle —
it sleeps the full interval after ~2 s of work). WS reconnects since deploy: 0.

Latency chains (archived epoch, owner-internal monotonic clocks): decision →
order command same tick (~0 ms); command → venue ack 0.2–0.8 s; venue
execution → local receipt 85–410 ms. Order plumbing is healthy — the system's
costs are CPU and growth, not order latency.

---

## A. Fixed during this audit

### 1. The WS kline plane streamed but never served — fixed (`a52b35e`)

The biggest finding of the audit, and a defect in the feature this same
session shipped hours earlier. Live cycle rows showed `kline_store_rows=0` on
carry (every row came from the on-disk cache) and 5,985 of 269,958 rows / 4 of
120 symbols on LONG. Three stacked causes:

- **Carry probe one bar in the future.** The shared reader's window is
  inclusive over bar opens with `end` = the newest closed bar's open
  (`_kline_window`, event_demo_data.py:89). Carry added `+1h` for a REST
  "exclusive end" contract that does not exist at that layer
  (carry_demo.py:1186, old), so `symbols_with_coverage_through(end)` —
  `max(bar_opens) >= end` (kline_store.py:445) — could never pass.
- **Retention below the window.** The stream manager built its store with the
  fixed 90-day default while LONG asks for a 100-day window; eviction is
  anchored to the newest bar, so the window head was evicted as it landed and
  only names listed inside 90 days could ever pass the full-window check.
  Now `retain_days = max(retain_days, lookback_days + 1)`
  (kline_stream_manager.py:118).
- **A dead gauge and a lying fake.** Carry's cycle row hardcoded
  `"kline_store_rows": 0` (so the zero read as normal), the unit-test fake
  store answered the coverage probe unconditionally, and the synthetic kline
  fixture implemented the same wrong exclusive-end belief as the production
  bug. The fake is replaced by the real `KlineStore`; the fixture is
  inclusive; the gauge is real.

Deployed 17:50 UTC staged install+activate, verified on first post-deploy
cycles: carry `kline_store_rows` 0 → 231,020 (98% of the window), LONG
5,985 → 193,263 (82 symbols), `kline_fetched_rows=0`. Decision inputs are
unchanged (the close-keyed view already cut at the decision bar; the strategy
tests pass with only the fixture contract corrected). Residual symbols
converge as hourly refreshes backfill heads the old retention never kept; the
full cache-skip fast path engages per sleeve at 100% coverage.

Lesson recorded to memory: absence of REST fetches is not proof of serving —
read the serving gauge; and a test fake that answers a probe unconditionally
tests nothing about the probe.

---

## B. The dominant steady-state cost: WS message decode

### 2. Each producer burns ~21% of a core decoding stream messages it discards

Confirmed by measurement, not just reading: the hottest thread in both
producers is the WebSocket receive loop — a 5 s `strace -c` shows ~360
`epoll_wait` wakeups and ~720 `read`s per second, with syscall time itself
negligible; the CPU is userspace frame parsing + `json.loads` + dict routing
in pybit at ~21,600 messages/minute. The consumers use ~150 confirmed bars per
hour (one per symbol) and one turnover ranking per hour; partial-bar kline
ticks and sub-second ticker updates — >99.9% of decoded messages — are
discarded after full decode (`add_bar` rejects unconfirmed bars behind one
lock bump, kline_store.py:236).

Fleet cost: ~2 × 13 CPU-s/min ≈ 42% of one core, the single largest steady
consumer in the system. It bought the REST removal, but most of it is
avoidable. Proposal directions (owner decides): a cheap pre-parse gate on the
raw frame (kline frames without `"confirm":true` need no full decode — needs a
custom on-message hook below pybit's dispatcher), a leaner ticker
subscription (the ranking needs turnover once an hour; today every tick of
~150 symbols is decoded all day), or accepting the cost knowingly. Verify any
change with the same thread-delta + strace method. My earlier memory note
blaming carry's panel rebuild for its steady burn was wrong and is corrected —
the registered engine replays once per day behind `frozen_decision`
(carry_demo.py:1349-1368); the recurring cycle cost is the venue-view rebuild
(~0.4–1.2 s: two full-window sorts + an as-of join whose outputs on 1,439 of
1,440 cycles are telemetry) plus, until finding 1 converges, the compact-cache
read.

---

## C. Growth: what makes epoch age expensive

The archived epoch (Jul 22 → Aug 3) is the denominator: **38,255 journal
segments in 12 days**, 30,885 of them `venue_snapshot`. On the current quiet
book the journal is 100% snapshots: one ~1.3 KB segment file every 30 s.

### 3. The journal's growth floor is the watchdog's freshness contract

`VENUE_SNAPSHOT_CHECKPOINT_INTERVAL_NS = 30 s` exists because "the watchdog
requires a journaled venue fact younger than one minute"
(account_reconcile.py:47-49). That couples liveness proof to permanent,
hash-chained, 3-fsync storage: ~2,880 files and ~3.7 MB/day at zero trading,
~86 k files/month in one flat directory, and every O(epoch) cost below grows
with it. Nothing compacts inside an epoch; operator resets are the de-facto
compaction (the thing pass 1 flagged). Proposal class (owner decision, touches
the watchdog contract): a snapshot rollup/checkpoint event, or moving the
freshness proof to a non-journal channel (the owner-health file already
exists) so the journal records changes, not heartbeats.

### 4. Owner tick work that scales with epoch age (agent-reported, spot-verified)

The owner is 8.8% of a core today on a 320-event journal — the concern is the
slope, not the level. The audit agent's strongest items, the two biggest
re-verified by me in source:

- Protection anchors re-project from a full event copy on the 10 Hz tick
  whenever any component target is nonzero, ~7 passes over the journal per
  call (`component_execution_anchors_from_snapshot`,
  account_strategy_state.py:374; called from protection_engine.evaluate on
  the tick loop, account_service_runner.py:1076) — verified.
- `_snapshot_ref()` copies the whole event list (`tuple(events)`,
  account_kernel.py:1296) ≥ tens of times/second across converge, protection,
  and 1 Hz notifications — verified. The reconcile copy itself stays (recorded
  deliberate non-change — a zero-copy view races the funding-index
  accounting), but the *call count* is a choice.
- Reconcile walks every symbol ever held (positions are never pruned) taking
  an O(journal) snapshot per symbol each 2 s; convergence re-scans the full
  event list per unconverged symbol at 20 Hz; `run_safety_flat_once` copies
  `processed_batches` at 10 Hz; the WS ack path linear-scans all orders ever
  per execution row; startup adds a quadratic `target_proposals` scan on top
  of the known full verified read. (Agent-reported with quoted lines,
  high confidence, each with a verify-by; not independently re-derived.)
- Producers: `canonical_entry_attempts` rebuilds the rejected-attempts set
  from every retained journal event, per cycle, per sleeve
  (account_strategy_state.py:156) — the one journal read model still outside
  the cursor/memo family.

Remedy class: incremental projections keyed on the cursor digest (the pattern
the producers already use), and pruning terminal state (flat positions,
closed orders) from the in-memory maps. Real design work — the kernel's
correctness argument leans on replay — so this is a program, not a patch.

### 5. Cycle-ledger write amplification: every append rewrites the partition

`_write_part` with append reads the whole part file, concats, dedups, sorts,
rewrites, double-fsyncs (storage.py:968-984) — per cycle. Carry and CONT
cycles are month-bucketed (storage.py:653), so the rewrite ramps to ~8 MB
*every 60 s* by month end (~11–22 GB/day of parquet I/O); LONG's day
partition caps at ~1,440 rows. Latent sharp edge: `carry_hold_mainnet_cycles`
is in nobody's bucket map and the carry producer passes `partition_by=()`, so
an armed mainnet would rewrite one *unbounded* monolith per cycle forever
(verified: carry_demo.py:226 + storage.py:838). Cheap remedies if wanted: day
buckets for the cycle ledgers, and add the mainnet dataset to
`LEDGER_BUCKET_SOURCE` before it ever matters.

### 6. Smaller growth items

- **Evidence tapes are RAM-resident forever**: event clock, outcome tape, and
  capture tape retain every row in process memory (~2–4 MB/day across the
  three) and re-verify the whole file hash chain on restart. Related: the
  capture tape appends a ~700 B row *per cycle even when nothing was
  published* — 322 rows in the first 165 min of this epoch, all with
  `requests: []` (measured). Skipping empty publications is a one-line
  question for the owner (it is an audit-trail semantics choice).
- **Carry funding dataset**: read whole before the hourly-throttle check
  (carry_demo.py:1008 — the read is above the throttle), one dir + parquet
  per symbol ever ranked (179 dirs, rewritten per touched symbol per sweep),
  never pruned (~450 rows/day).
- **Inbox completed/ and arrival/ scandir + subset test are O(all requests
  ever)** per producer cycle, and `seen_names` grows unbounded in daemon
  memory (account_service.py:766) — the parse is incremental (that fix
  holds); the listing isn't. Nothing prunes the directories.
- **Watchdog dataset reads glob every partition**: `read_dataset_columns`
  without `since_date` opens every `date=` part file (storage.py:921 +
  check_fleet_liveness.py:879) — ~90 opens/run at day 90, 480 runs/day. The
  reader already supports `since_date`; the watchdog just doesn't pass it.
- **Journal head-probe listings**: `_validated_contiguous_names` caches
  validation but relists the directory every call, several times per producer
  cycle (account_kernel.py:578, by design for freshness) — O(segment count),
  so it compounds with finding 3.

### 7. Owner REST that duplicates what it already has (agent-reported)

Every 2 s pass: funding reconcile re-queries a rolling **24-hour** SETTLEMENT
window (`DEFAULT_FUNDING_OVERLAP_MS`, account_reconcile.py:35 — verified) and
re-validates/hashes every re-seen row against an index that is already
incremental — ≥43 k REST calls/day re-fetching settled facts. Per-order REST
recovery (`get_trade_history` + `get_order_history` per commanded order)
duplicates the private WS `execution`/`order` frames it is simultaneously
healthy on; the WS subscribes neither `position` nor `wallet`, which stay
polled. And the notification engine copies the journal and rewrites+double-
fsyncs its state file every second even with nothing to send
(account_notifications.py:1098, 244-248): ~173 k fsyncs/day idle. Remedy
class: shrink the funding overlap to a few settlement periods, gate the
per-order REST recovery on WS gap detection, skip the no-op notification
write. All owner-side behavior changes — proposals only.

---

## D. Latent traps and deploy-surface issues (verified in unit files)

- **Mainnet owner can latch permanently failed**: `Restart=always` +
  `RestartSec=2` with systemd's default 5-starts-per-10 s limit — five fast
  startup failures and the real-money owner stays down until a manual
  `reset-failed`. The unit's own comment says restart is the recovery path.
  One `StartLimitIntervalSec=`/`StartLimitBurst=` line decides it. (Dormant:
  mainnet disarmed.)
- **Hedge timer has zero idle gap**: `OnUnitActiveSec=5min` with
  `TimeoutStartSec=300` — a full-length run re-triggers immediately; the
  service comment claims it is bounded *below* the interval but it is equal.
  (Dormant: CONT sleeve off, timer inactive.)
- **Retention/window contract**: after the finding-1 fix the store always
  retains its own lookback; but `replay_days` > the manager's lookback would
  still silently disable the fast path — the startup validation covers the
  bootstrap window, not serving. Worth one loud check if replay windows ever
  change.
- **Oneshot import weight**: the watchdog + hedge timers fork a fresh
  interpreter ~960–1,250×/day importing polars/pyarrow with
  `PYTHONDONTWRITEBYTECODE=1` (no bytecode cache) — est. tens of CPU-minutes
  /day. `cli/commands.py` also imports the Binance-archive module (pyarrow,
  certifi) at module scope on every producer start; the strategy imports are
  already lazy, this one block isn't.
- **journald rate limits**: only `SystemMaxUse=500M` is set; per-service
  defaults (10 k msgs/30 s) silently drop bursts rather than throttling.

## E. Baggage (dead weight, stale labels)

- **~1.3 GB of retired-fleet data on the VPS**: paper roots ~869 MB (fleet
  retired 2026-08-03), `bybit-continuous-demo-event` 327 MB (sleeve off, unit
  installed but stopped), `depth`/`liquidations` 142 MB (collectors dead
  since ~Jul 3), `_pre_v2_purge_backup` <1 MB. Plus 476 MB of reset archives
  (deliberate evidence retention — listed for completeness, not deletion).
  Disk is 36% used, so this is tidiness, not urgency; deletion is the owner's
  call, and the paper epochs are already invalid as cross-fleet evidence.
- **`carry_hold_v3` labels on a v4 book**: `CARRY_STRATEGY_ID = "carry_hold_v3"`
  (carry_demo.py:219) stamps every live cycle id and journal row of the v4
  rule; README, both carry unit `Description=` lines, and the launcher
  comment also still say v3. If the id is a deliberate ledger-continuity key
  (same sleeve, same book, new rule), say so where it is defined; if not, the
  rename is a change point. Owner call — it is attribution, not behavior.
- CONT's units + timers remain installed (startable by hand against an
  off sleeve); three deploy-path shims already chipped; `scripts/research/
  screen_deltaneutral_carry.py` referenced by nothing; the three
  `run_bybit_*_event_engine.sh` launchers triplicate a ~50-line preamble and
  have already drifted (`--risk-policy-file` vs `--operational-profile-file`).

## F. Claims checked and downgraded

For the record, things the code audits alleged that did not survive
verification, or that I previously recorded wrongly:

- The on-disk hourly kline dataset is **not** unbounded — a pruner anchored
  to the read window caps it (~103 date partitions ≈ 372 MB steady, matches
  the host).
- Owner-health/readiness writes, wedge probing, capture-store appends, the
  JSONL projection, docs links, and `.env.example` knobs are clean (checked).
- My own `a1058e9` claim that carry cycles were served from the WS store was
  wrong (finding 1); STATE.md and memory are corrected in this commit. The
  measured after-numbers (zero mid-hour REST rows, exact 60 s cadence) were
  real — the attribution was not.

## Standing deliberate non-changes (unchanged by this audit)

The owner-reconcile `journal.events()` copy (zero-copy races the funding
index) and carry's once-daily registered-engine replay (registered-rule
discipline) stay as recorded. The capital-preservation controls were audited
for cost only; nothing here proposes touching them.

## Suggested order if the owner wants a program

1. WS decode gate + leaner ticker subscription (finding 2) — biggest steady
   CPU, no strategy surface.
2. Journal checkpoint/rollup design (finding 3) — kills the growth floor and
   most of finding 4's slope with it.
3. Day-bucket the cycle ledgers + mainnet bucket entry (finding 5) — small,
   mechanical.
4. Owner REST diet (finding 7) — measurable REST/fsync cuts, needs care.
5. Baggage sweep (E) — one owner yes/no list.
