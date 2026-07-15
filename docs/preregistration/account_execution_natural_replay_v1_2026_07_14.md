# Natural LONG/CONT account replay and twin holdout v1

Status: prospective. Registered at `2026-07-14T11:36:25Z`, before any fresh
natural-cutover epoch, LONG/CONT target capture, paper/historical replay, or
holdout execution-twin result. The earlier V4--V7 runs are spent operational
diagnostics. V8, if it runs, is calibration/training evidence only. No result
from the Strategy Overhaul work running separately on the big PC is an input to
this contract. As of the last prospective amendment below, V7 has failed and
neither V8 nor the fixed 120-hour natural window has run. Implemented constructors and replay
paths are not run artifacts: no natural reset, owner-first receipt, stopped
seal, offline replay result, fresh-deploy epoch, authorization, or deployment
exists for this contract. The candidate now implements the exact stopped-tree
path/hash and derived-output provenance checks through target-replay manifest
v2, event parity v3, captured-account replay v3, comparison scope v3, kernel
receipt v4, natural sufficiency v3, and authority aggregate v4. That closes an
implementation gap only; it does not satisfy any registered evidence gate.
Locally declared analysis timestamps constrain internal ordering but do not
authenticate wall-clock execution. The target manifest assigns its completion
time after replay construction, and the source-reopened dependency hashes carry
the causal provenance.

Owner scope decision on 2026-07-15, before V8 or any natural result: this
five-day raw-tape holdout is deferred optional research and may be collected on
another machine. It is no longer a prerequisite for demo/paper VPS operation.
This changes no registered natural window, input, floor, comparison, stopping
rule, or evidence interpretation. If the study is later activated, both owners
must explicitly set `ACCOUNT_RAW_MARKET_PERSISTENCE=1` and every rule below
still applies. Operational mode with bulk persistence disabled cannot be
relabelled as this experiment and supplies no natural replay, drift, parity,
promotion, or alpha conclusion.

Prospective clarification at `2026-07-14T12:20:11Z`, before V7 or natural
results: the V7 live paths will be reused by the mandatory second reset, so an
explicit archive-source map must preserve and re-open the training bytes. V7
and natural source identity must be distinct rather than equal. This resolves
an implementation contradiction; it changes no target, sample, threshold,
window, or decision rule.

Prospective clock-evidence correction at `2026-07-14T15:00:52Z`, before any
natural-window result: one pre-window offset cannot support a 120-hour feed
latency claim. The frozen registered receipt is now the first member of a
source-reopening series. Capture the same credential-free 21/5 Bybit demo
public-time receipt on a six-hour target cadence, allow at most eight hours
between observed members, and retain a member on each side of `[T0,T1]` no
more than six hours from its endpoint. Feed rows use piecewise-linear offset
estimates at `local_receive_ts_ns`. Their reported uncertainty is the larger
bracketing receipt error plus the observed offset change across that bracket;
it is a sensitivity estimate, not a hard bound on an unsampled excursion.
Request/ack RTT and exchange-timestamp fill spacing remain clock-independent.
No V7 input, latency threshold, sample floor, or holdout result was changed or
inspected for this correction.

Prospective implementation-binding amendment at `2026-07-14T15:57:14Z`, before
V7 or natural results: the exact-candidate Linux gate is the manual
`candidate-ci` workflow-dispatch path. It must check out the candidate head and
pass full Ruff/pytest while every SSH-key, host-key, VPS verify, install,
recovery, and deploy step is recorded as skipped. A pull-request merge SHA or a
dispatch that contacted the VPS is not candidate evidence. The pre-window
freeze binds source configuration files; each natural producer separately
captures its fully resolved in-memory configuration before constructing public
market-data resources, and a stopped post-window bundle re-opens both exact
receipts. Replay, sufficiency, parity, drift, and authorization must bind that
bundle rather than infer effective settings from opaque flags.

