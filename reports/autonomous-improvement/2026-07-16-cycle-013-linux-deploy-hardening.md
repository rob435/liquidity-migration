# Autonomous improvement cycle 013: Linux deploy hardening

## Finding

- Audit and cutover window: `2026-07-16T13:31:32Z` through
  `2026-07-16T15:08:54Z`.
- This pass deployed the cycle-012 operational-authority work through the
  real stopped-install and activation path on the demo/paper VPS. That exposed
  two Linux-only reliability defects that the macOS suite and mocked mount
  tests did not reproduce.
- The reset-path normalizer retained one `O_PATH` descriptor for every visited
  directory until the whole operation ended. The first stopped install failed
  closed at descriptor 1023 with `Linux mount identity cannot be read`, before
  any managed unit was started.
- After that was fixed, activation exposed an intentional systemd sandbox
  boundary: `ReadWritePaths` bind-mounts the paper account root inside the
  non-root service namespace. The strengthened lease validator treated the
  private parent mount as hostile and stopped the paper owner before producers
  could start.

This remained operational safety and reliability work. No strategy signal,
decision rule, universe, leverage, target, ledger key, or research conclusion
changed. No research or backtest was run.

## Implementation

- Bounded reset-path directory descriptors and explicitly released transient
  setup descriptors. Wide-tree and many-root regressions hold at most three
  cached directory descriptors while retaining descriptor-relative traversal,
  mount checks, entry identity checks, and fail-closed behavior.
- Kept the historic paper mutex at
  `ACCOUNT_EXECUTION_ROOT/account_execution_owner.lock`, so the paper runner,
  reset tool, and older deployments still contend on one inode. A new
  cross-component regression prevents future split-lock drift.
- Added one narrow paper-runner opt-in for systemd's private-parent bind mount.
  It is rejected for root, rejected for the canonical demo lease, requires a
  non-root euid-owned mode-`0700` parent on the same device, and still requires
  a single-link mode-`0600` leaf on the parent's exact mount. Prepared parent
  and leaf mount identities remain pinned and revalidated after open, after
  `flock`, and for the full lease lifetime. The default path remains strict.
- Added positive lifetime revalidation, root-refusal, default mount-boundary,
  route-wiring, monkeypatch-forwarding, and reset/runner path-identity tests.

## Measured evidence

- Before: the live normalizer exhausted the Linux soft descriptor limit and
  failed at descriptor 1023.
- After: the successful live normalizer held 13 descriptors after 160 seconds
  and 11 after 308 seconds. It traversed the same large runtime trees and
  completed with `units_started=0`.
- The stopped install normalized 57,201 continuous-paper entries and 68,338
  LONG-paper entries, and preflighted 56,449 continuous-demo plus 67,726
  LONG-demo entries. This is live VPS evidence, not a synthetic benchmark.
- Installation still took roughly seven minutes because every historical tree
  is revalidated. The descriptor reduction is proven; an install-latency
  improvement is not claimed.
- The paper owner then started successfully inside the real systemd sandbox,
  published generation-bound health, and retained zero restarts.

## Validation and deployment

- Final locked local gate: 1,858 passed, 1 skipped; Ruff and mypy passed.
- Focused lease, route, and reset-contract gate: 83 passed.
- Exact-commit GitHub Actions run
  [29507633562](https://github.com/rob435/liquidity-migration/actions/runs/29507633562)
  passed dependency installation, Ruff, mypy, and pytest for
  `14d49d593ad7f1ad3e09162b48dc58221c52983d`.
- Stopped install passed with exact commit
  `14d49d593ad7f1ad3e09162b48dc58221c52983d` and `units_started=0`.
- Operational authority was issued create-only for the exact commit and
  `operational` profile. Receipt SHA-256:
  `dfa12a9a70eb5338a4e5964318ca97c634d9f1a4058d8a9d9b52b4ae709707bd`.
- Built-in activation and a separate status run both returned `verify-ok` for
  the exact commit. Demo order permission and immutable sizing-prior checks
  passed.
- Six long-running demo/paper units were active, enabled, and at zero restarts;
  RMOM, hedge, and liveness timers were active and waiting. Both owner-health
  records were fresh, healthy, generation-bound, and journal-bound.
- Demo and paper journals hash-verified with zero fills, working orders,
  non-flat positions, or nonzero component targets. Fresh authenticated Bybit
  demo reads found zero positions and zero regular or conditional orders;
  `REAL_MONEY=false` and real credentials were absent.
- An intentionally early manual watchdog run truthfully reported cold-start
  CONTINUOUS cycle staleness. The next run resolved those alerts but caught a
  transient stale ETH book and the paper follower's first empty cycle. The demo
  owner recovered without restart; the next two paper cycles each carried
  204,039 kline rows. The final watchdog at `2026-07-16T15:08Z` reported zero
  active alerts across ten monitored units.
- The paper producer logged public-WebSocket ping/pong timeouts during warm-up.
  It did not restart and subsequently completed the two populated cycles above;
  that recovery is point-in-time evidence, not a sustained-connectivity claim.
- No venue-accounting sufficiency claim is made: the account has zero fills, so
  the registered trade/closed-PnL/funding sample floors cannot pass.

## Residual scope and next candidates

- Make stopped installs faster only with measured equivalence. A content- or
  metadata-bound incremental normalizer is a candidate, but it must preserve
  mount, ownership, link, and mutation-race checks.
- Add a privileged Linux/systemd integration harness for `ReadWritePaths` and
  mount-ID lifetime changes. Mocked tests plus this live cutover are useful but
  not a repeatable CI boundary.
- Treat cold-start liveness as an operational sequence, not a reason to loosen
  the ten-minute health threshold. A deployment smoke command that waits for
  one fresh cycle per enabled sleeve before running the watchdog would remove
  avoidable alert noise without weakening monitoring.
- GitHub Actions warned that Node.js 20 actions are deprecated and forced onto
  Node.js 24. Updating official action majors is a small automation candidate
  after checking their current release and migration notes.
- Venue/accounting evidence remains structurally incomplete until real demo
  fills, closed PnL, and funding rows exist. Runtime health, paper cycles, and
  order permission do not substitute for that evidence.
