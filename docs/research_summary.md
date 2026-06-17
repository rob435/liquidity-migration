# Research Summary - Liquidity Migration

**Updated:** 2026-06-17

This is the consolidated decision surface. Historical staged-program details were
cleaned out of the hot path on 2026-06-17; git history and run artifacts are the
archive.

## Non-Negotiable State

- Research-stage only. Nothing is approved for real money.
- Forward demo/paper is the arbiter. There is no clean internal pre-2023 OOS root to
  rescue a result.
- Full-PIT membership, causal features, cost/funding treatment, ledgers, and
  reconstructable run records are correctness gates.
- The daily SHORT sleeve was erased on 2026-06-11 by operator order.

## Current Objects

**Continuous fade book**

- Old deployed system restored as the research anchor on 2026-06-17.
- Live demo object: `continuous_ensemble_v1`: p3 .30 / p4p3 .20 / p4p5 .40 / tp14
  .10, w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25, BTC-uptrend gate.
- Demo fills are execution evidence, not alpha proof.
- The temporary 2026-06-16 `BTC_TREND_GATE=off` plumbing window is not promoted-object
  forward evidence; revert to `uptrend` after plumbing confirmation.

**Continuous risk overlays**

- BTC+ETH 2-factor hedge is wired and armed.
- BTC-vol regime hedge is live forward-watch only, not a promotion or real-money pass.
- Sniper is armed in demo, code default off.
- Dynamic exit remains no-order paper shadow only.

**Long-native v11a**

- Demo + paper services were re-enabled on 2026-06-16 at current v11a sizing.
- Current profile: `div` + volup125 + weekend 1.5x tilt.
- No real-money claim is allowed.

## Active Binding Receipts

- `docs/preregistration/continuous-winner-robustness-2026-06-09.md` - frozen
  continuous ensemble/winner_base evidence.
- `docs/preregistration/continuous-hedge-2f-engine-2026-06-10.md` - 2f hedge
  candidate receipt.
- `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md` - BTC-vol hedge
  forward-watch clock.
- `docs/preregistration/2026-06-15-operator-override-promote-continuous.md` - registry
  override to include continuous in promoted profiles for demo/paper only.
- `docs/preregistration/continuous-forward-clock-spec-2026-06-09.md` - forward replay
  and evidence-clock design.
- `docs/preregistration/continuous-capacity-impact-2026-06-09.md` - fill calibration
  and capacity debt.
- `docs/preregistration/continuous-dynexit-forward-shadow-2026-06-10.md` - dynamic-exit
  paper shadow bar.
- `docs/preregistration/continuous-walkforward-allocator-2026-06-09.md` - frozen-weight
  / no-adaptive-reweighting policy.
- `docs/preregistration/sniper-staged-entries-2026-06-09.md` - sniper forward-watch
  candidate receipt.
- `docs/preregistration/rmom-latency-falsification-2026-06-09.md` - rmom latency
  verdict.
- `docs/preregistration/r4-risk-model-verdict.md` - residual-risk model foundation.
- `docs/preregistration/div-promotion.md` - long `div` profile receipt.
- `docs/preregistration/long-volup-candidate-2026-06-09.md` - long volup125 receipt.
- `docs/preregistration/long-provisional-entry-engine-2026-06-10.md` - PE2 future-OOS
  re-judgment path.
- `docs/preregistration/trade-atlas-2026-06-11.md` - long weekend tilt / forward-watch
  bars.

## Cleanup Policy

- Closed staged research receipts and one-off scripts should not be restored to the hot
  path.
- Local `~/SHARED_DATA/...` research artifacts may exist, but they are scratch unless a
  current binding receipt explicitly cites them.
- If a new experiment is requested, create a fresh dated pre-registration first.
