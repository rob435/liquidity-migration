# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies, mostly on
Bybit. Every fill in every record is simulated: no code here has ever made a
mainnet API call.

@AGENTS.md

## Read by purpose

| Question | File |
| --- | --- |
| What is running right now | [STATE.md](STATE.md) |
| The system without jargon | [docs/plain_english_guide.md](docs/plain_english_guide.md) |
| Producers, account owner, journals, how a target becomes an order | [docs/architecture.md](docs/architecture.md) |
| What each sleeve trades and where its evidence stops | [docs/trading_logic.md](docs/trading_logic.md) |
| Operator commands, deploy modes, unit topology | [docs/operations.md](docs/operations.md) |
| Active research queue | [docs/strategy_program.md](docs/strategy_program.md) |
| Everything else | [README.md](README.md) |

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
