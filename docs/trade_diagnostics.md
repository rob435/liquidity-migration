# Trade Diagnostics Contract

This contract defines the minimum institutional-grade diagnostics used to
generate and test new strategy theses. “Jane Street level” is an ambition, not
a public specification of Jane Street's proprietary process. Here it means
causal clocks, exact lineage, market-context-aware execution quality, honest
missingness, correct dependence, and no metric without a decision it informs.

The contract is diagnostic and research policy. It does not make paper fills
calibrated, prove alpha, authorize strategy changes, or authorize mainnet.

## Source hierarchy and grains

Use sources once and derive views from them:

1. verified account-journal transaction segments for target, risk, command,
   ACK, fill, status, fee, funding, and PnL facts;
2. exact sequence-aware book contexts for arrival liquidity and timing;
3. strategy feature/candidate rows for the pre-gate selection funnel;
4. PIT historical bars or bounded forward marks for future path labels.

The grains are deliberately different:

| Question | Required grain |
| --- | --- |
| Was an individual venue command executed well? | `command_id` with child executions aggregated by absolute quantity |
| Did several components represent one idea? | unique `(sleeve, symbol, signal_ts)` decision |
| Did concurrent ideas share one market shock? | simultaneous decision wave / target batch |
| What uncertainty is credible through time? | prospectively declared calendar or event block |
| What happened to each partial fill? | canonical `execution_id`; retain in the journal, do not count as an independent thesis observation |

Component rows, order updates, fills, venues, and overlapping horizons are not
independent replications.

## Sign and benchmark conventions

Let `s=+1` for a buy and `s=-1` for a sell. Costs are positive when adverse to
our order. Let `M0` be the midpoint of the exact decision book, `P` the
quantity-weighted fill price, and `Mh` the first healthy midpoint observed at or
after horizon `h` with the actual observation gap retained.

```text
arrival_shortfall_bps = 10,000 * s * (P - M0) / M0
effective_spread_bps  = 20,000 * s * (P - M0) / M0
signed_markout_bps(h) = 10,000 * s * (Mh - P) / P
post_fill_adverse_bps = -signed_markout_bps
fee_bps               = 10,000 * observed_fee / abs(filled_notional)
all_in_arrival_bps     = arrival_shortfall_bps + fee_bps
```

Also compute shortfall to the strategy decision reference and to the visible
depth-50 book-walk VWAP. The difference between observed fill VWAP and book-walk
VWAP is a residual diagnostic, not automatically “market impact”: book movement,
latency, hidden/RPI liquidity, venue protections, and clock error can contribute.

The SEC's amended Rule 605 standardizes effective-spread, execution-speed, and
realized-spread reporting, including 50 ms, 1 s, 15 s, 1 min, and 5 min realized
spread horizons. We borrow its execution-quality discipline and clocks, not its
U.S.-equity compliance scope or liquidity-provider interpretation. For the
current taker-heavy Bybit route, required operational markouts are 1 s, 15 s,
1 min, and 5 min. A 50 ms markout is optional unless exact raw observations and
clock bounds make it honest. Strategy labels at 1 h, 6 h, 24 h, and 72 h remain
separate from TCA.

The demo owner implements those four clocks as bounded observability. The
private WebSocket and REST-reconciliation consumers notify only newly committed
canonical fills, placing each execution ID on a bounded queue. The owner loop
later resolves the immutable fill and writes one schedule into the existing
capture root. Public order-book updates capture the first healthy book at or
after each target, within a stored five-second operational lateness bound.
If that bound expires, the record is terminally missing with a reason and null
midpoint. The requested horizon, actual horizon, lateness, sequence state, book,
fill identity, and source record ID remain explicit. At most 8,192 horizon tasks
can be pending. One owner-loop drain and one public-book update handle at most
128 registrations or marks respectively, and task symbols remain subscribed
only until the tasks clear.

The five-second bound caps owner resources; it is not a claim-validity
threshold. Analysis reports quantity-weighted markout coverage for every
horizon and cannot silently drop late, gapped, restarted, unregistered, or
capacity-rejected fills. Paper is still uncalibrated integration evidence and
does not acquire execution-quality authority from the same public books.

## Required command-level fields

### Identity and lineage

- account/environment, sleeve, strategy/profile identity, symbol and side;
- unique decision key, wave/batch ID, command ID, venue order ID;
- contributing target, component, and signal-time identities;
- reduce-only/action state and command chunk index/count;
- journal event/hash boundary and decision-book record identity.

### Arrival market state

- engine, venue-system, and local receive timestamps with clock-domain labels;
- update ID, cross-sequence, sequence-gap state, and book age;
- best bid/ask, midpoint, spread, top-level imbalance, and microprice;
- same-side/opposite-side quantity and notional within the touch and 5/10/25 bps;
- requested quantity/notional divided by visible touch and 10/25 bps depth;
- visible executable quantity, predicted book-walk VWAP, and book exhaustion.

Order-flow imbalance needs raw public-event capture and belongs only in claims
whose artifact budget enables it. Depth and top-book imbalance are always
available from the decision snapshot. Cont, Kukanov, and Stoikov found
short-horizon price changes more robustly related to order-flow imbalance than
trade volume alone; that motivates measuring liquidity state, not treating it
as a universal crypto result.

### Lifecycle and fills

- decision/command, local socket send, local ACK, exchange ACK, first/last
  exchange fill, first/last local fill receipt, and terminal status times;
