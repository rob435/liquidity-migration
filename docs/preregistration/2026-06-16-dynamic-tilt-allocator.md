# Pre-registration: Dynamic regime-timed capital tilt between LONG and CONTINUOUS

**Date:** 2026-06-16
**Author:** rob435 (operator goal: "dynamically time the tilt — shift capital to
whichever sleeve's regime is about to win — extract the alpha, make a robust system
that performs better than the best fixed-weight version")
**Stage:** rejected (clean both-venue NULL — see Verdict)
**Run label (max attainable):** `candidate` if it clears the rule below; otherwise
`exploratory`/NULL. (No real-money claim regardless — Tier-3 gate unchanged.)

## What's changing

A NEW object: a portfolio that splits capital daily between the two surviving
sleeves — LONG (v11a deployed profile) and CONTINUOUS (deployed fade ensemble incl.
the BTC-vol regime-hedge) — with the LONG weight `w_t ∈ [0,1]` set by a **causal,
parameter-light BTC regime signal**. Day-t blended return is
`p_t = w_t·r_long_t + (1-w_t)·r_cont_t` (gross = 1, a pure capital split).

This is **mechanistically distinct from all of W5**, which only ever modulated levers
*inside* the continuous book (hedge intensity, entry/exit/sizing). No prior work in
this repo allocates *between* the long and continuous sleeves. The "yearly rotation"
the goal references is not documented anywhere in the repo — Stage 0b generates it.

## Hypothesis (a-priori economic mechanism)

The two sleeves win in different BTC regimes:
- **LONG** is a sparse long-beta book — it earns when BTC is **trending up and calm**.
- **CONTINUOUS** is a mean-reversion fade + funding harvester with a BTC-uptrend gate
  — W5 established it **profits in high-volatility / dislocation regimes** and goes
  flat (cash) in downtrends.

So a causal regime signal built from **BTC trend** and **BTC volatility** should be
able to tilt capital toward whichever sleeve the current regime favors, capturing
return the best *static* blend cannot (the static blend must commit to one average
mix across all regimes). This is additive, not double-counting: the continuous gate
decides whether the fade book trades; it never adds long-beta, so in a calm uptrend
(gate may be flat → cash) the allocator tilting to LONG captures beta the continuous
book structurally cannot.

## The allocator (FROZEN — coded in `scripts/dynamic_tilt_allocator.py` before any run)

Two causal `[0,1]` regime scores, built with the **exact windows of the deployed
signal** (`continuous_regime.FROZEN_BTCVOL_REGIME`: 30d trailing measure, 250d
percentile; warm-up = neutral). Both use only BTC returns strictly before day `t`:
- `vol_score_t`   = trailing-30d BTC vol, percentiled over trailing 250 (the deployed
  BTC-vol regime signal, verbatim machinery).
- `trend_score_t` = trailing-30d BTC cumulative return, percentiled over trailing 250.

**Primary LONG-weight form (no free coefficients):**
```
w_t = clip( trend_score_t − vol_score_t + 0.5 , 0, 1 )
```
→ all-long when trend high & vol low; all-continuous when trend low & vol high;
neutral 0.5 otherwise. Symmetric, mean ≈ 0.5.

**A-priori robustness variants (reported, NOT used to select the winner):**
`trend_only` (w=trend_score), `vol_only` (w=1−vol_score), `binary`
(w=1 iff trend>0.5 & vol<0.5). Decision is made ONLY on the primary form.

There are **no tunable parameters** in the allocator: windows are inherited from the
deployed signal, the weight map is a fixed algebraic form. This is the central
overfit defense — there is nothing to mine.

## Benchmarks + negative controls

- **Oracle best fixed weight** (hindsight `argmax_w MAR` over a 0..1 grid) — the
  HARDEST static bar; clearing it means timing beats the best possible static blend.
- **Walk-forward best fixed weight** — fair operational bar (trailing-365d argmax-MAR).
- **Mean-fixed control** — `w_t = mean(w_dynamic)` constant: isolates TIMING from the
  average level.
- **Block-shuffle null** — circular block-bootstrap (block=63 trading days ≈ one
  quarter, the regime persistence scale) of the dynamic weight series, 300 seeds:
  preserves autocorrelation/turnover, destroys regime alignment. Dynamic must sit
  high in this null (predictive skill, not a slow-weight artifact).
- **Lagged-signal control** — 180d circular lag of the weight (destroys alignment).

## Predicted direction + magnitude

- If the regime genuinely times the rotation: dynamic MAR > oracle-fixed MAR on both
  venues, dynamic above the 90th percentile of the block-shuffle null, > mean-fixed,
  robust to 2× turnover cost, sign-consistent across chronological thirds.
- Failure mode (what falsifies): dynamic ≤ oracle-fixed, OR inside the shuffle null
  (timing adds nothing beyond having a wandering weight), OR the edge is one-venue /
  one-third only, OR it dies at 2× cost. Given only ~3–4 regime episodes in 2.9y, the
  most likely honest outcome is NULL — the shuffle null is built precisely to catch a
  false positive driven by too few independent regime switches.

## Roots that will be touched
- [x] bybit_full_pit (read-only: reconstruct sleeve returns + BTC regime)
- [x] binance_full_pit (read-only: reconstruct sleeve returns + BTC regime)
- [ ] forward demo/paper (not touched; demo book unchanged by this research)

No live config is modified. Sleeve return series are produced by the canonical
`scripts/equity_curves.sh` (deployed profiles), so they are full-PIT, causal, costed,
funded, ledger-backed.

## Decision rule (a priori — adapted Tier-2, bybit-primary per 2026-06-15 steer)

The dynamic allocator is a **CANDIDATE (→ extract into a system)** iff ALL hold:
1. **Beats the oracle best fixed weight** (ΔMAR > 0) on bybit AND ΔMAR ≥ 0 (not
   opposite-sign) on binance. [bybit-robust + binance not contradicting.]
2. **Beats both negative controls** on both venues: dynamic-MAR above the 95th
   percentile of the block-shuffle null AND > mean-fixed AND > lagged-signal.
3. **Survives 2× turnover cost**: still beats the oracle fixed weight on bybit.
4. **Sign-stable across chronological thirds** (ΔMAR vs oracle-fixed ≥ 0 in ≥ 2/3
   thirds per venue; not carried by one third).
5. **Non-degenerate**: mean LONG weight in [0.15, 0.85] and weight actually moves
   (both sleeves receive meaningful capital across regimes).

Intermediate outcomes:
- Beats the shuffle/mean controls but NOT the oracle fixed weight → "real timing
  skill, not harvestable above the best static blend" (honest partial; not a
  candidate). 
