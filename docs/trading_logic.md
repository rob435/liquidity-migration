# Trading logic

What each sleeve trades, how it sizes, how it exits, and where its evidence stops. Code is
the authority: the shared LONG reducer in
[`rules/long_contract.py`](../liquidity_migration/rules/long_contract.py), its live caller
[`long_native_event_demo.py`](../liquidity_migration/strategy/long_native_event_demo.py),
and its registered rules in [`rules/long_native.py`](../liquidity_migration/rules/long_native.py),
[`carry_demo.py`](../liquidity_migration/strategy/carry_demo.py) and
[`rules/carry_hold.py`](../liquidity_migration/rules/carry_hold.py) (scored by
[`financed_longs.py`](../liquidity_migration/research/backtest/financed_longs.py)),
and the independent [`exodus_producer.py`](../liquidity_migration/strategy/exodus_producer.py)
with [`rules/exodus_short.py`](../liquidity_migration/rules/exodus_short.py).

## On today

Publication switches live in [`deploy/sleeves.env`](../deploy/sleeves.env).

| Sleeve | Trades | Demo | Mainnet |
| --- | --- | --- | --- |
| LONG | Long a fresh volume pump, bought on a shallow retrace | on | gated by `REAL_MONEY` |
| CARRY | Long coins whose shorts pay a deep crowd fee | on | gated by `REAL_MONEY` |
| EXODUS | Short the name CARRY just abandoned, through the post-settlement fall | always on; consumes the tape independently | gated by `REAL_MONEY` |

## LONG — `LongV12WideStop`

**Signal.** Two registered profiles share this signal — `long_v11a_profile()` and the
deployed `long_v12_profile()`; they differ only in stop geometry, below. On fully closed
daily bars:

| Filter | Value |
| --- | --- |
| Universe | top 50 by trailing 90d turnover, ≥30d listing history |
| Regime | BTC **and** ETH above their 30d moving averages |
| Volume rank today | ≤ 10 |
| Pump trigger | 1d/3d/7d log return ≥ 2.5σ (30d daily σ); σ unavailable → 15% 1d |
| Close location | ≥ 0.70 (1d trigger), ≥ 0.60 (3d/7d) |
| Volatility ceiling | 14d ATR ≤ 12% of price |
| Signal freshness | < 24h |

The first entry check is one hour after the signal. Entry fires when price touches
`signal_close × 0.99` (`sniper_retrace`), or falls through at the 6-hour deadline while
the signal is still fresh (`sniper_deadline_fallthru`). Ten
concurrent positions, 7-day per-symbol cooldown.

**One decision contract.** Live and research call the pure typed reducer
`decide(DecisionInput, PriorState, StrategyConfig) -> DecisionOutput`. It owns
signal admission, sizing, entry timing, stop geometry and exit timing. The
producer owns inputs, durable state transitions, publication and reporting;
the historical runners own event ordering and accounting, not a second copy of
the rule.

The regime gate reads BTC and ETH daily closes. Both anchor frames are always fetched for
the join even when the frozen candidate artifact excludes the names — with either missing,
both flags read false and every native entry stops — and a force-added anchor is dropped
from candidacy, so the freeze still decides what may be traded. A pump the regime refuses
is counted (`skipped_regime_btc_off` / `skipped_regime_eth_off`) instead of folding into
the same no-signal count as a quiet day.

**Sizing.** Base slot `gross_exposure / max_concurrent_positions` = 10% of equity, times the
profile's `notional_multiplier`, times the BTC-vol scalar `clip(0.60 / btc_rv, 0.30, 1.25)`,
times the vol-parity weight `max(min(0.30/vol_used, 3.0), 0.25)` (30d realized vol, 30%
annual floor, 30% position-weight cap), times 1.5 on weekend entries. Entry leverage 5
changes margin only, never quantity. Five new entries per cycle maximum; how large a
multiplier runs is the owner's dial in the operational profile. The producer
resolves one typed effective config before planning and records field-level
source provenance. A supplied operational profile is the sole live sizing
source; the live command accepts no per-field sizing flags.

### `LongV12WideStop`

This profile uses the signal, universe, sizing, and entry described above. The
stop starts at **3× the typical daily swing** and tightens to 1.5× once a
position is **48 hours old** (`long_v12_profile()`, `fc_atr_stop_mult` /
`fc_stop_time_decay_hours` / `fc_stop_time_decay_atr_mult`).

