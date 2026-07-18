# Strategy Overhaul V2 account-state replay recovery, 2026-07-18

## Prospective outcome-blind repair contract

- **ID / registered time / mode:**
  `strategy-overhaul-v2-account-state-replay-recovery-2026-07-18`;
  2026-07-18 10:52 UTC; outcome-blind engineering parity and performance
  recovery under the parent
  `strategy-overhaul-v2-comparator-accounting-repair-2026-07-18` contract.
- **Generating code:** `8a6057c8560960f6c95981cb9a7473eb176bb933`.
- **Prior exposure:** no new price outcome was opened by the failed full-account
  attempt. The already-published V2 barebones ledger remains the sole replay
  input. The parent RMOM gate has separately failed; therefore this recovery
  cannot authorize a matched comparator, thesis selection, or holdout access.

## Preserved first attempt and diagnosed defect

The first registered full replay began at
`2026-07-18T10:41:41.744271Z` below:

```text
reports/strategy-overhaul-v2/comparator-accounting-repair-2026-07-18/
  .full-account-replay.working/
```

It was stopped before ten wall-clock minutes, prior to materializing the LONG
sleeve, after CPU/time scaling made a two-hour completion implausible. The root
is preserved and must not be reused, renamed as success, or deleted by this
recovery.

The first repair removed repeated history-sized event-list and event-ID-map
copies. The remaining hot path is now identified: every account transaction
still calls `copy.deepcopy(committed_state)`. `AccountState` retains historical
decisions, target proposals/desires, risk decisions, orders, executions,
closes, and P&L maps, so a full-state clone per transaction is superlinear in
prior account history even though event cache memory is bounded.

## Frozen repair

Add one explicit, default-off historical-research option that permits the
trusted read-only kernel builders to apply their prospective events directly
to the cached state during the single-process Windows buffered replay.

- Production/default `AccountJournal`, `AccountExecutionKernel`, and
  `HistoricalAccountSession` behavior remains unchanged: isolated prospective
  state, prior-state reader visibility, atomic-segment commit, and failed-write
  rollback semantics continue to use deep copies.
- The option must fail closed for any builder not marked
  `trusted_readonly_builder=True`.
- Only `scripts/analyze_strategy_overhaul_v2.py` may enable it, together with
  the already declared single-process buffered transaction adapter. It carries
  no concurrent-reader, concurrent-writer, rollback-after-buffer-failure,
  POSIX-lock, fsync, or crash-durability claim.
- Event specs, order, IDs, hashes, target content, reducer transitions, prices,
  risk decisions, fills, closes, P&L, and final state must not change.

No state pruning, alternate reducer, event omission, ledger sampling, changed
price/cost/funding input, changed transaction order, or changed persistence
hash is permitted.

## Parity and retry gates

Before a full retry:

1. default production-mode tests must retain failed-write rollback and
   concurrent prior-state visibility;
2. a deterministic fixture must prove that the research option avoids the
   full-state deepcopy and rejects an untrusted builder;
3. the frozen 100-key LONG and CONTINUOUS samples must reproduce their exact
   event counts, final state hashes, last event hashes, strategy-event-tape
   hashes, original kernel-transaction counts, source-key hashes, decision/fill
   coverage, and account-versus-ledger gross P&L at `rtol=1e-12`,
   `atol=1e-12`; and
4. a bounded benchmark must show completion time grows sufficiently to make
   the registered full retry plausible. This is a performance gate only; it
   may not inspect a new market outcome.

The one permitted retry writes to a new preserved root, never the first root:

```text
reports/strategy-overhaul-v2/comparator-accounting-repair-2026-07-18/
  .full-account-replay-retry-1.working/
```

It replays exactly 1,899 LONG and 16,745 CONTINUOUS trades from the hash-pinned
published ledger, uses concurrency one, and has a measured two-hour cap. Both
sleeves must end flat; all expected decisions, fills, closes, P&L attributions,
event hashes, source keys, and ledger gross-P&L identities must pass. On
success, only this retry root may be promoted to `full-account-replay/`. Any
mismatch or second performance failure closes the full-account claim as
invalid; no further reduction to a sample is allowed under this contract.

## Boundaries and non-conclusions

The retry reads no new market data and computes no strategy outcome. It must
not create, open, hash, scan, or infer the V2 holdout path. The already failed
RMOM gate means even a successful account replay cannot make the active
comparator valid. No demo target, external order, profile, size, capital,
mainnet, or real-money change is authorized.
