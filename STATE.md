# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, account-journal protection/P&L archaeology, and the full
2026-07-18/19 Telegram alert history at 2026-07-19 16:40 UTC. These facts
describe the deployed implementation commit below; a later documentation-only
commit may leave the branch ahead.

## Live authority and topology

- Installed and authorized implementation commit:
  `d520792f3b4a92b2cf0acce5f4fb6818d25bdeb9`, deployed from canonical `main`,
  profile `operational`, receipt artifact SHA-256
  `d689e3e60a26dfbd9ad7d20a03e8653b71bbd02fe3aafc453f45c26e61edcaa0`.
  Over `386120b` it deploys the 2026-07-19 load-bearing audit outcome
  (`docs/audit/2026-07-19-load-bearing-audit.md`): ten verified runtime
  defect fixes — most significantly the owner-process kill via an unguarded
  gapped-book protection read, committed-batch replay over supersession,
  strict REST pagination (no silent settlement truncation), exact Decimal
  order chunking, and terminal-partial component-protection eligibility —
  plus two O(history) scaling removals, each pinned by regression tests
  (gate: 2,088 passed / 3 skipped). No strategy decision path, sizing
  input, or registered estimator changed. This is a recorded mid-epoch
  runtime change point; the registered prospective clock and frozen
  comparator identity are unchanged.
- The prior change point remains on record: `386120b` (receipt
  `5e203d84...`) replaced the asdict-based event serializer.
- The prior mid-epoch change point remains on record: `a1ff6fe` (receipt
  `6b7f2f01...`) deployed four goal-directed runtime fixes over `9a2f20d`
  (funding-freshness measurement, lost-subscribe first-frame watchdog, hedge
  oneshot exit semantics, blocked-request traceback dedupe), and `main` was
  fast-forwarded over the full `codex/operational-profile-guards` and
  `codex/reconcile-prospective-epoch-20260719` lineages.
- The installed demo and paper operational-profile bytes are identical, with
  SHA-256 `cf68369c587c4eb736b5e63f9524a15eb125daa820f09c4167de49aac9fcac18`.
  The tracked editable source is `configs/operational.demo.json`.
- Active with zero restarts: demo and isolated-paper account owners plus demo
  and paper LONG and CONTINUOUS target producers. The fleet was deliberately
  quiescent about 16:10--16:25 UTC for the `a1ff6fe` staged install and again
  about 19:12--19:36 UTC for the `386120b` install; both stops are part of
  their recorded change points. The demo book was flat and healthy at both
  stop boundaries, and the first post-activation liveness run after `386120b`
  (19:37 UTC) reported zero active alerts across ten monitored units.
- Active timers: continuous hedge, residual-momentum refresh, and demo-paper
  liveness. Activation verified the immutable sizing-only model prior, demo
  order permissions, and a post-activation residual-momentum rewrite with
  7,691 rows and 567 stable symbols. The first hedge runs under the new
  checkout queued their target batches (`target_queued`), and the first
  post-activation liveness run at 16:36 UTC reported zero active alerts
  across ten monitored units.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.
- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs.
- Mainnet, `REAL_MONEY`, and real-money credentials remain unauthorized.

## Verified health and resource state

- The `TLMUSDT=-21,954` short that the 10:32 UTC reconciliation verified was
  later closed by its native protection at 11:17:07 UTC; the demo book has been
  flat since the final BUSDT stop trigger at 11:36:08 UTC. The pre-deploy owner
  health projection at ~16:10 UTC was `healthy`, zero positions, equity
  9,979.14 USDT. Eight native-stop closes on 2026-07-19 (BUSDT 03:50, 06:10,
  11:07, 11:36; TLMUSDT 07:09, 07:10, 09:16, 11:17) recorded a combined
  account-net P&L of about -9.49 USDT including funding. The earlier ONDOUSDT
  position had already closed via native protection at 13:57:05 UTC on
  2026-07-18: the full -1,097 exit filled at 0.3395 and recorded account-net
  P&L of -34.98418018 USDT.
- Demo and paper owners publish healthy current-generation state with zero
  restarts. The create-only forward-start collector fully verified the demo
  and paper journals at 13:09 UTC with 15,524 and 90 canonical events,
  respectively; each fresh owner-health projection matched its exact journal
  head, current systemd invocation, and required-book readiness.
  Excluding the deliberate stopped-deployment gap, a pre-deploy sample of 18
  persisted reconciliation-checkpoint intervals were 31.4--33.4 seconds with a
  32.0-second median.
- On a read-only copy of the pre-deploy 9,874-event journal, the optimized
  component-anchor projection was exactly equal to the prior projection and
  reduced local runtime from 1.416 seconds to 0.0116 seconds. Warm snapshot
  access fell from 23.8 ms to 0.258 ms and one journal append from 25.0 ms to
  1.62 ms. These are implementation benchmarks, not trading-performance
  evidence.
