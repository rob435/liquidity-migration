# Research evidence

## Purpose

Define how research results support decisions without treating a research report as account or execution authority.

## Spec Tables

| Evidence | Required content | Limit |
| --- | --- | --- |
| Causality | Input availability at or before the decision | Future membership, candle closes, and revised data invalidate the result |
| Executability | Fees, funding, spread, capacity and execution assumptions | Gross returns alone do not establish an executable edge |
| Accounting | Reconstructable cash, positions, fees and funding | Unreconciled P&L is diagnostic |
| Provenance | Source/config identities, input paths, shaped versus evaluated data | Reusing selection data is exploratory |
| Reporting | Every searched cell, calendar eras, gross beside net, uncertainty and concentration | A selected winner or pooled total alone is insufficient |
| Research operations | Explicit historical comparisons and recorded live outcomes | No rolling forward ledger, post-commit eligibility job or daily grading requirement |
| Operational decision | Claim, config commit, evidence window, decision and change point | The operator owns deployment and capital decisions |
| Funded permission | Host `REAL_MONEY=true` | Research and commits cannot set this switch |

| Empirical finding check | Existing standard |
| --- | --- |
| Statistical threshold | Two-sided `t >= 2.5`; prospective evidence boundary 2026-07-31 |
| Parameter smoothness | Neighbouring grid values have deltas of the same sign |
| Lag stability | One-day execution lag still beats the placebo |
| Persistence | Two consecutive qualifying timestamps still beat the placebo |
| Direction | Inverting the condition does not beat the placebo |
| Concentration | Top three trades contribute at most 50% of net gain |
| Placebo | At most 5% of matched random draws score as well |

| Evidence note | Content |
| --- | --- |
| Claim | Hypothesis and operational decision |
| Data | Which data shaped the rule and which evaluated it |
| Scope | Venue, population, dates and capital scale |
| Economics | Gross, costs, net, uncertainty and drawdown |
| Identities | Config/source commits and raw artifacts |
| Limits | What the result cannot establish |

## Invariants

- Must distinguish a seen-data comparison from an independent test.
- Must retain negative and inconclusive results and the raw research data.
- Must use causal inputs and state every execution approximation.
- Must never infer account balances, realized P&L or deployment from a backtest.
- Must never arm funded trading from a research command.

## Operational Recipes

```sh
scripts/ops.sh research-refresh --help
scripts/ops.sh equity --help
scripts/ops.sh status
```
