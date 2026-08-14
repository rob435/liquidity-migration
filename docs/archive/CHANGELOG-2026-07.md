# Changelog archive — July 2026

The dated operational log for entries before 2026-08-01, moved out of the main
changelog on 2026-08-14. Same format and the same reading rule: newest first,
each entry kept as it was written on its day, so a later entry supersedes an
earlier one. Entries from 2026-08-01 onward, and current truth, are in
[../../CHANGELOG.md](../../CHANGELOG.md).

> **Reading the entries below.** The operational-authority receipt was removed
> from the repository on 2026-07-31 (`c396d87`…`f5a37b7`, ~5.1k lines) by owner
> override: it gated every unit start on a clean-checkout hash and changed
> nothing about what a process could trade. `scripts/ops.sh
> operational-authority` and the `ConditionPathExists` gate on all 14 units are
> gone; the installed profile is now a plain `/etc/liquidity-migration/profile`
> marker. **Entries dated before that describe the tooling as it was and are
> accurate history — they are not runnable instructions.** Deployed
> 2026-07-31 in `cdb6e61`.

- **2026-07-31 — `flatten` shipped, both books taken to zero, and the fleet
  rolled out at `0506cef` through the guarded `rollout` path.** First rollout
  this fleet has ever completed: it proves the demo account flat three times and
  a position-taking book could never satisfy that, because turning a sleeve off
  stops publication but leaves its last targets standing in the journal.
  `scripts/ops.sh flatten` closes that gap — it publishes a zero replacement
  target for every component holding exposure and watches the journal until the
  owner converges, placing no order itself. Sequence run: sleeves off → `install`
  → `activate` → flatten demo (journal 44316 → 44383, both CARRY positions
  closed) → flatten paper (10361 → 10542) → `rollout` to `0506cef` with the
  sleeves back on. **The CARRY sample restarts from flat at this date**; the
  VANRY/BANK holdings and their history end here. Fleet after: 8 units, 0 failed,
  both owners healthy, demo equity 255,121.74, paper 253,784.56.
- **2026-07-31 — the paper target mirror is running for the first time.** The
  reason recorded for it being off was wrong. It was not a demo/paper filesystem
  boundary problem: the runner called `ensure_account_route`, an owner-side
  initializer that requires the manifests to belong to the running process. The
  mirror is not an owner and runs privileged on purpose (the demo capture tape is
  0600 root:root), so it now uses `require_account_route` with the paper owner's
  uid named explicitly — the read that function already documents for a
  privileged observer, and *narrower* than the default, which accepts whoever is
  running. No boundary was widened and the tape is still 0600 root-only. Its
  first start also adopted offset zero, which would have republished 9.4 MB of
  the leader's history onto a live paper book; it now adopts the tape head
  durably and follows from there. `CARRY_PAPER_SLEEVE` stays off.
- **2026-07-31 — no code fence blocks a mainnet owner any more.**
  `BybitNativeProtectionManager`, `BybitDemoExecutionAdapter` and the rollout
  readiness proof each refused any non-demo client. All three now call
  `client_venue_realm`, which accepts either realm but refuses a client whose
  declared realm and transport disagree, and refuses testnet — the coherence the
  fences were actually worth. `bybit_demo` stays the adapter's `name`: it is
  journaled as `adapter_name` and keys the position-truth and native-protection
  requirements, so it is an identity, not a description. **Nothing is armed.**
  Mainnet units are installed, disabled and inactive; `account-execution-mainnet.env`
  (paths only, no secrets) and the three state roots are in place;
  `real-money preflight` reports 5 steps outstanding, and all five are downstream
  of `bybit-mainnet.env` — the credential file, which is the owner's own act — or
  of the mainnet sleeve toggles, which are the arming decision.
- **2026-07-31 — `verify` reported a commit it never checked.** It printed
  `commit=$EXPECTED_COMMIT` verbatim, so a host 38 commits behind answered with
  the commit you asked about: `verify-ok commit=70b3a49` came back from a host
  sitting at `cdb6e61`. It now prints the installed HEAD and the requested commit
  separately and fails on a mismatch. An unknown deploy mode also succeeded
  silently having done nothing; it now fails.

