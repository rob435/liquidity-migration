# Phase 7 — pre-2023 OOS gate (pre-registration)

**Date:** 2026-05-27
**Stage:** pre-registered, not yet run (waiting on finalists from
upstream phases).
**Parent plan:** [2026-05-27 multi-phase research plan](2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md)
**Phase label per plan Appendix B:** `candidate` (Phase 7-passing
finalists are STILL candidates, not alpha — they go to ≥30-day forward
demo before any mainnet consideration).

## Purpose

The mandatory final OOS gate. Every finalist from any prior phase
(0, 1's non-biased 764 cells, 2, 3, 4, 6) must clear the dedicated
pre-2023 data roots on **all three** sub-period thirds × **both**
venues × sign-consistency × DD bound. This is the only truly clean
evidence surface left to this strategy.

## Hard deadline

**2026-06-15.** If by this date no Phase 7 candidate has emerged AND
passed the OOS gate, the inverse-direction edge hypothesis (H2-H4) is
**rejected** by fiat and the program closes with a documented null. The
existing promoted profile stays as-is. No "let's try one more thing"
escape.

## Data roots (operator-confirmed present 2026-05-27)

- Bybit pre-2023 OOS root (location TBD; operator confirmed exists on
  workstation — to be resolved at dispatch time via the `data_roots`
  MCP tool or operator hand-off).
- Binance pre-2023 OOS root (same).

If at dispatch time either root is missing OR not current with the
v5-listing supplement + Int64 fix, the operator is consulted before
proceeding; rebuild is ~6h of downloads.

## Window

Each pre-2023 root's full available range, split into **3 non-overlapping
sub-period thirds**. Exact dates depend on root coverage but
historically the program treats pre-2023 as ~Bybit-2021-04 → 2022-12
(~21 months, three 7-month thirds) and ~Binance-2019-09 → 2022-12
(~40 months, three ~13-month thirds).

## Cells

For each FINALIST that emerged from any prior phase × 2 venues × 3
sub-period thirds + a baseline reference per (venue × sub-period):

- finalist runs: `N_finalists × 2 venues × 3 sub-periods × 1 cell = 6N`
- reference runs: `2 venues × 3 sub-periods = 6`
- worst case (6 finalists, the FDR ceiling = 3 from Phases 2-4 + 3
  from Phase 6): `6 × 6 + 6 = 42 runs`

Each finalist's cell uses the finalist's exact parameter set
(direction, threshold, exits if Phase 3 produced any, etc.) from its
producing phase's verdict.

## Decision rule — Phase 7 OOS gate (final, no loosening)

A Phase 7 candidate becomes a **promotable finding** only if **ALL** of:

- sharpe-like **> 0** on **both** pre-2023 venues, **all three**
  sub-periods (9 paired comparisons, all positive); AND
- DD < **50%** on both venues, all three sub-periods; AND
- sign-consistent edge direction vs the finalist's in-sample direction,
  both venues, all thirds; AND
- per-sub-period trade count ≥ **20** on Bybit (≥15 on Binance).

A Phase 7 candidate is **rejected** if ANY of:
- single sub-period DD > 60%, OR
- sign flip between any two of {Bybit / Binance / sub-periods}, OR
- sharpe-like < 0 on any sub-period of either venue.

Inconclusive results (between candidate and rejected) are **closed-
rejected** — there is no Phase 8.

### Pass-and-promote ladder

Phase 7 PASS does NOT mean trade. It means:

  finalist → 30-day forward demo (reconciled vs same-config backtest)
           → operator review
           → mainnet consideration

The `paper_ready` label from `docs/backtesting_errors_we_never_repeat.md`
is the only ladder rung that touches real capital. Phase 7 PASS earns
`candidate` status, not `paper_ready`.

## Pre-commitments (read these before being tempted to bend a rule)

1. **No Phase 8.** A Phase 7 failure means CLOSED. We do not "try the
   cell on demo anyway" or look for an escape hatch. The committed
   close means the strategy stops at its current promoted profile.
2. **All-or-nothing OOS criteria.** A finalist that passes 8 of 9
   sub-period × venue combinations does NOT pass. The conjunctive rule
   is the rule.
3. **No window/data-root shopping.** The pre-2023 roots are the
   committed evidence surface. If a finalist fails on Bybit pre-2023
   but the operator is tempted to "try Binance 2018 too", that's an
   amendment to this doc with a dated entry — not a silent re-run.
4. **Forward-demo gating is non-negotiable.** A Phase 7 PASS finalist
   still goes to ≥30-day forward demo before any mainnet conversation.
   The current promoted profile (which is on forward demo) does not
   get "replaced" mid-stream — the new finalist gets ITS OWN forward
   demo slot first.
5. **Pre-2023 root re-use risk acknowledged.** The pre-2023 roots were
   the original "strategy fails pre-2023" kill-shot evidence. Re-using
   them dilutes their evidentiary value. The conjunctive rule above
   (all 3 sub-periods × both venues) is the mitigation; the forward-
   demo step is the secondary mitigation.

## Operator-confirmed data state (as of 2026-05-27)

The operator confirmed at session start that the pre-2023 Bybit AND
Binance OOS roots are already built on the workstation. Phase 7
dispatch does not need to wait on data downloads.

## Forward pointer

- Phase 7 PASS for one or more finalists → operator decides which to
  forward-demo first. The current promoted profile is NOT replaced
  until a PASS finalist clears 30-day forward demo.
- Phase 7 FAIL for every finalist → program null result, documented
  honestly. The existing promoted profile stays as-is. No "we spent
  the compute so we have to ship something" pressure applies.
- Phase 7 dispatch is triggered as soon as ANY finalist emerges from
  any prior phase. We do NOT batch finalists — each gets pushed to
  Phase 7 as soon as it's identified, so the deadline (2026-06-15)
  has the most runway.
