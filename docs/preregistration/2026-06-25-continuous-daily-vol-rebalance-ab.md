# Pre-Registration: Continuous Daily Vol Rebalance A/B

Date: 2026-06-25
Stage: run-complete

## Change

Test whether the continuous TP12 ensemble should re-enable its daily volatility
rebalance layer instead of the current constant-scale target.

## Hypothesis

The daily vol adjuster may improve portfolio risk by cutting exposure after
realized-vol or drawdown stress, but it can also add path dependency, resize
cost, and leverage concentration. Re-enable only if the exact legacy ON rule
beats the current OFF control on both venues under the same frozen components,
weights, BTC/ETH hedge, BTC-vol hedge regime, funding, and cost model.

## Data

- Roots: `C:/Users/user/SHARED_DATA/bybit_full_pit`,
  `C:/Users/user/SHARED_DATA/binance_full_pit`
- Window: full available PIT component history through each root's current
  `klines_1h` end date, rebuilt from inception for path-dependent scale state.
- PIT/cost/funding assumptions: current continuous event engine, PIT archive
  manifest, real funding, current TP12 components, current inverse-vol entry
  sizing, current frozen weights, current BTC/ETH 2f hedge, current BTC-vol hedge
  regime, current resize cost policy.

## Arms

Primary decision arm:

- `off_current`: current local target, `enabled=false`.
- `on_045_max4_legacy`: retained legacy params, `enabled=true`, 90d realized
  vol, target daily vol 0.045, max scale 4.0, drawdown half at -4%, no momentum
  cutoff.

Pre-registered diagnostics only:

- `on_045_max4_volonly`: legacy vol target without drawdown throttle.
- `on_035_max3_balanced`: lower target/cap with -4% drawdown half throttle.
- `on_025_max2_defensive`: lower target/cap with -3% drawdown half throttle.
- `on_045_max4_mom90_quarter`: legacy vol target with trailing-90d raw-momentum
  quarter-size throttle when the trailing sum is negative.

Diagnostics can suggest a future preregistered run, but cannot justify turning
the live target on in this run unless the primary legacy ON arm passes.

## Decision Rule

Accept re-enabling the daily vol adjuster only if `on_045_max4_legacy` versus
`off_current` passes every primary criterion on both venues:

- MAR improves by at least 10%.
- Max drawdown is no worse, with a 1 percentage point tolerance.
- Worst rolling 90-calendar-day return is no worse, with a 1 percentage point
  tolerance.
- Total return is not worse by more than 10% relative or 5 percentage points
  absolute, whichever is looser.
- Monthly leave-one-out does not show the positive edge flipping negative after
  removing a single month when the full-window delta is positive.
- The run artifacts include per-arm ledgers, monthly returns, summary CSV,
  comparison CSV, manifest JSON, and a markdown verdict.

If any venue fails, keep daily rebalance disabled. If a diagnostic arm passes
but the primary legacy arm fails, mark the result hypothesis-only and require a
new preregistration before any target change.

## Command

```bash
PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/Scripts/python.exe \
  scripts/continuous_daily_rebalance_ab.py \
  --venues bybit,binance \
  --out reports/continuous_daily_rebalance_ab_2026-06-25
```

## Artifacts

Expected output paths:

- `reports/continuous_daily_rebalance_ab_2026-06-25/report.md`
- `reports/continuous_daily_rebalance_ab_2026-06-25/summary.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/comparisons.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/monthly.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/manifest.json`
- `reports/continuous_daily_rebalance_ab_2026-06-25/ledgers/*.csv`

## Result

Verdict: `REJECT_KEEP_DAILY_REBALANCE_DISABLED`.

Artifacts:

- `reports/continuous_daily_rebalance_ab_2026-06-25/report.md`
- `reports/continuous_daily_rebalance_ab_2026-06-25/summary.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/comparisons.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/monthly.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/manifest.json`
- `reports/continuous_daily_rebalance_ab_2026-06-25/ledgers/*.csv`
- `reports/continuous_daily_rebalance_ab_2026-06-25/_tp12_components/`

Primary TP12 result, legacy ON versus current OFF:

| Venue | OFF total | OFF DD | OFF MAR | ON total | ON DD | ON MAR | Verdict driver |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | +3.95% | -0.91% | 22.21 | +9.78% | -2.50% | 22.32 | MAR gain below 10%; drawdown worse by 1.59pp |
| Binance | +18.42% | -1.41% | 3.91 | +79.64% | -5.60% | 3.66 | MAR worse; drawdown worse by 4.19pp; worst 90d worse by 2.07pp |

Mechanism read: legacy ON mostly turns into leverage, not risk control. Mean
scale was 2.71x on Bybit and 3.74x on Binance; p95 scale hit the 4.0 cap on
both venues. Higher total return comes with materially worse drawdown and lower
or insufficient MAR by the pre-registered rule.

Diagnostics: the lower-cap variants did not justify a target change. The
defensive `on_025_max2_defensive` improved Bybit MAR but did not improve Binance
MAR or worst-90d; it remains hypothesis-only.

Limitations: the current Bybit TP12 rebuild spans only 77 calendar days / 70
ledger days (`2026-03-08` to `2026-05-23`) despite a 2023 start config, so this
is current-root replay evidence rather than full-history Bybit acceptance
evidence. The dispatcher isolates the daily rebalance layer and does not
re-simulate the live `CTRL_BTC_RISK_70_90_35` entry-size overlay inside component
ledgers. These caveats do not support re-enabling; they only block making a
stronger positive claim.
