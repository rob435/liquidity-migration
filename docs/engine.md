# The execution engine (Rust)

The program's purpose shifted on 2026-08-13: this repository now has two parts
with a hard wall between them.

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
  order.

The target book is written by
[`engine_targets.py`](../liquidity_migration/research/engine_targets.py),
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
it cannot rot, and left out of `KNOWN_STRATEGIES` so no config can run it by
accident.

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
measured); the VPS's Linux `fdatasync` measured ~2.2 ms in wave 3. The
pretend venue is plain HTTP on localhost, so the venue-side cost is short by
about one TLS record's work. Numbers are re-measured by running
`engine bench` — they live in the log the bench writes, not only here.

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
is faster to make durable, so the chain is shorter overall. The comparison
that matters is against the Python fleet on that same box: 25.7 ms of
software time per order, median. Same hardware, same venue, ~10× less time.

## Layout

`engine/` is a Cargo workspace. Contracts live in one crate; every other crate
implements or consumes them, which is what lets the parts be built in
parallel and integrate by type-check.

| Crate | Owns |
| --- | --- |
| `engine-types` | every shared type and trait: events, intents, orders, log records, the `Strategy` trait, and the capability traits (`Wal`, `VenueGateway`, `RiskKernel`) |
| `engine-wal` | the append-only log: CRC-framed records, buffered appends, an explicit durability barrier for order sends, group flush for everything else, replay with torn-tail truncation |
| `engine-marketdata` | Bybit public WebSocket: subscribe, sequence-check, parse once into flat per-symbol state, stamp arrival time |
| `engine-venue` | the venue gateway, demo or funded by realm: HMAC signing, pre-warmed keep-alive TLS, order create/cancel/amend, stop attach, leverage, position and wallet reads, the realm's private WebSocket for order/fill updates |
| `engine-risk` | the four capital controls, ported: account loss guard, equity-anchored envelope, per-strategy capital partition, stop-attach discipline. Fail-closed |
| `engine-core` | the loop: wires the above together, runs strategies, keeps the latency ledger, hosts the mock venue used for measurement |
| `engine-strategies` | the plugs: a registry from name + TOML to a boxed `Strategy` |

## Safety posture

- **The funded account is reachable, and only with the owner's switch.** This
  changed on 2026-08-14. The venue crate used to contain no mainnet hostname at
  all, which made a whole class of mistake impossible and also made the engine
  unable to do the job it was built for. What replaced it is the Python fleet's
  own rule, ported so both halves behave identically while both are running:

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

  The structural half is still checked against the source, and now checks
  something sharper than absence: the four venue hosts may be written in
  exactly one file, `realm.rs`. That closes the door that opens the moment a
  mainnet address exists anywhere — the test-only constructor takes a hostname,
  and no test, benchmark or module can spell one.

  `engine-marketdata` holds `wss://stream.bybit.com/v5/public/linear`
  separately, because Bybit serves demo public data from the mainnet stream. It
  is unauthenticated and read-only.

  Nothing has ever been sent to the funded account. The path exists, is fenced,
  and is tested; it has not been exercised.
- **Shadow mode is the default.** The engine computes intents and logs them;
  it only sends orders when started with an explicit live flag, and the risk
  kernel still gates every send.
- **The four capital controls are ported, not bypassed.** They were ported
  from `account_loss_guard.py`, `equity_anchored_envelope.py`,
  `venue_protection.py` and the partition in `account_kernel.py`, with
  table-driven parity tests written against those files' decision semantics.
  Those Python originals were deleted on 2026-08-14 once the port was proved,
  so the parity tests are now the reference rather than a comparison — read
  them and `engine-risk/PORT_NOTES.md` together. Unknown state refuses the
  order.
- **One writer per account — measured, then enforced.** On 2026-08-14 the
  engine held a 0.001 BTCUSDT position on the demo account for about a
  hundred seconds. The Python owner refused new intents for the whole of it:
  its native-protection reconciliation requires the venue's size and its own
  reconstruction to agree per symbol, and a position it did not place never
  can. It recovered by itself once the engine closed out. The separate-books
  policy covers a foreign position for trading; it does not cover this. So
  the two **cannot share an account**, even briefly.

  The engine now takes the fleet's own lease rather than relying on that
  being remembered. It is one kernel `flock` per venue account, at
  `/run/lock/liquidity-migration/bybit-{realm}-user-{userID}.lock`, and the
  engine joins it exactly — same directory, same name from the venue's own
  authenticated account number, same open flags, same re-proof after the lock
  that the file locked is still the file at that path. A lease that differed
  in any one of those would protect nothing: two processes would hold two
  different locks and each would believe it was alone. A live engine takes
  it before it boots and refuses to start if somebody has it, naming the
  holder. A shadow engine only looks, because a shadow run holding the lease
  would lock out the writer that does. There is no heartbeat and no expiry —
  the kernel drops the lock when the holder dies, which is the only expiry
  that cannot be wrong. The log file is claimed the same way, so two engines
  cannot share one log either.

