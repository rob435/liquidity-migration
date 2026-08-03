---
name: repo-map
description: Orient in the liquidity-migration codebase, locate ownership, and trace cross-module behavior. Use for architecture, where-does-this-live, dependency-path, or cross-module change questions. Verify every material claim against current source, tests, runtime config, and artifacts; do not treat this skill as authority.
---

# Navigate the repository

Read `liquidity_migration/README.md` first: it names all eleven subpackages,
what belongs in each, and the measured import order (`core` knows nothing,
`runtime` is a sink). `scripts/README.md` does the same for the script tree.
Then `docs/architecture.md` (Subsystem map) for subsystem ownership and entry
points.
Before broad work, run `scripts/dev.sh doctor --json` when selected-Python,
worktree, dependency-lock, or skill-tree state could affect the result. Treat
diagnostics as local facts, never as runtime authorization.

Start from the source that owns the question:

- Runtime state: `STATE.md`, `deploy/sleeves.env`, systemd units, and current
  environment/config.
- Evidence policy: `AGENTS.md` for the standing rules, `docs/research/governance.md` for
  the Progressive Evidence Model itself.
- Research decisions and queue: `docs/research/strategy_program.md` and raw run
  artifacts.
- Active profile contract: `docs/trading_logic.md`, then the strategy
  modules, target producers, account owner, and deploy overrides it cites.
- CLI ownership: `liquidity_migration/cli/commands.py`, parser modules, and current
  `--help`.
- Data/PIT: `liquidity_migration/data/` (`storage.py`, `ingestion.py`,
  `archive_manifest.py`, `volume_events_pit.py`) and `docs/data.md` (Research
  roots, Point-in-time membership).
- Execution lifecycle: the target producers in `liquidity_migration/strategy/`,
  the kernel and owner in `liquidity_migration/account/`
  (`account_service.py`, `account_kernel.py`), the credentialed edge in
  `liquidity_migration/venue/` (`account_reconcile.py`), the tape in
  `liquidity_migration/data/trade_lifecycle.py`, the owner launchers and
  twin in `liquidity_migration/runtime/`, and their tests.

Use `rg --files` and `rg` for direct discovery.

## Trace before changing

Identify definition, callers, data shapes, configuration sources, persistence,
runtime overrides, tests, and external effects. For cross-module changes, state
which layer owns the invariant and avoid duplicating it into skills or status
docs.

Use `scripts/dev.sh test` for focused tests and `scripts/dev.sh check` for the
full local Ruff, mypy, and pytest gate. Add claim-specific validation when the
task concerns research, PIT, accounting, deployment, or live-runtime parity.
