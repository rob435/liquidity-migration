# Continuous Trading Logic Critique

Date: 2026-07-10

Object: `continuous_ensemble_v2`

Evidence label: `exploratory`

Real-money verdict: **blocked**. Keep demo/paper only.

## Remediation status after this audit

The hedge findings below describe the system at audit time. The 2026-07-10
follow-up has since:

- replaced the 00:35-only timer with five-minute idempotent target
  reconciliation;
- rebuilt the Bybit tape from the current TP12 + BTC-risk object on stable-only
  RMOM through 2026-07-09, with modeled funding and source-summary SHA-256;
- made stale non-flat state fail and page even when the target is below the $25
  resize floor;
- distinguished validated data freshness from the most recent nonzero strategy
  observation.

The replacement is a disclosed non-equivalent correctness migration. It does
not close the remaining hedge parity work: entry/exit-triggered reconciliation,
target-versus-actual beta-dollar telemetry, event-time research replay, and a
causal automatically updated realized live-return feed remain open. The other
P0 findings in this document—execution calibration, admission parity, fail-open
risk state, and unbudgeted single-name/cluster tails—also remain open.

## Executive verdict

The strategy has a plausible short-horizon crowded-pop reversal mechanism, and
the repository's PIT, causality, funding, ledger, and falsifier discipline is
well above a casual retail backtest. That is the strongest honest statement.

It is not close to a Jane Street-grade trading object yet. The limiting problem
is not a missing clever signal. The research simulator and the executable book
do not currently implement the same admission, fill, hedge, and risk lifecycle.
The first clean forward fills are far more expensive than the model, the hedge
can be absent for most or all of a 24-hour trade, the live selector omits a
material research crowding rule, and the book has no active ex-ante tail budget.
On top of that, the internal window is spent, the variant surface is
inference-fragile, and there is no clean OOS result.

The correct posture is:

1. freeze new alpha/parameter mining;
2. make backtest, paper, and demo share one decision and portfolio state
   machine;
3. replace the idealized fill/hedge model with the executable lifecycle;
4. install fail-closed entry and tail-risk controls;
5. then collect a new forward sample indexed by independent signal clusters,
   not duplicated component rows.

The reported current control remains useful as a research regression fixture.
It is not a reliable estimate of deployable return or drawdown.

## Current evidence, without marketing language

The 2026-07-03 control reports:

| Venue | Return | Max DD | MAR | Important caveat |
| --- | ---: | ---: | ---: | --- |
| Bybit | +24.63% | -1.20% | 6.33 | modeled execution; no clean OOS |
| Binance | +18.82% | -1.02% | 5.68 | funding is partial; modeled execution |

These are attractive internal numbers. They are still labelled `exploratory`
by the reports themselves. Historical overfit diagnostics are not reassuring:

| Venue | PBO | Baseline DSR probability |
| --- | ---: | ---: |
| Bybit | 41.43% | 23.17% |
| Binance | 35.71% | 20.08% |

There are 2,279 Bybit and 2,152 Binance component trade rows, but only 896 and
872 unique `(symbol, signal_ts)` decisions. Respectively 93.90% and 92.89% of
component rows sit on a decision shared by at least two components. On Bybit,
626 of 896 decisions appear in all three components; on Binance, 561 of 872 do.
The three sleeves are mostly three weights on the same bet, not three
independent alpha sources. Trade-row count must never be used as effective
sample size.

## Severity-ranked findings

### P0 — forward execution invalidates the current cost assumption

The research engine assumes a next-bar entry and charges a modeled round trip:

```text
2 * (5.5 bps taker + 2.5 bps spread)
+ 2 * 50 bps * sqrt(position_notional / signal-hour turnover)
```

The actual current component ledgers imply this round-trip distribution:

| Venue | Mean | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 23.30 bps | 22.53 bps | 30.69 bps | 37.17 bps |
| Binance | 19.90 bps | 19.24 bps | 25.38 bps | 32.53 bps |

The first four clean post-reset Bybit pairs show:

