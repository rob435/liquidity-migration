# liquidity-migration

Research and demo execution for crypto-perpetual strategies, primarily on
Bybit.

## Sleeves

| Sleeve | Profile | `deploy/sleeves.env` |
| --- | --- | --- |
| LONG | `LongV12WideStop` | `LONG_SLEEVE=on` — switched from `LongV11aDivWeekendVol` 2026-08-03; standing v11a positions drain under their published terms ([detail](docs/trading_logic.md)) |
| CARRY | `lane2_carry_hold_v3` | `CARRY_SLEEVE=on` |
| CONTINUOUS | `continuous_ensemble_v2` | `CONTINUOUS_SLEEVE=off` — retired 2026-07-29 by owner override |

Both mainnet sleeves are off. The paper fleet (paper owner, paper producers,
target mirror) was retired 2026-08-03; demo is the only practice book.
`deploy/sleeves.env` is a ceiling: a host override can turn an enabled sleeve
off, never on.

## Layout

| Path | Contents |
| --- | --- |
| [`liquidity_migration/`](liquidity_migration/README.md) | the package, in eleven subpackages — `core`, `marketdata`, `data`, `account`, `venue`, `strategy`, `research`, `policy`, `ops`, `cli`, `runtime` |
| [`scripts/`](scripts/README.md) | `dev.sh` and `ops.sh` at the root; `runtime/`, `research/`, `maintain/`, `data/`, `vps/`, `devtools/` below |
| [`deploy/`](deploy) | `sleeves.env`, systemd units, environment handling |
| [`configs/`](configs) | Lane-2 strategy registrations and operational profiles |
| [`data/`](data) | per-sleeve event stores and reconciliation captures |
| [`reports/`](reports) | research-run outputs |
| [`tests/`](tests) | executable contracts |
| `.codex/skills/` | task runbooks; `.claude/skills/` is a mechanical mirror |

## Local gate

```
scripts/dev.sh doctor        # read-only Git/Python/dependency/skill diagnostic
scripts/dev.sh check         # doctor, then ruff, mypy, pytest
.venv/bin/python -m pytest -q
```

`scripts/dev.sh` runs offline. Operator commands are `scripts/ops.sh help`; the
research and data CLI is `python -m liquidity_migration --help`. Python 3.11+.

## Documentation

| Doc | Covers |
| --- | --- |
| [STATE.md](STATE.md) | last recorded operating state and next action |
| [docs/plain_english_guide.md](docs/plain_english_guide.md) | the whole system without jargon — start here |
| [docs/operations.md](docs/operations.md) | `ops.sh` commands, deploy modes, unit topology |
| [docs/notifications.md](docs/notifications.md) | the two Telegram channels, the hourly digest, watchdog alert cadence and escalation, the heartbeat dead-man's switch |
| [docs/architecture.md](docs/architecture.md) | producers, account owner, journals, how a target becomes an order |
| [docs/trading_logic.md](docs/trading_logic.md) | what each sleeve trades and why |
| [docs/carry_hold.md](docs/carry_hold.md) | the lead strategy in full: mechanism, tests, run rules, kill conditions |
| [docs/data.md](docs/data.md) | data roots, point-in-time boundaries, refresh workflow |
| [docs/research_findings.md](docs/research_findings.md) | what the evidence supports, including the negative results |
| [docs/governance.md](docs/governance.md) | the Progressive Evidence Model — two lanes, what makes a number real, promotion notes |
| [docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md) | the failure taxonomy |
| [docs/strategy_program.md](docs/strategy_program.md) | active research queue |
| [docs/real_money.md](docs/real_money.md) | the funded-account envelope, the owner's arming runbook, and what is still unproven |
| [docs/archive/](docs/archive/README.md) | dated research runs — the underlying tables behind a number |

Registered configs cite the archived runs by section;
`docs/research_findings.md` is the durable summary.

## Standing rules

Working rules for agents live in [AGENTS.md](AGENTS.md) — read it before
changing anything.
