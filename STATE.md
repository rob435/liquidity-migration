# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, generation-bound strategy receipts, and watchdog evidence
at 2026-07-17 22:55 UTC. These facts describe the deployed implementation
commit below; a later documentation-only commit may leave the branch ahead.

## Live authority and topology

- Installed and authorized implementation commit:
  `3e1860d9c6e637300e540ca99fad22bc9b98fe3f`, profile `operational`, receipt
  SHA-256 `37cc00133773843028f2cd3fc24fddccbbaaccc96f98c9d028e2e6be92f4d248`.
- The installed demo and paper operational-profile bytes are identical, with
  SHA-256 `cf68369c587c4eb736b5e63f9524a15eb125daa820f09c4167de49aac9fcac18`.
  The tracked editable source is `configs/operational.demo.json`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. The first 2x hedge run exited zero at 22:51 UTC. An on-demand
  watchdog run under the exact authority exited zero at 22:55 UTC and resolved
  all four strategy-liveness alerts from the quiesced deployment interval.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- Authenticated Bybit demo reads after activation showed zero non-flat
  positions and zero regular or conditional orders. The canonical journal
  verified and applied all 8,193 events at head
  `b42c1863716bb26cd696734f5a84f820b87204aaa02a792f899abb74a8c20000`,
  with zero nonzero positions, working orders, component targets, or aggregate
  targets. The inbox had zero unresolved requests.
- Demo and paper owners publish healthy current-generation state with zero
  restarts. The account-health reader now accepts harmless heartbeat
  replacement only when the replacement binds the same journal head; a changed
  head still fails closed.
- All four target producers have current-generation completed-cycle receipts
  bound to the operational-profile SHA. LONG demo/paper completed with 100
  active symbols, 2x entry leverage, and a 0.5 notional multiplier. CONTINUOUS
  demo/paper completed with 513 active symbols, 2x entry leverage, a 1.0
  notional multiplier, no entry candidates, and no publication error.
  Completion-age liveness was clean after publication. Paper is explicitly
  `integration_only_uncalibrated`; its cycles are routing/lifecycle evidence,
  not performance or fill-quality evidence.
- The account owner caps leverage at 2x, symbol notional at 5,000 USDT,
  component/account gross at 20,000 USDT, and initial margin at 10,000 USDT.
  Startup/authorization reject unknown profile fields, producer leverage above
  the owner cap, or registered exposure envelopes outside the same profile.
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

- No trade was entered from the observed ONDO signal under the old deployment.
  LONG requested 10x leverage while the account owner allowed at most 2x, so
  every proposed entry was rejected before a venue order existed. Authenticated
  venue and journal evidence confirmed no hidden order or fill.
- LONG, CONTINUOUS, and hedge leverage plus exposure/risk knobs now come from
  one strict operational profile. Independent systemd sizing variables were
  removed. The current ONDO signal remains unqueued because its exact earlier
  risk-rejected attempt is terminally suppressed; CONTINUOUS has zero current
  entry candidates. A new strategy-qualified attempt is eligible at 2x, subject
  to the still-active account and venue safety checks.
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
