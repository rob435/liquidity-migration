# Repository map

This is a stable navigation aid, not an operational snapshot. For implemented
behavior, prefer current code, tests, deploy files, and generated artifacts.
For deployed state, read `STATE.md`. For evidence claims, follow
`docs/governance.md`.

## Start here

Run `scripts/dev.sh doctor` before broad work. It reports the selected Python,
Git state, exact-lock drift, project-skill mirror integrity, and Graphify
availability without contacting a venue or changing the repository. Use
`scripts/dev.sh doctor --json` when another tool will consume the result.

The package is deliberately navigated by ownership rather than filename alone.
Its flat module layout has extensive cross-module contracts; a physical move is
a separate refactor that must trace imports, runtime entry points, persistence,
deploy references, and tests.

## Top-level ownership

| Path | Owns | Does not establish |
| --- | --- | --- |
| `liquidity_migration/` | Strategy, data, account, execution, and CLI implementation | Deployed state or research validity |
| `tests/` | Executable behavioral and safety contracts | Live venue evidence |
| `scripts/` | Developer, operator, data, and reporting entry points | Authority beyond each script's explicit handshake |
| `deploy/` | Systemd topology and strict runtime environment handling | Mainnet authorization |
| `docs/` | Governance, contracts, operations, and evidence interpretation | Implemented behavior when code disagrees |
| `configs/` | Versioned research defaults | Runtime overrides |
| `data/`, `backtest-runs/`, `reports/` | Local data and generated evidence | Promotion or deployment authority |
| `.codex/skills/` | Canonical project workflows | Current status or factual authority |
| `.claude/skills/` | Mechanical mirror of project workflows | An independent source of policy |
| `graphify-out/` | Derived relationship map | Source-level proof |

## Change domains

| Domain | Primary implementation | Contract and tests |
| --- | --- | --- |
| Package CLI and research data | `cli.py`, `cli_parsers.py`, `config.py`, `storage.py`, `ingestion.py`, `downloaders.py`, `archive*.py`, venue download modules | `test_liquidity_migration_cli.py`, storage/download/archive tests, `docs/data_roots.md`, `docs/pit_gate.md` |
| LONG target production | `long_native.py`, `long_native_event_demo.py`, `long_native_event_demo_daemon.py`, `strategy_targets.py` | LONG profile, event-cycle, daemon, and target tests; `docs/active_trading_logic.md` |
| CONTINUOUS target production | `continuous_demo.py`, `continuous_demo_daemon.py`, `continuous_events.py`, `continuous_profile.py`, `continuous_rebalance.py`, `continuous_cycle_status.py` | CONTINUOUS profile, cycle, daemon, event, rebalance, and receipt-bound status tests; `docs/active_trading_logic.md` |
| Shared strategy-to-account boundary | `strategy_runtime.py`, `strategy_planning.py`, `strategy_targets.py`, `account_intent_client.py`, `account_service.py`, `account_route.py` | Strategy runtime/target, intent client, account service, and route tests; `docs/account_execution.md` |
| Account state and accounting | `account_contracts.py`, `account_kernel.py`, `account_strategy_state.py`, `account_reconcile.py`, `account_venue_accounting.py`, `historical_account_replay.py` | Kernel, journal, strategy-state, reconciliation, accounting, and replay tests; `docs/account_journal.md`, `docs/timestamp_glossary.md` |
| Venue execution boundary | `account_service_bybit.py`, `account_execution_stream.py`, `bybit_execution_adapter.py`, `bybit_market_data.py`, `execution_adapters.py` | Account-service Bybit, execution-stream, market-data-boundary, and adapter-facing tests |
| Market capture and liveness | `market_capture.py`, `ws_state_cache.py`, `account_owner_health.py`, `account_owner_readiness.py`, `strategy_cycle_health.py`, `run_diagnostics.py` | Capture, cache, owner health/readiness, strategy-completion, diagnostics, and liveness-script tests |
| Operations and deployment | `scripts/ops.sh`, guarded runtime/deploy scripts, `deploy/systemd/`, `.github/workflows/vps-deploy.yml` | Runtime-script and ops tests; `docs/operations.md`, `deploy/systemd/README.md`, `STATE.md` |
| Research integrity and reporting | Research modules, `scripts/equity_curves.*`, `scripts/research_refresh.*`, `three_way_reconciliation.py`, active contracts, and raw run artifacts | `docs/governance.md`, `docs/strategy_program.md` (single current program), `docs/research_refresh.md`, `docs/preregistration/INDEX.md`, applicable research skills and report tests |
| Trade diagnostics | Verified account journal, exact decision/markout books, and the read-only command projection | `trade_diagnostics.py`, `post_fill_markouts.py`, bounded capture hooks, `scripts/build_trade_diagnostics.py`, `docs/trade_diagnostics.md`, and focused diagnostic tests |
| Strategy-source diagnostics | Observer-only LONG/CONTINUOUS source gates, separated future-path labels, and registered exploratory analysis | `strategy_funnel.py`, hooks at the existing strategy owners, `scripts/build_candidate_tape.py`, the V2 contracts/receipt, and focused tests |

