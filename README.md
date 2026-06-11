# liquidity-migration

A research codebase for Bybit crypto-perp trading research. Two systems remain:
the CONTINUOUS fade book (the live demo system — a rolling intraday
liquidity-migration fade ensemble with a BTC+ETH hedge) and the LONG-only
sleeve (`MultiStratV1` / v11a, FOMO-chase), promoted-in-code but currently
toggled off on the live box; see [STATE.md](STATE.md).

**The original daily SHORT strategy was ERASED from the system 2026-06-11 by
operator order** (engine, daemon, CLI, deploy units, scripts, tests, docs); git
history is the archive.

## Status: research-stage — live demo running

Nothing is promoted to real money; the Bybit demo forward test is the arbiter.
Dated numbers + full record (the one research file):
[docs/research_summary.md](docs/research_summary.md); live state + what's next:
[STATE.md](STATE.md).

A Bybit demo account on a VPS hosts the forward tests. As of 2026-06-09 the box
runs only the continuous sleeve (research-stage, demo orders + paper shadow) plus
the risk engine and the hedge timer; the v11a long sleeve is toggled off in
`deploy/sleeves.env` but remains promoted-in-code and redeployable. Forward demo is the arbiter; clocks restarted
2026-06-09. No real-money trading is active: a real-money execution path exists in the code but
the account is a plain `.env` toggle (`DEMO` / `REAL_MONEY`, mutually
exclusive) that defaults to demo. The strategy is not validated for real
money.

## What the repo contains

- `liquidity_migration/` — Python package: data ingestion, point-in-time
  archive builders, the backtest / event engine, and the
  `python -m liquidity_migration` CLI (run `--help` for the subcommand list).
- `tests/` — `.venv/bin/python -m pytest -q`.
- `docs/research_summary.md` — THE consolidated research record (results, verdicts,
  open methodology debts, decision rules). All per-arc docs/scripts fold in here.
- `docs/backtesting_errors_we_never_repeat.md` — research methodology standard.
- `docs/data_roots.md` — data-root contract (research / live demo / OOS).
- `docs/event_demo_daemon.md` — demo forward-cycle daemon runbook + infra-hardening changelog.
- `.claude/` — Claude Code skills and an MCP server for working in this repo.
- `AGENTS.md` — repo rules.

Python 3.11+.
