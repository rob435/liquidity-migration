# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.

## Status: active research — refinement phase

Extensive point-in-time, costed backtesting and an adversarial model-court
review (`strategy-tribunal`) established that the signal is **statistically
real**: it has genuine cross-sectional rank skill and survives a full
negative-control battery (block-bootstrap, random-sign, inverted-edge).

It is **not yet deployable as a standalone strategy**. The edge is
regime-conditional — it monetizes in crash regimes and is flat-to-negative
through alt-bull markets — and the standalone configuration failed promotion
in the tribunal (funding costs unmodeled; only 2 of 3 pre-registered
out-of-sample windows positive).

The signal is real enough to be worth refining. The current research focus is
**regime-aware refinement**: gating the strategy with a regime / crash detector
so it is active only when the regime pays, and modeling funding costs. See
`docs/research_findings.md` for the full verdict and the roadmap.

A demo (paper) forward test runs on a Bybit demo account. No real-money trading
is active: a real-money execution path exists in the code but is disabled by
default — `demo=False` is refused unless real-money mode is deliberately armed.
The strategy is not validated for real money. See `docs/system_status.md`.

## What the repo contains

- `liquidity_migration/` — Python package: data ingestion, point-in-time
  archive builders, the backtest / event engine, and the
  `python -m liquidity_migration` CLI (17 subcommands; run `--help`).
- `tests/` — `.venv/bin/python -m pytest -q`.
- `docs/research_findings.md` — current research verdict and refinement roadmap.
- `docs/backtesting_errors_we_never_repeat.md` — research methodology standard.
- `docs/data_roots.md` — data-root contract (research / live demo / OOS).
- `docs/system_status.md` — strategy / deployment status.
- `docs/event_demo_daemon.md` — demo forward-cycle daemon execution runbook.
- `.claude/` — Claude Code skills and an MCP server for working in this repo.
- `AGENTS.md` — repo rules.

Python 3.11+.
