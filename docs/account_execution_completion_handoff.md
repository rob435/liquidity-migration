# Account-execution completion handoff

This is a copy-paste prompt for a continuation agent. It is workflow guidance,
not deployment or real-money authority. The canonical procedure remains
`docs/account_execution_cutover.md`; current facts remain in `STATE.md`, source,
tests, runtime configuration, and generated receipts.

Snapshot when this handoff was written on 2026-07-14:

- the working branch was `codex/account-execution-cutover` at committed head
  `98b3916a4a135df3508f051f2354bc2346904690`, based on `main` at
  `5f6d9986d935f3ae87f26ca0c931ee6acf038de5`;
- the worktree had 94 modified tracked files and 58 untracked files, with
  roughly 17,630 insertions and 4,251 deletions; nothing was staged, so those
  bytes were not a clean candidate;
- the installed `.git/hooks/pre-push` differed from the tracked hook and must be
  refreshed before the final candidate push;
- the dirty follow-on had no recorded complete-suite result; cached pytest state
  referred to removed tests and was not evidence;
- `STRATEGY_OVERHAUL_MASTER_PLAN_HANDOFF.md` was separate owner work for the
  big-PC alpha program and must not be deleted, folded into this cutover, or
  treated as deployment evidence;
- V7, the full partial-fill calibration gate, paper startup, the registered
  120-hour natural window, stopped/fresh epochs, final comparison, and cutover
  authorization did not exist.

The receiving agent must re-verify every snapshot claim. It must not reset,
switch away from, clean, or overwrite the dirty worktree merely because this
document names a committed head.

## Successor update — TradFi population exclusion, 2026-07-15

Replacement candidate `38f11d070d6a5d0a99bd76e52f139586df0c8aab` repaired the
terminal-order verifier, passed all 2,987 local/pre-push tests, exact-head
noncontacting Linux run `29432072223`, and stopped-fleet install-preflight run
`29432605744` with 174 host smoke tests. Its fresh 620-symbol candidate freeze
has artifact SHA-256
`c1fc7f03409de4df4aee024e25af9716a66fe905614dbcfc8c2b6a88ad1895ef`.

The create-only full probe verified 20 crypto rules, then failed at
`AAOIUSDT` with Bybit error `110126`: the venue requires a separate agreement
for that stock perpetual. The failure receipt self-hashes to
`76b52c589c4142674ef542390eba272e7acb3c90cb82c2202db5539dee46e610`;
cleanup and final authenticated flatness passed, no authority existed, and all
units remained stopped. Do not convert this attempt into a pass or sign a
master-account agreement under the general overhaul instruction.

The retained public source proves the underlying defect: Bybit's linear
endpoint mixed 100 `stock` and four `commodity` perps into a repository whose
declared strategy domain is crypto perps. The prospective schema-v3 repair
retains/hashes all 728 raw instrument rows, permits only empty and `innovation`
`symbolType` rows into ranking, and records each excluded non-crypto or unknown
type. Exact source replay reduces 620 candidates to 516 by removing those 104
rows while retaining all 120 `innovation` crypto rows. This is a deliberate
domain correction, so freeze a new exact candidate and repeat every gate and
artifact in a new namespace before startup.

## Successor update — full-population demo probe, 2026-07-15

Demo-operational candidate
`1690093011b35d0693f76ca754d0c28c12f9d8e1` passed repository Ruff, all 2,979
local/pre-push tests, exact-head Linux run `29429929636`, and the stopped-fleet
install preflight. The VPS was clean, flat, stopped, and paper-disabled. The
candidate froze a self-hashed 620-symbol public-demo population and attempted
the create-only full authenticated rule probe once.

That probe failed on the first symbol before authority or service startup. Its
`0GUSDT` PostOnly order was accepted at 9.97376 USDT, cancelled, and left no
position or execution, but exact order history remained empty for the complete
30-second/100-poll window. Cleanup and final authenticated flatness passed. The
failure receipt remains private and immutable at
`/var/lib/liquidity-migration/cutover-evidence/demo-operational-1690093-OgYr5Iyg/demo-rules-demo-operational.json.failed-1784131354681439580.json`,
artifact SHA-256
`091207fbdbda0935d296e96d8deb272bb2347ac9b91ff38a074d606f715a00b4`.
A later read-only exact query found the same order cancelled with zero
cumulative fill and no execution rows. That late observation diagnoses delayed
history visibility; it does not change the failed receipt into a pass.

