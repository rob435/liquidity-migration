# Decisions

## 2026-05-01

- Reset the repo documentation around the fact that the existing live trading logic is flawed and is not the next design target.
- Delete stale strategy/deployment docs instead of leaving them around to mislead future sessions.
- Keep the backtester and analysis plumbing because they are still likely to be useful for validating the replacement logic.
- Keep legacy runtime code for now because deleting it before the new contract exists would remove reusable exchange, persistence, and simulator behavior blindly.
- Remove production deployment scaffolding until a new strategy has a real runtime contract.
- Remove `full_env.txt`; environment dumps do not belong in source control.
- Keep Caveman opt-in only. This repo needs clarity more than extreme token compression.
- Keep Graphify/AO/Composio tooling outside runtime dependencies.
- Add `docs/bybit_aggression_carry_system_codex_spec.md` as the authoritative current plan for the Bybit aggression-carry alpha-proof overhaul.
- Future strategy, data, feature, and backtest work must reference that spec before implementation.
- Build the phase-one alpha proof in a new `aggression_carry/` package instead of importing the legacy `SignalEngine` or live execution runtime.
- Use partitioned Parquet, Polars, and pybit/direct archive handling for research data.
- Keep phase one research-only: no live execution, Telegram alerts, kill switches, or production deployment.