Why the tight stop is wrong: ATR-14d is a two-week average and this signal only fires when a
coin moved 2.5σ *today*, so a 1.5× stop sits inside the noise of the very move that triggered
the entry — 67 of 294 trades stopped out. v12 gives the trade room through that move and takes
it back after two flat days.

The registered identity is `long_native_v12_wide_stop`; that string is a
persisted target-book and execution-attribution key. The deployed contract has
no take-profit; results from a runner with different exits do not describe
this rule.

**How v12 publishes.** The wide initial stop is the entry's `stop_loss_fraction` in the
book, which the engine turns into a venue-native stop attached to the position. Each entry
also freezes its own decay contract (`stop_decay_after_ms`, `decayed_stop_loss_pct` =
`fc_stop_time_decay_atr_mult × atr_14d_pct` off the signal-day ATR), frozen per trade at
entry so a later profile change cannot rewrite a standing position's decay.

**The tightening reaches the venue.** Past the decay age the book declares the narrower
fraction, and the engine moves the position's venue-native stop in to match
(`Step::Restop` → `Action::SetStop` → `POST /v5/position/trading-stop`). It only ever
tightens: a declared stop further from the position than the one standing is refused, a
move smaller than a tick is read as the venue's own rounding and ignored, and a position
the venue holds no stop on is left to boot's repair rather than given one from a book. The
move is journaled (`WalRecord::StopSet`) before the call, so a crash leaves the log
claiming the tighter level and boot puts *that* back. Worth **+13 to +19 bp a trade**,
measured across 26 of 30 era-and-window cells
([record](research/research_findings.md)).

The shared reducer publishes an exit when the producer observes the stop breach
on an event-driven wake, subject to the configured debounce and periodic
reconciliation. Whichever of the two acts first ends the trade, and the venue's
own stop is the one that survives the producer dying.
Profile selection is
`LONG_STRATEGY_PROFILE` (`v11a`/`v12`) in the unit environment → `--strategy-profile`; the
planner plans exits across **both** registered identities, so components opened under v11a
keep their frozen stop and hold terms and drain normally (3-day max hold) while new entries
publish under `long_native_v12_wide_stop`.

`order_notional_pct_equity` (0.0 in
[`configs/operational.demo.json`](../configs/operational.demo.json)) **sets** each entry's
size as a fraction of equity when positive, replacing the derived slot; 0 keeps the
strategy's own chain (`gross_exposure / max_concurrent_positions × notional_multiplier`).
It is a setter, not a cap — the name says so — and the loader accepts [0, 10].

**Under the current 6.0 operational multiplier, the raw sizing chain runs from
4.5% to 112.5% of equity per entry.** The chain is the 10% base slot × the
6.0 multiplier × a BTC-volatility scale in [0.30, 1.25] × a per-name vol-parity weight in
[0.25, 1.0] × 1.5 on a weekend.

**Nothing caps a single name.** The account has no per-symbol ceiling: one name may hold a
sleeve's whole gross share. What bounds one position is its own venue-native stop; what
bounds the book is the account gross cap and account margin cap.

**On demo the account caps meet nothing either.** `operational.demo.json` declares no
`capital_reference` block, so the envelope does not track equity: the reference stays pinned
at 250,000 and the gross cap at 1,250,000 while the account holds a few thousand. The
producer sizes off *observed* equity, so those caps sit orders of magnitude above anything
it can ask for and never bind. On demo the venue-native stop is the only bound that acts.

On the funded profile the reference tracks the wallet, so the ratios are real there: a full
ten-slot book can exceed the 500% gross cap. The cap is account-wide — there is no
per-sleeve share, so one sleeve can spend the lot. Nothing resizes to fit — the engine
refuses each entry that would breach.
The installed typed profile defines the live number.

**The engine works each standing position toward its ask.** The book's notional is frozen
at entry, but the position is valued at mark: past the engine's 5%/$1 dead band it trims
what ran up and adds to what fell back, and every add re-declares the venue stop from the
position's average entry, so the stop walks down with each add. What the venue actually
holds is written onto the record each cycle (`venue_qty`, `venue_avg_entry_px`), and every
engine move is logged and counted (`engine_resized_symbols_json`).

