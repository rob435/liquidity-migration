# Continuous Forward Readiness

Date: 2026-06-28.

## Question

Is the current `continuous_ensemble_v2` paper/demo forward path producing enough
ledger evidence to compare live-demo execution with the paper shadow?

## Method

Run the existing readiness gate from the v2 baseline clock:

```bash
.venv/Scripts/python.exe -m liquidity_migration continuous-forward-readiness \
  --paper-data-root data/bybit-continuous-paper-event \
  --demo-data-root data/bybit-continuous-demo-event \
  --start-ts-ms 1781812440000 \
  --strategy-profile continuous_ensemble_v2 \
  --paper-strategy-id continuous_fade_v2_paper \
  --demo-strategy-id continuous_fade_v2 \
  --output-dir reports/continuous_forward_readiness/2026-06-28-v2-current
```

Start boundary: `2026-06-18T19:54:00+00:00`.

## Result

Readiness gate: `False`.

The failure is sample-size only:

- Paper cycles after the v2 clock: `121`.
- Demo cycles after the v2 clock: `123`.
- Paper rebalance audit: `True`.
- Demo rebalance audit: `True`.
- Paper operational audit: `True`.
- Demo operational audit: `True`.
- Paper trades: `0`.
- Demo trades: `0`.
- Paired paper/demo trades: `0`.
- Paper-only trades: `0`.
- Demo-only trades: `0`.
- Paper/demo operational-cycle anomalies: `0` entry-risk blocks, `0`
  order failures, `0` unprotected-position seconds, `0` portfolio-heat clamps,
  and `0` account-drawdown kill-switch activations.
- Not yet measurable: fill rate, fill latency, PostOnly cancel rate, fees,
  funding, maker/taker split, stop-placement latency, and stop-repair count.
- Blocking issue: paired trades `0` below `min_pairs_warning=20`.

Artifacts:

- `reports/continuous_forward_readiness/2026-06-28-v2-current/continuous_forward_readiness.md`
- `reports/continuous_forward_readiness/2026-06-28-v2-current/paper_demo/continuous_paper_demo_reconciliation.md`
- `reports/continuous_forward_readiness/2026-06-28-v2-current/paper_rebalance/continuous_rebalance_cycle_audit.md`
- `reports/continuous_forward_readiness/2026-06-28-v2-current/demo_rebalance/continuous_rebalance_cycle_audit.md`
- `reports/continuous_forward_readiness/2026-06-28-v2-current/paper_operational/continuous_operational_metrics.md`
- `reports/continuous_forward_readiness/2026-06-28-v2-current/demo_operational/continuous_operational_metrics.md`

## Verdict

Forward-readiness remains blocked by lack of trade sample, not by detected
paper/demo drift or cycle-level operational anomalies. This is
execution-readiness evidence only; it is not alpha, promotion, or live-size
evidence. Keep waiting for forward paper/demo trades before making slippage,
fill-rate, fee, funding, or maker/taker claims.

2026-06-28 amendment: the operational audit now treats account-drawdown
kill-switch activation as a first-class readiness failure even if mixed-version
cycle rows are missing `entry_risk_health_reasons`. The top-level readiness
report also surfaces portfolio-heat clamp and account-drawdown activation
counts. The current rerun still shows both counters at `0` for paper and demo.
