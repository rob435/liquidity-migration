# Prospective runtime-parity and execution epoch, 2026-07-18

## Prospective contract

- **ID / owner / registered time / study mode:**
  `prospective-runtime-parity-execution-epoch-2026-07-18`; repository owner
  with Codex operator; 2026-07-18 13:35 UTC. Input reconstruction, runtime
  parity, and full-ledger reconciliation are engineering validation. Demo/paper
  observation is `forward_execution`. Execution/TCA fitting is exploratory on
  its calibration half and confirmatory only on its frozen validation half.
- **Reason to open:** the V2 repair established that mutable historical inputs,
  incomplete active-runtime reconstruction, and a unitless numeric tolerance
  prevented a decision-grade comparator. This epoch corrects those defects on
  new infrastructure and newly accumulating forward observations. It does not
  reopen the failed V2 thesis search.
- **Generating code at registration:**
  `76a3f05e9b6b409c818f463e482f67edc0be60d9`.
- **Intended action:** build a reconstructable Bybit research epoch, prove that
  offline target/account behavior matches the active runtime including the
  accepted-decision BTC-risk state, complete the previously uninspected tail of
  the full account replay, and estimate execution error separately. A later
  economic or thesis contract is permitted only after these claims are valid.

## Claims and explicit non-claims

This contract evaluates four separate propositions:

1. a create-only content snapshot can reconstruct every raw input consumed by
   the named feature and comparator builds, with identical logical keys and
   bytes;
2. the offline comparator emits exactly the same discrete source decisions,
   target requests, accepted-decision BTC-risk evidence, lifecycle events, and
   account identities as the production functions on the same frozen inputs;
3. during one immutable forward epoch, demo and paper target-scheduling
   captures agree wherever their declared inputs and route-independent state
   are identical, and every disagreement is retained rather than pooled away;
4. command-grain demo execution cost can be calibrated from canonical journal,
   decision-book, and post-fill markout evidence and can outperform the frozen
   uncalibrated paper baseline on a chronologically later validation half.

No return, alpha, source characteristic, strategy lever, profile promotion,
capital, size, deployment-readiness, mainnet, or real-money conclusion is in
scope. Historical and forward returns must not be opened under this contract.

## Prior exposure and surfaces

- All Strategy Overhaul V2 discovery data and aggregate current-profile results
  through 2026-07-16 are spent. They may be used only as engineering fixtures.
- The known 100-key account receipts and the two known 200-key LONG BTCUSDT
  gross-P&L differences are calibration evidence. They cannot count as a new
  parity pass.
- The still-unreplayed account-ledger suffix beyond the first 200 hash-selected
  keys per sleeve is the new engineering validation surface. It contains no
  new strategy-price outcome; only event, coverage, flatness, and accounting
  identities may be inspected.
- The raw baseline snapshot uses the local Bybit PIT root and the half-open
  completed-data boundary `[2023-03-01, 2026-07-18)`. Earlier manifest rows may
  be retained solely to establish launch/listing history. No feature, target,
  path, return, or P&L outcome is inspected while the snapshot is built.
- The forward epoch begins only after a create-only start receipt proves the
  exact installed commit, profile/config identities, healthy demo/paper owners,
  journal/capture starting hashes, and an empty pre-start analysis directory.
  Its start is the first whole UTC hour after that receipt. It runs for 90
  consecutive UTC days; incidents and code/config changes create retained
  change points and do not reset the clock.

The venue is Bybit USDT linear perpetuals because that is the active demo
execution surface. Binance is not required for this venue-specific systems and
TCA claim.

## Immutable raw-data snapshot contract

The initial snapshot includes only claim-consumed raw/PIT datasets, never
legacy features, reports, locks, caches, or prior backtest outputs:

```text
archive_trade_manifest
klines_1h
klines_5m                 when present in the declared window
funding
open_interest
mark_price_1h
index_price_1h
premium_index_1h
```

The implementation stores file bytes and a sorted logical manifest in a
create-only content container. Every row records normalized relative path,
dataset, size, source mtime, and SHA-256. Publication requires:

1. stable descriptor reads with no symlink/reparse traversal;
2. a second complete source inventory with identical path, size, mtime, and
   file hash;
3. a deterministic logical-root hash over the ordered row identities;
4. a whole-container SHA-256 and create-only receipt;
5. read-only reopening followed by full row/content verification; and
6. reconstruction into a new empty run root with full byte-hash verification.

Staging is resumable and visible. A changed source invalidates that attempt;
the tool must not silently blend vintages. Final containers and receipts are
never overwritten. The baseline snapshot is followed by create-only daily
delta containers chained by prior receipt hash for the 90-day forward epoch.

## Run-scoped feature and PIT gate

Every feature is rebuilt below the named run directory from a verified
reconstruction of the snapshot chain. Shared-root feature files are never read
as evidence and are never overwritten. The fixed feature set is the one
consumed by the active LONG and CONTINUOUS profiles at the implementation
commit, including causal residual momentum with its seven-day window,
three-day shift, four-observation minimum, and explicit provisional owner.

For every decision timestamp, archive-manifest membership is applied before
turnover, volume, RMOM, composite, decile, or other cross-sectional ranks.
Membership uses `date(signal_ts_ms - 1ms)` for daily signals and the actual bar
date for hourly signals. Archive-observed and current-listing-inferred rows
remain separately counted. Missing required membership or kline coverage
invalidates the affected population claim; no symbol is silently dropped.

Feature receipts must pin snapshot-chain hashes, code/config, command,
start/end, schema, row/key counts, provisional counts, PIT source counts,
missingness, and every output SHA-256. Causal availability fixtures are run
before any parity comparison.

