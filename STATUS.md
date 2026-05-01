# Status

## Current

- The previous live strategy is considered flawed and no longer authoritative.
- The authoritative current spec is `docs/bybit_aggression_carry_system_codex_spec.md`.
- Old system spec, old research plan, stale production env examples, systemd/launchd deployment files, export-sync scripts, and local full-env dump have been removed.
- `aggression_carry/` now contains the phase-one alpha-proof skeleton.
- The legacy Python runtime is intentionally still present so useful pieces can be salvaged deliberately.
- The backtester remains the main reusable asset.
- Codex companion tooling remains installed and documented in `CODEX_TOOLS.md`.
- Graphify code graph exists under `graphify-out/` for navigation after the cleanup rebuild.
- Local tests pass with `124` tests.
- `pyflakes` is not installed in the active Python; compile, full tests, and `git diff --check` are clean.

## Keep For Now

- `backtest.py`
- `exchange.py`
- `database.py`
- `reconcile.py`
- `report.py`
- `deploy/cache_bundle.py`
- tests that protect reusable plumbing

## Implemented Alpha-Proof Path

- Fixture and Bybit REST/pybit data download CLI surface.
- Partitioned Parquet storage with merge/dedupe behavior.
- Public trade parsing and signed-flow aggregation.
- 1h feature engine for aggression, relative volume, momentum, carry, quality, OI impulse, and composite score.
- Alpha report with IC, quantile spread, leave-one-out ablations, monthly consistency, and gate snapshot.
- Research evidence tables for per-timestamp IC, cost-adjusted quantile ledgers, and monthly spreads.
- Costed portfolio backtest with period ledger, per-symbol positions, symbol/month attribution, long/short price PnL, funding, fees, slippage, and missing-forward-return accounting.
- Robustness checks for 2x costs, fee share, symbol concentration, excluding BTC/ETH/SOL, and excluding the best month.

## Legacy Until Rewritten

- `signal_engine.py`
- `execution.py`
- `main.py`
- current config surface in `config.py`
- runtime monitor/validation helpers

## Remaining Risks

- Passing tests still describe the legacy behavior; they are not proof the trading logic is good.
- Historical Bybit public-trade archive ingestion works against the public `.csv.gz` archive URL template for a one-day smoke; the main aggression alpha still needs a broad multi-symbol, multi-month run.
- Some retained modules may still contain stale assumptions from the old live system.
- `gh` and Composio still require user authentication before GitHub CI automation or external app actions can work.
- AO spawning is currently blocked until `gh auth login` is completed.
- Acceptance gates can fail on fixture data; that is expected. The fixture proves mechanics, not alpha.
