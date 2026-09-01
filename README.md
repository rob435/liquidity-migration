# liquidity-migration

Research and demo execution for crypto-perpetual strategies, primarily on
Bybit.

## The execution engine

[`engine/`](engine) is a Rust workspace that trades: one process, one thread,
one loop from market message to signed order. It **is the account owner,
deployed and live on the demo account** — it holds that account's
single-writer lease and it is the only order path. One credential-free Rust
signal worker per realm feeds the native LONG and CARRY reducers; Exodus is a
native consumer of CARRY's durable internal events. A second engine unit runs
on the funded account, and only while `REAL_MONEY` is armed in the host
credential file.

Measured by `cd engine && cargo run --release -- bench`, with real signing and
a real disk flush in the chain. Three native runs on the production box put
the decision at 80 ns median and market input through the parsed localhost
submit response at 1.26 ms median, 3.16 ms p99. This benchmark does not expose
a socket-write timestamp. (On a laptop the decision is faster and the flush
slower — 84 ns and 3.9 ms; both tables are in
[docs/engine.md](docs/engine.md).) Live
decision-to-acknowledgement is about 179 ms median, dominated by geography.

Each of the six venue families may write its hostnames in exactly one file, its own
`realm.rs`, and the funded gateway refuses to build unless `REAL_MONEY` is
armed in the host credential file. Design, crates and safety posture:
[docs/engine.md](docs/engine.md). Live truth: [STATE.md](STATE.md).

## Sleeves

| Sleeve | Profile | Toggle |
| --- | --- | --- |
| LONG | `LongV12WideStop` — native signal, sizing, entry, stop-decay, cooldown, and time-exit reducer; no take-profit | `LONG_SLEEVE` |
| CARRY | `carry_hold_v7_live_v1` — native daily scorer, sizing anchors, exit clocks, admission, and resize reducer | `CARRY_SLEEVE` |
| EXODUS SHORT | `lane2_exodus_short_v1` — native reducer consuming CARRY's WAL-backed pre-settlement events | always configured; events come only from CARRY |
| LONG / CARRY / EXODUS, real money | as above | `REAL_MONEY=true` in the host's `bybit-mainnet.env` — the single arming switch |

Which demo toggles are on is in [`deploy/sleeves.env`](deploy/sleeves.env), not
here. That file is a ceiling: a host override can turn an enabled sleeve off,
never on. Real money has no repo toggle at all — arming lives only on the host,
next to the live API key, so a git commit can never arm.
Demo is the only practice book. What each sleeve trades is in
[`docs/trading_logic.md`](docs/trading_logic.md).

The Rust `maker_canary` block remains present but disabled so its WAL identity
does not move. Its presence is not deployment evidence.

## Layout

| Path | Contents |
| --- | --- |
| [`liquidity_migration/`](liquidity_migration/README.md) | the Python research, evidence, policy, notification, and deployment-support plane; it has no live directional decision or order path |
| [`engine/`](engine) | the Rust execution engine workspace — seven crates, from the shared types to the loop |
| [`scripts/`](scripts/README.md) | `dev.sh` and `ops.sh` at the root; `runtime/`, `research/`, `maintain/`, `data/`, `vps/`, `devtools/` below |
| [`deploy/`](deploy) | the canonical fleet manifest, `sleeves.env`, systemd units, and environment handling |
| [`configs/`](configs) | Lane-2 strategy registrations and operational profiles |
| `data/` | per-sleeve event stores and reconciliation captures (runtime, not tracked) |
| `reports/` | research-run outputs (runtime, not tracked) |
| [`tests/`](tests) | executable contracts |
| `.codex/skills/` | task runbooks; `.claude/skills/` is a mechanical mirror |

## Local gate

```
scripts/dev.sh doctor        # read-only Git/Python/dependency/skill diagnostic
scripts/dev.sh check         # doctor, then ruff, shellcheck, mypy, pytest, engine fmt/clippy/tests
.venv/bin/python -m pytest -q
```

`scripts/dev.sh` runs offline. Operator commands are `scripts/ops.sh help`; the
research and data CLI is `python -m liquidity_migration --help`. Python 3.11+.

## Documentation

| Doc | Covers |
| --- | --- |
| [STATE.md](STATE.md) | the operational snapshot: what runs now and what constrains it |
| [CHANGELOG.md](CHANGELOG.md) | the dated operational log: deploys, incidents, repairs, change points |
| [docs/operations.md](docs/operations.md) | `ops.sh` commands, deploy modes, unit topology |
| [docs/notifications.md](docs/notifications.md) | the two Telegram channels, the pause/resume controls, watchdog alert cadence and escalation, the heartbeat dead-man's switch |
| [docs/architecture.md](docs/architecture.md) | the Rust signal, reducer, WAL, control, takeover, and execution seams |
| [docs/engine.md](docs/engine.md) | the Rust execution engine: crate contracts, latency budget, crash safety, safety posture |
| [docs/strategy_template.md](docs/strategy_template.md) | the required shape, durability order, replay seam, and tests for a native strategy |
| [docs/trading_logic.md](docs/trading_logic.md) | what each sleeve trades and why |
| [docs/data.md](docs/data.md) | data roots, point-in-time boundaries, refresh workflow |
| [docs/research/research_findings.md](docs/research/research_findings.md) | what the evidence supports, including the negative results |
| [docs/research/governance.md](docs/research/governance.md) | the Progressive Evidence Model — two lanes, what makes a number real, promotion notes |
| [docs/research/backtesting_errors_we_never_repeat.md](docs/research/backtesting_errors_we_never_repeat.md) | the failure taxonomy |
| [liquidity_migration/README.md](liquidity_migration/README.md) | which subpackage owns a module, and what may import what |
| [scripts/README.md](scripts/README.md) | which script to run, and who runs it |
| [docs/operations.md](docs/operations.md) §Real money | the funded-account envelope, the owner's arming runbook, and what is still unproven |

`docs/research/research_findings.md` is the durable evidence summary.

## Standing rules

Working rules for agents live in [AGENTS.md](AGENTS.md) — read it before
changing anything.
