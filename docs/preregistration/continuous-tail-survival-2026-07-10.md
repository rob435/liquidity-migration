# Continuous Tail-Survival Pre-Registration

Date: 2026-07-10

Status: registered; not run

Run label: `exploratory`

Dispatcher: `scripts/continuous_tail_survival_2026_07_10.py`

This run can reject an ex-ante sizing mechanism or authorize a separate
forward-shadow implementation review. It cannot promote a strategy, reset a
forward clock, enable real money, or justify a size increase.

## Trigger and diagnosis

The Bybit demo book entered six 1000TAGUSDT short legs on 2026-07-08: three
base components plus three now-disabled sniper adds. Venue executions allocate
price loss of $72.4438 to base and $15.1011 to sniper. Total trading fees were
$0.354602; six funding credits returned $0.20271274. The account-authoritative
Bybit Closed-PnL is therefore `-$87.69678926`, or 0.873502% of the
`$10,039.6785` entry equity. Local sniper-ledger PnL is not venue authority
because the shared-symbol exit was historically attributed to the wrong leg.

The later wallet mark was roughly 1.69% below its recorded high-water. A
read-only venue check found no open position or order after reconciliation.

This loss does **not** prove that a missing fixed stop caused the damage.
Current-profile 20%/40%/80% stops reduced MAR on both venues, and several other
stop, hold, and MFE-giveback variants also failed or split by venue. Adverse
excursion is often the strategy's reversion setup. The demonstrated control
failure was that same-symbol exposure could be added without a registered
ex-ante loss budget; the disabled sniper doubled that governance problem.

The 2026-07-03 tail-budget plan was never run. Its later claim that disaster
sizing is “a fixed stop in disguise” was methodologically wrong. A stop assumes
an executable exit after the move; an entry budget prevents the exposure before
the move. Fixed-stop failures do not falsify ex-ante sizing.

## Registered hypothesis

H1 -- per-component survival budget: cap entry notional so a +100% adverse move
costs at most 0.10%, 0.15%, or 0.25% of equity. A useful arm must materially
reduce realized and counterfactual tail risk without breaking return, split,
capacity, or two-venue stability.

Default verdict for every treatment is reject.

## Frozen object, hashes, and windows

- Venues: Bybit and Binance; both are mandatory for a follow-up pass.
- Signal window: 2023-04-01 through 2026-07-09 UTC inclusive.
- Signal/config end boundary: `2026-07-10`, exclusive.
- Exit-path data boundary: `2026-07-12`, exclusive. Klines and funding must
  therefore exist through 2026-07-11 so July 9 signals can complete their
  one-hour delayed entry and 24-hour hold without a clipped tail.
- Frozen forward-object hash:
  `c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`.
- Components and weights: p3 1/3, p4p3 2/9, p4p5 4/9.
- Entries: frozen component recipes, q25 stable residual momentum, causal hourly
  signals, one-hour entry delay, and the 30-day prior-day BTC uptrend gate.
- Sizing/control layers: inverse vol, target 0.01, clamp 2, and the current
  `CTRL_BTC_RISK_70_90_35` overlay.
- Exits: TP12 and 24-hour max hold. No treatment changes exits.
- Portfolio: current BTC+ETH hedge and BTC-vol hedge regime; daily rebalance off.
- Costs and funding: current engine costs and exact venue funding.

The effective TP12 control component hashes are frozen as:

| Component | Control config hash |
| --- | --- |
| `turn3p3` | `f4f75d9e0547` |
| `turn4p3` | `6e5f7336851e` |
| `turn4p5` | `89011515e462` |

The dispatcher derives and stamps every treatment hash from those exact configs:

| Cell | turn3p3 | turn4p3 | turn4p5 |
| --- | --- | --- | --- |
| `budget_010` | `5fbb4bba34cf` | `eaf8ebf26d18` | `d0b4f8203982` |
| `budget_015` | `4c85020e4a61` | `44dca1702a0b` | `7f401b73a216` |
| `budget_025` | `988d54548e6e` | `63426af6f5ad` | `60dc795a0076` |

Any frozen-object or effective-control hash drift is a hard refusal, not a new
version of this experiment.

The window is spent and includes the incident that motivated this audit. All
results remain in-sample `exploratory`; only a new forward demo/paper clock can
produce OOS evidence.

## Registered cells

| Cell | Per-component +100% loss budget | Other treatment |
| --- | ---: | --- |
| `control` | off | none |
| `budget_010` | 0.10% | none |
| `budget_015` | 0.15% | none |
| `budget_025` | 0.25% | none |

No arm may be added after a partial result. Each cell and venue rebuilds its own
endogenous BTC-risk decision history. A control multiplier tape is never reused.
The dispatcher purges process/disk decision state before a non-resumed cell and
refuses any executed trade whose BTC-risk lookup key is missing.

## Data and run-integrity gates

Before compute, each root must pass all of the following:

- no missing or empty daily kline partitions from 2023-04-01 through 2026-07-11;
- no missing or empty PIT membership partitions through 2026-07-09;
- no missing or empty funding partitions through 2026-07-11;
- `residual_momentum.parquet` has exact symbol/timestamp/value/provenance
  schema, no duplicate or non-finite keys, and at least 20 stable causal names
  on every signal day in the full registered window;
