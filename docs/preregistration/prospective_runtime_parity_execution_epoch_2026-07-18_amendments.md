# Prospective runtime-parity epoch: append-only amendments

This file extends, but does not rewrite, the immutable base contract
`prospective_runtime_parity_execution_epoch_2026-07-18.md`. The base contract
bytes remain pinned by SHA-256
`15edc498adf2bd068c33ff2f791fa3e46f161196db673a839adcf317aba35a31`
in the already-published raw snapshot receipt. Feature and later receipts must
pin this amendment file separately.

## Amendment 4: canonical leading-listing padding

Registered 2026-07-18 14:54 UTC after the first feature-builder attempt failed
closed in its first input chunk and before any feature, target, return, trade,
or P&L output was generated or inspected. The failed working directory is
retained as `.bybit-baseline.working` under the registered feature parent.

The structural check found six leading hourly rows for `MAGICUSDT` on
2022-12-13 where all four OHLC fields are null and both base volume and quote
turnover are exactly zero. The first subsequent row has complete OHLC and
nonzero activity. This is the archive's canonical pre-first-trade densification
pattern, not a priced bar. Existing production owners already give it the
intended semantics: the PIT coverage gate counts densified rows as data
presence, LONG daily aggregation retains the day while executable first-bar
fields remain unavailable, and CONTINUOUS removes rows whose close is null.

Input validation is therefore narrowed prospectively as follows:

- allow and separately count a row only when all OHLC fields are null and both
  base volume and quote turnover are exactly zero;
- continue to reject partial-null OHLC, non-finite or non-positive priced bars,
  nonzero all-null rows, negative/non-finite volume or turnover, blank keys,
  duplicate `(symbol, ts_ms)` keys, and date/timestamp disagreement; and
- make the first and second verified-read padding counts match exactly.

This changes no feature formula, membership rule, rank population, tolerance,
model, outcome boundary, or decision rule. It makes an existing raw-data
representation explicit before the affected feature surface is produced.

## Amendment 5: deterministic runtime-comparator ports and active authority

Registered 2026-07-18 15:47 UTC after source/config inspection and the
completed outcome-blind full-ledger reconciliation, but before any historical
active source-decision, target, lifecycle, return, or P&L transcript was
generated or inspected.

The deployed target producers, not the legacy historical equity engines, own
the active strategy semantics for this parity claim. In particular, source
inspection found that the standard CONTINUOUS component engine applies a
historical `entry_crowding_max_fresh=2` rule and independent per-component
capacity, while the deployed target producer exposes no crowding rule and owns
one shared 25-component book with at most five new component targets per cycle.
The standard curve also omits the deployed accepted-decision BTC-risk state.
Those are retained diagnostic differences, not rules to copy into the runtime
comparator and not authorization to change either strategy.

The offline comparator is therefore frozen to these production authorities:

- LONG uses `long_v11a_profile`, the deployed demo sizing controls
  (`notional_multiplier=1`, `entry_leverage=10`, five new entries per cycle),
  and the production candidate, target, account, and protection owners;
- CONTINUOUS uses `apply_continuous_demo_profile` with the deployed demo
  controls (`btc_trend_gate=uptrend`, `max_active=25`, five new component
  targets per cycle, `entry_leverage=10`, `notional_multiplier=10`, and
  `per_position_notional_pct_equity=2`), including its three ordered component
  definitions, shared capacity, inverse-volatility sizing, and complete
  accepted-decision BTC-risk evidence chain; and
- deterministic equity is USD 1,000,000. Route, clock, market, journal, and
  execution adapters remain explicitly historical and carry no venue-rule,
  latency, capacity, or fill-quality claim.

The source-decision window is `[2023-03-01, 2026-07-10)` for LONG and the
code-owned CONTINUOUS inception window `[2023-04-01, 2026-07-10)` for
CONTINUOUS. Run-scoped features and manifest membership remain the only
historical ranking inputs. At each eligible whole-hour decision boundary, the
frozen hourly close is the deterministic market reference. Software
stop/take-profit evaluation may observe those hourly closes only; it must not
infer an intrabar crossing from hourly high/low or incomplete five-minute
data. Forward target captures retain ownership of real intrabar/runtime parity.

