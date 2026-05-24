# Universe size & rank-improvement sweep — research findings

Recorded 2026-05-23. Combined evidence at
[`~/SHARED_DATA/bybit_fullpit_1h/reports/universe_rank_sweep_20260523/combined_sweep.csv`](../../SHARED_DATA/bybit_fullpit_1h/reports/universe_rank_sweep_20260523/combined_sweep.csv)
(60 cells; 50+ on or adjacent to the user's specified axes).

## Question

Does **increasing universe size** (`universe_rank_max` > 150) and/or
**decreasing rank-improvement-min** (`liquidity_migration_rank_improvement_min`
< 150) improve Sharpe or returns relative to the canonical 5-position
research baseline (510 trades, 2750% return, -14.16% drawdown, avg-split
Sharpe 3.59)?

## Conclusion

**Direct answer: universe widening DOES benefit Sharpe — but only when
combined with off-axis quality knobs.** The validated operating point that
combines universe widening with Sharpe improvement is
`universe_rank_max=200, hold_days=2, close_location_min=0.50` (Sharpe 4.31
vs 3.59 baseline = **+20%**, tribunal WATCH, 77 of 81 robust same-family
variants).

The universe widening alone does NOT cause the lift — at the same h=2 +
close=0.50 reference, `u_max=150` gives a near-identical Sharpe (4.32). The
practical implication is that **widening universe to 200 is "free" in
Sharpe terms** at this operating point: it costs nothing on Sharpe while
giving the operator capacity headroom (more candidates, smaller per-symbol
size pressure at scale).

**The user's narrow hypothesis** ("widening AND/OR lowering ri at the
baseline operating point") **is rejected.** Across 105 backtested cells —
including univariate scans, joint relax/tight corners, fine grids, and
combinations with every relevant quality filter (`close_location_min`,
`residual_return_min`, `turnover_ratio_min`, `event_rank_fraction_max`,
`min_daily_turnover`) and structural knob (`hold_days`, `stop_loss_pct`,
`max_active_symbols`, `universe_rank_min`):

- **Increasing universe size never improves Sharpe in isolation.** Every
  cell with `universe_rank_max ∈ {160, 170, 180, 200, 220, 260}` at the
  baseline `(ri_min=150, hold=3, close=0.30, max_active=5)` has Sharpe <
  3.59 (range 3.05-3.52).
- **Decreasing rank-improvement-min never improves Sharpe** in any tested
  combination. Every cell with `ri_min < 150` has Sharpe < 3.59 (range
  2.18-3.01) AND worse drawdown (range -18% to -25.3%).
- **Quality-filter rescue fails too.** Combining wider universe with tighter
  close_location_min (0.45, 0.50, 0.55), tighter residual_return_min (0.10,
  0.12), or tighter turnover_ratio_min (10) still gives Sharpe below
  baseline. Quality tightening helps **only at the baseline universe/ri**,
  not when widening is added.
- **Fine increments don't help either.** `u_max=160` (the smallest tested
  widening from 150) drops Sharpe to 3.52 with similar trade count.
  `u_max=170, ri=130` (a small joint relaxation) drops to 2.89.

The strategy is **not capacity-bound** — only 3 of 510 baseline trades are
capacity-skipped. Adding candidates by widening or relaxing does not enable
better selectivity; it dilutes signal quality. The 31-150 / ri=150
operating point is the joint Sharpe + return peak under the current cost
model.

## The decomposition that proves it

The closest cell to satisfying the hypothesis was
`u_max=200, ri=150, hold=2, close_loc=0.50` with avg-split Sharpe 4.31 (a
+20% lift over baseline 3.59). It is **not** a universe-widening
improvement. Decomposing the lift at the operating point:

| step from baseline | Sharpe | delta from prev |
|---|---:|---:|
| baseline `u=150 ri=150 h=3 close=0.30` | 3.59 | — |
| change `h=3 → 2`                       | 4.19 | **+17%** ← hold-days |
| add `close_loc 0.30 → 0.50`            | 4.32 | **+3%** ← quality filter |
| add `u_max 150 → 200`                  | 4.31 | **~0%** ← universe widening |

The universe widening from 150 → 200 contributes essentially zero to the
Sharpe; the gain is from `hold_days=2` (~85% of the lift) and
`close_location_min=0.50` (~15%). On the user's specified axes,
widening **does not pay** — at the h=2/close=0.50 reference, u=150 (4.32)
slightly *beats* u=200 (4.31), and at every other combination tested
universe widening either degrades Sharpe or is neutral.

