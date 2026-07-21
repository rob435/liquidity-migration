---
name: research-phase-runner
description: Route, execute, and record research work under the Progressive Evidence Model in this quant repository. Use before running, monitoring, or interpreting decision-influencing research. Lane-1 exploration is unlimited on seen data; Lane-2 configs are graded on the rolling run of forward days after their git commit; promotion is a five-line note under docs/governance.md.
---

# Run research work

Read `STATE.md`, `docs/strategy_program.md`, and
`docs/preregistration/INDEX.md` for current context, provenance, open work,
and prior formulations. Before selecting new decision-influencing work,
record its relationship to the compact priors in the strategy program.
Negative priors inform questions; they are not forbidden lines. Decide which
lane the work is in; neither lane has a waiting room.

## Lane 1 — exploration

Run freely on any already-seen data: prototypes, sweeps, diagnostics, many
ideas in parallel. Record one provenance note (which data this touched) and
label outputs exploratory. Report all grid cells, era-split results, and
costs next to gross — a pooled number that hides decay is a wrong answer.
Do not invent universal Sharpe, return, sample-count, cost, or era-sign gates.
Rank follow-ups by information gain, mechanism plausibility, effect shape,
uncertainty, concentration, and executable economics, and record the judgment.

## Lane 2 — rolling forward record

To graduate a prototype: commit its exact config plus scoring recipe
(metric, baseline, declared grid) — the commit is the registration, and its
evidence note records which prior or new mechanism it descends from. The
scorer appends one row per config per new day; a config's evidence is the
run of days after its commit, and editing it starts a new run. Grade a rule
only on data it did not shape; commit configs before opening a new surface
(reserved holdout, freshly backfilled history), and record the opening in
`docs/preregistration/INDEX.md`.

Keep the physics intact in both lanes — causal/PIT inputs, executable
economics (fills, fees, funding, capacity), reconstructable accounting, and
honest provenance. A miss relabels the number as a diagnostic; say so and
keep moving rather than silently relaxing it.

## Execution mechanics

Inspect the selected runner and current `--help`; do not infer dates,
venues, metrics, commits, or pushes from old experiments. Preserve commands,
stdout/stderr, partial outputs, failed cells, and hashes. Research execution
does not authorize external demo orders unless the task explicitly includes
them. It never authorizes mainnet.

## Record

Append results to the run's manifest and, when decision-relevant, a short
evidence note (claim; data that shaped vs graded it; scope; effect size,
uncertainty, and costs; artifact/commit identities; explicit
non-conclusions) into `docs/strategy_program.md`. Promotion of a winning
config is a five-line note plus a recorded change point through the normal
deploy flow. Negative results are priors, not prohibitions — a refuted idea
may return with a new mechanism or new data, and the provenance record
simply shows what each version saw.
