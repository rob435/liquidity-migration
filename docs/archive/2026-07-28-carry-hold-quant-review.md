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

1. ~~Wire `lane2_carry_hold_v2` into the same forward-scoring cadence as
   v1~~ — done same day: `scripts/research/score_financed_longs_forward.py` appends
   the ledger (`reports/financed_longs_forward/ledger.csv` on the bybit
   root) with one row per config-day for all four registered configs plus
   the derived `carry_hold_v2_minus_v1` paired differential;
   `forward_eligible` marks days strictly after each registration.
   Append-first and idempotent (`tests/test_score_financed_longs_forward.py`).
2. The financed-longs forward record still needs the data refresh past
   2026-07-17 (pre-existing queue item; now covers both configs).
3. Re-derive the stale negative-ledger rows (1, 2, 13–17, 20) on the
   corrected scorer before trusting any of their magnitudes (pre-existing
   queue item).
4. If Bybit's interval mix shifts materially again (e.g. majority-1h), rerun
   §2's T3/T6 tables — the acuteness/chronic split is microstructure-shaped
   and could invert.

## 7. Portfolio context (same-day follow-up, seen data)

Full-calendar daily returns 2023-03-14..2026-07-16 (canonical baselines;
flat days = 0; artifact `portfolio_fit_output.txt` in the review dir):

- **carry-hold is uncorrelated with the deployed book**: corr(v2, CONT) =
  −0.08, corr(v2, LONG) = +0.01. Annualized vols: CONT 2.2%, LONG 5.6%, v2
  63.7% (raw book).
- **Crash-day complementarity with a mechanism**: on CONTINUOUS's 20 worst
  days (its shorts getting squeezed), v2 averages **+173 bp** — the same
  squeeze that hurts a pump-fade short pays a crowded-short long. On v2's
  own 20 worst days the sleeves are flat (+2.3 / +2.7 bp): the tails did
  not coincide on seen data.
- **Overlay thought experiment** (research accounting, margin NOT modeled):
  base book = CONT 1× + LONG 0.5 → Sharpe 2.02, max DD −2.1%, +7.4%/yr. A
  3–5% equity slice of raw v2 lifts it to Sharpe ~2.47, +10.2–12.1%/yr at
  −2.1/−2.4% max DD. The same slice of v1 is consistently worse (2.35 at
  its best). Honest framing: at α=0.03 the slice already contributes ~48%
  of portfolio volatility — the "free" return rests on tail non-coincidence
  continuing, and a v2-style −49% stretch costs a 3% slice ~1.5% of the
  account.
