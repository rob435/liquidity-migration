---
name: pit-reconcile
description: Assess current account-execution reconciliation and PIT/model evidence after retirement of the sleeve-local reconciler. Use when checking account-journal parity, venue/account mismatches, execution agreement, model drift, fill or P&L evidence, archive_trade_manifest coverage, or pit_membership_fail. Distinguish available structural checks from the open captured-tape and venue gates; never invoke deleted reconcile scripts.
---

# Reconcile account evidence and PIT claims

First inspect the current operator and package surfaces:

```bash
scripts/ops.sh --help
scripts/ops.sh account-parity --help
scripts/ops.sh venue-accounting --help
python -m liquidity_migration --help
```

These commands are demo/paper or research surfaces. They never authorize real
money.

## Account runtime evidence

Use `scripts/ops.sh status` for a read-only deployed-service/liveness view.
Use `scripts/ops.sh account-parity` only when actual non-empty historical,
paper, and demo account roots have been captured:

```bash
scripts/ops.sh account-parity \
  --environment historical=/path/to/historical-account-root \
  --environment paper=/path/to/paper-account-root \
  --environment demo=/path/to/demo-account-root \
  --comparison-scope-file /path/to/frozen-natural-batches.json \
  --event-parity-receipt /path/to/event-parity.json \
  --fresh-epoch-reset-receipt /path/to/natural-reset-receipt.json \
  --risk-policy-file /path/to/risk-policy.json \
  --rules-file /path/to/demo-rules.json \
  --effective-runtime-config-bundle /path/to/effective-runtime-config-bundle.json \
  --twin-calibration-receipt /path/to/pre-reset-twin-calibration.json \
  --repo-root /path/to/clean/repository \
  --expected-commit FULL_40_CHARACTER_COMMIT \
  --quantity-tolerance 1e-12 \
  --output /path/outside/repository/account-kernel-parity.json
```

The schema-v3 comparison scope and schema-v4 kernel receipt reopen and rehash
the authoritative transaction files, then recompute a frozen, ordered
natural-batch window from each raw journal. The receipt requires exact
decision/target keys, target discrete fields, risk
acceptance/rejection keys, risk target presence, and semantic command tuples.
All target/risk/command quantities must be finite and agree at the fixed
absolute tolerance `1e-12`. Raw command IDs may differ by environment, but must
form a one-to-one map to semantic commands.

Actual demo acknowledgements, partial fills, prices, fees, P&L, funding,
reconciliation, owner-convergence, and native-protection facts are classified
and counted; they are deliberately not compared byte-for-byte with the model.
Historical-versus-paper modeled execution is reported as a separate normalized
exact subgate. A schema-v4 plan-parity pass therefore does not claim exact demo
execution, accounting agreement, market-tape provenance, scheduler parity,
fresh venue rules, alpha, or deployment readiness.

The deploy-valid receipt also binds the event-parity, fresh/reset, risk, rules,
source-reopened effective LONG/CONT runtime-config bundle, and pre-reset
calibration artifacts plus an exact clean Git commit. Its verifier rereads every
natural journal and evidence file; older receipts and self-rehashed receipts
with missing or changed sources fail. The
calibration receipt's embedded live paths are not reopened because reset reuses
those lexical paths. Kernel parity binds its immutable receipt/config hashes
and rejects calibration/natural journal-hash reuse; archived calibration-source
revalidation belongs to the independent execution-twin drift gate.

Source-process quiescence remains operator evidence, not something a journal
hash can prove. Read `docs/account_execution_cutover.md` before making a runtime
acceptance claim.

For a live mismatch, inspect the account journal, owner reconciliation report,
venue snapshot, and immutable venue records directly. Do not treat sleeve-local
trade/order Parquet projections as position or P&L authority.

After the bounded demo tape is complete and the demo owner is stopped, use the
owner-serialized, read-only accounting capture over the exact fresh-ledger
epoch (never more than seven days):

```bash
scripts/ops.sh venue-accounting \
  --account-root /absolute/path/to/demo-account-root \
  --account-id bybit-demo-unified \
  --start-time-ms FRESH_EPOCH_START_MS \
  --output /absolute/path/to/venue-accounting.json
```

The self-hashed receipt replays the current canonical journal and binds raw
Bybit demo TRADE, closed-PnL, and SETTLEMENT rows plus position/open-order
snapshots before and after capture. It checks exact execution/order identities,
target-to-order lineage, observed fees, fill P&L, funding identities/values,
and local/venue flatness. The registered minimum floors are two trade rows, one
closed-PnL row, and one funding settlement; a zero-funding window does not pass
by assumption. Registered maximum tolerances are `1e-12` quantity, `1e-8`
price/amount, and `1e-9` relative. The command and verifier reject lower floors
or wider tolerances. This is accounting evidence for the named fresh demo
epoch, not component P&L attribution, strategy parity, alpha, or deployment
authority.

## PIT and model evidence

PIT is checked inside the exact research run whose claim depends on it. There is
no current combined PIT plus live-ledger reconciliation command.

For a targeted manifest rebuild:

```bash
python -m liquidity_migration --data-root ROOT archive-manifest \
  --start YYYY-MM-DD --end YYYY-MM-DD
```

Inspect the selected command's current `--help`, preserve the end-exclusive
boundary, root identity, config, warnings, run label, and artifacts, and rerun
the same research command after closing the named data gap. Verify manifest
`source` provenance and kline coverage. Do not loosen
`require_full_pit_universe` after seeing a result in order to rescue a
historical-universe claim. A partial run may support only an explicitly narrower
diagnostic under `docs/governance.md`.

## Retired surface

The following were removed on 2026-07-13 because they compared sleeve-local
compatibility projections rather than the authoritative target/account-owner
boundary:

- `scripts/reconcile.sh` and `scripts/reconcile.py`;
- `scripts/reconcile_three_way.py` and `scripts/reconcile_fills.py`;
- `reconcile-long-paper-demo` and `reconcile-continuous-paper-demo`;
- `continuous-forward-readiness` and
  `continuous-rebalance-cycle-audit`.

Historical receipts from those tools remain evidence about their dated runs.
They are not a current operational gate and must not be recreated merely to
restore a green headline.
