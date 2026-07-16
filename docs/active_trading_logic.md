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
- execution scale: `deploy/systemd/` and the bound environment;
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

| Component | Event gate | Take profit | Weight |
| --- | --- | ---: | ---: |
| `p3` | `turn3_pop3` | 12% | 1/3 |
| `p4p3` | `turn4_pop3` | 12% | 2/9 |
| `p4p5` | `turn4_pop5` | 12% | 4/9 |

The producer admits at most 25 active component reservations and five new
components per cycle. It pauses new entries when the verified account journal
contains at least eight adverse reduction batches in the preceding 1,440
minutes; the pause never changes existing targets. That threshold is an
operational guardrail, not a validated optimum.

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

The current demo and paper component units set `NOTIONAL_MULTIPLIER=10` and
`ENTRY_LEVERAGE=10`. The multiplier changes target quantity; leverage changes
margin. Results from that scale are not evidence for a 1x performance claim.

### Exit and hedge

After an attributable fill, the account owner derives the 12% take-profit from
fill VWAP. The producer publishes a zero component target after 24 hours from
the first attributable fill. There is no component strategy stop. The demo
owner separately requires exchange-native disaster protection for net venue
exposure; that is an account safety control, not part of the alpha logic.

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

Current demo/paper units use notional multiplier 1, entry leverage 10, no
separate per-order notional cap, at most five new entries per cycle, and a 50%
projected initial-margin ceiling. Effective bound environment values override
documentation.

Each target carries a 1.5 ATR stop, 4.0 ATR take-profit, and three-day maximum
hold. The account owner derives executable prices and the hold clock from the
first attributable fill and owns the aggregate venue order.

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
