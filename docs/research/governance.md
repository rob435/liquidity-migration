# Research evidence

## Purpose

Define how research results support decisions without treating a research report as account or execution authority.

## Spec Tables

| Evidence | Required content | Limit |
| --- | --- | --- |
| Causality | Input availability at or before the decision | Future membership, candle closes, and revised data invalidate the result |
| Executability | Fees, funding, spread, capacity and execution assumptions | Gross returns alone do not establish an executable edge |
| Accounting | Reconstructable cash, positions, fees and funding | Unreconciled P&L is diagnostic |
| Provenance | Source/config identities, input paths, shaped versus evaluated data | Reusing selection data is exploratory |
| Reporting | Every searched cell, calendar eras, gross beside net, uncertainty and concentration | A selected winner or pooled total alone is insufficient |
| Research operations | Explicit historical comparisons and recorded live outcomes | No rolling forward ledger, post-commit eligibility job or daily grading requirement |
| Operational decision | Claim, config commit, evidence window, decision and change point | The operator owns deployment and capital decisions |
| Funded permission | Host `REAL_MONEY=true` | Research and commits cannot set this switch |

| Empirical finding check | Existing standard |
| --- | --- |
| Statistical threshold | Two-sided `t >= 2.5`; prospective evidence boundary 2026-07-31 |
| Parameter smoothness | Neighbouring grid values have deltas of the same sign |
| Lag stability | One-day execution lag still beats the placebo |
| Persistence | Two consecutive qualifying timestamps still beat the placebo |
| Direction | Inverting the condition does not beat the placebo |
| Concentration | Top three trades contribute at most 50% of net gain |
| Placebo | At most 5% of matched random draws score as well |

| Evidence note | Content |
| --- | --- |
| Claim | Hypothesis and operational decision |
| Data | Which data shaped the rule and which evaluated it |
| Scope | Venue, population, dates and capital scale |
| Economics | Gross, costs, net, uncertainty and drawdown |
| Identities | Config/source commits and raw artifacts |
| Limits | What the result cannot establish |

Every registered rule holds one row below. Status vocabulary: `exploration` →
`registered` → `canary` → `promoted`, or `retired`.

| Registered rule / config commit | Selection data (venue, dates) | Forward data untouched by selection | Absolute net, with uncertainty | Control and paired improvement (secondary) | Cohorts included | Fee basis | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `lane2_toxic_flow_quoter_v1`, `configs/lane2_toxic_flow_quoter_v1.json` at `f6423d71` | Bybit, 34 names, 2026-08-03 and 2026-08-04; 57,364 paired quote opportunities. Every grid arm was shaped and graded on both dates | None graded. 10 `maker_canary` AGIUSDT fills after the commit stop short of the registered 30-fill / 60-minute boundary and are a diagnostic: +8.81 bp all-in arrival cost, −14.52 bp signed one-minute markout. The forward recorder holds unseen L50, trades, mark/index, funding, open interest and liquidations for later shaping | −0.171 bp per quote opportunity with an available mark, at a 2.60% fill rate. No interval or t is reported for the absolute level; the per-date +0.058 (t 6.09) and +0.089 (t 10.12) are improvements, not levels | Fee-corrected no-flow control −0.248 bp/quote; paired +0.076, t 11.75. Every full-fee arm is negative. The `current` row keeps the installed 2 bp fee setting and is not a like-for-like control | Fill and no-fill share one denominator (bp per markable quote; 15 s mark coverage 99.67%). Pull-the-attacked-side variants are grid arms, not a cohort. Inventory unwind is absent: independent quote opportunities omit the inventory path | Modelled 4 bp maker round trip (2 bp per side). The account's observed maker rate is 3.6 bp per side (4 bp on CAP and HEMI), a 7.2–8 bp round trip, and is not applied | `registered`; quoting disabled (`quote_enabled=false`) and the canary stopped. No graded forward economics exist |

