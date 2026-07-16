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
- Prefer reversible changes and focused validation. Do not turn a historical
  implementation detail into permanent policy.

## Research And Evidence

- Follow `docs/governance.md` for evidence grades, claim scoping, validation,
  forward-data discipline, and authorization boundaries.
- Use `docs/backtesting_errors_we_never_repeat.md` as a failure-mode reference,
  not as borrowed authority.
- PIT membership is required when the claim depends on historical universe
  selection. Causal availability, material costs/funding, realistic fills and
  capacity, and reconstructable accounting apply whenever they are relevant to
  the claim.
- Neither two-venue agreement nor forward-only validation is a universal rule.
  Choose venues, holdouts, metrics, and thresholds from the declared claim and
  record the rationale before confirmatory results are inspected.
- Do not change a registered decision rule after seeing the affected result.
  Revisions are prospective; previously viewed data stays spent.

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
