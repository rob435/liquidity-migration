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

### Pre-outcome Binance current-month amendment — 2026-07-17 00:10:25 UTC

The isolated run completed both Bybit cells, then failed closed before any
Binance feature or performance result because monthly archive discovery found
790 symbols versus 804 persisted kline symbols. All 17 names classified as
dropped are present in Binance's public USD-M daily archive inventory, and all
of their persisted bars are confined to July 2026. They are current-month
daily-only contracts whose monthly ZIPs do not yet exist. The canonical script
intends to rebuild the monthly pair and append the current daily month, but the
monthly universe-shrink guard aborts before that second stage.

Before any Binance result is inspected, the deterministic repair is registered
as follows: build monthly history and the explicitly bounded current-month
daily tail inside one staging generation; derive membership from the combined
persisted kline coverage; verify uniqueness, provenance, row counts, and the
prior-universe non-shrink condition against the combined monthly-plus-daily
inventory; and publish the kline/manifest pair atomically. Monthly and daily
download failures retain the existing strict zero-tolerance setting for this
run. No `allow_degraded` override is permitted. This changes acquisition and
publication ordering only, not data values, strategy logic, features, costs,
leverage, window, cells, or interpretation.

### Outcome-exposed LONG diagnostics amendment — 2026-07-17 00:17:19 UTC

After Bybit LONG completed, its headline metrics were observed together with a
`WINDOW_CLIPPED_END` warning. Source inspection established that the warning is
a reporting off-by-one: LONG passes the end-exclusive boundary `2026-07-16` to
a generic diagnostic that compares it with the latest inclusive kline date
`2026-07-15`. The canonical root and run both contain exactly the registered
completed-day tail; the warning does not arise from a missing partition.

The repair will convert LONG's non-empty exclusive end to its preceding
inclusive data date only at the diagnostic call boundary. The generic
diagnostic's inclusive-date contract and the strategy's end-exclusive filter
remain unchanged. Tests must prove that `[start, 2026-07-16)` does not warn when
data reaches 2026-07-15 and still warns when data ends earlier. Because Bybit
LONG performance is already exposed, this is not a prospective evidentiary
reset and the observed metrics remain descriptive. Both LONG cells will be
rerun solely to produce truthful warning metadata and fresh artifact hashes;
strategy decisions and numerical outputs must match the pre-fix cells.

### Descriptive outcome — completed 2026-07-17 02:27:52 UTC

All four registered cells completed over `[2023-07-16, 2026-07-16)` at 1x.
The final Binance and corrected Bybit LONG runs used clean commit
`f84dde629fccc5ca3a51dce2df2e81ac5d99318d`. Bybit CONTINUOUS came from the
isolated clean run at `3d492e4`; the intervening source changes affect LONG
warning diagnostics and Binance acquisition/publication, not CONTINUOUS
strategy or numerical code. The retained run manifests make that split
explicit rather than presenting the cells as one atomic execution.

Canonical data gates:

- Bybit: 916 manifest and kline symbols, 591,596 required symbol-days, zero
  missing symbols or required days, `full_pit_universe_pass=true`, latest
  manifest/kline partition 2026-07-15.
- Binance: 19,749 monthly files plus 12,180 current-month daily checks staged
  together; 14,385,584 hourly rows, 812 symbols, 599,001 manifest symbol-days,
  zero failed monthly or daily files, latest manifest/kline partition
  2026-07-15. The 547 daily 404s were absent archive objects, not failed
  downloads, and were reconciled before atomic publication.

Registered cell results:

| Cell | Trades | Total return | Annualized | Max drawdown | Sharpe-like | MAR | Funding / warning status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Bybit LONG | 183 | +35.3783% | n/a | -3.2739% | 2.2217 | n/a | modeled 100%; `full_pit_universe`; no warnings |
| Binance LONG | 183 | +29.3684% | n/a | -3.0682% | 1.6649 | n/a | modeled 100%; `full_pit_universe`; no warnings |
| Bybit CONTINUOUS | 786 / 737 / 656 | +21.06% | +6.58% | -1.30% | 2.73 | 5.41 | all components modeled |
| Binance CONTINUOUS | 768 / 703 / 611 | +17.35% | +5.48% | -1.41% | 2.46 | 4.10 | all components partial |

The CONTINUOUS trade counts are component counts in
`turn3p3/turn4p3/turn4p5` order, not additive portfolio trades. Worst daily
returns were -0.93% on Bybit and -0.58% on Binance. Binance's `partial` funding
label is caused by the same `2026-03-23-s-HOOKUSDT` trade in all three
components; the remaining modeled counts are 767/768, 702/703, and 610/611.
The report does not expose a funding-event or notional modeled fraction, so the
Binance CONTINUOUS net-return read remains limited even though the partial
scope is narrow.

The post-exposure Bybit LONG diagnostic rerun removed only the false
`WINDOW_CLIPPED_END` warning. Its strategy event tape, baskets, trade, equity,
MTM equity, and monthly CSV SHA-256 values are byte-identical to the pre-fix
run. The JSON's `realized_gross_mean` changed from
`0.05360281970870105` to `0.05360281970870106` (~1e-17); paths and warning
metadata changed as intended. No strategy decision or material numeric output
changed.

Primary artifact identities:

- corrected Bybit LONG report SHA-256:
  `8ed9a804c27552f499150955f7058ae4b2a16d0c88a43e583d90c0fc6650277f`;
  run summary SHA-256:
  `9cdc2c131f343cf05a4497a3cf516b46ff13ed9695ceab7abb4246127d3265cf`.
- Bybit CONTINUOUS summary SHA-256:
  `b42fa5a39ba01e6044f500a0e9f8a9b30eaac6ba4106c5bf37fa17c828f00880`.
- Binance LONG report SHA-256:
  `065cd4eceea7af8161e014b9fd0f65c6718d0264f4dbed33623fd0a14a61ec4e`;
  CONTINUOUS summary SHA-256:
  `3b5b22eca93bb7a26aa2d4f65878bd1565811c842e9f2ba32c32bbb75187b432`;
  run summary SHA-256:
  `389b1dc8c2e6549ab4ecb5b59add818d26a8188871840b73b4c53b0729b3bff8`.

Both completed run ledgers record
`reconcile.demo_paper_backtest=skipped_no_account_snapshots`. No frozen local
demo and paper account roots were supplied, so no three-way equality,
execution, or accounting claim is made. The new reconciliation command remains
available for a later common-epoch snapshot.

Interpretation: both profiles are directionally positive on both venues, and
Bybit has the higher return and Sharpe in this descriptive window. This is
correlated robustness context, not independent confirmation or evidence that
the venue caused the difference. Listing sets, prices, funding evidence, and
fill proxies differ. The registered run had no pass threshold and changes no
profile, promotion state, deployment, capital boundary, or real-money
authority.

After the new run-scoped receipts were finalized, the mutable legacy
`reports/equity_curves` caches under both shared roots were deleted (192 MB
combined) because they had contaminated deterministic replay. Dated historical
research receipts and all failed-run ledgers were preserved.
