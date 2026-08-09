# Operational State

Current operational snapshot. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.** Every observation below is
point-in-time and dated; none of it is a claim about the account right now.

This file describes now. The dated history of how it got here — deploys,
incidents, repairs, change points — is [CHANGELOG.md](CHANGELOG.md). When
something happens, add the dated entry there and edit the sections here to
match; never append history to this file.

## Now (recorded 2026-08-08)

- **Host runs `05f34c7`, whole fleet green**: all nine units
  on/active/enabled, receipt `staged-ok commit=05f34c7 profile=operational`,
  `verify-ok … mainnet=armed`. Zero error-level lines on the funded owner since
  restart.
- **No symbol waits for a book to be priced.** All 509 candidate symbols carry
  a pushed top of book (`tickers`), which is exactly what the order path reads;
  the reconstructed L2 book is a quoting refinement, not a gate. Proved live: a
  first-ever entry on AVAXUSDT was priced `source=bybit_ticker_touch` with no
  book present. **A/B on the same host, symbol and probe, 15 minutes apart:
  with the feed off a cold entry waited 1002 ms to be commanded; with it on,
  216 ms.** Warm entries are unchanged either way (~830–880 ms) — they never
  had a book problem. It costs ~500 frames/s and **~14 points of one core per
  owner** (29.8% vs 15.5%), nearly all of it the websocket library's frame
  handling rather than parsing. It does **not** slow the owner loop: 76 ms on
  versus 77 ms off, because the loop is sleep-bound and the feed runs on its
  own thread. `ACCOUNT_TOUCH_FEED=0` turns it off with a unit restart. A traded
  symbol also keeps its subscription for 10 minutes after its work clears, and
  a head no socket is carrying yet is priced by one REST read rather than
  waiting.
- **A ticker touch is a price, not a book.** Callers opt in per read; markout
  grading and raw capture still refuse anything but real L2; a decision priced
  from it records `book_source=bybit_ticker_touch`. Subscribed depth stays at
  50 because `book_walk_shortfall_bps` walks the visible depth-50 decision book
  — the only measured impact evidence there is.
- **Entries rest for 45 s, not 120 s.** 15 live resting entries filled at a
  median of 1.28 s and a maximum of 36.6 s, so 45 s keeps every passive fill
  120 s got and bounds the tail. 30 s would have crossed 1 of 15; 15 s, 3 of 15.
- **Sizing from the producer's decision price is built but off**
  (`--producer-price-max-age-seconds`, default 0). Producers publish a notional
  with no price, carry's own price is a daily bar close, and publish-to-sizing
  is 3.1 s median but **443 s at p90**. Exits always size off the live price.
- **The funded owner loop runs at ~76 ms per iteration, measured**, and was
  69 ms when the journal was smaller — the ticker feed is not the cause (76 ms
  with it on, 77 ms with it off). It was 284 ms (3.52 Hz) at 6.2%. Venue
  position truth is
  0.23 s old at the health write, down from 1.37 s. A steady-state reconcile pass now
  makes **no REST call at all**: `get_positions` and the two `get_open_orders`
  ownership queries are served by a background read-only feed (250 ms and 2 s
  respectively). The main thread profiles as parked in the idle sleep, so the
  loop is sleep-bound rather than network-bound; `--idle-seconds` is 0.05 and
  `--reconcile-seconds` 0.5. The remaining floor is one signed Bybit round trip
  at ~175 ms, which is geography (see the memory note) and not shortenable
  here. Reduction admission still ages venue truth against the same 4 s bound
  it always had — it is pinned by a floor rather than derived from the cadence.
- **The `-21 USDT` available margin on the funded account is the owner trading
  by hand and is read correctly**, not a fault.
- **The owner stopped hand-trading the funded account on 2026-08-08.** This is
  a live dependency, not a note: the execution adapter now keeps a symbol's
  cached leverage when the symbol goes flat, which removes one ~190 ms round
  trip before every fresh entry. If hand-trading resumes, pass
  `--shared-leverage-authority` to the account owner and redeploy — otherwise
  an entry can be sized against a leverage somebody else changed. A venue value
  that contradicts the cache still drops it either way.
- **End-to-end order latency, measured on demo (2026-08-09, n=16 cycles):**
  entry **276 ms median, 250 ms best**; exit **252 ms median, 228 ms best**.
  Entry was 881 ms and exit 286 ms before the blocking venue reads came off the
  path. Entries rest at the touch, so on a real book time-to-fill is queue
  economics.
