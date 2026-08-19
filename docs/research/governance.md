# Progressive Evidence Model

Explore every day, ship improvements when they are ready, and let evidence
accumulate as a rolling record rather than through one-shot ceremonies. The
objective is not "zero bias" — that is unattainable. It is that assumptions,
data exposure, and uncertainty stay visible enough that contrary evidence can
change our minds quickly.

## 1. Two lanes, always open

**Lane 1 — explore, continuously.** Any data we have already seen is open
for unlimited exploration: plots, prototypes, threshold sweeps, diagnostics,
ad-hoc scripts, many ideas in parallel. No registration, no ceremony. Label
the output exploratory and note which data it touched. This is where most of
the work lives, every day.

**Lane 2 — rolling forward scoring.** When a prototype looks good, commit
its config. From that moment, every new day of data it could not have seen
becomes one more row of evidence — the git commit date is the entire
registration. There is no unveiling moment and no waiting for a distant date.

The habit that makes Lane 2 work: grade a rule on data it did not shape. A
rule graded on the data that suggested it always passes and teaches nothing.
Historical reserves (windows nobody has opened) are an optional accelerant
when one exists, not a requirement.

## 2. What makes a number real

Drop any of these and the number stops meaning anything:

- **Causality.** A simulated decision uses only information available at
  decision time: PIT membership, causal features, honest latency, adaptive
  state initialized when the live system could first know it.
- **Executability.** A performance number includes plausible fills, fees,
  spread, funding, and capacity at the stated scale. Costs are where most
  paper edges die; a gross number is a diagnostic, not a result.
- **Accounting.** Positions, cash, fees, and funding reconcile, and the
  inputs/code/config that produced a result can be identified again.
- **Provenance.** Keep a running note of which data has shaped which ideas.
  It is what lets "this config never saw these days" be true. Deleting the
  record of what was seen makes all future evidence fake.

A miss on one of these makes the affected number a diagnostic instead of a
result. Diagnostics generate the next idea; they do not get graded as
evidence.

### The significance bar is **t ≥ 2.5**, set 2026-07-31

This is the single number a screen or a registration is measured against, and
this document is its authority. Code constants in research screens derive
from here, not the other way round.

**What changed.** The program previously used a family-wise Bonferroni threshold
derived from its own search history — t ≈ 3.25 over ~44 mechanisms, rising to
≈ 3.58 over a 144-cell tuning grid. That is now replaced by a fixed **t ≥ 2.5**,
by owner decision on 2026-07-31.

**What it costs, stated plainly.** t 2.5 two-sided is p ≈ 0.012. Across the
~45 mechanisms this program has screened that is roughly **one false positive
expected**, against roughly one in twenty at 3.25. The bar no longer controls
family-wise error; it is a fixed evidence threshold and it will admit results
that would previously have been rejected. Two things carry the weight the
threshold used to:

- **A plateau, not a cell.** Report the neighbouring parameter values. One cell
  at 2.5 with worse neighbours is a spike and should be treated as noise
  regardless of the bar.
- **A placebo that fails.** An inverted or size-matched-random arm that
  *also* looks good means the result is an artifact of the construction. This
  catches what a higher threshold used to catch, and catches it for the right
  reason.

**It is prospective.** Verdicts recorded before 2026-07-31 stand as written;
`docs/research/archive/` entries quoting 3.25 or 3.58 are accurate history and are not
restated. A pre-2026-07-31 result that sits between 2.5 and 3.25 is not thereby
promoted — it is eligible to be re-examined, and the re-examination is a new
registration.

## 3. Promotion is a note, not a treatise — and demotion is too

When a config's rolling record earns a change to the live system, the
promotion record is five lines: claim, config commit, forward record,
decision, date. Ship it through the normal deploy flow and record the change
point.

Demotion is the mirror: the same five lines, a sleeve toggle, and a recorded
change point. There is no standing per-sleeve kill-criteria checker — the
module, its script and its two registrations were removed — so the exit rule
lives with the config that declares it. Writing that rule down before the
outcome is known is what keeps the rolling record honest in both directions.

Live runtime is **continuous with recorded change points**, not frozen. Each
change is recorded so the rolling record stays interpretable across it (a
config's evidence is the run of days between its commit and its replacement).
Fix bugs immediately.

Negative results are priors, not prohibitions. A refuted idea can return with
a new mechanism, new data, or a corrected defect; the record shows what its
predecessor saw. Rules in this document included: change them when reasoning
warrants, prospectively.

## 4. Reporting that keeps pace

A decision-influencing result travels with a short evidence note:

1. Claim, and the decision it informs.
2. What data shaped the idea; what data graded it.
3. Scope: venue, population, period, scale.
4. Effect size and uncertainty (not only pass/fail), including costs.
5. Where the artifacts and config commit live.
6. What this does not show.

Grids report all cells, results split by era halves (a pooled number that
hides decay is a wrong answer), and forgone upside is reported next to
avoided cost. `docs/research/backtesting_errors_we_never_repeat.md` remains the
failure-mode reference — lessons, not law.

## 5. Mechanics — how to actually commit work into the record

**Lane 1 needs nothing.** Explore freely on already-seen data. Label the output
exploratory and note which data it touched; that provenance note is what keeps
Lane 2 honest later.

**Lane 2 — the commit is the registration.** To move a prototype into the
rolling forward record:

1. Put its exact config (rule, parameters, feature definitions, cost model) in
   the repository and commit. The commit hash and date *are* the registration —
   no separate contract document is needed.
2. Declare the scoring recipe in the config or its manifest: metric,
   comparator/baseline, and the grid if there is one (all cells report).
3. Let the scorer append one row per config per new day. The config's evidence
   is the run of days after its commit; editing the config starts a new run
   under the new commit.

**The promotion note**, recorded alongside the deploy change point:

```text
Claim:
Config commit:
Forward record (days, net delta vs baseline, tail behavior):
Decision:
Date:
```

**Optional historical reserves.** If an untouched historical window exists for a
genuinely new idea, it can be opened once for instant forward-style evidence —
the provenance note records that it is now seen. Reserves are an accelerant,
never a prerequisite.

## 6. Real money is a separate door

Arming real money is one switch set by the owner's own hand: `REAL_MONEY=true`
in the host credential file beside the live key. No rolling record, green
report, or repository authority opens that door — a git commit can never arm.
Demo is the default operating surface, and capital-preservation
controls are never traded away for velocity.
