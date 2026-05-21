---
name: research-report
description: "Read, interpret, and label research and backtest reports in this quant repo. Use when reading a volume_event_research_report.md or strategy_tribunal_report.md, extracting run metrics like return, drawdown, OOS and split stability, comparing runs, or assigning a run label. Pairs with the liqmig-research MCP tools parse_report, list_reports, and audit_run_artifacts."
---

# Research reports

Reports live under `<DATA_ROOT>/reports/...`. The two main kinds:

- `volume_event_research_report.md` — a `volume-events` strategy run.
- `strategy_tribunal_report.md` — the adversarial promotion court output.

## Fast path — the `liqmig-research` MCP tools

- `list_reports {root}` — find report files under a data root, newest first.
- `parse_report {path}` — extract metrics as JSON (best-effort line capture).
- `audit_run_artifacts {path}` — check a run dir for required artifacts and
  return an artifact-completeness verdict.

Always sanity-check tool output against the report body — the parser captures
raw lines and can mislabel unusual formatting.

## Metrics every strategy report should carry

- trades (and candidate events)
- total return
- max drawdown
- max no-new-high stretch (days)
- worst 90d return
- worst split return (a.k.a. minimum split)
- average split Sharpe-like
- OOS return
- pre-registered train / validation / oos window returns
- promotion gate: pass / fail

A report missing the trade ledger, config/data identity, split report, or run
record is "a screenshot", not evidence (error #23).

## Promotion gate vs. model court

- `promotion gate: pass` is a within-report check — necessary, not sufficient.
- `strategy-tribunal` (the model court) is the adversarial audit: artifact
  checks, comparison-family filtering, recomputed-vs-reported path consistency,
  explicit pre-registered windows, block-bootstrap left tail, random-sign /
  inverted-edge / shuffled-symbol / shuffled-time / shuffled-event negative
  controls, cost/funding/slippage stress, monthly regime, symbol concentration,
  and same-hour entry crowding.
- A `WATCH` verdict (not `PASS`) means a real caveat remains — usually partial
  funding coverage, an unattached live-vs-backtest drift check, or a stress
  family that still breaches the drawdown gate. Treat `WATCH` as research
  evidence, never real-money proof.

## Assigning a run label

Every report gets exactly one of: `invalid`, `exploratory`,
`biased_benchmark`, `candidate`, `paper_ready` — see the **backtest-integrity**
skill for definitions. Default to the *lowest* label the evidence supports.
Funding-missing data, OOS-window reuse, or partial PIT each cap the label
below `candidate`.

## Writing a report

Include: config/param hash, data-root identity, all metrics above, a trade
ledger reference, equity curve, a split table, the run label, and a
research-log entry. State known gaps explicitly (e.g. "funding-missing,
fee/slippage stressed"). Honest negative results are valuable — record what
would make the edge disappear, not just the headline return.
