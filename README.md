# liquidity-migration

A research codebase for crypto-perp trading systems. Two systems remain:

- **Continuous fade book** - the live demo system, research-stage only.
- **Long-native v11a sleeve** - promoted-in-code for demo/paper only, currently
  toggled off on the live box.

The original daily SHORT strategy was erased from the system on 2026-06-11 by
operator order. Git history is the archive.

## Status

Nothing is approved for real money. Forward demo/paper, both venues, and the
three-tier gate in [STATE.md](STATE.md) are the arbiter.

Read first:

- [STATE.md](STATE.md) - live state, open operator decisions, decision rules.
- [docs/research_summary.md](docs/research_summary.md) - current research
  decisions, failure ledger, and revisit queue.

## Repository Map

- `liquidity_migration/` - package, data ingestion, PIT builders, strategy
  engines, execution helpers, and `python -m liquidity_migration`.
- `scripts/` - runtime helpers, deploy/reconcile tools, data builders, and a
  smaller set of active research drivers.
- `tests/` - pytest suite.
- `docs/backtesting_errors_we_never_repeat.md` - methodology standard.
- `docs/data_roots.md` - research/live/OOS data-root contract.
- `docs/preregistration/` - only active/binding receipts.
- `.codex/skills/` - Codex project skills.
- `.claude/` - Claude Code memory, skills, and the local MCP server.
- `AGENTS.md` - repo rules.

Python 3.11+.