**A refused ask leaves the book.** The engine heartbeat carries `entry_blockers` — why each
asked-for name is not being opened: kernel refusals (margin, latch, stale quote) and
planner skips (entry floor, venue minimum, no price or instrument rule). An ask the engine
has never confirmed and is refusing leaves the record the same cycle, which frees its slot;
no cooldown starts, because the name never held. A confirmed holding under a refusal is
exit business and stays. Both the drop and the skip are counted
(`engine_blocked_asks`, `skipped_engine_blocked`).

**The book is absolute, but only over the engine's own positions.** Silence about a symbol
means hold none of it — of what this engine opened. Exposure no order of its own ever
opened is left alone entirely: not entered, not exited, whoever put it there. Attribution
prevents an unowned position from being treated as an omitted owned target and closed.
On the funded dedicated UID, any such outside exposure breaches the operating contract
and can latch new entries off; this isolation is incident behavior, not permission for a
second trading authority.

**The producer's record fails closed.** `long-demo-state.json` is the producer's only
memory of what it asked for. A record that exists but cannot be read back (torn JSON, an
unknown version, one unreadable held row) fails the cycle loudly rather than reading as
empty, which would market-close every open position at once and, written back, make a
transient read failure permanent. Writes are fsynced before the rename. A missing file is
the one honest empty: a producer that has never written a record holds nothing.

**Exit.** Each target declares a venue-native ATR-scaled stop that narrows on the decay
clock; the engine attaches it at entry and moves it in when the book's declared distance
narrows. Time stop at 3 days publishes a zero target. **No take-profit**: graded on 5.5
years of hourly triggers it is negative at every multiple tested, so nothing on the live
path carries one ([record](research/research_findings.md)).

**Replay boundary.** `research/backtest/long_native.py` is the hourly
diagnostic. It calls the shared reducer, but its next-hour-open fills cannot
reconstruct the live ticker wake, minute entry path, Rust fill, or target
dead-band resizes. `research/backtest/long_live_physics.py` is the stricter
rebuild: it uses point-in-time hourly signals and one-minute execution windows,
calls the same reducer, carries the stable entry deadline, starts hold/decay at
the modeled fill, applies the current 5%-of-standing/$1/venue-minimum resize
deadband, and charges declared fees, slippage and funding with no take-profit.
The entry and stop comparisons use Bybit mark-price minutes, as the live ticker
and venue stop do. Crossing fills, position accounting, and resize checks use
the contemporaneous traded-price minutes. Funding uses the mark price at the
exact settlement minute.
From the same typed operational-profile read it derives a normalized 5× gross /
1× initial-margin account envelope for entries and risk-increasing resizes,
including immediate contraction and the configured expansion dead band on the
capital reference.
One-minute OHLC does not reveal tick order, queue position, order-book depth, or
historical quantity-step/minimum changes; it also lacks other-sleeve
reservations. The report is therefore a minute execution bound, not exact
live-fill parity. Only an untainted run whose artifacts establish membership,
trade-price, mark-price, and funding coverage supports a historical-universe claim
([`data.md`](data.md)).
The membership verdict covers the dates that can change that run: it starts at
the signal start minus the rule's longest feature window (90 calendar days)
and ends before the source day whose daily bar would be stamped at the
end-exclusive signal boundary. The report keeps whole-root coverage separate;
a gap outside those causal inputs does not taint the scoped result, while any
gap inside them still does.

## CARRY — the v7 pre-settle execution clock

The deployed CARRY sleeve is **v7** (`carry_hold_v7_live_v1`,
`CARRY_STRATEGY_PROFILE=v7` on both carry units). v7 is an execution clock, not
a config: it executes the registered rule `lane2_carry_hold_v7` (the file it
reads, `configs/lane2_carry_hold_v7.json`) byte-identical and moves **when** the
exit test is evaluated (see **Exit**). Selection is `CARRY_STRATEGY_PROFILE`
(`v3`/`v4`/`v6`/`v7`) in the unit environment → `--strategy-profile`, the same
dial shape as LONG's. The stable cycle strategy id is `carry_hold`; persisted
profile-specific components such as `carry_hold_v3` remain eligible to drain.
The forward grade for the v6/v7 rule
continues under one config id. The v7−v5 capital-normalised paired
differential is the registered forward experiment (see **Registered forward
experiment** below).

**Signal.** Long-only crowd-fee collection, replayed daily at 00:00 UTC over 90 days of
Bybit hourly data by calling the registered scorer functions directly, so the deployed book
and the forward scorer cannot drift apart. Universe: top 100 by 24h quote turnover.
Per-name hysteresis:

