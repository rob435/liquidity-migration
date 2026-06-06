# Pre-registration: Fresh Pop15 2m Daily-DD Rescue Scales

**Date:** 2026-06-05
**Author:** Codex
**Stage:** rejected

## What's changing

Add a causal entry-time rescue scale to the robust venue-scale allocation:

- Keep the daily drawdown entry gate: accept overlay entries only when prior
  closed daily-book drawdown is `>= -10%`.
- Base scales:
  - Bybit: `1.65`
  - Binance: `1.25`
- Rescue scales when prior closed daily-book drawdown is between `-10%` and
  `-5%`:
  - Bybit: `2.25`
  - Binance: `2.00`

## Hypothesis

Binance 2026 weakness is primarily from the deployed daily short book, while the
hourly `fresh_pop15` overlay remains positive. A causal rescue scale should
increase overlay weight only when the daily book is moderately underwater but
not past the `-10%` no-trade line, improving combined recovery without keeping
high overlay risk in normal states.

## Predicted direction + magnitude

- Combined return and MAR should improve versus the robust venue-scale variant
  on both venues.
- 2x-cost drawdown ratios should remain `<= 1.08` on both venues.
- The number of rescue-scaled trade entries should be small relative to total
  trades, so the variant should not behave like a global leverage increase.
- Failure mode if hypothesis wrong: the rescue-scaled entries coincide with
  continued adverse daily-book path, breaching the stress drawdown margin.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset)
- [ ] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

This run recombines existing execution artifacts and rebuilds filtered overlay
MTM from saved trade ledgers; it does not rerun per-venue signal generation.

## Frozen grid

- Source execution artifact:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m`
- Overlay construction: `short_only`
- Daily drawdown entry gate: prior closed daily-book drawdown `>= -0.10`
- Rescue trigger: prior closed daily-book drawdown `<= -0.05`
- Base venue scales: `bybit=1.65,binance=1.25`
- Rescue venue scales: `bybit=2.25,binance=2.00`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the rescue scales only if all of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- the 2x overlay-cost drawdown ratio is `<= 1.08` on both venues,
- compared with the robust venue-scale variant, combined return and MAR improve
  on both venues.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --venue-scales bybit=1.65,binance=1.25 --daily-dd-entry-min -0.10 --daily-dd-rescue-trigger -0.05 --venue-rescue-scales bybit=2.25,binance=2.00 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-rescue-scale.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_rescue_scale_2026-06-05_cost200`

Base combined-book result:

| Venue | Base scale | Rescue scale | Rescue trades | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.65 | 2.25 | 19 | 142.96% | 6.17 | 1.062 | yes |
| Binance | 1.25 | 2.00 | 51 | 31.16% | 0.92 | 1.021 | yes |

At 2x overlay costs:

| Venue | Base scale | Rescue scale | Combined return | Combined MAR | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.65 | 2.25 | 138.40% | 5.84 | 1.086 | no, per `<= 1.08` stress-margin rule |
| Binance | 1.25 | 2.00 | 29.30% | 0.85 | 1.040 | yes |

## Verdict

Rejected under the pre-registered stress-margin rule. The rescue mechanism is
promising, especially on Binance, but Bybit rescue scale `2.25` pushes the
2x-cost drawdown ratio to `1.086`, above the frozen `1.08` internal margin.
Do not accept this exact allocation. A lower Bybit rescue scale needs a fresh
pre-registration.