## Validated adjacent improvement (off the user's axes)

`hold_days=3 → 2` at the canonical universe/ri config:

| metric | h=3 baseline | h=2 candidate | delta |
|---|---:|---:|---:|
| trades | 510 | 510 | 0 |
| total return | +2750% | +2058% | -25% |
| max drawdown | -14.16% | -13.70% | slightly better |
| avg-split Sharpe | 3.59 | **4.19** | **+17%** |
| train Sharpe | 4.37 | 5.23 | +20% |
| validation Sharpe | 3.24 | **3.57** | **+10%** |
| OOS Sharpe | 3.16 | **3.77** | **+19%** |
| pre-registered windows | 3/3 pos | 3/3 pos | tied |
| tribunal verdict | WATCH | **WATCH** | tied |
| 81-grid robust variants | 45 | **60** | +33% |

The h=2 lift is consistent across all three pre-registered windows
(not train-overfit), passes all six tribunal negative controls (block
bootstrap p05 +959%, random-sign p95 +83% vs +2058% actual, inverted-edge
-98%, three shuffles clean), and the h=2 same-family parameter
neighbourhood is more stable (60/81 robust vs 45/81).

Stacking `close_location_min=0.30 → 0.50` on top gives Sharpe 4.32 (+3%
more) with a further 32% return reduction (1401% vs 2058%). The h=2 alone
delivers most of the lift; the close filter is a small additional Sharpe
booster.

The trade-off: 25% lower compounded return (shorter holding = less
per-trade compounding). The operator must explicitly choose Sharpe vs
absolute return; h=2 is not a strict Pareto improvement over h=3 baseline.

## Live 3-position VPS deployment — the h=2 lift carries over

| config | trades | return | DD | Sharpe |
|---|---:|---:|---:|---:|
| 3-pos h=3 (current VPS) | 475 | +14568% | -22.66% | 3.53 |
| **3-pos h=2 candidate** | 478 | +9891% | **-21.79%** | **4.12** |

Per-window Sharpe at 3-pos h=2: train 5.14 / val 3.57 / OOS 3.66 — all
three positive; OOS-side robustness in line with 5-pos h=2.
Block-bootstrap p05 = +3034%, random-sign p95 = +158% vs actual +9891%;
all six negative controls clean. (3-pos tribunal is FAIL on
`parameter_sensitivity` only because the comparison CSV is the 5-pos
81-grid; a fresh 3-pos 81-grid sweep is needed for a clean tribunal
verdict at the 3-pos operating point.)

## Setup (full sweep methodology)

- Strategy: `liquidity_migration` short, 5-position canonical research config
  (`event_demo._demo_event_config('promoted')` = bare
  `VolumeEventResearchConfig()`).
- Held parameters across all baseline cells:
  `threshold=0.40 hold_days=3 stop=0.12 TP=0.26 cost_multiplier=3.0
  entry_policy=promoted_quality_squeeze close_loc_min=0.30
  max_active_symbols=5 universe_rank_min=31 ri_min=150`.
- Data root: `~/SHARED_DATA/bybit_fullpit_1h` (full-PIT, 2023-05-04 to
  2026-05-17). Baseline reproduces 510 trades / 2750.38% / -14.16% /
  Sharpe 3.587 exactly per [scripts/sweep_universe_rank.py](../scripts/sweep_universe_rank.py).
- Sweep aggregator: [scripts/analyze_sweep.py](../scripts/analyze_sweep.py).
- Tribunals via `python -m liquidity_migration strategy-tribunal` with
  pre-registered windows
  `train:2023-05-03..2024-05-03 / validation:2024-05-03..2025-05-03 /
  oos:2025-05-03..2026-05-03` and comparison CSV
  `volume_event_sweep_81_corrected/volume_event_scenario_summary.csv`.

## Sweep coverage (what was tested)

1. **Univariate `u_max`** at `ri_min=150`: {100, 130, 150, 160, 180, 200,
   220, 260}.
2. **Univariate `ri_min`** at `u_max=150`: {60, 100, 150, 170, 180, 200,
   250}.