| Event | Rule |
| --- | --- |
| Enter | last settled funding print < −10 bp |
| Exit (normalize) | print rises above −3 bp |
| Exit (recovery) | trailing daily funding rate recovers > 30 bp over 2 days |
| Block entry, suspend hold to zero weight | trailing 3d return in [−30%, 0%) |
| Block entry | trailing 30d daily vol < 5% |
| Drop to zero weight (v4) | ≤ 10% of the name's last 20 settlements printed deeper than −10 bp — the isolated deep print is the book's one losing cohort |
| Halve size (v5/v7, flow) | trailing 24h turnover grew ≤ +40% vs 72h earlier — a held name whose crowd is not growing is a stale crowd |
| Halve size (v5/v7, whale) | Binance top-trader position long/short ratio fell ≥ 0.26 over 3 days — the informed side de-longing while the crowd still pays |

Null conditioning values fail open. The whale input is the book's one non-Bybit read: the
producer caches Binance end-of-day ratio values per symbol-day (public endpoint, no key,
`binance_whale_daily.parquet` under the producer root) and the registered 48h freshness
clause nulls anything stale, so a dead feed degrades v7 toward v7-minus-whale instead of
blocking a decision.

**Sizing.** `weight = 0.10 × clip((|trailing 24h settled funding| / 120bp-day)^1.5, 0.25, 1.0)
× persistence × flow × whale` — the exponent is v7's one change (v1..v5 ran the straight
ratio); the v4 persistence step is 1.0 above the 10% cut and 0.0 at or below it (a
name with fewer than 20 settlements of history fails open at full size); flow and whale are
the ×0.5 halvings above — gross capped at 1.0, then
`weight × sizing_equity × notional_multiplier`. The current operational profiles
supply 3.0, so a name at full weight takes 30% of sizing equity at 5x entry
leverage. CARRY resolves one typed effective configuration with field-level
provenance before planning; the operational profile is the live sizing source,
and the resolved data root owns the sizing-anchor and early-exit state paths.
Sizing equity is anchored to the decision, not
the live mark: sizing off the live mark makes the day's target a function of the book's own
unrealized P&L, and the book churns itself. A 5%-of-standing / $1 dead-band is the
backstop; entries below $6 notional are skipped.

**Exit — the v7 clock.** Exits and resizes are a diff against the Rust engine's
accepted reservations, published exit-first. Entry intents expire 6h after the
decision bar and are not published inside the last 15 minutes of that window.
That fixed deadline is carried on every target. The absolute book remains
valid through the 30h decision-staleness horizon, so a late restart can renew
holds and reductions without reopening or growing an expired entry.
A declared 35% stop backstops each position at the venue. No time stop.

The exit test is the registered one — a held name is sold when its funding print
reaches −3 bp or above. v7 moves **when** that test is evaluated: instead of at
the next midnight decision, the producer fires it against the venue's
pre-settlement running rate. The venue locks the upcoming crowd-fee rate just
under a minute before it pays, so inside the final minutes the public ticker's
running rate is tomorrow's print, visible early. When a held name's settlement
is at most 15 minutes away and that running rate is at or above −3 bp, the name
sells immediately — before the payment and the farmer exodus instead of one
minute into it. The settled-print path is the fallback, so a failed or missed
read degrades v7 to the ordinary clock.

The producer parses the venue and attributed holding snapshot into a typed
input, calls a pure exit planner, and durably appends every planned event before
transitioning its in-memory exit mask or persisting that private state. The
hash-chained event freezes the
realm, CARRY profile/config identity, fire and settlement clocks, running rate,
and, when available, the mark and exact CARRY-attributed side, quantity, and
average entry. Missing holding or mark fields remain empty. Exodus consumes
that record independently; CARRY does not write Exodus state or target bytes.

**Drop exit.** A held name the upcoming midnight decision zeroes — universe
rank, persistence cut, suspend — sells at the first cycle after the data is
ready post-midnight (~00:02) instead of on the 00:20 clock those names wait
for. The producer freezes the upcoming day's book early (same computation,
same gates, same refusal semantics as the pre-deadline freeze-ahead), masks
the zeroed names out of the served old-day book, and publishes their exit
intents immediately. Entries never move early: they exist only in the upcoming
book and stay behind the 00:20 flip. A resize (weight shrunk, not zeroed) is
not a drop and waits for the flip too. The exodus sleeve does not take these
over — its trigger is the fee-recovery fire above, never a membership drop.

