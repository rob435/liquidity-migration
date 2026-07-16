---
name: research-phase-runner
description: Route, execute, and write up a prospectively registered research experiment in this quant repository. Use before running, monitoring, or interpreting decision-influencing research. Enforce the exact contract, exposure boundary, decision rule, and artifact plan under docs/governance.md; never reconstruct a deleted roadmap from historical receipts.
---

# Run a research experiment

Read `STATE.md`, `docs/research_summary.md`,
`docs/preregistration/INDEX.md`, and the exact contract named there. Determine
whether the experiment has an active prospective contract. If it does not,
create one before decision-influencing work.

## Preflight

Confirm and record:

- claim, intended action, comparator, and complete tested set;
- study mode and which outcomes were already inspected;
- venue/population scope, data roots, and end-exclusive boundary;
- effective sample unit, horizon/stopping rule, and multiplicity treatment;
- primary decision method, guardrails, and inconclusive outcome;
- relevant PIT, timing, fill, cost, funding, capacity, and accounting requirements;
- code/config/data identities and artifact locations;
- resource, checkpoint, resume, and safe-concurrency behavior.

Inspect the selected runner and current `--help`. Do not infer dates, venues,
metrics, commits, or pushes from old experiments.

## Dispatch

Use the contract's named runner when it exists. Run its read-only preflight mode
first when available. Preserve command, stdout/stderr, partial outputs, failed
cells, hashes, checkpoints, and deviations. Stop or relabel when a validity gate
fails; do not silently relax it.

Research execution does not authorize external demo orders unless the task
explicitly includes them. It never authorizes mainnet.

## Decide and record

Apply the frozen decision and stopping rule. Append without rewriting the
prospective section:

- every completed, failed, skipped, and aborted cell;
- effect sizes, uncertainty, concentration, and robustness diagnostics;
- deviations and their effect on exposure;
- validity, result, scope, and explicit non-conclusions;
- artifact paths/hashes and reconstruction identities;
- the precise justified next action.

Update summary/state files only when facts change. A failed contract closes only
its exact claim under its conditions. Reopen work only for new data, a corrected
defect, or a genuinely different mechanism under a new prospective contract.
