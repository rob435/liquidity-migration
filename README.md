# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.
A long-only sleeve (`MultiStratV1` / v11a, FOMO-chase) is promoted alongside it
but is currently toggled off on the live box; see [STATE.md](STATE.md).

## Status: research-stage — selection + execution, live demo running

The strategy is a **selection signal** (a liquidity-migration event picks a
candidate pool) + an **execution signal** (the in-migrated flow exhausts and
fades — short the *confirmed* fade, not the top; this is a fade strategy, not a
catch-the-top strategy). The earlier "Round 2 = documented null" verdict has been
**retracted** (substantially a methodology artifact). Under realistic capped stop
fills at `max_active=12`, the daily strategy is **gross-positive on both venues
in-sample**. It stays in-sample; the Bybit demo forward test is the arbiter;
nothing is promoted to real money. Dated numbers + full record (the one
research file): [docs/research_summary.md](docs/research_summary.md); live state +
what's next: [STATE.md](STATE.md).

A Bybit demo account on a VPS hosts the forward tests. As of 2026-06-09 the box
runs only the continuous sleeve (research-stage, demo orders + paper shadow) plus
the risk engine and a dry-run BTC hedge timer; the frozen promoted short profile
and v11a long sleeve are toggled off in `deploy/sleeves.env` but remain
promoted-in-code and redeployable. Forward demo is the arbiter; clocks restarted
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
