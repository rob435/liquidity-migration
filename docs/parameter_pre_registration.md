# Prospective Experiment Registration

Registration protects confirmatory evidence from post-result story changes. It
does not ban exploration and it does not make a poorly designed rule scientific.
See `docs/governance.md` for the governing evidence policy.

## When To Register

Register before inspecting the affected outcomes when a run may:

- accept, reject, rank, or retain a strategy/mechanism;
- change a deployed, shadow, paper, or demo profile;
- choose parameters, features, exits, sizing, costs, or fill assumptions for a
  later claim;
- spend a held-out or forward evaluation surface.

Registration is optional for debugging, infrastructure checks, equivalence
tests, data exploration, and hypothesis generation, provided their outputs are
labelled exploratory and are not later presented as untouched confirmation.

## Minimum Contract

Record:

- Experiment ID, owner, date, and study mode.
- Exact claim, intended decision, and plausible failure mechanism.
- Data roots, venue/population scope, time boundary, and prior exposure status.
- Effective independent sample unit and planned sample/event horizon.
- Control/comparator and every variant allowed to influence selection.
- Primary metric or utility rule, guardrails, thresholds with rationale, and
  `supports` / `contradicts` / `inconclusive` outcomes.
- Multiplicity/dependence treatment and any sequential stopping rule.
- Material PIT, timing, fill, cost, funding, capacity, and accounting assumptions.
- Exact command/config/code identity and expected artifact/receipt paths.
- Conditions under which the precise claim may later be revisited.

Choose venues and metrics from the claim. Two venues are required for a
portability claim or when the experiment contract names them, not by universal
policy. A single-venue result stays single-venue.

## After The Run

Append, without rewriting the contract:

- artifact identities and paths;
- all completed, failed, and aborted variants;
- effect sizes, uncertainty, concentration, and material diagnostics;
- deviations and whether they spend the evaluation data;
- validity, result, scope, and justified next action;
- code/config/data hashes needed for reconstruction.

## Amendments

- Amend freely before the affected outcome is inspected; retain the old text and
  explain why the design changed.
- After exposure, the original result and rule stay visible. A revised rule needs
  a fresh evaluation surface or is exploratory on the spent data.
- Do not convert an inconclusive or failed claim into a different success after
  seeing results.
- A failed test closes only the registered claim under its conditions. New data,
  corrected implementation, or a genuinely different mechanism may justify a
  new contract.

Use `docs/preregistration/_template.md` for the compact receipt. Git history is a
backup, not the sole artifact registry; decision-grade runs should also leave a
content-addressed receipt with the run outputs.
