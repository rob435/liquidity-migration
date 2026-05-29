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

- **Bybit working dataset** → `~/SHARED_DATA/bybit_full_pit`. The default
  config resolves `DATA_ROOT` here. Use `--end` set to today's date in UTC
  (end-exclusive) so the run captures the full history available.
- **Binance working dataset** → `~/SHARED_DATA/binance_full_pit`. Same shape
  as the Bybit root. Use it for side-by-side venue validation; agreement
  across both venues is the robustness signal, disagreement flags a regime
  or microstructure artefact.
- **Live demo ledgers** → `data/bybit-demo-event`. NEVER point a research run
  here, and never point demo ledgers at the research root.
- **Paper-shadow ledgers** → `data/bybit-paper-event`. The
  `reconcile-paper-demo` and `reconcile-long-paper-demo` commands compare
  these against the demo ledgers to measure execution slippage.
- **Pristine OOS** → forward demo / paper ledgers only. There is no internal
  OOS surface; both per-venue roots span their full available history. Cite
  the forward ledger as the OOS evidence.
- Pass `--data-root` only when intentionally running a non-default audited
  root. See `docs/data_roots.md` and the `liqmig-research` MCP `data_roots`
  tool.

## Canonical commands

Active strategy backtest:

```bash
python -m liquidity_migration --config configs/volume_alpha.default.yaml volume-events
```

Build/verify the per-venue full-PIT data roots (archives old roots, builds both
roots — manifest + klines — and validates coverage; see `docs/data_roots.md`), then
run the `volume-events` backtest above against the rebuilt root:

```bash
bash scripts/build_full_pit_roots.sh        # full pipeline (bybit + binance)
bash scripts/verify_full_pit_rebuild.sh     # standalone coverage / data-layer-audit gates
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

## Subcommands (24 — run `--help` for the authoritative list)

`download-data` · `download-binance-proxy` · `data-layer-audit` ·
`discover-universe` · `archive-manifest` · `archive-download-klines` ·
`archive-download-klines-1h` · `archive-download-klines-1h-api` ·
`volume-events` · `strategy-tribunal` · `portfolio-hedge` · `feature-factory` ·
`signal-harness` · `event-demo-cycle` · `event-risk-cycle` · `event-risk-ws` ·
`long-native-event-demo-cycle` · `combined-book-telegram-report` ·
`regime-durability` · `reconcile-paper-demo` · `reconcile-long-paper-demo` ·
`reconcile-demo-bybit` · `reconcile-backtest-paper` · `reconcile-all`

## Guardrails

- `volume-events` requires full PIT by default; `--allow-partial-pit` is only
  for explicitly biased diagnostics, and that run must be labelled biased.
- Demo order submission is allowed only for `STRATEGY_PROFILE=promoted` —
  the runner refuses `SUBMIT_ORDERS=1` otherwise. Demo vs mainnet is the
  `DEMO` / `REAL_MONEY` `.env` toggle (`bybit.resolve_private_credentials`),
  which defaults to demo; keep it on demo without explicit owner instruction.
- Event-driven entries are the strategy path; fixed-day rebalance grids are
  legacy benchmarks only. Do not revive the retired daily-close short-fade.
- The deployed signal is the daily-close signal (daily-close features, +1h entry
  delay). A **continuous / sub-hourly** variant (rolling-window features, finer
  bars, 0h delay) is under research (see `docs/research_plan_selection_execution.md`);
  its faster-cadence path is NOT the deployed daily path and is research-gated (needs
  OOS re-validation before it can influence real-money work).
- Every serious run must leave enough report output to audit the decision.
- Before constructing a run, apply the **backtest-integrity** skill. After a
  run, read the output with the **research-report** skill before calling it a
  result.
