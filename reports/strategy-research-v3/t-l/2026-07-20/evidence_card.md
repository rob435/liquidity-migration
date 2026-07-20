# T-L — conditional listing study (Bybit) — Lane-1 evidence card

Date: 2026-07-20. Lane-1 exploratory, on seen data. Scripts:
`scripts/research_v3/tl_listing_conditional.py`,
`scripts/research_v3/tl_listing_execution_cost.py`. Raw outputs + hashes:
this directory's `manifest.json` and `execution-cost/manifest.json`.

Note on lineage: the mission queue referenced a "T-L v1" at this path; no
such artifact ever existed on this host or in git history (verified against
`git log --all` and the working tree). This is the first committed T-L
study; it covers the population read AND the conditional read in one
declared design.

## Claim and decision

Newly listed Bybit perps carry a first-week drift (short d1/d2 -> d7)
conditionable on entry-time state to clear the admission bar (era-stable
net >= +40 bp/trade after the frozen 45 bp round-trip hurdle and realized
funding). Decision informed: whether any cell earns a Lane-2 config commit.

## Data that shaped vs graded

Shaped and graded the same (Lane-1): Bybit full-PIT root (manifest, 1h
klines, funding) for listing events d0 in [2021-05-01, 2026-07-01], plus
`bybit_render_1m` for the execution-cost read. The reserved V2 label tape
was not read. The frozen grading windows G1/G2 (Binance) and G3 (Bybit,
pre-2021-05) were deliberately left unread by this family (event floor
2021-05-01), so they remain available to grade a committed T-L config
later. Raw 2025-2026 klines/funding are the same seen surface the R1/T-A
generation used.

## Population

881 listing events (0 d0 kline gaps). Exclusions, counted: 8 pre-floor,
12 right-censored, 1 relist quarantine (DATAUSDT), 1 manifest/first-obs
mismatch (PUMPBTCUSDT), 2 entry-bar-missing, 2 exit-bar-missing arms.
Eras by d0: e2122 n=179, e2324 n=336, e2526 n=364. Delisted-within-week
exclusion (n=2) biases mildly AGAINST the short arm (delistings usually
crash), direction stated.

## Headline: the unconditional arms flip at 2024/2025

Net45 = net of 45 bp round trip + realized funding, bp/trade, week-cluster
bootstrap s.e.:

| arm | pre-2025 (n=515) | post-2025 (n=364) |
| --- | --- | --- |
| short d1->d7 | +263 +/- 151 | -180 +/- 454 |
| short d2->d7 | +226 +/- 129 | -335 +/- 414 |
| long d1->d7 | -353 +/- 147 | +90 +/- 442 |
| long d2->d7 | -316 +/- 133 | +245 +/- 396 |

Funding flips with it: shorts collected ~0 funding pre-2025 and PAY
~92-117 bp/trade post-2025 (new listings carry negative rates post-2025 —
the listing short is crowded now). The calendar-only arm is dead; the
2024/2025 flip is the primary fact of this study.

## Conditional read — all cells reported, one survivor

Declared features (frozen in the script docstring before outcome reads):
pump01, tdecay, fund8h, crowd7d, btc30d + the single declared interaction
pump01 x fund8h. A ~90-pair pre/post scan produced 5 one-feature and 4
interaction cells clearing +40 bp in both era groups (all in
`arms_conditioned.csv` / `arms_interaction_pump_x_funding.csv`).
Calibration of the bar itself: a RANDOM same-size cell clears +40 bp in
both era groups 26.4% of the time (5,000 draws) — so bar-clearing alone is
weak; every cell below except the survivor is recorded as scanned, not
banked.

