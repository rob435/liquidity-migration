# Trading logic

The live decision path is Rust. The public-signal worker publishes normalized
facts; the account-owning engine records those facts in its write-ahead log
(WAL) and runs the native reducer for the affected sleeve. Python uses the same
Rust contracts for research replay. It does not decide live positions.

The deployed strategy order is load-bearing:

| Strategy ID | Rust strategy | Sleeve | Realm |
| ---: | --- | --- | --- |
| 0 | `carry_native` | CARRY | demo and mainnet |
| 1 | `long_native` | LONG | demo and mainnet |
| 2 | `exodus_native` | Exodus | demo and mainnet |
| 3 | `quoter` | `maker_canary` | mainnet, disabled |

An ID owns its fills, positions, orders, covers, checkpoints, and controls in
the WAL. Reordering these blocks is a state migration, not a config edit.

## Common live contract

Every directional sleeve follows the same sequence:

1. The signal worker writes one immutable, sequence-numbered observation.
2. The engine appends the exact observation bytes and crosses a WAL barrier.
3. A typed pure reducer receives that observation, its prior checkpoint, the
   attributed account facts, instrument rules, and the engine clock.
4. The reducer returns its next checkpoint, typed effects, and any durable
   cross-sleeve event.
5. The engine records state and intent before an opening order can reach the
   venue. Account-wide risk and venue rules remain the final authority.

The reducer cannot read a file, call a venue, inspect credentials, or ask for
the time. Its config fingerprint covers the registered rule and public-feature
contract. Restore requires the exact schema, fingerprint, and payload.

The venue position is the fact about quantity. WAL attribution is the fact
about sleeve ownership. A target held in reducer state is never treated as an
account position by itself.

Entry permission is a runtime input. Turning it off blocks entries and growing
resizes while the reducer continues to process signals, exits, settlement
clocks, checkpoints, and flatten requests.

## LONG

Source of truth:

- rule: [`configs/long_native_v12.json`](../configs/long_native_v12.json)
- reducer: [`engine/engine-strategies/src/native_long`](../engine/engine-strategies/src/native_long)

### Signal and admission

The worker builds a 50-name universe from trailing 90-day quote volume, subject
to listing history and the registered exclusions. A candidate must satisfy all
of these conditions:

- BTC and ETH regimes are on;
- its current volume rank is at most 10;
- its move clears both 15% and 2.5 recent standard deviations;
- close location is at least 0.70, or 0.60 for the registered multi-day form;
- 30-day daily volatility is positive and no more than 12%;
- the sleeve has fewer than 10 positions; and
- the symbol is outside its seven-day cooldown.

The entry arms one hour after the signal. It fires on a 1% retrace or, if that
does not happen, at the six-hour deadline. A late or stale entry is refused.

### Size and exits

Base weight is gross exposure divided across the available position slots,
capped at 30% per name. Size is adjusted by BTC volatility targeting, the
name's own volatility, the registered operational multiplier, and a 1.5
weekend multiplier. The live reducer anchors its clocks and stop to the
attributed fill rather than to an assumed fill.

The initial stop distance is three times daily average true range (ATR). After
48 hours it tightens to 1.5 times ATR. A position leaves at its stop or after
three days. There is no take-profit rule.

New entries below $6 notional are skipped. A live resize must be at least $1
and at least 5% of current notional. These are execution thresholds in the
native reducer, not a second Python rule.

## CARRY

Source of truth:

- rule: [`configs/lane2_carry_hold_v7.json`](../configs/lane2_carry_hold_v7.json)
- reducer: [`engine/engine-strategies/src/native_carry`](../engine/engine-strategies/src/native_carry)

### Daily book

The worker supplies the full causal envelope; the reducer ranks the top 100
Bybit names by trailing 24-hour quote turnover. Per-name hysteresis enters
when the last settled funding rate is below -10 basis points and leaves when
it is no longer below -3 basis points. A two-day trailing funding recovery of
more than 30 basis points also exits.

The reducer rejects a name whose three-day return is outside the registered
-30% to 0% toxic band or whose 30-day daily volatility is below 5%.

Each eligible name starts from a 10% cap inside a 100% gross cap. Four
multipliers then shape it:

- depth: `clip((abs(trailing 24h funding) / 120bp)^1.5, 0.25, 1)`;
- persistence: deep-settlement share at or below 10% sets size to zero;
- flow: three-day turnover growth at or below 40% halves size; and
- whale positioning: fresh Binance top-trader long/short change at or below
  -26% halves size.

