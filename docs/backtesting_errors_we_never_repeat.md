# Research Failure-Mode Reference

This historical filename is retained because code, tests, and old receipts link
to it. The governing evidence policy is `docs/governance.md`. This file is a
review taxonomy: use it to find ways a result can be wrong, not as a universal
recipe or a source of permanent metric thresholds.

The first eleven items were originally prompted by a 2022 public thread by
macrocephalopod; the rest were accumulated from this repository's own failures.
Their value comes from the failure mechanisms, not from the age or authority of
their source.

## Failure Taxonomy

1. **Future universe selection.** Selecting current winners, liquidity leaders,
   survivors, or today's listings and projecting them backward creates
   survivorship and look-ahead bias. Use point-in-time membership when the claim
   depends on a historical universe; otherwise scope the claim explicitly.

2. **Future information in signals.** A feature may use only information
   available at the decision. Audit close-time conventions, vendor publication,
   revisions, joins, rankings, and processing latency.

3. **Instantaneous trading.** Observing a price and filling at that same price is
   usually impossible. Separate signal formation, order submission, and the fill
   window unless venue mechanics prove otherwise.

4. **Revised or non-point-in-time data.** Corrected vendor, settlement,
   fundamental, or alternative data may differ from what was known. Use
   bitemporal/PIT sources when revisions can affect the decision.

5. **Ignoring capacity.** A result that needs implausible ADV, open interest,
   book depth, or participation is not executable at the reported scale.

6. **Ignoring fees.** Maker/taker mix, rebates, VIP tier, routing, and exchange
   fees must match the claimed execution or be stressed conservatively.

7. **Ignoring slippage.** Midpoint, close, or VWAP fills need justification.
   Spread and slippage should vary with liquidity, side, urgency, and size.

8. **Ignoring market impact.** Larger baskets need a participation or impact
   model; flat basis points are not automatically scale-valid.

9. **Borrow or short-access fantasy.** A short signal is not executable unless
   the instrument and venue support the exposure at the time.

10. **Borrow, funding, or carry fantasy.** Path-dependent financing can erase an
    edge. Missing material carry limits a net-performance claim.

11. **Trading bans and venue restrictions.** Halts, reduce-only states, margin
    changes, short restrictions, and maintenance can change feasible actions.

12. **Instrument lifecycle mistakes.** Launches, delistings, migrations, status,
    tick/lot changes, prelisting periods, and renames must be handled at the time
    they occurred. A manifest based partly on current listings must disclose that
    inference rather than calling it observed history.

13. **Timestamp and resampling leakage.** Audit candle stamps, availability
    stamps, timezone/session boundaries, rolling windows, joins, ranks, and
    provisional rows. A timestamp alone does not prove availability.

14. **Impossible OHLC ordering.** A bar does not reveal the path between high and
    low. If stop and target both touch, use finer data, a conservative ordering,
    or report the ambiguity.

15. **Warm-started state.** Trailing exits, MFE/MAE, basket memory, cooldowns,
    high-water marks, and kill logic must begin when the live process could have
    known them.

16. **Backtest/forward lifecycle mismatch.** Shared functions do not prove
    equivalent behavior. Compare decision time, state, capacity, order timing,
    partial fills, netting, rejection, and exit handling.

17. **Parameter mining.** Searching until a curve looks good without disclosing
    the tested set or freezing selection before evaluation overstates evidence.

18. **Out-of-sample reuse.** Once a window influences features, thresholds,
    timing, universe, or stopping, it is spent. Renaming it OOS does not restore
    independence.

19. **Multiple-testing denial.** Symbols, variants, windows, metrics, and repeated
    peeks create effective trials. Report the search surface, dependence, and an
    appropriate multiplicity or shrinkage treatment.

20. **Bad accounting.** Cash, equity, leverage, margin, liquidation, realized and
    unrealized PnL, compounding, fees, funding, flips, and netting must reconcile
    at the granularity needed by the claim.

