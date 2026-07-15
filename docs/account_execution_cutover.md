# Account execution cutover

Status: substantial cutover architecture is implemented locally, but the plan
is **not complete, not deployed, and not deployment-ready**. Demo and
deterministic paper only. This runbook does not grant real-money authority.

For a multi-session continuation, use the bounded copy-paste prompt and
progressive test cadence in `docs/account_execution_completion_handoff.md`.
That handoff does not replace this runbook or grant final deployment authority.

## Operational retention and authority boundary

The owner has separated demo/paper operation from the optional five-day
research holdout. Continuous raw order-book/public-trade persistence does not
participate in target generation, aggregation, risk, order submission,
protection, reconciliation, or account accounting. It is therefore not a VPS
operational prerequisite. The registered natural study remains prospective
and unchanged; omitting it makes no replay, drift, parity, promotion, or alpha
claim.

`ACCOUNT_RAW_MARKET_PERSISTENCE` is mandatory and has two explicit meanings:

- `1` persists raw L2 and public-trade frames for the registered V8 or natural
  evidence workflows;
- `0` is permanent demo/paper operation: public trades are not subscribed,
  bulk raw L2 frames are not written, but the owner still reconstructs live L2,
  publishes an atomic bounded same-generation market-readiness sidecar, and
  persists the exact book at every decision boundary.

The canonical account journal and exact decision books are never optional.
Existing capture history is preserved; changing to `0` stops future bulk
growth and is not permission to delete an archive.

Operational startup uses the create-only mode-`0600` receipt
`/etc/liquidity-migration/account-execution-operational-ready`, independently
of the research-promotion `account-execution-deploy-ready` receipt. The runtime
wrapper refuses if both exist. It also refuses natural/fresh override files,
the wrong/dirty commit, another machine, changed environment/config/input/root
identity, mainnet variables, `REAL_MONEY`, or an unregistered unit.

There are three profiles. `calibration` requires raw persistence `1`, binds only
the demo route and inputs, and authorizes only the demo account owner. This
breaks no evidence gate: it exists solely to run the unchanged registered V8
once before a paper calibration exists. After V8 closes, stop the owner and
preserve/archive that authorization receipt before changing any bound input.
`demo-operational` requires raw persistence `0`, demo-scoped liveness, and a
disabled paper sleeve; it binds only the demo route and authorizes the demo
owner, demo LONG/CONTINUOUS producers, hedge, RMOM refresh, and liveness. It
does not read or claim a paper twin receipt and cannot start a paper unit. Both
permanent profiles require `ACCOUNT_SYMBOLS_FILE` and
`CANDIDATE_UNIVERSE_FILE` to name the same immutable candidate artifact and
rebuild exact source-bound demo-rule coverage before authorization. This is an
execution compatibility gate, not a raw-tape or research-result claim.
`operational` requires raw persistence `0` on both owners, demo-paper liveness,
and a source-verified passing twin receipt; it then authorizes the checked
nine-unit demo/paper topology. Issue or verify a profile through:

```bash
COMMIT="$(git -C /opt/liquidity-migration rev-parse HEAD)"
scripts/ops.sh operational-authority --execute issue \
  --profile calibration \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration \
  --authorization-reference "owner task: bounded V8 bootstrap" \
  --owner-acknowledgement AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION

scripts/ops.sh operational-authority verify \
  --repo-root /opt/liquidity-migration
```

The full operational profile uses the same command with
`--profile operational` only after the twin receipt is installed and both
owner environments explicitly set `ACCOUNT_RAW_MARKET_PERSISTENCE=0`. These
receipts authorize only their named demo or demo/paper scope. After a failed or
deferred V8, a separately frozen candidate may use `--profile
demo-operational` with `ACCOUNT_RAW_MARKET_PERSISTENCE=0`,
`ACCOUNT_LIVENESS_SCOPE=demo`, and `CONTINUOUS_PAPER_SLEEVE=off`; that is a
demo operation decision, not a V8 retry or paper calibration. No profile
authorizes a `main` push, mainnet, real money, or a research conclusion.

The evidence plan is not complete. Prior candidate `c7d6509` passed a clean
local/non-contacting `candidate-ci` boundary but was never installed and is
spent after V7 exposed unrepaired owner defects. Replacement candidate
`54536f1` repaired those paths and passed its local/pre-push/exact-head Linux
gates, but its first public candidate-universe construction exposed a
noncanonical ticker-only source row that failed before output; it was not
installed or retried and is also spent. The prospective schema-v2 repair keeps
and hashes that raw source row while explicitly excluding it from candidate
evaluation, without widening the strategy symbol grammar. Schema-v2 candidate
`344cd72` passed its registered local Ruff/full-pytest gate, then the canonical
pre-push gate failed before network update because the hook's `.git/tmp`
basetemp violated existing source-snapshot tests' outside-repository invariant.
The candidate is spent. The prospective hook repair moves and validates that
basetemp outside the repository; it does not change Strategy Overhaul logic.
Replacement candidate `0f05060` then passed all 2,957 local/pre-push tests,
but its one exact-head Linux run retained one existing filesystem-order-
dependent test failure and 2,956 passes. It was not retried and is spent.
The prospective test repair selects the two exact dated liquidation outputs.
Candidate `181027b` then passed all 2,970 local tests, canonical pre-push, and
its one exact-head Linux run. It installed on the stopped VPS and passed the
authenticated six-root reset. Its first credentialed schema-v3 rule probe
failed before owner startup because BUSDT cancellation history appeared only
once at the five-second deadline rather than twice. Cleanup and final flatness
passed; the candidate is spent. A prospective replacement extends only the
read-only terminal-history observation defaults to 30 seconds/100 polls while
retaining every exact-identity, no-fill, rate, cleanup, and flatness gate.
Replacement `b501be3` passed all candidate gates and that probe, but V8 then
closed before publication because its fixed 160-USDT size was below BTC's
unchanged 160.15725-USDT quantization-safe minimum. No V8 target/order/fill
exists, paper remains blocked, and V8 cannot be resized or retried. There is
no natural 120-hour LONG/CONT epoch, periodic clock series,
venue-accounting/final-flatness receipt, offline replay/parity/drift/sufficiency
result, real stopped-natural-epoch seal, real fresh-deploy epoch, or cutover
authorization. A new demo-operational candidate may run only the raw-disabled
demo topology without claiming or authorizing paper. Promoting or deleting the
cutover branch is unsafe while those
gates remain open. An unrelated branch may be removed only after proving it has
no unique commit and no dirty or in-use worktree. The Strategy Overhaul/master alpha plan running
on the big PC is separate work and cannot advance this cutover's frozen `main`
base or supply evidence to it.

Demo-operational candidate `1690093011b35d0693f76ca754d0c28c12f9d8e1`
then passed all 2,979 local/pre-push tests, exact-head Linux run `29429929636`,
and stopped-fleet install preflight. Its first credentialed probe of the frozen
620-symbol operational population failed on the first symbol before authority
or startup: `0GUSDT` was accepted and cancelled without a fill, but exact order
history remained empty for 30 seconds/100 polls. Cleanup and authenticated
final flatness passed; the self-hashed failure receipt is retained with
artifact SHA-256
`091207fbdbda0935d296e96d8deb272bb2347ac9b91ff38a074d606f715a00b4`.
A later read-only exact query observed the same order as `Cancelled` with zero
cumulative execution and no trade rows. The prospective replacement queries
both official order-history and recent real-time closed-order surfaces on each
bounded poll. It requires two terminal observations and empty exact execution
history, rejects every returned identity/status/fill contradiction, and keeps
legacy history-only receipts readable. The failed receipt is neither retried
in place nor converted into passing evidence.

The earlier generic stopped-tree provenance implementation blocker is resolved
in source, not in operations. The schema-v4 authority aggregate reconstructs
the stopped seal's exact path/hash index, reopens the registered dependency
chain, validates captured- and target-replay derived outputs, and rejects
unregistered inputs or overlap with stopped/fresh namespaces. That validator
does not imply that any seal, replay, fresh root, or authorization has been
created.

## V8 corrected-defect successor boundary

V7 is closed and spent after its final funding-hold close failed the unchanged
position-truth freshness rule. Canonical recovery left the venue/account flat
and all units stopped, then exposed a concurrent immutable-Close collision.
The retained outcome and hashes are in
`docs/preregistration/account_execution_calibration_v7_2026_07_14.md` and
`STATE.md`; none of its target, fill, timing, fee, P&L, funding, or recovery
rows count toward another gate.

The next allowed calibration is the prospectively registered V8 contract in
`docs/preregistration/account_execution_calibration_v8_2026_07_15.md`. It keeps
the exact V7 sample, risk, clock, smoke, partial-fill, and abort rules while
repairing only reconciliation freshness/cache ordering and atomic Close/P&L
finalization. For the remaining procedure, normative references to the next or
passing V7 training epoch mean V8. Existing `v7_training`, `v7-archive`, and
`--v7-archive-map` names are compatibility labels and must bind V8; they never
permit the failed V7 receipt.

The current offline contract chain is target-replay manifest v2, event parity
v3, captured-account replay v3, kernel comparison scope v3, kernel receipt v4,
natural sufficiency v3, and authority aggregate v4. Their local
`created_ts_ns` values enforce declared internal ordering, but are not
authenticated wall-clock evidence. The aggregate therefore reports only
`declared_analysis_chronology_consistent` and
`declared_fresh_epoch_chronology_consistent` for those comparisons; neither is
a causal or post-seal execution pass. Machine-verifiable data dependency comes
from `analysis_sources_reopened_with_exact_hashes` and
`analysis_dependency_receipts_exactly_linked`: the target manifest binds the
exact canonical capture and replay outputs, event parity binds that manifest,
the comparison scope binds event parity and captured replay, and the downstream
validators reopen the exact path/hash chain. This proves dependence on the
sealed bytes, not authenticated wall-clock invocation after the seal; the
operator must still run the steps in the registered stopped-fleet order.

