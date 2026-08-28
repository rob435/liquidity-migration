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
  symbol, optionally an exact signed base quantity, the stop each carries, and
  how long it may be acted on. When exact quantity is present the engine
  converges to it directly instead of re-sizing from a later price. The engine
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

`engine strategies` lists the plugs the current binary can load.

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

One process, one thread, one loop: a market message is parsed once into shared
memory, the strategy decides, and risk is reserved in deterministic order.
Sibling placements are appended together, made durable with one barrier, then
handed to the selected venue adapter. Bybit overlaps distinct-symbol signed
HTTP chains over ten pre-warmed connections and preserves strict wire order
within each symbol because one-way positions share one Full stop.

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
| **message → durable → local submit result** | **3.9 ms** | **5.0 ms** | **10.8 ms** |

The durable step is nearly the whole chain, and it is the platform's price:
on macOS, Rust's `sync_data` is a full drive-cache flush (~3.2 ms/barrier
measured); the VPS's Linux `fdatasync` measures ~2.2 ms. The pretend venue is
plain HTTP on localhost, so the venue-side cost is short by about one TLS
record's work. Numbers are re-measured by running `engine bench` — they live
in the log the bench writes, not only here.

The local submit-call path is below 100 ms with a ~20× margin at p99. This is
not a decision-to-execution claim: the venue round trip remains on top and is
the same geography every non-colocated participant pays.

### On the production box

Measured 2026-08-28 by three consecutive native release runs on the VPS the
fleet runs on (2 cores, Linux, `fdatasync`; 20,000 quotes in and 1,000 orders
out per run, alongside the running fleet). The table reports the median of the
three run medians, the median of the three run p99s, and the largest observed
value across all three runs:

| Segment | median | p99 | worst |
| --- | --- | --- | --- |
| market message in → decision made | 80 ns | 199 ns | 25.0 µs |
| decision → order durable in the log | 1.06 ms | 2.75 ms | 6.77 ms |
| local API round trip | 164.1 µs | 921.1 µs | 1.62 ms |
| **message → durable → local submit result** | **1.26 ms** | **3.16 ms** | **7.52 ms** |

The disk flush still dominates the chain. Stop protection is indexed by
symbol and side: a decision reads one tightest live level per active key
instead of rescanning order history. With the WAL on memory-backed storage,
the durability median remains 14.6 µs at 10,000 outstanding orders; the
pre-index path reached 265 µs. This isolates the in-process scaling cost from
`fdatasync` noise.

### Sibling placement groups

One decision can emit several adjacent placements. The engine validates and
reserves them in deterministic request order, appends every accepted order,
and uses one durability barrier for the group. Only then does it call
`VenueGateway::send_orders`. Bybit overlaps independent symbol chains and sends
siblings within one symbol serially; the default adapter implementation remains
serial for venues whose nonces require wire order. A cancel, amend, or stop
change flushes the placement group first. Create, cancel, batch-cancel, amend,
trading-stop, leverage, and startup position reads each have their own
completion-anchored rolling quota. Bybit's HTTP/1.1 pool retains sixteen idle
sockets, so a ten-symbol burst does not evict its own warmed connections.

An opening reprice is assessed again before wire and gets the same durability
barrier as a placement. Until the venue gives a definitive answer, replay
retains the full old/requested price range: the high end charges notional and
both ends charge stop loss. The current venue contract has no correlated
private confirmation of an effective amend price, and Bybit's REST success is
only asynchronous acceptance, so every accepted or transport-ambiguous
opening reprice is canceled rather than falsely resolved; its range remains
reserved until cancellation is confirmed.

The latency tables here measure the single-order path. A deterministic Linux
integration test holds each of three distinct-symbol venue responses for
100 ms; five runs complete in 0.10–0.11 s and observe all three requests in
flight together. The same-symbol control observes one request in flight and
keeps request order. This proves local overlap, not live venue latency; no live
venue sample establishes current sibling-group completion time.

### Long-run account-state probe

The execution-ID set is bounded, but venue history can still make recovery
slower. Measure both effects inside one release process from `engine/`:

```text
cargo run -p engine-core --release --example account_state_soak -- --operations 2000000 --live-ids 65536 --sample-ops 4096 --history-rows 0,1000,10000,100000 --repeats 3
```

