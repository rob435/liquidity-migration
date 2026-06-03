# long-sleeve-5 / long-sleeve-6 — cross-sleeve margin budget + same-symbol reservation registry

**Status:** DESIGN ONLY (not built). Two coupled cross-sleeve defects on the ONE netted demo
account run by THREE separate OS processes (short `event_demo`, long `long_native_event_demo`,
continuous `continuous_demo`) plus the `ws_risk` reconcile daemon. This is genuinely
operator-gated — see "Why not auto-built" — so it is delivered as a complete spec the operator
can implement when they pick the budget numbers and are ready to coordinate the 3-process change.

## The two defects
- **long-sleeve-5 (margin starvation):** every sleeve sizes purely off total `equity_usdt` with
  NO account-wide initial-margin (IM) headroom check. The long book is the worst offender
  (per-position notional = gross_exposure/max_concurrent × notional_multiplier = 100%-of-equity
  at lev 10 ⇒ 10% IM × 10 slots = 100% of account margin fully loaded). Short + continuous draw
  IM off the same wallet, so a loaded long book can starve a sibling's `place_order` (or, on
  UNIFIED margin, raise netted-book liquidation risk). Nothing bounds aggregate IM today.
- **long-sleeve-6 (same-minute same-symbol race):** cross-sleeve same-symbol exclusion is
  enforced ONLY via the lagging venue snapshot (`_active_position_by_symbol`). Two processes that
  wake in the same ~60s window both read "no position on SYMBOL" before either fill lands → both
  enter → the disjoint-sleeve invariant is defeated. No shared in-process state exists (separate
  processes).

## Design — ws_risk owns ONE shared on-disk control table
`ws_risk` is the only component that already reads all three sleeve roots, so make it the OWNER
of the shared cross-sleeve state, persisted in one new dataset every sleeve consults read-only.

### New dataset `cross_sleeve_account_state` (register in storage.DATASETS + DATASET_KEYS)
Single control row per account key (`DATASET_KEYS = ("account_key",)`), rewritten by ws_risk each
reconcile pass:
- `account_key: str` — the netted account id.
- `equity_usdt: float` — last-known wallet equity.
- `account_im_used_pct: float` — aggregate IM used = Σ(per-trade initial_margin_usdt) / equity,
  computed by ws_risk from `state.all_trades` (already concats all 3 sleeves) over OPEN trades.
  Per-trade IM = `notional_usdt / leverage` (leverage from the sleeve's config, or a per-trade
  `leverage` column if present).
- `im_used_pct_by_sleeve: dict[str,float]` — the same, split by `sleeve`.
- `margin_budget_pct_by_sleeve: dict[str,float] | null` — the PRE-REGISTERED IM ceiling per sleeve
  (operator-owned, e.g. short 0.35 / long 0.45 / continuous 0.20). `null` = no clamp (legacy).
- `reservations: list[{symbol, sleeve, trade_id, reserved_at_ms, ttl_ms}]` — the registry below.
- `updated_at_ms: int`.

### long-sleeve-5 consumption (each sleeve, read-only, safe-by-default)
Before sizing new entries, a sleeve reads the control row (swallow read errors → no-op/legacy). If
`margin_budget_pct_by_sleeve[sleeve]` is set and `im_used_pct_by_sleeve[sleeve]` is at/over it,
clamp `max_new_entries` for the pass so the sleeve cannot exceed its IM budget. Default (no budget
row, or null split) = no clamp = byte-identical legacy behavior. Fail-safe: the clamp only ever
SHRINKS a sleeve's new entries; it never upsizes, never touches another sleeve's positions.

### long-sleeve-6 reservation protocol (the 3-process part)
A sleeve about to submit an entry on SYMBOL first acquires the dataset lock, re-reads the control
row, and — iff no active (non-expired, non-released) reservation on SYMBOL by ANOTHER sleeve and no
live venue position — WRITES a reservation `(symbol, sleeve, trade_id, now, ttl=~3 cycles)` then
submits. Sibling sleeves treat an active reservation as "symbol taken." ws_risk GCs reservations
each pass: drop expired ones and any whose `trade_id` is now closed/filled-and-reconciled. The
lock + re-read-under-lock makes the claim atomic across processes, closing the same-minute race
the venue snapshot cannot.

## Why not auto-built
1. **Operator-owned numbers:** the `margin_budget_pct_by_sleeve` split is a risk-allocation
   decision that must be pre-registered (it changes which entries each sleeve takes when loaded).
2. **Unvalidatable solo:** the reservation protocol is a multi-process concurrency invariant; it
   cannot be validated without the 3 live daemons (a unit test can't reproduce the cross-process
   race), and the IM-computation correctness needs the live account's margin model.
3. **Reconcile-authority risk:** ws_risk would now WRITE a new control dataset every pass and own
   IM math — a material change to the sole reconcile authority the operator should review.
4. **No dormant scaffolding:** the read-only "safe subset" (long clamp only) is inert until the
   ws_risk writer + budget numbers exist, so shipping it alone adds complexity for zero function —
   against the project's simple-over-guardrails preference.

## Pre-registration template (required before deploy)
`docs/preregistration/long-sleeve-5-6-margin-budget-<date>.md`: the IM split numbers + hypothesis
(prevents margin starvation without changing per-trade alpha), predicted (no sleeve exceeds its IM
ceiling when loaded; entry counts unchanged below the ceiling), falsifier (a sleeve trades through
its ceiling, or below-ceiling entry counts drop), roots (forward demo/paper), decision rule.

## Implementation checklist (when the operator proceeds)
1. Pre-register the budget split. 2. Register `cross_sleeve_account_state` in storage. 3. ws_risk:
compute IM-used + write the control row each pass; GC reservations. 4. All 3 sleeve cycles: read
the budget clamp (long first — worst offender). 5. All 3 sleeve cycles: reservation acquire-under-
lock before entry. 6. Tests: IM-used computation, budget clamp shrink-only, reservation atomicity
(simulated lock contention), GC of expired/closed reservations. 7. Forward-demo re-validation.
