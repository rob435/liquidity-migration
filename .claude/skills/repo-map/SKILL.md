---
name: repo-map
description: "Orient in the liquidity-migration codebase: module map, core abstractions, key docs, and the graphify knowledge-graph workflow. Use when answering architecture or where-does-X-live questions, navigating modules, tracing how modules relate, or before cross-module changes."
---

# Repo map

A Bybit (+Binance) research codebase for a liquidity-migration short strategy. The
strategy is under active research — its signal is statistically real but
regime-conditional. The current focus is the **Round 2 integrated-strategy program**
(see `STATE.md` and `docs/research_summary.md`;
`docs/research_findings.md` is the older research verdict). Direction: progressive —
moving toward a lowest-latency, fully-event-driven, continuous-signal (Architecture B)
system; the deployed daily-close signal is Architecture A.

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

- `cli.py` — argparse entry point, 24 subcommands.
- `volume_events.py` — active event-driven strategy: full-PIT gates, ledger,
  reports.
- `strategy_tribunal.py` — adversarial promotion court for completed reports.
- `event_demo.py` / `event_demo_daemon.py` — Bybit demo forward-cycle runner.
- `ws_risk.py` — websocket-first risk watchdog with REST fallback.
- `volume_features.py` / `feature_factory.py` — daily volume & liquidity-rank
  features; shadow research feature surface.
- `trade_lifecycle.py` — trade lifecycle, exits, baskets, equity helpers.
- `archive_manifest.py` — point-in-time manifest + 1h kline builders.
- `data_layer.py` / `ingestion.py` / `storage.py` — dataset read/write/audit.
- `bybit.py` / `binance.py` / `binance_vision.py` — venue clients and PIT
  proxy archives.
- `crowding.py` / `portfolio_hedge.py` — research-only crowding classifier and
  hedge overlay.
- `risk_model.py` — R4 JS-style factor model: factor panel + per-day cross-sectional
  factor-return fit + per-trade residual-P&L decomposition (Tier-3 residual-Sharpe input).
- `cost_model.py` — R6 per-name/per-bar cost model: surface + OLS fit + per-trade
  predict + ledger recosting; default honest 15 bps taker.
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
- `docs/system_status.md` — strategy / deployment status.
- `docs/event_demo_daemon.md` — demo forward-cycle daemon execution runbook.
- `README.md` — repo overview and status.

## Tests

`python -m pytest -q` (testpaths = `tests/`). CI runs the same on push and PR.
There is roughly one test module per source module — keep that pairing when
adding or changing modules.