| Execution comparison requirement | Contract | Current LONG standing |
| --- | --- | --- |
| Cohort | Fix the eligible-opportunity set from order intentions before any policy is scored; the same orders feed every arm | Selection used five usable LONG openings; the installed study at `441811eb` reads 24 aligned orders / 56 identified fills, thirteen complete comparisons, six usable LONG openings |
| Benchmark | A price known at decision time: the arrival midpoint from the first valid book within 2 s of the decision, with the decision clock bridged to WAL wall time | Implemented in [execution.md](../execution.md); unresolved clocks and stale or gapped books leave the arm unscored |
| Unfilled and expired | Preserved, never dropped, and valued at the common horizon (decision +180 s) as missed cost on the requested quantity | `passive_skip120s` values unfilled quantity at the same horizon; unfinished execution at the horizon is unscored rather than assumed filled |
| Evaluation | Intention-to-trade: price + fee + missed cost divided by requested notional, not filled notional | `saving_vs_cross_bp` is computed per order, requested-notional weighted, with openings/reductions, sleeves and UTC days kept separate |
| Fees | Realised per fill where the fee is known; modelled otherwise and labelled as a scenario | Authenticated per-symbol rates, 10/3.6 bp taker/maker on most selected symbols and 11/4 bp on CAP and HEMI; the latest 48 h actual sample is 10.3433 bp fees + 4.0142 bp slippage = 14.3575 bp per executed side at 100% cost coverage ([STATE](../../STATE.md) owns that reading) |
| Promotion | Measured forward fills in the realm being changed, compared against the crossing calibration error | Not met: all eight queue/latency cells save 6.063–7.309 bp against simulated crossing on the orders that shaped the choice, and the ARB crossing error is −21.615 bp at 5 ms. Realized savings are not established |

| Cross-venue signal element | Recorded today | Required before a cross-venue promotion |
| --- | --- | --- |
| Signal source venue | `sources.public_venue` in `configs/signal-worker.<realm>.json`: `bybit` for demo and mainnet, `mexc`, `hyperliquid`. Folded into the worker's source-contract hash and checkpoint key, so switching venue cold-starts the realm's features | The source venue on the decision record itself. `WireEvent` kinds stay Bybit-shaped for every venue, so the engine WAL does not name the venue that produced an observation |
| Rule provenance | `long_native_v12` is graded on Bybit PIT data only; on `mexc` and `hyperliquid` that rule reads the execution venue's own features. Two inputs stay cross-venue by construction on every realm: the Binance top-trader ratio (`whale_source`) and the shared LLM gate file | An own-venue forward receipt per source/execution pair. A Bybit grade establishes nothing for a MEXC or Hyperliquid pair |
| Execution venue | The realm: its `engine.toml`, root-owned realm env and unit set | Unchanged; the source and execution venue must be readable as a pair from one record |
| Funding interval and multiplier | Delivered per instrument as `funding_interval_min` and per settlement row: MEXC `collectCycle` 8 h, 4 h, 1 h or 24 h; Hyperliquid hourly; Bybit eight-hourly | Normalisation to the graded rule's settlement before any funding threshold reads the rate. `funding_interval_min` reaches `native_carry::plan` and no scorer reads it, so an unnormalised rate would enter `enter_bp = 10.0` directly |
| Basis and cost observation | Not recorded. The engine writes no signal-venue/execution-venue basis at decision or at fill; `basis` in the engine means cost basis | The basis at decision and at fill, with that venue's executed fees and slippage, on the same record. This is a later engine change and does not exist today |
| Disabled sleeves | CARRY and EXODUS render `entries_enabled: false` in `deploy/engine.mexc.toml.template` and `deploy/engine.hyperliquid.toml.template`; both still manage and exit what they own. LONG renders `entries_enabled: true` on both | Each pair carries its own forward economic receipt before entries open, and a mismatched interval or multiplier normalises first |

## Invariants

- Must distinguish a seen-data comparison from an independent test.
- Must retain negative and inconclusive results and the raw research data.
- Must use causal inputs and state every execution approximation.
- Must give every registered rule a registry row, with selection data and
  forward data untouched by selection named separately.
- Must report each version's absolute net with its uncertainty; a paired
  improvement over a control is secondary.
- Must report the fill, no-fill and cancel cohorts, the inventory unwind and
  the fee basis of one rule in one economic evaluation.
- Must compare execution policies on an eligible-opportunity cohort fixed
  before scoring, against a benchmark price known at decision time, valuing
  unfilled and expired quantity at the common horizon.
- Must state whether each fee is realised from the fill or modelled.
- Must record the signal source venue, the execution venue and the basis and
  cost observations behind a cross-venue decision.
- Must never promote a rule whose every arm is negative after fees.
- Must never promote an execution policy on its filled subset's slippage.
- Must never enter a funding interval or contract multiplier from one venue
  into a rule graded on another without normalising it.
- Must never carry a source/execution venue pair on another pair's receipt.
- Must never infer account balances, realized P&L or deployment from a backtest.
- Must never arm funded trading from a research command.

## Operational Recipes

```sh
scripts/ops.sh research-refresh --help
scripts/ops.sh equity --help
scripts/ops.sh status
```
