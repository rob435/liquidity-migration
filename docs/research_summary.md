# Research Summary

Updated: 2026-07-04.

This is the durable decision log. Historical receipts live in git history and
`docs/preregistration/INDEX.md`.

## Active Objects

| Object | Role | Current read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Continuous fade demo/paper book | VPS aligned; no forward trade sample yet |
| `LongV11aDivWeekendVol` | Long-native v11a demo/paper sleeve | Strongest current internal object; TP-tail dependent |

Mainnet is outside the current operating mode.

## Evidence Standards

- Forward demo/paper is the arbiter for execution behavior.
- Internal backtests are useful for mechanism and regression checks; they do not
  by themselves settle deployment or mainnet questions.
- PIT membership, causal feature availability, survivorship control, costs,
  funding, ledger identity, and reproducible artifacts are correctness gates.
- Exploratory runs can guide investigation, but cannot accept a parameter.

## Continuous v2

### Current Target

- Baseline clock: `2026-06-18T19:54:00Z`.
- Components: p3 `0.3333333333333333`, p4p3 `0.2222222222222222`, p4p5
  `0.4444444444444444`.
- Sizing: inverse vol, `TARGET_VOL_PER_NAME=0.01`, `VOL_WEIGHT_CLAMP=2`.
- Entries: `BTC_TREND_GATE=uptrend`, rmom q25, max 25 active shorts, max 5 new
  entries per cycle.
- Exits: 12% component TP and 24h max hold.
- Disabled: daily rebalance, daemon/server stop, `left_decile`,
  `stop_approach`, `failed_fade`, `breakeven`, re-entry cooldown.
- Hedge: BTC+ETH 2-factor hedge plus BTC-vol regime overlay.
- Add-ons: demo sniper execution watch; dynamic exit no-order shadow.
- Sizing overlay: `CTRL_BTC_RISK_70_90_35`.

Read-only VPS check on 2026-06-30 found commit `33ee7ffd2`,
`main...origin/main`, with long demo/paper, continuous demo/paper, shared risk,
and the continuous hedge timer active.

### Closed Continuous Research

| Arc | Result |
| --- | --- |
| Live-exit diagnosis | `stop_approach` and server-stop style exits broke the short-fade lifecycle; TP/24h became the v2 lifecycle. |
| Deep A/B foundation | No accepted parameter. Flow, conviction sizing, entry timing, and exit timing failed controls or data requirements. |
| TP variants | Bybit liked wider TP; Binance drawdown/MAR rejected the two-venue change. TP12 remains the current target, not broad proof. |
| One-minute execution books | No durable two-venue lead after path-aware controls. |
| Upper-wick sizing | Initial apparent gain was duplicate-counting; corrected full-ledger and parity checks retracted it. |
| BTC gate replacement | Replacement gates failed. `CTRL_BTC_RISK_70_90_35` improved MAR/drawdown but cut Binance total return, so it is a narrow sizing experiment. |
| Daily vol rebalance | 2026-06-25 TP12 A/B rejected rebalance ON. It mostly saturated at max leverage, worsened drawdown, and failed the MAR/worst-90d rule; keep disabled. |
| Regime-score work | No common robust replacement survived the current-control and anchor checks. |

Recurring conclusion: continuous signals exist, but most transforms either
vanish under execution constraints, split by venue, or act like leverage rather
than durable edge.

Daily-rebalance caveat: the 2026-06-25 A/B isolated the portfolio rebalance
layer on TP12 component ledgers. The current Bybit rebuild was only 77 calendar
days, and the live BTC-risk entry-size overlay was not embedded in those
component ledgers, so the run is rejection evidence, not positive acceptance
evidence for any new risk layer.

### Continuous Validation Baseline

2026-06-27 Phase-0 validation froze the current target under
`research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/`
(`continuous_ensemble_v2_baseline_current`, commit `9644fec`, profile hash
`c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`).
The TP12 + BTC-risk sizing + BTC/ETH hedge baseline returned +26.64% Bybit
and +18.84% Binance at full-book level, with MAR 7.33 / 5.72 and max DD
-1.13% / -1.02%. Direct per-row feature/data timing assertions now pass.
Label remains `exploratory`: skip/tail work is research maintenance rather
than acceptance evidence.

The 2026-07-03 no-hard-TP replay recomputed the same continuous target after a
fresh PIT refresh through the 2026-07-02 signal day. Artifacts live under
`/Users/jhbvdnsbkvnsd/SHARED_DATA/tail_no_tp_2026-07-03/`. Results:

