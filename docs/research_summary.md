# Research Summary - Liquidity Migration

**Updated:** 2026-06-08
**Status:** research-stage only. Demo and paper evidence are allowed; real-money promotion is not.

This file is the single research source of truth. Old per-experiment receipts and one-off
writeups were consolidated here; git history is the archive. New research should add a
short dated section here, not another pile of preregistration files, unless a parameter
change is meant to become a formal candidate/promotion decision.

## Non-Negotiable Status

- Nothing is approved for real money.
- The short sleeve and long sleeve can run on demo/paper only.
- The continuous sleeve is not promoted. Continuous demo orders remain off unless explicitly
  re-enabled by the operator.
- Forward demo/paper is the real out-of-sample arbiter. There is no clean internal pre-2023
  OOS root.
- Full-PIT data, causal features, cost/funding awareness, and trade/equity ledgers are
  correctness requirements, not optional research preferences.

## Current Map

There are two separate research lines:

1. **Daily liquidity-migration short.** The current promoted short profile is
   `drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`. It does **not** use rmom;
   `liquidity_migration_residual_momentum_max=10.0` is the inactive sentinel.
2. **Continuous fade.** This is research-only. The strongest existing result is the old
   decomposed daily-rebalance candidate. Recent independent work improved its trade logic
   and tail quality, but does not replace the rebalance engine.

The correct direction from here is **merge the good pieces**:

- Keep the old continuous rebalance/risk engine.
- Inject the cleaner independent entry/exit logic.
- Test with and without the old strategy-equity momentum gate.

Do not frame the independent continuous branch as a separate replacement system unless it
beats the rebalance candidate after the same portfolio construction is applied.

## Daily Short - Durable Findings

The daily strategy is a **selection signal** plus simple short execution. Earlier work
over-weighted execution timing; the evidence says selection is the real lever.

Current promoted short, resolved from code:

```text
drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend
rmom inactive: liquidity_migration_residual_momentum_max = 10.0
```

Durable daily findings:

- **Execution timing is not load-bearing.** Fade-confirmation entry and plain delayed entry
  were near-equivalent in the main E1 tests. The alpha is the candidate pool, not clever
  intraday entry timing.
- **Age gate is robust.** Dropping young listings materially improves both venues. Age
  thresholds around 300 days were the most important daily refinement.
- **Rmom is not currently used.** Historical rmom work should be treated as research-only,
  not as a current profile instruction. The operator chose the promoted short without rmom.
- **Concentration matters.** `max_active=3` was too concentrated. Wider books such as
  `max_active=12` reduce worst-day and drawdown risk materially.
- **Worst-case stop modeling caused false nulls.** `bar_extreme` was too punitive as the
  default. `bar_extreme_capped` is the current more realistic bad-case model.
- **Binance matters.** Single-venue Bybit wins are not sufficient; Binance frequently exposes
  overfit or microstructure-specific results.

Open daily issue:

- Keep docs and runbooks aligned to the code-resolved profile above. If rmom is ever
  reconsidered, it needs a fresh, explicit decision and current causality check.

## Continuous - Old Rebalance Candidate

The old continuous rebalance candidate is still the strongest continuous result on Bybit.
It should be treated as the risk-engine baseline to improve, not discarded.

Canonical old candidate:

```python
q25_liq500k_btcup_turn4_pop4_decomp_rebalance_w90_tv25_max4_dd4_trend180_hurdle2
```

Key mechanics:

- signal: `rmom q25 + max_ret168 D9 + BTC 30d uptrend + turn4_pop4`
- accounting: decomposed daily rebalance
- realized vol window: 90 days
- target daily vol: 2.5%
- max scale: 4x
- drawdown half-scale: -4%
- resize cost: 10 bps
- strategy-equity momentum gate: 180d return must be at least +2%; otherwise scale to zero

Primary artifacts:

- `C:\Users\user\SHARED_DATA\continuous_daily_rebalance_strategy_hurdle_2026-06-07`
- `C:\Users\user\SHARED_DATA\continuous_daily_rebalance_strategy_hurdle_cost200_2026-06-07`
- `C:\Users\user\SHARED_DATA\continuous_rebalance_robustness_strategy_hurdle_2026-06-07`

Bybit 3-year comparison:

| Curve | Return | Max DD | MAR | Worst day |
|---|---:|---:|---:|---:|
| Old rebalance candidate | +238.23% | -10.65% | 7.32 | -8.77% |
| Independent current raw | +16.03% | -1.56% | 3.37 | -0.91% |
| Independent current + no-momentum rebalance | +58.92% | -6.25% | 3.09 | -3.66% |

Conclusion: the old candidate is not just a trade signal; its portfolio engine and
strategy-equity momentum gate are doing major work. That is useful, but it also means we
must isolate whether any new entry/exit change improves the underlying trades.

## Continuous - Independent Entry/Exit Work

Goal of the independent branch: clean the trade logic so the system is not dependent on
trading its own equity curve and does not carry catastrophic raw trade tails.

Current independent branch before the latest merge idea:

```text
short, rmom q25, max_ret168 D9, liq >= 500k,
BTC 30d uptrend,
age >= 240d,
turn3_pop4,
crowd cap 2,
take profit 10%,
24h hold,
inverse-vol per-name sizing,
no stop, no rank decay, no equity-curve momentum
```

Important independent findings:

- **TP helps versus the old raw no-TP/no-stop engine.** The old raw engine had more trades
  and higher raw return, but carried unacceptable raw tails including <= -91% trade hits.
- **TP10 and 24h hold are the best current default.** TP12/14/16 were close, but TP10 had
  the cleaner pooled MAR/cost-stress balance.
- **Hard stops hurt.** Stop25/stop35 and failed-fade style loss cutters reduced expectancy.
  They removed some large raw losers but damaged the system more than they helped.
- **MFE giveback and breakeven exits did not help.** They cut winners/expectancy and did
  not improve the final profile enough to keep.
- **Rank decay exits did not matter enough.** Rank70/80 barely changed results; rank90
  increased exits but did not improve the profile.
- **Crowding cap matters.** Crowd cap 2 is cleaner than 3/4/off. Higher caps add trades but
  weaken stress behavior and can reintroduce ugly tails.
- **Entry can be loosened.** `turn3_pop3` improves the no-equity-momentum rebalance version
  versus current `turn3_pop4`, adds trades, and keeps the <= -91% raw tail count at zero in
  the selected sweep.

Main independent artifacts:

- `C:\Users\user\SHARED_DATA\independent_continuous_tp_hold_sweep_exploratory_2026-06-07`
- `C:\Users\user\SHARED_DATA\independent_continuous_entry_filter_sweep_exploratory_2026-06-07`
- `C:\Users\user\SHARED_DATA\independent_continuous_guardrail_sweep_exploratory_2026-06-07`
- `C:\Users\user\SHARED_DATA\independent_continuous_rank_crowd_final_sweep_exploratory_2026-06-07`
- `C:\Users\user\SHARED_DATA\independent_continuous_selected_variants_rebalance_nomom_2026-06-07`
- `C:\Users\user\SHARED_DATA\independent_continuous_turn3pop3_nomom_curves_2026-06-07`

## Continuous - Current Synthesis

The right next candidate is a **merged** system, not a separate independent system:

```text
q25_liq500k_btcup_turn3_pop3_age240_tp10_crowd2_decomp_rebalance_w90_tv25_max4_dd4
```

Formal merged test completed 2026-06-07:

- `C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_2026-06-07`
- `C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_cost200_2026-06-07`
- `C:\Users\user\SHARED_DATA\continuous_merged_signal_rebalance_robustness_2026-06-07`

