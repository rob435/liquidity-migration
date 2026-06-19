# Research Summary - Liquidity Migration

**Updated:** 2026-06-19

This is the consolidated decision surface. Historical staged-program details,
one-off reports, exploratory harnesses, audit logs, and stale pre-registration
receipts belong in git history and local run artifacts, not in the hot path.

For the full promoted-in-code demo/paper trading lifecycle, including live exits
and systemd env overrides, see `docs/promoted_trading_logic.md`.

## Non-Negotiable State

- Research-stage only. Nothing is approved for real money.
- Forward demo/paper is the arbiter. There is no clean internal pre-2023 OOS root to
  rescue a result.
- Full-PIT membership, causal features, cost/funding treatment, ledgers, and
  reconstructable run records are correctness gates.
- The daily SHORT sleeve was erased on 2026-06-11 by operator order.
- Broad parameter mining is not current work. A new experiment needs a fresh
  dated pre-registration unless explicitly marked exploratory; exploratory runs
  cannot support promotion, deployment, or alpha acceptance.
- The 2026-06-19 next-level Continuous V2 plan is a program-level
  pre-registration, not an executable construction receipt. Its first gate is
  full 1m PIT data plus a path-aware execution engine before new alpha A/B cells.

## Current Objects

**Continuous fade book**

- Demo/paper object: `continuous_ensemble_v2`.
- The 2026-06-18 operator override froze the current three-component object:
  p3 `0.3333333333`, p4p3 `0.2222222222`, and p4p5 `0.4444444444`.
- The v2-forward control baseline starts at `2026-06-18T19:54:00+00:00`
  (`1781812440000`) and is the only control arm for future continuous A/B tests.
  Receipt: `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md`.
- The 2026-06-19 continuous v2 A/B foundation has run through control,
  delayed-feature sanity, feature almanac, discovery screens, and the amended A4B
  price/carry hedge-intensity arm. A4B clears the loose backtest-only pooled MAR
  rule, but Binance MAR worsens and robustness left tails stay negative. It is
  not accepted as a parameter change and is not wired into demo/paper. Verdict:
  `docs/preregistration/2026-06-19-continuous-v2-a4b-price-carry-verdict.md`.
- The 2026-06-19 data top-up fixed Binance metrics archive use for OI/taker flow
  and makes Binance `market_flow` / `idiosyncratic_flow` admissible in the
  foundation almanac. It does not unblock the original A4/C2/C3 arms because
  Bybit OI/flow coverage remains partial/event-scoped and
  `flow_resid_return` / `flow_squeeze` are not value-built. Receipt:
  `docs/preregistration/2026-06-19-continuous-v2-data-topup-flow-blockers.md`.
- The operator amended the continuous v2 A/B plan on 2026-06-19 to run C-book
  flow research on Binance only. That branch is exploratory mechanism research,
  not two-venue candidate evidence and not Bybit demo/paper wiring evidence.
  Amendment:
  `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`.
- The 2026-06-19 deep A/B pass produced **no candidate**; all tested mechanisms
  are closed with falsifier-backed negatives or are data-gated. Order flow (C):
  real trade-level signal, but C2/C3 hedge overlays and C1 idiosyncratic-flow
  sizing all fail (untradeable via hedge timing or sizing; `flow_resid_return`
  fails the null) — Bybit flow-archive build not justified. Conviction sizing (B):
  B1 score-margin and B1P path-shape both lose to their hash controls on both
  venues (a random same-distribution tilt even tripped the loose pooled-MAR rule —
  judge sizing arms against their hash, not the loose rule). Execution E1 intrabar
  entry-timing (Bybit-only exploratory) is closed by mechanism: net −20% to −35%
  with adverse selection (selling a fade short into intrabar strength selects
  continuation-risk losers), so a Binance sub-hourly backfill is not justified for
  timing; only cost/impact axes (E2 maker, E3 clip-size) remain, needing both-venue
  depth/fill data. Exit-timing (Book F, both-venue no-order shadow) is closed and
  the 24h hold validated: shorter-hold / time-decay / MFE-giveback rules all lose
  36–87% by cutting the 150–420 trades that ride to the 10% TP (the 24h `max_hold`
  bucket looks weak only because winners already left via TP — a selection illusion).
  Exit-alpha phase 2 (TP sweep + full lifecycle + vol-scaled TP) found the session's one
  robust improvement — raising the take-profit to 12–15% lifts Bybit MAR +1.8/+2.2 — but
  it doubles Binance drawdown (MAR −3.5), a fundamental venue split that vol-scaling can't
  reconcile; the both-venue frozen object stays and the Bybit-only TP gain is an
  operator-gated G2 venue-policy/forward-shadow lead. The recurring result is that v2's
  signals are real but diffuse — a real IC is not a tradable edge for this book. Final verdict:
  `docs/preregistration/2026-06-19-continuous-v2-ab-research-final-verdict.md`.
