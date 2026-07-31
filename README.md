# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies, primarily on
Bybit. Every fill in every record in this repository is simulated: no code here
has ever placed an order against a mainnet account.

## Sleeves

| Sleeve | Profile | `deploy/sleeves.env` |
| --- | --- | --- |
| LONG | `LongV11aDivWeekendVol` | `LONG_SLEEVE=on` |
| CARRY | `lane2_carry_hold_v3` | `CARRY_SLEEVE=on` |
| CONTINUOUS | `continuous_ensemble_v2` | `CONTINUOUS_SLEEVE=off` — retired 2026-07-29 by owner override, no kill criterion tripped |
| paper target mirror | republishes the demo fleet's targets onto the paper route | `PAPER_TARGET_MIRROR=on` |

Paper sleeves for CONTINUOUS and CARRY, and both mainnet sleeves, are off.
`deploy/sleeves.env` is a ceiling: a host override can turn an enabled sleeve
off, never on.

## Layout

| Path | Contents |
| --- | --- |
| [`liquidity_migration/`](liquidity_migration) | package — strategy engines, account kernel, journals, venue adapters, CLI |
| [`scripts/`](scripts) | `dev.sh`, `ops.sh`, deploy, data builders, research screens |
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

`scripts/dev.sh` never contacts a venue. Operator commands are
`scripts/ops.sh help`; the research and data CLI is
`python -m liquidity_migration --help`. Python 3.11+.

## Documentation

| Doc | Covers |
| --- | --- |
| [STATE.md](STATE.md) | last recorded operating state and next action |
| [docs/plain_english_guide.md](docs/plain_english_guide.md) | the whole system without jargon — start here |
| [docs/operations.md](docs/operations.md) | `ops.sh` commands, deploy modes, unit topology |
| [docs/architecture.md](docs/architecture.md) | producers, account owner, journals, how a target becomes an order |
| [docs/trading_logic.md](docs/trading_logic.md) | what each sleeve trades and why |
| [docs/data.md](docs/data.md) | data roots, point-in-time boundaries, refresh workflow |
| [docs/research_findings.md](docs/research_findings.md) | what the evidence supports, including the negative results |
| [docs/strategy_program.md](docs/strategy_program.md) | active research queue |
| [docs/real_money.md](docs/real_money.md) | what real capital would require, and what is unbuilt |

## Standing rules

Working rules for agents live in [AGENTS.md](AGENTS.md) — read it before
changing anything.