| Metric | Observed |
| --- | ---: |
| Mean adverse entry-price drift | 129.80 bps |
| Median adverse entry-price drift | 141.65 bps |
| Worst adverse entry-price drift | 170.73 bps |
| Recorded entry fee range / mean | 5.50–11.00 / 6.88 bps |
| Cycle-start to venue-fill delay | 148.7–165.5 seconds |

The mean adverse price drift alone is 5.57 times the entire modeled Bybit round
trip. Price drift plus the recorded entry fee is already 5.87 times modeled
round-trip cost before any exit cost or exit slippage. This sample is only two
symbols and one entry day, so it is not a stable slippage estimator. It is more
than sufficient to reject the claim that the present fill model is calibrated.
The 11 bps TAC fee also needs attribution before assuming a universal 5.5 bps
venue rate or trusting the local fee aggregation.

The latency also changes the exit strategy. Live calculates the absolute TP
from the pre-order ticker and sends it with the market entry; after the actual
fill arrives, it updates `entry_price` but does not rebase `take_profit_price`.
The four rows labelled TP12 therefore have these actual fill-to-TP distances:

| Symbol/component | Label | Actual fill-to-TP distance |
| --- | ---: | ---: |
| TAC p3 | 12% | 11.42% |
| SKL p3 | 12% | 10.79% |
| SKL p4p3 | 12% | 10.70% |
| SKL p4p5 | 12% | 10.48% |

The venue carries one net SKL position; its quantity-weighted fill makes the
actual net position-to-TP distance about 10.63%. The component figures above
show the attribution mismatch.
These rows cannot be counted as forward TP12 evidence; adverse entry drift has
mechanically turned them into roughly TP10.5–TP11.4 trades.

The 24-hour deadline is likewise anchored to cycle start, 149–165 seconds
before these fills. The executable lifecycle is not currently TP12/24h even
when every order succeeds.

SKL's three component market orders are also submitted sequentially and filled
at 0.005257, 0.005252, and 0.005239: a 34.2 bps spread from first to last. The
venue should receive one net same-symbol target delta per decision batch, with
the confirmed aggregate fill allocated back to components pro rata. Component
iteration order should not create different execution economics.

As a sensitivity, applying an extra 129.8 bps to every historical entry would
reduce mean per-notional trade expectancy from 186 to 56 bps on Bybit and from
146 to 16 bps on Binance. That is not a forecast; it demonstrates that entry
latency is economically capable of consuming roughly 70%–89% of the measured
average edge.

The paper benchmark is also too flattering. It samples an idealized price near
the cycle boundary while demo spends roughly 2.5 minutes computing state and
reaching a fill. The order ledger does not separately stamp decision, submit,
acknowledgement, and fill times, so compute latency cannot be separated cleanly
from venue latency.

Required response:

- stamp `decision_ts`, `risk_accept_ts`, `submit_ts`, `ack_ts`, first fill, last
  fill, and protection-active times;
- block stale entries beyond a pre-registered latency SLA;
- make paper emulate the same submission delay, while retaining a separate
  ideal-price benchmark;
- replay empirical latency and next-executable prices in the simulator;
- aggregate same-symbol component targets before submission, then rebase the
  venue TP and max-hold deadline from confirmed net fills;
- calibrate spread/impact by symbol, time, participation, volatility, and the
  pump-event state. Signal-hour turnover is not ADV and is not an order book.

No return/MAR claim should advance until all-in forward execution cost is inside
a pre-registered edge budget with uncertainty measured across independent
decision clusters.

### P0 — the research hedge and the executable hedge are different objects

Research applies a causal daily BTC/ETH hedge ratio to every daily return row.
The live manager runs once at 00:35 UTC, up to 120 seconds later. Trades can
enter every hour and last at most 24 hours.

Historical entry timestamps imply the following marginal wait to the next live
hedge timer:

| Venue | Median wait | Mean wait | Trades exiting before first possible resize |
| --- | ---: | ---: | ---: |
| Bybit | 12.58h | 12.59h | 366 / 2,279 (16.06%) |
| Binance | 12.58h | 13.00h | 305 / 2,152 (14.17%) |

The current TAC entry at 02:00 and SKL entries at 06:00 occurred after the
00:35 run. They therefore wait about 22.58 and 18.58 hours for the next timer.
The current hedge ledger is flat.

The shipped Bybit warm-start ends on 2026-05-23. At the audit time it was 48
calendar days old, while the runner's declared maximum is three days. The code
claims to extend the model with realized live book days, but `_live_book_state`
always returns an empty `live_unit_by_day`; the runner also passes an empty live
BTC map. Beta therefore remains frozen to the committed CSV until an operator
regenerates, commits, and deploys a new artifact.

`beta_window_days=90` is also a misleading unit name. The estimator slices the
last 90 **ledger observations**, not the last 90 calendar days. In the current
warm-start, those 90 rows span 142 calendar days on Bybit and 139 on Binance.
Flat periods therefore make the effective memory older even before the explicit
48-day artifact staleness. Either rename this to an observation window and add
a calendar-age constraint, or implement an actual calendar/time-decayed model.

The stale-data guard does not necessarily fail. A resize is planned only when
the target delta is at least $25. Recent armed runs computed target notionals of
about $0.85–$2.52, produced no plan, exited green, and therefore bypassed the
stale-warmstart block. This is a silent unhedged state, not a healthy hedge run.

The hedge is material to the research headline. Removing hedge return, funding,
and cost columns from the current daily artifacts and recompounding changes:

| Venue | Reported hedged return | Diagnostic unhedged return | Hedge delta |
| --- | ---: | ---: | ---: |
| Bybit | +24.63% | +21.73% | +2.90 pp |
| Binance | +18.82% | +15.92% | +2.90 pp |

The live manager also multiplies the learned ratio by current gross exposure
relative to 50%; the research daily hedge does not. This may be a sensible live
risk adaptation, but it is not parity. It must be modeled and tested explicitly.

Required response:

- turn the hedge into an idempotent target-position manager triggered after
  every accepted entry/exit batch, with a periodic reconciler as backup;
- define a maximum unhedged exposure-time SLA and fail/block new risk when it is
  breached;
- source beta state from a causal, automatically updated daily return ledger;
- fail any armed non-flat run on stale model state even when the desired order
  is below the resize threshold;
- record and alert `target hedge`, `actual hedge`, `unhedged beta dollars`, and
  time outside tolerance;
- replay this exact event-time lifecycle, minimum-order behavior, and turnover
  in research before calling the portfolio hedged.

At tiny demo size it can be rational not to send a $2 hedge. If so, label the
demo explicitly unhedged below economic minimum and stop attributing the
research hedge to its expected behavior.

### P0 — research and live do not admit the same trades

Every current research component has `entry_crowding_max_fresh=2`. If more than
two fresh candidates exist in that component and signal hour, research skips
all of them. The live `ContinuousDemoCycleConfig` has no corresponding crowding
field and its per-component selection loop never enforces this rule.

The current candidate tapes show that this is not a dormant difference:

| Venue | Candidate rows | Research crowding rejects | Signal hours affected |
| --- | ---: | ---: | ---: |
| Bybit | 13,515 | 632 | 87 |
| Binance | 13,453 | 943 | 140 |

Other declared differences:

- research runs each component with its own `max_active=25`, then combines the
  ledgers;
- live counts globally held unique symbols, but caps five new component legs per
  cycle and permits several components on the same symbol;
- the combined frozen ledgers contain six new legs on 19 Bybit and 22 Binance
  entry hours, so a literal five-leg live cap cannot reproduce all of them;
- the 25-symbol cap was not historically binding: maximum combined unique
  active symbols was seven, although component legs reached 20/21;
- live pauses after eight adverse component exits inside 24 hours; research has
  this breaker disabled.

The shared feature panel is not enough to claim parity. Admission, capacity,
sizing state, and portfolio state are part of the strategy.

