# Research Summary

Updated 2026-07-16. This is a decision log, not policy or deployment authority.
Apply `docs/governance.md` and inspect the named artifacts before relying on a
claim. Current host state belongs in `STATE.md`.

## Current objects

| Object | Evidence read | Operating boundary |
| --- | --- | --- |
| `continuous_ensemble_v2` | Positive exploratory Bybit/Binance controls, but population-limited and materially exposed to short-fade tail risk | Demo components plus hedge; paper components only; no mainnet authority |
| `LongV11aDivWeekendVol` | Positive internal cross-venue result, but strongly dependent on take-profit winners and not validated by its tiny skewed forward sample | Demo/paper profile only; no size or mainnet authority |

Binance is a research/replay venue, not a live execution venue.

## Continuous v2

The active component runtime has three components (`p3`, `p4p3`, `p4p5`), stable
causal RMOM q25, inverse-vol sizing, a prior-day BTC uptrend gate, TP12, and a
24-hour max hold. It also applies the stateful live
`CTRL_BTC_RISK_70_90_35` size overlay. The separate demo hedge uses BTC+ETH and
the BTC-vol regime; daily rebalance is disabled.

The standard historical curve reconstructs the base components and hedge but
does not apply the accepted-decision BTC-risk state. It also does not establish
manifest-backed historical membership merely by reading a `full_pit` root.
The controls below are therefore limited diagnostics, not complete replay of
the active runtime or decision-grade historical-population evidence.

The retained controls below were produced by the retired receipt-derived TP10
reconstruction. The active code now reconstructs TP12 to match runtime, so
these numbers are not reproducible under the current profile and must not be
cited as current-profile performance.

Historical TP10 exploratory controls:

| Run | Venue | Return | Max drawdown | MAR |
| --- | --- | ---: | ---: | ---: |
| Retired TP10, 2026-06-27 | Bybit | +26.64% | -1.13% | 7.33 |
| Retired TP10, 2026-06-27 | Binance | +18.84% | -1.02% | 5.72 |
| Retired TP10 stable-only, through 2026-07-10 exclusive | Bybit | +24.36% | -1.20% | 6.22 |

These runs are controls, not untouched confirmation, live-size approval, or
proof that the daemon/account lifecycle exactly matches historical execution.
Funding coverage differed by run and must be rechecked for any new net-return
claim.

The 2026-07-10 1000TAGUSDT demo incident produced account-authoritative Closed
PnL of `-$87.69678926`, or 0.873502% of entry equity. The retired demo-only
sniper adds contributed part of the loss and had neither paper nor
decision-grade historical support. The full receipt is
`docs/incidents/2026-07-10-1000tag.md`.

Durable mechanism reads:

| Mechanism | Current conclusion |
| --- | --- |
| 20%/40%/80% fixed stops | Contradicted as improvements: each reduced MAR on both tested venues. This does not prove no executable safety control can help. |
| +1h/+2h entry delay | Rejected by the tested full component+hedge replays. |
| +1% adverse-limit entry | Useful path diagnostic, contradicted by the tested full replay; runtime add-on retired. |
| Daily volatility rebalance | Keep disabled; the tested version mostly saturated leverage and worsened registered risk metrics. |
| BTC gate removal/non-30d retunes | Contradicted on the tested grid; retain the 30-day prior-day control. |
| BTC-risk tail hard skip (separate from live 0.35x sizing) | Mixed venue result and rejected under its registered rule. |
| Conditional scale-in | Higher return but worse MAR/drawdown on both tested overlays; no runtime add-on. |
| Signal-invalidation exits | Sparse or zero-hit evidence; no retained exit. |
| Upper-wick sizing | Retracted after duplicate-counting and implementation-agreement audit. |
| Symbol/time blacklist | No deployable common arm under the tested contract. |

## Long v11a

Latest retained internal refresh through the 2026-06-23 signal day:

| Venue | Trades | Return | Max drawdown | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

The object remained positive after best-month removal, 2x/3x cost stress,
worst-12-month windows, and a matched-symbol null on both venues. Removing the
take-profit bucket changed Bybit/Binance returns to -0.92%/-5.99%, so the result
is TP-tail dependent. A single forward ADA pair had roughly 9.47 hours of entry
skew and 34,091.786 seconds of exit skew; that is execution disagreement, not
validation.

## Execution evidence

Retired demo probes produced no decision-grade execution model. Paper operation
therefore uses a visibly labelled `integration_only_uncalibrated` twin and is
not performance evidence. Demo/account receipts can support claims only about
their exact observed order, reconciliation, fee, funding, and recovery paths.

## Cancelled and retired work

The following prospective studies were cancelled without a treatment run and
therefore produced **no result**: account natural replay, continuous tail
survival/loss-budget, granular adverse-risk, BTC-month regime, and
time/symbol-risk variants. Their runners and verbose contracts were removed.
Do not describe cancellation as empirical rejection.

The strategy-overhaul Phase-0 diagnostic bundle
`strategy-overhaul-phase0-bccefdfc38ae9fda3c17` has receipt SHA-256
`ed5fb3687280db691dcda5e32e00005a8dd48dd2fb403c2f48fe6cb69a81bb03`.
It returned `NOT_READY`, inspected no outcomes, and established only limited
internal reconstruction under its captured environment.

A later current-source structural Phase-0 bundle,
`strategy-overhaul-phase0-8d6314fec8717954be9a`, returned `READY` with
`outcome_run_authorized=false`; its receipt SHA-256 is
`93308a9661a8dc4b2520afccbdda05f67d12ab5b7116655b8514c614bc74a8fa`.
The subsequent Phase-B run
`strategy-overhaul-phase-b-ebcfdad2b9cd2809ccefc7e2` completed only events
00--04 of nine and published no completion receipt, S02 feature tape, S03 entry
artifact, S04 labels, or outcome. The workflow and code were then retired. Both
Phase-0 receipts and the partial Phase-B directory are tombstones; they support
no population, alpha, promotion, deployment, or real-money conclusion. Durable
diagnostics and reusable constraints are recorded in
`docs/strategy_overhaul_lessons.md`.

No confirmatory research contract is active. New decision-influencing work must
register a new claim, exposure boundary, tested set, rule, and artifact plan
before outcomes are inspected.
