# Operational State

What is running right now. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.**

The dated history of how it got here — deploys, incidents, repairs, change
points — is [CHANGELOG.md](CHANGELOG.md). When something happens, add the dated
entry there and edit the sections here to match; never append history to this
file.

## Now

### The fleet

- **AUTOMATED TRADING IS ON, on both fleets.** The canonical manifest contains
  24 current units: ten continuous daemons, seven timers, and their seven
  timer-backed oneshots. The daemons are both engines, all six producers, the
  Telegram controls, and the forward recorder. Continuous daemons and timers
  are active and enabled; oneshots are normally inactive between scheduled
  runs. `scripts/ops.sh status` checks that inventory and reports the exact
  installed commit. `REAL_MONEY` is armed. `deploy/sleeves.env` carries
  `LONG_SLEEVE=on` and
  `CARRY_SLEEVE=on`, and no host override at
  `/etc/liquidity-migration/sleeves.env` narrows them, so a deploy brings the
  whole fleet up. That host file is how a sleeve is held down — it can only
  turn one off, never on — and the Telegram pause button writes it.

  The funded engine heartbeat reports roughly 149.52 USDT equity and no open
  positions. The independent mainnet attestor credential is not installed, so
  this is the engine's signed account view rather than a second credential's
  venue-wide flatness proof. The demo account carries the practice book. Exact
  live truth is `scripts/ops.sh status`, never this prose.

  The fleet runs on `208.84.103.4`. The funded key declares that address as
  primary and `116.202.15.128` as its deliberate backup; the backup host runs
  no fleet units.

- **A third sleeve, the EXODUS SHORT, runs on demo and the funded topology.**
  Each independent Exodus producer consumes its realm's hash-chained CARRY
  pre-settlement event tape. When carry's v7 pre-settle exit fires, Exodus
  publishes the abandoned filled quantity as a SHORT to the engine's
  `exodus` sleeve through its own state and target book, then covers 60 minutes
  after settlement. Registered config
  `configs/lane2_exodus_short_v1.json`; the Exodus units write
  `exodus-demo.json` and `exodus-mainnet.json`. The
  declared 0.35 stop is a disaster fence (every measured stop level loses; the
  cover clock is the exit). No funded Exodus fill has been observed yet; the
  sleeve waits for a v7 fire and no test order is forced.
  Evidence and the honest 2024-negative era shape:
  `docs/research/research_findings.md` (the exodus short row); promotion
  note in `docs/research/governance.md`.
- **The LLM ledger is research-only.** Its hourly service records judged public
  trigger events under
  `/var/lib/liquidity-migration/llm-driver-ledger/`. It has no venue
  credentials, target-book path, or input seam into LONG. Native LONG targets
  come only from the shared typed LONG decision contract. Detail:
  `docs/trading_logic.md` §LLM ledger.

- **The fourth registered sleeve, `maker_canary`, is installed but quoting is
  off.** Its first minimum-size AGIUSDT forward sample stopped after 10 new
  fills when a 10-AGI remainder fell below the ordinary close minimum. The
  engine keeps the sleeve block for append-only log identity and drains only
  inventory attributed to it. An exact whole-position market exit below the
  ordinary venue minimum uses Bybit's explicit full-close form; partial dust
  orders remain refused. Its registered rule and evidence boundary are
  `configs/lane2_toxic_flow_quoter_v1.json`; the result is in
  `docs/research/research_findings.md`. The ordinary CARRY, LONG and Exodus
  books remain independent of it.

- **LONG runs at 6.0× and carry at 3.0×; Exodus copies carry's filled quantity
  (owner directive, both fleets).** Sizing dials are read directly by
  the producers — `CARRY_NOTIONAL_MULTIPLIER` and `LONG_NOTIONAL_MULTIPLIER`. LONG/carry
  entries equal their base slot (at most 10% of equity) × multiplier. LONG sits at **6.0** (~60% of equity
  per entry before LONG's own vol/weekend scaling — the measured
  double-LONG-at-no-Sharpe-cost lever, research_findings §3); carry is **3.0**
  (~30% per name), and Exodus takes the exact quantity carry actually abandons,
  with no independent sizing dial. On demo the dials are in
  `producer-demo-source.env`; both funded dials are in
  `producer-mainnet-source.env` (see Risk envelope §Real money).
  There is no book-level margin ceiling in the way: what bounds a loss is the
  venue-native stop on each position. Held components keep their
  fill-anchored size — the dials reach new entries only. The mainnet account
  document is rendered once on the host from the reviewed base and operator
  dial: entry leverage 5, gross cap wallet × 5 across the sleeves, every cap
  a ratio of tracked equity. Both funded producers and the Rust engine read
  that identical artifact. The sizing is
  a forward-record change point for all fill receipts.
