# Pre-registration: WP3 Stage-A — market-neutral hedge overlay (instrument comparison)

**Date:** 2026-06-09 (registered BEFORE the run)
**Run label:** `exploratory` (Stage-A ledger-level overlay; a PASS gates Stage-B engine
integration — it is not itself promotion evidence)
**Program:** `docs/research_plan_continuous_regime_2026-06-09.md` WP3 (+ WP1c folded in),
upgraded by the WP1a mechanism finding (same-day alt-market corr −0.30/−0.36 beats BTC's
−0.22/−0.27; receipt `continuous-rs-squeeze-probe-2026-06-09.md`).

## Hypothesis

The book's uncompensated contemporaneous alt-market beta can be hedged with a causal
rolling-beta long leg. Candidate instruments: BTC (operationally simplest), the EW
alt-market (theoretical ceiling, not tradeable at full breadth), a top-10-liquid alt
basket (tradeable approximation). Last session's quick overlay (BTC, Sharpe 3.05→3.32 /
2.62→3.17) did NOT charge funding on the hedge leg; this run charges real funding.

## Inputs (verified)

- Book ledgers: `~/SHARED_DATA/continuous_robustness_2026-06-09/scale_sensitivity/{venue}/winner_base/w90_tv0.045_max{4,10}_ddh-0.04/equity.csv`.
  **max4 is the BINDING cell** (the WP2 re-anchored deployable); max10 reported for
  benchmark continuity.
- Hedge-instrument daily returns from the WP1a-verified daily panel construction
  (feature_panel + bybit raw-kline tail; consecutive-day closes; no survivorship):
  - `btc` = BTCUSDT;
  - `alt_ew` = the WP1a EW-alt factor (turnover(d−1) ≥ 500k membership) — diagnostic;
  - `alt_top10` = EW mean of day-d returns of the top-10 non-BTC symbols by day-(d−1)
    turnover (causal membership) — tradeable.
- Funding: real per-UTC-date sums of funding events for the hedge symbols from
  `{venue}_full_pit/funding` (bybit) and `binance_full_pit/binance_usdm_funding`
  (binance). Raw per-event daily sums are interval-agnostic (the 4h/8h debt concerns
  engine handling, not a raw sum). Long hedge pays positive funding.

## Mechanics (fixed a-priori)

- `y_unit(d) = basket_return(d)/scale(d)`; `beta_unit(d)` = OLS beta of y_unit on h over
  the trailing 90 LEDGER days ending d−1 (min 60 obs; hedge off before that). Causal:
  no day-d data in the estimate; `scale(d)` is already engine-causal.
- Hedge ratio `H(d) = clip(−beta_unit(d), 0, 2) · scale(d)` (long-only hedge, sanity cap).
- `r_hedged(d) = basket_return(d) + H(d)·h(d) − H(d)·funding_day(d) − cost_bps·|H(d) − H_prev|`.
  Off-ledger days: book flat, hedge CLOSED (turnover charged on close + reopen —
  conservative).
- Costs: primary 5 bps on hedge turnover; stress 0/10/20 bps (10 bps = the 2x-cost gate).
- Beta window: 90d primary; 60/120/150 sensitivity on BTC + the best instrument.
- Metrics per cell: total return, simple-annualized-return/maxDD MAR (repo convention),
  Sharpe (ann., full-calendar grid with flat days at 0), worst day, per-year and 2023–24
  sub-period Sharpe, mean/max H, annual hedge turnover. Convention check: control cells
  must reproduce the known benchmark numbers (bybit max10 +226%/MAR 6.18/Sharpe ~3.05).

## A-priori banking bar (gates Stage-B engine integration)

For an instrument, at **max4 (binding)**, primary cost + real funding, hedged vs control:

1. positive total return, both venues;
2. DD reduced OR Sharpe increased, both venues;
3. pooled MAR Δ > +0.1; neither venue MAR Δ < −0.5;
4. all of 1–3 hold at 2x hedge cost (10 bps);
5. flattens 2023–24: hedged 2023–24 Sharpe ≥ control's, both venues;
6. window stability: Sharpe Δ keeps its sign at W ∈ {60,120,150}, both venues.

Instrument selection among passers: highest pooled Sharpe Δ; ties → operationally
simplest (btc > alt_top10 > alt_ew). `alt_ew` passing alone does NOT bank (not
tradeable); it sets the ceiling and motivates a tradeable approximation.
If no instrument passes WITH funding but one passes with funding off: record
"hedge value real but carry-negative" — do NOT bank; the funding-aware design moves to
Stage-B as an open problem. Trade-count gates: N/A (overlay on identical book trades).

## Artifacts

`~/SHARED_DATA/continuous_hedge_overlay_2026-06-09/` — per-cell metrics CSV + report
JSON + this receipt's verdict section. Script: `scripts/continuous_hedge_overlay_driver.py`.

## Verdict (filled in after the run, same day)

**PASS — bank the BTC hedge for Stage-B engine integration.** Controls reproduced the
benchmark (bybit max10 +226.22%/MAR 6.17/DD −11.69%; binance +142.48%/6.00/−7.71;
max4 +84.01%/5.02 and +60.00%/4.57 — all match prior receipts; Sharpe levels here use
a full-calendar-grid convention, deltas are within-convention).

Banking bar at max4 (binding), W=90, 5 bps, REAL funding — per instrument:

| instrument | bybit ΔMAR / ΔSharpe / ΔS2324 | binance ΔMAR / ΔSharpe / ΔS2324 | 6 conditions |
|---|---|---|---|
| **btc** | +0.34 / +0.207 / +0.346 | +1.07 / +0.382 / +0.627 | **PASS 6/6** |
| alt_ew (diagnostic) | −0.43 / +0.341 / +0.478 | +1.47 / +0.507 / +0.898 | PASS 6/6 (non-bankable) |
| alt_top10 | −0.40 / +0.149 / +0.128 | +0.98 / +0.368 / +0.314 | PASS 6/6 |

Selection per the pre-registered rule: alt_ew excluded (not tradeable; it confirms the
WP1a mechanism ceiling); btc beats alt_top10 on pooled ΔSharpe (+0.295 vs +0.259), is
the ONLY instrument improving MAR on both venues, and is operationally simplest.
All survive 2x cost (10 bps) and the 60/120/150d window grid keeps sign. Real BTC
funding drag is negligible at meanH ~4–6% of equity (~0.8 pp over 3.1y); alt_top10's
funding was net-POSITIVE for the long hedge (alt funding ran negative on book-on days).

Per-year Sharpe (btc, max4): bybit 2023 1.17→1.71, 2024 1.19→1.40, 2025 3.53→3.52;
binance 2023 1.72→2.61, 2024 1.73→2.15, 2025 1.93→2.18 — the recent-tilt flattens
with 2025 unchanged: the durable claim is REGIME-ROBUSTNESS. Honest caveat re-affirmed:
part of the raw return gain (+8.6 pp bybit) is bull-sample-specific long-BTC drift.
At max10 (reported): hedged MAR 7.73/7.39 vs control 6.17/6.00, Sharpe up both venues.

**Stage-A limits (why this is not promotion evidence):** overlay-level daily-close
fills for the hedge leg (BTCUSDT — most liquid perp, approximation reasonable), beta
estimated on ledger days only, no engine lifecycle, no forward demo. Next: Stage-B —
hedge leg in the rebalance engine (same causal beta definition), pre-registered
robustness battery, then forward-demo accumulation (demo push = operator decision).

Artifacts: `~/SHARED_DATA/continuous_hedge_overlay_2026-06-09/{cells.csv, report.json}`;
script `scripts/continuous_hedge_overlay_driver.py`.
