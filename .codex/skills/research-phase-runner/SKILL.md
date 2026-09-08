---
name: research-phase-runner
description: Execute and record decision-influencing research with causal inputs, reconstructable accounting, source identities, cost sensitivity, era splits and honest selection-data boundaries.
---

# Research execution

## Purpose

Run research and record what its inputs and mechanics establish under `docs/research/governance.md`.

## Spec Tables

| Work | Contract |
| --- | --- |
| Exploration | Historical hypotheses and grids are permitted; report all cells |
| Comparison | Match population, dates and costs; expose different position paths |
| Independent evaluation | State exactly which data did not shape the rule |
| Cost model | Stored account fee snapshot or explicitly named scenario; funding separate |
| Evidence note | Claim, data, scope, economics, identities, limitations |
| Runtime | No forward-ledger scheduler or post-commit grading job |
| Deployment | Operator authority and current host configuration control execution |

## Invariants

- Must read current governance and relevant source before running a study.
- Must preserve raw inputs and negative results.
- Must report gross beside net, every cell and calendar eras.
- Must never treat an exploratory result as an independent test or arming authority.

## Operational Recipes

```sh
scripts/ops.sh research-refresh --help
scripts/ops.sh equity --help
```
