# Operational State

Updated from authenticated venue reads, exact-receipt verification, systemd,
owner/journal checks, account-journal protection/P&L archaeology, the full
2026-07-18/19 Telegram alert history, the 2026-07-20 rollouts, and the
2026-07-21 account-kernel incident audit. Point-in-time deployment facts and the
current remediation are separated explicitly below; exact live truth comes
from the authenticated deployment receipt and read-only status command.

## 2026-07-22 demo journal-publication stall and deployed remediation

- The supplied 12:11 UTC CRITICAL page was a real freshness failure in the
  canonical demo journal, not expected-commit drift and not a position
  mismatch. DEXEUSDT opened short `7.5 @ 4.423`; its already-installed Bybit
  Full stop triggered about three seconds later and closed `+7.5 @ 4.442`.
  The venue/local reconciliation remained healthy before and after the gap,
  and the watchdog emitted its resolution at 12:14 UTC.
- The stop execution reached the VPS at `12:09:02.146699`, but the atomic
  external-protection transaction did not commit until `12:11:08.025833`.
  The prior and next venue checkpoints were `12:08:26.768780` and
  `12:11:09.987654`, an exact `163.219s` gap. The delayed local entry-fill
  handling also tried to refresh protection after the venue was already flat;
  Bybit correctly rejected that stale request with error `10001` (zero
  position). The later authenticated snapshot proved DEXE flat and retained
  the unrelated MIRAUSDT position in exact venue/local agreement.
- The root cause was an in-process journal publication race. An atomic segment
  can become visible just before the writer publishes the matching cache. A hot
  reader treated that local segment as an external change and replayed the
  complete journal while holding the cache lock; the writer then waited behind
  the replay while still holding the cross-process journal lock, cascading into
  delayed fills, protection adoption, owner health, and reconciliation. An
  isolated copy of the 32,681-event journal reproduced the exact DEXE adoption
  in `9ms` with a coherent projection, excluding the business transaction as
  the slow path.
- The remediation keeps readers on the prior coherent cache during only
  the local atomic-replace/cache-publish window, publishes every cache field
  together after durability, and clears the guard on failure so a durable but
  unpublished segment is reconstructed normally. A deterministic concurrency
  regression enters the exact post-segment/pre-cache window and forbids any
  history replay. The full incident evidence and safety boundary are in
  `docs/audit/2026-07-22-demo-journal-publication-race.md`.
- Before deployment, MIRAUSDT remained `-2079.5` in exact venue/local agreement
  with an active exchange-native MarkPrice stop and no regular working orders.
  Producers were stopped before owners; the final authenticated checkpoint was
  flat with no venue position, reconstructed position, conditional order, or
  mismatch. No position was forcibly flattened.
- Commit `6dad49ca4ab099c83cb5e954533f71d9cee6929a` passed GitHub CI, was installed
  while the complete fleet was quiescent, and invalidated the prior receipt. A
  new create-only `operational` receipt (artifact SHA-256
  `fdc5b4cb2b84e710cbc81d9efe7086c3533181319f86d86ab6c7bf677822754e`)
  binds that exact commit and the demo/paper-only environment; activation and
  the independent read-only status check both returned `verify-ok`.
- Post-activation, both owners and all four producers were active with zero
  restarts, no project unit was failed, and the owner-health artifacts were
  fresh and healthy. Eight authenticated venue checkpoints remained flat and
  mismatch-free; their maximum interval was `32.889s` and immutable-transaction
  publication delay was `0.370--0.432s`. A real watchdog run at 16:03 UTC
  reported zero active alerts across all ten monitored units. These early
  observations verify rollout health, not proof that a rare race can never
  recur.
- Local validation is green: the complete account-kernel file and 255 focused
  account/reconciliation/protection/liveness tests passed; repository doctor,
  Ruff, mypy, and `git diff --check` passed; the full gate completed with 2,225
  passed / 1 skipped.

## 2026-07-22 deployment workflow and runtime follow-up

- The stopped install's dominant avoidable cost was confirmed in
  `normalize-paper`: it rewrote and synced every already-correct object in the
  roughly 123,000-entry paper tree. A controlled 2,202-entry Linux benchmark
  fell from `5.35s` to `0.79s` after selecting only metadata that actually
  differs. The independent complete descriptor/inode/mount/permission rescan
  remains mandatory and adversarial late-entry tests pass.
