# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.
A long-only sleeve (`MultiStratV1` / v11a, FOMO-chase) runs alongside on
demo.

## Status: Round 2 complete — documented null (2026-05-29) + live demo running

The Round 2 integrated-strategy program is **complete**, run on rebuilt full-PIT
roots under the hardened engine (capped/`bar_extreme` stops, 100% taker,
calendar-exact returns). Verdict: **both signal architectures are a documented
null → do nothing.** Architecture A (daily) shows a *real bybit edge* (best stack
MAR 1.39) but *no binance edge* (−1.3%), so it fails the cross-venue robustness
bar; Architecture B (continuous) is not tradeable after honest cost. The frozen
`promoted` profile is unchanged on demo, nothing is promoted, real money stays
off. See [STATE.md](STATE.md) and
[docs/preregistration/round2/](docs/preregistration/round2/) for the verdicts and
[docs/system_status.md](docs/system_status.md) for the deployment record. Any
further work (a bybit-only daily strategy from the single-venue edge, or a
momentum-continuation thesis) is a new operator pre-registration, not a
continuation of Round 2.

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
  `python -m liquidity_migration` CLI (24 subcommands; run `--help`).
- `tests/` — `.venv/bin/python -m pytest -q`.
- `docs/research_findings.md` — current research-evidence status (post-reset).
- `docs/backtesting_errors_we_never_repeat.md` — research methodology standard.
- `docs/data_roots.md` — data-root contract (research / live demo / OOS).
- `docs/system_status.md` — strategy / deployment status.
- `docs/event_demo_daemon.md` — demo forward-cycle daemon execution runbook.
- `.claude/` — Claude Code skills and an MCP server for working in this repo.
- `AGENTS.md` — repo rules.

Python 3.11+.