- Inside the shuffle null → **NULL**: the yearly rotation is not timeable; the
  apparent pattern was too few regime episodes (the overfit trap the goal names).

The strict **Tier-3 real-money gate is unchanged**; the most this can earn is a
demo/paper forward-watch candidate.

## Run command
```bash
# Stage 0 data (canonical deployed profiles, both venues):
bash scripts/equity_curves.sh --sleeves long,continuous --root ~/SHARED_DATA/bybit_full_pit   --venue bybit   --start 2023-04-01 --end 2026-06-03 --out ~/SHARED_DATA/dynamic_tilt_2026-06-16/bybit_curves
bash scripts/equity_curves.sh --sleeves long,continuous --root ~/SHARED_DATA/binance_full_pit --venue binance --start 2023-04-01 --end 2026-05-01 --out ~/SHARED_DATA/dynamic_tilt_2026-06-16/binance_curves
# Stage 0b descriptive, then the pre-registered Stage 1:
.venv/bin/python scripts/dynamic_tilt_allocator.py stage0b
.venv/bin/python scripts/dynamic_tilt_allocator.py stage1
```

## Post-run results

Run 2026-06-16 (uncommitted code, established W5 pattern). Artifacts:
`~/SHARED_DATA/dynamic_tilt_2026-06-16/` (`stage0b_descriptive.json`,
`stage1_allocator.json`, per-venue `*_curves/`). Sleeve series freshly reconstructed
by `scripts/equity_curves.sh` (run_label `full_pit_universe` long /
`continuous_research_stage_not_promoted` continuous), both venues.

