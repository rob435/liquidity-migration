# Operational State

What is running right now. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.**

The dated history of how it got here — deploys, incidents, repairs, change
points — is [CHANGELOG.md](CHANGELOG.md). When something happens, add the dated
entry there and edit the sections here to match; never append history to this
file.

## Now

### The fleet

- **A third sleeve, the EXODUS SHORT, is registered and deployed to demo.**
  When carry's v7 pre-settle exit fires, the carry producer publishes the
  abandoned position as a SHORT to the engine's `exodus` sleeve (its own
  `[[strategy]]` block — appended, per the id discipline — book
  `exodus-demo.json`, fill attribution), covered 60 minutes after the
  settlement. Registered config `configs/lane2_exodus_short_v1.json`; dial
  `EXODUS_SHORT_PROFILE=v1` on the demo carry unit only, mainnet unset;
  unsetting it drains the book flat. The declared 0.35 stop is a disaster
  fence (every measured stop level loses; the cover clock is the exit). No
  live fire yet — the sleeve waits for the first v7 fire like everything else.
  Evidence and the honest 2024-negative era shape:
  `docs/research/research_findings.md` §1 (the exodus short row); promotion
  note in `strategy_program.md`.
- **The LLM GATE is an entry source inside the LONG sleeve (owner decision,
  live on demo).** The hourly ledger service judges fresh 1/2/4/12/24h trigger
  events and publishes score ≥ 6 names to the LONG candidates file
  (`llm-gate-candidates.json`); the LONG producer takes them as ordinary LONG
  entries — same book (`long-demo.json`), same engine sleeve (`long`), same
  vol-scaled sizing at the profile multiplier, same v12 exits and venue-native
  stops. The ledger holds no venue credentials. Kill switches:
  `LONG_ENGINE_LLM_GATE_ENABLED=0` on the demo LONG unit, or stop
  `llm-ledger.timer`. Detail: `docs/trading_logic.md` §LLM GATE.

