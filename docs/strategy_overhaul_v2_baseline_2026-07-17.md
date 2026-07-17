# Strategy Overhaul V2 — Corrected Historical Baseline, 2026-07-17

Status: hash-pinned exploratory reference control. It is descriptive historical
evidence, not a confirmatory result, runtime-parity claim, promotion decision,
or trading authorization.

This baseline supersedes
`docs/strategy_overhaul_v2_baseline_2026-07-16.md` as the current V2 comparison
control. The prior receipt remains useful history, but its CONTINUOUS net
returns used the invalid modal-cadence funding calculation and must not be used
as a current accounting baseline.

## Frozen run identity

- Run: `benchmark-replacement-2026-07-17-rmom-lifecycle-fixed`
- Code: `b095d5ce0274d094147d1f63262bb6f6606f3e7d`
- Window: `[2023-07-17, 2026-07-17)` UTC; last eligible market day
  2026-07-16
- Roots: canonical Bybit and Binance full-PIT roots named in the run manifest
- Exposure: modeled `1.0x`; CONTINUOUS chart leverage `1.0x`
- Summary snapshot:
  `1a50d9fcaaa8064ca82b897d33cab0f44026c001b7d4d93195c57bbc6b540537`
- Run manifest SHA-256:
  `15187c01f77b5416a165ed390fc83546a8dc9f3512a600b8fe4c79bae2dbff9a`
- Run event ledger SHA-256:
  `07a2a5070fe97690b67ba5f50d8bd20d700d0e70741c485b54d74eb299aaf3ae`
- Summary file SHA-256:
  `9ca2cb8be2f5e4c470b382d06ccff82158d28e76e00c005fa6a21eff11203409`

The run started from a clean worktree. Both LONG cells are untainted,
warning-free, and `full_pit_universe`; their required manifest date-symbol
sets have zero missing klines. Every run step completed. No frozen demo or
paper account snapshots were available, so the automated three-way step
correctly recorded `skipped_no_account_snapshots` and establishes no
demo/paper/backtest parity.

## Reference results

| Venue | Sleeve | Return | Annualized | Max drawdown | Sharpe | MAR | Ledger population | Funding |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Bybit | LONG | 35.0733% | 10.5409% | -3.2739% | 2.2047 | n/a | 183 rows / 183 trade IDs | 183/183 modeled; 1,521 events |
| Bybit | CONTINUOUS | 20.6472% | 6.46% | -1.2951% | 2.66 | 5.31 | 786 / 739 / 659 component rows; 807 union trade IDs | 2,178 modeled and 6 partial component rows |
| Binance | LONG | 29.3684% | 8.9622% | -3.0682% | 1.6649 | n/a | 183 rows / 183 trade IDs | 183/183 modeled; 1,536 events |
| Binance | CONTINUOUS | 16.5479% | 5.24% | -1.5589% | 2.36 | 3.54 | 766 / 703 / 611 component rows; 787 union trade IDs | 2,077 modeled and 3 partial component rows |

CONTINUOUS component order is `turn3p3`, `turn4p3`, and `turn4p5`.
Component ledgers intentionally overlap; their row sums are not independent
sample sizes. Bybit has 2,184 component rows, 807 union trade IDs, and 642 IDs
present in all three components. Binance has 2,080 component rows, 787 union
IDs, and 590 IDs present in all three. Every ledger has zero duplicate trade
IDs within its own file.

Independent recomputation from each equity CSV reproduced final equity and
minimum drawdown exactly. Every ledger and equity CSV below matches the run
summary's SHA-256.

## Funding scope and unresolved venue gaps

The exact-settlement correction charges each distinct `(symbol, ts_ms)` venue
settlement once. It does not synthesize missing settlements from a presumed
cadence.

Bybit's six partial component rows are two trades repeated across all three
components:

- `2025-01-30-s-PENGUSDT`: 1% component notional, `data_end`, zero post-entry
  funding events. A fresh Bybit query over `[2025-01-30, 2025-02-02)` returned
  the same two canonical rows, both at or before entry; there is no additional
  venue history to append.
- `2025-06-11-s-1000MUMUUSDT`: 1% component notional, take-profit, all three
  in-hold settlements charged. A fresh query over
  `[2025-06-11, 2025-06-14)` returned the same five canonical rows and no later
  event with which to prove coverage past the archive end.

Binance's three partial component rows are the same
`2026-03-23-s-HOOKUSDT` 1% trade repeated across components. Two in-hold
settlements were charged. A fresh Binance query over
`[2026-03-23, 2026-03-26)` returned exactly the same five canonical rows and no
expected final in-hold settlement, so the missing charge cannot be recovered
or safely inferred.

The Binance ancillary tail also retained HTTP 400 `Invalid pair` warnings for
`ANTHROPICUSDT` and `OPENAIUSDT` index-price requests. Neither symbol appears in
any emitted benchmark ledger. The warnings remain part of the run log; they
are not silently represented as successful ancillary coverage.

## Artifact identities

Paths are repository-relative local research artifacts under ignored
`reports/`. A missing or hash-mismatched file is not this baseline and must not
be silently substituted.

### Bybit LONG

- Curve PNG: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/long/long_native_equity_btc.png`
  (`sha256:40f329963108e38cb327ef6777ece046c042dc72a7fd1298baa548f0dbe05bb9`)
- Equity CSV: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/long/long_native_equity.csv`
  (`sha256:a91880aa21089d7dd1312601fe5063c574583815182e2ca403905f03e2977313`)
- Report: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/long/long_native_research_report.json`
  (`sha256:a62ef00afa48315ff8f23278d4d50605efa434e1437ec74cd7ffefa348dec85f`)
- Full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/long/long_native_trades.csv`
  (`sha256:744313448c7ca29fd7e817b9ae6e4ead36a25104123e66e7aef2a733c9d1c789`)

