# R1 — Per-filter hypothesis audit — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R1 + the 2026-05-28 `max_active=12` amendment.
**Run label:** `exploratory` (full-PIT, costed, ledger-backed, split-stable — but in-sample/pre-OOS; per pre-reg commitment #7 no R0–R9 cell is `candidate` until R11 OOS passes).
**Verdict tooling:** `scripts/r1_robustness.py --sweep-tag r1_filter_audit_max12_2026-05-28` (Tier-2, authoritative) + `scripts/apply_decision_rule.py --rule investigation` (Tier-1 cross-check).

## Headline

`R1_drop_all_4` (production stack minus `day_return` + `stop_pressure` +
`realized_loss` + `rank_max`) is **DEMO-ELIGIBLE** under the Tier-2 bar →
**the pre-committed re-baseline cascade TRIGGERS.** R2/R3/R5/R6/R7/R8/R9/R10/R11
now compare against the `drop_all_4` stack. Tier-3 (real-money) gate is **not**
met and remains fully required before any production/mainnet change.

## Run provenance & integrity

- **This is the full-PIT re-run.** The prior `r1_filter_audit_max12_2026-05-28`
  sweep (2026-05-28 ~21:00) ran on the pre-`8c34cff` tooling that hard-coded
  `--allow-partial-pit` → `pit_membership_filtered_current_universe`
  (survivorship-biased) **and** OOM-crashed every bybit cell at 8 workers. It is
  tainted and was deleted + re-run clean. **Do not cite the old run.**
- **All 14/14 cells: `run_label='full_pit_universe'`, `pit_manifest.full_pit_universe_pass=True`** (verified directly per report JSON). Engine aborts on any coverage gap; none occurred.
- **Dispatch config:** `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8` (NOT the plan's
  8 workers). Forced by hardware: one full-PIT cell peaks **~23 GB RAM** on this
  32 GB box, so 8 concurrent → OOM (the prior crash). `SWEEP_MAX_WORKERS` is a
  perf knob, not a research/integrity threshold; serial is memory-safe and also
  avoids orphaned-lock hangs. Binance (klines 4.3 GB ≈ bybit 4.4 GB) is no
  lighter, so a venue-split 2-stream was ruled out. Wall time 101.5 min.
- **Stale-lock fix applied pre-run:** the prior OOM left orphaned read-locks in
  `bybit_full_pit/.locks/` (`index/mark/premium_price_1h.lock`); PID reuse made
  them look live → a clean cell hung 67 min on the 6 h `stale_seconds` timeout.
  Cleared before dispatch; serial execution prevents new orphans.
- Window 2023-04-01 → 2026-05-28 (1153 d), `max_active_symbols=12`, both venues.

## Results (engine daily-DD MAR; control = `00_baseline`)

| venue | cell | trades | ret | maxDD | MAR | MAR Δ |
|---|---|--:|--:|--:|--:|--:|
| bybit | 00_baseline | 761 | +2.26× | −11.9% | 3.84 | — |
| bybit | **R1_drop_all_4** | 816 | +2.95× | −10.6% | **5.14** | **+1.30** |
| bybit | R1_retest_realized_loss | 771 | +2.49× | −11.9% | 4.10 | +0.26 |
| bybit | R1_retest_rank_max | 751 | +2.43× | −11.9% | 4.04 | +0.20 |
| bybit | R1_drop_both_noops | 786 | +2.32× | −11.9% | 3.88 | +0.04 |
| bybit | R1_drop_day_return | 762 | +2.29× | −11.9% | 3.87 | +0.03 |
| bybit | R1_drop_stop_pressure | 785 | +2.30× | −11.9% | 3.85 | +0.01 |
| binance | 00_baseline | 477 | +0.58× | −13.9% | 1.16 | — |
| binance | R1_drop_day_return | 478 | +0.60× | −13.9% | 1.18 | +0.02 |
| binance | R1_retest_rank_max | 477 | +0.61× | −14.8% | 1.12 | −0.03 |
| binance | R1_retest_realized_loss | 479 | +0.55× | −14.9% | 1.02 | −0.13 |
| binance | R1_drop_both_noops | 488 | +0.58× | −17.7% | 0.90 | −0.26 |
| binance | R1_drop_stop_pressure | 487 | +0.56× | −17.7% | 0.88 | −0.28 |
| binance | **R1_drop_all_4** | 509 | +0.56× | **−20.7%** | **0.75** | **−0.40** |

## Tier-2 Demo-candidate verdict (pooled MAR Δ > +0.1, positive both venues, neither < −0.5, trades ≥30/20)

| cell | by MARΔ | bn MARΔ | pooled | verdict |
|---|--:|--:|--:|---|
| **R1_drop_all_4** | +1.30 | −0.40 | **+0.45** | **DEMO-ELIGIBLE** |
| R1_retest_rank_max | +0.20 | −0.03 | +0.08 | descriptive |
| R1_retest_realized_loss | +0.26 | −0.13 | +0.07 | descriptive |
| R1_drop_day_return | +0.03 | +0.02 | +0.03 | descriptive |
| R1_drop_both_noops | +0.04 | −0.26 | −0.11 | FALSIFY (pooled ≤0) |
| R1_drop_stop_pressure | +0.01 | −0.28 | −0.13 | FALSIFY (pooled ≤0) |

(Tier-1 `--rule investigation` marks all six non-control cells investigation-positive — the looser majority-venue bar — ranking `drop_all_4` #1 at combined ΔMAR +0.91. Tier-2's pooled metric is the binding gate.)

## Per-filter findings (full-PIT corrects two partial-PIT/LOO priors)

- **`day_return` → DROP.** +0.03 by / +0.02 bn: genuine no-op both venues, as predicted. Occam.
- **`stop_pressure` → load-bearing on binance.** +0.01 by / **−0.28 bn** (DD −13.9%→−17.7%). The Round-1 LOO (partial-PIT) called it a no-op DROP; **full-PIT shows its removal hurts binance.** Retained value individually; it is dropped only inside the `drop_all_4` stack, where the bybit interaction dominates.
- **`rank_max` → mild drop.** +0.20 by / −0.03 bn — helps bybit, neutral binance.
- **`realized_loss` → mixed.** +0.26 by / −0.13 bn — helps bybit, mild binance cost.
- **Interaction effect (the real finding):** `drop_all_4` bybit MAR Δ **+1.30** vastly exceeds the additive sum of single drops (~+0.50) — the four drops interact strongly positively on bybit. On binance the harms stack ~additively (−0.40). So `drop_all_4` is a **bybit-driven Pareto win with a real binance cost**, not a uniform improvement.

## Fragility (REPORTED, non-blocking at Tier-2; binding at Tier-3)

`R1_drop_all_4`:
- **Bybit — robust:** all three thirds positive; bootstrap ann-ret Δ p5=+1.7% (P(Δ>0)=99%); MAR Δ p5=−0.22, p50=+2.59 (P(Δ>0)=94%); LOO does not flip sign; top-3 months = 54% of positive Δ.
- **Binance — fragile:** third-third return negative; bootstrap MAR Δ p5=−2.07, p50=−0.50, **P(Δ>0)=28%** (a likely real degradation, not noise); ann-ret Δ P(Δ>0)=47%.

→ **Tier-3 status: FAILS.** The real-money gate needs block-bootstrap *pooled* MAR-Δ p5 ≥ 0; the binance side is solidly negative. `drop_all_4` is a demo candidate only — far from real money. (Also pending for Tier-3: residual Sharpe (R4), R7 stress, R8 capacity, R11 OOS, ≥30 d forward demo.)

## DECISION — re-baseline cascade: **TRIGGERED**

All four pre-committed cascade conditions met by `R1_drop_all_4` vs `00_baseline`:
return positive both venues ✓; pooled MAR Δ +0.45 > +0.1 ✓; neither venue worse
than −0.5 (binance −0.40) ✓; trades 816/509 ≥ 30/20 ✓.

Per the pre-commitment (no post-hoc litigation):
- **R2, R3, R5, R6, R7, R8, R9, R10, R11 re-baseline against the `drop_all_4`
  stack** (drop `day_return` + `stop_pressure` + `realized_loss` + `rank_max`).
- Three-tier thresholds unchanged (deltas hold against any baseline).
- **No production/profile change.** The frozen promoted profile is untouched;
  `drop_all_4` is a research re-baseline + demo candidate, not a deployment.
- Tier-3 real-money gate stays fully required.
- The binance fragility is recorded and sets demo ordering; it does not block
  the cascade (permissive where being wrong is free).

## Next

- **R13 (exit-rule re-optimization) unblocked** — it is conditional on R1
  confirming `drop_all_4`, which is now satisfied. Dispatch
  `scripts/r13_exit_rule_sweep.py` (baseline = the `drop_all_4` stack) next,
  full-PIT, `SWEEP_MAX_WORKERS=1`.
- R2 (per-feature decile, code ready) follows.
