# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies, mostly on
Bybit.

@AGENTS.md

## Read by purpose

| Question | File |
| --- | --- |
| What is running right now | [STATE.md](STATE.md) |
| The system without jargon | [docs/plain_english_guide.md](docs/plain_english_guide.md) |
| Which subpackage owns a module, and what may import what | [liquidity_migration/README.md](liquidity_migration/README.md) |
| Which script to run, and who runs it | [scripts/README.md](scripts/README.md) |
| Producers, account owner, journals, how a target becomes an order | [docs/architecture.md](docs/architecture.md) |
| What each sleeve trades and where its evidence stops | [docs/trading_logic.md](docs/trading_logic.md) |
| Operator commands, deploy modes, unit topology | [docs/operations.md](docs/operations.md) |
| Telegram channels, watchdog alerts, heartbeat dead-man's switch | [docs/notifications.md](docs/notifications.md) |
| Data roots, timestamps, point-in-time membership, refresh | [docs/data.md](docs/data.md) |
| The funded account: envelope, arming runbook, what is unproven | [docs/real_money.md](docs/real_money.md) |
| What the evidence supports, including the negative results | [docs/research_findings.md](docs/research_findings.md) |
| Ideas that work and still are not run, and what disqualifies each | [docs/research_theses.md](docs/research_theses.md) |
| How evidence is graded, registered, and promoted | [docs/governance.md](docs/governance.md) |
| Backtest failure modes we do not repeat | [docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md) |
| The lead strategy in full | [docs/carry_hold.md](docs/carry_hold.md) |
| Active research queue | [docs/strategy_program.md](docs/strategy_program.md) |
| The price pattern around funding payments, and why the carry book cannot be hedged | [docs/settlement_sawtooth_program.md](docs/settlement_sawtooth_program.md) |
| Everything else | [README.md](README.md) |

Dated research runs live in [`docs/archive/`](docs/archive/README.md) — the
underlying tables behind a number. `docs/research_findings.md` is the durable
summary; in-flight work is recorded in `STATE.md`.

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