## Exact runtime/comparator parity

The comparator must call the production profile, candidate, BTC-risk evidence,
target-construction, account-kernel, and lifecycle owners rather than duplicate
their formulas. Route, clock, market, journal, and execution ports may be
deterministic adapters. The complete parity surface includes:

- PIT membership before every active rank;
- LONG and all three active CONTINUOUS component source decisions;
- stable RMOM, prior-day BTC trend, event/age/liquidity/crowding gates,
  inverse-vol sizing, capacity/order, exits, and cooldown/re-entry state;
- the complete accepted-decision BTC-risk predecessor chain, warm-up count,
  score, tail decision, multiplier, evidence hash, and synchronized account
  acceptance state;
- exit-first target publication order, request content hashes, risk decisions,
  fills, positions, fees, funding, closes, P&L, final flatness, and event/state
  hashes.

Strings, timestamps, keys, booleans, discrete decisions, event order, hashes,
null positions, and integer counts require exact equality. Float comparison is
dimensioned and frozen as follows:

| Quantity | Absolute tolerance | Relative tolerance |
| --- | ---: | ---: |
| USDT cash, gross P&L, fee, funding, notional | `1e-8 USDT` | `1e-12` |
| dimensionless return, weight, score, multiplier | `1e-12` | `1e-10` |
| price or base quantity before venue discretization | `1e-12` native units | `1e-12` |
| venue-discretized price or quantity | exact declared tick/step | n/a |

`1e-8 USDT` is a currency-unit floor, one millionth of one cent, rather than a
copy of the known mismatch. Comparisons also require matching finite/non-finite
positions and report absolute, relative, and ULP differences. Aggregate
agreement cannot hide a component/key mismatch.

The known first 200 hash-selected keys are reported as calibration only. The
parity decision requires the complete 1,899 LONG and 16,745 CONTINUOUS replay,
including the previously unseen suffix. Both sleeves must end flat; every
source key must have exactly two decisions, two fills, one close/P&L
attribution, and a verified journal. One preserved attempt is allowed after
preflight; an unexplained mismatch makes the affected parity claim invalid.

## Forward demo/paper structural epoch

The active strategy/config remains frozen for the 90-day clock. Existing
target-scheduling capture tapes are the source of proposed requests; canonical
account journals own accepted targets and lifecycle facts. Paper is an
uncalibrated integration twin and cannot support fill-quality claims.

The primary units are `(environment, sleeve, component, symbol,
signal_ts_ms)` for target parity and canonical `command_id` for TCA. Every
cycle, decision wave, target, rejection, and missing record is retained. Exact
demo/paper request equality is required only when the receipt proves identical
candidate/rule/RMOM inputs and route-independent causal state. Other rows are
classified as explained input/state divergence or unexplained divergence.

Structural support requires at least one nonzero target decision in each sleeve
and zero unexplained request-content mismatches. A zero-decision sleeve is
inconclusive, never a vacuous pass. Operational incidents do not erase data;
they are explicit strata and change points.

## Separate execution/TCA calibration

TCA reads only verified demo journal commands/fills, exact decision books, and
the registered 1 s, 15 s, 1 min, and 5 min post-fill marks. It follows
`docs/trade_diagnostics.md` sign and missingness conventions. Paper fills are
not calibration observations.

The 90-day epoch is split once by time: days 1--45 are calibration and days
46--90 are validation. The complete tested model set is:

1. frozen paper baseline: visible depth-50 book walk + 2.0 bps residual adverse
   slippage + observed/frozen fee rule;
2. book-walk plus one calibration-half quantity-weighted median residual;
3. book-walk plus calibration-half Huber regression using only side, log
   requested notional, spread, book-walk shortfall, touch participation, 10 bps
   participation, and decision-book age.

No feature, transform, interaction, sleeve split, symbol effect, or model is
added after calibration outcomes are read. Hyperparameters are fixed before
the first calibration fit in the run receipt. Models are compared on validation
quantity-weighted MAE of all-in arrival bps; secondary diagnostics are signed
bias, 90th absolute error, horizon markouts, coverage, and concentration.
Block uncertainty uses UTC day with decision wave retained inside day.

Calibration supports model 2 or 3 only if it improves validation weighted MAE
over model 1, has smaller absolute signed bias, has at least 100 filled commands
across at least 20 UTC days, and no required-field/markout coverage is silently
imputed. Otherwise the execution model remains
`integration_only_uncalibrated` and the result is inconclusive or contradicts
the replacement. No economic strategy conclusion is permitted from TCA alone.

## Artifacts, resources, deviations, and stopping

The create-only root is:

```text
reports/prospective-runtime-parity-execution-epoch-2026-07-18/
```

It retains contract hash, code/config identities, baseline and delta snapshot
receipts/containers, reconstruction receipts, run-scoped features, PIT report,
parity journal/receipt, forward epoch start/change-point ledger, structural
comparison, and the separate TCA calibration/validation payload. Failed and
partial staging roots remain visible. Outcome-bearing commands run at
concurrency one; snapshot hashing may use bounded readers while preserving
ordered identities. No result is opened until its preflight and identities
pass.

Repairs to snapshot, portability, parity, or projection code are permitted
before the affected validation surface is read. Any rule, tolerance, feature,
model, horizon, or classification change after exposure requires an append-only
amendment and a new forward surface. Unknown safety-critical operational state
fails closed. This contract authorizes offline work and observation of the
already authorized demo/paper fleet; it does not authorize mainnet,
`REAL_MONEY`, new capital, or a strategy/profile change.
