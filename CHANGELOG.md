# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. Each entry is kept as it was written on its day, so a later
entry supersedes an earlier one — read from the top down. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

- **2026-08-28 — Isolated engines retain the existing account lease inode.**
  Stopped installation now gives persistent account-lease files root ownership
  and group write access for the isolated engine identities. The deployment
  preserves each file instead of replacing its flock inode, rejects links and
  non-regular paths, and lets both demo and funded services reopen leases made
  before the engines stopped running as root.

- **2026-08-28 — Bybit position-mode startup follows the row, not its cursor.**
  Explicit-symbol checks now request the venue's 200-row maximum and prove
  one-way mode from exactly one matching `linear` row with `positionIdx 0`.
  Demo and mainnet attach an opaque cursor to that complete response; following
  the observed demo cursor repeats the same row. Cursor presence no longer
  rejects a valid startup. Missing, duplicate, wrong-symbol, malformed, and
  hedge-mode rows still abort before a heartbeat or order.

- **2026-08-28 — Rollout recovery repairs producer inputs and lock cleanup.**
  Candidate-universe loading now accepts the deployment's exact immutable
  projection: root-owned mode `0640`, readable by the runtime group but not
  writable by producers. Private verifier-owned artifacts remain mode `0600`.
  This reconciles the producer loader with the installed demo and mainnet
  files without handing either producer authority to rewrite the reviewed
  universe. Lock-file orphan sweeping also invalidates its cache after a known
  staging mutation and bounds every clean cache entry, so equal or coarse
  directory mtimes cannot hide an abandoned alias indefinitely.

- **2026-08-28 — Rollout compilation leaves the incumbent fleet live.**
  The exact target commit now compiles during rollout prefetch. Stopped
  installation rechecks the immutable build source plus the candidate's path,
  owner, hard-link count, and prefetched SHA-256 before copying it, and performs
  no Cargo fetch or compilation. Prefetch fills a clean locked Cargo cache,
  then runs proc macros and build scripts offline in a private network. This
  phase also fetches and binds the target branch and downloads the exact-version
  Python wheels into a byte-digested cache; stopped install builds a fresh
  environment only from that cache with `--no-index`, proves its distribution
  set exactly matches the lock, and atomically exchanges it with the prior
  environment. Transient builders have a runtime bound and are stopped on exit
  or signal. A cancellation before the stop boundary leaves the incumbent
  topology untouched. This removes dependency downloads and the release build
  from the service outage without changing the installed artifact bindings.
  Each prefetch also scrubs its disposable compiler checkout before verifying
  the exact commit, so stale benchmark output and cross-platform metadata cannot
  block or contaminate a later rollout. Cargo's ordinary hard-linked promoted
  binary is confined to the disposable target, byte-verified into an atomic
  single-link handoff, and only that handoff can reach stopped installation.
  Fresh Python dependency verification now enumerates only that generation's
  own site-packages, so stale source-tree metadata cannot enter or reject the
  exact installed-distribution comparison. Telegram control-policy comparison
  also canonicalizes each command before sorting, making its exact four-command
  proof independent of sudo's presentation order. Deployment now also
  reconciles legacy demo-engine environments with the committed exact account,
  venue, and realm binding before the build. Missing bindings are appended
  atomically while host-only dials are preserved; empty or conflicting bindings
  abort without modifying the file. The installed release directory is now
  root:root mode `0755`, matching the activation watchdog's trust boundary, and
  release verification checks that parent before permit creation.

- **2026-08-28 — Exodus handoff uses the position actually abandoned.**
  A v7 pre-settlement fire now snapshots the fresh carry-attributed venue
  quantity and the same ticker's mark price. Target-book v2 carries that exact
  signed quantity alongside its frozen audit notional and direct entry
  deadline; the Rust follower converges entry and partial fills by quantity,
  so later price movement cannot resize the handoff. Legacy v1 target books
  and Exodus state remain readable, while new state is schema v2. The obsolete
  `EXODUS_NOTIONAL_MULTIPLIER` dial is removed. Heartbeat working-entry rows now
  come from a counted live-order index rather than scanning all orders retained
  in the current WAL segment, keeping account-state publication cost bounded by
  live work as history grows.

- **2026-08-28 — Funded risk configuration has one runtime source.**
  The engine now reads the same preflight-validated operational-profile artifact
  as the funded producers. Carry's rendered stop declaration can widen the
  engine baseline but cannot narrow the ceiling used by LONG and Exodus. This
  removes the case where a valid operator dial passed producer preflight and
  was then refused by an engine still holding the committed default.

