# Architecture

How the runtime is split, where account truth lives, what the kernel refuses. Code, tests, units and
generated artifacts define behavior when this file drifts; deployed state is
[`STATE.md`](../STATE.md), its dated history [`CHANGELOG.md`](../CHANGELOG.md).

The program has two parts: the research system, in Python, and the execution engine, in Rust
([`engine.md`](engine.md)). The engine is the order path now: the Python one was deleted on
2026-08-14 (CHANGELOG that date), though the host still runs its installed copy until the next
deploy.

**Scope warning.** Much of what follows describes that deleted Python order path — the account
owner, its inbox, its protection modules, its journal transitions — in the present tense, and cites
line numbers in files that no longer exist. It is kept because it is the most precise description
of the behaviour the engine had to reproduce, and because the host still runs it today. Read it as
the specification it became, not as a map of the current tree; `engine.md` is the current tree. The
producer half of this document — universes, target books, sleeve planning — is unaffected and
current.

## Producer / owner split

Strategy processes publish absolute component targets. They hold no venue credentials and never
submit, adopt, repair or close an order. Sleeve code may compute signals and desired notionals but
never call a venue client, mutate a ledger or reserve margin;
[`strategy_runtime.py`](../liquidity_migration/account/strategy_runtime.py) converts all sleeve intents
together into one atomic kernel batch.

| Role | Units | Mutates a venue |
| --- | --- | --- |
| Account owner, demo | `account-execution` | Yes — sole Bybit demo mutator |
| Account owner, mainnet | `account-execution-mainnet` | Yes, once the owner arms it |
| Target producers | `bybit-{long,carry}-{demo,mainnet}` | No |
| Liveness | `demo-liveness`, `mainnet-liveness` (+ timers) | No credential, no ordering dependency on the owner it watches |

```text
market data -> strategy target -> durable inbox -> account kernel
            -> risk decision -> OrderCommand -> submission attempt (provider only)
            -> venue or modeled observations -> account journal -> projections
```

Each producer is a plug on the resident strategy host
([`strategy_host.py`](../liquidity_migration/strategy/strategy_host.py), plug contract in its module
docstring). A pass runs on a confirmed bar; on an account-journal commit (inotify watch on the
transaction segments — a fill, receipt or protection event reaches producers in ~2s); on a time
deadline (max-hold stop, carry's 00:20 UTC decision boundary — never delayed by the debounce); and at
least once per 60s idle floor. The floor is a contract: it carries the health receipts the liveness
watchdog reads, the hourly funding sweep, and entry republication.

`submission_attempt` is emitted only on the `ambiguous_provider` branch (`execution_adapters.py`);
historical replay's modeled twin takes the direct `submit_effect` path and emits none.

**Entries are quoted.** An exposure-increasing entry is a GTC limit resting at the touch — one venue
order per command, same `orderLinkId`, stop attached at create.
[`entry_quote_manager.py`](../liquidity_migration/venue/entry_quote_manager.py) advances it each pass
on a lean/urgency/drift recipe (change point in its module docstring):

- The market stream thread wakes the owner when a quoted symbol's touch moves (self-pipe nudge in
  `market_capture.py`, one wake per symbol per 200ms), bypassing the periodic 3s gate for one pass;
  3s stays the backstop cadence.
- Amend through the far touch at the 120s window end; cancel an uncleared remainder after a 20s grace
  (convergence re-plans it); verify the attached stop at fill.
- A cross that cannot be priced retries until the grace runs out; a rejected cancel is retried, not
  latched. Every quoting gate (thin spread, missing tick rules, venue reject) falls back to the market
  order. The convergence health grace treats an in-window resting quote as intentional
  (`resting_quote_active`). Exits, resizes and native stops are market-path.

