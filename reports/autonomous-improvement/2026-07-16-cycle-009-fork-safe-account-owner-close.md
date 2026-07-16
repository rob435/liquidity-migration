# Autonomous improvement cycle 009: fork-safe account-owner lease release

## Finding

- Audit timestamp: `2026-07-16T02:14:21Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- `AccountOwnerLease.held` correctly rejected an inherited object in a fork
  child because the holder pid differed from `os.getpid()`.
- `AccountOwnerLease.close()` did not make the same ownership check. It always
  issued `flock(LOCK_UN)` before closing the descriptor.
- POSIX `flock` state follows the inherited open-file description. The child's
  explicit unlock therefore released the parent's kernel lock as well, while
  the parent's path/inode checks continued to report `held=True`.
- The current account-service runner does not intentionally fork after
  acquiring mutation authority, reducing present likelihood. The consequence
  is still severe: any future forked helper, library behavior, or cleanup path
  could silently remove the sole-owner boundary around venue mutation.

## Prospective reproduction

The regression acquires a lease, forks a child that observes `held=False` and
calls `close()`, then starts a separate contender while the parent remains in
the lease context.

Before implementation:

```text
child held before close: false
parent held after child close: true
contender: acquired
```

The contender should have received “already held.” The test failed on that
assertion. After implementation the same contender is blocked. The final test
runs the fork sequence in an isolated single-threaded subprocess so Python 3.13
does not warn about forking the already multi-threaded full pytest process.

## Implementation

- `close()` captures the recorded holder pid before clearing local object state.
- Only the original holder process may call `LOCK_UN`.
- A fork child closes its duplicate descriptor without an explicit unlock. The
  parent's still-open descriptor keeps the shared open-file-description lock
  alive.
- Normal same-process context-manager release and error cleanup are unchanged.

## Validation

- Lease, Bybit mutation-client, and owner-readiness focus: 114 passed in 0.53
  seconds before isolating the fork harness; the isolated regression passes
  with deprecation warnings promoted to errors.
- Fork regression repeated 50 times against the final ownership rule without a
  failure.
- Full local pytest suite after the isolated harness: 1,613 passed in 23.58
  seconds with no warning summary.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- `git diff --check`: passed.
- Independent locked Python 3.11.5 lease suite: 9 passed; all 26 dependency
  pins matched and `pip check` was clean.
- Independent fork regression: 50/50 passes; an additional exec-based probe
  confirmed child double-close is safe, contenders remain blocked while the
  parent holds, and acquisition succeeds after parent release.
- Independent repository-wide Ruff, package mypy (85 modules), and
  `git diff --check`: passed.

No account API, VPS, or other external system was contacted. This changes only
local mutation-authority lifetime semantics; it does not enable operation.

## Residual scope

- The lease still relies on a host-local filesystem and `flock`; it is not a
  distributed lease across hosts.
- A fork child that neither closes the inherited descriptor nor execs/exits can
  prolong the lock after the parent closes. Current account runners do not fork
  after acquisition.
- `held` verifies pid, descriptor/path type, link count, device, and inode, but
  cannot directly query whether another process explicitly unlocked a shared
  open-file description. Preventing non-holder `LOCK_UN` closes the repository's
  known path to that false-positive state.
