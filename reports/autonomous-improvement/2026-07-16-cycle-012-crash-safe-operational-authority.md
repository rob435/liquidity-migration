# Autonomous improvement cycle 012: crash-safe operational authority

## Finding

- Audit timestamp: `2026-07-16T13:02:38Z`; final acceptance:
  `2026-07-16T13:31:32Z`.
- The audit used committed base
  `d552941a9edb36c5256fe68eed298e73594d8164` while preserving the dirty tree
  produced by prior autonomous cycles and unrelated operator work.
- Operational-authority issuance still created the final receipt directly at a
  consumer-loadable mode before its data and parent directory were durable. A
  concurrent runtime could therefore accept authority before the issuer's
  durability boundary, and an interrupted or late-failing issuer could expose
  an ambiguous final artifact.
- Issuance captured Git, machine, environment, input, and runtime-root evidence
  once. Those sources could change after capture and before success, producing
  a receipt that the next verification would immediately reject.
- Checkout validation inherited caller Git state and the ordinary index. The
  audit reproduced false-clean results using redirected Git state,
  `skip-worktree`/`assume-unchanged`, and replacement objects.
- Adversarial review then reproduced different raw tracked bytes passing under
  repository-local clean filters and LF normalization. The index-based check
  also executed the configured clean command as the root issuer. A later review
  reproduced an untracked executable appearing during the tracked scan and
  escaping a single early untracked enumeration.
- The production operator path did not join the deploy/reset maintenance-lock
  boundary and did not prove the exact managed systemd fleet inactive. A
  cooperating deploy/reset or a still-active managed service could overlap
  issuance.
- The exported issuance API allowed an unprivileged direct caller to publish a
  user-owned receipt and report success even though the module's loader then
  rejected that receipt for not being root-owned.
- Raw module issuance could acquire a fallback maintenance lock only after
  importing checkout code, too late to prove the supported pre-import boundary.

This was operational safety and reliability work, not strategy research. No
strategy decision, signal, universe rule, ledger key, or continuous numeric
output changed.

## Implementation

- Added a reusable private-receipt publisher with descriptor-held parent
  validation, bounded writes, create-only randomized staging, file and
  directory fsyncs, identity-bound cleanup, no-replace hard-link publication,
  and repeated owner/mode/mount/path checks. The final name remains single-link
  mode `0400` and unloadable throughout semantic and source revalidation;
  descriptor `fchmod` to the profile's `0600` or `0640` is the sole authority
  commit.
- Reworked operational-authority issuance to validate the exact staged payload,
  reopen and compare all bound facts before and after final-name publication,
  cap receipts at 1 MiB, require the authorization owner at the public API
  boundary, and keep the existing schema-v2 payload and consumer modes.
- Isolated checkout verification behind a trusted absolute Git executable,
  explicit Git directory and work tree, minimal environment, disabled hooks,
  fsmonitor, replacement objects, external index/config/excludes, and a fresh
  temporary index used only for untracked enumeration. It brackets a
  descriptor-rooted raw tree scan with exact `HEAD^{commit}`, binary tree
  manifests, and two non-ignored-untracked checks. Every tracked regular file
  and symlink target is compared byte-for-byte with `cat-file --batch` object
  content; type, Git executable mode, and final metadata are rechecked. No clean
  filter runs, schema-v2 explicitly requires SHA-1 object storage, and gitlinks
  fail closed.
- Added an exact fixed-environment systemd observer for the nine managed
  services and three timers. Every unit must be loaded and inactive before
  capture and during both later precommit source checks.
- Added a dedicated `ops.sh` issuance route. The shell opens and non-blockingly
  flocks the canonical maintenance
  inode plus legacy deploy/reset leaves before changing into or importing the
  deployed checkout. The helper and issuer then revalidate the inherited
  descriptors and retain them until publication returns. Raw module issuance
  refuses to proceed without that exact inherited handoff.
- Updated operator, account-execution, and systemd documentation with the
  actual publication states, lock ordering, exact inactivity requirement, Git
  isolation, root/size constraints, recovery implications, and cooperative-host
  limitations.

## Prospective regressions

New and expanded tests cover:

- successful `0600`/`0640` publication and failures before link, after link,
  after source mutation, during fsync, on collision, and after parent redirect
  or late permission weakening;
- descriptor/content tampering, hard-link count, mount identity, forbidden
  output roots, unsafe parents, bounded size, staging exhaustion, and
  identity-safe cleanup when a foreign final inode appears;
