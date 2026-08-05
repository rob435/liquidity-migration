# Operational State

Current operational snapshot. **Exact live truth comes from `scripts/ops.sh
status` against the host, not from this prose.** Every observation below is
point-in-time and dated; none of it is a claim about the account right now.

This file describes now. The dated history of how it got here — deploys,
incidents, repairs, change points — is [CHANGELOG.md](CHANGELOG.md). When
something happens, add the dated entry there and edit the sections here to
match; never append history to this file.

## Now (recorded 2026-08-05)

- **Host runs `f85371e`, whole fleet green since 2026-08-05 10:18 UTC**: all
  nine units on/active/enabled, receipt `verify-ok commit=f85371e
  requested=f85371e profile=operational mainnet=armed`.
- **Real money is armed.** The funded account's owner reports healthy; last
  equity read 99.94 USDT through the coin-row wallet fallback (`48ebc50`) —
  roughly 100 USDT remains after the 2026-08-04 withdrawals (CHANGELOG entry
  of that date; owner confirmation still outstanding, see Open defects).
- **The funded account is on cross margin and one-way position mode, and
  one-way is load-bearing**: the fleet places every order and stop with
  `positionIdx 0` and the protection layer refuses nonzero-index rows, so
  enabling the venue's hedge mode would reject every fleet order. No startup
  check pins either mode — proposed, owner to decide.
- **Committed but not yet deployed** (`main` is ahead of the host): the
  four-dial real-money surface (`8303b34`), the 10× dial ceiling (`ad960df`),
  and the per-entry LONG size dial (`4269369`) — all mainnet-profile work
  whose committed defaults render byte-identically to the deployed sizing.
  **The host's `bybit-mainnet.env` still carries the retired `RM_*` dial
  names, so the next mainnet render/activation refuses until those lines are
  replaced with the four new dials** (the local `deploy/.env` staging copy is
  already converted).

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

**Real money** (as committed on `main` 2026-08-05; the deployed host still
renders the equivalent previous sizing): four owner dials.
`RM_CARRY_LEVERAGE` (default 1.0) and `RM_LONG_LEVERAGE` (0.75) are each
sleeve's book ceiling as a multiple of equity, worst case included — each
carry name takes a tenth of its dial, each LONG entry ≈ its dial / 18.75, and
the two dials may total 9.9. `RM_DAILY_LOSS_FRACTION` (0.1) and
`RM_CARRY_STOP_LOSS_FRACTION` (0.35) are the protections. One optional extra,
`RM_LONG_MAX_ORDER_NOTIONAL_PCT_EQUITY`, sets each LONG entry's size as a
fraction of equity (0, the default, keeps the strategy's own derivation).
Everything else the old surface exposed is derived and still proved at
render; a retired `RM_*` line in an env file is refused by name. The defaults
reproduce the previous effective sizing exactly (carry multiplier 1.0, LONG
0.4; derived account gross cap 1.7677×). Honest protection note: the loss
halt fires on realised loss only, so a dialled-up open book meets the venue's
liquidation engine before the halt.

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
| `unadopted external execution: external protection fill is not position-reducing` (2026-07-27 10:47:20, once) | A manual ~10 USDT spot BTCUSDT buy made in the demo account UI at 10:47:20.106 (venue execution `1784799743630817527`), three minutes before the owner's 250k top-up transfers (+90k 10:49:40, +100k 10:52:20, +50k 10:52:26). The kernel manages linear perps only and was flat, so it correctly refused adoption, surfaced the error once, and health returned to green. The ~0.000153 BTC sits in the unified wallet outside the managed book; no reconciliation drift. |

## Open operational defects

| Item | State |
| --- | --- |
| Host `bybit-mainnet.env` carries retired `RM_*` dial names | The next mainnet render/activation refuses by name until the owner replaces them with the four new dials (2026-08-05); the local `deploy/.env` staging copy is already converted |
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
