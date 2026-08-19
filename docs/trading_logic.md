# Trading logic

What each sleeve trades, how it sizes, how it exits, and where its evidence stops. Code is
the authority: [`long_native_event_demo.py`](../liquidity_migration/strategy/long_native_event_demo.py)
and [`rules/long_native.py`](../liquidity_migration/rules/long_native.py),
[`carry_demo.py`](../liquidity_migration/strategy/carry_demo.py) and
[`rules/carry_hold.py`](../liquidity_migration/rules/carry_hold.py) (scored by
[`financed_longs.py`](../liquidity_migration/research/backtest/financed_longs.py)).

## On today

Publication switches live in [`deploy/sleeves.env`](../deploy/sleeves.env).

| Sleeve | Trades | Demo | Mainnet |
| --- | --- | --- | --- |
| LONG | Long a fresh volume pump, bought on a shallow retrace | on | off |
| CARRY | Long coins whose shorts pay a deep crowd fee | on | off |

Producers publish absolute component targets; they never place orders and never own fills,
funding, or P&L ([`architecture.md`](architecture.md)). The paper fleet — a credential-free
twin owner fed by a target mirror — was retired 2026-08-03; demo is the only practice book
and the mainnet route is wired but off.

## LONG — `LongV12WideStop`

**Signal.** Two registered profiles share this signal — `long_v11a_profile()` (deployed
until 2026-08-03) and `long_v12_profile()` (deployed since); they differ only in stop
geometry, below. On fully closed daily bars:

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

**Sizing.** Base slot `gross_exposure / max_concurrent_positions` = 10% of equity, times
`notional_multiplier` 0.5, times the BTC-vol scalar `clip(0.60 / btc_rv, 0.30, 1.25)`,
times the vol-parity weight `max(min(0.30/vol_used, 3.0), 0.25)` (30d realized vol, 30%
annual floor, 30% position-weight cap), times 1.5 on weekend entries. Entry leverage 2
changes margin only, never quantity. Five new entries per cycle maximum; the producer
refuses to run if projected full-book initial margin exceeds 50% of equity.

### `LongV12WideStop` — registered 2026-08-01, deployed 2026-08-03

Same signal, same universe, same sizing, same entry. One thing changes: the stop starts at
**3× the typical daily swing instead of 1.5×**, and tightens back to 1.5× once a position
is **48 hours old** (`long_v12_profile()`, `fc_atr_stop_mult` /
`fc_stop_time_decay_hours` / `fc_stop_time_decay_atr_mult`).

Why the old stop is wrong: ATR-14d is a two-week average and this signal only fires when a
coin moved 2.5σ *today*, so the stop sat inside the noise of the very move that triggered the
entry — 67 of 294 trades stopped out. v12 gives the trade room through that move and takes it
back after two flat days.

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

**How v12 publishes (wired 2026-08-03).** The wide initial stop is a bigger `stop_loss_pct`
in the entry target's metadata — the account owner derives the resting venue-native stop from
it after the fill, exactly as for v11a, and never revises it. The 48-hour tightening is
producer-side: each entry freezes its own decay contract in the same metadata
(`stop_decay_after_ms`, `decayed_stop_loss_pct` = `fc_stop_time_decay_atr_mult ×
atr_14d_pct` off the signal-day ATR), and `_plan_time_stop_exits` publishes a zero target
(journal reason `decayed_stop_loss`) once a filled position is past the decay age and the
live price is at or below `entry_fill_price × (1 − decayed_stop_loss_pct)`. The contract is
frozen per trade at entry, so a later profile change cannot rewrite a standing position's
decay. The producer checks on its 60s cycle grid against the backtest's hourly intrabar lows
— finer than the measurement — but the *exit* is a market order after the breach is seen, not
a resting order at the level; the venue-native wide stop stays armed underneath. Profile
selection is `LONG_STRATEGY_PROFILE` (`v11a`/`v12`) in the unit environment →
`--strategy-profile`; the planner plans exits across **both** registered identities, so
components opened under v11a keep their published stop/TP/hold terms and drain normally
(3-day max hold) while new entries publish under `long_native_v12_wide_stop`.

