# Strategy Overhaul V2 — Historical Baseline, 2026-07-16

Status: superseded historical receipt. Its CONTINUOUS net returns used the
invalid modal-cadence funding calculation. The current comparison control is
`docs/strategy_overhaul_v2_baseline_2026-07-17.md`. This file is retained so the
old claim and artifact identities remain auditable; it is not a current
performance baseline, confirmatory result, runtime-parity claim, promotion
decision, or trading authorization.

This baseline formerly fixed the current-profile outputs against which the
first V2 diagnostic and barebones comparisons were intended to reconcile. The
evaluation window is
`[2023-07-16, 2026-07-16)` UTC, so the last eligible market day is 2026-07-15.
Both venue roots are full-PIT. CONTINUOUS used modeled and chart leverage `1.0`.
LONG used its native active configuration (`execution_leverage=10.0`,
`notional_multiplier=1.0`) with no additional chart scaling.

The source window is already exposed. These results can generate and compare
diagnostic hypotheses, but cannot confirm a thesis selected from them.

## Reference results

| Venue | Sleeve | Return | Annualized | Max drawdown | Sharpe | MAR | Ledger population | Funding |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| Bybit | LONG | 35.38% | 10.63% | -3.27% | 2.22 | 3.60 | 183 rows / 183 trade IDs | 183/183 modeled |
| Bybit | CONTINUOUS | 21.06% | 6.58% | -1.30% | 2.73 | 5.41 | 786 / 737 / 656 component rows; 807 union trade IDs | 2,179/2,179 component rows modeled |
| Binance | LONG | 29.37% | 9.04% | -3.07% | 1.66 | 3.22 | 183 rows / 183 trade IDs | 183/183 modeled |
| Binance | CONTINUOUS | 17.35% | 5.48% | -1.41% | 2.46 | 4.10 | 768 / 703 / 611 component rows; 789 union trade IDs | 2,079 modeled and 3 partial component rows |

CONTINUOUS has three intentionally overlapping component ledgers in the order
`turn3p3`, `turn4p3`, and `turn4p5`. The component-row totals are not independent
trades: Bybit has 2,179 rows but 807 union trade IDs, and Binance has 2,082 rows
but 789 union trade IDs. The full CONTINUOUS ledger evidence is therefore all
three component CSVs plus the aggregate equity series, not a sum treated as an
independent sample. Binance's three partial rows are the same `HOOKUSDT` trade
on 2026-03-23 repeated across components; every other Binance component row has
modeled funding.

Independent recomputation from each equity CSV reproduced its reported final
return and minimum drawdown exactly. Every ledger has zero duplicate trade IDs
within its own file. Bybit and Binance remain correlated robustness surfaces,
not independent replications.

## Artifact identities

Paths are repository-relative local research artifacts under ignored
`reports/`. A missing or hash-mismatched file is not this baseline and must not
be silently substituted.

### Bybit LONG

Run code commit: `f84dde629fccc5ca3a51dce2df2e81ac5d99318d`.

- Curve: `reports/research-refresh/benchmark-refresh-2026-07-16-bybit-long-diagnostics/backtests/bybit/equity_curves/long/long_native_equity_btc.png`
  (`sha256:26b0f076d04bd49d82f71fc6500e2bf6ef24425c63a3968e864a248455ce69c3`)
- Report: `reports/research-refresh/benchmark-refresh-2026-07-16-bybit-long-diagnostics/backtests/bybit/equity_curves/long/long_native_research_report.json`
  (`sha256:8ed9a804c27552f499150955f7058ae4b2a16d0c88a43e583d90c0fc6650277f`)
- Full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-bybit-long-diagnostics/backtests/bybit/equity_curves/long/long_native_trades.csv`
  (`sha256:98a2a61cd61b65f1fa039ff0593f8dc3aafe41e7830fe9bb1f79438944271806`)

### Bybit CONTINUOUS

Run code commit: `3d492e4e72e526d7f816a0845c121ca4dacf55c3`.

- Curve: `reports/research-refresh/benchmark-refresh-2026-07-16-isolated/backtests/bybit/equity_curves/continuous/bybit/continuous_equity_btc.png`
  (`sha256:1953f748cd5c1e6f79f723c48738c5f4042a089f5513bf265473d3fb7aaa93f0`)
- Summary: `reports/research-refresh/benchmark-refresh-2026-07-16-isolated/backtests/bybit/equity_curves/continuous/bybit/continuous_equity_summary.json`
  (`sha256:b42fa5a39ba01e6044f500a0e9f8a9b30eaac6ba4106c5bf37fa17c828f00880`)
- `turn3p3` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-isolated/backtests/bybit/equity_curves/continuous/components/bybit/merged_signal/continuous_trades.csv`
  (`sha256:8e4a2cad14403da824e1041fad91426ccaa57ca67c882bd994e3e4645b31a532`)