- **Our own software time — durable intent to bytes on the wire — is 25.7 ms
  at the median and 9.1 ms at best (n=60).** It was 83.9 ms median to command
  alone. Split at the median: `durable → commanded` 17.9 ms on an entry and
  11.9 on an exit; `commanded → send` 10.9 and 11.0. The rest of the wall
  clock is the 172 ms venue round trip.
- **Sub-10 ms happens, but it is not typical.** Measured on four separate
  runs: 9.2, 9.1, 9.7, 9.7 ms — roughly 1 order in 50. With the owner now
  89.6% idle, the median is no longer waiting for the loop; it is the order
  path's own cost, and that is two durable journal commits. Four disk syncs
  sit in it (file and directory, twice), at 1.3 ms each at the median and
  2.5 ms at p90 — so the sync tail alone spans 5–10 ms of a ~26 ms median.
- **Per-pass cost grew with the account's lifetime state, and that was the
  real cause of the median.** Protections, orders, decisions and executions all
  accumulate for the life of the account and nothing prunes them. Over one day
  of latency testing the demo book went from 129 orders and 200 protections to
  965 and 1,463, and **the reconcile's share of the owner loop went from 7.4%
  to 21.1% with no code change at all**. Three O(history) costs are now
  removed, all keyed on an identity a commit necessarily changes:
  - the anchor projection replayed the entire event history on every call,
    despite existing (per its own docstring) to avoid exactly that — memoized
    on `(events_applied, rolling_state_hash)`, the pair it already validates;
  - `_snapshot_ref` copied every event into a fresh tuple per call, and every
    protection check asks for one;
  - both native-protection lookups filtered the whole protection map, per
    symbol, per pass.

  **The reconcile is now 3.7% of the loop and the owner is 89.6% idle**
  (from 21.1% and 73.8%). Loop scheduling is no longer what holds the median.
- **Cross-session latency comparisons in this repo are confounded by that
  growth.** A number measured on a fresh epoch is not comparable to the same
  number a week later. Compare within a run, or reset the epoch first.
- **What is left is two durable journal commits, and almost nothing else.**
  Sizing an order measures 0.02–0.1 ms. One commit measured 5–7 ms at best and
  9–25 ms typical: ~1.3 ms of that is the disk sync (this is a virtualized
  block device; `fdatasync` is no faster, and an isolated probe understates it
  because it re-syncs a clean directory — in the real workload the directory
  sync alone is ~1.07 ms), and the remainder is CPython hashing and canonical
  JSON on a 2-core 2015-era Xeon. The order path takes two: the plan, then
  `record_submission_attempt`, which is the single-winner guard that makes a
  crash unable to submit the same exposure twice. It is deliberately the last
  durable act before the wire and it is not a latency knob.
- **Sub-10 ms is therefore not reachable on this host without changing what
  durability costs.** Three things would do it, none of them loop scheduling:
  one pre-wire commit instead of two (blocked by the guard above), a faster
  CPU for the serialize-and-hash, or a disk whose sync is tens of
  microseconds rather than 1.3 ms.
- **Blocking venue reads on the owner loop: 19.2% of wall clock → 1.3% idle,
  6.5% during live trading.** Idle went 73.3% → 82.3%. The one that mattered
  was `get_positions` at 18.55%, read inline because the warm feed's snapshot
  was tested for being *newer than the last report* rather than *fresh*;
  against a 500 ms cadence and a feed that refreshes every ~420 ms it lost that
  test constantly. What remains during trading is the ~172 ms window after each
  fill in which the warm snapshot legitimately disagrees with the book and the
  pass confirms at the venue — that one is correctness, not waste.
- **The order path no longer rebuilds things it can remember.** The authorized
  native-breach flat set walked every protection the account has ever recorded
  (200 on the demo book, growing all session) on every pass ahead of every
  request — 24.5% of order-path time, about 46% of everything that was not the
  network. It is cached on the committed state object, which a commit replaces
  rather than mutates. The journal's own paths were rebuilt from the root, with
  an `expanduser`, about fifteen times per order.
