# Autonomous improvement cycle 011: persistent locks and reset publication hardening

## Finding

- Audit timestamp: `2026-07-16T12:26:50Z`.
- The audit began from `cd2abdcbf87869af924d4ae931c15852e0d4b80d` and
  was revalidated after the working base advanced to
  `d552941a9edb36c5256fe68eed298e73594d8164`. Unrelated local and committed
  operator work was preserved.
- Dataset serialization still used a create/PID/payload/unlink protocol. Its
  pathname could be replaced between checks, PID and age inference were weaker
  than kernel ownership, and unlinking a released lock allowed mutually
  exclusive processes to hold different inodes. The protocol also paid JSON,
  liveness, and namespace churn on every uncontended acquisition.
- The destructive demo/paper reset had several related trust-boundary gaps:
  pathname-based archive and tree traversal, replaceable maintenance and owner
  lock leaves, incomplete paper-owner exclusion, ambient Git/PATH/config trust,
  and a receipt path that could become loadable before every final source,
  service, Git, and root check had passed.
- Deploy cleanliness could be hidden by index flags and Git replacement refs.
  Reset provenance could similarly interpret `HEAD` through `refs/replace`.
- Owner wrappers created account routes before acquiring the canonical owner
  lease. A competing or misrouted process could therefore mutate authority
  state before exclusion was established.
- Receipt archive validation loaded the complete compressed archive into
  memory. The measured 67,130,441-byte fixture made the avoidable memory cost
  concrete.

This was operational hardening, not strategy research. No strategy decision,
signal, universe rule, ledger key, or continuous numeric output was changed.

## Implementation

- Replaced the dataset create/unlink lock with a persistent inode and kernel
  `flock`. Directory and leaf identities are descriptor-validated, lock
  descriptors are registered across their whole waiting/held lifetime, and an
  at-fork barrier closes inherited descriptors and rebuilds vanished-thread
  mutex state in the child. Compatibility age arguments no longer influence
  ownership.
- Added descriptor-rooted reset path, archive, and epoch helpers. They reject
  symlink, hard-link, ownership, permission, path-redirection, and Linux mount
  identity violations; clear account epochs in place while retaining persistent
  locks; normalize demo/paper trees; bind archive output to an opened directory;
  and verify the durable archive before clearing begins.
- Unified deploy and reset under a persistent host maintenance lock while
  retaining legacy lock leaves during migration. Both demo and paper account
  owner leases are acquired through validated inherited descriptors, retained
  across archive/reset, and revalidated immediately inside the clear process.
- Reordered both account-owner runners to derive the route read-only, acquire
  the owner lease, revalidate it, and only then create or update route state.
- Isolated privileged Git invocations from inherited environment and system
  configuration, fixed executable paths, disabled hooks/fsmonitor/replacement
  objects, required commits rather than arbitrary tree-ish objects, bracketed
  the expected `HEAD`, and used a private temporary index. This detects tracked
  changes concealed by `skip-worktree` or `assume-unchanged` and ignores
  `refs/replace` when proving the executed commit and checkout.
- Made reset receipts descriptor- and mount-rooted, size-bounded, and
  independently recheck archive, fresh roots, Git, systemd, and source evidence.
  Publication now stages a private sibling at mode `0400`, fsyncs it, links it
  no-clobber to the final name, removes the stage link, repeats all mutable
  checks while the final remains unloadable, and uses descriptor `fchmod(0600)`
  plus `fsync` as the sole success commit. Interrupted `0400` files are never
  accepted as successful receipts.
- Streamed receipt archive hashing and gzip/tar validation instead of retaining
  the archive bytes in memory.
- Fixed privileged command resolution to a system-only PATH and an absolute,
  non-symlink `systemctl`; reset receipt service checks are independent rather
  than inferred from one unit.
- Updated operational documentation to state the actual guarantees and the
  remaining cooperative-host, ignored-path, point-in-time, and cross-root
  limitations rather than claiming atomic rollback or hostile-root resistance.

## Prospective regressions

New and expanded tests cover:

- uncontended, contended, process-death, path-replacement, directory-replacement,
  fork, inherited-descriptor, and persistent-inode storage lock behavior;
