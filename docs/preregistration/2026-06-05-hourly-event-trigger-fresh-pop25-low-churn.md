# Parameter Pre-Registration: Hourly Fresh Pop25 Low-Churn Overlay

Date: 2026-06-05

Status: PRE-REGISTERED before confirmatory run. Prior exploratory outputs may
motivate this run but are not acceptance evidence.

## Hypothesis

The hourly `fresh_pop15` overlay trades frequently because the trigger is a
cross-symbol hourly scanner. Raising the event threshold to `fresh_pop25` should
cut turnover materially while preserving enough fade edge to improve the
deployed daily short book on both venues.

## Frozen Parameters

- Venues: Bybit and Binance
- Data roots:
  - `C:\Users\user\SHARED_DATA\bybit_full_pit`
  - `C:\Users\user\SHARED_DATA\binance_full_pit`
- Window: `2023-04-01` through `2026-05-28`
- Signal side: short-only D9 overlay
- RMOM quantile: `0.25`
- Feature set: `max_ret168`
- BTC trend gate: `uptrend`
- Entry trigger: `fresh_pop25`
- Liquidity gate: hourly turnover >= `2,000,000`
- Entry delay: `1h`
- Hold: fixed `24h`
- Gross exposure in execution artifact: `0.5`
- Max active positions: `25`
- Funding and modeled impact: enabled, existing continuous engine defaults
- Combined-book overlay construction: `short_only`
- Combined-book venue scales: `bybit=1.8`, `binance=5.0`
- Cost stress: repeat combined-book check with `--overlay-cost-multiplier 2.0`

No daily drawdown entry gate, rescue scaling, or daily sleeve throttle is part
of the primary acceptance rule for this run.

## Acceptance Rules

The candidate passes only if all of the following are true on both venues:

1. Executed overlay trade count is at least 50% below the accepted `fresh_pop15`
   2m reference count.
2. Standalone overlay MTM total return is positive.
3. Standalone overlay MTM MAR is positive.
4. Combined-book total return exceeds the deployed daily short baseline.
5. Combined-book MAR exceeds the deployed daily short baseline.
6. Combined-book absolute max-drawdown ratio versus daily baseline is `<= 1.10`.
7. The same combined-book gates pass under 2x overlay-cost stress.

## Rejection Rules

Reject if either venue fails any acceptance rule, if results depend on
post-hoc daily throttling, or if the lower trade count comes from data gaps or
execution skips rather than the stricter trigger.

## Planned Artifacts

- Execution root:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05`
- Combined root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05`
- 2x cost root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05_cost200`
- Research note:
  `docs/research/hourly_event_trigger_fresh_pop25_low_churn_2026-06-05.md`

