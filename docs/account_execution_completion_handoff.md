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

The minimum successful wall time is still more than five days because the
registered natural window is exactly 120 hours. Avoiding redundant suite runs
saves compute and operator time; shortening that window would invalidate the
registered evidence rather than accelerate it.

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
- either all preregistered code, candidate-CI, V8, owner-first, 120-hour natural,
  clock, accounting/flatness, stopped-seal, replay/parity/sufficiency/drift,
  fresh-root, and assessment gates pass for one exact clean commit and you hand
  back a verified ready-to-deploy package for explicit owner authorization;
- or a specific gate fails or remains unidentified, all evidence is preserved,
  services are left in the safest registered stopped/flat state, the failed
  attempt is documented only after its frozen window closes, and you identify
  the smallest prospective next decision.

Neither outcome permits real money. Do not promote main or delete the cutover
branch until a separately authorized deployment has succeeded and verified.
```