The deterministic scheduling scope is the registered active LONG and
CONTINUOUS natural market-order paths. It is not a claim that every timer,
hedge/RMOM/liveness loop, historical mode, or adverse-limit path now shares
literal runtime parity.

## Evidence status

The local cutover has one strict `execution_environment=demo|paper` route.
There is no `SUBMIT_ORDERS`/`PAPER_MODE` fallback. LONG and CONTINUOUS import a
public-only market-data plane; the retired direct executor, exit/report
companions, sleeve router, order executor, and Bybit trade-router/WebSocket
submission clients are deleted. Only the account owner retains venue-mutation
authority.

The account sequencer, atomic journal, aggregation/risk, raw L2 capture,
paper/demo owners, native protection, reconciliation, target-only routes, and
desired-target convergence have local automated coverage. Accepted targets
remain `target_pending` until fills reconstruct the desired position;
terminal rejects/cancels are retried with bounded, restart-derived command
identities. That is software evidence, not host or venue evidence.
Queue-head market readiness gates only the newly queued request: an unreadied
head remains unclaimed, while an earlier deterministic convergence retry may
still advance so a stale or missing book on unrelated work cannot strand a
journaled reduction.

The account journal builds transactions on an isolated prospective state and
publishes its in-process cache only after the durable transaction rename. The
private execution consumer, reconciliation loop, and Telegram reader therefore
cannot observe a partially-applied or failed transaction. Native-protection
state and venue mutations are separately serialized by one reentrant manager
lock. These are concurrency invariants under automated fault tests, not proof
of host or venue behavior. Competing REST/private ACK observers decide against
fresh state under the journal lock: a semantically identical second observation
cannot duplicate the state transition, while changed acceptance or conflicting
nonempty venue order IDs fail closed. If a private fill establishes acceptance
before the HTTP create response, the later request/response timing is retained
as a supplemental immutable `ack_observation` rather than overwriting the ACK
or losing a calibration sample. Execution redelivery similarly permits
different local receive time/provenance but requires exact identity/timestamp
and `1e-12`-tolerant quantity, price, and fee agreement.

An accepted zero target removes the nonzero component owner before its
reduce-only fill necessarily reaches the journal. Native protection retains the
already-installed stop across only that bounded transition: the immutable state
must show an explicit zero aggregate, all-zero latest component desires, no
nonzero component target, fully covering opposite-side reduce-only work, and a
valid active same-direction stop. It never derives a replacement stop there.
Missing protection, incomplete/terminal/risk-increasing work, or a true orphan
position still blocks.

The 2026-07-13 audit first found the old fleet active at clean commit
`5f6d9986d935`, with both account owners absent. The demo key permission check
passed and a read-only venue query was flat. Flat maintenance then began at
`2026-07-13T22:53:17Z`: every project unit was stopped, a second venue query
confirmed zero positions and zero regular/conditional orders, and the unit plus
flatness evidence was retained under the host's mode-restricted cutover-evidence
directory.

Later read-only inspection proved the host clean on commit `98b3916a4a135`,
with the target-only topology installed. Candidate `c7d6509d3a21` passed its
local and non-contacting Linux gates but was never installed and is spent after
the V7 defects. Candidate `54536f194d91` then repaired those defects and passed
2,954 local tests plus exact-head Linux CI, but its first public-demo capacity
snapshot failed closed on a ticker-only synthetic label absent from the complete
instrument snapshot. It was not installed. Re-stage and verify only a new exact
post-`344cd72` replacement candidate with the external-basetemp hook repair
before V8. No staged revision or failed V7 path created a deploy-ready or
activation marker. The
guarded reset then re-proved venue flatness, archived 12 legacy
roots/projections to the verified archive whose SHA-256 is
`07e76e35e688fb6f20e17c78ea9bc8489144c852f4c99fcb9964d887c06c6d6a`,
rebuilt compatibility projections from canonical journals, and created all six
fresh empty account/inbox/capture roots. No service was active before or after
the reset.

The first 20-USDT rule-probe ceiling failed before order submission because
current `BTCUSDT` structural minimum quantity exceeded it. The failed attempt
is retained. A second, flat-account 200-USDT feasibility probe passed and
produced the self-hashed three-symbol rule receipt. Its largest observed minimum
is 62.1029 USDT, so the original 30-USDT calibration plan is closed as
infeasible. Static inspection then closed the 80-USDT v2 before startup because
BTC quantity-step rounding erased its executable buffer. V3 then fixed 160 USDT
and a quantization-safe 2.5-times-minimum bound, but its first clock receipt
failed the fixed 50-ms error ceiling; persistent-session diagnostics proved the
geographic path itself is roughly 169 ms RTT. No retry was reinterpreted as a
pass. V4 retained the $160 order plan and registered one preconnected session
plus a disclosed 100-ms worst-case clock-error ceiling. Its schema-v2 receipt
passed with an 84.805-ms estimated maximum midpoint error. The owner started
alone, and the first canonical BTC target received a real `0.002 BTC` demo fill.
The run then failed immediately: REST reconciliation briefly observed that fill
before local private-stream propagation, and the create-response/private-fill
race proposed the same ACK with different observation provenance, which the old
immutable journal rejected as changed content.

V4 was not resumed. A separately labelled canonical zero target recovered the
position, final read-only evidence proved zero local and venue position plus no
open order, and the owner was stopped. Paper and ordinary producers never
started. The failed V4 run/tape and recovery remain evidence but count toward no
calibration floor.

V5 then ran from another guarded reset whose verified archive SHA-256 is
`56cb3787d12b9c6e72bb684e59b37e3c6fbdc62fded8db32612da293bf629f7c`.
Its independent clock receipt passed, and its first BTC open/close round trip
produced two real `0.002 BTC` fills plus an immutable provisional
`-0.13755984 USDT` fee-inclusive P&L event. The V4 ACK/fill and reconciliation
repairs worked. V5 nevertheless aborted when protection synchronization saw the
accepted zero target after component-owner removal but before the closing fill
updated the position, and misclassified that canonical in-flight close as an
orphan. The close filled; a self-hashed final receipt proved zero local/venue
position and no open orders. V5 is spent and none of its sample counts toward
V6.

V6 then ran from a third guarded reset whose archive SHA-256 is
`bdcb6399c255863eef648b7424ca9121ef46c49726a1b98dff026d3d74969c0f`.
The retained-stop repair worked across four real closes. Event 9 opened
`+0.08 ETH`, but the next between-step exact-health gate failed with
`health=201, journal=202`. The owner deliberately journals reconciliation
snapshots every two seconds while unchanged health had been published every
five, so exact-head readers could remain one snapshot behind and a fixed retry
could miss the shorter matched windows. A separately labelled canonical
recovery command flattened ETH; final self-hashed evidence proved zero
local/venue position and orders plus an exact stopped health/journal match at
sequence 367. V6 is spent and none of its sample counts toward V7.

The original V7 registration retained every numerical sample/gate choice and
changed only the health-publication invariant: every new journal head causes an
atomic health refresh, while journal-only refreshes reuse the last wallet
snapshot to avoid a REST burst. Before any V7 target or outcome, its dated
prospective amendment added the separate partial-fill identifiability gate and
records the other independently reviewed code changes that may enter the exact
candidate commit. The fixed target sequence, $160 notional, risk envelope,
clock rule, and original latency/slippage floors remain unchanged; the latter
now support only the smoke claim until the new partial-fill gate also passes.
Exact-head validation remained unchanged. The V7 contract required fully
validated, exactly staged code and a new six-root reset before its first target;
the later run deviated by remaining on old host commit `98b3916`, then failed as
recorded in the V8 successor boundary above. This is a verified-flat failure
boundary, not deployment readiness.

The full acceptance gate remains open, and no deploy-authorization assessment
or receipt has been issued:

- the credentialed `api-demo` rule gate passed only for the three calibration
  symbols; candidate coverage for actual LONG/CONTINUOUS strategy tapes remains
  open;
- standard and bounded sniper-retrace LONG historical execution submit
  chronological same-timestamp
  targets to a persistent account kernel and consumes risk/execution feedback
  before the next daily decision. It evaluates capacity at the actual delayed
  or retrace entry boundary, after hourly exits already knowable by then; this
  intentionally fixes the legacy daily-scan and signal-time reservation ordering
  rather than claiming byte-equivalence. Provisional hourly triggers also feed
  the session chronologically, batch at equal timestamps, and consume account
  rejection before confirmation. Sniper waits longer than 24 hours remain
  post-run account replay until their trigger windows enter the same online
  decision path;
- CONTINUOUS market-order historical execution now advances positions one bar
  at a time and submits same-timestamp targets to a persistent account kernel;
  account rejection/acceptance feeds back before the next decision. The
  adverse-limit research mode remains a chronological strategy simulation with
  post-run account replay until a limit-order execution port exists;
- historical replay currently uses synthetic one-level bar books, while paper
  uses captured Bybit L2 and demo uses the venue. Actual market-tape parity is
  therefore false;
- the registered natural LONG/CONT and offline replay paths dispatch scheduling
  inputs through the same ordered, hash-chained event clock. The recorded event
  time is injected into the strategy callback, so the callback no longer
  rereads wall time. Hedge/RMOM/liveness and some historical signal-selection
  loops remain outside this claim, so this is deterministic scheduling
  infrastructure, not full strategy parity;
- no captured actual LONG/CONT target tape has yet passed decision key,
  rejection key, target quantity, and order/position hash comparison across
  all three environments;
- reconstructed fill P&L has not yet passed the new immutable venue-accounting
  receipt. The demo owner now ingests strict `SETTLEMENT` rows as canonical
  funding P&L, and the final read-only capture can bind exact `TRADE`,
  closed-PnL, and funding rows to the stopped journal plus pre/post flatness.
  No actual receipt exists yet. Missing execution fees remain explicitly
  pending, and same-symbol venue fills are netted, so exact component P&L is
  also pending unless a prospective allocation policy can be shown to be
  identifiable from canonical orders and executions;