The same amendment closes the final source epoch before analysis: after the
post-window safety targets converge, stop every managed unit and capture the
authenticated venue-accounting/final-flatness receipt first. Then create one
stopped-natural-epoch seal over the exact old mutable roots and evidence
namespace. Offline replay may read only that sealed source set and must write to
disjoint isolated roots. A later deployment, if every evidence gate passes,
must use a separate fresh-deploy epoch of ten empty roots (demo/paper account,
inbox, and capture plus LONG/CONT demo/paper data roots), with its exact
per-unit environment materialized and verified before startup. The stopped seal and fresh-deploy
epoch are machine evidence for authorization; neither grants authority. These
changes resolve implementation/source-mutation gaps and reorder accounting
before replay. They change no window, floor, threshold, comparison, or decision
rule.

Prospective gate-binding amendment at `2026-07-14T22:33:59Z`, before V7 or any
natural result: every configurable freshness/model/accounting parameter on the
registered path is now fail-closed instead of allowing a receipt to reproduce
its own weaker rule. Candidate demo rules may be at most 604,800 seconds old;
owner readiness and the post-window safety publisher use at most 30 seconds of
owner/capture age. The captured-account replay and both frozen V7-derived twin
configs use exactly 250 ms maximum decision age; replay market and capital
snapshots use exactly 5,000 ms; the decision replay uses the fixed `p50`
latency/slippage baseline and canonical kernel/twin seeds. Venue accounting
requires at least two trade rows, one closed-PnL row, and one funding row, with
maximum tolerances `1e-12` quantity, `1e-8` price/amount, and `1e-9` relative.
Commands and source-reopening verifiers reject wider ages/tolerances, lower
floors, alternate quantiles, or alternate seeds. The freeze also requires exact
demo/paper route and risk sources, parses their effective EnvironmentFile
values, requires semantically identical risk policies, and binds the distinct
three-symbol V7 seed rather than aliasing the candidate-universe file. These
values were defaults and runbook intent before the amendment; making them
machine-enforced closes a self-described-gate gap without inspecting or
changing any V7/natural outcome.

Prospective demo-rule-probe binding amendment at `2026-07-14T22:50:12Z`,
before V7 or any natural result: the registered V7 and natural rule probes use
exactly 100 basis points of PostOnly price distance, no more than 200 USDT of
probe notional, no more than five private requests per second, and no more than
10x tested leverage. The probe rejects an out-of-contract request before
reading credentials or touching the venue. Its self-hashed receipt records the
notional, distance, and rate contract, and every consumer revalidates those
fields plus each symbol's tested distance and leverage. Every recorded attempt
must also stay inside the declared notional cap and reproduce its structural
quantity step, minimum, maximum, price notional, and accepted rule minimum.
Lowering the notional or request-rate ceilings remains conservative; widening
either ceiling, changing distance, testing above 10x, or embedding an
out-of-contract attempt cannot self-describe a pass. These were the already
documented V7/natural command values; machine enforcement closes a
self-described-receipt gap without inspecting or changing an outcome.

Prospective runtime-safety amendment at `2026-07-14T23:16:37Z`, before V7 or
any natural result: non-finite freshness and readiness values are invalid, not
implicit opt-outs. The shared rule loader rejects a non-finite maximum age;
the V7/natural owners accept no more than the already registered 168 hours of
rule age and 30 seconds of queue-head market warmup, and validate both before
credentials or owner startup. The natural freeze independently parses those
effective route values. `REAL_MONEY` must be unset or one of the explicit false
forms; an unknown spelling fails the freeze and the credential-free V7 target
producer before marker, Git, credential, or venue work. Private Bybit HTTP and
WebSocket construction is restricted to `api-demo` (`demo=true`,
`testnet=false`), and every mutation rechecks that realm before the
credential-bound account lease. Fresh-epoch root values are reopened through
the authority verifier and transferred to Bash only as NUL-delimited data;
generated systemd EnvironmentFiles are never shell-evaluated. The older private
Bybit and account-route EnvironmentFiles are also descriptor-read as
current-user-owned, single-link, exact-mode-`0600` data, with only fixed
allowlisted keys transferred; sleeve toggles use a strict three-key data
parser. These changes close fail-open representations of the existing
demo-only/freshness/readiness contract. They do not change a nominal threshold,
target, sample, window, or observed outcome.