## Worked entries

An entry can afford to wait for a price; an exit cannot. So exits cross and
entries may rest, and the engine — not the strategy — does the working. A
strategy attaches a `work` policy to its intent and the engine rewrites the
order into a resting limit at the touch, then walks it: it reads which way the
displayed size leans and rests inside the spread or behind the touch
accordingly, moves the order as the market moves, **never chases a touch that
fell away** (an order left alone at the front of the book is where you want to
be), escalates as the window runs out, and finally crosses at a bounded price
rather than sending an unbounded market order. Every number is the recipe the
Python fleet measured; the decision is a pure function and the engine's
group-flush tick drives it.

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

## Crash safety

One append-only log (`engine.wal`) is the engine's memory. Every record is a
length-prefixed, checksummed frame with a monotonic sequence number. The
order lifecycle is written as it happens: intent → risk verdict → order sent
(made durable *before* the bytes leave, so a crash can never forget an
in-flight order) → venue ack or reject → fills. On boot the engine replays
the log, truncates a torn tail at the crash point, and reconstructs what was
in flight before touching the venue.

Then it asks the venue. The log is a perfect record of what this engine did;
it is not a record of what happened to the account, because the venue keeps
trading while the engine is stopped and other hands reach the same account.
Boot is the one moment the two pictures can be compared:

- An order both agree is working is adopted and keeps being charged to the
  strategy that placed it.
- An order the log says is working and the venue has never heard of ended
  while the engine was down. Noted, not re-sent.
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

Proved against a real account on 2026-08-14: an engine restarted on its own log
while holding a live position found nothing wrong; the same position under a
fresh log reported `the venue holds 299 and this log accounts for 0` and
refused to open anything.

## Adding a venue

Venues differ in kind, not just in address, so a venue states what it can do
rather than the engine assuming. `VenueCaps` declares four things — whether
the venue holds a position-level stop the engine can set, whether a resting
order can be amended in place, whether post-only is honoured, whether orders
batch — and the engine refuses an action a venue cannot honour instead of
quietly substituting a different one. Cancel-and-replace is not an amend: it
is a new order at the back of the queue at a fresh price, and a strategy that
asked to move a quote would never learn it had been given something else.

The engine picks its venue by name: `[engine] venue = "bybit_demo"` in
`engine.toml`, resolved once in `engine-venue/src/registry.rs`. Leaving the
key out means the Bybit demo account, so every config written before the key
existed still says what it said. An unknown name is refused at boot, by name,
listing what the binary knows — never defaulted to a venue nobody chose.

To add one (Hyperliquid, MEXC), four steps in `engine-venue`:

1. Write the adapter as a module in the crate and implement `VenueGateway` —
   `caps`, `send_order`, `cancel_order`, `amend_order`, `set_stop`,
   `account_view`, `instrument_rules` — stating the capabilities honestly. A
   venue with no native position stop is not a broken venue; it is a venue
   where an entry carrying a stop is refused, because the risk kernel's
   every-entry-carries-a-stop rule would otherwise be silently unenforced.
2. Add a variant to the `Venue` enum in `registry.rs` and delegate all seven
   methods to it. Dispatch is an enum, not `Box<dyn VenueGateway>`: the trait
   uses `async fn`, which cannot be a trait object at all, and a closed enum
   keeps the whole set of venues visible in one place — which is what the
   fence below depends on.
3. Add the name to `KNOWN_VENUES` and to the `by_name` match. Nothing in
   `engine-core` changes: the loop is generic over the gateway type, and
   `assembly.rs` already asks for a venue by name.
4. Nothing for the fence. `tests/demo_fence.rs` walks the whole crate, so a
   new module is scanned the moment it exists, and a host that is not a demo
   host turns the suite red wherever it is written. A companion test refuses
   a venue *name* that reads like real money; the scan is the real fence and
   that one is the cheap second check.

A name selects a compiled-in adapter and cannot introduce an endpoint, which
is why config may choose the venue at all. Whether an adapter may touch real
money is that adapter's own decision, made in its own crate: the trait cannot
express an endpoint.

