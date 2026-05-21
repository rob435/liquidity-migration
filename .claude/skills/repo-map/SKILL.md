---
name: repo-map
description: "Orient in the liquidity-migration codebase: module map, core abstractions, key docs, and the graphify knowledge-graph workflow. Use when answering architecture or where-does-X-live questions, navigating modules, tracing how modules relate, or before cross-module changes."
---

# Repo map

A Bybit research codebase for a liquidity-migration short strategy. The strategy
is under active research — its signal is statistically real but
regime-conditional, and the current focus is regime-aware refinement (see
`docs/research_findings.md`).

## graphify first (mandated by AGENTS.md)

- Read `graphify-out/GRAPH_REPORT.md` for god nodes and community structure
  before answering architecture questions.
- For cross-module "how does X relate to Y" questions, prefer
  `graphify query "..."`, `graphify path "A" "B"`, or `graphify explain "..."`
  over grep — they traverse extracted + inferred edges. If `graphify` is not on
  PATH, use `python3 -m graphify ...`.
- After modifying code files in a session, run `graphify update .` (AST-only,
  no API cost) to keep the graph current.

## Core abstractions (god nodes)

`EventWebSocketRiskEngine` · `ResearchConfig` · `read_dataset()` ·
`run_event_demo_cycle()` · `EventWebSocketRiskConfig` · `EventDemoCycleConfig`.

## Module map (`liquidity_migration/`)

- `cli.py` — argparse entry point, 16 subcommands.
- `volume_events.py` — active event-driven strategy: full-PIT gates, ledger,
  reports.
- `strategy_tribunal.py` — adversarial promotion court for completed reports.
- `champion_challenger.py` — demo champion + shadow-challenger manifest/audit.
- `event_demo.py` / `event_demo_daemon.py` — Bybit demo forward-cycle runner.
- `ws_risk.py` — websocket-first risk watchdog with REST fallback.
- `volume_features.py` / `feature_factory.py` — daily volume & liquidity-rank
  features; shadow research feature surface.
- `trade_lifecycle.py` — trade lifecycle, exits, baskets, equity helpers.
- `archive_manifest.py` — point-in-time manifest + 1h kline builders.
- `data_layer.py` / `ingestion.py` / `storage.py` — dataset read/write/audit.
- `bybit.py` / `binance.py` / `binance_vision.py` — venue clients and PIT
  proxy archives.
- `ic_diagnostic.py` — information-coefficient / multicollinearity diagnostics.
- `crowding.py` / `portfolio_hedge.py` — research-only crowding classifier and
  hedge overlay.
- `universe.py` / `downloaders.py` / `archive.py` — universe snapshots and
  archive download plumbing.
- `config.py` / `telegram.py` / `execution_router.py` — config, notifications,
  entry routing.

## Key docs

- `AGENTS.md` — repo rules.
- `docs/backtesting_errors_we_never_repeat.md` — mandatory methodology
  standard (see the backtest-integrity skill).
- `docs/research_findings.md` — current research verdict and refinement roadmap.
- `docs/data_roots.md` — canonical research / live demo / OOS root contract.
- `docs/system_status.md` — strategy / deployment status.
- `docs/event_demo_daemon.md` — demo forward-cycle daemon execution runbook.
- `README.md` — repo overview and status.

## Tests

`python -m pytest -q` (testpaths = `tests/`). CI runs the same on push and PR.
There is roughly one test module per source module — keep that pairing when
adding or changing modules.