Prospective publication-snapshot clarification at `2026-07-14T23:55:11Z`,
before V7 or any natural result: the post-callback capture must locate the one
current pending, processing, completed, or failed request while holding the
route-bound inbox lock, then parse that request and its arrival sidecar from
descriptor-bound regular-file snapshots. Symlink or hard-link substitution,
path/descriptor identity drift, changed canonical contents, multiple queue
copies, or a changed/missing arrival binding fails the capture. The evidence
path returned to the target tape is the lexical path bound to the accepted
descriptor, not a second `resolve()` result. This makes the existing
byte-for-byte durable-publication requirement machine-enforced; it changes no
target, window, sample floor, threshold, comparator, or stopping rule.
The paper owner and every non-owner fresh-epoch target/hedge/refresh unit also
explicitly remove demo/mainnet credential names, `REAL_MONEY`, and unused
Telegram credentials from their inherited systemd environment; only the demo
account owner retains demo mutation credentials.

## Prospective corrected-defect training successor — 2026-07-15T02:18:26Z

This amendment was registered after V7 closed under its owner-error rule and
verified-flat recovery, but before any V8 target or any natural-window result.
V7 is spent and cannot supply the training calibration required by this
contract. Its failed receipt, all 30 transition execution observations (15
round trips), funding hold, and recovery fill are excluded from every V8 and natural floor.

The new prospective training contract is
`account_execution_calibration_v8_2026_07_15.md`. It fixes only the V7
reconciliation-freshness ordering/cache path and concurrent Close/P&L journal
finalization while retaining the exact sample, size, risk envelope, clock
contract, smoke floors, partial-fill floors, abort rule, and no-retry rule.
Reopening is therefore a corrected-defect study on a new forward-time and
six-root epoch, not a relabelled V7 retry or a threshold rescue.

Every normative reference below to a *passing* V7 calibration, V7-derived
configuration, V7 training source, or V7 archive is prospectively rebound to
the single passing V8 artifact and its immutable archive. Existing schema keys
and command names such as `v7_training`, `--v7-archive-map`, and `v7-archive`
remain compatibility labels; they do not permit the failed V7 receipt. V8 must
pass its full schema-v3 gate, be flat/stopped, and be materialized before the
second six-root reset. All 120-hour holdout boundaries, comparisons, floors,
clock-series rules, accounting rules, sealing order, and decision rules below
remain unchanged. No natural outcome has been inspected.

## Claim and permitted action

This experiment asks four deliberately separate questions for the exact
candidate commit and effective demo/paper configuration:

1. Do natural LONG and CONT producers durably publish the same frozen target
   schedule when its recorded events are replayed through the common event
   clock?
2. Does the production account kernel produce the same strategy-to-order plan
   from those requests in historical, paper, and demo, allowing only declared
   environment fields and an absolute finite-quantity tolerance of `1e-12`?
3. Does the V7-calibrated market-order twin remain within its prospectively
   frozen latency, fill, and adverse-slippage envelope on an independent
   natural demo holdout?
4. Do the actual demo account journal and venue records reconcile target,
   command, order, fill, fee, realized P&L, funding, and final flatness?

Only a pass on every independent gate permits creation of an account-cutover
deployment assessment. It does not authorize a deployment. This study makes no
alpha, strategy-performance, raw-market-data equality, mainnet, HFT, passive
queue, capacity, or real-money claim. Historical and paper are modeled ports;
actual venue fills and P&L are not expected to equal their modeled values.

The deterministic scheduling claim is limited to the registered natural LONG
and CONT producers and the offline historical/paper/demo-labelled replay of
their captured bytes. It does not cover the hedge, RMOM refresh, liveness
timers, every historical signal-selection loop, or a live paper-producer run.

## Preconditions and frozen identities

No natural epoch may start until all of the following exist and are hashed in a
mode-`0600` freeze manifest:

- one exact clean commit that has passed the complete local suite and the
  repository Linux CI workflow through the non-contacting `candidate-ci`
  dispatch described above;
- the exact `origin/main` base and a fast-forward-only promotion contract. The
  Strategy Overhaul work on the big PC may continue separately, but it must not
  advance `main` or alter this cutover commit during the frozen evidence
  window. Any required merge/rebase/integration creates a new candidate commit
  and requires validation and all commit-bound forward gates again;