Add `--json` for machine-readable output. Both formats identify the engine
version, target OS and architecture, and whether the build is optimized. The
first phase prewarms the real bounded execution-ID set, retains 65,536 IDs, and
samples early, middle, and late insert-plus-duplicate paths. The second drives
the real engine boot path
with already-decoded synthetic execution history at each requested size and a
zero-I/O counting WAL. Steady-state percentiles are quantiles of each 4,096-op
window's mean nanoseconds per operation, not single-operation tail latency;
boot percentiles are per-boot samples. The probe excludes network time, JSON
decoding, venue query time, and disk durability. Ubuntu CI executes this exact
bounded workload in release mode and retains its JSON in the job log. That is
runner evidence, not a target-host or venue-history claim: run the same release
command on the target class and compare windows within one run, then measure
real fetch and decode separately when venue history is the suspect.

A 2026-08-28 native target-host run retained 65,536 live IDs across two
million operations. Its sampled mean cost does not grow within the run:

| Window | p50 mean/op | p99 mean/op | largest sampled mean/op |
| --- | ---: | ---: | ---: |
| early | 686 ns | 939 ns | 4.89 µs |
| middle | 678 ns | 1.57 µs | 4.97 µs |
| late | 522 ns | 862 ns | 874 ns |

Already-decoded synthetic recovery history remains the growing leg:

| History rows | median boot path | median per row |
| ---: | ---: | ---: |
| 0 | 52.4 µs | — |
| 1,000 | 1.26 ms | 1.26 µs |
| 10,000 | 23.8 ms | 2.38 µs |
| 100,000 | 401.5 ms | 4.02 µs |

That per-row rise is worse than linear and remains an operational startup
cost. The probe deliberately excludes venue fetch, JSON decoding, network,
and durable WAL time; those require a separate real-account measurement.

### Against the real venue

Measured 2026-08-18 from the live demo engine's own log — all 67 real orders
placed since 2026-08-14. The durable `OrderSent.wire_ns` field is stamped
before append and fsync, while the acknowledgement is stamped after response
parsing. Those records establish the total decision-to-acknowledgement time,
but they cannot honestly split disk, socket-write, and venue time.

| Leg | median | p90 | worst |
| --- | --- | --- | --- |
| decision → parsed venue acknowledgement | 179 ms | 512 ms | 1.01 s |

The sample also shows 27 of those 67 entries making an inline leverage
confirmation before submission. The historical fields do not support a clean
latency split for that extra call. Under `leverage_authority = "sole"` (the demo config)
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
| `engine-wal` | the append-only log: CRC-framed records, buffered appends, one explicit durability barrier per sibling placement group, group flush for everything else, replay with torn-tail truncation, size-triggered rotation into archived segments |
| `engine-marketdata` | every venue's public feed: subscribe or poll, parse once into flat per-symbol state, stamp arrival time. Bybit and MEXC enforce their numbered depth chains and resync on gaps; Lighter enforces its nonce chain; Hyperliquid rejects timestamp regression but its protocol cannot prove forward continuity. One enum is built from the same venue name as the gateway |
| `engine-venue` | five venue adapters, one directory each: realm selection, signing, live instrument rules, pre-warmed keep-alive TLS, and the account, order, and stream capabilities that venue supports. Unsupported capabilities fail explicitly |
| `engine-risk` | the capital controls: durable account daily-loss halt, equity-anchored envelope, account-wide caps, stop-attach discipline. Fail-closed |
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

  The structural half is checked against the source: every venue host may be
  written in exactly one file, that venue's own `realm.rs`. That closes the
  door a real-money address opens the moment it exists anywhere — each
  gateway's test-only constructor takes a hostname, and no test, benchmark or
  module can spell one. The scan reads `engine-marketdata` too, which needs
  hosts for public prices and no credential to use them; it takes them from the
  realm tables instead of writing its own.

  Bybit is the only adapter with live-order evidence. Compile and request-shape
  tests are the evidence boundary for the other adapters; they are not
  production validation.
- **Bybit proves one-way mode before startup completes.** During account
  identity, the adapter makes one signed read-only position query per configured
  symbol and requires exactly one matching `linear` row with `positionIdx 0`
  and no next page. Any missing, duplicate, malformed, hedge-mode, rejected, or
  failed response stops startup. A symbol added after boot is checked before its
  first order, stop, or leverage request. The engine never changes venue mode,
  does not check margin mode, and cannot prevent an operator changing mode after
  a successful check.
