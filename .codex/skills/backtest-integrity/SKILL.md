---
name: backtest-integrity
description: Assess whether a backtest, research run, strategy or feature change, or result interpretation produces evidence that is real, under the Progressive Evidence Model. Use before designing decision-influencing research, when judging a report, or before an alpha, robustness, candidate, or deployment claim. Apply docs/governance.md and the failure taxonomy in docs/backtesting_errors_we_never_repeat.md; keep the physics, skip the ceremony.
---

# Assess evidence integrity

Read `docs/governance.md` (Progressive Evidence Model) and the relevant
failure modes in `docs/backtesting_errors_we_never_repeat.md`. Treat raw
artifacts, code, and data provenance as evidence; labels and old verdicts
are not authority.

## State the claim

One sentence: the proposition, the decision it informs, venue, population,
period, scale, and which data has already shaped the idea. Everything else
is proportional to that.

## The physics — what makes the number real

- **Causality:** decision, availability, order, fill, exit-activation, and
  state-initialization times; only information available at decision time.
- **Population:** PIT membership when historical universe selection matters;
  archive observations vs current-listing inference stated honestly.
- **Executability:** feasible orders, fills, venue mechanics, capacity, and
  material costs/funding for any performance claim; costs next to gross.
- **Accounting:** positions, cash, fees, and funding reconcile; root, code,
  config, and artifact identities can be found again.
- **Anomalies:** impossible prices, sync gaps, missing rows, or drift get
  explained before the affected output is relied on.

A miss on one of these turns the result into a diagnostic — still useful for
generating the next idea, just not gradeable as evidence. Say which.

## Which lane graded it

Lane-1 output (graded on data that shaped it) is exploratory by definition —
valuable, never confirmatory. Lane-2 output is graded on the rolling run of
days after the config's commit; check the commit actually predates the
scored days, the declared grid reports all cells, and results are era-split.
An untouched historical reserve counts as forward-style evidence once,
recorded in provenance when opened. A second correlated crypto venue is
robustness evidence, not independence.

## Evidence note

Report: claim and decision; data that shaped vs graded it; scope; effect
size, uncertainty, concentration, and costs; artifact/commit identities;
explicit non-conclusions.

Research quality never grants mainnet authority. Real money requires a
separate owner instruction naming the deployment, capital/risk limits,
controls, and expiry under `docs/governance.md`.