| Venue | Component TP | Return | Ann. | Max DD | MAR | Sharpe | Worst day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | 12% | +24.63% | +7.02% | -1.20% | 6.33 | 3.10 | -0.93% |
| Bybit | none | +25.55% | +7.26% | -1.36% | 5.78 | 2.95 | -0.93% |
| Binance | 12% | +18.82% | +5.44% | -1.02% | 5.68 | 2.63 | -0.63% |
| Binance | none | +18.46% | +5.34% | -1.23% | 4.61 | 2.37 | -0.63% |

Interpretation: the book survives this historical replay without the hard TP,
but the hard TP improves risk quality. Bybit no-TP adds raw return but worsens
drawdown, MAR, and Sharpe; Binance no-TP is worse on return and risk-adjusted
metrics. Survival is mostly from small/inverse-vol exposure, max active/new
limits, 24h max-hold, BTC-risk sizing, and hedge/regime controls, not from
tail risk being solved. Label remains `exploratory`; this is not live-size
approval. Binance funding in this replay is `partial`, and the fresh proxy pull
failed for `ANTHROPICUSDT`, `EWTUSDT`, `IPUSDT`, `NFPUSDT`, and
`OPENAIUSDT`.

First diagnostics are cautionary. Trades that reached >=20% MAE recovered to
profit only 19.76% Bybit / 15.47% Binance. A diagnostic 20% stop cut component
net from +20.89% to +8.51% Bybit and +14.69% to +6.60% Binance. A 1h delay
worsened signal-level unit PnL/signal on both venues, while +1% adverse-limit
entry looked promising in signal-path diagnostics. Do not use this baseline as
live-size approval.

Engine-level added-delay replays rejected waiting longer. At full
component+BTC-risk+hedge portfolio level, Bybit baseline +26.64%/MAR 7.33 fell
to +23.58%/5.23 with +1h and +20.54%/6.35 with +2h; Binance baseline
+18.84%/5.72 fell to +17.41%/4.75 and +16.80%/5.32. The +4h replay timed out
with partial Bybit artifacts and is not counted as evidence.

A full +1% adverse-limit replay also rejected the signal-level lead. Bybit
baseline +26.64%/MAR 7.33 fell to +24.39%/5.94 with 2,131 component trades
versus 2,367 baseline trades; Binance +18.84%/5.72 fell to +16.59%/3.75 with
1,900 component trades versus 2,152. Treat this entry variant as rejected unless
future forward OOS evidence contradicts the replay.

Staged fixed-stop portfolio replays rejected simple price stops. At full
component+BTC-risk+hedge level, 20%/40%/80% fixed stops reduced Bybit to
+9.50%/MAR 0.94, +21.03%/3.38, and +23.96%/4.68, versus baseline
+26.64%/7.33. Binance fell to +7.55%/1.48, +12.70%/2.16, and +13.97%/2.50,
versus baseline +18.84%/5.72. Treat fixed tactical/catastrophic price stops as
rejected unless future forward OOS evidence contradicts the replay; loss
containment should move toward sizing, heat, and disaster accounting rather
than fixed price stops.

BTC-regime portfolio replays rejected both removing and retuning the 30d BTC
uptrend gate. Gate-off changed Bybit from +26.64%/MAR 7.33 to +26.53%/2.33 and
Binance from +18.84%/5.72 to +12.99%/0.86 while roughly doubling component
trades. The non-30d simple-return lookback grid also failed: best Bybit was 25d
at +20.72%/MAR 4.29, and best Binance was 60d at +20.25%/MAR 4.84, both below
the 30d baseline MAR. Treat this as rejection of the regime retune, not support
for the narrow 30d gate as a broader promoted parameter.

BTC-risk tail skip replay rejected replacing the existing 35% BTC-risk tail
sizing with a hard skip. The arm removed a net 7.52% Bybit / 7.53% Binance
component trades, inside the preregistered 5-15% range, but failed the
two-venue rule: Bybit changed from +26.64%/MAR 7.33/DD -1.13% to
+26.68%/MAR 7.08/DD -1.17%, while Binance improved from +18.84%/MAR
5.72/DD -1.02% to +20.49%/MAR 7.15/DD -0.89%. Keep the current 35% sizing
behavior unless forward OOS evidence contradicts the replay.

