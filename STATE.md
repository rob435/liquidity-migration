# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, generation-bound strategy receipts, and watchdog evidence
at 2026-07-18 21:22 UTC. These facts describe the deployed implementation
commit below; a later documentation-only commit may leave the branch ahead.

## Live authority and topology

- Installed and authorized implementation commit:
  `f1cdb918df697f379e5749e765419eb7258d6c9f`, profile `operational`, receipt
  SHA-256 `22560d358d7138ab0d75a1ad825dc2eabb5683de5a43489b676fed6bdd6d2679`.
- The installed demo and paper operational-profile bytes are identical, with
  SHA-256 `cf68369c587c4eb736b5e63f9524a15eb125daa820f09c4167de49aac9fcac18`.
  The tracked editable source is `configs/operational.demo.json`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. The first two post-activation hedge runs at 21:16 and 21:21 UTC
  exited zero and each queued the exact zero BTC and ETH targets. The timer
  remains active; neither run logged a warning or restart.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- Authenticated Bybit demo reconciliation at 21:21 UTC showed a flat venue and
  flat journal reconstruction, zero venue position/order rows, zero pending
  orders, and no mismatch. The canonical journal and owner-health receipt agree
  at sequence 12,242 and state hash
  `81a29cb14d8d37b5a943d933f246f5daf93b08524225c10f8e030dae343d6313`.
  This supersedes the 13:09 UTC ONDOUSDT-open snapshot: native protection
  triggered at 13:57:05 UTC, filled the full -1,097 exit at 0.3395, and recorded
  account-net P&L of -34.98418018 USDT.
- Demo and paper owners publish healthy current-generation state with zero
  restarts. Repeated post-activation samples showed both required live-L2 books
  healthy and the oldest demo and paper receive ages below one second; no
  owner warning was logged after final activation.
  Final demo health at 21:22 UTC was healthy and requested-symbol-ready at
  journal sequence 12,242; no warning-or-higher account, strategy, or hedge log
  was recorded after the 21:13 UTC activation.
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
  receipts after activation and matched their exact systemd invocation. At
  21:22 UTC their completion ages were 2.7--83.2 seconds. The bound
  operational profile retains 2x entry leverage, a 0.5 LONG notional
  multiplier, and a 1.0 CONTINUOUS multiplier. Paper is explicitly
  `integration_only_uncalibrated`; its cycles are routing/lifecycle evidence,
  not performance or fill-quality evidence.
- The current CONTINUOUS demo receipt is exactly bound to its private mode-0600,
  single-link notification projection. The rendered status is `BTC gate:
  BLOCKED · uptrend · 30d -0.44%`; aggregate component-opportunity counts are
  `D9 36 -> liquidity 0 -> event 0 -> age 0 -> capacity 0`; qualified-but-blocked
  symbols are `none`; and the first rejection is `liquidity`. The uptrend gate
  remains enabled and unchanged.
- The durable CONTINUOUS cycle row records 123 exact feature-state rows with
  state SHA-256
  `4fa60abea760563976be75e7d8de55ce74b0eb475e6dc8c8e1455792081041c6`,
  feature-contract SHA-256
  `7deeb923f3609de57b7c7379bd5590fb84866636a8249ea6b989234ac99b5f36`,
  stable RMOM-source SHA-256
  `6b112e7b69424748f2ad0d31f77325ca4d3b704de776434054e7b648d4c9dca2`,
  and a 495-row signal-day SHA-256
  `1816721819dc803ed68380f7bc62c539ed90aa72c49d0afa4e51fdf2090efe43`.
  Both observer and identity error fields are empty.
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
  fill attribution complete. Its native protection later triggered and closed
  it at 13:57:05 UTC as described above. The later fill does not retroactively
  change the earlier rejection.
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
- Hourly notifications now label the owner/journal line `Account execution
  health`, separately render the receipt-bound CONTINUOUS BTC gate, funnel,
  qualified-but-blocked symbols, and first rejection, and deduplicate the same
  reconciliation-stale cause even when its measured age changes. The growing
  Parquet cycle ledger is not scanned by the latency-sensitive account owner.
- The first rollout exposed a separate paper-owner startup race: a public
  WebSocket could close while a concurrent symbol refresh was sending, which
  killed the owner before readiness. Send failure now retires the unusable
  socket, retains the complete desired-symbol set, and restores subscriptions
  on reconnect. Final paper readiness passed with zero restarts.
- The 12:36 UTC `account execution live L2 is 5.3 min stale` alert was a real
  per-symbol market-subscription freshness gap, not an owner, accounting, or
  protection failure. The owner heartbeat and reconciliation remained healthy,
  while the aggregate readiness sidecar continued to be republished and its
  oldest required receive age grew. The exact required set was BTCUSDT plus the
  held ONDOUSDT, proving that one subscription was silent while the other was
  active. The overwritten aggregate did not retain enough evidence to identify
  the silent symbol conclusively. It recovered without an owner restart by
  12:39 UTC. The raw public stream now tracks accepted
  orderbook frames independently for every desired symbol. A symbol silent for
  120 seconds causes the socket to close and rebuild the full desired set, with
  a ten-second watchdog interval and a separate grace window for newly added
  subscriptions. This leaves bounded recovery margin before the three-minute
  external alert while preserving that alert for failed reconnects. Threaded
  regression coverage proves close, reconnect, and full resubscription; the
  full local gate passed 1,984 tests with one skipped.
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

Research-only addendum, 2026-07-18: Strategy Overhaul V2 closed with no
qualifying thesis and did not touch its reserved holdout. Its full diagnostic
portfolios are model-based and negative after costs/funding; production account
verification is a bounded 100-key sample per sleeve, not exhaustive parity.
The later comparator/accounting repair also closed invalid: current RMOM could
not reconcile to the legacy input vintage, and a prospective 200-key account
benchmark exceeded its frozen numeric tolerance on two economically tiny LONG
P&L rows. No exact comparator or full-account retry ran. The optional repository
historical state-copy optimization is default-off and is not a deployed runtime
change.
This changes no live/demo/paper authority or topology above. See
`docs/strategy_overhaul_v2_completion_receipt_2026-07-18.md` and
`docs/strategy_overhaul_v2_comparator_accounting_repair_receipt_2026-07-18.md`.