## What the engine cannot do yet

Measured against the Python fleet it is meant to replace, and kept honest
because the deletion order depends on it:

| Capability | State |
| --- | --- |
| Decide, gate, make durable, sign, send | Done, and measured |
| The four capital controls | Ported whole, including every load-time proof — the last one, that the account gross cap sits inside what the reference could fund, landed 2026-08-14. `engine-risk/PORT_NOTES.md` has the line-by-line |
| The fleet's own risk limits | Done. `[risk] operational_profile_path` loads `configs/operational.mainnet.json` itself, so a cap the owner tightens tightens for both halves. Measured: the Python and Rust loaders read that file identically, field for field |
| Quantizing to tick and step, venue minimums | Done |
| Following a research target book | Built, tested, and run against the demo account. The plug remembers what it sent until the account reading shows it, so the window between a fill and the next reading cannot become a second entry |
| One account, more than one sleeve | Done. Each sleeve names its own book path and that book reaches that sleeve only. Before this a book went to every strategy, and two followers on one account would have fought over the same positions |
| Stating leverage at the venue | Done. The leverage a decision was sized at travels with it, and an order whose leverage cannot be set is not sent. Entries only |
| Resting entry quoting (place at touch, reprice, escalate, cross) | Done. Off unless a strategy asks: the follower takes `rest_entries = true`. Exits and trims never rest |
| Venue reconciliation and restart recovery | Done. Boot reads the venue's working orders and compares them, and the account, against the log |
| Stop verify, repair, and a durable breach latch | Done. A position missing its stop gets back the one the log says it was opened behind; exposure the log cannot account for latches the engine out of opening |
| Single-writer lease | Done. The engine joins the fleet's own `flock`, refuses to start when another process holds the account, and claims its log the same way |
| Notifications and a liveness watchdog | Done, by feeding the fleet's own watchdog rather than growing a second one. The engine writes a heartbeat file; `check_fleet_liveness.py` reads it and pages on stale, unreadable, or latched. Off unless a path is configured |
| Reaching the funded account | Done, behind the owner's switch. `bybit_mainnet` is a venue the engine can be pointed at, and it refuses to build unless `REAL_MONEY` is armed in the host credential file. **Never exercised** — see below |
| Following a symbol a book names late | Done. A name the engine has never followed is taken on when a book asks for it: interned, subscribed, added to the gateway, taught to the private stream, and given an instrument rule. The four tables that map names to ids are checked against each other and a symbol they disagree about is dropped rather than traded |

### What that leaves

Not a capability, as of 2026-08-14. Every row above is Done, and on the same
day the Python order path was deleted, so there is no second implementation to
compare against any more.

What is left is **evidence**. Nothing above has ever run against real money.
The mainnet path exists, is fenced, and is tested; it has never carried an
order. A capability that has not been exercised is a claim, and the only thing
that turns it into a fact is running it — in shadow first, against the account,
for long enough to watch it.

**The engine is the account owner in the repository, and not yet on the host.**
The host runs its own installed checkout and keeps trading the old way until
somebody deploys. That deploy is the moment this becomes real, and it has one
precondition that is easy to miss:

- **The producers size from the engine.** They read `account_equity_usdt` and
  `account_observed_ns` out of the heartbeat and plan every entry as blocked
  when that reading is missing or stale. A host that takes this code with no
  engine running has a fleet that publishes exits and never opens a position.
  It fails closed and says so per cycle, but it says so in a cycle field, not
  in a page. Install, enable and watch the engine beat first.

**Two capabilities did not survive the deletion**, and the engine does not have
them:

1. **Flatten.** The operator's "close everything now" — `ops.sh flatten` and
   the Telegram close button — worked by publishing zero targets into the
   account owner's intent inbox. The engine reads target *book files* and has
   no inbox, so nothing drains those requests. Both routes now refuse with that
   explanation rather than appearing to work. Closing a book is a manual job at
   the venue, or a matter of stopping the sleeve and letting exits run.
2. **The hourly digest.** Rendered by the owner from the canonical journal.
   `docs/notifications.md` keeps its description as the specification for
   whatever replaces it.

Neither is hard to build on top of what exists — a flatten is a target book of
zeros, which the engine already follows — but neither has been built, and
saying so is the point of this section.

And the producers are not the order path. A carry decision is a batch over
ninety days of funding and bars; it stays in research and arrives as a target
book. "Delete the Python one" never meant deleting that, and it did not.

### The handover, concretely

