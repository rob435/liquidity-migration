# Carry-hold quant review — 2026-07-28

Lane-1 review of `lane2_carry_hold_v1` on the corrected settlement-exact
scorer: mechanism restated abstractly, six falsifiable theses declared then
tested, a declared improvement grid, and the registration of
`lane2_carry_hold_v2` (sizing refinement; v1 keeps scoring untouched).
Everything here is **seen data** — selection, not evidence. The forward runs
grade both configs.

- Panel: `cross_venue_panel_v1` 2021-01-01..2026-07-17 (1,883 score days).
- Baseline (corrected v1, module path): 18.0 bp/day, t 2.31, Sharpe 1.02,
  total +958%, max DD −60.0%, MAR 0.97, one-way turnover 0.271/day.
  Attribution: funding +7.19 units, price −3.40, fees −0.40.
- Artifacts: `reports/carry_hold_quant_review_2026-07-28/` on the bybit
  full-PIT root (parquets, all grid outputs, thesis tables).

## 1. The mechanism, stated abstractly

Carry-hold **sells insurance to crowded shorts**. When a perp's funding
prints deeply negative, the short side is paying to stay short; the strategy
takes the other side of the crowd, collecting that premium plus the squeeze
convexity (forced short covering lifts price), and bearing two risks: the
beta and idiosyncratic downside of hated names, and the possibility that the
shorts are *right* (death spirals). Abstractly, then, it should:

- **earn** when crowding is broad and the crowd is wrong or forced out —
  squeezes, capitulation reversals, fear-priced regimes;
- **bleed** when the crowd is right — cascading risk-off where price falls
  faster than funding compensates — and idle when nothing is crowded.

The P&L identity per name-day is `funding received − price drag − fees`; the
strategy is long a portfolio of hated assets financed by the haters.

## 2. Theses → verdicts

Declared before the conditional tables ran (`ch_theses.py`, output in
`theses_output.txt`).

| # | Thesis | Verdict | Key numbers |
| --- | --- | --- | --- |
| T1 | Edge increases with breadth of deep-negative names | **CONFIRMED** | Active-day net by breadth quintile: Q1 10.3 bp (Sh 1.15) → Q5 92.8 bp (Sh 2.40). PIT-lagged breadth >3 names: 45.3 bp/day (Sh 1.60) vs ≤3: 12.1 (0.81). |
| T2 | Book bleeds on BTC-down days; a trend gate would help | **HALF-REFUTED** | Contemporaneous beta 0.44; BTC-down days −58 bp (price −105, funding +49). BUT PIT 30d-trend<0 days earn MORE (38.2 bp, Sh 1.69) than trend≥0 (6.4 bp, 0.38), in every era pair. A risk-off gate would destroy the edge: it is a fear-premium collector. |
| T3 | Per-print threshold mistargets 4h/1h names; daily-rate entry fixes it | **REFUTED — inverted** | The "missed" cohort (trailing < −30 bp/day, no print < −10 bp; 2,879 name-days) earns 0.6 bp/nd (t 0.02) vs held 72.2 (t 3.21); 2026: −45 bp/nd. Distributed carry marks chronic decliners; a single deep print marks acute crowding. The per-print gate is load-bearing. |
| T4 | Funding capture decays with spell age | **REFUTED** | Funding per held-day flat across spell days (135/128/135/117/135 bp). No time-based exit is justified. |
| T5 | Max DD is price-led and cascade-marked, hence gateable | **CONFIRMED (price-led) / REFUTED (gateable)** | −60% DD (2025-11-09→2026-03-28, 139d): funding +222% cum-bp while price −285%. All worst 7d windows are price-led with funding positive. But the DD lives inside the same trend<0 regime that pays best on average — no clean PIT gate exists. |
| T6 | Forward net is monotone in carry depth | **CONFIRMED at both ends** | By trailing daily rate: deepest decile +228 bp/nd; shallowest (≥ −12 bp/day, hysteresis-tail holdings) **−67 bp/nd**. Deep carry pays; decayed carry bleeds. |

Structural reads that fall out of T2+T5: the book's losses are
*contemporaneous beta during cascades*, funding never stops paying on the
way down, and the 2026-07-24 audit already measured that beta hedging
relieves only ~5% of this tail. The residual risk is the price of the
premium.

## 3. Declared improvement grid — all cells