- the LONG and CONT source configuration files, account route, absolute risk
  policy, disaster stop, account id, and all six post-reset natural
  account/inbox/capture root paths;
- a passing V7 schema-v3 full twin receipt. Schema v3 separately identifies
  clock-adjusted socket-send-to-first-fill and exchange-fill-to-local-fill
  response; a schema-v2 receipt cannot configure paper. A smoke-only V7 result or failed
  partial-fill gate leaves this experiment blocked and paper stopped;
- an immutable V7 archive-source map created while the V7 roots still name the
  training epoch. It maps every live path embedded in the V7 receipt to the
  archived journal/capture file that preserves those exact bytes and hashes.
  The later reset reuses the lexical live-root names, so dereferencing an
  embedded V7 path after reset would read holdout data and is forbidden;
- a fresh independent clock-offset receipt observed no more than six hours
  before T0, frozen as the initial member of the periodic series, and the exact
  twin baseline and `p95` stress configurations derived mechanically from V7;
- one frozen candidate-universe artifact, one complete demo-rule receipt for
  that universe, plus the exact small initial L2-subscription seed file;
- the reset/archive receipt created with the reset's explicit `--receipt`
  output after V7, plus demo-owner-first and paper-owner-first evidence.

After the freeze is created and before either natural producer dispatches, build
the immutable natural run config from it. That config derives the exact
half-open window and canonical LONG/CONT event, outcome, target-capture, and
effective-config paths. Each producer must create or exactly re-open its own
resolved effective-config receipt before constructing public market-data
resources or dispatching an event.

The V7 journal/capture roots are training data and must not become a byte prefix
of the natural roots. After V7 is flat and its twin receipt has been built,
stop all units, verify the archived-source map against the receipt, and
archive/reset the demo and paper account, inbox, and raw-capture roots again.
This second six-root reset is the natural holdout boundary. It must emit the
source-reopening reset receipt before any owner restarts. The live paths may be
reused, but their epoch bytes and journal heads must differ and stay bound
separately. No V7 target, book, latency, fill, fee, or P&L row counts toward a
natural floor.

## Frozen candidate universe and rule discovery

Before the credentialed rule probe, query one point-in-time `api-demo` linear
USDT instrument/ticker snapshot. Build the union of every symbol mechanically
eligible for either effective strategy profile, before signal ranks, returns,
or entry conditions are applied. The artifact records every included and
excluded symbol with the exact mechanical reason, raw-source hashes, config
hashes, snapshot time, and canonical sorted symbol list. Existing held symbols
would be unioned in, but the fresh epoch must begin flat.

Both producers must enforce this exact allowlist. A listing after the snapshot
is ignored for this window; a required symbol becoming suspended, delisted, or
absent is an abort, not an opportunistic universe revision. This is a current
forward population contract, not historical PIT evidence and not an alpha
filter.

Do not pre-subscribe the account owner to the whole candidate universe merely
to make readiness convenient. Its initial symbol file contains only the frozen
health/calibration seed set and any already-held symbols (none are expected
after reset). The owner discovers all pending-request symbols, subscribes to
them in parallel, and keeps the durable head request pending until every symbol
has a fresh, ungapped reconstructed book. A later ready request may not
overtake an earlier warming request because account-wide risk and capital make
that reorder observable. Warmup timeout degrades the epoch without consuming
the request or inventing a venue rejection.

While every owner/producer is stopped and the demo account is flat, the rule
probe must consume the frozen symbol artifact directly and obtain one accepted,
cancelled PostOnly order-create observation for every candidate symbol, in
addition to its `api-demo` structural row. It must hold the canonical demo-user
mutation lease throughout, operate one order at a time, rate-limit itself, and
place each buy probe at the recorded default of 100 basis points below the
current bid (tick-rounded down) to reduce accidental fills while preserving a
conservative quantity/notional test. It must finish with no regular/conditional
order and no position. Any fill, unknown
reject, uncancelled order, structural drift, missing symbol, timeout, or
residual exposure fails that probe attempt. Its activity is outside the fresh
natural accounting epoch because the six-root reset follows it. A failed probe
is retained; do not silently drop its symbol or merge partial probe receipts.

## Owner and collection order

The operational order is fixed:

1. Finish and retain V7. If its full gate fails, stop.
2. Build and independently verify the V7 twin receipt while flat, then freeze
   its explicit archive-source map before any live path is reset/reused.
3. Stop all units, freeze the candidate universe, finish the complete rule
   probe, then archive/reset all six roots into the natural epoch.
4. Start the paper owner alone first, require exact-head health and growing
   capture, and leave every paper producer stopped. Stop it cleanly before the
   offline paper replay writes a new isolated root outside every live/sealed
   account path.
5. Start the demo owner alone, require exact-head health and growing capture,
   then start only the registered demo LONG and CONT producers. Continue the
   credential-free public clock capture on its six-hour target cadence; a
   missed interval that creates an observed gap above eight hours invalidates
   clock-adjusted feed-latency evidence and is not backfilled.
6. At the fixed end, stop producers, publish the separately classified
   post-window canonical zero targets, require convergence and venue flatness,
   let the demo owner publish its final exact-head health, then stop it. Leave
   the paper owner and every auxiliary unit stopped.
7. Build the ordered clock series and stopped effective-config bundle, capture
   the authenticated venue-accounting/final-flatness receipt, and only then
   create the stopped-natural-epoch seal over the exact old mutable namespace.
   Any source change after sealing invalidates the evidence set.
8. Replay the immutable capture offline from the sealed namespace into
   isolated historical and paper roots. Every replay, parity, sufficiency, and
   drift output must live under a dedicated derived-evidence root outside all
   11 sealed roots and all 10 later fresh-deploy roots. No replay process may
   have credentials or write a live inbox/root.
9. If every replay, drift, sufficiency, and accounting gate passes, create a
   disjoint ten-root fresh-deploy epoch, record the final evidence card, and
   bind both epoch manifests into the still-open authorization assessment. Root
   creation and environment materialization do not authorize startup.

Starting an owner proves only owner-first topology. It does not turn the later
offline replay into a live service run, and it does not substitute for replay
evidence.

## Fixed exposure and stopping rule

Set `T0` to a future UTC hour boundary in the freeze manifest before either
natural producer starts. Set `T1 = T0 + 120 hours`. The entire half-open window
`[T0,T1)` is used. Do not stop after a favorable trade count, add time to reach
a floor, or restart a failed window. The five-day window leaves two days inside
Bybit's seven-day transaction-log query limit for controlled flattening,
stopping, and final accounting.

Every wall-clock hour must contain at least one successfully captured event and
explicit callback outcome from both sleeves. Extra event-driven wakeups are
retained. Every raw event must have exactly one outcome, including an explicit
empty decision list. A callback/publication/capture failure, missing hour,
clock reversal, process restart without a continuous hash chain, or unbound
request closes the epoch as failed.

For a deployment-changing conclusion, the natural holdout must contain at
least 30 actually filled demo commands, at least 10 attributable to each
sleeve, at least three symbols, at least three completed open-to-flat round
trips per sleeve, and at least 10 canonical P&L events. These are repeated
lifecycle/heterogeneity floors, not a performance sample. If the natural
strategies do not emit them, the result is `inconclusive`; do not force trades,
extend the window, or count V7. A future window needs a new prospective
registration and fresh roots.

For these floors, a command is assigned by its originating accepted target
batch and must have been planned inside `[T0,T1)`; its venue fill may arrive
after `T1` only if it is the terminal observation of that already-submitted
command. A round trip requires fill-backed entry and terminal reduction plus
both natural strategy target decisions inside `[T0,T1)`. The controlled
post-window safety flatten and its P&L are retained for accounting but cannot
manufacture a lifecycle-floor pass.

To keep the full half-open window and replay scope unambiguous, the controlled
flatten begins only after `T1`. Every such target batch must use the reserved
`natural-safety-flatten/<freeze-id>/...` namespace, carry explicit
safety-flatten metadata, and appear in a separate hash-bound post-window
manifest. The account replay excludes exactly that declared set from natural
strategy-plan parity; any other extra `RISK_DECISION` batch fails. Venue
accounting and final flatness include the safety batches, while scheduling
parity, natural lifecycle floors, and twin holdout metrics do not. This is a
registered operational classification, not permission to discard an
inconvenient natural batch.

## Immutable replay contract

