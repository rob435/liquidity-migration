# The execution engine (Rust)

This repository has two parts, with a hard wall between them.

- **The research system** (Python, `liquidity_migration/`): finds and grades
  strategies. It owns data, backtests, evidence notes, and the research queue.
  Its output is a strategy *configuration* — a small TOML block of parameters.
- **The execution engine** (Rust, `engine/`): trades. It reads market data,
  runs strategy plugs, checks risk, signs orders, and writes one append-only
  log. It never does research; research never places orders.

The seam between them is the strategy plug: research emits a config, the
engine loads it by name. Nothing else crosses the wall.

### Two kinds of strategy, one seam

Strategies differ in where the decision has to happen, and the seam carries
both:

- **Decide in the loop.** A quoting or sniping strategy reacts to a price in
  microseconds, so its whole decision lives in Rust as a plug. It reads
  market state, its own resting orders, and emits places, cancels and
  amends. Config is a `[[strategy]]` block of parameters.
- **Decide in research, execute in the loop.** A strategy whose decision is
  a batch over months of history — carry reads ninety days of settled
  funding and hourly bars, and holds a state machine across all of it —
  cannot and should not compute that inside a trading process. Python
  decides on its own clock and writes a *target book*: absolute notional per
  symbol, the stop each carries, and how long it may be acted on. The engine
  follows it — diff against position, exits first, size, quantize, quote,
  attach stops — and every risk gate applies exactly as it does to any other
  order. The stop is not fixed for the life of a trade: a book that declares a
  narrower distance than the venue is holding moves the venue's stop in, with
  no order involved. It only ever tightens, and it keeps working after the
  book's entry window has shut.

The target book is written by
[`engine_targets.py`](../liquidity_migration/rules/engine_targets.py),
atomically, so a reader sees a whole book or the previous one. A missing or
stale book means *no decision*, and the engine holds its position steady; an
empty book means *hold nothing*, which is a decision and does get acted on.
Those two are deliberately different, because confusing them flattens a live
book on a data outage.

### Writing a new one

Copy [`engine-strategies/src/template/`](../engine/engine-strategies/src/template.rs).
It is a working, tested plug that does something trivial, so everything around
the decision is real and only the decision needs replacing. Its module doc is
the authoring guide: what a strategy may touch, what the engine holds it to,
and the five steps to register one. It is compiled and tested with the rest so
it cannot rot, and left out of the `PLUGS` table so no config can run it by
accident.

A plug overrides only the per-event hooks it acts on — `on_market`,
`on_timer`, `on_order`, `on_targets`, `on_intent_refused` — and the ones it
ignores do nothing by default. The one exhaustive match over engine events
lives in the `Strategy` trait itself, so adding a new kind of event never
forces an edit in a strategy that ignores it.

Two rules make strategies independent rather than merely separate:

- **A strategy owns its symbols.** The venue holds one position per symbol and
  keeps no note of who asked for it, so two plugs on one symbol each read the
  other's fills as their own. The engine refuses to boot such a config.
- **A strategy owns its config.** Everything it needs comes from its own
  `[[strategy]]` block. Deleting a strategy must leave nothing behind that
  anything else reads.

One thing the engine does not protect you from: `StrategyId` is the block's
position in the config, and it is written into the log with every order. Do not
reorder `[[strategy]]` blocks while orders are in flight — a restart would
re-register them against the wrong strategy's share of capital.

## The one-sentence design

One process, one thread, one loop: a market message arrives on a socket, is
parsed once into shared memory, the strategy decides, the risk kernel gates,
the order record is made durable in the append-only log, the signed order
leaves on a pre-warmed connection — all without leaving that loop.

## Honest latency contract

Geography is not ours to fix in software. From the current box, one round trip
to the venue is ~175 ms; no rebuild changes that. What the engine owns is our
side of the wire, and that is what we measure and promise:

Measured 2026-08-13 by `engine bench` (the real loop, real HMAC signing,
the real log with its fsync in the chain, a pretend venue on the same box;
release build, Apple silicon; 20,000 quotes in, 1,000 orders out):

