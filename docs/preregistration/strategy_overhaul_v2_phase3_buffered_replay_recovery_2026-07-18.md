# Strategy Overhaul V2 Phase-3 Bounded Account Recovery

Registered on 2026-07-18 before the final recovery run. This engineering-only
addendum follows the original completion contract (SHA-256
`702ab2e84e0c6acdc5c14acd251a60a63f8fdca68928b0109b2d440999876cc8`)
and first replay-recovery addendum (SHA-256
`d572818f7098a4ffda52c325881a98e49ed952b01b626c4e478c5288cb580095`).
Discovery outcomes remain spent; this is not a new sample or replication.

## Second preserved failure

The clean recovery evaluator at
`43cc5f9af3dde0136292aab14273e8a4d8b5f3ad` began at 2026-07-18 00:41:00
Europe/London. It passed every original and recovery-contract identity before
reopening the same discovery outcomes. Removing per-write `fsync` improved
throughput, but timestamp-only progress in dense 2023 data showed that a
Windows temp-file/rename per account transaction would still miss the
45-minute cap. The run was stopped prospectively at approximately 19 wall
minutes, before its cap, and renamed rather than deleted:

`reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/.phase3-analysis.failed-atomic-2026-07-18`

It contains 4,571 files and 66,504,399 bytes, including 4,570 partial LONG
transaction segments through 2023-10-29. Only process/file counts and account
event timestamps were inspected. No return, path, characteristic, portfolio,
or candidate-score value was inspected. This root is a failure receipt and is
not an input to the final recovery.

The first failed attempt used approximately 74 wall minutes and the second
approximately 19, for about 93 cumulative minutes. The final recovery has a
24-minute wall-clock stop, keeping cumulative Phase-3 analysis compute below
two hours.

After the stop, a synthetic outcome-free stress check identified a second,
non-I/O bound: 100 LONG round trips plus one CONTINUOUS round trip completed and
fully verified in 7.6 seconds, while 1,000 plus one did not complete within 124
seconds. Production uses one persistent component per trade, so historical
state copying/hashing is legitimately superlinear; changing or pruning that
state would alter production semantics. The completed partial LONG journal had
1,899 round trips, while the partial CONTINUOUS journal already had 867. A full
account replay cannot honestly fit the remaining stage budget.

The original stop-loss requires reducing the claim when the blocker cannot be
repaired in time. Full path, characteristic, lifecycle, funding, ledger, and
curve computation remain required, but the final portfolio is not described as
exhaustively account-replayed.

## Frozen buffered-persistence and account-sample change

For each sleeve, sort the full executed ledger by the lowercase SHA-256 of its
`source_key` UTF-8 bytes, with `source_key` as the collision tie-breaker, and
select the first 100 rows (or all rows when fewer). This key-only rule is fixed
before the final output and cannot use return, exit reason, symbol, or date.
The final recovery passes only this sample through the account kernel. It must
report sampled and full-ledger trade counts, the exact sample-key identity, and
`account_scope = bounded_key_sample_100_per_sleeve`.

The final recovery may otherwise alter only the single-process Windows
research adapter:

1. retain each account kernel transaction as its immutable ordered event batch
   in process memory during a sleeve replay;
2. suppress the rebuildable JSONL projection during that replay;
3. after all sampled decisions for the sleeve, pass every original batch, in original
   order and with its original boundary, through the unmodified production
   transaction serializer/hash builder;
4. write the resulting canonical transaction bytes directly into the unique
   ignored root without per-file temp/rename or durability flush;
5. reconstruct the complete projection once from the materialized events; and
6. run the unchanged authoritative journal reader, event/state hash checks,
   reducer, fill-count reconciliation, and final-flat checks before reporting.

The buffer may not combine, split, reorder, mutate, omit, or fabricate a
sampled account transaction or event. The production account kernel, reducer,
execution twin, event construction, decisions, prices, lifecycle, costs,
funding, arithmetic, estimators, selection rule, discovery inputs, bootstrap
seed, and four final payloads remain frozen. A failed or interrupted direct
write invalidates the run and is detected by final verification.

This adapter has no exhaustive-ledger, crash, atomicity, concurrency, POSIX
durability, runtime, demo, paper, mainnet, or real-money claim. It proves only
the sampled numerical/event equivalence that survives complete end-of-run
verification. Full ledger/curve values remain model-based diagnostics and may
not be called account-reconciled.

## Stop and decision boundary

The clean committed final recovery must record this addendum's path and SHA-256
in preflight/final identity and finish within 24 wall minutes. Both prior
failure roots remain untouched. If it fails, Phase 3 closes with even bounded
account evidence unavailable; there is no fourth same-surface attempt.

A successful run completes the original exploratory Phase-3 analysis. Phase-4
selection remains mechanical under the original rule, and no holdout label may
be generated before a selected thesis has its own committed preregistration.