- **2026-08-28 — Exodus short joins the funded engine as sleeve three.**
  The funded carry producer selects `lane2_exodus_short_v1` and writes
  `exodus-mainnet.json`; the funded engine consumes it as the appended
  `exodus` strategy, crosses entries and covers, and reports its book and fills
  separately. Carry and long keep WAL ids zero and one; boot accepts this
  suffix addition but still refuses any reorder. Once that longer Names record
  reaches the WAL, recovery requires a three-sleeve-compatible binary and
  config. Deployment installs the committed funded engine config atomically,
  validates, quarantines, waits for, flattens, and notifies the funded Exodus
  book. No
  synthetic venue order is used: the first live order waits for a real v7
  pre-settlement exit fire.

- **2026-08-28 — Rollout no longer depends on account-flatness attestation.**
  The deployment path no longer snapshots an outgoing attestor, runs account
  inventory at three rollout phases, accepts `--require-flat`, or requires a
  mainnet attestor credential during activation. It also stops asking the
  outgoing generation for a release marker and activation receipt before the
  fleet is stopped, so the markerless `e4e6750` production generation can cross
  the upgrade boundary. The target build, release binding, ordered fleet stop,
  persistent boot fence, quiescence check, activation lease, target-topology
  verification, and rollback/quarantine handling remain. `attest-flat` stays
  available as an explicit read-only operator command and for loss reset. The
  arbitrary 2026-08-27 key-creation cutoff is also gone; funded identity still
  requires UTA, write access, exact single-host IP, ContractTrade Order and
  Position permissions, no withdrawal permission, and the dedicated account ID.
  A pre-install failure now restores the exact active and persistent/runtime
  enablement topology it observed. Markerless incumbents restart directly;
  marked releases receive a temporary binding to the unchanged artifacts while
  only observed units restart, followed by a replacement completion receipt. A
  failure after checkout mutation leaves the fleet stopped.

- **2026-08-28 — Opening-stop lookup stays flat as order history grows.**
  The live-order ledger maintains a per-symbol, per-side multiset of opening
  stop prices and exposes only each side's tightest level to placement. This
  replaces a full allocation and scan of every outstanding order before every
  batch without moving the durability boundary or weakening whole-position
  stop protection. On the production host's memory-backed filesystem, the
  10,000-order durability median fell from 265 µs to 14.6 µs; the real-WAL
  5,000-order run fell from about 29 s to 7.83 s. Three standard 1,000-order
  native runs put the local submit-result median at 1.26 ms and the
  median-of-runs p99 at 3.16 ms. The Bybit aggregate-inventory tests also state
  their fixtures' actual row counts, restoring the Ubuntu release gate without
  changing production parsing. Private-stream integration tests consume the
  first successful subscription's readiness reset and prove the same reset
  precedes updates after reconnect, matching the runtime contract.

- **2026-08-28 — Venue-mutation bursts yield at bounded safety boundaries.**
  One strategy wake retains FIFO order, its original latency clock, and its
  flood limits across cooperative turns. After each completed placement,
  cancel, amend, or stop mutation, the engine gives ready private lifecycle
  updates and a due account refresh priority before sending the next group;
  an already-selected trailing exit still completes when shutdown becomes
  ready. The strategy-host heartbeat watcher now completes an installation
  handshake and compares the decision projection across both inotify and
  polling handoffs, closing the immediate-start rename gap. Release CI runs
  the optimized engine suite, bounded account-history soak, order-path
  benchmark, and artifact smoke test. Funded disarm remains available when CI
  is red, preempts a running rollout, shares one bounded lock deadline, and a
  canceled rollout leaves the fleet stopped. Rollout builds require the pinned
  Rust toolchain during prefetch as well as compilation. Latency output and
  standing docs call the measured local boundary a parsed submit result; the
  available records do not establish a socket-write timestamp.

- **2026-08-28 — Audit series pushed; Ubuntu qualification is billing-blocked.**
  The 42-commit Rust-only migration series, ending in audit commit `206e40c21`,
  was fast-forwarded to `main`. Push workflow run `33130163698` created both
  Ubuntu jobs, but GitHub rejected each before assigning a runner or executing
  a step because recent account payments failed or the Actions spending limit
  must be increased. This is not a passing or failing test result: release
  qualification remains pending until the account owner fixes billing and the
  exact pushed commit's Python, Rust, bounded soak, build, and smoke steps run
  green. No VPS deploy or live venue order was performed.