- **Funded Bybit identity enforces the execution-key shape.** The venue must report
  a UTA, write-capable key with
  ContractTrade Order and Position, no Wallet Withdraw, and exactly the one host
  IP declared by `BYBIT_REAL_API_KEY_IP`. A mismatch stops identity before the
  account is accepted. `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` must also equal
  the authenticated UID. That value is an operator acknowledgement that the
  funded UID has no hand trading, venue bots, copy trading, or other trading API
  keys; it is required because Bybit exposes no account-wide list for every bot
  family and is not itself a machine proof of exclusivity. The owner rotation
  workflow is in [`operations.md`](operations.md) §Funded key rotation.
- **Optional funded inventory uses a different, globally read-only key.** The narrow
  `InventoryProbe` reads `BYBIT_ATTEST_API_KEY` and
  `BYBIT_ATTEST_API_SECRET`, verifies UTA, exact single-host IP, the required
  ContractTrade and Wallet query scopes, global read-only status, and no
  withdrawal permission. The
  transient mainnet `attest-flat` and `loss-reset` services remove the execution
  key and `REAL_MONEY` from their environments. Persistent engines never load
  the attestor file, and deployment does not require it.
- **The engine sends orders, and the risk kernel gates every one.** It carries
  no mode of its own: whether the funded fleet runs at all is `REAL_MONEY` in
  the host credential file, and nothing else. Logs written before that was the
  only toggle still hold never-sent markers, and the reader keeps understanding
  them (`inflight.rs`) — a name that has been written cannot be dropped from
  the reader.
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
    that produced them when the log knows it. A failed history request aborts
    boot. After a live private-stream gap, the same failure durably latches
    `may_open=false` and stops the run. Boot also aborts when the required
    recovery interval predates the venue's history reach; clipping the interval
    would silently omit fills. `reconcile-clear` is the explicit review path
    for older debt.
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
  order.** The daily-loss halt, equity-anchored envelope, and stop-attach
  discipline live in `engine-risk`, each with table-driven tests over its
  decision semantics. Every cap is account-wide: no sleeve holds a private
  share, so any one of them can spend the lot. The tests are the executable
  decision contract.
- **The funded daily-loss trip is durable and operator-cleared.** Its first
  fresh account view of the UTC day anchors total account equity. At equity no
  more than 10 USDT below that opening, new risk stops and genuine reduce-only
  exits continue. The anchor and trip cross a WAL barrier and survive restart;
  a trip does not clear on recovery or day change. `scripts/ops.sh loss-reset
  --environment demo|mainnet --note TEXT [--execute]` requires that realm's
  engine and producers stopped and a fresh, credential-wide flat account.
  Mainnet venue reads use only the read-only attestor key. Dry-run writes
  nothing; execute writes the local WAL note and cleared anchor durably without
  gaining venue-mutation authority.
- **Instrument rules come from the selected Rust adapter at boot.** The engine
  aborts when the fetch fails or when a configured symbol has no rule. Quantity
  steps, price ticks, and venue minimums are live startup inputs, not deploy
  receipts.
- **One local engine per account.** The engine takes the fleet's own lease: one
  kernel `flock` per venue account, at
  `/run/lock/liquidity-migration/{venue}-{realm}-user-{account}.lock`, joined
  exactly — same directory, same name from the venue's own authenticated
  account (Bybit's and Lighter's account number, Hyperliquid's wallet address,
  and Bybit's spelling is byte-for-byte the Python fleet's because the two
  share that file), same open flags, same re-proof after the lock that the file
  locked is still the file at that path. A lease that differed in any one of
  those would protect nothing: two processes would hold two different locks
  and each would believe it was alone. The engine takes it before it boots
  and refuses to start if somebody has it, naming the holder. There is no
  heartbeat and no expiry — the kernel drops the lock
  when the holder dies, which is the only expiry that cannot be wrong. The log
  file is claimed the same way, so two engines cannot share one log either.
  A kernel lease cannot stop a venue UI, bot, or different API key. The funded
  dedicated-UID contract excludes those external writers separately.

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

**Patience runs to the deadline.** The order is moved every 15 s and no early
cross ends the wait, whatever the market does in between. A tape sweep of 24
policies over 20 symbols put a number on why: a rest that fills costs 3.53 bp
against 7.71 for crossing, and a rest that *misses* costs 8.65 — only 0.94 bp
worse than crossing at the start. Waiting is nearly free and filling is worth
4.18, so resting pays unless it fills less than 18% of the time. The window is
also the only dial that moves the result; chasing was worth 0.20 bp across
that sweep and resting a tick inside the touch 0.05.

