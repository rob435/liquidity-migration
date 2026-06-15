# Pre-registration: W5 Continuous Stage 8b - Lower-Turnover Regime Hedge

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Lead from:** Stage 8
(`docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md`) — R4
regime-hedge improved pooled MAR +0.078 on BOTH venues and beat the hash control by
+0.69, but missed the +0.1 bar and the 2×-hedge-cost arm collapsed it (binance
flipped negative). The binding constraint is **hedge turnover**: the continuous
daily-percentile intensity resizes the hedge every day.

## Question

Does a **lower-turnover** version of the *same* BTC-vol regime-hedge — a persistent,
banded intensity that resizes the hedge only on regime transitions — capture the
Stage 8 timing benefit with enough less hedge churn to clear the +0.1 pooled-MAR bar
AND survive the 2×-cost stress, on both venues?

This is mechanistically distinct from Stage 8 (continuous daily intensity → discrete,
persistent, hysteretic) and is motivated by the cost-fragility *mechanism*, not by
peeking at Stage 8's λ/threshold. It is NOT a re-tune: the band amplitude is locked
to match Stage 8's effective tercile-midpoint intensities (Stage 8 at pct≈0.167/0.833
gives intensity ≈0.67/1.33), so amplitude is held and only the *turnover profile*
changes.

## Mechanism (locked before the run)

Same engine hook `hedge_intensity` (default None → byte-identical; V0 reproduces the
Stage 0 ensemble). Same causal BTC-vol regime signal as Stage 8: trailing 30-day
population std of BTC daily hedge returns (causal, through day−1), ranked to a
percentile `pct_d` over the trailing **K=250** prior vol values.

**Banded, hysteretic intensity (locked):** three levels `{low: 0.7, mid: 1.0,
high: 1.3}` (mean ≈ 1.0 over ~equal-mass percentile terciles). State machine,
causal, day by day:

- enter **high** when `pct_d ≥ 0.667`; leave high only when `pct_d < 0.55`;
- enter **low** when `pct_d ≤ 0.333`; leave low only when `pct_d > 0.45`;
- otherwise hold the current band.
- warmup (insufficient vol/percentile history): `mid` (intensity 1.0).

Hysteresis (enter/exit gaps of ~0.12) prevents day-to-day flip-flop at the band
boundaries, so the intensity — and thus the added hedge turnover — changes only on
genuine regime transitions.

**Hypothesis:** the banded intensity holds the hedge at a regime-appropriate level
for long stretches, so it adds far less hedge turnover than Stage 8's daily intensity
while preserving the "more hedge in turbulence" timing; the cleaner cost profile lets
the +0.078 Stage-8 signal clear +0.1 and survive 2× cost.

## Arms (locked)

- `V0_control`: frozen hedge (`hedge_intensity=None`) — the Stage 0 ensemble.
- `R4b_banded_hedge`: the banded hysteretic BTC-vol intensity above.
- `R4b_banded_2xcost`: same, hedge `cost_bps` doubled (10 vs 5) — cost-stress arm.
- `R5b_hashweek_banded` (negative control): the same three levels {0.7,1.0,1.3}
  assigned by `hash(week)%3` (week = `floor((day−day0)/7d)`), held constant within
  each week — mean ≈ 1, comparable low-turnover persistence, **no market content**.
  If R4b does not beat R5b, the band timing carries no regime information.

## Constraints (binding)

- entries / exits / breadth / book sizing identical to V0 (only the hedge leg
  changes — asserted via V0 = Stage 0 ensemble);
- realized mean hedge intensity ∈ [0.95, 1.05] (constant average hedge — no
  hedge-level/leverage confound);
- hedge turnover/funding cost charged at the new intensity (engine-recomputed);
- funding ON (bybit modeled / binance partial-disclosed); causal regime only.

## Metrics (per arm, per venue, pooled)

- total return, MAR, max drawdown, worst day; R1-compatible monthly returns;
- realized mean intensity; **hedge turnover** = sum |Δintensity| and **n band
  changes** and total hedge cost (vs Stage 8 R4 — must be materially lower);
- MAR by BTC-vol tercile (fragility); chronological-third MAR-delta stability.

## Decision rule (a priori) / Pass bar

`R4b_banded_hedge` is admissible only if, vs `V0`:

1. positive total return on **both** venues;
2. **pooled MAR delta `> +0.1`**;
3. no venue MAR delta `< -0.5`;
4. max drawdown not worse than **+10% relative** on either venue;
5. realized mean intensity ∈ [0.95, 1.05] both venues;
6. survives `R4b_banded_2xcost` (still pooled MAR delta `> +0.1`);
7. the negative control `R5b_hashweek_banded` pooled MAR delta is strictly weaker;
8. not carried by one venue, one chronological third, or one vol tercile;
9. hedge turnover materially below Stage 8 R4 (else it is not a lower-turnover test).

Default label **`exploratory`** — historical. A pass nominates a demo/paper hedge
shadow only; the binary gate + frozen hedge remain the live control until a Tier-3
forward verdict.

## Falsifier

