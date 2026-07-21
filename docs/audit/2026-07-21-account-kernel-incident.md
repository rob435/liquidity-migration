# 2026-07-21 account-kernel incident audit

Scope: the 01:00--12:20 UTC Telegram transcript supplied on 2026-07-21,
the canonical demo journal and inbox, authenticated Bybit demo position/order
reads, owner health/systemd state, and the execution, reconciliation,
protection, public-market, and notification paths that produced those reports.
The audit started from local commit `a808c5877b201432798ae6e73aaa94338b7f1332`.
The VPS remained on `a7363070008266888b652104dfdd64f907507f3e`.

This document distinguishes three things deliberately:

- the incident facts under the old deployed code;
- the old deployment's later self-recovery;
- the local fixes and tests, which are not deployed or operational authority.

## Outcome

The most severe event was real: DEXEUSDT was open while its intended native
disaster stop was absent and already crossed. The owner repeatedly retried an
invalid Bybit stop mutation and blocked new exposure, but it had no software
flatten transition for this condition. The stop became installable only after
the mark moved back through it. That recovery was contingent market luck, not
an acceptable safety mechanism.

The local remediation converts the condition into authenticated, durable,
idempotent account state and a strict reduce-only close. It also closes the
public-L2 pre-open watchdog gap, preserves lifecycle confirmations, removes
misleading accounting promises, prevents notification truncation, and keeps
one failed protection symbol from starving the rest. A subsequent execution-
boundary audit also removes the original fill-then-install window: every demo
entry now requests durable provisional native protection in the same Bybit
create call and is independently verified/re-anchored after the fill. No live
account mutation or deployment was performed during this audit.

## Incident reconstruction

All times below are UTC. Telegram's surrounding display timestamp was one hour
ahead; the message body supplied the UTC incident time.

1. DEXEUSDT opened short `2.6 @ 12.659` at journal sequence 26879,
   2026-07-21 12:11:12.
2. At 12:12:49 Bybit rejected `stopLoss=12.913` because its MarkPrice/base price
   was `13.0944`. The exact error exposed internal integer units:
   `StopLoss:1291300000 ... base_price:1309440000` (ErrCode 10001).
3. The reconciliation alert became CRITICAL at 12:13. The old owner kept
   retrying the same mutation; it did not flatten.
4. Once price receded, the old code eventually installed the stop at sequence
   26957, 12:20:14. The unprotected interval was therefore about eight minutes.
5. The position later hit take profit, not the disaster stop: trigger sequence
   27058, close fill sequence 27066 at 13:02:22, buy `2.6 @ 11.127`.
   Journal close/P&L sequences 27067--27068 recorded account-net
   `+3.94918602 USDT`; native position-flat followed at 27069.
6. A fresh read-only audit at 13:49 found the deployed owner active/running
   with zero restarts, healthy current owner state, requested-symbol readiness,
   no local or venue positions, no aggregate target, working order, open venue
   order, or pending/processing/failed request. The deployment still verified
   exactly at `a736307...`, profile `operational`, with `DEMO=true` and
   `REAL_MONEY=false`.

The flat/healthy observation is point-in-time evidence. It proves the incident
ended under the old deployment; it does not validate the local repair.

## Findings and fixes

### 1. Crossed native stop had no deterministic recovery path

Old behavior combined several faults:

- stop planning consulted a frozen journal market reference before an
  authenticated venue stop/mark observation;
- a missing stop already beyond its trigger was flattened into a generic
  reconciliation error and retried as a venue mutation;
- no durable software flat represented the required safety transition;
- startup could abort on the very breach that only the owner was authorized to
  reduce;
- FIFO market warmup and missing L2 could delay a future recovery request;
- the first failing symbol could prevent protection work for sibling symbols.

The repair now:

- treats an authenticated position snapshot's current `stopLoss` as the first
  proof: an exact matching Full-position stop is adopted without mutation;