The post-callback capture is provenance for natural demo publication. It must
contain each source event, profile/environment identity, every durable
`AccountTargetRequest` byte-for-byte, queue arrival identity, exit-before-entry
publication order, and the decision keys derived from those requests. An empty
successful cycle is recorded explicitly.

The scheduling replay copies those exact bytes into historical, paper, and demo
input artifacts and dispatches every event through `DeterministicEventClock`.
The event comparator may normalize only the declared raw source names and the
explicit `execution_environment` field. Event time, phase/kind, source
sequence, all other payload, decision keys, and reconstructed normalized
chains must match exactly. This scheduling gate does not rerun signal selection
or authenticate the original market-data adapter.

The account replay is a separate required gate. It must re-open the frozen
target capture, actual demo journal, raw capture segments, rule receipt, risk
policy, source-reopened effective-runtime-config bundle, V7 twin receipt,
clock-offset series, scope-batches file, and freeze manifest. Every live-source
argument must name the source set recorded by the stopped-epoch seal; the
authorization aggregate check must reject a replay receipt that does not bind
back to that same sealed epoch. For every captured request batch it must find
exactly one
demo `MARKET_INPUT_REF`, its ungapped `book_context`, and its exact risk
snapshot; missing, duplicate, extra in-scope, mutated, or out-of-window inputs
fail. It then submits the captured requests through `HistoricalAccountReplay`
and the production account kernel in isolated historical and paper roots using
the same verified rules, risk policy, event time, books, and twin config.

Historical and paper modeled journals must match exactly after only declared
environment/id normalization. Demo comparison is limited to strategy decision,
risk acceptance/rejection, target, and semantic command-plan identity. It must
not demand equal asynchronous event families, command ids, fill partitions,
prices, fees, protection transitions, funding, or P&L from the actual venue.
Every finite planned quantity uses absolute tolerance `1e-12`; NaN, infinity,
missing quantity, ambiguous command mapping, or an unscoped extra batch fails.
The only scoped non-natural batches permitted in the stopped journal are the
exact post-window safety-flatten IDs in that separately verified manifest.
The V7 receipt supplies the prospectively frozen training identity and derived
twin configuration; it must not be required to name the natural journal/raw
manifest. Those are deliberately different epochs. This replay gate binds both
identities and rejects equal journal heads/raw manifests or source splicing,
but source-level V7 metric recomputation remains the independent drift gate.

The effective-config bundle is created only after both natural producers are
stopped. It must re-open the two producer-written receipts, their natural run
config, freeze, candidate-universe source, candidate commit, and exact window.
The replay and account-plan comparator must take this bundle explicitly; an
environment variable, profile name, or source config path is not equivalent
evidence of the values the process actually resolved.

## Out-of-sample twin drift rule

V7 is the only training sample. The drift verifier must re-open V7 through the
explicit archived-source map, verify those archived hashes against the V7
receipt, and separately re-open the post-reset natural sources. It must never
follow a stale embedded V7 live path into the reused natural root. The natural demo rows are holdout data and
cannot change the selected baseline/stress quantile, latency model, fill model,
queue assumption, threshold, or code. Recompute holdout metrics from the bound
journal/capture sources; never trust copied summary fields.

Before computing drift, the verifier must re-open the initial clock receipt
bound by the natural freeze and every subsequent registered receipt, reject
aliases, duplicate artifacts, non-increasing observation times, endpoint gaps
above six hours, or any adjacent observed gap above eight hours, and require a
post-T1 member. It linearly interpolates only the feed-row correction at that
row's local receive timestamp. It reports bracketing member indexes, used gaps,
point-correction range, and both latency endpoints under the estimated
uncertainty. The point-estimate gate below remains the registered decision rule;
the sensitivity output prevents it from being cited as a hard clock-error bound.

The drift receipt passes only when all of these prospectively fixed checks pass:

- 100% of holdout commands link one-to-one to an ungapped captured decision
  book, and 99% of adjusted feed and request/ack latency observations are
  nonnegative under the timestamp-specific clock-series correction;
- holdout `p50` and `p95` adjusted feed latency do not exceed V7 `p75` and
  `p99`, respectively; the same comparison applies to request/ack latency and
  adverse residual slippage after visible-book walking;