- no demo tape has met the preregistered execution-twin sample floors. V4--V7
  produced useful but spent execution failures; V8 closed prepublication on
  its unchanged size preflight and produced no tape. No calibration receipt has
  passed. Paper startup therefore remains intentionally blocked.

Do not issue the deploy-ready authorization by interpreting
`canonical_common_kernel_parity` as full strategy parity. Reports also carry
`cross_environment_strategy_parity=false`, `venue_rule_parity=false`, and more
specific evidence labels for this reason.

Once actual captured runs exist, derive the non-empty schema-v3 comparison scope
from the replay and scheduling receipts, then write the current schema-v4
structural journal receipt with every required source binding:

```bash
scripts/ops.sh account-parity-scope \
  --captured-account-replay-receipt /absolute/new/path/natural-account-replay/captured_account_replay_receipt.json \
  --event-parity-receipt /path/to/strategy-event-parity.json \
  --output /path/to/natural-batch-scope.json

scripts/ops.sh account-parity \
  --environment historical=/path/to/historical-account-root \
  --environment paper=/path/to/paper-account-root \
  --environment demo=/path/to/demo-account-root \
  --comparison-scope-file /path/to/natural-batch-scope.json \
  --event-parity-receipt /path/to/strategy-event-parity.json \
  --fresh-epoch-reset-receipt /path/to/natural-reset-receipt.json \
  --risk-policy-file /path/to/risk-policy.json \
  --rules-file /path/to/demo-rules.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --twin-calibration-receipt /path/to/frozen-v8-calibration.json \
  --repo-root /absolute/path/to/liquidity-migration \
  --expected-commit "$EXPECTED_COMMIT" \
  --quantity-tolerance 1e-12 \
  --output /path/to/account-kernel-parity.json
```

The command refuses empty journals, exits nonzero on mismatch, binds the
receipt to a normalized SHA-256 for each source, and writes a mode-`0600`,
self-hashed receipt atomically. It intentionally records
`full_cross_environment_acceptance_passed=false`: journal structure alone cannot
prove actual market-tape provenance, full strategy parity, fresh demo rules,
credentialed demo execution, or P&L agreement.

## Staged topology installation

Exact-candidate Linux validation is a separate, non-deploying GitHub Actions
dispatch: select `candidate-ci` on the candidate branch. Its receipt is valid
only when the candidate head passes full Ruff/pytest and the workflow records
every SSH-key, host-key, VPS verify, install-preflight, recovery, and deploy step
as skipped. Do not use a pull-request merge SHA, `verify`, or
`install-preflight` as the exact-candidate CI gate.

After entering the flat maintenance window and stopping the currently installed
fleet as described in the ownership switch below, install the current checkout:

```bash
CANDIDATE_BRANCH="$(git branch --show-current)"
test -n "$CANDIDATE_BRANCH"
INSTALL_PREFLIGHT_ONLY=1 \
  BRANCH="$CANDIDATE_BRANCH" \
  EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/deploy_vps_live.sh
```

GitHub Actions exposes the same phase as the manual `install-preflight` mode.
For provider-console recovery, use:

```bash
CANDIDATE_BRANCH="$(git branch --show-current)"
test -n "$CANDIDATE_BRANCH"
INSTALL_PREFLIGHT_ONLY=1 \
  BRANCH="$CANDIDATE_BRANCH" \
  EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/vps_console_recover_and_deploy.sh
```

This phase checks out the requested commit and installs the exact versions in
`requirements.lock` with dependency resolution disabled (no pip upgrade,
editable install, or pyproject range resolution),
runs the checked smoke tests, installs the current systemd manifest, and stops
and removes unknown historical `liquidity-migration-*` units and their drop-ins.
That last action is an intentional host mutation: it prevents a retired order
mutator from surviving the cutover preparation.

The phase does **not** inspect, require, create, or remove either
`account-execution-capture-enabled` or `account-execution-deploy-ready`; it does
not read the Bybit or account-owner route environments; and it does not enable,
start, or restart any current owner,
producer, collector, or other `liquidity-migration` service/timer. Current units
that were already running remain in their prior runtime state. Do not run it over
an active fleet: the checkout itself changes scripts/config under those
processes. The maintenance stop and flatness checks below are therefore
prerequisites, not cleanup left for after installation. The scripts enforce the
first boundary before checkout: any active, activating, deactivating, or
reloading `liquidity-migration-*` unit makes install preflight refuse.
`install-preflight-ok` means only that the software and unit topology are
installed. It is neither acceptance evidence nor authority to trade.

## Preconditions

1. Bybit demo is flat and has no open or conditional orders. The demo owner
   independently enforces this for an empty/new account journal before it can
   claim an intent: startup reads Bybit's all-kinds open-order view and an
   explicit `StopOrder` view, deduplicates by `orderId`/`orderLinkId`, and
   aborts on any row or either query failure. It does not cancel or adopt the
   order. On a non-empty restart, an open order is accepted only when it matches
   a still-working kernel command by client/venue id or the journal-backed
   native-protection verifier identifies the exchange-generated stop. REST
   position reconciliation must also be healthy before startup continues.
2. Archive the legacy LONG, CONTINUOUS, hedge, and risk ledgers. Do not delete
   the archive; those rows remain historical receipts, not operational truth.
3. Generate fresh empty demo and paper account roots, inboxes and raw-capture
   roots. Never seed the account kernel from stale `open` rows. The account
   journal schema is versioned and there is deliberately no automatic import of
   legacy sleeve ledgers. All six demo/paper account, inbox and capture paths
   must be absolute and pairwise disjoint; equality, nesting and canonical
   `..` aliases fail deploy, recovery and verification.
4. Supply a demo-verified rule row for every candidate/held symbol, including
   `tick_size`, `qty_step`, `min_qty`, `min_notional`, `max_order_qty`, and
   `max_leverage`. Public/mainnet instrument metadata is not accepted as demo
   verification. The owner also rejects receipts older than seven days by
   default (`MAX_DEMO_RULE_AGE_HOURS`).
5. Verify the configured demo API key is not read-only and has order-submit
   permission. The account owner checks this at startup; sleeves never receive
   the key.
6. Supply explicit absolute account risk limits and an explicit
   `DISASTER_STOP_FRACTION`. There is deliberately no hidden default.
7. The canonical strategy-state projection is implemented for LONG,
   CONTINUOUS, and hedge target routes. Complete captured-tape historical,
   paper, and demo comparison before issuing deploy authorization.

## Demo rule probe

Run this only during the flat maintenance window, with the account owner and
every pre-cutover order mutator stopped. It reads authenticated `userID` from
Bybit key metadata, then acquires the same host-global canonical lease as the
demo owner (`/run/lock/liquidity-migration/bybit-demo-user-<userID>.lock`). The
single-link regular-file inode is bound at acquisition and rechecked at every
mutation boundary; a symlink, hard link, unlink, or replacement invalidates the
capability rather than creating a second apparent owner. The probe then refuses
a non-flat account, reads structural metadata through `api-demo`,
binary-searches the smallest accepted PostOnly quantity-step notional at a
recorded price 100 basis points below the bid, cancels every accepted probe,
and performs a final flatness audit:

Run the probe in the staged VPS checkout through an authenticated operator
shell; it intentionally consumes only the VPS demo credentials and lease.

```bash
export ATTEMPT_ID=REPLACE_WITH_NEW_ATTEMPT_ID
export EVIDENCE_ROOT="/var/lib/liquidity-migration/cutover-evidence/$ATTEMPT_ID"
umask 077
mkdir "$EVIDENCE_ROOT"  # must fail if this attempt namespace already exists

( set -o noclobber
  printf '%s\n' BTCUSDT ETHUSDT BUSDT >"$EVIDENCE_ROOT/v8-symbols.txt"
)

python3 scripts/probe_bybit_demo_rules.py \
  --symbols BTCUSDT,ETHUSDT,BUSDT \
  --max-probe-notional-usdt 200 \
  --probe-distance-bps 100 \
  --max-private-requests-per-second 5 \
  --leverage 10 \
  --output "$EVIDENCE_ROOT/demo-rules-v8.json" \
  --confirm-demo-probe
```

Record `ATTEMPT_ID` and the absolute `EVIDENCE_ROOT` in the external run ledger
and re-export the same values in every later operator shell.

The first receipt covers only the three preregistered V8 symbols. The later
natural epoch requires a second, exact full-candidate probe after V8 is
archived; the two files are never aliases or replacements for one another.
The schema-v3 receipt is self-hashed, created mode `0600`, and fsynced before it
can gate owner startup. A pre-existing output is a spent or ambiguous attempt,
so the probe refuses it and the operator must preserve it and choose a new
attempt namespace. The probe and every receipt consumer enforce the registered
contract: exactly 100 basis points of distance, at most 200 USDT notional, at
most five private requests per second, and at most 10x tested leverage. Invalid
parameters fail before credentials are read or the venue is touched. The owner
also reproduces every attempt's declared notional cap, quantity-step count,
structural quantity bounds, and accepted rule minimum; it rejects unsigned,
altered, or self-described weaker receipts. The
resulting `min_notional` is venue-derived, not the retired local `$25` resize
floor. It is the smallest accepted quantity step at the recorded probe price,
so it conservatively upper-bounds the hidden threshold by at most one quantity
step. Unknown rejects, transport failures, uncancelled orders, stale receipts,
or any residual position fail the gate rather than being interpreted as a
minimum. The current source permits up to 30 seconds/100 bounded polls for
read-only terminal visibility. Each poll inspects both order history and
Bybit's recent real-time closed-order surface. Passing requires two exact
`Cancelled`/zero-fill observations from at least one official surface plus
empty exact-identity execution history; any returned identity, fill, or
non-pending/non-cancelled status contradiction from either order surface fails.
Bybit documents that demo uses `api-demo.bybit.com`, supports order
create/cancel, and has an incomplete API surface; the probe therefore uses
actual order create/cancel rather than assuming the optional pre-check endpoint
is available: <https://bybit-exchange.github.io/docs/v5/demo>. Bybit also
documents order history as asynchronous and the real-time endpoint as the
recent open/closed-order view:
<https://bybit-exchange.github.io/docs/v5/order/order-list> and
<https://bybit-exchange.github.io/docs/v5/order/open-order>.