**One owner per account**, held by a persistent lease
([`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py)); demo's is the
authenticated Bybit user-wide capability under `/run/lock/liquidity-migration`. An owner derives its
route without touching the filesystem, takes the lease, then creates the paired account/inbox manifests
([`account_route.py`](../liquidity_migration/account/account_route.py)), so a losing owner cannot
initialize routes before discovering the active one. Owners pin and revalidate the same single-link
inode plus parent and leaf mount identities for the lease lifetime;
`allow_private_parent_mount_boundary` stays `False`, so adding a `ReadWritePaths` entry to an owner
unit breaks lease acquisition. Route mismatch fails closed after acquisition.

**The inbox** (`AccountIntentInbox` in
[`account_service.py`](../liquidity_migration/account/account_service.py)) is a filesystem queue —
`pending/processing/completed/failed`, atomic claim, durable arrival sequence. It coalesces later
replacements and carries component revisions, so an older entry cannot reopen a component after a newer
zero target. Each queued file carries its own arrival order; a new request's order is one past whatever
unfinished requests already claim, floored by an advisory counter (buffered, no fsync) that keeps
numbering climbing across drained queues and restarts. An unreadable queue file is skipped with a
warning rather than blocking the publish — the owner's claim walk still fails closed on it. (`arrival/`
still exists and is still read for requests queued by an older build.)

A failed request releases back to `pending/` for retry, with one exception: when the failure is the
never-attempted `StaleUnsubmittedExposureCommand` and every entry intent is past its own declared
`signal_valid_until_ms`, the request retires terminally to `failed/` (`StaleEntryRequestExpired`).
Exits never expire; a batch with attempted commands keeps resuming past expiry so possibly-live venue
state reconciles.

Journals of the retired credential-free paper twin remain on disk; intents in them carry
`mirror_source_request_id` / `mirror_source_environment` / `mirror_scale` metadata.

## The account journal

Event-sourced, and the accounting authority for demo, mainnet and historical replay.
Implementation: [`account_kernel.py`](../liquidity_migration/account/account_kernel.py).

```text
<account_root>/account_journal/
  transactions/*.json   authoritative atomic transaction segments
  events.jsonl          rebuildable projection for humans and tooling
  journal.lock          cross-process writer lock
```

`AccountJournal.transact` serializes writers with `journal.lock`, hands the builder an **isolated**
committed state, validates and reduces every proposed event, writes one immutable transaction segment
by atomic replacement, then publishes every in-process cache field together. A crash exposes the prior
segment set or the whole new one — never half a target batch. General callers get a deep copy; only
kernel-owned trusted read-only builders share the reducer's prospective copy, and a builder that
mutates what it is handed corrupts persisted event and state hashes.

Writes are write-behind by default: the rename is the visibility point, disk syncs run on one
background flusher thread in strict commit order, and the order path makes them durable with one
`journal.barrier()` between the durable attempt claim and the venue send — nothing reaches the venue
before its plan and claim are power-loss durable. `ACCOUNT_JOURNAL_WRITE_BEHIND=0` restores inline
fsyncs per commit. Ops tools and research code stay synchronous writers and defensively sync the newest
segments when a write-behind owner's marker is present.

Between segment replace and cache publication, in-process readers are held on the prior coherent cache
by `_local_transaction_publish_in_progress`. **Never add a cache-refresh or `_storage_signature`
shortcut that re-reads during a local commit window** — the reader would replay the whole history while
holding `_cache_lock`, stalling the writer that owns the cross-process lock. A failed publication
clears the guard in a `finally`. The JSONL projection is written after the guard clears; a
projection-write failure cannot roll back or replace the committed transaction.

Where segments exist, readers ignore the projection as an authority source. A non-empty `events.jsonl`
with no segments is rejected, never migrated — "account journal has events.jsonl but no authoritative
transaction segments; reset the account root explicitly", from both `read_account_journal` and
`read_account_journal_head`. That reset is the sanctioned remedy.

Each event carries schema, stable UUID, global sequence, type, correlation and causation IDs,
account/sleeve/symbol, wall and monotonic time, payload, `prev_event_hash`, reducer `state_hash` and
`event_hash`. The event ID is deterministic from account ID plus the caller's idempotency key:
redelivery with identical content is a no-op, reuse with changed content is an integrity error.
Verification rejects sequence gaps, duplicate IDs, mixed account IDs, hash-chain breaks, per-event hash
mismatch (`event.event_hash != _event_hash(event.to_dict())`, `account_kernel.py:654`), illegal
transitions and state-hash disagreement. Shape, sequence, duplicate-ID and single-account checks always
run; the two hash-chain checks, the per-event hash and the state hash run only under `verify=True`.

**Fill admission (reducer).** A fill must name a command already in `state.orders` (`fill references
unknown command`), carry a non-empty `execution_id` never seen before (`duplicate execution_id`), and
find the order `acknowledged` or `partially_filled` (`fill for command X before accepted
acknowledgement`). Direction must agree with the command's sign and price must be positive (`invalid
fill direction/price`); cumulative filled quantity may not exceed the commanded quantity beyond
`quantity_tolerance` (`fill overstates command quantity`). Partial fills advance position state by
their own signed delta; on completion the reconstructed total is snapped to the commanded quantity. A
redelivered or out-of-order venue execution is rejected, not merged — every new fill source (adapter,
reconciliation backfill, replay tool) inherits these rules.

```text
market_input_ref -> decision -> target -> risk_decision -> order_command
                 -> submission_attempt -> ack -> fill -> protection -> close -> pnl
```

`ack_observation`, `order_status` and `venue_snapshot` are supplemental facts added without rewriting
earlier events. `venue_snapshot` is a change record: journaled the moment positions, mismatches or
order ownership change, otherwise only on a ten-minute floor — so proof the venue loop is still running
is `venue_facts_at_ns` in the owner-health file, checked at a one-minute bound. Positions, fees,
funding, P&L and cross-sleeve aggregates are reconstructed by replay, never read from a mutable
projection; read models, health, notifications and reports are consumers.

Three read paths, not interchangeable.

1. **Full verified read** — `read_account_journal(..., verify=True)` or `verify_account_journal(...)`.
   Sanctioned callers: `account_strategy_state.py`, `account_candidate_universe.py` (offline only;
   per-cycle callers pass their cursor), owner startup (`AccountJournal._events_ref`, which is what
   makes the head and tail reads valid), and the report tools
   `scripts/vps/check_deploy_rollout_readiness.py`, `scripts/research/build_trade_diagnostics.py`,
   `scripts/research/build_execution_cost_report.py`. `account_venue_accounting.py` verifies the same
   way over a captured byte snapshot via `read_account_journal_bytes`.
2. **Resumable cursor** — `AccountJournalCursor` for per-cycle producers: re-reads only segments added
   since the last call, cold-reads on any prefix mismatch. Never put a bare `read_account_journal` in a
   per-cycle path: at 28.5k segments a full read cost ~20 s CPU and ~250 MB peak, per call.
3. **Head and tail reads** — `read_account_journal_head` (owner-health hot path) and
   `read_recent_account_events` (the watchdog's venue-snapshot check). Both scan the transaction
   filename sequence for continuity (prefix-cached per root), then authenticate only the newest
   segment(s): filename-embedded transaction hash, first/last sequence agreement, and every event's
   shape, sequence, account id, chain link and `event_hash` inside the window. Tail window: 1024
   segments. Earlier payloads are unchecked, so neither is full integrity verification; both are valid
   only because every serving owner generation reconstructed the whole journal at startup.

`require_recent_account_owner_health` matches the head's sequence, account id and state hash against a
fresh health artifact, retrying a health/head/health triplet. `head_binding="exact"` (default) is what
sizing consumers need, so capital evidence cannot predate a fill; `head_binding="allow_behind"` is for
liveness consumers, whose on-disk health normally lags the head by one transaction. Health *ahead* of
the journal, or equal-sequence state-hash disagreement, fails closed; staleness stays bounded by
`max_age_ns`. `scripts/vps/check_deploy_rollout_readiness.py` adds two script-level modes that skip the
binding entirely: `none` and `stopped-maintenance`.

## Pre-trade gate

`AccountExecutionKernel._evaluate_batch` is the single admission point. It scores a whole batch and
journals a `risk_decision` carrying every rejection key. Strictly risk-reducing batches bypass the
growth caps — nothing here may block an exit.

| Check | Bound |
| --- | --- |
| Gross notional | `max_component_gross_notional_usdt`, `max_account_gross_notional_usdt` |
| Initial margin | `max_initial_margin_usdt`, and observed `available_margin_usdt` |
| Per-symbol notional | `max_symbol_notional_usdt` |
| Leverage | `max_leverage`, plus the venue's per-symbol ceiling |
| Sleeve partition (B3) | `sleeve_limits[sleeve]` gross and initial-margin shares |
| Instrument rules | quantity step, min quantity, min notional, tick |
| Inputs | missing market input, stale component revision, non-positive equity, unavailable capital snapshot |

**B3 sleeve partition.** Account-wide caps alone let one sleeve consume the whole envelope. When the
profile declares `sleeve_limits`, each sleeve is also held to its own share, priced at the same
reference prices as the prior book so the comparison isolates this batch's quantity change. A sleeve the
partition does not name gets nothing (`unpartitioned_sleeve`); an untouched, non-growing sleeve is
skipped, so a sleeve over its share cannot veto another's de-risking.

**Equity-anchored envelope**
([`envelope.rs`](../engine/engine-risk/src/envelope.rs)). The profile
is a set of ratios; the capital reference is the scale. Caps are a fraction of observed wallet equity,
not a pinned number. Equity down rescales immediately; equity up waits for a move past a dead band. A
missing, non-finite, non-positive or stale reading holds the current reference — unknown is not evidence
of small.

**Daily loss halt**
([`loss_guard.rs`](../engine/engine-risk/src/loss_guard.rs)). Account-level, because
the failure that matters is the whole book moving together. `OK`; `BLOCKED` when equity is too stale to
judge (no new risk, positions stay under their venue stops); `TRIPPED` when the daily ceiling breaks
against a fresh reading (flatten and stop, never self-clears). The anchor is the day's opening equity,
not a high-water mark, snapshotted so a restart cannot refresh the budget.

**Venue-native protection**
([`working.rs`](../engine/engine-core/src/working.rs),
[`reconcile.rs`](../engine/engine-core/src/reconcile.rs)). Every exposure-increasing
demo command durably carries a Full-position MarkPrice stop before any provider call — `stopLoss`,
`slTriggerBy=MarkPrice`, `tpslMode=Full`, `slOrderType=Market` in the create request. A position without
a durable active stop cannot be scaled up; an existing stop never moves inward. Order creation is
asynchronous, so the private stream and REST position truth still confirm it.

*Stop price at entry.* The kernel takes the outermost price across all same-direction component stop
contracts, anchored to each component's durable **decision reference** price
(`payload["reference_price"]`), never to a fill. Component metadata key is `stop_loss_pct`, required
finite in (0,1); source label `decision_reference_outermost_component_fraction`
(`account_kernel.py:1532`). With no component declaring a stop it falls back to the explicit account
fraction under `decision_reference_account_fallback_fraction` (`:1534`). The stop is rounded outward to
the verified tick (`round_native_stop`). The account fraction is `DISASTER_STOP_FRACTION` →
`--disaster-stop-fraction` → `fallback_stop_fraction`, required in (0,1) with no default:
`scripts/runtime/run_account_execution_service.sh:79-80` refuses to start the owner without it.
Reduce-only commands carry no entry TP/SL fields. Confirmed fills then re-anchor the Full-position stop
to exact component fill evidence — a separate later stage labelled
`fill_anchored_outermost_component_stop` (`venue_protection.py:310`); the two labels must not be merged.
A coalesced entry fill plus stop execution is processed entry-first even when Bybit sends the stop row
first (`account_execution_stream.py:309`) — processing the stop first would reconstruct a position that
never opened.

*Repair and the breach latch.* Repair begins from authenticated venue position truth. An exact matching
Full-position `stopLoss` is adopted without mutation (`_adopt_verified_venue_stop`). A missing or
mismatched stop is repaired via `set_trading_stop` (`:746-760`) from a positive authenticated MarkPrice
— absent one, repair raises rather than guessing — and is **never** latched. Only three things latch
`breached_unprotected` and flatten: an already-crossed stop threshold (`_require_repair_not_crossed`,
`:568`), a venue rejection of the mutation as crossed (`rejection_crossed`, `:843`), and an entry whose
declared stop never landed *and* whose direct repair already failed (`_resolve_unarmed_entry`, `:1055`).
A late-arriving stop does the opposite of flattening: "The stop landed late; clear rather than flatten a
live book" (`:1035-1037`).

The latch is durable — it survives price recovery and owner restart, rehydrated from journal
`protections` rows carrying `breach_mark` / `breach_evidence_source` / `breach_detail` (`:467-500`), so
"a price recovery or target revision cannot re-arm protection" (`:734-736`). Only authenticated proof of
the matching stop (status → `protection_restored`, `:600`) or a complete authenticated position snapshot
proving flat terminalizes it; reconstructed flatness has no such authority. One symbol's failure cannot
prevent reconciliation of sibling symbols.

A latched breach produces one atomic, revision-dominating zero-target request for every accepted or
unresolved component on the symbol. The priority FIFO bypass and the authenticated-mark market fallback
require an exact `software_flat_requested` journal authorization whose request hash matches the
immutable inbox request (`protection_engine.py:400,426`; `account_service.py:1062,2205`). The bypass may
cross uncommitted work but never a prior journal-committed crash-replay boundary. Execution still
requires fresh same-symbol venue/local quantity agreement and an in-kernel strict risk-reduction proof.
Startup may stay alive only for a structured breach-only reconciliation result so this recovery can run;
every other startup mismatch aborts.

## Owner health, streams, convergence

Owner health fails closed on: stale market data, missing instrument rules, rejected or unresolved
commands, journal corruption, position/order mismatch, missing native protection, and a private
execution/order WebSocket lacking any of socket liveness, positive authentication, or positive
subscription acknowledgements for **both** the `execution` and `order` subscriptions. Unsafe root/config
changes and authorization drift also fail closed. Any failed or unconfirmed condition blocks owner
health and new exposure immediately.

**Wedged commands.** A command the venue demonstrably does not hold can only be freed by a terminal
journal transition. The reconciler performs that transition itself in **both** realms, on the same
evidence ladder as the `wedged-command` CLI — a live order, an unreadable venue, or a reduction the book
has not booked yet always refuse it. An order the kernel adopted from the venue rather than submitted
carries no `orderLinkId`, so it is probed by venue order id; a reduce-only order whose book is already
flat has no reduction left to lose, so venue quantity past what it booked is foreign rather than
missing. What survives the automatic pass is listed in health as `wedged_command:<kind>:<symbol>:...`
(`:337`), blocking new entries on that symbol without ever blocking its reductions. Each open wedge is
probed at most once a minute and at most five per pass (`WEDGE_PROBE_INTERVAL_NS`,
`WEDGE_PROBES_PER_PASS`, `:53-54`).

**Private stream.** Readiness is probed every owner-loop iteration and again at exposure-increasing
admission. After `ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS` (default 180; `.env.example:87`,
`scripts/runtime/run_account_execution_service.sh:19` → `--private-ws-reconnect-seconds`) continuously
not-ready, the owner builds and subscribes one replacement in a background thread, reusing that value as
an attempt cooldown against authentication storms. Every authentication generation must obtain fresh
subscription ACKs — old ACKs do not survive an internal reconnect. The candidate gets 10 s to prove
readiness (`candidate_ready_timeout_seconds = 10.0`, `account_execution_stream.py:560`) while the prior
stream stays published; a recovered prior stream wins. REST reconciliation and strict risk-reducing
requests remain available during the handshake. An unavailable or ambiguous socket probe fails health
closed but is not enough evidence to destroy a possibly live authenticated connection.

**Public L2 watchdog.** Three distinct states: a connection attempt, an open transport, and a
subscription's first frame. A connection generation that does not open or deliver its first orderbook
frame within 30 s is retired (`DEFAULT_ORDERBOOK_FIRST_FRAME_RECONNECT_SECONDS = 30.0`,
`market_capture.py:59`); a previously active orderbook silent for 120 s is retired
(`DEFAULT_ORDERBOOK_STALE_RECONNECT_SECONDS = 120.0`, `:54`). Every callback carries its connection
generation; a retired generation can neither send a new subscription nor restore readiness. Socket
subscription writes and recorder I/O run outside the watchdog state lock, and their deadlines begin
before the potentially blocking operation. Public-stream failure blocks market readiness and new
exposure but does not kill the owner or disable REST/private reconciliation.

**Convergence.** Exposure-increasing or sign-flipping work has a finite configured retry budget and
becomes `retry_exhausted` after definite non-fills (`account_service.py:1696`). A strict reduce-only
residual is never abandoned because that budget elapsed: it stays durable, retries with exponential
backoff `convergence_retry_backoff_ns * 2**exponent` from a 1 s base (`:1164`) capped at 30 s
(`DEFAULT_CONVERGENCE_RETRY_BACKOFF_CAP_NS = 30_000_000_000`, `:65`; intermediate status
`retry_backoff`, `:1698`), and stays visibly unhealthy past the grace period
(`convergence_health_grace_ns`, default `30_000_000_000`, `:1163`) until it fills or the
desired/position state changes. A residual below verified venue quantity granularity is healthy
`venue_minimum_dust` (`:342, 1661, 1693`) and excluded from the unhealthy set (`:364, 373`) rather than
retried forever — do not hand-close dust on the venue.

**Submission freshness and ambiguity.** An exposure-increasing command with zero prior submission
attempts is refused as `StaleUnsubmittedExposureCommand` when `command.created_ts_ns <= 0`, `now_ns <
command.created_ts_ns`, or `now_ns − command.created_ts_ns > max_unsubmitted_exposure_age_ns` (default
`120_000_000_000` — 120 s — `bybit_execution_adapter.py:97`, set by the owner's
`--max-unsubmitted-exposure-age-seconds`, `account_service_runner.py:589`). The budget must cover
whole-batch venue latency rather than one round trip, because command age is anchored to the shared
batch journal instant. It is checked twice — before `prepare_submission` / leverage negotiation
(`execution_adapters.py:747-764`) and again at the order-create boundary (`:787-800`). Exactly one
thread may claim the first exposure-increasing attempt: the journal transaction in
`record_submission_attempt`, not the preflight read, owns the single-winner guarantee (`:803-812`).
After an ACK-lost or otherwise ambiguous order-create the same entry command is reconciled but **never**
blindly resent (`AmbiguousExposureSubmission`, `:734-741`) — this is what stands between a lost ACK and
a double position. An ambiguous leverage response stays retryable because it cannot open a position.
Reconciliation reports an attempted entry with no venue evidence as unhealthy. Reduce-only exits may
retry (`allow_repeat=order.reduce_only`) because they cannot increase venue exposure.

The owner never scans the growing strategy-cycle ledger; unavailable or stale strategy telemetry changes
only the notification message, never account admission. A STALE or absent gate line is cosmetic — do not
build a richer digest by reading the whole cycle ledger per hour.

**Notifications** (full behavior: [`notifications.md`](notifications.md)). Lifecycle output downgraded
to "… waiting for the exchange to confirm" stores an exact pending confirmation via
`_queue_lifecycle_confirmation`, emitted once position truth becomes healthy. Notification state
advances only after all lossless Telegram-sized pages are delivered. The hourly digest labels
owner/reconciliation state `Health:`; realized P&L carries a short `(pending: …)` note whenever funding
fees, trade fees or the exchange cross-check are not final — the digest number is reconstructed from
fills, never venue-final. Component bookkeeping detail goes to the service journal, not the chat. The
digest once carried a CONTINUOUS BTC gate and entry-funnel line, read from a separate receipt-bound
projection. That projection, the `CONTINUOUS_CYCLE_ROOT` switch that enabled it, and the owner flags
behind it were deleted with the sleeve on 2026-08-14. The root was already unset on the owner unit, so
the digest is unchanged; `tests/runtime/test_account_service_runner_readiness.py` now pins that the
launcher names no cycle root at all.

## Epoch reset

The descriptor-rooted preflight
([`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py),
[`account_epoch_reset.py`](../liquidity_migration/ops/account_epoch_reset.py)) rejects symlinked parent
components, multiply-linked regular files (`st_nlink != 1`), special files (anything not directory,
regular or symlink) and mount-boundary crossings — including same-device bind mounts, detected by Linux
mount id rather than `st_dev`. Leaf symlinks are rejected only under the strict account preflight
(`preflight_reset_targets(..., reject_symlinks=True)`). Epoch roots must be pairwise disjoint and
disjoint from every strategy root. A symlinked or bind-mounted data root is not a supported layout.

The clear plans the whole batch before the first unlink — every root, ancestor directory, removal target
and preserved lock bound by (device, inode, file type, mount id, and link count for regular files) —
then re-validates each entry's exact identity immediately before removing it, deepest-first, fsyncing
each parent. Any entry that changed, was redirected or appeared late fails the reset (`account epoch
entry changed before removal`, `reset entry changed during removal`); a file created inside a planned
directory makes its `rmdir` fail and aborts. That is why an apparently harmless concurrent touch of the
account tree aborts a reset. Removed targets are re-asserted absent and the anchor re-validated at the
end.

What survives is mechanical: every file whose name ends in `.lock`, plus everything under a top-level
`.locks/` namespace, is preserved by inode, and any directory containing one is not removed. The account
lease is held across the destructive boundary precisely because those inodes persist.

Archive creation and the final pre-clear check bind the recovery artifact to one exclusively created
inode (`O_CREAT|O_EXCL`, mode 0600) plus an exclusively created `<archive>.sha256` sidecar; revalidation
matches the expected inode as well as the digest.

Residual risk: reset is a fail-closed epoch transition, not an atomic transaction across the account
roots (account root, intent inbox, raw capture). The archive *output* is descriptor-bound; the tar
*input* walk is pathname-based under the stopped-fleet and owner-lease boundary, so a non-cooperating
writer arriving after the final identity check is outside what descriptor validation can exclude. After
the first unlink an I/O error or unmanaged writer can leave a partial clear, and the reset then leaves
all managed units stopped rather than claiming rollback.

## Realms and credentials

`ExecutionEnvironment` answers *which owner a producer publishes to*; `VenueRealm` answers *which venue
a private credential authenticates against*. Separate types on purpose.

| | demo | mainnet |
| --- | --- | --- |
| Realm | `demo` | `mainnet` |
| REST host | `api-demo.bybit.com` | `api.bybit.com` |
| Credentials | `BYBIT_DEMO_API_KEY/_SECRET` | `BYBIT_REAL_API_KEY/_SECRET` |
| Account ID | `bybit-demo-unified` | `bybit-mainnet-unified` |
| Env files | `bybit-demo.env`, `account-execution.env`, `sleeves.resolved.env` | `bybit-mainnet.env`, `account-execution-mainnet.env` |
| Unit user | root | root |
| Instrument rules | `demo_rule_probe`, empirical | `get_instruments_info`, read-only |
| Candidate universe | `freeze_account_candidate_universe.py --realm demo` | same script, `--realm mainnet` |

`--realm` is required on both freezers with no default, the artifact records the realm it was read from,
and a loader refuses an artifact stamped with any other one. Every remaining fallback lands on `demo`;
mainnet requires someone to type it *and* `REAL_MONEY` armed on top. Producers inherit only the
public/route values they need and explicitly unset private API, mainnet, `REAL_MONEY` and Telegram
variables. Env files are strict `KEY=VALUE` data, parsed and never sourced as shell.

[`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) submits and cancels real PostOnly
orders on demo to find the empirically accepted minimum notional: the demo realm rejects some orders its
own `minNotionalValue` says it should accept. It refuses any realm but demo; mainnet takes the declared
`minNotionalValue` at face value and labels the source `venue_declared`.

The modeled execution twin (`MarketOrderExecutionTwin`) survives for historical replay: commit-owned,
uncalibrated — 5.5 bps taker fee, 2.0 bps residual adverse slippage, a walk of the visible decision book,
partial fills by book level, zero modeled latency — every modeled observation tagged
`integration_only_uncalibrated`. Retired paper journals (modeled fills, `paper_modeled_funding` rows,
marked equity) carry that tag and must never be summed with venue-observed demo rows without filtering
on `source`.

## Units, profile, sleeves

A unit names only `UNIT:ENTRYPOINT`;
[`scripts/run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh) maps that pair to one
complete command line and `exec`s it. Callers cannot append argv. The installed profile is a plain marker
at `/etc/liquidity-migration/profile`, written at rollout and read back by verify. One profile exists —
`operational`: the demo owner, every demo producer its toggles allow, demo liveness. `demo-operational`
is rejected by name; an old marker self-heals on the next rollout. Liveness scope is hardcoded in the
committed argv: `check_fleet_liveness.py --account-scope demo` for the demo watchdog,
`--account-scope mainnet` for the mainnet one, choices `_ACCOUNT_SCOPES = ("demo", "mainnet")`. Deploy
modes: `install | activate | verify | rollout | stop-mainnet`. Activation starts owners before
producers; shutdown stops producers before owners.

When `REAL_MONEY=true` is set in the host's `bybit-mainnet.env` — the single arming switch — a plain
`activate` or `rollout` reaches the funded fleet through `start_mainnet_fleet`, which creates the mainnet
state roots and requires the arming preflight to pass before it starts anything; disarmed, `verify`
instead asserts the whole mainnet half inactive.

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling — a host override may turn a
repo-enabled sleeve off but cannot resurrect a disabled one. Which sleeves are on is in that file, not
here. Retired toggles are ignored with a warning if a stale host override still carries them. Turning a
sleeve off stops target publication; it does not cancel, close or zero prior state. Flattening means
publishing zero targets through the owner and waiting for fills.

Operator routes all run through [`scripts/ops.sh`](../scripts/ops.sh); the verb set and every deploy mode
are tabulated in [`operations.md`](operations.md).

**Host layout the install asserts.** The deploy requires `ACCOUNT_RAW_MARKET_PERSISTENCE=0`; owners still
maintain live sequence-aware L2, bounded readiness, exact decision books, journals, reconciliation and
protection, they simply do not append every public frame. The runner refuses to start unless the variable
is explicitly `0` or `1` — "ACCOUNT_RAW_MARKET_PERSISTENCE must be explicitly set to 0 or 1"
(`scripts/runtime/run_account_execution_service.sh`); `.env.example` ships it empty and the deploy sets
`"0"`.

Deployment derives one authorization-bound scheduling-capture tape:
`<ACCOUNT_CAPTURE_ROOT>/strategy-targets.jsonl`. Every producer shares it through a locked,
hash-chained writer — not one tape per producer. Older per-producer fallback tapes remain preserved as
pre-boundary history and are not silently merged into a prospective epoch.

Filesystem modes: demo and credential env files and `sleeves.resolved.env` root-owned `0600`. Deploy
fails on any mode mismatch. Env parsing refuses duplicate keys, shell syntax, aliases, nested roots and
unknown real-money spellings.

Each demo target producer owns one bounded public kline store fed by its own WS stream (LONG top-120 by
turnover, carry top-150 spanning its replay window). Missing or lagging bars retain the public REST
fallback; settled funding history has no stream on the venue, so carry's hourly funding sweep stays REST
by necessity. The demo credential file is read by `check_demo_order_permissions`, which loads it into the
process environment, runs `scripts/maintain/check_bybit_order_permissions.py` and unsets the keys again;
`activate` runs it in `deploy` context and `verify` re-runs it in `verify` context.

## Where modules live

Subpackage ownership, what may import what, and the import order:
[`liquidity_migration/README.md`](../liquidity_migration/README.md). Producer-side strategy modules
(`long_native*`, `carry_demo*`, `financed_longs.py`, `lane2_blend.py`) are documented
with the research they implement: [`trading_logic.md`](trading_logic.md),
[`strategy_program.md`](research/strategy_program.md). Data roots, PIT rules and clock domains:
[`data.md`](data.md).

Moving a module physically is a separate refactor: a file move breaks the systemd launchers
(`deploy/systemd/*.service` → `run_authorized_runtime.sh UNIT:ENTRYPOINT` → per-service launcher),
`deploy/sleeves.env` wiring, and on-disk journal/projection paths.

`requirements.lock` is the exact CI (`.github/workflows/vps-deploy.yml:51`) and deploy
(`scripts/deploy_vps_live.sh:1009`) dependency contract. `scripts/dev.sh doctor --strict-lock` turns a
difference from it into a failing diagnostic — the way to reproduce a deploy-only dependency failure
locally.

Removed from the tree, and not to be recreated from an old document: `research_data_snapshot`,
`unit_numeric_comparison`, the `active_runtime_comparator`, the `forward_epoch_start` collector,
`venue_lifecycle`, the Strategy Overhaul V2 aggregate analyser and full-ledger replay runner, the
`bybit_render_1m` / `binance_vision_alt` acquisition plans and their fetchers, and
the continuous hedge manager with its warm-start regeneration, and — on 2026-08-14 — the whole
CONTINUOUS sleeve: `continuous_demo*`, `continuous_cycle_status`, `continuous_identity`,
`continuous_component_sources`, the five `research/backtest/continuous_*` modules, the two
continuous research runners, and the `continuous-event-demo-cycle` subcommand. (The quote-lab package stays:
it is the machinery behind the registered entry recipes — CHANGELOG 2026-08-08.)

## Trade diagnostics

Four sources, used once and derived from: (1) verified account-journal transaction segments (target,
risk, command, ACK, fill, status, fee, funding, P&L); (2) exact sequence-aware book contexts; (3)
strategy feature/candidate rows for the pre-gate funnel; (4) PIT historical bars or bounded forward marks
for path labels — never live projections.

[`scripts/research/build_trade_diagnostics.py`](../scripts/research/build_trade_diagnostics.py)
`--account-root R --capture-root C --out DIR` builds one deterministic row per canonical order command
from sources 1 and 2. It refuses a dirty tree unless `--allow-dirty`, refuses an existing output, and
writes exactly `execution_tca.parquet` and `manifest.json`. `s=+1` buy, `s=-1` sell; shortfall, spread
and fee are positive when adverse. `M0` is the decision-book midpoint, `P` the quantity-weighted fill
price, `Mh` the first healthy midpoint at or after `h`.

```text
arrival_shortfall_bps  = 10_000 * s * (P - M0) / M0
effective_spread_bps   = 20_000 * s * (P - M0) / M0
fee_bps                = 10_000 * observed_fee / abs(filled_notional)
all_in_arrival_bps     = arrival_shortfall_bps + fee_bps      # null unless both operands are
signed_markout_bps(h)  = 10_000 * s * (Mh - P) / P            # POSITIVE = price moved our way
post_fill_adverse_bps  = -signed_markout_bps                  # this one follows the convention
```

| Field | Meaning |
| --- | --- |
| `signed_markout_bps` | The one column with the opposite sign convention: positive when price moves in our favour after the fill. |
| `post_fill_adverse_{1s,15s,1m,5m}_bps` | The adverse-positive column (`trade_diagnostics.py:143,149,155,161`; `:669`). |
| `reference_shortfall_bps` | Against the strategy command's `reference_price` (`:137, :876`). |
| `book_walk_vwap`, `book_walk_shortfall_bps` | Against a walk of the visible depth-50 decision book (`:100-101, :368-369`). |
| `book_walk_residual_bps` | `10_000 * s * (fill_vwap - walk_vwap) / M0` (`:138, :882`). A residual diagnostic, **not** measured impact: book movement, latency, hidden/RPI liquidity, venue protections and clock error all contribute. |
| `top_imbalance`, `microprice`, `opposite_touch_qty`, `opposite_depth_{qty,notional}_{5,10,25}bps`, `order_to_{touch,10bps,25bps}_depth` | Always available (`:85-99`). Order-flow imbalance needs raw public-event capture and is **not** derivable from the decision snapshot. |
| `missing_reason` | Why a horizon has no mark. An expired horizon is terminally missing with a reason and a null midpoint, never zero. |
| `book_condition` | On the capture record: `crossed_book` / `no_snapshot` / a sequence-gap reason / `empty_book_side` / `no_book_update_before_bound` (`market_capture.py:1081-1103`). |
| `markout_*_actual_horizon_ns`, `markout_*_source_records_json`, lateness, sequence/book state, book, fill identity | Kept explicit alongside the requested horizon. |
| `feed_delivery_plus_clock_offset_ns`, `fill_delivery_plus_clock_offset_ns` | The two cross-domain columns (`:118-119`). Never subtract timestamps from different clock domains and call the answer latency — the names *are* the caveat; do not rename them. |

Required horizons: 1 s, 15 s, 1 min, 5 min. A 50 ms markout is deliberately excluded — only honest with
exact raw observations and clock bounds. Strategy labels at 1 h, 6 h, 24 h, 72 h are a separate grain and
must not be joined into this table.

Consumers enqueue only an execution ID; the owner loop resolves it later and registers horizon tasks, so
diagnostics never delay fill accounting. The five-second lateness bound caps owner resources — it is not
a claim-validity threshold. Coverage is quantity-weighted (`observed_qty / total_qty` over
`abs(fill_qty)`, `:628-636`) and the markout is a quantity-weighted mean across the command's fills
(`:637-642`) — not a fill-count fraction. **Late, gapped, restarted, unregistered or capacity-rejected
fills cannot be silently dropped.**

Markout capacity is bounded on this route, so coverage can be incomplete on a busy batch: 8,192 pending
horizon tasks (`MAX_PENDING_POST_FILL_MARKOUTS`, `market_capture.py:67`) and 128 marks per public book
update (`MAX_POST_FILL_MARKOUTS_PER_BOOK_UPDATE`, `market_capture.py:69`, at `:1072`). Task symbols stay
subscribed only until their tasks clear. Over-capacity schedules come back `rejected_capacity`
(`market_capture.py:774`); never-registered fills surface as `not_registered`
(`trade_diagnostics.py:460`) and leave no schedule record behind.

**Nothing feeds this route any more, and the reader survived the writer.** The bridge that registered
fills for marking (`account/post_fill_markouts.py`) and the loop that drained it
(`runtime/account_service_runner.py`) went with the Python order path on 2026-08-14;
`market_capture.register_post_fill_markouts` is now called only from `tests/account/test_market_capture.py`.
The analysis stack above still works against already-captured roots, and produces nothing new. This
paragraph used to cite line numbers inside the deleted bridge.

**New markouts come from the Rust engine instead**, since 2026-08-14: it marks its own fills at the same
four horizons with the same signs, writes each one into its log when the horizon comes due, and reports
them through `engine fills --wal PATH` ([`engine.md`](engine.md) §What the fills cost). Two differences
are not cosmetic and must not be papered over when the two are read side by side. The engine anchors `M0`
on the **top of book**, not a depth-50 snapshot, so it can say nothing about `book_walk_*` or impact; and
it waits five seconds for a healthy midpoint before recording a horizon as terminally missing, where this
route's bound is the owner's own lateness budget.

**Grains do not interchange.** `command_id` answers "was this order executed well", `(sleeve, symbol,
signal_ts)` "did this idea work", a target batch "did concurrent ideas share one shock". The command
grain aggregates child executions by **absolute** quantity (fill VWAP from `abs(signed_qty)` weights,
`:850-856`); the canonical grain for an individual partial fill is `execution_id`, never counted as an
independent thesis observation. Component rows, order updates, fills, **venues** and overlapping horizons
are not independent observations.

**Decision funnel** (`strategy_funnel.py`). One pre-gate row per declared source-population key —
`(sleeve, venue, symbol, signal_ts_ms)`, four parts, distinct from the three-part TCA decision unit
(`:56-71`); `component_scope` carries genuinely different gate definitions without splitting the shared
decision key. Each source row records causal feature availability and population/PIT provenance, every
named gate's state (`gate_state()` → pass/fail/missing/not_applicable, `:73-79`), first rejection, all
applicable rejection keys, accepted target identity, the operational gates (capacity, existing exposure,
cooldown, health, account-risk admission) separately named, and a fixed, small feature set chosen before
looking at future labels. The table must not embed future path values — labels join later by stable key.
Keep one immutable source row plus gate-state transitions, never the same rejection appended every
minute (`:211-216` emits `source` then `transition`, deduplicating on `gate_state_sha256` and raising if
the source identity changes); the read-only projector folds them into one final row per source key
(first evaluation, first rejection, terminal disposition, evaluation count, first/last timestamps).
Freeze source-population and transition semantics before enabling the writer, which is
**observer-only**: its failure is reported and never becomes an admission gate (`:138`).

LONG sources are closed feature rows keyed by symbol and daily `ts_ms`, captured before
`_classify_entry`, with dynamic retrace, cooldown, capacity, health, unresolved-target, terminal-attempt
and publication gates as transitions. (The tape once carried CONTINUOUS `entry_state` symbol/hour rows
alongside them; that half went with the sleeve on 2026-08-14, and the tape is now the LONG funnel alone.)

**Artifact budget.** At most four claim-bearing payloads per run: `manifest.json` (identities, schema,
counts, nulls, hashes, deviations), `execution_tca.parquet` (one row per canonical command),
`decision_funnel.parquet` (one row per pre-gate decision unit) and `path_labels.parquet` (future labels
only) — the last two written by `scripts/data/build_candidate_tape.py:909-910`. The separate labels file
is what keeps lookahead out of the funnel. The verified journal and capture root stay sources and are not
copied into the run; intermediate partitions are resumable working state; charts and Markdown are
regenerated from the tables. Adding an artifact requires a named claim, a consumer and a retention rule.
`scripts/data/build_candidate_tape.py` reads only the preregistered PIT root and writes exactly one
run-scoped diagnostic partition; construct commands from `--help`. Claim-bearing exports require a
quiescent, frozen, read-only capture snapshot: the projector descriptor-checks every scanned segment
(`_read_stable_capture_segment`, `trade_diagnostics.py:1142-1152`), which is no substitute for a snapshot
boundary around the whole capture root.

**Contracted but unbuilt** — four command-level field families `EXECUTION_DIAGNOSTIC_SCHEMA` does not
implement: MAE/MFE with time-to-MAE/MFE and threshold crossing; exit reason and holding time;
realized/funding/fee decomposition with a counterfactual fixed-horizon return; signal-to-order delay and
opportunity cost for rejected, expired, cancelled or partially unfilled intent. The only implementation
anywhere is `mae_72h`/`mfe_72h` in the candidate tape (`scripts/data/build_candidate_tape.py:625-671`).

**Analysis standard.** Report median, robust spread, tails, missingness and effect sizes — not only means
— plus calibration residuals between the modeled book walk/cost and observed execution (the declared
consumer of `book_walk_residual_bps`). Ordinary holdout language is insufficient after repeated search:
the complete trial ledger is mandatory, and multiplicity control, deflated performance statistics or
backtest-overfit diagnostics apply **only** when their assumptions match the actual selection process.
Method references: SEC Rule 605 amendments
(<https://www.sec.gov/files/rules/final/2024/34-99679.pdf>) for effective spread, size-weighted execution
speed and the realized-spread horizons; Bybit order book
(<https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook>) and private execution
(<https://bybit-exchange.github.io/docs/v5/websocket/private/execution>); Cont, Kukanov & Stoikov
(<https://arxiv.org/abs/1011.6402>) for depth/OFI; Stoikov micro-price
(<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694>) behind `microprice`; Bailey et al.
(<https://escholarship.org/uc/item/4hn4t174>) and Harvey, Liu & Zhu (<https://doi.org/10.3386/w20592>)
for trial-count and multiple-testing discipline.

## Evidence boundary and working rules

Demo observes real venue order lifecycle, latency, fees and funding for its exact epoch. The retired
paper route validated the software path against its declared model and supports no execution-quality or
performance claim. LONG's forward record is demo-only. A venue-accounting receipt proves only its named
journal/venue interval. Grading: [`../AGENTS.md`](../AGENTS.md).

Validate in proportion: `scripts/dev.sh test tests/test_x.py`, then `dev.sh doctor`, then `dev.sh check`
(ruff, mypy, `.venv/bin/python -m pytest -q`).
