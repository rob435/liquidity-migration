# Pre-registration — long-sleeve-5/6: cross-sleeve margin budget + same-symbol reservation registry

- **Date:** 2026-06-03
- **Status:** BUILT on branch `audit/fixes-2026-06-03` (foundation → ws_risk owner → 3 sleeves),
  NOT deployed. **NO-OP until the operator seeds a budget split** (and the reservation registry
  is live as soon as ws_risk writes the control row). Forward-demo validation required before
  treating it as accepted.
- **Run class:** risk-allocation / cross-sleeve safety. Selection-affecting (a clamp changes which
  entries a sleeve takes when loaded), so it is pre-registered.

## What it does
On the ONE netted demo account run by 3 separate processes, `ws_risk` (sole reconcile authority)
each pass recomputes aggregate initial-margin-used per sleeve and rewrites a single shared control
row (`cross_sleeve_account_state`). Each sleeve, before sizing, reads that row and SHRINKS its
new-entry count if it is at/over its pre-registered IM ceiling (long-sleeve-5), and on a REAL submit
claims each symbol through an under-lock reservation so two processes can't both enter the same
symbol in the same minute (long-sleeve-6). All writes are under-lock read-modify-write (serialized).

## The numbers to set (OPERATOR DECISION — this is the pre-registered choice)
Seed the per-sleeve IM ceilings (fractions of equity) via:
```python
from liquidity_migration import cross_sleeve
cross_sleeve.seed_margin_budget(
    "<SHORT/AUTHORITY ROOT e.g. data/bybit-demo-event>",
    {"short": 0.35, "long": 0.45, "continuous": 0.20},  # <-- the pre-registered split; sum <= 1.0
    now_ms=<int(time.time()*1000)>,
)
```
Suggested starting split: **short 0.35 / long 0.45 / continuous 0.20** (sum 1.0; the long sleeve is
the worst offender so it gets the largest share). Adjust to your risk allocation. `None` clears it
(back to no-clamp). The split is preserved by ws_risk (never overwritten); only the operator sets it.

## Hypothesis / Predicted / Falsifier
- **Hypothesis:** bounding aggregate IM prevents one sleeve (esp. long, 10% IM × 10 slots = 100%)
  from starving the others' margin, without changing per-trade alpha below the ceiling.
- **Predicted:** no sleeve's IM-used exceeds its ceiling when the book is loaded; entry counts are
  unchanged BELOW the ceiling; same-symbol cross-sleeve double-entries drop to ~0.
- **Falsifier:** a sleeve trades through its ceiling, OR below-ceiling entry counts drop (the clamp
  is mis-triggering), OR two sleeves still enter the same symbol the same minute.

## Roots / decision rule
Forward demo/paper only. Accept iff, after the budget is seeded, the cycle telemetry
(`skipped_*_margin_budget`, `skipped_*_reservation`) shows the clamp/registry firing ONLY at/over the
ceiling and never below, and the per-sleeve `im_used_pct_by_sleeve` stays within its ceiling. Do NOT
treat the feature as validated until observed live (the 3-process race cannot be reproduced offline;
unit tests pin the lock-atomicity + the budget/IM math + the under-lock RMW that preserves a
concurrent claim).

## Deploy notes (safe-by-default)
- The whole feature is a **NO-OP until the budget is seeded** AND ws_risk writes the control row;
  every read fails open (a missing/torn row → no clamp, claim grants). So merging the code without
  seeding changes nothing.
- The reservation registry becomes active as soon as ws_risk writes the row (it always does once
  deployed). The claim fails open, so it can never halt a legitimate entry.
- IM accuracy: per-trade IM prefers a stored `initial_margin_usdt` (long), else notional/leverage
  (sleeve config or per-trade); unknown leverage defaults to a CONSERVATIVE 1.0 (over-counts → clamps
  EARLIER, never later). The short sleeve's leverage comes from ws_risk's own demo config; long &
  continuous carry their own IM/leverage on the trade row.
- Tests: `tests/test_liquidity_migration_cross_sleeve.py` (12 — clamp shrink-only, fail-open reads,
  under-lock RMW preserving a concurrent reservation, GC, claim atomicity incl. a 2-thread real-lock
  contention test, IM aggregation) + the per-sleeve no-op-default coverage in the existing cycle tests.