- The next-level Continuous V2 plan is registered at
  `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`.
  It freezes both the live override control and the pre-override evidence anchor
  as comparison baselines, then sequences data, execution, feature almanac, and
  only then limited A/B waves.
- Settings: inverse-vol component sizing (`target_vol_per_name=0.01`, clamp
  2.0), w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25, BTC-uptrend
  gate, TP/24h exits only, no daemon `left_decile`, `stop_approach`,
  `failed_fade`, `breakeven`, no re-entry cooldown, and no server stop.
- The temporary 2026-06-16 `BTC_TREND_GATE=off` plumbing window is closed in
  the live-v2 wiring.
- `continuous_ensemble_v2` is demo/paper only and not real-money-safe. The
  registered 2026-06-18 replay showed both daemon stops and the 25% server stop
  destroy the fade edge; a future mainnet path needs a different risk control.
- Demo fills are execution evidence, not alpha proof. The forward clock resets
  after material config-hash changes.

**Continuous risk overlays**

- BTC+ETH 2-factor hedge is retained, wired, and armed for demo/paper.
- Hedge warmstart staleness remains calendar-age based. Ledger-aware staleness
  after long flat periods is still an operator decision.
- BTC-vol regime hedge is live forward-watch only, not a promotion or
  real-money pass.
- Sniper is armed in demo, code default off.
- Dynamic exit remains no-order paper shadow only.

**Long-native v11a**

- Demo + paper services were re-enabled on 2026-06-16 at current v11a sizing.
- Current profile: `div` + volup125 + weekend 1.5x tilt.
- PE2 provisional entries remain off. Rejudge only after both full-PIT roots
  extend at least 60 days past 2026-05-28 and there are enough trades.
- Trade Atlas residue: weekend 1.5x tilt stays; repeat-name penalty,
  weekend-entry bonus, and continuous US-session penalty are forward-watch only.
- No real-money claim is allowed.

## Rejected Or Parked Lines

**Residualization target change - rejected**

- Keep the current `fwd_ret_1d` residualization target.
- Contemporaneous `ret_1d` was weaker on both venues.
- `fwd_intraday` only improved at near-zero operational margin; at a deployable
  margin it merely tied the current target.
- The rmom latency issue is not fixed by a target swap.

**Intraday residual reversal - rejected**

- The intraday residual signal IC was real, but the liquid-name short-hold gross
  Sharpe was near zero or negative.
- Taker execution was a wipeout.
- An idealized maker path did not rescue the book after liquidity gating,
  turnover, and funding drag.
- No live wiring, no forward maker shadow, and no further rmom squeeze work
  without genuinely new posted-order fill evidence.

**Rmom latency caveat**

- Daily rmom is causal but has no deployment-grade operational margin.
- Any revival must use realistic intraday latency, liquidity, cost, funding, and
  posted-fill evidence. Internal gross IC is not enough.

## Open Operator Queue

These are current decisions or correctness debts retained from the scrubbed
proposal/audit ledgers:

- Do not promote `continuous_ensemble_v2` to real money without a fresh
  risk-control design; it is intentionally no-stop demo/paper evidence.
- Decide whether A4B price/carry hedge intensity is worth a no-order forward
  shadow / separate paper sleeve despite the weak Binance and bootstrap evidence;
  otherwise park it.
- Decide whether hedge warmstart staleness should become ledger-aware.
- Keep Binance FAPI ancillary June top-ups current and finish Binance
  forward-liquidation capture from a permitted-region host.
- C-book flow work may proceed on Binance only under the 2026-06-19 amendment.
  If any two-venue C2/C3 claim is revived, build a resumable Bybit full-market
  taker-flow archive first; the current Bybit event-scoped flow tape is not
  enough for a two-venue C2/C3 decision.
- Continue forward replay at each data-root refresh; overlap drift is a hard
  alarm.
- Long provisional-trigger panel should use gap-safe calendar windows before
  PE2 is reconsidered.
- Long live sizing should either replicate the backtest per-symbol gross cap or
  fail fast when `max_per_symbol_weight != max_position_weight`.
- Long live vol-target sizing must not read incomplete current-day BTC vol if
  that path is changed; gate to closed bars.