- **Every strategy runs the same 3x multiplier, set from one dial bank (owner
  directive, both fleets).** Sizing is three env dials read directly by the
  producers — `CARRY_NOTIONAL_MULTIPLIER`, `LONG_NOTIONAL_MULTIPLIER`,
  `EXODUS_NOTIONAL_MULTIPLIER` — each entry = the strategy's base slot
  (10% of equity) × its multiplier; all three sit at **3.0** (~30% of equity
  per name, before LONG's own vol/weekend scaling), in both fleet env files,
  with the committed profiles carrying the same defaults. There is no
  book-level margin ceiling in the way: what bounds a loss is the
  venue-native stop on each position. Held components keep their
  fill-anchored size — the dials reach new entries only. The mainnet account
  document (`operational.mainnet.json`) is static: entry leverage 5, gross
  cap wallet × 5 split between the sleeves, every cap a ratio of tracked
  equity. Mainnet stays `shadow = true`, `ENGINE_LIVE=false`. The sizing is
  a forward-record change point for all fill receipts.
- **The engine owns the demo account, and the sleeves feed it.** It runs
  `e3ac11bf`, with carry_hold **v7** on both CARRY producers: v6's registered
  rule byte-identical plus the pre-settlement exit read (`strategy_profile=v7
  early_exit=1` — the early exit fires on the venue's running rate up to 15
  minutes before a dying print pays; settled-print fallback kept).

  The engine recovers fills its stream never delivered from the venue's own
  execution history — at boot and after every private-stream reconnect — so the
  ledger does not drift behind the venue. A reconciliation finding latches the
  may-open gate, and `engine reconcile-clear` is the deliberate operator act
  that gate waits for ([docs/engine.md](docs/engine.md) §Safety posture); a
  fresh finding latches again. The engine keeps an in-flight cover book and
  rotates its WAL in segments. The who-opened-what ledger (fill attribution)
  follows the venue: boot drops a sleeve's claim on any symbol the venue
  reports flat (durable `ClaimsDropped` receipt in the WAL), and a
  `reconcile-clear` restatement clears claims on the symbols it reports flat —
  so a close the log never got to charge cannot lock other sleeves out of a
  name.

  The demo engine runs `leverage_authority = "sole"` (set in the host's
  `/etc/liquidity-migration/engine.toml`, which staged deploys deliberately
  never rewrite; backup beside it). Mainnet stays `"shared"` — the owner
  hand-trades there.

  Six `account/` modules are the producers' library, not a dormant order path,
  and every one is load-bearing: the producers, the watchdog and the kept
  quote-lab tooling all reach them (`carry_demo` → `account_route`, the LONG
  producer → `account_service`, `check_fleet_liveness` → `account_kernel` and
  `account_owner_health`, quote lab → `market_capture`, and
  `execution_adapters` carries the never-rename
  `BybitDemoExecutionAdapter.name`). None is deletable at file level, and none
  has a dedicated test file, so coverage there is thin — known and accepted.

  The chain runs end to end:

  - the engine reads the venue and writes `account_equity_usdt` into its
    heartbeat;
  - both producers size from that equity (`carry … equity=$1,412.58 err=none`,
    `long … equity=$1,412.57 owner=healthy`);
  - both write an absolute target book —
    `/var/lib/liquidity-migration/targets/{carry,long}-demo.json`;
  - the engine reads each book, routes it to its own sleeve, and takes on
    symbols the books name that no config listed (`following a symbol a book
    named symbol=HOMEUSDT`).

  Live, carry wants $141.09 of HOMEUSDT against about $137 held, and the engine
  correctly does nothing — $4.09 is inside the 5% dead band.

  **The engine is LIVE on the demo account** (`ENGINE_LIVE=true`, and it holds
  the single-writer lease `bybit-demo-user-555899665.lock`). It is *shadow* in
  `engine.toml`; the env flag is what turns that off, and only ever off.

  What is not done, plainly:

  - **The funded engine runs in shadow and has never sent an order.** Its
    config and env file are on the host, both mainnet producers write books,
    and it has been watched reading the funded account (552445993) under the
    mainnet profile — reference $100, a real per-sleeve partition, and a $100
    gross cap. It sends nothing: `shadow = true` in `engine-mainnet.toml` and
    `ENGINE_LIVE=false` in `engine-mainnet.env`, two switches, both the
    owner's, and it takes no account lease while shadow.

    It writes essentially nothing: 9.6 KB of WAL and 21 KB of syslog in 90
    seconds.

    It is left *running* rather than stopped on purpose. Stopped would not
    stick — the deploy starts it wherever its env file and the binary both
    exist — so a stopped unit would be a false comfort. Shadow is the state
    that cannot trade. To keep it off for good, delete
    `/etc/liquidity-migration/engine-mainnet.env`.

  - **There is no hourly Telegram digest.** Pause, resume and `ops.sh flatten`
    work, on the engine's own path.

  - **`operational.demo.json` carries no `sleeve_limits`**, so there is no
    per-sleeve capital partition on demo: every sleeve draws on the
    account-wide caps and any one can spend the lot. The engine logs
    `sleeves=0` at boot, against `sleeves=2` on mainnet. It is left alone
    deliberately: demo's `capital_reference_usdt` is 250,000 against an account
    holding about $1,400, so every cap in that profile is already far above
    anything the account can reach and a partition drawn from it would never
    bind. Making it mean something is a retune of the reference, which is a
    dial the owner sets.

- **The engine binary is built in an isolated clone at `/opt/engine-build`** —
  never the deployed checkout the fleet runs from — with its own toolchain
  under `/opt/rust`. Its measured latency on the box, the single-writer lease
  it takes (one kernel `flock` per venue account at
  `/run/lock/liquidity-migration/bybit-{realm}-user-{userID}.lock`, named by
  the account number the venue itself reports), and the mainnet fence are
  [docs/engine.md](docs/engine.md). There are **two demo accounts on the
  box**: the fleet's 555899665, whose lease the live engine holds, and
  579580669 (credentials in `bybit-quote-lab.env`), whose lease nothing holds —
  the lease is what keeps the quote lab and an engine from ever writing to one
  account at once.

### The funded account

- **It holds money: the owner-health read shows equity 541.26 USDT** (read
  2026-08-19 19:24 UTC), and the mainnet CARRY producer sizes its book off it.
  The funding arrived by hand, outside the bot — not independently confirmed
  beyond the health read. The funded engine is **shadow** either way; money in
  the account changes what the producers publish, not what can trade.
- **There is no daily loss halt.** The whole control is out of the tree —
  `loss_guard.rs`, `LossGuardConfig`, the `LossGuardTripped` refusal, the
  `max_daily_loss_usdt` profile key and the `RM_DAILY_LOSS_FRACTION` dial. A
  profile that still carries the key is refused by name at start-up rather than
  silently ignored, and a host env that still carries the dial fails the render.
  **What bounds a loss now is the venue-native stop on each position. Nothing
  bounds the accumulation of many stopped positions in one day** — the owner
  accepted that knowingly. Its absence is a decision, not a fault: do not
  re-add it ([AGENTS.md](AGENTS.md)).
- **Real money is armed**, and the owner hand-trades the same venue account.
- **The bot and the owner keep separate books on one account.** Venue exposure
  above what the bot owns, and venue orders the bot did not place, are recorded
  in the venue snapshot (`foreign_positions`, ownership `status`) and left
  strictly alone — never traded, never blocking. The bot claiming exposure the
  venue does *not* hold still blocks, and heals itself by booking the reduction
  down to flat. A symbol carrying foreign exposure is still swept for its stop;
  skipping it would age its freshness out and re-block the account on `native
  protection health is stale`.
- **Shared leverage authority is on for mainnet**: the mainnet owner unit
  carries `Environment=ACCOUNT_SHARED_LEVERAGE_AUTHORITY=1` →
  `--shared-leverage-authority`, so a symbol that goes flat forgets its cached
  leverage and its next entry pays one `set_leverage` round trip (188–194 ms) —
  the cost of not sizing against a leverage somebody else changed. Demo is
  unaffected: the variable is unset there and unset means off. A venue value that
  contradicts the cache still drops it under either setting.
- **A `-21 USDT` available margin is the owner trading by hand, read correctly**,
  not a fault.
- **The safety stop covers the owner's hand-placed size.** The manager only
  creates Bybit **Full-position** stops (`tpsl_mode="Full"`), which close the
  entire venue position at trigger, so the bot's stop sits over the combined
  position whenever the owner scales a coin by hand. Known and accepted; the venue
  offers one stop per coin and cannot split it.
- **Cross margin and one-way position mode, and one-way is load-bearing**: the
  fleet places every order and stop with `positionIdx 0` and the protection layer
  refuses nonzero-index rows, so a venue-side switch to hedge mode would reject
  every fleet order. No startup check pins either mode — proposed, owner to
  decide.
- **No copy of the funded API key remains on the laptop.**
  `/etc/liquidity-migration/bybit-mainnet.env` on the host is the only copy and
  the only authority (`REAL_MONEY=true` and the carry stop 0.35). The key was
  readable in plaintext on the Desktop from 2026-08-05 to 2026-08-08, so
  **rotation is still owed and is the owner's act.**

### Trading-rule receipts

- **The funded pair is fresh.** `candidate-universe-20260817T205337Z` /
  `venue-rules-20260817T205337Z`, frozen as one pair from the live venue on
  2026-08-17 20:53 UTC; expires **2026-08-24 ~20:53 UTC**, and every deploy
  renews it (a failed renewal keeps the installed pair and the deploy
  finishes). The rules also cover any symbol the account still has exposure on,
  so a retiring symbol that is still held does not wedge the LONG cycle —
  entries stop, exits keep publishing until flat.
- **The demo receipt is fresh.** `demo-rules-20260818T220119Z`, probed
  2026-08-18 22:01 UTC inside a flat maintenance window. Expires **2026-08-25
  ~22:01 UTC**; any rollout in the back half re-probes on its own.

### Execution and market data

- **The engine says what its fills cost.** It keeps `is_maker` from the venue's
  execution row and writes the midpoint an order was decided against onto the
  order's own log record, so arrival shortfall, effective spread, fee and all-in
  are derivable from the log alone; the signed markout at 1 s / 15 s / 1 min /
  5 min is written when its horizon comes due, because a log holds no prices.
  Names and signs are `docs/architecture.md` §Trade diagnostics. `engine fills
  --wal PATH` is the read, per sleeve and symbol; five of the numbers are in the
  heartbeat. **`M0` is the top of book**, so nothing here measures impact. The
  engine is the only writer of these receipts, for its own fills only.
- **A deploy restarts both engines.** They share one binary, and
  `mainnet-engine-ok` prints beside `engine-ok`. Verify a deploy by a field only
  the new code produces, read on **both** heartbeats — "active" says nothing
  about which binary.
- **A resting entry waits 120 s.** That is the engine's
  `WorkPolicy::default().window_ms` (`engine-types/src/orders.rs`), it is not
  exposed in the strategy block, so no host setting reaches it. Measured on 15
  live resting entries, fills came at a median of 1.28 s and a maximum of
  36.6 s.
- **Pricing and market data for the order path live in the engine.** It
  subscribes its own venue stream per followed symbol and refuses an entry
  decided against a quote older than its declared bound (default 30 s —
  [docs/engine.md](docs/engine.md)).

### Measured latency

The live order path is the Rust engine's; the honest latency contract and the
measured table are [docs/engine.md](docs/engine.md). The short version, measured
on the fleet: **83 ns** to decide, **~2.7 ms** decision to bytes-on-wire (the
fsync-dominated software chain), and live against the venue (n=67): **179 ms
median decision→acknowledgment, 512 ms p90, 1013 ms worst** (at n=67 the tail
figure is the worst of the sample, not an estimated p99). Leverage pre-arm takes
the last software round trip out of an entry — 8.7 ms decided→wire on a
leverage-needing entry, where paying that round trip cost ~169 ms median.

- **The ~172 ms venue round trip is geography** — `api.bybit.com`,
  `api.bytick.com` and `api.byhkbit.com` are the same Frankfurt CloudFront edge
  proxying to an Asian origin. No code change reaches it; a host near the origin
  is the only lever and the largest single win left. Owner decision.
- **What remains of the software tail is grouped sibling entries sending
  serially** — each awaits the previous order's ~190 ms venue acknowledgment
  (measured: 8.7 / 199 / 369 ms for three same-decision entries). Concurrent
  sends are the one software lever left; observed, not built.
- **Cross-session latency comparisons are confounded by account-state growth** —
  protections, orders, decisions and executions accumulate for the life of the
  account and nothing prunes them. Compare within a run, or reset the epoch first.

## Topology

Nine units on and active: the demo engine (the account owner, LIVE), the
mainnet engine (shadow), demo LONG and CARRY producers, mainnet LONG and CARRY
producers, the Telegram controls daemon, and the demo and mainnet liveness
timers. The host carries exactly the eleven unit files in `deploy/systemd/` and
nothing else. Paper is retired whole; demo is the only practice book.

| Kind | Units |
| --- | --- |
| Account owners (engines) | demo (LIVE), mainnet (shadow) |
| Target producers | demo × LONG/CARRY, mainnet × LONG/CARRY |
| Always-on daemon | Telegram controls |
| Timers | demo liveness, mainnet liveness (both active) |

Bulk collectors are removed and raw account-market persistence is disabled. Live
L2 readiness and exact decision-book capture remain enabled.

## Risk envelope

**Demo** (risk-on): capital reference 250,000 USDT, per-symbol notional
125,000, component/account gross 1,250,000, initial margin 250,000. Entry
leverage 5× on every sleeve, account max leverage 5×, LONG notional multiplier
3.0 and CARRY multiplier 3.0 (per-name 0.10 and gross cap 1.0 come from the
registered rule and multiply through, so each new carry name takes 30% of the
sizing equity and a full CARRY book is 3× it). Startup and authorization
reject unknown profile fields and producer leverage above the owner cap; how
large a book the multipliers build is the owner's dial, bounded per position
by each venue-native stop.

**Real money**: sizing is the same three `*_NOTIONAL_MULTIPLIER` env dials
as demo, in `/etc/liquidity-migration/bybit-mainnet.env` (all at 3.0).
`RM_CARRY_STOP_LOSS_FRACTION` (**0.35**) is the protection dial and is the
owner's own. The account document (`configs/operational.mainnet.json`) is
static: entry leverage 5×, gross cap = wallet × 5 split carry 200/long 300,
margin cap = wallet, symbol cap half the wallet — every cap a ratio of the
equity-tracked reference, proved at load and re-proved on each rebase. A
book the dials build past those caps is refused per entry by the engine's
runtime admission; a retired `RM_*` line in an env file is refused by name.

## Standing operational constraints

- **Arming real money is one switch, set by the owner's own hand**:
  `REAL_MONEY=true` in the root-owned
  `/etc/liquidity-migration/bybit-mainnet.env`, beside the live key. A git commit
  can never arm; activation still walks the full preflight, and every
  capital-preservation control (envelope, native stops, partition,
  single-writer lease, reconciliation) gates the start.
- **The funded account must stay in one-way position mode** (see Now — a
  venue-side switch to hedge mode would reject every fleet order).
- **A guarded rollout proves the account venue-flat**; the proof binds on
  mainnet and is advisory off it. A failed verification is not permission to
  hand-start a partial fleet.
- **Both trading-rule receipts are a side effect of deploying, not a deadline.**
  Any deploy past half the 168-hour age bound renews them: demo by the
  order-placing probe, and the funded receipt by the read-only instruments-info
  freeze, which places no orders and needs no stopped window. Only mainnet holds
  the 168-hour ceiling as a hard one, so its expiry would be an owner that
  refuses to start; the other registered startup ceilings (warmup timeout,
  INVOCATION_ID, stray-order gate) also bind mainnet only.
- **The watchdog watches both rules receipts.** The mainnet liveness scope
  validates the funded receipt through the loader that admits one and pages
  WARNING inside 24 hours of expiry and CRITICAL past it, under its own
  `venue_rules_age` key with the deploy-renews-it remedy
  ([`check_fleet_liveness.py`](scripts/runtime/check_fleet_liveness.py)).
- **Unknown safety-critical state fails closed.**
- **Deploy is one command from the primary checkout** (`scripts/ops.sh deploy
  staged|rollout`). The manual GitHub workflow exposes `rollout`, `install`,
  `activate`, `verify`; the two mainnet modes are deliberately absent from CI
  ([docs/operations.md](docs/operations.md)). Installing over an armed funded
  fleet requires stopping it: with real money armed and a `-mainnet` unit up,
  `resolve_stop_first` turns stop-first off and `require_quiescent` refuses unless
  `--stop-first` is passed explicitly. Push only from the primary checkout until
  the pre-push hook's git-fixture tests are hermetic (a linked worktree run
  corrupted the repo once).