- All four target producers published current-generation completed-cycle
  receipts after final activation and matched their exact systemd invocation
  by 13:08 UTC. Both paper producers completed only after successfully writing
  the shared paper target-capture tape through the corrected strict systemd
  sandbox. At 13:16 UTC that private tape was still advancing, all six
  persistent services retained zero restarts, and neither paper producer had a
  warning-or-higher log record since activation. The bound
  operational profile retains 2x entry leverage, a 0.5 LONG notional
  multiplier, and a 1.0 CONTINUOUS multiplier. Paper is explicitly
  `integration_only_uncalibrated`; its cycles are routing/lifecycle evidence,
  not performance or fill-quality evidence.
- The current CONTINUOUS demo receipt is exactly bound to its private mode-0600,
  single-link notification projection. The rendered demo status is `BTC gate:
  OPEN · uptrend · 30d +3.37%`; aggregate component-opportunity counts are
  `D9 36 -> liquidity 15 -> event 3 -> age 3 -> capacity 3`; qualified-but-
  blocked symbols are `none`. The independently bound paper projection reports
  the same funnel through age, capacity zero, and names `BUSDT` as qualified but
  blocked by an already-reserved target. The uptrend gate remains enabled and
  unchanged.
- The 2026-07-18 21:22 durable CONTINUOUS cycle row recorded 123 exact
  feature-state rows with state SHA-256
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
  At the 10:31 UTC post-deploy sample the bounded demo owner used about 108 MiB
  and the isolated paper owner about 9.6 MiB, both with zero restarts.
- Live cgroup evidence found the first LONG demo soft threshold below its
  working set. Soft thresholds were retuned from measured footprints while all
  hard maxima remained unchanged. At the 10:32 UTC post-deploy sample the 4 GiB
  host had about 1.8 GiB available; full memory-pressure averages were 0.35%
  over 60 seconds and 0.42% over 300 seconds, with no managed-unit restart.
  This clean start is not a longer-horizon capacity result.