## Ownership switch

The switch must be one maintenance transaction, not a rolling overlap:

The command blocks below mix the router's documented locality surfaces. Run
the SSH-backed `reset`, `clock-offset`, `demo-calibration`, and
`natural-safety-flatten` routes from the control checkout. Run source-local
routes such as `twin-calibrate`, the compatibility V8 archive,
freeze/config/seal/replay, venue
accounting, fresh-root creation, and authority issuance in the staged VPS
checkout whenever their absolute inputs still live on that host. Record the
host with every receipt; see `docs/operations.md` for the full boundary.

1. Stop every strategy publisher, the hedge timer, the account owner, and any
   pre-cutover mutator or reporter still installed on the host. Repository
   manifests no longer install the old risk/order-repair or combined-book
   reporter units; a host upgraded from an older checkout must prove those
   processes absent rather than merely disabled for one boot.
2. Run the staged topology installation. It removes unknown historical units
   and installs the owner units needed by the reset and later checked deploy,
   without starting anything. Recheck that every current owner/producer and
   every pre-cutover authority remains inactive. Bind the exact clean candidate
   and machine for the evidence window before starting any newly installed
   guarded service:

   ```bash
   COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
   scripts/ops.sh authorized-deploy-epoch prepare-evidence-runtime \
     --expected-commit "$COMMIT" \
     --repo-root /opt/liquidity-migration
   ```

   This creates a mode-`0600`, non-deployment marker at
   `/etc/liquidity-migration/account-execution-pre-cutover-ready`. It can be
   issued only before activation state exists and is retained through the
   evidence window and authorization issuance. Once the deploy authorization
   appears, evidence-window services must remain stopped until checked
   `prepare` consumes the marker and publishes the complete latch/environment
   state. Activation first publishes the persistent mode-`0600`
   `account-execution-fresh-epoch-activation-started.json` history marker. It
   remains after a partial failure, so removing the environment, latch, or
   authorization cannot roll the phase back. Do not recreate pre-cutover
   evidence to bypass a partial or failed activation.
3. Confirm Bybit demo is still flat and manually cancel any remaining regular
   or conditional orders. The owner is a fail-closed verifier, not a cleanup
   mechanism: it aborts instead of cancelling an unowned row. Before any
   credentialed command, make `/etc/liquidity-migration/bybit-demo.env` an
   owner-only, single-link regular file with exact mode `0600` and strict
   `KEY=VALUE` syntax. Checked scripts parse only their fixed allowlist as data;
   they never shell-source this private systemd file.
4. Write `/etc/liquidity-migration/account-execution.env` with one durable demo
   account root, target inbox and raw-capture root shared by LONG, CONTINUOUS,
   hedge, the liveness checker and the owner:

   ```bash
   ACCOUNT_EXECUTION_KERNEL_REQUIRED=1
   ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-execution
   ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-intents
   ACCOUNT_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-market-capture
   ACCOUNT_RAW_MARKET_PERSISTENCE=1
   ACCOUNT_SYMBOLS_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/v8-symbols.txt
   ACCOUNT_DEMO_RULES_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/demo-rules-v8.json
   ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-execution/risk-policy.json
   DISASTER_STOP_FRACTION=REPLACE_WITH_EXPLICIT_FRACTION
   ```

   The placeholder is intentionally invalid; replace it with an owner-chosen
   fraction strictly between zero and one before evidence collection. Keep this
   route file owner-only, single-link, and exact mode `0600`; shell expansion,
   duplicate keys, escape syntax, and ambiguous whitespace are invalid.

   The target launchers require the account root, inbox, and capture-enabled
   marker when the kernel latch is set. The owner additionally requires the
   capture root; neither surface falls back to direct venue mutation.
5. Write `/etc/liquidity-migration/account-paper-execution.env` with the distinct
   paper account, inbox, and capture roots shown in the deterministic paper
   section below. Give it the same owner-only, single-link, exact-mode-`0600`,
   strict-data treatment. Do not alias or nest any demo and paper route.
6. Complete the credentialed demo-rule probe, then review and execute the
   flat-account archive/reset while the fleet remains stopped:

   ```bash
   scripts/ops.sh reset \
     --leave-stopped --sleeves all --label account-cutover
   scripts/ops.sh reset \
     --execute --leave-stopped --sleeves all --label account-cutover \
     --receipt "$EVIDENCE_ROOT/account-cutover-reset.json"
   ```

   The staged installation is what makes the new owner units available for the
   reset's route verification; the reset must not create the capture-enable
   marker, the commit-bound pre-cutover runtime marker, or the deploy-ready
   receipt, and must not start units that were inactive on entry. Retain the archive and SHA-256
   sidecar and mode-`0600` source-reopening reset receipt, and prove all six new
   account/inbox/capture roots are empty. Execute
   derives the authenticated Bybit `userID` before stopping services, then holds
   the same host-global canonical demo-account lease as the owner from verified
   quiescence through venue flatness, archive/reset, replay, and boundary
   heartbeats. It releases that lease only for the owner-first restart handoff;
   contention or a failed owner acquisition leaves downstream producers
   stopped. There is no operator lock-path override.
7. Create
   `/etc/liquidity-migration/account-execution-capture-enabled` at the start of
   the bounded demo/paper evidence window. This marker enables the guarded
   demo/paper capture topology; it makes no parity, calibration, P&L, or
   deployment claim. If every gate later passes, retain the same marker for the
   authorized deployment and runtime. Never recreate it to conceal an abort or
   failed boundary. The retired
   ambiguous `account-execution-ready` filename has no enabling effect and must
   not exist.
8. Start **only** `liquidity-migration-account-execution.service`. Verify its
   owner lease, API-key permission check, raw L2 capture, private execution
   stream, REST position reconciliation, strict transaction-log funding
   reconciliation, verified rules, native protection, owner-health artifact,
   convergence health, and canonical summary before starting a demo producer.
9. Start the target-only demo sleeves needed for the registered tape. They must
   report queued targets, never self-authored fills/P&L or venue mutation. Keep
   paper owner/producers stopped until demo calibration passes.
10. Capture the minimum registered demo target/order/ack/fill/P&L and raw-book
    sample, calibrate the twin, and install the self-hashed passing calibration
    receipt in the paper route. The full gate requires both the original
    latency/slippage smoke floors and the prospective partial-fill
    identifiability gate. Stop on a failed full gate; do not lower a threshold
    after inspecting the result.

    Natural LONG/CONTINUOUS events remain required for their strategy-parity
    comparison, but they need not be abused to manufacture the execution-twin
    sample count. The prospective bounded driver in
    `docs/preregistration/account_execution_calibration_v8_2026_07_15.md` publishes
    one tiny target at a time through the same owner and event clock, holds no
    credentials, and explicitly cannot satisfy LONG/CONTINUOUS parity:

    ```bash
    scripts/ops.sh clock-offset --execute \
      --output "$EVIDENCE_ROOT/clock-offset-v8.json"

    scripts/ops.sh demo-calibration --execute \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --inbox-root /opt/liquidity-migration/data/bybit-account-intents \
      --demo-rules-file "$EVIDENCE_ROOT/demo-rules-v8.json" \
      --event-tape "$EVIDENCE_ROOT/demo-calibration-v8-events.jsonl" \
      --output "$EVIDENCE_ROOT/demo-calibration-v8-run.json" \
      --expected-commit "$(git rev-parse HEAD)" \
      --plan-id demo-calibration-20260715-v8
    ```

    The launcher has no resume mode. It refuses any pre-existing event tape or
    output receipt. Once it emits an event, V8 is spent even if a later step
    aborts; retain the paths and close the attempt rather than deleting,
    resetting, or retrying it.

    After a successful driver receipt and its verified final flat boundary,
    let the demo owner publish one final exact-head reconciliation/health row,
    then stop it before constructing the immutable calibration receipt:

    ```bash
    scripts/ops.sh twin-calibrate \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --market-capture-root /opt/liquidity-migration/data/bybit-account-market-capture \
      --account-id bybit-demo-unified \
      --clock-offset-receipt "$EVIDENCE_ROOT/clock-offset-v8.json" \
      --output "$EVIDENCE_ROOT/execution-twin-calibration-v8.json"
    ```

    The calibration constructor source-reopens the stopped journal and raw
    capture set and creates, never replaces, the output. A non-passing receipt
    or any source mutation is the V8 outcome; it is not a test failure to retry.

    V8's small fixed orders do not guarantee a multifill. If every original
    floor passes but no within-order fill spacing is observed, retain the V8
    receipt as smoke evidence and leave paper stopped. Do not enlarge, extend,
    reset, or retry V8 to manufacture the missing event. A targeted partial-fill
    follow-up needs its own prospective size/risk and stopping rule.

    Add the preregistered `--funding-symbol BTCUSDT` and an explicit future
    `--funding-close-not-before-ms` only when that timestamp was recorded from
    the venue before the hold opened. Never improvise it after inspecting the
    funding result.