The prospective replacement queries both official order-history and recent
real-time closed-order endpoints on every bounded poll. Every returned row must
match the exact symbol/order/link identity and show zero cumulative fill; an
unexpected status or contradiction on either endpoint fails. Two terminal
`Cancelled` observations from an official surface and an empty exact execution
history are still mandatory. Legacy receipts remain readable. Freeze a new
exact candidate, rerun all candidate gates, install it while flat/stopped, and
use a new evidence namespace rather than overwriting the retained failure.

## Successor update — operational/raw-tape scope, 2026-07-15

The owner subsequently made a narrower operational decision: the five-day raw
market tape is optional research and may be collected on another machine. It
does not alter trading logic and is no longer a prerequisite for running the
demo/paper VPS. This does not reinterpret or pass the registered natural study;
that study remains prospective under its unchanged 120-hour contract and must
use raw persistence `1` if it is ever run.

The operational boundary is different and remains strict. Both owners must
consume live sequence-aware L2. A bounded, same-systemd-generation readiness
sidecar and exact decision-boundary books remain durable, as does the canonical
account journal. Only continuous bulk order-book/public-trade persistence is
disabled. The paper owner still requires the unchanged passing V8 execution-
twin calibration, because that evidence affects modeled paper fills rather
than storage alone.

Candidate `0f05060ee30de819f270c3cb695a7f9b66fbebdd` passed its complete local
and pre-push gates with 2,957 tests. Its sole exact-head Linux run
`29403931189` retained one nondeterministic existing test failure and 2,956
passes. It was not retried and is spent. The prospective repair names both
dated liquidation files explicitly. A new frozen candidate and every candidate
gate remain required.

The prospective implementation has three non-overlapping authorization profiles:

- `calibration` binds one clean commit, machine, demo route, credentials,
  roots, and immutable inputs; enables raw capture; and authorizes only the demo
  account owner so V8 can bootstrap without paper or producers;
- `demo-operational` requires raw persistence `0`, binds only the demo route,
  requires demo-scoped liveness and a disabled paper sleeve, and authorizes the
  six demo owner/producer/hedge/refresh/liveness services without reading or
  claiming a paper twin receipt. Its owner symbol file and both producers'
  `CANDIDATE_UNIVERSE_FILE` are the same immutable artifact, and authorization
  rebuilds exact source-bound demo-rule coverage before any service starts;
- `operational` requires the passing twin receipt, binds both owner routes and
  all immutable inputs, requires raw persistence `0`, and authorizes only the
  nine checked demo/paper owner/producer/hedge/refresh/liveness units.

No profile claims natural replay, parity, drift, alpha, promotion, or real
money. Natural/fresh override files and simultaneous research/operational
receipts fail closed. The exact checkout, environment files, runtime-root
identities, config inputs, machine, and allowed unit are re-opened in the same
wrapper process before each workload.

For the current owner-authorized operational task, the older five-day
ready-to-deploy definition below is now the optional research-promotion path,
not the VPS startup prerequisite. The current truthful terminal outcome is
either:

- a new exact candidate passes the complete local/pre-push/noncontacting Linux
  gates; V8 passes once; that same candidate is installed; full operational
  authority is issued with raw persistence disabled; owners start before
  producers; and demo/paper health, venue/account integrity, liveness, and
  bounded storage behavior verify; or
- a specific unchanged candidate, V8, venue, calibration, or runtime gate
  fails, its evidence is preserved, unsafe services remain stopped/flat, and
  the smallest prospective next decision is reported.

The owner's permission includes demo/paper VPS installation and activation for
this task. It still does not authorize `REAL_MONEY`, mainnet credentials, a
`main` push, force-push, branch deletion, or fabricated/retried evidence.

## Successor update — demo-only continuation after V8 preflight, 2026-07-15

Replacement candidate `b501be38a4caade21efd3607fcfcbdd1c892ec28`
passed its complete local/pre-push/single exact-head Linux gates and installed
cleanly. Its fresh rule receipt made the unchanged V8 fixed-size preflight
decisive before publication: BTC required `160.15725 USDT` under the registered
2.5-times-minimum rule, while V8 fixed `160 USDT`. No V8 target, event tape,
order, fill, or run receipt was emitted. Authenticated venue/local flatness
passed, every service stopped, and the calibration authority was retired. V8
is closed and paper remains blocked; resizing or rerunning it is forbidden.

The owner's broader operational goal can continue without converting that
negative result into a pass. A new candidate may implement the distinct
`demo-operational` profile above, repeat every candidate gate, install while
stopped/flat, disable bulk raw persistence and the paper sleeve, issue a new
exact-commit/machine/input authorization, then start the demo owner before the
authorized demo producers and timers. Demo runtime observations are operational
or exploratory only and cannot satisfy V8, authorize paper, or support a
research-promotion/mainnet claim. This continuation is complete only when the
demo owner, enabled demo strategies, hedge/RMOM timers, and demo-scoped liveness
are healthy under bounded storage while every paper unit remains stopped and
unauthorized.