The winning arm is **no strategy-equity momentum**. Soft 0.25x, soft 0.5x, and the
original hard-off 180d/+2% hurdle all reduced MAR; hard-off was especially bad under
2x costs. The useful inherited component is the decomposed rebalance/vol/DD engine,
not the strategy-equity momentum throttle.

Selected no-equity-momentum rebalance comparison:

| Variant | Pooled trades | Mean return | Mean MAR | Min MAR | Raw <= -91% |
|---|---:|---:|---:|---:|---:|
| current `turn3_pop4`, crowd2 | 1468 | +58.54% | 3.57 | 3.09 | 0 |
| best `turn3_pop3`, crowd2 | 1579 | +62.60% | 4.01 | 3.88 | 0 |

Per-venue for `turn3_pop3` no-momentum rebalance:

| Venue | Return | Max DD | MAR | Worst day | Trades |
|---|---:|---:|---:|---:|---:|
| Bybit | +67.57% | -5.56% | 3.88 | -3.66% | 857 |
| Binance | +57.63% | -4.52% | 4.14 | -2.52% | 722 |

Merged arm comparison:

| Arm | Mean return | Mean MAR | Min MAR | Worst DD | 2x-cost min MAR |
|---|---:|---:|---:|---:|---:|
| no momentum | +62.60% | 4.01 | 3.88 | -5.56% | 2.35 |
| soft 0.50x | +56.05% | 3.52 | 3.15 | -5.89% | 1.82 |
| soft 0.25x | +53.77% | 3.40 | 3.09 | -5.78% | 1.39 |
| hard off | +50.31% | 3.20 | 2.87 | -5.72% | 0.94 |

Interpretation:

- `turn3_pop3` is the best current independent entry improvement.
- It beats `turn3_pop4` on trades and MAR after the same no-momentum rebalance overlay.
- It still does not beat the old hard-momentum rebalance candidate on Bybit max-return.
- It does beat the old hard-momentum candidate as a cleaner cross-venue individual object:
  old Binance was +54.85% / MAR 1.23 / -14.87% DD, while merged no-momentum is
  +57.63% / MAR 4.14 / -4.52% DD.
- The next continuous research state is no longer "independent system replaces old
  candidate." It is: merged trade logic + decomposed rebalance engine, **without** the
  180d hard-off momentum throttle, unless forward evidence later proves otherwise.

### Derivatives-positioning filter frontier

Formal derivatives-positioning filter test completed 2026-06-08:

- `C:\Users\user\SHARED_DATA\continuous_derivatives_positioning_frontier_2026-06-08`

This tested causal 24h funding, premium-index, and mark-index-basis filters on the accepted
merged trade stream, then rebuilt MTM and decomposed rebalance ledgers. Historical OI and
taker flow were excluded from the formal decision because Binance only has the recent
rolling REST window in this root, not 2023-04-01 to 2026-05-28.

Feature coverage was effectively complete: Bybit 100.0% on funding/premium/basis; Binance
99.9% funding, 99.7% premium, 99.4% basis.

Verdict: rejected as a signal improvement. Every pre-registered derivatives hard filter
lost MAR versus the unfiltered merged stream under the base risk rule. The "crowded long
perp unwind" story is not the missing MAR-6 lever in this form; positive funding/premium/
basis mostly removed winners and did not cut drawdown enough.

Top base-cost pooled rows by minimum MAR:

| Filter | Risk rule | Min return | Mean return | Min MAR | Mean MAR | Worst DD |
|---|---|---:|---:|---:|---:|---:|
| all | high | +112.77% | +125.11% | 4.39 | 4.54 | -10.00% |
| all | mid | +80.70% | +88.96% | 4.19 | 4.34 | -7.41% |
| all | tight | +98.03% | +115.36% | 3.88 | 4.05 | -10.05% |
| all | base | +57.63% | +62.60% | 3.88 | 4.01 | -5.56% |
| funding_24h_ge0 | base | +46.54% | +48.32% | 2.80 | 3.40 | -5.32% |

The closest retarget was not a filter; it was unfiltered `high`:

| Venue | Return | MAR | DD |
|---|---:|---:|---:|
| Bybit | +137.46% | 4.39 | -10.00% |
| Binance | +112.77% | 4.70 | -7.79% |

That fails the user's current target: Binance return is below +120%, and both venues are
well below MAR 6. Under 2x costs, no row met min MAR >= 3.0; the best minimum MAR remained
the unfiltered base rule at min return +38.85% and min MAR 2.35.

### Adjacent-signal ensemble and weight frontier

Formal adjacent ensemble and retarget tests completed 2026-06-08:

- `C:\Users\user\SHARED_DATA\continuous_adjacent_signal_ensemble_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_ensemble_retarget_extension_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_ensemble_weight_frontier_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_weight_winner_validation_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_weight_winner_validation_cost200_2026-06-08`

The hand-picked component ensembles improved the merged signal but did not hit the target.
The retarget extension produced a near-miss: `equal_p3_p4p3_p4p5_tp14` with
`w90_tv0.045_max10_ddh-0.04` reached min return +138.12% and min MAR 5.21.
That proved return was no longer the binding issue; Binance shock drawdown was.

A coarse weight simplex over adjacent components then found the first base-cost
continuous individual target hit:

```text
winner_p3_30_p4p3_20_p4p5_40_tp14_10
turn3p3=0.30, turn4p3=0.20, turn4p5=0.40, age210tp14=0.10
rebalance: w90_tv0.045_max10_ddh-0.04
```

Base validation:

| Venue | Return | MAR | DD | Worst day | Avg scale |
|---|---:|---:|---:|---:|---:|
| Bybit | +226.22% | 6.18 | -11.69% | -5.57% | 7.47 |
| Binance | +142.48% | 6.01 | -7.71% | -6.26% | 7.98 |

2x-cost validation:

| Venue | Return | MAR | DD | Worst day | Avg scale |
|---|---:|---:|---:|---:|---:|
| Bybit | +150.51% | 4.02 | -11.94% | -5.73% | 7.24 |
| Binance | +94.27% | 3.24 | -9.46% | -6.33% | 7.68 |

Verdict: accepted as the current best continuous **research lead**, not paper-ready
evidence. It clears the user's base-cost target on both venues and is not immediately
destroyed by 2x costs, but it is grid-selected and still depends on modeled impact. It
needs robustness diagnostics, exact raw rerun if possible, and forward demo/paper before
any deployment-style claim.

### Downtrend regime extension

Formal downtrend extension completed 2026-06-08:

- `C:\Users\user\SHARED_DATA\continuous_downtrend_regime_extension_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_regime_extension_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_cost_robust_frontier_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_uptrend_weight_robustness_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_uptrend_weight_robustness_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_early_regime_stabilizer_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_early_regime_stabilizer_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_uptrend_component_filter_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_uptrend_component_filter_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_partial_tp14_filter_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_partial_tp14_filter_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_market_context_component_filter_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_market_context_component_filter_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_microcontext_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_microcontext_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_scale40_risk_polish_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_downtrend_scale40_risk_polish_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_scale_window_interpolation_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_scale_window_interpolation_cost200_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_tp14_stress_repair_dt40w70_base_2026-06-08`
- `C:\Users\user\SHARED_DATA\continuous_tp14_stress_repair_dt40w70_cost200_2026-06-08`

The uptrend-only weighted ensemble structurally could not trade BTC downtrends. The
downtrend scout found that raw downtrend shorts were weak, but the stricter `turn4_pop5`
stream became useful when gated by causal 24h positive premium-index mean. Current best
research lead:

```text
winner_up_p3_30_p4p3_20_p4p5_30_tp14_20_plus_dt40_turn4p5_premium_decomp_rebalance_w70_tv45_max10_dd4
uptrend sleeve: turn3p3=0.30, turn4p3=0.20, turn4p5=0.30, age210tp14=0.20
downtrend add-on: 40% * dt_turn4p5, premium_24h_mean >= 0
rebalance: w70_tv0.045_max10_ddh-0.04
```

