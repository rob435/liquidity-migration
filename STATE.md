# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, and cgroup evidence at 2026-07-16 15:08 UTC. These facts
describe the deployed commit below; unrelated uncommitted workspace changes
are not covered.

## Live authority and topology

- Installed and authorized commit:
  `14d49d593ad7f1ad3e09162b48dc58221c52983d`, profile `operational`, receipt
  SHA-256 `dfa12a9a70eb5338a4e5964318ca97c634d9f1a4058d8a9d9b52b4ae709707bd`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. Early manual cold-start checks surfaced stale cycle/book evidence
  and one empty paper-follower cycle; the owner and producers recovered without
  restart. The final watchdog at 15:08 UTC reported zero active alerts across
  all ten monitored units.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- Authenticated Bybit demo reads before install and after activation showed
  zero non-flat positions and zero regular or conditional orders. Demo and
  paper journals hash-verified with zero fills, working orders, non-flat
  positions, or nonzero component targets.
- Demo and paper owners publish healthy generation-bound status. Demo
  reconciliation is healthy with zero mismatches; live-L2 and owner-health ages
  are measured in seconds. With no active work, each owner subscribes only to
  the idle BTC book.
- All four target producers have completed post-cutover cycles. The two latest
  observed paper CONTINUOUS cycles each carried 204,039 followed kline rows.
  Paper is explicitly
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

- The reported negative owner-health ages were not future venue data or clock
  drift. Strategy event time was reused after concurrent owner heartbeats.
  Operational freshness now samples adjacent wall time while strategy/PIT time
  remains unchanged.
- The stale L2/reconciliation alerts were genuine symptoms of the old owner's
  unbounded memory retention. Dynamic subscriptions and bounded state removed
  that cause.
- Activation-time paper/cycle alerts were cold-start and cross-owner-reader
  defects. Paper ownership is now verified against its explicit runtime UID,
  reset-boundary rows are not treated as strategy cycles, and liveness has one
  bounded cold-start window before returning to its three-minute cadence.
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
