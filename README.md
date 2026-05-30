# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.
A long-only sleeve (`MultiStratV1` / v11a, FOMO-chase) runs alongside on
demo.

## Status: research-stage — selection + execution, live demo running

The strategy is a **selection signal** (a liquidity-migration event picks a
candidate pool) + an **execution signal** (the in-migrated flow exhausts and
fades — short the *confirmed* fade, not the top; this is a fade strategy, not a
catch-the-top strategy). The earlier "Round 2 = documented null" verdict has been
**retracted** (substantially a methodology artifact). Under realistic capped stop
fills at `max_active=12`, the daily strategy is **gross-positive on both venues
in-sample**. It stays in-sample; the Bybit demo forward test is the arbiter;
nothing is promoted; real money stays off. Dated numbers + full record:
[docs/research_summary.md](docs/research_summary.md); live state:
[STATE.md](STATE.md); forward plan:
[docs/research_plan_intraday_kernel.md](docs/research_plan_intraday_kernel.md).

A demo (paper) forward test of the frozen `promoted` short profile + the v11a
long sleeve runs on a Bybit demo account on a VPS — that demo is the actual
forward out-of-sample test (deployed parameters are tracked in [STATE.md](STATE.md)). No real-money
trading is active: a real-money execution path exists in the code but
the account is a plain `.env` toggle (`DEMO` / `REAL_MONEY`, mutually
exclusive) that defaults to demo. The strategy is not validated for real
money.

## What the repo contains

- `liquidity_migration/` — Python package: data ingestion, point-in-time
  archive builders, the backtest / event engine, and the
  `python -m liquidity_migration` CLI (run `--help` for the subcommand list).
- `tests/` — `.venv/bin/python -m pytest -q`.
- `docs/research_findings.md` — short verdict pointer (defers to research_summary.md).
- `docs/research_plan_intraday_kernel.md` — the forward research plan (5950X).
- `docs/backtesting_errors_we_never_repeat.md` — research methodology standard.
- `docs/data_roots.md` — data-root contract (research / live demo / OOS).
- `docs/event_demo_daemon.md` — demo forward-cycle daemon runbook + infra-hardening changelog.
- `.claude/` — Claude Code skills and an MCP server for working in this repo.
- `AGENTS.md` — repo rules.

Python 3.11+.