- A guarded `deploy --execute rollout` candidate now performs target prefetch,
  current-topology verification, repeated canonical plus directly authenticated
  flat-account proofs, producer-before-owner shutdown, stopped install,
  create-only authority issuance, owner-first activation, and final verification
  under one host maintenance lock. It never flattens/cancels or infers the
  profile/owner acknowledgement. Pre-install failure restores the prior
  topology; post-install failure leaves the managed fleet stopped. Material
  phases are timed and RMOM bootstrap defaults to a bounded 300 seconds rather
  than 1,800 seconds.
- The follow-up live sample on deployed commit `6dad49c` found zero service
  restarts, tracebacks, critical/reconciliation alerts, or unhealthy venue
  snapshots. Across 319 checkpoints after 16:00 UTC, the maximum interval was
  `42.842s` and none exceeded 60 seconds. Eleven public WebSocket ping/pong
  timeouts recovered in-process and were followed by completed cycles.
- One LONG cycle correctly blocked risk increase when healthy owner evidence
  briefly lagged the journal (`33272` vs `33273`), but discarding the whole
  hourly opportunity was unnecessary. The local candidate types only this
  strictly-behind race as retryable and allows at most three one-second waits;
  blocked, stale, future, health-ahead, wrong-account, and hash-conflicting
  evidence still fails immediately.
- At the final authenticated sample, MIRAUSDT was short `1896.2` in exact
  venue/local agreement with one verified reduce-only close-on-trigger native
  MarkPrice stop. The account was not flattened or stopped; the new rollout
  gate would refuse this non-flat state before service mutation.
- The first authorized release attempt later exposed an independent
  maintenance deadlock before any unit changed: the byte-bound 516-symbol demo
  rule receipt had crossed its registered 168-hour limit. The VPS clock was
  coherent and all six persistent services remained active with zero restarts;
  MIRAUSDT was still `-1896.2` locally and at Bybit with the same protected
  stop. A follow-up keeps activation strict but permits genuinely expired (not
  future-dated) bound rules only for old-topology shutdown verification. Once
  stopped and flat, rollout automatically re-probes stale rules. The prior
  receipt's adjacent quantity boundary is used only as a search hint and each
  result is freshly revalidated; changed boundaries fall back to full search.
  The old receipt contained 7,383 attempts (median 14 per symbol), versus at
  most two boundary attempts per unchanged symbol before terminal-evidence
  overhead. A post-probe direct local/venue flat proof remains mandatory.
- The next rollout attempt again changed no service: its old-topology check
  found the hedge one-shot failed. Journald proved hedge and liveness timers
  were exiting status 2 in strict authorization before workload execution,
  exactly because of the expired demo rules. Recovery verification now accepts
  only that failed-unit shape under the already-proven expiry condition. It
  also declares rollback unavailable before stopping: an expired receipt
  cannot truthfully restart the old topology, so any subsequent failure leaves
  the managed fleet stopped. Fresh-rule activation still rejects all failed
  units.
- Full local validation is green at `2249 passed / 1 skipped`, plus repository
  doctor, Ruff, mypy, shell parsing, and `git diff --check`. The detailed
  evidence is in
  `docs/audit/2026-07-22-deploy-workflow-and-runtime-followup.md`. Deployment
  status is authoritative only when tied to an exact pushed commit and a fresh
  authenticated rollout receipt; this implementation record does not imply
  activation by itself.

## 2026-07-22 paper reduction-convergence incident and local remediation

- The supplied 08:15 UTC CRITICAL page was generated by the paper account, not
  Bybit demo. TREEUSDT desired `-1334.2` against paper position `-3001.9`; three
  correct reduce-only `+1667.7` convergence commands were definitively rejected
  `stale_decision`, then the generic retry ceiling made recovery inert. LAUSDT's
  `0.1` residual was correctly classified as venue-minimum dust.
- The root mismatch was two freshness contracts: the owner accepted a complete
  market input up to five seconds old while the uncalibrated paper twin applied
  its 250 ms entry-model age to reductions as well. The remediation preserves
  250 ms for every entry, allows only proven reduce-only paper work to share the
  owner's five-second bound with explicit observation metadata, and keeps
  strict reduction convergence durable under exponential backoff capped at 30
  seconds. Exposure-increasing/sign-flipping retries remain finite and overdue
  reductions remain health-blocking until actually converged.
