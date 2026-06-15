# Pre-registration: W5 Continuous Stage 4d - Liquidity-Sniper Drop-Decile Robustness

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Label:** `exploratory`
**Builds on:** Stage 4 liquidity sniper (`…stage4-sniper.md`): dropping the least-liquid
decile (k=10%) improves pooled MAR +0.407 both venues and beats a count-matched random-drop
control at 1× and 2× cost. k=10% was pre-registered, but a single drop level could be a lucky
cut.

## Question

Is the liquidity-sniper edge SMOOTH in the drop fraction k, or a k=10% artifact? Bracket it:
re-run the sniper at **k=5%** and **k=20%**, each with its own per-day count-matched
random-drop control, both venues, 1× cost. Same causal trailing-180d turnover percentile,
same `size_mult=0` drop mechanism, same random-control construction as Stage 4 — only k
changes.

## Mechanism / arms (locked)

- `T0_control` (frozen ensemble; reproduces 4.748 / 5.255).
- for k ∈ {0.05, 0.20}: `T1_turnover_k{05,20}` (drop bottom-k turnover) and `T2_random_k{05,20}`
  (per-day count-matched random drop). Engine re-run per arm.

## Decision rule (a priori)

The sniper edge is robust across deciles iff, at BOTH k=5% and k=20%, on BOTH venues:
`T1_turnover_k` MAR > `T0_control` AND > `T2_random_k` (selection beats random). A smooth,
both-sided positive effect (e.g. pooled ΔMAR rising from k=5 → k=10 → k=20, or at least
positive and random-beating at both brackets) confirms a genuine liquidity gradient, not a
k=10% spike. Falsifier: if k=5% or k=20% fails to beat its random control on a venue, or flips
the sign, the k=10% result is fragile to the drop level and is downgraded.

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`, both full-PIT roots; engine re-run per arm.
Roots read-only; writes only to `reports/<tag>/` and `~/SHARED_DATA/w5_continuous_stage4d_*`.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage4d_decile_robustness.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage4d_decile_robustness_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, 1× cost, engine re-run per arm. Drop sets: bybit k=5%→69 /
k=20%→261; binance k=5%→73 / k=20%→270 (random count-matched). Per-arm MAR:

| Venue | T0 | T1_turn k5 | T2_rand k5 | T1_turn k20 | T2_rand k20 |
|---|---:|---:|---:|---:|---:|
| bybit | 4.748 | 5.455 | **5.499** | **3.436** | **7.592** |
| binance | 5.255 | 5.231 | 4.536 | 5.285 | 4.914 |

Turnover-drop ΔMAR vs T0 — bybit: k5 **+0.707**, (k10 +0.159), k20 **−1.312**; binance: k5
−0.024, (k10 +0.655), k20 +0.030. Selection (turnover − random) — bybit: k5 **−0.044**, (k10
+0.329), k20 **−4.156**; binance: k5 +0.695, (k10 +1.000), k20 +0.371. Pooled ΔMAR vs T0: k5
+0.342, (k10 +0.407), k20 **−0.641**. Pooled selection: k5 +0.326, (k10 +0.666), k20 −1.892.

## Verdict

**NOT robust across deciles — the Stage 4 k=10% headline was a FAVORABLE CUT, and the sniper
is VENUE-SPLIT (binance-real, bybit-noise).** Two findings:

1. **The single-seed random-drop control is inadequate at these drop sizes — huge MAR
   variance.** bybit random MAR swings 5.499 (k5) → 4.577 (k10) → **7.592** (k20): dropping a
   random 20% of trades, by luck of which drawdown-causing trades it hits, can lift MAR by
   +2.84. So "turnover beats random" on bybit at k=10% (+0.329) is within the noise of the
   control — at k=5% random ties it, at k=20% random crushes it (−4.16). A reliable control
   needs a MULTI-SEED null distribution, not one draw. (My Stage 4 design used one seed.)
2. **bybit has no robust liquidity effect; binance does (modestly).** vs the deterministic T0,
   bybit turnover-drop swings +0.707 / +0.159 / −1.312 across k — not a stable gradient, and
   strongly NEGATIVE at k=20% (the 10–20% turnover band on bybit holds GOOD trades). binance
   turnover-drop beats its random control at ALL k (+0.695/+1.000/+0.371) and is ≥ T0 — a
   genuine but modest liquidity effect peaking ~k=10%.

**Consequence — DOWNGRADE the Stage 4 sniper:** it is NOT a confirmed robust both-venue +0.407
MAR improvement; that was a k=10% favorable cut plus a noisy single-seed control. The real,
surviving piece is a **binance within-symbol liquidity effect** (consistent with the strongest
IC, +0.134) — a single-venue, k-sensitive effect, not a both-venue robust harvest. This joins
the path-shape lesson (Stage 5/7b): a real IC need not translate to a robust MAR improvement.
The **BTC-vol regime-hedge (Stage 8c) reverts to the standing both-venue candidate**; the 4c
combination's bybit gain was mostly the (real) hedge plus (fragile) sniper noise. Follow-up if
the sniper is pursued: a proper MULTI-SEED random-drop null at fixed k to characterize the MAR
variance and test whether binance turnover-drop is genuinely in the tail.