Prefixes are a discovery aid, not proof of ownership. Open the definition,
callers, persistence format, runtime configuration, and focused tests before a
cross-domain edit.

## Entry points

- Developer-only checks: `scripts/dev.sh help`.
- Safe operator router: `scripts/ops.sh help`.
- Package CLI: `python -m liquidity_migration --help`, then the selected
  subcommand's help.
- Demo and paper services: the matching systemd unit, its launcher in
  `scripts/`, and the Python runner named by that launcher.
- Research runs and equity curves: use the applicable project skill before
  constructing a command or interpreting output. Use
  `scripts/ops.sh research-refresh plan` for the combined append/benchmark
  workflow and inspect its frozen boundary before `run`.
- Research handoff: `docs/next_agent_prompt.md` launches open-ended anomaly
  discovery; `docs/strategy_program.md` remains the only status authority.
- Registered candidate-tape partitions: `scripts/build_candidate_tape.py --help`;
  this entry point reads only the preregistered PIT root and writes one run-scoped
  diagnostic partition.
- Cross-venue anomaly research: `liquidity_migration/cross_venue_panel.py` builds
  the causal panel via `scripts/build_cross_venue_panel.py`;
  `liquidity_migration/cross_section.py` holds the scoring primitives. Findings
  are consolidated in `docs/anomaly_research_2026-07-24.md`.
- Deleted on 2026-07-24, recoverable from Git history: the V2 aggregate analyser,
  the prospective full-ledger replay runner, the `research_data_snapshot`
  script/module/tests (zero references), and `unit_numeric_comparison`. All
  Strategy Overhaul V2 contracts and baselines, the
  `reports/autonomous-improvement/` cycle reports, and `docs/incidents/` went in
  the same pass. `scripts/build_candidate_tape.py` no longer hashes a contract;
  its `--contract` / `--base-contract` arguments were removed with the file they
  defaulted to.
- **The whole prospective runtime-parity epoch was deleted on 2026-07-24 by owner
  instruction**: `run_active_runtime_comparator.py`,
  `verify_integrated_runtime_comparator.py`, `active_runtime_comparator.py`,
  `freeze_forward_epoch_start.py`, `forward_epoch_start.py`, `venue_lifecycle.py`,
  their tests, and all eight registered contracts. Published receipts still exist
  on disk but nothing here reads or reproduces them. The forward stream is now
  just the rolling record under `docs/governance.md`.
- `docs/preregistration/` now holds only contracts with a live executable form
  (sleeve kill criteria, passive execution). A contract with no verifier is
  history, and history lives in Git.
- Context for the whole pass: `docs/audit/2026-07-24-repo-and-strategy-audit.md`.

## Validation ladder

Use the narrowest check that can falsify the change, then widen in proportion
to its consequences:

1. `scripts/dev.sh test tests/test_relevant_file.py`
2. `scripts/dev.sh lint`
3. `scripts/dev.sh types` for package changes
4. `scripts/dev.sh check` for the full local gate
5. Claim-specific research, PIT, accounting, or deployment checks when the
   conclusion depends on them

`requirements.lock` is the exact CI and deploy dependency contract. A local
environment may be usable while differing from it; `doctor --strict-lock`
turns that difference into a failing diagnostic when exact parity matters.

## Graph navigation

For broad dependency questions, inspect `graphify-out/GRAPH_REPORT.md`, then use
`graphify query`, `graphify path`, or `graphify explain`. Verify every material
edge against current source and tests. Update Graphify only after an
architecture-affecting change and only when doing so will not overwrite
unrelated graph work.
