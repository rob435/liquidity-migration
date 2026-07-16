# Benchmark Refresh 2026-07-16

## Prospective contract

- **ID / owner / registered time / study mode:**
  `benchmark-refresh-2026-07-16`; repository owner with Codex operator;
  2026-07-16 21:00:53 UTC; exploratory descriptive benchmark refresh.
- **Claim and intended decision:** reconstruct the current code-defined LONG and
  CONTINUOUS profiles on freshly rebuilt canonical Bybit and Binance roots so
  the repository has current comparison baselines. No parameter, promotion,
  deployment, or capital decision is authorized by this run.
- **Prior exposure and untouched surface:** historical strategy windows and the
  prior roots through 2026-07-09/10 are already heavily exposed. During this
  refresh, stale-root results and two replay integration failures were already
  inspected. The 2026-07-10 through 2026-07-15 tail has not been inspected in
  this task, but it is not declared statistically untouched or OOS. Every
  result is descriptive/exploratory.
- **Venue / population / roots / window:** Bybit USDT linear perpetuals at
  `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit` and Binance USD-M USDT
  perpetuals at `/Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit`;
  benchmark window `[2023-07-16, 2026-07-16)`. The end is the latest fully
  completed UTC day at registration. Builders request their full canonical
  histories through the same end-exclusive boundary. Any unavailable tail must
  be reported as missing; it must not be converted to zero or silently clipped.
- **Sample unit / horizon / stopping rule:** strategy trades and daily portfolio
  equity over the fixed three-year window. Run exactly once per registered
  sleeve/venue cell, aside from deterministic retries after an implementation,
  data, or infrastructure failure that is retained in the outcome log.
- **Control and complete tested set:** four cells only: LONG and CONTINUOUS at
  1x modeled exposure on Bybit and Binance. CONTINUOUS presentation-only extra
  leverage is disabled. No thresholds, strategies, gates, windows, symbols, or
  cost settings may be selected from the observed results.
- **Primary read / guardrails / inconclusive outcome:** record total return,
  maximum drawdown, trade count, Sharpe-like statistic, CONTINUOUS MAR, PIT
  label, warnings, funding coverage, realized date range, and artifact
  completeness. There is no pass threshold. A missing/tainted PIT gate,
  incomplete material funding, unreconciled accounting, failed cell, or clipped
  tail makes the affected performance read limited or invalid, never a pass.
- **Validity assumptions and required artifacts:** causal current code paths;
  manifest-backed membership where the runner establishes it; explicit
  end-exclusive timing; current fee/funding model and capacity settings;
  historical-account journal acceptance; trade, equity, monthly, report, JSON,
  component, hedge, command, warning, and hash evidence sufficient to
  reconstruct each cell. CONTINUOUS remains population-limited unless separate
  manifest evidence establishes its historical universe.
- **Code / config / data identities:** Git
  `be6367fdd8863c04d6c1aa303983c4043cedb12f`; cost config SHA-256
  `e5d28c0f1fd4c42db159fdf2fe4786ff087140c52e6c782abc385e67419b48be`;
  Bybit builder SHA-256
  `a6b3e5c7c373fbb7585a2b4d8154c217e1f97653a6e3a2bc99dc9a50603ce5a4`;
  Binance builder SHA-256
  `91de50c61c736cea4135fd7250e5be26ff4e309db1fbad53ceef1e1676a149a9`.
  Post-build root coverage and artifact hashes will be appended before result
  interpretation.
- **Expected artifact paths:** each root's
  `reports/equity_curves/{long,continuous}/`, plus the outcome log below and the
  compact interpretation in `docs/research_summary.md`.
- **Permitted deviations before exposure:** reduce worker counts or retry the
  same exact command after a rate limit, lock, process, or storage failure;
  repair a deterministic implementation/data-schema defect without changing
  strategy semantics, then rerun all affected cells. Preserve the failed
  command and explain the repair. No degraded PIT override is permitted.
- **Explicit non-conclusions:** not confirmation, untouched OOS evidence,
  runtime parity, execution calibration, promotion, sizing authority, mainnet
  readiness, or real-money authorization. Cross-venue agreement is correlated
  robustness evidence, not independence.

## Outcome log

Not exposed under this contract at registration time.