Required response: one pure decision kernel must consume a time-stamped market
snapshot plus prior portfolio/risk state and emit all candidate reasons and
desired target exposures. Research, paper, and demo must call that kernel. Live
safety blocks may be stricter, but every difference must be explicit in the
artifact and replayable.

### P0 — tail risk is knowingly unbudgeted and some risk paths fail open

The active profile has all of the following off:

- server/fixed stop;
- portfolio heat cap;
- account-drawdown entry kill switch;
- daily volatility/drawdown rebalance.

The live adverse-exit pause acts only after losses have been realized. It does
not cap exposure before a single-name gap or correlated squeeze.

The current historical book admits these simultaneous shock placements:

| Venue | Worst one-name +100% loss | Worst top-three +50% loss | Reported baseline DD |
| --- | ---: | ---: | ---: |
| Bybit | 3.12% | 3.28% | 1.20% |
| Binance | 3.15% | 3.49% | 1.02% |

At a 0.10% equity loss budget under a +100% name shock, 97.34% of Bybit and
97.44% of Binance component trades are oversized. Even at 0.25%, 77.14% and
78.53% are oversized. The registered 0.10%/0.15%/0.25% budget matrix has not
run.

Fixed 20%/40%/80% stops hurt both-venue historical MAR. That rejects those
particular tactical exits under the tested fill semantics. It does not prove
that stopless sizing is safe. An entry loss budget is not a stop: it prevents
exposure before a jump and makes no claim about an executable post-jump fill.

The BTC-risk overlay cannot substitute for a hard risk limit. It is deliberately
non-monotonic: scores in `[0.70, 0.90)` receive 0.35x, but the most extreme
scores `>=0.90` return to 1.0x. In the current artifacts the middle band is
negative and the small extreme band is positive on both venues (20 Bybit and 22
Binance unique decisions). That makes this a fitted alpha-conditioned sizing
overlay, not a monotonic safety control. Its missing selection receipt and tiny
extreme sample make the cut points inference-fragile.

Its runtime invariants also need correction:

- state load or scoring failure silently restores multiplier `1.0`; the frozen
  executable object therefore changes on error rather than blocking or using a
  declared conservative fallback;
- liveness does not alert on `btc_risk_sizing_error`;
- simultaneous candidates are scored sequentially, updating the global BTC
  history inside one decision timestamp. On 2023-12-28 Binance this made
  ICPUSDT 0.35x and SSVUSDT 1.0x under identical BTC context solely because of
  within-batch order;
- research builds BTC-risk history from the component-selected decisions;
  live persists only decisions that survive its different caps and execution
  path. Their percentile states can diverge.

All candidates at one timestamp must share the same prior global BTC state.
Score the batch from a frozen pre-batch snapshot, then commit the batch once.
Any risk-state corruption must block entries or use the conservative multiplier.

### P1 — the `30d` issue is real, but not for the reason first suspected

Replacing 30 with `365 / 12` is not the right direct fix.

- `365 / 12 = 30.4167` days.
- The repo's average-year convention is `365.25 / 12 = 30.4375` days.
- The difference between 30 and 30.4375 days is only 10.5 hours.
- A prior-completed-daily-bar feature cannot express that fractional day without
  changing its data and timing semantics.
- If the hypothesis is a human calendar month, use calendar-month arithmetic;
  calendar months are not a constant number of days.

The more important defect is that the active “30d BTC return” is not an
endpoint or compounded return. `_btc_trend_returns` sums 30 daily simple
returns. Because of volatility drag, its sign can be positive while BTC's
actual endpoint return over the same prior window is negative.

On the current full-PIT BTC histories:

| Venue | Comparable days | Gate-sign disagreements | Share |
| --- | ---: | ---: | ---: |
| Bybit | 1,986 | 54 | 2.72% |
| Binance | 2,344 | 60 | 2.56% |

