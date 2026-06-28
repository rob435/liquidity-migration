# Continuous Fade Cluster Risk-Of-Ruin Bootstrap

Date: 2026-06-28

Run label: exploratory.

Real money: no. Offline research only.

## Hypothesis

The deterministic active-book squeeze table showed current sampled sizing
survives single injected shocks, but it did not answer how often clustered
historical losses plus injected tail events produce material drawdowns. The next
tail diagnostic should resample same-signal trade clusters, not individual
trades, and report risk-of-ruin style probabilities.

## Method

Inputs:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/trades_enriched.parquet`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/synthetic_squeeze_survival.csv`

Bootstrap unit: `(venue, entry_signal_ts_ms)` cluster, with returns equal to the
sum of component-weighted `portfolio_net_return` for that cluster.

Trials: 10,000 deterministic paths per venue and scenario, seed `20260628`.
Each path has the same cluster count as the observed validation tape.

Scenarios:

| Scenario | Description |
|---|---|
| `cluster_bootstrap` | Sample same-signal clusters with replacement. |
| `block_bootstrap_20_clusters` | Sample consecutive 20-cluster blocks with replacement. |
| `worst_cluster_overweighted_3x` | Sample clusters with the worst 5% clusters receiving 3x weight. |
| `tail_injected_one_worst_100pct` | Cluster bootstrap plus one worst active-book one-coin +100% shock. |
| `tail_injected_one_worst_outage_100pct` | Cluster bootstrap plus one worst active-book one-coin +100% shock with outage/extra exit damage. |
| `tail_injected_three_p95_100pct` | Cluster bootstrap plus three p95 active-book one-coin +100% shocks. |

Metrics:

- median, 5th percentile, and 1st percentile final and annualized return
- expected shortfall of the worst 5% final returns
- probability of negative final return
- probability of 5%, 10%, 20%, and 50% drawdown
- probability of account impairment, proxied as equity falling below 50%
- worst observed simulated drawdown
- p95 and max longest drawdown stretch in cluster units

## Decision Rule

This diagnostic cannot approve live size. It can only:

- reject the current sizing as obviously too large if 20% or 50% drawdowns occur
  with non-trivial probability under cluster/tail resampling;
- support continued tiny-size demo/paper observation if 5% drawdowns are rare,
  10%+ drawdowns are very rare, and tail-injected paths still avoid account
  impairment.

Any result remains `exploratory` because the validation window is spent and this
is not a real exchange liquidation/order-book simulator.

## Known Limits

Cluster returns are component-level return contributions, not a full exchange
margin engine. Tail injection uses the already measured active-book squeeze
losses and places them inside bootstrapped paths. This does not model order-book
depth, liquidation queues, position-level maintenance tiers, insurance fund
behavior, or failed conditional stops.

## Outcome

Completed 2026-06-28. Verdict: exploratory tail-risk diagnostic, not a live-size
approval.

| Venue | Scenario | p(DD >=5%) | p(DD >=10%) | p(DD >=20%) | Account impairment p | Annual return p1 |
|---|---|---:|---:|---:|---:|---:|
| Bybit | cluster bootstrap | 0.00% | 0.00% | 0.00% | 0.00% | +4.01% |
| Binance | cluster bootstrap | 0.05% | 0.00% | 0.00% | 0.00% | +2.21% |
| Bybit | one worst +100% outage shock | 2.92% | 0.00% | 0.00% | 0.00% | +3.02% |
| Binance | one worst +100% outage shock | 7.32% | 0.00% | 0.00% | 0.00% | +1.02% |
| Bybit | three p95 +100% shocks | 7.50% | 0.00% | 0.00% | 0.00% | +2.02% |
| Binance | three p95 +100% shocks | 18.72% | 0.01% | 0.00% | 0.00% | -0.02% |
| Bybit | worst 5% clusters sampled at 3x weight | 87.68% | 33.70% | 0.07% | 0.00% | -5.40% |
| Binance | worst 5% clusters sampled at 3x weight | 97.38% | 66.18% | 0.95% | 0.00% | -6.64% |

The plain cluster bootstrap and one-off tail-injected paths do not show account
impairment or 20% drawdown risk over the spent validation tape. That supports
continued tiny-size demo/paper observation.

The worst-cluster-overweighted scenario is the important negative read. If bad
clusters recur more often than the historical mix, 10% drawdowns become common
and the 1st-percentile annualized return turns negative on both venues. This is
not a reason to change the signal, but it is a reason to keep portfolio heat
caps, circuit breakers, reconciliation health gates, and disaster protection in
scope before any size increase.

Artifacts:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/cluster_risk_of_ruin.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/reports/final_research_report.md`
