## Repo Rules

- Be honest. Call out bad assumptions, stale evidence, and weak decisions
  directly.
- Move forward. Refactors and performance changes are gated by tight numerical
  equivalence (`np.allclose` with matching NaN positions), not byte-identical
  floating-point output.
- Current state lives in [STATE.md](STATE.md) and
  [docs/research_summary.md](docs/research_summary.md). Do not rebuild old
  receipt archives in the hot path.
- Runtime default is demo/paper. A mainnet credential path exists; do not enable
  `REAL_MONEY` or submit approval-by-notification flows without explicit owner
  instruction.
- Serious research needs a hypothesis, data roots, decision rule, artifacts, and
  run label. Exploratory work is not acceptance evidence.
- PIT membership, causal features, survivorship control, costs/funding, and
  reconstructable ledgers are correctness gates.

## Graphify

- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
- Prefer `graphify query`, `graphify path`, or `graphify explain` for
  cross-module relationship questions.
- After modifying code, run `graphify update .` or `python3 -m graphify update .`.

## Codex Skills

Project skills live in `.codex/skills/`. Use the matching `SKILL.md` before
backtests, live/demo/paper reconciliation, research-report interpretation,
equity curves, VPS/deploy work, or architecture answers.
