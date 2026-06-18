# Research Program State

**Last updated:** 2026-06-18

Read this first. Detailed historical research belongs in git history and local run
artifacts, not in the hot path.

## Current Status

- Research-stage only. **Nothing is approved for real money**; keep `REAL_MONEY=false`
  unless the owner explicitly says otherwise.
- The daily SHORT sleeve was erased on 2026-06-11 by operator order. Do not restart or
  discuss it as dormant.
- Active systems are the frozen continuous v2 fade book and the long-native v11a
  demo/paper sleeve.
- Forward demo/paper is the arbiter. Internal backtests are not promotion evidence.

## Real-Money Gate

No sleeve is approved for real money. A profile can be demo/paper-runnable or
promoted-in-code without clearing the real-money gate. The gate remains:

- A meaningful forward-demo/paper sample, not an internal backtest rerun.
- Both surviving venues agree; venue disagreement is a regime/microstructure warning.
- Daily reconciliation is clean enough to audit model, demo, paper, fills, funding, and
  costs.
- Bootstrap/stress/capacity checks do not flip the result negative.
- The owner explicitly authorizes any real-money switch after those bars are met.

## What Is Running / Wired

- **Continuous demo book:** `continuous_ensemble_v2`: p3 .333 / p4p3 .222 / p4p5 .444,
  inverse-vol component sizing (`target_vol_per_name=0.01`, clamp 2.0),
  w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25, BTC-uptrend gate,
  TP/24h exits only, no daemon/server stop. Demo/paper only; not real-money-safe.
- The 2026-06-18 operator override froze the current three-component object and
  reset the continuous forward clock.
- The v2-forward reconcile/control baseline starts at
  `2026-06-18T19:54:00+00:00` (`1781812440000`) and is recorded in
  `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`.
- The temporary 2026-06-16 `BTC_TREND_GATE=off` plumbing window is closed in the
  live-v2 wiring; demo + paper are back to `uptrend`.
- **2f BTC+ETH hedge:** wired and armed. Warmstart CSVs were regenerated on 2026-06-13.
  After a long flat spell, the first risk-increasing leg can still block on calendar-age
  staleness and page unless the operator asks for ledger-aware staleness.
- **BTC-vol regime hedge:** live forward-watch overlay since 2026-06-15. It scales both
  hedge legs via causal mean-1 BTC-vol intensity. This is research-stage forward watch,
  not a real-money gate pass.
- **Sniper:** armed in demo, code default off. No promotion or forward-fill claim.
- **Dynamic exit:** no-order forward paper shadow only.
- **Long-native v11a:** demo + paper services were re-enabled on 2026-06-16 at current
  v11a sizing (`ENTRY_LEVERAGE=10`, 50% projected-IM cap). Demo/paper only.
- **VPS:** Hetzner demo host. Do not push/deploy without owner confirmation and the
  pre-push gate.

## Current Research Direction

The continuous research anchor is frozen v2. The abandoned staged program and
retired continuous configs are removed from the hot path. Do not use their
receipts, helper scripts, or artifact directories as binding evidence for
promotion, deployment, or parameter changes.

Current work should be limited to:

- Forward/demo reconciliation and drift diagnosis.
- Cost/slippage/depth calibration from real demo fills.
- Data-root maintenance and permitted-host top-ups.
- Operator-gated stability fixes for the frozen continuous v2 system.
- Long v11a demo/paper monitoring.

Do not start broad in-sample research or parameter mining without a fresh
pre-registration and explicit operator direction.

## Open Operator Decisions

1. Do not treat `continuous_ensemble_v2` as real-money-safe. It is intentionally
   no-stop demo/paper only; any mainnet path needs a new risk-control design.
2. Decide whether hedge warmstart staleness should remain calendar-age based or become
   ledger-aware after long flat periods.
3. Finish Binance FAPI ancillary June top-ups from a permitted-region host.
4. PE2 long provisional-entry OOS re-judgment is armed only after both full-PIT roots
   extend at least 60 days past 2026-05-28 and have enough trades.
5. Continue running the forward replay orchestrator at each data-root refresh; overlap
   drift is a hard alarm.
6. Binance forward liquidation capture needs a permitted-region host.

## Binding Receipts To Keep

- `docs/preregistration/2026-06-18-continuous-live-v2-exit-redesign.md`
- `docs/preregistration/2026-06-18-continuous-v2-invvol-max4-replay.md`
- `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`
- `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md`
- `docs/preregistration/2026-06-15-operator-override-promote-continuous.md`
- `docs/preregistration/continuous-capacity-impact-2026-06-09.md`
- `docs/preregistration/continuous-dynexit-forward-shadow-2026-06-10.md`
- `docs/preregistration/sniper-staged-entries-2026-06-09.md`
- `docs/preregistration/r4-risk-model-verdict.md`

Closed research receipts, staged plans, and one-off helper scripts are intentionally
removed from the hot path. Git history is the archive.
