# Trading logic

What each sleeve trades, how it sizes, how it exits, and where its evidence stops. Code is
the authority: [`long_native_event_demo.py`](../liquidity_migration/long_native_event_demo.py)
and [`long_native.py`](../liquidity_migration/long_native.py),
[`carry_demo.py`](../liquidity_migration/carry_demo.py) and
[`financed_longs.py`](../liquidity_migration/financed_longs.py),
[`continuous_demo.py`](../liquidity_migration/continuous_demo.py) and
[`continuous_profile.py`](../liquidity_migration/continuous_profile.py),
[`continuous_hedge_manager.py`](../liquidity_migration/continuous_hedge_manager.py). Plain
English: [`plain_english_guide.md`](plain_english_guide.md).

## On today

Publication switches live in [`deploy/sleeves.env`](../deploy/sleeves.env).

| Sleeve | Trades | Demo | Paper | Mainnet |
| --- | --- | --- | --- | --- |
| LONG | Long a fresh volume pump, bought on a shallow retrace | on | on (own producer, `operational` profile only) | off |
| CARRY | Long coins whose shorts pay a deep crowd fee | on | on (demo targets mirrored) | off |
| CONTINUOUS | Short decile 9 of an hourly pump composite | off | off | — |
| Hedge | Long BTC+ETH against the CONTINUOUS short book | off (follows CONTINUOUS) | none | — |

Producers publish absolute component targets; they never place orders and never own fills,
funding, or P&L ([`architecture.md`](architecture.md)). `PAPER_TARGET_MIRROR=on`
republishes demo CARRY targets onto the paper route, so the two books differ only in
execution; the paper CARRY producer is off because two independent producers raced their
caches into a TLMUSDT position demo never asked for (−70.73 USDT, 2026-07-29).

## LONG — `LongV11aDivWeekendVol`

**Signal.** One profile, `long_v11a_profile()`, on fully closed daily bars:

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

**Exit.** Each target declares a 1.5×ATR14 stop and a 4.0×ATR14 take-profit; the account
owner converts both to venue prices off the first attributable fill and places the stop.
Time stop at 3 days publishes a zero target.

**Limits.** The forward record is demo-only. The retained internal backtest result depends
materially on take-profit winners, and the research runner does not abort when PIT
membership is incomplete — only an untainted run whose artifacts establish the population
supports a historical-universe claim ([`data.md`](data.md)).

## CARRY — `lane2_carry_hold_v3`

**Signal.** Long-only crowd-fee collection, replayed daily at 00:00 UTC over 90 days of
Bybit hourly data by calling the registered scorer functions directly, so the deployed book
and the forward scorer cannot drift apart. Universe: top 100 by 24h quote turnover.
Per-name hysteresis:

| Event | Rule |
| --- | --- |
| Enter | last settled funding print < −10 bp |
| Exit (normalize) | print rises above −3 bp |
| Exit (recovery) | trailing daily funding rate recovers > 30 bp over 2 days |
| Block entry, suspend hold to zero weight | trailing 3d return in [−30%, −5%) |
| Block entry | trailing 30d daily vol < 5% |

Null conditioning values fail open. The book is empty on 28% of days in the full record;
flat is a state, not a fault.

**Sizing.** `weight = 0.10 × clip(|trailing 24h settled funding| / 120bp-day, 0.25, 1.0)`,
gross capped at 1.0, then `weight × sizing_equity × notional_multiplier` (1.0). Sizing
equity is anchored to the decision, not the live mark — sizing off the live mark makes the
day's target a function of the book's own unrealized P&L (2026-07-30: $84.7k traded against
a ~$30k book in thirteen hours, zero strategy exits). A 5%-of-standing / $1 dead-band is
the backstop; entries below $10 notional are skipped.

**Exit.** Exits and resizes are a diff against the account owner's accepted reservations,
published exit-first. Entry intents expire 6h after the decision bar and are not published
inside the last 15 minutes of that window. A declared 35% stop backstops each position at
the venue. No time stop.

**Limits.** Concentrated (~3–4 names when active), long-only crash beta, single-venue Bybit
evidence, capacity ~$1M at 1% participation. The registered daily frame exits every name
24h before its final panel bar, worth roughly +0.13 Sharpe. The single-clock level is
decision-hour lucky: the same construction over 12 daily offsets spans Sharpe 0.30–1.52 and
midnight is the best cell. The three v3 filters were chosen in-sample in the review that
registered them; the paired forward differential against v2 grades them. After the funding
double-count fix, the corrected carry-hold benchmark Sharpe is **1.21 (t 2.31)** — it does
**not** beat the CONTINUOUS benchmark; the superseded 2.57 / t 4.87 figures are wrong.
Detail: [`carry_hold.md`](carry_hold.md),
[`research_findings.md`](research_findings.md).