| Segment | median | p99 | worst |
| --- | --- | --- | --- |
| market message in → decision made | 84 ns | 125 ns | 209 ns |
| decision → order durable in the log | 3.8 ms | 4.8 ms | 10.5 ms |
| **in → durable → out the socket** | **3.9 ms** | **5.0 ms** | **10.8 ms** |

The durable step is nearly the whole chain, and it is the platform's price:
on macOS, Rust's `sync_data` is a full drive-cache flush (~3.2 ms/barrier
measured); the VPS's Linux `fdatasync` measures ~2.2 ms. The pretend venue is
plain HTTP on localhost, so the venue-side cost is short by about one TLS
record's work. Numbers are re-measured by running `engine bench` — they live
in the log the bench writes, not only here.

The <100 ms decision-to-execution goal is met on our side of the wire with
a ~25× margin at p99; the venue round trip on top is the same geography
every non-colocated participant pays.

### On the production box

Measured 2026-08-14 by the same command on the VPS the fleet runs on (2
cores, Linux, `fdatasync`; 20,000 quotes in, 1,000 orders out, alongside the
running fleet):

| Segment | median | p99 | worst |
| --- | --- | --- | --- |
| market message in → decision made | 721 ns | 1.6 µs | 12.5 µs |
| decision → order durable in the log | 1.91 ms | 4.71 ms | 8.82 ms |
| **in → durable → out the socket** | **2.28 ms** | **5.18 ms** | **9.47 ms** |

The box's CPU is slower than the laptop's, so thinking costs more; its disk
is faster to make durable, so the chain is shorter overall. For scale: the
Python fleet measured 25.7 ms of software time per order, median, on that same
box — same hardware, same venue, ~10× more time.

### Against the real venue

Measured 2026-08-18 from the live demo engine's own log — all 67 real orders
placed since 2026-08-14, each order's decide, wire, and venue-ack stamps:

| Leg | median | p90 | worst |
| --- | --- | --- | --- |
| decision → durable → out the socket | 2.7 ms | — | 185 ms |
| socket → the venue acknowledges | 172.4 ms | 177.5 ms | 486 ms |
| decision → order live at the venue | 179 ms | 512 ms | 1.01 s |

The slow tail above is 27 of those 67 entries paying an extra ~169 ms (844 ms
worst) *before* the wire, because every entry from flat re-confirmed leverage
with the venue inline. Under `leverage_authority = "sole"` (the demo config)
the confirmation is off the order path: what the engine set stays trusted
across flat spells, the book's leverage is armed at book arrival, and every
held position's leverage is verified against the venue's own position rows — a
mismatch alarms, is written to the log, and turns the inline confirmation back
on for that symbol. Under `"shared"` (the default, and the mainnet setting)
the engine keeps re-confirming, because a hand may retune a flat symbol.

What is left is the venue's: two long-idle orders acked at 277 ms and 486 ms
with no proven local cause — the account poll keeps the connection pool warm,
so cold TLS does not explain them. That residue is not ours, until more
samples say otherwise.

## Layout

`engine/` is a Cargo workspace. Contracts live in one crate; every other crate
implements or consumes them, which is what lets the parts be built in
parallel and integrate by type-check.

| Crate | Owns |
| --- | --- |
| `engine-types` | every shared type and trait: events, intents, orders, log records, the `Strategy` trait, and the capability traits (`Wal`, `VenueGateway`, `RiskKernel`) |
| `engine-wal` | the append-only log: CRC-framed records, buffered appends, an explicit durability barrier for order sends, group flush for everything else, replay with torn-tail truncation, size-triggered rotation into archived segments |
| `engine-marketdata` | Bybit public WebSocket: subscribe, sequence-check, parse once into flat per-symbol state, stamp arrival time |
| `engine-venue` | the venue gateway, demo or funded by realm: HMAC signing, pre-warmed keep-alive TLS, order create/cancel/amend, stop attach, leverage, position and wallet reads, the realm's private WebSocket for order/fill updates |
| `engine-risk` | the capital controls: equity-anchored envelope, per-strategy capital partition, stop-attach discipline. Fail-closed |
| `engine-core` | the loop: wires the above together, runs strategies, keeps the latency ledger, hosts the mock venue used for measurement |
| `engine-strategies` | the plugs: a registry from name + TOML to a boxed `Strategy` |