- **2026-07-31 — `lane2_carry_hold_v4` registered. NOT DEPLOYED, and the CARRY
  sleeve still runs v3.** A Lane-2 research registration, not a change to what
  publishes: `deploy/sleeves.env` and the CARRY producer are untouched, and a
  regression test pins that v1/v2/v3 score bit-identically before and after.
  v4 = v3 plus (a) the toxic band's high edge moved −5% → 0% and (b) a
  crowding-persistence size multiplier composed with v2's depth ladder. Switching
  the sleeve to it is a separate owner act through the normal deploy flow with a
  recorded change point. **The headline is capital efficiency, not return**: at
  its own capital v4 vs v3 is +1.07 bp/day at t 0.47 (not significant); at v3's
  average capital it is +10.76 at t 3.23, and at that leverage its worst dip is
  *worse* (33.5% vs 28.7%). Sharpe 1.41 → 1.64 (scale-free); MAR 3.08 → 4.14 at v4's own capital, 4.67 at v3's (MAR is not scale-free). 76.2% of the curve's log growth is 2025-26.
  Detail: [`docs/research/carry_hold.md`](../research/carry_hold.md) §0.1.
- **2026-07-31 — the program significance bar is now t ≥ 2.5**, owner decision,
  replacing the family-wise ≈3.25/3.58. Authority is
  [`docs/research/governance.md`](../research/governance.md) §2; `screen_phase1.py` and
  `screen_idio_charts.py` follow it and still print the retired threshold beside
  it. Prospective: verdicts recorded before this date stand as written. It
  admits roughly one false positive across the program's ~45 screened mechanisms
  against roughly one in twenty before, so a plateau and a failed placebo now
  carry the weight the threshold used to.
- **2026-07-31 — everything below deployed to the VPS at `cdb6e61`.** The host
  had been pinned at `b13cbfa` (2026-07-30) while 38 commits accumulated on
  `main`, so `scripts/ops.sh` from a current checkout could not reach it at all
  (`No module named 'liquidity_migration.policy'`). Landed by staged
  `install` + `activate`, not `rollout`: the guarded rollout proves the demo
  account flat three times, and the book was holding two CARRY positions
  (`VANRYUSDT`, `BANKUSDT`). Staged needs only a quiesced fleet, so both
  positions, their venue-native stops and 43,939 journal events survived
  untouched — verified after activation, journal and venue agreeing with no
  mismatches. Two defects surfaced at first contact, both recorded under **Open
  operational defects**: the restructure had rewritten a persisted identity tag
  (fixed in `68171bd`), and the paper target mirror could not start (parked in
  `cdb6e61`; the diagnosis recorded at the time was wrong and it was fixed in
  `0506cef`, see the entry above).
- **2026-07-31 — repository restructure, deployed in `cdb6e61`.** The
  125-module package moved into eleven subpackages
  (`liquidity_migration/README.md`), `scripts/` grouped by who runs it
  (`scripts/README.md`), `tests/` mirrored onto the package, and the dated
  research runs archived under `docs/research/archive/`. **No systemd unit file
  changed** — no `.service` names a Python module; all 19 Exec lines invoke
  `scripts/run_authorized_runtime.sh`. What did change is committed shell that
  ships with the checkout: the ten `-m liquidity_migration.<pkg>.<module>`
  invocations in `scripts/` and `deploy/`, and the six test paths
  `deploy_vps_live.sh` runs as its focused-runtime-tests preflight. **This is a
  code deploy, and the whole restructure must land in one release** — a
  checkout carrying the old shell against the new tree, or the reverse, fails
  at unit start. Behaviour is unchanged: no strategy, threshold, journal key,
  or ledger key was touched, and the suite is the same 2768 tests. One
  exception surfaced at deploy: `_ROUTE_ID_DOMAIN` reads like a module path but
  is hashed into every stored `account_route.json`, and the rewrite moved it, so
  the demo owner refused to start with `AccountRouteIntegrityError`. Frozen and
  pinned in `68171bd`; both live manifests reproduce exactly. The suite is 2770.
