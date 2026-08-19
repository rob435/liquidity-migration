---
name: repo-map
description: Orient in the liquidity-migration codebase, locate ownership, and trace cross-module behavior. Use for architecture, where-does-this-live, dependency-path, or cross-module change questions. Verify every material claim against current source, tests, runtime config, and artifacts; do not treat this skill as authority.
---

# Navigate the repository

Read `liquidity_migration/README.md` first: it names all twelve subpackages,
what belongs in each, and the measured import order (`core` knows nothing;
`runtime` is an empty sink — the owner runners it held went with the Python
order path, deleted 2026-08-14). `scripts/README.md` does the same for the
script tree. Then `docs/architecture.md` (§Where modules live) for the Python
side — its owner-era sections are historical spec — and `docs/engine.md` for
the Rust engine in `engine/`, which is the whole order path now.
Before broad work, run `scripts/dev.sh doctor --json` when selected-Python,
worktree, dependency-lock, or skill-tree state could affect the result. Treat
diagnostics as local facts, never as runtime authorization.

Start from the source that owns the question:

- Runtime state: `STATE.md` (the snapshot; dated history in `CHANGELOG.md`),
  `deploy/sleeves.env`, systemd units, and current environment/config.
- Evidence policy: `AGENTS.md` for the standing rules, `docs/research/governance.md` for
  the Progressive Evidence Model itself.
- Research decisions and queue: `docs/research/strategy_program.md` and raw run
  artifacts.
- Active profile contract: `docs/trading_logic.md`, then the strategy
  modules, target producers, the registered rules in
  `liquidity_migration/rules/`, and deploy overrides it cites.
- CLI ownership: `liquidity_migration/cli/commands.py`, parser modules, and current
  `--help`.
- Data/PIT: `liquidity_migration/data/` (`storage.py`, `ingestion.py`,
  `archive_manifest.py`, `volume_events_pit.py`) and `docs/data.md` (Research
  roots, Point-in-time membership).
- Execution lifecycle: the Rust engine owns the whole order path — `engine/`
  and `docs/engine.md` (contracts, latency budget, safety posture). On the
  Python side: the target producers in `liquidity_migration/strategy/` replay
  the registered rules from `liquidity_migration/rules/` and write the target
  books the engine follows; `liquidity_migration/account/` is the producers'
  library (contracts, the deterministic kernel, leases, health — nothing in
  it turns a target into an order any more); the credentialed Bybit edge for
  the surviving Python tools is `liquidity_migration/venue/`; the tape is
  `liquidity_migration/data/trade_lifecycle.py`; and their tests.

Use `rg --files` and `rg` for direct discovery.

## Trace before changing

Identify definition, callers, data shapes, configuration sources, persistence,
runtime overrides, tests, and external effects. For cross-module changes, state
which layer owns the invariant and avoid duplicating it into skills or status
docs.

Use `scripts/dev.sh test` for focused tests and `scripts/dev.sh check` for the
full local Ruff, mypy, and pytest gate. Add claim-specific validation when the
task concerns research, PIT, accounting, deployment, or live-runtime parity.
