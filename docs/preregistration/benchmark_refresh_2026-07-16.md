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

### Pre-outcome tooling amendment — 2026-07-16 21:45:31 UTC

Before inspecting any refreshed-tail strategy result, the owner requested one
repeatable data/feature/backtest workflow with append-first routine operation.
The registered four-cell run remains in `canonical` data mode: both full-PIT
builders are invoked over their original full-history boundaries, no degraded
manifest override is permitted, every selected dataset must reach 2026-07-15,
and the benchmark window, profiles, costs, leverage, cells, and interpretation
rule above are unchanged. The new tail mode is for separately labelled routine
refreshes and is not evidence for this registered full rebuild.

The run may use a descendant of the registered code commit whose intervening
diff is limited to the orchestration/reconciliation utility, its documentation
and tests, and making the existing equity wrapper return nonzero when any
requested sleeve fails. Those changes do not alter strategy, cost, feature,
data-builder, or numerical backtest semantics. The exact clean descendant and
artifact hashes will be captured by the immutable run manifest. The full local
gate passed before this amendment: 1,944 tests passed, one skipped, with Ruff
and package-wide mypy clean.

One canonical Bybit attempt was interrupted after manifest/kline work and
before ancillary completion. Its partial data state is retained and will be
resumed/revalidated by the exact canonical command; no refreshed-tail strategy
performance was inspected from that attempt.

### Pre-outcome PIT-incarnation amendment — 2026-07-16 22:00:32 UTC

The first automated canonical attempt failed before ancillary refresh or any
backtest cell because the strict manifest/kline validator reported 12 missing
symbol-days. Diagnosis against the persisted partitions and the public v5
`instruments-info` response established that `DATAUSDT` and `KORUUSDT` are
reused tickers: each has an older traded incarnation and a new 2026 listing.
The existing validator collapses all rows for a symbol into one first/last
kline span, so empty post-delisting and pre-relisting days are incorrectly
classified as missing mid-incarnation data.

Before any refreshed-tail strategy result is inspected, the deterministic
repair is registered as follows: persist the observed v5 `launchTime` as
listing-incarnation metadata; split required manifest/kline coverage at those
observed incarnation starts; and derive each segment's lower and upper traded
bounds only from klines inside that segment. A genuinely missing day inside a
segment must continue to fail, and independently observed active-listing tail
membership must continue to prevent an incomplete tail from self-passing.
Tests must pin both properties. The duplicate request-window defect noticed in
the same downloader inspection may be removed as a non-numerical efficiency
fix.

This changes PIT validation/data provenance only. It does not change the
registered venues, window, four cells, strategies, features, costs, leverage,
or interpretation rule. The failed attempt remains in the append-only run log,
and the exact canonical command will be retried after the repair passes the
repository gate.

### Pre-outcome feature-regeneration amendment — 2026-07-16 22:35:48 UTC

The repaired canonical run completed and validated the Bybit data root, then
stopped before any backtest because the checked residual-momentum append found
that the existing stable overlap no longer reproduced (`max_abs_diff =
0.00164931328375`). Inspection showed that the stored artifact covered 847
symbols, while the refreshed manifest and klines cover 916, and ended on
2026-07-11. It is therefore not safe to preserve that artifact as the feature
identity for a current canonical benchmark.

Canonical data mode will now invoke the existing atomic `--full-rewrite`
feature path from the fixed registered start. This is prospective and changes
no residual-momentum formula, causal shift, factor definition, strategy,
window, cost, leverage, cell, or interpretation rule. Routine `tail` mode keeps
the checked overlap append and must continue to fail rather than overwrite when
stable values move. The failed append and completed Bybit data receipt remain
append-only evidence; no refreshed-tail performance has been inspected.

### Pre-outcome report-isolation amendment — 2026-07-16 23:09:54 UTC

The first canonical-feature retry reached the Bybit LONG cell but failed before
writing a performance report because the standard runner reused
`<data-root>/reports/equity_curves`. That directory contained a prior
historical account event tape ending on 2026-07-07; replaying the refreshed
fixed window into that append-only tape correctly failed with `strategy event
clock cannot move backward`. A diagnostic replay into a new temporary report
directory completed, establishing that this is derived-artifact contamination,
not a strategy-clock or numerical failure. No metrics from the diagnostic run
were inspected or accepted.

Before any refreshed-tail result is inspected, derived backtest artifacts will
be isolated under the immutable refresh run directory and further separated by
venue. Each sleeve command must start from a clean sleeve directory inside that
run so a deterministic retry cannot append a new replay to a partial account
journal. The append-only run ledger and logs retain every failed attempt; raw
market data remains append-first where supported, and canonical residual
momentum remains an atomic full rewrite as already registered. This changes no
strategy, feature formula, data, cost, leverage, window, cell, or
interpretation rule.