- **2026-07-31 — four of six demo-only client fences off the mainnet owner's
  critical path, deployed in `cdb6e61`.** The wallet snapshot
  provider, the start-up order-ownership check, the position reconciler and the
  funding reconciler each refused a non-demo client, so `--realm mainnet` raised
  at construction before any start-up read. All four now take the realm the
  private client names (`require_named_realm`) and carry it in their journal
  sources, snapshot keys, health strings and error text; the three whose own
  names said `demo` lost it (`BybitAccountSnapshotProvider`,
  `{inspect,require}_bybit_order_ownership`). Demo decisions, journal keys,
  ledger keys and error text are unchanged. Two fences still blocked a mainnet
  start as of this entry, `BybitNativeProtectionManager` (built ahead of all
  four) and `BybitDemoExecutionAdapter`; both were lifted later the same day in
  `fb78c6c` — see the entry above.
  Off demo the funding reconciler now refuses a settlement row carrying nonzero
  `cashFlow` rather than double-counting it; that raise is outside
  `degrade_or_raise`. `REAL_MONEY` is still unset, no mainnet credential exists,
  and none of this has run against a funded account. Limitations:
  `docs/real_money.md`.
- **2026-07-31 — mainnet arming tooling, deployed in `cdb6e61`.**
  Four gaps closed: `scripts/maintain/freeze_account_candidate_universe.py` now takes a
  required `--realm`, so the universe can be frozen from `api.bybit.com`;
  `scripts/ops.sh real-money create-state-roots [--execute]` creates the mainnet
  journal roots; `scripts/check_demo_liveness.py` became
  `scripts/runtime/check_fleet_liveness.py --account-scope {demo,demo-paper,mainnet}`
  behind the new `liquidity-migration-mainnet-liveness.{service,timer}`; and
  `deploy_vps_live.sh` gained `activate-mainnet` / `stop-mainnet`, with its
  mainnet `verify` half now conditional on the resolved mainnet sleeve toggles.
  `CARRY_MAINNET_SLEEVE` and `LONG_MAINNET_SLEEVE` are off, `REAL_MONEY` is
  unset, no mainnet credential exists, and none of this has run against a funded
  account. Runbook: `docs/real_money.md`.
