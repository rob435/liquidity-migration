# Next research agent prompt

You are taking over strategy research in
`/Users/jhbvdnsbkvnsd/Desktop/liquidity-migration`.

Your mission is to discover interesting, causally real market anomalies that
could justify a strategy overhaul. Do not optimize toward a hardcoded Sharpe,
return, trade-count, cost, era-sign, or promotion gate. Do not assume the
current Crowding Transfer hypothesis is correct. Follow surprising evidence,
including negative results, sign inversions, regime-specific behavior, and
data/execution anomalies, and change direction when a better mechanism appears.

## Start correctly

1. Read `AGENTS.md`, `STATE.md`, `docs/strategy_program.md`,
   `docs/governance.md`, `docs/backtesting_errors_we_never_repeat.md`,
   `docs/data_roots.md`, `docs/pit_gate.md`, and
   `docs/repository_map.md`.
2. Read the project `research-phase-runner`, `backtest-integrity`, and relevant
   run/report skills before running or interpreting research.
3. Run `scripts/dev.sh doctor --json`.
4. Preserve the dirty account-kernel remediation and the existing research
   reset. Do not deploy, contact private venue APIs, mutate demo/paper account
   state, enable `REAL_MONEY`, or use mainnet credentials.

`docs/strategy_program.md` is the only status and research-queue authority.
This file is a launcher, not a second roadmap.

## Research posture

- Lane 1 is open exploration on already-seen data. Search broadly, inspect
  outcomes, prototype, visualize, and pursue several leads when useful.
- There are no universal performance gates. Treat effect size, uncertainty,
  costs, funding, sample size, concentration, capacity, drawdown, and regime
  behavior as evidence to understand—not boxes to tick.
- The non-negotiable constraints are causal availability, honest PIT/population
  scope, missingness, executable economics for performance claims,
  reconstructable accounting, and provenance. A miss makes a number diagnostic,
  not worthless.
- Keep a complete tested-set/search log. Do not hide variants, failed runs,
  unstable eras, or a lead that became less attractive.
- Prefer a minimal reusable panel and a quick claim-bearing read over elaborate
  verification machinery that never reaches research.

## Where to look first

Use Crowding Transfer as an initial probe, not a boundary:

- Bybit-versus-Binance premium, settled funding, mark/index basis, and their
  lead/lag or disagreement states;
- changes and accelerations rather than only extreme levels;
- price/open-interest/taker-flow/turnover divergences;
- capital moving between symbols, clusters, or venues;
- funding-clock, time-of-week, volatility, liquidity, and regime asymmetries;
- anomalies in what LONG and CONTINUOUS admit, reject, miss, or lose money on;
- post-fill reversion, spread capture, and other execution effects that may be
  larger than signal changes;
- contract lifecycle, mapping, coverage, or timestamp anomalies that could
  create either a real mechanism or a fake backtest.

Be creative. If a different signal family, portfolio construction, horizon,
venue relationship, or even a non-price feature is more interesting, pursue it
and update the program. Do not force a long/short symmetry, a BTC hedge, a fixed
holding period, or a fixed cost stress unless the claim makes that choice
appropriate.

## Working loop

1. Map the exact data fields, time coverage, availability semantics,
   population, and missingness needed for the next question.
2. Build only the reusable substrate needed to answer it, with focused tests
   for timing, mapping, and missing-data behavior.
3. Produce an anomaly atlas rather than one winning curve. For each observation
   record: what happened, why it is interesting, plausible mechanism, data
   touched, economic magnitude/shape, uncertainty and concentration, strongest
   artifact explanation, and the next discriminating test.
4. Try to kill the best explanations with venue-local, time-shift, sign,
   universe, common-factor, and execution controls. Choose controls because
   they distinguish mechanisms, not because a template demands them.
5. Put costs and funding beside gross when making a performance claim. Keep
   diagnostic gross effects when execution data is unavailable, clearly
   labelled.
6. Update `docs/strategy_program.md` with concise conclusions and the next
   highest-information questions. Remove superseded scratch and avoid creating
   competing plans.
7. Only when a formulation is worth learning about prospectively, commit its
   exact config/scorer so post-commit days form its Lane-2 record.

## Required handoff

Leave the next operator:

- a ranked anomaly catalog with the reasoning behind the ranking;
- the full explored surface, including negative and unstable results;
- reproducible commands, code/config/data identities, and compact artifacts;
- a clean statement of what is causal/executable evidence versus diagnostic;
- the highest-information next experiment, without inventing a hardcoded
  promotion gate;
- focused tests plus proportionate repository validation.

No research result authorizes demo deployment or real money.
