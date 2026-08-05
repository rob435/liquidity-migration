# Operational State

Current operational snapshot. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.** Every observation below is
point-in-time and dated; none of it is a claim about the account right now.

This file records what is deployed and what constrains it — not the history of
how it got there. That history is in Git.

## Deployment

> **Reading the entries below.** The operational-authority receipt was removed
> from the repository on 2026-07-31 (`c396d87`…`f5a37b7`, ~5.1k lines) by owner
> override: it gated every unit start on a clean-checkout hash and changed
> nothing about what a process could trade. `scripts/ops.sh
> operational-authority` and the `ConditionPathExists` gate on all 14 units are
> gone; the installed profile is now a plain `/etc/liquidity-migration/profile`
> marker. **Entries dated before that describe the tooling as it was and are
> accurate history — they are not runnable instructions.** Deployed
> 2026-07-31 in `cdb6e61`.

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
  changed — is
  [docs/audit/2026-08-03-latency-architecture-audit.md](docs/audit/2026-08-03-latency-architecture-audit.md);
  headline: each producer burns ~21% of a core decoding WS messages it
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
  Detail: [`docs/research/carry_hold.md`](docs/research/carry_hold.md) §0.1.
- **2026-07-31 — the program significance bar is now t ≥ 2.5**, owner decision,
  replacing the family-wise ≈3.25/3.58. Authority is
  [`docs/research/governance.md`](docs/research/governance.md) §2; `screen_phase1.py` and
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

## Topology

Since the 2026-08-03 paper retirement (pending its deploy receipt above): three
persistent services plus one active timer — the demo owner, the LONG and CARRY
demo producers, and the demo liveness timer. The hedge and residual-momentum
timers remain installed but disabled with the retired CONTINUOUS sleeve, and
the CONTINUOUS producer unit is installed-but-off. The mainnet owner, mainnet
producers, and mainnet liveness timer are installed and off.

| Kind | Units |
| --- | --- |
| Account owner | demo |
| Target producers | demo × LONG/CARRY |
| Timers | demo liveness (active) |

- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.

## Risk envelope

The account owner caps leverage at **2×**, symbol notional at **125,000 USDT**,
component/account gross at **500,000 USDT**, and initial margin at **250,000
USDT**, against a capital reference of **250,000 USDT** (the 25× profile
deployed 2026-07-27 ~14:03 UTC). The bound operational profile retains 2× entry
leverage, a 0.5 LONG notional multiplier, and (2026-07-29) a **1.0 CARRY
multiplier** — per-name 0.10 and gross cap 1.0 come from the registered rule,
so the CARRY book tops out at 1.0× the capital reference, unlevered; the
retired CONTINUOUS block is shrunk to minimum envelope. Startup and
authorization reject unknown profile fields, producer leverage above the owner
cap, or registered exposure envelopes outside the same profile.

LONG, CONTINUOUS, and hedge leverage plus exposure/risk knobs come from one
strict operational profile; independent systemd sizing variables were removed.

## Standing operational constraints

- **Rollout requires a locally and directly venue-flat account.** No
  flatten/cancel bypass is authorized. A failed verification is not permission to
  hand-start a partial fleet.
- **Demo rule receipt freshness is a side effect of deploying, not a deadline.**
  A rollout past half the age bound re-probes; only mainnet holds the registered
  168-hour ceiling, while demo takes any positive `--max-demo-rule-age-hours`.
  The watchdog warns in the final 24 hours but refreshes nothing itself.