Synthetic squeeze survival diagnostics are survivable at current sampled size,
but not a tail-risk closeout. Worst active one-coin +100% shocks lost 3.12%
Bybit / 3.15% Binance; worst three-coin +50% shocks lost 3.28% / 3.49%; adding
a one-hour exchange outage and 10% extra exit damage to the one-coin +100%
shock lifted losses to 3.43% / 3.46%. All stayed inside the 50% ruin bar, but
drawdowns reached about -3% to -4% and recovery can take 259 days. The result
supports tiny-sizing discipline; it does not replace order-book gaps or
exchange liquidation mechanics.

Cluster risk-of-ruin bootstrap now exists for the same validation tape. Plain
10,000-path same-signal cluster bootstrap had p(DD >=10%) 0% on both venues and
no account-impairment paths; injecting one worst active +100% outage shock into
each path still left p(DD >=10%) 0% and account-impairment p 0%. The fragility
case is repeated bad clusters: sampling the worst 5% clusters at 3x weight made
p(DD >=10%) 33.70% Bybit / 66.18% Binance and annual-return p1 -5.40% /
-6.64%. This supports current tiny-size observation, but it is not a size
increase argument; portfolio heat caps and circuit breakers remain in scope.

Dynamic liquidation/outage 5m overlay now exists for the same synthetic
placements. All 42 rows had complete 5m coverage and no maintenance-proxy
liquidations. Worst peak net loss was 3.33% Bybit / 3.56% Binance with peak DD
-4.02% / -3.70%; worst flatten loss was 3.59% / 3.68% under the risk-daemon
down one-coin +100% scenario. This supports continued tiny-size observation but
is still not an exchange liquidation engine, order-book gap model, disaster-stop
placement proof, or live-size approval.

Disaster-loss sizing is stricter than the survival overlays. At a fixed +100%
adverse move and 0.10% equity per-trade disaster-loss budget, 97.34% Bybit /
97.44% Binance component trades exceed the budgeted safe notional; median
current/safe notional is 3.88x / 4.17x and p95 is 7.90x / 8.34x. Even at a
0.25% budget, fixed +100% flags 77.14% / 78.53% over budget. This is not a
claim that the current book is liquidating; it says future sizing needs an
explicit loss-at-disaster cap before any increase.

Conditional scale-in is a mechanism lead, not evidence to deploy. The by-trade
diagnostic over the frozen baseline tape found the best arm at 5% MAE trigger /
50% add-on: Bybit component net changed from 20.89% to 29.85%, and Binance from
14.69% to 20.84%. A preregistered full component+hedge overlay replay then
confirmed the return lift but rejected deployment: every tested arm worsened MAR
and drawdown on both venues. Best MAR arms were Bybit `mae05_add25`
(+31.17%/MAR 6.75/DD -1.43% vs baseline +26.64%/7.33/-1.13%) and Binance
`mae10_add50` (+23.54%/MAR 5.36/DD -1.36% vs +18.84%/5.72/-1.02%). Do not add
live/paper scale-in behavior from this evidence.

Sparse candidate-tape signal invalidation is negative from the current evidence.
The best active candidate-pressure arm, score>=99 after 3h while the short is
losing, reduced Bybit component net from 20.89% to 17.85% and Binance from
14.69% to 12.87%; all active arms hurt both venues. The BTC-trend rejection arm
had zero in-window hits. Do not add a live invalidation exit without a full
hourly state panel and full component+hedge replay. The 2026-06-28 hourly
coverage audit is not that panel: Bybit candidate-state/OI/funding/BTC coverage
is 2.45%/67.55%/100.00%/100.00% across 48,447 state rows, Binance is
2.25%/7.12%/99.74%/100.00% across 44,416 rows, and spread/depth plus sector
proxy coverage are still 0.00%.

DSR/PBO now directly warns against trusting the internal replay ranking surface.
Using only existing full-portfolio replay artifacts, the diagnostic found PBO
41.43% Bybit / 35.71% Binance across 21 variants per venue, with baseline DSR
probability only 23.17% / 20.08%. The best-Sharpe variants were not deployable
positives: Bybit chose `mae05_add25`, already rejected by scale-in replay
MAR/DD, and Binance chose `skip_btc_tail_035`, already rejected by the
two-venue rule. Treat this as inference-risk evidence, not an alpha verdict.

5m timing/path diagnostics now exist for the full validation signal set. The
15m delay, 30m delay, and next-red 15m variants all reduced unit PnL/signal
versus immediate on both venues; Binance had 33 rows excluded by the complete
24h 5m path rule, Bybit had 0. Forward-path curves, path labels, worst-trade
dependency, component ledger recombination, disaster heat, skip-feature
buckets, and hedge attribution were written under the same run directory. The
result is still exploratory because these are diagnostics, not portfolio
replays or live execution evidence.

