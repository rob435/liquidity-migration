---
name: repo-map
description: Orient in the liquidity-migration codebase, locate ownership, and trace cross-module behavior. Use for architecture, where-does-this-live, dependency-path, or cross-module change questions. Verify every material claim against current source, tests, runtime config, and artifacts; do not treat this skill as authority.
---

# Navigate the repository

Read `liquidity_migration/README.md` first: it names the Python support-plane
packages, what belongs in each, and the measured import order. `scripts/README.md`
does the same for the script tree. Then read `docs/architecture.md` for the
cross-language boundary and `docs/engine.md` for the Rust runtime in `engine/`.
Before broad work, run `scripts/dev.sh doctor --json` when selected-Python,
worktree, dependency-lock, or skill-tree state could affect the result. Treat
diagnostics as local facts, never as runtime authorization.

Start from the source that owns the question:

- Runtime state: `STATE.md` (the snapshot; dated history in `CHANGELOG.md`),
  `deploy/sleeves.env`, systemd units, and current environment/config.
- Evidence policy: `AGENTS.md` for the standing rules, `docs/research/governance.md` for
  the Progressive Evidence Model itself.
- Research decisions and queue: `docs/research/research_findings.md` and raw run
  artifacts.
- Active profile contract: `docs/trading_logic.md`, the native Rust reducers
  under `engine/engine-strategies/`, their registered configs, and the deploy
  overrides the document cites.
- CLI ownership: `liquidity_migration/cli/commands.py`, parser modules, and current
  `--help`.
- Data/PIT: `liquidity_migration/data/` (`storage.py`, `ingestion.py`,
  `archive_manifest.py`, `volume_events_pit.py`) and `docs/data.md` (Research
  roots, Point-in-time membership).
- Execution lifecycle: `engine/signal-worker/` owns public directional signal
  production; the native reducers under `engine/engine-strategies/` own each
  strategy decision; `engine/engine-core/` owns durable application, account
  state, scheduling, and admission; `engine/engine-venue/` owns authenticated
  venue I/O. Python may replay the Rust contracts for research, read the WAL
  and venue history for accounting, and carry operator messages, but it does
  not decide or submit live orders.

Use `rg --files` and `rg` for direct discovery.

## Trace before changing

Identify definition, callers, data shapes, configuration sources, persistence,
runtime overrides, tests, and external effects. For cross-module changes, state
which layer owns the invariant and avoid duplicating it into skills or status
docs.

Use `scripts/dev.sh test` for focused tests and `scripts/dev.sh check` for the
full local Ruff, mypy, and pytest gate. Add claim-specific validation when the
task concerns research, PIT, accounting, deployment, or live-runtime parity.