`order_notional_pct_equity` (0.0 in
[`configs/operational.demo.json`](../configs/operational.demo.json)) **sets** each entry's
size as a fraction of equity when positive, replacing the derived slot; 0 keeps the
strategy's own chain (`gross_exposure / max_concurrent_positions × notional_multiplier`).
It is a setter, not a cap — the name says so — and the loader accepts [0, 10]. (Renamed
from `max_order_notional_pct_equity` 2026-08-05: the old name read as a bound while the
value replaced the whole sizing chain.)

At the profile's 250,000 USDT capital reference the registered worst-case envelope is
**234,375.00 USDT gross** and **117,187.50 USDT initial margin**: per-order 9.375% of
equity (= 23,437.50) × 10 concurrent positions ÷ entry leverage 2. The 9.375% is 5% base
slot × 1.25 worst-case BTC-vol scale × 1.5 weekend multiplier. Projected full-book initial
margin is therefore 46.875% against the 50% ceiling — 6.7% of headroom, so barely any
increase to `notional_multiplier` leaves the fleet able to boot. Runtime profile bytes
override any number written here.

**Exit.** Each target declares a 1.5×ATR14 stop and a 4.0×ATR14 take-profit; the account
owner converts both to venue prices off the first attributable fill and places the stop.
Time stop at 3 days publishes a zero target.

**Limits.** The forward record is demo-only. The retained internal backtest result depends
materially on take-profit winners, and the research runner does not abort when PIT membership
is incomplete — only an untainted run whose artifacts establish the population supports a
historical-universe claim ([`data.md`](data.md)). The scoped run label carries a
funding-coverage dimension as well as a PIT one, and funding downgrades it independently:
`pit_required_missing_manifest`, `pit_membership_filtered_current_universe`,
`full_pit_universe_funding_missing`, `full_pit_universe_funding_coverage_low`,
`full_pit_universe_funding_partial`, `full_pit_universe` (`research/backtest/long_native.py`, `_run_label` — not the 403-line `rules/long_native.py` the header links), plus a
methodology label `invalid` / `biased_benchmark` / `exploratory` from taint and manifest state
(`_methodology_run_label`, same file). `full_pit_universe_pass=true` beside a
`full_pit_universe_funding_coverage_low` label is not a historical-universe claim.

## CARRY — `lane2_carry_hold_v6`

> **Promoted 2026-08-19 by owner override** (previously v4 from 2026-08-03, v3 before that;
> change point and promotion note in [`strategy_program.md`](research/strategy_program.md),
> deploy receipt in `CHANGELOG.md`). On top of v4, v6 carries v5's two size halvings — stale
> turnover flow and Binance top-trader de-longing (the whale leg, the book's one non-Bybit
> input) — and bends the depth ladder with a 1.5 exponent, all in the shared registered
> scorer. Selection is `CARRY_STRATEGY_PROFILE` (`v3`/`v4`/`v6`) in the unit environment →
> `--strategy-profile`, the same dial shape as LONG's; the journal filing id is the
> version-free `carry_hold` and never changes with the profile (components born under the
> pre-2026-08-05 `carry_hold_v3` id drain under it). v4 and v5 keep scoring daily and the
> v6−v5 capital-normalised paired differential is the registered forward experiment; at
> promotion the forward record had **0 scored days**, exactly like the v4 promotion. See
> [`carry_hold.md`](research/carry_hold.md) §0.1.

**Signal.** Long-only crowd-fee collection, replayed daily at 00:00 UTC over 90 days of
Bybit hourly data by calling the registered scorer functions directly, so the deployed book
and the forward scorer cannot drift apart. Universe: top 100 by 24h quote turnover.
Per-name hysteresis:

| Event | Rule |
| --- | --- |
| Enter | last settled funding print < −10 bp |
| Exit (normalize) | print rises above −3 bp |
| Exit (recovery) | trailing daily funding rate recovers > 30 bp over 2 days |
| Block entry, suspend hold to zero weight | trailing 3d return in [−30%, 0%) — v4 widened the high edge from −5% |
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
the ×0.5 halvings above — gross capped at
1.0, then `weight × sizing_equity × notional_multiplier` (1.0). Sizing equity is anchored to
the decision, not the live mark — sizing off the live mark makes the day's target a function
of the book's own unrealized P&L (2026-07-30: $84.7k traded against a ~$30k book in thirteen
hours, zero strategy exits). A 5%-of-standing / $1 dead-band is the backstop; entries below
$10 notional are skipped.

**Exit.** Exits and resizes are a diff against the account owner's accepted reservations,
published exit-first. Entry intents expire 6h after the decision bar and are not published
inside the last 15 minutes of that window. A declared 35% stop backstops each position at
the venue. No time stop.

**Early exit (owner-directed 2026-08-19, `CARRY_EARLY_EXIT=1` on both carry units).** A held
name whose LATEST settled print has recovered to −3 bp or above — the registered exit test,
applied at print time instead of the next midnight — is sold at the first cycle after that
print sweeps in (~1–2 min after the settlement), and masked out of the desired book until
the next decision bar so the frozen day cannot re-buy it. If the next midnight print is deep
again the next decision re-enters normally (measured misfire rate ~17% all-day; the
research note charges it). No new threshold, no new data: the fire condition is
`lane2_carry_hold_v6`'s own `exit_above_funding_bp` read from the hourly funding sweep.
Setting the env to 0 restores the registered midnight exit clock. Evidence and the honest
caveats (positive-median, tail-exposed, mean below the t-bar):
[`research_findings.md`](research/research_findings.md) §Settlement-instant timing.

**Limits.** Concentrated (~2–3 names when active — v4 holds 22% fewer name-days than v3 and
is flat on 46% of days), long-only crash beta, single-venue Bybit evidence, capacity ~$1M at
1% participation. The registered daily frame exits every name 24h before its final panel
bar, worth roughly +0.13 Sharpe. The single-clock level is decision-hour lucky: the same
construction over 12 daily offsets spans Sharpe 0.30–1.52 and midnight is the best cell. The
three v3 filters were chosen in-sample in the review that registered them; the paired forward
differential against v2 grades them. After the funding double-count fix, the corrected
carry-hold benchmark Sharpe is **1.21 (t 2.31)** — it does **not** beat the CONTINUOUS
benchmark; the superseded 2.57 / t 4.87 figures are wrong. Detail:
[`carry_hold.md`](research/carry_hold.md),
[`research_findings.md`](research/research_findings.md).

## Retired sleeves

CONTINUOUS (`continuous_ensemble_v2`) was retired 2026-07-29; its systemd units and runtime
launchers left the deploy set on 2026-08-03, after the host's hedge book was verified flat.
Its own code — the producer, its daemon, the cycle-status projection, and the backtest
modules behind it — was deleted from the tree in the 2026-08-14 cleanup, together with the
`continuous-event-demo-cycle` CLI subcommand. Git history holds all of it.

What the sleeve left behind (this list said "three things … and they stay" until
2026-08-19; the same 2026-08-14 day that wrote it also removed the first item
nine hours later, and the schema now *refuses* a profile carrying a
`continuous` block — only the last two survive):

- ~~A token CONTINUOUS envelope in both operational profiles~~ — removed
  2026-08-14 (`4a8f8301`); its share went back to the dial ceiling
  (9.9 → 10.0). No `continuous` key exists in either profile or in
  `policy/operational_profile.py`.
- The tradable population itself, which schema 4 of the frozen candidate universe kept under
  the `continuous` name. It was never that sleeve's population: every gate in it was off, so
  it was simply every crypto-linear perpetual the venue listed at snapshot time, minus the
  shared exclusions. Schema 5 calls it `strategy_instruments` and drops the retired name;
  the symbols do not move, because each sleeve profile is that set with extra gates switched
  on and so already sits inside it. Convert an installed schema-4 artifact with
  [`migrate_candidate_universe_schema.py`](../scripts/maintain/migrate_candidate_universe_schema.py),
  which rebuilds offline from the raw snapshot, refuses if one symbol would change, and
  re-keys the retirement registry to the artifact's new hash.