Base validation:

| Venue | Return | MAR | DD | Worst day | Green months | Downtrend trades |
|---|---:|---:|---:|---:|---:|---:|
| Bybit | +265.24% | 7.50 | -11.28% | -5.57% | 31/38 | 85 |
| Binance | +190.87% | 6.84 | -9.06% | -6.33% | 29/36 | 91 |

Pooled base result: min return +190.87%, min MAR 6.84, worst DD -11.28%, and
common both-venue green months improved from 20/35 on the uptrend-only winner to
28/38.

2x-cost stress:

| Venue | Return | MAR | DD | Worst day | Green months |
|---|---:|---:|---:|---:|---:|
| Bybit | +177.28% | 5.11 | -11.07% | -5.72% | 28/38 |
| Binance | +134.13% | 4.85 | -8.98% | -6.40% | 27/36 |

Pooled 2x-cost result: min return +134.13%, min MAR 4.85, worst DD -11.07%,
and common both-venue green months 24/38. The plotted equity curve is:

```text
C:\Users\user\SHARED_DATA\continuous_scale_window_interpolation_base_2026-06-08\winner_scale40_w70_equity_curve_base_and_cost200.png
```

Verdict: scale/window interpolation accepted the 40% downtrend add-on with a 70d
vol window as the new strict continuous research winner. It improves base min
return, base min MAR, base common green months, 2x min return, and 2x min MAR
versus the prior 30%/90d strict winner while preserving 2x common green months at
24/38. Worst-DD differences are float-noise equal under the repo's numerical
equivalence rule. This is still research-only, not paper-ready and not promotion
evidence.

Premium-threshold follow-up rejected stricter thresholds (`premium_24h_mean >=
0.0001/0.00025/0.0005/0.001`) as replacements. They improved some stressed rows
but lost the base min-MAR/trade/monthly constraints. Keep the sign-only premium
filter.

Risk-retarget follow-up rejected lower max-scale / earlier drawdown half-scale
rules as replacements. They reduced drawdown but failed the base return/MAR rule.
Lower target-vol settings did not change the accepted max-10 row because scale was
max-bound.

Early-regime stabilizer follow-up rejected 72h downtrend positioning filters and
5/10/20d soft strategy-equity throttles as replacements. The best 72h premium
rows bought one extra common green month (28/38) but cut base min MAR to 6.08
and 2x-cost min MAR to 4.08. The named stress months (2023-07 Binance,
2023-12 Bybit, 2024-12 both venues, 2025-04 both venues) were unchanged, so the
next useful target is component-specific uptrend filtering/reshaping, especially
the noisy `turn4p5` source, not more downtrend add-on gating.

Component-specific uptrend filters were also tested. Hard-filtering `turn4p5`
by premium/funding cut too much return and MAR. Full replacement of
`age210tp14` with a premium-positive `tp14_prem24` source improved common green
months to 29/38 and cut drawdown, but failed the replacement bars on base return
and 2x-cost MAR/return. A partial follow-up found the best risk-stability lead:

```text
u_tp14f15_up_plus_dt0.3_dtgrid_turn4p54
uptrend: turn3p3=0.30, turn4p3=0.20, turn4p5=0.30,
         age210tp14=0.05, tp14_prem24=0.15
downtrend/risk unchanged
```

`u_tp14f15` improves base min MAR from 6.37 to 6.57, worst DD from -11.28% to
-10.19%, worst day from -6.33% to -4.73%, and 2x min MAR from 4.29 to 4.30,
while keeping base common green months at 27/38. It is not accepted as the new
winner because min return falls from +166.49% to +158.09% and it improves only
two of the six named stress checks; 2024-12 and 2025-04 get worse. Treat it as
a risk-stability lead, not a replacement.

Market-context component filters were tested next: partial `age210tp14` and
small `turn4p5` replacements gated by causal trailing 24h alt-market mean return,
alt-market breadth, or BTC 24h return. They improved smoothness but failed the
replacement bars. Best green-month row `u_tp14m15` raised base common green months
to 28/38 and reduced DD to -10.22%, but cut min return to +152.39%, base min MAR
to 5.97, and 2x min MAR to 3.86. The useful diagnostic was `u_tp14btc15`: it fixed
2023-12 Bybit (-6.36% to -4.17%) and cut DD to -9.21%, but min return fell to
+136.25% and 2x min MAR to 3.60. Keep the current winner unchanged.

The same TP14 stress-repair idea was retried under the accepted 40% downtrend /
70d risk-window engine. It still failed as a replacement. Control stayed at base
min return +190.87%, min MAR 6.84, worst DD -11.28%, both-green 28/38, and 2x
min return +134.13% / min MAR 4.85 / both-green 24/38. The best 2023-12 repair,
`u_tp14btc15`, improved Bybit 2023-12 from -6.36% to -4.17%, but cut base min
return to +139.50%, base min MAR to 3.72, and 2x min MAR to 4.17. The milder
`u_tp14btc05` held base both-green at 28/38 and improved 2x both-green to 26/38,
but still cut base min return to +164.20%, min MAR to 4.54, and worsened base DD
to -11.75%. Keep the current 40%/70d winner unchanged; TP14 filtering is only a
stress diagnostic.

Raw OI/taker-flow feasibility was checked before the next downtrend run. Bybit OI
is full-history (`2021-01-01` to `2026-05-27`), but Binance OI and taker-flow only
cover late 2026-04 to 2026-05, and Bybit has no taker-flow folder. These are not
valid cross-venue historical filters yet; use them only as forward diagnostics
unless Binance historical coverage is rebuilt.

Downtrend micro-context filters (`premium + market/BTC short-window context`) did
not beat the simple sign-only premium filter. The useful discovery was that the
plain 40% downtrend add-on is a near-miss: base min return +176.53%, base min MAR
6.33, base common green months 28/38, 2x min return +122.90%, and 2x min MAR 4.45.
It missed the base min-MAR replacement bar by about 0.04, so a tight risk-polish
grid was run.

The risk-polish grid first found the strongest aggressive risk-return lead:

```text
aggressive_downtrend_scale40_w60
uptrend: turn3p3=0.30, turn4p3=0.20, turn4p5=0.30, age210tp14=0.20
downtrend add-on: 40% * dt_turn4p5, premium_24h_mean >= 0
rebalance: w60_tv0.045_max10_ddh-0.04
```

Base: Bybit +263.96% / MAR 7.47 / -11.28% DD / 31-of-38 green months; Binance
+187.78% / MAR 6.73 / -9.06% DD / 28-of-36 green months. 2x cost: Bybit
+175.80% / MAR 5.07; Binance +131.49% / MAR 4.76. It is rejected as a strict
replacement only because 2x common both-green months fall from 24/38 to 23/38.
Keep the 30% row as the strict default winner, but track the 40%/60d row as the
aggressive risk-return lead. Equity curve:

```text
C:\Users\user\SHARED_DATA\continuous_downtrend_scale40_risk_polish_base_2026-06-08\aggressive_scale40_w60_equity_curve_base_and_cost200.png
```

The follow-up scale/window interpolation recovered the missing 2x common-green
month: `0.4` downtrend scale with `w70_tv0.045_max10_ddh-0.04` improved the 60d
lead's 2x common-green count from 23/38 to 24/38 while retaining materially better
return/MAR than the previous 30%/90d winner. This supersedes the 40%/60d row as
the current strict research winner.

### 2026-06-09 — rmom latency falsification: FAIL (methodology debt #3 verdict)

