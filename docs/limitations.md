# Known Limitations

A single, honest place that catalogs what this system **cannot** currently claim
or do. Maintained by the continuous audit loop
(`docs/audit/CONTINUOUS_AUDIT_LOG.md`). If a limitation is removed by a fix, move
it to the "Resolved" section with the date and commit.

This complements — does not replace — the methodology gate
(`docs/backtesting_errors_we_never_repeat.md`) and the live state (`STATE.md`).

## Evidence & promotion

- **No real-money validation.** Everything is research-stage, demo/paper only.
  `REAL_MONEY` stays `false`. Nothing has cleared the three-tier demo-arbiter
  gate to real money.
- **Internal backtests are not promotion evidence.** Forward demo/paper is the
  arbiter. A backtest (including the three-way reconcile's backtest leg) is
  agreement/execution evidence only — never alpha proof, never a promotion gate
  (`docs/backtesting_errors_we_never_repeat.md`).
- **No internal pre-2023 OOS root.** The cross-venue forward bar is the arbiter;
  there is no held-out historical OOS root to lean on (`docs/data_roots.md`).

## Data coverage

- **Binance forward-liquidation capture needs a permitted-region host.** Not
  currently captured from this environment (STATE.md open decision #6).
- **Binance FAPI ancillary June top-ups are incomplete** pending a
  permitted-region host (STATE.md open decision #3).
- **Same-day PIT membership lags ~1 day.** The trading-day archive publishes
  ~1 day late, so a same-day signal may not PIT-validate until the next manifest
  refresh. The fix is to wait a day, not to loosen the gate.
- **Residual momentum (`rmom`) completes ~2 days late.** Today's bars are
  incomplete; the continuous signal-consistency check is a consistency check on
  live data, not OOS/promotion evidence.

## Test coverage posture

Package coverage is **~79%** (line+branch, `pytest --cov`). The continuous-audit loop
(iter-9, 2026-06-18) analysed the uncovered ~21% and confirmed it is concentrated in
**network/IO glue that is not unit-testable**, NOT in untested business logic:

- Lowest-covered modules are the live collectors / WS loops / API clients
  (`liquidation_collector`, `depth_collector`, `binance`, `kline_follower`,
  `kline_stream_manager`, the `*_daemon`s). Their uncovered lines are the WS run loops,
  reconnect/backoff, symbol-universe fetch, and `main()` — exercised only against a live
  venue, not in CI.
- Their **pure logic IS unit-tested**: e.g. `liquidation_collector`'s parsers
  (`parse_bybit_event`/`parse_binance_event`, incl. the zero-`ap` truthy-string fallback
  and the zero/negative price+qty drop) and `connection_expired` are fully covered;
  `depth_collector.band_notionals` (incl. the crossed-book guard) is covered.

So the uncovered surface is an accepted integration-glue limitation, not a hidden-bug
reservoir. The core financial/PIT math additionally carries property-based invariant
tests (`tests/test_property_invariants.py`, iter-8).

## Reconciliation

- **Funding is off by default in the three-way reconcile.** Funding affects the
  backtest's PnL/cost only, not which entries the model picks; a full-universe
  funding backfill is slow (retry-on-empty across ~800 symbols). Use
  `--with-funding` for costed PnL.
- **CONTINUOUS has no 1:1 trade-ledger backtest leg.** A costed `continuous-events`
  run cannot reproduce `FROZEN_FORWARD_CONFIG`'s ensemble+hedge, so the faithful
  model leg is engine-decile membership of the live entries, not a trade pairing.
- **The three-way's runtime is dominated by the PIT data download** (network).
  Use `--no-data-refresh` to re-run the reconcile quickly once the root is current.

## Hedge / risk plumbing

- **Hedge warmstart staleness is calendar-age based.** After a long flat spell
  the first risk-increasing leg can block on calendar-age staleness and page,
  even on a live socket (STATE.md open decision #2 — whether to make it
  ledger-aware is undecided).

## Known data/code warts (from the continuous audit — tracked, not yet fixed)

These are confirmed, adversarially-verified findings that are latent or
design-decisions, tracked in `docs/audit/CONTINUOUS_AUDIT_LOG.md`. Behavior-changing
ones are proposals in `docs/strategy_improvements.md`.

- **Binance proxy funding hardcodes an 8h interval** (`downloaders._normalize_binance_funding`):
  wrong for 4h-cadence alts. Currently harmless — every live reader either ignores the
  stored interval (`derive_funding_interval_min` re-derives it data-intrinsically) or
  never sees the `funding_rate_8h_equiv` column (the Binance download runs no
  postprocess). DO NOT "fix" by adding `postprocess=normalize_funding_history`: it
  recomputes off the same hardcoded 480 and would ACTIVATE a 2x mis-scaling. Correct
  fix = derive the interval from `fundingTime` spacing before storing (audit-iter1 data-io-1).
- **Windowed ledger read can drop a >6-month-open trade** (`storage._recent_ledger_month_dirs`):
  a post-migration trade lives in its `entry_ts_ms` month bucket, not the legacy=0
  tail, so a position open longer than `months_back` (=6 in ws_risk) is invisible to the
  steady-state reconcile. Mitigated by the full read on every restart (`bootstrap`) +
  server-side stops; the longest sleeve hold is ~21 days, so the >6mo precondition is
  not reachable in normal operation (audit-iter1 archive-recon-1).
- **Orphan-close exit price drops priced-but-size-less legs** (`event_demo_exits._orphan_close_pnl_from_records`):
  the volume-weighted price uses only legs with `closedSize>0`, while fees sum over all
  legs — a minor price/fee leg-set inconsistency on a degenerate Bybit closed-PnL row.
  Reconciliation backfill path on a demo ledger only (audit-iter1 event-demo-4).
- **`data_layer` estimated `bar_coverage` no longer measures intra-day completeness**
  in the estimate path (the 24-bars/day factor cancels). Display-only, never wired into
  status/promotion; now suppressed when the row count was estimated (audit-iter1 data-io-2,
  fixed) — listed here so the report's coverage column is read correctly.
- **`continuous_demo` builds the live decile panel every cycle** even when
  `entry_confirm_delay_hours>0` (the deployed default), where neither entries nor exits
  read it — it only feeds the `live_d9` telemetry integer. Wasted hot-path compute, not a
  correctness bug; a fix must preserve the *live* (not confirmed-bar) semantics of the
  telemetry (audit-iter1 continuous-2).

### From audit iteration 2 (2026-06-18)

- **`risk_model.decompose_strategy_pnl` coerces a missing factor return to 0.0**
  (`fr_map[d].get(f) or 0.0`), which would understate factor-explained PnL and
  overstate residual alpha. The active trigger is REFUTED — `fit_factor_returns`
  emits all-or-nothing per day, so a surviving day always has every factor; the
  only path is a mismatched caller passing loadings whose factor set isn't a subset
  of the fitted factor returns, and the only such consumer (`decompose_strategy_pnl`)
  is currently exercised only in tests. Defensive gap, not active corruption
  (audit-iter2 risk-factor-1). Fix when touched: validate the factor-set subset at
  entry and treat a genuinely-missing factor return as unresolved, not 0.0.

### From audit iteration 3 (2026-06-18) — WS-close PnL/fee accounting (demo/paper)

Confirmed defects in the `ws_risk` multi-leg WS close path. All affect recorded
PnL/fee on the netted demo/paper ledger (no real money); `safe=False` because the
fix touches the latency-critical close path + trade-row schema and needs a targeted
split-close unit test. Tracked in `docs/audit/CONTINUOUS_AUDIT_LOG.md` iter-3.

- **Split (multi-sub-order) WS exit books only the FINAL sub-order's fee** into
  `exit_fee_usdt` (under-counts fees on a split close). Fix: accumulate fees across
  all sub-links before stamping (ws_risk ~1338/1700-1748).
- **`gross_trade_return` on a multi-leg WS close records only the final leg's gross
  return** (ws_risk ~1326/1340).
- **`reconcile_flat_pending_exit_orders` drops prior partial-reduce realized PnL**
  from `net_return` (ws_risk ~2520-2535).
- **An aged-out pending exit order (> `pending_exit_guard_seconds`) is dropped from
  tracking**, so a late WS fill for it is silently discarded (ws_risk ~2700-2709).
- **Config flatten-safety validator omits `continuous_addon_data_root`** from its
  warning set (ws_risk ~3192-3205).
- **`backfill_binance_funding_vision` reintroduces the 8h (480-min) hardcode** on a
  missing/blank `funding_interval_hours` — same family as data-io-1 (script ~133-134).

### From audit iteration 5 (2026-06-18) — deploy/ops + test-debt

Deploy/systemd items (safe=False — they touch the live VPS units / deploy flow, so they
need operator review before landing on the host). Tracked in the audit ledger iter-5.

- **`deploy/systemd/liquidity-migration-continuous-forward-report.service` compares
  against the ERASED daily-SHORT paper root** (`data/bybit-paper-event`). The
  continuous-vs-daily forward comparator points at a dead root → the comparison is
  meaningless. Needs the live unit updated (operator) to the surviving root or removal.
- **`deploy_vps_live.sh` rmom-seed verification checks the DEMO root only** — a false
  "rmom gate EMPTY" WARN in the `CONTINUOUS_SLEEVE=off` + paper-on combo (post-7d39d61
  per-root refresh). Cosmetic (a WARN), but misleading.
- **A kept-enabled hedge timer/oneshot is UNMONITORED when `CONTINUOUS_SLEEVE=off`**
  (`check_demo_liveness` line ~484): a failed hedge wind-down run never pages. This is
  the false-NEGATIVE flip-side of the orphan-hedge false-POSITIVE fixed this iteration
  (the watchdog now reads the real timer state). Needs a monitoring-design decision.

Test-debt — tests that assert on SOURCE STRINGS rather than behavior (they pass even if
the code regresses); low priority, listed so they aren't mistaken for real coverage:
- `tests/test_scripts_alpha_sweep.py` era-split MAR guard (checks the source contains
  `era1_mar`/`era2_mar`, not that the split is correct).
- `tests/test_liquidity_migration_continuous_demo.py` exit-planner wiring lock (checks a
  source substring, not which decile snapshot the planner receives).
- `tests/test_liquidity_migration_trade_lifecycle.py` cost-funding-4 test (checks a
  comment string exists).
- `tests/test_liquidity_migration_continuous_addon_shadow.py` `_float` regression inputs
  don't distinguish the buggy `float(value or 0.0)` idiom from the fix.

### From audit iteration 6 (2026-06-18) — deferred (low / design / unused tooling)

- **`ingestion.densify_trade_klines_{1m,1h}` silently drops a bar whose ts_ms is off
  the single-day grid** (a mislabeled archive partition). Raise-vs-warn is a design
  call (a hard raise risks failing on legitimate midnight-boundary spillover), so it's
  deferred rather than auto-fixed (audit-iter6 ingestion).
- **`ridge_combiner.select_lambda` optimizes a time-POOLED rank-IC, not the within-day
  cross-sectional IC the ridge is deployed for** — a metric/objective mismatch. The
  module is currently unused by any production path; flag for the operator whether to
  wire it in (with the per-day objective) or retire it before investing further.
- **`volume_events_pit` diagnostic field `manifest_date_symbols_missing_from_klines`
  counts the RAW manifest set, not the gate's `required` set** (overcounts pre-listing/
  post-delisting phantoms). It's a reported diagnostic only — no gate decision or
  downstream consumer depends on it — and the module is the methodology-critical PIT
  gate, so it's left untouched pending a deliberate change (audit-iter6).
- **`momentum_signals._empty_daily_bars` schema doesn't match the conditional non-empty
  `daily_bars` schema** (volume_base/turnover_quote always present in the empty frame,
  conditional in the non-empty). Edge-case-only; needs the caller contract clarified.

## Resolved

_None yet._