11. Treat V8 as training data, not the natural comparison epoch. Only after
    `execution_twin_gate_passed=true`, with the account already flat and every
    managed unit stopped, continue. Before any live V8 path is reset or reused, materialize and
    independently re-open its immutable archive-source map, then derive the
    exact baseline and `p95` stress configs from that archived calibration:

    ```bash
    scripts/ops.sh v7-archive from-stopped-roots \
      --repository-root /opt/liquidity-migration \
      --expected-candidate-commit "$(git rev-parse HEAD)" \
      --calibration-file "$EVIDENCE_ROOT/execution-twin-calibration-v8.json" \
      --destination-root /absolute/new/path/v8-immutable-sources \
      --archive-map-output /absolute/new/path/v8-archive-source-map.json

    scripts/ops.sh twin-drift freeze-configs \
      --calibration-file "$EVIDENCE_ROOT/execution-twin-calibration-v8.json" \
      --max-decision-age-ms 250 \
      --baseline-output /absolute/new/path/v8-baseline-p50.json \
      --stress-output /absolute/new/path/v8-stress-p95.json
    ```

    Archive materialization must happen before reset; recovering from the reset
    archive is an explicit fallback, not the normal order. Next freeze the
    candidate universe, complete the full candidate rule probe, and bind its
    exact coverage. Every output below must be absent; do not reuse the V8
    three-symbol receipt or overwrite a failed natural probe:

    ```bash
    .venv/bin/python scripts/freeze_account_candidate_universe.py \
      --output "$EVIDENCE_ROOT/candidate-universe-natural.json"

    .venv/bin/python scripts/probe_bybit_demo_rules.py \
      --symbols-file "$EVIDENCE_ROOT/candidate-universe-natural.json" \
      --max-probe-notional-usdt 200 \
      --probe-distance-bps 100 \
      --max-private-requests-per-second 5 \
      --leverage 10 \
      --output "$EVIDENCE_ROOT/demo-rules-natural.json" \
      --confirm-demo-probe

    .venv/bin/python scripts/verify_candidate_rule_coverage.py \
      --candidate-universe "$EVIDENCE_ROOT/candidate-universe-natural.json" \
      --demo-rules "$EVIDENCE_ROOT/demo-rules-natural.json" \
      --max-rule-age-hours 168 \
      --output "$EVIDENCE_ROOT/candidate-rule-coverage-natural.json"
    ```

    Before either natural owner starts, update both owner environment files so
    `ACCOUNT_SYMBOLS_FILE` names the candidate-universe JSON directly and
    `ACCOUNT_DEMO_RULES_FILE` names the full natural receipt. The shared symbol
    loader accepts the self-hashed JSON object's `symbols` field. The paper
    route must additionally name the passing archived-V8 calibration receipt.
    Source-reopen all three files after installing the environment files.

    ```bash
    ACCOUNT_SYMBOLS_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/candidate-universe-natural.json
    CANDIDATE_UNIVERSE_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/candidate-universe-natural.json
    ACCOUNT_DEMO_RULES_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/demo-rules-natural.json
    ACCOUNT_TWIN_CALIBRATION_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/execution-twin-calibration-v8.json  # paper only
    ```

    Only then archive/reset the demo and paper account, inbox, and raw-capture
    roots **again**:

    ```bash
    scripts/ops.sh reset \
      --execute --leave-stopped --sleeves all --label natural-holdout \
      --receipt "$EVIDENCE_ROOT/natural-holdout-reset.json"
    ```

    This second reset is the holdout boundary. In addition to the six
    account/inbox/capture roots, it archives and clears the registered LONG and
    CONT event/outcome/target and effective-config files, CONT lifecycle/risk
    telemetry, and the shared natural-runtime root. Retain its receipt and
    prove every registered output is empty. Start the **paper owner alone first**.
    Keep every paper producer stopped,
    verify exact-head owner health/topology and growing capture, then stop the
    paper owner cleanly. This is readiness evidence only. Do not replay old
    event timestamps into a live paper producer or retime them to make the
    check pass.

    Next start the **demo owner alone**, verify the same bound health checks,
    and keep every producer stopped. Create both reviewed owner-first receipts
    now, from immutable owner-only evidence captured before any producer starts:

    ```bash
    scripts/ops.sh cutover-authority review-evidence \
      --role paper_owner_start_sequence \
      --claim 'PAPER_OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_PAPER_PRODUCER_START_AND_STOPPED_BEFORE_REPLAY' \
      --reviewed-by OWNER_REVIEW_ID \
      --source /absolute/path/paper-owner-first-systemd.log \
      --output "$EVIDENCE_ROOT/paper-owner-first.json"

    scripts/ops.sh cutover-authority review-evidence \
      --role demo_owner_start_sequence \
      --claim 'DEMO_OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_DEMO_PRODUCER_START' \
      --reviewed-by OWNER_REVIEW_ID \
      --source /absolute/path/demo-owner-first-systemd.log \
      --output "$EVIDENCE_ROOT/demo-owner-first.json"
    ```

    Retain both receipts before creating the freeze.
    While the demo owner remains healthy, bind one future UTC-hour `T0`,
    `T1=T0+120h`, the exact clean commit/config, V8 receipt/config, six reset
    roots, owner-first receipts, rule/universe receipts, reset receipt, and the
    fresh clock receipt (the initial member of the periodic series, observed no
    more than six hours before T0) in the top-level freeze manifest. Create the
    immutable natural runtime config from that freeze before constructing either
    producer's market-data resources.

    The current freeze/run-config surfaces are explicit; use every registered
    source, not an abbreviated hand-written manifest:

    ```bash
    COMMIT="$(git rev-parse HEAD)"
    scripts/ops.sh natural-freeze create \
      --repository-root /opt/liquidity-migration \
      --candidate-commit "$COMMIT" --origin-main-commit "$FROZEN_MAIN" \
      --t0-ns "$T0_NS" --t1-ns "$T1_NS" \
      --demo-account-root /opt/liquidity-migration/data/bybit-account-execution \
      --demo-inbox-root /opt/liquidity-migration/data/bybit-account-intents \
      --demo-capture-root /opt/liquidity-migration/data/bybit-account-market-capture \
      --paper-account-root /opt/liquidity-migration/data/bybit-account-paper \
      --paper-inbox-root /opt/liquidity-migration/data/bybit-account-paper-intents \
      --paper-capture-root /opt/liquidity-migration/data/bybit-account-paper-market-capture \
      --local-suite /absolute/path/local-suite.json \
      --linux-ci /absolute/path/exact-candidate-linux-ci.json \
      --clock-offset /absolute/path/clock-offset-initial.json \
      --candidate-universe "$EVIDENCE_ROOT/candidate-universe-natural.json" \
      --demo-rules "$EVIDENCE_ROOT/demo-rules-natural.json" \
      --rule-coverage "$EVIDENCE_ROOT/candidate-rule-coverage-natural.json" \
      --calibration "$EVIDENCE_ROOT/execution-twin-calibration-v8.json" \
      --archive-map /absolute/path/v8-archive-source-map.json \
      --baseline-config /absolute/path/v8-baseline-p50.json \
      --stress-config /absolute/path/v8-stress-p95.json \
      --reset-archive /absolute/path/natural-holdout-reset.tar.gz \
      --reset-sha256 /absolute/path/natural-holdout-reset.tar.gz.sha256 \
      --reset-receipt "$EVIDENCE_ROOT/natural-holdout-reset.json" \
      --paper-owner-first "$EVIDENCE_ROOT/paper-owner-first.json" \
      --demo-owner-first "$EVIDENCE_ROOT/demo-owner-first.json" \
      --route demo=/etc/liquidity-migration/account-execution.env \
      --route paper=/etc/liquidity-migration/account-paper-execution.env \
      --risk-policy demo=/etc/liquidity-migration/account-execution/risk-policy.json \
      --risk-policy paper=/etc/liquidity-migration/account-paper-execution/risk-policy.json \
      --seed "$EVIDENCE_ROOT/v8-symbols.txt" \
      --output "$EVIDENCE_ROOT/natural-cutover-freeze.json"

    scripts/ops.sh natural-run-config build \
      --freeze-manifest "$EVIDENCE_ROOT/natural-cutover-freeze.json" \
      --output /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json

    scripts/ops.sh natural-run-config materialize-env \
      --config /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json

    scripts/ops.sh natural-run-config verify-env \
      --config /opt/liquidity-migration/data/bybit-natural-account-cutover/natural-run-config.json
    ```

    Freeze validation parses both route EnvironmentFiles instead of binding
    opaque hashes. Their demo/paper account, inbox, capture, candidate,
    demo-rule, risk-policy, calibration, and paper `p50` settings must match the
    named freeze sources exactly; both risk policies must resolve to the same
    limits, rule freshness cannot exceed 168 hours, queue-head market warmup
    cannot exceed 30 seconds, and `REAL_MONEY` must be unset or explicitly
    false. The distinct seed is the original mode-`0600` three-symbol V8
    file, not the full candidate-universe artifact.

    The last two commands atomically install and source-reopen the owner-only
    `/etc/liquidity-migration/natural-run.env` containing exactly
    `NATURAL_EVIDENCE_REQUIRED=1` and the absolute frozen config path. They do
    not start a service, deploy, or contact the venue. If either command is
    absent or fails, starting ordinary-mode producers does not count as the
    natural holdout.

    At `T0`, start only the registered demo LONG and CONT producers. Their
    event, outcome, and shared target-capture paths come only from the natural
    runtime config. Their natural input
    events are committed before the callback; only after callback success do
    they re-read actual durable requests under the inbox lock and append the
    shared hash-chained capture plus sleeve-local outcomes. A successful
    no-target callback records an explicit empty cycle. Any publication,
    callback, durability, or outcome failure invalidates that hour.
    Continue credential-free public clock samples at the six-hour target
    cadence. Do not backfill a missed observation; an adjacent gap above eight
    hours makes natural feed-latency evidence unavailable.

    Preserve the full half-open `[T0,T1)` window. Producers must refuse to
    dispatch at or after `T1`; stop them before publishing any post-window
    request. With the demo owner still active, publish canonical zero targets
    only through the registered target-only safety path under
    `natural-safety-flatten/<freeze-id>/...`, with
    `natural_safety_flatten=true` and the exact `natural_freeze_id`. Capture it
    in a separate mode-`0600` tape and build the mode-`0600`, self-hashed
    `natural_account_post_window_safety_manifest_v1`. The natural replay
    accepts only that exact registered post-window batch set; any other extra
    strategy batch fails. Safety batches are excluded from natural scheduling,
    parity, and lifecycle floors, but remain in final venue accounting and
    flatness. Let the demo owner converge and reconcile flat after consuming
    the safety requests.

    The credential-free target publisher refuses pre-T1 time, a
    stale/non-exact owner head, unresolved inbox work, working orders, unknown
    component ownership, or any nonzero/invented target. A partial publication
    retains each successful request in the separate capture but creates no
    manifest; after the owner consumes those requests, retry with the same
    capture path and a still-absent manifest path:

    ```bash
    scripts/ops.sh natural-safety-flatten --execute \
      --account-id bybit-demo-unified \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --inbox-root /opt/liquidity-migration/data/bybit-account-intents \
      --freeze-id "$NATURAL_FREEZE_ID" --t1-ns "$T1_NS" \
      --max-owner-health-age-seconds 30 \
      --target-capture /absolute/path/post-window-safety-target-capture.jsonl \
      --manifest-output /absolute/path/post-window-safety-manifest.json
    ```

    A successful manifest binds only the captured zero-target publications. It
    does not claim owner acceptance, order/fill completion, convergence, or
    final flatness; those remain the stopped-owner accounting gates below.

    Stop and preserve the natural capture. Do **not** start any offline replay
    yet. First complete the stopped accounting and source-seal transaction in
    step 12. The replay commands and their exact current arguments are in
    `docs/operations.md`; all outputs must be new and outside the sealed source
    roots.
