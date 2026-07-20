# R3a book-level daily loss budget — frozen A/B activation design

Registered 2026-07-20 (tail-risk program P1.2), before any forward outcome
of the rule exists. This freezes the design; **activation is a separate
operator decision** with a five-line note and a recorded change point.
Until that decision the rule runs as a SHADOW only
(`liquidity_migration/loss_budget_shadow.py` — log-only, acts on nothing;
`scripts/loss_budget_shadow_check.py` is the staged read-only oneshot).
Lane-1 replay receipts: `reports/tail-risk-program/p12-r3a-loss-budget-lane1-2026-07-20/`.

## Frozen rule

- **Trigger:** cumulative realized book P&L within the current UTC day
  (all canonical journal PNL rows — component closes, netted reductions,
  funding settlements — ordered by exchange timestamp) first reaches
  **−1.5% of the 10,000 USDT capital reference (−150 USDT)**. Threshold
  derivation: sleeve kill criterion K1 (−5%/epoch) ⇒ a daily budget near
  −1.5% binds well before the sleeve kill. Frozen; not tunable in flight.
- **Action (arm B only, once activated):** block all NEW entries for the
  remainder of the UTC day on the paper account. Entry-side only: existing
  positions, exits, protections, and the hedge are untouched. Reset at the
  next UTC midnight. A later intraday recovery does not un-trip.
- No other action. The proposal's "halve gross" variant remains a logged
  diagnostic (−0.75% crossing) and is NOT part of the activated arm.

## Frozen A/B assignment

- **Unit:** UTC calendar day. **Assignment:** proleptic-Gregorian ordinal
  parity of the UTC date (`date.toordinal()`): **odd → arm A (governor
  off), even → arm B (governor on)** — deterministic, venue-independent,
  pre-committed, mirroring the passive-execution experiment's parity
  pattern.
- **Surface:** the paper stream only. Demo behavior unchanged.
- **Blinding/no-peek:** the shadow log records both arms' would-do from
  day one; the activated experiment changes behavior only on arm-B days.

## Frozen metrics (insurance grading, item 27)

Per arm, from the canonical paper journal: trigger days and first-breach
times; false-trip rate (day recovers above threshold after breach);
continuation depth after breach; blocked-entry count (arm B) vs
would-have-blocked (arm A); premium = realized net of arm-A entries taken
after a would-have-blocked time (the uncensored counterfactual the parity
design exists to provide) next to arm-B forgone entries; book ES95/ES99 of
daily net per arm. **Not graded on return improvement.**

## Pre-committed kill rule for the experiment itself

- **X1 — premium runaway:** after ≥ 28 arm-B days, if arm-A
  after-breach-time entries earned a cumulative net > +2% of the capital
  reference while arm-B blocked-day avoided losses are ≤ 0, stop the
  experiment (governor off both arms) and record the negative result.
- **X2 — trigger famine:** if after 120 days fewer than 6 breach days
  occurred across both arms, close as insufficient-sample (the budget never
  binds at paper scale); re-registration required to retry.
- **X3 — integrity:** two consecutive weekly checks with journal
  verification failures pause the experiment pending operator review.
- Checked weekly alongside `scripts/ops.sh kill-criteria` (shadow-level;
  no runtime authority).

## What this registration is not

Not an activation; not a demo or mainnet change; not a return-improvement
claim. The Lane-1 replay's barebones-era net gain is context only. Real
money remains a separate, unopened door.