- decision-book age, decision-to-send, local request RTT, send-to-local-first
  fill, exchange ACK-to-fill, fill span, and clock-skew-plus-delivery values;
- acceptance/rejection/terminal reason, fill ratio, unfilled quantity, fill
  count, VWAP, fill-price range, fee and fee-provenance coverage;
- maker fraction, fee rate/currency, execution type/value, leaves quantity, and
  private-stream sequence when the venue supplies them.

Never subtract timestamps from different clock domains and label the answer
“latency.” A local-minus-exchange observation includes clock offset unless an
independent offset bound is applied.

### Post-fill and strategy path

- fixed-horizon midpoint markouts with target horizon, actual horizon, source,
  sequence health, and missing reason;
- MAE/MFE, time-to-MAE/MFE, threshold crossing, exit reason, holding time,
  realized/funding/fee decomposition, and counterfactual fixed-horizon return;
- signal-to-order delay and opportunity cost for rejected, expired, cancelled,
  or partially unfilled intent.

Unobserved marks, fees, MAE, MFE, opportunity cost, or paths are null with an
explicit reason. They are never zero.

## Decision funnel

Capture one row before alpha gates for every declared source-population key.
The default decision unit remains `(sleeve, symbol, signal_ts)`; when components
have genuinely different gate definitions, the source row also carries
`component_scope` while retaining the shared decision-unit key so component
rows are never counted as independent ideas. Preserve:

- causal feature availability and population/PIT provenance;
- every named gate's boolean or missing state;
- first rejection, all applicable rejection keys, and accepted target identity;
- capacity, existing exposure, cooldown, health, and account-risk admission as
  separate operational gates;
- a fixed, small feature set selected before looking at future labels.

This table measures attrition and lets diagnostics distinguish “no signal” from
“signal blocked by data, capacity, execution, or risk.” It must not embed future
path values. Future labels join later by stable key.

The implementation must not append the same unchanged rejection every minute.
At the strategy owner, retain one immutable pre-gate source row and only gate
state transitions for repeated evaluations. The read-only projector folds those
events into one final row per source key with first evaluation, first rejection,
terminal disposition, evaluation count, and first/last timestamps.

- LONG sources are closed feature rows keyed by symbol and daily `ts_ms`, before
  `_classify_entry`; dynamic retrace, cooldown, capacity, health, unresolved
  target, terminal-attempt, and publication gates become transitions.
- CONTINUOUS sources are `entry_state` symbol/hour rows before decile/liquidity
  filters, with component scope for trigger/age differences; shared health,
  adverse-pause, BTC-trend/risk, capacity, reentry, cooldown, crowding,
  unresolved-target, and publication gates remain separately named.

The prospective epoch must freeze source-population and transition semantics
before this writer is added. Otherwise a convenient logging grain would become
an undeclared statistical sample and repeat the retired artifact mistake.

## Analysis standard

Every diagnostic read must report:

- count at command, unique-decision, wave, and block grains;
- median, robust spread, tails, missingness, and effect sizes—not only means;
- concentration by symbol, day/wave, component, liquidity/depth bucket, and
  regime selected before the read;
- uncertainty resampled or estimated at the declared block grain;
- calibration residuals between modeled book walk/cost and observed execution;
- every inspected split, metric, transformation, and thesis candidate.

For variant selection, ordinary holdout language is insufficient after repeated
search. The complete trial ledger is mandatory; use multiplicity control,
deflated performance statistics, or backtest-overfit diagnostics only when their
assumptions match the actual selection process. None is a permission slip to
search an unlimited atlas.

## Artifact budget

A diagnostic run may retain at most four claim-bearing payloads by default:

```text
manifest.json                 identities, schema, counts, nulls, hashes, deviations
execution_tca.parquet         one row per canonical command
decision_funnel.parquet       one row per pre-gate decision unit, when required
path_labels.parquet           future labels only, when required
```

The verified journal and capture root remain sources and are not copied into the
run. Intermediate partitions are resumable working state, not duplicated final
artifacts. Charts and Markdown are regenerated from the tables; only the compact
evidence card and decision are committed to the research summary.

Claim-bearing exports use a quiescent, frozen read-only capture snapshot. The
projector descriptor-checks every scanned segment and refuses a segment that
changes during its read, but that is not a substitute for a filesystem or
operator snapshot boundary around the complete root.

If a claim does not need a listed table, omit it. Adding an artifact requires a
named claim, consumer, and deletion/retention rule.

## Primary references

- [SEC Rule 605 amendments](https://www.sec.gov/files/rules/final/2024/34-99679.pdf)
  for effective spread, size-weighted execution speed, and multiple markout
  horizons.
- [Bybit order-book contract](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)
  for snapshot/delta, update ID, cross-sequence, and matching-engine time.
- [Bybit private execution contract](https://bybit-exchange.github.io/docs/v5/websocket/private/execution)
  for fill identity, execution time/value, fee, maker state, and sequence.
- [Cont, Kukanov, and Stoikov](https://arxiv.org/abs/1011.6402) for depth and
  order-flow imbalance as short-horizon price-impact diagnostics.
- [Stoikov's micro-price paper](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694)
  for an imbalance-aware short-horizon book benchmark.
- [Bailey et al., Backtest Overfitting in Financial Markets](https://escholarship.org/uc/item/4hn4t174)
  and [Harvey, Liu, and Zhu](https://doi.org/10.3386/w20592) for trial-count and
  multiple-testing discipline.