## Safety posture

- **The funded account is reachable, and only with the owner's switch.** The
  rule is the fleet's own:

  - `REAL_MONEY=true`, in the host credential file, set by the account owner.
    Without it a mainnet gateway fails at the credential read, before a socket
    opens. **The engine never writes it, and no config can stand in for it.**
  - It cuts both ways: an armed host refuses to run the *demo* realm, so a box
    cannot be half-live and half-practice at once.
  - The two realms read disjoint environment variables, so a demo key cannot
    authenticate the funded account even if the realm were wrong.
  - A realm is always named. There is no default to fall back to.
  - A typo stops the engine. `REAL_MONEY=ture` is not "off"; it is a mistake in
    the one line that decides whether this is real.

  The structural half is checked against the source: the four venue hosts may
  be written in exactly one file, `realm.rs`. That closes the door a mainnet
  address opens the moment it exists anywhere — the test-only constructor takes
  a hostname, and no test, benchmark or module can spell one.

  `engine-marketdata` holds `wss://stream.bybit.com/v5/public/linear`
  separately, because Bybit serves demo public data from the mainnet stream. It
  is unauthenticated and read-only.

  Nothing has ever been sent to the funded account. The path exists, is fenced,
  and is tested; it has not been exercised.
- **Shadow mode is the default.** The engine computes intents and logs them;
  it only sends orders when started with an explicit live flag, and the risk
  kernel still gates every send. A shadow order is judged by the same kernel
  and then released at once rather than reserved — it can never fill, so a
  held reservation would lean on every later verdict for the length of the
  run. And a shadow book flip produces exits the kernel refuses, because the
  real account holds nothing to reduce: a long shadow run's denial lines on
  flips are that, not a fault.
- **The log recovers the fills its stream never delivered, and the may-open
  latch has a keyed door.** The private stream forgets: a stop firing during a
  deploy window or an execution inside a reconnect gap would otherwise leave
  the log's per-symbol fill sum permanently behind the venue, and the boot
  comparison would latch `may_open` false on debt no restart could repay. Two
  mechanisms answer that, neither touching the check itself:

  - **Recovery.** At boot, and again after every private-stream reconnect,
    the engine asks the venue's own execution history for the window it was
    deaf in and writes what it missed as `recovered_fill` records — deduped
    by the venue's execution id and against recently delivered fills, durable
    before the log is compared to the venue, attributed through the order
    that produced them when the log knows it. When history is unavailable the
    comparison runs on the log alone.
  - **The clear.** `engine reconcile-clear --config engine.toml [--execute]`
    is "somebody looks at the log" made executable, for debt older than the
    venue's history (about a week). It holds the log's own lock (so the
    engine must be stopped), prints the standing findings and the per-symbol
    ledger-versus-venue table, and with `--execute` appends one
    `latch_cleared` record: the exposure ledger restated to the venue's
    positions, the absorbed findings kept as the receipt, the latch reset.
    The next boot still runs the same comparison and latches again on
    anything that stands — the clear resets the memory, never the check.
- **The capital controls are the kernel's, and unknown state refuses the
  order.** The equity-anchored envelope, the per-strategy capital partition
  and the stop-attach discipline live in `engine-risk`, each with
  table-driven tests over its decision semantics. Read those tests and
  `engine-risk/PORT_NOTES.md` together: the notes carry every rule, its
  defaults, and every place this kernel is deliberately stricter.
- **One writer per account.** The engine takes the fleet's own lease: one
  kernel `flock` per venue account, at
  `/run/lock/liquidity-migration/bybit-{realm}-user-{userID}.lock`, joined
  exactly — same directory, same name from the venue's own authenticated
  account number, same open flags, same re-proof after the lock that the file
  locked is still the file at that path. A lease that differed in any one of
  those would protect nothing: two processes would hold two different locks
  and each would believe it was alone. A live engine takes it before it boots
  and refuses to start if somebody has it, naming the holder. A shadow engine
  only looks, because a shadow run holding the lease would lock out the writer
  that does. There is no heartbeat and no expiry — the kernel drops the lock
  when the holder dies, which is the only expiry that cannot be wrong. The log
  file is claimed the same way, so two engines cannot share one log either.

