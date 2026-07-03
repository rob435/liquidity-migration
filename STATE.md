# Research Program State

Last updated: 2026-07-03.

Read this first. This page is live state, not a receipt archive. Historical
details are in git history, local artifacts, and
`docs/preregistration/INDEX.md`.

## Current Systems

| System | Current role | Read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Continuous fade demo/paper book | VPS aligned; needs forward trade sample |
| `LongV11aDivWeekendVol` | Long-native v11a demo/paper sleeve | Best current internal positive object; still needs forward sample |

Mainnet is not the current operating mode; changing that requires explicit owner
action and fresh evidence.

## What Is Wired

- `deploy/sleeves.env`: long demo/paper ON, continuous demo ON, continuous paper
  ON.
- Continuous deployed target: TP12, daily rebalance disabled, no daemon/server
  stop, inverse-vol sizing, BTC/ETH hedge, BTC-vol regime, and
  `CTRL_BTC_RISK_70_90_35` sizing overlay.
- Read-only VPS check on 2026-06-30: commit `33ee7ffd2`,
  `main...origin/main`; long demo/paper, continuous demo/paper, shared risk,
  and the continuous hedge timer are active.

## Continuous Read

- Baseline clock: `2026-06-18T19:54:00Z`.
- Core components: p3 weight `0.3333333333333333`, p4p3 weight
  `0.2222222222222222`, p4p5 weight `0.4444444444444444`.
- Active exits: 12% component TP and 24h max hold.
- Disabled exits/risk rules: `left_decile`, `stop_approach`, `failed_fade`,
  `breakeven`, re-entry cooldown, server stop.
- `CTRL_BTC_RISK_70_90_35`: MAR and drawdown improved on both venues; Binance
  total return fell. Treat it as a local demo/paper sizing experiment, not
  broad acceptance proof.
- Daily vol rebalance A/B on 2026-06-25 rejected turning rebalance ON for
  TP12 components. The ON rule mostly hit max leverage and failed the
  MAR/drawdown/worst-90d rule; keep it disabled.
- Continuous validation baseline, 2026-06-27:
  `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`
  froze the current local TP12 + BTC-risk sizing + BTC/ETH hedge target
  (commit `9644fec`, profile hash
  `c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`).
  Full-book returns were +26.64% Bybit / +18.84% Binance, MAR 7.33 / 5.72,
  max DD -1.13% / -1.02%. Direct per-row feature/data timing assertions now
  pass. Label remains `exploratory`: skip/tail work is research maintenance,
  not acceptance evidence.
