# Autonomous improvement cycle 002: continuous venue-order ownership

## Scope and baseline

- Audit timestamp: `2026-07-16T01:15:10Z`.
- Current audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d` on
  `codex/demo-operational-cutover`, plus the named local changes below.
- The baseline moved during the cycle from `2d42c7b` to `cd2abdc` because a
  concurrent actor committed and pushed the pre-existing dirty worktree. The
  current diff was re-read and all final validation ran against `cd2abdc`.
- No VPS, authenticated venue session, credential, deployment, staging, commit,
  or push was used by this cycle. No strategy or research parameter changed.

## Finding and consequence

`require_bybit_demo_order_ownership()` proved account-wide regular and
conditional order ownership only once, during account-owner startup. The
periodic `BybitAccountReconciler` subsequently recovered known command history
and compared positions, but never re-read account-wide open orders. A manual,
stale, or otherwise unowned order appearing after clean startup could therefore
remain outside kernel working exposure while reconciliation continued to report
healthy.

The first three prospective regressions demonstrated the blind spot before the
implementation:

- a post-start regular order was reported healthy;
- a post-start conditional order was reported healthy; and
- a failed conditional-order query was reported healthy instead of unknown.

All three failed before the fix and passed after it. This is a capital-safety
evidence defect even on demo: the kernel must not claim complete account state
when an authenticated venue order is unowned or the account-wide read is
incomplete.

A second defect was found while tracing that proof to the private REST boundary.
`BybitPrivateClient._cursor_result_list()` stopped at `max_pages` and returned
the accumulated prefix even when `nextPageCursor` was still nonempty. It also
accepted a repeated cursor until the cap. Thus an API named `get_open_orders()`
could silently return only a prefix, undermining the new ownership proof.

## Implementation

- Extracted a side-effect-free `inspect_bybit_demo_order_ownership()` primitive
  from the startup gate. It reads both the all-kinds query and an explicit
  `StopOrder` query, validates rows, deduplicates by durable venue/client
  identity, and classifies unowned orders.
- Kept startup behavior fail closed and refactored it to consume the shared
  inspector, avoiding divergent startup and periodic ownership rules.
- Added the inspector to every position-reconciliation pass. Query, validation,
  or verifier failure becomes a global `venue_order_ownership` mismatch;
  unowned orders become symbol-scoped mismatches. The reconciler observes and
  journals only: it never cancels, adopts, or mutates a venue order.
- Included ownership status and row counts in reconciliation semantics so a
  clean/unowned/unknown transition is journaled immediately.
- Required every open-order row to contain a nonempty symbol and at least one
  durable string identity. A kernel command can own a row only when its durable
  identity and symbol both match; an identity reused across symbols fails
  closed.
- Global unknown ownership blocks even a reducing request. A known unowned
  order blocks requests for its symbol; unrelated-symbol reductions retain the
  existing deliberately narrow risk-reduction behavior.
- Changed the shared private cursor walker to reject non-advancing cursors and
  cap exhaustion with a nonempty cursor. `get_open_orders()`, `get_positions()`,
  and authenticated instrument-rule reads can no longer return a silently
  truncated prefix through this helper.

## Regression coverage

Coverage now includes:

- regular and conditional orders appearing after a clean first pass;
- either ownership query being unavailable;
- exact kernel-command ownership;
- missing and contradictory order symbols;
- journal-verified native protection ownership;
- preservation of prior reconciliation/service fakes at the new REST boundary;
- complete multi-page open-order reads;
- page-cap exhaustion; and
- repeated/non-advancing pagination cursors.

## Validation

- Focused local suite: 194 passed in 0.93 seconds.
- Full local pytest suite: 1,598 passed in 20.44 seconds.
- Repository-wide Ruff: passed.
- Package-wide local mypy: 85 modules passed.
- Locked compatibility environment: Python 3.11.5, mypy 1.20.2.
- Locked focused suite: 194 passed in 1.03 seconds.
- Locked package-wide mypy: 85 modules passed.
- Locked focused Ruff: passed.
- `git diff --check`: passed before the report was written.

This is a correctness/reliability change. No throughput or latency improvement
is claimed, so no before/after performance benchmark applies. The behavior cost
is explicit: each reconciliation now performs two account-wide paginated
open-order reads. At the current default two-second reconciliation interval,
that is at least one additional private REST request per second when both reads
fit on one page, and more when they paginate. No claim is made here that this
load is within a mutable external venue quota.

## Changed paths

- `liquidity_migration/account_service_bybit.py`
- `liquidity_migration/account_reconcile.py`
- `liquidity_migration/bybit.py`
- `tests/test_account_reconcile.py`
- `tests/test_account_service.py`
- `tests/test_liquidity_migration_bybit.py`

## Limitations and next candidates

- Tests use deterministic fakes; no authenticated demo read was needed. This is
  not a live-runtime parity or deployment claim.
- Durable ownership does not yet prove exact side, remaining quantity, or
  reduce-only semantics against the kernel command. That needs a carefully
  specified reconciliation rule that tolerates fills already in flight without
  normalizing real contradictions away.
- A kernel working command absent from both open orders and terminal history is
  not classified by this new account-wide comparison; the existing per-command
  recovery still governs submission-uncertain cases.
- The two extra reads should be observed in real demo telemetry before changing
  cadence. A guessed quota or arbitrary cache interval would weaken freshness.
- Highest-value remaining candidates are the PIT tail false-positive in
  `volume_events_pit.py`, empty incremental-tail coverage advancement in
  `downloaders.py`, watchdog/systemd ownership coupling, and cross-branch VPS
  deployment races. PIT-dependent research must not proceed until the PIT
  requirement defect is resolved.