## Worked entries

An entry can afford to wait for a price; an exit cannot. So exits cross and
entries may rest, and the engine — not the strategy — does the working. A
strategy attaches a `work` policy to its intent and the engine rewrites the
order into a resting limit at the touch, then walks it: it reads which way the
displayed size leans and rests inside the spread or behind the touch
accordingly, moves the order as the market moves, **never chases a touch that
fell away** (an order left alone at the front of the book is where you want to
be), escalates as the window runs out, and finally crosses at a bounded price
rather than sending an unbounded market order. Every number in the recipe is
measured; the decision is a pure function and the engine's group-flush tick
drives it.

Two gates decide whether resting is worth it at all: the spread must be at
least two ticks **and** at least one basis point of the price. The second is
the one that bites on high-priced instruments — BTC's spread is routinely
under a basis point, so an entry there crosses whatever the config says.

Off unless asked. `rest_entries = true` on the target-book follower turns it
on; a trim or an exit is never worked, whatever the config says.

One thing worth knowing: a strategy that decides before the market feed has
delivered a quote has no touch to rest at, and its order goes as written. A
target book already on disk at boot is read within a second, before the feed
connects, so the first entry after a restart crosses.

## What the fills cost

Latency is half the question. A fast order path that quietly pays two basis
points more than it needs to is a worse thing to own than a slow one that does
not, so there is a second ledger, for the price.

The names and the signs are the repository's own, from
[`architecture.md`](architecture.md) §Trade diagnostics, which the Python
research half computes against recorded books. `s` is +1 for a buy and
−1 for a sell, `M0` is the midpoint when the order left, `P` is the fill price,
`Mh` is the first healthy midpoint at or after the horizon. **Shortfall, spread
and fee are positive when they cost us**; `signed_markout_bps` is the one
number with the opposite convention, positive when the price moved our way.

Two facts are kept that the rest is built on:

- **`is_maker`.** Bybit sends it on every execution. It is the difference
  between earning the spread and paying it, and [`STATE.md`](../STATE.md)
  waits on those receipts for the funded maker-share grade.
- **`M0`, on the `OrderSent` record.** Written at the send, because that is
  the only moment it exists: a rested entry can wait a minute, and by the time
  it fills the price it was decided against is gone. Zero means the book could
  not be read, which makes every arrival number for that order *missing* — the
  code never turns an unknown into a zero, because a zero reads as "we
  measured, and it cost nothing".

Everything except the markout is then arithmetic over records the log already
holds, so the live summary and the report read back off a finished log are the
same code and cannot drift apart. The markout is the exception — a log holds no
prices — so it is written down when its horizon comes due, on the group-flush
tick that is already running. It waits five seconds for a readable book, then
records the horizon as terminally missing.

```bash
engine fills --wal /var/lib/liquidity-migration-engine/engine.wal
```

To see the whole path run without an account, give the bench's pretend venue
fills and read the log it writes:

```bash
engine bench --events 900 --rate 500 --every 60 --wal /tmp/f.wal --fills
engine fills --wal /tmp/f.wal
```

`--fills` is off by default and stays off, because the latency table above was
measured without it and turning it on silently would put fill handling inside
numbers nobody re-measured. Pace the run: the shortest horizon is a second, so
a bench that finishes in eighty milliseconds never brings a mark due.

One row per sleeve and symbol: maker share, fee, arrival shortfall, all-in, and
the signed markout at 1 s, 15 s, 1 min and 5 min. The footer confesses rather
than staying silent — what share of the traded notional had a book to be
measured against, how many horizons never found one, what was dropped, what is
still waiting. Five of those numbers are also in the heartbeat, so an operator
sees them without the log.