Parity artifacts must separately retain (a) the full frozen source population
and every gate decision, (b) the dynamic accepted target/lifecycle transcript,
(c) request content hashes and BTC-risk predecessor/evidence hashes, and (d)
account journal/state identities. They may compare the runtime transcript with
the legacy historical engine only as a labelled diagnostic; a mismatch cannot
be silently repaired, pooled away, or interpreted as alpha. No return, thesis,
profile, deployment, mainnet, or real-money conclusion is authorized.

## Amendment 6: chronological account port and terminal boundary

Registered 2026-07-18 after the production-function ownership trace and the
request-identity replay repair, but before the comparator generated or
inspected any active historical source decision, target, lifecycle, return, or
P&L transcript.

The historical port has no evidence for sub-hour daemon interleaving or a live
ticker between frozen bars. It therefore uses one declared chronological
schedule rather than inventing timing from thread or filesystem order. At each
whole-hour boundary it (1) presents that hour's frozen close to the production
account-protection owner and immediately processes any resulting risk-owned
zero targets, (2) runs the production LONG planner and immediately processes
its exit-first publication, then (3) runs the production CONTINUOUS planner and
immediately processes its exit-first, independently published component
requests. Nanosecond request ordinals remain those produced by the publication
owner. This is a deterministic comparator tie-break, not a claim that the two
live producer daemons always arrive in that order; the forward capture owns
actual interleaving.

Other historical ports are frozen as follows:

- the price available at boundary `T` is the close of the complete hourly bar
  starting at `T-1h`; missing required prices fail the affected run rather than
  carrying a stale mark;
- the CONTINUOUS 240-day age gate uses the first directly observed public
  archive membership date from the run-scoped PIT manifest, evaluated at the
  deciding bar date. Current launch-time metadata is not projected backward;
- LONG and CONTINUOUS share one deterministic USD 1,000,000 account, matching
  the deployed ownership topology. The account risk policy is deliberately
  non-binding (`10x` component/symbol, `100x` account/initial-margin, maximum
  leverage `10`) so this run tests the production sequencer rather than
  reverse-engineering an unavailable historical operator policy;
- the historical instrument port uses `1e-12` price and quantity grids,
  effectively unlimited displayed size, zero latency, zero modeled fee, zero
  funding, and exact close-price fills. Those fields and their hashes remain in
  the account evidence, but they support no venue-rule, cost, capacity,
  slippage, or economic conclusion; and
- after the last eligible source decision, hourly protection and strategy
  deadlines continue only through the frozen raw-data boundary. Any component
  still nonzero is then replaced by an explicit risk-owned
  `comparator_boundary_flat` target at its last available frozen close. These
  terminal targets are retained and counted, are excluded from strategy source
  decisions and forward-parity support, and exist only to make journal,
  lifecycle, attribution, and final-flatness checks total.

The comparator retains the frozen source-population identities, row-level
static/dynamic gate trace, source decisions, publication order and content
hashes, BTC-risk chain, canonical lifecycle projection, and verified account
hash chain. Validation may inspect discrete structure, coverage, identities,
and flatness only. Monetary P&L, returns, equity curves, and trade-outcome
ranking remain unopened under this engineering contract.

## Amendment 7: sub-millisecond comparator ordering clock

Registered 2026-07-18 after a synthetic one-symbol integration fixture, but
before any frozen active historical decision or lifecycle was generated or
inspected. The fixture correctly failed `stale_decision` when Amendment 6's
ordered producer stages shared an hourly market timestamp but the execution
port allowed zero elapsed nanoseconds between market observation and request
creation.

The deterministic boundary timestamp remains the market observation time. The
create-only request bases are frozen within the following millisecond as
`protection=+0ns`, `LONG=+100,000ns`, `CONTINUOUS=+200,000ns`, and terminal
boundary flats `=+900,000ns`; production publication functions continue to add
their exact ordinal nanoseconds. The historical execution twin's maximum
decision age is therefore one millisecond. All modeled latency components
remain zero. This records causal ordering without moving the market price,
changing a source decision, or claiming live daemon timing.

## Amendment 8: projection performance repair and failed-attempt boundary