- **2026-07-30 13:45 UTC — installed `b13cbfac3` (CARRY sizing anchored to the
  decision; resize alert no longer double-sent), STAGED path with three open
  positions.** Authorization: "fix this permanently please" / "go". Receipts:
  `install-ok commit=b13cbfac38838caa8b9850a3890948dc627b1e28 units_started=0`,
  authority `cf0dce0e…` (`operational`, scope
  `demo_paper_operational_only_no_real_money`), `verify-ok … profile=operational`.
  Prior authority retired to `retired-authority/20260730T134120Z`. 6 services +
  liveness timer up, 0 failed.
  - **Staged, not `rollout`:** rollout needs a venue-flat account; the CARRY book
    held `VANRYUSDT`/`LAUSDT`/`ESPUSDT` throughout. Avoids injecting a forced
    exit into `lane2_carry_hold_v3`'s forward record.
  - **6m52s stopped window (13:38:54–13:45:46):** positions held under
    venue-native stops only, verified armed before the first unit stopped
    (`0.002831` / `0.03769` / `0.04097`). Sizes and entries identical before and
    after — no trade forced. Fleet quiesced in 97s; the LONG-paper drain overrun
    did not recur.
  - **Fixes:** (1) sizing recomputed `weight × live_equity × multiplier` each
    cycle against a 0.1% dead-band, so with book/gross/standing constant, equity
    wander (±0.155%/cycle) cleared it almost every time. 2026-07-30 00:00–13:37:
    208 `carry resize: depth rescale`, zero strategy exits, vs 1 (07-28) → 23
    (07-29). ≈9%/yr of the account at 15.56bp. Sizing now anchors to the
    decision; dead-band is 5% of standing. (2) the notification path reused the
    reduction *admission* predicate, which trips after every fill by
    construction; a `settling` state debounces it. Not a real fault: 1,556/1,556
    journaled venue snapshots clean, every ⚠️→✅ gap under 14s.
  - **Not fixed, pre-existing:** CARRY cycle overruns its 60s interval by
    120–175s (#437 by 13:36), logged under `long_native_event_demo_daemon` via
    the shared base class — why cycles land ~3 min apart. Public linear WS
    `ping/pong timed out` ~every 5 min. One `native protection health is stale`
    at 11:05 returned a request to pending — that string asserts freshness of the
    last venue-side *proof* (4s bound from `reconcile_seconds * 2`), not a
    missing stop; all three stops were armed throughout. It shares wording with
    three gates that do mean protection is absent, which is worth splitting.
- **2026-07-29 18:24 UTC — installed commit `63f32765b` (Telegram observability
  and alert-noise fixes), deployed through the STAGED path with an open
  position.** Owner authorization: the "check telegram logs for errors and fix
  them robustly" / "fix these" chat instruction, with the owner explicitly
  choosing the staged `install` → authority → `activate` path over flattening
  the open CARRY book. Receipt: `install-ok commit=63f32765b units_started=0`,
  authority `90566b86…` (profile `operational`, scope
  `demo_paper_operational_only_no_real_money`), `verify-ok commit=63f32765b
  profile=operational`. Post-activation: 9/9 expected units up, 0 failed.
  - **Why staged, not `rollout`:** the guarded rollout proves a locally and
    directly venue-flat account and refuses otherwise (standing constraint
    below). The CARRY book held `LAUSDT` throughout. `install_mode` and
    `activate_mode` require only a quiescent fleet, so the staged path deploys
    without a flatten. This deliberately skips the flat-account proof; it was
    an explicit owner decision, taken to avoid injecting an operator-forced
    exit and re-entry into `lane2_carry_hold_v3`'s Lane-2 forward record,
    which began accruing the previous day.
  - **Exposure during the ~22-minute stopped window (18:01–18:24 UTC):**
    `LAUSDT` long 458723 held under its venue-native stop only
    (`StopLoss` market order, full size @ 0.03769, verified armed at Bybit
    before the first unit stopped). No owner reconciliation, no producer
    resizes, no notifications during the window. Position size and entry were
    identical before and after: no trade was forced and nothing was injected
    into the sleeve's forward record.
  - What this build fixes, all three observed live in the operator's Telegram
    feed against the journal: (1) **entrypoint logging** — every service runs
    as `python -m liquidity_migration.<module>`, so its module logger is
    `__main__` and sat outside the package logger the default handler
    configured; every INFO record the entrypoints emit was dropped and their
    WARNING/ERROR records rendered through `logging.lastResort` unformatted.
    The owners had delivered hourly digests for days with ZERO
    `Telegram delivered` audit lines. Confirmed fixed at 19:00 UTC —
    `[INFO] __main__: account Telegram delivered page=1/1 chars=646`, the
    first such line in the journal's history. (2) **retired-sleeve digest
    line** — `CONTINUOUS_CYCLE_ROOT` is removed from both owner units and the
    demo runner no longer defaults it, so the digest carries no
    `CONTINUOUS BTC gate: STALE · N min old` line for a sleeve retired the
    previous day; re-promotion must set the root explicitly. (3) **paper
    warmup alert flap** — the bounded queue-head warmup suppression tested
    the health detail with a strict prefix, which the paper twin's
    `execution_model_scope=…;` annotation could never match, so paper paged
    CRITICAL hourly and self-resolved minutes later. Zero CRITICAL pages and
    zero warmup blocks in the 40 minutes after activation.
  - Known defect NOT fixed in this build: the LONG **paper** producer's
    shutdown drain overruns `TimeoutStopSec=180`, so a clean stop is SIGKILLed
    and the unit lands in `failed` (cleared with `systemctl reset-failed`; the
    demo twin stops in ~2s). Same signature as the CONTINUOUS demo unit on
    2026-07-26. The drain is not bounded by the stop timeout it must finish
    inside.
  - Operational note: the staged `install` has no resumable receipt. A local
    client killed at a timeout (this deploy hit the 10-minute cap on the first
    attempt) leaves the remote checkout advanced with no success line; inspect
    remote `HEAD`, unit state, and the retired-authority archive before
    re-running. Re-running the install is otherwise safe and was clean here.
- **2026-07-29 — CONTINUOUS retired; CARRY sleeve deployed (owner override,
  two rollouts).** Owner authorization: the "depromote the continuous strat
  from demo and paper, and replace it with this one. just do it. properly.
  push and deploy" chat instruction (2026-07-28/29).
  - **Rollout 1 of 2 (COMPLETE)**: commit `6331222` (CONTINUOUS retirement),
    Actions run 30407493748 — CI and the guarded rollout green in one pass;
    every phase `phase-ok`, `install-ok units_started=0`,
    `verify-ok commit=6331222f… profile=operational`,
    `rollout-ok` at 23:26 UTC 2026-07-28. The account was venue-flat at
    dispatch (`rollout-flat-ok journal_sequence=30210 positions=0 targets=0
    working_orders=0 venue_positions=0 venue_orders=0`), so no flatten step
    was needed; the interval fleet ran LONG + owners only. Independent
    read-only verify re-confirmed post-activation.
  - **Rollout 2 of 2 (COMPLETE via the staged path)**: installed commit
    `a224afd8812cbb25d63c8370717cab62a80a70b7`, activated ~01:17 UTC
    2026-07-29, `verify-ok commit=a224afd… profile=operational`. The path:
    Actions run 30410327411 (CARRY build `bd4737b`) failed pre-stop at a
    read-only gate with the fleet untouched; the retry run 30411203410
    (bridges `55299fb`) cleared every pre-stop phase, then failed closed
    inside stopped-install at the demo-rule projection and force-stopped
    the fleet per design; the documented staged completion finished it —
    `install` (with the newly supported explicit rule-maintenance request)
    → fresh authority `2c92ad1a…` → `activate` → `verify-ok`. The
    maintenance step re-froze the candidate universe under schema 4
    (508 symbols, `candidate-universe-20260729T005224Z`) and ran the full
    authenticated demo-rule probe (508/508,
    `demo-rules-20260729T005229Z`, reason
    `candidate-addition-or-structural-drift`). Three transition fixes
    landed en route and are now permanent machinery: pre-install phases
    tolerate state their own install creates (`55299fb`), an unloadable
    prior-bound candidate artifact is structural drift → fresh probe, not
    a crash (`c93fdfd`), and a staged install can request rule maintenance
    explicitly across the SSH boundary (`a5af4c6`, `a224afd`).
  - What the CARRY build turns on: the **crowd-fee collector** (registered
    config `lane2_carry_hold_v3`, promoted 2026-07-28) as the CONTINUOUS
    replacement on BOTH fleets: units
    `liquidity-migration-bybit-carry-demo.service` / `-carry-paper.service`,
    daily decision at the 00:00 UTC close computed ~00:20, pure-REST market
    data (settled funding history + 1h klines; kline-derived turnover
    ranking; paper follows the demo market-data plane read-only), stateless
    90-day replay of the registered scorer's own functions
    (`prepare_decision` — live-vs-research parity: identical on every
    shared bar over the last 90 days, differing only at the decision bar
    the research frame cannot see), declared stop 0.35 / no take-profit,
    sizing w × owner equity × multiplier 1.0 under the unchanged 25×
    account risk caps.
  - CONTINUOUS: retired by owner override. Unit files stay
    installed-but-disabled; the
    hedge and rmom timers are off (hedge book flat at retirement); the
    profile's `continuous` block is shrunk to minimum envelope
    (`max_active` 1) so the freed envelope funds CARRY — re-promotion must
    re-size explicitly. The candidate-universe artifact schema bumped 3→4
    (a third `carry` profile: top-150 by 24h turnover, min age 7d); the
    rollout re-freezes and re-probes demo rules when the installed
    artifact is unreadable by the target code
    (`demo-rule-maintenance-plan path=refreeze`).
  - Known sharp edges recorded at deployment (`docs/research/carry_hold.md` §7.6):
    per-symbol-stable entry-attempt keys mean one terminal kernel-side
    rejection suppresses that symbol's entries until addressed (producer
    avoids the self-inflicted cases; kernel-side attempt versioning
    queued); the live bar keying is close-time, one grid-phase convention
    from the research panel, inside the registered decision-clock caveat.
  - Boundary unchanged: **`DEMO=true`, `REAL_MONEY=false`**; mainnet and
    real-money credentials remain unauthorized.