The pre-registered latency-delay falsification
(`docs/preregistration/rmom-latency-falsification-2026-06-09.md`,
`scripts/rmom_latency_falsification.py`) ran the merged-candidate selection layer
(q25/liq500k/btcup/turn3_pop3/age240/h24) with rmom rebuilt at shifts {2,3,4,5,7} —
residuals computed once, 100.0% exact replication of the production shift3 table,
production parquets restored after.

Pooled short-stream MAR by shift: 0.67 (s2, 1h-leak diagnostic) → **1.13 (s3,
control)** → 0.10 (s4) → −0.01 (s5) → −0.16 (s7). Bybit return sign-flips at s4.
Per-day selection Jaccard vs control: ~0.56 at ±1 day.

**Verdict: FAIL.** The rmom edge is a knife-edge at exactly the freshest legal
staleness — not a 7-day momentum factor. shift2 (more information) being WORSE argues
against a simple leak, but a noise-peak/timing-artifact reading fits everything: the
edge has zero operational margin (one day of data delay live reproduces shift4 = dead).
Consequences (binding, pre-registered): rmom supports NO deployment-grade claim; the
frozen continuous winner rests on a boundary-concentrated feature and is downgraded
accordingly; any future continuous promotion requires resolving debts #3+#4 at the
data layer first (factor/residual day-grid alignment audit, then re-test). The
deployed SHORT is unaffected (rmom inactive, sentinel 10.0).

## 2026-06-09 — Continuous window freeze (binding)

The 2023-04→2026-05 window is declared **spent** for continuous variant
adjudication. Rationale: the continuous winner is the product of a long
selection chain on a single window — component weight simplex → retarget grid →
downtrend add-on → premium gate → scale/window interpolation — with the
replacement bars themselves computed on the same window. After that much
selection, the reported MAR (6.8–7.5 base) is an order statistic, not an
expectation; the honest forward prior is heavy shrinkage toward the unweighted
base (~MAR 4) or below. Every additional sweep on this window makes the number
we will eventually trust *worse*, because it spends researcher degrees of
freedom without adding any out-of-window information.

Binding consequences (mirrored in STATE.md):

1. The 40%/70d downtrend-extended ensemble is frozen as-is. (A same-day parallel
   session's pre-registered fragility receipt DEMOTED it and re-anchored the
   canonical to the uptrend core @ max4-6 — see the regime-robustness section
   below; the freeze governs everything after these completed decisions.)
2. No further accept/reject sweeps, weight tweaks, filter frontiers, or
   risk-rule retargets on this window.
3. New continuous evidence = forward no-order paper only.
4. Exempt: methodology-falsification/causality audits (they can only kill the
   line, not improve it) and bug-fix re-runs of the frozen winner.

*(The following regime-robustness program ran in a PARALLEL session the same day, without awareness of the freeze above. Its receipts stand as pre-registered records; its continuous results inherit the rmom latency-knife-edge caveat; the freeze binds all future continuous adjudication including any revival of its Tier-1 leads.)*

### Regime-robustness program (2026-06-09) — demotion, re-anchor, RS-gate null

The 2026-06-09 sessions (commit 5e1c960 + working tree) closed the refinement era
and opened the regime program (`docs/research_plan_continuous_regime_2026-06-09.md`):

- **Winner robustness battery (5/5 PASS, pre-registered):** the uptrend ensemble
  `winner_base = {turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}` @ w90
  is a robust plateau, not weight-overfit — all 286 simplex vectors both-venue
  positive; survives de-lever to max4 (bybit +84%/MAR 5.0, binance +60%/4.6) and 3x
  cost; all sub-periods positive. `tv` is a dead knob (scale pins at `max_scale`).
  Real residual risks: recent-tilt, uncalibrated impact at max10, funding-interval
  debt, no forward OOS. Receipt:
  `docs/preregistration/continuous-winner-robustness-2026-06-09.md`.
- **Downtrend extension DEMOTED (WP2):** the 7.50/6.84 downtrend-extended headline
  rests on a fragile sliver (85/91 trades on ~10 active days; `dt_scale=0.4` at a
  cliff — binance collapses to MAR 3.3/-17% DD at a=0.7). Canonical re-anchored to
  the uptrend core at max4-6. Receipt:
  `docs/preregistration/continuous-demote-downtrend-extension-2026-06-09.md`.
- **Refinement levers tapped out:** parsimony (DoF-only), carry/funding (rank-IC
  +0.04/+0.06), multi-horizon (bybit-only; 24h is the cross-venue horizon),
  conviction-by-score (weak), entry circuit-breaker (null on component pool),
  rmom-gate loosening (0.25 optimal; looser adds correlated breadth, blows out DD).
  Ridge within-pool combiner rejected at Tier-1 (negative OOF IC; receipt
  `docs/preregistration/ridge-combiner-2026-06-09.md`).
- **WP1a alt-RS squeeze probe (pre-registered, NO-GO):** trailing EW-alt-minus-BTC
  relative strength does NOT predict forward squeezes — primary Spearman ICs
  +0.004..+0.061 (bybit) / -0.010..+0.055 (binance) vs the a-priori <= -0.08 bar;
  all sensitivities agree; squeeze days at the 51st RS-percentile. The mechanism is
  real but CONTEMPORANEOUS: same-day RS vs per-unit book return -0.26/-0.30 (raw
  alt-market return -0.30/-0.36), while alt-RS itself is a daily martingale
  (AR1 +0.02/+0.03; trailing RS does not predict next-day RS). Conclusion: the
  alt-season exposure cannot be timed at daily granularity — gate forms (WP1b) are
  dead a-priori; the treatment is a HEDGE. Receipt:
  `docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md`; artifacts
  `~/SHARED_DATA/continuous_rs_probe_2026-06-09/`.
- **BTC-beta hedge BANKED (WP3, two pre-registered stages, both PASS):**
  - *Stage-A instrument comparison (PASS 6/6, overlay-level):* btc vs alt_ew vs
    alt_top10, causal 90d beta, long-only, REAL funding charged. BTC selected — the
    only instrument improving MAR on both venues (ΔMAR +0.34/+1.07); alt_ew (highest
    ΔSharpe +0.34/+0.51) confirms the WP1a mechanism ceiling but is non-tradeable
    and worsens bybit MAR/DD; alt_top10 dominated. Quirk: alt funding was
    net-receivable for longs on book-on days. Receipt:
    `docs/preregistration/continuous-hedge-overlay-2026-06-09.md`.
  - *Stage-B through the engine (PASS 8/8, engine-grade `candidate`):* hedge leg
    integrated into `apply_rebalance_rule` (`ContinuousHedgeRule(w90/min60/cap2)`,
    causal beta on trailing ledger days, DD-half state on HEDGED equity, gap
    close/reopen turnover; unhedged path byte-identical; 8 tests). Controls
    reproduce the benchmark bit-close. Binding max4: ΔMAR +0.50 bybit / +1.07
    binance, ΔSharpe +0.233/+0.382, 2023-24 Sharpe +0.436/+0.627. Survives 2x/4x
    hedge cost, funding-off, 60-150d window grid, 1-day beta latency, and 2x BOOK
    cost — where the hedge helps MORE (ΔMAR +0.89/+1.03, bybit DD +0.58pp better).
    At max10: hedged MAR 8.39/8.17 vs 6.17/6.00. Mean hedge ~4-6% of equity (max4).
    Receipt: `docs/preregistration/continuous-hedge-engine-2026-06-09.md`; artifacts
    `~/SHARED_DATA/continuous_hedge_{overlay,engine}_2026-06-09/`.
  - Durable claim = REGIME-ROBUSTNESS (the recent-tilt flattens, 2025 unchanged);
    part of the raw return gain is bull-sample-specific long-BTC drift. In-sample
    candidate — Tier-2 ceiling; forward demo is the only Tier-3 arbiter. Remaining
    (operator): live hedge-leg executor plumbing + forward-demo accumulation.
