# Continuous V2 Problem Book B — Conviction Sizing Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Control: frozen v2 (`docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`)

Scope: CONTINUOUS demo/paper research, **two-venue candidate-track**
(`claimed_venue_scope=both_venue_candidate_track`). No real-money claim.

## Motivation (from the foundation/C0 screens)

Trade-level within-symbol (symbol-demeaned) rank-IC vs control short `net_return`,
compared to the per-venue null-max:

- `score_margin_d9_d8`: +0.022 (bybit) / +0.028 (binance) — positive on both
  venues but **at or below the null-max** (0.054 / 0.029). Weak conviction signal.
- `path_ret_6h_max`: **+0.105 / +0.115** — clearly above the null-max and
  cross-venue consistent. The strongest within-symbol predictor in the almanac.
  (Echoes the W5 finding that path-shape had real within-symbol residual IC.)

W5 caveat we are explicitly testing: path-shape IC was real before, but downstream
**sizing did not cleanly harvest** it. This pre-registers a clean v2 test of whether
a causal conviction-sizing tilt harvests, with a hash control and a both-venue bar.

## Arms

All keep entries unchanged and pass a causal per-trade `size_mult_lookup` to the
engine; the daily vol-target rebalance keeps book gross fixed, so this is a
relative within-book reweighting, not a leverage change.

- `B1_SCORE_MARGIN_SIZING`: feature `score_margin_d9_d8`.
- `B1P_PATH_SHAPE_SIZING`: feature `path_ret_6h_max` (strongest screen IC).
- `B6_SCORE_MARGIN_HASH_CONTROL`: B1 multiplier distribution, hash-permuted.
- `B6P_PATH_SHAPE_HASH_CONTROL`: B1P multiplier distribution, hash-permuted.

Two serious arms (B1, B1P) + two mandatory hash controls; within the Problem Book B
budget (4 serious arms after screens).

## Construction (fixed before running)

- multiplier `= clip(1 + 0.25 * z, 0.5, 2.0)`, where `z` is the **per-symbol
  expanding-prior** z-score (strictly prior rows, min 10 obs) of the feature.
- Strictly causal: no full-sample rescaling. The ~mean-0 z keeps the tilt ~mean-1;
  exact book gross is enforced by the frozen daily vol-target rebalance.
- Keyed by `(symbol, signal_ts_ms)`; warm-up rows get multiplier 1.0.
- Hash control permutes the multiplier multiset across `(symbol, signal_ts)` by a
  fixed hash seed — same distribution, feature→trade alignment destroyed.
- Both venues, full PIT, costed (engine fees/funding/impact), full lifecycle.
  Components are re-run with the lookup (not control reuse). Per-component
  multiplier diagnostics are written to `sizing_lookup.json`.

## Candidate bar (all required)

- Both venues positive and both improve MAR vs the v2 control (or one ties within
  tolerance while the other improves and pooled robustness passes).
- No venue worsens max drawdown materially.
- Monthly / sub-period thirds do not reveal a single-regime artifact.
- `scripts/r1_robustness.py`-equivalent monthly diagnostics (this runner's
  `--mode robustness`) do not flip the Tier-2 decision negative.
- The matching hash control (B6/B6P) does NOT beat or match the real arm.

## Falsifiers (any one closes the exact mechanism)

- The hash control matches/beats the real sizing arm (sizing is noise).
- One venue, one month, or one component carries the result.
- Drawdown worsens faster than return improves (MAR down).
- The arm ties the control (no conviction signal / no contention to exploit).
- B1P repeats the W5 non-harvest: strong IC, no MAR gain → path-shape is an
  entry/timing signal, not a sizing signal; route to an exit-timing shadow next.

No internal run is real-money evidence; forward demo/paper remains the arbiter.