- **The engine owns the demo account, and the sleeves feed it.** It runs the
  installed Rust release, with carry_hold **v7** on both CARRY producers: the v7 execution
  clock, `strategy_profile=v7 early_exit=1` — the early exit fires on the
  venue's running rate up to 15 minutes before a dying print pays; settled-print
  fallback kept. The drop exit is part of the producers' exit clock (no dial):
  a held name the upcoming midnight decision zeroes — universe rank,
  persistence cut, suspend — sells at the first post-midnight cycle (~00:02)
  instead of on the 00:20 clock; entries keep that clock.

  The engine recovers fills its stream never delivered from the venue's own
  execution history at boot and after every private-stream reconnect. A failed
  boot history read, or a WAL whose missing interval predates the venue's
  history reach, aborts boot. A failed read after a live stream gap writes a
  durable `may_open=false` latch and stops the run. Other reconciliation
  findings can also latch the may-open gate, and `engine reconcile-clear` is
  the deliberate operator act that gate waits for
  ([docs/engine.md](docs/engine.md) §Safety posture); a fresh finding latches
  again. The engine keeps an in-flight cover book and
  rotates its WAL in segments. The who-opened-what ledger (fill attribution)
  follows the venue: boot drops a sleeve's claim on any symbol the venue
  reports flat (durable `ClaimsDropped` receipt in the WAL), and a
  `reconcile-clear` restatement clears claims on the symbols it reports flat —
  so a close the log never got to charge cannot lock other sleeves out of a
  name.
  Both engines run `leverage_authority = "sole"`. A held position's venue
  leverage is still checked on every account reading, and a mismatch turns
  inline confirmation back on for that symbol. The funded dedicated-UID
  contract forbids a second trading authority.

  The chain runs end to end:

  - the engine reads the venue and writes `account_equity_usdt` into its
    heartbeat;
  - the LONG and CARRY producers size from that equity;
  - the Exodus producer copies the exact filled quantity CARRY abandons from
    its durable pre-settlement event tape;
  - all six producers write independent absolute books under
    `/var/lib/liquidity-migration/targets/` — `{long,carry,exodus}` for demo and
    mainnet;
  - each engine reads its three books, routes each to its own sleeve, and takes on
    symbols the books name that no config listed.

  A book within the 5% dead band of what is held moves nothing, which is why a
  running engine sending no order is the ordinary case rather than a fault.

  **The engine is LIVE on the demo account**, holding the single-writer lease
  `bybit-demo-user-555899665.lock`.

  **`REAL_MONEY` in `/etc/liquidity-migration/bybit-mainnet.env` is the only
  toggle.** Armed, the funded units start and the funded engine sends orders
  and takes that account's lease; unset, they do not start at all. The engine
  carries no second switch of its own — there is no shadow mode and no live
  flag, and a stale `ENGINE_LIVE` left in a host file does nothing. To stop the
  funded fleet and persist the switch off, run `scripts/ops.sh deploy
  disarm-mainnet`.

  What is not done, plainly:

  - **The funded engine trades.** Its account view reports the funded account
    (552445993) flat under the current empty books; a later valid book can open
    under the mainnet profile, with reference tracking equity and gross at five
    times it. What it has not yet had is a graded stretch: the forward record
    on real fills is days old, not weeks.

  - **There is no hourly Telegram digest of what is held.** Every position that
    closes is reported as it closes, with its P&L after fees, and a daily
    summary adds them up; nothing pages what is open right now. Pause, resume
    and `ops.sh flatten` work, on the engine's own path.

  - **There is no per-sleeve capital share, on either fleet or in either
    half of the system.** Every sleeve draws on the account-wide caps and any
    one can spend the lot. What bounds a sleeve is the account's gross and
    margin caps, the equity-anchored envelope, and the venue-native stop on
    each position. A profile that declares `sleeve_limits` is refused at load
    by both loaders rather than read and ignored.

