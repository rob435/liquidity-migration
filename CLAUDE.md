# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies, mostly on
Bybit.

@AGENTS.md

## Read by purpose

| Question | File |
| --- | --- |
| What is running right now | [STATE.md](STATE.md) |
| The system without jargon | [docs/plain_english_guide.md](docs/plain_english_guide.md) |
| Producers, account owner, journals, how a target becomes an order | [docs/architecture.md](docs/architecture.md) |
| What each sleeve trades and where its evidence stops | [docs/trading_logic.md](docs/trading_logic.md) |
| Operator commands, deploy modes, unit topology | [docs/operations.md](docs/operations.md) |
| Telegram channels, watchdog alerts, heartbeat dead-man's switch | [docs/notifications.md](docs/notifications.md) |
| Data roots, timestamps, point-in-time membership, refresh | [docs/data.md](docs/data.md) |
| The funded account: envelope, arming runbook, what is unproven | [docs/real_money.md](docs/real_money.md) |
| What the evidence supports, including the negative results | [docs/research_findings.md](docs/research_findings.md) |
| How evidence is graded, registered, and promoted | [docs/governance.md](docs/governance.md) |
| Backtest failure modes we do not repeat | [docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md) |
| The lead strategy in full | [docs/carry_hold.md](docs/carry_hold.md) |
| Active research queue | [docs/strategy_program.md](docs/strategy_program.md) |
| Everything else | [README.md](README.md) |

Dated research notes (`docs/anomaly_research_2026-07-24.md`,
`docs/continuous_ladder_mechanism_2026-07-27.md`,
`docs/research_2026-07-26_financed_longs.md`,
`docs/research_2026-07-28_carry_hold_quant_review.md`,
`docs/research_2026-07-30_idio_charts.md`) are the underlying runs;
`docs/research_findings.md` is the durable summary. In-flight plans live in
`docs/demo_paper_convergence_plan.md`.

Derive live state from those files; never copy sleeve status or thresholds here.

## Commands

| Command | Does |
| --- | --- |
| `scripts/dev.sh doctor` | read-only Git, Python, dependency, and skill diagnostic (`--json` for tools) |
| `scripts/dev.sh check` | doctor, then Ruff, mypy, pytest |
| `.venv/bin/python -m pytest -q` | tests |
| `.venv/bin/python -m ruff check liquidity_migration scripts tests` | lint |
| `scripts/ops.sh help` | operator router: status, equity, reset, deploy, and the rest |
| `python -m liquidity_migration --help` | research and data CLI |

Before a push, run the focused tests, then `scripts/dev.sh check`.
