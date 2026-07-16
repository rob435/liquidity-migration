# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, generation-bound strategy receipts, and watchdog evidence
at 2026-07-16 17:54 UTC. These facts describe the deployed implementation
commit below; a later documentation-only receipt may leave the branch ahead.

## Live authority and topology

- Installed and authorized implementation commit:
  `6c27b5e6052d517d31196a154548becb75a0ab62`, profile `operational`, receipt
  SHA-256 `b4d86d0b63044385563176bc521cb996f404556b190b7cbad33db7e29e2da3bb`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. An on-demand watchdog run under the new exact authority exited zero
  at 17:53 UTC with no active alert across all ten monitored units.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- Authenticated Bybit demo reads before install showed zero non-flat positions
  and zero regular or conditional orders. A second authenticated read after
  activation again observed zero non-flat venue positions and zero all-kind or
  conditional orders. Demo and paper journals hash-verified with zero working
  orders, non-flat positions, or nonzero aggregate targets.
- Demo and paper owners publish healthy generation-bound status. Demo
  reconciliation is healthy with zero mismatches; both reported exact
  queue-head readiness, zero restarts, and fresh owner health after activation.
  The demo inbox had zero pending, processing, or failed requests. Live-L2 and
  owner-health ages are measured in seconds. With no active work, each owner
  subscribes only to the idle BTC book.
- All four target producers have current-generation completed-cycle receipts
  bound to their exact durable cycle. LONG demo/paper reported 231,787 current
  kline rows; CONTINUOUS demo/paper reported 371,056. Completion-age liveness
  was clean after publication. Paper is explicitly
  `integration_only_uncalibrated`; its cycles are routing/lifecycle evidence,
  not performance or fill-quality evidence.
- The prior demo owner retained raw depth for the full universe, reached about
  2.75 GiB resident memory, and stalled reconciliation through swap pressure.
  The bounded owner remains near 93 MiB after cutover.
- Live cgroup evidence found the first LONG demo soft threshold below its
  working set. Soft thresholds were retuned from measured footprints while all
  hard maxima remained unchanged. At the latest sample the 4 GiB host had about
  2.3 GiB available; full memory-pressure averages were 0.22% over 60 seconds
  and 0.49% over 300 seconds, with no managed-unit restart.
- A pre-evidence BTC-risk state file was rejected rather than migrated. With
  zero authoritative CONTINUOUS trade rows and zero pending requests, it was
  archived at
  `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
  with SHA-256
  `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`.

## Incident interpretation

- The 16:50 and 17:05 `future_book` alerts were local read/update races, not
  future venue timestamps. The owner sampled wall time before acquiring the
  recorder snapshot, so a concurrent WebSocket update could publish a newer
  local receive timestamp between those operations. Book freshness now pairs a
  locked snapshot with the recorder's wall-time observation; the same ordering
  is used for notification midpoints. A real backward wall-clock step remains
  fail-closed, and deterministic regression coverage reproduces the former
  interleaving.
- The reported negative owner-health ages were not future venue data or clock
  drift. Strategy event time was reused after concurrent owner heartbeats.
  Operational freshness now samples adjacent wall time while strategy/PIT time
  remains unchanged.
- The stale L2/reconciliation alerts were genuine symptoms of the old owner's
  unbounded memory retention. Dynamic subscriptions and bounded state removed
  that cause.
- The 15:00--15:05 CONTINUOUS hung, owner queue-head, and paper empty-store
  alerts mixed causal cycle-start time with completion health and accepted
  unbound old-generation evidence. Liveness now consumes a strict receipt
  published only after durable cycle evidence, binds it to the current systemd
  invocation and exact cycle, and uses the producer manager's actual current
  store size. A no-receipt service generation receives at most the existing
  ten-minute SLA as startup grace; only the exact queue-head warm-up state is
  suppressed inside that bound. Unknown or expired state still fails closed.
- Live deployment of the hardened checkout found two further Linux-only
  boundaries. Wide-tree normalization now retains a bounded descriptor set
  instead of exhausting the process limit, and the non-root paper owner safely
  accepts systemd's intentional private-parent bind mount while keeping exact
  parent/leaf mount identities pinned. Demo, root, and default lease paths
  remain strict.

## Evidence boundary

The tracked hedge history is an immutable sizing-only model prior through
2026-07-09. It is not live-extended calibration or performance evidence. No
confirmatory research experiment is active, and no runtime status or paper
result authorizes research promotion or real-money deployment.