- **The engine binary is built in an isolated clone at `/opt/engine-build`** —
  never the deployed checkout the fleet runs from — with its own toolchain
  under `/opt/rust`. Rollout compiles the exact target commit during prefetch
  while the incumbent fleet remains live, then stopped installation rechecks
  and copies that same digest-bound candidate. Git and exact-version Python
  wheels are also fetched and byte-bound before the stop; installation uses the
  wheel cache with no package index. Its measured latency on the box, the
  single-writer lease
  it takes (one kernel `flock` per venue account at
  `/run/lock/liquidity-migration/bybit-{realm}-user-{userID}.lock`, named by
  the account number the venue itself reports), and the mainnet fence are
  [docs/engine.md](docs/engine.md). There are **two demo accounts on the
  box**: the fleet's 555899665, whose lease the live engine holds, and
  579580669 (credentials in `bybit-quote-lab.env`), whose lease nothing holds —
  the lease is what keeps the quote lab and an engine from ever writing to one
  account at once. `engine canary-order` is the bounded submit/cancel proof for
  the second account; it cannot select mainnet.

### The funded account

- **The funded engine heartbeat reports 149.52 USDT equity and no open
  positions.** Its account reading is fresh and the mainnet producers size
  their books from the same account. The independent mainnet attestor
  credential is not installed, so no second credential has proved venue-wide
  flatness. Money in the account changes what the producers publish and,
  through the tracked reference, every cap with it.
- **There is no account daily-loss circuit breaker.** Operational-profile
  schema v2 removed the field and the engine no longer restores, evaluates, or
  writes daily-loss anchors. Historical WAL anchor and verdict shapes remain
  decodable only; boot ignores them and the next rotation drops them.
- **Real money is armed on the installed fleet, and the owner confirms the
  venue account is engine-exclusive.** The audited generation requires
  `BYBIT_ENGINE_EXCLUSIVE_ACCOUNT_USER_ID` to equal the authenticated funded
  UID. That value is an operator acknowledgement that the UID is dedicated to
  this engine: no hand trading, venue bots, copy trading, or other trading API
  keys. Bybit does not expose an account-wide list for every bot family, so
  this is a reviewed operating contract, not a machine proof. Funded startup
  and flat attestation refuse a missing or mismatched acknowledgement. The
  account loses funded authority if that exclusive-owner contract stops being
  true.
- **Outside activity is not a second trusted book.** The engine does not claim or
  cancel foreign orders and does not count unowned fills as its exposure. A
  foreign fill, unexplained position quantity, or foreign working order in a
  symbol a configured strategy trades durably latches `may_open=false`.
  Reductions and stop protection continue. A foreign order in a symbol no
  configured strategy can address is reported but does not halt openings.
  Any breach of the dedicated-account contract requires investigation and an
  explicit `reconcile-clear` before entries resume; clearing the latch does not
  authorize sharing the account again.
- **Both engines run `leverage_authority = "sole"`**, so leverage is armed when
  a target book arrives rather than inline before an order, and an entry from
  flat no longer pays a `set_leverage` round trip (~172 ms, 844 ms worst). The
  funded UID contract forbids hand trading, venue bots, copy trading, and other
  trading API keys, which is what makes the cache trustworthy across flat
  spells. Every held position's leverage is checked against the venue's own
  position row on each account reading; a value that contradicts the cache
  alarms, is written to the log, and turns inline confirmation back on for that
  symbol. This setting does not authorize a second writer.
- **The safety stop covers unexpected outside size.** The manager only
  creates Bybit **Full-position** stops (`tpsl_mode="Full"`), which close the
  entire venue position at trigger. This keeps reductions safe if the dedicated
  account contract is breached; it does not make hand trading or another bot
  permitted. The venue offers one stop per coin and cannot split it.
- **A later entry cannot loosen that Full-position stop.** Admission holds
  same-side siblings against the tighter of the fresh venue stop and the
  fill-owned durable stop. Replay advances stop intent only when that order
  actually grows or crosses the position; rejected and unfilled siblings do
  not count. Every fresh account view restores a missing or looser level and
  durably latches new risk off if the repair fails.
- **Bybit one-way position mode is verified before startup completes.** Rust
  makes a signed read-only position query for every configured symbol and
  requests the 200-row maximum, and requires exactly one matching `linear` row
  with `positionIdx 0`. Bybit can return a cursor that repeats the same
  symbol-scoped row, so cursor presence is not treated as another position.
  Any missing, duplicate, malformed, hedge-mode, or failed response aborts
  startup. Configured checks run concurrently in rate-bounded 50-request waves;
  a symbol admitted later is checked before its first order, stop, or leverage
  request. The check does not mutate venue mode and does not pin cross margin.
  An external mode switch after verification remains possible; the next
  incompatible venue request rejects.