Two gates decide whether resting is worth it at all: the spread must be at
least two ticks **and** at least one basis point of the price. The second is
the one that bites on high-priced instruments — BTC's spread is routinely
under a basis point, so an entry there crosses whatever the config says.

Off unless asked. `rest_entries = true` on the target-book follower turns it
on; a trim or an exit is never worked, whatever the config says.

Two dials change the recipe above, and both are off unless a sleeve sets them,
because what they replace was measured over 199,785 paired attempts and they
were not:

- **`hold_decision_price = true`** — the first rest sits at the mid the order
  was decided against rather than at the touch, and the order never moves to a
  worse price than that. Nothing is bought above, or sold below, the price the
  strategy decided on. It pays for that in fill rate: a market that walks away
  is no longer followed, and the order simply sits.
- **`give_up_instead_of_crossing = true`** — when patience runs out, at the
  window's end or because the drift already proved waiting more expensive than
  the spread, the order is taken down instead of crossing for what is left.
  Without it the price cap only delays the cross; with it a missed entry is a
  trade not taken, and the strategy decides again on its next pass.

Either of them without `rest_entries` is refused at boot: nothing would rest,
so they would sit in the config doing nothing.

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

One row per sleeve and coin: maker share, fee, arrival shortfall, all-in, and
the signed markout at 1 s, 15 s, 1 min and 5 min. The footer confesses rather
than staying silent — what share of the traded notional had a book to be
measured against, how many horizons never found one, how many were read too
late to be the horizon they claim, what was dropped, what is still waiting, and
how many records of how many segments the numbers came from. Five of those
numbers are also in the heartbeat, so an operator sees them without the log.

Under that table is a second one: **what the positions made**. A sleeve's fills
in a coin add up to a position, and when that position comes back to flat the
round trip is closed — per sleeve, how many closed, how many won, the total
after fees, the best and the worst, and then the newest thirty one to a line
with entry, exit, time held and money. The arithmetic is one running sum: a buy
pays out and a sell takes in, so the cash left over when the quantity returns to
zero IS the gross, whichever way round the position was.

**The crowd fee (funding) is in none of it.** The venue settles that into the
wallet on its own eight-hourly clock and never tells the engine, so a net
carrying it would be an estimate in a receipt's clothes. Everything else is a
receipt out of the log.

A position whose opening fills are in an older segment is reported as closed
with no money on it, and the footer counts how many. A rotation restates the
*quantity* each sleeve holds and not what it paid, so a close over that boundary
is knowable and its P&L is not — and inventing one is worse than a gap: without
the restated quantity, the sale that closes such a position reads as opening a
short, and the purchase that opens the next position closes that phantom for a
profit nobody made.

The same accounting runs live. `trades_path` in `engine.toml` names a file the
engine appends one JSON line to per closed position, which is what puts an exit
on the owner's phone with its numbers ([`notifications.md`](notifications.md)
§The trading story).

A row is keyed by the **names** the ids meant where the record sits, not by the
ids. Within one run ids are only appended, but the next boot rebuilds both
tables from a config and a log whose universe has moved: id 8 has been
HYPEUSDT and BICOUSDT in one log, and a sleeve that was id 3 has since been
retired. Keyed by id, a report over a log that spans boots adds two coins'
trading into one row and labels it with whatever the last table called it. Two
sleeves cannot share a name — boot refuses a config where two blocks claim one
— so only a log written before sleeves were named puts two of them in a row.

Two rules keep it from measuring nothing and calling it something. A markout
is only taken against a book that **arrived at or after the horizon it
measures** — a halted or delisted symbol keeps its last quote for ever, and
four horizons marking against the identical mid read exactly like a
measurement while being a measurement of nothing; a book that spoke once just
after the fill and went quiet is the same trap one step later. And a mark that
turns up long after its horizon is not that horizon: a stall or a replayed
backlog would otherwise be averaged into the one-second column at full weight.

