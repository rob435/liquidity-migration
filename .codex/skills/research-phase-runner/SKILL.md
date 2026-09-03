---
name: research-phase-runner
description: Route, execute, and record research work under the Progressive Evidence Model in this quant repository. Use before running, monitoring, or interpreting decision-influencing research. Lane-1 exploration is unlimited on seen data; Lane-2 configs are graded on the rolling run of forward days after their git commit; promotion is a five-line note under docs/research/governance.md.
---

# Research Phase Runner & Evidence Lifecycle

## 1. Purpose
Specify execution procedures, lane routing, provenance requirements, and promotion protocols for quantitative research studies under the Progressive Evidence Model.

---

## 2. Spec Tables

### Research Lane Routing Matrix

| Attribute | Lane 1: Exploration | Lane 2: Graded Record |
| :--- | :--- | :--- |
| **Objective** | Ideation, grid sweeps, anomaly discovery, sensitivity tests. | Production candidacy, forward out-of-sample confirmation. |
| **Data Scope** | Seen historical datasets; unlimited backtests. | Rolling forward days post-registration; unseen holdout. |
| **Registration** | Provenance note in run manifest. | Git commit of immutable config JSON and scoring recipe. |
| **Reporting Rule** | Full grid reporting; era-split tables; gross vs net. | One row per day per config; cumulative forward performance. |
| **Evidentiary Weight**| Hypothesis-generating; non-confirmatory. | Decision-grade confirmatory evidence. |
| **Promotion Path** | Refine hypothesis $\rightarrow$ commit to Lane 2. | 5-line promotion note $\rightarrow$ operational staged deploy. |

### Research Run Manifest Schema

| Field | Type | Description | Invariant |
| :--- | :--- | :--- | :--- |
| `run_id` | String | Unique execution identifier (`YYYYMMDD-slug`). | Immutable once written. |
| `commit_sha` | String | Git commit of the codebase at execution time. | Working tree must be clean or diff recorded. |
| `config_sha256`| String | SHA-256 digest of the strategy configuration. | Matches committed JSON. |
| `data_root` | Path | Absolute path to Point-in-Time input dataset. | Must include manifest hash. |
| `era_split` | Object | Metrics broken down by calendar year / regime. | Required; pooled metrics alone are invalid. |
| `net_metrics` | Object | Sharpe, CAGR, maxDD, and fees under realistic costs. | Must sit directly next to gross metrics. |

### Five-Line Promotion Note Schema

| Line | Field | Specification | Example |
| :---: | :--- | :--- | :--- |
| **1** | `Candidate` | Exact rule and config identifier. | `Candidate: lane2_carry_hold_v7.json (SHA-256: 4ac21e95...)` |
| **2** | `Evidence Boundary`| Forward date window postdating commit. | `Graded Window: 2026-06-01 to 2026-08-31 (92 forward days)` |
| **3** | `Economic Return` | Net annualized return, Sharpe, fee drag. | `Economics: +18.4 bp/day net, Sharpe 1.42, MaxDD -3.8%` |
| **4** | `Predecessor` | Replaced configuration or active control. | `Replaces: lane2_carry_hold_v6.json (deployed 2026-05-15)` |
| **5** | `Action Point` | Operational deploy target and change point. | `Promotion: staged deploy on 2026-09-01; recorded in CHANGELOG.md` |

---

## 3. Invariants

- **Must Never Suppress Negative Results**: Negative results are active priors recorded in `docs/research/research_findings.md`; they *must never* be hidden or deleted.
- **Must Report Full Parameter Grids**: Reporting only the winning parameter cell is cherry-picking; every cell in the declared search space *must* be documented.
- **Commit Must Predate Graded Forward Days**: For Lane 2, the commit timestamp *must strictly precede* the dates evaluated; retroactively applied forward tests are invalid.
- **Research Does Not Authorize Orders**: Executing research backtests *must never* trigger external venue orders or arm real capital.

---

## 4. Operational Recipes

### Run Quantitative Study via Research Lab CLI
```bash
# Dump PIT inputs for study window
python -m liquidity_migration.research.lab.cli dump \
  --data-root ~/SHARED_DATA/bybit_full_pit \
  --start 2024-01-01 --end 2025-01-01

# Build study panel and run backtest
python -m liquidity_migration.research.lab.cli panel \
  --data-root ~/SHARED_DATA/bybit_full_pit \
  --config configs/lane2_carry_hold_v7.json \
  --out reports/lab/carry_v7_study
```

### Inspect Negative Priors Before Designing Studies
```bash
# Search research findings for previous studies on a mechanism
rg -i "funding rate arb|take-profit|trailing stop" docs/research/research_findings.md
```
