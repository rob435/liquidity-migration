---
name: run-strategy
description: Construct and run current liquidity_migration CLI, data, audit, and forward commands safely. Use whenever invoking python -m liquidity_migration or scripts/ops.sh so the correct operational surface, data root, end-exclusive boundary, profile, PIT mode, and demo or paper safety checks are derived from current help and the active experiment contract. Never assume today's date, a dry run, cross-venue scope, or mainnet authority.
---

# Run repository commands safely

Prefer the highest-level current surface that owns the task:

```bash
scripts/ops.sh --help
python -m liquidity_migration --help
python -m liquidity_migration SUBCOMMAND --help
```

Treat `--help`, the selected experiment contract, and current code as
authoritative. Do not maintain or invent a static subcommand list.

## Select the root and boundary

- Use `~/SHARED_DATA/bybit_full_pit` and
  `~/SHARED_DATA/binance_full_pit` as the normal research storage roots.
- Use a venue because the claim or contract requires it. A second venue is a
  robustness probe or portability test, not a universal gate.
- Keep research roots separate from demo/paper ledger roots under `data/` or the
  VPS. Never point an order-writing runtime at a research root.
- Derive `--start` and end-exclusive `--end` from the active contract or task.
  Do not silently substitute today's date for a frozen boundary.
- Inspect current PIT and dataset coverage directly; root names do not prove
  completeness.

## Use canonical wrappers

- Account evidence: `scripts/ops.sh status` for read-only deployment state and
  `scripts/ops.sh account-parity` for source-bound structural journal
  comparison; apply the `pit-reconcile` skill. There is no current combined
  PIT/model/runtime reconciliation command.
- Equity curves: `scripts/ops.sh equity` or `scripts/equity_curves.sh`; apply
  the `equity-curve` skill.
- Current registered tail experiment: `scripts/ops.sh tail-plan` before
  `tail-run`.
- Data build/audit, reset, deploy, status, and tests: use the named
  `scripts/ops.sh` command and preserve its explicit mutation handshake.

For a custom research run, use the preregistered dispatcher when one exists.
Otherwise call the package runner with a saved config and reconstructable
command; label ad hoc work exploratory.

## Verify order behavior

Never call a cycle “dry” merely because it targets demo. Before any forward
command, inspect the explicit `EXECUTION_ENVIRONMENT=demo|paper`, confirmation,
paper mode, profile, credentials, and `REAL_MONEY`. Use a true dry-run/plan mode
when the command provides one.

Demo submission still changes an external demo account and requires task scope
that includes running it. Mainnet is categorically separate: never set
`REAL_MONEY`, select mainnet credentials, or infer permission from broad project
authority.

## Apply evidence discipline

- Use full PIT when a historical-universe claim requires it; a partial/current
  universe can support only its declared narrower scope.
- Include material funding and costs for net-performance claims; omit them only
  when the claim does not depend on PnL and say so.
- Apply `backtest-integrity` before a decision-influencing run and
  `research-report` before interpreting the output.
- Preserve exact commands, configs, hashes, logs, failures, and artifacts needed
  by the claim.