- **The rollback floor is the one-line forward-compat commit `31ee68d`**:
  rolling back past it requires archiving each producer's event tape.
- Three delisting candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`) have
  venue `deliveryTime=1784538000000`, recorded prospectively with their
  first-observed anchors in LONG's private mode-0600 retirement registry; they
  may retire only while positions, targets, orders, and inbox exposure are all
  flat.

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

Change points currently accruing forward days: CARRY `lane2_carry_hold_v6`
(promoted 2026-08-19 with zero forward days at promotion — registered the same
morning; v4 and v5 keep scoring, and the v6−v5 capital-normalised differential
is the registered forward experiment; v4 held the sleeve from 2026-08-03), the
CARRY early exit (2026-08-19 late evening: an exiting name is sold at the
settled print that ends it, not at the next midnight — the registered exit
test at print time, `CARRY_EARLY_EXIT=1` on both carry units, kill switch is
setting it to 0; graded from the engine's realized exit fills), the CARRY v7
pre-settle exit clock (2026-08-19 ~22:49 UTC, `CARRY_STRATEGY_PROFILE=v7` on
both carry units: the same exit test read on the venue's running rate inside
the last 15 minutes before a held name's settlement, selling before the
payment instead of one minute after it; config unchanged — v7 trades
`lane2_carry_hold_v6` byte-identical, so its forward grade continues under one
config id; graded from engine exit fills against the settled-print
counterfactual; rollback dial is `v6`), LONG
v12 wide-stop (2026-08-03), and the entry execution recipes
(quote-first entries, touch-sized windows, and the replay-selected resting recipe,
all 2026-08-04 — deployed with `f85371e`). 2026-08-21 adds three sizing
change points on demo: the multipliers move to a fixed multiplier entry on
both sleeves (carry 2.0→5.0→3.0, LONG 1.5→5.0→3.0 within the day — fill
receipts grade forward from each deploy), and the LLM gate's judged entries
move inside the LONG sleeve — same book and identity from then on, so their
fills grade under LONG v12's config id beside the native entries. The same
day, sizing collapses into three env dials on both fleets (the RM leverage
dials and their render math retire; mainnet's account document goes static
at entry leverage 5) — mainnet has no funded fills to grade yet, so that
lands on the shadow books. The v6 whale halving makes the carry
producers read one non-Bybit input (Binance top-trader EODs, public endpoint,
fail-open under the registered 48h freshness clause). Full statements in
[docs/research/strategy_program.md](docs/research/strategy_program.md).

## Evidence boundary

The funded account has no performance record yet: its first night (2026-08-04)
legitimately decided cash, and the first honest maker-share grade waits on funded
`is_maker` receipts from a non-empty book. The engine records those receipts —
what is still missing is funded fills: the funded engine runs in shadow and
sends nothing. Demo fill economics
are not evidence — demo fills simulate without queue position, and the demo
realm's matching engine holds phantom internal liquidity its published book does
not show.

Funded P&L before 2026-08-07 is overstated: funding was booked whole rather than
by the share this book actually held. Five settlements totalling **+15.23 USDT**
are on the funded journal and the ACEUSDT **+10.72** of that is ≈96% not the
bot's. Since 2026-08-07 each settlement is scaled by `owned_qty_at_settlement /
venue_settled_size`, reconstructed from this book's own fills at the settlement
instant, with the venue's raw numbers kept verbatim (`venue_funding_usdt`,
`venue_settled_size`); the share is 1.0 whenever the venue position is the bot's.

Real money is a separate door: no runtime status or rolling record arms it —
arming is the owner's hand on the switch (constraint above).

Research-only: Strategy Overhaul V2 closed with no qualifying thesis and did not
touch its reserved holdout. The consolidated conclusion and successor direction
are in `docs/research/strategy_program.md`; current anomaly evidence is in
`docs/research/archive/2026-07-24-anomaly-research.md`.

## Known benign alert shapes

Each was diagnosed to a root cause and fixed. Listed so an operator does not
re-diagnose a page that has already been explained.

| Alert shape | Diagnosed cause |
| --- | --- |
| `unowned_venue_order` after a stop triggers | Owner disowning its own just-consumed Full stop while Bybit's open-order cache still lists it. Bounded 10-minute terminal-visibility grace, identity evidence required. |
| `account funding reconciliation is stale: age_ns~4-5e9` | Report timestamped itself before its paginated REST queries, then held to the shared 4-second position bound. Documented 30-second funding floor. |
| `waiting for queue-head market data: X:stale_book` | Lost/rejected orderbook subscribe. Socket rebuilds after 30 frameless seconds for a new subscription. |
| `latest cycle is 0.1 min future-dated` / `future_book` | Local read/update races sampling wall time before the snapshot. Ordering fixed; true future timestamps still page. |
| `continuous-hedge.service FAILED` after an owner-health page | Duplicate of the owner-health root cause. A hedge run blocked by unhealthy owner health exits 0 with a blocked receipt. |
| Negative owner-health ages | Strategy event time reused after concurrent heartbeats. Operational freshness now samples adjacent wall time. |
| `account execution live L2 is N min stale` (~1.5×/day, 3–8 min) | Venue-side quiet subscription on the owner's single-topic BTCUSDT book feed — the socket stays up and answers pings, Bybit stops pushing frames. Not the host, not scheduled, not load. A single stall self-heals in ~2.5 min via the 120 s internal watchdog and never alerts; the alerted episodes are rebuilds that came up quiet again, stretched by a per-attempt clock reset that is now fixed. Fails closed; zero trades lost. If quiet-stalls persist, next lever is a second heartbeat topic or proactive resubscribe. |
| `unadopted external execution: external protection fill is not position-reducing` | A hand-placed spot buy in the demo account UI. The kernel manages linear perps only, so with nothing to reduce it correctly refuses adoption and returns green; no reconciliation drift. An execution with nothing to reduce is logged as foreign and ignored, so the shape does not latch. |
| `ignoring foreign … execution … with no owned position to reduce` | Normal: the owner trading by hand on the same venue account. Recorded, never traded. Only a `venue=…:reconstructed=…:unbacked=…` line is a real fault. |
| `engine_heartbeat_stale: … dated 1s in the future`, firing and clearing all day | Not a clock fault: the watchdog sampled its clock at the top of a ~2 s run and compared it to a file the engine rewrites every 5 s. It reads the file, then the clock. If this shape returns, a clock really is wrong. |
| `account_health_stale` / `account_digest_stale` on demo, never clearing | Both read files whose only writer, the Python account owner, is gone. Freshness comes from the engine's heartbeat; the digest check is unprovisioned. A check that cannot clear is broken, not informative. |

**If the chat is loud and the fleet is green, treat the checks as the suspect.**
The failure mode is not noise but blindness: one real signal in a stream of false
ones is indistinguishable from them.

## Open operational defects

| Item | State |
| --- | --- |
| 2026-08-04 withdrawals await owner confirmation | The venue's own transaction log shows the money leaving through the account login (the API key holds no transfer/withdraw permission — probed, refused), so this was by hand. **If these withdrawals are not the owner's, treat the venue login as compromised immediately** |
| Quote-lab capture spams its own log when disk-blocked | The 6 GB min-free guard stops tape writes but not the process's nohup traceback spam, which can fill the disk to 0 bytes and kill a deploy. Both capture processes on the host are currently killed; the spam shape is still unfixed. (The in-repo quote-lab replay stays: it is the machinery behind the registered entry recipes — CHANGELOG 2026-08-08.) |
| No startup check pins margin/position mode | Cross + one-way are load-bearing (see Now); a venue-side flip is only caught at order rejection. Proposed, owner to decide |
| Nothing bounds convergence toward a stale accepted target while producers are down | Deliberately not built — a liveness-coupled trading halt needing owner design |
| Kline bootstrap logs `failed=N` on restart with an intact store | It re-fetches a window it already holds and counts zero new inserts as failure; bounded ~40–50 s per restart. Tracked follow-up |
| The LONG demo producer is SIGKILLed by every stop | It drains its cycle on SIGTERM, but a cycle runs ~180–350 s against the unit's 90 s `TimeoutStopSec`. Harmless for deploys (`require_quiescent` accepts `failed`, targets publish atomically), but no LONG stop is ever graceful |
| Reported P&L is provisional | Figures are fill-reconstructed, not venue-confirmed (most `pnl` events carry `funding_status=pending_venue_reconciliation`). No closed-loop accounting check yet, which real money needs |
| Entries execute ~23 minutes after the price the scorer models | Live runs the delayed-entry stress case, not the bar-close headline case. Recorded with the measured capacity numbers in `docs/research/carry_hold.md` |
| Intraday notional tracking is bounded, not continuous | Deliberately left as an owner decision; `docs/research/carry_hold.md` §7, item 5 states it rather than treating it as settled |
| Nothing watches the venue and our records disagreeing | `account_health_unhealthy` had one writer, the deleted Python owner; the engine reconciles but publishes no mismatch. Freshness is retargeted at the engine's heartbeat, agreement is not. `gather_account_health_alerts()` is kept uncalled as the specification. Needs the engine to publish a mismatch — a design question, owner to decide ([`docs/notifications.md`](docs/notifications.md) §What is not watched) |
| No positive liveness signal reaches the chat | There is no hourly digest and the dead-man's switch URL is unprovisioned, so silence means either a healthy fleet or a dead box. The engine's heartbeat is checked on-box only, and an on-box watchdog cannot report that the box died |

Audit reports are not kept as standing files. Their findings live in the topic
docs — `docs/research/research_findings.md`, `docs/architecture.md`,
`docs/data.md`, `docs/trading_logic.md`, `docs/notifications.md` — and in Git
history.

## Recovery archive

- 2026-08-03 clean-slate ledger reset (three sleeve roots, account
  journal/inbox/capture, reports, caches):
  `data/_archive/ledger-reset-20260803T143852Z.tar.gz`, SHA-256 `4b729c34…937a`.
- 2026-07-22 owner-authorized full reset (22 account journals, inboxes, captures,
  epoch projections):
  `/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
  (31,490,855 bytes; SHA-256
  `e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`).
- Pre-evidence BTC-risk state, rejected rather than migrated:
  `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
  (SHA-256 `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`).
- 2026-08-05 torn demo event tapes (disk-full casualties):
  `strategy_event_tape.jsonl.enospc-20260805.bak` beside each, in every demo
  producer root.
