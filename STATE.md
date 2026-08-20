# Operational State

Current operational snapshot. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.** Every observation below is
point-in-time and dated; none of it is a claim about the account right now.

This file describes now. The dated history of how it got here — deploys,
incidents, repairs, change points — is [CHANGELOG.md](CHANGELOG.md). When
something happens, add the dated entry there and edit the sections here to
match; never append history to this file.

## Now

### The fleet

- **A third sleeve, the EXODUS SHORT, is registered and deployed to demo
  (2026-08-20, owner: "build the exodus short as a standalone strat sleeve,
  but synergising").** When carry's v7 pre-settle exit fires, the carry
  producer publishes the abandoned position as a SHORT to the engine's new
  `exodus` sleeve (its own `[[strategy]]` block — appended, per the id
  discipline — book `exodus-demo.json`, fill attribution), covered 60
  minutes after the settlement. Registered config
  `configs/lane2_exodus_short_v1.json`; dial `EXODUS_SHORT_PROFILE=v1` on
  the demo carry unit only, mainnet unset; unsetting it drains the book
  flat. The declared 0.35 stop is a disaster fence (every measured stop
  level loses; the cover clock is the exit). No live fire yet — the sleeve
  waits for the first v7 fire like everything else. Evidence and the
  honest 2024-negative era shape: `docs/research/research_findings.md`
  §the exodus short; promotion note in `strategy_program.md`.
- **Demo sizing since 2026-08-20 evening: each sleeve sizes from HALF the
  account at 5x entry leverage** (owner directive; CHANGELOG that day).
  `operational.demo.json`: carry multiplier 0.5 (was 1.0; LONG already
  0.5; exodus inherits carry's notional so it halves too), all
  `entry_leverage` 5.0, account `max_leverage` 5.0. A forward-record
  change point for all demo fill receipts. Mainnet untouched.
- **The engine owns the demo account, and the sleeves feed it.** First
  deployed 2026-08-14 20:34 UTC; currently at `e3ac11bf` (staged deploy
  2026-08-20 ~15:5x UTC — the exodus sleeve plus the feed interning-order
  fix its first boot exposed, CHANGELOG that day). The 2026-08-19 ~22:49 deploy (`68b6a29e`) put carry_hold **v7** on
  both CARRY producers: v6's registered rule byte-identical plus the pre-settlement
  exit read (`strategy_profile=v7 early_exit=1` — the early exit now fires
  on the venue's running rate up to 15 minutes before a dying print pays;
  settled-print fallback kept — CHANGELOG 2026-08-19). Earlier the same
  day: `8074942d` (~19:20 UTC) promoted v6 + the Binance whale-EOD cache,
  and `f38f38d7` (~21:55 UTC) shipped the settled-print early exit. The
  first live early-exit fire has receipts: EDENUSDT fired 21:57:06 UTC,
  engine sold all 1,526 units one second later (WAL fill 0.05413, tag
  `book-exit`).
- **The demo engine's entry block is CLEARED, and the fault class is fixed
  for good (2026-08-20 ~00:10 UTC, owner: "fix this permanently").** The
  engine now recovers fills its stream never delivered from the venue's own
  execution history — at boot and after every private-stream reconnect —
  so the ledger stops drifting behind the venue; and `engine
  reconcile-clear` is the deliberate operator act the may-open latch waits
  for (docs/engine.md §Safety posture). The inherited ACE debt (−14,455.6
  logged vs 371.1 at the venue) was cleared with a journaled `latch_cleared`
  record; the next boot reconciled with **zero findings, may_open true**,
  and the 00:20 boundary traded normally (2 orders decided). The check
  itself is untouched: a fresh finding latches again. CHANGELOG 2026-08-20. The prior `ee8b72a6` (2026-08-18)
  brought the in-flight cover book, WAL
  segment rotation, the quote-age bound, and leverage pre-arm. The demo
  engine runs `leverage_authority = "sole"` (set in the host's
  `/etc/liquidity-migration/engine.toml`, which staged deploys deliberately
  never rewrite; backup beside it). Mainnet stays `"shared"` — the owner
  hand-trades there. The Python order path is gone from the repository and
  from the host: the account owner's runner, its two units, its launcher
  scripts and its risk layer, about 25,000 lines. Six `account/` modules
  survived that deletion and the 2026-08-19 import-graph check proved every
  one load-bearing — the producers, the watchdog, and the kept quote-lab
  tooling all reach them (`carry_demo` → `account_route`, the LONG producer
  → `account_service`, `check_fleet_liveness` → `account_kernel` and
  `account_owner_health`, quote lab → `market_capture`, and
  `execution_adapters` carries the never-rename
  `BybitDemoExecutionAdapter.name`). They are the producers' library now,
  not a dormant order path; none is deletable at file level. Their dedicated
  test files went with the runner, so coverage is thinner than before —
  known, accepted 2026-08-19.

  The chain runs end to end and was watched doing it:

  - the engine reads the venue and writes `account_equity_usdt` into its
    heartbeat;
  - both producers size from that equity (`carry … equity=$1,412.58 err=none`,
    `long … equity=$1,412.57 owner=healthy`);
  - both write an absolute target book —
    `/var/lib/liquidity-migration/targets/{carry,long}-demo.json`;
  - the engine reads each book, routes it to its own sleeve, and takes on
    symbols the books name that no config listed (`following a symbol a book
    named symbol=HOMEUSDT`).

  It has sent orders on this path: in shadow the log records `wants strategy 0
  Sell 14110 of symbol 4 [book-exit]` and `allowed … for 14110`, against a
  zero target carry published while its equity read was blocked. Live, carry
  now wants $141.09 of HOMEUSDT against about $137 held, and the engine
  correctly does nothing — $4.09 is inside the 5% dead band.

  **The engine is LIVE on the demo account** (`ENGINE_LIVE=true`, and it holds
  the single-writer lease `bybit-demo-user-555899665.lock`). It is *shadow* in
  `engine.toml`; the env flag is what turns that off, and only ever off.

  What is not done, plainly:

  - **The funded engine runs in shadow and has never sent an order.** Its
    config and env file are on the host, both mainnet producers write books,
    and it has been watched reading the funded account (552445993, equity
    $0.0397) under the mainnet profile — reference $100, gross cap $175, a real
    per-sleeve partition. It sends nothing: `shadow = true` in
    `engine-mainnet.toml` and `ENGINE_LIVE=false` in `engine-mainnet.env`, two
    switches, both the owner's, and it takes no account lease while shadow.

    It is left *running* rather than stopped on purpose. Stopped would not
    stick — the deploy starts it wherever its env file and the binary both
    exist — so a stopped unit would be a false comfort. Shadow is the state
    that cannot trade. To keep it off for good, delete
    `/etc/liquidity-migration/engine-mainnet.env`.

  - **The hourly Telegram digest did not come back.** Pause and resume did;
    `ops.sh flatten` did, on the engine's own path.

  - **`operational.demo.json` carries no `sleeve_limits`**, so there is no
    per-sleeve capital partition on demo: every sleeve draws on the
    account-wide caps and any one can spend the lot. The engine logs
    `sleeves=0` at boot, against `sleeves=2` on mainnet. It is left alone
    deliberately: demo's `capital_reference_usdt` is 250,000 against an account
    holding about $1,400, so every cap in that profile is already far above
    anything the account can reach and a partition drawn from it would never
    bind. Making it mean something is a retune of the reference, which is a
    dial the owner sets.

- **Before this, the host ran `2bd3a00`**, deployed 2026-08-13 23:46 UTC by `staged
  --stop-first` from main (`staged-ok
  commit=2bd3a0090deb2dbf34f04e34df14f320f190744f profile=operational`,
  `verify-ok … mainnet=armed`). All nine units active and enabled, zero
  error-level lines after. This put the wave-3 order path live for the first
  time — one durable write per queued request, the price-touch wake, the GC
  discipline. The first carry boundary on it (2026-08-14 00:20 UTC) worked as
  designed: freeze ahead at 00:18:45, deadline wake at 00:20:00.000 exactly,
  frozen book published in that pass (`build_skipped=True`, 2 exits + 1 entry
  grouped, `err=none`) — receipts in CHANGELOG 2026-08-14. The one-line
  rollback floor `31ee68d` remains: rolling back past it requires archiving
  each producer's event tape.
- **The engine binary is built in an isolated clone at `/opt/engine-build`** —
  never the deployed checkout the fleet runs from — with its own toolchain
  under `/opt/rust`. Its measured latency on the box, the single-writer lease
  it takes (one kernel `flock` per venue account at
  `/run/lock/liquidity-migration/bybit-{realm}-user-{userID}.lock`, named by
  the account number the venue itself reports), and the mainnet fence are
  [docs/engine.md](docs/engine.md). There are **two demo accounts on the
  box**: the fleet's 555899665, whose lease the live engine holds, and
  579580669 (credentials in `bybit-quote-lab.env`), where the engine ran its
  first live end-to-end day on 2026-08-14 and whose lease nothing holds now —
  the lease is what keeps the quote lab and an engine from ever writing to one
  account at once.

### The funded account

- **It holds money again: the 2026-08-19 19:24 UTC owner-health read shows
  equity 541.26 USDT** (the mainnet CARRY producer sized its book off it, so
  the mainnet target book is no longer near-zero). The funding arrived by
  hand, outside the bot — not independently confirmed beyond the health
  read. Before that it was near-empty: equity ≈0.04 USDT (read 2026-08-12
  19:14 UTC: 0.0398, available 0.0073, `healthy`, no positions), after going
  306.06 → ~0 entirely outside the bot's halted book — hand trading and/or
  funds moved off, cause not independently confirmed. The funded engine
  remains **shadow** either way; money in the account changes what the
  producers publish, not what can trade.
- **The daily loss halt tripped 2026-08-10 and was reset 2026-08-12 19:13 UTC by
  owner instruction.** Hand-trading drawdown took equity 450.08 → 306.06,
  crossing the 76.52 USDT ceiling; the bot's book was flat and it refused new risk
  for the two days between. The reset archived the anchor file beside itself
  (`account_loss_guard.json.tripped-20260810.reset-20260812T191330Z.bak`) and
  re-anchored the day cleanly on restart.
- **Real money is armed**, and the owner hand-trades the same venue account.
- **The bot and the owner keep separate books on one account** (owner's decision
  2026-08-07). Venue exposure above what the bot owns, and venue orders the bot
  did not place, are recorded in the venue snapshot (`foreign_positions`,
  ownership `status`) and left strictly alone — never traded, never blocking. The
  bot claiming exposure the venue does *not* hold still blocks, and heals itself
  by booking the reduction down to flat. A symbol carrying foreign exposure is
  still swept for its stop; skipping it would age its freshness out and re-block
  the account on `native protection health is stale`.
- **Shared leverage authority is on for mainnet** (since 2026-08-09): the mainnet
  owner unit carries `Environment=ACCOUNT_SHARED_LEVERAGE_AUTHORITY=1` →
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
- **No copy of the funded API key remains on the laptop.** `deploy/.env` is
  deleted; `/etc/liquidity-migration/bybit-mainnet.env` on the host is the only
  copy and the only authority (`REAL_MONEY=true`, carry 2.0, long 1.88, daily
  loss 0.25). The key was readable in plaintext on the Desktop from 2026-08-05 to
  2026-08-08, so **rotation is still owed and is the owner's act.**

### Trading-rule receipts

- **The funded pair is fresh.** `candidate-universe-20260817T205337Z` /
  `venue-rules-20260817T205337Z`, frozen as one pair from the live venue on
  2026-08-17 20:53 UTC; expires **2026-08-24 ~20:53 UTC**, and every deploy
  renews it (a failed renewal keeps the installed pair and the deploy
  finishes). The rules also cover any symbol the account still has exposure on,
  so a retiring symbol that is still held no longer wedges the LONG cycle —
  entries stop, exits keep publishing until flat.
- **The demo receipt is fresh again.** `demo-rules-20260818T220119Z`,
  probed 2026-08-18 22:01 UTC inside a flat maintenance window (flatten
  through the engine; the last 0.1 ACEUSDT was sub-minimum dust plus a
  pre-engine foreign remainder, closed reduce-only with the leased client
  while the engine was stopped), bound by the same staged deploy that
  reinstalled `54560ea5`. Expires **2026-08-25 ~22:01 UTC**; any rollout in
  the back half re-probes on its own. The watchdog confirmed
  `demo_rules_age` cleared at 22:24 UTC.

### Execution and market data

- **The engine says what its fills cost, since 2026-08-14.** It keeps
  `is_maker` from the venue's execution row and writes the midpoint an order
  was decided against onto the order's own log record, so arrival shortfall,
  effective spread, fee and all-in are derivable from the log alone; the signed
  markout at 1 s / 15 s / 1 min / 5 min is written when its horizon comes due,
  because a log holds no prices. Names and signs are `docs/architecture.md`
  §Trade diagnostics, unchanged. `engine fills --wal PATH` is the read, per
  sleeve and symbol; five of the numbers are in the heartbeat. **`M0` is the
  top of book**, so nothing here measures impact.

  This replaces a producer the fleet lost: `post_fill_markouts.py` and the
  owner loop that drained it went with the Python order path, and
  `market_capture.register_post_fill_markouts` has had no production caller
  since. The readers survived the writer; the engine is the new writer, for
  its own fills only.
- **A deploy restarts both engines, since 2026-08-14.** They share one binary
  and the funded engine is started in the activate phase, before the build; the
  build restarted only the demo unit, so the funded engine ran whatever binary
  it came up with. `mainnet-engine-ok` now prints beside `engine-ok`. Verify a
  deploy by a field only the new code produces, read on **both** heartbeats —
  "active" says nothing about which binary.
- **Entries rest for 45 s, not 120 s.** 15 live resting entries filled at a
  median of 1.28 s and a maximum of 36.6 s, so 45 s keeps every passive fill
  120 s got and bounds the tail; 30 s would have crossed 1 of 15, 15 s, 3 of 15.
- **Pricing and market data for the order path now live in the engine.** It
  subscribes its own venue stream per followed symbol and refuses an entry
  decided against a quote older than its declared bound (default 30 s —
  `docs/engine.md`). Four bullets that used to sit here — producer-price
  sizing, the touch feed, ticker-touch reads — described the Python account
  owner deleted 2026-08-14; they were removed 2026-08-19 and CHANGELOG holds
  the history.

### Measured latency

The live order path is the Rust engine's; the honest latency contract and the
measured table are `docs/engine.md`. The short version, measured on the fleet:
**83 ns** to decide, **~2.7 ms** decision to bytes-on-wire (the
fsync-dominated software chain), and live against the venue 2026-08-18
(n=67): **179 ms median decision→acknowledgment, 512 ms p90, 1013 ms p99**.
The 2026-08-18 leverage pre-arm removed the last software round trip in
entries — proven live 2026-08-19, 8.7 ms decided→wire on a leverage-needing
entry that paid ~169 ms median the day before. (The owner-loop numbers that
used to sit here — 76 ms loop, 25.7 ms durable-to-wire, 276 ms entries —
measured the Python owner deleted 2026-08-14 and moved to history.)

- **The ~172 ms venue round trip is geography** — `api.bybit.com`,
  `api.bytick.com` and `api.byhkbit.com` are the same Frankfurt CloudFront edge
  proxying to an Asian origin. No code change reaches it; a host near the origin
  is the only lever and the largest single win left. Owner decision.
- **What remains of the software tail is grouped sibling entries sending
  serially** — each awaits the previous order's ~190 ms venue acknowledgment
  (measured 2026-08-19: 8.7 / 199 / 369 ms for three same-decision entries).
  Concurrent sends are the one software lever left; observed, not built.
- **Cross-session latency comparisons are confounded by account-state growth** —
  protections, orders, decisions and executions accumulate for the life of the
  account and nothing prunes them. Compare within a run, or reset the epoch first.

## Topology

Nine units on and active: the demo engine (the account owner, LIVE), the
mainnet engine (shadow), demo LONG and CARRY producers, mainnet LONG and CARRY
producers, the Telegram controls daemon, and the demo and mainnet liveness
timers. Checked 2026-08-14: the host carries
exactly the eleven unit files in `deploy/systemd/` and nothing else — the
retired CONTINUOUS producer and the hedge and residual-momentum timers are
gone from the box, so the warning that used to stand here about a re-enabled
unit failing to start no longer applies. Paper is retired whole (2026-08-03);
demo is the only practice book.

| Kind | Units |
| --- | --- |
| Account owners (engines) | demo (LIVE), mainnet (shadow) |
| Target producers | demo × LONG/CARRY, mainnet × LONG/CARRY |
| Always-on daemon | Telegram controls |
| Timers | demo liveness, mainnet liveness (both active) |

Bulk collectors are removed and raw account-market persistence is disabled. Live
L2 readiness and exact decision-book capture remain enabled.

## Risk envelope

**Demo** (the 25× profile, deployed 2026-07-27): capital reference 250,000 USDT,
entry leverage 2×, per-symbol notional 125,000, component/account gross 500,000,
initial margin 250,000; LONG notional multiplier 0.5, CARRY multiplier 1.0
(per-name 0.10 and gross cap 1.0 from the registered rule, so the CARRY book tops
out at 1.0× the reference, unlevered). Startup and authorization reject unknown
profile fields, producer leverage above the owner cap, or registered envelopes
outside the bound profile.

**Real money**: four owner dials, set in the host `bybit-mainnet.env` (values read
from the host 2026-08-06; the installed risk profile is the render of them).
`RM_CARRY_LEVERAGE` (**2.0** since 2026-08-06, was 1.0) and `RM_LONG_LEVERAGE`
(**1.88**) are each sleeve's book ceiling as a multiple of equity, worst case
included — each carry name takes a tenth of its dial (≈ $20 on ~$100 equity), each
LONG entry ≈ its dial / 18.75 (≈ $10), and the two dials may total 10.0
(`MAX_REAL_MONEY_LEVERAGE` in `policy/real_money_profile.py`; was 9.9 until the
2026-08-14 CONTINUOUS-envelope removal freed its token share — this line said
9.9 until 2026-08-19).
`RM_DAILY_LOSS_FRACTION` (**0.25**) and `RM_CARRY_STOP_LOSS_FRACTION` (0.35) are
the protections. Everything else the old surface exposed is derived and still
proved at render; a retired `RM_*` line in an env file is refused by name. Derived
venue margin leverage ≈ 3.9× at these dials — on a fixed wallet a bigger book is
more leverage; the two cannot move independently. Honest protection note: the halt
trips on the day's equity reading falling to the floor — paper loss counts,
because equity marks the open book — but it only sees the account as often as
the engine reads it, so a move faster than that reading, or the venue's own
liquidation engine, can still outrun it at high leverage.

## Standing operational constraints

- **Arming real money is one switch, set by the owner's own hand**:
  `REAL_MONEY=true` in the root-owned
  `/etc/liquidity-migration/bybit-mainnet.env`, beside the live key. A git commit
  can never arm; activation still walks the full preflight, and every
  capital-preservation control (loss halt, envelope, native stops, partition,
  single-writer lease, reconciliation) gates the start.
- **The funded account must stay in one-way position mode** (see Now — a
  venue-side switch to hedge mode would reject every fleet order).
- **A guarded rollout proves the account venue-flat**; since the 2026-08-03
  de-friction purge the proof binds on mainnet and is advisory off it. A failed
  verification is not permission to hand-start a partial fleet.
- **Both trading-rule receipts are a side effect of deploying, not a deadline.**
  Any deploy past half the 168-hour age bound renews them: demo by the
  order-placing probe, and since 2026-08-09 the funded receipt by the read-only
  instruments-info freeze, which places no orders and needs no stopped window.
  Only mainnet holds the 168-hour ceiling as a hard one, so its expiry would be an
  owner that refuses to start; the other registered startup ceilings (warmup
  timeout, INVOCATION_ID, stray-order gate) also bind mainnet only.
- **The watchdog watches both rules receipts** (deployed with `2bd3a00`). The
  mainnet liveness scope validates the funded receipt through the loader that
  admits one and pages WARNING inside 24 hours of expiry and CRITICAL past it,
  under its own `venue_rules_age` key with the deploy-renews-it remedy
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
  corrupted the repo once, repaired 2026-08-03).
- Three delisting candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`) have
  venue `deliveryTime=1784538000000`, recorded prospectively in private mode-0600
  retirement registries; they may retire only while positions, targets, orders,
  and inbox exposure are all flat. (Recorded under CONTINUOUS when that sleeve
  existed; the 2026-08-14 schema-5 converter re-keyed them to LONG's registry
  with their first-observed anchors intact — this line said "CONTINUOUS
  candidates" until 2026-08-19.)

## Forward evidence stream

Everything that runs is graded under the Progressive Evidence Model
([docs/research/governance.md](docs/research/governance.md)): each committed
config is graded on the run of days it postdates, continuously, with recorded
change points — the commit is the registration; there is no waiting window and no
separate registration artifact. (The earlier prospective epoch machinery was
deleted 2026-07-24; its receipts survive on disk but nothing reads them.)

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
all 2026-08-04 — deployed with `f85371e`). The v6 whale halving makes the carry
producers read one non-Bybit input (Binance top-trader EODs, public endpoint,
fail-open under the registered 48h freshness clause). Full statements in
[docs/research/strategy_program.md](docs/research/strategy_program.md).

## Evidence boundary

The funded account has no performance record yet: its first night (2026-08-04)
legitimately decided cash, and the first honest maker-share grade waits on funded
`is_maker` receipts from a non-empty book. Since 2026-08-14 the engine records
those receipts — what is still missing is a funded account with money in it. Demo fill economics are not evidence —
demo fills simulate without queue position, and the demo realm's matching engine
holds phantom internal liquidity its published book does not show.

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
| `account execution live L2 is N min stale` (~1.5×/day, 3–8 min) | Root-caused 2026-07-27: venue-side quiet subscription on the owner's single-topic BTCUSDT book feed — the socket stays up and answers pings, Bybit stops pushing frames. Not the host, not scheduled, not load. A single stall self-heals in ~2.5 min via the 120 s internal watchdog and never alerts; the alerted episodes are rebuilds that came up quiet again, stretched by the old per-attempt clock reset (fixed in `d11db79`+`7af59f3`, deployed 2026-07-27 ~14:03 UTC). Fails closed; zero trades lost. If quiet-stalls persist, next lever is a second heartbeat topic or proactive resubscribe. |
| `unadopted external execution: external protection fill is not position-reducing` | Seen once (2026-07-27 10:47:20): a manual ~10 USDT spot BTCUSDT buy in the demo account UI. The kernel manages linear perps only and was flat, so it correctly refused adoption, surfaced the error once, and returned green; no reconciliation drift. Since 2026-08-07 an execution with nothing to reduce is logged as foreign and ignored, so the shape no longer latches. |
| `ignoring foreign … execution … with no owned position to reduce` | Normal since 2026-08-07: the owner trading by hand on the same venue account. Recorded, never traded. Only a `venue=…:reconstructed=…:unbacked=…` line is a real fault. |
| `engine_heartbeat_stale: … dated 1s in the future`, firing and clearing all day | Fixed 2026-08-17 (`e547041c`). Not a clock fault: the watchdog sampled its clock at the top of a ~2 s run and compared it to a file the engine rewrites every 5 s. Reads the file then the clock now. If this shape returns, a clock really is wrong. |
| `account_health_stale` / `account_digest_stale` on demo, never clearing | Fixed 2026-08-17. Both read files whose only writer, the Python account owner, was deleted 2026-08-14 19:58. Freshness now comes from the engine's heartbeat; the digest check is unprovisioned. A check that cannot clear is broken, not informative. |

**Alert-channel health, 2026-08-17.** For three days the fleet sent 273 alerts a
day and none described a live fault. That is recorded here because the failure
mode is not noise but blindness: the one real signal in the stream was
indistinguishable from 250 false ones. If the chat is loud and the fleet is
green, treat the checks as the suspect.

## Open operational defects

| Item | State |
| --- | --- |
| 2026-08-04 withdrawals await owner confirmation | The venue's own transaction log shows the money leaving through the account login (the API key holds no transfer/withdraw permission — probed, refused), so this was by hand. **If these withdrawals are not the owner's, treat the venue login as compromised immediately** |
| Quote-lab capture spams its own log when disk-blocked | The 6 GB min-free guard stops tape writes but not the process's nohup traceback spam — it filled the disk to 0 bytes on 2026-08-05 and killed a deploy. Both capture processes on the host are currently killed; the spam shape is still unfixed. (The in-repo quote-lab replay stays: it is the machinery behind the registered entry recipes — CHANGELOG 2026-08-08.) |
| No startup check pins margin/position mode | Cross + one-way are load-bearing (see Now); a venue-side flip is only caught at order rejection. Proposed, owner to decide |
| Nothing bounds convergence toward a stale accepted target while producers are down | Deliberately not built — a liveness-coupled trading halt needing owner design (2026-08-03) |
| Kline bootstrap logs `failed=N` on restart with an intact store | It re-fetches a window it already holds and counts zero new inserts as failure; bounded ~40–50 s per restart. Tracked follow-up |
| The LONG demo producer is SIGKILLed by every stop | It drains its cycle on SIGTERM, but a cycle runs ~180–350 s against the unit's 90 s `TimeoutStopSec`. Harmless for deploys (`require_quiescent` accepts `failed`, targets publish atomically), but no LONG stop is ever graceful |
| Reported P&L is provisional | Figures are fill-reconstructed, not venue-confirmed (most `pnl` events carry `funding_status=pending_venue_reconciliation`). No closed-loop accounting check yet, which real money needs |
| Entries execute ~23 minutes after the price the scorer models | Live runs the delayed-entry stress case, not the bar-close headline case. Recorded with the measured capacity numbers in `docs/research/carry_hold.md` |
| Intraday notional tracking is bounded, not continuous | Deliberately left as an owner decision; `docs/research/carry_hold.md` §7.5 states it rather than treating it as settled |
| Nothing watches the venue and our records disagreeing | Since 2026-08-14. `account_health_unhealthy` had one writer, the deleted Python owner; the engine reconciles but publishes no mismatch. Freshness was retargeted at the engine's heartbeat on 08-17, agreement was not. `gather_account_health_alerts()` is kept uncalled as the specification. Needs the engine to publish a mismatch — a design question, owner to decide ([`docs/notifications.md`](docs/notifications.md) §What stopped being watched) |
| No positive liveness signal reaches the chat | The hourly digest was retired with the Python owner and the dead-man's switch URL is unprovisioned, so silence means either a healthy fleet or a dead box. The engine's heartbeat is checked on-box only, and an on-box watchdog cannot report that the box died |

Audit reports are not kept as standing files. Their findings live in the topic
docs — `docs/research/research_findings.md`, `docs/architecture.md`,
`docs/data.md`, `docs/trading_logic.md`, `docs/notifications.md` — and in Git
history.

## Recovery archive

- 2026-08-03 clean-slate ledger reset (three sleeve roots, account
  journal/inbox/capture, reports, caches):
  `data/_archive/ledger-reset-20260803T143852Z.tar.gz`, SHA-256 `4b729c34…937a`.
- 2026-07-22 owner-authorized full reset (22 account journals, inboxes, captures,
  epoch projections, archived and verified before clearing; config, lock inodes,
  reports, caches, residual-momentum input and root-level market data preserved):
  `/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
  (31,490,855 bytes; SHA-256
  `e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`).
- Pre-evidence BTC-risk state, rejected rather than migrated:
  `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
  (SHA-256 `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`).
- 2026-08-05 torn demo event tapes (disk-full casualties):
  `strategy_event_tape.jsonl.enospc-20260805.bak` beside each, in every demo
  producer root.