- **Program framing consequence**: the 2026-07-26 goal ("beat CONTINUOUS to
  replace it") was the wrong question for this book. Carry-hold is not a
  replacement candidate; it is a small diversifying premium stream, and its
  forward record should eventually be judged in that role. Any live sizing
  is an owner decision on a book that today has no runtime.

## 8. Capacity (same-day follow-up, seen data)

v2 held name-days vs each name's own trailing-24h quote turnover
(`capacity_output.txt`; 5,590 name-days, 3,052 post-2025). Held names have
median $33M adv24 with a thin tail ($3.2M at p05). Participation:

| book | holdings p95 | entry-day p95 | entry max |
| --- | ---: | ---: | ---: |
| $250k (current envelope) | 0.34% | 0.27% | 3.0% |
| $1M | 1.37% | 1.09% | 12.1% |
| $5M | 6.9% | 5.5% | 60.6% |

Post-2025, p95 entry participation crosses 1% of a name's daily volume at a
**~$1.1M book** and 5% at ~$5.5M. The measured taker-fee cost model is
defensible at the current envelope and stops being conservative well before
$5M. This is a small-book premium stream; the config's "not a large-book
claim" now has numbers.

## 9. Wave 2 — owner goal "Sharpe ≥ 2"; `lane2_carry_hold_v3` registered

Same-day second wave against the owner's explicit target of unconditional
Sharpe ≥ 2 on this book. ~95 cells across entry / exit / sizing / signal /
clock levers; every batch's full table is in the review dir
(`wave2_*.txt/json`, `ensemble_output.txt`, `hourly_engine_output.txt`).
**The target was NOT reached and is judged not honestly reachable for this
mechanism family at measured costs.** What was reached, and what was learned:

### 9.1 Levers that survived (→ v3)

Declared with mechanisms before their conditional tables ran; direction
era-consistent (adverse only in 2023-24, the same regime bet v2 makes):

- **Toxic-band filter**: the forward net of qualifying names is U-shaped in
  the trailing 3d return — capitulation (<−30%) pays +266 bp/nd and
  stabilized (>−5%) pays +161, but the moderate-grind-down middle
  ([−30%,−5%)) loses −114 bp/nd at Sharpe −2.1 (30% of qualifying
  name-days): shorts pressing and slowly *winning*. v3 blocks entries and
  suspends holds there.
- **Dead-name floor**: deep-funding names with trailing 30d vol < 5%/day
  lose −77 bp/nd — a pinned price has no squeeze fuel. Entry-only floor.
- **Recovery-velocity exit**: once the trailing daily rate recovers by
  >30 bp over 2d, remaining hold loses −54 bp/nd — the squeeze is over.

Module-path v3 (panel through 2026-07-27): **19.8 bp/day, t 3.13, Sharpe
1.38, max DD −28.7%, MAR 2.84, turnover 0.156** (v2 same basis: 1.09,
−48.6%, 1.21, 0.198); bench window **1.71 / MAR 4.84** (v2 1.35/1.95).
Paired differential vs v2: +3.1 bp/day (t 1.6) — the registered forward
experiment.

### 9.2 Levers refuted (all cells reported)

Print-trend at entry (dead); spell-age/spell-loss exits and floors (dead);
per-name vol normalization (the premium scales WITH vol — Sharpe falls);
shallower −5 bp entries (junk even filtered); per-name caps 0.15/0.20
(mean and vol scale together — Sharpe pinned at ~1.5 while MAR rises to
~4: a sizing decision for an owner, not a Sharpe fix); breadth tilts and
every regime-scaling overlay (the pooled-variance arithmetic defeats them —
the deep-regime variance dominates the pool at any scaling); band-only-when-
shallow (worse); X1/band boundary grids (flat plateaus).

### 9.3 The conditional Sharpe-2 statement (the honest version of the goal)

On the PIT deep-funding regime (30d rolling median of universe funding
prints, lagged; the deeper HALF of days): the improved book runs at
**conditional Sharpe 2.15–2.35** (49.1 and 28.7 bp/day on the two deep
quartiles). On the shallow half it is EV-noise (1.4–5.0 bp/day at 0.24–
0.64). The strategy IS a Sharpe-2 strategy while its premium regime is on
— which is a deployment-sizing fact, not a pooled-Sharpe fact.

### 9.4 Program-level integrity findings (apply to v1/v2/v3 and the
benchmark comparisons alike)

- **Decision-clock fragility**: the identical construction swept over 12
  daily-grid offsets spans Sharpe **0.30–1.52**; midnight — the clock every
  registered financed-longs number uses — is the best cell. Settlement-
  aligned offsets (8h/16h, where the 00/08/16 cohort's prints are age-zero)
  score 0.73/1.04, refuting a freshness explanation: midnight is
  substantially luck. The offset-ensemble (8 offsets, equal capital) is the
  honest level: **~1.2 full / ~1.5 bench** for the v3 construction. The
  FILTERS' improvement over v2 is clock-robust (better at 2 of 3 stale
  offsets, tied with better DD on the third) — which is what the registered
  paired differential measures.
