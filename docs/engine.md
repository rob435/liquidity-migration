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
| `engine-venue` | the demo venue gateway: HMAC signing, pre-warmed keep-alive TLS, order create/cancel/amend, stop attach, position and wallet reads, the demo private WebSocket for order/fill updates |
| `engine-risk` | the four capital controls, ported: account loss guard, equity-anchored envelope, per-strategy capital partition, stop-attach discipline. Fail-closed |
| `engine-core` | the loop: wires the above together, runs strategies, keeps the latency ledger, hosts the mock venue used for measurement |
| `engine-strategies` | the plugs: a registry from name + TOML to a boxed `Strategy` |

## Safety posture

- **Demo only, by construction.** The venue crate contains the demo hostname
  and no other. There is no mainnet code path to misconfigure. Real-money
  arming remains exclusively the Python fleet's `REAL_MONEY` switch, set by
  the owner's hand; the engine has no equivalent and gets none without an
  explicit owner decision.
- **Shadow mode is the default.** The engine computes intents and logs them;
  it only sends orders when started with an explicit live flag, and the risk
  kernel still gates every send.
- **The four capital controls are ported, not bypassed.** The Python originals
  (`account_loss_guard.py`, `equity_anchored_envelope.py`,
  `venue_protection.py`, the partition in `account_kernel.py`) stay untouched
  and remain the reference; the Rust kernel carries table-driven parity tests
  against their decision semantics. Unknown state refuses the order.
- **One writer per account.** The engine must never trade the same account as
  the Python fleet at the same time — that is the wedge we already lived
  through once. Deployment posture is an owner decision recorded in
  CHANGELOG when it happens.

## Crash safety

One append-only log (`engine.wal`) is the engine's memory. Every record is a
length-prefixed, checksummed frame with a monotonic sequence number. The
order lifecycle is written as it happens: intent → risk verdict → order sent
(made durable *before* the bytes leave, so a crash can never forget an
in-flight order) → venue ack or reject → fills. On boot the engine replays
the log, truncates a torn tail at the crash point, and reconstructs what was
in flight before touching the venue.

## Adding a venue

Venues differ in kind, not just in address, so a venue states what it can do
rather than the engine assuming. `VenueCaps` declares four things — whether
the venue holds a position-level stop the engine can set, whether a resting
order can be amended in place, whether post-only is honoured, whether orders
batch — and the engine refuses an action a venue cannot honour instead of
quietly substituting a different one. Cancel-and-replace is not an amend: it
is a new order at the back of the queue at a fresh price, and a strategy that
asked to move a quote would never learn it had been given something else.

To add one (Hyperliquid, MEXC): implement `VenueGateway` in a crate of its
own — `caps`, `send_order`, `cancel_order`, `amend_order`, `set_stop`,
`account_view`, `instrument_rules` — and state the capabilities honestly. A
venue with no native position stop is not a broken venue; it is a venue where
an entry carrying a stop is refused, because the risk kernel's
every-entry-carries-a-stop rule would otherwise be silently unenforced.
Whether an adapter may touch real money is that adapter's own decision, made
in its own crate: the trait cannot express an endpoint.

## What the engine cannot do yet

Measured against the Python fleet it is meant to replace, and kept honest
because the deletion order depends on it:

| Capability | State |
| --- | --- |
| Decide, gate, make durable, sign, send | Done, and measured |
| The four capital controls | Ported, minus four account caps named in `engine-risk/PORT_NOTES.md` |
| Quantizing to tick and step, venue minimums | Done |
| Following a research target book | Seam built; the follower is the current work |
| Resting entry quoting (place at touch, reprice, escalate, cross) | **Absent.** The engine sends market orders. This is the largest execution-quality gap |
| Venue reconciliation and restart recovery | **Absent.** The engine replays its own log but never asks the venue what happened to an order it lost |
| Stop verify, repair, and a durable breach latch | **Attach only.** `set_stop` exists and is not yet called after a fill |
| Single-writer lease | **Absent.** Nothing but deployment discipline stops the engine and the Python fleet trading one account at once — the wedge we already lived through |
| Notifications and a liveness watchdog | **Absent.** Nothing outside the process can tell whether the engine is healthy |

Until the four absent rows are built and proven, the Python execution path
stays: deleting it would not be a risk, it would simply stop the fleet
trading.

## What v1 does not do

- No carry or continuous *decision* port — a carry decision is a batch over
  ninety days of funding and bars, so it stays in research and reaches the
  engine as a target book.
- No mainnet. The engine builds and runs on the VPS from an isolated clone and
  has run in shadow against the demo account; it trades nothing.
- No FPGA/kernel-bypass pretensions: the venue is an HTTPS cloud service and
  single-digit milliseconds on-box is already far below its floor.