- requires a positive authenticated MarkPrice before repairing a missing or
  mismatched stop;
- turns an already-crossed missing stop into `breached_unprotected`, including
  the exact plan, stop, signed quantity, mark, source, and observation time;
- recognizes only the narrow Bybit ErrCode 10001 crossed-stop shape and safely
  normalizes the integer-price representation seen in this incident;
- latches the breach across price recovery and process restart. Only an
  authenticated matching stop or authenticated flat position terminalizes it;
- publishes one atomic zero-target request covering accepted components and
  every newer pending/processing nonzero component revision for the symbol;
- binds FIFO-bypass and authenticated-mark fallback authority to a canonical
  `software_flat_requested` journal row and the exact immutable request hash;
- bypasses only uncommitted queue work. A prior journal-committed batch remains
  a crash-replay barrier;
- re-proves fresh same-symbol venue/local quantity agreement and strict risk
  reduction before execution. The authenticated breach mark may replace
  unavailable L2 only for that exact authorized close;
- allows only a structured breach-only reconciliation report to keep startup
  alive for recovery; text lookalikes, position drift, unknown orders, and
  transport failures still abort;
- attempts every open symbol and aggregates failures.

The durable authority check matters: an ordinary strategy producer can still
request a normal flat, but cannot impersonate this priority path by choosing a
special filename or metadata pattern.

### 2. Public L2 could hang before `on_open` without a watchdog deadline

The stream published a WebSocket object before `run_forever` had established a
transport. Subscription timestamps did not exist until `on_open`, so the
watchdog skipped a connection that never opened. This is a credible cause of a
stale-capture episode like the 03:35 alert, although the three-minute Telegram
episode alone cannot prove which network failure occurred.

The stream now separately tracks connection-attempt time, transport-open state,
subscription time, first frame, and connection generation. A never-open or
never-first-frame generation is detached and closed at the 30-second bound; a
previously active silent orderbook retains the 120-second bound. Recorder work
and subscription socket writes no longer hold the watchdog state lock, and the
deadline begins before a potentially blocking send. Retired generations cannot
re-open, send new subscriptions, or refresh readiness.

### 3. “Awaiting venue reconciliation” warnings had no eventual confirmation

When quantity truth was unhealthy, the notifier correctly downgraded an open
or reduction to a local-only warning. It nevertheless advanced its event
cursor after successful Telegram delivery, so the same lifecycle fact was not
eligible for a later confirmation once reconciliation recovered.

Notification schema v3 stores pending lifecycle confirmations transactionally.
A later healthy position-truth pass releases each exact confirmation once; the
state advances only after successful delivery.

### 4. Accounting text promised finalizers that do not exist online

The repeated phrases “provisional (funding, venue closed-PnL pending)” and
“component P&L attribution pending” implied future online completion. The
implemented account journal instead records fill-reconstructed account P&L,
journals funding as separate settlement rows, leaves venue closed-P&L to an
offline accounting audit, and cannot allocate account-netted reductions back
to components uniquely.

Notifications now state those boundaries directly:

- `accounting scope: fill reconstruction`;
- `funding journaled separately`;
- `venue closed-PnL not cross-checked online`;
- `component P&L not allocated (account-netted)`.

This changes claims, not historical accounting bytes.

### 5. One Telegram payload could acknowledge omitted tail facts

The old renderer truncated a combined message to the transport bound while the
notifier committed the final cursor. A sufficiently busy interval could
therefore lose tail events permanently.

The renderer now emits lossless pages of at most 3,900 characters and commits
notification state only after every page succeeds. A partial transport failure
may duplicate an earlier page on retry, but cannot acknowledge an unsent page.

### 6. Entry and native-stop submission were separate effects

The incident was possible because the old owner first submitted and filled the
market entry, then called `set_trading_stop`. No local scheduling improvement
can make two network effects atomic, and retrying the second effect cannot
protect the interval before it succeeds.

The repaired boundary now:

- persists a provisional MarkPrice Full-position stop on every
  exposure-increasing command and refuses a demo entry without it;
- derives the outermost component price from durable per-component references,
  applies the account fallback only when none is explicit, rounds outward, and
  clamps scale-ins to the already-active native stop;
- sends the stop fields in the same `/v5/order/create` request as the entry;
- treats Bybit's asynchronous create acknowledgement as acceptance, not proof,
  and still verifies position/order truth before claiming protection;
- re-anchors to confirmed fill evidence, while safely handling a stop execution
  coalesced before its entry fill in one private-stream message;
- performs non-exposure leverage negotiation first, then writes
  `submission_attempt` immediately before the exposure-capable order-create.
  The command-age bound is rechecked after leverage setup; an ambiguous
  leverage response can retry, but ambiguous entries are never resent. Stale
  never-attempted entries are refused, concurrent entry attempts have one
  atomic journal winner, and missing venue evidence blocks reconciliation
  health; reduce-only retries remain allowed;
- gives an already-bound child venue order identity precedence over an echoed
  parent `orderLinkId`, while routing unbound native-stop provenance before
  normal command matching. This covers stop-first/entry-second messages,
  scale-ins with an older active stop, and later partial-fill rows after restart;
- requires the complete authenticated venue position snapshot, not local
  reconstructed zero alone, before clearing a crossed-stop breach latch.

This does not claim zero network latency or exchange infallibility. It removes
the known unprotected two-call design and makes every remaining uncertainty
observable and fail closed.

## Cleared observations

- The 03:35 stale-L2 alert resolved at 03:38. It was a real freshness failure,
  but there is no evidence that the account owner process restarted or that
  venue/private truth was lost.
- `entry pause`, event, age, capacity, and same-signal-reentry rejections in the
  hourly funnel are implemented admission controls, not kernel failures.
- The later profitable DEXE close does not reduce the severity of the
  unprotected interval.

## Verification

- Account execution/protection focused tests: 273 passed.
- Full repository gate: doctor ready; ruff clean; mypy clean across 114 source
  files; 2,239 passed / 3 skipped.
- The full gate caught two integration seams outside the focused slice: a
  paper/demo import-boundary regression from a runtime-only type import, and an
  old protection-manager test double that did not implement the expanded
  identity-evidence contract. Both were corrected and the complete gate then
  passed.
- Scoped Graphify AST and semantic refresh: 5,238 nodes, 18,862 edges, 334
  communities.
  HTML generation was intentionally skipped by Graphify above its 5,000-node
  safety threshold; `graph.json` and `GRAPH_REPORT.md` were refreshed.
- `git diff --check` passed.

The tests cover never-open and retired WebSocket generations, attached-stop
parameterization and durability, partial fills, scale-in geometry, same-message
entry/stop ordering, stale and ambiguous submissions, concurrent submission
claims, authenticated venue-stop adoption, crossed-stop normalization,
contradictory rejection shapes, multi-symbol aggregation, breach idempotence,
restart with recovered price, terminalized recovery across a second restart,
current plus queued revision coverage, journal-hash authority, committed replay
barriers, absent L2, strict reduce-only execution, startup text spoofing,
lifecycle confirmation, current-hazard bootstrap, and lossless pagination.

## Residual boundary

No finite patch can prove that a networked trading kernel will “never break.”
The defensible claim is narrower: every failure class evidenced by this log is
now either recovered deterministically or fails closed without silently
granting exposure authority. A same-request attached stop substantially narrows
the vulnerable boundary, but Bybit acknowledges order creation asynchronously;
only later private/REST truth proves the installed stop. Provider unavailability,
contradictory account truth, journal corruption, or ambiguous submission still
blocks automatic action and requires preserved evidence and operator diagnosis.

These changes are local, uncommitted, and undeployed. A rollout requires the
normal stopped install, exact clean commit, new operational authority receipt,
activation, and read-only post-activation verification. It remains demo/paper
only and grants no mainnet authority.