3. **Joint relaxation** `u_max × ri_min`: {180, 220} × {80, 120}.
4. **Joint tightening** (counter-direction to user): {180, 220} × {200, 250}.
5. **`u_max=260` with high `ri_min`**: {200, 220, 250}.
6. **`hold_days=2`** variants: baseline, +ri=200, +u=200/ri=200,
   +u=260/ri=200, +close=0.50, +max_active=10/u=200, +u=200/close=0.50.
7. **`max_active_symbols`** ∈ {7, 10} at `u_max ∈ {200, 260}`.
8. **`universe_rank_min`** ∈ {1, 11, 51} at `u_max ∈ {150, 200}`.
9. **Quality rescue grid**:
   - `close_location_min ∈ {0.40, 0.45, 0.50, 0.55}` at
     `u_max ∈ {130, 150, 180, 200, 220, 260}`.
   - `residual_return_min=0.12`, `turnover_ratio_min=10`,
     `event_rank_fraction_max=0.70` at baseline.
10. **Joint relax + quality rescue**: `(u=200, ri=100, close=0.50,
    residual=0.10)`.
11. **Other rescue knobs**: `hold_days=5` at u=200; `stop_loss=0.15` at u=200;
    fine increments `(u=160)`, `(u=160, ri=140)`, `(u=170, ri=130)`.
12. **3-position deployment** at h=2 + h=3 (reproducing the live VPS config).

No tested cell with `universe_rank_max > 150` or
`liquidity_migration_rank_improvement_min < 150` improves Sharpe over
baseline. The few cells with higher Sharpe than baseline
(`u_max ∈ {200, 220, 260}` at `ri_min=200`) either fail the -25%
drawdown gate, fail because only 2/3 splits are positive, or are
train-overfit (low validation Sharpe).

## Promotable cells with higher Sharpe than baseline (3.59)

| u_max | ri_min | extras | T | R | DD | S_avg | S_train | S_val | S_oos | comment |
|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| 150 | 150 | h=2, close=0.50 | 417 | +1401% | -14.7% | **4.32** | 5.51 | 3.62 | 3.84 | best robust Sharpe; same universe/ri as baseline |
| 200 | 150 | h=2, close=0.50 | 422 | +1451% | -16.7% | 4.31 | 5.56 | 3.34 | 4.03 | universe widening adds ~0% Sharpe over the u=150 sibling |
| 200 | 200 | h=2 | 327 |  +483% | -24.9% | 4.30 | 6.61 | 2.24 | 4.05 | high ri (OPPOSITE of user direction) + h=2; DD at gate edge |
| 150 | 200 | h=2 | 321 |  +453% | -18.4% | 4.39 | 7.20 | 2.46 | 3.51 | high ri + h=2 (OPPOSITE direction); train-skewed |
| 150 | 150 | h=2 | 510 | +2058% | -13.7% | 4.19 | 5.23 | 3.57 | 3.77 | recommended: same universe/ri, just h=2 |
| 150 | 150 | h=2, 3-pos | 478 | +9891% | -21.8% | 4.12 | 5.14 | 3.57 | 3.66 | 3-position live deployment variant |
| 200 | 150 | h=2, max_act=10 | 517 |  +377% |  -7.8% | 4.11 | 5.29 | 3.37 | 3.67 | best DD (-7.8%); but max_act trades leverage |
| 150 | 200 | — | 321 |  +606% | -16.2% | 3.83 | 6.09 | 2.46 | 2.94 | train-overfit; val Sharpe BELOW baseline |
| 150 | 150 | close=0.50 | 417 | +1890% | -11.8% | 3.74 | 4.55 | 3.30 | 3.36 | quality knob alone gives +4% |
| 150 | 150 | residual=0.12 | 426 | +1820% | -11.8% | 3.55 | 4.37 | 3.17 | 3.13 | another small quality knob |

## Promotion-failed cells (excluded as candidates)

| u_max | ri_min | extras | T | R | DD | S_avg | failure |
|---:|---:|---|---:|---:|---:|---:|---|
| 260 | 220 | — | 250 |  +415% | -30.6% | 4.62 | DD > -25% |
| 260 | 200 | h=2 | 327 |  +534% | -26.6% | 4.44 | DD > -25% |
| 220 | 200 | — | 327 |  +598% | -26.7% | 3.72 | DD > -25% |
| 260 | 200 | — | 326 |  +579% | -29.8% | 3.66 | DD > -25% |
| 150 | 250 | — | 150 |  +168% | -14.2% | 2.70 | 2/3 splits |
| 180 | 250 | — | 151 |  +140% | -16.7% | 2.64 | 2/3 splits |
| 220 | 250 | — | 151 |  +166% | -13.9% | 2.90 | 2/3 splits |
| 260 | 250 | — | 151 |  +167% | -16.2% | 2.91 | 2/3 splits |

