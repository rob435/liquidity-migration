# liquidity-migration

Research and demo execution for crypto-perpetual strategies, mostly on
Bybit.

@AGENTS.md

CLAUDE.md is navigation and commands. AGENTS.md, imported above and read by
non-Claude agents too, is conduct — including the rule that everything is said
simply, without jargon.

## Read by purpose

**The system** — what runs and how to run it:

| Question | File |
| --- | --- |
| What is running right now | [STATE.md](STATE.md) |
| What changed, when, with receipts (dated log, newest first) | [CHANGELOG.md](CHANGELOG.md) |
| Which subpackage owns a module, and what may import what | [liquidity_migration/README.md](liquidity_migration/README.md) |
| Which script to run, and who runs it | [scripts/README.md](scripts/README.md) |
| Producers, account owner, journals, how a target becomes an order | [docs/architecture.md](docs/architecture.md) |
| The Rust execution engine: contracts, latency budget, safety posture | [docs/engine.md](docs/engine.md) |
| What each sleeve trades and where its evidence stops | [docs/trading_logic.md](docs/trading_logic.md) |
| Operator commands, deploy modes, unit topology | [docs/operations.md](docs/operations.md) |
| Telegram channels, watchdog alerts, heartbeat dead-man's switch | [docs/notifications.md](docs/notifications.md) |
| Data roots, timestamps, point-in-time membership, refresh | [docs/data.md](docs/data.md) |
| The funded account: envelope, arming runbook, what is unproven | [docs/operations.md](docs/operations.md) §Real money |

**The evidence** — research, all under `docs/research/`:

| Question | File |
| --- | --- |
| Active queue, current truth, and the measured-but-unrun theses | [strategy_program.md](docs/research/strategy_program.md) |
| What the evidence supports, including the negative results | [research_findings.md](docs/research/research_findings.md) |
| How evidence is graded, registered, and promoted | [governance.md](docs/research/governance.md) |
| Backtest failure modes we do not repeat | [backtesting_errors_we_never_repeat.md](docs/research/backtesting_errors_we_never_repeat.md) |
| The lead strategy in full | [carry_hold.md](docs/research/carry_hold.md) |
| Dated receipts and closed programs (the tables behind a number) | [archive/](docs/research/archive/README.md) |

Everything else: [README.md](README.md). Derive live state from these files;
never copy sleeve status or thresholds here.

## Commands

| Command | Does |
| --- | --- |
| `scripts/dev.sh doctor` | read-only Git, Python, dependency, skill, and deploy-env-toggle diagnostic (`--json` for tools) |
| `scripts/dev.sh check` | doctor, then Ruff, mypy, pytest, engine tests |
| `.venv/bin/python -m pytest -q` | tests |
| `.venv/bin/python -m ruff check liquidity_migration scripts tests` | lint |
| `cd engine && cargo test` | engine tests |
| `cd engine && cargo run --release -- bench` | engine benchmark: re-measures the latency table in [docs/engine.md](docs/engine.md) |
| `cd engine && cargo run --release -- fills --wal PATH` | what the trading cost: maker share, fee, arrival shortfall, markouts, per sleeve and symbol |
| `scripts/ops.sh help` | operator router: status, equity, reset, deploy, and the rest |
| `python -m liquidity_migration --help` | research and data CLI |

Before a push, run the focused tests, then `scripts/dev.sh check`.