- Prior installed implementation commit: `f1626565f` (7-commit batch
  `0ab8625..f162656`), deployed from canonical `main`, profile
  `operational`, on 2026-07-27 ~18:26 UTC, owner authorization: the "deploy
  all" chat instruction (Actions run 30293398218 — CI and the guarded VPS
  rollout both green in one pass). This batch is the complete remediation of
  the 2026-07-27 repo-wide audit (all 53 findings).
  Rollout evidence: every phase `phase-ok`, `deployment-plan class=routine
  rule_maintenance=reuse reason=fresh`, `rmom-bootstrap path=reuse
  reason=current-valid-gate`, `verify-ok commit=f1626565f… profile=operational`.
  Post-deploy: 9/9 units active, 0 failed, 0 unit restarts, no journal errors,
  demo owner healthy at 249,752.42 USDT equity; the transient
  `ETHUSDT:no_snapshot` at first start cleared within a minute (normal L2
  warm-up). Confirmed live from this batch: `PAPER_EQUITY_USDT=250000` (the
  paper owner now refuses to start unless it equals the committed profile's
  capital reference), `TimeoutStartSec` 2min/5min/15min on the three oneshot
  units, RMOM `MemoryHigh=1G`/`MemoryMax=1536M`, and both routes bound to
  profile `8e7cdffe…`. **Three approved change points ride in this batch —
  CONTINUOUS crowding now counts on the engine's base (strictly more
  crowd-skips, never fewer), Lane-2 financed-longs scoring reproduces its
  registered table, and residual momentum uses the registered calendar
  window** — see `docs/research/strategy_program.md` "2026-07-27 — recorded change
  points". The rollout's own phase gates are no longer fail-open: a failing
  pip/ruff/mypy/pytest phase now aborts instead of reporting `rollout-ok`.
