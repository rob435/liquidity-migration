# Backtesting Errors We Never Repeat

This is a mandatory standard for every serious research run in this repo.

The rule is simple: a backtest is evidence only when every decision can be
reconstructed from data, state, and venue rules that existed at the decision
time, and every fill is executable under an explicit cost and execution model.
Anything else is not alpha. It is a bug report.

## Source Thread

The starting point is macrocephalopod's December 2022 thread:
[announcement](https://x.com/macrocephalopod/status/1598823745681903616).
It announced 24 days of backtest errors. Publicly accessible X mirrors and the
FxTwitter API verified Days 1-11 only; searches for Days 12-24 returned no
source posts. We do not pretend the missing posts exist. Rules 1-11 below are
the verified thread items, paraphrased for this repo. Rules 12+ are our house
extensions.

Verified source posts:

| Day | Error | Source |
| --- | --- | --- |
| 1 | Look-ahead when picking the trading universe | [post](https://x.com/macrocephalopod/status/1598823749091893248) |
| 2 | Look-ahead in signals | [post](https://x.com/macrocephalopod/status/1598968013364994049) |
| 3 | Instantaneous trading | [post](https://x.com/macrocephalopod/status/1599071391973711872) |
| 4 | Backtesting with revised data | [post](https://x.com/macrocephalopod/status/1599370609015693312) |
| 5 | Ignoring capacity constraints | [post](https://x.com/macrocephalopod/status/1599743759448952838) |
| 6 | Trading fees | [post](https://x.com/macrocephalopod/status/1600272020788633601) |
| 7 | Slippage | [post](https://x.com/macrocephalopod/status/1601134583377850368) |
| 8 | Market impact | [post](https://x.com/macrocephalopod/status/1601582948540887040) |
| 9 | Borrow availability | [post](https://x.com/macrocephalopod/status/1601893435765673984) |
| 10 | Borrow fees | [post](https://x.com/macrocephalopod/status/1601896256103743488) |
| 11 | Short bans | [post](https://x.com/macrocephalopod/status/1602282017646907392) |

## Non-Negotiable Gates

If any gate fails, the run must be labelled `invalid` or `exploratory`; it must
not be called proof, candidate alpha, promotion evidence, or real-money support.

- Every run must declare `decision_ts`, `data_available_ts`, `order_submit_ts`,
  `fill_window`, `exit_activation_ts`, and `state_initialization_ts` for the
  strategy being tested.
- Every feature must be causal at `decision_ts`. A delayed copy of the feature
  must also be tested when data latency is uncertain.
- Every universe must be point-in-time, or the run must carry an explicit
  `current_universe_biased` label.
- Every cost model must include venue fees, expected aggressive/passive mix,
  funding or carry, spread/slippage, and capacity limits.
- Every adaptive exit, trailing stop, basket stop, cooldown, and protection
  state must have tests proving the state starts when the live system would
  first know it.
- Every result must include a trade ledger, equity curve, split metrics,
  drawdown, worst-day loss, parameter/config hash, data-root identity, and a
  research-log entry.
- Expensive full-listing grids must either checkpoint partial results or be
  explicitly staged. A multi-hour all-or-nothing grid with no progress artifact
  is a backtesting-system defect, not a proof standard.
- Any strange synchronization, such as most positions exiting in the same
  minute, is a stop-work anomaly until explained by code and market data.

## Verified And House Errors

1. **Future universe selection.** Never select today's winners, current top
   market-cap symbols, current liquidity leaders, or instruments that survived
   to the end of the dataset and then pretend they were tradable historically.
   Use rolling point-in-time membership. If unavailable, label the run biased.

2. **Future information in signals.** A signal can use only information
   available before the decision. Watch close-time mismatches, delayed external
   data, vendor availability changes, alternative-data history, and stochastic
   latency.

3. **Instantaneous trading.** Do not observe a price and fill at that same
   price unless the venue mechanics genuinely allow it. Close-to-close mean
   reversion backtests are especially dangerous. Signal formation and execution
   must be separated by time and fill assumptions.

4. **Revised or non-point-in-time data.** Final macro, fundamental, settlement,
   analyst, and vendor-corrected values are often not the values that existed at
   the time. Use point-in-time or bitemporal data when revisions matter.

5. **Ignoring capacity.** A strategy that needs multiples of ADV, open interest,
   order-book depth, or realistic quote turnover is not executable at the
   reported size. Capacity must be measured per symbol and per portfolio.

6. **Ignoring trading fees.** Fees are certain even when alpha is not. Maker,
   taker, broker, exchange, VIP-tier, rebate, and route assumptions must match
   the actual execution model or be stress tested.

7. **Ignoring slippage.** A small order normally pays spread. A larger order
   pays more. Backtests must not assume VWAP or midpoint fills without a
   conservative slippage model and a reconciliation plan.

8. **Ignoring market impact.** Slippage depends on order size, urgency, venue
   liquidity, and participation. Larger crypto baskets need participation or
   square-root impact stress tests, not flat bps fantasy fills.

9. **Borrow availability fantasy.** Short-side alpha is often strongest exactly
   where borrow is unavailable. Any short strategy must prove the instrument can
   be borrowed or use a venue where short exposure is actually available.

10. **Borrow fee fantasy.** Hard-to-borrow exposure can erase the edge. Borrow,
    funding, and carry must be path-dependent costs, not a single friendly
    constant.

11. **Trading bans and venue restrictions.** Exchanges and regulators can ban
    new shorts, force reductions, halt instruments, or change margin rules.
    Backtests must model known restrictions or explicitly mark them out of
    scope.

12. **Instrument lifecycle mistakes.** Delistings, launches, migrations,
    contract-status changes, prelisting periods, tick-size changes, lot-size
    changes, and symbol renames must be point-in-time. Missing dead instruments
    is survivorship bias.

13. **Timestamp and resampling leakage.** Joins, candles, rolling windows,
    rankings, daily bars, timezone conversions, and session boundaries must be
    audited. A feature timestamp is not enough; the data availability timestamp
    is what matters.

14. **Impossible OHLC ordering.** A candle high, low, open, and close do not
    reveal the intrabar path. If stop and target both touch in the same bar, the
    backtest must use a conservative rule or finer data.

15. **Warm-started state.** Adaptive exits, profit protection, trailing stops,
    rolling highs/lows, MFE/MAE, basket state, cooldowns, and kill logic must
    start from the same state the live system would have at activation. The
    2026-05-08 close-fade profit-protection audit exists because we violated
    this rule.

16. **Same-code illusion.** A backtest function that cannot be mapped to the
    forward/demo order lifecycle is not proof. Backtest, paper, and demo must
    agree on decision time, order timing, fill accounting, and exit state.

17. **Parameter mining.** Sweeping until a curve looks good is not discovery
    unless the selection rule existed before the validation window. Every grid
    must report the full distribution, not just the winner.

18. **Out-of-sample reuse.** Once an OOS window influences thresholds, filters,
    timing, universe rules, or exit logic, it is no longer OOS. Promote only
    after a fresh untouched window or live paper evidence.

19. **Multiple-testing denial.** Hundreds of symbols, filters, time windows,
    exits, and costs create false positives. A good single run means little
    without split stability, sensitivity analysis, and a clear prior.

20. **Bad accounting.** Equity, cash, leverage, margin, liquidation, realized
    PnL, unrealized PnL, compounding, fees, funding, and position flips must
    reconcile at the trade-ledger level.

21. **Hidden common risk.** Many trades in one basket can be one macro or market
    beta bet. Report basket-level returns, event clustering, sleeve attribution,
    market excess return, and worst synchronized exit behavior.

22. **Venue mechanics fantasy.** Minimum notional, reduce-only behavior, partial
    fills, rejected orders, stale orders, maintenance windows, WebSocket gaps,
    rate limits, and missed slices are part of execution.

23. **Pretty-report bias.** A backtest without a saved config, data identity,
    code version, trade ledger, split report, and research run record is not
    evidence. It is a screenshot.

24. **Unreconciled live drift.** Paper/demo/live results must be reconciled to
    the sim: expected orders versus submitted orders, fills, missed fills,
    slippage, fees, funding, exits, and PnL. If the drift is unexplained, the
    backtest is not trusted.

25. **All-or-nothing compute.** Large research jobs need bounded stages,
    checkpointed rows, or resume support. If an exhaustive grid can consume
    hours and leave no partial result, it is operationally unsafe research
    infrastructure. Narrowing a follow-up sweep is acceptable only when the
    aborted broad run is documented as aborted and not cited as evidence.

## Repo Application

For the current liquidity-migration strategy:

- Signal features must use only data known at the daily signal close.
- Entry is delayed by the configured 1h delay; the signal close is not an
  executable fill.
- Current-top Bybit universes are benchmarks only and must carry
  `current_universe_biased`.
- Promotion requires archive-derived point-in-time symbol/date membership,
  including delisted instruments and historical status changes.
- `volume-events` must require full PIT universe coverage by default. Use
  `--allow-partial-pit` only for explicitly biased diagnostics.
- Liquidity rank, rank improvement, turnover expansion, and event-rank filters
  must use prior/current data available at the event decision time.
- Event-decay exits, max hold, cooldown, max-active, and stop-pressure state
  must be initialized exactly as a forward/demo executor would initialize them.
- Funding is still a known gap when the data root has no funding dataset. Mark
  those runs as fee/slippage stressed but funding-missing.
- Report promotion tests by full distribution and split stability, not by the
  best isolated parameter set.

For retired paths:

- The fixed daily-close short fade is no longer an active strategy.
- Old daily-close profit-protection test results must not be cited as
  promotion evidence for the current event-driven system.
- Fixed-day volume rebalance grids are legacy benchmarks only, not the strategy
  path.

For paper/demo:

- Telegram may notify, but it must not approve or submit orders.
- Demo execution is execution evidence only. It is not alpha proof.
- Forward audit must reconcile expected event orders to actual orders, fills,
  misses, slippage, fees, funding, and exit reasons.

## Required Run Labels

- `invalid`: known bug, leakage, impossible fill, missing costs, or broken
  accounting.
- `exploratory`: useful research sketch, but missing one or more proof gates.
- `biased_benchmark`: intentionally biased or incomplete benchmark retained for
  comparison, never promotion.
- `candidate`: point-in-time, costed, split-stable, ledger-backed, and not tuned
  on the promotion window.
- `paper_ready`: candidate plus a paper/demo execution plan that matches the
  backtest lifecycle.

## Before Trusting Any Backtest

Answer these before looking at total return:

1. What exact data existed at the decision timestamp?
2. Which assets were tradable at that timestamp, and how do we know?
3. What order would have been submitted, when, and at what size?
4. What fill model was used, and how was it costed?
5. What state existed before each exit condition became active?
6. What would make this result disappear?
7. Which untouched window or forward evidence is still clean?
8. Where is the trade ledger and run record?

If any answer is weak, the backtest is not ready to influence real-money
decisions.