- the baseline replay reproduces each actual command's terminal status class and
  cumulative filled quantity. A deterministic `p95` stress replay then compares
  VWAP over exactly that actual filled quantity (never the stress model's larger
  natural quantity) and is no less adverse than the actual demo fill VWAP, after
  one tick of rounding tolerance, for at least 95% of filled commands. A model
  that cannot supply the actual quantity fails model scope rather than changing
  the comparison denominator;
- every observed positive within-order fill spacing is reported. If at least
  three holdout spacings exist, its `p95` must not exceed the frozen V7 `p99`;
  fewer than three are labelled insufficient holdout evidence and do not erase
  V7's calibration. Any observed fill behavior the configured model cannot
  represent fails the model-scope gate;
- rejected, cancelled, zero-fill, terminal-incomplete, and multifill orders are
  classified separately. They cannot be recoded to improve a latency or fill
  floor.

The baseline paper replay remains the decision model; the `p95` copy is a
stress guard, not a selected replacement after seeing the holdout. Passing
these envelopes is bounded drift evidence, not proof of distributional
stationarity or tail calibration.

The current strategy path uses market orders. MBP depth cannot identify passive
queue position, future book mutation, or market impact, so
`passive_queue_calibrated=false` and `immutable_replay_book=true` remain
mandatory. Native protection orders are assessed from venue/accounting events,
not misrepresented as a calibrated passive queue model.

## Accounting and final decision

Within the seven-day query boundary, the stopped demo journal and authenticated
venue receipt must reconcile exact request/target/command/order/fill lineage,
actual execution fees, account/symbol realized P&L, settlement funding, no
foreign exposure, and flat pre/post state. Same-symbol sleeve attribution is
accepted only where canonical command/execution identities make it
reconstructable; otherwise the account/symbol total is authoritative and the
component claim remains unavailable.

This venue receipt is captured before the stopped-epoch seal and before any
offline replay. The seal then binds the stopped demo journal, raw captures,
natural strategy tapes, post-window safety artifacts, effective-config bundle,
clock series, venue receipt, and all other registered live-source evidence.
Replay outputs are new analysis artifacts, not members of the old mutable
runtime namespace.

Historical and paper modeled P&L must match each other exactly under their
common twin. Demo P&L is not required to equal modeled P&L; its signed price,
fee, funding, and net differences are reported as model error and bound into
the drift/accounting receipts. Any local/venue accounting discrepancy or final
non-flat state fails.

The final outcome is:

- `supports` only if scheduling replay, schema-v4 account plan parity, V7
  out-of-sample drift, owner-first/topology review, rule/universe coverage,
  venue accounting, funding, P&L reconstruction, final flatness, stopped-source
  integrity, and fresh-deploy-root separation all pass;
- `inconclusive` when the fixed natural window is intact but a registered
  lifecycle or holdout-identifiability floor is not met;
- `contradicts` or `invalid` when an equality, provenance, causality,
  accounting, safety, or abort gate fails.

No branch promotion, VPS deployment, or deploy-ready authorization may happen
before that outcome is recorded. Mainnet remains categorically outside scope.

## Required artifacts

Retain the clean commit/CI receipt; freeze manifest; config/risk/route hashes;
candidate-universe raw snapshot and decision table; complete demo-rule probe;
both six-root reset archives and their explicit reset receipts; immutable V7
archive-source map; V7 journal, captures, clock and twin receipts; every ordered
natural clock-series member and its source-reopening series receipt; natural
run config and both producer effective-config receipts plus their stopped
bundle; paper/demo owner-first logs and health files; natural event, decision,
target, account, and raw-market tapes; post-window safety capture/manifest;
venue-accounting and flatness receipt; stopped-natural-epoch seal; schema-v2
target-replay manifest; schema-v3 event-parity and comparison-scope receipts;
registered analysis receipt/output path identities and disjointness checks;
isolated historical/paper journals; scheduling, schema-v4 kernel-parity,
sufficiency, and twin-drift receipts; fresh-deploy-epoch manifest and exact
per-unit environment-materialization receipt; every failure/recovery row; and
the final evidence card. Every machine receipt must re-open and hash its sources
during verification.