- **No copy of the funded execution key remains on the laptop.**
  `/etc/liquidity-migration/bybit-mainnet.env` on the host is the only copy and
  the only trading authority (`REAL_MONEY=true` and the carry stop 0.35). The
  revoked laptop-readable key has no authority; the only active execution key
  is the rotated host key. Funded Bybit identity refuses a key that is not UTA, is read-only,
  is not allowlisted only to the exact host-IP set declared by
  `BYBIT_REAL_API_KEY_IP` and optional `BYBIT_REAL_API_KEY_BACKUP_IP`, lacks
  ContractTrade Order and Position permissions, or carries Wallet Withdraw
  permission. Missing, wildcard, all-network, and undeclared IP entries fail.
  Startup also requires the exact host-IP set and dedicated account ID in the
  host environment. Optional operator
  inventory controls use a physically separate, globally read-only query key
  from the operator-owned root:root mode-0600
  `/etc/liquidity-migration/bybit-mainnet-attestor.env`; they never receive the
  execution key and are not a rollout prerequisite.

### Instrument rules

- **Each Rust venue adapter fetches current instrument rules at engine boot.**
  A failed fetch aborts boot. The engine also refuses to start when the venue
  omits a configured symbol, so quantity steps, price ticks, and venue minimums
  never come from a deploy receipt or a Python file.

### Execution and market data

- **The engine registry contains six venue families and ten exact realms, and one realm name in
  `engine.toml` picks the path.** `engine venues` lists every compiled realm and
  its evidence gate. Bybit demo and mainnet are `live-proven`;
  `hyperliquid_testnet` and `lighter_testnet` are runnable `testnet-canary`
  paths. Hyperliquid, Lighter, and MEXC mainnet plus both Binance realms are
  `production-blocked`, and Variational is `read-only`. `engine run` enforces
  this before opening a WAL, reading credentials, or opening a socket. MEXC
  has no testnet; Binance has one, but its unresolved ambiguous-503 outcomes
  and the missing signed protective-stop lifecycle do not support calling it
  runnable. The
  selected name decides the gateway, the private order stream
  and the public market feed together, so a config cannot send orders to one
  venue and price them off another's book. **Only Bybit has live-order
  evidence.** Binance's public sockets and offline request shapes are checked,
  but no signed account, order, Algo stop, fill, cancel, private-stream, or
  restart lifecycle has run. Account-wide execution recovery also refuses:
  Binance retains orders by creation time but trades by trade time and exposes
  no complete symbol source for a fill from an older order. A valid limit
  opening can also fill partially below the market-order minimum, and no dust
  close is proven. An entry response can also be unknown before the paired stop
  is sent, and an unknown stop response can leave an accepted stop behind; the
  adapter does not yet reconcile either case. Its feed
  deliberately carries complete top-20 snapshots rather than the engine's full
  L50 evidence. MEXC
  enforces consecutive depth versions and redials on
  gaps; Lighter enforces its nonce chain; Hyperliquid rejects a same-symbol BBO
  timestamp regression but its protocol cannot expose forward gaps. Lighter
  also cannot open a position yet: it has no
  leverage transaction here, and the engine refuses an entry naming a leverage
  it cannot set. `REAL_MONEY` is still the single arming switch, and it
  reaches every venue that reads a credential — which is every one but
  Variational, whose adapter authenticates nothing because the venue publishes
  nothing to authenticate against.
- **What differs between the venues changes decisions, not just addresses.**
  Hyperliquid pays funding **hourly** and quotes the hourly rate; Bybit quotes
  its next eight-hourly settlement, so a carry number carried across without
  scaling is out by a factor of eight. Only Bybit keeps a stop on the position
  row — Hyperliquid and Lighter keep it as a separate reduce-only trigger
  order, so "is this position protected" is answered from the open orders.
  Lighter's fills arrive by paced resync from the venue's execution history
  rather than by live stream, because its account channel does not carry the
  engine's own order ids. Variational publishes no trading API at all, and no
  account read either, so an engine cannot boot on it — its market feed is
  usable on its own.
  [docs/engine.md](docs/engine.md) §The venues.
- **Funded order mutations use Bybit's persistent trade WebSocket.** The
  dual-stack host resolves the official endpoint normally but dials it through
  the allowlisted `208.84.103.4`; TLS still verifies `stream.bybit.com`.
  Create, amend, cancel and their venue-native batches use that authenticated
  socket. A failed socket warm-up keeps the signed REST path available for the
  run; a venue rejection keeps the socket, since it is an answer rather than a
  broken connection. The adapter paces to the per-second quota Bybit states in
  each acknowledgement's header when that is larger than the documented
  default, so an upgraded market-maker tier takes effect without a code
  change.
