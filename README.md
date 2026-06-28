# liquidity-migration

Research and demo/paper execution code for two remaining crypto-perp systems:

- `continuous_ensemble_v2`: continuous fade book.
- `LongV11aDivWeekendVol`: long-native v11a sleeve.

The old daily short sleeve is gone; git history is the archive. The runtime
surface defaults to demo/paper. Mainnet use requires an explicit owner action
and a fresh evidence pack.

## Read First

- [STATE.md](STATE.md) - live state and immediate next work.
- [docs/research_summary.md](docs/research_summary.md) - compact decision log.
- [docs/promoted_trading_logic.md](docs/promoted_trading_logic.md) - active
  profile lifecycle and runtime env boundary.
- [docs/preregistration/INDEX.md](docs/preregistration/INDEX.md) - active
  anchors and closed research arcs.

## Map

- `liquidity_migration/` - package, PIT/data builders, strategy engines,
  execution helpers, and `python -m liquidity_migration`.
- `scripts/` - deploy, reconcile, data, equity, and active research tools.
- `tests/` - pytest suite.
- `docs/` - methodology, data-root contracts, and compact research state.
- `.codex/skills/` - Codex project workflows.
- `.claude/` - Claude memory and project workflows.

Python 3.11+.