What it cannot repair, it confesses. A private-stream reconnection is a window
in which fills happened and were delivered to nobody; the engine asks the venue
for its own execution history afterwards, and what comes back is priced against
its order's own `M0` like any other fill. The footer says how many gaps there
were and how many fills arrived that way. Recovery can run minutes after the
trade, and a fill found then is dated to when it happened rather than when it
was found, so its horizons read as already past instead of being marked
against a book from long afterwards; one older than the engine's clock — whose
origin is the process — is owed no mark at all. A restart ends every horizon a
fill was still owed, so the later columns cover less of the trading than the
earlier ones.

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
  while the engine was down. Its ending is written into the log and the
  reservation it holds against the account caps is released — no update for it
  will ever arrive, and left "in flight" it would charge those caps on every
  future boot and hold the one-order-per-symbol gate closed against its symbol,
  exits included. Nothing is re-sent.
- An order the log never placed is reported. If it is in a symbol a configured
  strategy trades, the engine durably stops opening. An order in an
  unaddressable symbol is reported without opening the latch. A blank client id
  is accepted as venue-native only when the adapter positively identifies a
  full reduce-only stop; ambiguous working orders invalidate the snapshot.
- A fill that cannot join to an order in the log and position quantity the
  log's own fills cannot account for durably stop the engine opening. Neither
  becomes trusted exposure. Reducing and stop protection stay allowed.
- A position with a missing or looser stop gets back the directionally tighter
  stop proved by the fills that actually built that position. Merely sending,
  rejecting, or leaving an opposite sibling unfilled cannot change repair
  authority. Same-side growth retains the tighter prior level; a genuine cross
  takes the crossing order's stop. Every fresh account view supervises this
  invariant and latches opening off if repair fails. If the log cannot prove a
  side and level, the engine refuses rather than inventing either.

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
reconciliation compares the venue against, and the fill-owned intended stop
with its position direction. Legacy restatements without a direction still
parse, but cannot authorize an automatic repair.
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

## The venues

Five are compiled in, and one name in `engine.toml` picks between them:

| `venue =` | What it is | Readiness |
| --- | --- | --- |
| `bybit_demo` | Bybit's practice account | `live-proven` |
| `bybit_mainnet` | Bybit's funded account | `live-proven` |
| `hyperliquid_testnet` | Hyperliquid's testnet | `testnet-canary` |
| `hyperliquid_mainnet` | Hyperliquid's funded account | `production-blocked` |
| `lighter_testnet` | Lighter's testnet rollup | `testnet-canary` |
| `lighter_mainnet` | Lighter's funded account | `production-blocked` |
| `mexc_mainnet` | MEXC funded futures — its only realm | `production-blocked` |
| `variational_mainnet` | Variational public data | `read-only` |

`engine venues` prints this table from the binary's exhaustive registry.
`engine run` refuses `production-blocked` and `read-only` realms before it opens
the WAL, reads credentials, or opens a socket. Moving an exact realm to
`live-proven` is a reviewed source change backed by its live order lifecycle;
another realm's result does not qualify it. Testnet-canary status permits the
Hyperliquid and Lighter practice realms only so they can gather that evidence.

Only Bybit has live-order evidence. Hyperliquid, Lighter, and MEXC compile and
pass offline adapter tests only. Feed continuity is
protocol-specific: MEXC requires consecutive unmerged-depth versions and emits
no ticker-derived quote before a depth epoch exists; Lighter requires each
update range to begin at the prior nonce and advance; Hyperliquid rejects a
same-symbol BBO timestamp regression but cannot detect a forward gap. Lighter
cannot open until its leverage transaction exists, MEXC has no practice realm, and
Variational has no account or trading API. Do not treat a successful build as
authority to arm any of these paths.

MEXC still prices from the complete ticker touch. The quote's sequence field is
the latest continuously observed depth version, not proof that the ticker and
that depth update are the same causal snapshot. Its incremental depth is not a
complete ladder without a separate snapshot bootstrap.

**One name decides three things**: the gateway that sends orders, the private
stream that reports what happened to them, and the public feed the strategies
price against. `runner.rs` parses the name once and hands the same value to all
three constructors, so a config cannot half-switch — send orders to one venue
and price them off another's book. An unknown name is refused at boot, by name,
listing what the binary knows.

A name is not an address. Every host any adapter knows is written in that
venue's own `src/venues/<venue>/realm.rs` and nowhere else, and
`engine-venue/tests/venue_fence.rs` reads the whole tree back — including the
market-data crate — to prove it. So no edit to `engine.toml` can point the
engine at an endpoint nobody compiled in.