- Prior installed commit: `13754d0be` (8-commit batch
  `2c6703a..13754d0`), deployed 2026-07-27 ~14:03 UTC (Actions run 30270697928 — CI and the guarded
  VPS rollout both green in one pass; no staged completion needed this
  time). What this batch turns on: paper-fleet Telegram (heading "Bybit
  paper", PAPER page label; first hourly digest lands at the next full
  hour), WS transport observability (connect/close/error logging + the
  cumulative silent-window clock — both owners logged
  `raw Bybit public stream connected generation=1` at first start), the
  2026-07-27 audit fix batch (watchdog disk/digest/hedge-runtime alerts,
  reconcile REST-timeout tolerance, journald cap, credential-backup prune),
  the demo-rule half-life auto-re-probe (fired during this rollout: fresh
  receipt `demo-rules-20260727T133929Z` captured 13:39, expires ~2026-08-03
  — the pending kernel-update reboot is now safe to schedule), and the
  **25× operational profile: capital reference 250,000 USDT, account/
  component gross 500,000, per-symbol 125,000, initial margin 250,000,
  leverage unchanged at 2×**, matching the funded wallet (≈249,799 USDT
  read post-deploy; the 2026-07-27 top-up transfers 90k+100k+50k are in the
  venue transaction log). Strategy semantics unchanged (no engine fields
  added; config hashes and kernel strategy identities are stable across
  this deploy). The 10:47 unadopted-execution and L2-stale root causes
  recorded under "Known benign alert shapes" shipped in the same batch.
- Earlier installed commit: `d16daf5a8` ("Align active docs with the
  single funding-gated CONTINUOUS shape", containing `1fe0e48` — the
  operator-ordered CONTINUOUS replacement), deployed from canonical `main`,
  profile `operational`, on 2026-07-26, owner authorization: the "align,
  clean, consolidate docs, then deploy" chat instruction. **Change point: the
  CONTINUOUS sleeve now runs the single funding-gated `turn3_pop3` cell**
  (profile revision `active_single_fund0_tp12_sl35_v1`: age 240d, TP 12%,
  declared stop 35%, weight 1.0, settled-funding admission floor 0.0 with
  counted/journaled unknown-admits). Honest same-window render of the
  deployed shape: +11.06% / maxDD −1.84% / Sharpe 1.45 / MAR 1.80 — see the
  `docs/research/strategy_program.md` promotion note for the reconciliation against
  the redesign table. Kernel strategy identities and config hashes shifted
  for all CONTINUOUS configs; the cycle-status funnel schema is v2.
