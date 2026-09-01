---
name: run-strategy
description: Construct and run current liquidity_migration CLI, data, audit, and demo operational commands safely. Use whenever invoking python -m liquidity_migration or scripts/ops.sh so data roots, end-exclusive boundaries, profiles, PIT modes, and mutation handshakes come from current help and code. Never assume today's date, a dry run, cross-venue scope, or mainnet authority.
---

# Run repository commands safely

Start with the current owning surface:

```bash
scripts/ops.sh help
python -m liquidity_migration --help
python -m liquidity_migration SUBCOMMAND --help
```

Do not maintain a static package-subcommand list.

## Select roots and boundaries

- Normal research roots are `~/SHARED_DATA/bybit_full_pit` and
  `~/SHARED_DATA/binance_full_pit`; inspect actual coverage.
- Keep research roots separate from VPS account, inbox, capture, and strategy roots.
- Derive `--start` and end-exclusive `--end` from the task or prospective
  contract; never silently use today's date.
- Choose venues from the claim. A second venue is not a universal gate.
- Use strict PIT only when the claim requires historical-universe coverage, and
  label partial/current-universe diagnostics honestly.

## Canonical wrappers

- Deployment/account state: `scripts/ops.sh status`.
- Deploy a new generation with `scripts/ops.sh deploy`, optionally pinning
  `EXPECTED_COMMIT`. It stops the fleet, installs the exact commit, and starts
  the funded realm only while `REAL_MONEY` is armed.
- Stop or persistently disarm funded trading with `scripts/ops.sh deploy
  stop-mainnet` or `scripts/ops.sh deploy disarm-mainnet` respectively.
- Mainnet arming state (read-only): `scripts/ops.sh real-money preflight`.
- Credential-wide flatness evidence: `scripts/ops.sh attest-flat
  --environment demo|mainnet`; apply `pit-reconcile` before drawing an
  accounting conclusion.
- Equity curves: `scripts/ops.sh equity`; apply `equity-curve`.
- Tests: `scripts/dev.sh test` (local only; not an operator route).
- Data builds: use the per-venue builders or current package command help.
- One bounded live demo order proof: inspect `engine help`, then use
  `engine canary-order --config PATH --symbol SYMBOL --expected-user-id UID
  --execute`. It only accepts `bybit_demo`, takes the account lease, and must
  finish with an exact terminal order state and two clean derivative-account
  readings. Never substitute a raw signed request or point it at mainnet.

## Forward safety

Demo is not a dry run: it changes the external demo account. Before a forward
command, inspect `EXECUTION_ENVIRONMENT`, credentials, confirmation,
checkout, and `REAL_MONEY`. Use a true plan/dry-run
mode when one exists.

Mainnet is categorically separate. Never set `REAL_MONEY`, select mainnet
credentials, or infer permission from broad repository work.

## Evidence

Preserve exact commands, configs, roots, code/data identities, warnings,
failures, and artifacts. Include material funding and costs for net-performance
claims. Apply `backtest-integrity` before decision-influencing research and
`research-report` before interpreting it.