- **Terminal-day dodge**: `prepare()`'s forward-return requirement makes
  every daily-frame book exit each name 24h before its final panel bar —
  an implicit look-ahead that dodges terminal delisting dumps, measured at
  roughly **+0.13 Sharpe** (hourly-frame check; flips 2022's sign). All
  published financed-longs numbers share it; cross-config comparisons
  remain fair.
- **Print-clock knife-catching**: deciding within the hour of each funding
  print (the "natural clock") collapses in 2026 (−94 bp/day era): an
  intraday deep print during a cascade is an invitation to catch a knife,
  and the daily clock's staleness doubles as a survived-to-the-bar filter.
  An hourly variant needs a persistence mechanism; parked.

### 9.6 → continued in §10: the neutral leg and the funding-carry program

### 9.5 Same-day operational receipt — refresh + rmom overlap incident

The append-first research refresh (both venues, tail mode) ran to
completion after two instructive failures: (1) it refuses a dirty tree
(by design); (2) `features.binance.residual_momentum` tripped its
append-overlap guard (values moved, max 0.019). Investigation: population
identical, end-window and pre-M21-code rebuilds identical, and even the
full Jul-17 package on today's byte-identical inputs cannot reproduce the
stored artifact — the artifact's own last-refresh window is the
irreproducible side, most plausibly the since-fixed pre-M9 full-directory
read sweeping duplicate files at write time. Today's store passed the
mandatory all-root validation. Remediation per the guard's own taxonomy:
forced `--full-rewrite` of BOTH research rmom artifacts (bybit 496,685
rows, binance 467,525 rows, through 2026-07-28), which also unifies them
on the post-M21 calendar-grid definition. Deployed path unaffected (live
roots always full-rewrite). Panel rebuilt 2021→2026-07-27 (11.73M rows;
score-level history verified byte-stable against the ledger). Forward
ledger live: `reports/financed_longs_forward/ledger.csv`, first eligible
days land as the calendar delivers them.

## 10. Wave 3 — the neutral leg; the funding-carry program reaches Sharpe 2 on the standard basis

The pooled-Sharpe plateau of §9 is structural: a conditional-2.25 stream
active half the time pools to ~1.6, and per-day scaling cannot change that
(§9.2). The desk answer is to change the P&L identity, not the thresholds:
capture the SAME premium market-neutrally as a cross-venue spread and run
both expressions as a portfolio.

### 10.1 `lane2_funding_spread_v1` (registered)

Long the perp on the venue where funding is more negative, short the same
symbol's perp on the other venue; hysteresis on the trailing settled spread
(enter |80| bp/day, exit |20|); both legs' taker fees charged. Checked
against both negative ledgers first — the prior cross-venue items were
price-premium signals and the directional Binance replication; neither is
this trade. Module path: **5.1 bp/day, t 2.92, Sharpe 1.34 full / 1.61
bench, max DD −16.7%, one-way 0.087/day**. Honest properties, measured:

- **Offset-STABLE** (unlike the directional family): clocks {0,6,12,18}h
  give 1.10–1.28 full / 1.36–1.61 bench.
- **Basis risk is why it isn't Sharpe 3**: the cross-venue price difference
  runs 40 bp/day sd normally but **677 bp/day when |spread| > 50** — the
  "neutral" book is riskiest exactly when active.
- 2023-24 eras ~zero/slightly negative (the spread barely existed before
  the inversion); depth-sizing the spread REJECTED (deeper spread = more
  basis vol; Sharpe falls); grid {30,50,80}×{10,20} all reported.
- corr(spread book, carry_hold_v3 daily nets) = **+0.09**.

### 10.2 The two-book program

PIT 60d rolling vol-parity weights (no full-sample optimization; mean
weights ~20/80 directional/neutral), both books' own costs:

| basis | Sharpe | max DD | MAR |
| --- | ---: | ---: | ---: |
| bench window, standard clock | **2.34** | −11.2% | 5.07 |
| bench window, clocks {0,6,12,18} | 2.34 / 1.93 / 2.09 / 2.29 | | |
| full window (2021-inc), standard clock | 1.87 | −11.2% | 3.03 |
| full window, worst clock | 1.55 | | |
| fixed non-optimized 25/75 split | 2.22 bench / 1.79 full | −10.8% | 5.06 / 3.15 |

**Verdict on the owner's Sharpe ≥ 2 target**: met on the program's standard
quote basis — the bench window every published comparison in this
repository uses — at the standard clock (2.34) and at 3 of 4 clocks
(ensemble ≈ 2.2); NOT met on the strictest basis (full 5.5y window, worst
clock: 1.55). Both numbers are the claim. The portfolio is a documented
construction over two registered configs — the forward ledger scores both
daily and the combination is reconstructable forward; any live expression
would additionally need two-venue margin and leg-execution engineering
(config `known_weaknesses`).

Frame caveats from §9.4 (midnight clock for the directional leg,
terminal-day dodge for both) still apply to every number above.

## 11. Loser identification and the intraday stop grid (owner follow-up, same day)

Owner question: most of the alpha is loss-avoidance — can losers be
identified early? Two Lane-1 seen-data artifacts in
`reports/carry_hold_v3_trade_diagnostics_2026-07-28/`:
`losers_early_id_diagnostics.txt` and `intraday_stop_grid.txt`.

### 11.1 Anatomy: the loss is one candle

Of 1,670 v3 trades, 60.1% lose (mean loser −10.4%, mean winner +20.4% —
the book is a right-skew harvest; the worst decile costs −361% book against
+375% total). But the losses are not processes, they are events:

- median losing trade lasts 2 days; in 98% of losers a single daily candle
  carries ≥ 50% of the loss (median worst-day share: 100%);
- 88% of losers hit their maximum adverse excursion within 2 days of entry;
- the median loser still RECEIVED +0.64% funding — price does the damage
  (median −9.3%); only 71/1,003 losers had negative funding receipts.

Early pain does not predict later pain: conditional on being ≥ 10%
underwater after day 1 (n=136) the remainder of the trade still averages
**+1.26%**; after day 2 (n=77) **+2.07%**. Only after day 3 does the deep
bucket turn negative (−2.50%, n=49 — noise-sized). This is why every daily
spell-floor cell (§9) was flat: cutting early losers forfeits EV.

### 11.2 Entry screening cannot separate them

The worst decile's entry fingerprint (deeper print −34 vs −22 bp, deeper
trail −124 vs −97 bp/day, higher vol 10.4 vs 8.0%/day, MORE pumped: ret3d
+27% vs +14%) is the same fingerprint as the biggest winners. Entry ret3d
buckets: the ≥ +50% "pumped" bucket has median **−6.4%** but mean **+8.2%**
and the largest book contribution of any bucket (+187%). Losers and monster
squeeze-winners are the same names at entry; blocking the fingerprint
deletes the strategy. (The [−5%, 0) sliver just above the toxic band is
mildly negative: mean −1.05%, −11% book — a possible small band extension.
Cohorts that ARE structurally lossy: suspension-touched trades −6.5%
(n=168), `open_at_series_end` −4.0% (n=70, −16% book over 5.5y).)

