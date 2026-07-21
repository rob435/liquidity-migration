# liquidity-migration

Research plus demo/paper execution for two crypto-perpetual profiles:

- `continuous_ensemble_v2`
- `LongV11aDivWeekendVol`

The executable runtime is demo/paper only. Mainnet and `REAL_MONEY` remain
unauthorized unless the owner gives a separate, narrow instruction naming the
deployment and risk boundary.

## Read first

- [STATE.md](STATE.md) — last recorded operating state and next action.
- [docs/account_execution.md](docs/account_execution.md) — execution ownership
  and operational authorization.
- [docs/account_journal.md](docs/account_journal.md) — authoritative account
  transaction/event storage.
- [docs/operations.md](docs/operations.md) — supported operator commands.
- [docs/active_trading_logic.md](docs/active_trading_logic.md) — active
  strategy profiles and reconstruction limits.
- [docs/governance.md](docs/governance.md) — evidence and authorization policy.
- [docs/strategy_program.md](docs/strategy_program.md) — consolidated evidence,
  current direction, and the only active strategy-research queue.
- [docs/next_agent_prompt.md](docs/next_agent_prompt.md) — concise transferable
  launcher for open-ended anomaly research; not a second roadmap.
- [docs/data_roots.md](docs/data_roots.md) and
  [docs/pit_gate.md](docs/pit_gate.md) — research-data boundaries.
- [docs/research_refresh.md](docs/research_refresh.md) — append-first data,
  benchmark, resume, and demo/paper/backtest reconciliation workflow.
- [docs/repository_map.md](docs/repository_map.md) — subsystem ownership,
  entry points, and validation paths.

## Layout

- `liquidity_migration/` — package, strategies, account kernel, journals, and CLI.
- `scripts/` — supported operations, deploy, data, and reporting entry points.
- `deploy/` — systemd topology and strict environment handling.
- `tests/` — executable contracts.
- `.codex/skills/` — canonical project workflows; `.claude/skills/` mirrors them.

## Developer workflow

Use `scripts/dev.sh doctor` for a read-only environment/worktree diagnostic and
`scripts/dev.sh check` for the full local Ruff, mypy, and pytest gate. These
commands never contact a venue or grant operational authority. Operator and
research commands remain under `scripts/ops.sh` and the task-specific skills.
Coordination locks and reset receipts do not by themselves grant operational or
mainnet authority; their exact limits are in `docs/operations.md`.

Plan the current research refresh with
`scripts/ops.sh research-refresh plan --end YYYY-MM-DD`; replace `plan` with
`run` only after checking the printed end-exclusive boundary and data mode.

Python 3.11+.
