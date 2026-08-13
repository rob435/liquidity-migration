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

## The one-sentence design

One process, one thread, one loop: a market message arrives on a socket, is
parsed once into shared memory, the strategy decides, the risk kernel gates,
the order record is made durable in the append-only log, the signed order
leaves on a pre-warmed connection — all without leaving that loop.

## Honest latency contract

Geography is not ours to fix in software. From the current box, one round trip
to the venue is ~175 ms; no rebuild changes that. What the engine owns is our
side of the wire, and that is what we measure and promise:

| Segment | Target | Why believable |
| --- | --- | --- |
| market message in → decision made | tens of microseconds | one parse, flat structs, no allocation on the hot path |
| decision → order durable in the log | < 3 ms worst (one fdatasync) | measured ~2.2 ms/fsync on the VPS disk; ~0.1 ms on NVMe |
| durable → signed bytes on the socket | < 1 ms | HMAC of a small body + write to a warm TLS connection |
| **in → wire, whole chain** | **< 5 ms p99 on VPS disk, < 1 ms on NVMe** | sum of the above |

Every event is stamped four times (`recv_ns`, `intent_ns`, `wal_ns`,
`wire_ns`) and the engine ships its own measurement: a mock venue lets us run
the full chain on one box and read the histograms, so the numbers above are
checked by the engine itself, not asserted in a doc. The <100 ms
decision-to-execution goal is therefore met on our side of the wire with two
orders of magnitude to spare; the venue round trip on top is the same
geography every non-colocated participant pays.

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

## What v1 does not do

- No carry or continuous port — their edge is measured in hours, not
  milliseconds; they stay on the Python fleet.
- No mainnet, no VPS deploy, no relocation dependency.
- No FPGA/kernel-bypass pretensions: the venue is an HTTPS cloud service and
  single-digit milliseconds on-box is already far below its floor.