- `btc_risk_decision_evidence` on journal rows written before the removal, now defined in
  `account/entry_attempts.py` beside the other metadata keys, so an entry's evidence still
  copies forward onto its close.

The hedge that sat against the CONTINUOUS short book — its model code and its committed
warmstart prior — was removed from the tree in the same cleanup. What did stay is the
`hedge` *envelope*: `PARTITIONABLE_SLEEVES` in `policy/operational_profile.py` is still
`("carry", "hedge", "long")` and both operational profiles carry a `hedge` block — an
empty seat, kept so a future hedge does not need a schema change.

The negative results, and what the retired sleeves did and did not establish, are in
[`research_findings.md`](research/research_findings.md) and the
[archive](research/archive/README.md).

## Shared machinery

[`configs/operational.demo.json`](../configs/operational.demo.json) is the one editable
sizing surface. Caps are a fraction of observed wallet equity
([`envelope.rs`](../engine/engine-risk/src/envelope.rs):
contraction immediate, expansion behind a dead band, unknown equity moves nothing);
the per-sleeve partition is the engine's
([`kernel.rs`](../engine/engine-risk/src/kernel.rs), fed from the profile's
`sleeve_limits` — and note the demo profile declares no `sleeve_limits`, so on
the demo account there is no per-sleeve fence; the Python partition in
`account_kernel.py` survives in the tree but nothing on the order path runs it);
[`loss_guard.rs`](../engine/engine-risk/src/loss_guard.rs) halts the day
at a loss ceiling.

**The venue stop is exchange-native, one Full-position stop per symbol.** The
installer is now the engine
([`gateway.rs`](../engine/engine-venue/src/gateway.rs) posts `tpslMode: "Full"`
with the stop; [`reconcile.rs`](../engine/engine-core/src/reconcile.rs) and
[`working.rs`](../engine/engine-core/src/working.rs) keep it true), taking one
`stop_loss_fraction` per symbol from the routed target book. CARRY declares
0.35. Honest difference from the deleted Python installer
(`venue_protection.py`, removed 2026-08-14, described here until 2026-08-19):
the old *outermost-among-components* aggregation did not transfer — the engine
takes the book's single per-symbol stop, so if two components of one symbol
ever declared different stops, the producer's book, not the engine, would have
to resolve them. Today's producers publish one target per symbol per sleeve,
so nothing exercises that difference.

**Profile load refusals** (all in
[`operational_profile.py`](../liquidity_migration/policy/operational_profile.py); cited by
function, not line — the 2026-08-14 CONTINUOUS removal cut ~90 lines and rotted every line
number this paragraph used to carry, one past end-of-file): unknown or missing fields in
any block (`_object`); any producer `entry_leverage` above `account_risk.max_leverage`; an
account gross cap above `capital_reference_usdt × max_leverage`; an initial-margin cap
above `capital_reference_usdt`; a symbol cap above the component cap, or a component cap
above the account cap; a LONG full-book margin projection above its own
`max_projected_initial_margin_pct_equity`; and any registered LONG/CARRY envelope —
per-symbol, combined gross, combined margin — outside the account caps
(`_validate_profile_envelopes`). A profile still carrying a `continuous` block is refused
by name. When `sleeve_limits` is declared (`_parse_sleeve_limits`), each producer envelope
must also fit its own share, and a sleeve with a non-zero envelope but no share is refused.
The validator re-runs on the equity-rescaled profile, not only at load. Separately: a
normal risk or venue-rule rejection when live account state differs from the validation
reference is a safety decision, not configuration drift — do not "fix" it by raising caps.

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

Bybit's demo realm rejects orders its own published `minNotionalValue` accepts, so
[`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py) measures the
executable minimum with bounded probe orders (≤200 USDT, 100 bps away) and caches it per
symbol; entry dust skips key off that. A component below 4× that minimum is
quantization-distorted, so a day where such components carry >20% of gross exposure measures
plumbing rather than economics
([`research_findings.md`](research/research_findings.md)).

Grading rules and the claim boundary are in [`AGENTS.md`](../AGENTS.md); mainnet arming is
[`operations.md`](operations.md) §Real money.
