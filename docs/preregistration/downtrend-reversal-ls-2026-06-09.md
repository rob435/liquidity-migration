# Pre-registration: D2 Stage-A — downtrend reversal L/S sleeve

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory` Stage-A
(vectorized daily simulator; an engine-grade build follows only on a PASS).
**Program:** `docs/research_plan_downtrend_sleeve_2026-06-09.md` D2; D1 lead receipt
`downtrend-opportunity-map-2026-06-09.md`.

## Strategy under test (fixed a-priori)

On days where BTC's trailing 30d return (through d-1) is NEGATIVE (the regime where
the deployed book is OFF):

- Universe: non-BTC, non-stable USDT perps with turnover(d-1) >= liq floor.
- Signal at d-1 close: trailing return (`ret_7d` primary = close(d-1)/close(d-8)-1).
- Book for day d: SHORT the top decile of the signal, LONG the bottom decile, equal
  weight within leg, each leg 50% of sleeve notional (market-neutral by construction).
- Flat on up-regime days (book closes when the regime flips; turnover charged).
- PnL day d = sum leg weights x day-d returns; costs = 12 bps round-trip equivalent
  (6 bps per side) on traded notional (|w_d - w_{d-1}| per name, entries AND exits) at
  1x; funding = REAL per-day per-symbol funding sums (shorts +receive/-pay positive/
  negative rates; longs the mirror).

## Named cells (declared up front — no additions without amendment)

liq floor {500k, 2M} x membership {plain decile-daily, hysteresis enter-decile/exit-
quintile} x signal {ret_7d, blend 0.5*rank(ret_1d)+0.5*rank(ret_7d)} = 8 cells/venue.
Sensitivities (reported, on the best cell only): hold variants via 2d/3d tranche
overlap; 2x cost; funding-off attribution; per-year split; leg attribution.

## A-priori Stage-A bars

- Tier-1 (lead survives contact with costs): in-regime NET return > 0 both venues on
  >= 4 of 8 cells, with the SAME cell ids passing on both venues.
- Stage-A PASS (gates the engine build): at least one declared cell with BOTH venues
  in-regime net Sharpe >= 1.0 (full-calendar convention over regime days), positive
  both years-with->60-regime-days, BOTH legs contributing positively gross (no
  single-leg beta bet), and surviving 2x cost with net return > 0 both venues.
- FAIL -> record the map as descriptive structure not tradeable at our cost stack;
  fall back to S1 (sniper entries) for the MAR target.

Capacity note: legs are ~25 names/side at liq>=500k; the 2M floor cell exists because
the deployable book is capacity-constrained (see continuous-capacity receipt) — a
sleeve that only works at 500k adds little deployable capital.

## Artifacts

`~/SHARED_DATA/downtrend_reversal_ls_2026-06-09/` — per-cell metrics + daily series.
Script: `scripts/downtrend_reversal_ls.py`.

## Verdict (filled in after the run, same day)

**FAIL — decisive, per the pre-registered bar.** Binance: 0/8 cells net-positive
(−1.4% to −11.8% over 503 regime days, Sharpe ≤ 0.11). Bybit ret_7d cells show
+33..+78% (Sharpe up to 1.31) but with DD −27..−46% and no binance confirmation —
the classic single-venue mirage; the both-venue requirement kills it.

Attribution (the useful part): the LONG leg (buy 7d losers) has REAL gross alpha on
both venues (best cells +135% bybit / +58–60% binance gross); the SHORT leg adds ~0;
REAL funding costs −28..−43% (shorting bear-market relative-winners = paying their
deeply negative funding — the crowding is priced), and turnover costs −16..−43%. The
D1 map's structure is real but not net-tradeable at our cost+funding stack — same
pattern as the 2026-06-03 alpha-hunt graveyard.

Disposition: descriptive structure recorded; NO off-menu rescue cells (a long-only
bounce sleeve would be a fresh post-hoc selection with brutal in-regime DD; if ever
revisited it needs its own dated pre-registration). Falling back to S1 (staged/sniper
entries on the proven uptrend book) per the program plan. The downtrend-capital
question stays open honestly: at current evidence, the right downtrend allocation is
the BTC hedge leg + cash, not a forced sleeve.
