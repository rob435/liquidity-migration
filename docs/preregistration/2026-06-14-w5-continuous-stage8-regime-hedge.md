# Pre-registration: W5 Continuous Stage 8 - Regime-Conditioned Hedge (R4)

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/09_stage8_regime_response.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Binding prior:** E2 (`docs/preregistration/2026-06-12-e2-regime-response-family.md`)
closed the V1/V2 bounded-threshold ENTRY-GATE family (NULL, pooled MAR −1.96 /
−2.52). E2's own conclusion: "down/euphoria-regime treatment remains: **hedge** +
per-name stops/caps." This stage tests exactly that.

## Question

Can a **causal, mechanistically-distinct** regime response that modulates only the
**hedge intensity** — leaving the V0 binary-uptrend entry gate and every trade
untouched — beat the V0 frozen control on pooled MAR across both venues?

This is NOT a V1/V2 variant: it changes neither the entry gate, the trade
population, nor the book sizing. It reallocates the existing BTC hedge across
regimes at **constant average hedge** (mean-1), so any MAR change comes from
hedge *timing*, not from more/less hedging overall or from leverage.

## Mechanism (locked before the run)

Engine hook `hedge_intensity` in `continuous_rebalance.apply_rebalance_rule`
(threaded via `continuous_forward_replay.build_full_ledger`), additive, default
`None` → byte-identical (343 rebalance/hedge/continuous/forward tests pass; V0 with
the hook reproduces the Stage 0 ensemble exactly: bybit 0.7707/4.748/−5.27%). It is
a causal per-day multiplier on the hedge leg only: `H(day) = clip(-beta,0,cap) *
scale * intensity(day)`. The book gross/funding/cost scale is untouched, so
entries/breadth/exits are identical to V0; only the hedge notional (and its
turnover/funding cost) is reallocated. Because the hedge PnL feeds the
drawdown-half-scale state, the full ledger is rebuilt through the engine (no
post-processing).

**Regime signal (causal, drift-robust, mean-1):**

1. BTC realized vol at ledger day `d`: trailing 30-day population std of the BTC
   daily hedge-instrument returns over days strictly before `d` (causal). Days
   with `< 10` prior obs get intensity 1.0 (warmup).
2. Percentile rank `pct_d` of `vol_d` among the trailing **K = 250** prior vol
   values (causal). Percentile (not z) so it is robust to a vol-level trend and
   stays mean-1 by construction.
3. `intensity(d) = 1 + λ·(2·pct_d − 1)`, **λ = 0.50** (locked) → intensity ∈
   [0.5, 1.5], mean ≈ 1. High BTC vol (pct→1) → hedge up to 1.5×; calm (pct→0) →
   hedge down to 0.5×.

**Hypothesis (locked direction):** the short-alt fade book draws down when alts
squeeze with the market, and squeeze risk rises with BTC volatility; hedging
*more* in high-BTC-vol regimes and *less* in calm regimes cuts drawdown at constant
average hedge, raising MAR. The opposite direction is a SEPARATE future receipt,
not this one.

## Arms (locked)

- `V0_control`: frozen hedge (`hedge_intensity=None`) — the Stage 0 ensemble.
- `R4_btcvol_hedge`: BTC-vol percentile intensity above.
- `R4_btcvol_hedge_2xcost`: same, but hedge `cost_bps` doubled (10 vs 5) — the
  registered cost-stress arm.
- `R5_hash_hedge` (negative control): `intensity(d) = 1 + λ·(2·pct_hash(d) − 1)`
  where `pct_hash` is a deterministic per-day hash bucket in [0,1] (no market
  content), same λ and mean-1 structure. If R4 does not beat R5, hedge
  reallocation carries no regime information.

## Constraints (binding)

- entries / exits / breadth / book sizing identical to V0 (only the hedge leg
  changes — asserted: V0 reproduces the Stage 0 ensemble);
- mean hedge intensity ≈ 1 (constant average hedge — not a hedge level change,
  not leverage); realized mean intensity reported and gated [0.95, 1.05];
- hedge turnover/funding cost charged at the new intensity (engine-recomputed);
- funding ON (bybit modeled / binance partial-disclosed);
- causal regime signal only (vol through day−1; rmom-latency lesson).

## Metrics (per arm, per venue, pooled)

- total return, MAR, max drawdown, worst day;
- realized mean hedge intensity; mean/peak hedge_ratio vs V0; total hedge cost;
- R1-compatible monthly returns (`scripts/r1_robustness.py`);
- MAR by BTC-vol tercile (fragility — is the effect one regime bucket?);
- chronological-third MAR-delta stability.

## Decision rule (a priori) / Pass bar

`R4_btcvol_hedge` is admissible only if, vs `V0`:

1. positive total return on **both** venues;
2. **pooled MAR delta `> +0.1`**;
3. no venue MAR delta `< -0.5`;
4. max drawdown not worse than **+10% relative** on either venue (R4's stated job
   is DD management — it must not worsen DD);
5. realized mean hedge intensity ∈ [0.95, 1.05] both venues (constant average
   hedge — no hedge-level/leverage confound);
6. survives `R4_btcvol_hedge_2xcost` (still pooled MAR delta `> +0.1`);
7. the negative control `R5_hash_hedge` pooled MAR delta is **strictly weaker**;
8. not carried by one venue, one chronological third, or one vol tercile.