Two rules keep it from measuring nothing and calling it something. A markout
is only taken against a book that **arrived at or after the horizon it
measures** — a halted or delisted symbol keeps its last quote for ever, and
four horizons marking against the identical mid read exactly like a
measurement while being a measurement of nothing; a book that spoke once just
after the fill and went quiet is the same trap one step later. And a mark that
turns up long after its horizon is not that horizon: a stall or a replayed
backlog would otherwise be averaged into the one-second column at full weight.

What it cannot repair, it confesses. A private-stream reconnection is a window
in which fills happened and were never delivered; the engine repairs its idea
of exposure from the venue but the fills themselves are gone, so the footer
says how many gaps there were. A restart ends every horizon a fill was still
owed, so the later columns cover less of the trading than the earlier ones.

Two honest differences from the Python half, both stated in the module header:
`M0` here is the **top of book**, because that is the only book the engine
carries, so nothing here says anything about market impact and nothing pretends
to; and rollups weight by **notional** rather than quantity, because a number
spanning symbols cannot add a quantity of BTC to a quantity of DOGE.

## Crash safety

One append-only log (`engine.wal`) is the engine's memory. Every record is a
length-prefixed, checksummed frame with a monotonic sequence number. The
order lifecycle is written as it happens: intent → risk verdict → order sent
(made durable *before* the bytes leave, so a crash can never forget an
in-flight order) → venue ack or reject → fills. On boot the engine replays
the log, truncates a torn tail at the crash point, and reconstructs what was
in flight before touching the venue. The log's own id table (`Names`) is
re-interned first, ahead of the config's subscriptions: ids are interning
positions and every replayed record names the old run's numbers, so a symbol
a book admitted at runtime keeps its id — and a position in it stays visible
to reconciliation and the stop discipline — across a restart.

Then it asks the venue. The log is a perfect record of what this engine did;
it is not a record of what happened to the account, because the venue keeps
trading while the engine is stopped and other hands reach the same account.
Boot is the one moment the two pictures can be compared:

- An order both agree is working is adopted and keeps being charged to the
  strategy that placed it.
- An order the log says is working and the venue has never heard of ended
  while the engine was down. Its ending is written into the log and its claim
  on the strategy's capital share is released — no update for it will ever
  arrive, and left "in flight" it would charge the partition on every future
  boot and hold the one-order-per-symbol gate closed against its symbol,
  exits included. Nothing is re-sent.
- An order the log never placed is reported. If it is in a symbol a strategy
  here trades, the engine stops opening; if it is in a symbol no strategy can
  even address, it is news — the owner hand-trades this account, and stopping
  for that would mean stopping most days. An order with no client id is the
  exchange's own stop, not a second writer.
- A position the log's own fills cannot account for stops the engine opening.
  Reducing stays allowed: taking exposure off is safe whoever put it on.
- A position with no stop gets back the stop **the log says it was opened
  behind**. If the log cannot say, the engine refuses rather than inventing a
  level.

The latch is written into the log and read on the next boot, because a restart
that cleared it would turn "stop and tell somebody" into "stop until the next
crash", and something restarts this process automatically.

### The log rotates

The log does not grow without bound. Once the current file passes
`wal_rotate_mb` (default 256; zero turns it off; a config that omits the key
still boots), the engine — on its group-flush tick, never between an order's
fsync and its send — starts a fresh segment in the same directory:
`engine.wal` is segment 1, then `engine.wal.000002`, `engine.wal.000003`, and
so on. Old segments stay in place as archives; nothing in the engine ever
deletes one — retention is the owner's decision.

Every new segment begins with one restatement record (`SegmentBase`) carrying
everything boot replay needs from the segments before it: the id tables, the
may-open latch, every still-open order with its
partial fills, whose fills built each position, the per-symbol fill totals
reconciliation compares the venue against, and the intended stop per symbol.
So boot replays only the newest segment it can trust, and recovers the same
engine it would have from the whole history. The restatement is one
checksummed frame, which makes "complete enough to trust" a single mechanical
check: if a crash lands anywhere inside a rotation, the half-written segment
fails that check and boot falls back to the old segment with nothing invented
and nothing lost. The one flock on the configured path still covers every
segment, so no second engine can slip in during a rotation.

