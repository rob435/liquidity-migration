## Repo Rules

- **This is a PROGRESSIVE system — move forward, don't anchor to the past.** Do not
  block an improvement on reproducing prior output byte-for-byte. Performance /
  refactor changes are gated by **numerical equivalence within a tight tolerance**
  (`np.allclose`, NaN positions matching), NOT bit-identical output — last-bit
  float-order differences carry no alpha. Current direction and status live in
  STATE.md + `docs/research_summary.md` — defer to them; don't restate dated
  numbers or results here. (The original daily SHORT strategy was ERASED
  2026-06-11 by operator order; the continuous fade book and the long v11a
  sleeve are what remain.) What stays strict is the real-money
  promotion gate (forward demo + the cross-venue bar is the arbiter; there is no
  internal pre-2023 OOS root — see `docs/data_roots.md`) and the
  methodology-correctness gates (PIT / no look-ahead / no survivorship — those are
  correctness bugs, not restrictions to loosen).
- Be honest and call out wrong decisions directly.
- Ask for exact intent, constraints, and success metrics when a request is vague.
- Do not optimize for a vague goal; define the objective before expensive research.
- The system is research-stage — see `docs/research_summary.md`. Promoted profiles deploy to demo + paper only (forward-demo arbiter); which sleeves actually run is operator-toggled in `deploy/sleeves.env` (see STATE.md "What Is Running / Wired"); nothing is promoted to real money. Do not make real-money deployment or promotion claims — the bar is the real-money gate in STATE.md (forward demo, both venues, reconciliation, funding/costs, stress, and capacity).
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

## Codex project skills

Codex-specific project skills live in `.codex/skills/`. Use the matching
`SKILL.md` before running backtests, reconciling live/demo/paper ledgers, reading
research reports, producing equity curves, changing VPS/deploy plumbing, or
answering architecture questions. Keep `.codex/skills/` separate from
`.claude/skills/`; Claude owns the latter.