- Full-PIT coverage should eventually add a per-symbol kline-vs-manifest lag
  check; this tightens a methodology gate and needs deliberate tests.
- WS multi-leg close accounting has known demo/paper ledger defects around fee
  aggregation, gross return, stale pending exits, and partial-reduce PnL. Treat
  ledger PnL diagnostics accordingly until fixed.

## Active Binding Receipts

- `docs/preregistration/2026-06-18-continuous-live-v2-exit-redesign.md` -
  registered exit redesign that freezes the demo/paper continuous lifecycle as
  v2.
- `docs/preregistration/2026-06-18-continuous-v2-invvol-max4-replay.md` -
  registered replay promoting inverse-vol entry sizing plus max4 daily
  vol-target rebalance into official v2 demo/paper wiring.
- `docs/preregistration/2026-06-18-continuous-v2-forward-baseline.md` -
  v2-forward reconcile/control baseline for future continuous A/B tests.
- `docs/preregistration/2026-06-19-continuous-v2-a4b-price-carry-verdict.md` -
  A4B price/carry hedge-intensity verdict; mixed exploratory result, not wired.
- `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md` -
  operator amendment allowing Binance-only exploratory C-book flow research.
- `docs/preregistration/2026-06-19-continuous-v2-data-topup-flow-blockers.md` -
  data top-up receipt; Binance OI/flow unblocked, Bybit/full residual flow still
  blocks original A4/C2/C3.
- `docs/preregistration/2026-06-19-continuous-v2-ab-research-final-verdict.md` -
  end-to-end verdict for the 2026-06-19 deep A/B pass; no candidate, branches closed.
- `docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-verdict.md`,
  `...-c1-flow-sizing-verdict.md` - Binance-only flow branch closed (overlays + sizing).
- `docs/preregistration/2026-06-19-continuous-v2-b-conviction-sizing-verdict.md` -
  both-venue conviction sizing closed; arms beaten by their hash controls.
- `docs/preregistration/2026-06-19-continuous-v2-e1-entry-timing-verdict.md` -
  Bybit-only intrabar entry-timing closed by adverse selection.
- `docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-verdict.md` -
  both-venue exit-timing shadow; simple exits closed, 24h hold validated.
- `docs/preregistration/2026-06-19-continuous-v2-f2-exit-tp-lifecycle-verdict.md`,
  `...-f2b-vol-tp-verdict.md` - raising TP is a robust Bybit-only MAR gain but a Binance
  drawdown loss (venue split); vol-scaled TP doesn't reconcile. No both-venue candidate.
- `docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md` -
  OPERATOR OVERRIDE (demo/paper, research-phase): daily vol-target rebalance DISABLED
  (reversible) + component TP promoted 10%->12% system-wide. Against the evidence (TP12 is
  Binance-MAR-negative / fails two-venue; the adjuster is value-adding); voids the forward
  ledger; owner-gated deploy + a volatility rework are the follow-ups. REAL_MONEY false.
- `docs/preregistration/2026-06-19-continuous-v2-voloff-retest-verdict.md` - retested the
  one-venue arms under the vol-off system: the adjuster confounded both (penalized A4B-Binance,
  inflated B1-Bybit). With it off, A4B is venue-neutral/within-noise (Binance bootP 0.24) and B1 is
  negative both venues (beaten by hash) -> neither is a robust two-venue candidate. No new candidate.
- `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md` -
  program-level next research plan; requires separate construction receipts
  before data-root changes or A/B execution.
- `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md` - BTC-vol
  hedge forward-watch clock.
- `docs/preregistration/2026-06-15-operator-override-promote-continuous.md` -
  registry override to include continuous in promoted profiles for demo/paper only.
- `docs/preregistration/continuous-capacity-impact-2026-06-09.md` - fill
  calibration and capacity debt.
- `docs/preregistration/continuous-dynexit-forward-shadow-2026-06-10.md` -
  dynamic-exit paper shadow bar.
- `docs/preregistration/sniper-staged-entries-2026-06-09.md` - sniper
  forward-watch receipt.
- `docs/preregistration/r4-risk-model-verdict.md` - residual-risk model
  foundation.

## Cleanup Policy

- Closed staged receipts, old exploratory reports, and one-off helper scripts
  should not be restored to the hot path.
- Local `~/SHARED_DATA/...` research artifacts are scratch unless a current
  binding receipt explicitly cites them.
- If a new experiment is requested, define the objective and success metric
  first, then create a fresh dated pre-registration before touching per-venue
  working datasets.