### Bybit CONTINUOUS

- Curve PNG: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/bybit/continuous_equity_btc.png`
  (`sha256:34a3ba08bdc212517e97e6057eb2971d0b5ef3059966d51f87c17c67e35fc290`)
- Equity CSV: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/bybit/continuous_equity.csv`
  (`sha256:0c61cb56b65107091ca1a148da65c0be573823515a1b4143ca8d550f64a61307`)
- Summary: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/bybit/continuous_equity_summary.json`
  (`sha256:f9632b87e4a195c762112474f1fc84169b0f5070733f38316c8e3c7041caacbf`)
- `turn3p3` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/components/bybit/merged_signal/continuous_trades.csv`
  (`sha256:8f811ffd8ccc2f8a2147fde8a218a427abaa80885b9d756f911a9e1ca4b46a69`)
- `turn4p3` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/components/bybit/age240_turn4pop3_crowd2/continuous_trades.csv`
  (`sha256:210c868dff89e1ab5ae3687d6f699975dce8a8fd2b644e94d1cfa7d34a705def`)
- `turn4p5` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/bybit/equity_curves/continuous/components/bybit/age240_turn4pop5_crowd2/continuous_trades.csv`
  (`sha256:9fb0755e1de25b1ab43eddffdffbce51a6299d7fbc8bdbb96608c85f39d5cfce`)

### Binance LONG

- Curve PNG: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/long/long_native_equity_btc.png`
  (`sha256:e5cebf8ccabc0819710fed835f797d7083f6e314f3c508f8c96235fd3f62625a`)
- Equity CSV: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/long/long_native_equity.csv`
  (`sha256:027c5d7f862641e102c6de326d5590ca8a951f9cdf16acd4786d1717c04888fd`)
- Report: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/long/long_native_research_report.json`
  (`sha256:17f5b46aaea406722f8d83539c8b6139c55791000651c642559305e6085e48ae`)
- Full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/long/long_native_trades.csv`
  (`sha256:dd3e8cba5361d27755d94907fcbb0bc6a7d2e045e46b84917752ff63093a80fe`)

### Binance CONTINUOUS

- Curve PNG: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/binance/continuous_equity_btc.png`
  (`sha256:5058d6cf8b5250014a469987ceb177608dbedfc541d3903bd60a6d6732920ced`)
- Equity CSV: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/binance/continuous_equity.csv`
  (`sha256:7eb90bebf10d630a8cf2eda3eb347e4bca0f69a0947d64f4a16e614da6378403`)
- Summary: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/binance/continuous_equity_summary.json`
  (`sha256:20997d55b48dd8f13498ba20d0cc6aecf5039f169b86f98ce487ff645efe1db3`)
- `turn3p3` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/components/binance/merged_signal/continuous_trades.csv`
  (`sha256:8f6e6137772a0ffebf8f1b01101871e4c1feae0c56d7740d3de4286ac28459cb`)
- `turn4p3` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/components/binance/age240_turn4pop3_crowd2/continuous_trades.csv`
  (`sha256:80a46fa2d357a199fb343f3ed3669ac0ca1779d1c50070dc88961c81e5e71c80`)
- `turn4p5` full ledger: `reports/research-refresh/benchmark-replacement-2026-07-17-rmom-lifecycle-fixed/backtests/binance/equity_curves/continuous/components/binance/age240_turn4pop5_crowd2/continuous_trades.csv`
  (`sha256:0dd4148922087fda2a06ba0cfdcaf7229d47fe65fef65395b4dc99af15aa1366`)

## Isolated funding attribution

The separate run `funding-correction-same-window-2026-07-17` held the old
`[2023-07-16, 2026-07-16)` window and all strategy fields fixed. Its snapshot
is `a4dd216d917fd5473b90392a807ef7e90a5132dbb2a523b392378665be32259f`.
All six LONG/component trade-ID sets, order, and non-funding fields were
unchanged. Only funding event count, funding return, and resulting net return
changed. Corrected CONTINUOUS returns were 20.25% Bybit and 16.58% Binance,
versus the invalid published 21.06% and 17.35%.

The rolling baseline above also includes a one-day window roll, the stable RMOM
aged-out-key repair, and chronological terminal-tape closure. Its delta from
the superseded baseline must therefore not be attributed to funding alone.

## Reuse without recomputation

This baseline is an immutable input to Strategy Overhaul V2 Phase 3. Verify the
pinned paths, hashes, commit, profile revision, and component config identities,
then read the existing ledgers and curves. Do not run a research refresh, data
tail, RMOM rebuild, active LONG/CONTINUOUS backtest, or equity regeneration to
reproduce it.

Phase 3 adds observer-only funnel and barebones artifacts. Writer-on/writer-off
tests must prove active decision and numerical equivalence on deterministic
fixtures. If a later change alters shared strategy semantics or a pinned
identity, stop and amend the plan. Missing artifacts or an unresolved mismatch
do not authorize an automatic four-cell replacement run.

## Comparison contract

Every V2 evidence card that compares a change with the current profile must:

1. name the applicable reference cell and artifact hashes;
2. match profile ID, profile revision, component config hashes, actual
   take-profit percentage, PIT universe, venue, `[start, end)`, timing, fills,
   costs, funding, capacity, accounting, modeled exposure, and chart
   presentation, or label the result as an unmatched sensitivity;
3. report unique decisions and simultaneous waves in addition to ledger rows;
4. preserve inspected variants, failures, and missingness;
5. keep exploratory conclusions separate from prospective support.
