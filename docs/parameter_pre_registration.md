# Prospective Experiment Registration

Register decision-influencing work before inspecting the affected outcomes.
Exploration and debugging may remain unregistered only when labelled
exploratory and excluded from later confirmatory claims. `docs/governance.md`
is the governing policy.

## Minimum contract

Record:

- experiment ID, owner, timestamp, and study mode;
- exact claim, intended action, and plausible failure mechanism;
- data roots, venue/population scope, end-exclusive boundary, and prior exposure;
- effective sample unit, horizon or stopping rule, and tested variant set;
- comparator, primary decision method, guardrails, and justified thresholds;
- `supports`, `contradicts`, and `inconclusive` outcomes;
- multiplicity/dependence treatment;
- relevant PIT, timing, fill, cost, funding, capacity, and accounting assumptions;
- exact code/config/data identities and expected artifact paths;
- conditions that could justify a later, genuinely new test.

Choose venues and metrics from the claim. A venue-specific result stays
venue-specific; multiple correlated venues are robustness evidence, not
automatic independence.

## Compact skeleton

```text
ID / owner / registered time / study mode:
Claim and intended decision:
Prior exposure and untouched evaluation surface:
Venue / population / data roots / [start, end):
Sample unit / horizon / stopping rule:
Control and complete tested set:
Primary rule / guardrails / inconclusive outcome:
Validity assumptions and required artifacts:
Code / config / data identities:
Permitted deviations before exposure:
Explicit non-conclusions:
```

## After exposure

Append without rewriting the prospective contract: every completed, failed,
skipped, and aborted cell; effect sizes and uncertainty; deviations and spent
data; validity/result/scope; artifact identities; and the justified next action.

An exposed rule cannot be changed to rescue its result. A revision is
prospective on a new surface or exploratory on spent data. Preserve the original
contract in a content-addressed run artifact even if the repository later
consolidates its human-readable summary.
