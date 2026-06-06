# Pre-registration: Fresh Pop15 2m Daily-DD Gate Scale 1.5

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Increase the combined-book overlay scale for the accepted `fresh_pop15` 2m
liquidity + daily drawdown gate variant from `1.0` to `1.5`.

## Hypothesis

The daily drawdown gate reduced the weakest combined-book timing problem while
leaving the overlay's positive expected return intact. The 1.0 overlay scale is
therefore conservative. Scaling to 1.5 should improve combined return, MAR, and
Sharpe on both venues while keeping drawdown inside the same cross-venue gate.

## Predicted direction + magnitude

- Combined return: higher than scale 1.0 on both venues.
- Combined MAR and Sharpe: higher than scale 1.0 on both venues.
- Drawdown ratio: higher than scale 1.0, but still no more than `1.10x` daily
  baseline on both venues, including 2x overlay-cost stress.
- Failure mode if hypothesis wrong: overlay concentration increases path
  drawdown faster than return, especially on Binance under cost stress.

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
- Overlay scale: `1.5`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept scale `1.5` as the best current combined-book risk variant only if all
of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- compared with scale `1.0`, combined return and MAR improve on both venues,
- compared with scale `1.0`, combined drawdown ratio remains no worse than
  `+0.10` absolute on both venues.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.5 --daily-dd-entry-min -0.10 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-scale15.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_scale15_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_scale15_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_scale15_2026-06-05_cost200`

Scale 1.0 control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_2026-06-05`

Base combined-book result:

| Venue | Scale | Trades | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.0 | 258 | 116.50% | 5.57 | 3.40 | 0.958 | yes |
| Binance | 1.0 | 258 | 24.59% | 0.72 | 1.49 | 1.034 | yes |
| Bybit | 1.5 | 258 | 135.82% | 6.03 | 3.58 | 1.032 | yes |
| Binance | 1.5 | 258 | 33.39% | 0.95 | 1.77 | 1.063 | yes |

At 2x overlay costs, scale 1.5 still passes:

| Venue | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | --- |
| Bybit | 131.90% | 5.75 | 3.51 | 1.052 | yes |
| Binance | 31.38% | 0.87 | 1.68 | 1.097 | yes |

## Verdict

Accepted as the best current combined-book risk variant. Scale 1.5 improves
combined return, MAR, and Sharpe on both venues versus scale 1.0, while the 2x
overlay-cost stress still passes all gates. Binance 2x-cost drawdown ratio is
close to the line at `1.097`, so this is a research-stage sizing candidate, not
a real-money promotion.