12. Using the registered post-window safety flatten from step 11, verify
    venue/demo flatness and keep all producers and auxiliary units stopped.
    Let the demo owner complete one final position/funding reconciliation and
    write exact-head health, then stop it; the paper owner remains stopped.
    Build the stopped effective-config bundle and the complete clock series
    while the source bytes are still in place:

    ```bash
    scripts/ops.sh natural-effective-config bundle \
      --receipt long=/opt/liquidity-migration/data/bybit-long-demo-event/natural-effective-runtime-config.json \
      --receipt continuous=/opt/liquidity-migration/data/bybit-continuous-demo-event/natural-effective-runtime-config.json \
      --output /opt/liquidity-migration/data/bybit-natural-account-cutover/effective-runtime-config-bundle.json

    scripts/ops.sh clock-series build \
      --freeze-manifest /absolute/path/natural-cutover-freeze.json \
      --clock-offset-receipt /absolute/path/clock-offset-initial.json \
      --clock-offset-receipt /absolute/path/clock-offset-periodic-01.json \
      --clock-offset-receipt /absolute/path/clock-offset-periodic-N.json \
      --output /absolute/path/clock-offset-series.json
    ```

    With all 12 managed services/timers stopped, capture the exact demo
    accounting epoch while the authenticated-account lease proves the demo
    account owner is not running:

    ```bash
    EPOCH_START_MS="$(python3 -c 'from liquidity_migration.account_kernel import read_account_journal; import sys; e=read_account_journal(sys.argv[1], verify=True); print(e[0].wall_ts_ns // 1000000)' /opt/liquidity-migration/data/bybit-account-execution)"
    scripts/ops.sh venue-accounting \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --account-id bybit-demo-unified \
      --start-time-ms "$EPOCH_START_MS" \
      --output "$EVIDENCE_ROOT/venue-accounting-natural.json"
    ```

    The prospectively registered defaults require at least two `TRADE` rows,
    one closed-PnL row, and one `SETTLEMENT` row. This deliberately requires the
    bounded demo sample to span an actual funding settlement; do not lower the
    floor after observing a zero-funding window. The fresh epoch must remain at
    most seven days because that is the venue API's explicit query boundary.
    The command is read-only at the
    venue, refuses mainnet credentials, replays the stopped canonical journal,
    and exits nonzero unless exact target/order/fill lineage, observed fees,
    fill P&L, funding, and local/venue pre/post flatness all agree. Reconcile the
    event-tape hashes, historical/paper/demo journal structure, and documented
    scheduler limitations separately. Remove
    `account-execution-capture-enabled` and stop producers immediately on any
    abort condition.

    Only after that receipt passes, create and re-open the mode-`0600`
    `stopped_natural_epoch_seal` over the exact 11 old mutable roots, every
    file below them, the freeze manifest, stopped effective-config bundle, clock
    series, post-window safety manifest, venue-accounting receipt,
    freeze/candidate/config/window identities, tape semantics, and the stopped
    state of all 12 managed units. This is the final live-source boundary. Use
    `scripts/ops.sh stopped-epoch create` with the exact five input roles and 11
    root roles listed in `docs/operations.md`, then verify it with the required
    seal flag before analysis:

    ```bash
    scripts/ops.sh stopped-epoch verify \
      --seal /absolute/new/path/stopped-natural-epoch.json \
      --require-currently-stopped
    ```

    Now—and only now—run target replay, event parity with the target replay's
    required `--replay-manifest`, captured-account replay, schema-v3 scope
    construction, schema-v4 account parity, twin drift, and natural-tape
    sufficiency using the exact commands in `docs/operations.md`. Every replay
    output and child manifest must be under a dedicated derived-evidence root
    outside the 11 sealed roots and the later ten fresh-deploy roots. Code-path
    existence, a pre-cutover tape, or the lower venue-accounting registered
    row floors do not satisfy the 120-hour natural gate. Lowering those floors
    or widening the registered accounting tolerances is rejected by both the
    accounting command and its receipt verifier. Constructor success is
    also insufficient: the schema-v4 authority aggregate must pass its exact
    stopped-tree path/hash reconstruction, dependency, and derived-output
    disjointness checks against the real artifacts.
13. Only after every registered gate passes, create a disjoint ten-root
    fresh-deploy epoch bound to that stopped seal. Its six demo/paper
    account/inbox/capture roots and four LONG/CONT demo/paper data roots must be
    new, empty, mode `0700`, pairwise disjoint, and outside every sealed and
    derived-evidence path:

    ```bash
    scripts/ops.sh fresh-deploy-epoch create \
      --stopped-seal /absolute/new/path/stopped-natural-epoch.json \
      --epoch-parent /absolute/new/path/fresh-deploy-epoch

    scripts/ops.sh fresh-deploy-epoch verify \
      --manifest /absolute/new/path/fresh-deploy-epoch/fresh-deploy-epoch.json \
      --require-empty-roots
    ```

    Then create an open assessment template bound to the already-staged full
    commit.
    Machine validation covers the natural freeze, candidate-rule coverage,
    complete demo-rule probe, V8 calibration, schema-v3 captured-account replay,
    schema-v3 strategy-event parity, schema-v3 comparison scope, schema-v4
    kernel parity, schema-v3 natural sufficiency, twin
    drift, venue accounting/final flatness, stopped-natural-epoch seal, and
    fresh-deploy epoch. The same venue receipt supplies both accounting and
    flatness roles. For topology, paper/demo owner-first start ordering, and the
    final evidence card, create reviewed-evidence wrappers over the exact
    immutable source files. Those
    wrappers bind bytes and an operator claim; they do not pretend the
    underlying judgment was automated.

    ```bash
    COMMIT="$(git rev-parse HEAD)"
    scripts/ops.sh cutover-authority template \
      --authorized-commit "$COMMIT" \
      --authorized-by OWNER_REVIEW_ID \
      --output "$EVIDENCE_ROOT/account-execution-cutover-assessment.json"

    # Repeat review-evidence for each remaining non-machine-validated role in
    # the template. The exact paper/demo owner receipts were already created
    # before the natural freeze and must be reused byte-for-byte here.
    ```

    Populate every evidence path and decision. Change a gate from `open` to
    `passed` only after its registered rule passes; do not revise the rule after
    viewing the result. Then issue and verify the short-lived receipt:

    ```bash
    scripts/ops.sh cutover-authority issue \
      --assessment "$EVIDENCE_ROOT/account-execution-cutover-assessment.json" \
      --repo-root /opt/liquidity-migration \
      --output /etc/liquidity-migration/account-execution-deploy-ready

    scripts/ops.sh cutover-authority verify \
      --receipt /etc/liquidity-migration/account-execution-deploy-ready \
      --expected-commit "$COMMIT" \
      --repo-root /opt/liquidity-migration
    ```

    Reviewed wrappers, the assessment, and the authorization are all
    create-only. Use new versioned paths for the wrappers and assessment. The
    fixed deploy-ready path must be absent for this unique activation attempt;
    an existing authorization is an unresolved prior activation boundary, not
    permission to overwrite or delete it. Stop and obtain an owner decision if
    that path already exists.

    Immediately before any authorized startup, the checked deploy must use the
    authority-aware preparation surface. It re-opens the receipt, requires the
    stopped seal to still observe every registered unit stopped, requires all
    ten roots empty, and materializes the exact late systemd environment:

    ```bash
    scripts/ops.sh authorized-deploy-epoch prepare \
      --authorization /etc/liquidity-migration/account-execution-deploy-ready \
      --expected-commit "$COMMIT" \
      --repo-root /opt/liquidity-migration \
      --output-directory /etc/liquidity-migration/fresh-deploy \
      --systemctl-bin systemctl
    ```

    Preparation also publishes an owner-only, source-bound runtime latch next
    to the fresh environment directory and consumes the explicit pre-cutover
    evidence marker. Every registered owner, producer, hedge, refresh, and
    liveness service enters through `run_authorized_fresh_runtime.sh`. That
    wrapper verifies the exact environment inherited by its own process and
    immediately `exec`s the workload, eliminating a second systemd environment
    load between verification and execution. Before activation the same wrapper
    requires the commit/machine-bound evidence marker. After activation it uses
    bounded identity checks for the clean checkout, machine, authorization,
    fresh manifest, materialization receipt, all fragments, and the unit's late
    values; it does not rehash the historical archives on every timer start.
    Partial or deleted activation state, a lingering pre-cutover marker, or any
    changed dependency blocks restart instead of silently reverting to an old
    account or strategy root. The persisted latch records the bounded
    authorization that activated this filesystem epoch; it is not reusable
    authority for another commit or deployment. Deployment never shell-evaluates
    the generated systemd EnvironmentFiles: it reopens the activated epoch through
    the authority verifier and transfers only the exact ten root paths as
    NUL-delimited data.

    Startup is owner-first in both environments: start the demo owner and wait
    for its exact-head/capture readiness check, then start the paper owner and
    wait for its readiness check, before starting any producer. Bootstrap the
    LONG/CONT public-data producers before RMOM refresh; enable hedge and
    liveness services/timers last. Live verification must also check the active
    processes' exact late variables. Recovery may re-open this epoch but must
    never recreate a missing bound root.

    Point both `venue_accounting_reconciliation` and
    `venue_flatness_snapshot` evidence entries in the assessment at the
    machine-validated accounting receipt; do not wrap it in an operator
    attestation. Owner-first health/topology is bound through the freeze and
    reviewed start-order evidence; final stopped state and journal/accounting
    identity come from the stopped seal and venue receipt.

    The mode-`0600` receipt expires within 24 hours and binds the assessment and
    evidence hashes to the host machine-id fingerprint and exact clean commit.
    Its self-hash is not a signature, and the operator remains responsible for
    the non-machine-verifiable decisions. Initial full deploy requires the
    persistent capture marker plus this unexpired receipt and refuses the
    retired ambiguous marker. After one-time activation consumes the
    pre-cutover marker, routine verification and same-commit recovery use the
    persistent activation latch and exact bound artifact identities; they do
    not turn an expired authorization into new deployment authority. With the
    current evidence, leave the deploy-ready receipt absent and do not deploy.

