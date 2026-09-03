# Progressive Evidence Model & Governance

Standards for hypothesis testing, evidence grading, model promotion, and risk governance.

---

## 1. Dual-Lane Research Architecture

| Research Lane | Scope & Purpose | Data Access | Registration Requirement | Scoring Rule |
| :--- | :--- | :--- | :--- | :--- |
| **Lane 1: Exploration** | Fast hypothesis testing, parameter sweeps, diagnostics | Any historical seen data | None (label output as exploratory) | Exploratory only; cannot grade or promote |
| **Lane 2: Rolling Forward** | Unbiased performance grading of committed prototypes | Data post-dating the commit date | Git commit hash + date *is* the registration | Graded on forward data it did not shape |

* **Invariant**: Never grade a rule on the data that suggested it.

---

## 2. Core Evidence Criteria (What Makes a Number Real)

A result must satisfy all four pillars, or it is demoted to an informal diagnostic:

| Pillar | Strict Requirement | Common Disqualifier |
| :--- | :--- | :--- |
| **1. Causality** | Uses only data available at or before `decision_ts_ms`. | Future universe leakage, unconfirmed candle closes. |
| **2. Executability** | Includes realistic transaction costs, funding, spread, and capacity. | Gross returns, infinite liquidity assumptions. |
| **3. Accounting** | Cash, positions, fees, and funding reconcile exactly. | Unreconciled PnL, floating point drift. |
| **4. Provenance** | Immutable audit trail of which data shaped which rules. | Untracked data exposure, missing commit hashes. |

---

## 3. Statistical Significance Standard

* **Significance Threshold**: **$t \ge 2.5$** (two-sided, $p \approx 0.012$).
* **Effective Date**: Prospective since 2026-07-31 (replaces legacy Bonferroni $t \ge 3.25$ threshold).

### The 5 Plateau & Placebo Validation Checks
A candidate parameter cell that beats its placebo must pass all 5 checks to be accepted as an empirical finding:
1. **Parameter Smoothness**: Neighbouring grid values carry performance deltas of the same sign.
2. **Lag Stability**: Rule executed with 1-day lag still beats its placebo.
3. **Persistence**: Rule required on two consecutive timestamps still beats its placebo.
4. **Directional Asymmetry**: Inverting the rule condition does not beat the placebo.
5. **Gain Concentration**: The top 3 trades contribute $\le 50\%$ of total net strategy gain.
* **Placebo Benchmark**: Variant beats placebo when $\le 5\%$ of matched random draws score as well.

---

## 4. Promotion & Demotion Protocol

Promoting or demoting a strategy requires a concise 5-line record committed alongside the config change point:

```text
Claim:                                 [Concise statement of operational change]
Config commit:                         [Git SHA of the committed configuration]
Forward record:                        [Days evaluated, net delta vs baseline, max drawdown]
Decision:                              [Promote | Demote | Modify]
Date:                                  [YYYY-MM-DD UTC]
```

### The 6-Item Evidence Note
Any decision-influencing research report must include:
1. **Claim**: The specific hypothesis and operational decision it informs.
2. **Data Lineage**: Exact data that shaped the model vs data that graded it.
3. **Scope**: Venue, asset universe, observation window, capital scale.
4. **Net Performance**: Effect size, Sharpe, max drawdown, net of all fees and funding.
5. **Artifact Provenance**: Links to immutable report JSONs, parquet files, and git commits.
6. **Negative Scope**: Explicitly state what the test does *not* prove.

---

## 5. Funded Execution Authority

* **The Air Gap**: Research reports, backtests, and git commits **cannot arm real money**.
* **Master Switch**: Live funded trading requires `REAL_MONEY=true` set manually by the operator in `/etc/liquidity-migration/bybit-mainnet.env`.
