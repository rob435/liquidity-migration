---
name: run-strategy
description: "Correct command invocations for the liquidity_migration CLI: data builders, audits, and the long/continuous forward runners. Use whenever running or constructing a 'python -m liquidity_migration' command, so the right data root, end-date boundary, and point-in-time flags are applied."
---

> **ERASURE NOTE (2026-06-11, operator order):** the daily SHORT sleeve was
> ERASED from the system — `volume-events` backtest, `event-demo-cycle`,
> `event_demo_daemon`, `short_profile`, `volume_events_cell.sh`, short deploy
> units and short reconcile commands NO LONGER EXIST. Ignore any instruction
> below that references them; long + continuous guidance still applies.

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
- **Live demo ledgers** → `data/bybit-continuous-demo-event` +
  `data/bybit-long-demo-event` (`data/bybit-demo-event` is the erased short
  sleeve's inert legacy root). NEVER point a research run at a live ledger
  root, and never point demo ledgers at the research root.
- **Paper-shadow ledgers** → `data/bybit-long-paper-event` +
  `data/bybit-continuous-paper-event` (`data/bybit-paper-event` is the erased
  short sleeve's inert root). Reconciliation is fully scripted — run
  `bash scripts/reconcile.sh` (skill: `pit-reconcile`) for the paper↔demo
  reconcile; do not hand-assemble `reconcile-*` calls.
- **Pristine OOS** → forward demo / paper ledgers only. There is no internal
  OOS surface; both per-venue roots span their full available history. Cite
  the forward ledger as the OOS evidence.
- Pass `--data-root` only when intentionally running a non-default audited
  root. See `docs/data_roots.md` and the `liqmig-research` MCP `data_roots`
  tool.

## Canonical commands

Research sweeps — `scripts/_sweep_runtime.py` is DEAD pending repoint (its
dispatcher shells to the ERASED `volume-events` subcommand; see its docstring).
Current patterns: continuous sweeps use an in-memory config-override driver
(see `scripts/alpha_sweep.py`); long sweeps call `run_long_native_research`
directly per cell (see `scripts/long_improve_sweep.py`). The erased
`volume_events_cell.sh` single-cell path has NO replacement — the daily-short
engine it drove is gone.

Build/verify the per-venue full-PIT data roots (archives old roots, builds both
roots — manifest + klines — and validates coverage; see `docs/data_roots.md`):

```bash
bash scripts/build_full_pit_roots.sh        # full pipeline (bybit + binance)
bash scripts/verify_full_pit_rebuild.sh     # standalone coverage / data-layer-audit gates
```

Demo forward, one dry cycle (the live roots are `data/bybit-continuous-demo-event`
and `data/bybit-long-demo-event`):

```bash
python -m liquidity_migration --data-root data/bybit-continuous-demo-event \
  --config configs/volume_alpha.default.yaml continuous-event-demo-cycle
python -m liquidity_migration --data-root data/bybit-long-demo-event \
  --config configs/volume_alpha.default.yaml long-native-event-demo-cycle
```

## Subcommands

Run `python -m liquidity_migration --help` for the current, authoritative
subcommand list — do not maintain a copy here.

## Guardrails

- Every backtest engine requires full PIT by default; partial-PIT runs (config
  `require_full_pit_universe=False` / `require_pit_membership=False` — the old
  `--allow-partial-pit` flag was erased with the volume-events CLI) are only for
  explicitly biased diagnostics, and that run must be labelled biased.
- Demo order submission is allowed only for the deployed `STRATEGY_PROFILE`
  (see STATE.md > What's running) — the runner refuses `SUBMIT_ORDERS=1`
  otherwise. Demo vs mainnet is the `DEMO` / `REAL_MONEY` `.env` toggle
  (`bybit.resolve_private_credentials`), which defaults to demo; keep it on demo
  without explicit owner instruction.
- Event-driven entries are the strategy path; legacy fixed-day rebalance-grid
  benchmarks are retired — do not revive them or cite their results as evidence.
- What is deployed vs. research-gated (the daily-close signal vs. the continuous
  variant) is tracked in STATE.md and `docs/research_summary.md` — defer to them.
- Every serious run must leave enough report output to audit the decision.
- Before constructing a run, apply the **backtest-integrity** skill. After a
  run, read the output with the **research-report** skill before calling it a
  result.
