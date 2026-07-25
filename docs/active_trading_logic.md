# Active Trading Logic

This is the human guide to the two active demo/paper strategies. Runtime
selection does not imply promotion, execution validity, or mainnet authority.
Implemented behavior belongs to the strategy modules, target producers,
account owner, systemd units, and effective environment.

Primary sources:

- CONTINUOUS: `continuous_demo.apply_continuous_demo_profile`,
  `continuous_events.ContinuousEventConfig`, and
  `continuous_profile.ACTIVE_CONTINUOUS_CONFIG`;
- LONG: `long_native.long_v11a_profile` and the long event producer;
- execution scale and account caps: `configs/operational.demo.json` after
  strict runtime-profile validation;
- fills, lifecycle, and protection: the account journal and account owner.

Both strategy processes publish absolute component targets. They do not place
orders or own fills, positions, fees, funding, or P&L. See
`docs/account_execution.md`.

## CONTINUOUS (`continuous_ensemble_v2`)

### Entry

The profile shorts decile 9 of the hourly composite after:

- a one-hour confirmation delay on fully closed bars;
- the causal prior-day 30-day BTC uptrend gate;
- stable residual momentum in the lowest quartile;
- hourly turnover of at least 500,000 USDT; and
- the component's 240-day listing-age and event gate.

| Component | Event gate | Take profit | Declared stop | Weight |
| --- | --- | ---: | ---: | ---: |
| `p3` | `turn3_pop3` | 12% | 35% | 1/3 |
| `p4p3` | `turn4_pop3` | 12% | 35% | 2/9 |
| `p4p5` | `turn4_pop5` | 12% | 35% | 4/9 |

The producer admits at most 25 active component reservations and five new
components per cycle. It pauses new entries when the verified account journal
contains at least eight adverse reduction batches in the preceding 1,440
minutes; the pause never changes existing targets. That threshold is an
operational guardrail, not a validated optimum.

Each cycle also persists an observer-only component funnel (D9, liquidity,
event, age, and capacity), qualified-but-blocked symbols, the first rejection
reason, an exact entry-feature-state hash, and both full-file and signal-day
RMOM identities. Those fields never grant admission authority or bypass the
BTC, account-health, pause, capacity, or account-risk gates.

### Sizing

Before account risk admission and venue discretization, a component target is:

```text
account equity * 2% * notional multiplier
               * component weight * inverse-vol multiplier
               * BTC-risk multiplier
```

The inverse-vol multiplier is `0.01 / rv_168h`, clamped to `[0.5, 2.0]`;
missing or invalid volatility uses `1.0`.

The live `CTRL_BTC_RISK_70_90_35` overlay starts after 50 prior accepted
decisions. A causal BTC-risk score in `[0.70, 0.90)` multiplies every component
for the same `(symbol, signal_ts)` by `0.35`; otherwise the multiplier is `1.0`.
The state is reconstructed from accepted account targets.

The current shared operational profile sets notional multiplier `1` and entry
leverage `2` for both demo and paper. The multiplier changes target quantity;
leverage changes margin. Producers do not read independent systemd sizing
variables: the same profile bytes also define the account owner's maximum
leverage and absolute exposure caps.

### Exit and hedge

After an attributable fill, the account owner derives the 12% take-profit from
fill VWAP. The producer publishes a zero component target after 24 hours from
the first attributable fill. Since profile revision `active_tp12_sl35_v1`
(2026-07-25, undeployed until the normal rollout) each component also declares
a 35% `stop_loss_pct`, so the venue stop the account places is this wide
backstop rather than its own disaster fallback; the research engine models the
identical stop (`docs/anomaly_research_2026-07-24.md` §20.1). The demo owner's
exchange-native disaster protection remains a separate account safety control
outside the alpha logic.

The separate demo hedge targets BTC and ETH using a causal 90-day rolling beta,
60-observation minimum, 2.0 cap, and 5 bps modeled cost. Its BTC-vol intensity
uses `lam=0.5`, a 30-day volatility window, and a 250-day percentile window.
Daily volatility rebalance is disabled.

There is no paper hedge service. Paper CONTINUOUS covers component decisions and
execution, not full hedged-portfolio parity.

### Reconstruction limits

