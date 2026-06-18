# Research Summary - Liquidity Migration

**Updated:** 2026-06-18

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

## Current Objects

**Continuous fade book**

- Old deployed system restored as the research anchor on 2026-06-17.
- Demo/paper object: `continuous_ensemble_v2`.
- The 2026-06-18 operator override dropped the stale `tp14` leg. Current
  ensemble weights are p3 `0.3333333333`, p4p3 `0.2222222222`, and p4p5
  `0.4444444444`.
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
  after config-hash changes such as the `tp14` removal.

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
- Decide whether hedge warmstart staleness should become ledger-aware.
- Finish Binance FAPI ancillary June top-ups and Binance forward-liquidation
  capture from a permitted-region host.
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
- `docs/preregistration/2026-06-18-drop-tp14-continuous-ensemble.md` - accepted
  operator override to the 3-component continuous ensemble, demo/paper only.
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