- operational source mutation after payload hashing and at both precommit
  revalidation phases, plus rejection of an interrupted mode-`0400` final file;
- ambient Git redirection, hidden tracked changes, replacement refs, and a
  changing `HEAD`, together with the exact isolated command/environment
  contract;
- clean-filter and CRLF normalization false passes, proof that repository-local
  filter commands never execute, raw executable-mode and symlink comparison,
  NUL-delimited and unusual paths, malformed manifests, gitlink rejection,
  FIFO/symlink-ancestor safety, early-entry mutation, and late untracked-file
  creation;
- complete, loaded, inactive systemd state for the exact 12-unit manifest and
  rejection of active, failed, unloaded, malformed, or incomplete observations;
- direct-API owner enforcement, inherited-lock marker and descriptor
  revalidation, missing-handoff refusal, and pre-import shell ordering.

An early private-index implementation omitted `update-index --refresh`; the
integrated gate correctly exposed clean worktrees as dirty. Adding the refresh
fixed that symptom, but adversarial review showed that refresh/diff path both
normalized bytes and executed clean filters. Those operations were removed in
favor of raw object-byte comparison. A later review found the single untracked
enumeration race; bracketing checks closed it. All affected regressions and both
full runtime suites pass after the final fixes.

## Measurement

The hardened checkout verifier intentionally spends more work to remove false
clean states. On a clean no-hardlink clone of the committed base, 30 alternating
warm-cache calls measured:

- prior verifier: median 23.620 ms, p95 26.204 ms;
- hardened raw verifier: median 122.728 ms, p95 157.091 ms;
- median cost: +99.108 ms (`5.196x`).

This is a local descriptive microbenchmark of one verifier call, not VPS or
end-to-end issuance latency. The clone contained 271 tracked blobs totaling
4,159,603 bytes. Issuance is rare and safety-critical; the measured cost is
accepted rather than hidden as a performance improvement.

## Validation

- Final full local Python 3.13 suite: 1,853 passed, 1 skipped in 47.88 seconds.
- Final full locked Python 3.11 suite: 1,853 passed, 1 skipped in 47.98 seconds.
  The skip is the expected Linux-only live mount-identity case on macOS.
- Final focused authority gate: 45 passed on each runtime.
- Repository-wide Ruff: passed on both runtimes.
- Package-wide mypy: 90 source files passed on both runtimes.
- Every shell script under `scripts/` passed `bash -n`.
- `pip check` passed on both environments.
- `git diff --check`: passed.
- Independent adversarial reviews found late parent permissions, direct-API
  ownership, clean-filter execution/normalization, post-import fallback locking,
  and late untracked creation. Every finding was fixed with a regression. Final
  raw-checkout and helper/adapter reviews reported no blocker for the documented
  demo/paper scope.

No Bybit API, VPS, workflow, deployed service, or real-money path was contacted.
No research or backtest was run. The work does not authorize deployment or
real-money operation.

## Residual scope and next candidates

- This is a cooperative, root-controlled-host protocol. Advisory locks and
  systemd observations do not detect unmanaged/manual processes or constrain a
  hostile privileged actor. Sequential systemd reads are point-in-time evidence,
  and authorization is not continuously revoked when later state changes.
- Tracked content is compared to raw Git object bytes, but ignored paths and
  mutable repository exclude rules remain outside the cleanliness claim. Git
  records only owner-executable state, and hard-linked tracked regular files are
  accepted. Schema-v2 supports the repository's 40-character SHA-1 commit
  contract and rejects SHA-256 repositories. Receipts remain unsigned local
  evidence, not remote attestation or WORM storage.
- A hard kill can leave a randomized mode-`0400` staging sibling, or a
  mode-`0400` final file before commit. Neither is loadable, but both require
  deliberate administrative inspection and cleanup. A kill after mode commit
  can leave a valid receipt before the CLI reports success.
- Linux mount-ID logic has source and mocked coverage, but its privileged live
  bind-mount case remains skipped on macOS and was not exercised on a VPS.
- Reset publication and operational publication now implement the same state
  machine through separate code paths. Migrating reset onto the neutral helper
  is a worthwhile behavior-preserving maintainability candidate, but it should
  be proven with exact failure-state equivalence rather than assumed mechanical.
- The stronger evidence-integrity candidate remains semantic reconciliation of
  optional reset archive-manifest members and bounding `ledger_reset_utc` by the
  measured reset interval. A privileged Linux mount-namespace integration
  harness is the other high-value operational candidate.
