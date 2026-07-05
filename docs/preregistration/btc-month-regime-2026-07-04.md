# Pre-Registration: BTC Month-Regime Lookback Robustness

Date: 2026-07-04
Stage: proposed
Run label until proven otherwise: exploratory

## Current Read

The existing continuous BTC trend gate is not intraday rolling. It uses the
prior completed daily BTC return sum over `btc_trend_lookback_days=30`, keyed
by the signal day and excluding the current day. That design is causal, but the
30-day month convention is arbitrary.

Prior full component+BTC-risk+hedge replays rejected gate-off and non-30d
simple daily lookbacks. This preregistration does not erase that result. It
tests a different mechanism: whether a month-equivalent duration and latest
confirmed BTC hourly close improve timing without using live ticks or future
same-day information.

## Hypotheses

### H1: Hourly Confirmed Month Improves Continuous Admission

The continuous short book may miss or block entries when the daily prior-30d
gate is stale during a fast BTC regime turn. A faster causal gate should use the
latest confirmed BTC hourly close available before the existing order-submit
time, not unconfirmed ticker data.

Registered continuous arms:

- `daily_prior`: current control, prior daily returns excluding signal day.
- `hourly_30d`: latest confirmed hourly BTC return over exactly 30 calendar
  days.
- `hourly_exact_month`: latest confirmed hourly BTC return over
  `365.25 / 12 = 30.4375` days.
- `smart_month`: low-capacity consensus score using `hourly_exact_month` and
  `daily_prior`, with a 1% disagreement tolerance.

Expected good behavior:

- MAR and max drawdown improve on both venues.
- Trade count does not collapse or simply mimic gate-off.
- Candidate-tape skip reasons show the arm admits/blocks different trades for
  the intended timing reason.

Falsifiers:

- The lead appears only on one venue.
- The best arm is isolated rather than part of a stable 30d/month family.
- The arm improves return by adding noisy entries while worsening MAR or ES99.
- Any value uses an unconfirmed BTC bar, current open bar, or future daily
  close.

### H2: Month-Regime Gate Is Useful For Long Without Pretending Same Timing

The long-native sleeve already uses BTC regime context, but its classifiers are
daily by default. A comparable BTC month regime must be joined at the daily
signal boundary from data known at that boundary. Hourly exact-month context is
valid only as the latest confirmed hourly BTC close that resolves at the daily
close; it is not a tick-level long trigger.

Registered long arms:

- `btc_month_regime_gate=off`: current control.
- `btc_month_regime_gate=uptrend, mode=daily_30d`.
- `btc_month_regime_gate=uptrend, mode=hourly_exact_month`.
- `btc_month_regime_gate=uptrend, mode=smart_month`.

Expected good behavior:

- Long v11a or the selected long research profile improves MAR without losing
  its sparse high-quality trade set.
- Improvements survive both Bybit and Binance.
- The gate does not merely turn off difficult periods by over-filtering.

Falsifiers:

- Long trade count becomes too sparse to evaluate.
- The gate blocks the known positive FOMO/sniper cluster without replacing its
  risk-adjusted contribution.
- A shortened read window truncates BTC history for active month-regime gates.

## Data And Decision Rule

- Venues: Bybit and Binance.
- Roots:
  - `/Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit`
  - `/Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit`
- Window: full available PIT history through the latest fully closed signal
  day at dispatch.
- Run label: `exploratory` unless a later forward/OOS gate says otherwise.
- Continuous control: current TP12/24h continuous target with BTC-risk sizing
  and BTC/ETH hedge.
- Long control: current promoted-in-code long profile, with default
  `btc_month_regime_gate=off`.

Decision rule:

- No live or deploy-default change from this preregistration alone.
- A candidate must beat control on both venues on MAR and max drawdown without
  unacceptable trade-count collapse.
- If continuous and long disagree, do not force a shared setting. The sleeves
  have different timing semantics.
- Candidate tapes and feature rows must expose the regime mode, lookback
  duration, source window, and data-available timestamp.

## Implementation Contract

- Existing defaults reproduce current behavior.
- Continuous hourly modes key by signal hour and use a BTC source close at or
  before the exact cutoff, with a one-hour archive-gap guard.
- Continuous daily mode keeps the prior-day exclusion.
- Long feature rows expose `btc_month_ret_30d`, `btc_month_ret_exact`, and
  `btc_month_ret_smart`; active gates fail closed on missing values.
- Any active shortened-read long run must reserve enough BTC warmup for the
  selected gate.

## Artifacts

Planned output root:

```text
research/btc_month_regime_2026-07-04/
```

Required outputs:

- Per-venue JSON/markdown reports.
- Continuous candidate tapes with BTC mode metadata.
- Long feature/gate diagnostics for admitted and blocked entries.
- A final verdict table comparing control, hourly 30d, hourly exact month, and
  smart month by venue and sleeve.

## Interim Result: Continuous Hourly 30d Rejected

Completed continuous cells so far:

- `control_daily_prior`: Bybit +24.63% / MAR 6.33 / max DD -1.20%;
  Binance +18.82% / MAR 5.68 / max DD -1.02%.
- `hourly_30d`: Bybit +20.07% / MAR 1.75 / max DD -3.54%;
  Binance +16.22% / MAR 1.50 / max DD -3.31%.

The ledger diff is under:

```text
research/btc_month_regime_2026-07-04/continuous/hourly_30d_vs_daily_prior_ledger_diff/
```

Mechanism: trade counts barely moved, but the hourly 30d gate admitted a
fragile `2025-04-19` cluster at full size when the rolling BTC return flipped
slightly positive intraday. Daily-prior still blocked those entries under the
BTC trend gate, then admitted later April 20 trades only after the BTC-risk
overlay had selected the tail bucket and downsized to 0.35x. The hourly-only
cluster included VOXEL, NKN, ALPACA, MEME, XAI, and HIGH; it drove the worst
hourly-minus-daily equity day on both venues.

This rejects naive faster hourly 30d gating. Any further hourly/month gate must
add hysteresis, confirmation, or a post-flip cooldown; plain hourly 30d is not
a candidate.
