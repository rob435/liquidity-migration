# Research Program State

**Last updated:** 2026-06-19

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
- Continuous v2 A/B foundation has run through control, delayed-feature sanity,
  feature almanac, discovery screens, and the amended A4B price/carry
  hedge-intensity arm. A4B is mixed exploratory evidence only: loose pooled MAR
  is positive, but Binance MAR worsens and bootstrap left tails are negative.
- The 2026-06-19 data top-up removed Binance OI/taker-flow as an almanac blocker
  by using Binance metrics archives, but the original A4/C2/C3 arms are still
  two-venue blocked: Bybit OI/flow is partial or event-scoped, and
  `flow_resid_return` / `flow_squeeze` are not value-built.
- Operator amended the continuous v2 A/B plan on 2026-06-19 to let C-book flow
  research proceed on Binance only. That branch is exploratory only and cannot
  clear the two-venue candidate or demo/paper bar.
- The 2026-06-19 deep A/B pass ran the full sequence and found **no candidate**.
  Closed with falsifier-backed negatives: C-book flow (C0 screen + C2/C3 hedge
  overlays + C1 idiosyncratic-flow sizing, all Binance-only exploratory) — flow is
  a real trade-level signal but untradeable via hedge timing or sizing; and
  Problem Book B conviction sizing (B1 score-margin, B1P path-shape, both venues)
  — both beaten by their hash controls (the random hash even tripped the loose
  pooled-MAR rule, exposing a sizing-mechanism artifact). Execution E1 intrabar
  entry-timing ran Bybit-only exploratory (Bybit `klines_5m`) and is **closed by
  mechanism**: net −20% to −35%, adverse selection (the missed shorts were the
  better ones), loses to a random-bar null — so a Binance sub-hourly OHLC backfill
  is not justified for entry timing. Remaining execution cost-axes (E2 maker, E3
  clip-size) need both-venue order-book depth / fill data. Exit-timing (Book F,
  both-venue no-order shadow) is also **closed and the 24h hold validated**:
  shorter-hold / time-decay / MFE-giveback rules all lose 36–87% on both venues by
  cutting the 150–420 trades that ride to the 10% TP (the 24h `max_hold` bucket
  looks weak only because winners already left via TP — a selection illusion), so
  the 24h hold stays. Exit-alpha phase 2 (TP sweep + full lifecycle + vol-scaled TP)
  found the one robust improvement of the session — raising the take-profit to
  12–15% lifts **Bybit** MAR +1.8/+2.2 (bootstrap 90–95%) — but the same change
  **doubles Binance drawdown** (MAR −3.5), a fundamental venue split that a
  volatility-scaled TP cannot reconcile. So the both-venue frozen object stays; the
  Bybit-only TP gain is an operator-gated Book G2 venue-policy / forward-shadow lead,
  not a frozen-object change. Final verdict:
  `docs/preregistration/2026-06-19-continuous-v2-ab-research-final-verdict.md`.
  Frozen v2 stays the anchor; next move is forward demo/paper accrual, not more
  in-sample mechanism mining.

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
  rmom q25, BTC-uptrend gate, TP/24h exits only, no daemon/server stop.
  **OPERATOR OVERRIDE 2026-06-19 (demo/paper only): component TP promoted 10% -> 12%,
  and the daily vol-target rebalance ("volatility adjuster", w90/tv0.045/max4/ddh-0.04)
  is DISABLED** (`enabled=False`; constant gross, no daily risk control) for the research
  phase — reversible one-line flip, to be reworked + retuned when research is finished.
  This voids the prior forward ledger (config-hash pin) and removes the book's only daily
  risk control; TP12 also fails the two-venue bar (Binance MAR-negative). Demo/paper only;
  not real-money-safe. Receipt:
  `docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md`.
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
- Operator-directed next-level Continuous V2 research is registered in
  `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`.
  It does not reopen closed branches by itself; the first gate is full 1m PIT
  data plus a 1m/trade-aware execution engine before any new alpha A/B cells.
  Wave 0 (freeze both controls) COMPLETE 2026-06-20: `V2_LIVE_RESEARCH_CONTROL`
  (`bfa8d385210d`) and `V2_EVIDENCE_ANCHOR` (`6579c8ece3bb`) reproduced and
  frozen (in-sample diagnostics, not promotion evidence); progress tracked in
  `docs/preregistration/2026-06-19-continuous-v2-next-level-progress-log.md`.
  Both 1m sources (Bybit trade archive, Binance Vision) confirmed reachable +
  checksum-valid; Wave 1 (build/audit 1m roots) is the active wave.