**Stage 0b (descriptive).** Common window bybit 2023-04-17..2026-05-13 (1123d),
binance 2023-04-17..2026-04-11 (1091d). The two sleeves are **nearly uncorrelated**
(ρ = **0.034** bybit / **0.066** binance). Sleeve MARs: long 2.33/2.13,
continuous 4.02/4.36. **Oracle (hindsight) best fixed weight: 47% long → MAR 4.645
(bybit); 18% long → MAR 4.944 (binance)** — i.e. the best static blend already beats
either sleeve alone (diversification), and the optimal mix DIFFERS by venue. The
"yearly rotation" is real but venue-identical and concentrated in ONE episode:
per-year winner 2023 cont / **2024 long** / 2025 cont / 2026 cont on BOTH venues.
Both sleeves earn MORE in low BTC-vol (long .213 vs .094; cont .468 vs .251 bybit),
so **BTC vol is a common adverse factor, not a differentiator**.

**Stage 1 (the pre-registered test). PRIMARY = NULL on both venues.**
`dMAR vs oracle-fixed = −0.74 (bybit) / −1.21 (binance)`; dynamic sits at the
**76th / 69th percentile of the 300-seed block-shuffle null** (bar = ≥95th) and is
WORSE than its own mean-weight (timing hurts). Dies further at 2× cost. Fails every
clause of the decision rule.

**No candidate clears the both-venue bar** (dMAR vs oracle-fixed, bybit / binance):
primary −0.74 / −1.21; trend_only −0.51 / −1.73; vol_only −1.08 / −0.58; binary
−1.75 / −1.98; inverse_vol −0.23 / −1.18; inverse_vol_60 −0.26 / −0.91; rel_mom_63
**+0.57 / −3.00**; rel_mom_126 −0.90 / −2.17. The lone positive cell — relative
momentum at a 63d lookback on bybit (+0.57, 94th pct) — is **venue-specific noise**:
on binance the SAME rule is **−3.00 at the 3rd percentile of the shuffle null** (worse
than 97% of randomly-timed weights), and its bybit lookback neighborhood is a lone
spike amid negatives (`dMAR by lb {21:−1.28, 42:−2.48, 63:+0.57, 90:−0.40, 126:−0.90}`).
This is the Stage-4 sniper / Stage-10 dispersion venue-split trap, caught by the
pre-registered controls.

**Constructive (non-timing) note.** Because the sleeves are uncorrelated, a *fixed*
modest long tilt diversifies: a fixed **30% long / 70% continuous** beats
continuous-only on BOTH venues (MAR 4.45 vs 4.02 bybit; 4.72 vs 4.36 binance),
sign-stable across thirds. Causal risk-parity is NOT robust (bybit +0.39, binance
−0.60). This is "turn the LONG sleeve on at a fixed weight" (open operator decision
#4), NOT a dynamic-timing alpha.

## Verdict

**REJECTED — clean both-venue NULL for the dynamic-tilt hypothesis.** Regime-timing
(BTC trend/vol), relative momentum, and inverse-vol all fail to beat the best fixed
weight and show NO predictive timing skill above the block-shuffle null on both
venues. The goal's own stated worry is confirmed empirically: the apparent yearly
rotation is ~one macro episode and is not timeable without overfitting (the single
"win", rel_mom_63 bybit, is venue-specific noise that fails catastrophically on
binance). The best fixed weight is near-optimal; dynamic allocation adds nothing
robust. Tier-3 real-money gate untouched. Constructive follow-up belongs to the open
LONG-capital decision (a *fixed* diversifying blend), not to a dynamic allocator.
