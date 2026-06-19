# Continuous V2 Exit-Alpha Phase 2 — Construction (Pre-Registration)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md` (Problem Book F).
Prior: `docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-verdict.md` (simple cuts closed).
Scope: CONTINUOUS demo/paper research, **both venues**, NO-ORDER shadow re-simulation of the exit
policy on the historical path. EXPLORATORY: any winner changes a frozen-v2 parameter
(`take_profit_pct` / `hold`), which voids the frozen forward ledger and needs operator approval +
a fresh forward clock; it is NOT auto-accepted. No real-money claim.

## Inspiration from the failure

The simple-cut shadow proved every blanket early exit destroys the edge by cutting the 150–420
trades that ride to the +10% TP. So exit alpha must be **profit-conditional** (never exit a loser
early) or **symmetric** (never cut an eventual TP winner short of its target). The book's PnL is the
~35% TP winners; the 65% time-exits are net-negative and ~28% give back >3% MFE. The open question:
is the frozen (10% TP, 24h hold) the right *point*, or is a different exit policy dominant on both
venues?

## Method

Re-simulate the exit policy for each V2_CONTROL short trade on the causal `klines_1h` path from entry
out to 72h (both venues). The 10% TP fires intrabar when `low <= entry*(1-T)`; time exit at the hold
cap. Validate the re-sim reproduces the recorded control at (T=10%, hold=24h) (recon error ~0).
Per-trade contribution = raw_short_return * notional_weight + cost + funding (first-order; no rebalance
re-solve — stated limitation). Report the FULL grid (no cherry-pick); a result counts only if it
improves the per-trade book contribution on **both venues** vs the (10%, 24h) control.

## Pre-registered policies (each a single declared mechanism — not stacked)

- **TP sweep** (hold 24h): T ∈ {4%, 5%, 6%, 8%, 10%(control), 12%, 15%}. Profit-conditional: a lower
  T captures reversion before giveback but caps winners; never cuts a loser early.
- **Hold sweep** (TP 10%): hold ∈ {24(control), 36, 48, 72}h. Symmetric: gives slow reverters more
  time to reach TP; never cuts a winner.
- **Time-decaying TP** (hold 24h): T(h) = 10% for h<12, then linearly decays to {6%, 4%} by 24h.
  Takes a declining profit as the reversion edge ages — profit-conditional, never cuts a loser, and
  the full 10% TP is still active early so it does not cut early winners.
- **Partial / two-stage**: realize fraction f∈{0.5} at T1∈{5%,6%}, ride the remaining (1−f) to the
  10% TP / 24h. Captures giveback on part of the position while keeping the upside on the rest.
- **Vol-scaled TP** (hold 24h): T_i = clip(k * trailing_168h_vol_i, 4%, 15%) using the causal
  entry-time `path_rv_168h` from the almanac (high-vol names get a wider target). Phase 2b if 2a warrants.

Negative control: the random-exit null from the Book F shadow already showed blanket randomness is
strongly negative; for the TP/hold grid the (10%,24h) recon IS the control and the grid is reported in
full so a single lucky cell cannot be cherry-picked.

## Pass / falsifier

- A policy is a **shadow candidate** only if it improves per-trade book contribution on BOTH venues
  vs (10%,24h), the improvement is not concentrated in one month/regime, and it is expressible as a
  causal live rule. Even then it is exploratory pending forward demo.
- FALSIFY a policy if it improves one venue and worsens the other, or the both-venue gain is within
  noise / one-month-driven, or it relies on cutting eventual TP winners.
- If NO policy dominates on both venues, the frozen (10% TP, 24h hold) is confirmed near-optimal and
  exit alpha is closed for this mechanism family.

## Limitation

Per-trade path re-sim on `klines_1h`; does not re-run the daily vol-target rebalance or re-solve
concurrency/cooldown under changed exits. It is the gate that decides whether a full lifecycle exit
A/B (engine) + forward shadow is worth building for any dominant policy.