- **Deployment mechanics receipt (2026-07-26):** the guarded `rollout`
  dispatch (Actions run 30207186469) completed CI, every flatness proof, and
  the stopped-install of `d16daf5`, then failed closed at
  `create-operational-authority`: stopping the CONTINUOUS demo producer had
  escalated past `TimeoutStopSec=180` (mid-cycle SIGKILL), leaving the dead
  unit flagged `failed`, and authority issuance requires exactly `inactive` —
  the run forced the managed fleet stopped with the prior authority already
  retired. Completed the same hour via the supported staged path:
  `systemctl reset-failed` on the verifiably-dead unit,
  `scripts/ops.sh operational-authority --execute issue` (fresh authority for
  the installed commit, bound to the still-fresh 2026-07-22 demo-rule
  receipt), and `scripts/ops.sh deploy --execute activate` → `verify-ok`,
  plus an independent read-only `verify` dispatch (Actions run 30207935844).
  The rollout stop path now clears the stale `failed` flag after each
  verified stop so this cannot recur.
- Prior deployment (2026-07-25, `ac18332b7`, Actions runs 30167979878 /
  30168234520) retained the three-component ensemble with the sizer epoch
  self-heal (`e55f410` + `ddbded5`), the venue-drift cycle-halt fix
  (`967d09e`), the 2026-07-25 execution-plumbing audit fixes
  (`bd54b9f`..`1a8f5f7`), the research-parity admission gates (per-component
  24h re-entry cooldown, crowd-2), and the retained CONTINUOUS 35% component
  stop (`active_tp12_sl35_v1`; anomaly research §20.1) — all still live.
- **2026-07-25 CONTINUOUS entry block, resolved.** Every cycle from 2026-07-22
  22:24 UTC (1,502 cycles) blocked all CONTINUOUS entries with
  `accepted_state_invalid`: `btc_risk_sizing_state.parquet` survived the
  2026-07-22 ledger reset while the journal evidence it references was
  archived away, and the sizer failed closed on prior-epoch state — the BTC
  trend gate and funnel were never the blocker. Remediation (deployed with the
  `ac18332b7` rollout): the sizer **self-heals** — persisted decisions absent
  from the complete authoritative journal are dropped and the state rebases
  onto the replayed authoritative chain, counted and journaled per cycle,
  sizing uninterrupted; `ddbded5` extends the same healing to
  partial-acceptance predecessor gaps and arm/policy retunes. Same-key
  same-arm evidence-hash conflicts (corruption, not epoch drift) still fail
  closed. No reset-time state ceremony exists or is needed. The empty forward
  CONTINUOUS record over those three days is a block artifact, not market
  evidence.
- Boundary: **`DEMO=true`, `REAL_MONEY=false`.** Mainnet, `REAL_MONEY`, and
  real-money credentials remain unauthorized.
- Installed demo and paper operational-profile bytes are identical, SHA-256
  `8e7cdffe6c6b6c775d9b8e887855def9d05ead614eef2c8eb2cf115a9bf2a443` (the 25×
  profile; the pre-scale-up bytes hashed to `cf68369c…`). The tracked editable
  source is `configs/operational.demo.json`.
- Deployment status is authoritative only when tied to an exact pushed commit and
  a `verify-ok` line read from the host for it.

### Deployed — paper-fleet Telegram notifications

