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

## STATUS 2026-06-03 (post adversarial round-6): BUDGET OFF — clamp NOT wired, deferred
The dynamic equal-split below was BUILT + tested, then an adversarial verifier (round 6) showed an
**equal 1/n split STARVES the over-subscribed sleeves**: long alone wants ~200% IM (10x leverage x
10x notional ⇒ 20% of equity per position), short ~50%, continuous ~25% — the three combined want
~275% of ONE netted account, so any ≤100% budget throttles someone, and an equal third clamps long to
~2 of its 5–10 positions and short to ~8 of 12 (this is literally the falsifier below). **Operator
decision: ship with the budget OFF** (ws_risk writes IM/equity + GCs reservations only; the clamp
stays a no-op, budget None — the verified pre-this-turn behavior). The building blocks
(`equal_split_budget`, `ws_risk._active_sleeves`, the `sleeves.env` EnvironmentFile, the
`write_account_state` budget arg) remain in place + tested, ready to wire a **sleeve-WEIGHTED** split
once chosen deliberately. The reservation registry stays active (separate, fail-open). DO NOT wire an
equal split as-is.

## The (DEFERRED) split equation — DYNAMIC EQUAL SPLIT
If/when wired: ws_risk computes the split every reconcile pass as an EQUAL division across the ACTIVE
sleeves —

> **budget(sleeve) = 1 / n_active**  (3 active → 0.333… each, 2 → 0.5 each, 1 → 1.0)

`equal_split_budget()` in `cross_sleeve.py` is the equation; `ws_risk._active_sleeves()` is the
denominator = sleeves that are BOTH owned (root configured on the risk unit) AND enabled (the
`SHORT_SLEEVE`/`LONG_SLEEVE`/`CONTINUOUS_SLEEVE` kill-switch in `deploy/sleeves.env`, now loaded as
the risk unit's `EnvironmentFile`; unset ⇒ on). So the split **self-adjusts the instant a sleeve is
toggled on/off** — no operator reseed. `seed_margin_budget()` remains for tests/manual override but
is overwritten by ws_risk on the next pass; the live budget is owner-computed.

**Buffer note:** the equal split sums to EXACTLY 1.0 (n × 1/n), i.e. the per-sleeve IM ceilings
together permit up to 100% of equity as initial margin (no free-margin liquidation buffer at the
worst case where all sleeves are simultaneously maxed). With leverage this is a large notional. If a
buffer is wanted, scale the equation by a factor <1 (e.g. 0.8/n each → sums to 0.8); this is a
one-line change to `equal_split_budget`. Left at 1.0 per the operator's `100/n` instruction.

## Hypothesis / Predicted / Falsifier
- **Hypothesis:** an equal IM ceiling prevents one sleeve (esp. long, 10% IM × 10 slots = 100%)
  from starving the others' margin on the shared netted account, without changing per-trade alpha
  below the ceiling.
- **Predicted:** no sleeve's IM-used exceeds 1/n_active when the book is loaded; entry counts are
  unchanged BELOW the ceiling; same-symbol cross-sleeve double-entries drop to ~0; the split tracks
  the kill-switch (kill a sleeve → survivors' ceilings rise on the next pass).
- **Falsifier:** a sleeve trades through its 1/n ceiling, OR below-ceiling entry counts drop (the
  clamp is mis-triggering), OR two sleeves still enter the same symbol the same minute, OR the split
  fails to re-divide when a sleeve is toggled.

## Roots / decision rule
Forward demo/paper only. Accept iff the cycle telemetry (`skipped_*_margin_budget`,
`skipped_*_reservation`) shows the clamp/registry firing ONLY at/over the 1/n ceiling and never below,
the per-sleeve `im_used_pct_by_sleeve` stays within 1/n_active, and the split re-divides on a
kill-switch toggle. Do NOT treat the feature as validated until observed live (the 3-process race
cannot be reproduced offline; unit tests pin the equation, the lock-atomicity, the budget/IM math, and
the under-lock RMW that preserves a concurrent claim).

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