## Successor update — 2026-07-15

V7 is now closed and spent. Its final BTC funding-hold zero failed the
unchanged reconciliation-freshness gate; a separate canonical recovery target
left the journal and venue flat with no orders and every project unit stopped.
The recovery then exposed a concurrent immutable-Close collision. Candidate
`c7d6509d3a21c75db77ed9486129a3cc4cfaa591` passed its exact local and
non-contacting Linux gates but was never installed and does not repair those
defects, so it is retained as spent candidate history.

The active corrected-defect contract is
`docs/preregistration/account_execution_calibration_v8_2026_07_15.md`. V8 keeps
V7's exact sample, risk, clock, smoke, partial-fill, and abort rules; V7 data is
excluded. Every operational reference below to the next/passing V7 training
epoch is superseded by V8. Lexical `v7_training`, `v7-archive`, and
`--v7-archive-map` names remain compatibility schema/tool labels bound to V8,
never permission to reuse the failed V7 artifact.

The registered 120-hour natural window remains unchanged but is now optional
research, not part of the operational completion wall time. Demo/paper VPS
operation still requires V8 for the paper twin, bounded live-L2 readiness,
exact decision books, canonical journals, and the operational authority path.

Replacement candidate `54536f194d91bcabb5fe8f47310c6a09928ecf12` later
passed its complete local, canonical pre-push, and exact-head noncontacting
Linux gates, but its first public-demo capacity diagnostic failed before output
on ticker-only label `WC_ENG_ARG_USDT-15JUL26`, which was absent from the
complete instrument snapshot and outside the strategy symbol grammar. The
candidate was not installed or retried and is spent. The prospective repair
bumps the candidate-universe artifact to schema v2: every raw source row remains
hash-bound, noncanonical ticker-only rows are recorded explicitly as rejected
from candidate evaluation, and instrument-mappable/missing/duplicate rows stay
fail-closed. A first repaired capacity-only diagnostic observed 616 union
symbols and one rejected ticker-only row; it is not the natural freeze or a
rule-coverage receipt. First schema-v2 candidate
`344cd727b0d89380dd8bf4e7aaa112bfe5b3d885` passed its registered local suite
with 2,956 tests, then its canonical pre-push gate failed before network update:
the tracked hook placed pytest output under `.git/tmp`, while nine existing
Strategy Overhaul source-snapshot tests require immutable outputs outside the
repository. The 2,947-pass/one-failure/eight-error result is retained; the
candidate was not pushed or installed and is spent. The prospective hook fix
moves and validates only the pytest basetemp, not alpha logic or a gate. A new
exact candidate and all candidate gates remain required.

Candidate `181027b0853db9e543e30504211d701c7c95fc86` later passed its complete
local, canonical pre-push, and single exact-head noncontacting Linux gates,
installed cleanly, and produced a passing authenticated six-root reset. That
reset compressed the prior 6.9-GB raw root into a verified 877-MB archive and
left roughly 25 GB free. The first credentialed schema-v3 rule probe then
failed before owner startup and before any V8 target: BUSDT terminal
cancellation history appeared only once at the five-second boundary, not the
required twice. Cleanup and final flatness passed. Candidate `181027b` is
spent. The registered prospective repair changes only bounded read-only
terminal-history observation to 30 seconds/100 polls; all identity, no-fill,
rate, cleanup, and flatness gates remain unchanged. A new exact candidate and
all candidate gates are required before another create-only rule-probe path.

## Efficient validation cadence

Testing is progressive, not absent:

1. During implementation, map each changed invariant to its closest tests and
   run only those tests plus lint/type checks for the touched files. Batch
   related edits before rerunning them.
2. At subsystem boundaries, run the associated account-owner, replay/twin,
   runtime-script, or authorization test cluster. Do not repeatedly run the
   repository-wide suite on an unchanged class of failure.
3. Freeze all source, deploy, workflow, requirement, test, Graphify, and
   documentation changes before candidate validation. Then run the complete
   local gate and exact non-contacting `candidate-ci` workflow once for that
   exact commit.
4. If a complete gate fails, diagnose and iterate with its smallest reproducer.
   After the repair is frozen, rerun the complete gate once on the new exact
   candidate. A focused pass never substitutes for the registered candidate
   gate.
