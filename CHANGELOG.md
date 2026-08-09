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
    Subscribed depth stays at 50: `docs/architecture.md:605` and
    `docs/research/carry_hold.md:300` both walk the visible depth-50 decision
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
