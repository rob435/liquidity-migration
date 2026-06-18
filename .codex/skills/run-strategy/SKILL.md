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
  short sleeve's inert root). Reconciliation is fully scripted behind ONE
  front door — `bash scripts/reconcile.sh` (skill: `pit-reconcile`) runs the full
  demo↔backtest↔paper three-way for BOTH sleeves by default; add `--quick` for
  the fast paper↔demo execution-only check. Do not hand-assemble `reconcile-*`
  calls. (`scripts/reconcile_three_way.sh` is a back-compat alias for the default
  full run.)
- **Pristine OOS** → forward demo / paper ledgers only. There is no internal
  OOS surface; both per-venue roots span their full available history. Cite
  the forward ledger as the OOS evidence.
- Pass `--data-root` only when intentionally running a non-default audited
  root. See `docs/data_roots.md`; if a `liqmig-research` MCP server is available,
  its `data_roots` tool can be used as a convenience check.

## Canonical commands

Research sweeps: continuous sweeps use an in-memory config-override driver
(see `scripts/alpha_sweep.py`); long sweeps should use a small dated dispatcher
for the specific pre-registered cells and call `run_long_native_research`
directly per cell. Do not revive the retired historical long-sweep helper. The erased
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
- Demo order submission requires `SUBMIT_ORDERS=1` + `CONFIRM_DEMO_ORDERS=1`
  and a known `STRATEGY_PROFILE`; the DEPLOYED profile is pinned by the
  deploy/verify scripts, not refused at runtime — check STATE.md > What's
  Running before changing it. Demo vs mainnet is the `DEMO` / `REAL_MONEY` `.env` toggle
  (`bybit.resolve_private_credentials`), which defaults to demo; keep it on demo
  without explicit owner instruction.
- Continuous and LONG have separate lifecycles; do not transfer assumptions
  between them. Legacy fixed-day rebalance grids and erased short-sleeve paths
  are retired evidence only.
- What is deployed vs. research-gated is tracked in STATE.md and
  `docs/research_summary.md` — defer to them.
- Every serious run must leave enough report output to audit the decision.
- Before constructing a run, apply the **backtest-integrity** skill. After a
  run, read the output with the **research-report** skill before calling it a
  result.
