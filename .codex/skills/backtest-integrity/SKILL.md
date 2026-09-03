---
name: backtest-integrity
description: Assess whether a backtest, research run, strategy or feature change, or result interpretation produces evidence that is real, under the Progressive Evidence Model. Use before designing decision-influencing research, when judging a report, or before an alpha, robustness, candidate, or deployment claim. Apply docs/research/governance.md and the failure taxonomy in docs/research/backtesting_errors_we_never_repeat.md; keep the physics, skip the ceremony.
---

# Backtest & Evidence Integrity

## 1. Purpose
Specify validation criteria, evidence grading mechanics, and causality verification required to assess backtests, research runs, and alpha claims under the Progressive Evidence Model.

---

## 2. Spec Tables

### Physical Verification Dimensions (The Physics of Evidence)

| Dimension | Required Invariant | Diagnostic Failure Mode | Evidentiary Consequence |
| :--- | :--- | :--- | :--- |
| **Causality** | Input availability timestamp $\le$ decision timestamp. | Forward leakage; future kline/funding data available at decision. | Invalidates run; relabeled as diagnostic only. |
| **Population** | Strict Point-in-Time (PIT) universe membership. | Survivorship bias; current-listing extrapolation into history. | Restricts validity strictly to declared surviving subset. |
| **Executability** | Realistic order fills, spread friction, slippage, and funding. | Zero-cost fills, impossible capacity, missing liquidation risk. | Unexecutable; performance claims discarded. |
| **Accounting** | Continuous cash, position, fee, and funding reconciliation. | Phantom equity jumps, double-counted funding, margin drift. | Accounting failure; requires complete recalculation. |
| **Data Provenance** | Immutable artifact hashes, commit IDs, and root paths recorded. | Unreproducible results, untracked manual CSV edits. | Non-citable diagnostic. |

### Progressive Evidence Model Lanes

| Lane | Purpose | Permitted Data | Registration Gate | Evidentiary Weight |
| :--- | :--- | :--- | :--- | :--- |
| **Lane 1: Exploration** | Hypothesis generation, parameter sweeps, prototypes. | Seen historical data. | Commit with provenance note; no formal filing. | Exploratory only; never confirmatory. |
| **Lane 2: Graded Record** | Production candidates, forward tracking. | Rolling forward days post-commit. | Exact config commit; forward days must strictly postdate commit. | Confirmatory decision-grade evidence. |

### Six-Item Evidence Note Schema

| Item | Key | Required Content |
| :---: | :--- | :--- |
| **1** | `Claim & Decision` | Proposition tested and specific operational choice it informs. |
| **2** | `Data Provenance` | Data that shaped the rule vs data that graded the rule. |
| **3** | `Scope & Population` | Venues, instruments, date ranges, and non-generalizable limits. |
| **4** | `Economic Magnitude` | Effect size, gross vs net costs, uncertainty, and drawdowns. |
| **5** | `Identities` | Exact Git commit hash, config JSON SHA-256, artifact URIs. |
| **6** | `Non-Conclusions` | Explicit statement of what the result does *not* prove. |

---

## 3. Invariants

- **Must Never Conflate Lanes**: Lane-1 exploratory runs on seen data *must never* be cited as confirmatory proof for production deployment.
- **Must Place Costs Next to Gross**: Every performance metric *must* report gross return alongside net return inclusive of maker/taker fees and funding rates.
- **Must Split by Era**: All long-term backtests *must* report metrics segmented by regime/era (e.g. per-calendar-year); never rely on pooled metrics that mask decay.
- **Must Treat Research as Non-Authorizing**: Research validation *must never* be treated as authorization to trade real money; funded trading requires the physical host arming switch (`REAL_MONEY=true`).

---

## 4. Operational Recipes

### Verify Backtest Inputs & PIT Integrity
```bash
# Verify PIT manifest coverage for targeted date window (end is exclusive)
python -m liquidity_migration --data-root ~/SHARED_DATA/bybit_full_pit archive-manifest \
  --start 2024-01-01 --end 2025-01-01

# Inspect decision and availability timestamps in generated backtest ledgers
python -c "
import pandas as pd
df = pd.read_parquet('reports/lab/latest_run/trades.parquet')
assert (df['available_ts_ms'] <= df['decision_ts_ms']).all(), 'Causality violation detected!'
print('Causality check: PASSED')
"
```

### Check Era Breakdown & Net Costs
```bash
# Evaluate per-era returns and fee drag from backtest output
python -c "
import json
with open('reports/lab/latest_run/summary.json') as f:
    d = json.load(f)
print('Net Sharpe:', d.get('sharpe_net'), 'Gross Sharpe:', d.get('sharpe_gross'))
print('Era Breakdown:', json.dumps(d.get('eras', {}), indent=2))
"
```
