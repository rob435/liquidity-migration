# Architecture

How the runtime is split, where account truth lives, what the kernel refuses, which module
owns what. Code, tests, units, and generated artifacts define behavior when this file
drifts; deployed state is [`STATE.md`](../STATE.md).

## Producer / owner split

Strategy processes publish absolute component targets. They hold no venue credentials and
never submit, adopt, repair, or close an order.

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

`submission_attempt` is emitted only on the `ambiguous_provider` branch
(`execution_adapters.py`); the modeled twin (historical replay, and the retired paper
owner's journals) takes the direct `submit_effect` path and emits none, so a join keyed on
that type finds a path that never emits one, not missing data.

Since 2026-08-04, an exposure-increasing entry is created as a GTC limit resting at the
touch instead of a market order (still one venue order per command, same `orderLinkId`,
stop attached at create). The owner loop's
[`entry_quote_manager.py`](../liquidity_migration/venue/entry_quote_manager.py) advances it
each pass — re-evaluate the price on the lean/urgency/drift recipe that replaced
the old 15s staleness reprice (change point 2026-08-04, in the module docstring): since
2026-08-12 the market stream thread wakes the owner the moment a quoted symbol's touch
moves (a self-pipe nudge in `market_capture.py`, floored at one wake per symbol per
200ms), and that wake bypasses the periodic 3s gate for one pass, which stays as the
backstop cadence; then amend
through the far touch at the 120s window end, cancel an uncleared remainder after a 20s
grace (convergence re-plans it), and run the attached-stop verification at fill. A cross
that cannot be priced retries until the grace runs out, and a rejected cancel is retried
rather than latched. Exits, resizes, and native stops are
market-path, unchanged. Every quoting gate (thin spread, missing tick rules, venue
reject) falls back to the market order, and the convergence health grace treats an
in-window resting quote as intentional (`resting_quote_active`).

One owner per account, held by a persistent lease
([`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py)): demo's is the
authenticated Bybit user-wide capability under `/run/lock/liquidity-migration`. An owner
derives its route without touching the filesystem, takes the lease, then creates the paired
account/inbox manifests
([`account_route.py`](../liquidity_migration/account/account_route.py)), so a losing owner cannot
initialize routes before discovering the active one. Owners pin and revalidate the
same single-link inode plus parent and leaf mount identities for the lease lifetime;
`allow_private_parent_mount_boundary` stays `False`, so adding a `ReadWritePaths` entry to
an owner unit breaks lease acquisition. Route mismatch still fails closed after
acquisition.
The inbox (`AccountIntentInbox` in
[`account_service.py`](../liquidity_migration/account/account_service.py)) is a filesystem queue —
`pending/processing/completed/failed/arrival`, atomic claim, durable arrival sequence — that
coalesces later replacements and carries component revisions, so an older entry cannot
reopen a component after a newer zero target. A failed request normally releases back to
`pending/` for retry, with one owner-approved exception (2026-08-03): when the failure is
the never-attempted stale-command refusal (`StaleUnsubmittedExposureCommand`) and every
entry intent in the request is past its own declared `signal_valid_until_ms`, the request
retires terminally to `failed/` (`StaleEntryRequestExpired`) instead of retrying a dead
decision forever — the 2026-08-01 outage loop. Exits never expire; a batch with attempted
commands keeps resuming past expiry so possibly-live venue state reconciles.

**Retired paper route (2026-08-03).** A credential-free paper twin — its own owner with
modeled fills, per-sleeve paper producers, and later a target mirror that republished demo
targets verbatim — ran beside demo until 2026-08-03 and was removed whole: it produced
routing evidence only (`integration_only_uncalibrated`), its one live research use was
dormant, and it generated a disproportionate share of operational incidents. Journals under
the old paper roots remain on disk as history; intents in them carry
`mirror_source_request_id` / `mirror_source_environment` / `mirror_scale` metadata from the
mirror era.

## The account journal

Event-sourced, and the accounting authority for demo, mainnet, and historical
replay. Implementation: [`account_kernel.py`](../liquidity_migration/account/account_kernel.py).

```text
<account_root>/account_journal/
  transactions/*.json   authoritative atomic transaction segments
  events.jsonl          rebuildable projection for humans and tooling
  journal.lock          cross-process writer lock
```

`AccountJournal.transact` serializes writers with `journal.lock`, hands the builder an
**isolated** committed state (general callers get a deep copy; only kernel-owned trusted
read-only builders share the reducer's prospective copy — a builder that mutates what it is
handed corrupts persisted event and state hashes), validates and reduces every proposed
event, writes one immutable transaction segment by atomic replacement, then publishes
every in-process cache field together. A crash exposes the prior segment set or the whole
new one — never half a target batch. Since the write-behind default (branch
`engine/fast-order-path`), the rename is the visibility point and the segment's disk
syncs run on one background flusher thread in strict commit order; the order path makes
them durable with one `journal.barrier()` between the durable attempt claim and the venue
send, so nothing reaches the venue before its plan and claim are power-loss durable.
`ACCOUNT_JOURNAL_WRITE_BEHIND=0` restores inline fsyncs per commit; ops tools and
research code stay synchronous writers and defensively sync the newest segments when a
write-behind owner's marker is present. Between segment replace and cache publication,
in-process readers are deliberately held on the prior coherent cache by
`_local_transaction_publish_in_progress`: a reader that instead re-stat'ed the changed
directory and treated the new segment as an external commit would replay the whole
immutable history while holding `_cache_lock`, stalling the writer that still owns the
cross-process lock. Never add a cache-refresh or `_storage_signature` shortcut that re-reads
during a local commit window. A failed publication clears the guard in a `finally`, so the
next reader reconstructs the segments rather than hiding a committed transaction. The
JSONL projection is written after the guard clears (by the flusher, in write-behind mode);
a projection-write failure cannot roll back or replace the committed transaction.

Where segments exist, readers ignore the projection as an authority source. A non-empty
`events.jsonl` with no segments is rejected, never migrated — "account journal has
events.jsonl but no authoritative transaction segments; reset the account root explicitly",
raised by both `read_account_journal` and `read_account_journal_head`. That reset is the
sanctioned remedy, and the reason the refusal is safe to keep strict.

Each event carries schema, stable UUID, global sequence, type, correlation and causation
IDs, account/sleeve/symbol, wall and monotonic time, payload, `prev_event_hash`, reducer
`state_hash`, and `event_hash`. The event ID is deterministic from account ID plus the
caller's idempotency key: redelivery with identical content is a no-op, reuse with changed
content is an integrity error. Verification rejects sequence gaps, duplicate IDs, mixed
account IDs, hash-chain breaks, per-event hash mismatch (`event.event_hash !=
_event_hash(event.to_dict())`, `account_kernel.py:654`), illegal transitions, and
state-hash disagreement. Shape, sequence, duplicate-ID and single-account checks always
run; the two hash-chain checks, the per-event hash and the state hash run only under
`verify=True`.

**Fill admission (reducer).** A fill must name a command already in `state.orders` (`fill
references unknown command`), carry a non-empty `execution_id` never seen before
(`duplicate execution_id`), and find the order `acknowledged` or `partially_filled` (`fill
for command X before accepted acknowledgement`). Direction must agree with the command's
sign and price must be positive (`invalid fill direction/price`); cumulative filled
quantity may not exceed the commanded quantity beyond `quantity_tolerance` (`fill
overstates command quantity`). Partial fills advance position state by their own signed
delta; on completion the reconstructed total is snapped to the commanded quantity so
accumulated ulps never reach a downstream comparison. A redelivered or out-of-order venue
execution is rejected, not merged — any new fill source (adapter, reconciliation backfill,
replay tool) inherits these rules.

```text
market_input_ref -> decision -> target -> risk_decision -> order_command
                 -> submission_attempt -> ack -> fill -> protection -> close -> pnl
```

`ack_observation`, `order_status`, and `venue_snapshot` are supplemental facts — transport
timing, terminal partial-fill/cancel state, authenticated venue truth — added without
rewriting earlier events. `venue_snapshot` is a change record: the reconciler journals one
the moment positions, mismatches or order ownership change, and otherwise only on a
ten-minute floor that keeps an unchanging book visible. Proof that the venue loop is still
running is therefore not the journal but `venue_facts_at_ns` in the owner-health file,
which carries the reconciler's own last completed pass and is checked at a one-minute
bound. Positions, fees, funding, P&L, and cross-sleeve aggregates are
reconstructed by replay, never read from a mutable projection; read models, health,
notifications, and reports are consumers.

Three read paths, and they are not interchangeable.

1. **Full verified read** — `read_account_journal(..., verify=True)` or
   `verify_account_journal(...)`. Sanctioned callers: `account_strategy_state.py`,
   `account_candidate_universe.py` (offline callers only; per-cycle callers pass their
   cursor), owner startup
   (`AccountJournal._events_ref`, which is what makes the head and tail reads valid below),
   and the audit/report tools
   `scripts/vps/check_deploy_rollout_readiness.py`, `scripts/research/build_trade_diagnostics.py`,
   `scripts/research/build_execution_cost_report.py`. `account_venue_accounting.py` verifies the
   same way over a captured byte snapshot via `read_account_journal_bytes`.
2. **Resumable cursor** — `AccountJournalCursor` for per-cycle producers: re-reads only
   segments added since the last call, cold-reads on any prefix mismatch. Never put a bare
   `read_account_journal` in a per-cycle path: at 28.5k segments a full read cost ~20 s CPU
   and ~250 MB peak, per call.
3. **Head and tail reads** — `read_account_journal_head` (owner-health hot path) and
   `read_recent_account_events` (the liveness watchdog's venue-snapshot check). Both scan
   the transaction filename sequence for continuity (prefix-cached per root), then
   authenticate only the newest segment(s): filename-embedded transaction hash, first/last
   sequence agreement, and every event's shape, sequence, account id, chain link and
   `event_hash` inside the window. The tail window is 1024 segments: deep enough that a
   ten-minute venue-snapshot checkpoint is always inside it unless the journal is taking
   more than one transaction per second, about 250x the observed non-snapshot rate.
   Earlier payloads are unchecked, so neither is full
   integrity verification; they are valid only because every serving owner generation
   reconstructed the whole journal at startup. The watchdog used to full-verify every 3
   minutes instead — 22–28 s CPU and a ~250 MB+ peak per run at 28.5k segments,
   re-verifying a chain the owner had already verified, and the cost grew with epoch age.

`require_recent_account_owner_health` matches the head's sequence, account id and state
hash against a fresh health artifact, retrying a health/head/health triplet.
`head_binding="exact"` (default) is what sizing consumers need, so capital evidence cannot
predate a fill; `head_binding="allow_behind"` is for liveness consumers, because the
execution consumer appends fills independently of the owner loop and on-disk health
normally lags the head by one transaction until the next republish. Health *ahead* of the
journal, or equal-sequence state-hash disagreement, still fails closed, and staleness stays
bounded by `max_age_ns`. `scripts/vps/check_deploy_rollout_readiness.py` adds two script-level
modes that skip the binding entirely: `none` and `stopped-maintenance`.

## Pre-trade gate

`AccountExecutionKernel._evaluate_batch` is the single admission point. It scores a whole
batch and journals a `risk_decision` carrying every rejection key. Strictly risk-reducing
batches bypass the growth caps — nothing here may block an exit.

| Check | Bound |
| --- | --- |
| Gross notional | `max_component_gross_notional_usdt`, `max_account_gross_notional_usdt` |
| Initial margin | `max_initial_margin_usdt`, and observed `available_margin_usdt` |
| Per-symbol notional | `max_symbol_notional_usdt` |
| Leverage | `max_leverage`, plus the venue's per-symbol ceiling |
| Sleeve partition (B3) | `sleeve_limits[sleeve]` gross and initial-margin shares |
| Instrument rules | quantity step, min quantity, min notional, tick |
| Inputs | missing market input, stale component revision, non-positive equity, unavailable capital snapshot |

**B3 sleeve partition.** Account-wide caps let one sleeve consume the whole envelope. When
the profile declares `sleeve_limits`, each sleeve is also held to its own share, priced at
the same reference prices as the prior book so the comparison isolates this batch's
quantity change. A sleeve the partition does not name gets nothing
(`unpartitioned_sleeve`); an untouched, non-growing sleeve is skipped, so a sleeve over its
share cannot veto another's de-risking.

**Equity-anchored envelope**
([`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py)). The
profile is a set of ratios; the capital reference is the scale. Caps are a fraction of
observed wallet equity, not a pinned number. Equity down rescales immediately; equity up
waits for a move past a dead band. A missing, non-finite, non-positive, or stale reading
holds the current reference — unknown is not evidence of small.

**Daily loss halt**
([`account_loss_guard.py`](../liquidity_migration/policy/account_loss_guard.py)). Account-level,
because the failure that matters is the whole book moving together. `OK`; `BLOCKED` when
equity is too stale to judge (no new risk, positions stay under their venue stops);
`TRIPPED` when the daily ceiling breaks against a fresh reading (flatten and stop, never
self-clears). The anchor is the day's opening equity, not a high-water mark, and it is
snapshotted so a restart cannot refresh the budget.

**Venue-native protection**
([`venue_protection.py`](../liquidity_migration/venue/venue_protection.py),
[`protection_engine.py`](../liquidity_migration/venue/protection_engine.py)). Every
exposure-increasing demo command durably carries a Full-position MarkPrice stop before any
provider call — `stopLoss`, `slTriggerBy=MarkPrice`, `tpslMode=Full`, `slOrderType=Market`
in the create request. A position without a durable active stop cannot be scaled up and an
existing stop never moves inward. Order creation is asynchronous, so the private stream and
REST position truth still confirm it.

*Stop price at entry.* The kernel takes the outermost price across all same-direction
component stop contracts, anchored to each component's durable **decision reference** price
(`payload["reference_price"]`), never to a fill. Component metadata key is `stop_loss_pct`,
required finite in (0,1); source label
`decision_reference_outermost_component_fraction` (`account_kernel.py:1532`). With no
component declaring a stop it falls back to the explicit account fraction under
`decision_reference_account_fallback_fraction` (`:1534`). The stop is rounded outward to
the verified tick (`round_native_stop`). The account fraction is `DISASTER_STOP_FRACTION` →
`--disaster-stop-fraction` → `fallback_stop_fraction`, required in (0,1) with no default:
`scripts/runtime/run_account_execution_service.sh:79-80` refuses to start the owner without it.
Reduce-only commands carry no entry TP/SL fields. Confirmed fills then re-anchor the
Full-position stop to exact component fill evidence — a separate later stage, labelled
`fill_anchored_outermost_component_stop` (`venue_protection.py:310`); the two labels must
not be merged. A coalesced entry fill plus stop execution is processed entry-first even
when Bybit sends the stop row first (`account_execution_stream.py:309`), because processing
the stop first would reconstruct a position that never opened.

*Repair and the breach latch.* Repair begins from authenticated venue position truth. An
exact matching Full-position `stopLoss` is adopted without mutation
(`_adopt_verified_venue_stop`). A missing or mismatched stop is repaired via
`set_trading_stop` (`:746-760`) from a positive authenticated MarkPrice — absent one,
repair raises rather than guessing — and is **never** latched. Only three things latch
`breached_unprotected` and flatten: an already-crossed stop threshold
(`_require_repair_not_crossed`, `:568`), a venue rejection of the mutation as crossed
(`rejection_crossed`, `:843`), and an entry whose declared stop never landed *and* whose
direct repair already failed (`_resolve_unarmed_entry`, `:1055`). A late-arriving stop does
the opposite of flattening: "The stop landed late; clear rather than flatten a live book"
(`:1035-1037`).

The latch is durable. It survives price recovery and owner restart, rehydrated from journal
`protections` rows carrying `breach_mark` / `breach_evidence_source` / `breach_detail`
(`:467-500`), so "a price recovery or target revision cannot re-arm protection"
(`:734-736`). Only authenticated proof of the matching stop (status → `protection_restored`,
`:600`) or a complete authenticated position snapshot proving flat terminalizes it —
reconstructed flatness has no such authority. One symbol's failure cannot prevent
reconciliation of sibling symbols.

A latched breach produces one atomic, revision-dominating zero-target request for every
accepted or unresolved component on the symbol. The priority FIFO bypass and the
authenticated-mark market fallback require an exact `software_flat_requested` journal
authorization whose request hash matches the immutable inbox request
(`protection_engine.py:400,426`; `account_service.py:1062,2205`). The bypass may cross
uncommitted work but never a prior journal-committed crash-replay boundary. Execution still
requires fresh same-symbol venue/local quantity agreement and an in-kernel strict
risk-reduction proof. Startup may stay alive only for a structured breach-only
reconciliation result so this recovery can run; every other startup mismatch still aborts.

## Owner health, streams, convergence

Owner health fails closed on: stale market data, missing instrument rules, rejected or
unresolved commands, journal corruption, position/order mismatch, missing native
protection, and a private execution/order WebSocket lacking any of socket liveness,
positive authentication, or positive subscription acknowledgements for **both** the
`execution` and `order` subscriptions. Unsafe root/config changes and authorization drift
also fail closed. Any failed or unconfirmed condition blocks owner health and new exposure
immediately.

**Wedged commands.** A command the venue demonstrably does not hold can only be freed by a
terminal journal transition, and the reconciler performs that transition itself in **both**
realms, on the same evidence ladder as the `wedged-command` CLI — a live order, an
unreadable venue, or a reduction the book has not booked yet always refuse it. An order the
kernel adopted from the venue rather than submitted carries no `orderLinkId`, so it is
probed by its venue order id; and a reduce-only order whose book is already flat has no
reduction left to lose, so venue quantity past what it booked is foreign rather than
missing. Anything that survives the automatic pass is listed in health
as a `wedged_command:<kind>:<symbol>:...` mismatch (`:337`), which blocks new entries on
that symbol without ever blocking its reductions. Each open wedge is probed at most once a
minute and at most five per pass (`WEDGE_PROBE_INTERVAL_NS`, `WEDGE_PROBES_PER_PASS`,
`:53-54`), so a newly wedged command is probed on the first pass that sees it.

**Private stream.** Readiness is probed every owner-loop iteration and again at
exposure-increasing admission. After `ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS` (default 180;
`.env.example:87`, `scripts/runtime/run_account_execution_service.sh:19` →
`--private-ws-reconnect-seconds`) continuously not-ready, the owner builds and subscribes
one replacement in a background thread, reusing that same value as an attempt cooldown
against authentication storms. Every authentication generation must obtain fresh
subscription ACKs — old ACKs do not survive an internal reconnect. The candidate gets 10 s
to prove readiness (`candidate_ready_timeout_seconds = 10.0`,
`account_execution_stream.py:560`) while the prior stream stays published; a recovered
prior stream wins. REST reconciliation and strict risk-reducing requests remain available
during the handshake. An unavailable or ambiguous socket probe fails health closed but is
not enough evidence to destroy a possibly live authenticated connection.

**Public L2 watchdog.** Three distinct states: a connection attempt, an open transport, and
a subscription's first frame. A connection generation that does not open or deliver its
first orderbook frame within 30 s is retired
(`DEFAULT_ORDERBOOK_FIRST_FRAME_RECONNECT_SECONDS = 30.0`, `market_capture.py:59`); a
previously active orderbook silent for 120 s is retired
(`DEFAULT_ORDERBOOK_STALE_RECONNECT_SECONDS = 120.0`, `:54`). Every callback carries its
connection generation; a retired generation can neither send a new subscription nor restore
readiness. Socket subscription writes and recorder I/O run outside the watchdog state lock,
and their deadlines begin before the potentially blocking operation. Public-stream failure
blocks market readiness and new exposure but does not kill the owner or disable
REST/private reconciliation.

**Convergence.** Exposure-increasing or sign-flipping work has a finite configured retry
budget and becomes `retry_exhausted` after definite non-fills (`account_service.py:1696`).
A strict reduce-only residual is never abandoned because that budget elapsed: it stays
durable, retries with exponential backoff `convergence_retry_backoff_ns * 2**exponent` from
a 1 s base (`:1164`) capped at 30 s (`DEFAULT_CONVERGENCE_RETRY_BACKOFF_CAP_NS =
30_000_000_000`, `:65`; intermediate status `retry_backoff`, `:1698`), and stays visibly
unhealthy past the grace period (`convergence_health_grace_ns`, default `30_000_000_000`,
`:1163`) until it fills or the desired/position state changes. A residual below verified
venue quantity granularity is healthy `venue_minimum_dust` (`:342, 1661, 1693`) and is
excluded from the unhealthy set (`:364, 373`) rather than retried forever — do not
hand-close dust on the venue.

**Submission freshness and ambiguity.** An exposure-increasing command with zero prior
submission attempts is refused as `StaleUnsubmittedExposureCommand` when
`command.created_ts_ns <= 0`, `now_ns < command.created_ts_ns`, or `now_ns −
command.created_ts_ns > max_unsubmitted_exposure_age_ns` (default `120_000_000_000` —
120 s — `bybit_execution_adapter.py:97`, set by the owner's
`--max-unsubmitted-exposure-age-seconds`, `account_service_runner.py:589`). The budget must
cover whole-batch venue latency rather than one round trip, because command age is anchored
to the shared batch journal instant. It is checked twice — before `prepare_submission` /
leverage negotiation (`execution_adapters.py:747-764`) and again at the order-create
boundary (`:787-800`), because preparation is itself a provider round trip. Exactly one
thread may claim the first exposure-increasing attempt: the journal transaction in
`record_submission_attempt`, not the preflight read, owns the single-winner guarantee
(`:803-812`). After an ACK-lost or otherwise ambiguous order-create the same entry command
is reconciled but **never** blindly resent (`AmbiguousExposureSubmission`, `:734-741`) —
this is what stands between a lost ACK and a double position. An ambiguous leverage response
stays retryable because it cannot open a position. Reconciliation reports an attempted
entry with no venue evidence as unhealthy. Reduce-only exits may retry
(`allow_repeat=order.reduce_only`) because they cannot increase venue exposure.

The owner never scans the growing strategy-cycle ledger, and unavailable or stale strategy
telemetry changes only the notification message, never account admission. A STALE or absent
gate line is cosmetic; do not build a richer digest by reading the whole cycle ledger per
hour.

**Notifications.** When lifecycle output is downgraded to "… waiting for the exchange to
confirm" the notifier stores an exact pending confirmation via
`_queue_lifecycle_confirmation` and emits it once position truth becomes healthy — a
downgraded alert is not a lost alert. Notification state advances only after all lossless
Telegram-sized pages are delivered. The hourly digest labels owner/reconciliation state
`Health:`, and realized P&L carries a short `(pending: …)` note whenever parts (funding
fees, trade fees, the exchange cross-check) are not final — the digest number is
reconstructed from fills, never venue-final. Component-level bookkeeping detail is logged
to the service journal instead of the Telegram line. The CONTINUOUS BTC gate and entry-funnel line comes from
a separate receipt-bound projection shown only when `CONTINUOUS_CYCLE_ROOT` is configured;
it is deliberately unset on the owner unit
(`deploy/systemd/liquidity-migration-account-execution.service`, pinned by
`tests/scripts/test_runtime_scripts.py`) so
a retired sleeve leaves no permanently `STALE` line. Re-promotion must set the root
explicitly.

## Epoch reset

The descriptor-rooted preflight
([`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py),
[`account_epoch_reset.py`](../liquidity_migration/ops/account_epoch_reset.py)) rejects
symlinked parent components, multiply-linked regular files (`st_nlink != 1`), special files
(anything not directory, regular, or symlink), and mount-boundary crossings — including
same-device bind mounts, detected by Linux mount id rather than `st_dev`. Leaf symlinks are
rejected only under the strict account preflight (`preflight_reset_targets(...,
reject_symlinks=True)`). Epoch roots must be pairwise disjoint and disjoint from every
strategy root. A symlinked or bind-mounted data root is not a supported layout.

The clear plans the whole batch before the first unlink — every root, ancestor directory,
removal target and preserved lock bound by (device, inode, file type, mount id, and link
count for regular files) — then re-validates each entry's exact identity immediately before
removing it, deepest-first, fsyncing each parent. Any entry that changed, was redirected,
or appeared late fails the reset (`account epoch entry changed before removal`, `reset entry
changed during removal`); a file created inside a planned directory makes its `rmdir` fail
and aborts. That is why an apparently harmless concurrent touch of the account tree aborts
a reset. Removed targets are re-asserted absent and the anchor re-validated at the end.

What survives is mechanical, not curated: every file whose name ends in `.lock`, plus
everything under a top-level `.locks/` namespace, is preserved by inode, and any directory
containing one is not removed. These are the persistent owner, route, journal, inbox and
dataset lock inodes — synchronization infrastructure, not carried-forward account state, so
preserving them leaks nothing across the boundary; the account lease is held across the
destructive boundary precisely because the inodes persist.

Archive creation and the final pre-clear check bind the recovery artifact to one
exclusively created inode (`O_CREAT|O_EXCL`, mode 0600) plus an exclusively created
`<archive>.sha256` sidecar, and revalidation matches the expected inode as well as the
digest — not a reopened predictable path.

Residual risk, stated honestly: reset is a fail-closed epoch transition, not an atomic
transaction across the account roots (account root, intent inbox, raw capture). The archive *output* is descriptor-bound, but the tar *input* walk is
pathname-based under the stopped-fleet and owner-lease boundary; a non-cooperating writer
arriving after the final identity check is outside what descriptor validation can exclude.
Once the first unlink happens, an I/O error or unmanaged writer can leave a partial clear,
and the reset then leaves all managed units stopped rather than claiming rollback.

## Realms and credentials

`ExecutionEnvironment` answers *which owner a producer publishes to*; `VenueRealm` answers
*which venue a private credential authenticates against*. Separate types on purpose (the
distinction predates the paper retirement: the retired paper environment was an owner with
no venue at all).

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

`--realm` is required on both freezers with no default, the artifact records the realm it
was read from, and a loader refuses an artifact stamped with any other one. Every
remaining fallback lands on `demo`; mainnet requires someone to type it *and* `REAL_MONEY`
armed on top. Producers inherit only
the public/route values they need and explicitly unset private API, mainnet, `REAL_MONEY`,
and Telegram variables. Env files are strict `KEY=VALUE` data, parsed and never sourced as
shell.

[`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) submits and cancels real
PostOnly orders on demo to find the empirically accepted minimum notional: the demo realm
rejects some orders its own `minNotionalValue` says it should accept. It refuses any realm
but demo; mainnet takes the declared `minNotionalValue` at face value and labels the source
`venue_declared`.

The modeled execution twin (`MarketOrderExecutionTwin`) survives for historical replay:
commit-owned, not calibrated — 5.5 bps taker fee, 2.0 bps residual adverse slippage, a walk
of the visible decision book, partial fills by book level, zero modeled latency — with
every modeled observation tagged `integration_only_uncalibrated`. The retired paper owner
ran this twin live; its journals (modeled fills, `paper_modeled_funding` rows, marked
equity) remain on disk under that tag and must never be summed with venue-observed demo
rows without filtering on `source`. The twin was never calibrated against demo fills: the
measurement needed the mirror era to produce matched pairs, and the sample was still zero
when the fleet retired.

## Units, profile, sleeves

A unit names only `UNIT:ENTRYPOINT`;
[`scripts/run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh) maps that pair
to one complete command line and `exec`s it. Callers cannot append argv. The installed
profile is a plain marker at `/etc/liquidity-migration/profile`, written at rollout and
read back by verify; since the 2026-08-03 paper retirement one profile remains —
`operational`: the demo owner, every demo producer its toggles allow, hedge/RMOM, demo
liveness (`demo-operational` was "operational minus paper" and is rejected by name; an old
marker self-heals on the next rollout). Liveness scope is hardcoded in the committed argv:
`check_fleet_liveness.py --account-scope demo` for the demo watchdog and
`--account-scope mainnet` for the mainnet one, choices `_ACCOUNT_SCOPES = ("demo",
"mainnet")`. Deploy modes: `install | activate | verify | rollout | stop-mainnet`.
Activation starts owners before producers; shutdown stops producers before owners.
When `REAL_MONEY=true` is set in the host's `bybit-mainnet.env` — the single arming
switch — a plain `activate` or `rollout` reaches the funded fleet through
`start_mainnet_fleet`, which creates the mainnet state roots and requires the arming
preflight to pass before it starts anything; disarmed, `verify` instead asserts the
whole mainnet half inactive.

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling — a host override
may turn a repo-enabled sleeve off but cannot resurrect a disabled one. Which sleeves are
on is in that file, not here. The retired paper toggles are ignored with a warning if a
stale host override still carries them. Turning a sleeve off stops target publication; it does not
cancel, close, or zero prior state. Flattening means publishing zero targets through the
owner and waiting for fills.

Operator routes all run through [`scripts/ops.sh`](../scripts/ops.sh); the current verb set
and every deploy mode are tabulated in [`operations.md`](operations.md).

**Host layout the install asserts.** The deploy requires
`ACCOUNT_RAW_MARKET_PERSISTENCE=0`; owners still maintain live sequence-aware L2, bounded
readiness, exact decision books, journals, reconciliation and protection, they simply do
not append every public frame. The runner refuses to start unless the variable is
explicitly `0` or `1` — "ACCOUNT_RAW_MARKET_PERSISTENCE must be explicitly set to 0 or 1"
(`scripts/runtime/run_account_execution_service.sh`); `.env.example` deliberately
ships it empty and the deploy sets `"0"`.

Deployment derives one authorization-bound scheduling-capture tape:
`<ACCOUNT_CAPTURE_ROOT>/strategy-targets.jsonl`. LONG and CONTINUOUS share
that tape through a locked, hash-chained writer — it is not one tape
per producer. Older per-producer fallback tapes remain preserved as pre-boundary history
and are not silently merged into a prospective epoch.

Filesystem modes: demo and credential env files and `sleeves.resolved.env` root-owned
`0600`. Deploy fails on any mode mismatch. Env parsing refuses duplicate keys, shell
syntax, aliases, nested roots, and unknown real-money spellings.

Each demo target producer owns one bounded public kline store fed by its own WS stream
(LONG top-120 by turnover, carry top-150 spanning its replay window). Missing or lagging
bars retain the public REST fallback; settled funding history has no stream on the venue,
so carry's hourly funding sweep stays REST by necessity. The demo credential file is read by
`check_demo_order_permissions`, which loads it into the process environment, runs
`scripts/maintain/check_bybit_order_permissions.py` and unsets the keys again; `activate` runs it in
`deploy` context and `verify` re-runs it in `verify` context, so order permission is
re-checked live rather than being bound once.

## Subsystem map

The package is deliberately navigated by ownership, not filename. The flat layout carries
extensive cross-module contracts and the map below is not a target directory tree: moving a
module physically is a separate refactor that must trace imports, runtime entry points,
persistence formats, deploy references, and tests. A file move breaks the systemd launchers
(`deploy/systemd/*.service` → `run_authorized_runtime.sh UNIT:ENTRYPOINT` → per-service
launcher), `deploy/sleeves.env` wiring, and on-disk journal/projection paths.

| Module | Owns | Tests |
| --- | --- | --- |
| [`account_kernel.py`](../liquidity_migration/account/account_kernel.py), [`account_contracts.py`](../liquidity_migration/account/account_contracts.py) | Journal storage, reducer, event/risk contracts, pre-trade gate, command emission | `test_account_kernel.py`, `test_account_journal_cursor.py`, `test_account_risk_reduction.py`, `test_sleeve_capital_partition.py` |
| [`account_service.py`](../liquidity_migration/account/account_service.py) | Inbox queue, owner loop, crash-safe request replay | `test_account_service.py` |
| [`account_service_runner.py`](../liquidity_migration/runtime/account_service_runner.py) | Owner process, realm wiring | `test_account_service_runner_readiness.py`, `test_account_owner_health.py` |
| [`account_service_bybit.py`](../liquidity_migration/venue/account_service_bybit.py), [`bybit_execution_adapter.py`](../liquidity_migration/venue/bybit_execution_adapter.py), [`account_execution_stream.py`](../liquidity_migration/venue/account_execution_stream.py) | Bybit providers, order submission, private WS | `test_account_service_bybit.py`, `test_account_execution_stream.py` |
| [`execution_adapters.py`](../liquidity_migration/account/execution_adapters.py) | Deterministic modeled execution twin (historical replay) | `test_account_service_bybit.py` |
| [`account_intent_client.py`](../liquidity_migration/account/account_intent_client.py), [`strategy_targets.py`](../liquidity_migration/account/strategy_targets.py) | Write-only producer boundary | `test_account_intent_client.py`, `test_strategy_targets.py` |
| [`strategy_planning.py`](../liquidity_migration/strategy/strategy_planning.py) | Shared cross-sleeve suppression invariant: read owner health for equity, snapshot planning state, suppress intents duplicating unresolved durable work, retry terminally rejected attempts, avoid same-cycle exit collisions — shared so it cannot drift between sleeves | `test_strategy_planning.py` |
| [`strategy_runtime.py`](../liquidity_migration/account/strategy_runtime.py) | The rule that sleeve code may compute signals and desired notionals but never call a venue client, mutate a ledger, or reserve margin; converts all sleeve intents together into one atomic kernel batch | no dedicated file — covered by `test_account_kernel.py`, `test_account_service.py`, `test_account_risk_reduction.py`, `test_protection_engine.py`, `test_account_service_runner_readiness.py` |
| [`strategy_funnel.py`](../liquidity_migration/account/strategy_funnel.py) | **Observer-only** diagnostic serialization of LONG/CONTINUOUS source gates and separated future-path labels; callers compute gates from causal state and pass immutable row snapshots, and writer failure is reported without becoming an admission gate (`:138`). Entry point `scripts/data/build_candidate_tape.py` | `test_strategy_funnel.py`, `test_candidate_tape.py` |
| [`historical_account_replay.py`](../liquidity_migration/research/backtest/historical_account_replay.py) | Historical-replay accounting path | `test_historical_account_replay.py` |
| [`bybit_market_data.py`](../liquidity_migration/marketdata/bybit_market_data.py) | Venue market-data boundary | `test_bybit_market_data_boundary.py` |
| [`cli/commands.py`](../liquidity_migration/cli/commands.py), [`cli/parsers.py`](../liquidity_migration/cli/parsers.py), [`config.py`](../liquidity_migration/core/config.py), [`storage.py`](../liquidity_migration/data/storage.py), [`ingestion.py`](../liquidity_migration/data/ingestion.py), [`downloaders.py`](../liquidity_migration/data/downloaders.py), [`archive.py`](../liquidity_migration/data/archive.py) / [`archive_manifest.py`](../liquidity_migration/data/archive_manifest.py), venue download modules | `python -m liquidity_migration` CLI and the research-data domain | `test_cli.py`, `test_config.py`, `test_ingestion.py`, `test_storage.py`, `test_storage_since_date.py`, `test_downloaders.py`, `test_archive.py`, `test_archive_manifest.py` |
| `scripts/research/equity_curves.sh` / `.py` (behind `ops.sh equity`) | Research integrity and reporting | `test_equity_curves_runner.py`, `test_scripts_equity_curves.py` |
| [`account_route.py`](../liquidity_migration/account/account_route.py), [`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py) | Root identity; one owner per account | `test_account_route.py`, `test_account_owner_lease.py` |
| [`account_reconcile.py`](../liquidity_migration/venue/account_reconcile.py), [`account_venue_accounting.py`](../liquidity_migration/research/execution/account_venue_accounting.py), [`account_strategy_state.py`](../liquidity_migration/strategy/account_strategy_state.py) | REST recovery, venue truth, accounting rows, sleeve planning read model | `test_account_reconcile.py`, `test_account_funding_reconcile.py`, `test_account_venue_accounting.py`, `test_account_strategy_state.py` |
| [`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py), [`operational_profile.py`](../liquidity_migration/policy/operational_profile.py) | Capital reference and derived caps | `test_equity_anchored_envelope.py`, `test_operational_profile.py` |
| [`account_loss_guard.py`](../liquidity_migration/policy/account_loss_guard.py) | Daily loss halt | `test_account_loss_guard.py` |
| [`venue_protection.py`](../liquidity_migration/venue/venue_protection.py), [`protection_engine.py`](../liquidity_migration/venue/protection_engine.py) | Stop placement, repair, external-fill adoption | `test_venue_protection.py`, `test_protection_engine.py` |
| [`venue_realm.py`](../liquidity_migration/core/venue_realm.py), [`execution_environment.py`](../liquidity_migration/account/execution_environment.py), [`venue_instrument_rules.py`](../liquidity_migration/venue/venue_instrument_rules.py), [`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) | Realm/owner separation, credential selection, rules per realm | `test_venue_realm.py`, `test_venue_instrument_rules.py`, `test_demo_rule_probe.py` |
| [`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py), [`account_epoch_reset.py`](../liquidity_migration/ops/account_epoch_reset.py), [`account_reset_archive.py`](../liquidity_migration/ops/account_reset_archive.py) | Descriptor-rooted checks before an `rm -rf`, epoch transition, archive | `test_reset_path_safety.py`, `test_account_epoch_reset.py`, `test_account_reset_archive.py` |
| [`trade_diagnostics.py`](../liquidity_migration/research/execution/trade_diagnostics.py), [`post_fill_markouts.py`](../liquidity_migration/account/post_fill_markouts.py) | Command-grain TCA projection, markout scheduling | `test_trade_diagnostics.py`, `test_post_fill_markouts.py` |
| [`market_capture.py`](../liquidity_migration/account/market_capture.py), [`ws_state_cache.py`](../liquidity_migration/marketdata/ws_state_cache.py), [`account_owner_health.py`](../liquidity_migration/account/account_owner_health.py), [`account_owner_readiness.py`](../liquidity_migration/runtime/account_owner_readiness.py), [`strategy_cycle_health.py`](../liquidity_migration/strategy/strategy_cycle_health.py), [`run_diagnostics.py`](../liquidity_migration/research/backtest/run_diagnostics.py) | Book capture, stream state, health projection, whether the owner accepts work at all, cycle health read by the liveness units | `test_market_capture.py`, `test_account_owner_health.py`, `test_account_owner_readiness.py`, `test_strategy_cycle_health.py`, `test_run_diagnostics.py`, plus the `scripts/runtime/check_fleet_liveness.py` tests |

Producer-side strategy modules (`long_native*`, `continuous_*`, `carry_demo*`,
`financed_longs.py`, `lane2_blend.py`) are documented with the research they implement:
[`trading_logic.md`](trading_logic.md),
[`strategy_program.md`](research/strategy_program.md). Data roots, PIT rules, and clock domains:
[`data.md`](data.md).

`requirements.lock` is the exact CI (`.github/workflows/vps-deploy.yml:51`) and deploy
(`scripts/deploy_vps_live.sh:1009`) dependency contract. A local environment can be usable
while differing from it; `scripts/dev.sh doctor --strict-lock` turns that difference from a
warning into a failing diagnostic — the way to reproduce a deploy-only dependency failure
locally.

Removed from the tree, and not to be recreated from an old document:
`research_data_snapshot`, `unit_numeric_comparison`, the `active_runtime_comparator`, the
`forward_epoch_start` collector, `venue_lifecycle`, the Strategy Overhaul V2 aggregate
analyser and full-ledger replay runner, and the `bybit_render_1m` / `binance_vision_alt`
acquisition plans and their fetchers.

## Trade diagnostics

Four sources, used once and derived from: (1) verified account-journal transaction segments
for target, risk, command, ACK, fill, status, fee, funding and P&L facts; (2) exact
sequence-aware book contexts for arrival liquidity and timing; (3) strategy
feature/candidate rows for the pre-gate selection funnel; (4) PIT historical bars or
bounded forward marks for future path labels — never live projections.

[`scripts/research/build_trade_diagnostics.py`](../scripts/research/build_trade_diagnostics.py)
`--account-root R --capture-root C --out DIR` builds one deterministic row per canonical
order command from sources 1 and 2. It refuses a dirty tree unless `--allow-dirty`, refuses
an existing output, and writes exactly `execution_tca.parquet` and `manifest.json`. `s=+1`
buy, `s=-1` sell; shortfall, spread and fee are positive when adverse. `M0` is the
decision-book midpoint, `P` the quantity-weighted fill price, `Mh` the first healthy
midpoint at or after `h`.

```text
arrival_shortfall_bps  = 10_000 * s * (P - M0) / M0
effective_spread_bps   = 20_000 * s * (P - M0) / M0
fee_bps                = 10_000 * observed_fee / abs(filled_notional)
all_in_arrival_bps     = arrival_shortfall_bps + fee_bps      # null unless both operands are
signed_markout_bps(h)  = 10_000 * s * (Mh - P) / P            # POSITIVE = price moved our way
post_fill_adverse_bps  = -signed_markout_bps                  # this one follows the convention
```

`signed_markout_bps` is the one column with the opposite sign convention: it is positive
when the price moves in our favour after the fill. The adverse-positive column is
`post_fill_adverse_{1s,15s,1m,5m}_bps` (`trade_diagnostics.py:143,149,155,161` schema; `:669`).

Two further shortfall benchmarks sit beside the decision midpoint: `reference_shortfall_bps`
against the strategy command's `reference_price` (`:137, :876`), and `book_walk_vwap` /
`book_walk_shortfall_bps` against a walk of the visible depth-50 decision book (`:100-101,
:368-369`). Their difference from the observed fill VWAP is `book_walk_residual_bps`
(`:138, :882`, `10_000 * s * (fill_vwap - walk_vwap) / M0`) — a residual diagnostic, **not**
measured market impact: book movement, latency, hidden/RPI liquidity, venue protections and
clock error all contribute.

The required operational horizons for the current taker-heavy Bybit route are 1 s, 15 s,
1 min, 5 min. A 50 ms markout is optional and only honest when exact raw observations and
clock bounds support it; it is deliberately excluded here. Strategy labels at 1 h, 6 h,
24 h, 72 h are a separate grain and must not be joined into the execution-quality table.

The private-execution and REST consumers enqueue only an execution ID; the owner loop
resolves it later and registers horizon tasks, so diagnostics never delay fill accounting.
The five-second lateness bound caps owner resources — it is not a claim-validity threshold.
A horizon that expires is terminally missing with a reason and a null midpoint, never zero.
The record keeps requested horizon, `markout_*_actual_horizon_ns`, lateness, sequence/book
state, book, fill identity and `markout_*_source_records_json` explicit, plus two different
fields answering two different questions: `missing_reason`, and `book_condition` on the
capture record enumerating `crossed_book` / `no_snapshot` / a sequence-gap reason /
`empty_book_side` / `no_book_update_before_bound` (`market_capture.py:1081-1103`). Coverage
is quantity-weighted (`observed_qty / total_qty` over `abs(fill_qty)`, `:628-636`) and the
markout itself is a quantity-weighted mean across the command's fills (`:637-642`) — not a
fill-count fraction. **Late, gapped, restarted, unregistered, or capacity-rejected fills
cannot be silently dropped.**

Markout capacity is bounded on purpose, which is why coverage can be incomplete on a busy
batch: at most 8,192 pending horizon tasks (`MAX_PENDING_POST_FILL_MARKOUTS`,
`market_capture.py:67`); a fill-notification queue of `MAX_PENDING_FILL_REGISTRATIONS =
8192 // 4 = 2048` (`post_fill_markouts.py:24-27`) whose overflow makes `notify()` return
False and increment `dropped_registrations` (`:57, :66-73`) so the fill is never registered
at all; at most 128 registrations per owner-loop drain
(`MAX_FILL_REGISTRATIONS_PER_DRAIN`, `:29`); at most 128 marks per public book update
(`MAX_POST_FILL_MARKOUTS_PER_BOOK_UPDATE`, `market_capture.py:69`, applied at `:1072`).
Task symbols stay subscribed only until their tasks clear. Over-capacity schedules come
back `rejected_capacity` (`market_capture.py:774`); never-registered fills surface as
`not_registered` (`trade_diagnostics.py:460`) and leave no schedule record behind.

Order-flow imbalance requires raw public-event capture and is not derivable from the
decision snapshot. Always available: `top_imbalance`, `microprice`, `opposite_touch_qty`,
`opposite_depth_{qty,notional}_{5,10,25}bps`, `order_to_{touch,10bps,25bps}_depth`
(`trade_diagnostics.py:85-99`).

Grains do not interchange: `command_id` answers "was this order executed well",
`(sleeve, symbol, signal_ts)` answers "did this idea work", a target batch answers "did
concurrent ideas share one shock". The command grain aggregates child executions by
**absolute** quantity (fill VWAP from `abs(signed_qty)` weights, `:850-856`); the canonical
grain for an individual partial fill is `execution_id`, retained in the journal and never
counted as an independent thesis observation. Component rows, order updates, fills,
**venues**, and overlapping horizons are not independent observations — the same idea
expressed on two venues is one bet, and two complete fills of one decision are not two
observations. Never subtract timestamps from different clock domains and call the answer
latency: a local-minus-exchange observation includes clock offset unless an independent
offset bound is applied, which is why the schema's two cross-domain columns are named
`feed_delivery_plus_clock_offset_ns` and `fill_delivery_plus_clock_offset_ns` (`:118-119`)
rather than any form of "latency". The names *are* the caveat; do not rename them.

**Decision funnel.** Capture one pre-gate row per declared source-population key —
implemented as `(sleeve, venue, symbol, signal_ts_ms)`, four parts, distinct from the
three-part TCA decision unit above (`strategy_funnel.py:56-71`). The row carries
`component_scope` when components have genuinely different gate definitions while keeping
the shared decision key, so component rows are never counted as independent ideas. Each
source row must preserve causal feature availability and population/PIT provenance; every
named gate's state (`gate_state()` → pass/fail/missing/not_applicable, `:73-79`); first
rejection, all applicable rejection keys, and accepted target identity; capacity, existing
exposure, cooldown, health and account-risk admission as separately named operational
gates; and a fixed, small feature set chosen before looking at future labels. The table must
not embed future path values — labels join later by stable key. Do not append the same
unchanged rejection every minute: keep one immutable source row plus gate-state transitions
(`:211-216` emits `source` first and `transition` after, deduplicating on
`gate_state_sha256` and raising if the source identity ever changes), and let the read-only
projector fold them into one final row per source key with first evaluation, first
rejection, terminal disposition, evaluation count, and first/last timestamps. Freeze
source-population and transition semantics before enabling the writer, or a convenient
logging grain becomes an undeclared statistical sample.

LONG funnel sources are closed feature rows keyed by symbol and daily `ts_ms`, captured
pre-gate — before `_classify_entry` — with dynamic retrace, cooldown, capacity, health,
unresolved-target, terminal-attempt and publication gates recorded as transitions. CONTINUOUS rows
are `entry_state` symbol/hour rows before the decile and liquidity filters, additionally
carrying component scope for trigger/age differences, with shared health, adverse-pause,
BTC-trend/risk, capacity, reentry, cooldown, crowding, unresolved-target and publication
gates separately named.

**Artifact budget.** A diagnostic run retains at most four claim-bearing payloads:
`manifest.json` (identities, schema, counts, nulls, hashes, deviations),
`execution_tca.parquet` (one row per canonical command), `decision_funnel.parquet` (one row
per pre-gate decision unit) and `path_labels.parquet` (future labels only) — the last two
written by `scripts/data/build_candidate_tape.py:909-910`. Separating future labels into their
own file is the mechanism that keeps lookahead out of the funnel. The verified journal and
capture root stay sources and are not copied into the run; intermediate partitions are
resumable working state; charts and Markdown are regenerated from the tables, and only the
compact evidence card and decision are committed to the research summary. Adding an
artifact requires a named claim, a consumer, and a retention rule.
`scripts/data/build_candidate_tape.py` reads only the preregistered PIT root and writes exactly
one run-scoped diagnostic partition; construct commands from `--help`.

Claim-bearing exports require a quiescent, frozen, read-only capture snapshot. The projector
descriptor-checks every scanned segment and refuses one that changes during its read
(`_read_stable_capture_segment`, `trade_diagnostics.py:1142-1152`), but that is not a
substitute for a filesystem or operator snapshot boundary around the complete capture root:
run the builder against a live root and the table can span a moving root.

**Contracted but unbuilt.** Four command-level field families the contract requires and
`EXECUTION_DIAGNOSTIC_SCHEMA` does not implement: MAE/MFE with time-to-MAE/MFE and
threshold crossing; exit reason and holding time; realized/funding/fee decomposition with a
counterfactual fixed-horizon return; signal-to-order delay and opportunity cost for
rejected, expired, cancelled or partially unfilled intent. The only implementation anywhere
is `mae_72h`/`mfe_72h` in the candidate tape (`scripts/data/build_candidate_tape.py:625-671`).
The TCA table is not the definition of complete.

**Analysis standard.** Every diagnostic read reports median, robust spread, tails,
missingness and effect sizes — not only means — plus calibration residuals between the
modeled book walk/cost and observed execution (the declared consumer of
`book_walk_residual_bps`). For variant selection, ordinary holdout language is insufficient
after repeated search: the complete trial ledger is mandatory, and multiplicity control,
deflated performance statistics or backtest-overfit diagnostics apply **only** when their
assumptions match the actual selection process — deflated Sharpe assumes an iid trial
structure this program's overlapping searches do not have.

Method references. Execution quality: SEC Rule 605 amendments
(<https://www.sec.gov/files/rules/final/2024/34-99679.pdf>) for effective spread,
size-weighted execution speed, and the 50 ms/1 s/15 s/1 min/5 min realized-spread horizons —
the discipline and clocks are borrowed, not the U.S.-equity compliance scope; Bybit order
book (<https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook>) for
snapshot/delta, update ID, cross-sequence and matching-engine time; Bybit private execution
(<https://bybit-exchange.github.io/docs/v5/websocket/private/execution>) for fill identity,
execution time/value, fee, maker state, sequence; Cont, Kukanov & Stoikov
(<https://arxiv.org/abs/1011.6402>) for depth/OFI as short-horizon impact diagnostics,
motivating measuring liquidity state rather than treating the result as a universal crypto
finding; Stoikov micro-price
(<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694>) behind the `microprice`
column. Multiple testing: Bailey et al., *Backtest Overfitting in Financial Markets*
(<https://escholarship.org/uc/item/4hn4t174>) and Harvey, Liu & Zhu
(<https://doi.org/10.3386/w20592>) for trial-count and multiple-testing discipline.

## Evidence boundary and working rules

Demo observes real venue order lifecycle, latency, fees, and funding for its exact epoch.
The retired paper route validated the software path against its declared model and supports
no execution-quality or performance claim. LONG's forward record is demo-only. `carry_hold`'s
corrected benchmark Sharpe is 1.21 (t 2.31) — it does not beat the CONTINUOUS benchmark. A
venue-accounting receipt proves only its named journal/venue interval. Grading:
[`../AGENTS.md`](../AGENTS.md).

- Validate in proportion: `scripts/dev.sh test tests/test_x.py`, then `dev.sh doctor`, then
  `dev.sh check` (ruff, mypy, `.venv/bin/python -m pytest -q`, 2752 passing).