All observed disagreements were summed-simple positive versus endpoint
negative. They touch 93 of 2,279 Bybit and 89 of 2,152 Binance selected
component trades, about 4.1% on each venue. Those rows contributed a positive
diagnostic component-weighted +1.43 pp Bybit and +0.65 pp Binance. Therefore a
semantic correction may reduce the backtest. It must be a registered strategy
change, not a cleanup silently assumed equivalent.

The completed `daily_prior` versus `hourly_30d` comparison is confounded:

- daily control uses a sum of simple daily returns;
- hourly 30d uses an endpoint price ratio;
- cadence, endpoint, and aggregation semantics all changed together.

The next valid family must isolate one axis at a time:

1. current daily prior sum-of-simple control;
2. daily prior 30-day endpoint return;
3. daily prior compounded/log-return equivalent;
4. daily calendar-month endpoint;
5. only then hourly 30d/exact-month variants with a frozen confirmation or
   hysteresis rule.

Existing evidence still says removing the gate is bad and the sparse 25/30/60
family favors 30d. It does not establish a robust plateau or validate the name
“30-day return.” Exact-month hourly and smart-month arms remain uncompleted.

Conclusion for the owner: the concern is not nitpicky, but changing 30 to
30.4375 would address the smallest part of it.

### P1 — TP12 and several controls are governance overrides, not accepted parameters

The original TP12 promotion was explicitly an operator override against the
two-venue decision rule. The historical receipt at commit `25ffb8f` reports:

| Arm | Bybit MAR delta | Binance MAR delta |
| --- | ---: | ---: |
| TP12 vs TP10 | +1.787 | -3.655 |
| TP15 vs TP10 | +2.228 | -3.448 |

Binance drawdown almost doubled in that lifecycle replay. The same override
disabled the daily risk adjuster even though the receipt described it as
value-adding. A later no-TP comparison favors TP12 over no TP on both venues,
but that does not answer TP12 versus TP10 under the current executable object.

An operator may deliberately choose a Bybit-specific policy. It must be named
as a venue-specific hypothesis and judged on fresh Bybit forward evidence. It
must not be rewritten later as a general two-venue research acceptance.

The BTC-risk thresholds `[0.70, 0.90)` and `0.35x`, with 50-prior warmup, also
lack a surviving dated selection receipt in the current repo. They were added
to live in commit `476c7d0` and then incorporated into the control. The current
baseline can describe their behavior; it cannot retroactively make their
selection independent.

### P1 — inference is weaker than the headline statistics

The engine itself describes the selection arc as heavily multiple-tested and
its impact coefficients as uncalibrated. The internal window has been used for
component selection, weight selection, regime gates, TP/exit work, sizing,
hedges, and many risk overlays. The PBO/DSR results quantify the consequence.

Further limitations:

- no clean OOS window remains;
- the forward sample is tiny and has already been reset after material changes;
- the 1000TAG incident entered the researcher's information set and motivated
  the current tail studies;
- Binance funding is partial in the current control;
- component overlap makes apparent breadth and trade count misleading;
- the active hedge supplies about 2.9 percentage points of each research result
  but is not currently executed as modeled;
- current forward entry cost is capable of consuming most of mean modeled
  expectancy.

The honest prior is “plausible effect needing executable confirmation,” not
“MAR 6 strategy with a few engineering tasks left.”

### P1 — the blacklist plan was administratively closed, not fully falsified

`docs/preregistration/continuous-time-symbol-risk-2026-07-04.md` now says H1,
H2, and H3 produced no deployable improvement. The durable evidence does not
support that broad wording:

- the Bybit forced-time-boundary cells were negative;
- Binance time-boundary work was stopped before a two-venue verdict;
- there are no retained H1/H2/H3 metric tables or artifacts;
- the deleted dispatcher at `c613518^` implemented time rules, local symbol
  rules, train-frozen permanent lists, and disaster cells;
- it did **not** implement the registered H2 hierarchical empirical-Bayes
  entry-time learner at all.

Therefore:

- forced clock exits have negative evidence and should stay closed;
- H1/H3 have no durable positive result and no basis for deployment;
- H2 is untested, not falsified;
- the current receipt should be read as “abandoned/rejected by owner review,”
  not as a completed empirical rejection of every blacklist mechanism.

The plan's best idea is a low-capacity, causal, pre-entry risk learner with
shrinkage and hash/label/delayed-state controls. Its biggest dangers are sparse
buckets, repeated component rows masquerading as observations, within-timestamp
state leakage, nonstationarity, and a huge researcher-degree-of-freedom surface.

Do not implement it now. First make the base executable object identical to the
research object. If revisited with genuinely new forward data:

- use unique `(venue, symbol, signal_ts)` clusters, not component legs, as the
  observation unit;
- batch simultaneous decisions from one prior-state snapshot;
- start with hour-of-week x crowding and perhaps component, not
  symbol x hour-of-week;
- prefer conservative downsizing to hard skips;
- preserve the 24h-delayed, hash-bucket, and label-permutation controls;
- run it as a no-order shadow before any admission change.

Permanent PnL-mined symbol blacklists remain a bad idea. Permanent exclusions
should be limited to objective operational eligibility: delisting state,
contract mechanics, broken market data, insufficient executable depth, or
other facts known before the decision.

The immediate blacklist-related priority is simpler: restore the already
declared `crowd2` admission rule to live parity before inventing a learned
crowding model.

## Research/live parity matrix

| Layer | Research control | Executable demo | Verdict |
| --- | --- | --- | --- |
| Feature panel | shared hourly/rmom logic | shared core logic | mostly aligned |
| Crowding | reject component hour when fresh count >2 | absent | material mismatch |
| Capacity | 25 independently per component | 25 unique symbols, 5 new component legs globally | mismatch |
| Adverse-exit pause | off | 8 component exits / 24h | live-only mismatch |
| Entry fill | exact next-bar reference | market fill after ~149–165s in first clean sample | invalid calibration |
| Entry cost | ~20–23 bps mean round trip | 129.8 bps price drift plus 6.9 bps fee before exit | invalid calibration |
| BTC-risk state | component-selected decision tape | accepted post-cap/execution tape | can diverge |
| BTC-risk failure | multiplier always available in artifact | state error fails to 1.0x | unsafe |
| Hedge cadence | hedge on every daily return row | five-minute target reconciliation | improved; event-time parity still open |
| Hedge model state | current replay history | TP12/stable-only tape, data through 2026-07-09 | current; live realized-return feed still absent |
| Hedge threshold | modeled ratio always represented | no action below $25 | mismatch |
| TP / max hold | 12% / 24h from modeled fill | labelled 12% / 24h, but current effective TP is 10.48%–11.42% and deadline starts before fill | mismatch |
| Stop / heat / DD overlay | off | off | aligned but tail-unsafe |
| Funding | Bybit modeled, Binance partial | account-level forward evidence incomplete | unresolved |

## What Jane Street-grade would mean here

It does not mean a larger feature grid or a more sophisticated blacklist. It
means the following controls are boring, exact, and enforced:

1. **One strategy state machine.** Backtest, paper, and demo use the same
   candidate, capacity, sizing, risk-state, and target-position transition code.
2. **Event-driven executable simulation.** Orders have submit/ack/fill events,
   latency, partial fills, rejection, queue/market semantics, protection delay,
   venue minimums, and calibrated adverse selection.
3. **Portfolio target accounting.** Same-symbol component legs aggregate before
   risk; hedge target and residual beta dollars update with every fill; funding,
   fees, and PnL reconcile to account authority.
4. **Fail-closed risk.** Missing model state, stale beta, stale private state,
   missing PIT membership, corrupt sizing state, or breached latency blocks new
   exposure. A de-risking model never fails to full size.
5. **Tail budgets before return targets.** Single-name, cluster, liquidity-gap,
   and venue-outage losses are bounded ex ante. Historical MAR cannot waive a
   risk limit.
