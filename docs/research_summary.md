# Research Summary

Updated: 2026-07-10.

This is the durable decision log. Live operational state is in `STATE.md`;
dated experiment contracts are indexed in `docs/preregistration/INDEX.md`.

## Evidence model

- Forward demo/paper decides execution behavior.
- Two-venue, full-PIT research can reject mechanisms and nominate a new shadow
  test; it does not silently change the deployed object.
- PIT membership, causal availability, survivorship control, costs/funding,
  reconstructable ledgers, frozen configs, and immutable receipts are part of
  the result—not optional reporting.
- Mainnet is outside the current operating mode.

## Active objects

| Object | Role | Current read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Bybit continuous fade demo/paper | Base stays on; sniper retired after first material forward loss; clean post-fix clock pending |
| `LongV11aDivWeekendVol` | Bybit long demo/paper | Strong internal cross-venue object; TP-tail dependent; tiny forward sample has execution skew |

Binance is a research/replay venue, not a live execution venue.

## Continuous v2

### Frozen target

- Clock: `2026-06-18T19:54:00Z`.
- Components: p3 `1/3`, p4p3 `2/9`, p4p5 `4/9`.
- Entry/sizing: stable causal rmom q25, inverse vol (`target=0.01`, clamp `2`),
  prior-day BTC uptrend, `CTRL_BTC_RISK_70_90_35`.
- Capacity: 25 active shorts, 5 new entries per cycle.
- Portfolio: BTC+ETH hedge and BTC-vol regime; daily rebalance off.
- Exit: component TP12 plus 24-hour max hold.
- Off: sniper, fixed/server stop, left-decile, stop-approach, failed-fade,
  breakeven, re-entry cooldown, heat and account-drawdown overlays.

The profile hash remains
`c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`.

### 2026-07-10 forward incident

Six 1000TAGUSDT short legs—three base and three demo-only sniper adds—closed
for account-authoritative Bybit Closed-PnL of `-$87.69678926` (0.873502% of
`$10,039.6785` entry equity).

| Layer | Base | Sniper | Total |
| --- | ---: | ---: | ---: |
| Price PnL | -$72.44380000 | -$15.10110000 | -$87.54490000 |
| Fees | -$0.29927414 | -$0.05532786 | -$0.35460200 |
| Before funding | -$72.74307414 | -$15.15642786 | -$87.89950200 |
| Six funding credits | — | — | +$0.20271274 |

The base/sniper split is execution-attributed; funding is account/symbol-level.
The old local sniper price-PnL is not authoritative because shared exit
attribution was wrong. See `docs/incidents/2026-07-10-1000tag.md`.

Decision: retire sniper and keep legacy cleanup active. Do not infer that a
fixed stop is now positive: 20%/40%/80% fixed-stop portfolio replays reduced
MAR on both venues. The justified research response is ex-ante loss budgeting
plus a separately executable, granular adverse-state study.

### Main baseline

The 2026-06-27 frozen TP12 + BTC-risk sizing + BTC/ETH hedge replay produced:

| Venue | Return | MAR | Max DD |
| --- | ---: | ---: | ---: |
| Bybit | +26.64% | 7.33 | -1.13% |
| Binance | +18.84% | 5.72 | -1.02% |

Label: `exploratory`. It is a stable research control, not live-size approval.

The refreshed 2026-07-03 no-TP comparison kept raw survival but reduced risk
quality:

| Venue | TP12 return / MAR / DD | No-TP return / MAR / DD |
| --- | --- | --- |
| Bybit | +24.63% / 6.33 / -1.20% | +25.55% / 5.78 / -1.36% |
| Binance | +18.82% / 5.68 / -1.02% | +18.46% / 4.61 / -1.23% |

Keep TP12. Binance funding was partial, so this is survival/mechanism evidence.

### Decisions retained from closed arcs

| Mechanism | Durable read |
| --- | --- |
| 20% / 40% / 80% fixed stops | Rejected: all three trailed no-stop MAR on both venues. |
| +1h/+2h entry delay | Rejected by full component+hedge replay on both venues. |
| +1% adverse-limit entry | Promising path diagnostic, rejected by full replay. |
| Daily volatility rebalance | Keep off; it mostly saturated leverage and worsened the registered risk metrics. |
| BTC gate off / non-30d retunes | Rejected; the 30d prior-day control remains the comparison object. |
| BTC-risk 35% tail hard skip | Rejected by the two-venue rule: Binance improved, Bybit MAR/DD worsened. |
| Conditional scale-in | Raised return but worsened MAR/DD on both full overlays; no live add-on. |
| Signal-invalidation exits | Negative or zero-hit on sparse state; no deployed exit. |
| Upper-wick sizing | Retracted after duplicate-counting/parity audit. |
| Symbol/time blacklist plan | Rejected; no deployable common arm. |