- **The engine says what its fills cost.** It keeps `is_maker` from the venue's
  execution row and writes the midpoint an order was decided against onto the
  order's own log record, so arrival shortfall, effective spread, fee and all-in
  are derivable from the log alone; the signed markout at 1 s / 15 s / 1 min /
  5 min is written when its horizon comes due, because a log holds no prices.
  Names and signs are `docs/architecture.md` §Trade diagnostics. `engine fills
  --wal PATH` is the read, one row per sleeve and coin — keyed by the names the
  ids meant where each record sits, because the id tables are rebuilt every
  boot. Five of the numbers are in the heartbeat. **`M0` is the top of book**,
  so nothing here measures impact. A fill the private stream missed and the
  venue's execution history gave back is priced like any other. The engine is
  the only writer of these receipts, for its own fills only.
- **A deploy restarts both engines.** They share one binary, and
  `mainnet-engine-ok` prints beside `engine-ok`. Verify a deploy by a field only
  the new code produces, read on **both** heartbeats — "active" says nothing
  about which binary.
- **A resting entry waits the full 120 s.** That is the engine's
  `WorkPolicy::default().window_ms` (`engine-types/src/orders.rs`); the order
  moves every 15 s and nothing crosses it early. 180 s was cheaper on tape but
  does not fit the Rust engine's 120 s sibling-batch freshness budget.
  `hold_decision_price` and `give_up_instead_of_crossing` are the only two
  dials a strategy block can set, both off by default — the tape sweep says
  cross at the deadline rather than give up. Measured on 15 live resting
  entries, fills came at a median of 1.28 s and a maximum of 36.6 s.
- **Pricing and market data for the order path live in the engine.** It
  subscribes its own venue stream per followed symbol and refuses an entry
  decided against a quote older than its declared bound (default 30 s —
  [docs/engine.md](docs/engine.md)).

### Measured latency

The live order path is the Rust engine's; the honest latency contract and the
measured table are [docs/engine.md](docs/engine.md). Three native runs of the
release engine put the decision at **80 ns** and
market input through durability to a parsed localhost submit response at
**1.26 ms median / 3.16 ms p99**. The benchmark has no socket-write timestamp.
Live against the venue (n=67), the
honest reconstructible total is **179 ms median decision→acknowledgment,
512 ms p90, 1013 ms worst** (at n=67 the tail figure is the worst of the
sample, not an estimated p99). Historical records cannot split that total
cleanly into disk, socket, leverage call, and venue legs.

- **The order path does not wait for the disk.** The log's bytes reach the
  operating system before an order is sent and the barrier runs beside the
  flight to the venue; the first news that an order traded is what waits for
  it. On a venue milliseconds away that wait is nothing, and `still waiting on
  the disk` in the latency line is what says so. A machine that dies inside a
  barrier can leave an order the log does not name, which boot reconciliation
  answers by latching opening off.
- **Every order, cancel and amend records where its time went.** The log keeps
  queue admission, venue-task start, socket write, acknowledgement, task
  completion, core resume, and the time the adapter held the command back to
  stay inside the request quota. That last one is the engine's own pacing, not
  venue latency, and it is what the funded canary's 249.74 ms p99 venue task
  was mostly made of. `cd engine && cargo run --release -- latency --wal PATH`
  reports each step per operation at p50, p90, p99 and p99.9.
- **The live heartbeat exposes execution health, not only a headline
  latency.** It carries p99 disk-wait residue and request-quota hold, accepted
  amends confirmed versus pulled after silence, private-stream resets, and the
  venue clock minus the host clock. These separate a slow venue from our own
  pacing, a missing private update, or a drifting machine clock.
- **An accepted amend keeps its order.** The venue states a resting order's
  price by republishing it on the private stream, and that is what settles the
  reservation an amend opens. Only an amend whose price goes unstated for two
  seconds is cancelled.
- **The funded host's warm signed account read is 12.71 ms median / 23.80 ms
  p95.** Thirty successful `/v5/position/list` samples on one reused connection
  ranged from 10.96 to 27.70 ms. The declared backup host measured 172.14 ms
  median / 486.59 ms p95 over the same 30-sample run. These are REST account
  reads, not order acknowledgements.
- **Sibling placements share one durable batch.** The engine validates and
  reserves them in deterministic order, appends every accepted order, crosses
  one WAL barrier, then asks the venue adapter to send the group. Bybit overlaps
  distinct-symbol chains over ten warm sockets and preserves same-symbol wire
  order; nonce-sensitive adapters keep the serial default.
  With three deterministic 100 ms local responses, five Linux integration
  runs finish in 0.10–0.11 s and observe peak in-flight three. No live venue
  sample establishes current sibling-group latency yet.
