## Repo Rules

- Be honest and call out wrong decisions directly.
- This repo is a research lab, not a live trading runtime.
- Current implementation plan: `docs/volume_alpha.md`.
- Bybit venue/data reference: `docs/bybit_aggression_carry_system_codex_spec.md`.
- Before changing strategy, data ingestion, feature engineering, or backtesting logic, read both docs.
- Do not rebuild the deleted legacy live runtime or blended signal stack unless explicitly requested.
- Do not add live execution, kill switches, deployment, or exchange order submission.
- Telegram is allowed only for paper forward-test notifications; it must not submit or approve orders.
- Do not combine signals until each alpha clears costs standalone.
- Use `volume-alpha` for signal sweeps, `volume-backtest` for trade-ledger testing, and `volume-grid` for concurrent parameter sweeps.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep. These traverse the graph's EXTRACTED + INFERRED edges instead of scanning files.
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost). If `graphify` is not on PATH, use `python3 -m graphify update .`.
