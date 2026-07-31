# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies, primarily on
Bybit.

## Sleeves

| Sleeve | Profile | `deploy/sleeves.env` |
| --- | --- | --- |
| LONG | `LongV11aDivWeekendVol` | `LONG_SLEEVE=on` |
| CARRY | `lane2_carry_hold_v3` | `CARRY_SLEEVE=on` |
| CONTINUOUS | `continuous_ensemble_v2` | `CONTINUOUS_SLEEVE=off` — retired 2026-07-29 by owner override |
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
| [docs/demo_paper_convergence_plan.md](docs/demo_paper_convergence_plan.md) | in-flight demo/paper convergence work |
| [docs/real_money.md](docs/real_money.md) | the funded-account envelope, the owner's arming runbook, and what is still unproven |

Dated research notes ([anomaly_research_2026-07-24](docs/anomaly_research_2026-07-24.md),
[continuous_ladder_mechanism_2026-07-27](docs/continuous_ladder_mechanism_2026-07-27.md),
[research_2026-07-26_financed_longs](docs/research_2026-07-26_financed_longs.md),
[research_2026-07-28_carry_hold_quant_review](docs/research_2026-07-28_carry_hold_quant_review.md),
[research_2026-07-30_idio_charts](docs/research_2026-07-30_idio_charts.md)) hold
the underlying runs. Registered configs cite them by section;
`docs/research_findings.md` is the durable summary.

## Standing rules

Working rules for agents live in [AGENTS.md](AGENTS.md) — read it before
changing anything.
