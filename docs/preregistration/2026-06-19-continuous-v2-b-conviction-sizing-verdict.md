# Continuous V2 Problem Book B — Conviction Sizing Verdict (both venues)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Construction: `docs/preregistration/2026-06-19-continuous-v2-b-score-sizing-construction.md`
Scope: CONTINUOUS demo/paper research, two-venue candidate-track. No real-money claim.
Run label `exploratory`; forward demo/paper is the arbiter.

## What ran

`backtest-runs/continuous_v2_ab_bsizing_2026-06-19/` (ab_table.csv, robustness.json/csv/report)
Almanac (both venues): `backtest-runs/continuous_v2_feature_almanac_2026-06-19_flow_topup`.
Entries unchanged; causal mean-1 per-trade `size_mult_lookup` (per-symbol expanding-prior z,
clip(1+0.25·z,[0.5,2.0]), strictly causal; book gross enforced by the daily vol-target rebalance).

## Results — MAR delta vs v2 control

Control: bybit MAR 5.660, binance MAR 8.185.

| Arm | bybit MAR Δ | binance MAR Δ | pooled MAR Δ | boot P(Δ>0) by/bi | verdict |
| --- | ---: | ---: | ---: | --- | --- |
| `B1_SCORE_MARGIN_SIZING` | +0.232 | −0.356 | −0.062 | 0.51 / 0.58 | FALSIFY (venue-split, pooled ≤0) |
| `B6_SCORE_MARGIN_HASH_CONTROL` | +0.340 | +0.357 | +0.348 | 0.63 / 0.76 | (random) trips loose rule |
| `B1P_PATH_SHAPE_SIZING` | −0.419 | −0.506 | −0.462 | 0.08 / 0.26 | FALSIFY (negative both venues) |
| `B6P_PATH_SHAPE_HASH_CONTROL` | +0.216 | −1.290 | −0.537 | 0.39 / 0.21 | (random) |

## Verdict — conviction sizing CLOSED, no candidate

- **B1 (score-margin sizing): FALSIFIED.** Venue-split (Bybit +0.232, Binance −0.356),
  pooled −0.062. And it is **beaten by its own hash control B6 on both venues**
  (B6 +0.340 / +0.357). The real score-margin→trade alignment does not beat random sizing
  of the same multiplier distribution. Consistent with the C0 screen: score_margin_d9_d8
  within-symbol IC was at/below the per-venue null-max.
- **B1P (path-shape sizing): FALSIFIED.** Negative on both venues (−0.419 / −0.506), pooled
  −0.462, and far below its hash B6P. This is the **W5 lesson reproduced under v2**:
  `path_ret_6h_max` had the strongest within-symbol IC (+0.105 / +0.115), but sizing **up**
  the highest-IC names concentrates the squeeze tail and lowers risk-adjusted return.
  A strong cross-sectional IC is not a tradable sizing edge.

## Critical methodology catch — the hash control earned its keep

The **random** score-margin hash control `B6` scored pooled MAR Δ **+0.348** and tripped the
runner's loose "DEMO-ELIGIBLE by loose backtest rule" verdict, while the *real* B1 (−0.062)
failed it. This proves the per-trade sizing **mechanism** (a mean-1 random tilt interacting
with the daily vol-target rebalance) emits a spurious, sample-specific pooled-MAR bump of
~+0.2–0.35 with no signal content. Any sizing-arm result must be judged against its matched
hash control, **not** the loose pooled-MAR rule. Neither real conviction signal cleared its
hash. (No code/threshold was changed in response — this is recorded as a known limitation of
the loose rule for sizing-mechanism arms.)

## Falsifier ledger

The construction receipt's falsifiers fired: "the hash control matches/beats the real arm"
(both B1 and B1P), and "ties/worsens MAR" (both). Drawdown also worsened slightly for both
real arms (ddΔ −0.001 to −0.0025). Closed.

## Status

No candidate-track improvement from Problem Book B conviction sizing. Both serious arms closed
with falsifier-backed negatives and matched hash controls. Problem Book B budget: 2 serious arms
used (B1, B1P) + 2 hash controls; 2 serious arms remain for a future dated amendment if a
risk-aware sizing idea (size by return/risk, not return alone) is pre-registered — the evidence
here says sizing-by-expected-return alone does not harvest.
