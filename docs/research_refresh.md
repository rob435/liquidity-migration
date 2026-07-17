# Research refresh and three-way reconciliation

`scripts/research_refresh.sh` is the supported one-command workflow for current
market data, residual momentum, the standard LONG/CONTINUOUS backtests, and an
optional demo/paper/backtest structural comparison.

It is an offline research utility. It does not place orders, contact private
venue APIs, pull a VPS checkout, promote a strategy, or authorize capital.

## Quick use

Inspect the exact boundaries and commands without writing anything:

```bash
scripts/ops.sh research-refresh plan --end 2026-07-16
```

Run the routine append-first workflow through the latest completed day named by
the end-exclusive boundary:

```bash
scripts/ops.sh research-refresh run --end 2026-07-16
```

Use the complete canonical builders when the registered claim requires a full
root reconstruction:

```bash
scripts/ops.sh research-refresh run \
  --end 2026-07-16 \
  --start 2023-07-16 \
  --data-mode canonical \
  --preregistration docs/preregistration/benchmark_refresh_2026-07-16.md \
  --run-id benchmark-refresh-2026-07-16
```

The named end is always exclusive. `[2023-07-16, 2026-07-16)` uses data only
through 2026-07-15 UTC.

## What is incremental

| Phase | Routine `tail` behavior | Why it is safe or limited |
| --- | --- | --- |
| Bybit membership | Rebuild the independent manifest over its canonical history. | A narrow membership scan could silently lose delisted names, so membership is not tail-only. |
| Bybit klines | Recheck a trailing overlap and fetch missing or sub-20-bar partitions. | The complete root is then validated against independent membership. A failure triggers a full missing-only scan. |
| Binance klines/membership | Append strict current-month daily archives and rebuild membership from observed archive coverage. | Crossing a month that is not already materialized falls back to the atomic canonical monthly builder. |
| Ancillary market data | Re-fetch from the stalest dataset boundary minus the overlap. | Writers reuse valid partitions. Tail mode is operational refresh evidence, not a substitute for a registered full-history rebuild. |
| Residual momentum | Recompute a checked overlap, prove stable rows unchanged, then atomically append/refresh the provisional tail. | Final causal rows are retained for aged-out symbols. A legacy schema without `is_provisional` receives one explicit atomic full rewrite; any other stable-overlap mismatch fails closed for inspection. |
| Backtests | Reuse an identical completed run-scoped report; otherwise recompute the fixed window from a clean sleeve directory. | Incremental PnL would require a separately verified engine-state checkpoint. The tool does not invent one or append a new replay to an old account journal. |

`--data-mode canonical` invokes `build_full_pit_bybit.sh` and
`build_full_pit_binance.sh`. Their download stages reuse valid data where their
owners support it, but the Binance monthly membership/kline pair is rebuilt in
verified staging and published atomically. Canonical mode also regenerates the
residual-momentum table from its fixed history start and atomically replaces
the prior table; a full data reconstruction can legitimately change historical
cross-sectional inputs, so retaining a previously “stable” feature overlap
would mix evidence identities. Routine `tail` mode continues to require exact
checked-overlap agreement before appending.

When inspection establishes that only the feature table must be reconstructed,
`--force-rmom-full-rewrite` atomically rebuilds residual momentum from its fixed
causal start without changing `tail` market-data behavior. The choice is frozen
in the run manifest and command fingerprint. It is an explicit recovery or
migration control, not an automatic waiver for an unexplained overlap failure.

All selected datasets must expose a partition for `end - 1 day`; the all-root
manifest check must pass; and every requested backtest report must match the
frozen start/end and 1x modeled exposure. A partial sleeve now makes
`equity_curves.sh` return nonzero.

## Resume and receipts

Each run has a stable directory under `reports/research-refresh/<run-id>/`:

```text
manifest.json       frozen code/config/root/window identity
events.jsonl        append-only command start/failure/success/resume ledger
logs/*.log          command output retained across attempts
summary.<hash>.json immutable coverage, cell result, and artifact-hash card
backtests/<venue>/  isolated LONG/CONTINUOUS reports and replay journals
reconciliation/     optional immutable three-way output
```

Reusing the same `--run-id` skips only a step whose exact command fingerprint
succeeded and whose expected artifact still exists. Failures remain in the
event ledger and logs; a retry clears only that run's partial derived sleeve
directory. Raw data is untouched. Changed windows, roots, source commits, or
run configuration are refused under an existing ID.

## Demo / paper / backtest comparison

The account inputs must be quiescent, frozen, read-only snapshots of the current
canonical account roots. The tool verifies both journals before reading them.
It does not copy a live root while writers are active.

Run the comparison during a refresh by supplying both roots, or attach them to
a completed run later:

```bash
scripts/ops.sh research-refresh reconcile \
  --run-dir reports/research-refresh/benchmark-refresh-2026-07-16 \
  --demo-account-root /snapshots/bybit-account-demo \
  --paper-account-root /snapshots/bybit-account-paper \
  --account-snapshot-commit <full-40-character-commit>
```

The primary grain is:

```text
(sleeve, active component, symbol, causal signal_ts_ms)
```

The report separately retains proposed targets, risk-accepted targets, and the
account-level net-symbol command/fill state. CONTINUOUS runtime tags
`p3/p4p3/p4p5` are mapped to their code-defined historical components. The
backtest leg comes from the standard trade artifacts for the same fixed window.

Exact agreement supports only a structural entry-key claim. It does not prove
component-attributed venue fills, slippage calibration, fee/funding equality,
account P&L agreement, market-tape parity, alpha, deployment readiness, or
mainnet authority. Those require their own journal/venue accounting and
claim-scoped research evidence.