- A pre-evidence BTC-risk state file was rejected rather than migrated. With
  zero authoritative CONTINUOUS trade rows and zero pending requests, it was
  archived at
  `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
  with SHA-256
  `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`.

## Prospective execution epoch

- The create-only start receipt was collected at 13:09:37 UTC and published at
  `reports/prospective-runtime-parity-execution-epoch-2026-07-18/forward/start/receipt.json`.
  Its file SHA-256 is
  `db508862314972da310404814519bd701ffc18d2be51a3d39debddee1ef79376`
  and its self-hashed artifact identity is
  `25441106b82adf95364d4e602d4b5912ecc0d2871b18778d8fe47684e8ddafbf`.
- The registered clock starts 2026-07-19 14:00 UTC. Calibration is
  `[2026-07-19 14:00, 2026-09-02 14:00)` and validation is
  `[2026-09-02 14:00, 2026-10-17 14:00)`. No pre-boundary row is eligible.
- The receipt binds the exact final comparator at implementation commit
  `9a2f20d`: receipt SHA-256
  `f9ad5a6bfcc8948f742ae9bd877b8dda0e3f79d3908d96f274967445d6431e77`
  and independent Linux verification SHA-256
  `bb6a8e755c2f07c7361dcb483fb46348b5806931a2027024c64659805dbb5a22`.
  That verification covered 87 files, 175,721,151 bytes, under logical
  SHA-256 `6babc66a5445d43f2559e2d6fc6838cceaf848c37cdd256398591928ed499699`.
- At collection, all six persistent services had zero restarts; demo and paper
  queues each had zero pending, processing, and failed requests; and the valid
  shared target tapes contained 123 demo and six paper pre-boundary rows. Those
  rows were inventoried but are not forward observations.
- Two earlier start calls failed before publication: the root collector first
  used a current-user route-owner check against the correctly paper-owned
  manifest, then current-generation paper health exposed that both paper
  producer sandboxes lacked the already-authorized shared capture root. Commits
  `ba51bd6` and `9a2f20d` repaired those boundaries without changing the
  registered estimator or reading an affected outcome. No failed call created
  a start receipt.

## Incident interpretation

- The 2026-07-19 07:08 and 09:11 UTC `TLMUSDT unowned_venue_order` CRITICAL
  bursts were native-stop replacement identity gaps under the then-deployed
  `f1cdb91`, not foreign orders. Continued entry fills moved each position's
  fill-anchored stop, the owner replaced the Full-position stop, and Bybit
  issued a new conditional orderId that the old verifier could never re-own;
  each burst blocked intents from the replacement until the position went flat
  (07:07:55 to 07:09:41 and 09:10:07 to 09:16:51). The trigger-price/lineage
  verifier deployed at 10:25 UTC in `296cdf8` resolved this: the 10:33 and
  11:13 UTC BUSDT stop replacements verified without a single unowned report.
- The 10:35--11:35 UTC `account funding reconciliation is stale:
  age_ns~4.1-4.8e9` errors were false staleness, not accounting gaps. The
  funding-recovery report timestamped itself before its paginated REST queries
  and was then held to the shared 4-second position bound while the slower
  position pass ran after it. `a1ff6fe` measures pass completion and applies a
  documented 30-second funding floor to the chain bound; a wedged recovery
  loop still fails closed inside one liveness cycle. Position, order, and
  protection truth keep the tight bound.
- The recurring ~3-minute `waiting for queue-head market data: X:stale_book`
  CRITICALs around new-symbol entries (ETHUSDT 2026-07-18 12:51 and 22:02,
  BUSDT 2026-07-19 06:08) were lost/rejected orderbook subscribes that only
  the 120-second silent-stream watchdog could catch. Bybit answers a
  successful subscribe with an immediate snapshot, so `a1ff6fe` rebuilds the
  socket after 30 frameless seconds for a new subscription while
  quiet-but-live books keep the full silent window.
- The 07:09 and 09:12 UTC `continuous-hedge.service FAILED` pages duplicated
  the owner-health root cause and arrived after it had resolved. An armed
  hedge run blocked by unhealthy owner health now exits 0 with the blocked
  receipt printed; equity/price/book failures and publish errors still fail
  the oneshot. The 2026-07-18 21:00 and 2026-07-19 09:58 hedge TERM kills were
  deliberate full-fleet deploy stops, not runtime failures.
- A persistently blocked account request logged an identical full traceback
  every ~4.4 seconds (about 900 traceback lines during the 09:10--09:16
  burst). The owner now logs one traceback per distinct cause and one line per
  repeated blocked pass, and clears the signature on the next accepted batch.
- The paper target producers were effectively down 10:26--13:03 UTC (liveness
  paged DAEMON DOWN/HUNG 11:19, resolved 13:15): their strict sandbox denied
  the shared paper capture lock path, which `9a2f20d` fixed. No recurrence
  since the 13:03 activation.
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
  disappearance: the 2026-07-18 validation cycles recorded 20 temporarily
  ineligible symbols with exact reasons and continued. Missing ticker/instrument
  rows, structural contract changes, malformed inputs, retirement evidence
  changes, or retained exposure still fail closed.
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
- Follow-on Telegram evidence exposed several independent defects now deployed
  in `296cdf84576d8e5ad434289f4b74458ad261c1c0`: Bybit Full stop replacements
  retain native order-ID lineage; owner health republishes against each new
  immutable journal head without replaying the full payload history; typed
  queue-head warm-up is suppressed only while fresh, while stale and timed-out
  states still page; dependent account alerts coalesce behind their root cause;
  and CONTINUOUS reports same-signal qualified-but-blocked symbols by name. The
  exact full local/pre-push gate passed Ruff, mypy, 2,001 tests, and one skip.
- Live deployment of the hardened checkout found two further Linux-only
  boundaries. Wide-tree normalization now retains a bounded descriptor set
  instead of exhausting the process limit, and the non-root paper owner safely
  accepts systemd's intentional private-parent bind mount while keeping exact
  parent/leaf mount identities pinned. Demo, root, and default lease paths
  remain strict.

## Evidence boundary

The tracked hedge history is an immutable sizing-only model prior through
2026-07-09. It is not live-extended calibration or performance evidence. The
prospective execution epoch is registered but has not inspected a forward
outcome at this snapshot. It is not a strategy-alpha experiment, and no runtime
status or paper result authorizes research promotion or real-money deployment.

Research-only addendum, 2026-07-18: Strategy Overhaul V2 closed with no
qualifying thesis and did not touch its reserved holdout. Its full diagnostic
portfolios are model-based and negative after costs/funding; production account
verification is a bounded 100-key sample per sleeve, not exhaustive parity.
The later comparator/accounting repair also closed invalid: current RMOM could
not reconcile to the legacy input vintage, and a prospective 200-key account
benchmark exceeded its frozen numeric tolerance on two economically tiny LONG
P&L rows. No exact comparator or full-account retry ran within that closed V2
repair. The optional repository
historical state-copy optimization is default-off and is not a deployed runtime
change.
This changes no live/demo/paper authority or topology above. See
`docs/strategy_overhaul_v2_completion_receipt_2026-07-18.md` and
`docs/strategy_overhaul_v2_comparator_accounting_repair_receipt_2026-07-18.md`.
