# liquidity-migration

Research and demo execution for crypto-perpetual strategies, mostly on
Bybit.

@AGENTS.md

CLAUDE.md is navigation and commands. AGENTS.md, imported above and read by
non-Claude agents too, is conduct — including the rule that everything is written
in the token-efficient, Spec-First structured format without narrative padding.

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
| The market tape: recorders, Drive archives, loader, book rebuild, bars | [market_tape/README.md](market_tape/README.md) |
| The study harness: panel, backtester, overlay, plateau checks, evidence note | [liquidity_migration/research/lab/](liquidity_migration/research/lab/) |
| The funded account: envelope, arming runbook, what is unproven | [docs/operations.md](docs/operations.md) §Real money |

**The evidence** — research, all under `docs/research/`:

| Question | File |
| --- | --- |
| What the evidence supports, including the negative results | [research_findings.md](docs/research/research_findings.md) |
| How evidence is graded, registered, and promoted | [governance.md](docs/research/governance.md) |
| Backtest failure modes we do not repeat | [backtesting_errors_we_never_repeat.md](docs/research/backtesting_errors_we_never_repeat.md) |

Everything else: [README.md](README.md). Derive live state from these files;
never copy sleeve status or thresholds here.

## Commands

| Command | Does |
| --- | --- |
| `scripts/dev.sh doctor` | read-only Git, Python, dependency, skill, and deploy-env-toggle diagnostic (`--json` for tools) |
| `scripts/dev.sh check` | doctor, then Ruff, ShellCheck, mypy, pytest, and the engine's rustfmt, clippy, and tests |
| `.venv/bin/python -m pytest -q` | tests |
| `.venv/bin/python -m ruff check liquidity_migration scripts tests` | lint |
| `cd engine && cargo test` | engine tests |
| `cd engine && cargo run --release -- bench` | engine benchmark: re-measures the latency table in [docs/engine.md](docs/engine.md) |
| `cd engine && cargo run --release -- wal-cost --wal PATH` | what one append and one durability barrier cost on the filesystem holding PATH: the storage's share of the order path |
| `cd engine && cargo run --release -- latency --wal PATH` | how long each step of the order path took, per operation, at p50/p90/p99/p99.9: the venue's round trip, the engine's own work, and the time it held a command back to stay inside the request quota, as separate numbers |
| `cd engine && cargo run --release -- fills --wal PATH` | what the trading cost and what the positions made: maker share, fee, arrival shortfall, markouts, and closed round trips with their P&L |
| `cd engine && cargo run --release -- backtest --config PATH --tape PATH --instruments PATH --wal PATH` | the live loop on a recorded `market_tape`, in the tape's time, on a simulated venue: [docs/engine.md](docs/engine.md) §8 |
| `python scripts/research/run_engine_backtest.py --config PATH --tape PATH --instruments PATH --out-dir DIR` | runs `engine backtest` and reads its report, trades, and equity back as research metrics |
| `scripts/ops.sh help` | operator router: status, equity, reset, deploy, and the rest |
| `python -m liquidity_migration --help` | research and data CLI |
| `python -m market_tape --help` | the market tape: check a capture config, record, pack, list hours, read rows, build bars, rebuild a book |
| `python -m liquidity_migration.research.lab.cli dump\|panel` | the study harness: dump the point-in-time inputs once, then build the daily panel every study reads |

Before a push, run the focused tests, then `scripts/dev.sh check`.
