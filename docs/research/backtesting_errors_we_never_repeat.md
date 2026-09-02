# Research Failure-Mode Reference

Catalog of 35 backtesting, execution, and statistical traps identified across quantitative research and platform operations.

---

## 1. Information Leakage & Causality

| # | Failure Mode | Pathology & Mechanism | Prevention Contract |
| :-: | :--- | :--- | :--- |
| **1** | **Future Universe Selection** | Backtesting current liquid names backward; ignores delistings. | Use Point-in-Time (PIT) membership manifests only. |
| **2** | **Future Information in Signals** | Features peeking ahead of the decision instant. | Audit close timestamps, vendor publication delays, and joins. |
| **4** | **Revised / Non-PIT Data** | Using restated historical prints (e.g. revised index/funding). | Use bitemporal raw snapshots recorded as-of trade time. |
| **12** | **Instrument Lifecycle Ignored** | Treating pre-listing or post-delisting intervals as tradeable. | Intersect eligible intervals strictly with launch/delivery dates. |
| **13** | **Timestamp / Resampling Drift** | Aggregating hourly bars with inconsistent session boundaries. | Enforce causal alignment; timestamps reflect bar open, close is actionable only at end. |
| **14** | **Impossible OHLC Ordering** | Assuming high occurred before low within an intraday bar. | When stop and target both trigger in 1 bar, use tick/1m data or worst-case fill. |
| **15** | **Warm-Started Memory** | Pre-seeding trailing indicators with data unavailable at boot. | Initialize state filters strictly when the live engine could first observe them. |
| **26** | **Optional Stopping / Peeking** | Stopping forward evaluation early once a curve looks positive. | Pre-commit fixed evaluation epochs or use sequential testing rules. |
| **31** | **Post-Filter Observability** | Inspecting candidate tapes only after restrictive entry gates. | Retain and audit pre-gate candidate drop rates. |
| **32** | **Partition Materialization Drift** | Raw keys and replayed parquet partition keys disagree. | Enforce partition schema validation before feature materialization. |

---

## 2. Execution, Fills & Cost Modeling

| # | Failure Mode | Pathology & Mechanism | Prevention Contract |
| :-: | :--- | :--- | :--- |
| **3** | **Instantaneous Fills** | Assuming order fills at the exact signal price. | Model execution latency, queue position, and post-signal price impact. |
| **5** | **Ignoring Capacity** | Assuming fills at scales exceeding market depth or ADV. | Bound notional to $\le 1-2\%$ of historical hourly volume. |
| **6** | **Ignoring Venue Fees** | Overlooking exchange taker fees, VIP tiers, and rebates. | Apply conservative venue fee tiers (e.g. 5.5–7.8 bp per side). |
| **7** | **Ignoring Slippage** | Assuming execution at mid-price or closing print. | Model spread crossing and volume-dependent slippage penalties. |
| **8** | **Ignoring Market Impact** | Assuming infinite liquidity without adverse price shift. | Apply square-root participation models for large order baskets. |
| **9** | **Short-Access Fantasy** | Assuming unrestricted shorting on illiquid or margin-restricted coins. | Check historical short-sale eligibility and borrow availability. |
| **10** | **Financing / Funding Fantasy** | Omitting perpetual funding payments from net return. | Deduct settled 8-hour funding cash flows directly from position cash. |
| **11** | **Trading Bans / Limits** | Missing venue circuit breakers, maintenance halts, or reduce-only. | Incorporate historical venue halt status and contract leverage limits. |
| **22** | **Venue Mechanics Fantasy** | Omitting minimum notional, tick sizes, or lot quantizations. | Pass orders through venue instrument filters before evaluation. |
| **35** | **100% Turnover Cost Assumption** | Assuming 100% rebalancing on slow, sticky signals. | Measure realized turnover; slow strategies do not pay 100% round trips every bar. |

---

## 3. Statistical & Experimental Methodology

| # | Failure Mode | Pathology & Mechanism | Prevention Contract |
| :-: | :--- | :--- | :--- |
| **17** | **Parameter Data Mining** | Over-optimizing thresholds without disclosing trial counts. | Pre-register parameter ranges; require parameter plateaus ($t \ge 2.5$). |
| **18** | **Out-of-Sample Reuse** | Re-testing on out-of-sample data until it passes. | Once seen, data is spent; forward validation requires new prospective days. |
| **19** | **Multiple Testing Denial** | Ignoring the cumulative false-positive rate across sweeps. | Report full grid surfaces and false discovery rates (FDR). |
| **21** | **Hidden Common Risk** | Correlated positions masquerading as independent bets. | Measure cross-sectional beta and industry/factor concentration. |
| **27** | **Safety / Alpha Conflation** | Treating risk management stops as expected return drivers. | Distinguish between edge generators and capital preservation constraints. |
| **28** | **Administrative Truth** | Trusting prior document labels ("promoted", "approved") over code. | Always verify claims against primary code, WAL, and venue receipts. |
| **29** | **Pseudoreplication** | Treating sub-events of a single decision as independent $N$. | Aggregate observations to the unique decision / wave cluster. |
| **34** | **Log Returns as P&L Target** | Using log returns induces negative variance drag ($-35\text{ bp/d}$). | **Always score strategy P&L on arithmetic returns**, not log returns. |

---

## 4. Accounting & Integrity

| # | Failure Mode | Pathology & Mechanism | Prevention Contract |
| :-: | :--- | :--- | :--- |
| **16** | **Lifecycle Mismatch** | Discrepancy between research state machine and live engine. | Replay research inputs through native Rust `strategy_contract` adapter. |
| **20** | **Faulty Accounting** | Unreconciled margin, leverage, or cash flips. | Enforce double-entry accounting reconciling cash, equity, and positions. |
| **23** | **Unreconstructable Visuals** | Plots without attached config hashes or dataset commits. | Require immutable `manifest.json` and code commit hashes for all artifacts. |
| **24** | **Unreconciled Live Drift** | Live fills drifting from model targets without explanation. | Continuous WAL reconciliation against venue trade history. |
| **25** | **All-or-Nothing Compute** | Uncheckpointed runs failing after hours without receipts. | Checkpoint pipeline jobs every hour or fixed batch. |
| **30** | **Missing-as-Zero Bias** | Treating unmeasured slippage, costs, or data as zero. | Preserve explicit `NaN` / null values; do not coerce missing data to 0. |
| **33** | **Process Displacement** | Building excessive tooling that delays decision-making. | Focus on decision-useful code; retire unused verification shims. |