- `turn4p3` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-isolated/backtests/bybit/equity_curves/continuous/components/bybit/age240_turn4pop3_crowd2/continuous_trades.csv`
  (`sha256:1cb44b31dbd5265be8f99ef7cc1c064855a0b870a52014150e2a2a05bddc55de`)
- `turn4p5` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-isolated/backtests/bybit/equity_curves/continuous/components/bybit/age240_turn4pop5_crowd2/continuous_trades.csv`
  (`sha256:e06b989fb0d56f83b8ded4738b709adcc08e67488494ff5ea743b51e491d1823`)

### Binance LONG

Run code commit: `f84dde629fccc5ca3a51dce2df2e81ac5d99318d`.

- Curve: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/long/long_native_equity_btc.png`
  (`sha256:e5cebf8ccabc0819710fed835f797d7083f6e314f3c508f8c96235fd3f62625a`)
- Report: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/long/long_native_research_report.json`
  (`sha256:065cd4eceea7af8161e014b9fd0f65c6718d0264f4dbed33623fd0a14a61ec4e`)
- Full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/long/long_native_trades.csv`
  (`sha256:dd3e8cba5361d27755d94907fcbb0bc6a7d2e045e46b84917752ff63093a80fe`)

### Binance CONTINUOUS

Run code commit: `f84dde629fccc5ca3a51dce2df2e81ac5d99318d`.

- Curve: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/continuous/binance/continuous_equity_btc.png`
  (`sha256:0b5cb3c0f23f8d44d0fb67359a89efdad3b5dd07641307f45c67e388d31576e4`)
- Summary: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/continuous/binance/continuous_equity_summary.json`
  (`sha256:3b5b22eca93bb7a26aa2d4f65878bd1565811c842e9f2ba32c32bbb75187b432`)
- `turn3p3` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/continuous/components/binance/merged_signal/continuous_trades.csv`
  (`sha256:90fb1f0489026d6ef1593d1366fa9f8532ca62bae9cac02055aa5ea7743137c2`)
- `turn4p3` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/continuous/components/binance/age240_turn4pop3_crowd2/continuous_trades.csv`
  (`sha256:4f191f7861cd25cfc6c50613d93a967e14047d74e240430f059e63fc06d26bd8`)
- `turn4p5` full ledger: `reports/research-refresh/benchmark-refresh-2026-07-16-binance-atomic/backtests/binance/equity_curves/continuous/components/binance/age240_turn4pop5_crowd2/continuous_trades.csv`
  (`sha256:d07b679f455bd1ab454e4ac1bcece9c95d4b961c583d33888b246585312deeed`)

## Provenance boundary

The Bybit CONTINUOUS artifact predates the other three cells by one code
commit. The complete diff from `3d492e4` to `f84dde6` changed Binance ingestion,
LONG boundary handling, documentation, scripts, and tests; it did not change
CONTINUOUS implementation, configuration, or equity generation. The artifact is
accepted as the hash-pinned historical reference on that narrow basis. It is
not represented as a single-commit four-cell rerun.

This was the comparison rule before the funding defect was found. New
claim-bearing work must use the corrected 2026-07-17 baseline instead. This
receipt remains immutable historical context and must not be silently
overwritten or treated as a valid net-accounting control.

## Superseded comparison contract

The following contract governed this historical receipt. The active contract
is in `docs/strategy_overhaul_v2_baseline_2026-07-17.md`.

1. name the applicable reference cell and artifact hashes;
2. match PIT universe, venue, `[start, end)`, timing, execution, cost, funding,
   capacity, accounting, modeled exposure, and chart presentation, or label the
   result as an unmatched sensitivity rather than a direct comparison;
3. report decision and simultaneous-wave counts in addition to ledger rows;
4. preserve all inspected variants and missingness;
5. keep historical/exploratory conclusions separate from prospective support.