## CONTINUOUS — `continuous_ensemble_v2` (off)

**Signal.** Shorts decile 9 of the hourly composite through one component
(`p3` / `turn3_pop3`), after: a 1-hour confirmation delay on closed bars, the causal
prior-day 30d BTC uptrend gate, residual momentum in the lowest quartile, ≥500,000 USDT
hourly turnover, a 240-day listing-age floor, and the settled-funding admission (last
settled print at signal-bar close ≥ 0 — only fade pumps whose longs are paying; settled
history only, and no observable print admits and is counted as an unknown admit). Two
research-parity gates: a re-entry cooldown equal to the 24h hold, and the crowd-2 gate that
drops a component's whole fresh stack when more than two signals share one `signal_ts`. New
entries pause while the journal shows ≥8 adverse reduction batches in 1,440 minutes.

**Sizing.** `equity × 2% × notional_multiplier × component weight × inverse-vol × BTC-risk`.
Inverse-vol is `0.01 / rv_168h` clamped to [0.5, 2.0]; missing volatility uses 1.0. The
BTC-risk overlay starts after 50 accepted decisions and multiplies by 0.35 when the causal
score sits in [0.70, 0.90). [`configs/operational.demo.json`](../configs/operational.demo.json)
currently holds the book to `max_active: 1` and one new entry per cycle.

**Exit.** 12% take-profit off fill VWAP, a declared 35% stop placed at the venue, and a
zero target 24 hours after the first attributable fill.

**Limits.** The standard historical curve reproduces the component book, the funding
admission, inverse-vol sizing, TP12, the 24h hold, and the BTC+ETH hedge with its regime.
It does not reproduce the live accepted-decision BTC-risk state, account risk admission,
venue rules, fills, or reconciliation. A data root named `full_pit` establishes nothing
about membership ([`data.md`](data.md)).

## Hedge (off with CONTINUOUS)

Small long BTC and ETH positions sized to the CONTINUOUS short book's causal rolling beta:
90-day window, 60-observation minimum, 2.0 per-leg cap, 5 bps modeled cost, 30%
total-equity sanity cap, BTC-vol intensity `lam=0.5` over a 30-day vol window and 250-day
percentile window. Daily volatility rebalance is disabled. The timer fires every 5 minutes
and is enabled only while CONTINUOUS is.

Betas are rolling OLS over the trailing 90 ledger days of
[`bybit_warmstart.csv`](../deploy/hedge_warmstart/bybit_warmstart.csv) (200 rows, data
through 2026-07-09). The runtime never extends that prior with live returns: the live
account path cannot reconstruct the regression's per-unit book return. Regeneration runs
via [`regenerate_hedge_warmstart.py`](../scripts/regenerate_hedge_warmstart.py) after each
research refresh of the continuous equity pipeline, at least quarterly, from the
code-defined TP12 component ledgers. `ContinuousHedgeRule` supports `shrinkage_weight` /
`prior_beta_1` / `prior_beta_2` (`beta = (1−w)·OLS + w·prior`), previous vintage as prior,
`w = 0.3` intended at the first refresh; the deployed value is `0.0`, so enabling it is a
committed change. Regeneration refuses on input drift (`--max-unit-drift`), row-count
regression, missing date overlap, fewer than 60 observations, and coefficient drift above
`MAX_PRIOR_BETA_DRIFT = 0.25`, with `--force` the only override; insufficient or degenerate
windows produce a zero beta, never the prior alone. No refresh has run yet — the deployed
vintage is still 2026-07-09.

## Shared machinery

[`configs/operational.demo.json`](../configs/operational.demo.json) is the one editable sizing
surface. Caps are a fraction of observed wallet equity
([`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py):
contraction immediate, expansion behind a dead band, unknown equity moves nothing);
[`account_kernel.py`](../liquidity_migration/account_kernel.py) holds each sleeve to its own
partition of it; [`account_loss_guard.py`](../liquidity_migration/account_loss_guard.py) halts
the day at a loss ceiling. Turnover, listing age, and rank are re-evaluated every cycle, so a
symbol can be skipped without disappearing; a newly observed future `deliveryTime` drops it
from new-entry membership, and retiring it requires position, targets, working orders, and
unresolved inbox flat for that symbol. Bybit's demo realm rejects orders its own published
`minNotionalValue` accepts, so
[`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py) measures the executable
minimum with bounded probe orders (≤200 USDT, 100 bps away) and caches it per symbol; entry
dust skips key off that. A component below 4× that minimum is quantization-distorted, so a
day where such components carry >20% of gross exposure measures plumbing rather than
economics ([`research_findings.md`](research_findings.md)).

Grading rules and the claim boundary are in [`AGENTS.md`](../AGENTS.md); mainnet arming is
[`real_money.md`](real_money.md).