- lock and reset behavior under both local Python 3.13 and the exact supported
  Python 3.11 runtime;
- archive source/output redirection, unsafe permissions and ownership, hard
  links, mount aliases, digest mismatch, descriptor races, and durable sidecars;
- batch preflight followed by an exact descriptor rescan at the destructive
  boundary;
- both account-owner leases being held and identity-revalidated in the clear
  process, plus owner-lease-before-route ordering and failure cleanup;
- staged receipt failures before link, after link, during late callbacks, on
  final-name collision, parent redirect, service change, Git change, oversized
  evidence, and source/root/mount changes. Every observed pre-commit final file
  remains mode `0400` and unloadable;
- deploy/reset rejection of dirty tracked files hidden by index flags and of a
  worktree made to look clean through Git replacement objects; and
- macOS group/ownership portability for operational authority contracts.

## Measurements

- Persistent lock microbenchmark: five alternating current-versus-`HEAD` runs,
  5,000 uncontended acquisitions per run. The pooled median fell from
  119.666 microseconds to 90.250 microseconds (`-24.58%`), and p95 fell from
  171.625 microseconds to 112.750 microseconds (`-34.30%`). The correctness
  change therefore did not trade away uncontended performance.
- Receipt archive validation: five fresh subprocess runs against the same
  67,130,441-byte archive. Median wall time fell from 0.098359459 seconds to
  0.038746667 seconds (`-60.61%`). Peak macOS `ru_maxrss` fell from the raw
  reading 273,268,736 to 40,681,472 bytes (`-85.11%`). RSS units are reported
  explicitly because `ru_maxrss` units are platform-dependent.

These are local descriptive microbenchmarks, not claims about end-to-end VPS
latency or throughput.

## Validation

- Final full local Python 3.13 suite: 1,802 passed, 1 skipped in 46.86 seconds.
- Final full locked Python 3.11 suite: 1,802 passed, 1 skipped in 49.91 seconds.
  The one skip is the expected Linux-only live mount-identity case on macOS.
- Focused integrated safety gate: 291 passed, 1 expected skip.
- Replacement-object regression: 17 passed on each runtime.
- Repository-wide Ruff: passed on both runtimes.
- Package-wide mypy: 89 source files passed on both runtimes.
- Every shell script under `scripts/` passed `bash -n`.
- `pip check` passed on both environments.
- `git diff --check`: passed.
- Independent adversarial review found the replacement-ref gap; after its fix
  and regression test, the final review reported no concrete blocker for the
  documented demo/paper scope.

No Bybit API, VPS, workflow, deployed service, or real-money path was contacted.
No research or backtest was run. The work does not authorize deployment or
real-money operation.

## Residual scope and next candidates

- The protocols assume a cooperative, root-controlled local host and a
  flock-capable filesystem. They do not defend against a concurrently hostile
  privileged process, unmanaged writers, storage corruption, or arbitrary I/O
  failure across multiple account roots.
- Linux mount-ID behavior has source/mocked coverage, but the live privileged
  bind-mount case remains skipped on macOS and was not exercised on a VPS.
- Repository-local Git attributes, filters, excludes, and ignored runtime paths
  remain outside the tracked-checkout provenance claim. Receipts are
  point-in-time local evidence, not signed remote attestation or WORM storage.
- The reset manifest is integrity-hashed, and required fresh epoch roots are
  independently checked, but optional `archived_targets` and
  `preserved_risk_state` fields are not fully semantically reconciled to every
  tar member. `ledger_reset_utc` is format-validated but not bounded by the
  measured reset interval.
- A hard kill can leave a randomized mode-`0400` staging sibling. It cannot be
  loaded as a successful receipt, but it requires later administrative cleanup.
- Bash must open inherited lease descriptors before the Python helper can
  revalidate them. Private `0700` parents, host quiescence, and immediate receipt
  validation constrain that unavoidable shell-open boundary.
- The next highest-value candidate is the operational runtime-authority receipt,
  whose direct final-path publication should receive the same descriptor-bound,
  staged/unloadable, crash-safe success-commit treatment. After that, reconcile
  optional archive-manifest semantics and add a privileged Linux bind-mount
  integration harness.