- **2026-08-27 — The seven execution-audit gaps become explicit Rust and
  rollout contracts.** Sibling placements now validate and reserve in request
  order, append together, cross one WAL barrier, and reach Bybit as overlapping
  distinct-symbol HTTP chains over a ten-socket warm pool; same-symbol and
  nonce-sensitive chains retain serial wire order. Each mutation endpoint has
  a completion-anchored rolling quota, and native batch cancellation pulls a
  halted book in bounded ten-order groups while private terminal updates stay
  ahead of confirmation deadlines. Risk reservations include cumulative
  opposite-side pending quantity and restart charges only each order's
  unfilled remainder. Opening reprices require finite risk approval and retain
  their full old/requested price range through ambiguity, rotation, and
  restart, so high-price notional and low-price short-stop loss are both
  charged until a definitive answer or cancel. Whole-position stop intent now belongs to the fill that
  actually grows or crosses the position, never an unfilled sibling;
  same-side growth cannot loosen the tighter existing level, pre-wire checks
  include prior-wake live orders, and fresh account views actively repair any
  venue regression or latch opening off. Malformed daily-loss anchors abort
  startup instead of silently resetting the circuit breaker. Before fetch,
  rollout digest-verifies and freezes the outgoing installed engine. That
  immutable binary performs the pre-stop and owners-stopped flatness checks;
  the final boundary requires both it and the digest-bound installed target,
  while the incoming checkout and build candidate never attest. An outgoing
  release without `attest-flat` requires a signed, reviewed out-of-band
  bootstrap rather than falling back to incoming code. Mainnet checks
  receive only a separate globally read-only query key from an exact-schema,
  operator-owned attestor file, never the execution key. Direct install,
  activate, staged, and funded unit start/restart paths no longer bypass
  rollout on a funded-configured host. Mainnet inventory covers ordinary,
  spread, RFQ, active venue-native strategy, and reported cross-account
  asset/bot state. Nonadditive venue aggregates are not treated as an API
  guarantee, while aggregate-only values cannot masquerade as cash unless
  coin detail explicitly identifies positive USDT/USDC. Because Bybit cannot enumerate every bot instance,
  funded identity also requires an account-bound acknowledgement that its UID
  is dedicated to the engine with no hand trading, venue bots, copy trading, or
  other trading API keys. Rollout activation now uses a root watchdog to renew
  a boot- and process-bound ten-field six-second permit while trusted launchers
  supervise the candidate topology; only a synced, verified six-field release
  receipt survives reboot, so process death or power loss cannot preserve a
  partial activation. Permit renewal now records the pre-validation inode,
  takes a non-creating pin, and revalidates it under lock, so direct deletion or
  a valid-looking replacement cannot race recreation or adoption. Remote
  funded stop/disarm execute no checkout code; stop never reads credentials,
  and disarm uses an isolated root-owned interpreter with an embedded strict
  atomic rewrite after persistent quarantine. Deploy preflight and launchers reject
  writable critical checkout ancestry or Git metadata. Bybit startup verifies one-way mode
  for every configured or newly admitted symbol. Execution recovery aborts
  instead of clipping intervals older than venue history. Ubuntu CI runs the
  bounded-ID release soak that separates within-run ID cost from synthetic
  recovery-history cost. UTC loss rollover now carries bounded durable pre-midnight equity
  evidence (periodically and immediately on rises), preventing a crash or the
  first post-midnight order from erasing a boundary loss without making every
  account poll an unconditional fsync. Hyperliquid and Lighter testnets remain canary paths, while their
  mainnets and MEXC mainnet are source-gated from `engine run` until exact-realm
  live lifecycle evidence exists; public-feed continuity checks now match each
  protocol's evidence. Funded risk gains a durable 10 USDT UTC-day account-loss
  halt plus a stopped-engine, flat-account `loss-reset`; demo leaves it
  disabled. Standing docs now match fail-closed foreign-activity handling,
  direct adapter rule reads, and the remaining live-validation boundaries.
  Funded Bybit identity now rejects the exposed key generation and unsafe key
  shapes: keys must be created on or after 2026-08-27 22:30 UTC, UTA,
  write-capable, allowlisted only to the declared production host IP,
  ContractTrade Order+Position capable, and unable to withdraw. Creating the
  replacement and revoking the old key remain external owner actions.