## Branch promotion and cleanup

Do not equate “latest branch” with “safe `main`.” A push that touches a path in
the VPS workflow's registered `main` filter triggers the checked deployment,
while this experiment prospectively forbids branch promotion before the final
source-reopened outcome. Keep the candidate on its cutover branch through Linux
    CI, V8, the fixed natural window, final accounting/flatness and stopped sealing,
replay, drift, sufficiency, fresh-deploy-root creation, and authorization
review.

Only after the short-lived authorization exists for that exact commit and the
live remote `main` still equals the base frozen before the holdout may the
operator perform a fast-forward-only promotion:

```bash
: "${FROZEN_MAIN:?set the full main commit frozen before the holdout}"
: "${AUTHORIZED_COMMIT:?set the full commit from the verified authorization}"
test -z "$(git status --porcelain=v1)"
git fetch --no-tags origin main
test "$(git rev-parse --verify 'origin/main^{commit}')" = "$FROZEN_MAIN"
test "$(git rev-parse --verify 'codex/account-execution-cutover^{commit}')" = "$AUTHORIZED_COMMIT"
git switch main
git merge --ff-only origin/main
test "$(git rev-parse HEAD)" = "$FROZEN_MAIN"
git merge --ff-only codex/account-execution-cutover
test "$(git rev-parse HEAD)" = "$AUTHORIZED_COMMIT"
git push origin HEAD:main
```

The `main` push is the deployment boundary, not repository housekeeping. Wait
for the checked deployment and verification to succeed before deleting the
remote cutover branch. Remove any remaining local branch only after proving it
has no unique commits and pruning only missing or clean worktrees. Never force
push `main`, delete a branch to hide an unmerged result, or let unrelated
big-PC alpha work advance the frozen base during the window.

Do not combine a mass repository cleanup with this forward-evidence cutover.
After the cutover is either verified deployed or explicitly closed and the
worktree is clean, use `docs/repository_cleanup_handoff.md` from a separate
branch/worktree. Apparent age, naming, or an `rg` miss is not sufficient proof
that a migration reader, evidence verifier, recovery path, or historical
receipt can be deleted.

## Deterministic event tape