Default label **`exploratory`** — historical. A pass nominates a demo/paper hedge
shadow only; the binary uptrend gate + frozen hedge remain the live control until a
forward verdict (Tier-3, `STATE.md`).

## Falsifier

Reject as regime-hedge alpha if it works on only one venue, is matched/beaten by
the R5 hash control, fails the 2x-hedge-cost arm, worsens drawdown, needs a mean
intensity outside [0.95,1.05] (a hedge-level change masquerading as timing), or the
MAR gain lives in a single chronological third or vol tercile.

## Window, roots, universe

- Window `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap).
- Reuses the **Stage 0** component ledgers (V0 entries, frozen) under
  `~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage0_candidate_tape_2026-06-14/{component}/`;
  rebuilds the ensemble per arm with the hedge knob. Roots read-only; writes only
  to `~/SHARED_DATA/w5_continuous_stage8_*`. Forward demo/paper untouched.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage8_regime_hedge.py \
  --venues bybit,binance --stage0-tag w5_continuous_stage0_candidate_tape_2026-06-14 \
  --lam 0.5 --vol-window 30 --pct-window 250 \
  --out ~/SHARED_DATA/w5_continuous_stage8_regime_hedge_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage8_regime_hedge_2026-06-14/`: per-arm
`ensemble_hedged_ledger.csv`, `volume_event_best_monthly.csv`,
`volume_event_research_report.json` (R1-compatible); `hedge_intensity_{venue}.csv`;
`stage8_summary.{json,md}`, `stage8_metrics.csv`, `vol_tercile.csv`,
`code_hash.txt`.

## Post-run results

Run UTC 2026-06-14, both venues, reusing the Stage 0 component ledgers (V0 entries,
frozen), git HEAD `5dd4e12` (engine hook + Stage 8 code uncommitted; code hash
`1b96771a…`), frozen config hash `1fc760f1…`, λ=0.5, vol_window=30, pct_window=250.
Artifacts `~/SHARED_DATA/w5_continuous_stage8_regime_hedge_2026-06-14/`. V0 (hook
absent) reproduces the Stage 0 ensemble exactly (bybit 0.7707/4.748; binance
0.6428/5.255), so only the hedge differs across arms.

| Venue | Arm | Return | MAR | MaxDD | Mean int | Hedge cost |
|---|---|---:|---:|---:|---:|---:|
| bybit | V0 | 0.7707 | 4.748 | −5.27% | 1.000 | −0.0030 |
| bybit | R4 | 0.7876 | 4.856 | −5.26% | 0.985 | −0.0033 |
| bybit | R4 2x-cost | 0.7832 | 4.822 | −5.27% | 0.985 | −0.0066 |
| bybit | R5 hash | 0.7520 | 4.443 | −5.49% | 1.001 | −0.0072 |
| binance | V0 | 0.6428 | 5.255 | −3.97% | 1.000 | −0.0041 |
| binance | R4 | 0.6527 | 5.303 | −3.99% | 0.989 | −0.0040 |
| binance | R4 2x-cost | 0.6446 | 5.195 | −4.02% | 0.989 | −0.0081 |
| binance | R5 hash | 0.6257 | 4.332 | −4.69% | 1.000 | −0.0097 |

Pooled MAR delta vs V0: R4 **+0.078** (bybit +0.108, binance +0.048 — same sign
both venues); R4 2x-cost **+0.008** (bybit +0.074, binance −0.060); R5 hash
**−0.614**. Mean hedge intensity 0.985/0.989 (constant average hedge, in band); max
drawdown unchanged. Vol-tercile attribution (R4−V0 cumulative return, low/mid/high
BTC-vol): bybit +0.0027 / +0.0014 / +0.0055; binance −0.0038 / +0.0054 / +0.0045 —
the gain concentrates in the high-vol regime (the hypothesized mechanism), not a
single bucket.

## Verdict

**NULL on the registered Tier-2 bar — but a real, both-venue, control-beating
directional signal (the program's first).** R4 (regime-hedge timing) improves
pooled MAR by **+0.078** on both venues at constant average hedge, with no drawdown
increase, and **decisively beats the R5 hash control** (−0.614; a +0.69 pooled
spread), the gain concentrated in high-BTC-vol regimes as hypothesized. The BTC-vol
regime carries genuine hedge-timing information. But it **misses the predeclared
+0.1 pooled-MAR bar** (+0.078) and is **cost-fragile**: at 2× hedge cost the pooled
delta collapses to +0.008 and binance flips negative — the percentile-daily
intensity reallocates the hedge frequently, so hedge turnover cost eats most of the
timing benefit. Per the registration the bar is **not moved**; the arm is NULL and
the 2x-cost falsifier fired.

This is **not a dead mechanism** — it is the program's strongest lever, and the cost
fragility points to one specific, mechanistically-distinct follow-up: a
**lower-turnover regime-hedge** (a smoothed/banded intensity, a slower regime clock,
or a turnover-aware intensity that resizes the hedge only on regime *changes*) that
captures the same timing benefit with less hedge churn — to be registered as its own
dated receipt with its own locked parameters, **NOT** a λ/threshold re-tune of this
run. Falsifier outcome: **triggered** on the +0.1 bar and the 2x-cost arm; the
directional signal and both-venue control-beating margin are banked as the lead.
