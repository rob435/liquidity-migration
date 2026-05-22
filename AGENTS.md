## Repo Rules

- Be honest and call out wrong decisions directly.
- Ask for exact intent, constraints, and success metrics when a request is vague.
- Do not optimize for a vague goal; define the objective before expensive research.
- The liquidity-migration short signal is statistically real but regime-conditional; the strategy is under active research — see `docs/research_findings.md`. It is not deployed, and the standalone strategy is not deployable as-is. Do not make deployment or promotion claims unless the regime overlay is validated out-of-sample and funding is costed.
- A real-money (mainnet) execution path exists but is **disabled by default** and gated behind `bybit.real_money_armed()`. Keep it disabled — never set the `LIQMIG_TRADING_MODE` / `LIQMIG_REAL_MONEY_ACK` arming variables without explicit owner instruction. Demo-only order submission is the operating default; the strategy is not validated for real money.
- Telegram may notify; it must not approve or submit orders.
- Serious research runs should leave enough report output to audit the decision.

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
