# Pre-registration: Single shared Sharpe convention

**Date:** 2026-06-14
**Author:** Claude (audit metrics-correctness loop)
**Stage:** run-pending
**Finding:** metrics-3 (audit bucket b04).
**Approval:** OPERATOR-APPROVED 2026-06-14 — the operator answered the audit's
flagged-items questions and approved this consolidation. The code change is done
and uncommitted in the working tree; this receipt is filed before the confirming
(reconcile-against-the-same-ledger) run, per the AGENTS.md "Parameter
pre-registration" rule.

## What's changing

Collapse three divergent Sharpe formulas into one canonical helper
`trade_lifecycle.annualized_sharpe` (`mean / std(ddof=1) * sqrt(365.25)`) and
route all three reporting sites through it so cross-report Sharpes are directly
comparable.

This is a **reported-metric** consolidation, not a decision-rule input. STATE.md
gates on MAR primary; Sharpe is secondary and the only hard Sharpe bar is the
Tier-3 **residual Sharpe `>= +0.3`**, which is real-money-only and currently
unmet. No per-venue backtest *signal* changes — same ledgers, same entries, same
returns; only the Sharpe summary number at each site moves.

## Exact files / knobs touched

New helper (single source of truth):

- `liquidity_migration/trade_lifecycle.py:80` —
  `annualized_sharpe(daily_returns, *, ann_days=365.25) -> float`:
  filters non-finite/None, returns `0.0` for `< 2` finite points or
  `std(ddof=1) <= 1e-12`, else `mean / std(ddof=1) * sqrt(ann_days)`.

Three Sharpe sites rerouted through it:

1. **`trade_lifecycle._daily_sharpe`** (`trade_lifecycle.py:95`, return at
   `:130`). WAS: local `mu / sd * math.sqrt(365.0)` with `sd = std(ddof=1)`.
   NOW: `return annualized_sharpe(daily_ret)`. Net change at this site is the
   annualizer only (`sqrt(365.0) -> sqrt(365.25)`); ddof was already 1 here.
   This is the long-native / trade-backtest report path (`summarize_trade_backtest`).
2. **`continuous_events._daily_pnl_metrics` `sharpe_like`**
   (`continuous_events.py:1025`, assignment at `:1037`). WAS:
   `statistics.mean(pnl) / statistics.pstdev(pnl) * (365 ** 0.5)`
   (population std, ddof=0; gated on `len(pnl) > 2`). NOW:
   `sharpe = annualized_sharpe(pnl)`. Change at this site: `ddof 0 -> 1` AND
   `sqrt(365) -> sqrt(365.25)`. This helper is the single source of truth for the
   continuous additive summary (`_additive_summary` now delegates to it — the old
   duplicate `pstdev/sqrt(365)` copy in `_additive_summary` was deleted, see
   code-quality-6 note in the diff).
3. **`continuous_forward_replay` `forward_sharpe`**
   (`continuous_forward_replay.py:367`, emitted at `:379`). WAS:
   `rets.mean() / rets.std() * (365.25 ** 0.5)` over observed rows with NumPy
   default `std()` (ddof=0). NOW: `annualized_sharpe(series, ann_days=ANN_DAYS)`
   with `ANN_DAYS = 365.25` (`continuous_forward_replay.py:95`), computed on the
   **gap-filled calendar series** the forward-replay-2/6 fix already introduced
   (so the Sharpe and the MAR share the same calendar basis). Change at this site:
   `ddof 0 -> 1`, annualizer already 365.25, plus the calendar-basis change that
   is owned by the forward-replay-2/6 fix (not by this receipt).

Test: `tests/test_audit_fix_b04.py:99-100` asserts the shared convention is
`ddof=1` (sample std) with `sqrt(365.25)`.

## Hypothesis

Three independently-written Sharpe formulas — `sqrt(365.0)` + ddof=1 (long),
`pstdev` (ddof=0) + `sqrt(365)` (continuous additive), NumPy default `std`
(ddof=0) + `sqrt(365.25)` (forward replay) — made the Sharpe reported in the
long, continuous, and forward reports **non-comparable for the same ledger**. A
reader comparing "the continuous Sharpe" to "the forward Sharpe" was comparing
different estimators (population vs sample variance, and two different
annualizers), not different performance. A single canonical convention makes
those numbers mean the same thing.