What a rotation drops: almost nothing, because a restart already dropped it.
Markout horizons still owed at a restart are not rebuilt from the log, and
the run's own cost score starts fresh each boot — rotation changes neither.
The one genuine narrowing: after a rotation and a restart, a fill arriving
for an order that ENDED before the rotation is charged to no strategy (the
restatement carries open orders, not every order ever). That is the same
treatment a hand-placed order's fill gets, it is diagnostics rather than
safety, and reconciliation still notices the exposure. `engine fills` and
`engine replay` read the whole segment family in order when given the
configured path, so the offline history stays complete.

## What a venue stall does

Three layers, from the socket up:

- **The feed reconnects itself.** The market stream pings every 20 seconds,
  times a missing pong out at 10, and redials with backoff. A reconnect
  clears every price and delivers a `FeedReset`, so nobody reads a pre-gap
  quote as current. What the reconnect machinery cannot catch is a socket
  that stays open and says nothing, or a redial loop that never lands — both
  leave the engine's last picture standing.
- **The account reading is bounded.** `[risk] max_account_view_age_s` — 120 in
  the deployed configs — is the age past which the kernel refuses to judge an
  entry against the reading.
- **The quote is bounded** (`max_quote_age_ms`, default 30000): an entry
  decided against a quote older than the bound is refused before the risk
  kernel sees it, the refusal is written to the log as a verdict, and the
  strategy hears it the way it hears every refusal. The age is measured from
  the quote's own receive stamp on the engine's monotonic clock, never a
  wall-clock guess, and a symbol that has never quoted — including right
  after a feed reset wiped the book — is refused the same way: the absence
  of a price is the stalest price there is.

Both bounds gate ONLY the opening of exposure. Exits flow under a stale
reading and a stale quote alike, and cancels and amends of protective orders
are never gated: taking risk off must not wait for the market data to come
back.

## Adding a venue

Venues differ in kind, not just in address, so a venue states what it can do
rather than the engine assuming. `VenueCaps` declares five things — whether
the venue holds a position-level stop the engine can set, whether a resting
order can be amended in place, whether post-only is honoured, whether orders
batch, whether the engine may set a symbol's margin leverage — and the
engine refuses an action a venue cannot honour instead of
quietly substituting a different one. Cancel-and-replace is not an amend: it
is a new order at the back of the queue at a fresh price, and a strategy that
asked to move a quote would never learn it had been given something else.

The engine picks its venue by name: `[engine] venue = "bybit_demo"` in
`engine.toml`, resolved once in `engine-venue/src/registry.rs`. Leaving the
key out means the Bybit demo account. An unknown name is refused at boot, by
name, listing what the binary knows — never defaulted to a venue nobody chose.

To add one (Hyperliquid, MEXC), four steps in `engine-venue`:

1. Write the adapter as a module in the crate and implement `VenueGateway` —
   nine required methods: `caps`, `account_identity`, `send_order`,
   `cancel_order`, `amend_order`, `set_stop`, `account_view`,
   `instrument_rules`, `working_orders`; `add_symbol`, `set_leverage` and
   `executions` carry defaults, twelve in all. Take `executions` seriously:
   its default refuses, so an adapter that leaves it alone runs with fill-gap
   recovery off — the engine falls back to today's behaviour silently. State
   the capabilities honestly. A
   venue with no native position stop is not a broken venue; it is a venue
   where an entry carrying a stop is refused, because the risk kernel's
   every-entry-carries-a-stop rule would otherwise be silently unenforced.
2. Add a variant to the `Venue` enum in `registry.rs` and delegate all
   twelve methods to it. Dispatch is an enum, not `Box<dyn VenueGateway>`: the trait
   uses `async fn`, which cannot be a trait object at all, and a closed enum
   keeps the whole set of venues visible in one place — which is what the
   fence below depends on.
3. Add the name to `KNOWN_VENUES` and to the `by_name` match. Nothing in
   `engine-core` changes: the loop is generic over the gateway type, and
   `assembly.rs` already asks for a venue by name.