21. **Hidden common risk.** Many trades can be one correlated market, cluster, or
    factor bet. Report concentration, synchronized loss, and relevant attribution.

22. **Venue-mechanics fantasy.** Minimum notional, reduce-only behavior, partial
    fills, rejections, stale orders, WebSocket gaps, rate limits, and missed
    cycles belong in an execution claim.

23. **Pretty-report bias.** A chart without data/code/config identity, tested-set
    disclosure, and reconstructable source artifacts is presentation, not
    decision evidence.

24. **Unreconciled forward drift.** Paper/demo/mainnet behavior must be compared
    with the intended model: expected orders, submissions, fills, misses,
    slippage, costs, exits, and PnL. Unexplained drift limits both execution and
    performance conclusions.

25. **All-or-nothing compute.** Large jobs should checkpoint or stage when a
    failure would erase hours of evidence. An aborted broad run must remain
    visible; a narrowed follow-up cannot be portrayed as the original test.

26. **Optional stopping and forward peeking.** Watching a forward epoch until it
    looks convincing, resetting after incidents, or adapting without retaining
    prior epochs converts prospective evidence into selection data. Freeze a
    horizon/event count or use a registered sequential rule.

27. **Safety/alpha conflation.** A capital-preservation constraint need not
    improve a backtest metric. Conversely, an alpha-improving rule is not
    automatically a valid catastrophe control.

28. **Administrative truth.** Names such as promoted, frozen, approved, or closed
    can anchor judgment. Inspect the evidence and implemented behavior instead.

29. **Pseudoreplication.** Nested components, repeated legs, or several rows from
    one decision are not independent observations. Declare the unique decision,
    simultaneous wave, and calendar/cluster unit before computing uncertainty.

30. **Missing-as-zero diagnostics.** Unmeasured MAE, MFE, cost, funding, or path
    values are missing evidence, not favorable zeros. Preserve null provenance
    and exclude or bound it according to the claim.

31. **Post-filter observability.** A candidate tape emitted only after major
    gates cannot audit the claimed source population or gate attrition. Capture
    the pre-gate population with first rejection and missingness.

32. **Capture/materialization drift.** Discovery objects, terminal raw keys,
    materialized partition keys, and independently replayed expected keys must
    agree at the grain used by the claim. A mismatch invalidates population
    completeness until explained and repaired.

33. **Verification-system displacement.** Receipt, schema, and provenance
    machinery can be internally rigorous while never reaching the decision it
    was built to support. Timebox infrastructure, require claim-bearing
    milestones, and retire unused machinery rather than treating artifact
    volume as progress.

## Applying The Taxonomy

The relevant checks depend on the claim:

- A feature-timestamp audit needs causal data and reconstructable event rows; it
  does not need a portfolio funding model.
- A historical cross-sectional performance claim needs PIT population control,
  executable fills, material costs, capacity, and ledger accounting.
- An entry-agreement reconciliation can omit PnL costs, but it cannot become an
  alpha claim.
- A venue-specific mechanism need not pass another venue; a portability claim
  does.
- An exploratory current-universe benchmark may be retained for comparison when
  clearly scoped and never relabelled as historical-universe evidence.

For the active repository:

- Treat daily closes and synthetic current bars according to their actual
  availability and executable order window.
- Verify the precise manifest provenance in `docs/pit_gate.md`; archive-observed
  membership and current-listing-derived tail coverage are not the same fact.
- Treat demo/paper reconciliation as execution evidence. It is not automatic
  alpha proof, OOS proof, or deployment authorization.
- Inspect funding and alternative-data coverage per run. Root names and old
  coverage receipts do not establish current completeness.
- Preserve every forward epoch and change point even when operational ledgers are
  reset; otherwise later evidence is selected on survival.

Use the evidence card and validity rules in `docs/governance.md` to decide what a
specific artifact can support.