2026-07-27 (deployed ~14:03 UTC in the `2c6703a..13754d0` batch): the paper
account owner has its own Telegram notifications
(`account_paper_runner` drives the shared `AccountNotificationEngine`
with a `Bybit paper` heading and a `🧪 PAPER · integration-only twin` label
on every page; demo output is byte-identical to before). Wiring: the paper
owner unit stops scrubbing `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, sets
`TELEGRAM_ENABLED=1` and the paper CONTINUOUS cycle root; deploy provisioning
preserves operator-provided paper Telegram credentials or seeds them from
`bybit-demo.env` (venue credentials remain forbidden in the paper
environment); both paper producers still scrub Telegram variables. The paper
fleet is no longer Telegram-silent; the first hourly digest lands at the next
full hour after the deploy. The same rollout carried the 2026-07-27
observability and resilience fixes
(public-stream transport logging + cumulative-outage watchdog, owner INFO
logging, Telegram delivery audit trail, REST-timeout-tolerant periodic
reconcile, watchdog disk/digest/hedge-runtime alerts and heartbeat
send-failure suppression, LONG_PAPER_SLEEVE toggle, null equity on blocked
cycles, journald cap + credential-backup pruning at provision) from the
2026-07-27 fleet audit. The BTC hedge sizing and BTC trend gate were
independently verified legit-as-designed (bit-identical
recomputation); the one open hedge item is the policy-due model-prior
regeneration, which needs the next standard continuous equity refresh's
component ledgers.

2026-07-27 (owner-directed scale-up): the owner funded the demo account
toward 250,000 USDT (live wallet read 99,920.74 at 10:52 UTC, still below
the stated target — possibly mid-top-up) and directed a scale-up with
leverage unchanged at 2×. `configs/operational.demo.json` is scaled exactly
25× (capital reference 250,000; account/component gross 500,000; symbol
125,000; margin 250,000; every ratio, sizing fraction, and leverage
untouched), and paper provisioning derives `PAPER_EQUITY_USDT` from the
profile's capital reference instead of a per-host tuning value (the runner
script and both authority paths now refuse a missing or mismatched value).
The rollout later the same day installed that profile, so the old
20k-gross/5k-symbol caps no longer bind — see Deployment above. With the full
250k landed, the BTC hedge now crosses its venue-minimum threshold on a single
open short (~$133 target vs ~$65–130 floor): the policy-due hedge-prior
regeneration (old-ensemble vintage) is material from here and stays queued
behind the next standard research refresh.

2026-07-27 (same batch): the demo-rule expiry deadline trap is removed —
rollout now re-probes whenever the bound receipt is past half of its
168-hour lifetime, not only after expiry
(`REGISTERED_ROLLOUT_RULE_REFRESH_AGE_SECONDS`, plan line
`reason=refresh-due-past-half-life`). The hard runtime freshness bound is
unchanged. Consequence: the earlier "dispatch shortly after Wed 21:57 UTC"
advice is obsolete — the current receipt (age >3.5 d) triggers a probe on
ANY rollout dispatched from this code, so dispatch whenever convenient,
before expiry; reboot for the kernel updates only after the refreshed
receipts are installed. The 14:03 UTC rollout exercised exactly that path:
the current receipt is `demo-rules-20260727T133929Z`, expiring ~2026-08-03.

2026-07-27 (DEPLOYED ~18:26 UTC, Actions run 30293398218): the repo-wide
audit remediation (all 53 findings). Three items are change points rather than
refactors and were owner-approved before landing; the full statements are in
`docs/research/strategy_program.md` under "2026-07-27 — recorded change points":
**CONTINUOUS crowding now counts on the engine's base** (funding-admitted fresh
entrants, before the age gate), which can only skip more entries than the
current live shape, never fewer — expect fewer entries in hours where a young
listing shares a signal timestamp with older pumps; **Lane-2 financed-longs
scoring** now reproduces its registered full-calendar table directly (no verdict
moved); and **residual momentum** now uses the registered calendar window, which
rewrites values for gapped symbols — harmless on this fleet because
`run_continuous_rmom_refresh.sh` already runs `--full-rewrite`. Two operational
gates also changed: the rollout script's phase gates are no longer fail-open (a
failing ruff/mypy/pytest/pip phase now aborts the rollout instead of reporting
`rollout-ok` — this rollout was the first to run the rewritten machinery, and
every phase reported explicitly), and the paper owner refuses to start unless
`PAPER_EQUITY_USDT` equals the committed profile's capital reference (verified
live at 250000). The first CONTINUOUS entry decisions under the new crowding
base are the deployment check for M2.

The prior change point: the 2026-07-26 CONTINUOUS replacement
(`1fe0e48`, docs alignment `d16daf5`) deployed the same day — see Deployment
above. Expected first-cycle shapes after that change point, not incidents:
the sizer's authoritative-chain self-heal (`ddbded5`) rebases prior-epoch
state onto the shifted kernel strategy identities (counted and journaled),
and the account notification may show one `CONTINUOUS BTC gate: unavailable ·
unsupported schema 1` line until the first new cycle writes the v2 status
projection. The new revision's forward evidence run restarts at `1fe0e48`.