Reject if it works on one venue only, is matched/beaten by R5b, fails the 2×-cost
arm, worsens drawdown, needs a mean intensity outside [0.95,1.05], the MAR gain lives
in one chronological third or vol tercile, or it does not actually reduce hedge
turnover vs Stage 8 R4. If 8b also misses the bar, the BTC-vol regime-hedge family is
banked as "real directional signal, sub-threshold under realistic hedge cost" and
the next lever is a different stage (exit / entry-style / sniper).

## Window, roots, universe

Window `2023-04-01 <= signal_ts < 2026-05-01`; reuses the Stage 0 component ledgers
(V0 entries, frozen); rebuilds the ensemble per arm with the hedge knob. Roots
read-only; writes only to `~/SHARED_DATA/w5_continuous_stage8b_*`.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage8b_lowturnover_hedge.py \
  --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
  --vol-window 30 --pct-window 250 \
  --out ~/SHARED_DATA/w5_continuous_stage8b_lowturnover_hedge_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage8b_lowturnover_hedge_2026-06-14/`: per-arm
ledgers + monthly + report JSON (R1-compatible); `hedge_intensity_{venue}.csv`;
`stage8b_summary.{json,md}`, `stage8b_metrics.csv`, `turnover.csv`, `vol_tercile.csv`,
`code_hash.txt`.

## Post-run results

Run UTC 2026-06-14, both venues, reusing the Stage 0 component ledgers, git HEAD
`5dd4e12` (code hash `76962b79…`), bands [0.7,1.0,1.3], vol_window=30, pct_window=250.
Artifacts `~/SHARED_DATA/w5_continuous_stage8b_lowturnover_hedge_2026-06-14/`. V0
reproduces the Stage 0 ensemble (only the hedge differs).

| Venue | Arm | Return | MAR | MaxDD | Mean int | Int changes | Hedge cost |
|---|---|---:|---:|---:|---:|---:|---:|
| bybit | V0 | 0.7707 | 4.748 | −5.27% | 1.000 | 0 | −0.0030 |
| bybit | R4b banded | 0.7598 | 4.685 | −5.26% | 0.993 | 26 | −0.0033 |
| bybit | R4b 2x-cost | 0.7543 | 4.645 | −5.27% | 0.993 | 26 | −0.0066 |
| bybit | R5b hashweek | 0.7548 | 4.390 | −5.58% | 1.029 | 75 | −0.0031 |
| binance | V0 | 0.6428 | 5.255 | −3.97% | 1.000 | 0 | −0.0041 |
| binance | R4b banded | 0.6492 | 5.307 | −3.97% | 0.985 | 28 | −0.0039 |
| binance | R4b 2x-cost | 0.6427 | 5.216 | −4.00% | 0.985 | 28 | −0.0079 |
| binance | R5b hashweek | 0.6404 | 4.809 | −4.32% | 1.024 | 76 | −0.0046 |

Pooled MAR delta vs V0: R4b **−0.005** (bybit **−0.063**, binance **+0.052**);
2x-cost −0.071; R5b hashweek −0.402. Turnover reduced ~25× (26–28 intensity changes
vs Stage 8's ~660) — but hedge cost is essentially unchanged (−0.0033 / −0.0039 vs
Stage 8 R4 −0.0033 / −0.0040): the few band changes are LARGE 0.6-step jumps, so the
total hedge turnover cost did not fall.

## Verdict

**NULL — and it falsifies the lower-turnover fix.** The banded hysteretic intensity
cut the NUMBER of hedge resizes ~25× but NOT the hedge cost (the few changes are big
0.6-step jumps), and the coarse, hysteresis-lagged response MISTIMED the hedge:
pooled MAR delta **−0.005**, with bybit **flipping from +0.108 (Stage 8 continuous)
to −0.063 (banded)** while binance stayed stable (+0.048→+0.052). Two findings:

1. The turnover-cost fragility is **not fixable by reducing the number of resizes** —
   the cost lives in resize MAGNITUDE; banding trades many small resizes for few big
   ones at ~equal total cost.
2. The Stage-8 pooled +0.078 was partly **bybit form-luck**: bybit is highly
   sensitive to the exact hedge-intensity form (+0.108 continuous → −0.063 banded),
   whereas binance is form-stable at ~+0.05. The robust, form-stable regime-hedge
   benefit is ~**+0.05 (binance)** — well below the +0.1 bar; bybit does not
   generalize across forms.

R4b still beats the R5b hashweek control (−0.402), so the BTC-vol regime carries
*some* information — but neither tested form (continuous Stage 8, banded 8b) clears
the Tier-2 bar, and the benefit is thin, cost-bound, and venue-form-fragile.
Falsifier outcome: **triggered** (sub-threshold, split venues, no turnover-cost
reduction).

**Banked conclusion for the regime-hedge family:** the BTC-vol hedge-intensity lever
carries a real but **sub-threshold** directional signal (~+0.05 form-stable), not
harvestable above the +0.1 MAR bar net of realistic hedge cost in either the
continuous or the banded form. Two mechanistically-distinct forms tried — do not keep
re-parameterizing this lever. **Next lever is a different stage:** Stage 3 exit alpha
(the exit lifecycle is untouched and drives both return and drawdown), then Stage 2
entry-style / Stage 4 sniper. A genuinely different regime *signal* (cross-sectional
dispersion / multifactor) feeding the hedge would be a separate future receipt, lower
priority given the lever is thin.