`REAL_MONEY` is the one arming switch: a real-money realm refuses to build
without it, a practice realm refuses to build with it, and the rule is stated
once in `engine-venue/src/arming.rs` rather than restated per venue. Each realm
reads its own credential variables, disjoint across every realm of every venue,
and a test walks the venue list to prove no two share one.

The check runs at the credential read, so it reaches every realm that reads a
credential — every one but Variational, which authenticates nothing because
there is nothing there to authenticate against. Its realm still reports itself
as real money, and the moment a trading API arrives it reads credentials
through the same path and is armed like the rest.

### What differs between them, where it changes a decision

Venues differ in kind, not just in address, so each states what it can do
rather than the engine assuming. `VenueCaps` declares three things — position
stop, amend in place, leverage — and the engine reads every one of them and
refuses an action a venue cannot honour instead of quietly substituting a
different one. Nothing is declared that nothing reads.
Cancel-and-replace is not an amend: it is a new order at the back of the queue
at a fresh price, and a strategy that asked to move a quote would never learn
it had been given something else.

- **Funding periods differ.** Bybit quotes the rate for its next eight-hourly
  settlement. Hyperliquid pays **hourly** and quotes the hourly rate. A carry
  number carried between them without scaling is out by a factor of eight.
  Each feed reports what its venue states and converts nothing;
  `Ticker::next_funding_ms` says when the next one lands. **Lighter's public
  socket carries no funding at all** — no mark price, no index, no rate — so
  that feed emits no ticker rather than an invented zero, and a carry sleeve
  cannot be run from it.
- **Lighter cannot open a position today.** It has no leverage transaction in
  this adapter, and the engine refuses an entry that names a leverage it cannot
  set — the margin posted would not be the margin the position was sized at.
  The target-book follower names one on every entry, so an engine on Lighter
  reads the market, protects and exits what it holds, and opens nothing.
- **A few Hyperliquid assets cannot be subscribed.** The venue spells `kPEPE`,
  `kBONK` and their kin with a lower-case prefix, and every symbol reaching the
  engine is upper-cased by the Python fleet's own books. The gateway folds the
  case and will trade them; the public feed names the coin in its subscription
  and cannot recover the venue's spelling, so it gets no book for them.
- **MEXC has no practice account.** Bybit has a demo realm, Hyperliquid and
  Lighter have testnets; MEXC publishes no testnet host for the futures API at
  all, so `mexc_mainnet` is the only spelling and every MEXC order is real
  money. It has no live-order evidence and `engine run` keeps it
  `production-blocked`.
- **MEXC counts contracts, not coins.** One contract is `contractSize` of the
  base coin — 0.0001 BTC, 1 XRP, 100 TUT — and fewer than a quarter of its
  contracts have that equal to 1. Sizes cross that boundary through the venue's
  own contract table in both directions. Its ten inverse contracts (the
  USD-quoted ones) are not listed at all: their contract size is denominated in
  the quote currency, and the linear rule would size them out by the price of a
  coin. MEXC also publishes `apiAllowed` per contract and sets it false on some,
  independently of whether the contract is otherwise live.
- **MEXC's REST and websocket are on different hosts.** The venue moved its REST
  domain in January 2026 and left the websocket behind. The retired REST host
  still answers, byte-identically and with no deprecation header, so nothing at
  runtime would notice a fallback to it.
- **Only Bybit keeps a stop on the position.** Hyperliquid and Lighter keep it
  as a separate reduce-only trigger order, so "is this position protected" is
  answered by reading the open orders, and a position with no such order comes
  back unprotected — which is what makes the risk kernel's every-entry-carries-
  a-stop rule mean the same thing on all three.
- **Only Bybit has a market order.** Hyperliquid takes an immediate-or-cancel
  limit priced through the book; Lighter takes a market order type whose price
  field bounds the fill.
- **Hyperliquid signs with a wallet key, Lighter with a curve key.** Neither is
  an HMAC secret. Hyperliquid's is an API wallet the account approved, which
  cannot withdraw; Lighter's is a private key registered against one of the
  account's API key slots. `engine venue-key --config engine.toml` prints what
  the host signs as, so it can be registered at the venue.
- **Lighter's fills arrive by resync, not by stream.** Its account channel
  names a fill by the venue's own order id, not by the client order index the
  engine minted, so a live fill cannot be attributed to the strategy that
  caused it. Its feed paces the engine's existing resync instead and the fills
  come from the venue's execution history, which does carry the ids. Fills are
  seconds late there, not milliseconds.