- The exact journal reconstruction, safety invariants, and evidence boundary
  are recorded in
  `docs/audit/2026-07-22-paper-reduction-convergence.md`. The historical journal
  is intentionally preserved; the next deterministic recovery batch is ordinal
  `0004`, so no reset or fabricated target is needed. Deployment and live
  recovery are not claimed by this local-remediation entry.
- Local validation is green: focused account/execution coverage 219 passed;
  repository doctor, Ruff, and mypy passed; the full gate completed with 2,224
  passed / 1 skipped.

## 2026-07-21 account-kernel incident and local remediation

- The supplied 01:00--12:20 UTC Telegram transcript exposed one severe live
  safety gap and three reporting/recovery defects. DEXEUSDT opened short
  `2.6 @ 12.659` at 12:11:12 UTC. At 12:12:49 Bybit rejected its intended
  Full-position MarkPrice stop `12.913` because the authenticated base/mark was
  `13.0944`; the old owner blocked health and retried but had no deterministic
  software-flat transition. The mark later receded and the old code installed
  the stop at 12:20:14. The position subsequently closed through take profit at
  13:02:22 (`+2.6 @ 11.127`, account-net `+3.94918602 USDT`). That contingent
  recovery does not make the roughly eight-minute unprotected interval safe.
- The remediation was developed from base commit
  `a808c5877b201432798ae6e73aaa94338b7f1332` and is incorporated into the
  current `main` candidate. It implements the audited repair
  in `docs/audit/2026-07-21-account-kernel-incident.md`: authenticated
  venue-stop-first reconciliation; a durable crossed-stop breach latch across
  price recovery and restarts; exact Bybit integer-price normalization; an
  atomic revision-dominating strict reduce-only flat; journal-hash-bound FIFO
  bypass and authenticated-mark fallback; committed-batch replay preservation;
  structured breach-only startup recovery; all-symbol reconciliation; a
  pre-open/first-frame public-L2 watchdog with generation fencing; durable
  lifecycle confirmations; truthful accounting scope; and lossless Telegram
  pagination. The deeper execution audit also persists an entry-attached
  provisional Full MarkPrice stop on every demo exposure command, preserves the
  outermost existing stop during scale-in, re-anchors from fills, handles
  same-message entry/stop races, journals an atomic pre-provider attempt, never
  blindly resends ambiguous entries, rechecks freshness after non-exposure
  leverage setup, preserves child venue identity across partial-fill restart,
  and marks unresolved ambiguity unhealthy while retaining reduce-only retries.
  Public capture also keeps blocking recorder/subscription I/O outside the
  watchdog state lock, and crossed-stop recovery now requires authenticated
  venue flatness rather than reconstructed zero alone.
- Local verification is green: focused account execution/protection 273 passed;
  repository doctor, ruff, and mypy green; final consolidated full gate 2,222
  passed / 1 skipped.
  Graphify's scoped architecture refresh produced 5,238 nodes, 18,862 edges,
  and 334 communities.
- Before the owner-authorized demo/paper rollout later on 2026-07-21, a
  read-only live audit at 13:49 UTC found exact deployed commit
  `a7363070008266888b652104dfdd64f907507f3e`, profile `operational`, demo owner
  active/running with zero restarts and healthy current state, requested-symbol
  readiness true, no local or venue position, no aggregate target, working or
  open venue order, and zero pending/processing/failed requests. The boundary
  remained `DEMO=true`, `REAL_MONEY=false`. This is a historical point-in-time
  flatness observation, not current account truth or authorization to trade.
  The authorized rollout remains demo/paper-only; it does not authorize
  `REAL_MONEY` or mainnet.

## Recorded live authority and topology history

The newest entry below is the last authenticated snapshot before the
2026-07-21 rollout. After that change point, use the exact deployment receipt
and `scripts/ops.sh status` for current authority rather than this prose.

