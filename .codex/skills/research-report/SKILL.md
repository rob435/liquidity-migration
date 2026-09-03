---
name: research-report
description: Read, validate, compare, and label research or backtest reports and their raw artifacts in this quant repository. Use when extracting metrics, interpreting a run, comparing controls or venues, checking OOS and split claims, or deciding what conclusion the evidence supports. Apply the evidence rules in docs/research/governance.md: state which data shaped vs graded the result, keep the evidence physics, and write the short evidence note rather than a fixed metric checklist or historical promotion gate.
---

# Research Report Validation & Interpretation

## 1. Purpose
Specify validation checks, metric extraction standards, and evidence card formatting required to review, verify, and interpret quantitative research reports and raw run artifacts.

---

## 2. Spec Tables

### Report Verification Checklist

| Audit Area | Inspection Target | Verification Rule | Failure Response |
| :--- | :--- | :--- | :--- |
| **Headline Metrics** | `summary.json`, `report.md` | Recompute Sharpe, CAGR, and maxDD directly from `daily_equity.csv`. | Flag discrepancy; treat report as unverified. |
| **Causal Clocks** | `trades.parquet` | Confirm `decision_ts_ms` $\ge$ `available_ts_ms` for every trade row. | Reject run as look-ahead contaminated. |
| **PIT Membership** | Universe manifest | Confirm symbol traded was actively listed in archive at decision time. | Restrict claim scope to survivorship subset. |
| **Cost Realism** | Fee and funding ledgers | Verify exchange taker/maker fee schedule and actual funding payments. | Subtract realistic fees; recalculate net metrics. |
| **Regime Stability** | Per-era breakdown | Evaluate return consistency across bull, bear, and chop eras. | Reject claim if all alpha concentrates in one window. |

### Metric Reporting Standards by Study Type

| Study Type | Primary Required Metrics | Secondary Diagnostics | Prohibited / Misleading Practices |
| :--- | :--- | :--- | :--- |
| **Alpha / Directional** | Net Sharpe, Sortino, maxDD, CAGR, annual fee drag, win rate. | Trade count, average hold duration, profit factor. | Annualized Sharpe without fee friction; pooled return hiding down-years. |
| **Carry / Funding** | Basis yield bp/day, funding capture efficiency, liquidation risk. | Turnover, rebalance frequency, skewness. | Reporting gross funding rate without hedge execution costs. |
| **Execution Quality** | Arrival shortfall (bps), fill rate, maker share %, order latency (p50/p99). | Cancel-to-fill ratio, queue position, venue slippage. | Quoting mid-price fills without modeling bid-ask spread crossing. |

### Evidence Card Schema

| Field | Content Specification | Invariant |
| :--- | :--- | :--- |
| **Claim** | Precise technical hypothesis tested and the decision it informs. | Must state concrete operational choice. |
| **Validity** | Exactly one of: `VALID`, `LIMITED`, or `INVALID`. | Any physical defect forces `LIMITED` or `INVALID`. |
| **Data Provenance** | Explicit separation of data that shaped the rule vs data that graded it. | Must name datasets and date cuts. |
| **Scope & Boundaries**| Venues, asset class, liquidity tier, and non-generalizable limits. | Never claim cross-venue transfer without proof. |
| **Economic Magnitude**| Net return, Sharpe ratio, max drawdown, fee drag, and uncertainty. | Net figures must accompany gross figures. |
| **Artifact Identities**| Git commit SHA, config SHA-256 digest, raw output paths. | Must be reproducible from repository. |
| **Non-Conclusions** | Explicit declaration of what this study *does not* prove. | Mandatory to prevent unwarranted generalization. |

---

## 3. Invariants

- **Must Never Trust Summary Without Ledger Proof**: A chart or markdown table is not proof; headline figures *must* reconcile with underlying trade rows and equity series.
- **Must Separate Shaped vs Graded Data**: Always explicitly disclose if an idea was discovered on the same dataset used for evaluation.
- **Must Preserve Inconclusive Findings**: Reports that show zero edge or inconclusive statistical power *must* be filed as negative priors, not discarded.
- **Labels Carry No Authority**: Labels like `promoted`, `candidate`, or `approved` in older reports carry no weight; evaluate strictly by current physical evidence.

---

## 4. Operational Recipes

### Verify Report Summary from Underlying Daily Equity
```bash
# Recompute Sharpe ratio and Max Drawdown directly from daily equity CSV
python -c "
import pandas as pd, numpy as np
df = pd.read_csv('reports/lab/latest_run/daily_equity.csv')
returns = df['equity'].pct_change().dropna()
sharpe = (returns.mean() / returns.std()) * np.sqrt(365) if returns.std() > 0 else 0
cummax = df['equity'].cummax()
drawdown = (df['equity'] - cummax) / cummax
max_dd = drawdown.min()
print(f'Computed Net Sharpe: {sharpe:.2f}')
print(f'Computed Max Drawdown: {max_dd * 100:.2f}%')
"
```

### Inspect Trade Ledger Execution Timestamps
```bash
# Verify no future timestamp leakage in trades parquet
python -c "
import pandas as pd
df = pd.read_parquet('reports/lab/latest_run/trades.parquet')
violations = (df['decision_ts_ms'] > df['fill_ts_ms']).sum()
assert violations == 0, f'{violations} causality violations found!'
print('Execution timestamps: VERIFIED CAUSAL')
"
```
