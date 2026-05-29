## Repo Rules

- **This is a PROGRESSIVE system — move forward, don't anchor to the past.** Do not
  block an improvement on reproducing prior output byte-for-byte. Performance /
  refactor changes are gated by **numerical equivalence within a tight tolerance**
  (`np.allclose`, NaN positions matching), NOT bit-identical output — last-bit
  float-order differences carry no alpha. The deployed daily-frequency design
  (Architecture A) is the *current* state, not a frozen baseline: the active direction
  is the lowest-latency, fully-event-driven, continuous-signal Architecture B (Round 2
  C-phases). What stays strict is the real-money promotion gate (OOS + forward demo is
  the arbiter) and the methodology-correctness gates (PIT / no look-ahead / no
  survivorship — those are correctness bugs, not restrictions to loosen).
- Be honest and call out wrong decisions directly.
- Ask for exact intent, constraints, and success metrics when a request is vague.
- Do not optimize for a vague goal; define the objective before expensive research.
- The liquidity-migration short signal is statistically real but regime-conditional; the strategy is under active research — see `docs/research_findings.md`. It is not deployed, and the standalone strategy is not deployable as-is. Do not make deployment or promotion claims unless the regime overlay is validated out-of-sample and funding is costed.
- A real-money (mainnet) execution path exists; the account is a `.env` toggle (`DEMO` / `REAL_MONEY`, mutually exclusive) read by `bybit.resolve_private_credentials()`, defaulting to demo. Keep it on demo — do not set `REAL_MONEY=true` without explicit owner instruction. The strategy is not validated for real money.
- Telegram may notify; it must not approve or submit orders.
- Serious research runs should leave enough report output to audit the decision.

## Parameter pre-registration

Every parameter change that will touch a per-venue working dataset (the new
`bybit_full_pit` / `binance_full_pit` roots) gets a pre-registration entry
under `docs/preregistration/` BEFORE the run, and the receipt is committed in
the same PR as the code change. Skipping pre-registration is allowed only for
`EXPLORATORY` runs — those must not be cited as evidence in any decision to
promote, deploy, or accept a parameter as alpha.

The standard, template, and worked examples live in
[docs/parameter_pre_registration.md](docs/parameter_pre_registration.md).

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