- **The WebSocket library was re-proving UTF-8 in Python.** A profile of the
  ticker stream's thread put roughly a third of its awake time in
  `websocket-client`'s `_validate_utf8` and `_decode`, against 0.6% in this
  repo's own frame handler — pure-Python byte loops holding the GIL the order
  path needs. `skip_utf8_validation=True` cut that thread from 19.8% CPU to
  14.4%. Frames now arrive as bytes and go straight to `json.loads`, which
  decodes UTF-8 strictly and rejects a malformed frame exactly as before.
- **That ~190 ms floor is geography, and it is now priced.** TCP connect to
  `api.bybit.com` is 7.2 ms and the TLS handshake 18.1 ms, but a full request is
  187.6 ms — so ~180 ms of every round trip is the Frankfurt CloudFront edge
  proxying to Bybit's Asian origin. `api.bytick.com` (193 ms) and
  `api.byhkbit.com` (206 ms) are the same edges and no better. No code change
  reaches it; a host near the origin is the only lever, and it is the largest
  single win left. Owner decision.
- **The account state copy no longer scales with position count.** Positions
  are shared and privatized through `position_for_write`, exactly as orders
  are; at 301 positions the whole copy is 0.002 ms against 0.295 ms before.
  Positions are never pruned, so every symbol ever traded was being copied on
  every journaled event batch. Both reducer write sites — the fill accounting
  and the PNL checkpoint — have regression tests that fail if either stops
  privatizing.
- **The `bad876c` deploy at 17:20 UTC** carried the first 33-agent latency
  sweep: the journal cursor's per-read filename revalidation, the capture
  path's JSON normalization of every WebSocket frame, and a write-only
  book-context cache. The `8aa8f25` deploy at 16:51 UTC carried the two fixes
  below.
- **The bot no longer goes blind when you hand-trade.** A negative available
  margin — which the funded account reports whenever a hand-opened position
  absorbs the wallet as position margin and the mark moves — was failing the
  wallet read, blocking the owner and paging you. The kernel already refuses
  new risk on that number while letting reductions through; two upstream guards
  made it unreachable. Now passed through. Only a nonpositive equity still
  fails the read. Before the fix the owner sat blocked ~20 minutes with
  `equity=$0.00` at both producers.
- **The declared capital reference is the equity floor, not an invented
  number.** It was 2,500 USDT against an observed ~355; the reference tracks
  the wallet, so the declared figure only sizes the caps in the instant before
  the first read — and at the floor that instant is the smallest envelope
  rather than a 7x one. The envelope is also now anchored on the bootstrap
  wallet before the first request is served. Live log reads
  `capital reference 100.00 -> 331.81`.
- **The 15:28 UTC deploy of `0a6c0e0`** carried the second and third audit
  passes — six money-affecting fixes: a refused stop that could report success,
  a five-minute convergence outage from one ambiguous submission, a protection
  stop that could never be republished, an accounting fault that blocked exits,
  an account-wide health latch cleared by unrelated evidence, and an entry
  escalation that was unreachable. See CHANGELOG 2026-08-08 (later) and
  (third pass).
- **It went out through `deploy_everything.command` (`staged --stop-first`)
  over an open funded book.** The whole fleet was down 15:26:52 → 15:28:53 UTC,
  about two minutes, positions covered only by their venue-side stops. Every
  unit came back clean: the funded owner logged zero error-level lines, rebased
  its envelope to observed equity (359.96 USDT), and left the owner's two
  hand-placed ENAUSDT conditionals strictly alone; both funded producers
  bootstrapped and cycle with `err=none` and `owner=healthy`.
- **The 13:44 UTC deploy of `91f6dab`** carried the two-pass hot-path audit
  (CHANGELOG 2026-08-08) — the loss ceiling that now actually closes the book,
  the venue error classifier that no longer reads a definite refusal as
  retryable, the market-window tail check that stops a stale price reaching a
  decision, and the per-component isolation of protection evaluation. It went
  `staged --stop-first` too, and for a reason worth keeping: the guarded
  rollout refused correctly, because the bot held **HOMEUSDT 13,120** and
  **HFTUSDT 11,243**, both long, each behind its venue-side conditional stop,
  and flattening to satisfy the guard would have closed two live positions.
- **The loss-guard day anchor now survives a restart.** New file
  `/var/lib/liquidity-migration/account-mainnet/account_loss_guard.json`,
  first written 13:43 UTC. Before it, a restart re-anchored the day's loss
  budget to an already-drawn-down equity and forgot a trip.