- C-book flow research may run on Binance only under the 2026-06-19 amendment.
  For any two-venue C2/C3 claim, still build a resumable Bybit full-market
  taker-flow archive first; do not treat the current event-scoped Bybit flow
  tape as full-market evidence.
- Operator-gated stability fixes for the frozen continuous v2 system.
- Operator decision on whether A4B deserves a no-order forward shadow / separate
  paper sleeve despite weak Binance robustness, or should be parked.
- Long v11a demo/paper monitoring.

Do not start broad in-sample research or parameter mining without a fresh
pre-registration and explicit operator direction.

## Open Operator Decisions

1. Do not treat `continuous_ensemble_v2` as real-money-safe. It is intentionally
   no-stop demo/paper only; any mainnet path needs a new risk-control design.
2. Decide whether hedge warmstart staleness should remain calendar-age based or become
   ledger-aware after long flat periods.
3. Keep Binance FAPI ancillary June top-ups current from a permitted-region host;
   the Binance metrics archive tail is complete through the current Binance root
   kline span as of the 2026-06-19 data receipt.
4. PE2 long provisional-entry OOS re-judgment is armed only after both full-PIT roots
   extend at least 60 days past 2026-05-28 and have enough trades.
5. Continue running the forward replay orchestrator at each data-root refresh; overlap
   drift is a hard alarm.
6. Binance forward liquidation capture needs a permitted-region host.
7. **2026-06-19 operator override (vol adjuster OFF + TP12) is a CODE change only.**
   Before the live demo/paper book trades it, an owner-gated deploy must: archive the
   continuous forward state dir + start a fresh clock (config hash changed), regenerate
   the hedge warmstart CSVs, and redeploy the VPS daemon via the pre-push gate. Not done
   here (no push/deploy). `REAL_MONEY` stays false. Reworking the volatility control
   (re-enable + retune) is the planned follow-up once research is finished.

## Binding Receipts To Keep

- `docs/preregistration/2026-06-18-continuous-live-v2-exit-redesign.md`
- `docs/preregistration/2026-06-18-continuous-v2-invvol-max4-replay.md`
- `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`
- `docs/preregistration/2026-06-19-continuous-v2-a4b-price-carry-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`
- `docs/preregistration/2026-06-19-continuous-v2-data-topup-flow-blockers.md`
- `docs/preregistration/2026-06-19-continuous-v2-ab-research-final-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-c1-flow-sizing-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-b-conviction-sizing-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-e1-entry-timing-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-f2-exit-tp-lifecycle-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-f2b-vol-tp-verdict.md`
- `docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md`
- `docs/preregistration/2026-06-19-continuous-v2-voloff-retest-verdict.md`
- `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
- `docs/preregistration/2026-06-19-continuous-v2-phase0-baseline-construction.md`
- `docs/preregistration/2026-06-20-continuous-v2-1m-data-foundation-construction.md`
- `docs/preregistration/2026-06-20-continuous-v2-intrabar-execution-engine-construction.md`
- `docs/preregistration/2026-06-20-continuous-v2-book-a-stops-tpsl-construction.md`
- `docs/preregistration/2026-06-20-continuous-v2-book-b-admission-construction.md`
- `docs/preregistration/2026-06-20-continuous-v2-book-g-volcontrol-construction.md`
- `docs/preregistration/2026-06-19-continuous-v2-next-level-progress-log.md`
- `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md`
- `docs/preregistration/2026-06-15-operator-override-promote-continuous.md`
- `docs/preregistration/continuous-capacity-impact-2026-06-09.md`
- `docs/preregistration/continuous-dynexit-forward-shadow-2026-06-10.md`
- `docs/preregistration/sniper-staged-entries-2026-06-09.md`
- `docs/preregistration/r4-risk-model-verdict.md`

Closed research receipts, staged plans, and one-off helper scripts are intentionally
removed from the hot path. Git history is the archive.
