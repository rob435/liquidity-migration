# Evidence-First Research Governance

This document defines how evidence is judged in this repository. It is policy,
not a strategy thesis and not a substitute for inspecting code, data, or run
artifacts.

The objective is not “zero bias”—that is unattainable. The objective is to make
assumptions, data exposure, incentives, uncertainty, and authority visible enough
that contrary evidence can change the conclusion.

## 1. Separate The Axes

Do not compress methodology, result, deployment, and permission into one label.
Every material conclusion should state:

- **Claim:** the exact proposition being evaluated.
- **Validity:** `valid`, `limited`, or `invalid` for that claim.
- **Study mode:** `exploratory`, `confirmatory`, or `forward_execution`.
- **Result:** `supports`, `contradicts`, or `inconclusive` relative to the
  registered decision rule.
- **Scope:** venue, instruments, period, operating scale, and mechanism to which
  the result applies.
- **Deployment:** `offline`, `shadow`, `paper`, `demo`, or `mainnet`.
- **Authorization:** `unauthorized` or the exact owner authorization reference.

Names such as `candidate`, `paper_ready`, `promoted`, and `frozen` are retained
where code expects them, but they are compatibility/status labels. They are not
evidence and never authorize a deployment.

## 2. Hard Validity Constraints

These constraints are not inherited preferences. They are what make the affected
claim identifiable and reconstructable.

### Causal information

- Record decision time, data availability time, order time, fill window, exit
  activation, and state initialization when those times affect the result.
- Use only information actually available at the decision. Model uncertain
  vendor or processing latency, or run a delayed sensitivity copy.
- Initialize adaptive state when the live system could first know it. Do not
  warm-start exits, highs/lows, cooldowns, or risk memory from future history.

### Population and survivorship

- Use point-in-time membership when historical universe selection is part of the
  claim. Include launches, delistings, renames, migrations, status changes, and
  missing dead instruments to the extent the source supports them.
- A current-universe run may still answer a current-universe diagnostic. It
  cannot support a historical-universe or portability claim.
- State provenance limits honestly. “Full PIT” means full coverage under the
  repository's declared manifest contract; it does not prove facts the source
  never observed.

### Executability and economics

- Performance claims require plausible order timing, fill logic, venue rules,
  fees, spread/slippage, material funding or carry, and capacity at the stated
  scale.
- Model partial fills, rejects, minimum notionals, rate limits, maintenance,
  margin/liquidation, and market impact when they could change the conclusion.
- Logic, data-quality, or entry-agreement studies need not backfill irrelevant
  PnL costs, but they must not be presented as net-performance evidence.

### Accounting and reconstruction

- Preserve data-root identity, code/config identity, tested variants, effective
  sample unit, and the artifacts needed to reproduce the conclusion.
- Strategy-performance work normally needs a trade ledger and equity/accounting
  reconciliation. Feature diagnostics may instead need event rows or a panel;
  artifact requirements follow the claim.
- Resolve strange synchronization, impossible prices, unexplained live drift,
  and ledger imbalance before relying on affected results.

A violation makes the affected claim `invalid`; it does not require deleting a
useful diagnostic artifact. Missing but non-fatal evidence makes the claim
`limited`, not secretly valid.

## 3. Evidence Is Proportional To The Decision

### Exploratory diagnostics

Exploration may use partial data, flexible plots, ad hoc scripts, or many ideas.
Record important data limitations and all materially inspected variants. Its
purpose is hypothesis generation and debugging, not acceptance or deployment.

### Confirmatory research

Before inspecting the affected result, freeze a claim, comparison, data/exposure
boundary, primary decision method, guardrails, multiplicity treatment, and
expected artifacts. Use the strongest feasible controls for the declared claim.
Report effect sizes and uncertainty, not only pass/fail.

### Forward execution evidence

Paper/demo can test signal agreement, order lifecycle, fills, slippage, fees,
funding, and operational reliability. It supports alpha only when the profile and
evaluation clock were frozen prospectively and the sample is large enough for the
registered performance claim.

