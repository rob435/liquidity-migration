---
name: research-report
description: Read, validate, compare, and label research or backtest reports and their raw artifacts in this quant repository. Use when extracting metrics, interpreting a run, comparing controls or venues, checking OOS and split claims, or deciding what conclusion the evidence supports. Apply the evidence card and claim-scoped validity policy in docs/governance.md rather than a fixed metric checklist or historical promotion gate.
---

# Interpret research reports

Read `docs/governance.md`, then inspect the report and its referenced raw
artifacts directly. A helper summary or attractive chart is not a substitute for
the ledger, event rows, config, or data identity behind the claim.

## Establish context

- Identify the exact claim, intended decision, study mode, and preregistration.
- Locate Markdown/JSON summaries, ledgers/event rows, equity/accounting outputs,
  configs, receipts, and logs under the run root.
- Record venue, population, window, scale, data exposure, and every variant that
  influenced selection.
- Check that compared runs use compatible windows, populations, capital, costs,
  leverage, and metric definitions.

## Validate before interpreting

- Recompute or cross-check headline metrics from raw artifacts where practical.
- Inspect run labels and data-quality flags, but verify their underlying facts.
- Check causality, PIT provenance, fills/costs/capacity, accounting, sample unit,
  multiplicity, and OOS exposure as relevant to the claim.
- Investigate missing cells, non-finite metrics, duplicate rows, unexplained
  synchronization, and report/body disagreement.
- Treat incomplete funding or PIT as a scoped limitation; do not rely on root
  names or stale status docs to infer coverage.

## Choose metrics from the question

For performance work, usually report return, drawdown/tail loss, turnover/cost,
trade or event count, concentration, relevant risk-adjusted measures, and
uncertainty. For execution work, emphasize agreement, fills, slippage, latency,
fees/funding, misses, lifecycle, and reconciliation. Do not require irrelevant
metrics merely because an older template listed them.

## Write the conclusion

Produce the evidence card from `docs/governance.md`:

- claim;
- validity: valid, limited, or invalid;
- study mode and result;
- scope and non-generalizable boundaries;
- deployment and authorization state;
- effect sizes, uncertainty, concentration, and material caveats;
- artifact/identity references;
- justified action and explicit non-conclusions.

Report compatibility labels exactly as emitted, but do not treat `candidate`,
`paper_ready`, `promoted`, or a within-report gate as real-money proof or as a
replacement for the evidence card. Preserve honest negative and inconclusive
results.
