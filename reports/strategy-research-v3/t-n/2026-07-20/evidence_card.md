# T-N — cascade-riding long + C-H1/C-H2 estimands — Lane-1 evidence card

Date: 2026-07-20. Lane-1 exploratory on seen data. Script:
`scripts/research_v3/tn_cascade_long.py`. Hashes/params: `manifest.json`
(Bybit), `binance/manifest.json`.

## Claim and decision

Two claims, one population (the deployed CONTINUOUS book's own trigger
surface, inverted):
1. The frozen backlog estimands C-H1/C-H2
   (`docs/strategy_overhaul_lessons.md`, registered as untested priors):
   does D9 membership (C-H1), and the BTC-uptrend state within D9 (C-H2),
   condition the 24-hour short-directional path after the frozen
   categorical controls?
2. T-N: does riding these pump events LONG (the anti-book) clear the
   admission bar in any declared cell?

## Population and design

Engine-owned production decile panels (rmom-low-quartile universe,
exclusions baked): Bybit v4 cache 2023-04→2026-07-09, Binance v3 cache
2023-04→2026-06-24. Static eligibility applied: turnover_24h ≥ $500k
(production liq gate), age ≥ 240 d (archive membership), deciles {7,8,9};
24 h per-symbol cooldown collapses excursion repeats into one decision
(item 29). Label: plain 24 h hold from the first close ≥ decision+1 h (the
production entry delay) — NO TP/stop variants; the deployed TP12/24h shape
is untouched (closed line). Funding realized in (entry, exit], signed.
Bybit: 19,660 labelled events (1 entry-bar, 54 exit-bar exclusions);
Binance: 20,129 (0 exclusions). Era split at 2025-01-01; the panel starts
2023-04 so no e2122 era exists here (stated).

## Estimand results (the frozen family: 4 primary tests, α=0.05, Bonferroni 0.0125)

Stratified (terciles of ret1 × turnover_spike_168h × max_ret168, ≥5 events
per side per stratum), 28-day-block bootstrap (2000, seed 20260720):

| Test | point (bp) | s.e. | p | verdict |
| --- | ---: | ---: | ---: | --- |
| C-H1 Bybit (D9 − D7/8, short path) | +28.2 | 20.5 | 0.16 | not significant |
| C-H1 Binance | +31.3 | 17.2 | 0.075 | not significant |
| C-H2 Bybit (BTC-up − fail, within D9) | +62.3 | 46.7 | 0.19 | not significant |
| C-H2 Binance | +26.1 | 37.4 | 0.47 | not significant |

Reading: all four points are positive and cross-venue sign-consistent —
D9 events fall harder than D7/8, and uptrend-state D9 events fall harder
than downtrend ones — i.e., the deployed book's decile-9 selection and
BTC-uptrend gate both point the right way. But none clears the frozen
per-test bar, and the magnitudes (~+26 to +62 bp) sit at or below the 45 bp
cost hurdle: there is no decile-expansion case (D7/8 shorts would arrive
with ~30 bp less gross than D9), and the effects are exactly the size T-K's
power arithmetic says this surface cannot confirm. The two-year-old backlog
rows resolve as "directionally consistent, underpowered" — priors, not
findings, now with measured magnitudes attached.

## T-N long arms (descriptive, all 24 cells reported)

Net45 = long gross + signed funding − 45 bp, by era (means; block s.e. in
`long_arms.csv`):

- Bybit: ALL 12 cells negative (e2324 −5 to −70; e2526 −27 to −185).
- Binance: 11 of 12 negative; the lone positive (D9_btc_down e2324:
  +12.8 ± 26.1, n=829) is insignificant and flips to −58.3 post-2025.
- Frequency: 3–15 events/day per cell — the ≥5 bets/day arm of the bar has
  the frequency but no deployable gross (means negative everywhere that
  matters).

**Verdict: the naive anti-book long is closed on both venues — below bar,
era-unstable, cross-venue consistent in its deadness.** Pump events keep
falling over the following 24 h; that is precisely why the deployed short
book earns gross. Riding them long buys the decay.

## Scope boundary (what T-N did NOT test)

The v7 thesis text's full idea — longs conditioned on a CONFIRMED
liquidation-cascade / squeeze state — requires the P2.1 squeeze features
(OI acceleration, premium spikes, breadth, forward liquidations as P0.3
accrues) as conditioning variables. That conditioned variant is P2.2's
design surface by construction (they share the feature build) and is NOT
closed by this card. What IS closed: the unconditioned inversion and the
BTC-state-conditioned inversion of the deployed trigger surface.

## Non-conclusions

- No statement about squeeze-state-conditioned longs (P2.2 owns it).
- No statement about pre-2023-04 eras (panel window).
- The estimand direction (D9/uptrend validate the short book) is
  directional support, not confirmation — all four tests are underpowered
  by the frozen family rule, and this card does not relax that rule.
- Venues are correlated surfaces; the cross-venue agreement strengthens
  the "dead" verdict but is not independent replication.