- Installed and authorized implementation commit:
  `a7363070008266888b652104dfdd64f907507f3e`, deployed from canonical `main`,
  profile `operational`, receipt artifact SHA-256 `dc95caaf0be2776f...`,
  activated 2026-07-20 ~17:22 UTC (fleet quiescent ~17:12--17:22 with the
  demo book flat and the owner healthy at the stop boundary; install,
  authority issuance, and activation each verified `a736307` exactly, and
  the first post-activation pinned status reported verify-ok with all six
  persistent services running). Over `f2ad171` it deploys exactly one
  runtime surface: the native-stop ownership verifier in
  `liquidity_migration/venue_protection.py` — the 2026-07-20 16:36 UTC
  BLUAIUSDT false `unowned_venue_order` fix (bounded 10-minute
  terminal-visibility grace) plus the same-day pre-deploy adversarial
  tightenings (Full-stop-only grace provenance, identity-evidence-required
  matching including the live observed binding, exchange-time bound), each
  pinned by consumer-driven regression tests; see the incident entry below.
  No strategy decision path, sizing input, or registered estimator changed.
  The delta also carried research-only breadth and planning artifacts, none on
  a deployed service path; those obsolete working-tree artifacts were retired
  during the 2026-07-21 strategy reset. The pre-deploy audit, full local gate,
  remote install gate, and two independent review passes were all green. Recorded
  mid-epoch change point; clock and comparator identity unchanged.
- The prior change point remains on record: `f2ad171` (receipt
  `43ce6490c8b0dee4...`), activated 2026-07-20 14:04 UTC (fleet quiescent
  ~13:44--14:04; the install
  ran long in normalize-paper over the ~60k-file paper event trees and the
  client-side SSH timed out at 10 min — the orphaned remote install
  completed correctly and the authority issuance revalidated the stopped
  manifest before activation). Over `93133ff` it deploys two watchdog
  false-CRITICAL fixes from the 13:06 UTC pages: a 2-minute floor on the
  journaled venue-snapshot age (the 30s checkpoint heartbeat can legally
  slip past 1 minute during one busy owner iteration; a wedged owner still
  pages via the independent owner-health freshness bound) and
  `head_binding="allow_behind"` for the liveness consumer of
  `require_recent_account_owner_health` (the background fill thread
  ordinarily advances the journal one transaction past the on-disk health;
  exact head binding remains the default and stays mandatory for sizing
  consumers). No strategy, sizing, or execution-path change. Prior receipt
  on record: `2e819c8c7da0fe10...` (`93133ff`, 13:34 UTC same day).
  Over `cd93772` it deploys exactly one runtime change, scoped to the PAPER
  owner: the registered passive-execution A/B
  (`docs/preregistration/passive_execution_experiment_2026-07-20.md`) —
  eligible CONTINUOUS paper entries alternate by trade-id hash parity
  between the unchanged market-IOC twin (arm A) and a post-only
  at-the-touch arm with re-peg and 20s/10bps market fallback (arm B), with
  per-arm metadata on every execution event and startup recovery that
  terminal-cancels orphaned working orders. Demo behavior is unchanged; the
  same install carries runtime-neutral additions (hedge shrinkage support
  at weight 0.0, the weekly `ops.sh kill-criteria` checker, the measured
  execution-cost tooling, and the 2026-07-20 governance registrations).
  The fleet was deliberately quiescent about 13:24--13:34 UTC for this
  staged install; the first post-activation weekly kill-criteria check
  reported NO TRIP. Recorded mid-epoch change point; clock and comparator
  identity unchanged. Prior receipt on record:
  `6e76b2271988694696d934406a7f4be3ef1778516c783fd195e1bd86800a05f2`
  (`cd93772`, 11:04 UTC same day).
  Over `70d666c` it deploys two runtime fixes for the 2026-07-20 morning
  Telegram defects — a 15s freshness floor on position-truth reconciliation
  staleness (the funding-floor pattern; kills the false `age_ns=~4-5e9`
  BLOCKED pages, the "Position truth stale" banner on an in-agreement book,
  and the false owner-health CRITICAL) and Bybit `set_trading_stop` ErrCode
  34040 "not modified" classified as a converged no-op instead of latching
  reconciliation unhealthy — plus the behavior-preserving architecture
  consolidation (account_contracts/strategy_planning/env_flags extraction,
  shared exit-intent, kline-universe, and historical-submission tiers; no
  strategy decision path, sizing input, or registered estimator changed; full
  gate 2,144 passed / 3 skipped). The fleet was deliberately quiescent about
  10:52--11:04 UTC for this staged install; activation verified order
  permissions, the immutable sizing-only model prior, and a fresh
  residual-momentum gate (485 current stable symbols). Recorded mid-epoch
  change point; clock and comparator identity unchanged.