## Long v11a

Latest internal cross-venue refresh through 2026-06-23:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Positive evidence:

- Both venues stay positive after removing their best month.
- 2x and 3x existing cost stress remain positive.
- Deterministic monthly bootstrap p05 remains positive: +5.76% Bybit, +5.17%
  Binance.
- Worst 12-month windows remain positive: +2.55% Bybit, +2.79% Binance.
- Active monthly sign agreement: 24/26.
- Same-entry paired trades: 146 common trades, 144/146 sign agreement, 0.9679
  return correlation.
- Removing BTCUSDT, top three positive symbols, or best exit day leaves both
  venues positive.
- Matched random-symbol null beaten on both venues.
- PIT OHLC exit-path validation supports all recorded exits under bar-end
  timestamps and stop-before-TP ordering.

Material caveats:

- Take-profit exits drive the result. Removing the TP bucket flips Bybit/Binance
  to -0.92%/-5.99%.
- Absolute return trails BTC buy-and-hold, though monthly MAR, beta, and
  beta-adjusted residual return are better.
- The latest evidence is still internal; no forward trade sample exists yet.

Closed long research:

| Arc | Result |
| --- | --- |
| FC sigma cadence loosening | More trades, worse guard outcomes; no retained arm. |
| Cross-venue stability audit | Positive but not enough to skip forward evidence. |

## Data And Ops

- Bybit and Binance PIT roots are current enough for the latest long refresh and
  continuous replay maintenance.
- 2026-06-27 5m backfill added canonical `klines_5m` coverage for every PIT
  manifest symbol-day in the continuous validation sample on both venues.
  Presence audit is clean; strict 288-bars/day audit still has partial source
  days after retry (781 Bybit / 25 Binance), so sub-hour research must handle
  those explicitly.
- Bybit June manifest kline/funding coverage is clean for refreshed work.
- Binance daily Vision kline/manifest coverage is current through the
  2026-07-02 signal day after the 2026-07-03 daily top-up. June/July-tail
  funding remains sparse for many manifest symbols; continuous Binance
  2026-07-03 replay funding is `partial`, while refreshed long trades use
  modeled funding.
- VPS forward capture is Bybit-only for live liquidation `allLiquidation` rows
  and hourly order-book depth bands. The short-lived
  `data/liquidations/binance` capture from 2026-07-02 is historical residue, not
  an active feed; the Binance liquidation leg is retired and excluded from
  liveness freshness checks.
- Reconciliation now runs from Windows with UTF-8 Python I/O and SCP fallback
  when `rsync` is absent.
- Latest full three-way reconcile found no unexplained drift and no active
  trade/order rows.
- Continuous forward-readiness gate run from the v2 baseline clock
  (`2026-06-18T19:54:00Z`) passed paper/demo rebalance telemetry audits
  (121 paper cycles / 123 demo cycles) and the new paper/demo operational-cycle
  audits (0 entry-risk blocks, 0 order failures, 0 unprotected-position
  seconds). It still found 0 paper trades, 0 demo trades, and 0 paired trades,
  so fill rate, fill latency, PostOnly cancel rate, fees, funding, maker/taker
  split, stop-placement latency, and stop-repair count are not yet measurable.
  The gate is not ready because paired trades are below the 20-trade warning
  threshold; this is lack-of-sample evidence, not detected paper/demo drift.
  The operational gate now surfaces portfolio-heat clamp and account-drawdown
  kill-switch counts in the top-level readiness report and treats any account
  drawdown kill-switch row as a readiness failure; the current rerun has 0 of
  both on paper and demo.
- Continuous current rows still show no entries or exits. Rows with
  `btc_trend_gate_allows_entry=false` are explained by PIT 30d BTC trend, not an
  unexplained daemon mismatch.
- Continuous submit-mode entries now have an entry-risk-health gate that blocks
  private snapshot errors, stale private execution WS after a stream has emitted,
  open continuous ledger symbols missing from the venue position snapshot, and
  exchange-only positions that can be attributed to a recent continuous entry
  order but have no open continuous trade row. It also blocks new submitted
  entries while an open non-hedge continuous position has no venue `stopLoss` in
  the private position snapshot; under current `STOP_LOSS_PCT=0`, that is a risk
  brake, not an accepted disaster-stop policy. Cycle rows and blocked-event
  JSONL now carry unprotected-position age telemetry. Dry-run/paper evidence
  cycles are not suppressed. Blocked submit cycles append
  `continuous_risk_events.jsonl` rows. Exchange-only positions with no
  continuous order evidence remain a ws_risk/reconciliation authority task.
