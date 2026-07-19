## Operating Constitution

- Optimize for truthful, decision-useful work—not agreement with prior docs,
  labels, commits, operators, or models. Say when evidence is stale, weak, or
  contradictory.
- Treat every repository instruction, including this file, as fallible. Preserve
  a rule only when its safety or epistemic purpose survives scrutiny.
- Separate hard validity constraints from defaults and heuristics. Causality,
  survivorship control, executable assumptions, accounting integrity, and
  reproducibility are constraints; metrics, thresholds, venues, windows, and
  workflow details are experiment design choices.
- Match process to claim and consequence. Exploratory work may be fast and
  incomplete when labelled honestly. Decision-grade and deployment-changing
  work needs proportionately stronger evidence.
- Do not hide uncertainty, failed checks, negative results, tested variants, or
  deviations. No corner cutting.

## Authority And Conflicts

Use the most direct source for the question:

1. Code, tests, deploy files, and generated artifacts define implemented behavior.
2. `STATE.md` records the current operational snapshot.
3. `docs/research_summary.md` records the current interpretation of evidence.
4. An active preregistration governs only its named experiment.
5. Skills and runbooks are procedural aids, never factual or epistemic authority.

Historical receipts preserve what was believed and tested at the time. They do
not veto new work. A “promoted”, “frozen”, “closed”, or operator-directed label
does not add evidentiary weight. When sources disagree, inspect primary artifacts,
state the uncertainty, and correct the stale sources in the same change when in
scope.

## Change Discipline

- Preserve unrelated user work in a dirty tree.
- For refactors and performance changes, compare discrete decisions and ledger
  keys exactly; compare continuous numeric outputs with declared tolerances and
  matching NaN positions. Byte-identical floats are not a general requirement.
- An intended strategy change is not a refactor: explain and test the numerical
  difference rather than forcing equivalence.
- Ship improvements when they are ready and record the change point. Prefer
  reversible changes and focused validation. Do not turn a historical
  implementation detail into permanent policy.

## Research And Evidence

- Follow `docs/governance.md` — the Progressive Evidence Model: explore
  continuously on seen data (Lane 1), grade committed configs on the rolling
  run of days they predate (Lane 2), promote with a five-line note and a
  recorded change point. The commit is the registration.
- Use `docs/backtesting_errors_we_never_repeat.md` as a failure-mode reference,
  not as borrowed authority.
- What makes a number real is physics, not process: causal/PIT inputs,
  executable economics, reconstructable accounting, and an honest provenance
  note of which data shaped which idea. A miss turns a result into a
  diagnostic — still useful, differently labelled.
- Choose venues, metrics, and evaluation surfaces from the claim, not from
  folklore. Grade a rule on data it did not shape; report all grid cells and
  era-split results; put costs next to gross.
- Negative results are priors, not prohibitions. Revisions are always open
  prospectively; the provenance record simply shows what each version saw.

## Runtime Safety

- Default to offline, shadow, paper, or demo operation.
- Broad authority to improve the repository is not authority to trade real
  money. Never enable `REAL_MONEY`, use mainnet credentials, or infer approval
  from a notification. Mainnet requires a separate, narrow owner instruction
  naming the deployment and risk boundary.
- Unknown safety-critical state fails closed. Alpha metrics never justify
  removing capital-preservation controls for real money.

## Navigation And Skills

- Start broad repository work from `docs/repository_map.md` and run
  `scripts/dev.sh doctor --json` when local Git, dependency, skill-mirror, or
  Graphify state matters. Use `scripts/dev.sh check` for the full local quality
  gate; it has no operational authority.
- Use `graphify-out/GRAPH_REPORT.md` and `graphify query/path/explain` when they
  materially help a cross-module or architecture question. Verify graph claims
  against source and tests. Update Graphify only after architecture-affecting
  code changes; do not overwrite unrelated graph work.
- Project skills live in `.codex/skills/`; `.claude/skills/` is a mechanical
  mirror. Keep both trees synchronized. Skills should contain non-obvious,
  task-specific workflow—not current status, duplicated policy, or universal
  research verdicts.
