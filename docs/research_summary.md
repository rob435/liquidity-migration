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

## Open Methodology Debts

These are still real and can move numbers:

1. **Binance funding interval.** Some Binance alts settle funding every 4h, but parts of the
   stack historically assumed 8h. Any Binance result with incomplete funding handling must be
   treated cautiously.
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
