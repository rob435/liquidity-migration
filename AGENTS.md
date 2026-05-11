## Repo Rules

- Be honest and call out wrong decisions directly.
- Ask for exact intent, constraints, and success metrics when a request is vague.
- Do not optimize for a vague goal; define the objective before expensive research.
- This repo's primary objective is a profitable Bybit demo-account trading system.
- Current implementation plan: `docs/bybit_aggression_carry_system_codex_spec.md`.
- Secondary volume-alpha research notes: `docs/volume_alpha.md`.
- Bybit venue/data reference: `docs/bybit_aggression_carry_system_codex_spec.md`.
- Before changing strategy, data ingestion, feature engineering, or backtesting logic, read both docs.
- Demo-only Bybit order submission is in scope; the private client must keep refusing `demo=False` unless real-money support is explicitly requested.
- Keep the active path on the selected Stage 4 daily-close short fade unless a change has clear forward/demo evidence.
- Telegram may notify; it must not approve or submit orders.
- Do not mix secondary signals into the demo stack without standalone cost-cleared evidence.
- Use `volume-alpha` for signal sweeps, `volume-backtest` for trade-ledger testing, and `volume-grid` for concurrent parameter sweeps.
- Serious strategy runs should leave enough report output to audit the decision.

## graphify

This project has a graphify knowledge graph at graphify-out/.
`graphify-out/GRAPH_REPORT.md` is tracked as the lightweight navigation report.
`graphify-out/graph.json` is generated locally and intentionally ignored to keep
the repo light.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep. These traverse the graph's EXTRACTED + INFERRED edges instead of scanning files.
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost). If `graphify` is not on PATH, use `python3 -m graphify update .`.
