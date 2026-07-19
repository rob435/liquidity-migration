# Prospective Runtime Parity Execution Epoch: Post-22 Amendments

This file extends the frozen base contract and Amendments 1--22 without
changing any earlier file. A start, change-point, structural, or TCA receipt
using this amendment must bind this file by exact SHA-256 in addition to every
earlier contract identity.

## Amendment 23: engineering closure, forward boundary, and fixed TCA estimator

Registered 2026-07-19 08:27 UTC after the repaired historical comparator and
the operational-branch merge were structurally inspected, but before a
forward start receipt, forward-eligible target capture, calibration fit, or
validation observation was opened. No monetary outcome, return, alpha,
strategy thesis, execution residual, or TCA aggregate was inspected.

### Historical engineering evidence and deployment qualification

The clean full-window comparator at commit
`d8c9c051b4ffcb6116d4332b3244471de6f79e32` completed all 29,449 registered
hourly cycles. Its receipt is
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/active-production-comparator/receipt.json`,
with file SHA-256
`c2bcd3ebe2e7524bf7370f8209bf471dbf75e59f2e88bf33147a476b5d348c19`
and receipt-payload SHA-256
`ad168fddc155a36604e4be0a1b6e002b15d5c7242e9e999508511fda00ef6cf7`.
It verified 12,812 canonical account events, 911 accepted requests, zero
rejected strict risk-reduction batches, exact BTC-risk reconciliation, all 235
registered venue lifecycle events, a verified journal, and a structurally flat
terminal account. Its monetary fields remained unopened.

The repaired research line and the separately operated VPS line were merged at
`c947716982b648fb756deda74fd49f56f96af61c`, followed by the scoped portable-I/O
repair at `4be40d378a20b9441e4146c0f874bd31f7b43226`. A clean Linux clone of the
latter passed Ruff, mypy, and the complete test suite with 2,055 passed and two
explicitly skipped tests whose local-only barebones-ledger fixture was absent.
This establishes integration quality, not full-window comparator identity.

The installed forward candidate must be a clean descendant of `4be40d3` and,
before the start receipt, must run the complete comparator into a new
create-only output. That run must reproduce the registered population and
cycle counts, satisfy Amendment 22, verify its journal, report zero rejected
strict risk-reduction batches, reconcile BTC-risk and lifecycle identities,
and end flat. The start receipt binds that new receipt and its file and payload
hashes. A unit-test pass cannot substitute for this final integrated check.

These facts close the registered historical engineering surface only. They do
not validate historical economics, the legacy equity engines, live intrabar
interleaving, costs, capacity, or a strategy thesis.

### One authorization-bound forward capture plane

Before the epoch starts, deployment must set and operational authorization
must validate these exact derived paths:

```text
demo  STRATEGY_TARGET_CAPTURE_PATH = <ACCOUNT_CAPTURE_ROOT>/strategy-targets.jsonl
paper STRATEGY_TARGET_CAPTURE_PATH = <ACCOUNT_PAPER_CAPTURE_ROOT>/strategy-targets.jsonl
```

Both paths are absolute, live inside their environment's already authorized
capture root, and are shared by LONG and CONTINUOUS in that environment. The
existing interprocess-safe hash-chain writer remains the sole owner. The
complete private per-producer fallback tapes, if any, are inventoried in the
start receipt as pre-epoch history; they are not deleted, copied into the new
tapes, or counted as forward observations. Only rows with causal event times
inside the registered epoch are eligible.

### Create-only start receipt

The start tool runs as root on the installed Linux host after activation. It
must fail closed unless all of the following are simultaneously evidenced:

- exact clean Git HEAD and a currently valid `operational` authorization whose
  environment-file, runtime-root, input, profile, and commit identities verify;
- the six demo/paper owner and LONG/CONTINUOUS producer services are loaded,
  enabled, active, running, on one valid systemd invocation each, and have zero
  automatic restarts in that generation;
- both account owners have current-generation, at-most-30-second health and
  live-book readiness bound to their verified canonical journal heads;
- each producer has a current-generation completed-cycle sidecar no more than
  ten minutes old;
- both canonical journals verify completely, and the receipt records their
  event count, terminal event/state hashes, account state hash, nonzero
  position, working-order, aggregate-target, and component-target census;
- all four scheduling tapes and both shared target-scheduling capture tapes
  parse and verify completely, with path, byte-prefix SHA-256, row count, and
  rolling chain hash retained; missing new shared tapes are valid genesis only
  after their exact configured paths and writable parent roots verify;
- queue-state counts and sorted filenames are retained for pending,
  processing, failed, and processed request directories; and
- `forward/analysis`, `forward/structural`, and `forward/tca` do not exist.

The canonical JSON receipt is published create-only, reopened, and
self-hash-verified. Its validity commit must occur at least five minutes before
the proposed boundary. The epoch starts at the first whole UTC hour strictly
after publication; calibration is `[start, start + 45 days)`, validation is
`[start + 45 days, start + 90 days)`, and the epoch ends at the latter boundary.
The tool must still observe a wall clock earlier than `start` after reopening
the receipt. Otherwise that attempt is invalid and a new create-only path is
required; the epoch never backdates.

Inherited positions, targets, or queued requests are retained and reported;
the start tool must not trade, flatten, cancel, reset, or erase them. Forward
classification uses causal timestamps and the recorded boundary. Any code,
config, service generation, authorization, capture-path, or input-identity
change after start creates a create-only, prior-hash-chained change point and
does not reset or extend the 90-day clock.

### Exact TCA common support and response

The unit remains one canonical demo `command_id`. Paper commands and fills are
never calibration observations. A command enters the common comparison support
only when all three models can be evaluated from one verified diagnostic row:
the command is accepted and terminally filled; requested and filled quantities
are positive and equal within the declared venue step/tolerance; its exact
decision context and depth-50 book are complete and not exhausted; the book
walk covers the request; observed fee provenance is complete; and every
registered response, weight, and predictor is finite and non-null. Every
excluded command remains in coverage tables with its reason. No imputation,
winsorization, clipping, symbol deletion, or model-specific support is allowed.

Let the positive comparison weight be `filled_notional_usdt`. Let
`r = book_walk_residual_bps = arrival_shortfall_bps -
book_walk_shortfall_bps`. Every model's predicted all-in cost is
`book_walk_shortfall_bps + predicted_r + observed fee_bps`; use of the same
observed fee term isolates the registered residual-execution comparison.
Model 1 fixes `predicted_r = 2.0`. A later paper implementation continues to
use its separately frozen 5.5 bps fee rule; this comparison does not fit a fee
model.

Model 2 fixes `predicted_r` to the calibration weighted median of `r`. Sort by
`(r, command_id)` and choose the smallest `r` whose cumulative weight is at
least half of total calibration weight.

Model 3 uses exactly these design columns, in order:

1. intercept;
2. side, `+1` for buy and `-1` for sell;
3. `ln(requested_qty * decision_mid)`;
4. `quoted_spread_bps`;
5. `book_walk_shortfall_bps`;
6. `order_to_touch_depth`;
7. `order_to_10bps_depth`; and
8. `decision_book_age_ns / 1,000,000`.

The six continuous columns after side are centered by their calibration
weighted median and divided by `1.4826` times their calibration weighted median
absolute deviation. A non-finite or at-most-`1e-12` scale makes model 3
ineligible; no column is silently removed. Side is not standardized.

Model 3 minimizes weighted Huber loss for `r`. The fixed response scale is
`max(1e-9 bps, 1.4826 * weighted_MAD(r))`; Huber delta is `1.345`. Initialize
with weighted least squares and then use IRLS weights
`filled_notional_usdt * min(1, 1.345 / abs(standardized_residual))`, with the
multiplier defined as one at zero residual. Each solve uses
`numpy.linalg.lstsq(..., rcond=None)` with no ridge. A non-full-rank design,
non-finite coefficient, non-finite prediction, or failure to reach maximum
absolute coefficient change `<= 1e-10` within 100 iterations makes model 3
ineligible. Coefficients and all calibration transforms are frozen in a
create-only fit receipt before any validation row is read.

### Validation, selection, and uncertainty

Validation is opened once, only after the 90-day end. Primary weighted MAE and
signed bias use `filled_notional_usdt`; the 90th absolute error uses the same
deterministic weighted-quantile rule as Model 2. A candidate can replace Model
1 only when the common validation support has at least 100 commands on at least
20 distinct UTC command days, its weighted MAE is strictly smaller than Model
1, and its absolute signed bias is strictly smaller. If both candidates qualify,
choose the lower weighted MAE; equality within `1e-12` bps selects simpler
Model 2. If neither qualifies, retain Model 1 and
`integration_only_uncalibrated`.

The 1 s, 15 s, 1 min, and 5 min markouts remain separate secondary labels and
never become predictors or support filters for the primary arrival comparison.
Their missingness and quantity-weighted coverage are reported by model, sleeve,
symbol, side, action, UTC day, and incident/change-point stratum.

Uncertainty uses 10,000 validation UTC-day block-bootstrap replicates, retaining
every command and decision wave inside each sampled day. Sampling uses NumPy
`PCG64` with the unsigned big-endian first eight bytes of
`SHA256("prospective-runtime-parity-execution-epoch-2026-07-18|tca-day-bootstrap-v1")`
as its seed. Percentile 2.5% and 97.5% intervals are reported for candidate
minus baseline MAE and signed bias, but do not replace the frozen point-estimate
selection rule. No early look, optional stopping, clock reset, return analysis,
alpha claim, deployment promotion, mainnet use, or real-money authority is
created by this amendment.

## Amendment 24: canonical queue-state name

Registered 2026-07-19 08:35 UTC during start-tool source inspection, before its
implementation or any forward start. Amendment 23 used “processed request
directory” as prose, while the production inbox names that terminal directory
`completed`; `processed` is a receipt disposition and a journal batch set, not
a directory. The start receipt therefore inventories the four real request
directories `pending`, `processing`, `completed`, and `failed`, plus the
reduced journal's processed-batch census. This is a nomenclature correction
only. It changes no row, boundary, support rule, model, outcome, or authority.