Registered 2026-07-18 after terminating the first incomplete active comparator
attempt at code commit `467030bbb3c4b61a5ec468dfe0c9df4f6f2ab5a3`, but before
running its replacement or inspecting any monetary outcome. The preserved
attempt termination receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-467030bbb3c4/termination.json`
with SHA-256
`dd5df88b6d77fe181ba1fb1737b97fa3a62841065d16c425b1f26499954063d1`.
The incomplete attempt is invalid for any strategy or parity conclusion.

A read-only `py-spy` stack sample showed the active thread rescanning every
account event for every historical order batch inside
`canonical_component_execution_anchors`, then repeating that same canonical
anchor projection inside `AccountProtectionEngine.evaluate` after the
comparator had already produced it from the identical verified event snapshot.
The attempt was stopped because this quadratic path was not credible inside
the registered four-hour cap. Inspection was limited to the sampled call
stack, partial structural row counts and chronology, and file identities; no
P&L, return, equity curve, or trade-outcome ranking was calculated or opened.

The replacement may make only these semantics-preserving performance changes:

- construct one order-by-batch/symbol and fill-by-command index per canonical
  account projection while retaining the former direct scan as a test
  reference; and
- allow the comparator to pass the canonical component anchors it already
  built, together with their account-event snapshot, to the protection owner.
  Normal production callers continue to build their own verified projection.

The indexed and direct-scan anchor objects must compare exactly in grouped
entry/reduction tests. The replacement run must also reproduce the preserved
pre-repair structural prefixes byte-for-byte: continuous-gate part 00000
SHA-256
`ae7d56f33b6642a43227b8f4affd4c054f8be59f2fc90f27d9c777a4b5a41eb2`
and LONG-funnel part 00000 SHA-256
`31f4d87816b8972b18626eb8297e726ab6aa15efb48ea8286f977fed7090d83e`.
Failure of either identity invalidates the replacement. These comparisons are
refactor equivalence checks only; the partial rows remain spent structural
diagnostics and carry no thesis or economic evidentiary weight.

## Amendment 9: one canonical projection per dirty account snapshot

Registered 2026-07-18 after terminating the first indexed replacement at code
commit `e45d90c55aa543ceeb7cbbcd9dfff688978218fe`, but before another
replacement run or any monetary inspection. Its create-only termination receipt
is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-e45d90c55aa5/termination.json`
with SHA-256
`701386b22989a35c39bb5cd544dc9377a02caa66d10dbd6b2c6fefa688cbc8ed`.
That attempt passed both Amendment 8 byte-identical prefix checks, proving the
fill index and cached protection anchors preserved the observed prefix, but it
remains incomplete and invalid for a parity or strategy conclusion.

Two later read-only stack samples showed a second performance defect: one
dirty-account refresh replayed the identical event tuple through
`reduce_account_events` independently for the LONG trade projection, the
CONTINUOUS trade projection, and then the component-anchor projection. The
second call alone remained active more than ten minutes at the partial
boundary. No outcome was opened; inspection again covered only call stacks,
prefix identities, chronology, and process performance.

The next replacement may derive one `CanonicalAccountProjection` per dirty
event snapshot and share its accepted batches, quantity tolerance, component
revision evidence, and execution anchors across both sleeve projections and
protection. It may reuse the account owner's in-process materialized state only
when both `events_applied == len(account_events)` and its rolling state hash
equals the final event's state hash; any mismatch fails the run. The context is
consumed synchronously before another account mutation. Exact terminal entry
attempt sets may also be cached from that same snapshot instead of rescanning
unchanged events each hour.

Direct replay, trusted-state context, and shared-context trade/anchor outputs
must compare exactly in tests and on a verified account-journal sample. The
replacement remains bound to Amendment 8's two byte-identical trace prefixes.
This is a read-model performance repair only: it cannot alter source data,
gates, source decisions, targets, request order, execution, lifecycle rules,
BTC-risk state, or the ban on inspecting monetary outcomes.

## Amendment 10: hash-bound protection state snapshot