- Continuous submit-mode lifecycle telemetry now classifies open ledger rows
  into explicit live states (`PROTECTED`, `PROTECTION_PENDING`,
  `EXIT_ORDER_SUBMITTED`, `ORPHAN`, etc.) from the ledger plus private position
  snapshot and records compact counts in cycle/risk-event rows. Submitted
  cycles now enforce an explicit trade-row lifecycle transition table before
  trade-ledger flush: closed/terminal rows cannot be reopened, close rows
  without prior trades are rejected, protected rows cannot silently regress to
  `PROTECTION_PENDING`, and in-flight exit markers cannot be dropped. Rejections
  are logged to `continuous_risk_events.jsonl`. Healthy submitted cycles now
  persist monotonic `PROTECTED` promotions from the private position snapshot
  onto full copied trade rows. Submitted live cycles also append
  `continuous_lifecycle_events.jsonl` for crash-safe preflight/order-prepared
  events, final order events, and accepted trade-row state writes; deterministic
  `event_key` fields support downstream de-duplication. `RECONCILED` and
  `FORCE_FLATTENED` remain reserved states, not active flow claims.
- `ws_risk` now appends stop/take-profit repair attempts to
  `reports/event-risk-ws/stop_audit_events.jsonl` after sleeve tagging, carrying
  target/current stop and TP, submit status, routed sleeve, link, and error text.
- Continuous submit-mode entries now apply a portfolio heat cap before candidate
  selection: non-hedge open notional times `entry_portfolio_heat_shock_frac`
  divided by equity, with a default 5% equity cap under a +100% shock. Dry-run
  and paper evidence cycles are not clamped; cycle rows record `portfolio_heat_*`
  and `skipped_portfolio_heat`.
- Continuous submit-mode entries now also have an account drawdown kill-switch:
  current wallet equity more than 2% below the prior healthy cycle high-water
  blocks new entries through `entry_risk_health`. Snapshot errors do not trip the
  drawdown rule on fallback equity; they already block as private snapshot
  errors. Cycle rows record `entry_account_drawdown_*`, and the
  forward-readiness operational audit fails explicitly if the kill-switch trips.

## Revisit Queue

1. Forward trade sample for both active systems.
2. Long v11a paper/demo/fill/funding audit once trades appear.
3. Promote no continuous skip/tail change from the timestamped tape. Gate-off,
   non-30d BTC-lookback regime changes, and BTC-risk 35% tail skip are already
   rejected by full replay. Synthetic active-book squeeze and cluster-bootstrap
   diagnostics plus the dynamic 5m outage overlay are survivable at current
   tiny size, but disaster-loss sizing flags strict per-trade budgets. The
   2026-07-03 no-hard-TP replay says the book survives without TP in historical
   data, but risk-adjusted quality gets worse, especially on Binance. Treat all
   of this as exploratory diagnostics rather than live-size evidence. The
   research tooling/report checklist is backed by frozen-baseline artifacts;
   conditional scale-in's full overlay replay lifted return but failed MAR/DD,
   and sparse-tape signal-invalidation exits were negative or zero-hit. The
   hourly state-coverage audit confirms the full invalidation panel is still
   unavailable. The DSR/PBO diagnostic marks the internal replay variant surface
   inference-fragile. The next registered tail method is
   `docs/preregistration/continuous-tail-budget-control-2026-07-03.md`: keep
   TP12/24h as lifecycle, then test loss-at-disaster sizing, portfolio heat
   caps, and drawdown step-down as a risk governor rather than another fixed
   price stop. The revised blacklist plan is
   `docs/preregistration/continuous-time-symbol-risk-2026-07-04.md`: after the
   negative Bybit time-stop diagnostic, focus on no-time-stop month-scale symbol
   blacklists and causal learned entry-time blackouts rather than forced UTC
   exits.
4. Continue live-safety audit work. Submitted-row lifecycle transition enforcement, `PROTECTED` trade-row
   promotion, and the append-only lifecycle event stream now exist. Risk/audit
   logs currently cover blocked entry-health events, rejected lifecycle
   transitions, stop-repair attempts, and lifecycle state transitions.
5. Only targeted continuous research with a specific missing-data or execution
   mechanism; no broad mining replay.
