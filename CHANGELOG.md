# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. Each entry is kept as it was written on its day, so a later
entry supersedes an earlier one — read from the top down. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

> **Reading the entries below.** The operational-authority receipt was removed
> from the repository on 2026-07-31 (`c396d87`…`f5a37b7`, ~5.1k lines) by owner
> override: it gated every unit start on a clean-checkout hash and changed
> nothing about what a process could trade. `scripts/ops.sh
> operational-authority` and the `ConditionPathExists` gate on all 14 units are
> gone; the installed profile is now a plain `/etc/liquidity-migration/profile`
> marker. **Entries dated before that describe the tooling as it was and are
> accurate history — they are not runnable instructions.** Deployed
> 2026-07-31 in `cdb6e61`.

- **2026-08-21 — the LONG v13 rework is measured and closed: v12 stands.**
  Owner-directed rework attempt of the LONG sleeve's entries and exits. 25
  cells over 2021→2026-08 through the registered kernel accounting, every
  cell paired daily against a v12 baseline the lab first reproduced
  trade-for-trade: exit re-anchoring to live ATR, hold extension for winners,
  regime and volume-fade exits, take-profit removal, the price-volume
  alignment factor (entry veto and exit — both harmful, worst −0.93 bp/day
  t −2.74), intraday rolling-24h entries (immediate, retrace, fallthrough —
  worst −1.44 bp/day t −2.58), confirmation-gated hybrids, and fallthrough
  removal. Nothing clears the bar. The surviving decomposition: intraday
  entry beats the daily-confirmed entry by +16 bp/trade (t 3.76) on the pumps
  that go on to confirm and loses everything on the ones that don't — the
  daily confirmation is information, not latency. New forward-only tool:
  `scripts/research/llm_driver_ledger.py` journals live mover nominations and
  LLM driver judgments before outcomes exist (unarmed without a key; trades
  nothing). Receipts: `docs/research/archive/2026-08-21-long-v13-rework-program.md`;
  conclusions folded into research_findings.md §LONG and strategy_program.md §9.

- **2026-08-20 ~21:35 UTC — the lean-docs commit is deployed (`58a57ecd`,
  staged `--stop-first`).** Pushed with the pre-push gate green, then
  `ops.sh deploy staged --profile operational --stop-first`: install verified
  `commit=58a57ecd requested=58a57ecd`, both engine binaries rebuilt at that
  commit (`engine-ok` and `mainnet-engine-ok`), all units active and enabled,
  `mainnet=armed` untouched. Running proof after the restart: the demo engine
  holds the single-writer lease on 555899665 and read all three target books
  (carry v7, LONG v12 with 9 targets, exodus); the mainnet engine sits in
  shadow with heartbeat, market feed, and private stream up; the carry
  producer's first cycle reports `stale=False frozen=False err=none`, equity
  $1,465.91; the demo watchdog reports 0 active alerts. One doc claim died on
  contact: a plain `staged` does not quiesce a running fleet — it refuses and
  lists the active units; `--stop-first` is what stops them. operations.md
  §recovery now says so.

- **2026-08-20 ~21:26 UTC — the docs and comments lose their history, and a
  rule keeps them lean.** Every core doc is rewritten in the present tense:
  what changed, when, and what it replaced now lives only here and in the
  archive directories. Gone: deletion notes, "formerly/previously/used to"
  framings, retellings of closed programs (each cut verified present in this
  file or an archive dossier first), and whole sections describing deleted
  Python machinery (verified dead symbol by symbol). Kept: every number,
  command, contract, grading anchor, and standing decision. Headline sizes:
  STATE.md 541→446, strategy_program 878→714, architecture 676→602, engine.md
  691→606, carry_hold 428→380 — carry_hold now documents the registered v6
  config in full instead of v1 plus a version story. ~85 history-narration
  comments cleaned across ~40 Python/shell/Rust files with zero executable
  lines changed; the stale comments naming the removed daily loss halt as
  live machinery are reconciled everywhere, including a dangling block in
  `engine.toml` and an unread `.env.example` line. Three stale facts fixed on
  the way: `(deployed)` moved from LONG v11a to v12 in research_findings, the
  evidence boundary now says the missing piece is funded fills (the funded
  engine shadows; the account holds money), and operations.md §Flatten states
  the kernel's real exit-clamp contract. The rule that keeps it this way is
  AGENTS.md §Lean Docs. No behavior change; `dev.sh check` green (ruff, mypy,
  2370 tests, engine suite).

- **2026-08-20 ~20:15 UTC — the write loop is fixed at the strategy, where it
  lives.** Removing the loss halt stopped the `LossGuardTripped` spam and
  changed nothing about the rate: the funded engine went straight on refusing
  at ~340/second, now `AvailableMarginExhausted`, because the owner's hand
  positions hold the account's available margin at 5.9e-05. So the halt was
  never the cause — it was only the reason on the line.

  **The cause is structural: a shadow engine never converges.** The target-book
  follower plans on every quote; a refused order never rests and never fills,
  so the account reading stays flat, the book keeps wanting the name, and the
  next quote asks again. Forever. The follower already solved this exact shape
  for *warnings* — the `complained` list exists because "this runs on every
  quote" — but refused intents fell through to the trait's do-nothing default.
  They now latch the same way: an entry the kernel refused is left out of the
  pass entirely, and the next book clears it, because a new book has always
  earned a fresh hearing here. **Exits never latch** — taking risk off is
  retried on the next quote, and there is a test for each half. The entry test
  was proved failing first.

  Nothing is emitted from inside the refusal wake, so the old property holds by
  construction: no plug ever re-emits into the queue being drained.