5. Do not change the repository after the candidate freezes. Keep operational
   receipts and the run ledger outside the repository throughout V8, the
   natural window, analysis, authorization, and deployment.
6. Do not rerun, resize, extend, or reinterpret V8 or the natural window to fish
   for a pass. Operational samples are forward evidence, not test retries.

## Copy-paste continuation prompt

```text
Continue the account-execution cutover in
/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration until it is either genuinely
deployment-ready under the registered contract or honestly blocked by a failed
forward gate. Continue to a truthful terminal outcome, not a guaranteed green
result. Do not call implemented constructors, green unit tests, or an offline
demo-labelled replay operational evidence.

Authority for this prompt:
- You may inspect and edit this repository, preserve and organize the existing
  cutover work, create focused commits on codex/account-execution-cutover, push
  that candidate branch without force, run non-mainnet demo/paper maintenance
  steps that the current runbook explicitly registers, and collect their exact
  receipts.
- You may not enable REAL_MONEY, use mainnet credentials, weaken a gate after
  seeing its outcome, force-push, discard dirty work, delete evidence, advance
  main, trigger deployment, or delete branches. FINAL_DEPLOY_AUTHORITY=NO.
- When every machine and operator gate passes, stop at the verified
  ready-to-deploy boundary, report the exact commit and authorization/evidence
  hashes, and ask the owner for a separate explicit go/no-go for the main push.

Read completely before acting:
- AGENTS.md
- STATE.md
- docs/account_execution_cutover.md
- docs/operations.md
- docs/governance.md
- docs/preregistration/account_execution_calibration_v7_2026_07_14.md
- docs/preregistration/account_execution_calibration_v8_2026_07_15.md
- docs/preregistration/account_execution_natural_replay_v1_2026_07_14.md
- .codex/skills/repo-map/SKILL.md
- .codex/skills/pit-reconcile/SKILL.md
- .codex/skills/run-strategy/SKILL.md before invoking operational CLI routes
- .codex/skills/vps-migrate/SKILL.md only if host, SSH, deploy-key, workflow, or
  expected-commit recovery actually enters scope

Start by preserving state, not by testing everything:
1. Record git status, current and upstream commits, worktrees, branch topology,
   stashes, staged/unstaged/untracked paths, and a diff summary. The worktree was
   already materially dirty when handed off. Do not reset, switch branches, run
   git clean, or overwrite unrelated owner work. Preserve
   STRATEGY_OVERHAUL_MASTER_PLAN_HANDOFF.md as separate big-PC work.
2. Reconcile source, tests, systemd units, workflows, requirements.lock,
   STATE.md, and the two active preregistrations. Treat code, tests, runtime
   config, and generated receipts as stronger than prose. Correct stale prose in
   the same pre-freeze change when it is in scope.
3. Identify the smallest remaining implementation gaps. Work in coherent
   batches. During edits run git diff --check, Ruff on touched paths, scoped
   mypy where types changed, and only the directly mapped tests. Use cluster
   suites at subsystem boundaries. Do not begin with repeated full-suite runs.

Candidate boundary:
4. Once implementation and documentation are frozen, review the whole diff for
   accidental strategy changes, private/public Bybit boundary regressions,
   alternate mutation paths, unsafe defaults, arbitrary command passthrough,
   hash-then-parse races, mutable evidence paths, and stale deleted-surface
   references.
5. Refresh the installed pre-push hook from scripts/git-hooks/pre-push and
   verify its bytes. After focused and subsystem validation is green, stage only
   the intended files, inspect the staged diff, and create one clean candidate
   commit. Record its exact commit and tree. That commit is the candidate freeze:
   do not edit repository bytes while validating it.
6. On that frozen commit, run one complete local candidate validation:
   repository-wide Ruff, full pytest, current scoped mypy, packaging/import
   integrity, and every gate required by the cutover runbook. If it passes, push
   only the candidate branch without force; the canonical pre-push hook must
   validate the same commit. Dispatch the exact `candidate-ci` mode and prove all
   SSH, host, install, recovery, verification, and deployment paths were
   skipped. Do not use a PR merge SHA as the frozen candidate. Record exact
   commands, counts, commit, environment, and deviations.
7. If a complete gate fails, preserve its receipt and use focused reproducers
   before making a new candidate commit; the prior frozen candidate is spent.
   Rerun the complete failed gate once on the new frozen commit, and never rely
   on an earlier green run after changing bytes. Once every candidate gate
   passes, keep that exact commit frozen through the terminal operational
   outcome: do not edit or commit any repository file, including STATE.md and
   runbooks. Store all receipts and the live evidence ledger outside the repo.
   A later code change spends the epoch: stop safely, retain the failure, make
   the prospective repair, and repeat the applicable candidate/forward gates.

Forward operational sequence:
8. Follow the current runbook and current --help output; do not reconstruct
   commands from memory. Keep every liquidity-migration unit stopped while
   re-proving demo flatness and archiving/resetting the six V8 account, inbox,
   and capture roots into a new verified epoch.
9. Run V8 exactly as preregistered: start the demo account owner alone, prove
   route/readiness and growing books, then collect actual target/order/ACK/fill/
   fee/P&L/funding and timing tapes. Paper and ordinary producers stay stopped.
   Preserve every abort and failed receipt. If the registered market-order smoke
   gate or partial-fill identification gate does not pass, do not resize,
   extend, reset, retry, or relabel V8. Paper remains blocked and the cutover is
   honestly blocked pending a new prospective study.
10. Materialize and verify the immutable V8 archive-source map through the
   compatibility `v7-archive` surface before reusing
    any live path. Freeze the candidate universe and complete the exact
    credentialed demo-rule coverage, then perform the second full registered-
    output natural-holdout archive/reset. Build and verify the exact natural
    freeze and mode-0600 natural-run environment.
11. Start the paper owner alone, prove readiness, and stop it cleanly. Start the
    demo owner alone before any producer. Only then start registered LONG and
    CONT target producers for the fixed half-open [T0,T1) 120-hour window.
    Maintain the registered periodic clock-offset series. Do not couple in the
    big-PC alpha job, hedge/RMOM/liveness evidence, or unregistered views.
12. At T1 use only the registered safety-zero path, converge flat, stop the
    complete fleet, capture authenticated venue accounting/funding/final
    flatness, bundle effective runtime configuration, and seal the exact stopped
    source epoch before any offline analysis. Unexpected exposure, open orders,
    route/credential mismatch, stale owner health, journal disagreement,
    mutated evidence, or failed flatness is an abort condition, not a waiver.
13. Write replay and analysis outputs outside every sealed source path. Run the
    source-reopening target replay, event parity, captured account replay,
    comparison scope, kernel parity, natural sufficiency, execution-twin drift,
    and all authority aggregation/verifiers in the registered order. Verify
    actual demo acknowledgements, fills, fees, realized P&L, funding, and
    flatness separately from modeled historical/paper agreement.
14. Only if every gate passes, create and verify the ten disjoint fresh
    deployment roots, materialize exact per-unit environments, and prepare the
    human evidence assessment. Do not self-pass a non-machine-verifiable field.
    Obtain owner review before issuing the short-lived exact-commit/machine/
    evidence-bound authorization. Do not start the fresh runtime and do not
    push main under this prompt.

Documentation and handoff discipline:
15. Before candidate freeze, update docs only for actual source/CLI behavior.
    During forward evidence, keep an external ledger containing gate name,
    command, start/end time, machine, commit, input hashes, output path/hash,
    result, and next allowed action. Send concise progress updates during long
    waits. A 120-hour wait is not completion and unchanged state is not a reason
    to improvise.
16. After verified deployment, use a separate docs-only commit to append the
    outcome to STATE.md, docs/research_summary.md, the preregistration index and
    append-only V7/V8/natural outcome sections. If a gate blocks the cutover,
    formally close that evidence attempt first, preserve all external receipts,
    then document the block in a new commit that is explicitly not the frozen
    candidate. Never rewrite registered thresholds or past outcomes.
17. Keep the .codex and .claude project-skill mirrors synchronized when either
    changes. Update Graphify before freeze only when architecture changed and
    doing so will not overwrite unrelated graph work.

Definition of done for this prompt:
- either one exact clean demo-operational candidate passes its complete local,
  pre-push, and single exact-head Linux gates; installs while stopped/flat;
  binds one immutable full candidate universe to exact source-bound current
  demo rules; starts the raw-disabled demo owner before enabled demo producers
  and timers; and verifies owner/strategy/liveness health, journal/venue
  integrity, bounded storage, and complete paper isolation;
- or a specific gate fails or remains unidentified, all evidence is preserved,
  unsafe services remain stopped/flat, and you identify the smallest
  prospective next decision without weakening or retrying an evidence gate.

The older V8/natural/replay/fresh-root definition is the separate optional
research-promotion path. V8 is already closed `failed_prepublication`; this
prompt must not retry it, infer a paper twin, or wait for the five-day tape.

Neither outcome permits real money. Do not promote main or delete the cutover
branch until a separately authorized deployment has succeeded and verified.
```
