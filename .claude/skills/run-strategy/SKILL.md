---
name: run-strategy
description: "Correct command invocations for the liquidity_migration CLI: the volume-events backtest, event-demo-cycle forward runner, data builders, and audits. Use whenever running or constructing a 'python -m liquidity_migration' command, so the right data root, end-date boundary, and point-in-time flags are applied."
---

# Running the liquidity_migration CLI

Entry point: `python -m liquidity_migration [--config ...] [--data-root ...] <subcommand>`.

Always check `--help` before constructing a run — the parsers are large and
change often:

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
- **Paper-shadow ledgers** → `data/bybit-paper-event`. Reconciliation is fully
  scripted — run `bash scripts/reconcile.sh` (skill: `pit-reconcile`) for the
  demo↔paper↔backtest↔Bybit reconcile; do not hand-assemble `reconcile-*` calls.
- **Pristine OOS** → forward demo / paper ledgers only. There is no internal
  OOS surface; both per-venue roots span their full available history. Cite
  the forward ledger as the OOS evidence.
- Pass `--data-root` only when intentionally running a non-default audited
  root. See `docs/data_roots.md` and the `liqmig-research` MCP `data_roots`
  tool.

## Canonical commands

Research cell / sweep — the official path (fills the ~30 baseline flags; do not
hand-assemble `volume-events` flags):

```bash
bash scripts/volume_events_cell.sh --venue <bybit|binance> --cell-id <id> \
  --phase <tag> --overrides 'KEY=VAL,…'   # DRY_RUN=1 to preview
```

Build/verify the per-venue full-PIT data roots (archives old roots, builds both
roots — manifest + klines — and validates coverage; see `docs/data_roots.md`), then
run the `volume-events` backtest above against the rebuilt root:

```bash
bash scripts/build_full_pit_roots.sh        # full pipeline (bybit + binance)
bash scripts/verify_full_pit_rebuild.sh     # standalone coverage / data-layer-audit gates
```

Demo forward, one dry cycle:

```bash
python -m liquidity_migration --data-root data/bybit-demo-event \
  --config configs/volume_alpha.default.yaml event-demo-cycle
```

## Subcommands

Run `python -m liquidity_migration --help` for the current, authoritative
subcommand list — do not maintain a copy here.

## Guardrails

- `volume-events` requires full PIT by default; `--allow-partial-pit` is only
  for explicitly biased diagnostics, and that run must be labelled biased.
- Demo order submission is allowed only for the deployed `STRATEGY_PROFILE`
  (see STATE.md > What's running) — the runner refuses `SUBMIT_ORDERS=1`
  otherwise. Demo vs mainnet is the `DEMO` / `REAL_MONEY` `.env` toggle
  (`bybit.resolve_private_credentials`), which defaults to demo; keep it on demo
  without explicit owner instruction.
- Event-driven entries are the strategy path; legacy fixed-day rebalance-grid
  benchmarks are retired — do not revive them or cite their results as evidence.
- What is deployed vs. research-gated (the daily-close signal vs. the continuous
  variant) is tracked in STATE.md and `docs/research_plan_intraday_kernel.md`
  — defer to them.
- Every serious run must leave enough report output to audit the decision.
- Before constructing a run, apply the **backtest-integrity** skill. After a
  run, read the output with the **research-report** skill before calling it a
  result.
