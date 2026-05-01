## Repo Rules

- Be honest and call out wrong decisions directly.
- Do not let agent-tooling convenience change the trading runtime contract.
- Before changing strategy, data ingestion, feature engineering, or backtesting logic, read `docs/bybit_aggression_carry_system_codex_spec.md`.
- Treat `docs/bybit_aggression_carry_system_codex_spec.md` as the authoritative current plan. Old live-runtime assumptions are not authoritative.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep. These traverse the graph's EXTRACTED + INFERRED edges instead of scanning files.
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost). If `graphify` is not on PATH, use `python3 -m graphify update .`.
