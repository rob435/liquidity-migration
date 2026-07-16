# Autonomous improvement cycle 004: retry empty incremental tails

## Finding

- Audit timestamp: `2026-07-16T01:41:11Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- `_download_symbol_dataset()` correctly withheld completion markers for an
  empty fresh response, but wrote a full requested-range marker whenever an
  incremental request was `tail_only`, even if that tail returned no rows.
- `write_dataset()` creates the dataset directory for an empty frame but stores
  no parquet rows. The new marker therefore claimed evidence that did not exist.
- A same-range retry then returned cached without calling the provider; later
  wider requests began after the poisoned end boundary. This could permanently
  suppress recovery of a transiently empty interval.

All 13 per-symbol download routes use this helper. Eleven un-clamped routes are
directly exposed; the rolling-window open-interest/taker routes are exposed when
their effective marker window covers the requested start. No caller supplies an
authoritative fact that an empty provider response proves an interval empty.

## Prospective reproduction

The regression seeded proven coverage `[10,20)`, requested `[10,30)`, and queued
an empty provider response followed by a recoverable row at timestamp 25.
Before the fix, the first call wrote `BTCUSDT_10_30.done`; the assertion that the
marker remain absent failed, and the recovery response would never be consumed.

After the fix:

```text
fetch calls: [(20, 30), (20, 30)]
marker after first empty tail: absent
marker after recovered row: present
persisted timestamps: [25]
```

## Implementation

- A completion marker is now written only after a non-empty postprocessed frame
  is durably stored.
- Empty fresh and empty tail responses both remain retryable.
- The earlier prefix marker is retained, so retrying an empty extension still
  fetches only the missing tail rather than re-downloading proven history.
- Logging distinguishes empty fresh and tail fetches.
- Marker documentation now describes successful non-empty coverage rather than
  attempts.

## Validation

- Downloader suite: 54 passed locally in 0.27 seconds.
- Full local pytest suite after this cycle: 1,605 passed in 21.43 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- Locked Python 3.11.5 suite: 54 passed in 2.35 seconds.
- Locked mypy 1.20.2: 85 modules passed.
- Locked Ruff 0.15.14: passed.
- `git diff --check`: passed in the final combined worktree.

This is a correctness/recoverability change. No throughput improvement is
claimed, so no benchmark applies. A genuinely empty tail may now be queried
again on future refreshes; that is the deliberate fail-safe tradeoff until a
provider supplies durable authoritative empty-coverage evidence.

The locked install again warned that Polars 1.41.0 and its runtime package are
yanked without a supplied reason. Dependency replacement remains separate.
