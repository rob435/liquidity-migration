# Pre-registration: rmom latency-delay falsification (methodology debt #3)

**Date:** 2026-06-09
**Author:** claude (for owner)
**Stage:** run-pending

## What's changing

Nothing in production code or any deployed profile. This is a **causality
falsification audit** of the residual-momentum (rmom) feature that the entire
continuous research line is built on. Freeze-exempt per STATE.md 2026-06-09
(causality audits can only kill the line, not improve it).

## Hypothesis

If rmom@shift3's edge is a real, slow phenomenon (a 7-day residual-momentum
window), one extra day of staleness (shift4) refreshes only ~1/7 of the window
and should degrade the stream modestly. If instead the edge **collapses** at
shift4, the edge is concentrated exactly at the freshest legally-usable day —
the signature of leakage at the causality boundary (this codebase's worst
historical failure was rmom shift1, a ~25h look-ahead worth +248% of fake
edge), or of a timing fragility indistinguishable from it given that
methodology debt #4 (factor/residual day-grid alignment) is still open.

shift2 (which leaks ~1h past the D 00:00 decision) is run as a **forbidden
diagnostic only** — it measures how hot the boundary is; it can never be
deployed or counted as evidence.

## Test object

The merged-candidate selection layer on both venues, window 2023-04-01 →
2026-05-28: `q25 / liq500k / btc-uptrend / turn3_pop3 / age240 / h24 / fixed
exit / inverse_vol` via `scripts/continuous_causal_rmom_vs_daily.py` (the 06-05
harness). TP10/crowd2 exit polish is NOT included (the harness predates it);
the rmom selection under test is identical, so a causality verdict transfers.

Shifts: {2 (diagnostic), 3 (control), 4, 5, 7}. Residuals are computed once per
root; only the rmom shift varies. Per-day bottom-q25 selection Jaccard vs
shift3 is reported alongside.

## Predicted direction + magnitude

- Causal-and-slow world: pooled short MAR decays gently (shift4 ≥ 70% of
  shift3), Jaccard(s3,s4) high (≥ ~0.7).
- Leak/fragile world: shift4 pooled MAR < 50% of shift3 or a venue return sign
  flips; shift2 markedly above shift3.

## Roots that will be touched

- [x] bybit_full_pit — `residual_momentum.parquet` is temporarily swapped per
  shift (original backed up and restored in a `finally`; panel cache
  self-invalidates on mtime). Reports under `~/SHARED_DATA/rmom_latency_falsification_2026-06-09/`.
- [x] binance_full_pit — same.
- [ ] forward demo/paper — untouched (the deployed short does not use rmom;
  sentinel 10.0).

## Decision rule (a priori)

- **PASS**: pooled(short_only_mtm_mar) at shift4 ≥ 0.5 × shift3's AND no venue
  return sign-flip vs shift3. Consequence: methodology debt #3 is downgraded
  from "unproven" to "robust to +1d latency" (the live-join audit remains
  open); the continuous line keeps its current research-only status.
- **FAIL**: otherwise. Consequence: rmom is flagged NOT validated for any
  deployment-grade claim; the continuous frozen winner is annotated as resting
  on a boundary-concentrated feature; any future continuous promotion case must
  first resolve debts #3+#4 at the data layer.
- shift2 is context only and cannot rescue or damn the verdict.

No threshold may be moved after seeing results.

## Run command

```bash
POLARS_MAX_THREADS=6 .venv/bin/python scripts/rmom_latency_falsification.py
```

## Post-run results

Driver replication sanity: our shift3 rmom matched the production
`residual_momentum.parquet` at **100.0% exact** on both venues (448k/405k joined rows)
— the control is bit-identical to the production feature. Production parquets restored
after the run (verified in output). Artifacts:
`~/SHARED_DATA/rmom_latency_falsification_2026-06-09/` (per-shift harness outputs +
`falsification_summary.csv` + `verdict.json`).

| shift | bybit ret / MAR | binance ret / MAR | pooled MAR | J(sel vs s3) |
|---|---|---|---:|---:|
| 2 (1h leak, diagnostic) | +9.6% / 0.43 | +14.6% / 0.90 | 0.67 | 0.56 |
| **3 (causal control)** | **+11.6% / 0.61** | **+24.0% / 1.65** | **1.13** | 1.00 |
| 4 (+1d) | **−3.2% / −0.10** | +8.9% / 0.30 | 0.10 | 0.56 |
| 5 (+2d) | +6.1% / 0.26 | −9.7% / −0.29 | −0.01 | 0.43 |
| 7 (+4d) | −3.7% / −0.12 | −8.3% / −0.20 | −0.16 | 0.29 |

Pre-registered rule check: pooled MAR(shift4)=0.10 < 0.5×1.13=0.56 → fails the MAR
condition; bybit return sign-flips at shift4 → fails the sign condition.

## Verdict

**FAIL** — and the shape is worse than a simple decay:

1. shift3 is the **unique peak**. One day of extra staleness destroys ~90% of pooled
   MAR and flips Bybit's return sign. The "7-day residual momentum" story is false:
   the predictive content sits almost entirely in the single freshest legal day
   (residual_return[D−3]).
2. shift2 — strictly MORE information — is also materially worse than shift3. This
   argues against a simple information-leak (a leak would make fresher better), but it
   does NOT rescue the feature: a cross-sectional quantile selection whose edge exists
   at exactly one staleness setting, with ±1-day Jaccard of only ~0.56, is a timing
   artifact / noise-peak, not a robust factor.
3. Per the pre-registered consequence: rmom is **NOT validated for any
   deployment-grade claim**; the continuous frozen winner is annotated as resting on a
   boundary-concentrated feature; any future continuous promotion case must first
   resolve methodology debts #3+#4 at the data layer (day-grid alignment audit, then
   re-test). Live operational margin is zero: any data delay or rebuild lag of one day
   reproduces shift4, i.e. a dead or negative edge.
