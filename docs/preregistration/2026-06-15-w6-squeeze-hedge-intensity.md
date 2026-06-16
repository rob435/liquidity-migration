# Pre-registration: W6 A4 — squeeze × hedge-intensity overlay

**Date:** 2026-06-15
**Author:** rob435 (operator-directed W6 bybit-first program)
**Stage:** run-pending (binding). Filed BEFORE the run, per AGENTS.md.

## Why this stage exists
A1 (squeeze-proxy SIZING) is REJECTED — admissible-not-harvestable, the W5 diffuse-edge
root cause confirmed on the orderflow axis (sizing UP high-squeeze fades lifts return but
worsens drawdown more → MAR falls). Per W5, the ONLY mode that harvests is a
**non-selection OVERLAY that keeps the whole book and hedges the squeeze tail** (the live
BTC-vol regime-hedge, Stage 8c). A4 tests whether the orderflow squeeze signal helps in
THAT mode instead of as sizing.

## Thesis
A day when the book is collectively short CROWDED squeezes (high aggregate OI-buildup
across active fades) is a market-wide squeeze-risk day → **hedge MORE**. This composes
with (does not fight) the deployed BTC-vol regime-hedge: a second, orderflow-based
mean-1 hedge-intensity multiplier, distinct from BTC's own volatility.

## What's changing (the intervention)
A causal, mean-1 **aggregate-book-squeeze hedge intensity**, composed MULTIPLICATIVELY
with the frozen BTC-vol regime intensity (λ=0.5, the live deliverable):
- Per-entry squeeze score = within-symbol causal-expanding z of `oi_chg_24h` (the A1 score).
- Per ledger day d: `agg[d]` = equal-weighted mean of the squeeze z over fades ACTIVE on d
  (`signal_ts ≤ d_end AND exit_ts > d_start`); causal (entries ≤ d only). Days with too few
  covered active fades → neutral.
- `sq_intensity[d] = 1 + λ_sq·(2·pct[d] − 1)`, pct = trailing-250 percentile of `agg[d]`
  among PRIOR days (causal; warm-up → 1.0), bounded [1−λ_sq, 1+λ_sq], mean-1.
- `combined[d] = btcvol_intensity[d] · sq_intensity[d]`, passed as `hedge_intensity` to
  `build_full_ledger`. Hedge-only; ALL trades kept; book unchanged.

Component reuse only (Stage-0 frozen component ledgers + the merged hedge layer); no
engine backtests, no data-root writes.

## Arms
- `C0` no regime (intensity None) = frozen control.
- `H` BTC-vol regime only (λ=0.5) = the LIVE deliverable baseline.
- `SH_{λsq}` BTC-vol × squeeze, λ_sq ∈ {0.25, 0.5}.
- `shuffle` control (≥5 seeds): BTC-vol × squeeze with the daily `agg` series permuted
  across days (kills timing, keeps the marginal) — the decisive "does book-squeeze TIMING
  add value beyond BTC-vol" null.
- `hash` control: BTC-vol × random daily regime (Stage-8c R5 style).
- Cost stress: `H` and `SH` at 1.5× hedge cost.

## Decision rule (a priori) — bybit-primary
A4 is a demo/paper FORWARD-WATCH candidate iff, vs the **H (BTC-vol) baseline**:
1. **bybit ΔMAR(SH vs H) > 0 for BOTH λ_sq** (robust across the free param), AND
2. beats the **shuffle control** distribution (≥5 seeds) AND the hash control on bybit, AND
3. SH bybit MAR ≥ H in **≥2/3 chronological thirds**, AND
4. survives **1.5× hedge cost** (bybit ΔMAR vs H still > 0), AND
5. binance ΔMAR(SH vs H) ≥ ~0 (not completely losing), AND
6. mean composed intensity in [0.95, 1.05] (no covert leverage).
Otherwise: the squeeze adds nothing beyond BTC-vol as a hedge regime → logged NULL, the
BTC-vol regime-hedge stands alone. Tier-3 real-money gate UNCHANGED.