Kill switches: `CARRY_EARLY_EXIT=0` silences both exit clocks;
`CARRY_STRATEGY_PROFILE=v6` keeps the settled-print clock only.

**Mechanism.** Funding is the price of one side of a crowded perp. When it prints deeply
negative, crowded shorts pay longs ~3×/day to keep the position on; this book supplies that
long side. The premium persists because the risk is real — these names are usually falling,
some to zero (LUNA 2022-05 is in the record) — and the unhedgeable version pays in 6/6 eras
while the delta-neutral version was arbitraged out by 2022. Measured attribution 2021-26:
**+7.2 units from funding received against −3.4 from price** — a 2.1:1 carry payment, not a
price anomaly. The book is empty on 28% of days in that record; flat is a state, not a fault.

**Evidence (seen data).** The base-book mechanism, full sample 2021-26 at flat 0.10 per name:
full-sample **t 2.31**, against the program bar of t ≥ 2.5
([`governance.md`](research/governance.md) §2). By era, bp/day: 2021 **+3.8** · 2022 **+3.0** ·
2023 **+26.0** · 2024 **+13.7** · 2025 **+30.3** · 2026 **+32.5** — every year positive, but
2021-22 is thin and the book makes no bear-robustness claim. The registered v7 rule on seen
data (Aug 25 run, panel ending 2026-08-25): mean net **+21.8 bp/day**, Sharpe **1.85**, worst dip
**−18.6%**, MAR **5.62**, **+31.7×** over ~4.9 years. Against the deployed benchmark this base
book does not win on Sharpe — the corrected carry-hold benchmark Sharpe is **1.21 (t 2.31)**,
and the 2.57 / t 4.87 figure for it is a wrong number; return wins, the owner goal was both.
The same construction on Binance funding and prices does not replicate (t 0.4, Sharpe 0.18)
— evidence is single-venue Bybit until shown otherwise. These are Lane-1 numbers that
selected the rule; only the forward record grades it.

**Registered forward experiment.** The v7−v5 capital-normalised paired daily differential on
shared days is the graded claim. Its registration boundary is 2026-08-19. The
graded rows through 2026-08-21 carry `lane2_carry_hold_v6`; the current config
identity is `lane2_carry_hold_v7`. Those ids refer to the same rule surface.
It had **0 scored forward days at promotion**; the ledger accrues to 2026-08-21
(2 forward days). The "+0.63 bp/day, t +2.86" figure is a seen-data
reconstruction on the midnight grid —
positive in 24 of 24 hourly clock phases at a mean of +0.43 bp/day — not forward evidence.
Quote the mean, not the midnight cell. At its own capital the pair is a wash by construction,
so the claim is capital released, not return gained.