- **2026-08-26 — The demo rule-receipt freshness alert is removed (owner
  directed).** The demo receipt no longer pages `demo_rules_age`; nothing in
  the demo runtime path reads the receipt, and a demo receipt in the back half
  of its life renews itself on the next rollout, so the weekly WARNING only
  taught operators to ignore a WARNING. The funded receipt still gates the
  owner, still renews on any deploy, and still pages WARNING/CRITICAL under
  `venue_rules_age` — that gate is untouched. `check_fleet_liveness.py` now
  scopes the rules-receipt gather to mainnet only.

- **2026-08-26 — The carry rule rename: registered rule goes to `lane2_carry_hold_v7`
  (name only).** The registration that was `lane2_carry_hold_v6` becomes
  `lane2_carry_hold_v7`, so the live name and the config filename both read
  v7. Nothing about the rule, the config, the parameter values, or the
  forward grading changed — the file `configs/lane2_carry_hold_v6.json` was
  renamed to `lane2_carry_hold_v7.json` and its `config_id` updated to
  `lane2_carry_hold_v7`; `CARRY_CONFIG_PATH`, the v7 profile, and both clock
  profiles now read `lane2_carry_hold_v7.json`. The v6↔v7 id is a DATING/NAME
  change point, not an evidence one: rows graded under `lane2_carry_hold_v6`
  (through 2026-08-21) are the same rule under the old id, and the forward
  experiment differential is now `carry_hold_v7_minus_v5`. The journal keys
  (`carry_hold_v6_live_v1`, `carry_hold_v7_live_v1`) and the settled-print
  rollback dial `CARRY_STRATEGY_PROFILE=v6` are unchanged.


  🔴 lost it, only where there is a verdict — and the verdict leads: an
  exit's first line is the dot, the account, the sleeve, and the net in
  bold, because the phone's notification preview shows one line. Every
  message names its account (RM = real money, DEMO = demo), sleeves act in
  verbs (enters, shorts, exits, covers, closed), prices carry four
  significant figures, every return reads as percent of the position (never
  basis points — those stay in the engine's reports), slip reads "paid" or
  "saved" because its adverse-positive convention runs against the net
  beside it, and the daily summary
  opens with the day's own colour
  over a monospace win–loss table whose rows are per account and sleeve — so
  real money never melts into a demo figure. Messages are Telegram HTML now:
  `send_telegram_message` grows an opt-in `parse_mode` argument, opt-in
  because HTML rejects a stray `<` — the notifier escapes its text and asks
  for it, the watchdog stays plain. The notifier's state schema is unchanged,
  so the changeover run sends nothing spurious. `scripts/runtime/
  notify_book_changes.py`, `liquidity_migration/ops/telegram.py`,
  `docs/notifications.md`.

- **2026-08-24 — LLM gate prompt v7: the crime-pump playbook joins the
  rubric (owner approved).** The driver-judgment prompt
  (`scripts/research/llm_driver_ledger.py`) moves to
  `driver-judgment-v7-crime-pump`. Two changes, both judgment food, no new
  mechanical rule: (1) a new enrichment fact `turnover_to_oi_24h` — the
  day's traded volume against the standing open interest (the venue reports
  OI in contracts, so notional derives as contracts × price) — the churn
  read that public research on manufactured pumps calls "brushed" volume;
  (2) the manufactured-pump step now names the two documented crime-pump
  shapes — the low-float walk-up and the short-squeeze bait — and each
  judgment reports a `manipulation_shape` verdict. The one outside number
  (volume-to-OI low single digits typical, 20+ suspect) is labeled
  unmeasured on this desk inside the prompt itself; every measured prior in
  the rubric is unchanged. `--grade` buckets by prompt version, so v7
  accrues its own forward record and v6's rows are untouched. The entry
  gate is unchanged: score ≥ 6, same candidates file, same LONG-sleeve
  sizing, exits, and stops. Motivation: a public post-mortem of seven
  manipulated tokens (MYX, COAI et al.); its mechanical signals are already
  measured dead on this book (OI exits, funding-flip exits, pool-level
  taker reads — receipts in `docs/research/research_findings.md` §2), so
  the judged rubric is the one seam that takes it. This commit is the
  change point. Deployed `b51aa3a8` via `staged --stop-first` the same day:
  verify-ok on the commit, both engines rebuilt on it, the funded engine's
  boot reconciliation stayed clean (`may_open: true` in the mainnet
  heartbeat — false is the latch, `engine/engine-types/src/wal.rs`), and
  the ledger service's first run under v7 completed green on a quiet hour
  (0 movers, 0 triggers, so 0 rows — the first journaled
  `driver-judgment-v7-crime-pump` row is the runtime receipt to watch).