Missing or stale whale data contributes no multiplier. The feature contract,
including its freshness window, is part of the decision fingerprint.

The installed operational profile supplies account sizing. The native config
applies the registered 3x notional multiplier, 5x entry leverage, 35% disaster
stop, $6 entry floor, and the same $1/5% resize boundary used by LONG.

### Settlement lifecycle

CARRY owns all ordinary exits, settled-funding exits, drop exits, and the
pre-settlement exit:

- in the final 15 minutes before the next settlement, a held name whose live
  rate no longer clears the exit threshold is closed;
- a confirmed settlement observation provides the fallback exit;
- a name absent from a healthy absolute decision is reduced to zero; and
- an unhealthy or incomplete observation cannot turn absence into an exit.

When the pre-settlement condition fires, the reducer emits one typed
`CarryPresettlementFire`. Its stable event ID binds the symbol, quantity,
source rule and profile, fire time, and settlement time. The event is durable
before the CARRY checkpoint records it as fired.

## Exodus

Source of truth:

- rule: [`configs/lane2_exodus_short_v1.json`](../configs/lane2_exodus_short_v1.json)
- reducer: [`engine/engine-strategies/src/native_exodus`](../engine/engine-strategies/src/native_exodus)

Exodus has no independent universe, score, polling clock, or size model. It
consumes only a durable CARRY pre-settlement event with the exact accepted
source rule and profile.

For an accepted event it asks for a short equal to the CARRY-attributed venue
quantity at the fire. The entry crosses immediately. The registered 20-minute
post-settlement validity and the engine's 15-minute entry cutoff make S+5 the
last opening time. A retry keeps the same event identity and cannot create a
second record.

The cover is a hard clock at settlement plus 60 minutes. The 35% stop is a
venue disaster fence, not the strategy exit. Pause blocks a new Exodus short
but never blocks a due cover. An event is retired only after its entry window
is closed and both attributed position and owned entry work are conclusively
absent.

## `maker_canary`

Source of truth:

- rule: [`configs/lane2_toxic_flow_quoter_v1.json`](../configs/lane2_toxic_flow_quoter_v1.json)
- reducer: [`engine/engine-strategies/src/quoter`](../engine/engine-strategies/src/quoter)

The mainnet block remains in the fourth durable strategy slot (strategy ID 3)
and `quote_enabled = false`. Rust renders its economic fields from the
registered JSON. When quoting is disabled, the sleeve cancels recovered opening
orders and drains its attributed inventory. When quoting is enabled, only
symbols removed from its configured universe are drained; configured symbols
are never flattened merely because the process restarted. A refused drain waits
for its bounded retry timer before another attempt.

When enabled by a reviewed config, the reducer combines microprice, weighted
book imbalance, volatility, inventory, queue value, and fast/slow aggressive
flow. Buy aggression protects the ask; sell aggression protects the bid. The
current rule quotes AGIUSDT at $5.25 per side, caps inventory at $6, starts from
a 6.5-basis-point half spread, and requires at least four basis points of edge
after its fee model.

This is an execution-protection experiment. Its two seen-tape research days
are negative after fees and do not establish profitable quoting.

## Risk and collision rules

All sleeves draw from the same account-wide caps. There is no private sleeve
wallet. The engine charges pending and live exposure, verifies quote and
account freshness, rounds to instrument rules, sets venue leverage and stops,
and serializes one-way venue transitions. Once its own closed trades have lost
the profile's share of the capital reference inside 24 hours, it refuses every
sleeve's entries until those trades age out.

Two sleeves cannot own the same symbol at the same time. The current owner may
reduce it; another sleeve waits for flat venue quantity and complete
attribution before opening. Unknown or contradictory ownership blocks new
risk, not a genuine reduction.

## Research contract

Python research calls the persistent Rust `strategy_contract` process through
[`rust_strategy_contract.py`](../liquidity_migration/rules/rust_strategy_contract.py).
Replay compares exact discrete effects, event IDs, checkpoint JSON, and
matching missing-value positions. Continuous calculations use declared
tolerances.

That fence proves decision-code parity for the tested inputs. It does not prove
fills, costs, capacity, or profit. Historical results follow
[`research/governance.md`](research/governance.md); live execution claims use
the engine WAL, authenticated venue state, and attributed trade log.
