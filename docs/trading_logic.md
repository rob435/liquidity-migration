# Trading logic

What each sleeve trades, how it sizes, how it exits, and where its evidence stops. Code is
the authority: [`long_native_event_demo.py`](../liquidity_migration/strategy/long_native_event_demo.py)
and [`rules/long_native.py`](../liquidity_migration/rules/long_native.py),
[`carry_demo.py`](../liquidity_migration/strategy/carry_demo.py) and
[`rules/carry_hold.py`](../liquidity_migration/rules/carry_hold.py) (scored by
[`financed_longs.py`](../liquidity_migration/research/backtest/financed_longs.py)),
[`rules/exodus_short.py`](../liquidity_migration/rules/exodus_short.py) for the exodus
short (wired inside `carry_demo.py`).

## On today

Publication switches live in [`deploy/sleeves.env`](../deploy/sleeves.env).

| Sleeve | Trades | Demo | Mainnet |
| --- | --- | --- | --- |
| LONG | Long a fresh volume pump, bought on a shallow retrace | on | off |
| CARRY | Long coins whose shorts pay a deep crowd fee | on | off |
| EXODUS | Short the name carry just abandoned, through the post-settlement fall | on | off |

EXODUS has no toggle in `sleeves.env`: it is published by the carry producer (its trigger
is carry's own pre-settle exit fire), armed per unit by `EXODUS_SHORT_PROFILE` in the unit
environment, and holds its own engine sleeve, book file, and fill attribution.

Producers publish absolute component targets; they never place orders and never own fills,
funding, or P&L ([`architecture.md`](architecture.md)). Demo is the only practice book; the
mainnet route is wired but off.

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
| Signal freshness | ≤ 24h |

Entry fires when price touches `signal_close × 0.99` (`sniper_retrace`), or falls through
at the 6-hour deadline while the signal is still fresh (`sniper_deadline_fallthru`). Ten
concurrent positions, 7-day per-symbol cooldown.

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
multiplier runs is the owner's dial in the operational profile.

### `LongV12WideStop` — registered 2026-08-01, deployed 2026-08-03

Same signal, same universe, same sizing, same entry. One thing changes against v11a: the
stop starts at **3× the typical daily swing instead of 1.5×**, and tightens back to 1.5×
once a position is **48 hours old** (`long_v12_profile()`, `fc_atr_stop_mult` /
`fc_stop_time_decay_hours` / `fc_stop_time_decay_atr_mult`).

Why the tight stop is wrong: ATR-14d is a two-week average and this signal only fires when a
coin moved 2.5σ *today*, so a 1.5× stop sits inside the noise of the very move that triggered
the entry — 67 of 294 trades stopped out. v12 gives the trade room through that move and takes
it back after two flat days.

Every other v11a rule was ablated on the real engine and kept — volume rank, the BTC-and-ETH
regime gate, the 2.5σ trigger family, the 7-day cooldown, the 3-day hold, the 4×ATR target,
the 1%/6h retrace, the top-50 universe all lose Sharpe when loosened. Measured over
2021-04 → 2026-07 against v11a: total **38.5% → 51.6%**, daily Sharpe **1.24 → 1.49**,
worst dip **−4.4% → −3.9%**, stop-outs 67 → 50, paired daily difference **+0.48 bp/day
(t 3.27, n 1927)**, better or equal in all six calendar years, and less concentrated
(best 20 trades carry 62% of P&L against 78%). Render it with
`bash scripts/research/equity_curves.sh --sleeves long --long-profile v12`.

Lane-1 evidence: simulated on the data that also chose the rule. The forward record starts
at the registering commit. Its identity `long_native_v12_wide_stop` is separate from v11a's
because that string is a persisted account-journal key.

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
([receipts](research/archive/2026-08-21-llm-gate-window-lab.md)).

`_plan_time_stop_exits` still publishes a zero target (journal reason `decayed_stop_loss`)
when the producer sees the breach first on its 60s cycle — whichever of the two acts first
ends the trade, and the venue's own stop is the one that survives the producer dying.
Profile selection is
`LONG_STRATEGY_PROFILE` (`v11a`/`v12`) in the unit environment → `--strategy-profile`; the
planner plans exits across **both** registered identities, so components opened under v11a
keep their published stop/TP/hold terms and drain normally (3-day max hold) while new entries
publish under `long_native_v12_wide_stop`.

`order_notional_pct_equity` (0.0 in
[`configs/operational.demo.json`](../configs/operational.demo.json)) **sets** each entry's
size as a fraction of equity when positive, replacing the derived slot; 0 keeps the
strategy's own chain (`gross_exposure / max_concurrent_positions × notional_multiplier`).
It is a setter, not a cap — the name says so — and the loader accepts [0, 10].

**One entry runs from 2.25% to 56.25% of equity.** The chain is the 10% base slot × the
3.0 multiplier × a BTC-volatility scale in [0.30, 1.25] × a per-name vol-parity weight in
[0.25, 1.0] × 1.5 on a weekend.

**Nothing caps a single name.** The account has no per-symbol ceiling: one name may hold a
sleeve's whole gross share. What bounds one position is its own venue-native stop; what
bounds the book is the account gross cap, the account margin cap, and — on the funded
profile only — each sleeve's share.

**On demo the account caps meet nothing either.** `operational.demo.json` declares no
`capital_reference` block, so the envelope does not track equity: the reference stays pinned
at 250,000 and the gross cap at 1,250,000 while the account holds a few thousand. The
producer sizes off *observed* equity, so those caps sit orders of magnitude above anything
it can ask for and never bind. On demo the venue-native stop is the only bound that acts.

On the funded profile the reference tracks the wallet, so the ratios are real there: a full
ten-slot book would be 562% gross against a 500% cap, and LONG's own share stops at 300% of
the wallet. Nothing resizes to fit — the engine refuses each entry that would breach.
Runtime profile bytes override any number here.

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
opened is left alone entirely: not entered, not exited, whoever put it there. A position
the owner places by hand is attributed to nobody, so without that it read as a name the
book does not mention, and every pass closed it again.

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
path carries one ([receipts](research/archive/2026-08-21-llm-gate-window-lab.md)).

**Limits.** The forward record is demo-only. The retained internal backtest result depends
materially on take-profit winners — **and the live path takes no take-profit**, so the
graded rule and the running one differ in the term the result leans on. On the gate's
hourly triggers a take-profit is measurably negative at every multiple, which is why the
live path has none; whether the daily rule's dependence survives the same test is not
established. The research runner also does not abort when PIT membership
is incomplete — only an untainted run whose artifacts establish the population supports a
historical-universe claim ([`data.md`](data.md)). The scoped run label carries a
funding-coverage dimension as well as a PIT one, and funding downgrades it independently:
`pit_required_missing_manifest`, `pit_membership_filtered_current_universe`,
`full_pit_universe_funding_missing`, `full_pit_universe_funding_coverage_low`,
`full_pit_universe_funding_partial`, `full_pit_universe` (`research/backtest/long_native.py`,
`_run_label` — not the `rules/long_native.py` the header links), plus a methodology label
`invalid` / `biased_benchmark` / `exploratory` from taint and manifest state
(`_methodology_run_label`, same file). `full_pit_universe_pass=true` beside a
`full_pit_universe_funding_coverage_low` label is not a historical-universe claim.

## CARRY — `lane2_carry_hold_v6`

> **Promoted 2026-08-19 by owner override**; forward grading starts there, and it entered
> with **0 scored days** (promotion note in
> [`strategy_program.md`](research/strategy_program.md)). On top of v4, v6 carries v5's two
> size halvings — stale turnover flow and Binance top-trader de-longing (the whale leg, the
> book's one non-Bybit input) — and bends the depth ladder with a 1.5 exponent, all in the
> shared registered scorer. Selection is `CARRY_STRATEGY_PROFILE` (`v3`/`v4`/`v6`/`v7` — v7
> is the dial on both carry units) in the unit environment → `--strategy-profile`, the same
> dial shape as LONG's; the journal filing id is the version-free `carry_hold` and never
> changes with the profile (components filed under the older `carry_hold_v3` id drain under
> it). v4 and v5 keep scoring daily and the v6−v5 capital-normalised paired differential is
> the registered forward experiment. See [`carry_hold.md`](research/carry_hold.md) §4.

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
| Halve size (v5/v6, flow) | trailing 24h turnover grew ≤ +40% vs 72h earlier — a held name whose crowd is not growing is a stale crowd |
| Halve size (v5/v6, whale) | Binance top-trader position long/short ratio fell ≥ 0.26 over 3 days — the informed side de-longing while the crowd still pays |

Null conditioning values fail open. The whale input is the book's one non-Bybit read: the
producer caches Binance end-of-day ratio values per symbol-day (public endpoint, no key,
`binance_whale_daily.parquet` under the producer root) and the registered 48h freshness
clause nulls anything stale, so a dead feed degrades v6 toward v6-minus-whale instead of
blocking a decision. The book is empty on 28% of days in the full record; flat is a state,
not a fault.

**Sizing.** `weight = 0.10 × clip((|trailing 24h settled funding| / 120bp-day)^1.5, 0.25, 1.0)
× persistence × flow × whale` — the exponent is v6's one change (v1..v5 ran the straight
ratio); the v4 persistence step is 1.0 above the 10% cut and 0.0 at or below it (a
name with fewer than 20 settlements of history fails open at full size); flow and whale are
the ×0.5 halvings above — gross capped at 1.0, then
`weight × sizing_equity × CARRY_NOTIONAL_MULTIPLIER` (3.0 — a name at full weight takes 30%
of the sizing equity at 5x entry leverage; the dial overrides the profile's own value). Sizing equity is anchored to the decision, not
the live mark: sizing off the live mark makes the day's target a function of the book's own
unrealized P&L, and the book churns itself. A 5%-of-standing / $1 dead-band is the
backstop; entries below $10 notional are skipped.

**Exit.** Exits and resizes are a diff against the account owner's accepted reservations,
published exit-first. Entry intents expire 6h after the decision bar and are not published
inside the last 15 minutes of that window. A declared 35% stop backstops each position at
the venue. No time stop.

**Early exit (owner-directed, `CARRY_EARLY_EXIT=1` on both carry units).** A held name whose
LATEST settled print has recovered to −3 bp or above — the registered exit test, applied at
print time instead of the next midnight — is sold at the first cycle after that print sweeps
in (~1–2 min after the settlement), and masked out of the desired book until the next
decision bar so the frozen day cannot re-buy it. If the next midnight print is deep again the
next decision re-enters normally (measured misfire rate ~17% all-day; the research note
charges it). No new threshold, no new data: the fire condition is `lane2_carry_hold_v6`'s own
`exit_above_funding_bp` read from the hourly funding sweep. Setting the env to 0 restores the
registered midnight exit clock. Evidence and the honest caveats (positive-median,
tail-exposed, mean below the t-bar):
[`research_findings.md`](research/research_findings.md) §Settlement-instant timing.

**v7 pre-settlement exit (owner-directed, `CARRY_STRATEGY_PROFILE=v7` on both carry units).**
The venue locks the upcoming crowd-fee rate just under a minute before it pays, so inside the
final minutes the public ticker's running rate is tomorrow's print, visible early. Under the
v7 profile the producer batch-reads that rate for its held names whenever one of their
settlements is at most 15 minutes away, and fires the SAME registered exit test on it —
selling before the payment and the farmer exodus instead of one minute into it. v7 changes
only this execution clock: its membership rule is `lane2_carry_hold_v6` byte-identical (the
config's forward grade continues under one id), and the settled-print path above stays as the
fallback, so a failed or missed read degrades v7 to exactly the v6 clock. Measured: +21.3 bp
per fire all-in over the settled-print sell (median +11.3, t 4.9; 2025/26 ≈ +29 bp per fire,
+2.4–3.1 bp/day book-level), premature fires ~4% of fire-days costing ~2.3 bp/fire-day,
charged in the research row. `CARRY_EARLY_EXIT=0` kills both exit clocks;
`CARRY_STRATEGY_PROFILE=v6` keeps the settled-print clock only.

**Limits.** Concentrated (~2–3 names when active — v4 holds 22% fewer name-days than v3 and
is flat on 46% of days), long-only crash beta, single-venue Bybit evidence, capacity ~$1M at
1% participation. The registered daily frame exits every name 24h before its final panel
bar, worth roughly +0.13 Sharpe. The single-clock level is decision-hour lucky: the same
construction over 12 daily offsets spans Sharpe 0.30–1.52 and midnight is the best cell. The
three v3 filters were chosen in-sample in the review that registered them; the paired forward
differential against v2 grades them. The corrected carry-hold benchmark Sharpe is **1.21
(t 2.31)** — it does **not** beat the CONTINUOUS benchmark, and a 2.57 / t 4.87 figure for it
is a wrong number. Detail: [`carry_hold.md`](research/carry_hold.md),
[`research_findings.md`](research/research_findings.md).

## EXODUS — `lane2_exodus_short_v1`

> **Registered and deployed to demo 2026-08-20.** A standalone sleeve at the engine — its own
> `[[strategy]]` block, book file (`exodus-demo.json`), capital attribution, and kill dial —
> produced from inside the carry process, because its entire trigger is carry's v7
> pre-settle exit fire. Registered config: [`lane2_exodus_short_v1.json`](../configs/lane2_exodus_short_v1.json);
> rules module: [`rules/exodus_short.py`](../liquidity_migration/rules/exodus_short.py).

**Signal.** None of its own. When the carry sleeve's pre-settle exit fires — the running
rate says a held name's deep funding print is dying — the name's price keeps falling for
about an hour past the settlement (measured bottom S+60: −104 bp all-era, −127 bp 2025-26
vs S+1, on all 1,112 historical fires). The sleeve takes over the exact position carry
abandons, as a short.

**Entry.** At the fire, immediately; the engine crosses. Book validity ends 20 minutes
after the settlement, and the engine closes entries 15 minutes before expiry, so no fill
happens later than S+5. The venue holds one net position per symbol, so the short cannot
open until carry's exit fill lands — the engine leaves foreign-held names alone and
retries; the seconds-scale delay is inside the measured entry tolerance.

**Sizing.** The notional carry held at the fire (weight × sizing equity × carry's
multiplier), frozen at fire so covers never need an equity read. No entry without a live
owner-health read, same gate as carry entries; a fire arriving during an outage is
skipped for good and receipted (`exodus_entry_blocked`).

**Exit.** A hard clock: cover 60 minutes after the settlement — the name simply leaves the
book, and the engine reads absence as the exit. Time-boxed, never price-boxed. The
declared 0.35 stop is a disaster fence, not an exit: every strategy-level stop tested
(+30 bp to +1500 bp on 1m wicks, all 1,130 event windows) lost more on clean-event
whipsaw than it saved on the tail — these names wick violently while dying. Covers ride
the producer's 60s idle-floor contract; the cover time also becomes the daemon's next
wake deadline.

**Kill switches.** Unset `EXODUS_SHORT_PROFILE` on the carry unit: no new entries, and the
book drains flat immediately (open records are covered on the next cycle, not at S+60).
`CARRY_EARLY_EXIT=0` silences the fires (so also all new exodus entries) while open
records still cover on their clock. A lost or torn state file reads as flat and covers
every open short — losing state never strands a position.

**Limits.** The edge is a regime trade on the 2025-26 farmer crowd: overlay +6.1 bp/day
pooled, but 2023 +0.2, **2024 −0.8 (a losing year)**, 2025 +7.8, 2026 +18.2. The premature
tail is fat and real: ~8% of fire-days the print was still deep — the short pays it and
sometimes gets squeezed (worst −945 bp, SOMI 2025-10-01); the measured answer is size, not a
stop. Entries and covers are priced at 1m kline opens — no fill model yet; the first demo
weeks exist to measure that gap. The all-name generalization (shorting settlement deaths
carry never held) is measured-but-unrun and NOT part of this config. Evidence:
[`research_findings.md`](research/research_findings.md) §1 (the exodus short row).

## LLM GATE — judged entries inside the LONG sleeve

> **Live on demo since 2026-08-21 by owner decision.** The hourly
> `liquidity-migration-llm-ledger.service` judges fresh 4/12/24h trigger
> events and publishes every **score ≥ 6** judgment to the LONG sleeve's
> candidates file; the LONG producer takes those names as entries through its
> own sizing, exits, and venue-native stops. One strategy, one book
> (`long-demo.json`), one engine sleeve (`long`) — the ledger holds no venue
> credentials and writes nothing but the candidates file and its own ledger.

**Signal.** The hourly trigger scan: a **top-10**-turnover name whose rolling
4/12/24h move clears its vol-scaled bar (the daily 2.5σ trigger × √time) with
range location ≥ 0.70, BTC-and-ETH regime on, ATR-14d ≤ 12%. Each event is
judged by a language model walking the fixed step-rubric over enriched public
facts; **a pump_quality_score ≥ 6 is an entry candidate**, at the trigger-hour
price. Everything below 6 stays ledger-only.

The window set and the rank depth are graded, on 5.5 years of hourly bars
against the sleeve's own exit geometry
([receipts](research/archive/2026-08-21-llm-gate-window-lab.md)): the 1h and 2h
windows each have a significantly negative year and are not run; turnover rank
is the strongest thing measured about these triggers, and depth 10 roughly
doubles the edge per trade against depth 30 in every year. The judged gate
itself has no lane-1 evidence — the model's contribution over the mechanical
trigger is what the forward record is testing.

**Entry path.** The LONG producer reads the candidates file each 60s cycle
(`LONG_ENGINE_LLM_GATE_CANDIDATES_PATH` + `LONG_ENGINE_LLM_GATE_ENABLED=1`
on the demo unit; mainnet sets neither, so the gate is inert there). A fresh
judged event becomes a candidate in exactly the native shape: stop
`fc_atr_stop_mult`×ATR (v12: 3×), decayed stop `1.5×`ATR after 48h, 3-day
hold, and the same vol-parity position weight the
FC path computes — the judgment is the trigger and nothing else. From there
the candidate shares every cut the native candidates face: per-cycle pacing,
free slots, owner-health gate, 7-day per-symbol cooldown, fill-anchored
sizing at the profile's LONG multiplier, and the engine's admission. A
missing, stale, or malformed candidates file reads as "no signal"; a dead
ledger service stops new gate entries within the hour, and no signal is acted
on more than an hour after the bar that made it — three clocks (file age,
declared validity, trigger age) all held to the same hour.

One asymmetry stands: the native FC path selects only from the frozen
candidate population, while the gate builds candidates from the events file
against the live ticker snapshot without that filter — so the gate can enter
a listing that postdates the freeze, which the native path cannot touch until
a re-freeze.

**Kill switches.** `LONG_ENGINE_LLM_GATE_ENABLED=0` on the demo LONG unit:
no gate entries, native entries and all exits unaffected. Stopping
`llm-ledger.timer`: the candidates file ages out and gate entries stop on
their own. Every judgment and publication is journaled in the driver ledger
(`row_type` trigger).

## Shared machinery

[`configs/operational.demo.json`](../configs/operational.demo.json) is the one editable
sizing surface. Caps are a fraction of observed wallet equity
([`envelope.rs`](../engine/engine-risk/src/envelope.rs):
contraction immediate, expansion behind a dead band, unknown equity moves nothing);
the per-sleeve partition is the engine's
([`kernel.rs`](../engine/engine-risk/src/kernel.rs), fed from the profile's
`sleeve_limits` — and neither committed profile declares any, so on both
accounts there is no per-sleeve fence and one sleeve may spend the account's
whole envelope; the Python partition in `account_kernel.py` survives in the
tree but nothing on the order path runs it).
There is no daily loss ceiling: the owner's standing decision is per-position safety, so the
venue-native stop on each position is what bounds a loss.

**The venue stop is exchange-native, one Full-position stop per symbol.** The
installer is the engine ([`gateway.rs`](../engine/engine-venue/src/gateway.rs)
posts `tpslMode: "Full"` with the stop;
[`reconcile.rs`](../engine/engine-core/src/reconcile.rs) and
[`working.rs`](../engine/engine-core/src/working.rs) keep it true), taking one
`stop_loss_fraction` per symbol from the routed target book. CARRY declares 0.35.
There is no aggregation across components: if two components of one symbol ever declared
different stops, the producer's book, not the engine, would have to resolve them. Today's
producers publish one target per symbol per sleeve, so nothing exercises that.

**Profile load refusals** (all in
[`operational_profile.py`](../liquidity_migration/policy/operational_profile.py); cited by
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

`PARTITIONABLE_SLEEVES` in `policy/operational_profile.py` is `("carry", "hedge", "long")`
and both operational profiles carry a `hedge` block — an empty seat, so adding a hedge needs
no schema change. `btc_risk_decision_evidence` is defined in `account/entry_attempts.py`
beside the other metadata keys, so an entry's evidence copies forward onto its close.

**Universe membership.** Turnover, listing age, and rank are re-evaluated every cycle, so a
symbol can be skipped without disappearing. A newly observed future `deliveryTime` drops it
from new-entry membership, and retiring it requires position, component targets, component
desires, working orders, the aggregate target, **and** the unresolved inbox all flat for that
symbol (`require_scheduled_retirements_flat` in `account_candidate_universe.py`);
any remainder raises `scheduled-retirement symbols are not account-flat`. The private
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

Bybit's demo realm rejects orders its own published `minNotionalValue` accepts, so
[`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) measures the
executable minimum with bounded probe orders (≤200 USDT, 100 bps away) and caches it per
symbol; entry dust skips key off that. A component below 4× that minimum is
quantization-distorted, so a day where such components carry >20% of gross exposure measures
plumbing rather than economics
([`research_findings.md`](research/research_findings.md)).

The negative results, and what the evidence does and does not establish, are in
[`research_findings.md`](research/research_findings.md) and the
[archive](research/archive/README.md).

Grading rules and the claim boundary are in [`AGENTS.md`](../AGENTS.md); mainnet arming is
[`operations.md`](operations.md) §Real money.