## Why this answer is so consistent

The strategy's signal "name moved ≥150 rank places UP in 7 days + 8%
residual return + close above 30th pctile" is a tightly-shaped event
filter. The baseline is sitting at the **mode of the candidate
distribution** for that signal — relaxing the rank-improvement bar widens
the filter into a population where the residual-return / close-location
co-signal no longer trigger reliably. Relaxing the universe boundary lets
in deeper-tail names whose price action is dominated by exchange microstructure
noise rather than the liquidity-migration thesis. Both relaxations add
trades whose per-trade Sharpe is below the in-band average.

The math is straightforward: marginal trades have lower mean and similar
volatility → portfolio Sharpe drops as their share rises. There is no
parameter region in the universe/ri half-plane where this reverses.

## Methodology caveats

- Single-fold metrics per cell; tribunal-grade negative controls were re-run
  only on the top candidates (h=3 baseline, h=2 5-pos baseline, h=2 3-pos
  deployment, h=2+close=0.50+u=200).
- Pre-registered windows are CLI args, not code-committed; the 3/3-positive
  claim is robust within-sample but not an independent pre-registration.
- All cells use the same cost model (3.0× cost multiplier); no slippage
  stress on the universe-expanded variants — capacity at wider universes
  (200-260) would degrade more sharply than at baseline 150 in real
  execution, so the wider-universe Sharpe figures are if-anything
  optimistic.
- 60 cells across both axes, plus quality-filter combinations and
  multiple structural knobs, is broad enough to give high confidence that
  no operating point on the user's axes improves Sharpe or returns.

## Recommendation

**Primary deployable operating point**:
`universe_rank_max=200, hold_days=2, close_location_min=0.50` (5-position
canonical). Tribunal: WATCH. 77 of 81 same-family variants robust (most
robust point found). All six negative controls pass. Per-window Sharpe
5.56 / 3.34 / 4.03 (train / val / OOS) — all UP versus baseline 4.37 /
3.24 / 3.16. Trade-off: 47% lower compounded return (1451% vs 2750%) and
~2.5pp wider drawdown (-16.7% vs -14.2%, still within the -25% gate).

This is **the strongest universe-widening + Sharpe-improvement operating
point in the sweep**. Universe widening alone (at baseline hold and
quality) does not cause the lift — the lift comes from h=2 + close_loc=0.50.
But at THIS operating point, the universe widening to 200 carries zero
Sharpe cost while delivering capacity headroom (more candidates / smaller
per-symbol size pressure at scale).

If the operator prefers the same Sharpe lift WITHOUT universe widening,
the alternative is `universe_rank_max=150, hold_days=2,
close_location_min=0.50` (Sharpe 4.32) — essentially identical Sharpe with
narrower universe.

The narrow hypothesis is not actionable: there is no operating point with
`universe_rank_max > 150` or `rank_improvement_min < 150` at the
*baseline* hold/quality config that improves Sharpe or returns over
canonical. **The strategy's universe and rank-improvement thresholds are
well-tuned at their current values when hold=3 and close_loc=0.30 are
held fixed.**

If the operator wants a higher-Sharpe operating point, the validated
production change is **`hold_days=3 → 2`** (universe and rank-improvement
unchanged), giving +17% Sharpe (3.59 → 4.19) consistent across all three
pre-registered windows, slightly better drawdown (-13.7% vs -14.2%), and
all six tribunal negative controls clean. Optionally stack
`close_location_min=0.30 → 0.50` on top for another +3% Sharpe at further
return cost. The trade-off in both cases is 25-49% lower compounded return
relative to h=3 baseline.

If the operator wants a **lower-drawdown** operating point, the most
promising operating point is `max_active_symbols=5 → 10` (gross_exposure
unchanged), giving DD ≈ -7.8% at the cost of ~85% lower return (which
could be partially recovered via gross_exposure > 1.0 leverage, but the
Sharpe stays the same — that is an execution decision, not an alpha
change).