The design lesson is narrower than “use an event bus.” Jane Street's
[exchange talk](https://www.janestreet.com/tech-talks/building-an-exchange/)
ties recoverability to ordered transactions, deterministic state-machine
application, and representing timer-driven state changes in the replayable
log. NautilusTrader's
[architecture](https://nautilustrader.io/docs/nightly/concepts/architecture/)
similarly shares a common kernel across backtest, sandbox, and live contexts,
while its
[deterministic-simulation contract](https://nautilustrader.io/docs/latest/concepts/dst/)
calls out every clock, scheduler, randomness, collection-order, thread, and
network escape that remains outside deterministic scope. This cutover therefore
treats the event clock, durable target publication, account kernel, and replay
adapters as one ordered state-transition path. It does **not** claim that merely
wrapping three different strategy loops in the same clock makes their raw
market inputs or signal selection equal; those remain explicit source and
decision evidence.

For the registered natural LONG/CONT evidence path, historical, paper, and demo
scheduling inputs use `StrategyEvent` and `DeterministicEventClock`. The total
order is event time, event phase, source, source sequence, and canonical event
id. Inputs are fsynced to a hash-chained JSONL tape before the callback runs;
duplicates, chain corruption, and backward time fail closed. A `VirtualClock`
advances to the same recorded event time in replay, and live callbacks receive
that time as `now_ms` instead of rereading ambient wall time. This does not
cover the hedge, RMOM/liveness timers, every historical signal-selection loop,
or a live paper-producer run.

This establishes one scheduling boundary and replay mechanism. It does not make
different market tapes equal, turn synthetic bar books into L2, or prove that
all historical signal-selection code is the live adapter. Terminal raw tape
hashes are expected to differ when raw environment sources differ and are not a
parity test.

The cutover comparison is machine checked by `scripts/ops.sh event-parity`. Its
schema-v3 CLI requires
`--replay-manifest /path/to/target-replay/replay_manifest.json`. The schema-v2
manifest source-reopens the canonical capture and all 12 canonical replay
outputs and deterministically rederives their event, decision, and
scheduled-target semantics. Event parity is deployment-valid only when that
manifest names a `demo` source capture. It rejects empty, corrupt, backward,
duplicate, or under-specified tapes; requires byte-identical bound replay inputs
plus per-event `replay_input_sha256` and a separate one-outcome-per-event
hash-chained decision tape with sorted `decision_keys`; and normalizes only
explicit raw source maps and `execution_environment` payload fields. It compares
event time, phase/kind, canonical normalized event identity, source sequence,
all other payload, decisions, and both reconstructed normalized chains exactly.
`ingest_ts_ns` remains bound in the raw tape but is excluded because arrival
telemetry is not part of the event order key or replay input. No numeric
tolerance applies to those discrete identities; target-quantity tolerance
remains the separate account-kernel parity policy. The receipt still cannot
authenticate the market-data source, prove omitted strategy/config identity, or
establish signal-selection, raw-market, order/fill/P&L/funding parity, or deploy
authority. The adapter's `demo` output is explicitly an offline scheduling
replay and must never be cited as demo orders, fills, or P&L; those require the
actual demo account and venue tapes.

## Demo execution-twin calibration

After the demo owner has captured actual data, create the receipt with a fresh,
independently sourced clock-offset receipt:

```bash
scripts/ops.sh clock-offset --execute \
  --output "$EVIDENCE_ROOT/clock-offset-v8.json"

scripts/ops.sh twin-calibrate \
  --account-root /opt/liquidity-migration/data/bybit-account-execution \
  --market-capture-root /opt/liquidity-migration/data/bybit-account-market-capture \
  --account-id bybit-demo-unified \
  --clock-offset-receipt "$EVIDENCE_ROOT/clock-offset-v8.json" \
  --output "$EVIDENCE_ROOT/execution-twin-calibration-v8.json"
```

Both outputs are create-only. Preserve a failed clock or calibration receipt
and register a new prospective attempt rather than replacing either file.

The default preregistered floors are 5,000 clock-adjusted feed observations, 30
targets, 30 commands, 30 request/ack samples, 30 actually filled orders, 10 P&L
events, three symbols, and 95% command-to-captured-book linkage. Entry/response
latency and feed latency must be at least 99% nonnegative after clock correction.
At least 99% of command references must also match the linked, ungapped captured
decision-book midpoint within 0.01 basis point. Rejected or cancelled zero-fill
commands cannot satisfy the filled-order floor.

Those original floors now produce `market_order_smoke_gate_passed`. Receipt
schema v3 retains the v2 partial-fill rule: at least three observed multifill orders and three
within-order spacings between positive fills with valid venue timestamps that
strictly increase. Equal venue timestamps identify a multifill but not its
spacing. Terminal incomplete single-fill orders remain reported but satisfy
neither the multifill nor spacing floor. Only the conjunction is
`execution_twin_gate_passed`. The fixed V8 sample may therefore finish as a valid
latency/slippage smoke and an inconclusive partial-fill calibration.

The receipt binds the verified account journal and every raw capture segment by
SHA-256. Schema v3 reports feed latency, decision-to-socket delay, request/ack RTT,
clock-adjusted order entry/response, socket-send-to-first-fill, fill response,
and inter-fill spacing, plus multifill and
incomplete-filled-order rates, zero-fill terminal orders, fees, and adverse
slippage. Rejects are not mislabeled as partial fills. Slippage is separated into
the visible decision-book walk and the residual observed after that walk; the
paper twin applies a selected residual quantile after walking captured depth.
The baseline uses `p50`; `p75`/`p95`/`p99` are explicit stress choices.

These clocks are not interchangeable. The deterministic twin anchors socket
send to the immutable order command's `created_ts_ns`, rejects a command created
before its linked book was received, and measures decision-book age at socket
send. First-fill exchange time is calibrated from clock-adjusted socket send to
the first execution. Fill local-receive time uses the separately observed
exchange-fill-to-local-fill response. Inter-fill spacing applies only to fills
after the first; it is never reused as first-fill or terminal-status latency.

Those names follow hftbacktest's explicit separation of
[feed, order-entry, and order-response latency](https://hftbacktest.readthedocs.io/en/latest/latency_models.html).
Its [fill-model documentation](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)
also makes the relevant limitation clear: market-by-price data does not reveal
an order's passive queue position, so any passive queue model is an assumption
rather than an observation. The current market-order twin calibrates visible
book walking, latency, partial-fill timing, and residual slippage; it does not
smuggle in a passive queue estimate.

Zero multifills still produce the reported empirical rate and rule-of-three
upper bound, but they provide no fill-spacing estimate. Fewer than three
multifill orders or three positive spacing samples leave the full gate false.
The receipt must leave
`partial_fill_calibrated=false`, the full gate false, and paper startup blocked.
An offline smoke-only config uses `allow_partial_fills=false`,
`fill_spacing_ns=0`, and `single_level_full_fill_or_reject`, rejecting any path
that needs multiple book levels or an incomplete fill. Zero means no split-fill
timing is modeled; it is not a latency estimate. Never restore the fabricated
1-ns fallback.

Three repeated observations are a minimal bounded existence/timing basis, not a
multifill frequency estimate. At N=3, `p75`/`p95`/`p99` are stress labels rather
than empirically resolved tail quantiles.

The demo client preserves [Bybit V5's top-level response `time`](https://bybit-exchange.github.io/docs/v5/order/create-order#response-example)
in the canonical ack metadata path; without that server timestamp the request/ack RTT remains
observable, but one-way order-entry and response estimates are unavailable and
the calibration gate cannot pass. Duplicate-link recovery lookups are excluded
from latency samples: they prove idempotent venue ownership, not create-request
timing. The response-envelope timestamp is an API-server boundary, not proof of
matching-engine entry time. A fill may therefore carry an earlier exchange
timestamp than the HTTP create response; journal causality still records an
accepted or execution-inferred acknowledgement before applying the fill without
rewriting either venue timestamp. Schema-v2 calibration receipts cannot start
paper under this corrected model; a schema-v3 receipt must be reproduced from
the bound immutable calibration sources.

The clock receipt samples Bybit's unauthenticated `GET /v5/market/time` endpoint
from the VPS and combines it with a required NTP-synchronized local clock. V4
preconnects one TLS/HTTP1.1 session, aborts on reconnect, selects the five
lowest-RTT observations from 21, and refuses an estimated error above 100 ms or
selected RTT above 250 ms. The first 50-ms receipt failed and remains retained;
the wider prospective ceiling is explicitly scoped to hourly/sub-hourly paper
timing, not HFT or matching-engine claims. This bounds, but does not eliminate,
the symmetric-path assumption in one-way latency estimates.

That single receipt remains a V8 calibration input; it is not enough to correct
five days of natural feed timestamps. Before T0, freeze a registered receipt no
more than six hours old, then capture the same credential-free public receipt
on a six-hour target cadence through a sample at or after T1. Build the
source-reopening series with `scripts/ops.sh clock-series build`. It rejects
non-ordered or aliased members, any observed gap above eight hours, and endpoint
coverage farther than six hours. Natural drift consumes
`--clock-offset-series`, interpolates the local-minus-exchange point estimate at
each feed row's `local_receive_ts_ns`, and reports the bracketing-receipt error
plus observed offset movement as uncertainty. That uncertainty is not a hard
bound between samples. Request/ack RTT and exchange-timestamp fill spacing are
left unchanged. No timer is installed or enabled by the repository; an
operator-controlled timer may call the public-only capture with
`--output-directory`, which refuses private Bybit credential variables and
creates immutable timestamp-named files.

Bybit depth is market-by-price, not market-by-order. It cannot identify passive
queue position. The receipt therefore leaves passive queue calibration false
and scopes the twin to market orders with an immutable replay-book assumption.
That limitation is not repairable by inventing a queue parameter.
An observed multifill also does not identify its probability outside the
bounded sample, prove that MBP levels correspond one-to-one with venue execution
partitions, or turn the replay book into a market-impact model.

## Target lifecycle and operator messages

- An accepted target is desired state, not a fill. Strategy projections label
  it `target_pending`; it reserves admission/capacity but is excluded from
  exits, P&L, protection, and open-position reporting until reconstructed fills
  converge.
- A component lifecycle starts only from an attributable first fill. Max hold is
  first-fill time plus the target's duration, and component stop/take-profit
  prices are derived from confirmed fill VWAP. A pre-fill target, partial entry,
  or ambiguous aggregate-only same-symbol fill cannot trigger component
  protection.
- A rejected, cancelled, or partially-filled-cancelled order leaves the latest
  accepted desire durable. The owner retries only the uncovered residual with
  deterministic command ids and bounded exponential backoff. It never sends an
  offsetting order while an older order for the symbol is still working.
- Before executing a restart backlog, an older non-flat request may be archived
  as `superseded` when the final intents from one or more later requests replace
  all of its component keys. This matters when one atomic multi-component entry
  is followed by separate per-component safety flats. A target-flat safety
  transition is never skipped in favor of a later re-entry. Inbox order is a
  locked, durable local arrival sequence, not a producer filename, exchange
  timestamp, or wall-clock sort.
- Each target also carries its local creation revision. The kernel rejects a
  delayed request older than the latest accepted revision for that component,
  so an entry generated before a protection flat cannot arrive late and reopen
  it. Equal-time ambiguity is fail-closed for flat-to-nonzero transitions.
- Strictly proven reducing targets may proceed when entry-only margin/cap
  checks or unrelated books are unhealthy. Entries and increases still require
  fresh account-wide market, capital, rule, reconciliation, and protection
  health. Demo reductions still require a fresh direct venue-vs-kernel match
  for every requested symbol; venue-flat/local-open truth blocks the order
  instead of retrying Bybit error 110017. The kernel repeats the reduction
  proof inside the serialized transaction; the service preview cannot grant an
  exemption.
- Telegram sends a compact hourly account summary plus confirmed open, close,
  protection, and deduplicated loss-threshold events. Rejections and
  convergence polls do not emit per-cycle messages. During a venue/local
  mismatch it reports venue quantities separately from local reconstruction
  and suppresses untrusted exposure/estimated-uPnL and loss alerts. Local
  fill/P&L events are labelled as awaiting venue reconciliation rather than
  green closes. Valuation is explicitly the fresh captured Bybit L2 midpoint,
  not Bybit `markPrice` or venue-supplied uPnL. A missing, stale or gapped
  midpoint renders midpoint/notional/estimated-uPnL unavailable and degrades
  health; entry price is never substituted as a zero-P&L valuation.
- Each terminal reduce-only batch checkpoints incremental account/symbol P&L,
  including when another component remains open on the same symbol. The close
  message says `Reduced` rather than `Closed` in that case and retains the
  causal component ids and reasons. It does not allocate the net venue result
  to a component: component attribution stays `pending_account_netting`.
  The demo owner records strict venue funding settlements as separate immutable
  P&L events. Fill P&L, venue closed-PnL, funding completeness, and absent
  execution fees remain visibly provisional until the final machine-checked
  venue-accounting receipt reconciles the stopped epoch.
- Entry circuit-breaker losses are counted from those canonical account/symbol
  reduction events once per P&L key. Component ownership rows cannot multiply a
  single venue reduction into several losses.
- Bybit native TP/SL is one `Full`-position process-death seatbelt. When
  components supply different stops, it uses the outermost stop (long: lowest;
  short: highest), never the tightest component stop. Component stop/TP exits
  remain software target replacements, so one component trigger cannot flatten
  all same-symbol owners at the venue.

## Deterministic paper owner

Paper sleeves publish to a separate shared inbox and one paper account owner;
they do not independently pencil fills into sleeve Parquet ledgers. Configure
`/etc/liquidity-migration/account-paper-execution.env` with distinct roots:

```bash
ACCOUNT_PAPER_KERNEL_REQUIRED=1
ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-paper
ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-paper-intents
ACCOUNT_PAPER_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-paper-market-capture
ACCOUNT_RAW_MARKET_PERSISTENCE=1
ACCOUNT_SYMBOLS_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/candidate-universe-natural.json
CANDIDATE_UNIVERSE_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/candidate-universe-natural.json
ACCOUNT_DEMO_RULES_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/demo-rules-natural.json
ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-paper-execution/risk-policy.json
ACCOUNT_TWIN_CALIBRATION_FILE=/var/lib/liquidity-migration/cutover-evidence/REPLACE_WITH_NEW_ATTEMPT_ID/execution-twin-calibration-v8.json
ACCOUNT_TWIN_LATENCY_QUANTILE=p50
ACCOUNT_TWIN_SLIPPAGE_QUANTILE=p50
PAPER_EQUITY_USDT=10000
```

The paper owner uses the same account kernel and captured-book market-order
twin as historical replay. It refuses to start unless the self-hashed demo
calibration receipt passes its registered sample gate. Its L2 capture has an
independent liveness check; a healthy demo capture cannot mask a dead paper
feed.

## Abort conditions

Remove `account-execution-capture-enabled` and the
`account-execution-deploy-ready` authorization receipt, then stop target producers immediately if any
of these occur. If reconstructed or venue exposure is non-flat, keep the sole account
owner running only for reconciliation, native protection, and strictly reducing
recovery; stopping the only exit authority while exposure remains is not a
fail-closed action. Stop the owner after flatness is verified, or immediately if
the owner lease/journal itself is unsafe.

- venue/reconstructed position mismatch;
- desired target/working order/position convergence exceeds its health SLA or
  exhausts the bounded retry policy;
- missing, stale, crossed, or sequence-gapped live book outside an explicitly journaled, strictly reducing
  exit-only path;
- missing demo-verified rule row;
- open reconstructed position without active native disaster protection;
- a native stop is cancelled with residual reconstructed exposure;
- unknown external execution that is not position-reducing under an active
  native protection;
- duplicate authenticated demo-account lease;
- target/order/position parity hash mismatch.

Do not fall back to legacy direct order submission. Flattening demo exposure is
an explicit operator decision; real-money action remains prohibited.