- **2026-08-20 ~20:00 UTC — the daily loss halt is removed, whole, on the
  owner's instruction** ("just remove the daily loss ceiling all together, we
  use per position safety"). `loss_guard.rs`, `LossGuardConfig`, the
  `LossGuardTripped` refusal, the kernel's fifth evaluation step, the
  `max_daily_loss_usdt` profile key, the `RM_DAILY_LOSS_FRACTION` dial and the
  seventeen tests that held it: gone. **What bounds a loss now is the
  venue-native stop on each position. Nothing bounds the accumulation of many
  stopped positions in one day**, and the owner took that trade knowingly —
  written into AGENTS.md so no later agent reads the absence as a regression
  and puts it back.

  **Why it went, measured rather than asserted.** It had tripped twice on the
  owner's own hand trading, because `kernel.rs` folded whole-account equity and
  the funded account is one the owner trades beside the bot: 2026-08-10 (450.08
  → 306.06, bot flat, two days locked out) and again 2026-08-20 18:00 UTC
  (equity 479.225 against a floor of 479.446) on an engine that has never sent
  an order. And the ceiling was **a flat $25, not a fraction of anything**: the
  render computes `reference × RM_DAILY_LOSS_FRACTION` at the $100 floor, and
  `envelope.scale()` is applied to the symbol cap (`kernel.rs:292`) and both
  partition shares (`:378-379`) but never to the loss ceiling — `loss_guard.rs`
  read it raw. So a 0.25 dial bought $25 on a $530 wallet, ~4.7%, tightening as
  the account grew. STATE.md called it "the 10%-of-equity daily loss halt" and
  operations.md described it as working "against the day's opening wallet
  equity"; both were wrong, and both are corrected.

  **Two wire-format traps, both checked before cutting.** An unparseable WAL
  record makes the engine refuse to boot by design ("the disk is fine and the
  data is real, so refuse rather than delete it"), so removing a `DenyReason`
  variant is only safe if no surviving log carries it: the **live demo ledger
  has zero** `LossGuardTripped` records (demo never configured a ceiling) while
  the funded log had 1.6 million — with zero orders sent and zero fills, so
  nothing evidentiary — and it was purged with its engine stopped. The demo
  ledger's six `ControlAnchor` records mean that variant STAYS; the kernel now
  simply takes the trait's no-op default, so old anchors replay and are ignored.
  Second trap: the engine refuses a profile key it does not read, and every
  funded host had `max_daily_loss_usdt` installed — a new test asserts that key
  is refused **by name** rather than silently ignored, because an engine that
  reads a ceiling and drops it is worse than one that will not start. The
  retired dial is refused the same way, by the mechanism the repo already had.

- **2026-08-20 ~19:20 UTC — the deep clean is deployed (`41f8c1d4`, staged
  `--stop-first`), and cleaning the VPS found a live defect.** `staged-ok`,
  `verify-ok … mainnet=armed`, and both `engine-ok` and `mainnet-engine-ok` on
  the new binary — the two-units-one-binary trap checked, not assumed. All
  eight units active and enabled, producer state intact across the restart,
  demo engine holding its lease with zero refusals and zero unsent orders, both
  heartbeats fresh. The mainnet preflight prints the sizing this evening
  installed: `leverage 5, gross 1x equity, partition carry 0.50x, long 0.50x`.

  **The defect: the funded shadow engine writes about 1 GB an hour.** Its daily
  loss halt latched at ~18:00 UTC (equity 479.225 against a floor of 479.446)
  and a latched guard turns into an unbounded loop — intent logged, kernel
  denies `LossGuardTripped`, denial logged, strategy told "refused", strategy
  re-emits on the next market message. 300–450 refusals a second, ~30,000
  orders "decided" a minute, from a process in shadow that has never sent an
  order. Measured after the restart: 657 MB/h of WAL and 356 MB/h of syslog,
  against 20 GiB free — **a ~20-hour fuse**, and the live demo engine shares
  that disk. It does not self-heal: the trip is checked before the UTC
  day-roll, is persisted across boots, and `reset_loss_guard()` has no
  production caller. It had already destroyed the journal — journald is
  correctly capped at 500 MB and 99% of it was this one message, leaving the
  oldest entry hours old. Recorded in STATE.md; the fix is an owner decision.

  **The cleanup itself reclaimed ~10 GB, 68% → 43% of a 38 G disk.** Twenty-two
  archived mainnet WAL segments (6 GB): the WAL's own module doc says boot
  replays only the newest trusted segment and "retention is the owner's
  decision", and `rotation.rs` proves an engine booted from a restatement alone
  equals one booted from the whole log — so the archives were droppable, and
  the flock path plus two segments were kept. `/var/log/syslog` at 3.5 GB
  against yesterday's 56 MB, 93% of it the storm: the 798,433 non-storm lines
  were archived to `retired-state` first. A stale 302 MB build clone
  (`engine.old`, untracked, so no deploy could ever have cleaned it). The six
  unreferenced files in `/etc/liquidity-migration` — five tarred to
  `retired-state` with the live engine TOMLs, and the duplicate funded
  credential deleted outright rather than copied into an archive. Rotated
  brute-force login logs, a journal from a machine-id the box no longer uses,
  month-stale deploy staging dirs, a failed rules-probe receipt nothing reads.

  **Left alone deliberately:** the quote-lab tapes (2.5 GB of compressed
  order-book capture behind the registered entry recipes, a twice-recorded
  owner keep-decision), the ledger-reset archives, the superseded receipts, and
  the cargo caches (283 MB of `target/` costs a full cold rebuild on the next
  deploy — 2m44s of build time is worth more than the space here). Two
  pre-existing things surfaced for the owner: `/root/live_demo.sh`, which greps
  the demo key and secret out of the env file with `sed` and launches an
  engine, and an orphaned quote-forge poll loop running since 2026-08-04 that
  STATE.md believes was killed.

- **2026-08-20 evening — a deep clean of the repository (owner: "deep clean
  the repo"). Commits from `bb8bbe0c` on.** Four
  audits ran in parallel — dead Python, engine cruft, doc staleness, test
  hygiene — and every finding was re-verified from source before anything
  moved.

  **Code that nothing calls: about 1,000 lines.** The largest was the indexed
  trade simulator in `data/trade_lifecycle.py` (`_IndexedTradeState` and the
  eight helpers only it reached, 402 lines): `long_native.py` reimplemented
  the loop inline and left the original behind, and the class name appeared
  exactly once in the tracked tree — its own `class` statement. Twenty single
  symbols went the same way, each appearing once repo-wide: the whole
  convergence-report chain, dead accessors on `market_capture` and
  `account_service`, `BybitPositionStreamCache` (its wallet twin stays live),
  ownership and metadata guards orphaned by the Python order-path deletion.
  Ten `TradeLifecycleConfig` fields nothing set and nothing read, five
  `.env.example` keys the same, ten orphan test helpers. Suite: 2385 → 2371,
  the difference being the 13 tests that pinned only the dead simulator.

  **Five guards that could not fail, each proved by mutation.** Make
  `annualized_sharpe` return a constant 0.0 and all six property invariants
  still pass — the loop's `continue` for a degenerate Sharpe swallows every
  iteration. Make `carry_hold_weights` or `KlineStore.get_klines` return an
  empty frame and their "this input changes nothing" guards still pass,
  because two empty frames are equal. All now pin their content first, and
  each was re-run red under the mutation and green without it. Eight further
  assertions were greps for literals of retired features that exist nowhere
  in the tree, and one grep matched only a comment — the probe-CLI test now
  reads its source with comments tokenized out.

  **The engine stops declaring what it never uses.** Five manifests named
  crates no source line mentions; removing them takes 31 packages out of the
  lock, `aws-lc-sys` and `cmake` included, so a clean build no longer compiles
  a C crypto library nothing links. Clippy is at zero warnings from eight, and
  the workspace's one uncalled public item is gone. A comment claiming
  integration tests see only `[dev-dependencies]` was false and is deleted —
  `engine-risk` proves it, declaring `engine-types` in `[dependencies]` alone
  and using it from `tests/`.

  **Twenty stale doc claims, most of them from this same evening.** The sizing
  change left five files describing the old numbers: `STATE.md` still said the
  funded env file holds carry 2.0 / long 1.88 (it does not — `grep ^RM_` on
  the host returns the two protection dials only), and 2× entry leverage and
  the $175 mainnet gross cap survived in `engine.md`, `trading_logic.md`,
  `carry_hold.md`, the `operations.md` dial table and `PORT_NOTES.md`, whose
  every mainnet number was stale and which named three deleted Python files as
  the live reference in the present tense. `trading_logic.md`'s margin
  projection was wrong by 2.5× as a result. Also: two research docs claiming a
  launchd job deleted the day it was made, a p99 that was a worst-of-67, and
  the exodus sleeve — shipped this afternoon — absent from every operational
  doc. The markdown-link test covers none of this: backticked paths and prose
  numbers are not links.

  **Also removed:** 126 bare `# noqa` markers that ruff reported as unused
  under this repo's own selection (the 143 carrying a written reason keep their
  comment; the live ones are untouched). Most name rules the repo does not lint
  at all — BLE001, the ANN family, PLC0415, S310 — but eight were `E402` in
  `scripts/maintain/freeze_venue_instrument_rules.py`, which IS selected: ruff
  exempts imports that follow a `sys.path` mutation, so those markers were
  already inert. `scripts/dev.sh check` green throughout: doctor ready, ruff
  clean, mypy clean, 2371 Python and 745 engine tests passing.

  **Before deploying it, five agents audited the range adversarially** (the
  shipped Python, the engine binary and its dependency graph, the deploy
  mechanics, the registered-evidence surfaces, then a completeness critic).
  Verdict GO: an AST-level comparison of every changed shipped module proved
  no surviving function body changed — all 87 added lines are re-added
  `except`/`import` lines from the noqa strip plus seven docstring lines — and
  the trimmed manifests produce a `cargo check --locked` that leaves the lock
  consistent. It found four things worth fixing, all fixed here: the hardened
  doctor assertion compared `git status --porcelain` against the doctor's own
  `--untracked-files=all`, so any untracked *directory* in a checkout reddened
  the pre-push gate (proved both ways and fixed); the `--telegram` assertion
  dropped above was wrong to drop, because unlike the other five literals that
  flag is live in `check_fleet_liveness.py` and `run_authorized_runtime.sh` and
  the guard pinned that a *producer* runner never gets it (restored); the
  `real_money_profile.py` inline comment still said "past 2x" where the
  docstring beside it now says 5 (deleted, per the minimal-comment rule); and
  `engine-venue/src/tls.rs` justified its crypto-provider pin by a two-provider
  conflict that stopped existing when `rcgen` left the graph in this very range
  — the pin stays, the note now says why it still stays.

- **2026-08-20 ~17:5x UTC — the sub-minimum resize churn is dead at the
  planner (`c15c4740`, staged deploy verified).** The follower's dead band
  (max($1, 5% of standing)) could sit under the venue's $5 minimum order
  value; drift landing in that gap was planned every quote and refused
  every send, forever. The evening's halved notionals widened the gap to
  every position between ~$20 and ~$100 and the churn hit ~55 refused
  sends/second (~33k lines per 10 minutes of journal). The venue minimum
  now joins the resize floor, so a position waits until its drift is
  worth an order the venue will take. Test proven failing against the old
  threshold; live verification after deploy: 0 not-sent and 0 refusals in
  the window that previously logged ~6,600. This closes the long-standing
  churn defect (it predates today at ~1/s; the demo-fleet journals since
  ~2026-08-19 carry its spam).

- **2026-08-20 evening, second entry — the SAME sizing goes to the mainnet
  surface (owner: "make sure the live system has the same sizing too …
  remove the dollar caps … im responsible for flipping the switch, you can
  do anything else").** The mainnet profile is a render of ratio dials and
  already tracks live equity — there are no absolute dollar caps on that
  surface; the dollar figures in the file are the render at the $100
  floor. Committed dial defaults move to the demo posture: carry 1.0 →
  0.5 and long 0.75 → 0.5 (each sleeve at most HALF the wallet, worst
  case), and the venue entry-leverage floor 2 → 5
  (`real_money_profile.py`); `configs/operational.mainnet.json`
  re-rendered from the new defaults, and every pin updated on both sides
  of the cross-language twin (`test_real_money_arming.py` /
  `engine-risk/tests/operational_profile.rs`). KEPT, deliberately: the
  10%-of-equity daily loss halt, the equity floor, the 50/50 sleeve
  partition, and the 0.35 carry stop — capital-preservation controls, not
  sizing. What this changes TODAY: the books the mainnet producers
  publish and what the shadow engine computes — `shadow = true`,
  `ENGINE_LIVE=false`, and `REAL_MONEY` stays the owner's own switch; no
  real order flows from any of this until the owner flips it. Funded
  equity at the change: $515.31, flat, the daily loss guard tripped
  earlier today at −11% (its floor is 10%).

- **2026-08-20 evening — demo sizing change point, all three sleeves
  (owner: "each sleeve to use 50% of the account… lever up 5x so we have
  plenty room for entries… we are running tiny size, lets just see how it
  does").** `operational.demo.json`: carry `notional_multiplier` 1.0 →
  0.5 (LONG already 0.5 — each sleeve now sizes from half the account;
  the exodus short inherits carry's notional at fire, so it halves with
  it), `entry_leverage` 2.0 → 5.0 on all three producer blocks, and the
  account cap `max_leverage` 2.0 → 5.0 so the kernel admits it. Margin
  per entry drops to a fifth, which un-tightens LONG's projected-margin
  gate — the "room for entries" the directive names. The exodus book's
  leverage now follows the operational dial like carry's own book does
  (the registered `lane2_exodus_short_v1` file is untouched; leverage is
  a margin knob, never part of the measured economics — test pins the
  override). Mainnet untouched. **This is a forward-record change point
  for all demo sleeves' fill receipts**: notionals halve for carry, and
  every entry margins at 5x. Research configs and their RAW records are
  unaffected. Deploy receipt below this entry's push.

- **2026-08-20 ~15:55 UTC — the exodus short is live on demo as a third
  engine sleeve, and its first boot exposed (and paid for fixing) a
  boot-order fault in the engine's market feed.** Registered
  `lane2_exodus_short_v1` and built it in `146642f6`: when carry's v7
  pre-settle exit fires, the carry producer publishes the abandoned
  position as a SHORT to the engine's new `exodus` sleeve (own
  `[[strategy]]` block appended to the demo engine config, book
  `exodus-demo.json`, dial `EXODUS_SHORT_PROFILE=v1` on the demo carry
  unit only), covered 60 minutes after the settlement. The stop was
  settled by measurement before the config froze — every level from
  +30 bp to +1500 bp loses against the time-boxed cover on 1m wicks, so
  the declared 0.35 stop is a disaster fence like carry's. Evidence, the
  honest 2024-negative era shape, and the promotion note are in
  research_findings and strategy_program.
  **The incident:** ten minutes after the first three-sleeve boot the
  demo engine began refusing ~150 orders/second (`SymbolNotionalBreached`,
  a phantom ~$156k of LINK on a $1,400 account). Root cause, proven with a
  failing test: the market feed interned its symbol table in subscription
  order (seeds first) while every other part interns the log's order, and
  nothing translates — the exodus DOGEUSDT seed named a symbol the log
  already carried as a runtime admission, so every feed id between the
  seed block and DOGE's old position shifted by one and prices landed
  under the wrong ids. The risk kernel refused everything (zero orders
  reached the venue; `reconcile-clear` report-mode confirmed the ledger
  and the venue agreed throughout) and putting the two-sleeve config back
  stopped the storm instantly, isolating the trigger before the fix
  existed. Fix `e3ac11bf`: `boot_subscriptions` now emits in the
  canonical table order, with a test that rebuilds the exact broken boot.
  Redeployed staged with the three-sleeve config restored
  (`staged-ok commit=e3ac11bf`); verified: zero risk refusals over the
  soak, heartbeat `strategies=["carry","long","exodus"] may_open=true`,
  the exodus book written (empty — no fire yet) and routed to strategy 2,
  and the seed collision itself latched correctly ("another strategy …
  holding this name; leaving it alone symbol=DOGEUSDT", once).
  Two pre-existing observations, not regressions: the sub-minimum
  resize churn continues (~1/s, chipped separately), and the MAINNET
  shadow engine now logs ~200 refusals/second because the funded
  account's hand-traded equity ($479.22 at 15:50 UTC) sits below the
  daily loss-guard floor — the guard is correct, the log volume is the
  same unthrottled-refusal hygiene issue as the churn.

- **2026-08-20 ~12:34 UTC — both LONG producers were down ~10 hours on the
  first live price-touch wake; fixed, redeployed, recovered.** The strategy
  host has been able to wake on a touched price level since the wave-3
  event clocks (2026-08-13), but the event clock's kind table never learned
  the word: the first real touches (mainnet ~01:08 UTC, demo 03:01:23 UTC)
  raised `unknown strategy event kind: price_touch` mid-cycle and killed
  each producer — and each dying process left its target-capture tape
  hash-chain inconsistent near the tail (mainnet line 41225 of 41230, demo
  43425 of 43425), so every later boot failed closed at tape load and the
  units restart-looped until ~12:30 (demo restart counter 588). Held LONG
  positions stayed protected throughout — stops are venue-native and the
  engine keeps its working orders without the producers; carry and the
  engine were unaffected. The fix (`ff3ca996`, test proven failing first):
  `price_touch` joins the clock's kind table at the data-arrival tier, and
  a new contract test derives every kind the host can set from the host's
  own source and constructs each one, so the next wake kind cannot ship
  half-wired. Deployed staged 12:31 (`staged-ok commit=ff3ca996`); the two
  broken tapes were set aside on the host as
  `strategy-targets.jsonl.chain-broken-2026-08-20` (capture tapes are
  derived records on the demo-reset deletion list, so a fresh chain is the
  established recovery shape) and both producers booted clean and cycled
  healthy by 12:34 (`owner=healthy` on both; demo equity $1,454). Also in
  this push (`c1780c86`): v7 is named as the current carry version
  everywhere the dial is documented (trading_logic, operations, STATE's
  forward-days list, the equity wrapper and skill), and the wrapper summary
  now reports the PNG the run actually wrote instead of the alphabetically
  oldest name in the folder.

- **2026-08-20 ~00:10 UTC — the engine's ledger heals itself now, and the
  demo entry block is CLEARED (owner: "fix this permanently").** Two
  repairs shipped in `ce6465ac`+`2c071703` (staged deploys, engines
  verified), the fail-closed check untouched:
  (1) **Recovery** — at boot and after every private-stream reconnect the
  engine asks the venue's own execution history for the window it was deaf
  in and journals the missed fills as `recovered_fill` records, deduped by
  the venue's execution id, durable before the log is compared to the
  venue. The per-symbol fill sum therefore stops drifting behind the venue
  — the root cause of the entry block. (2) **The clear** — `engine
  reconcile-clear --config … --execute` is "somebody looks at the log"
  made executable, for debt older than the ~week of history the venue
  serves: it requires the engine stopped (it takes the log's own lock),
  prints the findings and the ledger-vs-venue table, then appends one
  `latch_cleared` record restating the exposure ledger to the venue's
  positions with the findings kept as the receipt. Run once on the demo
  WAL for the inherited ACE debt: the WAL now reads `reconciled …
  may_open:false` → `latch_cleared` (ACE −14,455.6 → 371.1, note attached)
  → `reconciled … findings:[], may_open:true`. The 00:20 boundary then
  traded normally (2 orders decided at 00:20:58). The next boot still runs
  the same comparison and latches again on anything new — 290 engine tests
  pin exactly that, including "a clear resets the memory, not the check".
  One trap found live and fixed in `2c071703`: the venue wrapper enum
  delegates trait methods by hand, so the new method's default impl
  silently swallowed the real one on the first deploy. The mainnet shadow
  engine gets the same recovery; its latch state is the owner's
  hand-trading and stays as-is (shadow sends nothing).
- **2026-08-19 ~23:20 UTC — the first live early-exit fire has full
  receipts, and the demo engine is refusing NEW entries on a ledger
  disagreement it inherited.** (1) At 21:57:06 UTC the settled-print early
  exit fired EDENUSDT (under `f38f38d7`, before the v7 deploy); the engine
  sold the whole 1,526-unit position at market one second later —
  WAL-recorded fill at 0.05413, 21:57:07.9 UTC, tag `book-exit`, $82.60.
  The producer's `pub exit=0 suppressed=1` counters describe the vestigial
  inbox path; the engine's book-omission path is what executes, and it
  did. (2) Every engine boot today (19:21, 21:55, 22:49) journals
  `reconciled … may_open:false` on one finding: the venue holds 371.1
  ACEUSDT while the engine's own log accounts −14,455.6 — inherited
  accounting debt (five private-stream reconnect gaps whose fills were
  never delivered, plus the 2026-08-07 ACE hand-trade wedge era), not a
  today defect. Consequence: **exits work, new entries are refused** until
  the disagreement clears. The v6-deploy note calling the boot ERRORs
  transient was half right: the symbol-5/7 findings were transient (7 was
  EDEN mid-exit and cleared once it sold to zero — evidence the check
  clears a vanished position at the next boot), the ACE finding is
  persistent. Repair is an owner decision; the natural candidate is
  letting ACE exit on its own dead print, then one engine restart.
- **2026-08-19 ~22:49 UTC — v7 is DEPLOYED to both carry producers: the
  early exit now fires BEFORE the fee pays (owner: "10 min before, let's
  call this v7 … keep improving").** Commit `68b6a29e`, staged deploy with
  `--stop-first`: `staged-ok commit=68b6a29e`, both engines verified on it,
  both producers boot `strategy_profile=v7 early_exit=1` and ran their
  first cycles clean at 22:50–22:51 UTC (a running v7 producer is itself a
  new-code field — old code rejects the profile name at startup). v7 is an
  execution-clock version: profile `carry_hold_v7_live_v1` trades
  `lane2_carry_hold_v6` byte-identical (one config id, its forward grade
  unbroken) and fires the registered −3 bp exit test on the venue's
  running rate whenever a held name's next settlement is at most 15
  minutes away — one public tickers batch inside the window, the existing
  mask path, the settled-print clock kept as fallback. The shipped window
  came from a 13-cell sweep (safety margins LOSE — the edge curve is
  steeper than the premature drag; 30-minute windows churn): every-minute
  15-window nets +19.0 bp/fire all-era, +28.3 in 2025/26, ≈ +2.4–3.1
  bp/day book-level in the modern eras. Rollback dials:
  `CARRY_STRATEGY_PROFILE=v6` (settled-print clock) or `CARRY_EARLY_EXIT=0`
  (registered midnight clock). Evidence rows in `research_findings.md`
  §Settlement-instant timing; promotion note in `strategy_program.md` §5.
  Nine new tests pin the fire/boundary/window/mask/fallback/gate and the
  same-cycle published sell; gate green with the exit code read directly
  (the check-pipe trap struck once again and was caught).
- **2026-08-19 ~22:30 UTC — measured: the early exit can fire BEFORE the
  print settles; proposed, not built (owner: "why not exit before funding
  is paid — front-run the farmers").** The deployed exit sells ~1 min
  after the dying print pays, into the crowd's exodus. Tardis tick data
  shows the venue locks the upcoming rate ~55 s before settlement — the
  S−1 min read matched the final print in 230/230 walk-forward days, so
  the same fire decision is knowable minutes early from a public ticker
  read. On the deployed cascade's own 1,112 fires (5y, 1m klines):
  selling at S−1/S−5/S−10 is worth +6.6/+12.8/+21.3 bp per fire all-in
  over today's S+1 sell (medians +1.9/+3.6/+11.3; the forfeited final
  print costs ~nothing), 2025/26-concentrated; book-level +1.6–3.1 bp/day
  in the modern eras, ~0 before. Premature-fire drag ~1.5 bp/fire-day at
  S−5, zero at S−1. Full row in `research_findings.md` §Settlement-instant
  timing; proposal in `strategy_program.md` §5 (dials S−1 free / S−5
  recommended / S−10 aggressive; settled-print path stays as fallback).
  The side finding — universe-drop exits leak +74/+43 bp over the last 25
  min before the 00:20 fill but only +18/+15 in 2026 — is recorded and
  not proposed. Research only: no producer or engine change shipped.
- **2026-08-19 ~21:55 UTC — the early exit is DEPLOYED to both carry
  producers (owner: "sell after 1 dead hour is the most interesting, I do
  believe it is the right approach").** Commit `f38f38d7`, staged deploy
  with `--stop-first`: `staged-ok commit=f38f38d7`, both engines verified
  on it, both producers boot `strategy_profile=v6 early_exit=1`. The
  shipped rule is parameter-free: a held name whose LATEST settled print
  reaches the registered −3 bp exit threshold is sold at the first cycle
  after the print sweeps in (~1–2 min after its settlement) and masked out
  of the desired book until the next decision bar (mask persisted at
  `carry_early_exits.json` beside each producer root; a deep-again
  midnight re-enters normally). Kill switch: `CARRY_EARLY_EXIT=0`. The
  evidence and its stated gap — fires are 100% fresh-settlement events,
  medians +49…+150 bp/fire 2023–26 and ~59% of fires positive, but
  tail-exposed both ways with the pooled mean at t 2.3 (2026 t 1.5), 2024
  mean negative, 2022 flat — are in `research_findings.md` §Settlement-
  instant timing; the owner chose with the gap on the table. Forward
  grade: the engine's realized exit fills vs the same-day 00:20
  counterfactual. Six new tests pin fire/boundary/mask/persist/expiry and
  the same-cycle published sell; gate 2,359 tests green.
- **2026-08-19 ~21:30 UTC — the evening exit: tonight's exit is knowable at
  23:00, and it is the largest execution number this book has produced
  (owner: "more exits, intraday, be creative").** The modern book is ~100%
  hourly settlers, so the last settled print visible at 23:00 forecasts the
  midnight recovery exit at 98% precision (15 false fires in five years,
  sensitivity 56%). Selling fired names at 23:00: all-in +49 bp per fire
  (t 4.2), decaying monotonically to zero by the deployed 00:20 — recovered
  names leak price all evening, and unfired exits drift identically (+53),
  so the print is the causal permission slip, not the alpha. Combined with
  the 00:02 pass for the rest, the two-leg exit clock is worth
  **+0.7 to +5.3 bp/day, positive all six years, +2.6 in 2026** —
  weight-summed, all-in (skipped prints booked, false fires charged fees +
  re-buy). PROPOSED in `strategy_program.md` §5, owner to decide; nothing
  deployed, no config changed. Full grids and the selection note in
  `research_findings.md` §Settlement-instant timing.
- **2026-08-19 ~20:30 UTC — fill-clock research: entries are already
  optimal, exits are on the wrong side of the drift (owner: "why 00:20 not
  00:00", then "make it predictive").** Two studies on Bybit's own 1m
  klines, all 1,255 v6-book entries and all 1,255 exits, 2021–2026, zero
  fetch failures. (1) The 00:20 entry fill — born as a kline REST margin —
  collects the post-payout dump in full: filling at 00:00 costs 46 bp/entry
  mean (2026 deep entries 90 bp); later buys nothing; the one positive
  early cell is 2025-only. (2) Adaptive snipe rules (sign-flip, retrace,
  stall) are early-fill in disguise and die in 2026; the oracle gap shows
  the path is noise. (3) NEW: exits sold at minute 0/1/3 beat 00:20 by
  +45/+31/+24 bp/exit (t 5.3/4.0/3.5, every era positive, trim-robust),
  worth +0.6–3.8 bp/day weight-summed — an **exit-first early pass is
  proposed in `strategy_program.md` §5, owner to decide**; the exit inputs
  are knowable by ~00:01–00:03 via the WS kline store and the swept print.
  Findings rows beside the 2026-08-03 fill-delay grid in
  `research_findings.md`; no config or clock changed.
- **2026-08-19 ~19:20 UTC — carry_hold v6 is PROMOTED and DEPLOYED to both
  CARRY producers (owner: "get v6 live and running… the real money side as
  well, everything needs to be running").** Commit `8074942d`, staged deploy
  with `--stop-first`: `staged-ok commit=8074942d profile=operational`,
  `verify-ok … mainnet=armed`, `engine-ok` + `mainnet-engine-ok` on the same
  commit, all nine units active after. Both producers boot
  `strategy_profile=v6` (profile `carry_hold_v6_live_v1`); the old demo unit
  drained cleanly after 1,077 cycles with zero errors. v6 is the first
  deployed rule reading a second venue: each producer now keeps
  `binance_whale_daily.parquet` (Binance top-trader position long/short
  EODs, public endpoint, no key) and attaches it to the venue view with the
  research panel's exact as-of shape; every feed failure fails OPEN under
  the registered 48h freshness clause. First live receipts, both books:
  demo whale store 648 rows / 108 symbols / 30 venue-absent nulls,
  `whale_event_rows=618`, `whale_error=None`, decision universe 100; first
  v6 decision holds ACE/EDEN/HOME at gross **0.162** against the v4 book's
  4 names at 0.372 (the bent ladder plus the flow halving, arithmetic
  verified by hand), and demo and mainnet decide identically. The engine
  reads and routes the book (`source=v6 targets=3`); its two boot-time
  "cannot account for" ERRORs are the known transient reconcile shape and
  cleared. Boot-time `failed=8` kline lines are the known benign restart
  shape. The mid-day restart leaves today's entry validity expired
  (decision+6h), so the book re-arms entries at the next 00:20 boundary —
  registered behavior. Forward record at promotion: **0 scored v6 days**;
  v4/v5 keep scoring and the v6−v5 capital-normalised differential is the
  registered experiment. Promotion note in
  `docs/research/strategy_program.md`; archive-vs-API series check ≤0.015
  divergence on a ~1.8 ratio against a −0.26 trigger. Observed in passing:
  the mainnet producer sized off owner-health equity **$541.26** — the
  funded account is no longer near-empty (the 2026-08-12 read was $0.04);
  hand-funded outside the bot, funded engine still shadow.
- **2026-08-19 ~18:40 UTC — everything that is not carry-hold or LONG is
  DELETED, operator override ("kill everything else, brutal again").**
  Sixteen files removed: the three non-carry configs
  (`lane2_financed_leaders_v1`, `lane2_financed_leaders_binance_v1`,
  `lane2_funding_spread_v1`), the whole idio family
  (`idio_features.py`, `residual_price.py`, `cross_section.py`,
  `build_idio_panel.py`, the three `screen_idio_*` scripts),
  `screen_financed_longs.py` (the leaders-vs-benchmark reproduction
  harness), and their six test files. `financed_longs.py` lost its
  leaders/spread classes and dispatch branches — it is now the carry-hold
  scorer only, and a new test pins that the dead rule shapes raise loudly.
  The forward scorer's `DEFAULT_CONFIGS` is carry v1..v6 only; old
  spread/leaders ledger rows remain as receipts. KEPT with reasons: the
  execution/ tree and quote lab (they serve the two live books; quote-lab
  keep-decision of 2026-08-08 still stands), the residual-momentum data
  chain (`daily_feature_panel.py`, `risk_model.py`,
  `residual_momentum.py`, its precompute — the LONG candidate tape and the
  reset tooling carry the artifact as a contract, though no current
  profile reads the feature), `volume_alpha` config +
  `volume_events_charts.py` (LONG substrate), and the cross-venue panel.
  Idio-family keep-note from 2026-08-14 (`screen_idio_directional.py`
  "result-bearing, test-pinned") consciously overridden by today's order.
  Tombstones in strategy_program §Theses 1 and research_findings; dated
  archive dossiers untouched. Full gate green after the cut.
- **2026-08-19 ~18:20 UTC — the premium/momentum blend is DELETED, operator
  override ("brutal deletion, exterminate old research stuff so it doesn't
  contaminate").** Removed: `configs/lane2_premium_momentum_blend_v1.json`,
  `liquidity_migration/research/panels/lane2_blend.py`, its test file,
  `scripts/research/screen_phase1.py` (the blend-family re-screen harness)
  and its test. The blend never scored in the forward ledger and nothing in
  the order path imported it; `financed_longs.py` only cited it in a
  docstring (now points at `carry_hold.settlement_exact_funding`, which is
  what actually runs). Reason on the record: registered 2026-07-24 below
  the bar (Sharpe 0.69 at measured costs), and the 2026-08-19 portfolio
  test showed it LOWERS carry+LONG from Sharpe 2.15 to 1.99. Active docs
  carry tombstones (research_findings §2 row, strategy_program §Theses;
  the momentum_1w thesis section deleted with it); dated archive dossiers
  and old CHANGELOG entries stay as history, per the reading rule at the
  top of this file. `cross_section.py` stays — the idio screens use it.
- **2026-08-19 ~17:15 UTC — the three-book portfolio question is answered:
  two books.** The last big open item in `strategy_program.md` §Theses:
  carry↔LONG correlate +0.002 (stable ~0 in every era, 1,747 shared days);
  **carry_v6+LONG at equal risk is Sharpe 2.15 with a 3.6% worst dip**, and
  adding the premium/momentum blend LOWERS it to 1.99 (drop-one −0.17; the
  blend's 0.69 Sharpe correlates +0.21 with carry — same funding family —
  and earns no slot). The equal-risk pair is 89% LONG by capital (LONG runs
  ~27 bp/day vol vs carry's ~225), so converting the Sharpe into money is
  the previously-declined envelope decision, not research. LONG leg = the
  2026-07-24 on-disk mark-to-market build (ends 2026-07-17); weights
  in-sample. Recorded in `strategy_program.md` §Theses item 6; docs only.
- **2026-08-19 ~17:10 UTC — entry/exit timing program closed with mechanism;
  one designed-not-built candidate (pre-settlement entry).** Owner: "we don't
  have to enter at a specific time every day, we don't have to exit after
  funding is confirmed." Third hunt of the day, documented in
  `research_findings.md` (three new ledger rows) and `strategy_program.md`
  (new thesis §5). Headlines: an hourly engine on the full panel re-ran the
  parked print-clock entry behind the modern filter stack — still loses
  (17.0 vs 25.9 bp/day same-frame), and no fixed survival delay or
  second-print rule interpolates back; decomposition shows "midnight" =
  instant entry at the 00:00 settlement (28.8 bp/d, Sharpe 2.24 alone) plus a
  survived-to-the-bar filter on off-hour prints. 44 free tardis days (32
  downloaded today, 2023-01..2025-08, ~6.4 GB total) explain both closed
  clocks mechanically: price rises +93 bp INTO a deep settlement and falls
  after; rises after a recovery print — the daily lag is load-bearing on
  both sides. Intraday Binance positioning (33,443 deep prints, 5-min data
  fetched for the gap) and Bybit liquidation mix separate outcomes at pool
  level but COLLAPSE on the book's own entries — the stack already removes
  what they see. The opening: the engine forfeits the entry print on every
  fresh entry (291 entries, mean +41.8 bp, ≈ +0.3–0.5 bp/day); a 23:00 entry
  on the venue's live running rate would capture it — positive median in all
  four eras on tardis, unbacktestable on the panel, so it waits for the
  research box's forward capture or an owner-decided demo A/B. No code or
  config changed; docs only.
- **2026-08-19 ~16:10 UTC — `lane2_carry_hold_v6` registered (research-only):
  the depth ladder bends.** Second hunt of the day, owner directive "the
  causes are right, the implementation is crude — more sophisticated,
  non-overfit." ~40 response-shape cells through the same verified harness
  (bit-exact v4/v5 reproduction first), and every crude-to-smooth idea LOST:
  smoothing v5's flow step is a wash (the step is at the right place — the
  measured response is flat above the cut), smoothing the whale step is
  worse (the effect is threshold-local), softening v4's persistence kill is
  worse (names that rarely print deep are net negative even at quarter
  size), inverse-vol sizing is worse (t −2.5; low-vol names lack squeeze
  fuel), raising the depth cap is a pure 2025-26 regime bet (placebo-real
  but worse Sharpe and dip at matched capital), an age taper has nothing to
  taper (median episode 1 day), and the depth-conditional flow drop passed
  era + placebo but failed the clock sweep (14/24, mean +0.27 — the sweep
  caught what the other gates missed). The one survivor: exponent 1.5 on
  the depth ladder's ratio, floor and cap unchanged — same names, same
  days, mid-depth names sized down. vs v5: capital-normalised +0.63 bp/day
  (t 2.86) at midnight, 24/24 clock phases (mean +0.43 — the citable),
  placebo 0/20, exponent plateau 1.25/1.5/2.0 all t ≥ 2.7, worst year
  −0.14; own-capital a wash (Sharpe 1.842 vs 1.841) on 3.5% less gross.
  Pipeline verification reproduced the harness to three digits. Change:
  `depth_scaling.exponent` in the rules (default 1.0; v1–v5 bit-identical
  pinned by test, 8 new tests failed before the fix), `lane2_carry_hold_v6.json`
  with the full selection-debt ledger, forward scorer + differential
  `carry_hold_v6_minus_v5`. v6 is NOT promoted; v5's and v4's forward
  records accrue untouched.
- **2026-08-19 ~15:30 UTC — `lane2_carry_hold_v5` registered (research-only)
  after an owner-directed A/B: two size halvings on non-price axes.** The
  day's hunt scored ~60 in-book cells through the registered settlement-exact
  scorer (harness proved bit-exact v4 reproduction first); premium-discount,
  funding-instability, OI-unwind, and cross-venue-disagreement all closed
  negative or noise — the durable lesson is that depth-correlated cuts remove
  the book's payoff days (the deepest-discount name-days alone are 39% of
  v4's gross P&L). What survived every battery only as a COMPOSITE: halve a
  held name when its 3d turnover growth is ≤ +40% (stale flow), halve when
  Binance's top-trader long/short ratio fell ≥ 0.26 in 3d (whales de-longing);
  the two fire on nearly disjoint name-days and cover each other's blind
  regime. A/B vs v4 on seen data: same mean at own capital, Sharpe 1.62 →
  1.84, worst dip 24.5% → 18.7%; capital-normalised +6.13 bp/day (t 3.30),
  24/24 clock phases positive (mean +3.10), placebo 0/20 (best shuffle 3.26 —
  thin), era gain concentrated 2025-26. Selection debt is stated in the
  config; the v5−v4 forward differential is the experiment. Plumbing: new
  `scripts/data/refresh_binance_metrics.py` (public Binance metrics archive →
  daily parquet), panel `--metrics-root` attaches `bn_tt_ls` as a next-midnight
  as-of join (48h freshness, nulls fail open), `flow_scaling`/`whale_scaling`
  knobs in the rules (default OFF; v1–v4 proven bit-identical by test), the
  daily runner gains the metrics step, and the forward ledger scores v5 +
  v5−v4 (a config whose panel lacks the columns is skipped loudly, exit 1,
  without costing the others their rows).

- **2026-08-19 ~12:10 UTC — The daily automation came off the owner's Mac
  the same day, by owner order.** The owner is buying a dedicated machine
  (a Mac mini) for data work and wants no background load on this one. The
  launchd job is removed and its plist deleted; launchctl, LaunchAgents,
  and crontab were checked — no project-scheduled work remains on the Mac.
  The runner script stays in the repo and is run by hand until the new box
  arrives. Stated plainly: the runner has never finished an end-to-end
  green run. Test 1 failed on the dirty-tree refusal (correct behavior),
  test 2 on the residual-momentum overlap guard (fixed by the full-rewrite
  flag, `bf16b124`), and test 3 was stopped mid-refresh when the owner
  pulled the schedule — at a safe point, during factor-panel compute and
  before any feature-store write, with the lock released and the data
  roots whole. First task on the new box: one end-to-end run.

- **2026-08-19 ~11:30 UTC — The daily evidence run is automated, by owner
  order.** `scripts/research/daily_evidence_run.sh` chains the documented
  sequence — data refresh → full panel rebuild → forward-ledger append —
  under a launchd job on the research box
  (`com.liquidity-migration.daily-evidence`, 14:30 local: after Binance
  publishes yesterday's daily archive, whose late arrival failed the
  too-early 04:40 UTC run). One run at a time (directory lock); one-line
  JSON status (`daily_run_status.json` beside the ledger) naming the
  failing step on failure; the refresh's dirty-checkout refusal is kept
  deliberately — evidence runs stamp git hashes, so a dirty tree fails
  closed rather than scoring unprovenance'd days. The failure path is
  live-tested (first test run failed on exactly that refusal and wrote
  the status correctly); the happy path re-tested on the clean tree.

- **2026-08-19 ~06:10 UTC — The evidence machine is caught up, and the
  promoted carry config has its first forward record.** The 22-day
  backfill ran end to end: append-first data refresh on both venues (the
  one incomplete step is Binance's own not-yet-published 2026-08-18 daily
  archive — everything the panel reads got there), full panel rebuild
  (2021-01-01 → 2026-08-18), ledger append (+3,730 rows, scored through
  2026-08-17). First forward reading, stated with its size: over ~17–22
  forward days, `lane2_carry_hold_v4` is the **only config positive**
  (+15.04 bp/day, 17 days), and the promotion's own experiment — the
  paired daily differential **v4−v3 — reads +37.01 bp/day** over those 17
  days. Right sign for the 2026-08-03 promotion; far too few days to call
  significant, and the window was brutal for the whole family (v1 −94.20,
  v3 −20.00 bp/day forward). The machine only stopped because it was a
  hand ritual; automating the daily sequence remains proposed, owner's
  call.

- **2026-08-19 ~04:30 UTC — The brutal cleanup, by owner order ("delete and
  don't look back"), executed with receipts.** Three read-only scouts
  inventoried; every candidate was re-verified from source before a single
  cut; nothing was deleted by a subagent's hand. **Deleted from the tree
  (git history holds all of it):** the dead top of the old Python order
  path — `venue/account_reconcile.py` and `venue/account_execution_stream.py`
  (~2,400 lines reached by nothing but their own tests, which went with
  them, `test_account_funding_reconcile.py` included) — plus four
  receipt-less research leftovers: `tune_phase5.py` (archive-mention only),
  `diagnose_idio_panel.py` (closed program's diagnostic), 
  `build_execution_cost_report.py` (the engine's `fills` subcommand is the
  cost reading now), and `quote_lab/tape.py` (zero callers even inside the
  kept package). `bybit_execution_adapter.py` and `entry_quote_manager.py`
  stay deliberately: they are fixtures for the kernel's own tests, the
  adapter carries the never-rename identity string, and the engine's
  working-order port names the quote manager as its reference. **Deleted
  from disk (untracked):** ~12.5 GB — the cargo build cache, 187 MB of
  superseded refresh runs, seven dead local event-store roots (all
  CONTINUOUS/paper era), the closed idio program's 228 MB regenerable
  panel, orphan reconcile outputs, tool caches, a stale bench log. **Host:**
  two orphaned July-13 files in the demo config dir (superseded by the
  receipt system) and a five-week-old credential backup removed; the
  "retired sleeves.env toggle" long flagged as pending turned out already
  clean. **The doc lie hunt that rode along:** a second sweep found and
  fixed operator-facing falsehoods in eleven more files — worst three: 
  `deploy/engine.env.template` claimed the engine runs a sandbox account
  (579580669) when it owns the live demo book (555899665);
  `deploy/systemd/README.md` and `docs/architecture.md` both named a
  deleted unit as the sole venue mutator; `docs/notifications.md` described
  an emergency close button that does not exist (the panel is pause/resume
  only — closing the book is `ops.sh flatten --execute`). Also fixed:
  activate's start order (producers first, engine last — the doc had it
  inverted), the wedged-command section that promised an auto-clearing
  reconciler nobody runs, the repo-map skill that never mentioned the
  engine, and the equity-curve skill citing the superseded carry config.
  Kept, each with its reason on file: the quote-lab package (owner
  keep-decision, twice recorded), `screen_idio_directional.py`
  (result-bearing and test-pinned), `research/engine_config.py` (the
  research→engine plug seam this architecture is for), the six `account/`
  modules (import-graph-proven producers' library), and the registered
  blend/funding-spread configs (the active forward-scoring program). Gate
  after the cut: doctor, ruff, mypy (130 files) clean; 2,438 Python tests;
  29 engine suites; docs-links green.

- **2026-08-19 ~02:00 UTC — The owner's skepticism audited: 24 stale doc
  claims fixed, the queue consolidated, and the "short system" question
  answered with receipts.** Two independent read-only sweeps plus source
  spot-checks verified every claim before any edit. The worst rot: this
  repo's own program file kept the settlement-sawtooth item OPEN for 18
  days after its dossier killed every hypothesis (H1 "blocked on minute
  data" was withdrawn by that same dossier on 2026-08-01 — the fetcher
  exists and `klines_1m/` holds 2,034 partitions); `trading_logic.md`
  asserted a CONTINUOUS envelope "stays" in profiles whose schema now
  refuses one; `STATE.md` carried a stale live risk ceiling (dials total
  10.0, not 9.9, since `4a8f8301`) and presented the deleted Python owner's
  latency numbers as current state. All four docs corrected; line-number
  citations that had rotted (one past end-of-file) replaced with function
  names. **The short-system answer:** production never shorted — carry is
  long-only (long the names crowded shorts pay to press), LONG is long-only,
  and no producer has ever emitted a negative notional; what left the tree
  was the CONTINUOUS BTC/ETH hedge (08-14) and ancient `research_v3`
  listing-short runners (long before). The engine's short *execution* path
  is alive and tested; the registered blend and funding-spread short legs
  are intact and forward-scored. **Owner deletion decisions executed
  tonight:** the deletion-blocked CONTINUOUS admission scoring item is
  RETIRED (both docs, one line each); the two dead listing-short tables in
  the program file's priors are compacted to their conclusions (git history
  holds the tables). **Deletion refused, with proof:** the six ex-owner
  `account/` modules are all load-bearing per a transitive import graph
  from live entry points — `carry_demo` → `account_route`, LONG producer →
  `account_service`, watchdog → `account_kernel`/`account_owner_health`,
  quote lab → `market_capture`, `execution_adapters` carries the
  never-rename adapter name. File-level deletion there breaks the demo
  fleet; the dead weight is intra-module functions, a separate carving job
  nobody has ordered. **The evidence machine restarted:** every data root
  on both venues had sat at `date=2026-07-27` since the last hand-run on
  2026-07-28 — three weeks of the promoted v4's forward record never
  scored. The 22-day append-first backfill is running; panel rebuild and
  ledger append follow. Automating the daily sequence so it cannot
  silently stop again is proposed and awaits the owner.

- **2026-08-18 23:53 UTC — The whole 08-17/08-18 wave shipped: committed as
  `ee8b72a6`, pushed, and staged-deployed to the fleet.** Gate before the
  push: doctor, ruff, mypy clean; 2,483 Python tests and 29 engine suites
  green, exit code captured off the gate itself. One commit, 104 files:
  the live rules out of `research/` into `liquidity_migration/rules/` with
  the import-order AST test; the engine's in-flight cover book, WAL segment
  rotation, quote-age bound, and leverage pre-arm under per-realm
  `leverage_authority`; the deploy-env doctor check; the two rewritten
  pagination test pins. Deploy: `staged-ok commit=ee8b72a6
  profile=operational`, both engine binaries rebuilt at that commit,
  `verify-ok … mainnet=armed` (pre-existing owner state, untouched).
  **One completing hand-edit:** the staged deploy deliberately never
  rewrites `/etc/liquidity-migration/engine.toml` (it is a filled-in
  template, hand-rendered 08-14), so the new `leverage_authority = "sole"`
  line was added there by hand inside `[engine]` (backup
  `engine.toml.bak-20260818-preleverage` beside it) and the demo engine
  restarted — clean boot, lease retaken, both books read, heartbeat live.
  Absent keys fall to code defaults, so rotation (256 MB) and the quote
  bound (30 s) were already active from the binary alone; mainnet's config
  stays without the key, which *is* `"shared"`, the right authority while
  the owner hand-trades. `[engine]` does not refuse unknown keys, so a
  clean boot alone proves nothing about the new line — the positive proof
  had to come from the first post-deploy carry entry window, read off the
  WAL. **Read 2026-08-19 ~00:20 UTC: it works.** The first entry of the
  night (ACEUSDT — leverage freshly needed, flat account) went
  decided→wire in **8.7 ms** where yesterday's leverage-needing entries
  paid a 168.7 ms median. Two sibling entries decided in the same pass
  show 199/369 ms, but that is head-of-line queueing — the engine sends
  grouped entries serially, each awaiting the previous order's ~190 ms
  venue acknowledgment — not leverage. And EDENUSDT (169 ms) was a name
  the engine had never followed until seconds before the entry: pre-arm
  at book arrival silently skips a symbol not yet in the market table,
  and the inline confirmation on the order path carried it, exactly the
  designed fallback. Reading those stamps honestly has two traps, now
  written down: grouped entries share one `decided_ns`, so a naive
  decided→wire join reads queueing as per-order latency; and WAL records
  are internally tagged (`"kind": "order_sent"`, snake_case). One
  follow-up candidate observed, not built (per the no-unrequested-
  machinery rule): sibling entries serializing behind each other's acks
  is now the dominant tail on grouped books — concurrent sends are the
  next real p99 lever if the owner wants one.

- **2026-08-18 22:24 UTC — The expired demo rules receipt renewed in a flat
  maintenance window; `demo_rules_age` cleared.** The receipt from 08-09
  passed its 168-hour bound on 08-16 (the alert itself had been made
  truthful on 08-17: WARNING, not CRITICAL, because nothing in the runtime
  path refuses on it — what goes stale is the evidence the research and
  candidate-universe tooling reads). The renewal is the order-placing
  PostOnly probe, and the probe requires a genuinely flat account — any
  position it sees, it treats as its own and fails. The window:
  `ops.sh flatten --execute --environment demo` (its first real use since
  the 08-18 argument-passing fix) stopped both demo producers and handed
  the engine zero books; the engine closed BMTUSDT and HOMEUSDT and worked
  ACEUSDT down to 0.1 — where flatten timed out, and the logs said why in
  two lines: the LONG follower left ACEUSDT alone as another strategy's
  name, and carry's own attributed share came to $0.02, under the venue's
  $5 minimum order value. The remainder was a pre-engine foreign scrap
  plus sub-minimum dust — unclosable through any minimum-respecting book
  path. With the demo engine stopped (watchdog timer too, to keep the
  window quiet) and its lease therefore free, the 0.1 ACEUSDT was closed
  by hand with the repo's own leased client — reduce-only Market, which
  the venue exempts from the minimum — and the venue confirmed flat.
  `deploy staged --stop-first --refresh-demo-rules --profile operational`
  then re-probed on the flat account and bound
  `demo-rules-20260818T220119Z` (expires 2026-08-25 ~22:01 UTC; any
  rollout in the back half re-probes on its own), reinstalling the same
  commit `54560ea5` the box already ran — verified identical on the box,
  on GitHub, and locally before the window opened. The fleet came back
  whole (`verify-ok`, both engines rebuilt, both watchdog timers
  re-enabled by the deploy), and the next watchdog pass printed
  `cleared: demo_rules_age`. The demo positions the window flattened are
  the sleeves' to re-open on their next cycles.

- **2026-08-18 (second entry) — The plug-and-play gaps closed, on the owner's
  instruction.** The morning's architecture review named four ways the repo
  fell short of "completely modular plug and play" and ranked four
  improvements; the owner said build all of them, plus move the
  production-critical code out of `research/`. Three subagents did the
  building; every load-bearing claim was re-verified from source afterwards,
  and every genuinely new test was proven to fail with its mechanism
  neutered. Nothing here is deployed; the tree is uncommitted.

  **The registered rules moved out of research/ — new package
  `liquidity_migration/rules/`.** The live sleeves, the policy package (the
  real-money profile among them), and the CLI were importing their decision
  rules from `research/backtest/` — production-critical code living under a
  name that says "safe to break", which had already once nearly cost us
  research tooling misread as unused. `rules/` now holds the target-book
  contract (`engine_targets`), the persisted LONG identity strings, the
  registered LONG profiles and feature builders (`rules/long_native.py`),
  the registered carry rule (`rules/carry_hold.py`: `prepare_decision`,
  `carry_hold_weights`, `daily_grid`, `top_n_universe`,
  `settlement_exact_funding` and helpers), and `momentum_signals`, whose only
  consumer in the repo was the LONG rule. The historical engines stay in
  `research/backtest/` and import the rules back — one body of decision code,
  as before. Every moved def/class was mechanically compared against `git
  show HEAD:` and is byte-identical (the single exception carries the
  morning's uncommitted forward-window fix verbatim). The frozen
  `reproduce_with` recipes in `configs/lane2_*.json` still execute —
  `CarryHoldConfig` remains reachable through `financed_longs`, and the
  score functions never moved. Import order gains one rung: `core →
  marketdata → data → account → rules → research → strategy → venue →
  policy → ops/cli → runtime`; measured after the move, `rules` imports only
  `core`. Package README, root README, trading_logic, carry_hold, engine
  doc, and the repo-map skill all updated; dated receipts left as history.

  **The import order is now a test, not a sentence.**
  `tests/repo/test_import_order.py` walks every module's imports from the
  AST and fails on any edge pointing up or sideways in the order — an
  unranked (new) package is itself a failure, so the order can only grow
  deliberately. `ops` and `cli` share a rank and, verified from the real
  edges, do not import each other; that is encoded. Proven to bite on
  synthetic bad edges and on a real upward import temporarily planted in
  `core/` (removed byte-exact afterwards).

  **Engine: one new event no longer edits every strategy.**
  `Strategy::on_event` is now a provided method holding the single
  exhaustive match over `EngineEvent`, routing to five provided hooks with
  do-nothing defaults (`on_market`, `on_timer`, `on_order`, `on_targets`,
  `on_intent_refused`). The follower, the touch sniper, and the authoring
  template override only what they use; a future event variant is one enum
  arm plus one defaulted hook, zero strategy edits. The follower's
  never-emit-from-an-order-wake property now holds by construction — it
  does not override those hooks at all.

  **Engine: the in-flight cover book moved out of the follower into the
  engine.** The bridge across the ~2.5 s account-view lag (without which a
  strategy re-enters after every fill) was ~120 lines of private
  bookkeeping inside one strategy; a second book-following strategy would
  have had to reimplement all of it. It is now `engine-core/src/covers.rs`,
  keyed per strategy and symbol, fed at the engine's own four lifecycle
  points: booked at the send with the post-quantization size (so the old
  ack-clamp is unnecessary by construction), released on reject in full and
  on cancel by the unfilled remainder, dropped for the whole symbol on a
  refused exit (the account reading is the fact), and absorbed against each
  new account reading by the same delta rule the follower used. Strategies
  read one number: `ctx.in_flight(symbol)`, signed; `position`/`my_position`
  are untouched. The follower now computes held = reading + in-flight and
  keeps only its policy (foreign-position exclusion, closed-under-us latch,
  busy gate). Two deliberate deviations from the old follower, both pinned
  by tests: a refused ENTRY no longer releases anything, because at the
  send-point registration a refusal never had a cover — releasing the
  newest would steal a live cover from an earlier send (the exact
  double-entry window this exists to close); and shadow sends ARE covered,
  because a shadow order never fills and never produces news, so an
  uncovered shadow follower would resend the same pretend entry on every
  quote — today's funded-engine evidence run is exactly this shape. One
  strict improvement fell out: an intent dropped by the per-wake flood cap
  used to leak a phantom emit-time cover; with send-point booking it never
  books. One latent fault was preserved deliberately for parity, and is a
  named candidate follow-up, not smuggled in: two covers on one symbol each
  carry their own anchor, so a single reading movement can be eaten twice
  (needs a second send before the first fill is shown; the busy gate mostly
  prevents it). Proof battery: eight neuter mutations (register no-op,
  absorb no-op, release no-op, refuse no-op, refuse-as-drop-newest,
  follower ignoring in-flight, shadow-guarded registration, registering at
  the asked rather than quantized size) — each killed by named tests;
  workspace 718 tests green.

  **Engine: the log now rotates instead of growing forever.** New config
  `wal_rotate_mb` (default 256, `0` = off; a missing key boots fine), checked
  only on the group-flush tick so it can never fall between an intent's
  durability barrier and its send. A rotation opens the next numbered
  segment beside the configured path and writes one new record kind,
  `SegmentBase` — a restatement of everything boot replay actually consumes,
  which an inventory showed includes three sums over unbounded history
  (per-strategy fill attribution, per-symbol logged exposure including
  strangers' fills, and the newest intended stop per symbol) that no
  restating-with-old-kinds design could carry without copying the whole
  fill history. One record is atomic under the existing frame checksum, so
  "complete enough to trust" is a single mechanical check: boot replays the
  highest-numbered segment whose first record reads back whole and falls
  back to the previous segment otherwise — a crash at any byte of a
  rotation loses nothing and invents nothing (the ordering argument is a
  four-step comment at the rotation itself, and a truncation sweep across
  seven byte offsets tests it; the sweep caught a real hole in the first
  cut, where a segment torn inside its 8-byte header refused boot instead
  of being skipped). Old segments are archived in place, never deleted —
  retention is the owner's call. The `fills` and `replay` tools read the
  whole segment family; a never-rotated log is a family of one, CLI
  unchanged. Honest residuals, on the record: a very late fill for an order
  that ended before a rotation is charged to no strategy after a
  rotation-plus-restart (diagnostics, not safety; reconcile still sees the
  exposure), and the mint-id collision check now sees only open orders plus
  the current segment, so a wall clock stepped back to an earlier boot's
  exact millisecond could theoretically re-mint an ended pre-rotation id.

  **Engine: a declared bound on trading against a stalled feed.** The only
  staleness the engine declared was the account view's age; quote age was
  bounded nowhere — the feed pings and reconnects, but a silent-but-alive
  socket leaves the last quote standing and nothing refused to open
  against it. Now `max_quote_age_ms` (default 30 000, serde default so
  deployed configs boot) refuses to OPEN when the quote the decision was
  made against is older than the bound, measured on the quote's own
  receive stamp — never a wall-clock guess. A symbol that has never quoted
  (or whose stamps a feed reset wiped) is refused the same way: the
  absence of a price is the stalest price. Exits flow whatever the age,
  and cancels and amends never pass the gate — same asymmetry as the
  account-view bound. The refusal is a logged verdict (`StaleQuote`,
  mirroring `StaleAccountView`) through the normal refusal path, so the
  strategy hears `IntentRefused` and the cover book stays consistent. Both
  new keys landed in both engine config templates with comments.

  Engine total after both: 733 workspace tests, 0 failures, release build
  clean; 15 new tests each proven to fail under a targeted neuter of its
  mechanism (four neuters for rotation, one for the gate).

  **The leverage round trip left the order path, on the owner's
  instruction, after live measurement.** Total decision-to-execution
  latency was measured from the live demo engine's own log (all 67 real
  orders since 08-14): 179 ms median to "live at the venue", but p90 512 ms
  and worst 1.01 s — and the tail decomposed cleanly: 27 of 67 entries paid
  an extra ~169 ms (844 ms worst) BEFORE the wire, re-confirming leverage
  inline because the cache forgets every flat symbol on every account
  reading, and every carry hold ends flat before the next one opens. New
  engine config `leverage_authority`: `"shared"` (the serde default —
  today's behavior exactly, and the mainnet template's explicit setting,
  because the owner hand-trades that account) and `"sole"` (the demo
  template's setting — the engine holds that account's single mutation
  lease). Under sole: the cache survives flat spells; the book's leverage
  is armed at book arrival, where nobody is waiting on an order (never in
  shadow — a shadow engine must not mutate the account it reads); and the
  venue's own position rows, which now carry the row's `leverage` into the
  account view, verify every held position — a mismatch alarms, writes a
  log Note, and evicts the trust so the next entry confirms inline again.
  In effect the pre-send confirmation became a post-fill verification
  against a better witness. Five new tests (sole keeps cache across flat,
  shared still re-confirms, contradiction evicts + is written down, book
  arrival pre-arms, shadow never arms) plus config and parse pins; four
  targeted neuters each killed exactly their own test and nothing else.
  Expected effect on the live numbers: decision-to-venue collapses to the
  ~172-178 ms geography band for every entry; the remaining tail (two
  long-idle orders at 277/486 ms) has no proven local cause — the account
  poll keeps the connection warm, so cold TLS does not explain it — and no
  warmer was built on an unproven diagnosis.

  **The doctor now proves every deployed toggle has a reader.**
  `scripts/dev.sh doctor` gained a fourth section: every key set in
  `deploy/*.env(.template)` (28 today) must appear, whole-name-bounded, in
  package, scripts, deploy, or engine source — a toggle nothing reads is
  either a deleted reader (the host file carries a dead switch) or a typo
  whose real reader silently defaults. `RUST_LOG` is allowlisted with its
  reason (read by the tracing library inside the engine binary,
  `engine-core/src/main.rs`), and an allowlist entry whose key disappears is
  itself flagged, so the exemption list cannot outlive its keys. Proven
  live: a planted `FAKE_RETIRED_TOGGLE` turned doctor to `overall: error`.
  Known limit, on the record: the retired sleeve toggle from 2026-08-03
  lives in the HOST's hand-managed sleeves.env, which no repo check can
  reach — one-line cleanup owed on the next fleet session.

  **Two stale test pins from yesterday's pagination fix, found by the full
  suite and fixed.** `tests/venue/test_bybit.py` still pinned the OLD
  "raise on a mid-range empty page" contract that yesterday's re-ask fix
  replaced — it escaped yesterday's gate, which should not have been
  possible and is noted here honestly. Its `open_interest` twin was worse:
  passing vacuously, its fake keyed to an endTime the exclusive-bound cap
  never requests, so the fake's KeyError was wrapped into the very error
  the test expected and the pagination path never ran. Both now pin the
  real contract — one ask, exactly two re-asks, then the empty page is
  believed as end-of-history — asserting the full call sequence.

- **2026-08-18 — Three-auditor sweep: nine code fixes with fails-without-fix
  proofs, and the operator prose caught up with the 08-14 deletion.** A deep
  audit (three parallel readers: engine, Python package, operational glue),
  every load-bearing claim re-derived from source before anything was touched.
  Baseline was green before and after (`dev.sh check`).

  **The emergency close command never worked from the laptop.**
  `ops.sh flatten` handed `remote_exec` the bare script path as the remote
  script body, so `flatten_account.sh` ran on the host with zero arguments and
  refused every call (`--environment` has no default) — args were serialized
  into `REMOTE_ARGS` and never consumed. Shipped 2026-08-14 in `30b78ae1`; it
  was the one ops.sh verb without a payload test. Fixed, with the test every
  neighbouring verb has. Running the script on the host directly always
  worked, and it fails safe — nothing half-executed.

  **A mis-ordered polars window leaked funding across symbols.**
  `rolling_sum(h).over("symbol").shift(-h)` shifts the materialized column,
  handing each symbol's last `h` rows the NEXT symbol's forward funding sums.
  Four columns across `financed_longs.py` (`settlement_exact_funding`,
  `paid_by`, `paid_bn`) and `lane2_blend.py` — the correct idiom sat on the
  adjacent `ret_by` line. Research frames were unaffected today because the
  `contiguous`/`is_finite` filters drop exactly the contaminated rows (masked,
  not absent); the live frame `prepare_decision` carries the contamination on
  the decision bar itself, unread so far. Fixed with two-symbol tail tests.

  **Smaller confirmed defects, each with a test proven to fail without the
  fix:** the daily panel minted off-grid day keys for symbols missing their
  00:00 bar (listing days), making singleton cross-sections for `over(ts_ms)`
  ranks — now floored like the funding/OI/premium aggregators beside it;
  Bybit backwards pagination aborted a whole threaded backfill when a
  symbol's history ended exactly on a page boundary (P≈1/200 per mid-window
  listing) — an empty page after a full one is now re-asked twice before
  being believed as end-of-history; `engine_held_symbols` crashed a producer
  pass when the heartbeat was atomically replaced mid-read
  (`read_stable_file`'s RuntimeError escaped the `(OSError, ValueError)`
  catch); the strategy host's WS thread inserted into `_price_wake_fired`
  in place while the cycle thread iterates it without a lock — now rebinds a
  fresh dict; the engine risk kernel leaked one `pending` reservation per
  fully-filled order (scanned per assessment, grown per fill, and a lingering
  entry on a symbol that later lost its price would fail-close entries);
  the engine's markout anchored `mid_after` at the FILL, so a book that spoke
  once just after the fill and went quiet recorded that early mid as the
  1m/5m markout — now anchored at the horizon it measures.

  **Deleted:** `tests/repo/test_project_skill_mirrors.py` compared a
  symlinked tree to itself (`.claude/skills -> ../.codex/skills`, same
  inodes) — the one failure it named could never fire; the real check lives
  in `repo_doctor.skill_mirror_report` + `test_dev_tooling.py`. The
  watchdog's duplicate `VENUE_SNAPSHOT_AGE_FLOOR_MINUTES` (defined at :558
  and again at :1302 — defaults bound the first, clamps read the second) is
  one definition now.

  **Prose caught up with 2026-08-14:** STATE.md lost its pre-pivot engine
  bullet ("trades a demo account of its own… no unit is deployed") and its
  "loss halt fires on realised loss only" claim (the guard trips on the
  equity reading — paper loss counts; `loss_guard.rs`); topology now names
  the two engines; README.md front page no longer says "not deployed and not
  trading" / "demo only by construction"; operations.md §Flatten now
  describes the zero-book mechanism and the real exit codes, and stops
  attributing `run_safety_flat_once` to `loss_guard.rs`;
  `engine-risk/PORT_NOTES.md` §Order of evaluation now matches the kernel
  (exits classify before staleness and the trip, ruled 2026-08-14);
  docs/engine.md names the real fence test (`venue_fence.rs`) and the
  horizon-anchored markout rule; the scripts/tests/package READMEs lost a
  deleted owner entry point, a wrong test filename, stale policy/runtime
  rows, and counts that no longer counted.

  **The four findings first reported as owner decisions were then fixed the
  same day, on the owner's instruction ("fix these fully"):**

  **The engine's restart seam, whole.** Symbol ids are interning positions,
  and boot never re-read the log's own `Names` record — so after a restart,
  every join against replayed records (whose fills are whose, what exposure
  the log accounts for, which symbol an in-flight order is in) named the OLD
  run's numbers against a NEW table. Now the table STARTS from the log:
  `assembly::symbol_order` puts the previous run's id order first and the
  config's subscriptions on top, boot interns the same sequence, and the
  market feed opens with a quote subscription for every carried-over symbol
  — so a book-admitted name keeps its id, and a position in it stays visible
  to reconciliation and the stop discipline (the runner now claims and
  replays the log before anything is given a symbol table). An in-flight
  order the venue's own working-order listing lacks is reaped at boot — its
  ending written to the log, its partition claim released, its symbol's
  one-order gate freed — instead of surviving as a ghost on every future
  boot. And the follower's sent-ahead cover stops believing in exposure that
  cannot arrive: the engine tells a strategy when its intent dies inside the
  engine (a new `IntentRefused` event, sent on every refusal path), terminal
  order news shrinks cover by exactly the unfilled remainder (via the new
  `order_facts` ledger lookup), an ack clamps cover down to the quantized
  size actually sent, and a partial view catch-up now shrinks the record
  instead of dropping it whole (the old exact-match drop was the measured
  double-entry window, half-fixed). A refused EXIT drops every cover for the
  symbol: the reading is the fact, and anything less re-plans the same
  doomed exit on every quote. Eight new tests across engine-core and the
  follower; each proven to fail on the pre-fix code (the refused-exit pin
  passes under full revert — the stuck entry and exit covers cancel
  numerically there — and guards partial regressions instead).

  **Shadow mode stops poisoning its own verdicts.** A shadow order can never
  fill or be cancelled, so its kernel reservation could never be released;
  a long shadow run's verdicts leaned on phantom pending exposure — and the
  funded engine's evidence run is exactly that. A shadow order is now judged
  by the same kernel and then not reserved. Book-flip exits still read as
  denials against the real (empty) account; docs/engine.md now says so.

  **The deploy flat gate reads evidence that exists.**
  `check_deploy_rollout_readiness.py` read the deleted owner's journal and
  health file, so strict (`--require-flat`/armed) rollouts could never pass
  their running-phase proofs. It now proves flat from the venue directly in
  every phase, plus — for `exact`/`allow_behind`, when the fleet is running —
  the engine's own heartbeat: recent, demo-realm, and naming an empty
  holdings list. The stopped phases (`none`/`stopped-maintenance`) skip the
  heartbeat, because a stopped engine's file is legitimately frozen. The
  rollout ordering already agrees: the engines stop in the owners phase,
  after the `exact` proof. `--account-root` is accepted and unused, so the
  deploy script needed no change. Tests rewritten around the new evidence.

  **Carry stopped publishing into the inbox nothing reads.** LONG was gated
  when the books shipped; carry still queued exit-first requests every cycle
  for an owner deleted four days ago. With `CARRY_ENGINE_TARGET_BOOK_PATH`
  set (every deployed unit), the cycle now writes its book and publishes
  nothing; the plan and suppression accounting still run, so the cycle
  receipt records what was decided. The stale "engine trades nothing yet"
  comment went with it. Existing carry tests cover the cycle; the gate
  mirrors LONG's shipped pattern and carries no dedicated new test — the
  seam is a five-line conditional at the single publish site.

- **2026-08-17 — 273 Telegram alerts a day, none of them a live fault.**
  Reported by the owner as "a lot of critical errors". Four causes, and the
  fleet was healthy throughout: `verify-ok`, every unit active, producers
  `err=none`, engine trading demo with a fresh account view.

  **180/day — the watchdog was timing the engine against its own start.**
  `engine_heartbeat_stale` fired and cleared ninety times a day saying the
  heartbeat was "dated 1s in the future". Nothing was wrong with any clock: the
  box is NTP-synced and the engine stamps a plain `SystemTime::now()`. The
  watchdog read its clock once at the top of a run, then spent a second or two
  on datasets and `systemctl` before opening a file the engine rewrites every
  five seconds, so anything written in between came out negative. The identical
  trap had already been found and tested for the carry check; the engine check,
  added later, was handed `main`'s opening reading. It now reads the file and
  then asks the clock, in that order. Fixed in `e547041c`, deployed the same
  evening.

  **70/day — two checks reading a component deleted on 2026-08-14.**
  `/opt/liquidity-migration/data/bybit-account-execution/` stopped being written
  at 19:58 that day, when the Python order path went. `account_health_stale` and
  `account_digest_stale` went on ageing the frozen files and reporting a dead
  component's last words as illness, 70 times a day, never able to clear. The
  call site carried a comment claiming the engine kept feeding the journal
  "through the same kernel" — it does not; no engine crate names the journal,
  verified by grep over every crate. Account freshness is now taken from the
  engine's own heartbeat (`engine_account_view_stale`), which has a live writer,
  and the digest check defaults to unprovisioned.

  **23/day — a warning whose consequence no longer existed.** `demo_rules_age`
  said an expired receipt meant "the next authorized runtime start will fail
  closed". True while the Python owner loaded the receipt at startup; false
  since. `run_authorized_runtime.sh` has no rule gate, neither producer script
  mentions one, and the engine parses instrument rules straight off the venue.
  Now a WARNING naming the deploy flag that refreshes it. Mainnet's receipt does
  gate the funded owner and is untouched.

  The lasting finding is the gap this hid: demo has had **no working
  account-health alerting since 2026-08-14**, and "the exchange and our records
  disagree" (`account_health_unhealthy`) still has no writer — the engine
  reconciles but publishes no mismatch. `gather_account_health_alerts()` is kept
  uncalled as the specification for whatever writes that evidence next. A
  channel that is entirely false positives is worse than a quiet one, because
  the real alert lands in a habit of ignoring it.

  One self-inflicted bug caught in review before it shipped: the first cut of
  the retarget passed `--max-account-health-age-min` (default 1 minute) straight
  into the new check, where the old code had applied `max(dial, 25 min)`. That
  silently tightened the bound 25× against a reading that refreshes every few
  seconds, and would have reintroduced exactly the flapping being removed. The
  floor now lives inside the check where no caller can bypass it.

  Deployed `e547041c` with `--stop-first`, which is required while real money is
  armed — the owner's call, asked and given. Both engines rebuilt.

- **2026-08-14 — A deploy left the funded engine on the previous binary.**
  Found by deploying the entry below and reading both heartbeats. The two
  engines share one binary; the activate phase starts the funded one and the
  engine-build phase that compiles the binary runs after it, restarting only
  the demo unit. So every deploy since the funded engine was installed gave
  the demo engine the new code and silently left the funded one where it was.
  Both units read `active`, both heartbeats looked healthy, `staged-ok`
  printed.

  It surfaced only because the entry below changed a field both engines
  publish: the demo engine named its sleeves `carry, long` and the funded one
  still said `target_book` twice, from identical config. Harmless so far only
  because the funded engine is in shadow.

  Fixed in `deploy_vps_live.sh` and proved by redeploying:
  `mainnet-engine-ok` now prints beside `engine-ok`, and the funded heartbeat
  reads `realm=mainnet mode=shadow strategies=[carry, long]`. Deployed
  `f89c0583`; 9 units active, none failed, zero error lines, producers
  `err=none owner=healthy`.

- **2026-08-14 — The engine says what its fills cost.** It measured our own
  side of the wire to the nanosecond and could say nothing about the price it
  got. Two facts were on the wire and thrown away: Bybit sends `is_maker` on
  every execution and the parser dropped it, and the midpoint an order was
  decided against lived only in memory, for worked limit orders, and was
  discarded when the order was reaped — so no finished log could be asked what
  its trading cost.

  Both are kept now. `is_maker` on the fill; `M0` on the `OrderSent` record,
  written at the send because that is the only moment it exists. Everything
  except the markout is then arithmetic over records the log already holds, so
  the live summary and the report read off a finished log are the same code.
  The markout is written when its horizon comes due, on the group-flush tick,
  after waiting five seconds for a readable book — then recorded as terminally
  missing, never as a zero. Names and signs are `docs/architecture.md` §Trade
  diagnostics unchanged, so the two halves of the repository stay comparable.
  **`M0` is the top of book**: nothing here measures impact.

  This also replaces a producer the fleet lost without noticing. The bridge
  that registered fills for marking and the loop that drained it went with the
  Python order path, so `market_capture.register_post_fill_markouts` has had
  no production caller since — the readers survived the writer. The engine is
  the new writer, for its own fills.

  Read it with `engine fills --wal PATH`, per sleeve and symbol, with a footer
  that says what share of the traded notional could be measured at all. Five
  of the numbers are in the heartbeat.

  Two things had to become readable first. The log recorded **strategy and
  symbol ids**, which are positions, and nothing that said what they meant —
  `engine replay` could say an order was strategy 1's for symbol 4 and no
  more. The engine now writes both tables, at boot and again whenever a book
  names a new symbol.

  And a strategy could not read **its own** position. `StrategyCtx::position`
  is the venue's account reading: seconds old, and on a two-sleeve account the
  sum of both. `my_position` is that strategy's own fills, moving the instant
  one arrives. The quoter is the plug that needed it, and took three other
  things with it — many symbols instead of one instance per coin, a quote
  centre that leans against inventory (`skew_bps`, optional, absent means the
  strategy exactly as it was), and a re-quote on its own fill rather than on
  the next price. A name another sleeve holds now has our quotes pulled: there
  is one venue stop per position.

  677 engine tests. Proved both ways round — the write test fails when the
  anchor is stubbed to zero, the read test fails when the lookup is stubbed
  out.

- **2026-08-14 — The Python order path is deleted, and the engine is the
  account owner.** Owner-directed. Ten modules reachable only from the account
  owner, its two systemd units, its two launcher scripts, the four dispatcher
  arms, and thirty-one test files: about 25,000 lines. Among them the three
  capital-preservation modules `AGENTS.md` had named as off-limits —
  `account_loss_guard.py`, `equity_anchored_envelope.py`, `venue_protection.py`
  — removed on the owner's explicit instruction after the engine carried all
  three with parity tests written against those very files. That rule in
  `AGENTS.md` now names the Rust originals; the controls did not go, they
  moved. Nothing is deployed: the host still runs `2bd3a00` and keeps trading
  until somebody deploys.

  *The producers could not have traded without a second change, and it is the
  substance of this entry.* They do not merely tolerate an account owner —
  they **size from it**. Every cycle read `equity_usdt` out of the owner's
  `account_owner_health.json` and planned every entry as blocked when it was
  missing or stale (`strategy_planning.account_owner_equity_or_error` returns
  `(0.0, error)` and the caller blocks). Deleting the owner with nothing else
  changed would have left a fleet that ran, published exits, and never opened
  another position — quietly, with no error anywhere. So the engine now
  publishes what the producers need in its heartbeat:
  `account_equity_usdt`, `account_available_usdt`, and
  `account_observed_ns`, read through the new
  [`engine_account_health.py`](liquidity_migration/account/engine_account_health.py).

  Three details of that seam are deliberate. **The timestamp is the venue's
  reading time, not the file's write time** — an engine whose loop keeps
  beating while its venue reads fail ages out on exactly the number that
  matters, which is the case the check exists for. **An engine that has not
  read the venue yet writes null, not zero**, so a producer can tell "no
  reading" from "no money". **The account id is compared**, and a mismatch
  blocks entries with both values in the message, because the route's id and
  the id the engine authenticated as could not be proved equal offline; if
  they ever disagree it says so in one line instead of going quiet.

  Two capabilities did not survive, and neither is a test problem. **Flatten
  is gone**: it market-closed a book by publishing zero targets into the
  owner's intent inbox, and the engine reads target *book files* and has no
  inbox, so nothing drains them. `ops.sh flatten` now refuses with that
  explanation and the Telegram close button is removed rather than left as a
  button that cannot work — closing a book is a manual job at the venue until
  the engine grows a path of its own. **The hourly digest is gone** with the
  owner that rendered it; `docs/notifications.md` keeps its shape as the
  specification for whatever replaces it.

  The rest is plumbing that hung off the owner, repointed: the producers no
  longer order themselves after it (`Wants=`/`Requires=` removed — they write
  a book on disk and the engine reads it), the engine units take the owner's
  place in `ROLLOUT_OWNER_UNITS` so they stop last and start first,
  `lib_sleeves.sh` requires the engine unit in the manifest instead of the
  owner, and the watchdog and Telegram controls watch the engine units. The
  watchdog's owner-artifact checks — market-readiness sidecar, owner-health
  file, notification state — were removed rather than repointed, because their
  writer is gone; its unit-liveness and account-journal checks stay.
  `verify_topology()` no longer demands the demo owner, which was the trap that
  would have broken every deploy after this one.

  `scripts/dev.sh check` green: 2444 Python tests, 600 Rust tests, ruff and
  mypy clean. Each new check landed with a failing-first proof — removing the
  venue-reading staleness check or the account-id comparison turns exactly its
  own test red.

- **2026-08-14 — The engine can be pointed at the funded account, reads the
  fleet's own risk limits, tells each sleeve's book apart, and states leverage
  at the venue.** Six commits, `80d31eb8`, `68576db1`, `5283f978`, `39b110fa`,
  `0f1f1190`, `ea9bb5a0`. Nothing is deployed, nothing is installed, and
  nothing has been sent to the funded account.

  *The fence changed shape rather than coming down.* The venue crate used to
  contain no mainnet hostname, and a test read the source back to keep it that
  way. That made a class of mistake impossible and also made the engine unable
  to do the job it was built for. What replaced it is the Python fleet's own
  rule, ported so both halves behave identically while both run: `REAL_MONEY`
  in the host credential file, set by the owner, or a mainnet gateway fails at
  the credential read; an armed host refuses the *demo* realm in turn, so a box
  cannot be half-live; the two realms read disjoint credential variables; a
  realm is always named, never defaulted; and a typo in the switch stops the
  engine rather than reading as "off". The structural half now checks something
  sharper than absence — the four venue hosts may be written in exactly one
  file, which is what stops a test or a benchmark handing the test-only
  constructor a real address.

  *One document, not two.* `configs/operational.mainnet.json` says how much of
  the funded account may be at risk. The engine did not read it; the same
  numbers were transcribed by hand into a test. `[risk]
  operational_profile_path` now loads the real file, so a cap the owner
  tightens tightens for both halves. Measured, not assumed: the Python and Rust
  loaders read that file identically, field for field, including the two sleeve
  margin shares that carry repeating decimals and sum to the account margin cap
  to the last place. Proved by tightening the symbol cap from 50 to 49 in the
  real profile — two Python tests and one Rust test failed, each naming the
  field.

  *A book went to every strategy.* `TargetBook.source` said in so many words
  that it was "for the log, not for routing". Fine with one sleeve; with two —
  which is what the funded account runs — each follower would take the other's
  book as instructions, and since the venue holds one position per symbol and
  no note of who asked for it, they would have fought over the same positions.
  Books are now routed by configuration: each strategy names its own
  `book_path`. Proved by restoring the broadcast and watching the carry sleeve
  hear `["carry x1", "long x2"]`.

  *Leverage was parsed and thrown away.* The target book carries one per name;
  the follower read it and dropped it, and there was no `set_leverage` anywhere
  in the Rust order path. Margin posted is notional divided by the symbol's
  leverage, and the symbol keeps whatever the last person to touch it chose —
  the owner trades that account by hand. The leverage now travels with the
  decision and the engine makes the venue agree before the order goes, failing
  the order closed if it cannot. Two details carried over because both are
  load-bearing: retCode 110043 ("not modified") is success, and the remembered
  leverage is forgotten the moment a reading shows the symbol flat.

  *And the last load-time proof.* `PORT_NOTES` had recorded one Python check
  the Rust kernel did not run. Harmless while the caps were hand-written;
  not harmless once the engine loads the fleet's own profile, because then a
  profile Python refuses would have been accepted here. Porting it turned up a
  rounding fault: the engine holds the account gross cap as a multiple and the
  profile states it as money, and rebuilding one from the other lands a
  fraction of a cent off for most numbers — an exact comparison would have
  refused a profile for agreeing with itself. The two shipped files never
  showed it because 1.75 and 2.0 are exact in binary.

  *What this does and does not mean.* Every capability above is built, fenced
  and tested; none has been exercised against real money. One real gap remains:
  a follower's symbol universe is fixed when it boots, so it cannot trade a
  name that a later book first mentions — and the fleet's universe moves with
  listings and delistings. Past that, what is missing is evidence rather than
  capability. 598 Rust tests, 3002 Python tests.

- **2026-08-14 — The engine stops being looser than the fleet, becomes visible
  from outside, and gets a unit to run under.** Four commits, `f2317a14`,
  `344e9914`, `465e333d`, `e47c917a`. Nothing is deployed and nothing is
  installed; the host still runs `2bd3a009`.

  *Not looser any more.* `engine-risk/PORT_NOTES.md` had carried this sentence
  since the port: "The engine is looser than the Python fleet by exactly these
  four caps." That was the last measurable reason the Rust kernel was not a
  replacement for the Python one, and it had nothing to do with which host it
  talks to. Ported: the per-symbol gross ceiling (across the whole projected
  book, stricter than Python, which nets producers' rows per symbol first), the
  second gross ceiling, account-level initial margin, and the available-margin
  test. One of the four was deliberately **not** written and the notes now say
  why instead of leaving a hole: a separate account-gross gate is unreachable,
  because the loader proves component ≤ account and the envelope refuses any
  book over the allowance first, so a test for it could only pass vacuously.

  That work turned up a real fault. The partition's load-time proof compared
  its margin shares against gross ÷ leverage — a stand-in for a cap that did
  not exist. The funded account's shares sum to exactly the declared 100.0 and
  the old bound was 87.5, so **the engine could not have accepted the shipped
  mainnet partition at all.**

  *Visible from outside.* The last row the engine's capability table marked
  Absent was "nothing outside the process can tell whether the engine is
  healthy", and it is the row that gates anything standing down. The engine
  now writes a heartbeat file — atomically, from the group-flush tick, never on
  the order path, and a failure to write it can never stop the loop. The field
  that matters is `may_open`: an engine that is alive, writing healthy
  heartbeats, and quietly refusing to open anything looks perfectly well from
  outside. The judging stays in the fleet's existing watchdog rather than a
  second notification stack; `check_fleet_liveness.py` pages on stale,
  unreadable, or latched, and is off unless a path is configured.

  The two halves were built separately and **did not meet** — only three of
  eight field names agreed. The reader caught it and refused to bend to what it
  found, which is the right instinct: a reader that silently adapts to whatever
  the writer emits is not a contract. Settled deliberately (`wall_ts_ms`
  because the log already spells a wall stamp that way; `mode` as a word rather
  than a boolean because a boolean cannot grow a third state; account and lease
  optional because a shadow run legitimately has neither), then checked end to
  end against a document the engine actually wrote.

  *A unit to run under.* The engine had none and had only ever been started by
  hand. It now has one, running its **own** demo account (579580669), shadow
  unless the host's environment file says otherwise, with `REAL_MONEY` unset in
  the unit whatever the environment holds. Everything about it in the deploy is
  conditional on the unit fragment, the binary, and an operator-placed
  environment file all being present, because `verify_topology` runs on every
  mainnet deploy and a unit demanded unconditionally would fail every deploy
  and every status read on the funded fleet.

  The build cannot break a deploy: every path returns 0 and prints why, it runs
  last in both staged and rollout, and in rollout deliberately after the two
  lines that disarm the rollback trap. Test-enforced — making it a strict phase
  turns the rollout flatness test red.

  Two hazards found and closed on the way. A relative log path resolved inside
  the deployed checkout would write an untracked file into the tree the deploy
  proves clean at every exact-commit step, so the unit gets its own state
  directory. And a *running* engine left out of the rollout stop list would
  deadlock every deploy, funded fleet included, because `require_quiescent`
  refuses to install while any `liquidity-migration-*` unit runs while
  stop-first only stops what those lists name.

  A third was found by reading the result back: as first built, an installed
  engine that would not start was a verification mismatch, and a mismatch
  inside a rollout reaches the cleanup that stops the fleet — so a bad
  `engine.toml` or an expired demo credential could have taken the funded
  account down. Its row is now **reported, never fatal**. Every other unit in
  that table carries orders; the engine carries none.

  *And the question that keeps being asked.* Whether the Python order path can
  be deleted was measured rather than argued: walking the import graph from all
  nine units, **93 of 135 modules are reachable from a live unit and not one is
  demo-only** — demo and mainnet run byte-identical command lines, the realm is
  a parameter branched inside shared modules. A symbol-level scan across the 45
  order-path modules found zero unreferenced functions or classes. The host's
  installed units already match `deploy/systemd/` exactly. There is nothing in
  it to delete. `docs/engine.md` now carries the order in which it could
  actually happen, and names the one step that is the owner's.

- **2026-08-14 — The engine holds an account, works an entry, and checks
  itself against the venue; and a dead sleeve stops deciding what the live
  ones may trade.** Two commits, `4a8f8301` and `a8388b27`.

  *The dead sleeve's grip.* The frozen candidate universe kept the venue's
  whole tradable instrument set under the retired CONTINUOUS sleeve's name,
  and every producer read the union of the three profiles — so the funded
  account's tradable population was a deleted sleeve's config. Measured on
  both live artifacts, not inferred: `continuous` is 510 symbols on demo and
  512 on mainnet, and in each case that **is** the entire symbol list; the
  union added nothing. Schema 5 renames it `strategy_instruments` — a venue
  fact no sleeve owns — and keeps profiles for live sleeves only. CARRY now
  binds to its own profile and reads the instrument set, so its population
  does not move. Narrowing CARRY to its own 150 names would cut 362 symbols
  and is a strategy change; it has **not** been made, and is an open question
  for the owner. `scripts/maintain/migrate_candidate_universe_schema.py`
  converts an old artifact offline at the same snapshot, refuses if one symbol
  would change, and re-keys LONG's retirement registry so the three recorded
  delistings keep their first-observed anchors. Run against the real funded
  artifact: 512 in, the same 512 out, frozen epoch preserved, CARRY's binding
  passes.

  *And its claim on capital.* The retired sleeve still held a share of the
  funded envelope. Removed: the account gross cap moves 176.77 → 175.0 (−1%)
  and the margin shares it was holding go back to the two live sleeves in
  proportion (carry 56.57 → 57.14, long 42.43 → 42.86). The shares that
  actually bind — carry 100.0 and long 75.0 gross — are unchanged, and the
  margin total still sums to the 100.0 account ceiling. The combined dial
  ceiling widens 9.9 → 10.0. **Deploy note: the new schema refuses a profile
  that still carries a `continuous` block.** The deploy re-renders the
  installed profile from the dials, so code and profile move together, but a
  code-only push leaves the funded host holding a profile the new code will
  not load.

  *A live fault found on the way.* When the venue **moved** a delisting date,
  the reconciler wrote `evidence_source="live_instrument_delivery_time_updated"`
  and the reader accepted only `"live_instrument_delivery_time"` — so the cycle
  after a move raised `candidate-retirement registry record is invalid`, for
  good, on a path LONG runs every cycle with three real retirements standing on
  the funded box. Fixed; the test now runs the third cycle, which is where it
  bit.

  *The engine's three gaps, closed and proved live.* The single-writer lease
  is not a new invention: the fleet already had one, and the engine joins it —
  same `flock`, same path named by the venue's own account number, same
  re-proof after the lock that the file locked is still the file at the path.
  Entries can now rest at the touch and be worked in rather than crossing, the
  whole recipe ported from the fleet's quote manager. Boot compares the log
  against the venue's working orders and the account, repairs a missing stop
  from the level the log says it was opened behind, and latches out of opening
  on exposure it cannot account for — durably, in the log.

  Proved against demo account **579580669**, which the fleet does not own and
  whose lease nothing held (funded from Bybit's demo faucet for the test):
  a live engine took the lease and a second engine was refused, naming the
  holder by pid; a market entry was rewritten into a resting limit and filled
  299 BEATUSDT at 0.666 with its stop attached at 0.434; a restart on the same
  log found nothing wrong; the same position under a fresh log reported `the
  venue holds 299 and this log accounts for 0` and refused to open; an empty
  book flattened it. The fleet's own two leases were held throughout and
  nothing it does was touched.

  *Modularity.* A config where two strategies claim one symbol is now refused
  at boot — the venue holds one position per symbol and cannot say whose it is,
  so each would read the other's fills as its own. And there is a strategy
  template: a working, tested plug whose module doc is the authoring guide,
  compiled with the crate so it cannot rot and left out of the registry so no
  config can run it by accident.

  *What is still true.* The engine reaches demo hosts only, by construction and
  by a test that reads the crate back. It cannot trade the funded account, so
  the Python order path cannot be deleted while it is the only thing that can.

- **2026-08-14 — The retired CONTINUOUS sleeve's code leaves the tree
  (~14,600 lines), and nothing the live fleet does changes.** This supersedes
  the "kept deliberately" note in the 2026-08-14 ~00:40 entry below: the three
  live imports named there were unwired first, each in the way that keeps the
  running behaviour identical. Deleted: the five `strategy/continuous_*`
  modules, the five `research/backtest/continuous_*` modules, the two
  continuous research runners, the `continuous-event-demo-cycle` subcommand,
  and 25 files of tests. Not deployed; no unit runs any of it, and the fleet
  is untouched. **Three things the sleeve left behind are data, not code, and
  they stay.** (1) The token continuous envelope in both operational profiles
  — the JSON is byte-identical, and the sizing shape the deleted profile
  resolved to (one component at weight 1.0, inverse-vol clamp 2.0) is now two
  named constants in `operational_profile.py`. Every capital number the
  envelope proof computes was captured before the change and re-checked after:
  identical on both profiles. (2) The `continuous` profile in the frozen
  candidate universe. This one nearly bit: that profile is the *unbounded*
  member (no rank, symbol, turnover or age floor), the tradable population is
  the union of all three, and CARRY intersects its universe against that
  union — so dropping it would have quietly narrowed what CARRY may trade.
  Worse, `load_candidate_universe` rebuilds and re-hashes all three profiles
  and refuses an artifact whose profile set differs, so the running fleet
  would have rejected its own installed universe. Its inputs are now frozen
  literals; all three profile hashes verified unchanged. (3)
  `btc_risk_decision_evidence`, moved to `account/entry_attempts.py` beside
  the other journal-metadata keys, so an entry's evidence still copies
  forward onto its close. **The account owner** no longer reads a continuous
  cycle status. Its unit never set `CONTINUOUS_CYCLE_ROOT`, so the digest
  already rendered no line; the reader, the two flags and the launcher's
  argument block went together, which is what keeps a host env file from
  passing a flag the owner no longer accepts. `scripts/dev.sh check` green:
  3200 → 2966 tests, the whole 234-test drop being tests of deleted code.
  **Three honest consequences.** An open item in the research queue named one
  of the deleted scripts; rather than quietly break it, the item now says it
  is blocked by this deletion, that git history holds the tooling, and that
  retiring a research question is an owner decision, not a cleanup's.
  `docs/research/research_findings.md` had two markdown links to deleted
  modules — now plain names with the same pointer, no claim touched.
  And one behaviour did change: the residual-momentum refresh in
  `research_refresh.py` was gated on `"continuous" in sleeves` and is now
  gated on `--skip-features` alone, so under the shipped default it runs as
  before, but `--sleeves long` now runs it where it did not.
  **Coverage genuinely lost, not papered over**: `_build_path_labels` and
  `_bars_by_symbol` in the candidate tape have no test now. Replacing them
  needs a real fixture, and a silently-empty one would be worse than none.

- **2026-08-14 ~02:20 UTC — The engine becomes plug-and-play on both axes:
  a venue chosen by name, and a market maker beside the book follower.** Not
  deployed; the fleet is untouched. **Venues**: `Venue` is a closed enum in
  the venue crate with `KNOWN_VENUES` and a by-name constructor, so adding
  Hyperliquid or MEXC is a module, a variant and a name — the engine's wiring
  does not move. Dispatch is an enum rather than a trait object because
  `VenueGateway` uses `async fn` in trait and cannot be made into one at all,
  and because a closed set keeps every venue visible in one place. **That
  last property is the safety fence**, which venue selection is exactly the
  mechanism to undermine: a name selects an adapter already compiled in and
  can never introduce an endpoint, the source scanner covers every module the
  crate declares (a new test fails if one escapes it), and a check walks
  `KNOWN_VENUES` for anything that smells of real money. Verified by planting
  `api.bybit.com` in the new registry: two fence tests failed by name.
  **Strategies**: a `quoter` plug — the in-the-loop kind — quoting both sides
  around mid, post-only, each quote carrying a stop because the kernel
  refuses an opening order without one. It moves a quote rather than
  replacing it (a replacement gives up the queue position it earned), leaves
  a nearly-right quote alone, stops quoting the side that would push
  inventory past its ceiling, and pulls both sides when the book is empty or
  crossed. No adverse-selection model, no queue estimate, and nothing has
  graded whether it makes money — it is a working maker on the engine's
  contracts, not a strategy. 372 Rust tests, full Python gate green.

- **2026-08-14 01:56 UTC — The engine placed its first live orders, a full
  round trip through the carry plug on the DEMO account — and blocked the
  Python owner for about a hundred seconds doing it.** The round trip: a book
  saying hold 70 USDT of BTCUSDT went in, the follower entered **Buy 0.001 at
  market**, and Bybit accepted it (`4a9dcdc5-03b7-4861-9ab4-adae7eedc11c`,
  835 ms from process start). An empty book then went in, the follower exited
  **Sell 0.001 reduce-only**, accepted as
  `8c18803e-6f6c-46fb-8d0c-b7a5cdb49b24` (173 ms from start), and a read-only
  probe afterwards found nothing left to close. Research decided, the engine
  traded, the account came back flat.
  **The cost, and it is the finding that matters.** While that 0.001 position
  was open the demo account owner logged, every pass:
  `account reconcile blocked new intents: native protection reconciliation
  failed: BTCUSDT reconstructed flat contradicts authenticated venue size
  0.001`. The fleet's separate-books policy tolerates a foreign *position* for
  trading, but native-protection reconciliation is stricter — it requires the
  venue's size and the owner's own reconstruction to agree per symbol, and a
  position the owner did not place can never agree. The owner refused new
  intents until the engine closed it, then recovered on its own with no
  intervention; the fleet took no trades in that window and nothing was lost.
  **This is the wedge, demonstrated cheaply.** It is why the single-writer
  lease is a hard blocker rather than tidiness, and it settles a question that
  was open: the engine and the Python fleet **cannot share an account**, even
  for a minute, even on demo. Either the engine gets a lease that stops both
  running, or it gets an account of its own. Until one of those exists, the
  engine runs against this account in shadow only.

- **2026-08-14 01:42 UTC — The target-book seam runs end to end on the
  production box.** A book rendered by the Python side
  (`carry_hold_v4_live_v1`, one 120 USDT BTCUSDT target, 0.35 stop) was
  written to the host; the engine logged `watching for a target book`, read
  it, delivered it, and the follower planned `Buy 0.001 BTCUSDT [book-enter]`
  — risk allowed it, the record went durable, and shadow declined the send.
  Research decides, the engine executes, and nothing reached the venue.
  The follower re-plans on every quote and suppresses a repeat by looking for
  its own working order; a shadow order is marked never-sent immediately, so
  the suppression could not see it and the entry was re-emitted until **the
  envelope stopped it** (worst case 1000.40 against a 993.09 allowance) — the
  backstop firing on a runaway without being asked to.
  **Correction, same day**: an earlier revision of this entry called that
  shadow-only and said live was covered because the order stays in flight.
  That is wrong, and the reviewing agent caught it. A fully filled order is
  ended the moment the fill lands (`inflight.rs` `apply_update`), so it leaves
  `ctx.resting()` at once, while the account reading only refreshes every
  `account_view_max_age_ms / 2` — 2.5 s at the shipped 5000. For that window a
  live run sees **neither a resting order nor a position**, and the follower
  would enter a second time; only the envelope and partition bound it. The
  **Fixed the same night**, by the second of the two candidates: the plug now
  remembers what it sent against the reading it was decided from, and folds
  that into the position the planner sees. The record is dropped when the
  reading moves off the value it was sent against — no timer and no guess —
  and the fill path pays no venue round trip. Proven by reverting it: the test
  fails with "nothing resting and a stale reading must not become a second
  entry". The test that shipped alongside the fault had pinned the wrong
  behaviour ("with nothing resting the plug does try again"), and now pins the
  right one.

- **2026-08-14 ~02:00 UTC — The engine learns to cancel and amend, venues
  learn to say what they can do, and carry learns to hand over its book.**
  Not deployed; the fleet is untouched and still runs `2bd3a00`. Three
  pieces. (1) **A strategy could only place**, which is no vocabulary for a
  market maker: an action is now place, cancel or amend, and a strategy can
  read its own working orders (`ctx.resting`) because the engine mints the
  ids. Under a flood, cancels and exits keep flowing while entries and
  amends are dropped. **An amend that raises size adds exposure, so it is
  made durable before the wire exactly as a send is**; repricing and
  shrinking are not, and ride the group flush — a distinction the reviewing
  agent raised and left to a decision, with a test that fails without it.
  (2) **Venues now state their capabilities** — native position stop, amend
  in place, post-only, batching — and the engine refuses what a venue cannot
  honour rather than substituting cancel-and-replace, which is a different
  trade at a different queue position. That is what makes a second venue
  (Hyperliquid, MEXC) an adapter rather than a rewrite; `docs/engine.md`
  §Adding a venue says what one has to implement. (3) **The second plug
  seam**: carry's decision reads ninety days of settled funding and hourly
  bars and runs a state machine over all of it, so it stays in Python and
  hands the engine a *target book* — absolute notional per symbol, the stop
  each carries, how long it may be acted on. Carry writes one when
  `CARRY_ENGINE_TARGET_BOOK_PATH` is set (off by default, and wrapped so a
  failed write can never raise into the sleeve that is trading). The
  follower's planner is pure and tested: exits before entries, the
  max(1 USDT, 5%) resize dead band, the entry cutoff, and a resize that adds
  to a position re-declaring its stop **anchored on the entry price, not
  today's** — anchoring on today's price loosens the stop on size already
  held. **No book means no decision and the position is held; an empty book
  means hold nothing and is acted on** — confusing those two flattens a live
  book on a data outage. Also: a systemd helper that two producers imported
  from the account owner moved to `core`, removing a decision-side
  dependency on the execution path. `docs/engine.md` now carries a table of
  what the engine still cannot do — resting entry quoting, venue
  reconciliation and restart recovery, the single-writer lease, and any
  watchdog — because the order in which the Python execution path can be
  deleted depends on that list being honest.

- **2026-08-14 ~01:05 UTC — The engine runs on the production box, and its
  signing is venue-proven: first shadow run against the demo account.** Built
  on the VPS in an isolated clone (`/opt/engine-build`, never the deployed
  checkout the fleet runs from): rustup + `cargo build --release` took 4m34s
  on the 2-core box. **Measured there by `engine bench`** (real loop, real
  signing, real fsync in the chain, pretend venue on the box; 4,000 quotes,
  200 orders): decision **448 ns**, durable **2.14 ms**, whole chain
  **2.60 ms median / 5.65 ms p99 / 10.12 ms worst**. Linux `fdatasync` beats
  macOS's full flush exactly as wave 3 predicted, so the chain is shorter
  here than on the laptop despite a slower CPU. The comparison that counts:
  the Python fleet's software time on that same box is 25.7 ms median —
  **about ten times longer for the same job**. Then the **first live shadow
  run against the demo account**: the engine connected to the public feed,
  and **the private stream authenticated** — the HMAC signing had only ever
  been proven against test vectors, and Bybit has now accepted it. It read
  real equity (1,413.82 USDT), the touch fired, the risk kernel allowed the
  order, the record was made durable, and the send was declined because
  shadow mode declines it. Nothing reached the venue, so the one-writer rule
  with the Python fleet holds. Two faults the run found and fixed: the engine
  **refused to boot** because the demo account holds HOMEUSDT, a symbol no
  strategy subscribed to — now read as somebody else's position and left
  alone, which is the fleet's own separate-books rule and necessary on an
  account the owner hand-trades (the old behaviour meant any unfamiliar
  holding stopped the engine dead); and that warning repeated on every
  account refresh (~30k lines/day, and log spam filled this box's disk once
  before), now said once per symbol. Two config mistakes of mine were caught
  by the engine's own gates, which is the gates working: a size below the
  venue minimum, and a partition share above the account gross cap.
  `engine/` is still not deployed and trades nothing.

- **2026-08-14 00:20 UTC — First carry boundary on the wave-3 order path, and
  it worked as designed.** Receipts from the demo carry producer's journal:
  `froze_ahead=True` at 00:18:45 (the pre-boundary freeze), then the deadline
  wake at **00:20:00.000 exactly** (cycle id `…1786666800000`, epoch
  milliseconds on the boundary) with `build_skipped=True` — the boundary pass
  published the frozen book instead of rebuilding it — and the day's
  publications grouped in that same pass: 2 exits + 1 entry, book 2→1, gross
  0.200→0.100, `err=none`. Follow-up cycles at 00:20:04 and 00:20:07 clean.
  Zero fleet error-level journal lines since the 23:46 deploy (one unrelated
  sshd scanner line). The freeze-ahead → deadline-wake → publish-frozen chain
  built in wave 3 is now measured live, not just designed.

- **2026-08-14 ~00:40 UTC — Repo cleanup: the engine promoted to first class,
  dead files deleted, docs cut to essential facts (owner directive).** Not
  deployed; the host still runs `2bd3a00` and nothing here changes what the
  fleet does. What happened, in five parts. (1) **Deleted, each verified
  reference-free first**: the standing audit report `docs/audit/…latency-
  architecture-audit.md` (findings live in the topic docs), the retired
  CONTINUOUS sleeve's hedge model and prior (`continuous_hedge_manager.py`,
  `regenerate_hedge_warmstart.py`, `deploy/hedge_warmstart/`, their tests —
  this supersedes the 2026-08-08 "stays for research" note below: the sleeve
  is retired, no forward grade depends on it, git history keeps it), and
  three closed one-shots (`continuous_stop_counterfactual.py`,
  `check_residual_momentum_gate.py`, `migrate_cycle_ledger_buckets.py` + its
  test). (2) **Deleted and then restored on review: the quote-lab package.**
  The 2026-08-08 entry below declined exactly this deletion — the replay is
  the machinery behind the registered entry recipes still accruing forward
  days — and that reasoning still binds, so it stays, and the keep is now
  recorded in STATE.md and docs/architecture.md. (3) **Docs compacted to
  current facts** (history lives here): STATE.md 587→324 lines,
  architecture.md 780→626, trading_logic.md 442→263 (the retired CONTINUOUS
  and Hedge sections are now one short note), operations.md 492→428,
  data.md 384→341, plus README/CLAUDE/scripts/tests/deploy READMEs trued up
  (the tests README claimed a deploy-preflight pytest subset that has not
  existed for weeks). Research evidence docs got a cautious pass only —
  every number, table row and negative result diffed and preserved. Entries
  before 2026-08-01 moved verbatim to
  [docs/archive/CHANGELOG-2026-07.md](docs/archive/CHANGELOG-2026-07.md)
  (byte-fidelity verified by reconstruction; 61 kept + 14 moved = 75). Two
  stale line-number citations in the 2026-08-09 entry were re-pointed to
  section names; entry text is otherwise untouched. (4) **The engine is
  first-class**: README leads with it, `scripts/dev.sh check` now runs the
  engine's Rust tests when a toolchain is present, and CI gained an
  independent `rust` job. (5) **Two live defects fixed, committed but not
  yet deployed**: the Telegram control panel no longer stops/queries the
  retired CONTINUOUS producer unit that no longer exists on the host (a
  pause collected a spurious systemctl failure; test proven failing without
  the fix), and `research-refresh --sleeves carry` no longer asserts the
  retired `lane2_carry_hold_v3` summary against a runner that renders v4 —
  the expectation now derives from the deployed config
  (`CARRY_CONFIG_PATH`). Kept deliberately, with reasons on file: every
  `continuous_*` module wired into live processes (the account owner
  imports its cycle status and BTC-risk key; the operational-profile
  envelope proof imports `continuous_demo`; the token continuous envelope
  in both profiles is test-guarded), all lane-2 configs (test-read,
  forward-scored), and every research script named by an open queue item in
  `docs/research/strategy_program.md`.

- **2026-08-13 23:46 UTC — Wave 3 deployed to the whole fleet: `staged
  --stop-first` from main at `2bd3a00`, all nine units back active+enabled,
  `verify-ok … mainnet=armed`.** Receipt `staged-ok
  commit=2bd3a0090deb2dbf34f04e34df14f320f190744f profile=operational`; the
  carry demo producer logged its clean restart at 23:46:14 UTC and the
  journals show zero error-level lines after. This puts the wave-3 order
  path live for the first time (one durable write per queued request, the
  price-touch wake, the GC discipline, the readiness sync off the order
  path's lock — merged entry below), so the next 00:20 UTC carry boundary
  is the first measured on it. The engine source ships in the checkout but
  the engine itself is not built, not running, and trades nothing — the
  Python fleet owns everything live. (The entry date corrects an earlier
  draft that read 2026-08-14 07:45 UTC for the merge below: the merge
  commit is stamped 2026-08-13 23:39 UTC.)

- **2026-08-13 ~23:39 UTC — The program grows a Rust execution engine
  (`engine/`), merged to main, NOT deployed and NOT trading anywhere.** Owner
  directive: shift the program to a purpose-built execution system (<100 ms
  decision to execution, plug-and-play strategies, research kept separate,
  "lets use rust because we can"; relocation explicitly off the table).
  [docs/engine.md](docs/engine.md) is the architecture. Measured on this
  Mac by the engine's own benchmark (real loop, real HMAC signing, the real
  log's fsync in the chain, pretend venue on the same box): market message →
  decision **83 ns**; decision → durable on disk and out the socket **3.9 ms
  median, 4.9 ms p99** — the goal met on our side of the wire with a ~25×
  margin, the venue's ~175 ms geography unchanged on top. Six Opus-built
  crates against a contract crate pinned first (one shared clock, one
  append-only log, one parse of market data, orders out the same
  single-thread loop); two adversarial reviewers then attacked the tree and
  found five HIGHs, all fixed with tests proven to fail first: both feeds
  rebuilt onto worker tasks (the engine's own `select!` cancellations starved
  every reconnect — proven with an engine-shaped repro that froze for 8 s),
  the loss guard's daily anchor and trip latch now ride the log durably
  (restored at boot; a crash-loop could previously re-mint the daily loss
  budget), reduce-only exits shed their stop fields (Bybit rejects them —
  the covering test was vacuous), fills newer than the account reading count
  against the envelope, and a restart re-registers in-flight orders so the
  partition cannot hand shares out twice. The four capital controls are
  ported with table-driven parity tests mirroring the Python cases
  (engine-risk/PORT_NOTES.md maps every rule); NOT ported, by scope: the
  symbol-notional cap, the component/account gross split, the account
  initial-margin cap, and the available-margin increase test. Known, chosen
  differences from the fleet's LONG sleeve: the touch trigger reads bid/ask
  (not mark), one exit resend (not backoff-until-filled). Safety posture:
  demo hostnames only, by construction (a self-testing fence scans the venue
  crate); shadow mode default; no `REAL_MONEY` equivalent exists in the
  engine. The research seam is
  `liquidity_migration/research/engine_config.py` → flat `[[strategy]]` TOML
  blocks. Rust 270 tests + full Python gate 3,212 green. The Python fleet is
  untouched and still owns everything live.

- **2026-08-13 ~21:50 UTC — Wave 3 merged to main, NOT deployed: queueing one
  target request costs one durable file write instead of three, the owner pass
  parses the queue once instead of twice, a price touch wakes the LONG sleeve
  in ~2 s instead of up to a minute, the owner's garbage-collector pause
  shrinks from ~14 ms worst to ~0.2 ms measured, and the market-readiness
  pointer's file syncs no longer hold the lock the order path takes.** Owner
  directive: "implement the rest of phase 3 … take any measures to reduce
  latency." Built by three reviewed agent branches on top of `ea05e1f` (the
  capture-pointer deferral), then two adversarial reviewers attacked the whole
  diff; six confirmed faults were fixed in `a9984b8`, each with a test proven
  to fail on the unfixed code. Full gate 3,198 tests green.
  - **The inbox** (`4f83667` + fixes): the arrival order rides inside the
    request's own file (6 fsyncs → 2 per publish, ~11 ms → ~4.4 ms on the
    deployed host's disk); the counter survives only as an advisory buffered
    write so numbering keeps climbing across producer rebuilds; a queue file
    the publish scan cannot read is skipped with a warning instead of blocking
    a safety exit from queueing (the claim walk still fails closed on it);
    the parse is memoised per file identity so the readiness peek and the
    claim share one read; requests queued by the old build still read through
    their sidecar, so the live fleet's inboxes migrate in place.
  - **Producer wakes** (`990b1bc` + fixes): LONG cycles carry a stored health
    reading and a wake reason like carry's, served only while the owner
    receipt behind it would still pass a live read (ages never stack); the
    host checks price levels inside the ticker callback (float compares only)
    with a 2 s debounce and a fired-latch per registration, so a level that
    cannot clear wakes one cycle and the minute grid owns the retries.
  - **Owner runtime** (`f6eb634`): collect-then-freeze once warm, raised
    young-generation threshold, full collect at the end of every pass unless
    an order is already waiting. Measured (local, Python 3.13): worst pause
    13.7–14.1 ms → 0.14–0.24 ms, peak memory down 118→97 MB; the stalled-tail
    case degrades to a bounded 112 MB with automatic collection still armed.
    Magnitudes want one re-run on the VPS before quoting for that host.
  - **Deliberately not merged:** the arm/fire trigger module (branch
    `wave3/arm-fire`, `f76ae52`, fully tested). Carry cannot migrate onto it
    as a pure refactor — carry freezes a decision and computes intents at the
    boundary against boundary-time state, so arming intents ~90 s early would
    change what the sleeve trades — and LONG's exits already ride the new
    price wake through the normal publish path. The module waits on its
    branch until a real consumer exists.
  - **Deploy notes:** forward migration is in place (old inbox files read
    fine); **rollback is not** — the old build cannot parse new-format queue
    files, so roll back only with a drained pending/processing queue or an
    epoch reset. Not deployed tonight so the 00:20 UTC boundary receipts
    measure the wave-2 code actually running.
  - **Held by the program's own verdicts:** the journal WAL shape (decide
    after measuring fsync on the relocation candidate box); WS trade
    rehearsal and the Bybit institutional-gateway question (owner); the
    relocation A/B itself (owner green-light); moving the post-ack stop
    verification off the blocking pass (owner proposal — it is a capital
    control's timing).

- **2026-08-13 18:25–18:36 UTC — Deployed `4063a87` (wave-2 order path + universe
  resilience) staged stop-first, and the new funded renewal ran live on its
  first try: fresh funded universe and rules without VANRYUSDT, so the
  2026-08-16 funded expiry is gone. Then measured the demo order path with
  real entries: a two-entry batch went publish→venue-ack in 232–272 ms.**
  Owner directive: "go simulate and execute some demo entries and measure
  total latency again, break that down, and compare to old."
  - **Deploy receipt.** Fleet stopped 18:22:48, `staged-ok
    commit=4063a873…` 18:25, all nine units active+enabled, `mainnet=armed`,
    zero warning-level lines in any unit through 19:00.
  - **First live renewal under the new machinery** (both receipts were past
    half-life, so the deploy itself was the exercise): fresh funded universe
    `candidate-universe-20260813T182505Z-4063a873c415` (512 symbols,
    VANRYUSDT absent) and rules `venue-rules-20260813T182505Z-4063a873c415`
    (512 rules, held-exposure list empty — the funded account is flat), all
    three env vars rebound to the pair as a unit. New funded expiry
    2026-08-20 ~18:25 UTC. The un-renewable frozen universe and its
    2026-08-16 ~19:36 cliff are history.
  - **The measured order-path ledger** — six real demo requests through the
    production inbox (BTCUSDT + ETHUSDT, ~139/41 USDT at 2x under a
    probe-only carry strategy id; every position closed; account exposure
    identical before and after; receipts carry the new span milestones).
    Grouped two-entry request, leverage already proven (n=2):
    publish→owner-pickup 17–18 ms; pickup→plan-committed 33–62 ms (the
    attempt claims FUSED into the same commit: 0 ms extra; the 62 was the
    first pass paying a fresh market-data read); commit→disk-proof 4–5 ms
    (the write-behind journal's barrier, real VPS disks); disk→wire under
    1 ms; wire→venue-ack 178–186 ms for ONE batched request carrying BOTH
    entries. Totals 232 and 272 ms. Single reduce-only exits (n=4):
    pickup→wire 20–39 ms, same ~172–183 ms venue exchange; quiet-path total
    226 ms, worst 733 ms when the exit queued behind the pass ahead (the
    one-request-per-pass line is unchanged; grouped exits stay deliberately
    reverted). Cold-leverage evidence from two earlier probe rounds the
    risk check rejected (25 USDT is under one BTC qty step —
    `qty_step_mismatch`, refused loudly as designed, whole grouped request
    refused as a unit): both symbols' leverage was set at the venue
    concurrently, join 247 ms, overlapped with planning BEFORE the commit.
  - **Old vs new, measured against the 2026-08-13 00:20 ledger below:**
    owner decision-to-wire 37–68 ms warm was ~341 ms cold / ~95 ms warm
    (admission ~50 + separate claim commit + 40 claim→wire, plus 251 ms
    serial leverage when cold); the second entry now rides the same batch
    and the same 178 ms venue exchange instead of its own pass ~1.3 s later;
    the venue round trip itself (172–186 ms) is geography and does not move
    until the Singapore box. What the probe cannot exercise — the producer
    side (wake → publish of a frozen book) — self-reports at the next 00:20
    boundary (`froze_ahead`/`build_skipped` receipts).
  - **Open item for the owner: the demo rules receipt still expires
    2026-08-16 ~19:13 UTC** (`demo-rules-20260809T191337Z`). The deploy's
    demo refresh is flag-gated (`ROLLOUT_REFRESH_STALE_DEMO_RULES=1`) and at
    this receipt age routes to the order-placing probe, which requires a
    flat demo account — demo holds carry's KAITOUSDT and COTIUSDT, so a
    refresh deploy today would fail with the fleet stopped. Options: refresh
    in a naturally flat window; flatten demo first (closes carry's live
    positions); or let it age — a RUNNING demo owner keeps running past
    expiry, but any restart after 08-16 19:13 refuses until refreshed, and
    the watchdog goes warning from ~08-15 19:13.

- **2026-08-13 ~17:45 UTC — The system now handles a changing venue universe:
  delistings and new listings stop breaking renewals, sleeves, and exits.
  Merged to main after review; deployed 18:25 UTC (entry above), where the
  new renewal ran live on its first try.** (This entry originally said "NOT
  yet deployed" and carried a wrong ~19:00 stamp; corrected same day when
  the deploy landed.) Owner directive: "we
  need to be able handle an ever changing universe." Branch
  `engine/universe-resilience`, four fixes, each with a test proven to fail
  without it; gate green at 3,158 tests.
  - **A retiring symbol that is still held no longer stops the LONG sleeve**
    (`092be04`). The flatness gate raised before exit planning, so the one
    producer able to publish the flattening exits refused to run — a
    deadlock broken only by venue settlement or an operator flatten, and
    VANRYUSDT's exact shape (announced 2026-08-10, settled 08-12; demo held
    it for weeks in July and was flat at the announcement by luck). The
    cycle now reports the draining symbol (warning log + payload field,
    persisted dataset schema unchanged), entries stay suppressed through
    the population reconciliation, and exits keep publishing from the
    account journal — which never needed the universe at all: the max-hold
    exit is pure clock, and the armed venue-native stop covers the rest.
    CARRY already did all of this by design and is untouched.
  - **Rules coverage follows exposure** (`7aadb86`). Exits need per-symbol
    rules, and the receipt had to equal the entry universe exactly — so a
    held symbol dropped at renewal left a position the owner could neither
    exit nor dust-terminalize, wedging the strict-FIFO request queue at its
    head (every route closed: readiness never claims it, a claimed one
    fails the rules provider, convergence cannot call a residual dust
    without the rule; an uncovered HELD symbol also failed every entry
    account-wide through the account-wide input check). The receipt now
    covers universe ∪ declared held-exposure symbols: the mainnet freeze
    scans the account (positions, targets, working orders, unresolved
    inbox), a symbol the venue settled carries its structural rule forward
    from the prior receipt so a stale queued exit can still claim and die
    on the venue's definite reject, and the demo projection retains rules
    and probe evidence the same way (the probe alternative requires a flat
    account, which exposure precludes). Coverage stays exact on both parts:
    an undeclared extra fails, declaring a universe symbol as exposure
    fails, old receipts load unchanged, and rollback is safe (old code
    reading a new receipt just sees more rules). The demo order-placing
    probe is untouched — it proves whole-account flatness first.
  - **Mainnet renewal freezes a fresh universe every time** (`5529efd`).
    The funded symbol list was frozen 2026-08-03 and could never re-freeze
    (one fixed path + create-only writes made it mechanically impossible),
    so the delisted VANRYUSDT pinned every renewal against a live venue
    that no longer listed it. A renewal now freezes universe + rules as one
    pair from the live venue into timestamped receipts, the rebind writes
    the rules path and both universe variables together, and nothing
    rebinds until both freezes and the coverage proof pass — any failure
    keeps the installed pair consistent and the deploy finishes (the
    70baf5f rule, extended to both halves, now pinned per half). Bootstrap
    freezes also scan exposure, so a reprovision over a live account cannot
    mint a receipt missing held symbols. **This resolves the owner decision
    recorded below as due before 2026-08-16 ~19:36 UTC.**
  - **The watchdog watches the funded receipt's age** (`f212601`). The
    mainnet liveness scope had the receipt path in hand and discarded it,
    so the one receipt whose expiry makes the funded owner refuse to start
    could hit its 168-hour cliff silently. Now WARNING inside 24h, CRITICAL
    past expiry, own `venue_rules_age` key, deploy-renews-it remedy text.
  - Adversarial review (one reviewer, 5 findings, each re-verified at
    source): CONFIRMED and fixed — a queued ZERO-notional request (every
    exit and flatten is one) was invisible to the exposure scan while being
    a real rules consumer, so a stale exit for an already-flat symbol could
    lose its rules at renewal and wedge the queue head ~10 min on restart
    (any unresolved intent now counts, labeled `unresolved_request`);
    working-order exposure now labels per-order like the owner's own
    account-wide check, not netted; a live venue row declaring zero
    tick/step now fails the freeze for a universe symbol and is skipped in
    favor of the prior receipt's rule for a held one; the demo projection
    now records exposure it cannot retain (`held_exposure_unruled`) instead
    of staying silent. Each fix has a test proven to fail without it.
    ACCEPTED as a documented residual: structural drift on a held,
    still-listed symbol fails the demo projection into the probe path, and
    the probe refuses a non-flat account — so that compound state (schema
    bump forcing a projection + held exposure + unsafe drift on that exact
    symbol) fails a refresh-demo-rules deploy loudly with the fleet
    stopped; carrying drifted structural rules would be worse (venue-
    rejected exits), and the operator remedy is to deploy without the
    refresh flag or flatten first.
  - Recorded residuals, deliberately not built: the strict-FIFO queue head
    can still starve behind an unservable request (the ACE-wedge shape;
    coverage-follows-exposure removes the uncovered-symbol trigger, and
    `ops.sh wedged-command` remains the operator tool); the LONG cycle
    still fails on a venue-wide empty universe before exit planning (needs
    a venue-wide data outage, which fails the cycle earlier anyway); CONT
    (retired, off) keeps the raising gate. Host clock monitoring was
    evaluated per the owner's "only if useful" and NOT built: timesyncd is
    active and synchronized (~17 ms offset measured), and a venue 10002
    timestamp reject is already classified retry-safe with the venue's own
    self-describing message.

- **2026-08-13 ~17:30 UTC — Wave 2 built, reviewed, and merged to main:
  the signal→order chain is engineered for under 150 ms once the box moves
  to the venue's region. NOT yet deployed — the deploy waits for tonight's
  2026-08-14 00:20 boundary to prove wave 1 first.** Branch
  `engine/wave2-hotpath` (`b46dca0`, `5d17449`, `4539163`), full gate green
  at 3,149 tests. What changed, and what the adversarial review killed:
  - **Producer boundary** (`b46dca0`): the 00:20 pass pays no health reads
    and no retry sleeps (worst case was ~3s of sleeping at exactly the wrong
    moment) — the freeze window stores its live owner-health reading and the
    boundary serves it. Declared change point: the day sizes off freeze-time
    equity, ~90s early, worst case ~2 minutes of owner-state age; the resize
    dead-band absorbs the drift and the disaster stop never reads this.
    Route manifests verify once per process (ctime in the identity so
    metadata-only changes still re-verify); rule config parses once; a dead
    payload attach nothing consumed is deleted.
  - **Owner order path** (`5d17449`): plan and attempt claims fuse into ONE
    durable commit for reduce-only batches (crash semantics unchanged) and
    for entries whose leverage was proven pre-commit by concurrent
    speculative `set_leverage` — sole-authority (demo) only; the funded
    hand-traded account keeps today's sequence byte for byte. Declared
    trade: a fused batch crashing in the ms-wide commit→wire window resolves
    by the fail-closed wedge ladder (~5-6 min, no double-send) instead of a
    quick retry. Plus: pre-boundary quiescence (the owner is parked and
    listening at 00:20 instead of mid-Telegram ~10% of boundaries), a
    per-order span ledger on existing receipts (every boundary now
    self-reports inbox→plan→leverage→barrier→wire→ack in ns), and the
    completed-request path stops copying the whole journal per request.
  - **Venue client** (`4539163`): the library's hidden sleep-retries are
    stripped (a mocked rate-limit held the order path 18s; now it
    classifies immediately), and a keep-warm thread pins the order
    connection so the boundary never pays a TLS handshake. Named loss:
    pybit's recv_window auto-bump is gone, so persistent host clock skew
    >~5s surfaces as repeated transient errors — proposing host clock
    monitoring to the owner rather than building it unasked.
  - **Killed by evidence, no sunk cost:** (1) the WebSocket order channel —
    Bybit's v5 WS trade API does not exist on the demo cluster (docs state
    it twice; live probe: demo host 404s where mainnet answers) and the
    account rate limit is shared WS↔HTTP anyway; (2) grouped exit
    publication as the default — two independent review probes showed one
    dead symbol (delisting precedent) fails a grouped all-flat request at
    owner admission as a unit, and the loss-guard flatten's republish latch
    then never fires, leaving healthy positions open while the loss ceiling
    is tripped. Exits stay one request each everywhere; grouping survives
    only as an opt-in publisher capability with a landed-request check that
    prevents double-queueing.
  - **Review record:** two adversarial reviewers, five CONFIRMED defects
    (all fixed, each with a test that fails without its fix): the grouped
    loss-guard regression above; an armed fusion spec leaking to the
    convergence retry inside the same failed pass (specs now name their
    batch, mismatches discard); speculative leverage outcomes surviving into
    unrelated batches (cleared every round); the fusion leverage map fusing
    warm-cache mainnet entries (empty under shared authority); double
    publication when a grouped submit raised after landing durably.
  - **Rollback:** no tape or schema changes; wave-2 journals carry
    additive span keys in attempt payloads that older code tolerates on
    replay. Rolling back is redeploying the prior commit.
  - **Expected numbers, to be proven by receipts:** boundary signal→inbox
    ~15-35 ms (was ~4.5 s measured); owner software floor ~5-10 ms (was
    25.7 ms median); with the Singapore move, whole chain ~60-90 ms
    confirmed, against the 150 ms target. The pre-move chain stays wire-
    dominated (~175 ms round trip from Helsinki).

- **2026-08-13 ~13:40 UTC — The producers went perpetual: sleeves are plugs
  on one strategy host, cycles fire on account-journal commits and exact
  deadlines with a 60s idle floor, and carry's daily entries reach the venue
  as one batch.** Deployed `staged-ok commit=a61b8ab profile=operational`,
  all nine units active+enabled, zero error lines, the existing hash-chained
  event tapes accepted unchanged (carry:demo resumed at sequence 13400). A
  one-line forward-compat commit (`31ee68d`, admitting the new
  `journal_change` tape kind) was deployed FIRST and is the safe rollback
  floor — rolling back past it requires archiving each producer's
  `strategy_event_tape.jsonl` or the daemons crash-loop on the unknown kind.
  What changed (branch `engine/perpetual-host`, commits `f475aad..3ebfc6a`):
  - The producer daemon base is extracted to `strategy_host.py`; LONG,
    CARRY, and the retired CONTINUOUS are plugs (contract in the module
    docstring). Behavior-preserving; carry no longer inherits the LONG
    class.
  - New journal-change wake: producers watch the account journal's
    transaction segments (commits are rename-visible) and react to fills,
    rejection receipts, and protection events in ~2s instead of ≤60s.
    Change points: carry flips from the pure 60s timer to event-driven
    (same idle floor, same 00:20 deadline + freeze-ahead machinery), and
    the event wait is deadline-first — the 2.0s debounce never delays a
    due deadline (LONG max-hold/decay exits fired up to 2.0s late before),
    and a wake landing at the deadline instant folds into the boundary
    cycle. Carry journal-wake cycles serve the frozen day without a data
    build unless the funding sweep is due or a freeze-ahead ask is pending.
  - Carry publishes its entries+resizes as ONE grouped request (change
    point): one admission pass (was ~1.3s per extra entry), one journal
    claim, and the ≥2-command venue batch path finally engages for carry.
    Grouping is stricter on risk (all intents against the same prior book).
    With it, a rejection-suppression fix that also covers LONG: terminal
    suppression now follows what a rejection was ABOUT — an attempt named
    by a scoped key (`:SYMBOL`/`:target_key` suffix), or the whole batch
    only for account-level keys — so one dust symbol or a transient margin
    reading can no longer blank the whole grouped day's entries until
    signal expiry.
  - Fault fix: LONG cycles never wrote `kline_store_max_ts_ms`, so the
    fleet watchdog's LONG WS-staleness alarm had been dead code since it
    shipped. Now written.
  Review: two adversarial passes, 13 findings, 7 fixed (each with a test
  proven to fail without the fix — including a measured 419,704-spin hot
  loop in the journal watch's failure path), the rest recorded: the
  rollback-floor rule above; a deleted-and-recreated transactions dir
  silently parks the inotify wake on the idle floor (production resets stop
  producers first); wake-label counters can swap journal/bar attribution
  under a benign race (market_boundary is never mislabeled); producer
  evidence growth under sustained owner commit streams is floored at one
  cycle per 2s debounce — POST-DEPLOY WATCH ITEM. Receipts to watch at the
  2026-08-14 00:20 UTC boundary: `froze_ahead=True` on a pre-boundary
  cycle, `build_skipped=True` at 00:20, one grouped entry request, and the
  first carry batch venue submission if the day has ≥2 entries.

- **2026-08-13 10:55-12:01 UTC — Deploying the freeze-ahead change hit a
  latent fault: the mainnet venue-rules renewal fails on a delisted symbol,
  and its failure stranded the three mainnet units stopped for ~5 minutes.**
  The staged deploy of `2623afb` stopped the fleet, restarted the demo units
  clean, then died in the opportunistic mainnet venue-rules renewal:
  VANRYUSDT was closed and settled by Bybit on 2026-08-12 09:00 UTC
  (instruments-info `status=Closed`, delivery 1786525200000), so the frozen
  509-symbol funded universe no longer builds rules, and `fail` exited with
  the mainnet units still stopped and disabled — including the mainnet
  liveness watchdog timer, so the outage was silent. Repaired by hand at
  11:00:40 UTC (`systemctl enable --now` on the three units + timer; owner
  logs clean, the installed receipt `venue-rules-20260809T193602Z` is valid
  until 2026-08-16 so startup validation passed). Root-cause fix deployed
  in `70baf5f`: a failed renewal now keeps the validated receipt, prints a
  loud warning, and lets the deploy finish — the 168-hour ceiling is
  untouched, the funded owner still refuses an expired receipt, and the
  renewal retries every deploy. Final deploy receipt: `staged-ok
  commit=70baf5f profile=operational`, all nine units active+enabled,
  `verify-ok … mainnet=armed`, zero error lines after restart, no new
  rules receipt minted (correct: the freeze still fails on the delisted
  symbol). **Owner decision needed before 2026-08-16 ~19:36 UTC**, when the
  receipt expires and the funded owner will refuse to start: either (a)
  drop delisted symbols at renewal, which needs the coverage contract
  (exact universe↔rules symbol equality,
  `candidate_rule_coverage.build_candidate_rule_coverage`) to admit
  receipt-documented drops, or (b) re-freeze the mainnet candidate
  universe, which supersedes the recorded frozen-only-when-absent choice
  and would also admit venue listings added since 2026-08-09. Neither was
  taken unilaterally; both are small once chosen.

- **2026-08-13 — Where the boundary's ~5 s went, and the fix: carry now
  freezes the day's book ahead of the deadline and the deadline wake skips
  the data build.** Tonight's live cycle, split by receipt: wake +0.001 s;
  carry pass 4.50 s (summary logged 00:20:04.505 — of which ~2.0 s is the
  every-minute data build the frozen decision never reads (steady-state
  cycles measure p50 1.96 s over 183 cycles) and ~2.5 s is the fresh-day
  decision: venue-view join + 90-day replay + publish); owner pickup and
  admission commit ~50 ms; commit→claim 251 ms (one `set_leverage`, its
  documented 188-194 ms — both symbols were fresh after the 2026-08-12
  epoch reset); claim→wire 40 ms; one-way to the venue ~87 ms (geography).
  The change: any cycle inside a 90 s pre-deadline window computes and
  freezes the upcoming day's book from its own build
  (`_freeze_decision_ahead`), and the `market_boundary` wake on an
  already-frozen day goes straight to plan-and-publish — expected boundary
  pass tens of milliseconds, signal→order-at-venue ~0.3-0.45 s (~0.25 s of
  which is the mandatory leverage set plus wire; repeat-symbol entries skip
  the leverage set). Same decision, same registered 00:20 clock: the
  decision bar's inputs (23:00-00:00 kline, 00:00 settlement) are public
  and cached minutes after midnight; the synthetic-market test asserts the
  frozen-ahead book equals the boundary-computed book cell for cell, and
  sizing equity still anchors at the boundary cycle. The freeze store holds
  two days (a single slot made today's and tomorrow's freezes evict each
  other once a minute). Freeze-ahead refuses any build carrying
  repair-pending evidence — klines REST-repaired or store-unserved, or a
  funding sweep with fetch failures (adversarial review finding: an outage
  healing inside the window would otherwise pin a staler book than the
  boundary's own rebuild; the refusal test is proven to fail without the
  gate). Accepted residual, documented not gated: the top-150 fetch
  universe is sampled at the prewarm instant, so per-symbol ticker
  staleness healing inside the 90 s window can shrink the frozen reachable
  set (total outage self-refuses via the build failure or the 50-symbol
  decision floor) — a stricter ticker gate is the owner's call. Remaining
  serial cost outside carry's hands, measured tonight: the owner admits one
  request per loop pass (entry 2 waited ~1.3 s behind entry 1), and carry
  publishes per-symbol requests so the batch venue path stays dormant —
  collapsing both needs a cross-target batch request shape, proposed, not
  built. Receipts to watch tonight: the ~00:19 cycle logs
  `froze_ahead=True`, the 00:20:00.001 cycle logs `build_skipped=True`,
  entries admitted within ~0.3 s.

- **2026-08-13 00:20 UTC — First live deadline-fired producer cycle: carry's
  daily decision ran at +0.001 s instead of the grid's median +24 s.** The
  tape's first `market_boundary` event carries cycle id
  `carry-target-carry_hold-1786580400001` — 00:20:00.001 UTC, against a
  measured +1.8 s…+58 s (median ~24 s, mean ~28 s) across the eight prior
  days the daemon was up at the boundary (yesterday: +57.96 s). The cycle
  computed the fresh 2026-08-13 decision, published two entries (~4 s pass),
  both admitted by 00:20:07 and open on the venue with their stops verified
  by 00:20:13 — day-boundary signal to live orders in ~7 s, where
  yesterday's identical chain started 58 s late. The create-boundary stop
  verifier also earned its keep twice: the venue applied both entry stops
  one tick off the commanded price and the verifier installed the correct
  stop directly. The first multi-order batch receipt remains pending: carry
  publishes entries as one-command requests, so the batch path (two-plus
  commands in one admission) correctly stayed dormant — it arrives with a
  LONG multi-entry cycle or a sliced entry.

- **2026-08-13 — The amend-budget question is answered: keep 8; the tick
  cadence itself is the measured win.** The quote-forge replay (evening tape,
  22 symbols, 2,901 paired attempts per world, the fleet's real 45 s window,
  conservative fill bound; sim taught the fleet's budget semantics, forge
  commit `4baef96`) found the 8-amend budget binds on 0.4% of attempts —
  the recipe itself wants only ~1.6 reprices per attempt (95th percentile 5)
  even evaluating on every book event, so the feared burn-and-freeze is a
  tail case with no measurable cost, and a budget of 24 is bit-identical to
  unlimited. **No config change.** The finding that matters: tick-driven
  evaluation beats the 3-second clock by **0.29 bp per entry (t = +5.5),
  +4.0 fill points, −4.0 deadline-cross points** at the 45 s window — the
  2026-08-12 tick-wake change is now directly measured at its deployed
  window, a larger edge than the recipe-vs-control gap itself. Caveats
  recorded in the forge findings: one evening tape (the recipe's own
  assembly tape, though no arm was ever tuned toward amend demand),
  conservative bound, flat maker fee.

- **2026-08-12 — The fast-execution engine merged to main
  (`engine/fast-order-path`, `c5985a8..d929bfb`, nine commits): journal disk
  syncs off the order path, batched venue orders, tick-driven quote
  repricing, deadline-driven exits.** Every piece was adversarially reviewed
  by independent agents before merge; all confirmed findings were fixed on
  the branch. Change points, each in its own commit message:
  - **Write-behind journal, default on** (`c5985a8`, `bc0a6aa`): a commit is
    visible at the rename; the disk syncs run on one background thread in
    commit order, and the order path pays one fail-closed barrier before
    bytes leave for the venue. Steady-state commit measured 5.3 → 1.1 ms
    with VPS-class disk-sync latency injected. `ACCOUNT_JOURNAL_WRITE_BEHIND=0`
    is the opt-out. A crashed owner's torn tail is quarantined and settled at
    the next start.
  - **Batch venue submission** (`f50ae8a`, `3172339`): two or more
    never-attempted commands pay one claim transaction, one disk sync, and
    one venue request per 20 rows instead of one each. Exits and entries go
    in separate requests; a row the venue answers ambiguously stays claimed
    for the existing probe ladder; a refused batch envelope degrades to the
    old one-at-a-time path. Found and fixed in passing: the duplicate-order
    detector matched the wrong venue error code (110089, a risk-limit code,
    instead of 110072).
  - **Tick-driven quote repricing** (`8bfe7a4`, `c54c75f`, `d929bfb`): the
    market stream thread wakes the owner the moment a quoted symbol's touch
    moves (at most once per 200 ms per symbol, healthy books only), and that
    wake bypasses the 3-second reprice clock for one pass — the clock stays
    as the backstop, and cross/cancel pacing is untouched. A wake pass reads
    memory only; a symbol whose book is dark keeps its blocking REST
    fallback on the 3-second clock. Basis: the 2026-08-04 quote-forge
    cadence check measured faster evaluation as strictly better.
  - **Deadline-driven exits** (`d57632a`, `6ae01eb`) — a strategy change
    point: each producer cycle reports the earliest instant a time rule can
    change its book (LONG max-hold stop or decayed-stop arming, CARRY's
    daily 00:20 UTC decision), and the daemon cuts its wait short at that
    instant. A known-in-advance exit fires within seconds of its deadline
    instead of up to a full 60-second grid interval late (mean ~30 s). A
    fired deadline hands any retry back to the grid, so a suppressed exit
    can never hot-loop a producer. Persisted cycle dataset schemas are
    unchanged.
  - **Not built, on purpose:** journal epoch snapshots (bounding replay by
    restoring state at a checkpoint) were audited and refused — the event
    log is load-bearing memory for protection anchors, funding dedup, and
    strategy cooldowns, and truncating it would disarm stops for the oldest
    positions. Measured costs today are small (~3 s replay per 100k events
    at startup; ~2.6 ms per order-path claim at 50k orders). The audit and
    the migration program it would take are in the session record.
  - **Open measurement:** at the 200 ms wake floor a fast market can spend
    the 8-amend budget in ~1.6 s (it spanned 24 s at the 3 s cadence). The
    drift cross and window-end cross still bound every entry; re-sizing the
    budget is a quote-forge replay question, queued.

- **2026-08-12 19:13 UTC — The funded account's tripped daily loss halt was
  reset by owner instruction, and the account is near-empty.**
  - **The trip.** 2026-08-10 09:24 UTC: `daily loss 144.02 USDT reached the
    76.52 ceiling (2026-08-10 open 450.08 -> 306.06)`. The ceiling rides on
    the equity-rescaled risk policy (0.25 × the ~306 post-drawdown equity).
    The bot's own book was flat and it was refused new risk from the trip
    until this reset — the drawdown was the owner's hand trading; the halt
    reads account-wide wallet equity, so it sees hand losses too, by design.
  - **The reset.** The owner asked for a complete reset. The mainnet owner
    unit was stopped, the anchor file archived beside itself as
    `account_loss_guard.json.tripped-20260810.reset-20260812T191330Z.bak`,
    and the unit started at 19:13:37 UTC. Deleting the file is the deliberate
    re-anchor path the reader documents (`_read_loss_guard_state`); the
    guard's own `reset()` has no wired operator command.
  - **After.** Owner `healthy`, empty detail, zero error-level lines; guard
    re-anchored day 2026-08-12 with a clean trip field.
  - **Finding: the account holds ≈0.04 USDT** (equity 0.0398, available
    margin 0.0073, venue facts healthy, no positions). Between the trip at
    306.06 and this reset the account went to ~zero entirely outside the
    bot's halted book — continued hand trading and/or funds moved off the
    account; cause not independently confirmed from here. At this equity no
    entry can size (the 6 USDT entry floor exceeds the whole wallet), so the
    fleet will decide cash until the account is funded again.

- **2026-08-09 — Hand-trading resumed on the funded account, so the funded
  owner gives back the cached-leverage fast path.** The owner reported setting
  leverage by hand. That makes this process one of two writers of leverage on
  the account, which is exactly the condition the fast path assumed away when
  it was taken on 2026-08-08 (`960c17c`).
  - **What changed.** `run_account_execution_service.sh` had no route for the
    flag at all — `--shared-leverage-authority` existed in the Python runner
    and in the adapter, and nothing on the host could reach it. It now reads
    `ACCOUNT_SHARED_LEVERAGE_AUTHORITY`, unset meaning off, wired the same way
    as the raw-market diagnostic dial; the mainnet owner unit sets it to 1.
  - **What it costs.** A symbol that goes flat forgets its cached leverage, so
    its next entry pays one `set_leverage` round trip, 188–194 ms measured. The
    entry path therefore returns to roughly its pre-`960c17c` cost on a fresh
    symbol. This is a deliberate trade of latency for correct sizing, not a
    regression.
  - **Scope.** Mainnet only. The demo account has no second hand on it, leaves
    the variable unset, and keeps the fast path. A venue value that
    *contradicts* the cache still drops it under either setting — that is what
    protects sizing and it was not touched.
  - **Deployed from a flat funded book.** `ops.sh flatten --environment
    mainnet` reported `already_flat` (journal sequence 4958, no positions, no
    orphans) immediately before the owner restart, so no exposure sat
    unsupervised across it.

- **2026-08-09 19:36 UTC — Deployed `d3c7b5c`. Both trading-rule receipts
  renewed; the funded one had no renewal path at all.** The demo watchdog
  warned that its receipt expired in 18 hours. Chasing it found a second
  receipt expiring ten hours after that, which nothing watched and nothing
  renewed.
  - **The funded account's rules were frozen once, on 2026-08-03, and never
    again.** `provision_mainnet_prerequisites` wrote
    `account-execution-mainnet/venue-rules.json` only `if [ ! -f ... ]`, so
    every later deploy skipped it. The demo receipt has been renewed by any
    deploy past half its life since 2026-07-27; the funded one had no such
    path. It would have expired 2026-08-10 21:55 UTC.
  - **That is the receipt that cannot be waived.** `enforce_registered_ceiling`
    is passed the mainnet flag, so the funded owner is held to 168 hours and
    demo is not. With `Restart=always`, `RestartSec=2` and the restart limiter
    now genuinely disabled (`b0870b1`), expiry turns any crash or reboot into
    an endless two-second restart loop failing at rule loading — with exposure
    on the book. Nothing pages on it: the age check in
    `check_fleet_liveness.py` is gated on the demo scope, and the funded
    receipt would fail the demo loader that check uses.
  - **Fixed by renewing it in the receipt's back half**, the same policy the
    demo probe follows. This one reads the venue's declared rules and places no
    orders, so it needs no stopped window. The freeze refuses to overwrite, so
    a renewal is a new artifact under
    `/var/lib/liquidity-migration/mainnet-venue-rule-receipts/` plus a rebind of
    `ACCOUNT_DEMO_RULES_FILE`; the superseded receipt stays as evidence. An
    unreadable or future-dated receipt is not an age question and still fails
    the deploy. The test drives the real function over stubs and fails against
    the previous script, which did nothing when the file existed.
  - **The rollout was tried first and aborted on its own gate.** `rollout`
    proves the **demo** account flat — it loads the demo root and demo
    credentials, and `mainnet_armed` only decides whether that proof is a hard
    gate or a warning. The demo book held `LAUSDT` 1959, so the pre-stop proof
    refused with the whole fleet still running and untouched. Its first phase
    also verifies topology, which requires every sleeve-on producer up, so
    there is no way to hold the book flat and pass topology at once.
  - **`staged --stop-first --refresh-demo-rules` is the path that works** when
    the book has to be quiet first, and it is what the operations guide already
    names for a deploy that died inside rule maintenance. Producers stopped,
    both books flattened (demo `LAUSDT` 1959 closed, journal 19142 → 19156;
    funded already flat), then one deploy: demo probe 510 symbols in ~22 min,
    funded freeze 509, both rebound, all nine units back active and enabled.
  - **Receipts.** Demo `demo-rules-20260809T191337Z`, funded
    `venue-rules-20260809T193602Z`, both verified 2026-08-09 and expiring
    2026-08-16. The demo candidate universe grew 509 → 510, which is why the
    plan was a full probe rather than a projection. Zero error-level lines on
    any of the six services since; both liveness scopes report no active
    alerts, and the demo watchdog prints `cleared: demo_rules_age`.
  - **Left open, owner to decide:** nothing pages on funded rule age. Renewal
    by deploy is now the only thing keeping it fresh, so a fleet that goes a
    week without a deploy would reach expiry unwarned. Also unchanged: the
    funded candidate universe is frozen only when absent, so it never grows —
    which looks deliberate, since what the funded account may trade is a
    trading decision rather than maintenance.

- **2026-08-09 — The order path stops queueing behind the owner's own venue
  reads. Entry 345 ms → 276 ms median, exit 277 ms → 252 ms; our own software
  time 83.9 ms → 21 ms median, 11.6 ms best.** Commits `c94862d`, `9263a7e`,
  `24a2734`, `e8b4ff5`, `0ce028d`, `9ab90eb`, `bdf396c`. Same probe, same
  symbol (SOLUSDT), same host throughout.
  - **The order path now runs first in the pass.** A live profile: 73.3% idle,
    20.2% inside the reconcile's blocking REST reads, everything else under
    1.5% — and the order path ran tenth, *after* that reconcile. Two orderings
    were preserved on purpose: the private-stream supervisor still runs first,
    so a stream that stopped delivering fills refuses new exposure in the pass
    that notices; and protection still reaches the venue in the pass that saw
    the breach, by serving its published flat explicitly.
  - **The wake-up had a 50 ms hole.** The sleep read the pending directory's
    mtime when it *began sleeping*, so an intent that arrived while the
    previous pass was still running had already moved it, looked like no
    change, and waited out the whole interval. inotify queues arrivals while
    the pass works and returns them at once, in microseconds rather than on a
    4 ms poll slice. Polling remains the fallback off Linux, with the baseline
    corrected either way.
  - **The largest single item was not what it looked like.** After the
    reorder, 18.55% of all wall clock was `get_positions`, read *inline*
    despite a warm feed existing to prevent exactly that. The feed was being
    bypassed by a test for "newer than the report I last published"; it runs
    one thread over three ~172 ms reads, so a position refresh lands every
    ~420 ms and lost that test constantly against a 500 ms cadence. Freshness
    is the right test and is what everything downstream consumes. Blocking
    venue reads on the loop: **19.2% → 1.3%**, idle 73.3% → 82.3%.
  - **That fix broke exits, and the next measurement caught it.** Within a
    second of a fill the warm snapshot still describes the old book, and the
    reduction gate reads that as the venue contradicting the kernel —
    `venue=0:reconstructed=0.3`. The gate was right; the input was wrong. An
    exit published straight after an entry waited ~1.1 s, on the
    risk-reducing side of the book. A warm snapshot may now never *declare* a
    disagreement: any disagreement is confirmed at the venue, so a mismatch is
    only ever declared on truth read just now. The feed also re-reads
    positions the moment a fill is seen, off the loop, so by the time an exit
    arrives the snapshot already agrees.
  - **Smaller, measured:** the journal projection stopped fsyncing (it is
    rebuildable, the transaction segment is the commit point and still syncs)
    and stopped re-reading its own tail on every commit; pending-order
    confirmation and funding recovery now stand aside for a waiting intent,
    bounded at 5 s so the drop-recovery backstop cannot be starved.
  - **Where it stops, and why.** What is left is two durable journal commits
    and almost nothing else — sizing an order measures 0.02–0.1 ms. One commit
    is 5–7 ms at best: ~1.3 ms disk sync on a virtualized device (`fdatasync`
    is no faster) plus CPython hashing and canonical JSON on a 2-core
    2015-era Xeon. The second commit is `record_submission_attempt`, the
    single-winner guard that stops a crash submitting the same exposure twice;
    it is deliberately the last durable act before the wire and was left
    alone. **Sub-10 ms needs one commit instead of two, a faster CPU, or a
    faster sync — not more loop tuning.**
  - **Two corrections to earlier readings.** An isolated fsync probe reported
    0.007 ms for a directory sync; in the real workload it is ~1.07 ms — the
    probe was re-syncing an already-clean directory. And lowering Python's GIL
    switch interval, which looked promising against a saturating stub decoder
    (63 ms → 49 ms), made things *worse* at the real 17% decode duty
    (16.8 → 18.9 ms). It was not shipped.
  - **Second pass, after the above proved too early to stop.** Commits
    `e954831`, `77a987d`, `5441be6`, `63e20ca`. Software path **22 ms → 25.7 ms
    median but 11.6 ms → 9.1 ms best**, with the first orders measured entirely
    under 10 ms (9.2 ms, then 9.1 ms in the next run, 1 in 60).
    - **The WebSocket library was re-proving UTF-8 in Python.** A profile of
      the ticker stream's own thread: 87.6% idle in select, and roughly a third
      of what is left in `websocket-client`'s `_validate_utf8` and `_decode`,
      against 0.6% in this repo's frame handler. Pure-Python byte loops holding
      the GIL the order path needs. `skip_utf8_validation=True` took that
      thread from 19.8% CPU to 14.4%; frames now arrive as bytes and go to
      `json.loads`, which decodes UTF-8 strictly exactly as the library's own
      decode did.
    - **The order path rebuilt two things on every pass.** The authorized
      native-breach flat set walked every protection ever recorded — 200 on the
      demo book, growing all session — ahead of every request, at 24.5% of
      order-path time (about 46% of everything that was not the network). And
      the journal rebuilt its own directory paths from the root, through
      `expanduser`, about fifteen times per order. Both are now remembered.
    - **`recovered_rows` was replaced by the test it was approximating.** Any
      pass that had applied fills skipped the warm feed entirely — 20.8% of
      wall clock during live trading, on exactly the passes where orders flow.
      A snapshot older than the fills cannot agree with the book they produced,
      so the agreement check already detects it, and detects it precisely.
      Blocking venue reads during trading: 20.8% → 6.5%, and what is left is
      the genuine ~172 ms window after each fill.
    - **`json_safe` got exact-type fast paths** — it is the hottest function on
      the order path at 2,315 calls per order, and its general chain reached
      `isinstance(value, Mapping)` on the `typing` alias, so every dict paid ABC
      machinery. Verified byte-identical over 60,000 generated structures
      (enums, IntEnum, `.item()` scalars, non-string and mixed keys, NaN/inf)
      and over every event in the live journal.
    - **Third pass: the median is an ageing problem, not a scheduling one.**
      Commits `b921ae6`, and the reverted `c9fbfbb`/`3837cfe`/`d3e8906`.
      - **The mid-pass yield was tried twice and reverted twice.** Having the
        pass go back to the top when an intent is waiting — where the order
        path runs first — should have closed the bimodal gap. Yielding on the
        bare arrival signal took the median from 25.7 ms to **54.1** (exits
        23.6 → 101.9): the signal stays raised until read, so it had to be
        consumed to stop the loop spinning on an unready request, and
        consuming a wake-up for an intent that then did not get served left
        nothing to wake on — the pass ended by sleeping the whole idle
        interval. Asking the readiness gate first fixed that mechanism and
        still measured worse (**32.3 ms**), because a pass that yields pays
        its top-of-pass work twice. Removed.
      - **Three O(history) costs removed, and the reconcile fell 21.1% ->
        3.7% of the loop; the owner is now 89.6% idle.** The largest was the
        anchor projection, which replayed the entire event history on every
        call despite existing -- per its own docstring -- so that protection
        checks would not. It is memoized on the `(events_applied,
        rolling_state_hash)` pair it already validates. `_snapshot_ref` was
        copying all 16,059 events into a fresh tuple per call, and every
        protection check asks for one. Both native-protection lookups filtered
        the whole protection map per symbol per pass. All three are keyed on an
        identity a commit necessarily moves, and each has a test that fails
        against a cache that does not invalidate.
      - **What made the median grow is the account getting older.** Over
        this one day of testing the demo book went from 129 orders and 200
        protections to 965 and 1,463, and the reconcile's share of the owner
        loop went **7.4% → 21.1% with no code change**. Nothing prunes any of
        it. The two native-protection lookups were filtering the whole
        protection map per symbol per pass — 16% of the reconcile — and are
        now indexed on the committed state object, taking the reconcile back
        to 17.2%. The `account_strategy_state` scans behind the rest still
        walk the event history every pass.
      - **This confounds every cross-session latency number here.** The same
        code measures slower on an older account, so a figure from a fresh
        epoch is not comparable to one a week later.
    - **Not taken, and why.** Dropping `sort_keys` from `canonical_json` is
      provably redundant (0 mismatches in 40,000 cases) and worth 0.05 ms of
      0.51 — not worth touching a hash chain for. Thinning the capture store's
      per-record free-disk check (~1% of CPU) would weaken a fail-closed
      control. And merging the two pre-wire commits, which would roughly halve
      the software path, would make every crash between commit and send
      non-retryable: the second commit is late *precisely* so an un-attempted
      command can be safely retried and an attempted one cannot. That is an
      owner decision, not a performance one.

- **2026-08-09 — Three blocking venue reads leave the order path. Entry
  881 ms → 345 ms median, exit 286 ms → 277 ms.** Commits `bdc705b`,
  `021edd4`, `d5836bd`, `f7544ce`. Measured with the same probe, same
  symbol (SOLUSDT), same host, before and after each step.
  - **The wallet was the hidden one.** Bybit's `wallet` topic pushes only
    when the balance *changes*, so on a quiet book the pushed row aged out of
    its 5 s window and every batch paid a blocking `get_wallet_balance`. It
    showed as a pinned **~195 ms** inside intent-durable-to-order-commanded —
    three exits measured 194.4, 194.7 and 194.9 ms, which is a round trip, not
    a scheduler. The warm feed that already serves positions and open orders
    now serves the wallet at 1 s, making the cached equity *fresher* than the
    window it feeds. `durable → commanded` **228 ms → 43 ms**.
  - **Entry-attached stop verification cost two more round trips.** It read
    position truth back over REST right after the create, and retried, because
    Bybit lags between accepting an order and making the position readable.
    The venue pushes that position, `stopLoss` included, within milliseconds of
    the fill — so wait for the push instead of asking for what has not
    happened yet. **The control is not relaxed:** a pushed row is accepted only
    if observed strictly after the acknowledgement being verified, the wait is
    bounded at 500 ms (well under the two round trips it replaces), and any
    timeout or fault runs the same REST loop. A test that removes the
    acknowledgement bound fails.
    - *First attempt was wrong and the measurement caught it.* Reading the
      cache at acknowledgement time never worked: a market order is
      acknowledged **before** it fills, so no pushed row could be newer than
      the ack and every entry still paid both trips. Entries did not move.
  - **The owner also stops sleeping through arrivals.** A flat 50 ms sleep
    meant an intent landing just after a pass waited out the whole interval;
    a new request is a new file in `pending/`, so one `stat` every 4 ms wakes
    the loop instead. On its own this changed nothing measurable — which is how
    the wallet round trip was found.
  - **Where the remaining time is.** A warm entry is now ~10 ms publish, 25–40
    ms to command, ~15 ms to the socket, **172 ms of venue round trip**, and
    the rest. The floor without moving the host is ~250 ms, and the best
    entries measured 266 ms with exits at 251 ms. The median is held above it
    by loop scheduling: when the pass is mid-reconcile as the intent lands,
    `durable → commanded` runs 250–408 ms instead of 25–40 ms.
  - **Deployed `b0870b1`, units up at 10:09:46 UTC** (`staged --stop-first`,
    the funded book flat before each stop), after `2aa7f36` at 10:01:24. Both
    owners, both demo sleeves, both funded sleeves and the two liveness timers
    came back active and enabled; both owners publish `healthy` with an empty
    detail and there are zero errors on any unit since.
    - **The deploy restarted the two funded producer sleeves**, which had been
      stopped cleanly at 02:12 UTC and left down for eight hours — the funded
      owner was brought back alone at 09:50. `activate` starts the whole fleet
      whenever real money is armed and the sleeve toggles say on, so the
      funded account went from managed-but-not-trading to trading at 10:01:24.
      Flagged for the owner rather than reverted: the toggles are the
      configured intent, and `verify-units` reads producers-running as correct.
  - **The real-money owner's restart limiter had never actually been
    disabled.** `StartLimitIntervalSec=0` sat in `[Service]`, where systemd
    ignores it and says so on every start ("Unknown key name ... ignoring");
    `systemctl show` read the default 5-starts-in-10s window. With
    `RestartSec=2`, five restarts fall inside that window, so a venue blip on
    the startup permission probe could latch the funded owner failed and leave
    exposure unsupervised — the outcome `Restart=always` exists to prevent.
    Moved to `[Unit]`; `StartLimitIntervalUSec=0` now reads back from the host.
    The test asserted only that the substring appeared somewhere in the file,
    so it passed the whole time the limiter was live; it now asserts the
    section and fails against the old unit.
  - **The owner can no longer be started without the ticker touch feed.**
    Deleting the `--no-touch-feed` flag and the `ACCOUNT_TOUCH_FEED` wrapper
    case removes the only configuration that made a cold entry wait ~790 ms.
    The switch existed because the feed costs ~16% of one core; the A/B settled
    that trade the other way, so keeping the loser reachable only left a way to
    be slow by accident. Owner-directed: *"remove the slower version
    permanently, strip it out."* The optional `touch_cache` on
    `SequenceAwareMarketRecorder` stays, because the standalone bulk-capture
    CLI records L2 and prices nothing — but no account owner can reach that
    shape.

- **2026-08-08 — A symbol no longer has to have a book before it can be
  priced.** Commit `a2db3c1`, deployed 22:26 UTC, both realms.
  - **What the order path actually reads is the top of book** — reference
    price, bid, ask, and the two displayed sizes. It was waiting for a full
    depth-50 snapshot to arrive first (~200 ms, up to 3 s). All 509 candidate
    symbols now carry a pushed ticker, so a target on a symbol the L2 stream
    has never carried is priceable at once. **Proved on the live demo owner:
    an entry on AVAXUSDT, never traded before, was priced
    `source=bybit_ticker_touch` with no reconstructed book present at all.**
  - **A ticker touch is a price, not a book, and never pretends otherwise.**
    Callers opt in per read; markout grading and raw capture still refuse
    anything but real L2; a decision priced from the touch records
    `book_source=bybit_ticker_touch`, so the journal never implies depth that
    was never observed.
  - **A/B, same host, same symbol, same probe, 15 minutes apart.** Feed off, a
    cold SOLUSDT entry waited **1002 ms** to be commanded and took 1783 ms end
    to end; feed on, **216 ms** and 1026 ms. Warm entries are the same either
    way (821–881 ms) — they never had a book problem. Exits: 459 ms median off,
    286 ms on.
  - **Measured cost: ~500 frames/s, 90 KB/s, and ~14 points of one core per
    owner** (29.8% with, 15.5% without). Nearly all of that is the websocket
    library's own frame handling (12.8% with a no-op handler), not parsing —
    and 98% of ticker frames carry the touch, so filtering before parsing saves
    nothing. **It does not slow the owner loop:** 76 ms with it on, 77 ms with
    it off, because the loop is sleep-bound and the feed runs on its own
    thread. An earlier note in this entry claimed the loop went 69 → 80 ms
    because of the feed; the A/B disproves that, and the drift from 69 ms is
    the journal growing. `ACCOUNT_TOUCH_FEED=0` turned the feed off with a unit
    restart — **superseded 2026-08-09: that switch and the `--no-touch-feed`
    flag behind it were deleted, so the variable now does nothing.**
  - **A symbol keeps its subscription for 10 minutes after its work clears**
    (`--symbol-warm-seconds`), so a repeat entry never re-warms; and a queue
    head no socket is carrying yet is priced by one REST tickers read rather
    than waiting (`--touch-rescue-seconds`).
  - **Entry rest window 120 s → 45 s.** 15 live resting entries filled at a
    median of 1.28 s and a maximum of 36.6 s (60% as maker), so 45 s keeps
    every passive fill 120 s got while bounding the tail. Going shorter costs
    fills: 30 s would have crossed 1 of 15, 15 s would have crossed 3 of 15.
  - **Two measurements that stopped changes rather than causing them.**
    Subscribed depth stays at 50: `docs/architecture.md` §Trade diagnostics and
    `docs/research/carry_hold.md` §5 both walk the visible depth-50 decision
    book for `book_walk_shortfall_bps`, the only measured impact evidence in
    the repo. And **the venue floor is geography** — TCP connect to
    `api.bybit.com` is 7.2 ms and the TLS handshake 18.1 ms, but a full request
    is 187.6 ms, so ~180 ms of every round trip is the Frankfurt CloudFront
    edge proxying to Bybit's Asian origin. `api.bytick.com` (193 ms) and
    `api.byhkbit.com` (206 ms) are the same edges. No code change reaches it;
    only moving the host near the origin does.
  - **Sizing from the producer's own decision price is built but off**
    (`--producer-price-max-age-seconds`, default 0). Producers publish a
    notional with no price at all, the carry producer's own price is a daily
    bar close, and publish-to-sizing is 3.1 s at the median but **443 s at
    p90** — so a stale price would misconvert notional to buy latency the
    ticker feed already removed. Exits always size off the live price.
  - **Incident, self-inflicted and cleared.** The latency probe left 0.1
    AVAXUSDT (~$0.65) — under the venue's 5.10 USDT minimum notional, so the
    kernel correctly refused to order against it, and protection blocked the
    demo owner on a position with no component target. The owner then refused
    the lift that would have taken it back over the minimum, which is the
    deadlock. Cleared by stopping the demo owner, taking its single-writer
    lease, and placing one reduce-only close. Mainnet was healthy throughout.
    **A probe that opens exposure must close it in one order, not two.**

- **2026-08-08 — Owner stops hand-trading; the entry path loses its two
  waits. Entry ~1.0–1.2 s → ~0.76–0.82 s, exit ~250–370 ms.** Commit
  `960c17c`, measured with the same demo probe as the entry below.
  - **`set_leverage`, 188–194 ms before every fresh entry, is gone.** Bybit
    keeps a symbol's leverage after its position closes, so the cached value is
    still what the venue holds — but the cache was dropped whenever a symbol
    went flat, precisely because the owner hand-traded the same account and
    could change it underneath. With hand-trading stopped, drop-on-flat goes
    and the round trip with it: **11.9–13.9 ms** at that step, from 184–194 ms.
    A venue value that *contradicts* the cache still drops it under either
    setting — that is what protects sizing, and it is not what this relaxes.
    `--shared-leverage-authority` restores the old behaviour in one word the
    moment hand-trading resumes, and the old behaviour keeps its own test.
  - **Symbol subscription no longer waits on the refresh interval.** The
    readiness gate reads the queue head every tick, so the loop knew within
    50 ms which symbols a request needed, then waited up to
    `--symbol-refresh-seconds` (5 s) before telling the stream to carry them —
    and the request could not be served until its book arrived. Measured on a
    flat symbol: 229 ms at best, **3053 ms** at worst, all of it idle. An
    unsubscribed queued symbol now forces the refresh on the next tick;
    afterwards `intent durable → order commanded` measured 191–229 ms.
  - **Caveat on reading the entry number.** Entries rest at the touch first, so
    on a real book the time to *fill* is queue economics, not system latency.
    The system number is intent → order live at the venue: ≈400 ms for a fresh
    symbol, ≈215 ms once held, ≈245 ms for an exit.

- **2026-08-08 — End-to-end order latency, measured with real demo orders.
  Exit ~260–320 ms, entry ~1.0–1.2 s, and the difference is one `set_leverage`
  plus a cold-symbol book.** Six orders placed and closed on the demo account
  (SOLUSDT, $30, target key `carry/latency_probe/…`, published through
  `AccountTargetPublisher` exactly as a sleeve does). Demo book returned to its
  prior state each time — HOMEUSDT/HFTUSDT untouched, no working orders.
  Timings are wall-clock on the host, from the inbox write to the receipt, with
  the middle stages taken from the journal the owner writes.

  | stage | exit (reduce-only) | first entry into a flat symbol | entry while already held |
  | --- | --- | --- | --- |
  | publish → intent durable in inbox | 10–14 ms | 8–11 ms | 11 ms |
  | intent durable → order commanded | 17–72 ms | 229–429 ms | 23 ms |
  | order commanded → REST send begins | 12–16 ms | 188–194 ms | 11 ms |
  | **total, publish → receipt** | **259–322 ms** | **983–1202 ms** | **606 ms** |

  - **The entry penalty is not general slowness.** A second entry placed while
    the position was still held paid **11 ms** where the first paid **188 ms** —
    that step is `set_leverage`, which `bybit_execution_adapter.py:237` runs
    before every non-reduce-only order whose cached venue leverage does not
    match. Exits are reduce-only and skip it. The cache is deliberately dropped
    for a flat symbol (`retain_confirmed_leverage` keeps only what an
    authenticated position row confirms), because the owner hand-trades the same
    account and can change leverage underneath — so **every fresh entry pays one
    round trip by design.** Left alone; changing it is an owner decision about
    the shared-account policy, not a latency fix.
  - A genuinely cold symbol (never streamed) cost **2171 ms** on its first
    entry — book warmup, paid once per symbol.
  - **The exit path is what matters for risk, and it is ~300 ms against a
    ~190 ms physical floor**: one signed Bybit round trip. Roughly 50 ms to
    notice, 190 ms of geography, 60 ms to confirm the fill.

- **2026-08-08 — The 200 ms target, met by profiling instead of designing.
  Owner loop 284 ms → 69 ms; venue truth 1.37 s → 0.23 s.** After the
  clever design below failed review, the win came from the dull question: what
  is the loop actually waiting on? Three REST reads ran on *every* reconcile
  pass with nothing gating them — one `get_positions`, and the two paged
  `get_open_orders` queries behind order-ownership inspection — at ~175 ms each
  against a CloudFront edge. Everything they starve is time-critical: software
  stops, take-profits, quote repricing.
  - All three moved to a background read-only thread (`VenuePositionFeed`,
    `account_reconcile.py`). It touches no kernel state, so it adds no second
    mutator. Positions refresh at 250 ms because that is what the reduction
    gate ages; open orders at 2 s because ownership only decides whether a
    hand-placed order gets logged — since 2026-08-07 it blocks nothing.
  - Two conditions send a read back inline, both covered by tests that were
    verified to fail with the guard removed: a pass that **recovered rows**
    (venue view must post-date the mutations it is compared against, or drift
    is reported that is not drift and new risk is blocked), and a feed with
    **nothing newer than the published report** (a stalled feed would otherwise
    re-stamp an old observation as this pass's freshness). A dead feed degrades
    to exactly the pre-change behaviour — no new failure mode.
  - With the reads warm, `--reconcile-seconds` 2.0 → 0.5 and `--idle-seconds`
    0.1 → 0.05. The reduction-admission bound was **decoupled** from the
    cadence and pinned at the 4 s it has always been: how fast reconciliation
    runs is a latency choice, how stale truth may be when an EXIT is admitted
    is not.
  - **Measured on the funded owner, not argued.** Before: 284 ms/iteration
    (3.52 Hz) at 6.2% of one core — 6% CPU with 65% of the iteration blocked
    said "network, not compute", and `py-spy dump` named the exact frame
    (`get_open_orders` ← `inspect_bybit_order_ownership` ← `reconcile_once`).
    After the reads moved: 139 ms/iteration (7.19 Hz), 6.3% CPU, main thread
    parked in the idle sleep rather than a socket, venue-fact age 0.387 s. With
    the tick then halved: **69 ms/iteration (14.56 Hz), 8.2% CPU, venue-fact age
    0.228 s.** Steady-state reconcile makes no REST call at all. Commits
    `6f9d091`, `acee4bf`, `05f34c7`.
  - Remaining floor: a signed Bybit round trip is ~175 ms of geography and no
    amount of ticking shortens it, which is why the tick stops at 50 ms.

- **2026-08-08 — The 200 ms design failed its own safety review. Negative
  result, recorded before anything was built.** Owner set a 200 ms target
  against today's 2 s. Four designs competed; three judges independently picked
  the same winner — *push the private socket's `position` topic into a cache
  that accelerates the position-truth gate, and move REST reconcile off the
  loop*. Its central claim was that the cache is a **monotone accelerator**: it
  can only lower `age_ns` and only raise the venue quantity, so it can unblock
  an exit but never block one. Three adversarial lenses all returned fatal, on
  the same root cause, and the two load-bearing branches were then confirmed
  directly:
  1. **The staleness check is two-sided.** `account_reconcile.py:631`, `:657`
     and `:969` all read `if age_ns < 0 or age_ns > bound_ns: raise`. Pushing
     `observed_ns` up toward now destroys the slack that absorbs a backwards
     wall-clock step (NTP correction, VM live-migrate). One step back and
     `age_ns` goes negative → `AccountReconciliationStaleError` → **every exit
     on every symbol refused** until wall time climbs back past the cached
     stamp. "Monotone" was simply false.
  2. **In the new-risk gate the acceleration is pure masking.**
     `require_recent_healthy` (`:625-635`) never reads `venue_positions` — it
     checks age, then `report.require_healthy()`. So the cache's *only*
     possible effect there is to suppress the staleness error, i.e. to keep
     admitting new risk against a reconciler that has stopped.
  3. **The continuity guard compares a number to itself.** The cache would be
     seeded with the same `observed_ns` the report carries, so
     `_seeded_at_ns >= report.observed_ts_ns` holds by equality **forever**.
     With freshness stamped from *any* inbound frame, a dead reconciler reads
     as current indefinitely — and the frame most likely to restamp it is the
     one that *refutes* the report (owner hand-closes, venue pushes size 0,
     the merge correctly refuses to lower the quantity, and that same frame
     certifies the contradicted number as fresh).

  **What survives:** the quantity substitution alone (raise the venue quantity,
  never lower it), which no lens could break — though its reach is narrower
  than advertised, since `require_recent_symbols_consistent` short-circuits on
  any `{symbol}:` mismatch at `:669-676` before reaching the comparison. The
  real latency win is separable and needs no cache: **the REST pass blocks the
  10 Hz quote loop**, and taking it off-thread is what buys responsiveness.

  **Two corrections to the numbers this repo was working from:** a reconcile
  pass is 3 round trips *minimum*, not exactly 3 — `get_positions` and
  `get_open_orders` both page through `_cursor_result_list` (`bybit.py:518`)
  until the cursor empties, so ~525 ms is a floor and ~1.05 s is possible. And
  concurrent signed REST from two threads on one shared client is **already in
  production**: `account_execution_stream.py:385` calls `sync_symbols` from the
  consumer thread, reaching `set_trading_stop`.

- **2026-08-08 17:59 UTC — Deployed `9cbe889`.** Receipt `staged-ok
  commit=9cbe889`, `verify-ok … mainnet=armed`, nine of nine units. Carried the
  positions copy-on-write, a second 35-agent sweep, and the dead code the first
  sweep verified. Funded owner back with **zero** error-level lines; producer
  cycle time settled at 1.1–1.2 s, matching the pre-change steady state (the
  33.7 s first cycle is the kline bootstrap, not a regression).

- **2026-08-08 (sixth pass) — Reversing a refusal, and a vacuous test over a
  real-money branch.**
  1. **Positions get the copy-on-write that orders already had.** The earlier
     pass declined this optimisation because the version on offer enumerated
     `PositionState`'s fields positionally — which silently resets any field
     added later, in the accounting path, for 3.5x. Declining that was right;
     stopping there was not. The robust form is the pattern `orders` already
     uses: `transaction_state_copy` shares position objects and writes go
     through a new `position_for_write`. At 301 positions the whole state copy
     goes **0.295 ms → 0.002 ms** (the positions term alone was 0.240 ms) and
     no longer scales with position count at all. Positions are never pruned,
     so a symbol traded once was copied on every journaled event batch forever.
     Exactly two reducer write sites, both converted, each with a regression
     test driven through the real reducer — **both first drafts passed with the
     fix removed** and were rewritten until they failed, which is the trap this
     repo has been caught by before.
  2. **A test guarding a real-money branch asserted nothing.**
     `test_the_producer_clamp_is_disabled_when_the_ceiling_tracks_equity` grepped
     `cli/commands.py` for the *text* of an if-expression, so it passed with the
     arms swapped — and swapped arms ship the fixed clamp on the funded profile,
     which governs how much notional the carry producer may target. The branch
     is now a named function evaluated against both shipped profiles, verified
     to fail when swapped.
  3. **Dead:** `_cal_roll` (a pass-through to `calendar_roll` left by the
     package split, never wired), `explicitly_false_or_unset` (last caller went
     with paper trading), the superseded non-API kline downloader,
     `format_universe_report`, `lag_screen`,
     `enforce_frozen_candidate_population`. **Junk tests:** an arity lint that
     greps for literals naming the retired `paper` environment so it can never
     fire, and an exact duplicate universe test. **Stale comments:** four
     pointing at code deleted with the SHORT sleeve, plus two describing a
     `probe_verified` receipt field that exists nowhere.
  4. **Kept, against the sweep's advice.** Three helpers with a definition,
     test callers and no production caller are **test seams, not dead code**:
     `KlineStore.has_symbol` (backs an add-only merge assertion),
     `read_account_route_manifest` (five tamper-rejection tests verify through
     it), `load_venue_accounting_receipt` (two tests check it rejects mode
     0644). And `continuous_hedge_manager` stays: `docs/trading_logic.md:311`
     says the model code "stay[s] for research", written by the same
     2026-08-03 change that removed its runtime, and deleting it orphans
     `regenerate_hedge_warmstart.py`, which still writes a 30 KB artifact
     validated only by the test that would go with it. `continuous_cycle_status`
     is not dead at all — `continuous_demo.py` writes through it on every
     published cycle.

- **2026-08-08 17:20 UTC — Deployed `bad876c`** (the sweep below). Receipt
  `staged-ok commit=bad876c`, `verify-ok … mainnet=armed`, nine of nine units.
  Owner back at 17:20:33 anchoring `100.00 -> 326.21`, zero error-level lines,
  both funded producers cycling `owner=healthy` / `err=none`.

- **2026-08-08 (fifth pass) — A 33-agent latency sweep, and the two biggest
  "dead" findings were the ones worth keeping.** Five readers over disjoint hot
  paths, every candidate then handed to an adversarial verifier: **15 of 28
  confirmed, 13 refuted**, four of the refutations catching fabricated numbers.
  Both reconcile candidates were refuted, which matched the independent
  judgement made here — deleting the reconciler's second open-order read looked
  like a free round trip but the repo documents that read twice as a deliberate
  refusal to trust a wrapper default to expose conditional orders.

  Taken:
  1. **`AccountJournalCursor.read` regex-revalidated every segment filename on
     every read.** An identical-semantics, prefix-cached helper already existed
     180 lines above and was already used by two other readers; the cursor —
     the per-producer-cycle reader — was the one caller never switched.
     One-line swap: 3.4 ms → 2.1 ms at 4,105 segments, and the removed term is
     the one that grows with journal age rather than with new events. The two
     superseded helpers are deleted with it (44 lines).
  2. **The capture path JSON-normalized every WebSocket frame even when nothing
     was written.** `_persist` walked and rebuilt every book level to produce a
     value only its own return used. Raw persistence is **off** on the funded
     owner, so that was every orderbook frame. Record identity is unaffected
     (`capture_record_id` hashes before the copy), and the live callers take
     only scalars off it — bids and asks are read back solely from the stored
     tape, verified by grepping every consumer.
  3. **`CapturedBybitMarketProvider.execution_book` had no callers**, which made
     its `_contexts` dict write-only: a lock acquisition, a dict write and a
     10,000-entry eviction scan per symbol per batch, feeding a reader that did
     not exist.

  Declined, with reasons:
  - **The `PositionState` copy** (0.21 ms per journaled event batch). The
     verifier's own caveat is right: explicit field construction would silently
     reset any field added later, in the money-accounting path, to save 0.2 ms
     against a system whose cost unit is a 175 ms signed round trip.
  - **The quote-lab shadow replay, 1,100 lines, the single biggest "junk"
     find.** It has no production consumer and that is beside the point: it is
     the machinery behind registered results still cited in
     `research_findings.md:202` and `strategy_program.md:46` — including the
     −0.36 bp/entry figure the urgency ladder rests on. Research tooling's
     consumer is the findings document.
  - **The LONG `funnel_observer`** (581 lines). The claim may hold —
     `build_candidate_tape.py` builds that funnel itself rather than injecting
     an observer — but it is documented architecture and was not worth cutting
     at the end of a long session over a live account.

- **2026-08-08 16:51 UTC — Deployed `8aa8f25`.** Receipt `staged-ok
  commit=8aa8f25`, `verify-ok … mainnet=armed`, nine of nine units. Deployed
  immediately rather than bundled with the latency work, because the funded
  owner had been **blocked for ~20 minutes** — `owner=blocked`, `equity=$0.00`
  at both producers — and a blocked owner cannot close its own book either. It
  came back `owner=healthy` with `equity=$333.29`, and the envelope now anchors
  `100.00 -> 331.81` instead of from an invented 2,500.

- **2026-08-08 (fourth pass) — The alert that pages you for hand-trading, and
  the end of the declared capital.**
  1. **A negative available margin was being treated as a broken wallet read.**
     `_snapshot_from_account` raised on `available < 0.0`, which failed the
     whole snapshot, blocked the owner, and fired a CRITICAL Telegram alert from
     the liveness watchdog. On the funded account it flapped every few minutes —
     `equity=340.37, available=-1.89` at 16:25, cleared 16:28, `available=-6.92`
     at 16:31 — because the owner hand-trades this account and a hand-opened
     position absorbing the wallet as position margin takes available below zero
     each time the mark moves. It is a true and ordinary reading. **The kernel
     already handles it exactly right**: `account_kernel.py:2365` refuses new
     risk on a negative available margin *but lets reductions through*. Two
     upstream guards — the snapshot builder and the health-record validator —
     made that state unreachable, so instead of "no new entries" the outcome was
     "owner blocked, cannot close its own book, page the operator". Both now
     pass the number through; only a nonpositive *equity* still fails the read,
     because nothing can be sized against it.
  2. **The declared capital reference is gone as an invented number.** The
     render defaulted to 2,500 USDT — pure scaffolding, since
     `capital_reference.mode = account_equity` makes the runtime reference track
     the wallet and every figure in the profile is a ratio of it. But the scale
     is live until the first wallet read, and 2,500 against an observed 355 is a
     7x envelope. The default is now the equity **floor** the owner already
     sets (100 USDT), so the declared number is no longer invented and the
     pre-read instant is the smallest envelope the runtime can hold rather than
     the largest. `configs/operational.mainnet.json` is regenerated: every cap
     scaled by exactly 1/25 with all ratios identical, and the host's own dials
     (carry 2.0, long 1.88, loss 0.25) re-render to the same shape — verified
     ratio by ratio against the live profile. Runtime behaviour after the first
     read is unchanged.

- **2026-08-08 15:28 UTC — Deployed `0a6c0e0`: the second and third audit
  passes reach the host.** `deploy_everything.command`, which is `deploy staged
  --profile operational --stop-first`. The fleet was down 15:26:52 → 15:28:53
  UTC, about two minutes, with the funded book covered only by its venue-side
  stops — the same posture as the 13:44 deploy, and for the same reason: a
  guarded `rollout` wants a flat account, and flattening would close live
  positions. Receipt `staged-ok commit=0a6c0e0 profile=operational`, then
  `verify-ok … mainnet=armed` with all nine units on/active/enabled, and no
  drift on a re-verify five minutes later.

  Everything came back clean. The funded owner logged **zero** error-level
  lines, resumed at journal sequence 4,699, rebased its envelope from the
  declared 2,500 reference down to observed equity (**359.96 USDT** — it tracks
  the wallet), and left the owner's two hand-placed ENAUSDT conditionals
  strictly alone, before and after the restart. Both funded producers
  bootstrapped their kline stores and cycle with `err=none` / `owner=healthy`.
  No loss-guard trip, no admission halt, nothing wedged
  (`wedged-command report` → `{"wedged": []}`).

  Two things seen on the way through that are **not** deploy damage. The carry
  producer reads `frozen=False` on its first cycle after any restart and `True`
  after — the day's decision is held in memory, so a restart pays one panel
  rebuild. And the funded carry sleeve still carries `stranded=1`: one standing
  reservation whose accepted quantity is zero. It is inert on every path but
  counts for admission, so that one name cannot be re-entered underneath its
  own unconverged target. It predates this deploy and no order command is
  wedged, so `ops.sh wedged-command resolve` — the remedy the code comment
  names — has nothing to act on. Owner's call.

  **Verifying the deploy found a real one, fixed but deliberately NOT
  redeployed.** The startup log rebases the envelope `2,500.00 -> 359.96` about
  ten seconds after the process starts. That 2,500 is the declared capital
  reference, and until the rebase lands the six absolute caps are ratios of it
  — a ~7x envelope against an observed 355 USDT. The rebase lives in the
  health-publish block (`account_service_runner.py:1479`), which runs *after*
  `run_ready_request_or_converge` (:1376) in the same first iteration, so a
  request already sitting in the inbox across a restart could be admitted
  against the declared reference at exactly the moment the queue drains. The
  bootstrap wallet is already read one line before the loop (:1218) and was
  simply going unused; the envelope is now anchored on it there. Verified by
  source ordering, not by a test — the loop has no seam to drive without
  building a harness, and a source-text ordering assertion is the trap that
  already let one broken invariant pass. **Not deployed:** the window only
  opens during owner startup, the running owner is past it and correctly
  anchored, so a restart today would buy nothing that the next restart does not
  get for free.

- **2026-08-08 (third pass) — Seven agents, and the sharpest one was pointed at
  the previous two hours of my own work.** Four adversarial/audit agents reading
  and four compaction agents editing disjoint directories. Net **−313 source
  lines** with 2,976 tests green. What it found that touches money:
  1. **A refused stop install was reported as success whenever the stop price
     contained the digits `34040`.** `set_trading_stop` scanned the whole error
     text for that code to recognise Bybit's "not modified" no-op — and the
     rendered error ends with the request body, stop price included. So a stop
     at `134040` (BTC) or `0.0034040` (an alt) turned **every** refusal of that
     install into a silent success. The caller then journals
     `status="active"`, clears the breach latch and blanks `last_error`:
     a naked position recorded as protected. The refusal that matters most —
     "the stop has already crossed the mark" — is exactly the one it swallowed.
     This is the same defect class fixed in `bybit_errors.py` earlier today;
     `set_trading_stop` and `set_leverage` were never converted. Both now
     anchor on the code's position, not its digits.
  2. **One ambiguous entry submission took convergence down for five minutes,
     every pass — including reduce-only exits for every other symbol.** The
     step-over used a 300-second age bound, but the driver refuses to resend an
     exposure command from the instant its submission attempt is journaled. For
     those five minutes `converge_once` returned on that plan and raised. That
     is the shape of the recorded nine-hour funded block. The predicate is now
     the union of the two conditions — a never-dispatched command still replays
     promptly, which is the case the step-over must not eat.
  3. **A protection stop/TP that reached `failed/` could never be republished,
     and took every later component's stop with it.** The retirement of a failed
     copy existed but sat *after* the immutability comparison, and a protection
     request keeps a stable id while rebuilding its body each pass with a fresh
     timestamp — so the comparison raised out of the engine's evaluate loop
     before the retirement could run. Retirement now happens first: a copy in
     `failed/` is not an in-force publication and its content promises nothing.
  4. **An accounting fault blocked exits.** Funding and position reconciliation
     shared one attempt, funding ran first, and the funding reconciler raises on
     every row it cannot account for. Position truth then stopped refreshing,
     and within 15s that is a stale-position error on the reduction gate — the
     admission check for *closing* a position. Its bookmark only advances on a
     clean pass, so it never cleared itself. The two now fail independently in
     one direction: funding can no longer stop position truth.
  5. **The account-wide health latch was cleared by evidence about a different
     symbol.** Any terminal status on any flat symbol blanked `last_error`,
     which is a single field written by several unrelated conditions and gates
     all new exposure. A live warning that some other symbol held an unverified
     stop was thrown away, and health passed. It now clears only a message its
     own symbol's flatness disproves.
  6. **The late-window entry escalation was structurally unreachable.** The
     amend budget shipped as 8 when a reprice was every 15s — exactly one
     window. When the cadence went to 3s the same 8 covered the first 24s of a
     120s window, so a quote whose touch moved early could never reach the
     urgency ladder (join at half the window, improve at 85%) that the same
     commit added and justified at −0.36 bp/entry over 199,785 paired attempts.
     The outcome was not a stranded order: it was a would-be maker fill
     degraded to a taker cross at the deadline. Past the join threshold the
     escalation now outranks the budget. **A change point, not a refactor.**
  7. **The leverage-cache invalidation added this morning was too eager.** It
     read "venue reported no leverage" as "venue contradicts the cache" — and
     Bybit blanks fields per margin mode (it did exactly that to the
     account-wide wallet totals on 2026-08-04). Every symbol would have been
     dropped on every 2s pass, handing back the 175 ms round trip the cache
     exists to avoid. It now distinguishes contradicted, unvouched-for, and
     no-evidence.
  8. **The morning's own halt had a hole, found before any agent reported it.**
     `halted_for_new_risk` ignored whether a batch was committed while the
     refusal exempted committed batches, so a halted owner could claim an
     unready head that nothing then refused. Worse than a wasted pass: an
     adversarial reproduction showed a committed *reducing* batch executing off
     a stale book, and a committed entry retiring to `failed/` after the 600s
     retry budget. The two predicates now agree.
  9. **Two storage faults in the morning's own union-schema fix.** The parallel
     footer read created one future per path — ~1.0 GB of transient objects on
     the 600k-part funding root, and `chunksize` is inert on a thread pool; it
     is batched now. And first-wins dtype merging meant one all-null part
     sorting first declared `Null` for the whole scan, silently dropping the
     read back to the 158s path with no signal.

  **Two agent recommendations were wrong and were rejected after checking.** The
  convergence fix was proposed as *replacing* the age predicate, which drops a
  behaviour an existing test pins (a never-dispatched command must replay
  promptly) — it had to be a union. And moving `quote.verified = True` after the
  verifier call would retry a raising verifier at `advance()`'s 10 Hz, a REST
  storm; reconciliation already re-covers that fill on its own cadence.

  Still open and deliberately not built, because they need a decision rather
  than a fix: the ceiling has no vote over **convergence** (a target accepted
  before the trip keeps being pursued), an unservable **exit** head still parks
  a queued flatten behind it (both are reducing the same book, so it delays
  rather than contradicts), and a halted refusal is visible only in journald.
  Measured but unfixed: the 2s reconciler makes three serial signed reads where
  one is a strict subset of another, ~26% of wall clock.

- **2026-08-08 (later) — The six items the audit passes named and left open.**
  All six closed, with the measurements that justify each.
  1. **The loss ceiling now refuses queued risk at admission.** Health going
     BLOCKED stops a producer *publishing*, but a cycle that published seconds
     before the trip already has its request in the queue, and nothing looked
     at the ceiling again before that request was filled. Admission now refuses
     any not-yet-committed request carrying a nonzero target while the ceiling
     is tripped — before it reads a book or a wallet — and completes it
     `disposition="halted"`. **This is a risk change, not a refactor**: on a
     tripped account an in-flight entry that used to fill is now dropped. Two
     exemptions, both deliberate: a batch already in the journal replays (its
     commands may be half-submitted at the venue), and a zero target is an
     exit, which the ceiling exists to encourage.
  2. **The ceiling's own all-flat can no longer park behind a head it cannot
     serve.** The flatten is an ordinary FIFO request, so an unservable head —
     a symbol with no healthy book, which on a delisted or unsubscribed name is
     forever — held it back indefinitely. A halted owner now claims that head
     anyway: refusing it reads no market data, so the queue drains and the
     flatten reaches the front.
  3. **One expiry no longer retires a symbol for the life of the journal.**
     The attempt key is a pure function of the target key, so the next cycle
     minted the identical key and suppressed itself — for ever, however fresh
     the decision behind it. The *rejected* half was bounded by its signal
     window in an earlier pass; the *expired* half could not use that bound,
     because an expiry is only ever recorded once the window has already
     passed. It is now scoped to the signal instant it expired on. All three
     sleeves align `signal_ts_ms` to a closed bar (carry to 00:00 UTC, LONG and
     CONTINUOUS to the kline), so the republished decision still matches and
     stays suppressed, and the next bar is free.
  4. **Leverage the owner changed by hand is no longer trusted from cache.**
     The adapter caches what it last sent, to save a ~175 ms round trip ahead
     of every entry — but this account has a second writer. Each reconcile pass
     now hands back the leverage every open position actually carries, and any
     cached value the venue does not confirm is dropped, including for a symbol
     that has gone flat. A re-entry into a flat symbol pays one `set_leverage`
     again; a scale-in into a confirmed position still does not.
  5. **The funding root reads in one scan again.** `funding_event_kind` and
     `source` were added in 2026-07, leaving 592,837 narrow parts and 7,804
     wide ones, and a mismatched scan fell back to reading all 600,641 files
     individually. Declaring the union of the on-disk schemas keeps it on one
     scan: **157.6s → 59.0s** for the collect, frames proved identical
     (same shape, dtypes, `equals()`), all 8 columns and all 37,475 non-null
     `funding_event_kind` rows preserved. End to end through `read_dataset`,
     including the 36s glob and a doomed first scan, **~229s → 131s**. A part
     whose column types genuinely conflict still falls back per file.
  6. **The stale local copy of the funded API key is deleted.** `deploy/.env`
     held the live mainnet key and secret in plaintext on the laptop since
     2026-08-05, and had drifted from the host anyway (carry 1.0 vs 2.0, long
     0.75 vs 1.88, daily loss 0.1 vs 0.25). The host file is authoritative and
     complete. **Rotation is still owed** — the key sat readable for three
     days — and only the owner can do it.

  Also corrected: the daily loss halt was described in
  `deploy/bybit-mainnet.env.template` and `docs/operations.md` as firing on
  **realised** loss. It reads `totalEquity` (or `totalWalletBalance +
  totalPerpUPL`), so an open position's paper loss has always counted. The
  docs understated the control's reach.

- **2026-08-08 — Two audit passes over the trading hot path, and the second
  one found six faults in the first one's own work.** An eight-agent
  adversarial review of the (then uncommitted) hot-path changes, with every
  load-bearing claim re-checked against source. What changed that touches
  money:
  1. **The available-margin gate now charges only a batch's increase, not the
     whole projected book.** The venue's free margin already nets out the open
     book, so charging the whole book against it counted the standing book
     twice and capped the account near half its equity — carry could not reach
     its own declared share. **This is a risk change, not a refactor: it
     roughly doubles reachable exposure**, from about equity/2 of initial
     margin to the profile's declared `max_initial_margin_usdt`. The absolute
     ceiling and the per-sleeve partition still bind. Recorded here as the
     change point.
  2. **The daily loss ceiling closes the book, and no longer latches after
     closing part of it.** It used to call a path that could only claim a
     native-stop breach, so it logged critical, latched, and closed nothing.
     It now publishes the same zero targets the operator flatten publishes,
     and re-plans every pass while tripped — a strategy reads owner health
     once per cycle and then spends minutes fetching data, so a cycle
     straddling the trip could otherwise re-open exposure that nothing ever
     closed.
  3. **A definite venue refusal is no longer read as "try again later".** The
     transient classifier scanned the whole error text for a bare code digit
     run, and the text ends with the entire order body — so an "insufficient
     balance" refusal on a stop price of `100025.5` came back retryable.
     Anchored on the venue's own `ErrCode:` rendering.
  4. **A stale price could reach a decision.** The hourly-window hole check
     tested the interior and the head but never the tail, and one bar stamped
     past the window vouched for a window whose newest price was an hour old.
     The staleness metric clamped negative lag to zero, reporting "fresh" at
     exactly the moment the host clock was behind the venue. Both fixed.
  5. **One unreadable protection fraction no longer stops stop-loss and
     take-profit evaluation for every other position** — isolated per
     component, the way the venue reconciler already isolates per symbol.
  6. **The owner loop is no longer starved by its own recovery reads.**
     Pending-order confirmation was two signed REST calls per order per pass;
     one entry resting for its quote window paid 120 round trips. Gated and
     capped, least-recently-polled first. A dropped fill is recovered up to
     ten seconds later, with the every-pass position-truth check behind it.
  Also: the entry cross retry is paced like its neighbouring cancel (it ran at
  the owner's ~10 Hz tick, ~200 signed calls inside one 20 s window); a full
  disk on the notification write no longer crash-loops the owner; and the
  funded `.env.example` documented 17 real-money dials when only four exist —
  copying it, as the file invites, hard-failed the arming preflight.

- **2026-08-07 — Hand-trading the funded account no longer stops the fleet;
  the ACEUSDT wedge is fixed at the cause.** The owner bought ACEUSDT by hand
  at 00:26 UTC on the same account the bot trades, and closed the whole lot by
  hand at 04:46 UTC. The funded account owner was **blocked from 00:26 to the
  deploy — about nine hours** — and paged at 07:43. Five separate faults,
  each fixed:
  1. **The book counted only its own 283.6, so nine of the eleven sell
     executions from the hand-placed close were each larger than it and were
     refused outright.** The book was left holding 175.2 ACE against a flat
     venue, forever. A venue reduction bigger than the book is now booked down
     to flat and the remainder recorded as foreign, with the fee split by
     quantity.
  2. **One venue order was adopted as two commands.** The synthetic command id
     mixed in the protection key, which legitimately changes between two
     executions of the same order — the first adoption moves the protection to
     a reduction status. The hand-placed close split across two commands, both
     left part-filled and working forever. The venue order id alone is the
     identity now.
  3. **The wedge probe asked for those orders by client id**, which an adopted
     external order never has, so the venue answered "absent" for an order it
     plainly held as `Filled` — and absent is the one classification that
     needs authorization. It probes by venue order id now.
  4. **A reduce-only wedge refused to terminalize while the venue showed more
     filled than the book had booked.** Once the book is flat there is no
     reduction left to lose and the excess is foreign, so the refusal is
     skipped in exactly that case. The standard is otherwise unchanged: a live
     order, an unreadable venue, or a real unbooked reduction still refuse.
  5. **Mainnet only classified wedges and never resolved them**, so the wedge
     sat until someone noticed. Both realms now resolve on the same evidence
     ladder. `scripts/ops.sh wedged-command` also grew `--environment
     demo|mainnet`; it was hardwired to demo, so there had been no operator
     command for this at all.

  **Policy change, owner's decision 2026-08-07: the bot and the owner keep
  separate books on one venue account.** Venue exposure above what the bot
  owns, and venue orders it did not place, are recorded and left strictly
  alone instead of blocking. The reverse — the bot claiming exposure the venue
  does not hold — still blocks, and now self-heals. **The known weak point is
  the safety stop**: the venue takes one stop per coin, sized to the merged
  position, so the bot's stop would close a hand-placed position too.

  **Same-day correction to this entry.** The first cut also skipped the
  protection sweep for any symbol carrying foreign exposure. That was wrong
  and was reverted the same morning: a skipped symbol stops having
  `last_sync_ns_by_symbol` advanced, ages out, and makes
  `require_recent_healthy` raise `native protection health is stale` — the
  same account block returning through the back door. Under the owner's
  standing workflow (scale the bot's coin by hand shortly after it enters) it
  would have fired on **every scaled position**. The skip is for a book that
  cannot be trusted; here the book is right, it is simply not the whole venue
  position, and a Full-position stop plan is a price with no quantity of its
  own. Now pinned by a test that models the workflow directly.

  Rehearsed against a copy of the live funded journal before deploying: the
  book converges 175.2 → 0, both wedges terminalize on `terminal` evidence,
  no working orders remain, report healthy. Deployed `a67e035` 09:38 UTC;
  the account recovered on the first pass at 09:39:26–29 and has logged zero
  errors since.

  **Correction (same day).** A first revision of this entry and of STATE.md
  said the funded account took no trade on 2026-08-07. Wrong: the carry sleeve
  opened **ACEUSDT long 283.6 @ 0.11327 at 00:21 UTC** (command `43e6bc00`),
  five minutes *before* the hand-placed buy blocked it. That position was
  closed at 04:46 as part of the owner's hand-placed close rather than on its
  own terms; realised from fills **+4.32 USDT**, fees 0.048.

  **Found while checking the above, and fixed the same day: funding was booked
  whole, not by share.** Bybit settles funding on its netted position, and
  `BybitAccountFundingReconciler` booked the whole SETTLEMENT row as the bot's
  P&L. The 04:00 UTC ACEUSDT settlement credited the bot **+10.72 USDT** when
  it owned 4.08% of that position — ≈+0.44 earned, ≈+10.28 not its own. No
  real money moves wrongly; the bot's *record* was overstated, and **funding
  is the entire thesis of the carry sleeve**, so a wrong share inflates the
  measured edge directly.

  Each settlement is now scaled by
  `owned_qty_at_settlement / venue_settled_size`. The owned quantity is
  reconstructed from this book's own fills **at the settlement instant**:
  reading the current position would have booked zero here, because the
  position was closed at 04:46 and the settlement not re-read until 09:39. The
  venue's raw numbers are kept verbatim in the event metadata and are what the
  immutability re-check now compares against — getting that wrong would raise
  on every later pass and block the account, so it is pinned by its own test.
  **The share is 1.0 whenever the venue position is the bot's own, so this is
  an identity on an account nobody else trades.**

  Rehearsed on a copy of the live journal: the five pre-existing whole-booked
  settlements re-verify cleanly (0 re-recorded, no raise), and the real ACE row
  re-identified as new books +0.43693 against the venue's +10.71918 on a 0.0408
  share — 283.6 of 6,957.5, reconstructed from the journal at 04:00 UTC.

  **Not restated:** the five settlements already on the funded journal, +15.23
  USDT in total, were booked whole. Funded P&L before 2026-08-07 is overstated
  by roughly the ACEUSDT +10.28.

  **This is a recurrence.** The 2026-08-06 entry below reads the same
  mechanism on HOMEUSDT as "cleared on their own, as designed". That was too
  generous: it cleared only because that hand-placed close happened to be no
  larger than the book. Same cause, worse draw.

- **2026-08-06 (19:24 UTC) — The size fixes and the doubled carry dial are
  DEPLOYED; the funded account is flat, healthy, and sized off the new
  dials.** One `deploy staged --profile operational --stop-first` from the
  primary checkout carried both undeployed batches — the 2026-08-05 friction
  fixes and the same-day entry-size fixes — from `8be7461` to `aa6f793`.
  Receipts: `staged-ok commit=aa6f793 profile=operational`, `verify-ok …
  mainnet=armed`, all nine units on/active/enabled; preflight `profile
  matches dials` (leverage 3.91919, gross 3.91919× equity, carry 2.00×,
  long 1.88×); the restarted funded producer logs `notional_x=2.0
  leverage=3.9` under `strategy_profile=v4` and files under the
  version-free `carry_hold` journal id. The owner's hand-opened HOMEUSDT
  position was closed before the deploy, and both refusals it had caused
  cleared on their own with no operator action — the controls behaved
  exactly as designed. Funded equity reads **160.75 USDT** against 99.94 on
  2026-08-05; the rise accompanies the hand-traded position and its cause is
  not independently confirmed here. At this equity each carry name sizes to
  ≈32 USDT. **2026-08-06 was a cash day on the funded account**: the size
  floor blanked the 00:20 UTC decision, and the hand position then held the
  owner blocked past the ~05:50 UTC signal expiry, so the fixes first bite
  at the 00:20 UTC cycle.
- **2026-08-06 (research, Lane-1, seen data — no config changed) — A much
  deeper entry bar was measured for the first time and it LOSES; the
  registered thresholds stand.** Owner question: enter only below −0.30%
  per settlement, else exit. Swept enter ∈ {−0.10, −0.20, −0.30, −0.50}%
  against exit ∈ {−0.03, −0.10, same-as-enter} on the full panel with the
  measured 7.78 bp/side fee and the registered scorer; the control cell
  reproduces v3's registered 19.83 bp/day and Sharpe 1.38 exactly, so the
  harness is sound. Every deeper cell is worse and the degradation is
  monotonic: the owner's rule scores 11.22 bp/day, Sharpe 0.99, worst dip
  −48.7% against the control's 19.83 / 1.38 / −28.7%, and the paired daily
  differential is **−8.60 bp/day at t −2.46** over 1,894 shared days,
  negative in all six eras. Two mechanisms, both already visible elsewhere
  in the evidence: the −0.10…−0.30% band carries the bulk of the earning
  name-days (flat days rise 36% → 62%), and prints deeper than −0.30%
  cluster in cascades, so the worst dip nearly doubles on a *smaller* book.
  Dropping the hysteresis band (enter = exit) is separately worse at every
  depth. Added to the do-not-retest list in
  [research_findings.md](docs/research/research_findings.md).
- **2026-08-06 (night) — The funded book missed its entries by six cents: a
  10-dollar size floor silently skipped every ~$10 name, and a hand-opened
  HOME position then wedged the owner. Floor fixed, skip made visible, carry
  dial doubled.** At 00:20 UTC both realms picked the same two carry names.
  Demo (equity $1,427) entered both within three minutes; the funded account
  (equity $99.94) entered nothing, with `suppressed=0 err=none` — each name
  sized to 0.1 × 99.94 = **9.994 USDT**, six cents under the producer's
  `ENTRY_MIN_NOTIONAL_USDT = 10.0`, and the skip counter
  (`entry_dust_skips`) was recorded in the payload but never rendered in the
  heartbeat line. Three fixes: (1) the floor drops to **6.0** — the venue's
  own floor is 5 USDT per order and the kernel already enforces the exact
  per-symbol rules (min qty, min notional, step rounding), so 10.0 was
  double-counted safety that blanked a small account; (2) the heartbeat now
  prints `dust=N` whenever entries are skipped as too small; (3) the wallet
  fault message carries the numbers (`equity=…, available=…`). Owner
  instruction "bigger trade size": `RM_CARRY_LEVERAGE` **1.0 → 2.0** in the
  host env (each carry name ~0.2 × equity ≈ $20; derived venue margin
  leverage rises to ≈3.9× of the 10× ceiling — on a fixed wallet a bigger
  book *is* more leverage, the two cannot move apart; LONG stays at the
  owner's 1.88 ≈ $10 per entry). Separately, at 00:33:57 UTC a hand-opened
  HOMEUSDT position (56,980 units) with venue TP/SL wedged the running
  owner, correctly: reconcile refuses unowned exposure
  (`HOMEUSDT:venue=56980:reconstructed=0`), and the wallet snapshot's
  available margin went negative under the position's isolated-margin lock.
  Both blocks self-clear once the position is closed and its conditional
  order cancelled. The day's entry signal stays publishable until ~05:50 UTC
  (6 h validity minus the 15-min guard). Also corrected: the host
  `bybit-mainnet.env` was ALREADY converted to the four new dials (read
  directly; the 2026-08-05 STATE warning was stale — the 11:38 UTC deploy of
  `8be7461` could not have armed otherwise).
- **2026-08-05 (evening) — The friction audit lands: carry versions become a
  dial, the carry journal id stops lying, and the misnamed LONG size dial is
  renamed (committed, not yet deployed — the next owner redeploy carries it).**
  The owner audited the sleeve-logic reference, named the confusions, and
  ordered the root causes fixed. (1) CARRY version selection is now
  `CARRY_STRATEGY_PROFILE=v3|v4` in the unit env → `--strategy-profile`,
  exactly LONG's dial shape — switching versions was previously a code commit
  editing a constant. (2) The carry journal filing id is the version-free
  `carry_hold`; it had been frozen as `carry_hold_v3` while the sleeve ran
  v4. A standing component keeps the id it was born with: planning reads
  both ids, exits and resizes publish under each component's own id, new
  entries file under `carry_hold`, and one symbol standing under two ids
  fails closed — tests pin the drain. Decisions are unchanged; this is
  bookkeeping identity, not strategy. (3) `max_order_notional_pct_equity` →
  `order_notional_pct_equity` everywhere, committed profile JSONs included
  (renamed atomically, the strict loader refuses a mixed pair): the dial
  SETS each LONG entry's equity fraction, replacing the sizing chain, and
  the old "max" name read as a cap. (4) The CONTINUOUS dataclass defaults
  now equal the profile resolver's values with a pinned identity test —
  reading the dataclass used to give seven wrong numbers. Deliberate
  non-change: LONG's dataclass leverage/multiplier defaults stay as they
  are (a bare config at leverage 2 would trip the 50% margin boot guard;
  the operational profile remains the only runtime sizing surface).
- **2026-08-05 (afternoon) — The funded account is back on CROSS margin
  (owner instruction, executed via API on the flat account).** Tuesday's
  hand-trading had left it in isolated margin — the very mode that blanks
  the account-wide wallet totals; the switch to `REGULAR_MARGIN` was
  accepted and the totals repopulated immediately, confirming the
  2026-08-04 diagnosis (the coin-row fallback in `48ebc50` stays as a
  dormant net). Position mode is one-way and MUST stay one-way: the fleet
  places every order and stop with `positionIdx 0` and the protection
  layer refuses nonzero-index rows, so enabling the venue's hedge mode
  would reject every fleet order. No startup check pins either mode —
  proposed, owner to decide.
- **2026-08-05 (midday) — The real-money dial surface collapsed to four dials
  (owner instruction: "just a leverage dial per sleeve, keep the daily loss
  and some protection").** `RM_CARRY_LEVERAGE` (1.0) and `RM_LONG_LEVERAGE`
  (0.75) are each sleeve's book ceiling as a multiple of equity, worst case
  included (each carry name = a tenth of its dial; each LONG entry ≈ its
  dial / 18.75); `RM_DAILY_LOSS_FRACTION` (0.1) and
  `RM_CARRY_STOP_LOSS_FRACTION` (0.35) stay. Everything else the old
  surface exposed is derived and still proved at render; the defaults
  reproduce the previous effective sizing exactly (carry multiplier 1.0,
  LONG 0.4), so nothing trades differently until a dial moves. A retired
  `RM_*` line is refused BY NAME at render — **the host's
  `bybit-mainnet.env` still carries the old dials, so the next
  render/activation will refuse until those lines are replaced with the
  new four** (the local `deploy/.env` staging copy is already converted).
  Committed profile regenerated (account gross cap is now the derived
  1.7677x, was a slack 2.0x; sleeve caps unchanged in effect).
- **2026-08-05 (10:18 UTC) — The owner's re-run deploy landed and the whole
  fleet is green on `f85371e`; one more disk-full scar surfaced and was
  repaired in the same pass.** The staged deploy installed and activated
  everything, but its verify phase failed on the two demo producers: their
  strategy event tapes ended in a partial line (the append that was running
  when the disk hit 0 bytes), and the loader refuses a torn tape. Repair:
  each tape backed up beside itself (`strategy_event_tape.jsonl.enospc-20260805.bak`),
  the never-completed tail dropped (LONG 1 byte, CARRY 450 bytes), both
  chains re-validated through the repo loader (1,645 / 1,809 events), and
  the units' own auto-restart brought them up at 10:18:30. Receipts:
  `verify-ok commit=f85371e requested=f85371e profile=operational
  mainnet=armed`, all nine units on/active/enabled; mainnet owner health
  `healthy` with equity 99.94 USDT read through the coin-row fallback
  (`48ebc50`'s first live proof — the venue still blanks the account-wide
  totals); demo watchdog 10:19 UTC sent "✅ cleared" for its last standing
  alert. The compressed quote-lab tape stands at 2.5 GB, disk 35%.
- **2026-08-05 (early) — The overnight activation failed on a hand-placed
  position, the disk then filled to zero and killed the morning redeploy,
  and both are resolved; the fleet is STOPPED awaiting the owner's re-run.**
  Last night's activate (20:46 UTC) died at the startup ownership gate,
  correctly: the account carried a hand-opened HYPEUSDT long (246.44 HYPE
  ≈ 13.7k USDT notional against 385 USDT equity, ~35× leverage) whose
  venue TP/SL orders the journal does not own; the owner unit crash-looped
  31 times overnight (each pass: refuse → readiness timeout → restart) —
  the source of the night's pages. By morning the position was gone
  (equity 385.51 → 99.95 USDT, account flat, zero open orders), so that
  gate now passes. The 04:18 UTC redeploy then failed at staged-install
  because the disk hit 100%/0 bytes: the two quote-lab capture processes
  had sat below their 6 GB min-free bound crash-looping since ~15:00,
  spraying tracebacks into their nohup logs (tape-a.log 2.5 GB +
  tape-b.log 5.1 GB) — the guard stops tape writes, not the process's own
  log spam (defect flagged for a fix). Repair, same morning: both capture
  processes killed, the two spam logs deleted (their tails were
  unsalvageable at 0 bytes free), and the fully-replayed tape
  (`tape-night/`, 20 GB, days 08-03/08-04 — the sweep and OOS model runs
  completed 2026-08-04 ~15:50) is compressing in place to zstd in the
  background (~2 GB when done; decompress before any re-replay). Disk 100%
  → 69% and falling. The failed deploy left every unit stopped, demo
  included, watchdog timers too — the quiesce ran, the install died, so
  nothing restarted and nothing is paging. Host checkout stands at
  `cc66c0e` (carries the wallet-reader fix `48ebc50`). Remaining act, the
  owner's: re-run the one-click deploy; expect the mainnet owner to report
  the true ~100 USDT equity and the envelope/loss controls to speak to the
  1417 → 100 collapse if their anchors reach back past the emptying.
- **2026-08-04 (afternoon, late) — The funded account was emptied through the
  venue's own website, the wallet reader broke on the account's new payload
  shape, and the fix is committed (`48ebc50`, not yet deployed — the next
  owner redeploy carries it).** Three CRITICAL pages (11:44–13:46 UTC) said
  the mainnet owner was blocked on "totalMarginBalance is missing/non-numeric"
  with equity going stale. Two separate things happened. (1) At ~11:42 UTC
  Bybit began blanking every account-wide margin total in the funded account's
  wallet response (a documented unified-account margin-mode behavior; the
  per-coin USDT row stays populated) — the snapshot parser only knew the
  account-wide fields, so every capital refresh crashed. It now reads the
  coin row when the totals are blank, charges unrealized losses but never
  counts gains, and still fails closed naming what was blank when nothing
  numeric remains. (2) The venue's own transaction log shows the money left
  by hand: 11:48 UTC −999.2 USDT transfer out + a 999 USDT on-chain
  withdrawal to BSC `0x23d3…1250` in the same second (the address that
  received 3,940.99 USDT from this account on Aug 2, before the fleet was
  funded), then 13:11 UTC the remaining 419.27 USDT self-transferred out,
  leaving 0.00002922 USDT. The API key holds no transfer/withdraw
  permission (probed: refused), so this was the account login, i.e. the
  owner's own hand — **owner to confirm; if these withdrawals are not
  yours, treat the venue login as compromised immediately.** Until the
  redeploy, the mainnet owner keeps paging hourly; after it, the owner will
  truthfully report the ~0.04 USDT equity and the envelope/loss controls
  react as designed. The 14:39 UTC disk warnings (80%, both fleets) were
  ~1.7 GB of stale CI temp dirs, two old comparator tarballs, and the pip
  cache — deleted, disk 80% → 75% (9.0 GB free). The remaining growth is
  the quote-lab tape (17 GB, ~1 GB/h while its two capture processes run;
  they self-stop at 6 GB free, and two replay jobs were actively reading
  the tape, so it was left untouched — owner decision: stop the capture,
  compress finished days, or accept the warning returning within ~a day).
- **2026-08-04 (evening) — The resting-entry recipe was upgraded from the
  quote-forge lab's full-night replay (third execution change point; not
  yet deployed — next owner redeploy carries it).** Entries now place by
  the displayed touch sizes (improve into the spread when the book leans
  toward the entry, rest one tick behind when it leans hard against, join
  otherwise), never rest behind the touch past half the window, improve
  near the end, and cross early when the mid has run against the entry past
  twice the half-spread-plus-taker-fee; the 15 s staleness reprice is gone
  (chasing a retreating market surrendered queue position for nothing). The
  quote manager now reads the owner's own reconstructed book (free, carries
  sizes) instead of REST tickers. Selected on 199,785 queue-honest paired
  replay attempts over the full overnight tape: −0.36 bp/entry vs the
  shipped recipe (t −11.1), deadline crosses halved, and the churn
  alternatives measured worse — evidence in
  `docs/research/research_findings.md` §1, change point in
  `strategy_program.md`, the lab itself at `~/Desktop/quote-forge`. Demo
  probes there also proved the demo realm's matching engine holds phantom
  internal liquidity (post-only at the published touch dies ~80% there), so
  demo fill numbers overstate nothing for this change — grading stays with
  funded `is_maker` receipts.

- **2026-08-04 (afternoon) — Owner's one-click redeploy landed; whole fleet
  on `544bee0` since 10:59 UTC** (all units restarted together: both carry
  producers, both LONG producers, both account owners, Telegram controls).
  This carries the kline tail-fetch fix, the envelope boundary tolerance,
  and the fully live-tested entry slicing onto the funded account ahead of
  tonight's 00:20 decision. Separately, an **execution lab (quote-forge)**
  now runs beside the repo — a standalone project (owner directive) probing
  cheaper entry recipes with real demo orders and queue-honest replays of
  recorded books; its evidence lives in `~/Desktop/quote-forge/FINDINGS.md`
  (Mac) and `/root/quote-forge/runs/` (VPS). Nothing in the fleet changes
  until a recipe wins there and an integration is separately approved. One
  finding matters to fleet evidence directly: the demo realm's matching
  engine holds internal liquidity its published book does not show, so demo
  fill rates and maker shares overstate reality — the first honest
  `is_maker` grade still comes from the funded account's own receipts.

- **2026-08-04 (midday, later) — The slicing was tested LIVE on the demo
  account and three real defects were found and fixed in the loop (owner:
  "test it live on the demo and tweak it live").** Two controlled entries
  through the demo owner's own inbox (1,000 USDT on 1000XECUSDT, 500 USDT
  on ZESTUSDT, carry sleeve idle that day): both arrived as sequences of
  floor-sized windows (10 and 5), every window clip-capped with its stop,
  both converged, both exited clean. Found live, fixed, pushed: (1) a
  fully-filled clip never terminated its command — the stream and the
  kernel both waited for fills to reach the COMMANDED quantity, parking
  window one forever (`0de55a1`); (2) the health exemption was gated on a
  working order, turning off in exactly the between-windows gap it exists
  to cover — the owner flickered blocked at every hand-over and the
  watchdog missed a page by seconds (`939dc47`; the health line now shows
  `attempts=since-fill/limit:total=N`); (3) the journaled market-input
  event omitted the displayed touch sizes the clip is cut from
  (`713f153`). Demo units run this code since 10:50 UTC (hand-staged;
  mainnet processes untouched on `6cb159a` until the owner's redeploy).
  Honest limits: demo fills simulate without queue position, so fill rates
  and fees here are not evidence, and no window happened to run to its
  120 s cross live — that path stays covered by tests and the overnight
  lab only.
- **2026-08-04 (midday) — Big entries arrive as touch-sized windows (owner:
  "prepare for big sizing, up to 5,000 USDT notional").** A resting entry
  now rests at most the quantity already displayed at the touch (bid size
  for a buy, ask size for a sell; floor 100 USDT per window), the command
  terminates its window with the shortfall un-ordered, and convergence
  plans the next window — with two supporting changes that make the loop
  first-class: a convergence retry that made progress (any fill since the
  last attempt, ordered by journal sequence) no longer spends the
  3-attempt retry budget or grows the backoff, and a finished window's
  quote state survives until its probe horizon so the health exemption
  covers the seconds between windows. Each window carries its own attached
  stop and journals `entry_clip_qty` beside the commanded quantity. Deep
  books are untouched (no cap when the touch absorbs the command); the
  market path, exits, and resizes-down are unchanged. Dials
  `--entry-clip-touch-fraction` (0 disables) and
  `--entry-clip-min-notional-usdt`. Motivating measurement (depth tape,
  22 symbols): the whole displayed touch on the thin half of the universe
  is 23–181 USDT — a 5,000 USDT order would be 30–200× the queue it
  joins. Ungraded at size until real receipts.
- **2026-08-04 (morning) — First funded night: no trades (legitimately), two
  faults found and fixed, quote recipe confirmed by the full night fit.**
  The 00:20 UTC carry decision failed for 42 minutes on both fleets
  ("decision bar carries 6 universe symbols") and healed itself at 01:00;
  the recovered decision was an empty book — cash — and demo decided
  identically on a healthy 100-symbol universe, so the funded account
  held no positions and the first maker-share receipts wait for the first
  non-empty book. Root cause: `get_klines` treats its end as exclusive
  while two callers pass inclusive bar-open windows, so the newest closed
  hourly bar could never be fetched by REST — the cycle's tail fetch
  returned zero rows every cycle (`kline_fetch_symbols=105`,
  `kline_fetched_rows=0`) and the kline-store bootstrap could never fill
  its newest target bar (`failed=36` on every restart). Normally the WS
  stream's own confirm covers the tail invisibly; the 23:50 redeploy
  restart left store holes the reader refuses to serve, and at 00:20 the
  daily decision needed exactly the bar REST could not supply. Both call
  sites fixed (+1 bar at the boundary), regression-tested both ways
  (`event_demo_data.py`, `kline_stream_manager.py`). Second fault the same
  night: the funded owner paged CRITICAL at 00:03 refusing a one-cent
  equity rebase — the mainnet dials pin the gross cap at exactly
  reference × max leverage, and the rescale's floating-point rounding sat
  a hair above the strict bound (~1 cent value in 10 fails); the envelope
  re-proof now carries the same micro-USDT tolerance the other checks
  already had (`operational_profile.py`). The overnight quote lab
  completed all eight segments; the full fit (n=12,656) validates the
  Sell side and keeps the shipped 15s/120s recipe unchanged —
  `docs/research/research_findings.md` §1 has the per-arm table.
- **2026-08-04 (early) — Entries rest at the touch instead of crossing the
  spread (owner instruction, first funded night; money landed in the Unified
  Trading account the same hour).** Both account owners now create an
  exposure-increasing entry as a GTC limit at the touch (same single order per
  command, same `orderLinkId`, stop attached at create), and the owner loop
  advances it: reprice toward a moved touch every 15s, amend through the far
  touch at the 120s window end (a taker fill at a bounded price, unlike a
  market order), cancel an uncleared remainder after 20s and let convergence
  re-plan it, verify the attached stop at fill instead of at create
  (`entry_quote_manager.py`; `--entry-quote-window-seconds`, 0 restores
  market orders). Exits, resizes, and venue-native stops stay market-path.
  Thin spread (< 2 ticks or < 1 bp), missing tick rules, or any venue reject
  of the limit create falls back to the market order. The convergence health
  grace reads an in-window resting quote as intentional
  (`resting_quote_active`); past its window it ages and pages exactly as
  before. Recipe = the overnight quote lab's first completed arm, measured
  the same night (70.4% passive fill, n=1,586, median fill 41.6s, median
  all-in 1.9 bp vs 7.78 taker — `docs/research/research_findings.md` §1);
  the full overnight fit may retune it in the morning. Change point recorded
  in `docs/research/strategy_program.md`. Deploy: requires the owner's own
  stop/activate of the funded units (platform rule: Claude never starts
  them) — receipt to follow that act.
- **2026-08-03 (late) — Arming path collapsed to two owner acts (operator
  override), and the quote lab ships.** The nine-step real-money runbook is
  now: write `/etc/liquidity-migration/bybit-mainnet.env` by hand (key,
  secret, dials, `REAL_MONEY=true`), then `deploy --execute activate`.
  Activation itself installs the static route env, normalizes perms,
  defaults a missing Telegram pair from the demo file, **always re-renders
  the risk profile from the current dials**, freezes universe/rules when
  absent, creates state roots, and still gates on the full preflight —
  every capital control (loss halt, envelope, native stops, partition,
  single-writer lease, reconciliation) unchanged. `REAL_MONEY` in the
  root-owned host file remains the single arming switch; no agent handles
  the live key. Quote lab: `b7ecca4`+`44a26cb` (registration is the
  commit), two real-order windows run 2026-08-03 evening on the fleet
  account in staged-install pauses (book flat both sides, receipts in
  `/var/lib/liquidity-migration/quote-lab/`), and an all-night policy-
  rotating run is live on the second, separate demo account
  (`bybit-quote-lab.env`) beside the untouched fleet.
- **2026-08-03 — The audit's whole program lands: decode gate, journal
  decoupling, day buckets, owner diet, continuous-runtime removal.** Owner
  directive: fix the ranked findings from audit pass 2, agents doing the
  grunt work. Seven commits (`8f3cb18`…`580d4e8`), gate green at 2,829
  tests, all read-side — no journal byte, no capital-preservation control,
  and no strategy decision changed, with one bounded exception named below.
  - **WS pre-decode gate** (`f377046`): raw-frame substring gates below
    pybit drop unconfirmed kline ticks and sample ticker deltas at one per
    symbol per 5s (snapshots always pass; both gates fail open). **Change
    point: WS decision prices (mark/last) may now age up to 5s** where the
    60s REST cache replacement already bounded them — the one
    strategy-adjacent effect in the program. Liveness stamps on the drop
    path only; a seam test pins pybit's `_on_message` so an upgrade fails a
    test instead of silently costing the fleet ~42% of a core.
  - **Journal ↔ watchdog decoupling** (`ab485e3`): venue snapshots were
    already change-triggered; the heartbeat floor rises 30s → 10min (~2,880
    → ~144 segments/day flat), and the sub-minute venue-fact liveness proof
    moves to owner health as `venue_facts_at_ns` (schema v3), stamped from
    the reconciler's own report. This closes a real hole: an owner whose
    venue reads failed forever kept publishing healthy, and mainnet had no
    venue-fact freshness check at all. Every detection bound tightens or
    holds. Owner restarts first at deploy; the watchdog's startup grace
    covers the schema window.
  - **Day-bucketed cycle ledgers** (`55fc6bc`): the per-append rewrite drops
    ~30x (month → day parts); `carry_hold_mainnet_cycles` is registered so
    an armed mainnet can never write an unbounded monolith; a latent
    `since_date` reader bug (non-date partitions silently dropped) is fixed;
    `scripts/maintain/migrate_cycle_ledger_buckets.py` re-parts a live root
    with row-count + content-digest proof.
  - **Owner diet** (`1e76f2b`, `6ebdf5f`): notifier skips identical state
    writes (~173k no-op fsyncs/day gone) and gates its 1Hz journal copy on
    the head; protection anchors memoized on
    `(rolling_state_hash, events_applied)`; rejected entry attempts join the
    planning cursor's memo family; the settled-funding REST query gates at
    60s + hour boundaries under the untouched 24h overlap (~43k → ~1.4k
    calls/day, worst case a 60s discovery delay, never a miss); venue-order
    and target-proposal acceleration indexes kill the O(orders-ever) ack
    scan and the quadratic replay term; the convergence walk collapses to
    one pass. Deferred by design: gating per-order REST recovery on WS gap
    detection — the one non-equivalent transformation; measure first.
  - **Continuous runtime removed** (`8f3cb18`, `580d4e8`): five units + four
    launchers deleted (hedge book verified flat first), deploy/watchdog/
    reset threads excised, the watchdog cooldown state re-anchored to repo
    data (it had been resurrecting the retired sleeve's directory every 3
    minutes), display labels tell the truth (v4 book, `CARRY_STRATEGY_ID`
    stays `carry_hold_v3` on purpose — it is the frozen journal key the
    standing book is filed under), the mainnet owner can no longer latch
    permanently failed (`StartLimitIntervalSec=0`), and the dead
    delta-neutral probe script is gone. Research surfaces stay.
  - **VPS cleanup (same day, before the code program):** ~2.3GB of retired
    data deleted — paper roots (~869MB), the CONT demo root (327MB), dead
    `depth`/`liquidations` collectors (142MB), and the unreferenced
    `cutover-evidence` debris (981MB, dominated by a nested copy of a
    v8-era reset archive). Disk 36% → 30%. The reset archives and
    `retired-authority` stay as deliberate evidence retention.
- **2026-08-03 — WS kline store made to actually serve; audit pass 2 filed.**
  The kline plane deployed at `a1058e9` streamed but never served a cycle:
  carry's reader window ended one bar in the future, so the store's coverage
  probe could never pass (`kline_store_rows=0` live — and that gauge was
  itself a hardcoded 0), and LONG's store retained 90 days against a 100-day
  window, serving 4 of 120 symbols. Fixed in `a52b35e` (window end passed
  unmodified, store retention = lookback+1, real gauge, real-store tests) and
  deployed 17:50 UTC staged install+activate, `verify-ok`. First post-deploy
  cycles: carry store rows 0 → 231,020 (98% of the window), LONG 5,985 →
  193,263 (82 symbols), zero REST kline rows; remaining names converge as
  hourly refreshes backfill heads the old retention never kept, and the
  cache-skip fast path engages per sleeve at 100% coverage. Decision inputs
  unchanged — the close-keyed view already cut at the decision bar. This
  corrects the `9fb64c1` entry's "store serves" attribution below (its other
  numbers stand). The owner-requested second-pass audit — measured CPU by
  thread, storage growth, latency chains, ranked findings, nothing else
  changed — was written up as `docs/audit/2026-08-03-latency-architecture-audit.md`
  (removed in the 2026-08-14 cleanup: audit reports are not kept as standing
  files — findings live in the topic docs and this log, the full text in Git
  history); headline: each producer burns ~21% of a core decoding WS messages it
  discards, the journal grows ~2,880 snapshot files/day at zero trading by
  design coupling to the watchdog, and cycle ledgers rewrite their whole
  month partition every 60 s.

- **2026-08-03 — Telegram control buttons deployed (owner request).** Deployed
  `3a319b3` via `ops.sh deploy rollout`, `rollout-ok` 17:37 UTC, `verify-ok …
  mainnet=off`, now twelve units in expected states. A new always-on daemon
  (`liquidity-migration-telegram-controls.service`, the bot's only
  `getUpdates` consumer) serves buttons in the main chat: `/controls` shows
  Pause / Resume / Close-all per environment; real-money rows appear only
  while the mainnet owner is active. Pause = sleeve toggles off in the host
  override (verbatim copy saved) + resolve + producer units stopped — the
  owner, protections, and watchdog keep running, and the pause survives
  reboots and deploys. Close = two-tap confirm (120 s expiry), pause first,
  then the standard flatten path. Verified end-to-end on the host 17:37–17:38
  UTC: pause stopped carry+long and left the owner active; resume restored
  `sleeves.env` **byte-identical** (matching md5) and brought both producers
  back; watchdog "0 active alert(s)"; startup dropped 2 stale queued updates
  as designed; the panel message was posted to the main chat. First rollout
  needed a same-day fix (`3a319b3`): the pre-install verification runs against
  the outgoing topology, so a unit new in the deployed commit is only checked
  where its unit file exists. Group chats refuse presses until
  `TELEGRAM_CONTROL_USER_IDS` is set (docs/notifications.md §Owner control
  buttons).
- **2026-08-03 — CARRY promoted to `lane2_carry_hold_v4` (owner override).**
  Deployed `95497d1` via `ops.sh deploy rollout`, `rollout-ok` 17:06 UTC,
  `verify-ok … mainnet=off`, all eleven units in expected states. The change
  point is visible in the persisted cycle journal: 17:03:32 UTC row
  `strategy_profile=carry_hold_v3_live_v1` (desired book 2 names, gross
  0.143) → 17:07:17 UTC row `carry_hold_v4_live_v1` (1 name, gross 0.055 —
  v4's persistence cut acting on its first decision). `strategy_id` stays
  `carry_hold_v3` on purpose: a frozen journal lineage key, documented at the
  constant. Standing book was 0 (same-day clean-slate epoch), so no migration
  diffs were needed; today's desired entry publishes at the next 00:00 UTC
  decision because the 6h entry-validity window had already closed — registered
  behavior, not a fault. Watchdog 17:07 UTC: "0 active alert(s)". Promotion
  note (with the honest caveat: **0 forward-scored days at promotion**; v3
  keeps scoring as comparator) in `docs/research/strategy_program.md`.
  **Mainnet remains disarmed** — when the owner sets `REAL_MONEY=true` in the
  host's `bybit-mainnet.env` and runs activate, the funded CARRY trades v4
  through this same code path (preflight still gates).
- **2026-08-03 — Latency/efficiency program (owner priority): WebSocket-first
  market data, incremental hot-path state, watchdog slimmed.** Measured
  before-numbers on the aged epoch, same day: the watchdog burned 22–28 s CPU
  (peak 61 s, 430 MB) per 3-minute run re-verifying the whole journal chain;
  carry burned ~24 CPU-seconds per 60 s cycle (~40% of a core, REST-only,
  plus a full-universe ticker stream whose data the cycle discarded); the
  fresh-epoch watchdog floor is ~1.0 s. Changes, all deployed together:
  (1) **Carry now streams its 1h klines** like LONG — own `KlineStreamManager`
  (top-150 by turnover, store spans the 90-day replay window + 2), REST only
  for gaps; the ticker stream it already paid for now serves its universe
  ranking through the shared cache; the hourly settled-funding sweep stays
  REST (the venue has no stream for it) but runs on a worker pool with one
  persistent session instead of a fresh TLS handshake per cycle. Same bars,
  same close keys, same daily decision on the same 60 s grid — only the
  transport and cost changed (change point for forward grading all the same).
  (2) **Watchdog reads a bounded tail** (`read_recent_account_events`, newest
  512 transactions) instead of a genesis replay, reads producer cycle datasets
  column-projected and lock-free (it no longer takes the producers' write
  locks or plants lock files in observed roots), and runs at `Nice=10` so the
  observer stops preempting the sleeves. Target: ~1 s per run regardless of
  epoch age.
  (3) **Remaining O(journal-age) folds fixed**: per-cycle projection and trade
  rows are memoized on the digest head (rebuilt only when a new journal event
  arrives), the inbox's `completed/` directory is read through a resumable
  cursor (was: re-parse every completed request ever, every cycle, under the
  inbox lock), the retirement-flatness check reuses the cycle's cursor instead of a
  cold full read, the shared target-capture tape verifies only appended bytes
  (was: re-parse the whole file per cycle under the interprocess lock), the
  notification poll takes a tail slice instead of scanning all events every
  second, event tapes append O(1), and journal filename validation is
  prefix-cached. Deliberately NOT changed: the owner reconcile's per-2 s
  events copy (a view would race the funding index; the copy is milliseconds)
  and carry's replay-from-scratch discipline (registered-rule semantics; its
  ~40%-core panel rebuild is measured and reported, owner to decide).
  Carry demo `MemoryMax` 1152M→1408M for the in-memory store (mainnet unit
  mirrored). Full gate green (2763 tests).
  **Deployed and measured after** (`a1058e9` + bootstrap-workers plumbing
  `3b15ba5`, staged install+activate 16:35–16:41 UTC over the live book):
  watchdog **1.01–1.04 s CPU per run** (was 22–28 s on the aged epoch);
  carry **~16.4 CPU-s per 60 s cycle** (was ~24) on an exact 60 s cadence
  (was slipping), **zero REST kline rows on mid-hour cycles** and a single
  1-bar-per-symbol top-up + funding sweep at each hour boundary; carry's WS
  store bootstrapped 150 symbols / 296,665 bars in 38.9 s and flushes within
  seconds of each bar close; carry RSS 823 M under the 1408 M cap, host
  2.8 G available. Carry's remaining burn is the registered rule's
  replay-from-scratch panel rebuild (documented above, owner's call).
  Known pre-existing wart surfaced while verifying (not from this change):
  on a restart with an intact store, the kline bootstrap re-fetches the
  window it already holds and logs the run as `failed=N` because zero new
  inserts count as failure — bounded (~40–50 s per restart), tracked as a
  follow-up. The main Telegram line now
  carries only the book's story in plain words (digest, fills, closes, stops,
  loss warnings, entry blocks); accounting boilerplate and component
  bookkeeping moved to the owner's service journal. Watchdog pages moved to a
  second chat line — `TELEGRAM_ALERT_CHAT_ID`, same bot, plain one-line
  headline plus a stable `ref <key>` to hand to Claude; full technical detail
  stays on the watchdog's journald. Wired live the same afternoon: the owner
  created the "liquidity-migration" Telegram group with the existing bot, and
  `TELEGRAM_ALERT_CHAT_ID=-5503250433` is set in the host's
  `bybit-demo.env` (delivery test-confirmed; the watchdog re-reads env every
  3-min fire, no restart needed). A separate
  `@liquidity_migration_alerts_bot` exists but is parked — using it would
  need per-channel token support and a deploy. Deployed at `4152d3b`:
  `rollout-ok commit=4152d3b profile=operational` 14:37 UTC, book proved flat
  at every rollout phase. Immediately after, the **clean-slate ledger reset**
  ran as the first production use of the Python reset tool (`3f52edd`): all
  three sleeve roots + account journal/inbox/capture + reports + caches
  archived to `data/_archive/ledger-reset-20260803T143852Z.tar.gz` (sha256
  `4b729c34…937a`), demo venue-flat verified at the boundary, fresh empty
  epoch, owner-first restart, pre-reset active set restored and verified.
  First digest of the new epoch delivered 14:41 UTC in the new format
  (`🕐 Bybit demo · 14:41 UTC`, 1 page, 105 chars). All equity/P&L numbers
  before this boundary belong to the archived epoch.

- **2026-08-03 — two owner-ordered follow-ups to the purge, deployed the same
  afternoon.** (1) **The account-owner lease slimmed to its load-bearing core**
  (`1c8d32c`, rollout-ok 12:53 UTC, first watchdog run after it "0 active
  alert(s)"): the ~900-line filesystem provenance chain and the reset script's
  10-field receipt plumbing are gone; what stays is one kernel flock per
  authenticated Bybit account, the credential binding, and the
  deleted/replaced-lock-file check — the live owner was verified holding the
  slim lock on the host (fresh contender refused). (2) **Arming real money is
  one switch** (`3d5462e`): `REAL_MONEY=true` in
  `/etc/liquidity-migration/bybit-mainnet.env`, set by the owner's own hand
  next to the live API key, is the whole arming decision. The mainnet sleeve
  toggles (`CARRY_MAINNET_SLEEVE`/`LONG_MAINNET_SLEEVE`), the
  `activate-mainnet` mode, and the repo-edit-then-install dance are deleted; a
  plain `activate`/`rollout` starts the funded fleet when armed (state roots +
  preflight still gate the start; the installed risk profile decides sleeve
  shares), and setting the switch false makes a stop survive the next
  activate — the stop-mainnet persistence hole is closed. Real money has no
  repo toggle: a git commit can never arm. `docs/real_money.md` deleted on the
  same order; its envelope, dials, runbook, preflight contract, hazards list,
  and ramp live in `docs/operations.md` §Real money. **Deploy receipt:**
  `rollout-ok commit=3d5462e profile=operational` 13:21 UTC, verify table
  all-expected with the new `mainnet=off` field (the switch read disarmed from
  the absent credential file), account proved flat mid-rollout (positions 0,
  targets 0, working orders 0), `sleeves.resolved.env` regenerated without the
  mainnet keys, first post-deploy watchdog run 13:23 UTC **"0 active
  alert(s)"**.

- **2026-08-03 — the de-friction purge deployed: every non-critical operator
  ritual removed (owner instruction), live at `6d366fe` since 12:11 UTC in one
  rollout together with the paper retirement and the memory retune below.**
  What changed operationally: a wedged order command now terminalizes itself
  on demo — the account owner's ~2s reconcile pass probes any command past
  the 300s wedge bound and resolves it on the same venue-evidence ladder the
  CLI uses (live orders and unreduced fills always refuse; mainnet only
  surfaces the wedge in health, the transition stays an operator act). An
  inbox head request retries at most 10 minutes before retiring to `failed/`.
  `wedged-command` lost its intent-typing flags (`--operator`/`--reason`
  optional, never-submitted needs no absent authorization, `resolve --all`
  sweeps). The demo owner unit lost the ExecStartPost readiness gate and
  MemoryHigh; producers went `Requires=`→`Wants=`; the hedge lost its owner
  edge; the watchdog first fires 1 minute after enable, cooldown 60, alerts
  on enabled-but-inactive units, and honors a per-check startup grace.
  Deploy is one command (`ops.sh deploy staged|rollout`, EXPECTED_COMMIT
  optional, auto-stop on a no-mainnet fleet, venue-flat proof advisory off
  mainnet, no stopped-window lint/tests — CI on main is the gate). Registered
  startup ceilings (demo-rule age, warmup timeout, INVOCATION_ID, stray-order
  gate) bind mainnet only. Mainnet gates and the four capital controls are
  unchanged (the producer wrappers' kernel guard survives as a direct env
  read). **Deploy receipt:** `rollout-ok commit=6d366fe profile=operational`;
  verify table all-expected (demo owner + LONG + CARRY + demo-liveness on,
  CONTINUOUS off, all four mainnet units off); **zero paper unit files on the
  host**, `/etc/liquidity-migration/account-paper-execution*` removed,
  `sleeves.resolved.env` 0600 root:root, the designed "retired sleeve toggle
  ignored: CONTINUOUS_PAPER_SLEEVE" warning observed once; memory shape live
  (owner MemoryHigh=infinity MemoryMax=1G MemorySwapMax=384M RestartSec=5;
  carry Max=1152M, long Max=1024M, both MemoryHigh-free; vm.swappiness=20);
  demo rules re-probed in-rollout (509 symbols, refresh-due-past-half-life);
  owner digest delivered 12:12 UTC; first post-deploy watchdog run 12:16 UTC:
  **"0 active alert(s)"** after sending resolved notes for the last two
  demo-paper agreement warnings. Deliberately not done (flagged, not lost):
  symbol-scoped entry gating, the owner-lease provenance chain, persistent
  `stop-mainnet`, the mainnet owner's own MemoryHigh=384M throttle (owner
  decisions), and the pre-push hook's git-fixture tests corrupting the real
  repo when run from a linked worktree (repaired same day; hermeticity fix is
  a spawned task — until it lands, push only from the primary checkout).

- **2026-08-03 — demo fleet memory retune (owner-approved), spending the
  ~740 MiB the paper retirement frees on the same 3.7 GiB host. Rides the
  same deploy as the retirement below.** Measured before the change: carry
  and long producers pinned exactly at their MemoryHigh watermarks (800M/805M
  and 603M/604M) with 850 MiB swapped host-wide and the owner pinned at its
  256M swap cap — silent reclaim throttling, the mechanism behind slow
  cycles. Producers drop MemoryHigh entirely (kill-and-restart at MemoryMax
  is loud and recovers off the journal cursor; throttling is quiet and
  persisted for weeks): carry MemoryMax 896M→1152M, long 640M→1024M, owner
  MemorySwapMax 256M→384M. Deploy also installs `vm.swappiness=20` and
  tightens the journald cap 1G→500M. Re-enabling CONTINUOUS requires a fleet
  re-budget (noted in the rmom-refresh unit).

- **2026-08-03 — paper trading retired whole (owner instruction). Deployed
  12:11 UTC in the combined rollout; the host receipt (zero paper units,
  `/etc` paper config removed, resolved sleeves root-only) is in the purge
  entry above.** One
  deliberate removal: the paper owner, all three paper producers, the target
  mirror, the paper sleeves.env toggles, the `demo-operational` deploy profile,
  the demo-paper watchdog scope, paper Telegram, `PAPER_EQUITY_USDT`
  provisioning, the follower market-data mode, and the docs web. Demo (real
  venue, simulated fills) is the only practice book; mainnet is unchanged
  (wired, off). The next deploy's manifest install removes the five paper
  units from the host (`lm_cleanup_unknown_liqmig_units`), deletes the
  deploy-generated `/etc/liquidity-migration/account-paper-execution*` config,
  and normalizes `sleeves.resolved.env` to root-only. Paper journals and state
  roots stay on disk as history; nothing reads or routes to them, which also
  closes the TLMUSDT wedge and the demo/paper agreement warnings as
  operational concerns. A stale host `sleeves.env` carrying the retired
  toggles is warned about and ignored, not fatal. Rationale (assessment
  2026-08-03): paper was `integration_only_uncalibrated` routing evidence with
  zero performance weight, its one live research use (the passive-exec A/B)
  was dormant at 2/8 fills behind a retired sleeve, the real-money path never
  referenced it, and it produced a disproportionate share of the month's
  incidents.
- **2026-08-03 — Two-day fleet outage root-caused and repaired; both Telegram
  channels delivering again.** One busy minute (2026-08-01 00:20 UTC) broke
  both books independently. Demo: a LONG entry batch chunked 1000XECUSDT into
  nine slices sharing one journal timestamp; the first slices each spent ~4s
  in venue stop verification, the 5-second unsubmitted-exposure budget then
  refused every later slice forever, and the queue-head request retried every
  ~10s for two days while start-post readiness never passed. Paper: the target
  mirror (root) left `arrival_counter.json` and three arrival sidecars
  root-owned 0600 inside the paper-owned inbox on its first production
  publish, and the paper owner crash-looped on "unreadable arrival sequence".
  The 2026-08-02 06:37 unattended-upgrades userspace restart then left every
  producer down (`Requires=` on owners that never came back), which is what
  the watchdog's 12 standing alerts were reporting. Repair: thirteen
  never-submitted commands terminalized through `wedged-command` on
  per-command venue probes (all absent, zero fills, zero venue orders); paper
  roots re-owned with `reset_path_safety normalize-paper`. Change points
  deployed with this entry: the unsubmitted-exposure age is anchored to the
  shared batch journal instant, so the default budget is now 120s and the
  owner takes `--max-unsubmitted-exposure-age-seconds`
  (`bybit_execution_adapter.py`, was a hard-coded 5s); the account inbox
  writer hands every inode — request body, arrival sidecar, arrival counter —
  to the inbox owner when running privileged (`_atomic_replace`), replacing
  the mirror's one-file chown hook; `scripts/ops.sh wedged-command` now owns
  the account root/id/realm and sources the demo credentials remotely
  (probe/resolve could previously not run through it at all); demo owner
  memory raised to high 768M / max 1024M after it ran throttled at its old
  384M ceiling through the recovery. BANKUSDT's two working exits were expected
  to clear as the books converged; the demo/paper agreement warnings became
  moot with the same-day paper retirement.

- **2026-08-03 — stale entry requests now retire terminally (owner-approved
  follow-up to the outage).** The Aug-1 loop's request half: a failed entry
  request whose every intent is past its own `signal_valid_until_ms` AND whose
  failure is the never-attempted stale-command refusal now moves to `failed/`
  (`StaleEntryRequestExpired`, original cause chained) instead of bouncing
  pending↔failed forever. Exits never expire; attempted batches still resume
  past expiry so possibly-live venue state reconciles (the crash-resume
  contract is pinned by test); never-attempted commands the batch journaled
  remain `ops.sh wedged-command` scope, named in the failure record. Surfaced
  and explicitly approved ("nothing expires a stale pending inbox request —
  do it"). **The adjacent control is deliberately NOT built**: nothing bounds
  the owner's convergence toward a stale *accepted* target while producers
  are down — that is a liveness-coupled trading halt needing owner design,
  re-surfaced in the session report.
- **2026-08-03 — LONG sleeve switched to `LongV12WideStop` (v12) on demo;
  mainnet wiring updated, still unarmed. Receipt: live since the recovery
  activation at `6df3329` (~09:34 UTC, verify-ok, fleet green — reported by
  the recovery-deploy session; `6df3329` contains `4a4da11` v12 and `5af6bda`
  expiry).** Owner instruction: "wire v12 into
  the live systems, paper, demo, live" (the paper leg was overtaken hours
  later by the same-day paper retirement above). The registration (2026-08-01,
  `f04ccdc`) recorded that v12 was not deployable by a profile flip; this
  change builds that path: entries freeze a per-trade stop-decay contract in
  their target metadata (`stop_decay_after_ms`, `decayed_stop_loss_pct` =
  1.5 × signal-day ATR) beside the wide 3×-ATR `stop_loss_pct`, and
  `_plan_time_stop_exits` publishes a `decayed_stop_loss` zero target when a
  filled position is past the decay age with live price at or below
  `entry_fill × (1 − decayed_stop_loss_pct)`. The venue-native wide stop is
  armed from entry and never revised. Profile selection is explicit end-to-end
  (`LONG_STRATEGY_PROFILE=v12` in the LONG units → `--strategy-profile`
  → `long_v12_profile()`; unknown values fail startup). LONG planning now
  reads **both** registered identities, so v11a components open at the switch
  keep exits, capacity, and cooldown history, drain under their own published
  terms (≤3-day hold), and exit targets stay keyed under each trade's own
  identity. New entries publish under `long_native_v12_wide_stop`. Owner-side
  kernel, risk envelope, and sizing are untouched (same signal, same sizing;
  only stop geometry changed). **Mainnet: wiring only** — the unit names v12
  but `LONG_MAINNET_SLEEVE=off`, `REAL_MONEY` unset, no credential exists;
  arming remains the owner's separate act. Change point recorded in
  `docs/research/strategy_program.md` §2026-08-03; mechanism in `docs/trading_logic.md`.



## 2026-08-14 — the four gaps closed, and two more faults out of them

Deployed `a4bb8b88`. Everything STATE.md listed as not done this morning is
done except the two that are the owner's to decide, and both of those are
written down with their reason rather than left implied.

**The engine says what it holds.** Its heartbeat carries `positions`, by name,
from the same venue reading the equity comes from. LONG uses it in three
states: confirmed held, mark it seen; confirmed before and not now, something
closed it that this producer never asked for, so it leaves the book and starts
its cooldown; never confirmed, leave alone — that is an entry still on its way.
A fourth sits above them: the engine saying *nothing* is not "holds nothing",
so `positions` is always present and sometimes empty, never absent. It
publishes the venue's per-symbol reading rather than per-strategy attribution
on purpose — attribution starts empty on a fresh log, so a restart would report
every position closed and every producer would drop its whole book at once.

**Flatten is back**, and restoring it found a fault. An empty book did not
close everything: with no targets the follower's candidate names collapsed to
the seed list from `engine.toml`, two symbols a sleeve, and the seed is
explicitly not a ceiling — the normal steady state is positions in names the
config never mentioned. Every one of those was left standing by the single
instruction whose whole meaning is "hold nothing". The plug now also looks at
what it held last time round. Flatten itself writes explicit zero rows naming
everything the engine reports, stops the producers first, and clears LONG's
record so a restart cannot republish what it just closed.

**The funded engine can run, and it was calling itself demo.** Setting it up
and watching one shadow run found `account_identity` hardcoding the demo realm
— left from when this crate reached the practice account and nothing else. The
funded engine published `realm: "demo"` beside the funded account's own user
id. The mainnet producers compare that against their own environment and would
have blocked every entry; the single-writer lease is named by it, so a live
funded engine would have taken a demo-realm lease. Fixed and confirmed live:
the funded engine now reports `realm: "mainnet"` on account 552445993.

It is left in shadow and has never sent an order. Two switches stand between
it and live, both the owner's. It is left running rather than stopped because
stopped does not stick — the deploy starts it wherever its env file and binary
both exist — so shadow, which cannot trade, is the honest resting state.

Both realms' candidate-universe artifacts on the host were schema 4 against
code that reads schema 5, which stopped every producer cycle before it decided
anything. Migrated offline, both unchanged symbol for symbol (demo 510, mainnet
512), and the env files repointed. This was pre-existing and unrelated to the
engine; it surfaced because the producers finally got far enough to hit it.

Left alone, with the reason: demo has no per-sleeve partition, and drawing one
from a profile whose capital reference is 250,000 against a $1,400 account
would produce a control that never binds. Retuning that reference is a dial the
owner sets.

## 2026-08-14 — the engine owns the demo account, live

Deployed `4c914435` by `staged --profile operational --stop-first`; the engine
is live on the fleet's demo account (555899665) and holds its single-writer
lease. The Python order path is gone from the host as well as the repository.

Four faults were found and fixed getting there, three of which only a live run
could have surfaced.

**A stop that fired was undone by the next quote.** The book said hold the
name, the venue stop closed it, the book still said hold it, so the follower
bought it straight back at full size seconds later. This applied to carry too,
the sleeve that was called ready. A name that goes flat under a book that
still wants it is now latched and left alone until the producer stops asking;
the latch deliberately survives a new book, because LONG rewrites the same
decision every sixty seconds.

**Each follower read the other sleeve's position as its own.** The venue holds
one position per symbol and its reading carries no strategy. A carry follower
whose book did not name a symbol LONG held would send a full-size reduce-only
and close it. `attribution.rs` now sums filled quantity per strategy and
symbol from the log — the order says who sent it, the fill says which order —
and rebuilds it at boot. A name another strategy holds is skipped wholesale,
not shared pro-rata, because the venue's stop belongs to the position and two
sleeves on one name would have one stop between them.

**The account journal was frozen, not empty.** Every producer learned what it
held by reading the journal the deleted owner wrote. The file is still on
disk, so a producer reading it believes it holds whatever the owner last
wrote, for ever. `long_book_state.py` is LONG's own memory of what it asked
for, shaped to read back as the table the journal produced so the entry
screen, exit planner and cooldown are unchanged.

**The heartbeat's account stamp was on the engine's monotonic clock.** Found
in the first cycle after the producers came up: a healthy engine six seconds
old published `account_observed_ns: 5530327701`, the producers compared it
against the wall clock, and both sleeves blocked every entry as fifty-six
thousand years stale. The age crosses now, rendered as a wall stamp beside
`wall_ts_ms`. Nothing caught it because both halves were tested against
themselves; both now have a test that says which clock it is.

Verified live: producers size from the engine's equity (`equity=$1,412.58
err=none`, `owner=healthy`), both write books, the engine routes each to its
own sleeve and takes on symbols the books name that no config listed. In
shadow the log records `wants strategy 0 Sell 14110 [book-exit]` and `allowed
… for 14110`. Live, carry wants $141.09 of HOMEUSDT against ~$137 held and the
engine does nothing, correctly — $4.09 is inside the 5% dead band.

Also on the way: `start_mainnet_fleet` still ran `systemctl enable` on the
deleted Python mainnet owner, which would have failed the strict activate
phase on any host with real money armed. It names the mainnet engine now,
guarded, and stops the retired unit. The demo engine unit moved from its own
second demo account to the fleet's, because nothing else writes there any
more. A schema-4 candidate-universe artifact on the host was migrated to
schema 5 (510 symbols in, 510 out, unchanged).

Not done: the funded engine is not running — `engine-mainnet.env` does not
exist and writing it is an owner act. `operational.demo.json` has no
`sleeve_limits`, so demo has no per-sleeve partition. LONG still cannot see a
stop that fires.

## Earlier entries

Entries dated before 2026-08-01 live in
[docs/archive/CHANGELOG-2026-07.md](docs/archive/CHANGELOG-2026-07.md) —
same format, same reading rule (newest first, each entry as it was written on
its day).
