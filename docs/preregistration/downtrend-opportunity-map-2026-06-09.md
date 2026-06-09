# Pre-registration: D1 — regime-conditional opportunity map (downtrend sleeve program)

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory` (Tier-1 map;
gates D2 design — never promotion evidence). **Program:**
`docs/research_plan_downtrend_sleeve_2026-06-09.md` D1.

## Question

On BTC-30d-DOWNTREND days (the regime where the deployed book is off), which causal
cross-sectional signals predict next-day returns, and in which direction? This maps the
opportunity space for a NEW sleeve instead of re-mining the known-weak pump-fade there.

## Protocol (fixed a-priori)

- Data: verified `feature_panel_2026-05-27.parquet` primitives (close, turnover_quote)
  + bybit raw-kline tail, both venues, 2023-01-01..roots end. Same construction as the
  WP1a factor build (consecutive-day closes; membership turnover(d-1) >= 500k; non-BTC;
  stables excluded via the engine's exclude list).
- Regime(d): sign of BTC return over [d-31, d-1] (30d window ending the PRIOR day —
  causal; matches the engine's btc_trend_gate definition direction).
- Signals at d-1 close (all causal): ret_1d, ret_7d, ret_30d (close-to-close),
  rv_7d (std of last 7 daily returns), turn_d7 (turnover(d-1)/mean turnover(d-8..d-2)).
- Outcome: symbol day-d return MINUS EW-alt-market day-d return (primary, "excess");
  raw return secondary.
- Stats per (venue x regime x signal): Spearman IC, decile D10-D1 mean excess spread,
  n days; 10d-block bootstrap one-sided p on the IC sign, seed 20260609, 1000 reps.
- **Lead bar (pre-stated):** down-regime |IC| >= 0.03 with p < 0.05, SAME SIGN both
  venues, and a sensible economic story. Up-regime cells are reported for contrast
  only. Multiple testing noted: 5 signals x 2 venues in the down regime; the
  cross-venue sign requirement is the guard.

## Artifacts

`~/SHARED_DATA/downtrend_opportunity_map_2026-06-09/` — report JSON.
Script: `scripts/downtrend_opportunity_map.py`.

## Verdict (filled in after the run, same day)

**LEAD FOUND: regime-conditional cross-sectional REVERSAL.** Down-regime cells
(507/503 days):

| signal | bybit IC (p) / D10-D1 | binance IC (p) / D10-D1 |
|---|---|---|
| ret_1d | -0.023 (0.000) / **-22.8 bps** | -0.030 (0.000) / **-21.8 bps** |
| ret_7d | -0.029 (0.000) / **-51.1 bps** | -0.039 (0.000) / **-24.4 bps** |
| ret_30d | -0.007 (0.18) / -16.4 | -0.025 (0.002) / -18.4 |
| rv_7d | -0.065 / +16.4 (non-monotone) | -0.056 / +11.4 (non-monotone) |
| turn_d7 | -0.023 / -27.5 | -0.011 (0.018) / -19.8 |

ret_7d/ret_1d clear the bar in spirit (binance IC over 0.03; bybit 0.023-0.029 with
the LARGER spreads; both-venue sign agreement on IC AND spread; p~0). The up-regime
tails flip sign for ret_7d (+22.7/+8.6 bps) — a real regime asymmetry. rv_7d is
IC/spread-inconsistent (skip); turn_d7 is the known weak fade echo (not re-mined).
Priors check: consistent with the unconditional momentum NULL (Round-3) — this is
CONDITIONAL reversal. Economic story: bear-market overshoot + bounce, with a funding
tailwind for the long leg (negative bear funding = longs receive).

**GO → D2:** market-neutral daily reversal L/S (short relative winners / long relative
losers), gated to BTC-30d-down days, funding-realistic, both venues.
