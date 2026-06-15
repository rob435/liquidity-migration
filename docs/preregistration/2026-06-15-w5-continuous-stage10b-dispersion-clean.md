# Pre-registration: W5 Continuous Stage 10b - Dispersion Hedge (gross-corrected) + BTC-vol stack

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Label:** `exploratory` (component reuse, no engine)
**Trigger:** operator bybit-primary steer ("if bybit-robust and not completely losing on
binance, worth it?") prompted re-examination of the Stage 10 dispersion NULL.

## Question

Stage 10 banked the cross-sectional dispersion hedge NULL ("venue-split, binance −0.273") —
but that was **confounded by a gross-neutrality bug**: the prior-month normalization was
omitted, so the dispersion intensity averaged 1.14–1.17 (~15% over-hedge). Gross-corrected, is
dispersion a real hedge-regime signal, and on which venue? Validate with the checks that killed
the Stage 4 sniper (λ-robustness, cost-robustness, sub-period stability) and controls that
isolate regime TIMING from the mean hedge LEVEL.

## Mechanism / arms (locked)

Dispersion intensity = trailing-10d-mean cross-sectional std of universe daily returns →
trailing-250 percentile → `1+λ(2pct−1)` → **prior-month normalized (causal, ~mean-1)**.
Component reuse via `build_full_ledger`. Arms at λ∈{0.25,0.5,0.75} × hedge cost {5,7.5,10}:
`C0`; `H_disp`; `H_btcvol` (Stage 8c); `H_stack` (BTC-vol×dispersion, renormed); `H_hash`
(random regime, same prior-month-normed construction — isolates timing vs noise); `H_const`
(constant hedge at the dispersion arm's mean intensity — isolates timing vs mean LEVEL).

## Decision rule (a priori)

Dispersion is a robust BOTH-VENUE signal iff, on BOTH venues: `H_disp` > `C0` AND > `H_hash`
AND > `H_const` at λ=0.5/1× cost, AND positive across λ and cost, AND sub-period ΔMAR vs C0
not < −0.05 in any third. (A single-venue robust result is a per-venue hedge note, not a
both-venue candidate.)

## Post-run results

ΔMAR vs C0 (selected):

| Venue | λ | disp | btcvol | stack | hash | const |
|---|---:|---:|---:|---:|---:|---:|
| bybit | 0.25 | +0.025 | +0.059 | +0.085 | **+0.045** | +0.008 |
| bybit | 0.5 | +0.041 | +0.108 | +0.087 | −0.145 | +0.019 |
| bybit | 0.75 | +0.051 | +0.100 | +0.064 | +0.004 | +0.036 |
| binance | 0.25 | +0.136 | +0.026 | +0.171 | −0.150 | −0.012 |
| binance | 0.5 | **+0.293** | +0.049 | **+0.368** | −0.206 | −0.102 |
| binance | 0.75 | **+0.516** | +0.056 | +0.601 | −0.224 | −0.123 |

Sub-period MAR Δ vs C0 (λ0.5, 1×, thirds): bybit disp **−0.058 / +0.023 / −0.059**; binance
disp **+0.311 / +0.576 / −0.032**.

## Verdict

**Dispersion is a ROBUST hedge regime on BINANCE, NOISE on bybit — venue-split (binance-real,
bybit-noise).** On binance it beats the random-regime hash (by +0.4–0.7) AND the constant-level
control (which HURTS binance, −0.10 — so the gain is regime TIMING, not the residual
over-hedge), at every λ and cost, monotone in λ (+0.14→+0.52), positive in 2/3 thirds strongly.
On bybit it is weak (+0.04) and NOT robust: it fails the hash control at λ=0.25 (hash got lucky
+0.045 > disp +0.025) and is negative in 2 of 3 sub-periods — the same fragility that falsified
the Stage 4 sniper. So it does NOT clear the both-venue bar, and under the bybit-primary steer
it is the WRONG venue (its edge is on binance).

**CORRECTS STAGE 10:** the Stage 10 "binance −0.273" was a gross-neutrality BUG; clean dispersion
is binance **+0.293** (sign flip). The Stage 10 "venue-split" CONCLUSION stands, but the venue
polarity reverses: dispersion is binance-good / bybit-noise (not bybit-good / binance-bad).

**Use:** for the bybit book, no change — the BTC-vol regime-hedge remains the one robust bybit
edge. For a binance sleeve, dispersion (or the BTC-vol×dispersion stack, binance +0.368) is a
real, robust hedge — a legitimate per-venue complement. Tier-3 real-money gate UNCHANGED.
Next (optional): a per-venue hedge (BTC-vol on bybit, dispersion/stack on binance) is a
defensible deploy if both sleeves run — but it is venue-specific tuning, not a both-venue signal,
and would be a forward-watch item, not a both-venue candidate.
