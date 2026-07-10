---
name: research-phase-runner
description: Route, execute, and write up a registered research experiment in this quant repository. Use before running, conditionally running, monitoring, or interpreting an experiment. Read current state and the exact experiment contract, enforce its data boundary and decision rule, preserve artifacts and deviations, and apply docs/governance.md. Do not impose the historical three-tier MAR scripts, both-venue rule, commits, or pushes unless the active contract or user request requires them.
---

# Run a research experiment

Use the selected experiment contract as the procedural authority for that
experiment. Use `docs/governance.md` to judge evidence. Do not reconstruct an old
program from stale receipts.

## Resolve the active contract

1. Read `STATE.md`, `docs/research_summary.md`, and
   `docs/preregistration/INDEX.md` for current context.
2. Read the exact preregistration named for the experiment in full.
3. Inspect the dispatcher or runner and its current `--help`.
4. Confirm whether the request is exploratory or decision-influencing.

If a decision-influencing run lacks a prospective contract, create or amend one
before inspecting the affected outcomes. If the work is exploratory, label it
and keep it out of confirmatory claims.

## Preflight

Confirm and record:

- claim, comparator, allowed cells, and intended action;
- data roots, venue/population scope, end-exclusive boundary, and prior exposure;
- effective sample unit, horizon/stopping rule, and multiplicity treatment;
- primary decision rule, guardrails, and inconclusive outcome;
- PIT/timing/fill/cost/funding/capacity requirements relevant to the claim;
- code/config/data identities and expected artifact/receipt locations;
- machine resources, checkpoint/resume behavior, and safe concurrency;
- relevant worktree state without disturbing unrelated changes.

Do not substitute “both venues”, MAR, Sharpe, or a fixed trade count for this
design. Use them when the contract and claim justify them.

## Dispatch

- Prefer the experiment's named dispatcher. For the active tail study, use the
  `scripts/ops.sh tail-plan` and `tail-run` surface described by its contract.
- Run the plan/readiness mode first when provided.
- Use current `--help`; never infer today's end date when the contract freezes a
  boundary.
- Preserve command, stdout/stderr, partial cells, failures, hashes, and receipts.
- Report meaningful progress and any deviation while a long run is active.
- Stop or relabel when a validity gate fails; do not silently relax it.

## Decide and write up

Apply the contract's registered decision and stopping rule. Run
`scripts/r1_robustness.py` or `scripts/apply_decision_rule.py` only when the
contract names that historical preset or when using it as a labelled diagnostic.

Append results without rewriting the prospective section:

- every completed, failed, skipped, and aborted cell;
- effect sizes, uncertainty, concentration, and robustness diagnostics;
- deviations and their impact on data exposure;
- validity, result, scope, and explicit non-conclusions;
- artifact paths/hashes and reconstruction identities;
- the precise next action or reason to stop.

Update `docs/research_summary.md`, `STATE.md`, or the preregistration index only
when the run changes their stated facts. A request to run research does not by
itself authorize a commit, push, deploy, or profile change.

## Preserve negative evidence

A failed contract rejects only its precise claim under its conditions. Retain it.
Reopen with a new contract only for new data, a corrected defect, or a genuinely
different mechanism—not an undisclosed rescue sweep.