- **Long-run account latency has a repeatable within-run probe.** The execution
  ID set is bounded, while venue execution history can still grow. Run the
  release `account_state_soak` example described in
  [docs/engine.md](docs/engine.md) on a production-like Linux host and compare
  its early, middle, and late windows. A two-million-operation target-host run
  measured 686/678/522 ns p50 mean cost in those windows, so within-run ID
  handling stays flat. Already-decoded cold recovery measured 1.26 ms at
  1,000 rows, 23.8 ms at 10,000, and 401.5 ms at 100,000; growing venue
  history remains the startup concern. The Ubuntu workflow retains the same
  bounded probe. Real venue fetch and decode time remains a separate
  measurement.

## Topology

Ten daemons run continuously: the demo and mainnet Rust engines, independent
LONG, CARRY, and Exodus producers for each realm, the Telegram controls, and
the public forward recorder. Seven timers drive seven oneshots beside them —
demo liveness, mainnet liveness, the LLM ledger, the trade notifier, the
forward uploader, the nightly state backup, and the weekly demo recovery
drill. Those 24 current units are the canonical topology in
`deploy/fleet_manifest.tsv`. The execution host at `208.84.103.4` carries the
exact commit and unit inventory reported by `scripts/ops.sh status`; its
`deploy/systemd/README.md` describes the installed inventory. Demo is the only
practice realm.

Private account-market persistence is off. Public forward persistence is on:
the 10 ms L1 touch, L50, trades, mark/index price, the crowd fee (funding),
open interest and liquidations are written with local and venue times, verified
into compressed segments, and retained for 30 days within a 60 GB / 25 GB-free
disk boundary. L1 and L50 rows name their depth and keep separate sequence
state while retaining the venue cross-sequence that orders them. The recorder
starts from an 81-symbol list: every existing maker/saved-L50 name plus the
LONG sleeve's top 50 by 90-day median daily turnover and a ten-rank buffer.
Completed compressed segments are copied hourly to Google Drive. Each new batch
is checked against the local files before its upload ledger advances and leaves
a SHA-256 list beside the remote tape; partial segments and private account
state never enter that path.
Live L2 readiness and exact decision-book capture are also on.

## Risk envelope

**Demo** (risk-on): capital reference 250,000 USDT, component/account gross
1,250,000, initial margin 250,000. No per-symbol ceiling. Entry
leverage 5× on every sleeve, account max leverage 5×, LONG notional multiplier
6.0 and CARRY multiplier 3.0 (per-name 0.10 and gross cap 1.0 come from the
registered rule and multiply through, so a LONG entry takes 60% of the sizing
equity before its own vol/weekend scaling, each new carry name 30%, and a full
CARRY book is 3× it). Startup and authorization
reject unknown profile fields and producer leverage above the owner cap; how
large a book the multipliers build is the owner's dial, bounded per position
by each venue-native stop.

**Real money**: the funded fleet sizes LONG at 6.0 and CARRY at 3.0. LONG's
6.0 is set both in the committed profile and as `LONG_NOTIONAL_MULTIPLIER=6.0`
in `producer-mainnet-source.env`, the no-secrets file the two mainnet
producer units load; they do not load `bybit-mainnet.env`, which holds the
key. Carry sizes from the committed profile's 3.0 with no env line.
`RM_CARRY_STOP_LOSS_FRACTION` (**0.35**) is the protection dial and is the
owner's own. The account document (`configs/operational.mainnet.json`) is
static: entry leverage 5×, gross cap = wallet × 5 account-wide and the same
per component, margin cap = wallet, and no per-symbol ceiling — one sleeve can
spend the whole envelope, and every cap is a ratio of the
equity-tracked reference, proved at load and re-proved on each rebase. A
book the dials build past those caps is refused per entry by the engine's
runtime admission; a retired `RM_*` line in an env file is refused by name.

## Standing operational constraints

- **Arming real money is one switch, set by the owner's own hand**:
  `REAL_MONEY=true` in the root-owned
  `/etc/liquidity-migration/bybit-mainnet.env`, beside the live key. A git commit
  can never arm; activation still walks the full preflight, and every
  capital-preservation control (envelope, native stops, single-writer lease,
  reconciliation) gates the start.
- **The funded account stays in one-way position mode.** Startup verifies every
  configured symbol read-only; it never changes account mode. An operator must
  still avoid switching it after the check.
