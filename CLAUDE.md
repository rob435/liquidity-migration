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
| The equity curve on the host, what is sampled every minute, Grafana Cloud | [docs/observability.md](docs/observability.md) |
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
| `scripts/dev.sh check` | doctor, then Ruff, ShellCheck, mypy, pytest, and the engine's rustfmt, clippy, and tests; prunes the workspace crates' build artifacts first when the target volume has under `LM_TARGET_FREE_GIB` (30) GiB free |
| `scripts/dev.sh prune` | `cargo clean --profile dev -p` every workspace member and drop `target/debug/incremental`: the tens of GiB repeated gates pile up, with the dependency builds kept |
| `.venv/bin/python -m pytest -q` | tests |
| `.venv/bin/python -m ruff check liquidity_migration scripts tests` | lint |
| `cargo test --manifest-path engine/Cargo.toml --workspace --locked` | engine tests |
| `cargo build --manifest-path engine/Cargo.toml --release --locked -p engine-tools --bins` | build the runtime and companion tools used below |
| `engine/target/release/engine-tools bench [--contention [--quota]] [--json]` | the real loop on this box against a local stand-in venue: decide, durable, wire, ack and end-to-end at p50/p99. Our side of the wire, not the venue's. `--contention` measures what a risk-off cancel waits for behind slow openings and how many openings the dispatch TTL refused unsent; `--quota` gives that venue a local request quota of MEXC's shape (16 signed requests per 2 s, 4 reserved for risk-off) with a 5 ms venue, and pulls every resting order every 50 quotes so risk-off arrives while openings are still queued: the wait a command serves shows up as its queue wait, not inside its call; `--json` prints the result as JSON |
| `engine/target/release/engine-tools wal-cost --wal PATH` | what one append and one durability barrier cost on the filesystem holding PATH: the storage's share of the order path |
| `engine/target/release/engine-tools latency --wal PATH` | how long each step of the order path took, per operation, at p50/p90/p99/p99.9: the venue's round trip, the engine's own work, and the time it held a command back to stay inside the request quota, as separate numbers |
| `engine/target/release/engine-tools fills --wal PATH` | what the trading cost and what the positions made: maker share, fee, arrival shortfall, markouts, and closed round trips with their P&L |
| `engine/target/release/engine-tools cohort --wal PATH [--json]` | every opportunity in the log, not only the orders that filled: source rows and order decisions each split into admitted, rejected, expired and unresolved, every record counted once with the totals proved; a decision's own cause joins its source row, so the report follows rows to intents, orders, the wire and fills, and a stamp a record does not carry is a named count, never a zero |
| `engine/target/release/engine-tools backtest --config PATH --tape PATH --instruments PATH --wal PATH` | embedded reducers on a recorded `market_tape`, in the tape's time, on a simulated venue: [docs/engine.md](docs/engine.md) §9 |
| `engine/target/release/engine-tools sim --seed N [--seeds K] [--crashes C] [--faults light] [--twice] [--strategies quoter\|demo\|mainnet\|mexc\|hyperliquid] [--hours H] [--shock on\|off]` | embedded reducers on a seeded synthetic market with venue, private-stream, feed and signal faults, a market shock and process deaths, judged against the venue's books, the sleeves' own health and the log at the end; `--strategies` runs a deployed template's own strategy blocks against a synthetic signal producer, `quoter` is the market maker alone; one seed is one run, byte for byte: [docs/engine.md](docs/engine.md) §10 |
| `python scripts/research/run_engine_backtest.py --config PATH --tape PATH --instruments PATH --out-dir DIR` | runs `engine backtest` and reads its report, trades, and equity back as research metrics |
| `python -m liquidity_migration.research.day_reconciliation --realm R --day D --wal PATH --capture PATH --equity-samples DIR --out PATH` | one account's whole UTC day between two equity-recorder boundary samples and the venue's own transaction log: every cash row by the venue's `type`, the residual preserved rather than forced to zero, and `gate: pass\|fail` with its reasons: [docs/operations.md](docs/operations.md) §Observed production-day reconstruction |
| `scripts/ops.sh curve [REALM] [SAMPLES]` | the live account's recorded equity curve, read on the host: [docs/observability.md](docs/observability.md) |
| `scripts/ops.sh help` | operator router: status, units, logs, restart/stop/start, flatten, attest-flat, real-money, deploy |
| `python -m liquidity_migration --help` | research and data CLI |
| `python -m market_tape --help` | the market tape: check a capture config, record, pack, list hours, read rows, build bars, rebuild a book |
| `python -m liquidity_migration.research.lab.cli dump\|panel` | the study harness: dump the point-in-time inputs once, then build the daily panel every study reads |

Before a push, run the focused tests, then `scripts/dev.sh check`.