**Survivor: turnover-collapse short (`tdec_lt30`: entry-day per-hour
turnover < 30% of d0's).** The only cell that is (a) stable at BOTH entry
days, (b) stable across the threshold band 0.2-0.4 (dies at 0.5), (c)
three-era positive, (d) permutation-distinguishable from random cells
(nominal p = 0.0148, both-era joint, 5,000 draws — selected from the ~90
scan, so read as promising, not confirmed):

short d2->d7, net45 bp by era: e2122 +247 (n=9), e2324 +246 (n=114),
e2526 +510 (n=116); pre-2025 +246 +/- 169, post-2025 +510 +/- 241.
short d1->d7: e2122 +329 (n=4), e2324 +162 (n=55), e2526 +680 (n=58).
Net90 (stress cost): subtract 45 bp — still clears everywhere.
Gradient structure post-2025: tdec_30_70 and tdec_ge70 shorts are
strongly NEGATIVE (-887 / -520 net45 at d2) — sustained-turnover listings
are what killed the calendar short; collapsed-turnover listings kept
working. Mechanism-consistent (dead attention -> drift down; sustained
attention -> squeeze risk).

Tail honesty: short means hide a catastrophic right tail — worst cell
trade -19,551 bp (an unlevered short can lose >100%); mid-bin e2526 mean
-887 vs median +542. Any implementation needs the book-level tail
program's sizing/insurance layers, not per-trade stops (closed line).
Concentration: 239 cell events over 130 week-blocks, top-5 |net| share
12% — not a handful of prints. e2122 rests on n=9 — era-stability there
is thin by construction (collapse events were rare in 2021-22).

## Execution-cost reality read (1m)

675/881 events lie in the 1m window; 278 have render-1m coverage
(397 never entered the render universe — the missing ones skew thin, so
this read is OPTIMISTIC; stated). 0 divergent 1m/1h days at the 50 bp
quarantine bar. Listing week (d1-d7) vs same-symbol mature (d60-66):
median 1m rel-range 2.18x mature (median-of-medians ~25-40 bp vs ~17 bp
sigma_1m); zero-volume minutes LOWER in listing week; Amihud impact
LOWER (deep turnover). Implication at demo scale (<= $5k notional):
taker round trip ~ fees (~11 bp) + spread-crossing (~25-40 bp) ~= 35-50
bp — the frozen 45 bp hurdle is realistic in-range, 90 bp covers p75.
Candidate-cell entry days are NOT dead: median day turnover $4.8M
(e2324) / $17.8M (e2526), participation <0.1%.

## Against the admission bar

- Era-stable net >= +40 bp/trade: the tdec_lt30 short clears on point
  estimates in all three eras at both entry days, at 45 and 90 bp costs.
  Uncertainty: 1.45 sigma (pre) / 2.1 sigma (post) — not individually
  decisive; the cross-entry-day + threshold-band + gradient structure is
  the strength.
- \>= 5 independent bets/day: unreachable (cell rate ~45 events/yr).

Verdict: candidate, pending the Binance robustness pass (the V4
double-verification habit — same sign on a second venue — is the
strongest in-repo anti-selection control). No Lane-2 config committed on
this card alone.

**Cross-venue verdict (added same day, after the Binance pass):** the
cell FAILS. On `binance_full_pit` (same script/design, 659 events) the
turnover-collapse short at d2 is negative in ALL THREE eras (−415/−41/
−290 bp, n=6/38/86) with no stable threshold region; only the
population-level 2024/2025 flip replicates. See
`binance/divergence_note.md`. **T-L closes with no Lane-2 candidate**;
the Bybit cell is recorded as selected-fluke-or-venue-quirk, dropped in
the hypothesis ledger.

## Non-conclusions

- No statement about the long mirror: post-2025 long cells are noise
  (s.e. ~400-460 bp); the small interaction long cell (pump_25_100 x
  fund_neg, n=15/10) is a curiosity, recorded not banked.
- No statement about pre-2021-05 listings, G1/G2/G3, or the reserved
  holdout (unread).
- Nothing here is deployment evidence; per-trade exit variants remain a
  closed line; the 1m cost read does not cover the 397 never-rendered
  symbols.
