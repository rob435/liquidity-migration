# Decisions

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

## 2026-05-01

- Add the Bybit aggression-carry spec as the first authoritative overhaul
  reference.
- Build phase-one research code in `aggression_carry/` instead of importing the
  old live runtime.
- Use partitioned Parquet, Polars, pyarrow, pybit, and direct Bybit
  archive handling for research data.