- **Variational cannot trade at all, and an engine cannot run on it.** The
  venue publishes market statistics and no trading API — no orders, and no
  account either. `account_view` therefore returns an error rather than a
  fabricated empty account, and the engine stops at boot on it: an equity of
  zero invented here would be a number the envelope and the partition judge
  real positions against. Its market feed works on its own, through
  `MarketFeeds`, without an engine. The order paths refuse with the reason, and
  it reports no instrument rules, so the engine's own "no rule, nothing can be
  sent" refusal would stop an order before the gateway's did.

### Adding a venue

Six steps, five in `engine-venue` and one next door:

1. A directory under `src/venues/`, with `realm.rs` holding its hosts,
   credential variable names, and whether each realm is real money. That file
   is the only place its hosts may appear.
2. Implement the required `VenueGateway` methods and state capabilities
   honestly. The default `send_orders` is serial; override it only when the
   venue's signing and nonce rules permit overlap or it offers a native batch.
   The `executions` default refuses, so missing history stops recovery.
   Credential-wide inventory belongs in a separate read-only `InventoryProbe`
   wrapper with no mutation methods; leaving it out keeps flatness attestation
   unavailable rather than falling back to configured symbols.
3. A variant in `VenueName` **and in `VenueName::ALL`**, then in `Venue` and
   `OrderFeeds`, plus `InventoryProbe` when the venue has the complete read
   (`engine-venue/src/registry.rs`). Give every exact realm an explicit
   `VenueReadiness`; a new mainnet stays `production-blocked` until reviewed
   live lifecycle evidence promotes that exact realm. `ALL` is the one list:
   the parser, discovery output, refusal, and completeness checks walk it.
4. A variant in `MarketFeeds` (`engine-marketdata/src/feeds.rs`). Its switch
   test matches exhaustively, so this does not compile until the test says
   which name reaches the new feed.
5. A row in `venue_hosts()` in `tests/venue_fence.rs`. The fence fails if a
   venue directory exists that it does not know about, so this cannot be
   forgotten silently.
6. Nothing in `engine-core`. The loop is generic over the gateway type, and
   `assembly.rs` already asks for a venue by name.

## What the engine does

| Capability | State |
| --- | --- |
| Decide, gate, make durable, sign, send | Done, and measured |
| The capital controls | In the kernel, with every load-time proof, down to the proof that the account gross cap sits inside what the reference could fund. The `engine-risk` tests are the executable contract. |
| The fleet's own risk limits | Done. `[risk] operational_profile_path` loads the preflight-validated host rendering also read by the funded producers, so reviewed defaults and the operator's carry-stop dial bind the Rust owner directly. |
| Quantizing to tick and step, venue minimums | Done |
| Following a research target book | Done, and run against the demo account. The engine remembers what each strategy sent until the account reading shows it, so the window between a fill and the next reading cannot become a second entry — see the in-flight row below |
| One account, more than one sleeve | Done. Each sleeve names its own book path and that book reaches that sleeve only |
| Stating leverage at the venue | Done. The leverage a decision was sized at travels with it, and an order whose leverage cannot be set is not sent. Entries only |
| Resting entry quoting (place at touch, reprice, escalate, cross) | Done. Off unless a strategy asks: the follower takes `rest_entries = true`. Exits and trims never rest |
| Venue reconciliation and restart recovery | Done. Boot reads the venue's working orders and compares them, and the account, against the log |
| Stop verify, repair, and a durable breach latch | Done. A missing or looser stop returns to the directionally tighter level proved by position-growing fills; unfilled siblings cannot control it, fresh views supervise it, and an unrepairable breach latches opening off |
| Single-writer lease | Done. The engine joins the fleet's own `flock`, refuses to start when another process holds the account, and claims its log the same way |
| Notifications and a liveness watchdog | Done, by feeding the fleet's own watchdog rather than growing a second one. The engine writes a heartbeat file; `check_fleet_liveness.py` reads it and pages on stale, unreadable, or latched. Off unless a path is configured |
| Reaching the funded account | Done, behind the owner's switch. `bybit_mainnet` refuses to build unless `REAL_MONEY` is armed in the host credential file — the only toggle there is. Current operational status lives in `STATE.md` and `scripts/ops.sh status` |
| Saying what the fills cost | Done. `is_maker` kept from the venue, `M0` written on every send, arrival shortfall / effective spread / fee / all-in derived, and the signed markout at 1s/15s/1m/5m written when its horizon comes due. `engine fills --wal PATH` is the read; five of the numbers are in the heartbeat |
| Saying what its own ids mean | Done. Strategy and symbol ids are positions, so the log records both tables — at boot and again whenever a book names a new symbol |
| A strategy reading its own position | Done. `my_position` is that strategy's own fills, moving the instant one arrives. The account reading beside it lags seconds and, on a two-sleeve account, is the sum of both |
| A strategy reading what it has in flight | Done. `in_flight` (`engine-core/src/covers.rs`) is the engine's own note of what a strategy sent that the account reading has not absorbed yet — booked at the send, at the quantized size that actually went, and released as the reading catches up or a reject, cancel, or refused exit ends it. Every plug gets the one truthful reading |
| Following a symbol a book names late | Done. A name the engine has never followed is taken on when a book asks for it: interned, subscribed, added to the gateway, taught to the private stream, and given an instrument rule. The four tables that map names to ids are checked against each other and a symbol they disagree about is dropped rather than traded |