The standard CONTINUOUS historical curve reconstructs the three components,
inverse-vol sizing, TP12, 24-hour hold, disabled daily rebalance, BTC+ETH hedge,
and BTC-vol regime. It does not reproduce the live accepted-decision BTC-risk
state, account risk admission, venue rules, fills, or reconciliation. It also
does not establish manifest-backed historical membership merely by reading a
root named `full_pit`.

## LONG (`LongV11aDivWeekendVol`)

### Entry

`long_native.long_v11a_profile()` defines the only LONG strategy:

- top 50 by trailing 90-day turnover, with 30 days minimum listing history;
- BTC and ETH above their 30-day moving averages;
- current-day volume rank at most 10;
- one-, three-, or seven-day pump trigger at 2.5 sigma, with a 15% one-day
  fallback threshold when volatility is unavailable;
- close location at least 0.70 for one day or 0.60 for multi-day triggers;
- 14-day ATR no greater than 12% of price; and
- signal age no greater than 24 hours.

The entry waits for a 1% retrace below the signal close and falls through at the
six-hour deadline while the signal remains fresh.

### Sizing and exit

The base slot is 10% of account equity (`gross_exposure=1` over ten maximum
positions) before volatility, BTC-vol, weekend, wallet-fraction, and deployment
scales. The profile uses a 30-day volatility estimate with a 30% annual floor,
a 30% position-weight cap, a 60% BTC-vol target with scale `[0.30, 1.25]`, and a
1.5 weekend multiplier. Exit cooldown is seven days.

The current shared operational profile uses notional multiplier `0.5`, entry
leverage `2`, no separate per-order notional cap, at most five new entries per
cycle, and a 50% projected initial-margin ceiling. At the profile's 10,000 USDT
validation reference, the worst-case registered LONG envelope is 9,375 USDT
gross and 4,687.50 USDT initial margin. Runtime profile bytes override this
documentation.

Each target carries a 1.5 ATR stop, 4.0 ATR take-profit, and three-day maximum
hold. The account owner derives executable prices and the hold clock from the
first attributable fill and owns the aggregate venue order.

## Operational risk and candidate retirement

`configs/operational.demo.json` is the single editable source for LONG,
CONTINUOUS, hedge leverage, and account caps. The current account policy caps
projected component gross at 20,000 USDT, account gross at 20,000 USDT, one-symbol
notional at 5,000 USDT, initial margin at 10,000 USDT, and leverage at `2`.
Authorization refuses unknown fields, producer leverage above the owner cap,
or registered LONG/CONTINUOUS exposure envelopes outside those limits. Normal
risk or venue-rule rejection remains possible when actual account state differs
from the validation reference; that is a safety decision, not configuration
drift.

The frozen candidate artifact retains profile-specific admission populations;
post-freeze listings cannot enter. Live turnover, age, and configured liquidity
rank are re-evaluated each cycle, so a frozen symbol that temporarily fails one
of those dynamic filters is skipped and the exact reason is written to the
cycle receipt. That normal ranking movement is distinct from disappearance.

A symbol with a newly observed future venue `deliveryTime` is recorded
prospectively in a private retirement registry and removed from new-entry
membership; a moved delivery date updates the registry in place. Retirement
itself still requires the canonical position, component desires/targets,
working orders, aggregate target, and unresolved inbox to be flat for that
symbol. The registry preserves the observation after the venue removes the
instrument row. A missing ticker/instrument without prior evidence or a
structural contract change drops the symbol to journaled temporary
ineligibility and the cycle continues — it re-enters automatically if the
venue restores it, and a cancelled delisting leaves the symbol non-tradable
while its delivery evidence stands. Malformed eligibility input and any
remaining exposure on a retiring symbol still fail closed.

### Reconstruction limits

The LONG research runner records manifest/kline agreement, funding coverage,
warnings, taint, and a scoped run label. It does not abort solely because PIT
membership is incomplete. Only an untainted run whose artifacts establish the
required population can support a historical-universe claim. The retained
internal result is materially dependent on take-profit winners, and the small
forward sample is execution evidence only.

## Claim boundary

Any performance claim must state population and PIT treatment, decision and
availability times, order/fill model, costs and funding, capacity, accounting,
effective runtime overrides, and prior data exposure. Neither this document nor
an active runtime profile authorizes mainnet.
