# T-F — MFE give-back ladder (exploratory, Lane 1)

**Status: EXPLORATORY.** Re-simulation of the spent V2 discovery surface. No
alpha, robustness, candidate, or promotion claim. Grid exactly as declared in
`docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md` (9 cells,
2 axes); no cell added.

## What ran

- Re-simulator: every trade re-walked from its recorded entry on 1h bars with
  the exact barebones fill conventions, mirroring the dormant engine hooks in
  `liquidity_migration.trade_lifecycle` verbatim (mae/mfe updated with the
  current bar, TP touch precedence, adaptive exit at bar close with `<=`
  comparisons, 24h boundary close, engine exit-reason strings).
  **Validation: the no-rule walk reproduces all 16,745 recorded exits exactly
  (0 mismatches, 0 price diff)** — the re-simulator is engine-equivalent on
  this surface, so a surviving cell would have been renderable without
  redefinition.
- Declared cells: mfe_giveback arm A ∈ {4%, 6%, 8%} × retain R ∈ {0.5, 0.7};
  breakeven_stop arm A ∈ {4%, 6%, 8%}. Axes: full ledger and the T-E skip_h6
  book (3,635 trades; axis baseline +5.81% net, matching the T-E grid).
  Changed trades keep their modeled round-trip cost; funding recharged per
  settlement to the new exit. Era split at 2023-02-22.

## Results

**Full axis (baseline −20.23%):** best cell giveback A=8%/R=0.7 reaches
−19.03% (**+1.20pp**; era split +0.32pp / +0.88pp). But the decisive
accounting shows that margin is the residue of two enormous opposing flows:
forfeited TP completions **−53.33pp** vs captured give-back **+54.52pp**
(3,031 adaptive exits). Every other cell is weaker or negative; the tight
arm A=4% cells are outright destructive (−23.1% to −25.1%: they forfeit
completions worth more than the give-back they capture — at R=0.7 forfeits
reach −148pp vs +144pp captured). Breakeven cells are all worse than
baseline. TP rate collapses under every cell (15.8% baseline → 5.8–15.2%).

**T-E-filtered axis (baseline +5.81%): every cell loses money.** Best cell
(A=8%/R=0.7) drops to +4.56% (−1.25pp); A=4% cells lose 10pp+. On the
higher-quality book the give-back pool is exactly where the TP completions
come from — the ladder amputates them.

MAE relief exists but is small (best-cell mean MAE −7.6% vs −8.4% baseline;
maxDD −35.1% vs −38.7%) and does not pay for the forfeited completions.

## Read

**No cell survives; the adaptive-exit direction is now dead on both
granularities** — the 2026-06-20 1m intrabar program closed dynamic-TP with no
candidate on the sub-hourly engine, and this closes give-back/breakeven
capture on 1h bars against the barebones shape. The draft's own framing
applies: the ~6%/trade give-back is real, but it is not harvestable at bar
granularity — the same retracement that signals give-back is the path TP12
completions ride through. The best cell's +1.2pp margin (difference of ±54pp
flows) inverts on the declared quality axis, which is disqualifying under the
program's interaction report.

## Limitations

- Spent discovery surface; 1h bar closes only; no intrabar exit modeling.
- Changed trades keep the original modeled round-trip cost; exit-side
  slippage of the new bar-close exits is not separately modeled (a real
  implementation would pay more, making every cell worse).
- 15 partial-funding trades: recomputed funding on moved exits may misstate
  slightly (counts per cell in `tf_grid.csv`).
- No capacity backfill; freed capital from earlier exits is not redeployed
  (a redeployment model would be a different, undeclared thesis).

## Next action

No prototype; no engine render (the ledger-level rule did not survive).
Adaptive-exit work on this sleeve should not be re-opened without a
fundamentally different instrument (e.g., render-native surfaces or
intrabar data with an execution model), and any such re-opening must cite
this closure and the 2026-06-20 one.

Artifacts: `tf_grid.csv` (60 rows: 9 cells + baseline × 2 axes × 3 eras),
manifest with rule semantics, reproduction check, and grids.
