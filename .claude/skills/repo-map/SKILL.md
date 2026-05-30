---
name: repo-map
description: "Orient in the liquidity-migration codebase: module map, core abstractions, key docs, and the graphify knowledge-graph workflow. Use when answering architecture or where-does-X-live questions, navigating modules, tracing how modules relate, or before cross-module changes."
---

# Repo map

A Bybit (+Binance) research codebase for a liquidity-migration short strategy. The
strategy is under active research — its signal is statistically real but
regime-conditional. The current focus is the **selection-vs-execution research plan**
(see `STATE.md`, `docs/research_summary.md`, and
`docs/research_plan_selection_execution.md`). Direction: progressive — the deployed
daily-close signal is positive in-sample under realistic fills; the open lead is the
fade-confirmation execution layer (plus a continuous candidate signal under research).

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

- `cli.py` — argparse entry point, 20 subcommands.
- `volume_events.py` — active event-driven strategy: full-PIT gates, ledger,
  reports.
- `event_demo.py` / `event_demo_daemon.py` — Bybit demo forward-cycle runner.
- `ws_risk.py` — websocket-first risk watchdog with REST fallback.
- `volume_features.py` — daily volume & liquidity-rank features.
- `trade_lifecycle.py` — trade lifecycle, exits, baskets, equity helpers.
- `archive_manifest.py` — point-in-time manifest + 1h kline builders.
- `data_layer.py` / `ingestion.py` / `storage.py` — dataset read/write/audit.
- `bybit.py` / `binance.py` / `binance_vision.py` — venue clients and PIT
  proxy archives.
- `crowding.py` — research-only crowding classifier.
- `risk_model.py` — R4 JS-style factor model: factor panel + per-day cross-sectional
  factor-return fit + per-trade residual-P&L decomposition (Tier-3 residual-Sharpe input).
- `kline_store.py` / `kline_stream_manager.py` / `ws_state_cache.py` — WS-driven kline
  store + stream manager + private/ticker state caches (the event-driven runtime).
- `signal_harness.py` — daily-aggregation + cross-sectional feature/IC research harness.
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
- `docs/event_demo_daemon.md` — demo forward-cycle daemon runbook + infra-hardening changelog.
- `README.md` — repo overview and status.

## Tests

`python -m pytest -q` (testpaths = `tests/`). CI runs the same on push and PR.
There is roughly one test module per source module — keep that pairing when
adding or changing modules.
