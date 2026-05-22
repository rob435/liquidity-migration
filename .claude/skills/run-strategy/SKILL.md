---
name: run-strategy
description: "Correct command invocations for the liquidity_migration CLI: the volume-events backtest, strategy-tribunal promotion court, event-demo-cycle forward runner, data builders, and audits. Use whenever running or constructing a 'python -m liquidity_migration' command, so the right data root, end-date boundary, and point-in-time flags are applied."
---

# Running the liquidity_migration CLI

Entry point: `python -m liquidity_migration [--config ...] [--data-root ...] <subcommand>`.

Always check help before constructing a run — the `volume-events` parser alone
has 100+ flags:

```bash
python -m liquidity_migration --help
python -m liquidity_migration <subcommand> --help
```

## Data root — pick the right one (critical)

- **Serious research / promotion evidence** → `~/SHARED_DATA/bybit_fullpit_1h`.
  The default config resolves `DATA_ROOT` here. Use `--end 2026-05-18`
  (end-exclusive; completed bars run 2023-05-03..2026-05-17).
- **Live demo ledgers** → `data/bybit-demo-event`. NEVER point a research run
  here, and never point demo ledgers at the research root.
- **Out-of-sample validation** → `~/SHARED_DATA/bybit_oos_pre2023`,
  `~/SHARED_DATA/binance_oos_pit`. Validation only — no longer pristine.
- Pass `--data-root` only when intentionally running a non-default audited
  root. See `docs/data_roots.md` and the `liqmig-research` MCP `data_roots`
  tool.

## Canonical commands

Active strategy backtest:

```bash
python -m liquidity_migration --config configs/volume_alpha.default.yaml volume-events
```

Full-PIT overnight runner (syncs main, installs env, smoke tests, builds
manifest + klines, validates coverage, runs the strategy):

```bash
bash scripts/run_fullpit_volume_overnight.sh
```

Promotion court (run after a `volume-events` report exists; replace `DATA_ROOT`):

```bash
python -m liquidity_migration --data-root DATA_ROOT strategy-tribunal \
  --report-dir DATA_ROOT/reports/volume_event_research \
  --comparison-csv DATA_ROOT/reports/stress_summary.csv \
  --comparison-family promoted_funding \
  --pre-registered-window train:2023-05-03:2024-05-03,validation:2024-05-03:2025-05-03,oos:2025-05-03:2026-05-03 \
  --execution-data-root DATA_ROOT
```

Demo forward, one dry cycle:

```bash
python -m liquidity_migration --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml event-demo-cycle
```

## Subcommands (16)

`download-data` · `download-binance-proxy` · `data-layer-audit` ·
`discover-universe` · `archive-manifest` · `archive-download-klines` ·
`archive-download-klines-1h` · `archive-download-klines-1h-api` ·
`volume-events` · `strategy-tribunal` · `portfolio-hedge` · `feature-factory` ·
`event-demo-cycle` · `event-risk-cycle` ·
`event-risk-ws` · `reconcile-paper-demo`

## Guardrails

- `volume-events` requires full PIT by default; `--allow-partial-pit` is only
  for explicitly biased diagnostics, and that run must be labelled biased.
- Demo order submission is allowed only for `STRATEGY_PROFILE=promoted` —
  the runner refuses `SUBMIT_ORDERS=1` otherwise. Demo vs mainnet is the
  `DEMO` / `REAL_MONEY` `.env` toggle (`bybit.resolve_private_credentials`),
  which defaults to demo; keep it on demo without explicit owner instruction.
- Event-driven entries are the strategy path; fixed-day rebalance grids are
  legacy benchmarks only. Do not revive the retired daily-close short-fade.
- Every serious run must leave enough report output to audit the decision.
- Before constructing a run, apply the **backtest-integrity** skill. After a
  run, read the output with the **research-report** skill before calling it a
  result.