4. Nothing for the fence. `engine-venue/tests/venue_fence.rs` walks the whole crate, so a
   new module is scanned the moment it exists, and a host that is not a demo
   host turns the suite red wherever it is written. A companion test refuses
   a venue *name* that reads like real money; the scan is the real fence and
   that one is the cheap second check.

A name selects a compiled-in adapter and cannot introduce an endpoint, which
is why config may choose the venue at all. Whether an adapter may touch real
money is that adapter's own decision, made in its own crate: the trait cannot
express an endpoint.

## What the engine does

| Capability | State |
| --- | --- |
| Decide, gate, make durable, sign, send | Done, and measured |
| The capital controls | In the kernel, with every load-time proof, down to the proof that the account gross cap sits inside what the reference could fund. `engine-risk/PORT_NOTES.md` has the line-by-line |
| The fleet's own risk limits | Done. `[risk] operational_profile_path` loads `configs/operational.mainnet.json` itself, so a cap the owner tightens tightens for both halves. Measured: the Python and Rust loaders read that file identically, field for field |
| Quantizing to tick and step, venue minimums | Done |
| Following a research target book | Done, and run against the demo account. The engine remembers what each strategy sent until the account reading shows it, so the window between a fill and the next reading cannot become a second entry — see the in-flight row below |
| One account, more than one sleeve | Done. Each sleeve names its own book path and that book reaches that sleeve only |
| Stating leverage at the venue | Done. The leverage a decision was sized at travels with it, and an order whose leverage cannot be set is not sent. Entries only |
| Resting entry quoting (place at touch, reprice, escalate, cross) | Done. Off unless a strategy asks: the follower takes `rest_entries = true`. Exits and trims never rest |
| Venue reconciliation and restart recovery | Done. Boot reads the venue's working orders and compares them, and the account, against the log |
| Stop verify, repair, and a durable breach latch | Done. A position missing its stop gets back the one the log says it was opened behind; exposure the log cannot account for latches the engine out of opening |
| Single-writer lease | Done. The engine joins the fleet's own `flock`, refuses to start when another process holds the account, and claims its log the same way |
| Notifications and a liveness watchdog | Done, by feeding the fleet's own watchdog rather than growing a second one. The engine writes a heartbeat file; `check_fleet_liveness.py` reads it and pages on stale, unreadable, or latched. Off unless a path is configured |
| Reaching the funded account | Done, behind the owner's switch. `bybit_mainnet` is a venue the engine can be pointed at, and it refuses to build unless `REAL_MONEY` is armed in the host credential file. Deployed and running in shadow; **it has never sent an order** — see below |
| Saying what the fills cost | Done. `is_maker` kept from the venue, `M0` written on every send, arrival shortfall / effective spread / fee / all-in derived, and the signed markout at 1s/15s/1m/5m written when its horizon comes due. `engine fills --wal PATH` is the read; five of the numbers are in the heartbeat |
| Saying what its own ids mean | Done. Strategy and symbol ids are positions, so the log records both tables — at boot and again whenever a book names a new symbol |
| A strategy reading its own position | Done. `my_position` is that strategy's own fills, moving the instant one arrives. The account reading beside it lags seconds and, on a two-sleeve account, is the sum of both |
| A strategy reading what it has in flight | Done. `in_flight` (`engine-core/src/covers.rs`) is the engine's own note of what a strategy sent that the account reading has not absorbed yet — booked at the send, at the quantized size that actually went, and released as the reading catches up or a reject, cancel, or refused exit ends it. Every plug gets the one truthful reading |
| Following a symbol a book names late | Done. A name the engine has never followed is taken on when a book asks for it: interned, subscribed, added to the gateway, taught to the private stream, and given an instrument rule. The four tables that map names to ids are checked against each other and a symbol they disagree about is dropped rather than traded |

### What is left

**Evidence.** Nothing above has ever run against real money. The mainnet path
exists, is fenced, and is tested; it has never carried an order. A capability
that has not been exercised is a claim, and the only thing that turns it into a
fact is running it — in shadow first, against the account, for long enough to
watch it.