## Predicted direction + magnitude

- **No effect on returns, MAR, drawdown, trade counts, or entries** — these do
  not depend on the Sharpe formula; they must be byte-for-byte unchanged at each
  site (the Sharpe change is purely a summary statistic).
- **Sharpe Δ (per site): small, deterministic, mechanism-explained.**
  - ddof `0 -> 1` (continuous additive, forward replay): sample std `>` population
    std, so Sharpe is scaled **down** by `sqrt((n-1)/n)` — a fraction of a percent
    for the multi-hundred-row daily series here, larger only for short series.
  - annualizer `sqrt(365) -> sqrt(365.25)` (long, continuous additive): Sharpe
    scaled **up** by `sqrt(365.25/365) ≈ 1.00034` — sub-permille.
  - Long site sees only the annualizer nudge (ddof was already 1).
  - Forward-replay also moves with the calendar-basis change owned by
    forward-replay-2/6 (out of scope of this receipt's effect claim).
- **Failure mode if hypothesis wrong / what would falsify:** any change to a
  return, MAR, drawdown, or trade-count number at a site; OR a Sharpe shift larger
  than the ddof+annualizer mechanism predicts (which would mean the reroute
  silently changed the input series rather than just the estimator); OR the three
  reports still disagreeing on Sharpe for one shared ledger after the change.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset) — read-only confirm; no sweep,
  no new parameter mined.
- [ ] binance_full_pit (per-venue working dataset) — read-only confirm; same.
- [x] forward demo/paper (always, by virtue of being live) — only the
  `forward_sharpe` *display* in the readiness/forward-replay report changes; no
  orders, no ledger writes.

This is a metric-definition change, not a parameter sweep over the working
datasets, so it dilutes no OOS surface. The confirming run reads existing ledgers
and recomputes the summary; it does not regenerate per-venue working data.

## Decision rule (a priori)

This is a reported-metric consolidation, so the accept/reject rule is internal
consistency, not a Tier MAR/Sharpe edge bar:

1. **ACCEPT** iff, on one shared ledger run through all three report paths
   (long / continuous additive / forward replay): every site's Sharpe equals
   `annualized_sharpe(<that site's daily series>)` to floating tolerance
   (`np.allclose`), i.e. all three reports report the SAME convention; AND every
   non-Sharpe number (return, MAR, drawdown, worst-day, trade count) is unchanged
   vs the pre-change code at each site (numerical equivalence within tight
   tolerance per AGENTS.md — `np.allclose`, NaN/None positions matching).
2. **REJECT** if any non-Sharpe metric moves, OR a site's Sharpe diverges from the
   shared helper, OR the per-site Sharpe shift exceeds the ddof+annualizer
   mechanism above (evidence the reroute changed the underlying series).

Explicit note on the Tier-3 bar: the **residual Sharpe `>= +0.3`** gate in
STATE.md "Decision Rules / Tier 3" is now measured on the **ddof=1, sqrt(365.25)**
convention. That gate is real-money-only and currently unmet; it is not loosened
or rescued by this change (per Non-Negotiable #5). Any future Tier-3 residual
Sharpe must be read against this convention.

## Run command

```bash
# Confirm: same ledger through all three report paths reports one Sharpe convention,
# and no non-Sharpe metric moved. Read-only against existing per-venue roots.
.venv/bin/python -m pytest -q tests/test_audit_fix_b04.py
# Cross-report consistency check against a shared ledger (long / continuous / forward):
.venv/bin/python -m liquidity_migration continuous-forward-readiness --paper-only
```

## Post-run results

(fill in after the confirming run; include report paths + the commit SHA at which
the change lands. Working tree is uncommitted; git HEAD at filing is `5dd4e12`.)

## Verdict

(pending the confirming run — accept only if all three reports share the
`annualized_sharpe` convention for one ledger and no non-Sharpe metric moved.)
