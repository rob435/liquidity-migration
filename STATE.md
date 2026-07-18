# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, generation-bound strategy receipts, and watchdog evidence
at 2026-07-18 09:59 UTC. These facts describe the deployed implementation
commit below; a later documentation-only commit may leave the branch ahead.

## Live authority and topology

- Installed and authorized implementation commit:
  `61b40ef2c39ba824252d15a234ab351d0d21a4bf`, profile `operational`, receipt
  SHA-256 `075ffb62300f174af7d55dcfd95434432e8952cc0c86c1f41e02b9749e33b057`.
- The installed demo and paper operational-profile bytes are identical, with
  SHA-256 `cf68369c587c4eb736b5e63f9524a15eb125daa820f09c4167de49aac9fcac18`.
  The tracked editable source is `configs/operational.demo.json`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. Three consecutive scheduled hedge runs at 09:44, 09:49, and 09:54
  UTC exited zero after the final activation. The scheduled 09:54 watchdog run
  also exited zero and emitted explicit resolutions for the demo account-owner
  health and continuous-hedge unit alerts.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- Authenticated Bybit demo reads at 09:56 UTC showed one non-flat position:
  ONDOUSDT long 1,097 at average entry 0.371. The sole open venue order was its
  untriggered reduce-only, close-on-trigger sell stop for 1,097 at 0.3397. The
  canonical journal verified and applied all 9,959 events at head
  `84893236def807218397c3f26c1b8a42ddf09d7948f6efc38df38d7f85cdb502`;
  reconstructed position, aggregate target, and latest authenticated venue
  snapshot all agreed at ONDOUSDT +1,097 with no mismatches. There were zero
  working orders, zero pending or processing requests, and zero failed requests.
- Demo and paper owners publish healthy current-generation state with zero
  restarts. Final demo health was 1.4 seconds old at journal sequence 9,959;
  no `account reconciliation is stale` error was logged after final activation.
  Excluding the deliberate stopped-deployment gap, the latest 18 persisted
  reconciliation-checkpoint intervals were 31.4--33.4 seconds with a 32.0-second
  median.
- On a read-only copy of the pre-deploy 9,874-event journal, the optimized
  component-anchor projection was exactly equal to the prior projection and
  reduced local runtime from 1.416 seconds to 0.0116 seconds. Warm snapshot
  access fell from 23.8 ms to 0.258 ms and one journal append from 25.0 ms to
  1.62 ms. These are implementation benchmarks, not trading-performance
  evidence.
- All four target producers published current-generation completed-cycle
  receipts after activation; demo and paper LONG plus CONTINUOUS had all
  advanced again by 09:58 UTC. Completion-age liveness was clean. The bound
  operational profile retains 2x entry leverage, a 0.5 LONG notional
  multiplier, and a 1.0 CONTINUOUS multiplier. Paper is explicitly
  `integration_only_uncalibrated`; its cycles are routing/lifecycle evidence,
  not performance or fill-quality evidence.
- The account owner caps leverage at 2x, symbol notional at 5,000 USDT,
  component/account gross at 20,000 USDT, and initial margin at 10,000 USDT.
  Startup/authorization reject unknown profile fields, producer leverage above
  the owner cap, or registered exposure envelopes outside the same profile.
- The prior demo owner retained raw depth for the full universe, reached about
  2.75 GiB resident memory, and stalled reconciliation through swap pressure.
  At the final sample the bounded demo owner used about 129 MiB and the isolated
  paper owner about 70 MiB, both with zero restarts.
- Live cgroup evidence found the first LONG demo soft threshold below its
  working set. Soft thresholds were retuned from measured footprints while all
  hard maxima remained unchanged. At that cutover sample the 4 GiB host had about
  2.3 GiB available; full memory-pressure averages were 0.22% over 60 seconds
  and 0.49% over 300 seconds, with no managed-unit restart.
- A pre-evidence BTC-risk state file was rejected rather than migrated. With
  zero authoritative CONTINUOUS trade rows and zero pending requests, it was
  archived at
  `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
  with SHA-256
  `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`.

## Incident interpretation

- No trade was entered from the earlier observed ONDO signal under the old
  deployment. LONG requested 10x leverage while the account owner allowed at
  most 2x, so that exact attempt was rejected before a venue order existed.
  Authenticated venue and journal evidence confirmed no hidden order or fill
  for that rejected attempt.
- A distinct later strategy-qualified ONDO attempt was accepted at 2x and
  filled at 06:03:10 UTC on 2026-07-18: long 1,097 at 0.371, with component-level
  fill attribution complete. It remains open under the native 0.3397 stop. The
  later fill does not retroactively change the earlier rejection.
- LONG, CONTINUOUS, and hedge leverage plus exposure/risk knobs now come from
  one strict operational profile. Independent systemd sizing variables were
  removed. New strategy-qualified attempts are eligible at 2x, subject to the
  still-active account and venue safety checks.
- The 2026-07-18 Telegram `account_health_stale` and continuous-hedge failures
  were genuine freshness failures but not position contradictions. Venue and
  journal quantities agreed throughout. The owner repeatedly replayed or deep-
  copied the growing immutable journal, and component-anchor projection
  rescanned roughly 9,800 events for each historical batch. That scaling aged
  otherwise-correct reconciliation reports beyond the four-second execution
  bound. Owner-internal coherent snapshots, indexed fill projection, bounded
  transaction copies, and cached journal reads removed those hot-path costs.
- Position-truth reporting now distinguishes `mismatch`, `stale`, and
  `unavailable`. A stale matching venue/local snapshot therefore reports stale
  evidence instead of falsely claiming that ONDO quantities disagree.
- The first rollout exposed a separate paper-owner startup race: a public
  WebSocket could close while a concurrent symbol refresh was sending, which
  killed the owner before readiness. Send failure now retires the unusable
  socket, retains the complete desired-symbol set, and restores subscriptions
  on reconnect. Final paper readiness passed with zero restarts.
- Three CONTINUOUS candidates (`HIGHUSDT`, `PUMPBTCUSDT`, and
  `WHITEWHALEUSDT`) have venue `deliveryTime=1784538000000`. They are recorded
  prospectively in private mode-0600 retirement registries and may retire only
  while account positions, targets, orders, and inbox exposure are all flat.
- Normal live LONG turnover/rank movement is no longer mistaken for a
  disappearance: the latest cycles recorded 20 temporarily ineligible symbols
  with exact reasons and continued. Missing ticker/instrument rows, structural
  contract changes, malformed inputs, retirement evidence changes, or retained
  exposure still fail closed.
- Unresolved Telegram risk blocks now carry signal expiry and are removed after
  the immutable signal window ends. Timer-driven oneshot failures no longer say
  that systemd has permanently stopped retrying; the alert identifies the
  failed unit and tells the operator to inspect its journal.

- The 17:20 `latest cycle is 0.1 min future-dated` page was another local
  watchdog read race, not future scheduler data or a stopped producer. The
  watchdog sampled one run-wide wall time before other health gathers, while a
  concurrent cycle could publish its completion receipt before the later
  strategy read. It also read the cycle dataset before the receipt, allowing a
  concurrent publication to form an impossible cross-snapshot pair. Strategy
  liveness now observes the receipt before its already-durable causal dataset
  and samples wall time after that observation. True future timestamps remain
  critical; deterministic tests reproduce both former interleavings.
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
- Earlier pre-cutover stale L2/reconciliation alerts were genuine symptoms of
  unbounded market-depth retention. The 2026-07-18 reconciliation incident was
  a different journal-history CPU-scaling failure; dynamic subscriptions alone
  did not fix it.
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
