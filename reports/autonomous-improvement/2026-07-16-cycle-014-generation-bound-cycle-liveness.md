# Autonomous improvement cycle 014: generation-bound cycle liveness

## Finding

- Incident window: `2026-07-16T15:00Z` through `2026-07-16T15:05Z`;
  implementation and live cutover: `2026-07-16T15:17Z` through
  `2026-07-16T16:44Z`.
- The watchdog treated a strategy cycle's causal start timestamp as proof of
  process completion. A legitimate long-running or in-flight cycle could
  therefore page as `DAEMON DOWN/HUNG` even though its producer was active and
  finishing work. A pre-restart cycle could also satisfy freshness for a newly
  started service because the evidence was not bound to systemd's current
  invocation.
- Cold activation exposed the same category error in two other checks. A
  queue-head `stale_book` while the current owner built its first requested
  books paged immediately, and CONTINUOUS paper's cycle-start coverage counter
  was interpreted as the follower's current store size. That produced an
  `EMPTY (kline_store_rows=0)` warning despite a populated follower store.
- The observed sequence included CONTINUOUS demo/paper ages of 63.7/63.8
  minutes at 15:00 UTC, a demo-owner queue-head `ETHUSDT:stale_book` alert and
  paper empty-store warning at 15:02, and a paper false-hung age of 10.9 minutes
  at 15:05.

This was operational reliability and observability work. No signal, strategy
decision, target, universe, leverage, ledger key, execution route, or research
conclusion changed. No strategy research or backtest was run.

## Implementation

- Added a strict, private `strategy_cycle_health.json` projection. It records
  sleeve, execution environment, durable cycle identity, causal cycle time,
  non-causal completion time, systemd `INVOCATION_ID`, and the manager's actual
  current kline-store row count.
- Publication occurs only after the cycle output, target capture, and decision
  outcome are durable. Publication failure is counted and logged separately;
  it cannot rewrite or invalidate already-durable strategy evidence.
- The artifact is canonical JSON, capped at 8 KiB, written by fsync plus atomic
  replacement at mode `0600`, and read through the repository's stable
  descriptor-bound single-link reader.
- Liveness now requires the health artifact to match both the current systemd
  invocation and the exact latest durable cycle identity and causal timestamp.
  A prior service generation, a future timestamp, or unrelated output cannot
  mask a hung current producer. Age is measured from completed work.
- A current service generation receives at most the existing ten-minute cycle
  SLA as startup grace when it has no completed receipt. Only the exact
  queue-head warm-up condition is suppressed inside that same bound. Every
  other owner failure still pages, and missing completion after the bound fails
  closed.
- CONTINUOUS empty-store detection consumes the producer's current manager row
  count. A custom `--unit` selection cannot disable producer-generation
  binding.

## Regression and measurement evidence

- New tests cover strict serialization and file safety, evidence ordering,
  publication failure isolation, current/prior invocation binding, in-flight
  next cycles, stale completion, future evidence, cold-start expiry, the
  narrowly scoped queue-head exception, custom unit filters, and actual paper
  follower rows.
- Final locked local gate after rebasing the concurrent data-preservation
  change: 1,888 passed, 1 skipped. Repository Ruff passed; package mypy passed
  for 93 source files; direct mypy for `scripts/check_demo_liveness.py` passed.
- A local warm-cache microbenchmark measured 200 atomic health writes at
  0.1770 ms mean and 2,000 stable reads at 0.0304 ms mean. This is descriptive
  local overhead, not an end-to-end VPS latency claim.
- Exact implementation commit
  `bd5e97343d89e823b978080aff00a7551a89a63e` passed GitHub Actions run
  [29514810674](https://github.com/rob435/liquidity-migration/actions/runs/29514810674):
  locked install, Ruff, mypy, and all tests. The conditional VPS job was
  skipped as designed.

## Publication and live deployment

- The implementation was pushed to
  [PR #1](https://github.com/rob435/liquidity-migration/pull/1), which was open,
  ready, and mergeable at exact implementation head when checked.
- The fleet was stopped before install. An authenticated read captured zero
  demo positions and zero regular or conditional orders. The stopped install
  completed at the exact commit with no managed unit running.
- Operational authority was issued only for that exact commit and the
  demo-plus-isolated-paper profile under reference
  `codex-goal-019f685e-alert-remediation-20260716`; receipt SHA-256
  `5d3159aeae55623ffff4491819126f9c9eae541d6f29ec99c9d8024324911120`.
- Exact post-activation status returned `verify-ok`, demo order permissions
  passed, and the immutable sizing-only model prior remained valid. Both
  owners, all four producers, and all three timers were active with zero
  service restarts.
- A stdout-only watchdog run during current-generation cold start reported
  zero active alerts across ten monitored units. It did not use Telegram or an
  external heartbeat.
- All four producers then published completed-cycle receipts matching their
  current systemd invocation. Current row counts were 231,787 for LONG demo
  and paper and 371,056 for CONTINUOUS demo and paper. The CONTINUOUS
  completion intervals were about 30.4 seconds for demo and 33.2 seconds for
  paper; both were correctly fresh after completion.
- A second stdout-only watchdog run after those receipts again reported zero
  active alerts across ten monitored units.
- The first scheduled systemd watchdog run under the new exact authorization
  completed at `2026-07-16T16:44:28Z` with exit status zero and reported zero
  active alerts across all ten monitored units. Its journal contained the
  authorized implementation commit above.
- Current-generation demo and paper owner-health records were healthy and
  journal-bound. The demo owner's fresh authenticated REST reconciliation
  observed zero venue positions, zero all-kind or conditional orders, and zero
  mismatches; local state had zero non-flat positions, working orders, and
  nonzero aggregate targets. Paper local state was likewise flat and idle.

## Evidence boundary and next candidates

- The account remains too quiet for a venue-accounting sufficiency claim. The
  registered evidence window has zero canonical fills, TRADE rows, closed-PnL
  rows, and settlement/funding rows, so its sample floors correctly fail even
  though the separate final-flatness gate passes.
- Startup grace is deliberately bounded and generation-specific. It is not a
  general suppression mechanism, and it does not weaken owner, strategy-input,
  or post-grace failure checks.
- Add a privileged Linux/systemd integration test for invocation turnover and
  `CLOCK_BOOTTIME`; the unit and parser logic are covered locally, but the live
  cutover remains the only full namespace/systemd exercise.
- Define follower freshness beyond row count, such as a generation-bound
  applied-snapshot or maximum-market-timestamp receipt. Current row count fixes
  the false empty claim but is not proof that every expected latest bar was
  applied.
- Make stopped install normalization incremental only after proving exact
  mount, ownership, link, mutation-race, and final-tree equivalence.
- Update official GitHub action majors after validating the Node.js 24
  migration; the current run passed but emitted the Node.js 20 deprecation
  notice.
