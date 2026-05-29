# R13 — Exit-rule re-optimization — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R13 (conditional on R1 confirming `drop_all_4` — satisfied).
**Run label:** `exploratory` (full-PIT, costed, ledger-backed; in-sample / Tier-1 carry-forward only — no OOS consumed).
**Decision rule:** Tier-1 Investigation (MAR Δ > 0 majority venues vs control, no return sign-flip, ≥30 by / ≥20 bn trades). Tools: `apply_decision_rule.py --rule investigation --control 00_baseline_drop4` + `r1_robustness.py --sweep-tag r13_exit_rule_2026-05-28 --control 00_baseline_drop4`.

## Headline

**Winner: `R13_ff6_4pct`** (failed_fade 6h / 4% loss / 1% mfe) — the strongest
exit on the `drop_all_4` entry population, combined ΔMAR **+0.52**, MAR-improving
**both** venues. It **carries forward to R9 assembly** as the exit rule on the
drop_all_4 stack. It does NOT skip the OOS (R11) / forward-demo gates.

## Run provenance & integrity

- Baseline / control = **`00_baseline_drop4`** = the `drop_all_4` lead candidate
  (R1) with the promoted exit. Every cell overrides ONLY exit knobs, so trade
  ENTRIES are identical across all cells (816 bybit / 509 binance everywhere) and
  every metric delta is a **pure exit-rule effect**.
- All 16/16 cells `run_label='full_pit_universe'` (the verdict tools returned
  graded verdicts, not `INVALID (partial-PIT)`). `SWEEP_MAX_WORKERS=1` full-PIT
  (23 GB/cell). Window 2023-04-01 → 2026-05-28. Wall 111.3 min.
- Control reproduces R1 `drop_all_4` exactly (bybit 2.95×/−10.6%/MAR 5.14;
  binance 0.56×/−20.7%/MAR 0.75) → re-baseline is consistent.

## Results (engine daily-DD MAR; control = `00_baseline_drop4`)

| cell | by MAR→ (Δ) | bn MAR→ (Δ) | combined ΔMAR | Tier-1 verdict |
|---|---|---|--:|---|
| **R13_ff6_4pct** (ff 6h/4%/1%mfe) | 5.14→5.62 (+0.48) | 0.75→0.79 (+0.04) | **+0.52** | **investigation_positive** |
| R13_ff6_3pct (ff 6h/3%/1%mfe) | 5.14→5.43 (+0.29) | 0.75→0.78 (+0.02) | +0.31 | investigation_positive |
| R13_rankexit_045 | 5.14→5.14 (0.00) | 0.75→0.75 (0.00) | 0.00 | descriptive (no-op: rarely triggers <0.55) |
| R13_rankexit_065 | 5.14→4.98 (−0.16) | 0.75→0.71 (−0.04) | −0.20 | descriptive |
| R13_tp21 (TP 0.21) | 5.14→4.90 (−0.23) | 0.75→0.73 (−0.02) | −0.25 | descriptive |
| R13_tp30 (TP 0.30) | 5.14→4.16 (−0.98) | 0.75→0.74 (−0.01) | −0.99 | descriptive |
| **R13_stop10** (stop 0.10) | 5.14→2.68 (−2.45) | 0.75→0.74 (−0.02) | −2.47 | **REJECT (falsifier MAR Δ ≤ −1.0)** |

## Findings

- **failed_fade is the high-leverage exit knob** (replicates the 2026-05-23
  finding, now full-PIT on the drop_all_4 population). `ff6_4pct` and `ff6_3pct`
  are the only investigation-positive cells. The mechanism is venue-asymmetric
  but aligned: on **bybit** the failed-fade timeout shaves max-DD (−10.6%→−9.6%
  for 4%) with return ~flat; on **binance** it lifts return (+0.56→+0.59×) — both
  raise MAR. All thirds positive on both venues for both ff cells.
- **`ff6_4pct` > `ff6_3pct`** on MAR-primary (combined +0.52 vs +0.31) → 4%-loss
  threshold is the pick.
- **Tighter fixed stop (0.10) is decisively falsified** (bybit MAR 5.14→2.68, ret
  2.95→1.89×; bootstrap ann-ret Δ p5 −26%, P(Δ>0)=0%) — it stops winners out. The
  promoted 0.12 stop stays.
- **take-profit moves both hurt** (0.21 cuts winners early; 0.30 deepens DD). TP
  0.26 stays. **rank-exit 0.45 is a no-op** (rarely binds below 0.55); 0.65 mildly
  worse. rank_exit 0.55 stays.

## Fragility (REPORTED, non-blocking at Tier-1; binding at Tier-3)

`R13_ff6_4pct`: bybit bootstrap MAR Δ p5=−1.12 / p50=+0.45 / P(Δ>0)=68%; binance
p5=−0.17 / p50=+0.05 / P(Δ>0)=63%. The MAR gain is real but modest and the
bootstrap p5 is slightly negative on both venues → **not Tier-3-robust on its own**
(Tier-3 needs block-bootstrap pooled MAR-Δ p5 ≥ 0). The bybit improvement is
DD-driven (ann-ret Δ p50 ≈ −1%), so it's a risk-reduction refinement more than an
alpha addition — appropriate for an exit rule.

## DECISION — carry `ff6_4pct` exit into R9

Per the R13 pre-commitment ("a winning exit cell feeds R9 assembly; does NOT skip
OOS / forward-demo gates"):
- **R9 assembly uses the `drop_all_4` entry stack + the `ff6_4pct` exit**:
  take_profit 0.26, **failed_fade 6h / 4% loss / 1% mfe ENABLED**, rank_exit 0.55,
  stop 0.12. (`ff6_3pct` is the documented runner-up.)
- No production/profile change. Frozen promoted profile untouched (still TP 0.26 /
  failed_fade off / rank_exit 0.55). The failed_fade exit must still clear R10
  demo-candidate + R11 OOS + ≥30d forward demo before any deployment.
- Exit knobs falsified/closed: fixed stop 0.10 (REJECT), TP 0.21/0.30, rank_exit
  0.45/0.65 (descriptive, not carried).

## Next

R2 (per-feature standalone decile + correlation matrix) dispatches next (code in
`liquidity_migration/r2_decile_sort.py`); then R5 (sizing). R4 risk model built in
parallel (code).