- **Live-readiness R0+R1 (2026-06-09, operator full-authority mandate):** the binance
  funding-interval debt is CLOSED for the continuous path (accrual verified vs raw
  datasets 40/40 to 5e-20; receipt `continuous-funding-debt-closure-2026-06-09.md`),
  and the walk-forward causal-allocator falsifier KILLED the weight-overfit concern
  (pre-registered): causal-chooser OOS haircut 13.8% (≤15% bar), equal-weight matches
  the fixed winner OOS (pooled Sharpe 2.334 vs 2.317 → weights not load-bearing), and
  adaptive re-weighting actively hurts (wandering choices + switch costs). Live weight
  policy: FROZEN receipt weights, no re-estimation. Receipt:
  `continuous-walkforward-allocator-2026-06-09.md`. Program doc:
  `docs/research_plan_continuous_live_readiness_2026-06-09.md`.

## Rejected Continuous Ideas

Do not re-run these unless there is a new reason:

- Hard stop losses as currently implemented.
- Failed-fade loss exits for independent continuous.
- Breakeven arm exits.
- MFE giveback exits.
- Rank-decay exits as primary edge.
- Crowd caps above 2 as default.
- Drawdown-zero threshold in the old rebalance candidate; it slightly helped one stress
  metric but hurt base-cost Binance and robustness.
- Giveback-style entry triggers (`turn*_gb*`, `pop*_gb*`); they reintroduced ugly tails.
- Trailing alt-vs-BTC relative-strength entry gates at daily granularity (hard gate,
  size-down, BTC-30d replacement): alt-RS is a daily martingale, so trailing RS cannot
  forecast the squeeze it causes contemporaneously (2026-06-09 WP1a, pre-registered NO-GO).
- Weak-market skip; it reduced opportunity and did not improve the profile.
- Funding, premium-index, or mark-index-basis hard filters on the merged continuous
  stream; they lost MAR versus the unfiltered stream in the 2026-06-08 frontier.
- The old coarse 40% downtrend add-on with the 90d risk window as default; it had
  higher return but missed the base min-MAR bar before scale/window interpolation.
- Stricter downtrend premium thresholds as the default; they over-filtered and failed
  the base replacement rule.
- Current-winner risk retargeting with lower max scale or earlier half-scale; it
  protected drawdown but lost too much return/MAR.
- Short-window soft strategy-equity throttles on the current continuous winner;
  they did not improve the accepted profile in the 2026-06-08 stabilizer scout.
- 72h downtrend premium/funding/basis filters as replacements; they added one
  green month at best but failed the MAR and 2x-cost bars.
- Hard-filtering the full `turn4p5` uptrend component by premium/funding; it cut
  too much return and MAR.
- Full or partial premium-positive `age210tp14` replacement as the default
  winner; `u_tp14f15` is a useful risk-stability lead but misses the stress-month
  replacement rule.
- Market-context hard filters on partial uptrend components; they smooth some
  months but cut Binance return/MAR and fail the base plus 2x replacement bars.
- Downtrend micro-context filters using premium plus short-term market/BTC state;
  the simple sign-only premium filter remained better.
- The aggressive 40%/60d downtrend add-on as strict default; it missed the 2x
  common-green replacement bar by one month and was superseded by the 40%/70d row.
- TP14 stress-repair replacements under the accepted 40%/70d engine; they fixed
  2023-12 Bybit only by giving up too much broad return/MAR or base drawdown.
- Pure retargeting of the hand-picked ensemble; it reached return but stalled at min
  MAR 5.21, so the final improvement came from component weighting, not another DD knob.

## 2026-06-09 — BTC-trend gate Tier-2 validation: DEMO-ELIGIBLE

The deployed `btc_trend_gate=uptrend` (operator-directed 2026-06-04, validation was
PENDING) got its binding Tier-2 battery: gate off vs uptrend, both venues, full-PIT
clean, exact deployed profile. Receipt with full numbers:
`docs/preregistration/btc-gate-tier2-validation-2026-06-09.md`.

Verdict line: `by MARΔ +1.52  bn MARΔ −0.12  pooled +0.70 → DEMO-ELIGIBLE`.