Registered 2026-07-18 after terminating the shared-projection attempt at code
commit `573ca637763b20687e82a7887baa4d43c1128f0a`, before another run and
without monetary inspection. Its termination receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-573ca637763b/termination.json`
with SHA-256
`28eac53954835d799e066642ecba4843037ab05f8fddb587ba4fa1b89360a738`.
The attempt again reproduced both registered prefixes exactly, but remains
incomplete and conclusion-ineligible.

The shared canonical context removed all sampled journal replays. The next
read-only stack sample found the protection owner calling `kernel.state()`,
which deep-copied the entire materialized account state only to iterate current
component targets. That redundant history-sized copy prevented the next trace
partition from closing promptly.

The replacement may pass the same in-process account-owner state snapshot into
`AccountProtectionEngine.evaluate` together with its event snapshot and
canonical anchors. Protection must independently require the same exact
binding as Amendment 9: applied-event count equals snapshot length and rolling
state hash equals the last event state hash. Missing or mismatched provenance
fails closed. Normal production callers retain the defensive replay copy. The
trusted snapshot is read synchronously before another comparator mutation and
does not change trigger logic, target construction, publication, accounting,
or any registered trace identity requirement.

## Amendment 11: production-ordered LONG price dependencies

Registered 2026-07-18 after the first non-performance comparator failure at
code commit `55be92283972217841ffc16b32a785632959250f`, before its
replacement and without monetary inspection. The failure receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-55be92283972/termination.json`
with SHA-256
`ec37b1bb95d8e7aac4780716504f74d87aba6a93ec9082e824d796906167c80a`.

At `2024-08-13T01:00:00Z` the comparator requested a frozen hourly price for
`CANTOUSDT` and correctly refused to carry a stale value when that day's file
was absent. Subsequent structural diagnosis showed the request itself was not
production-equivalent. The CANTO daily row had a raw pump trigger but only 82
days of history, null PIT universe membership and 90-day turnover, both active
regimes false, and production `_classify_entry == None`. The direct archive and
hourly reconstruction end on 2024-08-12 and the PIT manifest has no CANTO row
on 2024-08-13. The production LONG selector applies classification, exposure,
cooldown, and delay gates before its live-price lookup; therefore CANTO's price
was not decision-required. The historical port had instead requested prices
for every raw pump source before calling that selector.

The replacement may discover strict LONG price dependencies with one pure,
observer-free call to the production selector using a positive read-recording
price probe. Only symbols whose selector path actually reads a price are then
loaded from the frozen reconstruction. The authoritative selector runs again
with those real prices and the normal funnel observer. A missing price for any
symbol that reaches the production price read still fails the run; no stale
fill, row drop, inferred price, or relaxed gate is allowed.

This repair must reproduce every closed trace partition from the failed
attempt byte-for-byte: continuous-gate parts 00000 through 00007 and
LONG-funnel parts 00000 through 00010. Those hashes are pinned in the runner.
The partial attempt remains incomplete and has no strategy or economic
evidentiary weight.

## Amendment 12: separate strict selector prices from optional funnel prices

Registered 2026-07-18 after terminating the first replacement at code commit
`3d1c407dcdf1794104e00e80e04e2d22ab607d69`, before executing another
replacement and without monetary inspection. Its create-only termination
receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-3d1c407dcdf1/termination.json`
with SHA-256
`75382d0ed1c6e75f9fbdb2bd0f018c955a488850ca39539b9d9f427d211d6dae`.
The replacement implementation was prepared after the structural failure but
must not execute until this amendment and that receipt are hash-pinned by the
runner at a clean commit.

The first closed CONTINUOUS gate partition remained byte-identical, but the
first 20,000-row LONG funnel partition changed from registered SHA-256
`31f4d87816b8972b18626eb8297e726ab6aa15efb48ea8286f977fed7090d83e`
to `9f872686dd0b1a1c1aaed9951c9fcc3985048586d54fe7ff7ecbcdda6fae15ae`.
Shape and schema were identical. Differences were confined to
`entry_ts_ms`, `gate_entry_anchor`, `rejection_keys`, `first_rejection`, and
their `gate_state_sha256`. The attempt was stopped immediately under the
registered prefix-identity rule; no active-decision equivalence is inferred
from this partial comparison.

The cause was a dependency conflation inside the production selector. Its
active candidate path reads live prices only after classification, exposure,
cooldown, and entry-delay gates. Its non-participating funnel observer, called
inside the same selector, reads prices for the wider raw-pump population. The
first Amendment 11 implementation supplied only strict active-path prices to
both consumers, so previously populated optional observer fields became null.

The next replacement must first load every active-path price discovered by the
observer-free production-selector probe, with any missing or invalid required
price failing closed. It may then augment that same price map with frozen
prices for the raw-pump observer population when those exact prices exist.
Absence of an observer-only price leaves that optional diagnostic unset and
must not fail or alter active selection. The authoritative selector still runs
once with real frozen prices and the registered funnel observer. All 19 closed
trace partitions from the CANTO failure remain pinned byte-for-byte.