- **Unknown safety-critical state fails closed.**
- Three CONTINUOUS candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`) have
  venue `deliveryTime=1784538000000`. They are recorded prospectively in private
  mode-0600 retirement registries and may retire only while account positions,
  targets, orders, and inbox exposure are all flat.
- Push remains CI-only. The manual GitHub workflow exposes four of the six deploy
  modes — `rollout`, `install`, `activate`, `verify` — with explicit profile, task
  reference and demo authorization inputs
  (`.github/workflows/vps-deploy.yml`). There is no `recover` mode and no
  reset-receipt input; both were removed with the receipts. The two mainnet modes
  are deliberately absent from CI ([`docs/operations.md`](docs/operations.md)).

## Forward evidence stream

**The prospective runtime-parity epoch machinery was deleted on 2026-07-24 by
owner instruction** — the comparator, the epoch-start collector
(`forward_epoch_start`), and all eight registered contracts. Its published start
and verification receipts still exist on disk and on the VPS, but nothing in this
checkout reads, validates, or can reproduce them.

What remains is the plain rolling record, which is what the Progressive Evidence
Model in `docs/research/governance.md` actually calls for: each committed config is graded
on the run of days it predates, continuously, with recorded change points. There
is no ceremony, no waiting window, and no separate registration artifact — the
commit is the registration.

### Change point — 2026-07-31: cross-fleet comparison, and what it invalidates

**Cross-fleet P&L comparison before 2026-07-31 is invalid. Do not quote a
demo-vs-paper difference from any earlier data as an execution result.** Two
independent reasons, both measured:

1. **The two fleets decided off different data.** Paper's producers were
   read-only followers polling the demo producers' data stores on their own
   grid. Over 1,633 live paper cycles the funding cache matched demo's 94.2% of
   the time and the kline cache 82.4%. On 2026-07-29 that opened and closed a
   `TLMUSDT` position demo never asked for, for −70.73 USDT.
2. **The two books are on different price bases.** Demo's carry targets were
   re-stamped through the 2026-07-30 resize churn (ending 13:36:24) and paper's
   still carry their entry stamp, so the fleets hold 9–17% different quantities
   of the same three symbols while their published notionals agree to 0.005%.
   This does not self-heal; it clears when the book is next rebuilt.

Reason (1) was closed by the target mirror (one fleet decides, both execute);
reason (2) was closed by the 2026-07-31 flatten of both books. The whole
comparison lane ended with the 2026-08-03 paper retirement — the cross-fleet
invalidation above stands as the reason no pre-2026-07-31 demo-vs-paper number
may ever be quoted, and no post-retirement one exists.

## Evidence boundary

The tracked hedge history is an **immutable sizing-only model prior through
2026-07-09**. It is not live-extended calibration or performance evidence.

The forward execution stream accumulates rolling evidence continuously under the
Progressive Evidence Model. At this snapshot **no committed config has a graded
forward record**, and this is not a strategy-alpha experiment. Promotion is a
five-line note plus a recorded change point when a rolling record earns it.

**Real money remains a separate owner door: no runtime status, paper result, or
rolling record opens it on its own.**

Research-only: Strategy Overhaul V2 closed with no qualifying thesis and did not
touch its reserved holdout; its diagnostic portfolios are model-based and
negative after costs/funding. The consolidated research conclusion and successor
direction are in `docs/research/strategy_program.md`; current anomaly evidence is in
`docs/research/archive/2026-07-24-anomaly-research.md`. Retired receipts remain in Git history.

## Known benign alert shapes

Each was diagnosed to a root cause and fixed. Listed so an operator does not
re-diagnose a page that has already been explained.

| Alert shape | Diagnosed cause |
| --- | --- |
| `unowned_venue_order` after a stop triggers | Owner disowning its own just-consumed Full stop while Bybit's open-order cache still lists it. Bounded 10-minute terminal-visibility grace, identity-evidence required. |
| `account funding reconciliation is stale: age_ns~4-5e9` | Report timestamped itself before its paginated REST queries, then held to the shared 4-second position bound. Documented 30-second funding floor. |
| `waiting for queue-head market data: X:stale_book` | Lost/rejected orderbook subscribe. Socket rebuilds after 30 frameless seconds for a new subscription. |
| `latest cycle is 0.1 min future-dated` / `future_book` | Local read/update races sampling wall time before the snapshot. Ordering fixed; true future timestamps still page. |
| `continuous-hedge.service FAILED` after an owner-health page | Duplicate of the owner-health root cause. A hedge run blocked by unhealthy owner health now exits 0 with a blocked receipt. |
| Negative owner-health ages | Strategy event time reused after concurrent heartbeats. Operational freshness now samples adjacent wall time. |
| `account execution live L2 is N min stale` (~1.5×/day, 3–8 min) | Root-caused 2026-07-27: venue-side quiet subscription on the owner's single-topic BTCUSDT book feed — socket stays up and answers pings (20s/10s keepalive active), Bybit stops pushing frames. Not the host (kernel clean, producers' busy 508-symbol feeds unaffected through every episode), not scheduled (14 episodes over Jul 19–27 spread across the whole clock), not load (32% of all 20-min windows have a >100 s LONG cycle; only 3/13 episodes do). A single stall self-heals in ~2.5 min via the 120 s internal watchdog and never alerts; the alerted episodes are rebuilds that came up quiet again, stretched by the old per-attempt clock reset (fixed in `d11db79`+`7af59f3`, **deployed 2026-07-27 ~14:03 UTC**; both owners logged `raw Bybit public stream connected generation=1` at first start — record Bybit's verbatim close/error codes here from the transport logs on the next episode). Fails closed; zero trades lost. If quiet-stalls persist post-deploy, next lever is a second heartbeat topic or proactive resubscribe. |
| `unadopted external execution: external protection fill is not position-reducing` (2026-07-27 10:47:20, once) | A manual ~10 USDT spot BTCUSDT buy made in the demo account UI at 10:47:20.106 (venue execution `1784799743630817527`), three minutes before the owner's 250k top-up transfers (+90k 10:49:40, +100k 10:52:20, +50k 10:52:26). The kernel manages linear perps only and was flat, so it correctly refused adoption, surfaced the error once, and health returned to green. The ~0.000153 BTC sits in the unified wallet outside the managed book; no reconciliation drift. |

## Open operational defects

Carried forward from the 2026-07-30 demo/paper reconciliation (a lane that
ended with the paper retirement; the demo findings stand). Decision logic is
verified faithful — an independent replay reproduces the live book exactly — so
every item here is operational, not strategy.

| Item | State |
| --- | --- |
| Remediation for the journal re-projection, paper funding snapshot, per-bar decision freeze, and stranded zero-quantity reservations | **Deployed** in `cdb6e61`. The cursor is 138× faster on the steady-state planning read at 3.3× lower peak memory, outputs identical on the real journal |
| ~~Paper CARRY has no target source~~ | **Resolved 2026-07-31 in `0506cef`**, and the diagnosis recorded here was wrong: it was never a filesystem boundary problem. The runner called `ensure_account_route`, an owner-side initializer that requires the manifests to belong to the running process; the mirror is not an owner. It now uses `require_account_route` with the paper owner's uid named explicitly. The unit is active and both books started flat. `CARRY_PAPER_SLEEVE` remains **not** the fallback: it reinstates the raced read it was retired for |
| The LONG demo producer is SIGKILLed by every stop | It drains its current cycle on SIGTERM, but a cycle runs ~180–350s against the unit's 180s `TimeoutStopSec`, so systemd kills it and the unit ends `failed`. Harmless for a deploy (`require_quiescent` accepts `failed`, and targets publish atomically), but it means no LONG stop is ever graceful |
| ~~Paper `TLMUSDT` reservation, wedged since 2026-07-29 03:45~~ | **Closed by the 2026-08-03 paper retirement**: the paper owner no longer runs, so the wedged reservation is inert history in a journal nothing reads |
| Reported P&L is provisional | 166 of 187 `pnl` events carry `funding_status=pending_venue_reconciliation`; every figure is fill-reconstructed, not venue-confirmed. No closed-loop accounting check yet, which real money needs |
| Sizing was not clamped to the capital reference in the deployed build | The clamp shipped in `cdb6e61` (`policy/equity_anchored_envelope.py`, present on the host). The pre-deploy observation stands as history — live sizing anchored at 255,357.40 against a 250,000 reference (+2.14%) — and that it now binds at runtime is unobserved until the next resize |
| Entries execute ~23 minutes after the price the scorer models | Live runs the delayed-entry stress case, not the bar-close headline case. Recorded with the measured capacity numbers in `docs/research/carry_hold.md` |
| Intraday notional tracking is bounded, not continuous | Deliberately left as an owner decision; `docs/research/carry_hold.md` §7.5 states it rather than treating it as settled |

Audit reports are not kept as standing files. Their findings live in the topic
docs — `docs/research/research_findings.md`, `docs/architecture.md`, `docs/data.md`,
`docs/trading_logic.md`, `docs/notifications.md` — and in Git history.

## Recovery archive

The 2026-07-22 owner-authorized full reset archived and verified all 22 selected
account journals, inboxes, captures, and strategy epoch projections before
clearing them. Recoverable archive:
`/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
(31,490,855 bytes; SHA-256
`e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`). Config,
persistent lock inodes, reports, caches, residual-momentum input, and root-level
market data were preserved.

A pre-evidence BTC-risk state file was rejected rather than migrated and archived
at `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
(SHA-256 `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`).