Grid and selection bar declared before any cell ran (`ch_variants.py`).
Selection bar: beat corrected v1 on raw Sharpe AND MAR on the full AND bench
windows, flip no positive era, turnover ≤ 1.5× v1. Costs/universe/scoring
identical to the registered scorer; only the weight rule varies.

Full window 2021-01..2026-07 (script path; see §5 basis note):

| cell | net bp/d | t | Sharpe | max DD | MAR | one-way | bench Sh | bench MAR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 (baseline) | 18.0 | 2.3 | 1.02 | −60.0% | 0.97 | 0.271 | 1.21 | 1.43 |
| A trail-exit −10 bp/d | 17.9 | 2.3 | 1.02 | −59.7% | 0.97 | 0.270 | 1.22 | 1.44 |
| A trail-exit −15 | 17.6 | 2.3 | 1.01 | −60.2% | 0.94 | 0.270 | 1.21 | 1.41 |
| A trail-exit −20 | 17.5 | 2.3 | 1.01 | −57.9% | 0.97 | 0.272 | 1.22 | 1.49 |
| **B depth ref 40** | 17.9 | 2.4 | 1.06 | −53.9% | 1.12 | 0.248 | 1.28 | 1.72 |
| **B depth ref 60** | 17.6 | 2.4 | 1.08 | −51.1% | 1.18 | 0.231 | 1.32 | 1.87 |
| **B depth ref 80** | 17.6 | 2.5 | 1.11 | −48.3% | 1.28 | 0.217 | 1.36 | 2.05 |
| C breadth-tilt ≤2 | 15.3 | 2.2 | 0.98 | −62.5% | 0.79 | 0.238 | 1.13 | 1.11 |
| C breadth-tilt ≤3 | 14.1 | 2.2 | 0.96 | −66.0% | 0.69 | 0.221 | 1.10 | 0.96 |
| C breadth-tilt ≤5 | 11.6 | 2.1 | 0.93 | −59.3% | 0.64 | 0.190 | 0.98 | 0.77 |
| D daily-rate entry −30/−9 | 11.6 | 1.4 | 0.62 | −65.0% | 0.34 | 0.312 | 0.64 | 0.36 |

- **A (exit tighten)**: flat. The T6 tail bleed is real per name-day but
  binary exits don't help the book — removal loses re-entry participation
  and saves no turnover.
- **B (depth sizing)**: the only surviving lever, monotone in ref.
- **C (breadth tilt)**: hurts everything. T1's signal predicts the *mean*,
  but the DD window is itself high-breadth, so the tilt shrinks recoveries
  without protecting the crash. A lagged-breadth overlay is a worse version
  of what depth sizing does per-name.
- **D (daily-rate entry)**: collapses, exactly as T3 predicted after the
  missed-cohort table. 2026 era goes negative.

Post-hoc extensions (labelled; `ch_robustness.py`, `ch_final_cells.py`):

| cell | Sharpe | max DD | MAR | one-way | bench Sh | bench MAR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| B ref 100 | 1.12 | −45.5% | 1.37 | 0.206 | 1.38 | 2.21 |
| **B ref 120 (chosen)** | 1.13 | −44.6% | 1.39 | 0.196 | 1.38 | 2.22 |
| B ref 160 | 1.16 | −41.9% | 1.51 | 0.180 | 1.41 | 2.37 |
| B ref 80, floor 0.15 | 1.12 | −47.7% | 1.33 | 0.216 | 1.36 | 2.09 |
| B ref 80, floor 0.35 | 1.09 | −49.1% | 1.23 | 0.220 | 1.35 | 2.00 |
| P print-depth ref 20 | 1.04 | −51.8% | 1.06 | 0.245 | 1.24 | 1.57 |
| P print-depth ref 30 | 1.02 | −44.0% | 1.16 | 0.210 | 1.20 | 1.66 |
| P print-depth ref 40 | 0.98 | −48.4% | 0.95 | 0.183 | 1.15 | 1.33 |

ref 100–160 is a flat plateau, no cliff; ref=120 is the middle of the flat
region, not the argmax. The floor is not load-bearing. Print-depth sizing
(same variable as the state machine) is strictly inferior to trailing-rate
sizing — one print is a noisier premium estimate than the 24h sum.

## 4. Robustness of the chosen cell

- **Fee stress** (9.78 bp/side, +2 over measured): v2 Sharpe 1.08 vs v1
  0.99 under the same stress — gap preserved.