Synthetic squeeze, outage, and cluster-bootstrap diagnostics say the sampled
tiny book is survivable, but repeated worst-cluster weighting is fragile. The
loss-at-disaster diagnostic is more actionable: at a +100% shock and 0.10%
equity loss budget, about 97% of historical component trades were oversized.
That is the basis for the registered budget study—not a claim that a fillable
stop exists after a gap.

### Open continuous experiments

1. `continuous-tail-survival-2026-07-10.md`: control plus ex-ante 0.10%, 0.15%,
   and 0.25% +100%-loss budgets. Both venues and the full four-cell matrix are
   mandatory. Signals end 2026-07-10 exclusive; exit data ends 2026-07-12
   exclusive. Root receipts are byte-bound. No heavy run has executed.
2. `continuous-granular-adverse-risk-2026-07-10.md`: a separate, causal
   sub-hour adverse-state experiment with common entry timing, sequential
   one-intervention risk sets, frequency-matched nulls, and strict granular
   readiness. No treatment run has executed.
3. BTC month-regime work remains preregistered but defaults are unchanged. The
   first bare hourly-30d arm was worse on both venues, so any continuation needs
   a newly frozen confirmation/hysteresis mechanism.

## Long v11a

Latest internal cross-venue refresh through 2026-06-23:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Supporting checks:

- positive after best-month removal and 2x/3x cost stress;
- positive deterministic monthly bootstrap p05 and worst 12-month windows;
- 24/26 active-month sign agreement;
- 144/146 paired-trade sign agreement, return correlation 0.9679;
- matched random-symbol null beaten on both venues;
- PIT OHLC paths mechanically support recorded exits under the frozen ordering.

Material dependency: removing the take-profit exit bucket flips Bybit/Binance
to -0.92%/-5.99%. The object therefore needs forward fill/exit/funding evidence;
its internal result is not permission to expand size or mode.

The current ADA forward pair matched the signal but showed roughly 9.47 hours
of entry skew and 34,091.786 seconds of exit skew. That is a reconciliation
failure to learn from, not strategy validation.

## Data, reconciliation, and operations

- Latest pre-reset CONTINUOUS: paper 12, demo base 9, paired 7, paper-only 5,
  demo-only 2, sniper-only 4, open sniper 0, one exit-reason divergence.
- Latest pre-reset LONG: one paired ADA entry and no unmatched entries, with the
  timing/exit skew above.
- Venue independently confirmed flat/no-orders after the incident.
- Reconciliation now separates component legs, local price PnL, recorded fees,
  venue Closed-PnL allocations, and unavailable funding. Unknown/failed PIT is
  a failed LONG model leg.
- Stable residual momentum now has explicit provisional provenance and exact
  schema/duplicate/non-finite gates; consumers use stable rows only.
- Current roots are not granular-ready. Bybit lacks canonical current-root 5m;
  Binance granular files are legacy/stale before the current PIT tail. The old
  2026-06-27 validation artifact must not be cited as current canonical data.
- The strict seven-day audit measured 4,288 Bybit and 5,292 Binance PIT
  symbol-days. Bybit 5m is missing and funding completeness is 14.16%; Binance
  5m/metrics have no complete current-window days, bookDepth is missing, and
  funding/OI/premium/taker-flow completeness is
  83.79%/79.48%/83.79%/83.31%. No granular treatment run is ready.
- Bybit forward depth/liquidation collectors are useful shadow context. They do
  not backfill historical causal features.
- Routine work goes through `scripts/ops.sh`; ledger reset is dry-run by default
  and checked deploy requires explicit `--execute`.

## Current research direction

1. Deploy the audited safety release, archive/reset only while venue-flat, and
   start a clean post-fix demo/paper clock.
2. Accumulate enough paired forward trades to measure fills, latency, fees,
   funding, and lifecycle—not just signal agreement.
3. Run the frozen budget matrix on the larger machine only after the fixed data
   boundary and full-PIT receipts are ready.
4. Build/audit granular roots before the adverse-state experiment. Missing 5m
   data stays missing; never synthesize it from hourly bars.
5. Reopen a closed arc only with a new falsifiable mechanism or genuinely new
   data, not another broad parameter grid.
