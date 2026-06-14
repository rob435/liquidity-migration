# Pre-registration: Compounded (not additive) continuous comparator

**Date:** 2026-06-14
**Author:** Claude (continuous reconciliation audit loop)
**Stage:** run-pending
**Findings:** metrics-2, reconciliation-2
**Approval:** operator-approved 2026-06-14 (audit flagged-items answers). Change is
already staged/uncommitted in the working tree; this receipt precedes the
confirming run and is committed in the same PR as the code change.

## What's changing

`liquidity_migration/reconciliation._calendar_metrics` now computes the
standalone-continuous comparator's equity, drawdown, and MAR by **compounding**
the daily fractional returns (`equity *= 1.0 + ret`, peak-relative drawdown)
instead of additively **summing** them (`equity += ret`, absolute drawdown). The
additive read is retired.

## Exact files / knobs touched

- `liquidity_migration/reconciliation.py`, function `_calendar_metrics`
  (the staged diff lands at the `_calendar_metrics` body; see the `@@ -1027,9
  +1144,9 @@` hunk and the explanatory comment block inserted just above the
  `equity = 1.0` initializer, around line 1133 in the working tree).
- Two operative lines change inside the per-day loop:
  - `equity += ret` → `equity *= 1.0 + ret`
  - `dd = equity - peak` → `dd = (equity - peak) / peak if peak > 0.0 else 0.0`
- Downstream of those two lines, `total = equity - 1.0`,
  `annualized = total / years`, and `mar = annualized / abs(max_drawdown)` are
  unchanged in form but now read off the compounded curve. The per-day
  `curve` dicts (`equity`, `drawdown`) the report renders are likewise now
  compounded / peak-relative.
- No engine knob, no config value, no parameter is changed. This is a
  reporting/metrics-computation correction in the reconcile path, not a strategy
  or sleeve-engine change.

## Hypothesis

The continuous engine's own equity COMPOUNDS: `continuous_rebalance` persists
`rebalance_scaled_equity = prior * (1 + scaled_return)`
(`continuous_rebalance.py:687`, `equity *= 1.0 + basket_return`) and defines
drawdown peak-relative as `equity / equity.cum_max() - 1.0`
(`continuous_rebalance.py:715`; the same `equity / peak - 1.0` form appears at
`:236`). The comparator in `_calendar_metrics`, by contrast, was summing the
same fractional daily series additively and reporting an ABSOLUTE drawdown
(`equity - peak`). On a book returning ~70% over its window, an additive sum of
daily fractional returns diverges materially from the compounded product, so the
standalone-continuous `total_return` / `max_drawdown` / `MAR` shown to the
operator disagreed with the engine's own curve for the SAME ledger. Compounding
the comparator makes the operator-facing read agree with the engine it sits
beside in the forward-readiness summary.

## Predicted direction + magnitude

- Reported continuous `total_return`: shifts from the additive sum toward the
  compounded product. On the ~70% case the additive comparator diverged
  ~25–30% from the engine's compounded equity; after the change the comparator
  should match the engine's `total_return` (within tolerance) on the same
  ledger.
- Reported continuous `max_drawdown`: shifts from absolute (`equity - peak`) to
  peak-relative (`(equity - peak)/peak`), matching the engine's definition.
- Reported continuous `MAR`: re-expressed off the corrected return and the
  peak-relative drawdown, so it should match the engine's own MAR within
  tolerance. **MAR is STATE.md's named primary forward arbiter** — so this is a
  CORRECTED READ of an existing number, not the manufacture of a new alpha
  number.
- Trade count Δ: 0. No trades, ledgers, selections, or engine state change; only
  the comparator's summary statistics over an unchanged daily-return series move.
- Failure mode if hypothesis wrong: the compounded comparator does NOT converge
  to the engine's own equity-curve metrics on the same ledger (i.e. a residual
  gap remains beyond float tolerance), which would mean the divergence was not
  purely additive-vs-compounded and a deeper reconciliation bug exists.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset) — NOT touched. No backtest
  sweep; the per-venue working datasets are not read or rewritten.
- [ ] binance_full_pit (per-venue working dataset) — NOT touched.
- [x] forward demo/paper: read-only. The confirming check re-renders the
  continuous-vs-daily forward comparator over the SAME live continuous ledger;
  no orders, no demo/paper state change.

This is a reporting-correctness fix on the reconcile/comparator path. It does not
qualify as a per-venue parameter sweep, but MAR is the named primary forward
arbiter, so the corrected read is pre-registered before any decision binds on the
new value (per AGENTS.md and the in-code NOTE).

## Decision rule (a priori) / equivalence

On the SAME continuous ledger over the SAME calendar window:

- ACCEPT iff the compounded `_calendar_metrics` comparator equals the engine's
  own equity-curve metrics — `total_return`, `max_drawdown`, and `MAR` — within
  tolerance (`np.allclose` on the equity/drawdown curves, MAR agreement to the
  reported 6-dp precision). "The engine's own metrics" = the
  `rebalance_scaled_equity` / peak / `equity/peak - 1` curve produced by
  `continuous_rebalance` / `continuous_demo` for the same book.
- The retired ADDITIVE value is not a competing candidate: it was a wrong read
  and is dropped, not weighed against the compounded read.
- REJECT (and escalate) iff a residual gap beyond tolerance remains after
  compounding — that would indicate the comparator and the engine disagree for a
  reason other than additive-vs-compounded accounting.
- Because MAR is the primary arbiter (STATE.md Decision Rules; Tier-1/2/3 all key
  off MAR), no Tier promotion, demo-candidate, or real-money claim may cite the
  pre-change additive MAR. Tier gates remain unchanged; only the value fed into
  them is corrected. This change must not be used to move any cell across a Tier
  boundary — it is a correctness fix to the read, not new evidence.

## Run command

```bash
# Re-render the continuous-vs-daily forward comparator on the live continuous
# ledger and confirm the standalone-continuous block matches the engine curve.
.venv/bin/python -m pytest -q tests/ -k reconcil
bash scripts/reconcile.sh --sleeves continuous
```

## Post-run results

(fill in after the confirming run; include the comparator report path, the
engine equity-curve metrics it is checked against, the `np.allclose` /
MAR-agreement result, and the commit SHA at which the change lands. Working tree
is uncommitted on git HEAD `5dd4e12`.)

## Verdict

(pending confirming run — accepted | rejected | inconclusive, with a
one-paragraph why, per the equivalence rule above.)