- **Stale entry** (+24h): both books lose ~40% of mean (v1 18.0→10.4, v2
  17.2→9.6 bp/day); v2 keeps its relative edge (Sh 0.82 vs 0.77). The first
  day after the signal carries the fat; +1h/+4h delays measured at v1
  registration were free, +24h is not. Do not let scoring drift later than
  the daily cadence.
- **Gross/concentration**: same names (3.8 mean when active); mean gross
  0.24 vs v1's 0.37; p95 gross 0.74 vs 1.00. Same P&L from one-third less
  average capital deployed.
- **Vol-target overlay (registered recipe)**: hurts BOTH configs on
  corrected accounting (v1 vt 0.64 vs raw 1.02; v2 vt 0.52 vs raw 1.11;
  worst vt day −24.6%). The backward-looking scale levers up after quiet
  stretches into crashes, and it double-adjusts what depth sizing already
  does. Raw is the primary basis; vt stays reported for recipe
  comparability only.
- **Binance replication: FAILS.** Corrected-scorer carry-hold on Binance's
  own funding/prices/universe: +2.7 bp/day, t 0.4, Sharpe 0.18, max DD
  −85%, MAR negative; depth sizing does not rescue it (0.16). The v1
  registration's replication claim (Sharpe 1.38, ratio 0.50) was an
  artifact of the funding double-count. **Single-venue mechanism** until
  shown otherwise; re-examine if Bybit's interval mix or fee schedule
  changes.

## 5. Decision and honesty notes

**Registered `lane2_carry_hold_v2`** (commit = registration): v1 state
machine + `w = 0.10 × clip(|trail_fund_24h| / 120 bp-per-day, 0.25, 1.0)`.
Module-path numbers (the `reproduce_with` authority): 17.0 bp/day, t 2.53,
Sharpe 1.11, total +1052%, max DD −48.6%, MAR 1.25, one-way 0.197; bench
window Sharpe 1.36 / MAR 1.98. Paired daily differential vs v1: −1.0
bp/day, t −0.4 — **a risk reallocation, not a mean claim**. Selection bar:
cleared on every leg (Sharpe and MAR up on both windows, all eras positive,
turnover down 27%).

- **Basis note**: the review scripts computed the trailing rate after
  `prepare()`'s row filter; the module computes it before (true recent-bars
  window). They differ on 50/1883 gap-adjacent days — script Sharpe 1.13 vs
  module 1.11 for the chosen cell. Orderings unaffected; registered numbers
  are module-path.
- **The regime bet, stated plainly**: v2 gives up roughly half of v1's era
  mean in shallow-carry grind years (2023: 14.4 vs 26.0 bp/day) in exchange
  for the drawdown cut concentrated in deep-carry regimes (2025-26 eras
  improve to 32.6/42.4 vs 30.3/32.5). If funding microstructure normalizes
  to 2023 shape, v2 trails v1 while both stay positive.
- **Multiplicity**: this review ran ~19 seen-data cells on top of the
  program's ~90 prior constructions. t 2.53 does not clear the ~3.4
  Bonferroni-style bar the program applies to *new mechanisms*; v2 is a
  sizing refinement of an already-registered mechanism and is graded by its
  forward run and the paired differential vs v1, which continues scoring
  untouched.
- **What was NOT changed and why**: entry/exit thresholds (broad plateau at
  v1 registration; per-print acuteness proven load-bearing by T3/D); the
  gross cap (cascade-diluting by design); no trend/breadth gates (T2/T5:
  the edge lives in the fear regime the gates would remove); no beta hedge
  (2026-07-24 audit: relieves ~5% of tail); no time-based exit (T4).
- Standard-format charts (wrapper `--research-config`):
  `reports/equity_curves_2026-07-28/research/lane2_carry_hold_v{1,2}/` —
  same window, v1 +695.9% / DD −60.0% / Sh 1.21 / MAR 1.44 vs v2 +854.3% /
  DD −48.6% / Sh 1.36 / MAR 1.98.

## 6. Open follow-ups

1. Wire `lane2_carry_hold_v2` into the same forward-scoring cadence as v1
   (one row per completed UTC day; paired differential is the primary
   comparison).
2. The financed-longs forward record still needs the data refresh past
   2026-07-17 (pre-existing queue item; now covers both configs).
3. Re-derive the stale negative-ledger rows (1, 2, 13–17, 20) on the
   corrected scorer before trusting any of their magnitudes (pre-existing
   queue item).
4. If Bybit's interval mix shifts materially again (e.g. majority-1h), rerun
   §2's T3/T6 tables — the acuteness/chronic split is microstructure-shaped
   and could invert.
