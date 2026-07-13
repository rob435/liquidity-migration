---
name: repo-map
description: Orient in the liquidity-migration codebase, locate ownership, and trace cross-module behavior. Use for architecture, where-does-this-live, dependency-path, or cross-module change questions. Use Graphify as a navigation aid when it adds value, then verify every material claim against current source, tests, runtime config, and artifacts; do not treat generated graph output or this skill as authority.
---

# Navigate the repository

Start from the source that owns the question:

- Runtime state: `STATE.md`, `deploy/sleeves.env`, systemd units, and current
  environment/config.
- Evidence policy: `docs/governance.md`.
- Research decisions: `docs/research_summary.md` and raw run artifacts.
- Active profile contract: `docs/promoted_trading_logic.md`, then the registry,
  engine, daemon, and deploy overrides it cites.
- CLI ownership: `liquidity_migration/cli.py`, parser modules, and current
  `--help`.
- Data/PIT: `storage.py`, `data_layer.py`, `ingestion.py`,
  `archive_manifest.py`, `volume_events_pit.py`, `docs/data_roots.md`, and
  `docs/pit_gate.md`.
- Execution lifecycle: continuous/long target producers,
  `account_service.py`, `account_kernel.py`, `account_reconcile.py`,
  `trade_lifecycle.py`, the demo/paper account-owner launchers, and their tests.

Use `rg --files` and `rg` for direct discovery.

## Use Graphify proportionately

For broad relationships or an unfamiliar subsystem, inspect
`graphify-out/GRAPH_REPORT.md` and use:

```bash
graphify query "QUESTION"
graphify path "A" "B"
graphify explain "QUESTION"
```

Treat the graph as derived, potentially stale evidence. Open the cited source and
tests before answering or editing. Do not force Graphify into a local one-file
change.

After an architecture-affecting code change, update the graph only when doing so
will not overwrite unrelated dirty graph work:

```bash
graphify update .
```

## Trace before changing

Identify definition, callers, data shapes, configuration sources, persistence,
runtime overrides, tests, and external effects. For cross-module changes, state
which layer owns the invariant and avoid duplicating it into skills or status
docs.