### Evidence boundary

Bybit is the only venue with live-order evidence. Hyperliquid, Lighter, and
MEXC have compile and offline adapter evidence only. Variational has no account
or order API. A capability that has not been exercised against its venue is a
claim, not production proof.

**The engine is the account owner, in the repository and on the host.** It
reads the venue, writes `account_equity_usdt` into its heartbeat, both
producers size from that equity, both write an absolute target book, and the
engine reads each book, routes it to its own sleeve, and takes on symbols the
books name that no config listed. It is **live on the demo account** and holds
that account's single-writer lease. Funded status is read from
`scripts/ops.sh status`; `STATE.md` records the current operational snapshot.

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

**It is installed on the host, and `REAL_MONEY` is the only thing that decides
whether it runs.** Armed, the unit starts, the engine sends orders and takes the
funded account's single-writer lease. Unset, the unit does not start. Delete
`/etc/liquidity-migration/engine-mainnet.env` to keep it off for good — a
stopped unit would not stick, because the deploy starts it wherever its env
file and the binary both exist.

The account it reads, the caps it reads them under, and why it is left running
rather than stopped are operational state, so they live in one place:
[STATE.md](../STATE.md) §The fleet.

The pieces, all in `deploy/`:

| File | What it is |
| --- | --- |
| `systemd/liquidity-migration-engine-mainnet.service` | The unit. Deliberately does **not** conflict with anything else on the box: the kernel lease stops a second local engine, while the dedicated-UID contract excludes hand trading, venue bots, and other trading keys |
| `engine.mainnet.toml.template` | The engine's config: `venue = "bybit_mainnet"`, capital limits loaded from `/etc/liquidity-migration/producer-mainnet-source/operational-profile.json`, one block per sleeve with its own book path |
| `engine.mainnet.env.template` | Unit settings: which config, where the heartbeat goes |

Neither template carries a live switch. `REAL_MONEY=true` in
`/etc/liquidity-migration/bybit-mainnet.env`, set by the owner's own hand, is
the whole of what arms the funded fleet.

Each sleeve must be given names the other does not trade: the engine refuses a
config where two strategies claim one symbol, because the venue holds one
position per symbol and keeps no note of who asked for it.

**What is left is evidence, and only time produces it.** Run it against a
funded account that is not near-empty, long enough to see what it does and what
it costs — `engine fills` off its log is the reading that answers the second
half. Arming it is `REAL_MONEY=true` in the host credential file, by someone
who meant it, and unsetting it is the way back. **That step is the owner's and
nobody else's.**

## What the engine does not do

- No carry or continuous *decision* inside the engine — a carry decision is a
  batch over ninety days of funding and bars, so it stays in research and
  reaches the engine as a target book.
- No production proof for Hyperliquid, Lighter, or MEXC. Their adapters have
  offline test evidence only; Variational is market-data-only.
- No market impact measurement. `engine fills` anchors on the top of book,
  which is the only book the engine carries. Walking a depth-50 book is the
  Python half's job and stays there.
- No FPGA/kernel-bypass pretensions: the venue is an HTTPS cloud service and
  single-digit milliseconds on-box is already far below its floor.