- **A generation-changing rollout does not depend on account-wide inventory or
  outgoing release receipts.** It pins the target commit, stops producers before
  both account owners, persists the boot fence, requires quiescence, installs
  the target, then activates and verifies the new topology. Before checkout
  mutation, failure restores the exact active/persistent/runtime-enabled unit
  snapshot; a markerless incumbent is restarted directly and a marked incumbent
  receives a temporary binding for its unchanged release while only the observed
  units restart. After checkout mutation, failure leaves
  the managed fleet stopped for explicit recovery. Optional manual
  `attest-flat` keeps its separate read-only credential and does not gate
  deployment ([docs/operations.md](docs/operations.md)).
- **The mandatory demo engine has a deployment-reconciled identity.** A missing
  `engine.env` is installed from the committed non-secret template. A legacy
  file gains only absent account, venue, and realm bindings atomically; host
  dials are preserved, while empty or conflicting bindings abort unchanged.
- **Unknown safety-critical state fails closed.**
- **Service activation has a durable commit point.** Candidate services run
  only under a root-watchdog-renewed six-second permit bound to the boot,
  rollout PID/start ticks, release commit, and five artifact hashes. Trusted
  launchers poll that authority every two seconds and stop their child when it
  expires. Lease renewal records the permit identity before validation, takes a
  non-creating pin, and revalidates it under lock; deletion or replacement
  revokes without recreation or adoption. Deploy preflight and the launcher reject writable
  critical checkout ancestry or Git metadata before trusting the commit. The
  persistent six-hash completion receipt is installed only after the complete
  topology is enabled, active, verified, and synced; it authorizes reboot
  without any `/run` state. A crash before that receipt therefore leaves the
  candidate stopped rather than partially bootable
  ([docs/operations.md](docs/operations.md#activation-commit-protocol)).
- **A funded-configured host changes or activates a generation only through
  `scripts/ops.sh deploy rollout`.** Direct `install`, `activate`, and `staged`
  refuse before mutation even while disarmed, and `ops.sh start|restart`
  refuses funded units; fail-safe stop and disarm remain available. Demo-only
  hosts retain the direct modes. The remote fail-safe paths execute no checkout
  or virtualenv code: stop never reads the funded credential, and disarm uses
  an isolated root-owned system interpreter plus an embedded stable atomic
  rewrite only after the funded unit allowlist is stopped and disabled. The manual GitHub workflow may expose a mode,
  but the host-side gate still decides whether it is legal
  ([docs/operations.md](docs/operations.md)). Push only from the primary
  checkout until the pre-push hook's git-fixture tests are hermetic (a linked
  worktree run corrupted the repo once).
- **Release evidence is revision-specific.** A candidate is qualified only when
  that exact commit's Python checks, optimized Rust suite, bounded account soak,
  order-path benchmark, build, and smoke test run green on Ubuntu. Production
  advances only after that gate passes and rollout activates the candidate.
- **There are two forward-only rollback floors.** Rolling a producer behind
  `31ee68d` requires archiving its event tape. Once the funded engine has
  durably named `carry`, `long`, and `exodus` in its WAL, its binary and config
  must continue to understand that three-name prefix; the former two-sleeve
  generation cannot replay that WAL.
- Three delisting candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`) have
  venue `deliveryTime=1784538000000`, recorded prospectively with their
  first-observed anchors in LONG's private mode-0600 retirement registry; they
  may retire only while venue positions, target books, and engine-owned orders
  are all flat.

## Forward evidence stream

Everything that runs is graded under the Progressive Evidence Model
([docs/research/governance.md](docs/research/governance.md)): each committed
config is graded on the run of days it postdates, continuously, with recorded
change points — the commit is the registration; there is no waiting window and no
separate registration artifact.

Standing invalidation: **cross-fleet P&L comparison before 2026-07-31 is invalid**
— the two fleets decided off different data and held different price bases, both
measured (CHANGELOG 2026-07-31). The comparison lane ended with the 2026-08-03
paper retirement, so no valid demo-vs-paper number exists on either side of that
boundary. All demo equity/P&L numbers before the 2026-08-03 14:38 UTC clean-slate
reset belong to the archived epoch.

Change points currently accruing forward days: the CARRY **v7** pre-settle exit
clock (`CARRY_STRATEGY_PROFILE=v7` on both carry units: the same exit test read on
the venue's running rate inside the last 15 minutes before a held name's
settlement, selling before the payment instead of one minute after it; the
rule v7 executes is `lane2_carry_hold_v7` byte-identical, so
its forward grade continues; graded from engine exit fills
against the settled-print counterfactual; rollback dial is `v6`). The v7−v5
config differential (`lane2_carry_hold_v7` vs v5, with v4 and v5 keeping
scoring) is the registered forward experiment, and the drop
exit (a held name the upcoming decision zeroes sells ~00:02 instead of 00:20,
entries unchanged; no dial, the rollback is a revert and redeploy), LONG v12
wide-stop, and the entry execution recipes (quote-first entries, touch-sized
windows, and the replay-selected resting recipe). Sizing is the fixed
multipliers on both sleeves (carry 3.0, LONG 6.0). The LLM ledger records its
judged cohorts for research but publishes no target and contributes no LONG
fills. Sizing uses two
  env dials on both fleets; Exodus copies carry's actual handed-off quantity.
  Mainnet's rendered account document keeps entry leverage 5. The v6 whale
  halving makes the carry producers read one
non-Bybit input (Binance top-trader EODs, public endpoint, fail-open under the
registered 48h freshness clause). Full statements in
[docs/research/research_findings.md](docs/research/research_findings.md).

## Evidence boundary

The funded account has no performance record yet: its first night (2026-08-04)
legitimately decided cash, and the first honest maker-share grade waits on funded
`is_maker` receipts from a non-empty book. The engine records those receipts —
what is still missing is funded fills. Demo fill economics
are not evidence — demo fills simulate without queue position, and the demo
realm's matching engine holds phantom internal liquidity its published book does
not show.

Funded P&L before 2026-08-07 is overstated: funding was booked whole rather than
by the share this book actually held. Five settlements totalling **+15.23 USDT**
are on the funded execution record and the ACEUSDT **+10.72** of that is ≈96% not the
bot's. Since 2026-08-07 each settlement is scaled by `owned_qty_at_settlement /
venue_settled_size`, reconstructed from this book's own fills at the settlement
instant, with the venue's raw numbers kept verbatim (`venue_funding_usdt`,
`venue_settled_size`); the share is 1.0 whenever the venue position is the bot's.

Real money is a separate door: no runtime status or rolling record arms it —
arming is the owner's hand on the switch (constraint above).

## Known benign alert shapes

Each was diagnosed to a root cause and fixed. Listed so an operator does not
re-diagnose a page that has already been explained.

| Alert shape | Diagnosed cause |
| --- | --- |
| `unowned_venue_order` after a stop triggers | Owner disowning its own just-consumed Full stop while Bybit's open-order cache still lists it. Bounded 10-minute terminal-visibility grace, identity evidence required. |
| `waiting for queue-head market data: X:stale_book` | Lost/rejected orderbook subscribe. Socket rebuilds after 30 frameless seconds for a new subscription. |
| `latest cycle is 0.1 min future-dated` / `future_book` | Local read/update races sampling wall time before the snapshot. Ordering fixed; true future timestamps still page. |
| `engine_heartbeat_stale: … dated 1s in the future`, firing and clearing all day | Not a clock fault: the watchdog sampled its clock at the top of a ~2 s run and compared it to a file the engine rewrites every 5 s. It reads the file, then the clock. If this shape returns, a clock really is wrong. |

**If the chat is loud and the fleet is green, treat the checks as the suspect.**
The failure mode is not noise but blindness: one real signal in a stream of false
ones is indistinguishable from them.

## Open operational defects

| Item | State |
| --- | --- |
| Nothing bounds convergence toward a stale accepted target while producers are down | Deliberately not built — a liveness-coupled trading halt needing owner design |
| Kline bootstrap logs `failed=N` on restart with an intact store | It re-fetches a window it already holds and counts zero new inserts as failure; bounded ~40–50 s per restart. Tracked follow-up |
| Reported P&L is provisional | Figures are fill-reconstructed, not venue-confirmed (most `pnl` events carry `funding_status=pending_venue_reconciliation`). No closed-loop accounting check yet, which real money needs |
| Entries execute ~23 minutes after the price the scorer models | Live runs the delayed-entry stress case, not the bar-close headline case. Recorded with the measured capacity numbers in `docs/research/research_findings.md` |
| Intraday notional tracking is bounded, not continuous | Deliberately left as an owner decision; `docs/research/research_findings.md` states it rather than treating it as settled |
| No independent venue/WAL agreement page | The engine reconciles and latches `may_open=false` on uncertainty, while the watchdog pages on that latch and heartbeat freshness. A separately rendered mismatch summary is still absent. |
| No continuous off-box liveness signal | The daily digest is a point-in-time message, not a dead-man signal. The external heartbeat URL is unprovisioned, so silence between digests still means either a healthy fleet or a dead box |

Audit reports are not kept as standing files. Their findings live in the topic
docs — `docs/research/research_findings.md`, `docs/architecture.md`,
`docs/data.md`, `docs/trading_logic.md`, `docs/notifications.md` — and in Git
history.
