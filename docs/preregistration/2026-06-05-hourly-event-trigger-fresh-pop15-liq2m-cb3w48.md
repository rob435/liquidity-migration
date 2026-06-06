# Pre-registration: Fresh Pop15 2m CB3/W48 Risk Throttle

**Date:** 2026-06-05
**Author:** Codex
**Stage:** rejected

## What's changing

Add a causal adverse-exit entry pause to the accepted `fresh_pop15` 2m liquidity
variant: pause new entries after at least 3 net-negative exits have closed in
the trailing 48 hours.

## Hypothesis

The sparse event overlay still has occasional market-wide squeeze clusters. A
light breaker should skip only the entries that arrive immediately after several
confirmed losing exits, reducing cluster risk without changing the alpha signal
or materially lowering trade count.

## Predicted direction + magnitude

- Trade count: slightly lower than the 2m `fresh_pop15` control on both venues.
- Standalone return: similar to or modestly better than the 2m control on both
  venues.
- Combined-book drawdown: no worse than the 2m control on both venues.
- Failure mode if hypothesis wrong: the breaker overfits the realized loss
  sequence, removes rebounds after losing fades, or improves one venue while
  degrading the other.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper

## Frozen grid

- Venues: Bybit, Binance
- Feature set: `max_ret168`
- Residual-momentum quantile: `0.25`
- Liquidity gate: hourly `turnover_quote >= 2,000,000`
- BTC trend gate: `uptrend`
- Side/construction: short-only D9
- Entry event trigger: `fresh_pop15`
- Entry delay: `1` hour after the deciding closed bar
- Hold: `24h`
- Exit mode: `fixed`
- Adverse-exit pause: `entry_pause_after_adverse_exits = 3`
- Pause window: `entry_pause_window_hours = 48`
- Costs: existing continuous execution model, funding and impact included
- Combined-book check: add short-only overlay at scale `1.0` to deployed daily
  short baseline.
- Stress: repeat combined-book check with 2x overlay trading costs.

## Decision rule (a priori)

Accept CB3/W48 as a risk-throttled variant only if all of the following are
true:

- standalone short-only overlay has positive MTM total return on both venues,
- trade count is lower than the 2m `fresh_pop15` control on both venues,
- combined daily+overlay MAR beats deployed daily short MAR on both venues,
- combined total return beats deployed daily short total return on both venues,
- combined absolute max drawdown is no more than `1.10x` the daily baseline on
  both venues,
- the 2x overlay-cost stress still passes the same combined-book MAR/total/DD
  gates,
- compared with the 2m control, combined drawdown ratio does not worsen on
  either venue and either combined MAR or combined return improves on at least
  one venue.

If it passes these gates but has lower raw combined return than the 2m control,
classify it as a risk-throttled variant, not the primary candidate. Passing is
research evidence only and is not real-money promotion.

## Run command

```bash
.\.venv\Scripts\python.exe scripts\continuous_causal_rmom_vs_daily.py --venues bybit,binance --rmom-quantiles 0.25 --feature-set max_ret168 --liq-turnover-mins 2000000 --hold-hours 24 --exit-modes fixed --btc-trend-gates uptrend --entry-event-triggers fresh_pop15 --entry-pause-after-adverse-exits 3 --entry-pause-window-hours 48 --pre-registration docs/preregistration/2026-06-05-hourly-event-trigger-fresh-pop15-liq2m-cb3w48.md --out $HOME\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_cb3w48_2026-06-05
```

## Post-run results

Run root:
`C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop15_liq2m_cb3w48_2026-06-05`

Combined-book roots:

- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_cb3w48_2026-06-05`
- `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_fresh_pop15_liq2m_cb3w48_2026-06-05_cost200`

Standalone short-only overlay versus 2m control:

| Venue | Variant | Trades | Breaker skips | Total return | MAR | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 2m control | 258 | 0 | 17.64% | 2.26 | -2.56% |
| Binance | 2m control | 263 | 0 | 13.86% | 1.45 | -3.35% |
| Bybit | CB3/W48 | 254 | 4 | 17.87% | 2.29 | -2.56% |
| Binance | CB3/W48 | 253 | 15 | 14.00% | 1.47 | -3.35% |

Combined daily+short-only overlay:

| Venue | Variant | Combined return | Combined MAR | DD ratio | Pass |
| --- | --- | ---: | ---: | ---: | --- |
| Bybit | 2m control | 116.50% | 5.57 | 0.958 | yes |
| Binance | 2m control | 24.16% | 0.69 | 1.060 | yes |
| Bybit | CB3/W48 | 117.02% | 5.96 | 0.899 | yes |
| Binance | CB3/W48 | 24.32% | 0.65 | 1.126 | no |

At 2x overlay costs, Binance still fails the drawdown gate with DD ratio
`1.147`; Bybit still passes with return `114.64%`, MAR `5.80`, and DD ratio
`0.906`.

## Verdict

Rejected. The breaker improves standalone returns and Bybit combined-book
quality, but it fails the binding cross-venue combined drawdown rule on Binance.
This is not a robust promotion of the risk throttle. Keep the breaker off for
the current primary/2m candidates; the result is useful as evidence that the
daily-book interaction, not standalone overlay PnL, is the limiting constraint.