- The prior change point remains on record: `97d0ee0` (receipt
  `64c3d64b...`) deployed the dust-convergence fix. Over `d520792` it added
  one runtime change — a convergence residual that no venue-admissible order
  can express (the 2026-07-20 00:04 UTC ACEUSDT paper block: 0.1 units
  against a ~$5 venue minimum) now classifies as
  `converged_within_venue_minimum` instead of exhausting retries and
  latching owner health blocked — plus the repo-wide Progressive Evidence
  Model documentation alignment and the big-PC V4/V5 research artifacts
  (Lane-2 forward scorer; research-only, no runtime surface).
- The prior change point remains on record: `d520792` (receipt
  `d689e3e6...`) deployed the 2026-07-19 load-bearing audit fixes.
  Over `386120b` it deploys the 2026-07-19 load-bearing audit outcome
  (`docs/audit/2026-07-19-load-bearing-audit.md`): ten verified runtime
  defect fixes — most significantly the owner-process kill via an unguarded
  gapped-book protection read, committed-batch replay over supersession,
  strict REST pagination (no silent settlement truncation), exact Decimal
  order chunking, and terminal-partial component-protection eligibility —
  plus two O(history) scaling removals, each pinned by regression tests
  (gate: 2,088 passed / 3 skipped). No strategy decision path, sizing
  input, or registered estimator changed. This is a recorded mid-epoch
  runtime change point; the registered prospective clock and recorded
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
- The registered clock starts 2026-07-19 14:00 UTC. The start receipt
  declared a calibration window `[2026-07-19 14:00, 2026-09-02 14:00)` and a
  validation window `[2026-09-02 14:00, 2026-10-17 14:00)`; no pre-boundary
  row is eligible.
- Operating interpretation (2026-07-19, per the Progressive Evidence Model in
  `docs/governance.md`): the forward stream is a rolling evidence surface.
  Each committed model or config is graded on the run of days it predates,
  continuously, with recorded change points; the receipt's 45/45 window
  structure remains on file as one declared read that may still be taken, not
  as a waiting period that blocks progressive evaluation of the same stream.
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

- The 2026-07-20 16:36 UTC `BLUAIUSDT unowned_venue_order` CRITICAL was the
  demo owner disowning its own just-consumed Full stop, not a foreign order.
  The stop (venue id `4bf19243…`, kept by Bybit across the 16:13:30
  replacement and therefore in recorded lineage) triggered at 16:35:30 and
  its adopted fill moved the protection record to `triggered`, which removed
  it from the `{active, triggering}` ownership set while Bybit's open-order
  cache still listed the consumed conditional row; the next reconciliation
  pass paged until the row drained (resolved 16:39). The fix (deployed in
  the `a736307` rollout above) gives the ownership verifier a
  bounded 10-minute terminal-visibility grace: provenance-bearing rows are
  verified against the latest native protection record after it leaves the
  active statuses. Pre-deploy adversarial review tightened the window's
  contract: grace rows must carry Full-stop provenance (the only kind the
  manager creates), must match recorded/live identity evidence (recorded
  venue id, lineage, or the still-held in-memory observed id) whenever any
  exists, and the record's exchange time is bounded by the same window so an
  owner-downtime recovery cannot reopen a long-dead venue window. Lingering
  rows past the window fail closed again and the stream/observe path is
  byte-identical. Two residual page/acceptance classes are documented, not
  hidden: (1) same-symbol re-entry while the old consumed row still lingers
  re-pages under the unchanged active-path contract (pre-existing
  strictness, errs safe); (2) after a restart following a first-install
  fast trigger whose record carries no identity evidence, a same-price
  Full-stop row is accepted on the price fallback alone for the bounded
  window (pinned by test as a deliberate residual). Consumer-driven
  regression tests replay the incident shape, same-price foreign rejection,
  partial-stop rejection, restart identity, and the exchange-time bound.
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
forward execution stream accumulates rolling evidence continuously under the
Progressive Evidence Model; at this snapshot no committed config has a graded
forward record, and it is not a strategy-alpha experiment. Promotion is a
five-line note plus a recorded change point when a rolling record earns it.
Real money remains a separate owner door: no runtime status, paper result, or
rolling record opens it on its own.

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
This changes no live/demo/paper authority or topology above. The consolidated
research conclusion and successor direction are in `docs/strategy_program.md`;
detailed retired receipts remain available in Git history.