- **A tripped loss ceiling now refuses queued risk, not just new publishing** —
  live since 15:28 UTC. Admission drops any uncommitted request carrying a
  nonzero target while the ceiling is tripped, before reading a book, and a
  halted owner claims an unservable head rather than letting its own all-flat
  queue behind it. Exits, and any batch already in the journal, are untouched.
  It still has **no vote over convergence**: a target accepted before the trip
  keeps being pursued. See CHANGELOG 2026-08-08 (later).
- **No copy of the funded API key remains on the laptop.** `deploy/.env` is
  deleted; `/etc/liquidity-migration/bybit-mainnet.env` on the host is the only
  copy and the only authority (`REAL_MONEY=true`, carry 2.0, long 1.88, daily
  loss 0.25). The key was readable in plaintext on the Desktop from 2026-08-05
  to 2026-08-08, so **rotation is still owed and is the owner's act.**
- **The owner was hand-trading through the deploy** (KAITOUSDT, filled
  13:22–13:23 UTC). The bot left the position and both its conditional orders
  strictly alone, before and after the restart — the separate-books policy
  observed working under a restart.
- Before this, host ran `a67e035` (2026-08-07 hand-trading fixes) and `aa6f793` carried the
  2026-08-05 friction fixes (CARRY `v4` profile dial, version-free carry
  journal id, `order_notional_pct_equity` rename) and the 2026-08-06
  entry-size fixes (floor 6.0, `dust=` counter, wallet fault with numbers).
- **The ACEUSDT wedge is repaired and the funded owner reads `healthy` with
  an empty detail.** On the first pass after restart the book converged
  175.2 → flat, both stalled commands terminalized on `terminal` evidence,
  and the error loop that had been running at ~250 failures a minute stopped
  dead. Zero errors since 09:39:30 UTC.
- **The funded account DID trade on 2026-08-07.** The carry sleeve opened
  ACEUSDT long 283.6 @ 0.11327 at **00:21 UTC** (command `43e6bc00`, ≈32 USDT,
  sized off the then-current 160.75 equity). The hand-placed buy at 00:26 then
  blocked it from any further entry until 09:39, and its position was closed
  at 04:46 as part of the owner's hand-placed close, not on its own terms.
  Realised from fills **+4.32 USDT** (fees 0.048). An earlier revision of this
  file said no trade was taken; that was wrong.
- **Funding is booked by the share the bot actually held** (since 2026-08-07).
  Bybit settles funding on its netted position, so a settlement can cover
  exposure this book does not own. Each settlement is now scaled by
  `owned_qty_at_settlement / venue_settled_size`, where the owned quantity is
  reconstructed from this book's own fills **at the settlement instant** — not
  the current position, which on 2026-08-07 had already gone flat by the time
  the settlement was discovered. The venue's raw numbers are kept verbatim in
  the event metadata (`venue_funding_usdt`, `venue_settled_size`) and remain
  what the immutability re-check compares against. **The share is 1.0 whenever
  the venue position is the bot's own, so this is an identity on an account
  nobody else trades.** Before it, the 04:00 UTC ACEUSDT settlement credited
  the bot +10.72 USDT when it had earned +0.44. Funding is what this strategy
  is for, so a wrong share inflates the measured edge directly.
- **Funding booked before 2026-08-07 was booked whole and is not restated.**
  Five settlements totalling **+15.23 USDT** are on the funded journal; the
  ACEUSDT +10.72 of that is ≈96% not the bot's. Treat funded P&L before this
  date as overstated by roughly that amount.
