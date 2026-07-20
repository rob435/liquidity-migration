# Hypothesis Ledger — multiple-testing accounting across research generations

Started 2026-07-20. This ledger makes the selection surface explicit: how
many hypothesis families and configurations have been evaluated against each
dataset, so that the prior on any new "survivor" is set by the size of the
search that produced it. `docs/backtesting_errors_we_never_repeat.md` items
17–19 (parameter mining, out-of-sample reuse, multiple-testing denial) are
the failure modes this document operationalizes.

Rule going forward: **every Lane-2 config commit records, in its evidence
note, the ledger row it descends from and the approximate family count at
commit time.** A candidate that survived a 300-cell search across five
generations of the same data is a different object from a first-look idea,
and its forward record should be read with that prior.

## The one shared surface

Nearly everything below touched the same underlying data: the full-PIT
Bybit root (discovery window 2021-05-01 → 2024-12-01) and its derived
ledgers and render books. The reserved label-level holdout
(2025-01-01 → 2026-07-06) has **never been opened** and remains the only
untouched historical surface. Forward days after each config's commit are
the only other unmined data and accrue at one day per day.

## Generation inventory

| Gen | Program (date) | Hypothesis families | Configs/cells evaluated (approx, era-collapsed) | Data touched | Outcome |
| --- | --- | --- | --- | --- | --- |
| 1 | V1 / pre-overhaul era | 11 mechanism families (stops, delays, gates, scale-in, blacklists, …) + LONG v11a ablations | ~15+ single-config reads; 4 benchmark cells | full-PIT Bybit + Binance replay | **Both deployed sleeves descend from here** (`LongV11aDivWeekendVol`, `continuous_ensemble_v2`); most variants refuted |
| 2 | Strategy Overhaul V2 (2026-07-17/18) | 6 LONG post-cost contrast families + CONT exploratory leads | 6 families over 225,696 admitted labels; barebones books 1,899 LONG / 16,745 CONT trades | full-PIT Bybit (embargoed at 2024-12) | Closed, **no qualifying thesis**; comparator repair closed invalid; nothing deployed |
| 3 | V3, T-A…T-D (2026-07-19) | 4 (gate ablation, funding floor, pump deceleration, funding forecast) | ~40 distinct cells (grid CSVs: 6+72+12+21 rows incl. era triplication) | V2 barebones ledger + shared caches; T-A render books | All closed; gate retained (T-A refuted removal); no prototype |
| 4 | V4, T-E…T-I (2026-07-19) | 5 (fresh-high, MFE give-back, funding state, ML ranker, regime intensity) | ~60 distinct cells (grids: 15+60+30+18+12 rows) + 10 ML coefficients ×10 refits | Same ledger + both render books + render caches | All closed; key finding: barebones entry cuts invert on deployed books; no prototype |
| 5 | T-J deployed-book conditioning (2026-07-20) | 3 (exit geometry, gate override, freshness tilt) | ~20 cells + 500-seed permutation controls | Render books + barebones + bybit_render_1m | 3 killed; **1 Lane-2 prototype committed** (`t-j/2026-07-20/prototype_freshness_gate_override.json`), not deployed |
| 6 | T-K breadth funnel replay (2026-07-20) | 1 (continuous_breadth_v1 admission knobs; Lane-1 prerequisite, not an edge hypothesis) | 6 declared cells (liq × cap), live-producer semantics replayed | Live-shape panel (rmom 0.33) over the render window | Mechanics verified (7.3–7.7 bets/open-day, target ≥8–10 missed); measured ρ≈0.21 / per-bet vol≈1,000 bps refute the power table's 300 bps premise — knobs cut days-to-t by only ~9% |

Running totals against the shared discovery surface: **~29 hypothesis
families** and on the order of **150+ distinct configurations** evaluated
across five generations (grid rows including era splits total several
hundred). Anything now proposed from that surface is at minimum a
sixth-generation read.

## How to use this when judging evidence

- A Lane-2 candidate's forward record is clean by construction — but the
  *decision to commit it* was selected from this surface. Until the forward
  sample is meaningful on its own, weight it as one draw from a ~150-config
  search, not as an independent discovery. The T-J summary already applies
  this honestly ("second-generation selection… selection risk higher than
  V4's"); this ledger extends that habit to every future candidate.
- The V4 double-verification rule (same sign on barebones + both render
  books + both eras) is the strongest in-repo anti-selection control;
  prefer it for any new family on the spent surface.
- Opening the reserved holdout spends it forever. Per the V2 closure it is
  reserved for a *genuinely new* mechanism — which, given this ledger,
  should mean a hypothesis family not descended from the 29 above, recorded
  here when opened.
- When a generation's artifacts prune to a survivor, record the pruning
  ratio here (families in → survivors out). Five generations in: 29
  families in, 2 deployed sleeves (both from Generation 1) and 1 undeployed
  prototype out.

## Correction (2026-07-20, P0.2 verification)

The claim above that the reserved holdout is "the only untouched historical
surface" — and the tail-risk proposal's D1 claim that no generation touched
the pre-2021-05 slices — required correction after git-history archaeology
(`docs/preregistration/untouched_slice_provenance_2026-07-20.md`):

- **Binance [2020-01-01, 2021-01-01) was outcome-graded on 2026-05-24** by
  the V1-era momentum-factor tri-root gate (`binance_OOS_2020`; receipts at
  commit `4e2d943`, artifacts since purged). That family was killed/shelved
  2026-05-26/27 and descends into nothing deployed — but the read happened
  and is now on the ledger.
- **Bybit [2021-01-01, 2021-05-01) is outcome-unread but feature-touched in
  full** (V2 discovery `read_window` began 2021-01-01), with a 5–8 symbol
  universe.
- **Binance [2021-01-01, 2021-05-01) is pristine** — the only genuinely
  unopened historical surface besides the reserved V2 label-level holdout.

## Direction note (2026-07-20)

The repository's main focus is the book-level tail-risk program
(`docs/tail_risk_program.md`); new families route through it. Per-trade
price-exit families are closed on the spent surface (see
`docs/research_summary.md`). The reserved holdout remains earmarked for the
first genuinely non-descended family — currently the R2 squeeze-state
governor, built on fields none of the 29 families above used — and its
opening will be recorded here and in `docs/preregistration/INDEX.md`.