Everything below is the owner's. Nothing in this repository does any of it, and
nothing here is installed on the funded host — the unit and the two templates
exist so the steps are a command rather than a design exercise.

The pieces, all in `deploy/`:

| File | What it is |
| --- | --- |
| `systemd/liquidity-migration-engine-mainnet.service` | The unit. Deliberately does **not** conflict with the Python mainnet owner: a shadow engine takes no lease and is meant to run alongside it, which is step 2 below. What stops two *live* writers is the kernel lease, which knows the difference between shadow and live; a systemd conflict does not |
| `engine.mainnet.toml.template` | The engine's config: `venue = "bybit_mainnet"`, capital limits loaded from `configs/operational.mainnet.json` itself, one block per sleeve with its own book path |
| `engine.mainnet.env.template` | Unit settings: which config, shadow or live, where the heartbeat goes |

Both templates ship with `shadow = true` and `ENGINE_LIVE=false`, and both have
to say otherwise before anything is sent — the flag can only turn shadow off,
never on.

The symbol lists in the config template are placeholders. Replace them with the
real per-sleeve universes, and give each sleeve names the other does not trade:
the engine refuses a config where two strategies claim one symbol, because the
venue holds one position per symbol and keeps no note of who asked for it.

**The order these happen in matters more than any of them.**

1. Install the unit and fill in the two files. Nothing runs yet.
2. Start it in shadow, alongside the Python owner. A shadow engine takes no
   lease, so the two coexist. It computes what it would do and logs it, and
   the heartbeat starts feeding the fleet's own watchdog.
3. Watch. This is the whole point of the exercise, and there is no shortcut
   through it. What is being looked for is the engine and the Python owner
   reaching the same decisions on the same books — and where they differ,
   which one is right.
4. Only then, if it has earned it: `ENGINE_LIVE=true` and `shadow = false`,
   then stop the Python owner and start this. Two commands in that order, by
   someone who meant it — a live engine refuses to start while the owner holds
   the account's lease, so the order is not optional. Reversible the same way:
   stop the engine, start the owner.

`REAL_MONEY=true` in `/etc/liquidity-migration/bybit-mainnet.env` is a
precondition for step 2, not part of it. It is already set, by the owner's own
hand, and it is what the funded fleet runs under today.

### The order it can actually happen in

Each step earns the next. Nothing here is a policy anyone chose to impose; each
one is what the step after it needs in order not to be reckless.

1. **The engine can be seen from outside.** Done — it writes a heartbeat and
   the fleet's watchdog pages on stale, unreadable, or latched. Nothing can
   stand down while nothing can tell you the replacement is sick.
2. **The engine enforces what the fleet enforces.** Done — and now from the
   same document rather than a copy of its numbers.
3. **The engine can widen its universe without a restart.** Done — a name a
   book mentions for the first time is taken on while the engine runs. This
   was the last thing it did worse than the owner it would replace.
4. **The engine runs as a service, on its own demo account, in shadow.** This
   is the first step that produces evidence continuously rather than once, and
   it is where the comparison against the Python owner's decisions starts.
5. **The demo Python owner becomes retirable.** That means decoupling
   `verify_topology` from it — the sleeve toggles a few lines above already
   show the shape. Not before step 4 has run long enough to be worth trusting.
6. **Mainnet.** The adapter, the leverage call, and the profile are built and
   fenced. What is left is not code: it is `REAL_MONEY`, set by the owner's
   own hand in the host credential file, and a shadow run against the funded
   account long enough to show the engine and the Python owner reaching the
   same decisions. **This step is the owner's and nobody else's.**
7. **Only then, the Python order path**, and even then the deploy engine
   (`ops/maintenance_lock.py`, `policy/systemd_environment.py`,
   `policy/real_money_arming.py`, `ops/candidate_rule_coverage.py`,
   `ops/reset_path_safety.py`) must survive or be rebuilt. Those are not the
   trading path; they are what ships it.

What changed on 2026-08-14 is that the distance stopped being capability at
all. The question now is not what the engine can do but whether anyone has
watched it do it — and the answer, on the funded account, is nobody, ever.

## What v1 does not do

- No carry or continuous *decision* port — a carry decision is a batch over
  ninety days of funding and bars, so it stays in research and reaches the
  engine as a target book.
- No mainnet. The engine builds and runs on the VPS from an isolated clone and
  has run in shadow against the demo account; it trades nothing.
- No FPGA/kernel-bypass pretensions: the venue is an HTTPS cloud service and
  single-digit milliseconds on-box is already far below its floor.
