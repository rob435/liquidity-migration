# Autonomous improvement cycle 008: never age-evict a live lock owner

## Finding

- Audit timestamp: `2026-07-16T02:08:56Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- `exclusive_file_lock()` first checked whether a valid lock's pid/token owner
  was dead. When that returned false, it nevertheless deleted the same lock
  solely when its file mtime exceeded `stale_seconds`.
- A long-running, paused, or slow live holder could therefore lose its mutex.
  The contender entered a second critical section while the first continued.
- The primitive protects LONG/CONTINUOUS cycle singletons, account journal and
  inbox/capture operations, route initialization, strategy replay, and every
  dataset read/write. Thresholds ranged from 600 seconds for account state to
  21,600 seconds for datasets; duration changes likelihood, not correctness.

## Prospective reproduction

An independent two-process probe aged a valid holder's lock while the holder
remained alive, then started a contender:

```text
holder critical section:    0.000s - 0.502s
contender critical section: 0.153s - 0.206s
overlap: true
process exit codes: 0, 0
```

The checked-in regression uses a spawned holder process and a parent-process
contender, deliberately ages the valid lock past a 50ms compatibility timeout,
and observes a 250ms exclusion interval. Before the fix it failed because the
contender entered during that interval. After the fix, the contender enters
only after the holder is explicitly released.

This is a correctness measurement, not a throughput benchmark.

## Implementation

- Valid locks are now removed only when pid/token evidence proves their owner is
  dead or the pid was reused by a newer process.
- A valid live or unknown owner is never overridden by wall-clock age. Elapsed
  time cannot prove that a critical section ended.
- `stale_seconds` remains accepted for call-site compatibility but cannot
  override live-owner evidence. Its former behavior was intrinsically unsafe.
- Malformed lock payloads retain their separate bounded grace and dead/reused
  pid recovery remains unchanged.
- Comments and docstrings now describe evidence-based eviction rather than an
  age backstop.

## Validation

- Storage suite: 28 passed locally in 0.99 seconds.
- Major lock-caller suites (account kernel/route/service, target replay, LONG,
  and CONTINUOUS): 203 passed in 1.23 seconds.
- Full local pytest suite after this cycle: 1,612 passed in 21.15 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- `git diff --check`: passed.
- Locked Python 3.11.5 storage suite: 28 passed in 1.06 seconds; major callers:
  203 passed in 2.45 seconds; all 26 dependency pins matched.
- Independent 25-iteration aged-live-holder stress: zero overlaps. Contenders
  remained excluded for 80.292-90.242ms and acquired after explicit release in
  every iteration.
- Independent package mypy (85 modules), repository Ruff, and
  `git diff --check`: passed.
- The multiprocess regression uses `try/finally` cleanup so an earlier
  diagnostic assertion cannot strand its holder until timeout.

No strategy or numerical decision behavior changed, no research was run, and no
external system was contacted.

## Safety tradeoff and next candidates

- A live but wedged owner now blocks contenders until the process exits or an
  operator terminates it. That is the safe mutex behavior; admitting concurrent
  writers is not recovery.
- The create/unlink protocol still has smaller time-of-check/time-of-use risks,
  especially malformed-payload grace and a successor appearing between dead
  owner inspection and unlink. Replacing the primitive with a persistent kernel
  `flock` would remove stale-file inference entirely, but requires a separately
  tested cross-process migration rather than being folded into this narrow fix.
- `AccountOwnerLease.close()` also needs a fork-safety follow-up: an inherited
  child descriptor can currently issue `LOCK_UN` against the parent's shared
  open-file description. The active service does not presently fork after lease
  acquisition, so it ranks below this shared, demonstrated storage overlap.