Forward data is not an uncapped arbiter. Looking, adapting, resetting clocks, or
stopping opportunistically spends it. Keep immutable epochs, record every change
point, and predeclare either a fixed horizon/event count or a valid sequential
stopping rule.

### Mainnet readiness

Research quality and permission are separate. Mainnet requires all of:

- a decision-grade evidence pack for the exact code/config and target venue;
- explicit capital, gross, per-order, leverage, loss, and expiry limits;
- reconciliation, monitoring, kill/recovery paths, and independent liveness;
- separate, narrow owner authorization immediately before enablement.

Safety controls for real money are constraints on ruin and exposure. They are not
required to improve MAR or any alpha metric. Broad repository authority, a green
report, or a notification never satisfies this boundary.

## 4. Choose Validation From The Claim

### Venues

- A venue-specific claim may be tested on that venue and must remain
  venue-specific.
- A portability or shared-microstructure claim needs multiple relevant venues.
- A second venue is usually valuable as a robustness probe, but Bybit and Binance
  share instruments and crypto regimes; agreement is not independent OOS proof.
- Analyze disagreement as heterogeneity, data quality, or mechanism evidence.
  Do not automatically pool it away or call either side an artefact.
- If an experiment contract requires both venues, that requirement remains
  binding for that experiment.

### Time and out-of-sample data

A root containing all available history does not prevent temporal holdouts,
walk-forward evaluation, purging, or embargoes. What matters is exposure: once a
window influences design, it is no longer untouched.

For strategies already mined across the full historical range, prospective
paper/demo epochs may be the cleanest remaining surface. For a genuinely new
claim, a historical window can still be reserved before inspection. Record an
exposure ledger rather than declaring all historical data permanently in-sample
or all forward data permanently pristine.

### Metrics and thresholds

- Choose metrics from the objective and failure modes. Return, drawdown, tail
  loss, turnover, capacity, concentration, calibration, and execution error may
  matter; no metric is universally primary.
- Give thresholds an economic, operational, or statistical rationale. Arbitrary
  constants inherited from an older experiment are not policy.
- Historical artifacts may contain legacy decision presets and labels. They are
  binding only when the active experiment prospectively names the exact rule;
  otherwise treat them as clearly labelled diagnostics.
- Predeclare how many variants, symbols, segments, and metrics will influence
  selection. Address dependence and effective trials; showing only the winner is
  invalid selection evidence.

## 5. Prospective Rule Changes

- Change policy when reasoning or evidence warrants it; do not preserve a bad
  rule for consistency.
- Once confirmatory outcomes are inspected, do not lower or swap that study's
  rule to rescue a result. Amendments after exposure must use a fresh evaluation
  surface and retain the original result.
- A negative result closes only the precise tested claim under its conditions.
  Reopen work when there is new data, a corrected defect, a genuinely different
  mechanism, or a better-identified question. State why the new test is not the
  same mining loop.
- “Closed” and “rejected” entries in the research summary are current evidence
  states and compute priors, not prohibitions.

## 6. Minimum Evidence Card

Every decision-influencing report should answer:

1. What exact claim and action were under consideration?
2. What data was available, what had already been inspected, and what was held
   out?
3. What population and venue scope does the evidence cover?
4. What was the effective independent sample unit and tested-set size?
5. What comparator, decision rule, stopping rule, and guardrails were frozen?
6. Were timing, PIT, fills, costs, capacity, and accounting valid for this claim?
7. What effect size, uncertainty, concentration, and failure modes were observed?
8. Which artifacts and identities reconstruct the result?
9. What changed after preregistration, and does that spend the evaluation data?
10. What conclusion is justified—and what is explicitly not justified?

Use the failure taxonomy in `docs/backtesting_errors_we_never_repeat.md` as a
review aid. Artifact presence is necessary but never substitutes for judgment.
