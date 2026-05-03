# Decisions

## 2026-05-03

- Add automated current-universe discovery, but label it as survivorship-biased
  until point-in-time delisted/dead-contract data is added.
- Test lower-cap exposure through daily liquidity-rank buckets inside a broad
  downloaded universe instead of hardcoding "shitcoin" lists.
- Keep core, mid, and tail bucket results separate. The broad one-year test
  showed the tail bucket's best direction is `short_high_long_low`, not the
  original `long_high_short_low` direction.
- Treat the tail-liquid reversal as a separate research lead. Do not blend it
  with the original 16-symbol result until it survives longer history,
  point-in-time universe checks, and execution/funding stress.

## 2026-05-02

- Strip the repo down instead of continuing to patch the old system.
- Make `docs/volume_alpha.md` the current implementation plan.
- Keep `docs/bybit_aggression_carry_system_codex_spec.md` as Bybit data/source
  reference material, not as permission to rebuild a blended composite.
- Delete the old live runtime and old root backtest stack because it depended on
  `SignalEngine`, `ExecutionEngine`, `MarketState`, old config, and alerting.
- Delete the old aggression/carry/momentum/quality/OI composite modules from the
  new package.
- Keep only the research pieces needed for the current alpha: downloaders,
  archive parsing, Bybit public data, ingestion helpers, storage, math utilities,
  and `volume_alpha.py`.
- Keep Codex/Graphify/AO tooling outside runtime dependencies.
- Keep phase one research-only: no live execution, alerts, kill switches,
  deployment, or exchange order submission.
- Add a separate `volume-backtest` command instead of overloading
  `volume-alpha`. The sweep tests signal evidence; the backtest records actual
  trade entries, exits, exit reasons, stops, costs, basket returns, and
  attribution.
- Keep the first detailed lifecycle non-overlapping: `rebalance_days` must be
  greater than or equal to `hold_days`. Overlapping sleeves can be added later
  only after the simple trade ledger is understood.
- Default the detailed test to the current lead from the corrected sample:
  `dollar_volume_rank`, 50% long/short buckets, 7-day hold/rebalance, 1.0x
  gross, 8% fixed stop, no take-profit cap.
- Add `volume-grid` for lifecycle parameter testing instead of manually running
  one-off backtests. The grid tests no/fixed/volatility stops, rank exits,
  hold/rebalance periods, quantiles, costs, and optional side reversal.
- Use process-level CPU concurrency for grid variants. Do not use the RTX GPU
  until/unless the simulation is rewritten around vectorized CUDA primitives.

## 2026-05-01

- Add the Bybit aggression-carry spec as the first authoritative overhaul
  reference.
- Build phase-one research code in `aggression_carry/` instead of importing the
  old live runtime.
- Use partitioned Parquet, Polars, pyarrow, pybit, and direct Bybit
  archive handling for research data.