- No-hard-TP replay, 2026-07-03: recomputed continuous v2 after fresh PIT
  refresh through the 2026-07-02 signal day. Artifacts:
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/tail_no_tp_2026-07-03/`. TP12 vs no-TP:
  Bybit +24.63%/MAR 6.33/DD -1.20% vs +25.55%/5.78/-1.36%; Binance
  +18.82%/5.68/-1.02% vs +18.46%/4.61/-1.23%. The book survived without hard
  TP in this historical replay, but risk-adjusted quality worsened. Keep TP12
  as the current target. This is `exploratory` survival evidence, not live-size
  approval; Binance funding was `partial`.
- 5m timing/path diagnostics, 2026-06-27: 15m delay, 30m delay, and next-red
  15m all underperformed the 1h immediate signal-level diagnostic on both
  venues. 5m complete-path exclusions were 0 Bybit and 33 Binance. Path labels
  marked 659 Bybit / 644 Binance `FAILED_FADE` trades and 13 / 19 `DISASTER`
  trades. Treat these as mechanism evidence, not a skip/entry rule.
- Added-delay portfolio replays, 2026-06-27: full component+BTC-risk+hedge
  replays rejected adding delay. Bybit baseline +26.64%/MAR 7.33 fell to
  +23.58%/5.23 with +1h and +20.54%/6.35 with +2h; Binance baseline
  +18.84%/5.72 fell to +17.41%/4.75 and +16.80%/5.32. The +4h arm timed out
  with partial Bybit artifacts and is not evidence.
- Adverse-limit portfolio replay, 2026-06-27: the +1% adverse-limit diagnostic
  did not survive full component+BTC-risk+hedge replay. Bybit baseline
  +26.64%/MAR 7.33 fell to +24.39%/5.94 with 2,131 component trades vs 2,367;
  Binance baseline +18.84%/5.72 fell to +16.59%/3.75 with 1,900 component
  trades vs 2,152. Reject this entry variant unless new forward OOS evidence
  contradicts the replay.
- Fixed-stop portfolio replays, 2026-06-27: staged 20%/40%/80% fixed adverse
  stops all trailed the no-stop baseline. Bybit fell to +9.50%/MAR 0.94,
  +21.03%/3.38, and +23.96%/4.68; Binance fell to +7.55%/1.48,
  +12.70%/2.16, and +13.97%/2.50. Reject fixed tactical/catastrophic price
  stops unless new forward OOS evidence contradicts the replay; prioritize
  sizing, heat, and disaster accounting over simple fixed stops.
- BTC-regime portfolio replays, completed 2026-06-28: gate-off and non-30d
  simple-return lookbacks were full component+BTC-risk+hedge replays and are
  rejected. Gate-off changed Bybit from +26.64%/MAR 7.33 to +26.53%/2.33 and
  Binance from +18.84%/5.72 to +12.99%/0.86. The best non-30d lookbacks still
  trailed the 30d baseline on MAR: 25d Bybit +20.72%/4.29 and 60d Binance
  +20.25%/4.84. This is not promotion evidence; it says not to retune the BTC
  gate from this grid.
- BTC-risk tail skip replay, completed 2026-06-28: `skip_btc_tail_035` was a
  full component+BTC-risk+hedge replay replacing the existing 35% BTC-risk tail
  sizing with a hard skip. It is rejected by the preregistered two-venue rule:
  Bybit changed from +26.64%/MAR 7.33/DD -1.13% to +26.68%/MAR 7.08/DD -1.17%,
  while Binance improved from +18.84%/MAR 5.72/DD -1.02% to +20.49%/MAR
  7.15/DD -0.89%. Net component trade removal was 7.52% Bybit / 7.53% Binance.
  Keep the current 35% sizing behavior unless forward OOS evidence contradicts
  this replay.
- Synthetic squeeze survival diagnostic, 2026-06-28: injected median/p95/worst
  active-book shocks into the frozen baseline. Worst
  active one-coin +100% shocks lost 3.12% Bybit / 3.15% Binance; worst
  three-coin +50% shocks lost 3.28% / 3.49%; a one-hour exchange-outage
  surcharge lifted one-coin +100% losses to 3.43% / 3.46%. All survived the
  50% ruin bar, but drawdowns reached about -3% to -4% and recovery could take
  259 days. This is diagnostic sizing evidence, not a liquidation-engine or
  live-size approval.
- Cluster risk-of-ruin bootstrap, 2026-06-28: ran 10,000 deterministic
  same-signal cluster bootstrap paths plus
  tail-injected variants. Plain cluster bootstrap had p(DD >=10%) 0% both
  venues and no account-impairment paths; one worst +100% outage shock still
  had p(DD >=10%) 0% and account-impairment p 0%. The stress read is
  worst-cluster recurrence: sampling the worst 5% clusters at 3x weight made
  p(DD >=10%) 33.70% Bybit / 66.18% Binance and annual-return p1 -5.40% /
  -6.64%. This supports tiny-size observation but keeps heat caps and circuit
  breakers in scope.
- Dynamic liquidation/outage overlay, 2026-06-28: overlaid synthetic active-book
  shocks on actual post-event 5m paths. All 42
  rows had complete 5m coverage and no maintenance-proxy liquidation. Worst
  peak net loss was 3.33% Bybit / 3.56% Binance with peak DD -4.02% / -3.70%;
  worst flatten loss was 3.59% / 3.68% under the risk-daemon-down one-coin
  +100% scenario. This supports continued tiny-size observation, not a size
  increase or exchange-liquidation-engine claim.
- Disaster-loss sizing diagnostic, 2026-06-28: compared current per-trade
  notional with loss-budgeted safe notional. Fixed
  +100% adverse moves with a 0.10% equity per-trade budget flag 97.34% Bybit /
  97.44% Binance component trades over budget; medians are 3.88x / 4.17x
  current/safe notional. Even a 0.25% budget still flags 77.14% / 78.53% over
  budget under +100%. This argues for explicit loss-at-disaster caps before any
  size increase.
- Conditional scale-in diagnostic, 2026-06-28: simulated add-on shorts after MAE
  thresholds on the frozen baseline tape. The
  best diagnostic arm was 5% MAE trigger / 50% add-on: Bybit component net
  changed from 20.89% to 29.85%, Binance from 14.69% to 20.84%. This is a
  directionally positive mechanism lead, but it is path-conditioned and not a
  full component+hedge portfolio replay; no live sizing change follows from it.
- Scale-in portfolio replay, 2026-06-28: replayed `mae05_add25`,
  `mae05_add50`, and `mae10_add50` as explicit child
  shorts through component MTM and the BTC/ETH hedge. Returns increased on both
  venues, but every arm worsened MAR and drawdown. Best MAR arms were Bybit
  `mae05_add25` (+31.17%/MAR 6.75/DD -1.43% vs +26.64%/7.33/-1.13%) and
  Binance `mae10_add50` (+23.54%/MAR 5.36/DD -1.36% vs +18.84%/5.72/-1.02%).
  Reject live/paper scale-in behavior unless new forward evidence contradicts
  this replay.
- Signal-invalidation diagnostic, 2026-06-28: simulated sparse candidate-tape
  exits over explicit future same-symbol
  candidate rows only. All active candidate-pressure arms reduced component net
  on both venues; the least harmful active arm was score>=99 after 3h, changing
  Bybit from 20.89% to 17.85% and Binance from 14.69% to 12.87%. The BTC-trend
  rejection arm had zero in-window hits. The follow-up hourly state-coverage
  audit wrote `signal_invalidation_hourly_state_panel.parquet` and found Bybit
  candidate-state/OI/funding/BTC coverage of 2.45%/67.55%/100.00%/100.00% over
  48,447 rows and Binance 2.25%/7.12%/99.74%/100.00% over 44,416 rows; spread
  and depth plus sector proxy coverage remain 0.00%, so the full panel is not
  ready. Do not add this live exit without a full hourly state panel and full
  component+hedge replay.
- DSR/PBO diagnostic, 2026-06-28: used only frozen full-replay artifacts, no new
  strategy sweeps. Across 21 full-replay variants
  per venue, PBO was 41.43% Bybit / 35.71% Binance and baseline DSR probability
  was 23.17% / 20.08%; best-Sharpe variants were already rejected by the
  scale-in and BTC-risk tail-skip replay rules. Treat the internal replay
  surface as inference-fragile; do not trust internal Sharpe/MAR rankings as
  deployment proof.
- Continuous entry risk-health gate, 2026-06-28: submit-mode new entries now
  block and record `entry_risk_health_*` when private snapshots error, when a
  genuine private execution WS stream has emitted and then gone stale beyond the
  configured threshold, or when an open continuous ledger symbol is missing from
  the venue position snapshot. It also blocks exchange-only positions that can
  be attributed to a recent continuous non-reduce-only entry order but have no
  open continuous trade row, and blocks new submitted entries when an open
  non-hedge continuous position has no venue `stopLoss` protection in the
  private position snapshot. The current v2 profile still has `STOP_LOSS_PCT=0`,
  so this is a brake on adding risk while primary positions are unprotected, not
  a stop-policy acceptance claim. The cycle rows and risk events now include
  unprotected-position age telemetry (`entry_risk_health_unprotected_*`) so the
  operator can see how long the exposure has been unprotected. This covers the
  first live-safety checklist items without changing dry-run/paper evidence
  flow. Exchange-only positions with no continuous order evidence remain a
  ws_risk/reconciliation authority task. Blocked submit cycles append
  `continuous_risk_events.jsonl` with `entry_risk_health_blocked` events.
- Continuous lifecycle telemetry, 2026-06-28: the submit-mode entry gate now
  classifies open continuous trades into explicit live states such as
  `PROTECTED`, `PROTECTION_PENDING`, `EXIT_ORDER_SUBMITTED`, and `ORPHAN` from
  the ledger plus private position snapshot, and persists compact counts in
  cycle rows/risk events. Submitted cycles now enforce an explicit trade-row
  lifecycle transition table before trade-ledger flush: closed/terminal rows
  cannot be reopened, close rows without prior trades are rejected, protected
  rows cannot silently regress to `PROTECTION_PENDING`, and in-flight exit
  markers cannot be dropped. Rejections are logged to
  `continuous_risk_events.jsonl`. Healthy submitted cycles now also persist
  monotonic `PROTECTED` promotions from the private position snapshot onto full
  copied trade rows. Submitted live cycles also append
  `continuous_lifecycle_events.jsonl` for crash-safe preflight/order-prepared
  events, final order events, and accepted trade-row state writes; `event_key`
  gives downstream de-duplication. `RECONCILED` and `FORCE_FLATTENED` remain
  reserved states, not active flow claims.
- Stop repair audit logging, 2026-06-28: `ws_risk` now appends
  `reports/event-risk-ws/stop_audit_events.jsonl` rows for stop/take-profit
  repair attempts after sleeve tagging. Each row carries target/current stop and
  TP, submit status, sleeve, link, and error text. This is audit telemetry only;
  it does not change order routing or repair behavior.
- Continuous portfolio heat cap, 2026-06-28: submit-mode continuous entries now
  clamp `_eff_max` using a disaster-loss heat proxy:
  non-hedge open notional * `entry_portfolio_heat_shock_frac` / equity, with a
  default 5% equity cap under a +100% shock. Dry-run/paper evidence is not
  clamped. Cycle rows record `portfolio_heat_*` and `skipped_portfolio_heat`.
- Continuous account drawdown kill-switch, 2026-06-28: submit-mode entries now
  block through `entry_risk_health` when current wallet equity is more than 2%
  below the prior healthy cycle high-water mark. Snapshot errors do not trip the
  drawdown check on fallback equity; they already block through
  `private_snapshot_error`. Cycle rows record `entry_account_drawdown_*`.
- Continuous forward-readiness gate, 2026-06-28: ran
  `continuous-forward-readiness` from the v2 baseline clock
  (`2026-06-18T19:54:00Z`) against local paper/demo roots. Paper and demo
  rebalance-cycle audits passed (121 paper cycles / 123 demo cycles, no
  rebalance telemetry issues). Paper and demo operational-cycle audits also
  passed: 0 entry-risk blocks, 0 order failures, and 0 unprotected-position
  seconds. There were still 0 paper trades, 0 demo trades, and 0 paired
  paper/demo trades, so fill rate, fill latency, PostOnly cancel rate, fees,
  funding, maker/taker split, stop-placement latency, and stop-repair count are
  not yet measurable. The refreshed operational gate also reports 0
  portfolio-heat clamps and 0 account-drawdown kill-switch activations on both
  paper and demo; account drawdown activation is now a first-class readiness
  failure even if mixed-version cycle rows are missing `entry_risk_health`
  reason text. Readiness is `False` because paired trades are below the 20-trade
  sample warning threshold; this is lack-of-sample evidence, not detected
  paper/demo drift.
- The 2026-06-19 and later continuous A/B work produced no accepted candidate.
  Flow, conviction sizing, intrabar entry timing, hold/exit timing, TP variants,
  upper-wick sizing, and gate replacements either failed hash/two-venue controls
  or were not executable with current data.

## Long v11a Read

Latest internal cross-venue refresh through the 2026-06-23 signal day:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Durability checks:

- Positive after removing best month and after 2x/3x cost stress.
- Deterministic monthly bootstrap p05 positive on both venues.
- Worst 12-month windows positive.
- Active monthly sign agreement: 24/26.
- Paired same-entry signal agreement: 144/146, return correlation 0.9679.
- Random-symbol null beaten on both venues.

Material caveat: take-profit tail winners carry the sleeve. Removing the TP exit
bucket flips both venues negative. PIT OHLC exit-path validation supports the
recorded TP exits mechanically; it does not remove the concentration caveat.

## Data And Reconciliation

- Full-PIT Bybit and Binance roots are current through the 2026-07-02 signal
  day for continuous replay maintenance after the 2026-07-03 Binance daily
  Vision top-up.
- Continuous 5m kline backfill, 2026-06-27: filled `klines_5m` across the
  validation sample for every PIT manifest
  symbol-day on both venues. Final presence audit has 0 missing symbol-days;
  strict 288-bars/day audit still shows partial exchange source days
  (781 Bybit / 25 Binance) after retry. Do not synthesize 5m paths from 1h bars.
- Binance June/July-tail funding remains sparse for many manifest symbols.
  The 2026-07-03 continuous Binance replay used `partial` funding; refreshed
  long trades used modeled funding.
- Forward capture on the VPS is Bybit-only for both live liquidation
  `allLiquidation` rows and hourly order-book depth bands. The briefly collected
  `data/liquidations/binance` files from 2026-07-02 are historical residue only;
  the Binance liquidation leg is disabled and the liveness watchdog ignores it.
- Latest full three-way reconcile exited 0, pulled demo/paper telemetry, rebuilt
  PIT context, and found no unexplained live/paper/model drift.
- No active forward trade/order rows yet for the active sleeves; current
  evidence is therefore state/reconciliation/readiness, not performance.

## Current Next Work

1. Let forward demo/paper accrue actual trade samples.
2. Keep running `scripts/reconcile.sh` after meaningful VPS/data changes.
3. Finish continuous tail work before treating any diagnostic improvement as
   actionable. Added-delay, +1% adverse-limit, staged fixed-stop, gate-off,
   non-30d BTC-lookback, and BTC-risk 35% tail-skip replays are now rejected;
   synthetic active-book squeeze, cluster-bootstrap, and dynamic 5m outage
   diagnostics are survivable at current tiny size, while disaster-loss sizing
   flags strict per-trade budgets. The 2026-07-03 no-hard-TP replay survives
   historically but worsens risk-adjusted quality, especially on Binance. All
   remain exploratory, not live-size evidence. The 5m data is present; keep
   excluding or explicitly accounting for the documented partial source days.
   The plan's research tooling/report checklist is now backed by frozen
   baseline artifacts. Conditional scale-in's full overlay replay lifted return
   but failed MAR/DD, and signal-invalidation exits were negative/zero-hit on
   the sparse tape. The hourly coverage audit also confirms candidate-state
   sparsity, missing spread/depth, and missing sector-proxy state. DSR/PBO now
   flags the full-replay variant surface as inference-fragile. None of these can
   influence deployment without new forward OOS evidence.
4. Continue live-safety audit work: the submitted-row lifecycle transition table, `PROTECTED` trade-row
   promotion, and append-only lifecycle event stream are now implemented. The
   new entry risk-health gate covers private snapshot/WS stale,
   continuous-ledger-missing-position, recent-continuous-entry
   exchange-only-position, and non-hedge unprotected-position cases; the
   append-only risk event log currently records blocked entry-health events with
   unprotected-position age and lifecycle-state telemetry. `ws_risk` stop repair
   attempts now have append-only audit JSONL. Submit-mode entries are also
   portfolio-heat capped and account-drawdown gated; the forward-readiness
   operational audit now fails explicitly on account-drawdown kill-switch rows.
5. Do not reopen broad continuous mining without a specific falsifiable
   hypothesis and missing-data plan.
6. If long v11a receives forward trades, audit paper/demo/fills/funding before
   making any claim from the sample.

## Canonical Docs

- `docs/research_summary.md` - compact decision log.
- `docs/promoted_trading_logic.md` - active lifecycle and runtime boundary.
- `docs/data_roots.md` - data-root contract.
- `docs/pit_gate.md` - PIT/reconcile contract.
- `docs/preregistration/INDEX.md` - active anchors and closed arcs.