- root identity includes SHA-256 content hashes for every relevant file plus
  the stable-rmom parquet; a fast path/size/mtime fingerprint is checked after
  each cell and the exact byte hash is repeated after the full matrix;
- every executed component symbol is present in that signal day's PIT manifest;
- every held component symbol/day has a readable finite funding record;
- BTCUSDT and ETHUSDT returns and funding have complete strict coverage. Missing
  hedge inputs may not default to zero.

A matching canonical root-build verification receipt is required for a positive
registered verdict. The receipt is bound to venue, resolved root, fixed windows,
and the exact dispatcher data fingerprint. Missing, malformed, stale, or
root-mismatched receipts force the entire run to `diagnostic_only`; they can
never produce `pass_followup_only`. `scripts/verify_full_pit_rebuild.sh` writes
these receipts only after its independent data-layer, historical-parity, smoke,
test, and lint gates pass.

One `run_id` hashes the commit, fixed windows, complete registered matrix, frozen
configs, root content, and diagnostic settings. An output root belonging to a
different `run_id` is refused. Summaries read only exact matching receipts.
Dirty-worktree, single-venue, or changed-window overrides are stamped in the
manifest and every receipt; they can never pass. An incomplete matrix remains
`incomplete`, never `reject` or `pass`.

## Metrics

All verdict comparisons use unrounded equity values recomputed from
`continuous_equity.csv` over the fixed calendar. Rounded chart/report fields are
not decision inputs. Per cell and venue the dispatcher records:

- total and annualized return, max drawdown, MAR, and worst day;
- CDaR95, daily ES95/ES99, and five-worst-loss-day concentration;
- worst rolling 90-calendar-day return and maximum no-new-high duration;
- pre/post-2025-06-01 return, drawdown, and MAR;
- maximum simultaneous one-name +100% shock loss;
- maximum simultaneous top-three-name +50% shock loss;
- component candidate, trade, capacity-skip, and risk-clamped counts;
- exact funding modes, component config hashes, data-plane validation, root
  fingerprints, artifact fingerprints, commit, and run ID.

The shock diagnostics aggregate simultaneous same-symbol exposure across all
three components. They do not assume an exit fill.

## Decision rule

All gates apply separately to both venues. There is no pooled rescue.

A budget arm passes to follow-up review only if:

- total return stays positive;
- raw full-window MAR is at least 95% of control;
- max drawdown does not worsen by more than 5% relative;
- CDaR95 improves at least 15%;
- ES99 improves at least 10%;
- one-name +100% and top-three-name +50% shock losses each improve at least 20%;
- the budget binds at least one trade;
- candidate, selected-trade, and capacity-skip counts equal control exactly,
  proving this arm changed sizing rather than admission or exits;
- both pre/post-2025-06-01 returns stay positive and each split's raw MAR is at
  least 90% of the corresponding control split;
- worst-90-day loss does not worsen by more than 5%;
- no-new-high duration does not worsen by more than 10% or seven days, whichever
  tolerance is larger;
- exact PIT, stable-rmom, funding, hedge, config, root, and receipt gates pass.

A `pass_followup_only` requires the clean, complete four-cell/two-venue matrix.
It authorizes only a separate code/parity review and new paper/demo clock. It
does not change the promoted-in-code profile.

## Dispatcher and checkpoints

Plan first:

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/continuous_tail_survival_2026_07_10.py --plan
```

As registered, the local roots correctly refuse: their funding/market tails do
not reach July 11, their stable residual-momentum history must be rebuilt with
provenance, and verified root receipts are not yet current. Do not shorten
either boundary or waive receipt identity to make the plan pass.

Run the full matrix on the larger machine after the relevant code is committed:

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/continuous_tail_survival_2026_07_10.py \
  --bybit-root /path/to/SHARED_DATA/bybit_full_pit \
  --binance-root /path/to/SHARED_DATA/binance_full_pit \
  --output-root /path/to/SHARED_DATA/continuous_tail_survival_2026-07-10
```

For staged compute, retain the same output root and manifest. Control is
automatically prepended; only exact matching completed receipts are skipped:

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/continuous_tail_survival_2026_07_10.py --cells budget_010 budget_015

POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/continuous_tail_survival_2026_07_10.py --cells budget_025
```

## Expected artifacts

Under `continuous_tail_survival_2026-07-10/`:

- `run_manifest.json` and `config.json`;
- `<cell>/<venue>/continuous_equity.csv` and equity report;
- `<cell>/components/<venue>/<component>/continuous_trades.csv`, candidate tape,
  and report;
- per-cell endogenous BTC-risk decision components, state, and multipliers;
- `<cell>/<venue>/cell_receipt.json`;
- `summary.csv`, `summary.json`, and `verdict.md`.

## Deferred mechanism arcs

Portfolio heat is not in this runnable preregistration. It needs a separately
specified causal, component-aggregated admission contract and live/backtest
parity before scoring; no reused control BTC tape is acceptable.

Failed-fade and other adverse exits are also deferred. The current research hook
detects the threshold at a bar close and fills on that same close. That is not
credible safety execution evidence. A future exit arc must specify decision
latency, next-executable-price/slippage semantics, server-side protection where
relevant, partial-loss-side behavior, and a frequency-matched null before any
two-venue replay.

## Result

Not run. Current local root readiness fails as documented above.