**Risk.** Concentrated (~2–3 names when active; v4 holds 22% fewer name-days than v3 and is
flat on 46% of days), long-only crash beta, single-venue Bybit evidence, capacity ~$1M at 1%
participation, and the deep-negative-funding opportunity set inflates if the structural
funding inversion normalises. Sizing changes depth, never duration: at 15% vol the max
underwater spell in the bench window is 204 days (2024-02-26 → 2024-09-17) and the longest
spell is endemic to every book here. A single-name disaster costs up to its 10% cap; the
book will hold names that go to zero, and the claim is only that the funding collected
across the book pays for them. ~90% of the return is name selection, not market timing. The
registered daily frame exits every name 24h before its final panel bar (worth roughly +0.13
Sharpe in research's favour); the live sleeve cannot dodge, so forward comparisons quote the
delayed-entry basis. No take-profit is measured, not assumed: **105 cells across nine
families** on the v4 book, and not one beats the baseline on mean bp/day. Not modelled: any
impact book beyond the measured demo taker fee (observed 5.50 bp/side across 346 live orders,
conservatively scored at 7.78 bp/side), partial fills, borrow, margin cost, venue outage.

## EXODUS — `lane2_exodus_short_v1`

A standalone sleeve and producer in each realm — its own `[[strategy]]` block,
state root, cycle receipts, realm book (`exodus-demo.json` or
`exodus-mainnet.json`), capital attribution, and systemd unit. The producer
reads CARRY's typed durable event tape; it never reconstructs CARRY's trigger.
Registered config: [`lane2_exodus_short_v1.json`](../configs/lane2_exodus_short_v1.json);
producer: [`exodus_producer.py`](../liquidity_migration/strategy/exodus_producer.py);
rules module: [`rules/exodus_short.py`](../liquidity_migration/rules/exodus_short.py).

**Signal.** None of its own. When the CARRY sleeve's pre-settle exit fires — the running
rate says a held name's deep funding print is dying — the name's price keeps falling for
about an hour past the settlement (measured bottom S+60: −104 bp all-era, −127 bp 2025-26
vs S+1, on all 1,112 historical fires). For a complete handoff, the sleeve takes over the
frozen CARRY-attributed quantity as a short; an incomplete handoff opens nothing.

**Entry.** On the independent consumer's next cycle after the fire; the engine
crosses. Book validity ends 20 minutes after the settlement, and the engine
closes entries 15 minutes before expiry, so no fill happens later than S+5. The
venue holds one net position per symbol, so the short cannot open until CARRY's
exit fill lands — the engine leaves foreign-held names alone and retries; the
seconds-scale delay is inside the measured entry tolerance.

**Sizing.** When CARRY's fresh attributed holding is a valid long, its exact venue
quantity is frozen in the event. The audit notional uses that quantity and the
fire-time Bybit mark when it is available; the signed quantity remains
authoritative in the engine, so partial fills and later price movement cannot
resize the handoff. No entry without a complete long holding and mark; an
incomplete fire is skipped for good and recorded in the Exodus cycle row's
`blocked_events` field with reason `no_exact_carry_long`.

**Exit.** A hard clock: 60 minutes after settlement the producer publishes an
explicit zero for the name, which the engine reads as the exit. Time-boxed,
never price-boxed. The declared 0.35 stop is a disaster fence, not an exit:
every strategy-level stop tested
(+30 bp to +1500 bp on 1m wicks, all 1,130 event windows) lost more on clean-event
whipsaw than it saved on the tail — these names wick violently while dying. Covers ride
the producer's 60s idle-floor contract; the cover time also becomes the daemon's next
wake deadline.

**State and activation.** The producer resolves its registered profile and
rule, execution environment, event, target-book and heartbeat paths, account
identity, service invocation, and operational-profile leverage once into a
typed effective config with field-level provenance. Accepted, expired, and
incomplete handoffs become terminal consumed IDs; an incomplete event is
recorded with `no_exact_carry_long`, so it cannot be invented or retried as a
different decision. Health, symbol-state, and compatibility blocks remain
unconsumed for later cycles. New exposure and newly consumed IDs become durable
before publication. At the cover clock it publishes an explicit zero and
removes the open record only after the engine conclusively reports the position
and working entry absent. Unknown engine state retains the record.
The fleet manifest keeps demo Exodus active independently of the CARRY toggle
and activates funded Exodus with the funded realm. `CARRY_EARLY_EXIT=0` stops
new fires while existing Exodus records
continue to their cover clocks. A lost or torn state file is unknown state:
the producer reports the error and leaves the last engine-visible target
untouched instead of silently flattening or inventing a replacement decision.

**Limits.** The edge is a regime trade on the 2025-26 farmer crowd: overlay +6.1 bp/day
pooled, but 2023 +0.2, **2024 −0.8 (a losing year)**, 2025 +7.8, 2026 +18.2. The premature
tail is fat and real: ~8% of fire-days the print was still deep — the short pays it and
sometimes gets squeezed (worst −945 bp, SOMI 2025-10-01); the measured answer is size, not a
stop. Entries and covers are priced at 1m kline opens — no fill model yet; the first demo
weeks exist to measure that gap. The all-name generalization (shorting settlement deaths
carry never held) is measured-but-unrun and NOT part of this config.

## LLM ledger — research only

The hourly `liquidity-migration-llm-ledger.service` records judged public-data
triggers for research. It has no venue credentials, no target-book path, and no
input seam into LONG. Native LONG targets come only from the shared typed LONG
decision contract.

## Shared machinery

[`configs/operational.demo.json`](../configs/operational.demo.json) is the one editable
sizing surface. Caps are a fraction of observed wallet equity
([`envelope.rs`](../engine/engine-risk/src/envelope.rs):
contraction immediate, expansion behind a dead band, unknown equity moves nothing).
Every cap is account-wide: no sleeve holds a private share, so one sleeve may
spend the account's whole envelope.
There is no account daily-loss circuit breaker; entry admission is bounded by
the current equity envelope, gross, margin, freshness, ownership, and native-stop
rules instead.

**The venue stop is exchange-native, one Full-position stop per symbol.** The
installer is the engine ([`gateway.rs`](../engine/engine-venue/src/venues/bybit/gateway.rs)
posts `tpslMode: "Full"` with the stop;
[`reconcile.rs`](../engine/engine-core/src/reconcile.rs) and
[`working.rs`](../engine/engine-core/src/working.rs) keep it true), taking one
`stop_loss_fraction` per symbol from the routed target book. CARRY declares 0.35.
There is no aggregation across components: if two components of one symbol ever declared
different stops, the producer's book, not the engine, would have to resolve them. Today's
producers publish one target per symbol per sleeve, so nothing exercises that.

**Profile load refusals** (all in
[`operational_profile.py`](../liquidity_migration/core/operational_profile.py); cited by
function, not line): unknown or missing fields in any block (`_object`); any producer
`entry_leverage` above `account_risk.max_leverage`; an account gross cap above
`capital_reference_usdt × max_leverage`; an initial-margin cap above
`capital_reference_usdt`; a component cap above the account cap
(`_validate_profile_envelopes`). A profile carrying a `continuous` block is
refused by name. How large a book the sizing multipliers build is the owner's dial and is
not refused at load — per-position risk is bounded by each position's own venue-native
stop. The validator re-runs on the equity-rescaled profile, not only at load. Separately: a
normal risk or venue-rule rejection when live account state differs from the validation
reference is a safety decision, not configuration drift — do not "fix" it by raising caps.

Both operational profiles carry a `hedge` block — an empty seat, so adding a hedge needs
no schema change.

**Universe membership.** Turnover, listing age, and rank are re-evaluated every cycle, so a
symbol can be skipped without disappearing. A newly observed future `deliveryTime` drops it
from new-entry membership. Producer cycles keep publishing exits while the Rust heartbeat
reports exposure; offline retirement checks require the Rust engine to report the symbol flat
(`require_scheduled_retirements_flat` in `account_candidate_universe.py`). The private
retirement registry preserves the delivery observation after the venue removes the instrument
row; a moved delivery date updates the record in place, keeping the original first-observed
timestamp as the causal anchor. A symbol that leaves the live population *without* delivery
evidence does not fail the cycle: it drops to journaled temporary ineligibility and returns
automatically when the venue restores it — a venue hiccup that self-heals must not be
intervened on. Reasons are `turnover_below_floor`, `listing_age_below_floor`,
`listing_age_above_ceiling`, `outside_configured_liquidity_rank`,
`unexplained_absence_from_venue`, and `scheduled_retirement_reentered_eligibility` (a
cancelled or moved delisting, which leaves the symbol non-tradable while its delivery
evidence stands). Malformed eligibility input still raises.

The frozen candidate artifact is a forward population contract: the active set is the
intersection of the frozen per-profile population with the current live population
(the freeze-intersection step in `account_candidate_universe.py`), so a post-freeze listing can never enter until
someone re-freezes, and a frozen symbol failing a dynamic filter is skipped with its exact
reason written to the cycle receipt (`temporarily_ineligible_candidates_json`) — normal
ranking movement, distinct from disappearance.

Schema 5 of that artifact names the tradable population `strategy_instruments`: every
crypto-linear perpetual the venue listed at snapshot time, minus the shared exclusions. Each
sleeve profile is that set with extra gates switched on, so it already sits inside it.
Convert an installed schema-4 artifact with
[`migrate_candidate_universe_schema.py`](../scripts/maintain/migrate_candidate_universe_schema.py),
which rebuilds offline from the raw snapshot, refuses if one symbol would change, and
re-keys the retirement registry to the artifact's new hash.

The negative results relevant to each sleeve are stated in that sleeve's section
above — the no-take-profit finding, the cross-venue non-replication, and the
measured-but-unrun generalizations. Failure taxonomy:
[`backtesting_errors_we_never_repeat.md`](research/backtesting_errors_we_never_repeat.md);
grading rules and the claim boundary are in [`AGENTS.md`](../AGENTS.md); mainnet arming is
[`operations.md`](operations.md) §Real money.