Read: on Bybit the gate is a genuine risk transform (DD −15.6%→−7.2%, MAR 1.38→2.89,
bootstrap P(MAR Δ>0)=98%, all thirds positive) at the cost of ~4% of return. On
Binance it is a coin flip (P=59%) that cuts return by more than half — the gate is NOT
cross-venue evidence of regime alpha; it is venue-fit risk engineering for the deployed
Bybit book. A run-quality note: the first battery attempt was TAINTED
(PIT_SURVIVORSHIP) by a single missing kline partition (WDCUSDT 2026-05-29; manifest
row carried a `bybit_v5_listing` sentinel URL the downloader can't fetch); it was
killed, the partition backfilled from the public archive, and the battery re-run clean.
The same sentinel-URL failure mode can silently break future backfills — worth a
downloader fix.

## 2026-06-09 — Levered long-sleeve stress (the live 10x multiplier vs 1x evidence)

The live long sleeve applies `notional_multiplier=10 / entry_leverage=10` on top of the
1x-validated v11a+div profile. All promoted evidence is the 1x curve. The levered read
(`scripts/long_sleeve_10x_stress.py` over the 1x backtest ledger, 189 trades
2023-06→2026-05; report `.../reports/equity_curves/long/levered_stress_10x.md`):

| metric | 1x (evidence) | 10x (live config) |
|---|---:|---:|
| max drawdown | −8.4% | **−60.5%** |
| worst single exit-day | −2.28% | −22.8% |
| funding P&L (sum) | −1.68% | −16.8% |

Key findings:

1. **Zero isolated liquidations**: no trade's intratrade MAE (computed from klines; the
   ledger's mae column is NaN) reached the −9.5% 10x liq distance — the ~−5% ATR stops
   protect single names. No near-misses either (≤80% of liq distance: 0).
2. **But the stop→liq cushion is thinner than one hourly bar**: in 139/189 trades a
   single bar's open→low range exceeded the stop→liquidation cushion — if such a bar
   arrives with price just above the stop, the fill is at/past liquidation. Gap risk is
   structural on FC names (entered *because* they pumped ≥15%).
3. **Margin exhaustion**: peak 8 concurrent positions → peak account gross leverage
   **11.6x**, peak margin demand **116% of equity** (5 events >100%). The live book
   cannot replicate the backtest at 10x — entries would be rejected at the margin
   boundary, so the levered backtest curve is not executable as modeled.
4. **Correlated-crash wipeout**: at peak gross leverage a uniform **−8.6%** book move
   ends the account. FC entries cluster in same-day FOMO names; crypto-wide flash-crash
   days of that size occur in-window. Median in-market gross leverage is only 2.5x —
   the danger is episodic concentration at peaks, which coincide with peak-FOMO markets.
5. The 10x exit-compounded "return" (+12,023%) is a linear fantasy — it ignores (3) and
   (4); do not cite it.

Caveat: ledger vintage is the 2026-06-05 equity-curves run (pre PIT-gap fix,
current-universe-labeled). The risk *shape* is robust to that; the headline 1x return is
not the point here.

**Recommendation to owner**: either cap live concurrency/notional so peak gross
leverage stays ≤ ~5x (wipe threshold −20%), or produce levered forward-demo evidence
with margin telemetry before trusting any 10x extrapolation. The 1x evidence does not
transfer linearly to 10x.

## 2026-06-09 — Book-level regime concentration (one trade, three wrappers)

`scripts/book_regime_concentration.py` over the fresh ungated short baseline ledger
(btc_gate_tier2 00_baseline) + the long ledger, window 2023-04→2026-05 (1,153 days).
Report: `.../btc_gate_tier2_2026-06-09/00_baseline/book_regime_concentration.md`.

- SHORT regime-on (btc_ret30>0, lag1): **57%** of days. LONG regime-on (BTC&ETH>SMA30):
  40%. Both deployed sleeves simultaneously on: **37%**; P(long_on | short_on) = 65%.
  The frozen continuous uptrend ensemble shares the short indicator.
- **The 37% both-on days carry ~82% of total book P&L** (+102.9% of ~+126% combined at
  1x). The book is, quantitatively, one BTC-regime trade expressed three ways.
- Regime flips in-window: 107 (short) / 95 (long) — the 30d-return-sign indicator
  chatters; many flips are day-scale whipsaws, so effective independent regime
  observations are well below 107 but well above the naive "one bull cycle = 2-3".
- **Regime-transition chop is where the short bleeds**: the 2025-04 flip cluster shows
  −5.8%/−6.9%/−9.4%/−5.8%/−7.4% 11-day short P&L windows around successive flips. The
  deployed uptrend gate removes downtrend entries but cannot remove boundary chop (the
  gate itself flips late by construction).
- The "10 worst BTC days" joint table shows no booked blowups, but it books P&L on
  EXIT days — open-position marks on crash days surface later; treat that table as a
  lower bound on crash-day pain, not proof of immunity.

Consequence: portfolio-level risk work should treat "BTC-uptrend-on" as THE factor
exposure of the whole program; diversification claims between sleeves measured on
full-sample daily correlation overstate independence (see long-sleeve-diversifier note).

## 2026-06-09 — Funding-interval debt quantified (methodology debt #1 closed-as-scoped)

Empirical audit of both roots' funding datasets vs the code's assumptions.

**Data-layer facts:**

- Bybit `funding`: 764 symbols, 2.44M settlement rows — coverage is effectively
  complete. Empirical modal settlement interval per symbol: 349 @8h, 322 @4h, 17 @2h,
  **76 @1h**. By 2025-2026, **76-86% of settlement rows are ≤4h-spaced**.
- Binance `binance_usdm_funding`: **only 51 symbols** have any funding data (39 @4h,
  10 @8h). This is a COVERAGE hole, not an interval bug — every trade on the other
  ~700 symbols books `funding_mode=missing` → zero funding.
- The stored `funding_interval_min` is **480 for every row on both venues** (the
  ingestion default), regardless of true interval.

**Code-path consequences:**

1. **P&L path: CORRECT since 2026-06-03.** `_funding_lookup` exact-stamp dedup counts
   every settlement row present in data; the old interval-bucketing undercount is fixed.
   Bybit funding P&L is therefore right; Binance funding P&L is right *only* for the 51
   covered symbols.
2. **Window-SUM features are correct** (`funding_24h_sum`, `funding_3d/7d_sum`, daily
   sums): because stored interval=480 makes `funding_rate_8h_equiv == funding_rate`,
   summing raw per-settlement rates over a window gives the true window funding. The
   2026-06-08 derivatives frontier's `funding_24h_sum` was computed correctly.
3. **Per-settlement LEVEL features are mis-scaled** for sub-8h symbols
   (`funding_rate_last`, `funding_recent`, `fs_recent_funding_min` thresholds): a 4h
   symbol's last-rate is ~half the true 8h-equivalent, 1h symbols ~1/8. **No deployed
   profile consumes these** (short funding gates at sentinel ±10; long funding-squeeze
   pattern disabled) — research-only exposure.
4. **Binance results are biased OPTIMISTIC by missing funding** — empirically, this
   strategy's shorts PAY funding on net (Bybit baseline ledger: funding_return sums to
   **−11.6%** across 596 trades, 595 fully modeled, vs +65.4% total net; post-pop
   shorts sit in negative-funding regimes). Missing Binance coverage therefore omits a
   real COST. The trade-level bias band is quantified in the Binance-gap section below.
   (The naive "shorts collect positive funding" prior is wrong for THIS event
   population.)

**Recommendations (data work, operator-schedulable):**

- Rebuild Binance funding coverage (`download-data --datasets funding` reach-back, or
  archive source) — this directly moves the cross-venue arbiter for the short sleeve.
- At ingestion, set `funding_interval_min` from the empirical per-symbol modal spacing
  (or instruments `fundingInterval*`) instead of defaulting 480, so `8h_equiv` level
  features become trustworthy before any research consumes them.

## 2026-06-09 — Binance gap decomposed (selection vs execution vs universe)

`scripts/binance_gap_decomposition.py` over the fresh full-PIT ungated baseline ledgers
(btc_gate_tier2 00_baseline, both venues; Binance window clipped at its data end
2026-04-30). Report: `.../binance_full_pit/.../00_baseline/binance_gap_decomposition.md`.

| | bybit | binance |
|---|---:|---:|
| trades / symbols | 596 / 272 | 307 / 182 |
| mean gross fade per trade | +0.167% | +0.101% (60% of bybit) |
| **shared-symbol** mean gross | +0.203% (316 tr) | +0.159% (252 tr) — **79% of bybit** |
| gross win rate | 61.9% | 57.3% |
| mean net per trade | +0.110% | +0.062% |
| sum net | +65.4% | +19.1% |

Verdict on the three hypotheses:

- **NOT pure Bybit microstructure**: on the like-for-like shared-name pool the fade
  transfers at ~79% of Bybit's per-trade gross with a comparable win rate. The
  cross-venue arbiter remains meaningful.