### 11.3 The intraday stop grid — the last untested stop-family cell

Design (declared first): trigger on cumulative held-path price return from
entry, evaluated on hourly closes (panel has no highs/lows — detection
conservative); thresholds −10/−15/−20/−25/−30%; fills at trigger-bar close
(A) and next-bar close (B, primary); stop kills the spell (no re-entry
until the original spell would have ended); settlement-exact funding
credited up to the fill bar; stop fee 7.78 bp × w. Counterfactual built as
exact per-day deltas on the registered `scores_v3_module` series (baseline
mean/t/Sharpe reproduce 19.83/3.13/1.376 to the digit). Alignment: 3,314
held-day starts checked against hourly bars — 0 missing, 0 price
mismatches. DD/MAR in this table are on this script's arithmetic-cumsum
basis (baseline −31.1%/2.32; the §8 quote −28.7%/2.84 is the review's
earlier DD basis — within-table comparisons are like-for-like).

| cell (fill B) | mean bp/d | Sharpe | max DD | MAR | stops |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline (no stop) | 19.83 | 1.376 | −31.1% | 2.32 | — |
| stop −10% | 16.09 | 1.290 | −47.9% | 1.23 | 652 |
| stop −15% | 14.78 | 1.149 | −52.9% | 1.02 | 410 |
| stop −20% | 14.68 | 1.118 | −63.6% | 0.84 | 251 |
| stop −25% | 16.98 | 1.263 | −51.7% | 1.20 | 146 |
| stop −30% | 17.38 | 1.279 | −55.5% | 1.14 | 88 |

Fill A (trigger-bar close, optimistic) is within a few tenths of B on every
cell — the conclusion does not hinge on fill assumptions. Every cell is
worse on mean AND Sharpe AND max DD AND MAR.

Mechanism, measured at −15% B: the stop wins the **median** stopped trade
(+0.5 bp book) and loses the **mean** (−23.3 bp; p10 −153, p90 +132) — it
sells the right tail. The DD paradox is the deep finding: max DD *worsens*
from −31% to −48…−66% because the book's drawdown is made of the same
trades that later pay; excising recoveries converts a drawdown into a
realized hole. Era split says the same: stops help slightly in the
shallow-carry years (2022–24, Sharpe 0.42→0.53, 1.48→1.61, 1.18→1.47 at
−25%) and destroy the paying eras (2025: 41.7→33.7 bp/d; 2026:
64.7→35.7 bp/d at −15%). Slip beyond threshold at the next-bar fill:
median 1.5%, p90 9.4% — the dump is fast even at hourly resolution.
Triggers are ~uniform across UTC hours (mild peak at 00:00): no
time-of-day structure to exploit.

Because post-touch EV is positive (+1.3%/+2.1% after a day-1/day-2 −10%
touch), a stop-and-re-enter variant is bounded between this grid and
baseline minus round-trip fees and whipsaw — it cannot beat baseline
either. Limitation stated: hourly closes only; real mark-price stops would
trigger earlier and fill worse.

### 11.4 Verdict

The stop family is now closed at both clocks (daily spell floors §9,
intraday MAE stops §11.3): in this book, reaction-based loser
identification is structurally impossible — the loss is one candle, early
pain predicts recovery, and the losers' entry fingerprint is the winners'.
Remaining untested loser-identification doors, in mechanism order:
same-symbol cross-venue funding confirmation at entry (consensus short vs
venue-local liquidation), suspend→hard-exit, toxic-band hi → 0 extension,
turnover-rank-decay dropout warning. OI remains banned
(survivorship-contaminated); spot-basis needs data the roots don't have.
