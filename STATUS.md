# Status

## Current

- The repo is now a stripped-down research lab, not a live trading bot.
- Current implementation plan: `docs/volume_alpha.md`.
- Bybit venue/data reference: `docs/bybit_aggression_carry_system_codex_spec.md`.
- The only active alpha path is `volume-alpha`.
- The old live runtime and old blended signal stack have been deleted.
- No live execution, Telegram alerts, kill switches, production deployment, or
  exchange order submission exist in the active code.

## Active Path

- Download Bybit public `instruments` and `klines_1h`.
- Build daily volume-only features from 1h quote turnover.
- Test 1d, 3d, and 7d forward returns.
- Run long/short costed portfolios at configured quantiles.
- Write `volume_alpha_report.md`, JSON report, feature Parquet, metrics Parquet,
  and portfolio Parquet.

## Current Research Read

- Increasing-volume variants failed the corrected 3-month sample.
- `dollar_volume_rank` is the only promising lead so far.
- The promising result is mostly short-leg driven, so it should be treated as a
  low-volume/weak-liquidity short effect until broader tests say otherwise.

## Remaining Risks

- Three months and 16 symbols is not enough evidence.
- Bybit-only data may miss the venue where price discovery actually happens.
- Dollar-volume rank may be a hidden size/liquidity effect, not the podcast's
  claimed increasing-volume alpha.
- Funding, borrow constraints, squeezes, and live execution are intentionally out
  of scope until the standalone alpha is proven.