- **UNIVERSE composition is the biggest drag**: each venue's exclusive names are weaker
  than the shared pool (bybit-only names dilute bybit's mean too); Binance's 41
  exclusive traded names drag it hardest, and Binance fires only ~half the events.
- **EXECUTION costs are symmetric** (identical mean cost) — but the **funding hole is
  not**: 296/307 Binance trades book zero funding (coverage). Using Bybit's per-trade
  funding distribution as proxy, unbooked Binance funding ≈ **−5.8%** total (band
  −3.1%..+1.5%) → honest Binance sum-net ≈ **+13%**, i.e. the published +19.1% is
  ~30% optimistic. Risk-adjusted (ret/DD 1.10 vs 5.42) the venue gap stays severe:
  fewer, lumpier trades against a similar DD.

Actionable: (1) the Binance funding-coverage rebuild directly de-biases the arbiter;
(2) a shared-universe-only profile variant is a legitimate FUTURE pre-registerable idea
(both venues' exclusive names underperform the shared pool) — not run here.

## 2026-06-09 — Long-sleeve improvement program (structural sweep + TSMOM overlay)

Operator-directed improvement pass on the long sleeve ("better returns, better MAR,
fix the step-function curve"). Constraint honored: the 5-wave signal-family search is
exhausted (FC is the selection ceiling) — this program tested PORTFOLIO/EXECUTION
structure plus one literature-sourced new mechanism. All cells EXPLORATORY, full-PIT
clean, both venues, window 2023-04-01→2026-05-28 (`scripts/long_improve_sweep.py`,
`scripts/long_tsmom_overlay.py`; reports under `.../reports/long_improve_2026-06-09/`).

**Accepted candidate (receipt: `docs/preregistration/long-volup-candidate-2026-06-09.md`):**
`vol_target_max_scale` 1.0 → **1.25** — return +23.3%→+28.9% (bybit) / +19.0%→+23.6%
(binance) with ret/DD ≥ 99.6% of baseline and Sharpe unchanged on both venues.
Identical trade set — pure Moreira-Muir exposure timing (lever the calm regimes), the
symmetric half of the already-promoted div de-risking. Both 1.25 and 1.5 passed the
pre-registered rule; the pre-committed tie-break picked 1.25 because of the live 10x
leverage interaction. NOT deployed — operator sign-off required, and it should ride
with the leverage-cap decision.

**Clean nulls (do not re-run):**

- Candidate breadth (fc_top_volume_rank_max 10→20/30, daily-best-N): ret/DD collapses
  ~50-65% on both venues. The top-10-liquidity gate is load-bearing for FC quality.
- Hold extension (3d→7d), ATR trailing stop, scaled exits: all materially worse —
  the 72h time-stop is right; FC's edge is fully decayed by day 3.
- Pyramiding (max_per_symbol_concurrent 2): no-op (cooldown binds first).
- **Majors TSMOM/Donchian overlay**: literature-mined (Zarattini/Han/Man — published
  Sharpe 1.5+ spot, no funding). On 2023-26 perp data with REAL funding the anomaly
  survives but degrades to MAR 0.92 (sma50 best; published Donchian ensemble: 0.42),
  corr +0.35 to the FC stream — every blend weight DILUTES combined MAR
  monotonically (2.38 → 2.25 @ 10% → 1.73 @ 50%). Anti-finding from the same
  literature pass: post-listing drift is a SHORT phenomenon (89% of 2025 listings
  negative) — never a long candidate.

**The step-function curve was substantially an accounting artifact.** The engine books
P&L on exit only. Rendered as daily mark-to-market (every open trade marks daily;
costs+funding on exit day; same per-trade totals), the FC book's honest curve is
already continuous-ish: MAR 2.38, DD −8.5%, Sharpe(daily) 1.66, active 26% of days.
The right "fix" was rendering, not diluting the book with a weaker always-on sleeve.

## 2026-06-09 — Cross-listing filter: REJECTED (Tier-1 fail, with a sharpened insight)

Follow-up to the Binance-gap decomposition. Hypothesis: "listed on both venues at
decision time" is a PIT-computable quality proxy (single-venue listings = the scammier
tail). Ledger-level scout on tonight's clean cells (PIT cross-listing from the other
venue's kline coverage at entry date, 1000x-prefix symbol normalization, trailing-7d
window; ledger filtering is conservative — freed slots aren't reallocated):

| venue/cell | unfiltered MAR | cross-listed-only MAR | dropped P&L |
|---|---:|---:|---:|
| bybit baseline | 2.17 | 1.28 | +18.4% (165 tr) |
| bybit uptrend | 7.89 | 4.12 | +24.1% (105 tr) |
| binance baseline | 0.52 | 0.75 | −2.3% (25 tr) |
| binance uptrend | 0.38 | 0.62 | −2.4% (14 tr) |

**Verdict: REJECTED as a universal filter** — helps Binance, materially hurts Bybit
(Tier-1 fail: MAR Δ −0.89 / +0.23). The outcome-side proxy in the gap decomposition
("traded on both venues") had conditioned on the name also EVENTING on Binance — a far
stronger condition than being listed there. Sharpened conclusion: **Bybit-exclusive
listings carry real, diversifying edge; only Binance's own exclusive listings are
toxic.** A Binance-only gate would be venue-specific tuning and is not proposed.
Do not re-run as a cross-venue candidate.

## 2026-06-09 — Binance funding REBUILT + gate re-verified; ridge combiner REJECTED

**Funding rebuild (receipt: `docs/preregistration/binance-funding-rebuild-2026-06-09.md`):**
`binance_usdm_funding` rebuilt from the survivorship-free data.binance.vision monthly
fundingRate archives (+ fapi top-up): **51 → 697 symbols**, ~2.23M rows, TRUE
per-settlement intervals stored (1.26M of 2.23M rows are sub-8h — closing both halves
of the old funding debt). Old dataset kept as `.bak`. Re-measured Binance gate cells on
honest funding: baseline +18.7%→+14.2% (−4.5% abs, inside the predicted −3..−6 band),
uptrend +8.1%→+6.9%, trades unchanged. **Gate Tier-2 verdict re-checked and HOLDS:
binance MAR Δ −0.05 (better than the pre-rebuild −0.12), pooled +0.73 → DEMO-ELIGIBLE.**
All future Binance numbers use this basis; pre-rebuild Binance results are ~3-6% abs
optimistic on 3y windows.

**Ridge combiner (operator's pre-registered scout): REJECTED.** Bybit pooled
out-of-fold rank-IC **−0.04** (anti-predictive, 7 folds, coefficients stable);
Binance arm unmeasurable (0 folds — OI history there starts ~2026-04). The Tier-1
gate (positive IC both venues) fails; engine-sizing wiring does not proceed. A re-run
needs a Binance OI backfill (vision metrics archive reaches 2020-09) + a freshly
pre-registered feature set. Receipt: `docs/preregistration/ridge-combiner-2026-06-09.md`.

## 2026-06-09 — Day-grid alignment audit: GRID CORRECT (debt #4 closed)

End-to-end audit of the factor/residual day-grid timeline (the blocking prerequisite
for any continuous revival, and the prime suspect behind the rmom knife-edge):

1. **Producer verified.** Panel rows are start-of-day 00:00-UTC stamps; features at
   row D are EOD-of-D observables; `fwd_ret_1d` = first-bar close (D+1)01:00 →
   (D+2)01:00 via calendar-exact joins (gap days null, never misaligned).
   **Empirically recomputed from raw klines for sample (symbol, day)s: bit-exact
   match.** `residual_return[d]` therefore completes (d+2)01:00 as documented, and
   rmom[D]'s newest term completes (D−1)01:00.
2. **Consumers verified.** Continuous engine joins rmom[bar's own day] — causal for
   every hourly bar with ≥24h margin to the newest residual. The volume-events
   trading-day join consumes rmom one further day stale (extra-conservative;
   feature inactive in the deployed short anyway). The falsification driver's
   shift3 rebuild matched the production table 100.0% — pipeline self-consistent.

**Verdict: no off-by-one anywhere.** Consequences:

- The rmom latency-FAIL is re-interpreted: NOT leakage — a **genuine ultra-fast-decay
  idiosyncratic reversal effect** (alive at 23–47h staleness, dead past 47h). The
  binding consequence is unchanged (zero operational margin for a daily system; rmom
  still supports no deployment-grade claim), but the mechanism is now understood.
- The residual-Sharpe machinery (Tier-3 gate input) is methodologically trustworthy.
- Methodology debt #4 is CLOSED; any future continuous revival now needs an
  intraday-class execution design (to consume a <2-day-half-life signal), not a
  data-layer fix.

## 2026-06-09 — Live-vs-backtest age definition audited (debt #2 closed-as-acceptable)

The two definitions: backtest `pit_age_days` = trading day − first archive-manifest
date; live `listing_age_days` = now − instruments `launchTime`. Quantified against a
live Bybit instruments snapshot × the manifest (569 symbols compared):

- **Median divergence 0 days; 564/569 within ±3 days.** The definitions agree for
  essentially the whole universe.
- 5 outliers, ALL with live age YOUNGER (launchTime resets on contract
  relaunch/migration: ETH/SOL/UNI/SUSHI — ancient either way, no gate impact — and
  FHEUSDT: backtest 419d vs live 153d, the known 54-day archive-gap name).
- **Exactly 1 symbol flips the age-300 gate today (FHEUSDT), and in the SAFE
  direction: live skips a name the backtest would trade.** There is no case where
  live trades something the backtest's gate would reject.

Verdict: divergence is rare (≈0.2% of the universe) and conservative-by-direction.
Accepted as-is; no code change. If a relaunched major ever re-enters the young-age
band, the live gate errs toward skipping — acceptable for a fade strategy.

## 2026-06-09 — Combined-book construction (deployed short × volup125 long)

Daily-grid combination of the two real books (bybit, window 2023-04→2026-05): the
deployed gated short (exit-day booked, 3d holds) × the accepted long volup125
candidate (honest daily-MTM stream).

- **corr(short, long) daily = −0.03** — effectively zero.
- | book | total | ann | maxDD | MAR | Sharpe | worst day |
  |---|---:|---:|---:|---:|---:|---:|
  | short only (deployed) | +81.0% | +20.7% | −7.2% | 2.89 | 1.67 | −2.87% |
  | long only (volup125) | +28.2% | +8.2% | −3.6% | 2.25 | 1.68 | −1.66% |
  | **combined 30% short** | +42.9% | +12.0% | **−2.5%** | **4.87** | **2.40** | −1.33% |
  | combined 50% short | +53.3% | +14.5% | −3.8% | 3.82 | 2.20 | −1.56% |
  | combined 70% short | +64.1% | +17.0% | −5.1% | 3.30 | 1.94 | −2.08% |

Caveats (attach when citing): mixed booking conventions slightly flatter the short
side; in-sample, 1x research scale, bybit only; daily corr ≈ 0 does NOT remove the
regime-flip joint-tail exposure (see the regime-concentration section) — both sleeves'
crash risk clusters at BTC-trend transitions. The split row is a research
illustration, not a deployment instruction; the live netted account currently runs no
explicit budget split (margin-budget feature shipped OFF). Notable: the combined-book
case strengthens the volup125 sign-off — the long candidate composes with the short
book at near-zero daily correlation.

**Binance addendum (same session, honest refunded ledgers):** corr(short, long) =
**−0.037 — the near-zero correlation REPLICATES cross-venue** (structure, not a Bybit
artifact). But the venues mirror: Binance's strong leg is the LONG (MAR 1.80 vs the
refunded gated short's 0.17), so any fixed blend dilutes the stronger sleeve there
(30% short: 1.80→1.38). Cross-venue-robust conclusion: two structurally uncorrelated
sleeves, each venue's book dominated by its stronger leg; a single global split
prescription does NOT fall out of the data — venue-level sizing is an operator
risk-preference decision, not a fitted parameter.

## 2026-06-09 — Capacity quantified for the deployed short (Tier-3 input)

First explicit capacity computation (per-trade participation = per-trade notional
[C × gross/max_active = C/12] ÷ entry-day symbol turnover; gated cells, both venues):

| venue | entry-day turnover (median / p10) | C @ 1% participation (median trade / p10 thinnest) | C @ 5% |
|---|---|---|---|
| bybit | $9M / $1.9M | **$1.1M / $0.2M** | $5.6M / $1.2M |
| binance | $36M / $7.3M | $4.3M / $0.9M | $21.6M / $4.4M |

Read: the strategy shorts thin names BY CONSTRUCTION (the liquidity-migration event
selects them), so capacity is structurally small — disciplined (1%-participation)
capacity is **single-digit $M on Bybit**, with the thin tail binding well below $1M.
Tier-3's "capacity ≥ 10x deployment" passes comfortably at personal scale (≤$100k
deployment) and becomes binding around $0.5-1M deployment. The research configs'
$1M deploy-capital / 50bps impact assumptions sit right at this edge — cost-stress
results should be read with that in mind. This closes the "nobody has computed
capacity" gap in the Tier-3 checklist.

## 2026-06-09 — Funding-settlement-aware exits: NULL at the bound (don't build)

With full settlement timestamps now in data, scouted whether the short book pays
funding it could dodge by exiting just before a settlement: of the gated Bybit book's
+4.51% total funding cost (equity-rel, 3y, 339 funded trades), only **+0.55%** sits at
settlements ≤1h before exit (51 trades) — the hard upper bound on any exit-timing
recovery is ~0.18%/yr. Not worth an engine change; recorded so it's never mined.

## 2026-06-09 — PROPOSED (operator gate): intraday-class harvest of the fast-decay residual signal

Status: **design proposal only — nothing runs without operator greenlight + a fresh
pre-registration.** This is the one creative direction tonight's falsification work
actually EARNED, written down before it's forgotten.

**What we verified (not conjecture):** the residual-reversal signal (rmom family) is
real but lives at 23–47h staleness and is dead past 47h (latency falsification,
shifts 2-7, grid audited bit-exact). A daily-rebalanced system structurally cannot
harvest a <2-day-half-life signal with margin — that's WHY continuous failed, not
because the idio reversal doesn't exist.

**Design sketch (the only shape that fits the verified physics):**
- Decision cadence: hourly bar close (infrastructure EXISTS: the continuous engine is
  hourly; live WS klines + state caches are deployed).
- Signal: residual-return reversal computed on a rolling intraday grid — residuals
  vs the same 4-factor model, but fit on hourly/6h windows so the newest legal
  information is hours old, not 23h. (The day-grid machinery is verified correct;
  this is a finer grid, same construction.)
- Holding: 12–24h max (inside the verified decay window), TP/time exits only (hard
  stops are a documented null in this family).
- Costs are the killer risk: hourly-cadence shorts on thin alts at 45bps+ round trip
  — the EDGE PER TRADE must clear ~2x the daily system's because turnover doubles.
  The capacity ceiling (single-digit $M) gets TIGHTER, not looser.

**Validation plan (freeze-compatible):** ONE pre-registered backtest shot (no
iterative sweeps — design frozen on paper first, exactly like the ridge receipt),
then no-order paper collection. The 2023-26 window freeze covers continuous VARIANT
adjudication; a single pre-committed test of a structurally new design with
forward-first emphasis is the carve-out the freeze anticipated — but the operator
owns that call.

**Recommendation:** park until (a) the forward demo has produced its first
Tier-3-relevant evidence on the existing book and (b) the operator decides the
program wants a second research arc. Written here so the option is preserved with
its rationale, not re-derived from scratch later.

## Open Methodology Debts

These are still real and can move numbers:

1. **Binance funding — RESOLVED on both axes (2026-06-09, two parallel sessions).**
   COVERAGE: dataset rebuilt from data.binance.vision (51→697 symbols, true settlement
   intervals); pre-rebuild Binance numbers are ~3-6% abs optimistic (receipt:
   `binance-funding-rebuild-2026-06-09.md`). ACCRUAL: per-event arithmetic verified
   end-to-end vs raw datasets — 40/40 sampled continuous trades to 5e-20, interval-
   agnostic exact-stamp dedup (receipt:
   `continuous-funding-debt-closure-2026-06-09.md`).
2. **Age definition.** Live age and PIT backtest age may differ near threshold boundaries.
3. **Residual-momentum causality.** Rmom features must be proven causal at the decision
   timestamp before any deployment/paper-ready claim.
4. **Residual/factor day grid.** Factor decomposition day alignment must be audited before
   relying on residual Sharpe for real-money gates.
5. **Forward evidence.** Continuous-vs-daily forward comparison is immature locally; current
   common-window evidence is not enough to claim success.

## What To Keep In Repo

Keep only:

- this file;
- `STATE.md` for live operational state and binding decision rules;
- `docs/backtesting_errors_we_never_repeat.md`;
- `docs/parameter_pre_registration.md`;
- a minimal `docs/preregistration/_template.md`;
- only preregistration receipts that still represent an active deployment/promotion decision.

Everything else belongs in git history or `SHARED_DATA` artifacts, not as active repo clutter.

## Preregistration Policy Going Forward

Default mode is now:

- Exploratory sweeps: write a short dated result section here after the run.
- Formal candidate/promotion changes: create one concise preregistration receipt, then add the
  verdict back into this file and keep only the receipt if it remains binding.
- Failed exploratory branches: consolidate here and delete the receipt.

The repository should make the current conclusion obvious without reading dozens of stale
experiment files.
