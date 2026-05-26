# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.
A long-only sleeve (`MultiStratV1` / v11a, FOMO-chase) runs alongside on
demo.

## Status: research-evidence reset (2026-05-27) + live demo running

The per-venue full-PIT research roots and all backtest reports under them
were deleted on 2026-05-27. The signal idea, engine code, demo runners,
and live VPS deployment are unchanged; the supporting numerical evidence
needs to be re-generated against a rebuilt data root before it can be
cited. See [docs/research_findings.md](docs/research_findings.md) for what
is and is not currently substantiated, and
[docs/system_status.md](docs/system_status.md) for the deployment record.

A demo (paper) forward test of the 3-position concentrated `promoted`
short profile + the v11a long sleeve runs on a Bybit demo account on a
VPS — that demo is the actual forward out-of-sample test. No real-money
trading is active: a real-money execution path exists in the code but
the account is a plain `.env` toggle (`DEMO` / `REAL_MONEY`, mutually
exclusive) that defaults to demo. The strategy is not validated for real
money.

## What the repo contains

- `liquidity_migration/` — Python package: data ingestion, point-in-time
  archive builders, the backtest / event engine, and the
  `python -m liquidity_migration` CLI (17 subcommands; run `--help`).
- `tests/` — `.venv/bin/python -m pytest -q`.
- `docs/research_findings.md` — current research-evidence status (post-reset).
- `docs/backtesting_errors_we_never_repeat.md` — research methodology standard.
- `docs/data_roots.md` — data-root contract (research / live demo / OOS).
- `docs/system_status.md` — strategy / deployment status.
- `docs/event_demo_daemon.md` — demo forward-cycle daemon execution runbook.
- `.claude/` — Claude Code skills and an MCP server for working in this repo.
- `AGENTS.md` — repo rules.

Python 3.11+.
