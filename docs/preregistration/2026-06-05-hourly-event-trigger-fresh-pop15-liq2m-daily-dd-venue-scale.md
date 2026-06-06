# Pre-registration: Fresh Pop15 2m Daily-DD Venue Scales

**Date:** 2026-06-05
**Author:** Codex
**Stage:** accepted

## What's changing

Use venue-specific overlay scales for the accepted `fresh_pop15` 2m liquidity +
daily drawdown gated variant:

- Bybit scale: `1.75`
- Binance scale: `1.50`

## Hypothesis

The global scale `1.5` is constrained by Binance's 2x-cost drawdown ratio. Bybit
has more stress headroom, so it can carry a larger overlay allocation without
breaching the same risk gate. Venue-specific scaling should improve Bybit
combined return/MAR while leaving Binance unchanged.

## Predicted direction + magnitude

- Bybit combined return, MAR, and Sharpe should improve versus global scale
  `1.5`.
- Binance metrics should match the accepted scale `1.5` variant.
- Both venues must still pass the normal and 2x overlay-cost MAR/total/DD gates.
- Failure mode if hypothesis wrong: Bybit drawdown increases faster than return
  and breaches the 2x-cost `1.10x` daily drawdown ratio gate.

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
- Venue scales: `bybit=1.75,binance=1.50`
- Costs: existing continuous execution model, funding and impact included
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept the venue-specific scales only if all of the following are true:

- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- compared with global scale `1.5`, Bybit combined return and MAR improve,
- compared with global scale `1.5`, Binance combined return, MAR, and drawdown
  ratio are unchanged within numerical tolerance.

Passing is research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\combine_daily_continuous_overlay.py --execution-root $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_2026-06-05 --cell-id q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop15_imp50_cap1m --overlay-construction short_only --scale 1.0 --venue-scales bybit=1.75,binance=1.50 --daily-dd-entry-min -0.10 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-daily-dd-venue-scale.md --out $HOME\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_venue_scale_2026-06-05
```

## Post-run results

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_venue_scale_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_venue_scale_2026-06-05_cost200`

Global scale 1.5 control:
`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_dailydd10_scale15_2026-06-05`

Base combined-book result:

| Venue | Scale | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.50 | 135.82% | 6.03 | 3.58 | 1.032 | yes |
| Binance | 1.50 | 33.39% | 0.95 | 1.77 | 1.063 | yes |
| Bybit | 1.75 | 146.02% | 6.25 | 3.65 | 1.070 | yes |
| Binance | 1.50 | 33.39% | 0.95 | 1.77 | 1.063 | yes |

At 2x overlay costs, the venue scales still pass:

| Venue | Scale | Combined return | Combined MAR | Combined Sharpe | DD ratio | Pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit | 1.75 | 141.26% | 5.92 | 3.57 | 1.092 | yes |
| Binance | 1.50 | 31.38% | 0.87 | 1.68 | 1.097 | yes |

## Verdict

Accepted as the best current combined-book allocation variant. Venue-specific
scaling improves Bybit combined return, MAR, and Sharpe versus global scale
`1.5`, while Binance remains numerically unchanged. Both venues pass the 2x
overlay-cost stress, but both stressed drawdown ratios are close to the `1.10`
limit, so this remains research-stage sizing and not a real-money promotion.