**The engine is the account owner, in the repository and on the host.** It
reads the venue, writes `account_equity_usdt` into its heartbeat, both
producers size from that equity, both write an absolute target book, and the
engine reads each book, routes it to its own sleeve, and takes on symbols the
books name that no config listed. It is **live on the demo account** and holds
that account's single-writer lease. The funded engine runs in **shadow** and
has never sent an order.

- **The producers size from the engine.** They read `account_equity_usdt` and
  `account_observed_wall_ts_ms` out of the heartbeat and plan every entry as
  blocked when that reading is missing or stale. A host that takes this code
  with no engine running has a fleet that publishes exits and never opens a
  position. It fails closed and says so per cycle, but it says so in a cycle
  field, not in a page. Install, enable and watch the engine beat first.

  The heartbeat sends an **age**, and the renderer turns it into a wall stamp
  beside its own. The engine's other clocks are monotonic and count from an
  arbitrary instant near boot, so a raw `account_observed_ns` means nothing
  read from outside the process.

Two operator capabilities sit beside the engine, one built and one owed:

1. **Flatten** — `ops.sh flatten`, on the engine's own path. It stops the
   producers, then writes a book of explicit zero rows naming everything the
   engine says it holds, which the engine reads as "hold none of this" and
   closes. Explicit rows rather than an empty book, because an empty book only
   reaches the names the plug already has in hand. Dry run unless `--execute`.
2. **The hourly digest** is owed. [`notifications.md`](notifications.md) keeps
   its description as the specification for whatever renders it.

### The funded engine, concretely

**It is installed on the host and running in shadow.** It has never sent an
order and cannot: `shadow = true` in `engine-mainnet.toml` and
`ENGINE_LIVE=false` in `engine-mainnet.env`, two switches, both the owner's,
and a shadow engine takes no account lease. Delete
`/etc/liquidity-migration/engine-mainnet.env` to keep it off for good — a
stopped unit would not stick, because the deploy starts it wherever its env
file and the binary both exist.

The account it reads, the caps it reads them under, and why it is left running
rather than stopped are operational state, so they live in one place:
[STATE.md](../STATE.md) §The fleet.

The pieces, all in `deploy/`:

| File | What it is |
| --- | --- |
| `systemd/liquidity-migration-engine-mainnet.service` | The unit. Deliberately does **not** conflict with anything else on the box: what stops two *live* writers is the kernel lease, which knows the difference between shadow and live; a systemd conflict does not |
| `engine.mainnet.toml.template` | The engine's config: `venue = "bybit_mainnet"`, capital limits loaded from `configs/operational.mainnet.json` itself, one block per sleeve with its own book path |
| `engine.mainnet.env.template` | Unit settings: which config, shadow or live, where the heartbeat goes |

Both templates ship with `shadow = true` and `ENGINE_LIVE=false`, and both have
to say otherwise before anything is sent — the flag can only turn shadow off,
never on.

Each sleeve must be given names the other does not trade: the engine refuses a
config where two strategies claim one symbol, because the venue holds one
position per symbol and keeps no note of who asked for it.

**What is left is evidence, and only time produces it.** Watch the shadow run
against a funded account that is not near-empty, long enough to see what it
would have done and what it would have cost — `engine fills` off its log is the
reading that answers the second half. Only then, if it has earned it:
`ENGINE_LIVE=true` and `shadow = false`, by someone who meant it. Reversible
the same way. **That step is the owner's and nobody else's.**

## What the engine does not do

- No carry or continuous *decision* inside the engine — a carry decision is a
  batch over ninety days of funding and bars, so it stays in research and
  reaches the engine as a target book.
- No funded trading. The mainnet path is built, fenced, deployed and running
  in shadow; it has never sent an order.
- No market impact measurement. `engine fills` anchors on the top of book,
  which is the only book the engine carries. Walking a depth-50 book is the
  Python half's job and stays there.
- No FPGA/kernel-bypass pretensions: the venue is an HTTPS cloud service and
  single-digit milliseconds on-box is already far below its floor.
