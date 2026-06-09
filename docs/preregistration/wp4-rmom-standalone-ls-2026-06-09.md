# Pre-registration: WP4 Stage-A — residual-momentum standalone cross-sectional L/S

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory` Stage-A.
**Authority:** the P3 lead ("most promising alpha lead, needs an engine build to
certify, operator-gated") — the operator's 2026-06-09 full-authority grant ("design a
new system from the ground up... full authority over absolutely everything") is taken
as the RESEARCH greenlight; live-engine/demo integration remains operator-gated.
**Causality:** uses the post-2026-06-03-fix causal panel (`residual_momentum[D]` =
residual sum over [D-9, D-3], complete by D 00:00 UTC; pinned by
`test_residual_momentum_is_causal_shift3`). The old leaky-signal calibrations are NOT
cited. Day-grid join: panel ts at day-D start predicts day-D returns (the documented
live join convention).

## Strategy (fixed a-priori)

ALL days (regime-agnostic — this book also deploys downtrend capital): LONG the top
decile of `residual_momentum[D]`, SHORT the bottom decile, among non-BTC non-stable
names with turnover(D-1) >= liq floor; EW within leg; 0.5x equity per leg
(market-neutral). Tranche holds spread entries; k-day tranches. Costs 12 bps RT on
traded notional; REAL per-symbol funding both legs.

## Declared cells (final menu)

liq ∈ {500k, 2M} x hold k ∈ {1, 3, 5} = 6 cells/venue. Sensitivities on the best cell
only: lag-1 falsifier (use rmom[D-1] for day D — a slow signal must retain >= half its
Sharpe), 2x cost, funding-off attribution, leg attribution, per-year split.

## A-priori bars

Standalone: net > 0 AND Sharpe >= 1.0 on BOTH venues for the same cell id, BOTH legs
contributing positively gross. Combined-book: stitched into the hedged-max4 deployable
calendar, pooled MAR >= baseline + 1.0 with both venue deltas positive and DD worse by
<= 1.5pp per venue; survives 2x cost at both levels; lag-1 falsifier passes. Goal
check reported: combined pooled >= 7.25 (+30%). FAIL -> WP4 Stage-A records the
negative; the lead returns to the engine-build/forward path; the goal terminates with
the operator-keyed menu.

## Artifacts

`~/SHARED_DATA/wp4_rmom_ls_2026-06-09/` — cells + report JSON.
Script: `scripts/wp4_rmom_standalone_ls.py`.

## Verdict (filled in after the run)

_pending_

## Verdict (filled in after the run, same day) — FAIL, WP4 Stage-A closed

No cell passes the standalone bar (best liq2m_k5: Sharpe 1.09 bybit / 0.88 binance —
binance under 1.0; all other cells lower). Combined-book bar fails everywhere (best
delta bybit -0.20, binance -2.80): the L/S carries -18..-37% standalone DD vs the
deployable's -5% budget — the same risk-class mismatch as D3. Attribution: the book's
return is dominated by FUNDING CARRY (+31..+69%) net of heavy turnover costs
(-20..-56%); price legs are modest. The P3 trade-level IC (-0.19/-0.35, measured on
EVENT CANDIDATES) does not translate into a standalone daily cross-sectional book at
this cost stack.

Disposition per the receipt: the rmom lead returns to the engine-build/forward path
(as a SELECTION gate it remains validated research; as a standalone book it is
falsified at Stage-A). Tonight's combined evidence (D2, D3, WP4) establishes a
durable program lesson: THIS SYSTEM'S EDGE IS EVENT SELECTION + EXECUTION; daily
cross-sectional books fail on DD-class and cost/funding at our scale. The +30% goal
terminates with the operator-keyed menu (R4 fill calibration -> bank the sniper +13%;
forward evidence; any new alpha needs new DATA, not new combinations of this data).