Honest prior: W5 Stage 8d/8e/8g found BTC-vol is the UNIQUE both-venue hedge regime among
6 tested signals (book-vol/book-DD/dispersion/multifactor/aggregate-funding all failed or
venue-split). Aggregate book-SQUEEZE (OI-crowding) is a NEW, thesis-aligned signal not in
that set, but the prior that "alternative hedge regimes rarely beat BTC-vol on both venues"
is strong. The shuffle/hash controls + bybit-robustness decide.

## Roots touched
- [ ] bybit_full_pit / binance_full_pit — READ-ONLY (Stage-0 component ledgers + BTC
  klines/funding). No dataset writes, no selection knob tuned.
- [x] forward demo/paper — only if it passes and the operator green-lights.

## Run command
```bash
POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
    scripts/w6_squeeze_hedge_intensity.py --venues bybit,binance \
    --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
    --out ~/SHARED_DATA/w6_squeeze_hedge_intensity_2026-06-15
```

## Post-run results

**Run 2026-06-15** (`scripts/w6_squeeze_hedge_intensity.py`, component reuse, no engine).
Coverage: bybit 616 entries / 486 of 673 days with a squeeze score; **binance 0** (OI history
~6 weeks → no squeeze signal; binance SH==H exactly, leg vacuous — data-gated by E4).

bybit MAR (vs **H = BTC-vol baseline**; C0 = no regime = 4.748):

| arm | MAR | ΔMAR vs H | maxDD |
|---|---:|---:|---:|
| C0 (no regime) | 4.748 | −0.108 | −0.0527 |
| H (BTC-vol λ0.5) | 4.856 | +0.000 | −0.0526 |
| SH λ_sq=0.25 | 4.769 | **−0.087** | −0.0528 |
| SH λ_sq=0.5 | 4.913 | **+0.057** | −0.0529 |
| shuffle controls (8 seeds) | — | −0.343…**+0.095** (mean −0.069) | — |
| hash control | 4.551 | −0.305 | −0.0540 |
| SH λ0.5 @1.5× hedge cost | 4.764 | **−0.092** (vs H@1.5× −0.070) | −0.0529 |

Gate scorecard (bybit-primary, vs H): **gate1 FAIL** (λ0.25 −0.087, not robust across λ);
**gate2 FAIL** (real +0.057 < shuffle max +0.095; 2/8 shuffles beat it → within noise);
gate3 PASS (thirds 2/3); **gate4 FAIL** (1.5× hedge cost −0.070); gate5 PASS-vacuous
(binance no OI); gate6 PASS (mean intensity ~0.98).

## Verdict
**REJECTED (NULL — squeeze adds nothing beyond BTC-vol as a hedge regime).** The λ=0.5
+0.057 is **within the 8-seed shuffle-control noise band** (which swings −0.34…+0.10 from
pure hedge-resize-timing luck; 2/8 seeds beat the real value) and is **not robust across λ**
(λ=0.25 is −0.087) and **cost-fragile** (1.5× hedge cost → −0.070, the overlay adds hedge
turnover). The multi-seed shuffle is decisive — exactly the Stage-4 single-seed trap avoided.
Mechanism: aggregate book-squeeze (OI-crowding) is largely collinear with the BTC-vol regime
it composes on, so it adds turnover/cost without independent timing value. This **extends the
W5 Stage 8d/8e/8g result** (BTC-vol is the unique both-venue hedge regime) to the orderflow
axis. Combined with A1 (sizing NULL): the orderflow OI-squeeze signal is a real IC that
harvests **neither as sizing nor as a hedge regime**. binance squeeze is data-gated (E4, OI
forward tape). **No deployment; the live BTC-vol regime-hedge stands alone; Tier-3 unchanged.**
Next high-prior axis = Track B cost/execution alpha (orthogonal, untouched, no diffuse-edge
conflict).