- **The funded account is flat, healthy, and sized off the new dials.**
  Equity **268.78 USDT** (was 160.75 on 2026-08-06; the rise accompanies the
  owner's hand-traded ACE position, cause not independently confirmed).
  The producer reports `notional_x=2.0 leverage=3.9` and the preflight
  reads `profile matches dials`. At this equity each carry name is
  ≈ 0.1 × 2.0 × 268.78 ≈ **54 USDT**, two names ≈ 108 USDT gross.
- **The bot and the owner keep separate books on one venue account**
  (owner's decision 2026-08-07). Venue exposure above what the bot owns, and
  venue orders the bot did not place, are recorded in the venue snapshot
  (`foreign_positions`, ownership `status`) and left strictly alone — never
  traded, never blocking. The bot claiming exposure the venue does not hold
  still blocks, and now heals itself by booking the reduction down to flat.
  A symbol carrying foreign exposure **is still swept for its stop** — it must
  be, because a skipped symbol stops being marked fresh, ages out, and
  re-blocks the account on `native protection health is stale`. The skip is
  only for a book that cannot be trusted.
- **The safety stop covers the owner's hand-placed size, on every trade.** The
  manager only ever creates Bybit **Full-position** stops (`tpsl_mode="Full"`),
  which close the entire venue position at trigger and carry no quantity of
  their own. The owner's standing workflow is to scale a coin by hand shortly
  after the bot enters it, so the bot's stop sits over the combined position
  as a matter of course. On 2026-08-07 the ACEUSDT disaster stop was 0.07362
  (35% below the bot's 0.11327 entry, `DISASTER_STOP_FRACTION=0.35`) over the
  merged 6,957.5 — had it fired, it would have closed the owner's 6,673.9
  (≈763 USDT at ≈0.11437) for a loss near **−272 USDT** on an account with
  ~269 USDT of equity. Known and accepted; the venue offers one stop per coin
  and cannot split it.
- **The 2026-08-06 hand-opened HOMEUSDT position is closed** (owner's own
  act, some time before 19:20 UTC). Both refusals it caused cleared on their
  own — but only because that close was no larger than the bot's own book;
  the same mechanism wedged ACEUSDT hard on 2026-08-07 (CHANGELOG). The
  funded account took no trade on 2026-08-06: the entry-size floor blanked
  the 00:20 UTC decision and the hand position then held the owner blocked
  past the ~05:50 UTC signal expiry.
- **Real money is armed.** The funded account's owner reports healthy; last
  equity read **268.78 USDT** through the coin-row wallet fallback
  (`48ebc50`). The 2026-08-04 withdrawals still await owner confirmation
  (CHANGELOG entry of that date; see Open defects).
- **The funded account is on cross margin and one-way position mode, and
  one-way is load-bearing**: the fleet places every order and stop with
  `positionIdx 0` and the protection layer refuses nonzero-index rows, so
  enabling the venue's hedge mode would reject every fleet order. No startup
  check pins either mode — proposed, owner to decide.
- **No code is committed-but-undeployed**; the host runs the tip of `main`.
  The host's `bybit-mainnet.env` carries
  the four new dial names (read
  directly 2026-08-06; the earlier retired-names warning was stale), and the
  installed risk profile is the render of those dials.

## Topology

Nine units on and active: the demo owner, demo LONG and CARRY producers, the
mainnet owner, mainnet LONG and CARRY producers, the Telegram controls
daemon, and the demo and mainnet liveness timers. The CONTINUOUS producer and
the hedge and residual-momentum timers remain installed but off (sleeve
retired 2026-07-29). Paper is retired whole (2026-08-03); demo is the only
practice book.

| Kind | Units |
| --- | --- |
| Account owners | demo, mainnet |
| Target producers | demo × LONG/CARRY, mainnet × LONG/CARRY |
| Always-on daemon | Telegram controls |
| Timers | demo liveness, mainnet liveness (both active) |

Bulk collectors are removed and raw account-market persistence is disabled.
Live L2 readiness and exact decision-book capture remain enabled.

## Risk envelope

**Demo** (the 25× profile, deployed 2026-07-27): capital reference 250,000
USDT, entry leverage 2×, per-symbol notional 125,000, component/account gross
500,000, initial margin 250,000; LONG notional multiplier 0.5, CARRY
multiplier 1.0 (per-name 0.10 and gross cap 1.0 from the registered rule, so
the CARRY book tops out at 1.0× the reference, unlevered). Startup and
authorization reject unknown profile fields, producer leverage above the
owner cap, or registered envelopes outside the bound profile.

**Real money**: four owner dials, set in the host `bybit-mainnet.env`
(values below read from the host 2026-08-06; the next render/activation
applies them). `RM_CARRY_LEVERAGE` (**2.0** since 2026-08-06, was 1.0) and
`RM_LONG_LEVERAGE` (**1.88**) are each sleeve's book ceiling as a multiple
of equity, worst case included — each carry name takes a tenth of its dial
(≈ $20 on ~$100 equity), each LONG entry ≈ its dial / 18.75 (≈ $10), and the
two dials may total 9.9. `RM_DAILY_LOSS_FRACTION` (**0.25**) and
`RM_CARRY_STOP_LOSS_FRACTION` (0.35) are the protections.
Everything else the old surface exposed is derived and still proved at
render; a retired `RM_*` line in an env file is refused by name. Derived
venue margin leverage ≈ 3.9× at these dials — on a fixed wallet a bigger
book is more leverage; the two cannot move independently. Honest protection
note: the loss halt fires on realised loss only, so a dialled-up open book
meets the venue's liquidation engine before the halt.

## Standing operational constraints

- **Arming real money is one switch, set by the owner's own hand**:
  `REAL_MONEY=true` in the root-owned
  `/etc/liquidity-migration/bybit-mainnet.env`, beside the live key. A git
  commit can never arm; activation still walks the full preflight, and every
  capital-preservation control (loss halt, envelope, native stops, partition,
  single-writer lease, reconciliation) gates the start.
- **The funded account must stay in one-way position mode** (see Now — a
  venue-side switch to hedge mode would reject every fleet order).
- **A guarded rollout proves the account venue-flat**; since the 2026-08-03
  de-friction purge the proof binds on mainnet and is advisory off it. A
  failed verification is not permission to hand-start a partial fleet.
- **Demo rule receipt freshness is a side effect of deploying, not a
  deadline.** A rollout past half the age bound re-probes; only mainnet holds
  the registered 168-hour ceiling. The watchdog warns in the final 24 hours
  but refreshes nothing itself. The other registered startup ceilings (warmup
  timeout, INVOCATION_ID, stray-order gate) also bind mainnet only.
- **Unknown safety-critical state fails closed.**
- **Deploy is one command from the primary checkout** (`scripts/ops.sh deploy
  staged|rollout`). The manual GitHub workflow exposes `rollout`, `install`,
  `activate`, `verify`; the two mainnet modes are deliberately absent from CI
  ([docs/operations.md](docs/operations.md)). Push only from the primary
  checkout until the pre-push hook's git-fixture tests are hermetic (a linked
  worktree run corrupted the repo once, repaired 2026-08-03).
- Three CONTINUOUS candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`)
  have venue `deliveryTime=1784538000000`, recorded prospectively in private
  mode-0600 retirement registries; they may retire only while positions,
  targets, orders, and inbox exposure are all flat.

## Forward evidence stream

Everything that runs is graded under the Progressive Evidence Model
([docs/research/governance.md](docs/research/governance.md)): each committed
config is graded on the run of days it postdates, continuously, with recorded
change points — the commit is the registration; there is no waiting window
and no separate registration artifact. (The earlier prospective epoch
machinery was deleted 2026-07-24 by owner instruction; its receipts survive
on disk but nothing reads or can reproduce them.)

Standing invalidation: **cross-fleet P&L comparison before 2026-07-31 is
invalid** — the two fleets decided off different data and held different
price bases, both measured (CHANGELOG 2026-07-31). The comparison lane ended
with the 2026-08-03 paper retirement, so no valid demo-vs-paper number exists
on either side of that boundary. All demo equity/P&L numbers before the
2026-08-03 14:38 UTC clean-slate reset belong to the archived epoch.

Change points currently accruing forward days: CARRY `lane2_carry_hold_v4`
(promoted 2026-08-03 with zero forward days at promotion; v3 keeps scoring as
comparator), LONG v12 wide-stop (2026-08-03), and the entry execution
recipes (quote-first entries, touch-sized windows, and the replay-selected
resting recipe, all 2026-08-04 — deployed with `f85371e`). Full statements in
[docs/research/strategy_program.md](docs/research/strategy_program.md).

## Evidence boundary

The tracked hedge history is an **immutable sizing-only model prior through
2026-07-09** — not live-extended calibration or performance evidence.

The funded account has no performance record yet: its first night
(2026-08-04) legitimately decided cash, and the first honest maker-share
grade waits on funded `is_maker` receipts from a non-empty book. Demo fill
economics are not evidence — demo fills simulate without queue position, and
the demo realm's matching engine holds phantom internal liquidity its
published book does not show.

Real money is a separate door: no runtime status or rolling record arms it —
arming is the owner's hand on the switch (constraint above).

Research-only: Strategy Overhaul V2 closed with no qualifying thesis and did
not touch its reserved holdout. The consolidated conclusion and successor
direction are in `docs/research/strategy_program.md`; current anomaly
evidence is in `docs/research/archive/2026-07-24-anomaly-research.md`.

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
| `unadopted external execution: external protection fill is not position-reducing` (2026-07-27 10:47:20, once) | A manual ~10 USDT spot BTCUSDT buy made in the demo account UI at 10:47:20.106 (venue execution `1784799743630817527`), three minutes before the owner's 250k top-up transfers (+90k 10:49:40, +100k 10:52:20, +50k 10:52:26). The kernel manages linear perps only and was flat, so it correctly refused adoption, surfaced the error once, and health returned to green. The ~0.000153 BTC sits in the unified wallet outside the managed book; no reconciliation drift. Since 2026-08-07 an execution with nothing to reduce is logged as foreign and ignored, so this shape no longer latches an adoption failure or repeats. |
| `ignoring foreign … execution … with no owned position to reduce` | Normal since 2026-08-07: the owner trading by hand on the same venue account. Recorded, never traded. Only a `venue=…:reconstructed=…:unbacked=…` line is a real fault. |

## Open operational defects

| Item | State |
| --- | --- |
| 2026-08-04 withdrawals await owner confirmation | The venue's own transaction log shows the money leaving through the account login (the API key holds no transfer/withdraw permission — probed, refused), so this was by hand. **If these withdrawals are not the owner's, treat the venue login as compromised immediately** |
| Quote-lab capture spams its own log when disk-blocked | The 6 GB min-free guard stops tape writes but not the process's nohup traceback spam — it filled the disk to 0 bytes on 2026-08-05 and killed a deploy. Both capture processes are currently killed; flagged for a fix |
| No startup check pins margin/position mode | Cross + one-way are load-bearing (see Now); a venue-side flip is only caught at order rejection. Proposed, owner to decide |
| Nothing bounds convergence toward a stale accepted target while producers are down | Deliberately not built — a liveness-coupled trading halt needing owner design (2026-08-03) |
| Kline bootstrap logs `failed=N` on restart with an intact store | It re-fetches a window it already holds and counts zero new inserts as failure; bounded ~40–50 s per restart. Tracked follow-up |
| The LONG demo producer is SIGKILLed by every stop | It drains its cycle on SIGTERM, but a cycle runs ~180–350 s against the unit's 180 s `TimeoutStopSec`. Harmless for deploys (`require_quiescent` accepts `failed`, targets publish atomically), but no LONG stop is ever graceful |
| Reported P&L is provisional | Figures are fill-reconstructed, not venue-confirmed (most `pnl` events carry `funding_status=pending_venue_reconciliation`). No closed-loop accounting check yet, which real money needs |
| Entries execute ~23 minutes after the price the scorer models | Live runs the delayed-entry stress case, not the bar-close headline case. Recorded with the measured capacity numbers in `docs/research/carry_hold.md` |
| Intraday notional tracking is bounded, not continuous | Deliberately left as an owner decision; `docs/research/carry_hold.md` §7.5 states it rather than treating it as settled |

Audit reports are not kept as standing files. Their findings live in the topic
docs — `docs/research/research_findings.md`, `docs/architecture.md`,
`docs/data.md`, `docs/trading_logic.md`, `docs/notifications.md` — and in Git
history.

## Recovery archive

The 2026-08-03 clean-slate ledger reset archived all three sleeve roots plus
the account journal/inbox/capture, reports, and caches to
`data/_archive/ledger-reset-20260803T143852Z.tar.gz` (SHA-256
`4b729c34…937a`) before starting the fresh epoch.

The 2026-07-22 owner-authorized full reset archived and verified all 22
selected account journals, inboxes, captures, and strategy epoch projections
before clearing them. Recoverable archive:
`/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
(31,490,855 bytes; SHA-256
`e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`). Config,
persistent lock inodes, reports, caches, residual-momentum input, and
root-level market data were preserved.

A pre-evidence BTC-risk state file was rejected rather than migrated and
archived at
`/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
(SHA-256 `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`).

The 2026-08-05 torn demo event tapes (the disk-full casualties) are backed up
beside themselves as `strategy_event_tape.jsonl.enospc-20260805.bak` in each
demo producer root.
