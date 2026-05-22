# liquidity-migration

A research codebase for a Bybit liquidity-migration short strategy — a
cross-sectional strategy that ranks the perpetual-futures universe by a
volume / liquidity-migration signal and shorts the weakest-ranked names.

## Status: active research — regime-narrow edge, demo forward test

Point-in-time, costed backtesting on the audit-corrected engine, an 81-scenario
robustness sweep, and an adversarial model-court review (`strategy-tribunal`)
established that the signal is **statistically real** in the 2023-2026 sample
window: it has cross-sectional rank skill, survives the full negative-control
battery (block-bootstrap, random-sign, inverted-edge, three shuffles), and
covers 3 of 3 in-sample pre-registered windows positive.

It is **not yet real-money-validated** and the edge is **regime-narrow**:
dedicated pre-2023 OOS data roots (Bybit + Binance) fail every variant tested
(0/3 windows promotable, drawdowns -46% to -51%+) — the strategy does not
generalize backward into the 2020-22 alt-mania-and-winter regime. The
conditional alt-beta is ~-0.45, so the edge is materially short-alts-beta in
the bear-or-range alt regime that prevailed in 2023-26.

A demo (paper) forward test of the 3-position concentrated `promoted` profile
runs on a Bybit demo account on a VPS — that demo is the **actual forward
out-of-sample test** of whether the IS evidence holds outside 2023-26. No
real-money trading is active: a real-money execution path exists in the code
but the account is a plain `.env` toggle (`DEMO` / `REAL_MONEY`, mutually
exclusive) that defaults to demo. The strategy is not validated for real
money. See `docs/research_findings.md` for the full verdict and caveats; see
`docs/system_status.md` for the deployment record.

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