6. **Inference governance.** Every tunable family has a dated hypothesis,
   plateau/falsifier, effective independent sample definition, immutable
   artifacts, and a genuinely untouched forward arbiter.
7. **Operational independence.** Off-box heartbeat, model/data freshness,
   target-versus-actual exposure, and ledger/account drift page without relying
   on a green process exit.

## Ordered remediation gates

### Gate 1 — executable parity

- Implement one shared decision batch and portfolio target kernel.
- Require exact decision/rejection-key agreement and numerical sizing
  equivalence across the full two-venue frozen tape.
- Explicitly encode whether capacity is per component, per unique symbol, or per
  component leg. Do not mix the definitions.
- Make BTC-risk batch scoring order invariant and fail closed.

No new signal or blacklist research before this passes.

### Gate 2 — execution and hedge realism

- Add full order-latency telemetry and an entry-staleness block.
- Calibrate fills from forward data and granular order-book/trade data.
- Rebuild the simulator with the observed event lifecycle.
- Make the hedge event-driven, fresh, monitored, and replayable.
- Report hedged and unhedged return separately; do not let hedge alpha hide weak
  short-book execution.

### Gate 3 — tail survival

- Run the already frozen 0.10%/0.15%/0.25% disaster-budget matrix only after
  the data receipts and parity gates pass.
- Aggregate same-symbol and cluster exposure before live risk acceptance.
- Add a separately registered portfolio heat/admission rule if per-name budgets
  do not control correlated clusters.
- Treat a result that survives only at tail-unsafe size as non-deployable.

### Gate 4 — controlled parameter re-evaluation

- Retest the BTC trend estimand with daily endpoint/compounded/calendar arms
  before any exact-month hourly work.
- Re-evaluate TP10 versus TP12 under the corrected executable simulator and
  current Bybit-specific objective.
- Retain 30d and TP12 as regression controls until a registered replacement
  passes; do not silently edit either.

### Gate 5 — clean forward arbiter

- Freeze code, configs, data contracts, and model hashes.
- Define sample adequacy by unique decision clusters and confidence around
  all-in slippage and net expectancy, not a round number of component trades.
- Require enough calendar/regime coverage to observe clustered losses, not only
  ordinary winners.
- Any material config change resets the forward inference clock.

Only after these gates should H1/H2 entry-risk research be reconsidered.

## Bottom line

The strategy may contain real mean-reversion alpha. The current evidence cannot
separate that alpha cleanly from parameter selection, nonstandard regime
semantics, idealized entry timing, an unequivalent admission path, and a hedge
that contributes materially in research but is absent or delayed live.

The owner's 30-day and delayed-hedge concerns were directionally correct. The
30-versus-30.4375 constant is a minor detail; the return definition, state
machine, execution delay, and hedge lifecycle are major defects.

Do not spend the next research dollar on a smarter blacklist. Spend it on exact
parity, latency, hedge target control, and ex-ante tail budgets. If the edge
survives those, it is worth building. If it does not, the current MAR was never
tradable alpha.

## Reproduction anchors

Current artifacts used:

- `research/btc_month_regime_2026-07-04/continuous/control_daily_prior/`
- `data/bybit-continuous-demo-event/reports/continuous_paper_demo_reconciliation/`
- `deploy/hedge_warmstart/bybit_warmstart.csv`
- `docs/research_summary.md`
- `docs/lookback_audit.md`
- `docs/preregistration/continuous-time-symbol-risk-2026-07-04.md`
- `docs/preregistration/continuous-tail-survival-2026-07-10.md`

Historical receipts inspected through git:

```bash
git show 25ffb8f:docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md
git show 25ffb8f:docs/preregistration/2026-06-19-continuous-v2-f2-exit-tp-lifecycle-verdict.md
git show 24c0eaf^:docs/preregistration/2026-06-28-continuous-fade-dsr-pbo.md
git show c613518^:scripts/continuous_time_symbol_risk_2026_07_04.py
```

All extra calculations in this critique are diagnostics over frozen artifacts;
no new backtest cell was run and no result is labelled candidate evidence.
