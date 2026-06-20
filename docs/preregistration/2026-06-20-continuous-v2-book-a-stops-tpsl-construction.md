# Construction + Verdict: Continuous V2 Next-Level — Problem Book A (Real Stops / TPSL)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction + verdict
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Engine: `docs/preregistration/2026-06-20-continuous-v2-intrabar-execution-engine-construction.md`
Run label: `exploratory` (per-trade realized-PnL screen). **Verdict: NO both-venue candidate. Book A stop family CLOSED.**

## Objective

Test whether real (catastrophic) stops can cut tail losses on the continuous fade
book without deleting the trades that ride to the 12% TP — the question the 1h
engine could not answer honestly (it can't resolve same-bar stop-vs-TP order). Now
answerable with the validated Wave-2 1m intrabar engine.

## Method

`scripts/continuous_v2_book_a_stops.py` re-resolves every `V2_CONTROL` short trade
on the Wave-1 1m cache under:
- **A1** immediate catastrophic stops: stop_loss_pct ∈ {15,20,25,30,40}% (adverse-first 1m first-touch).
- **A2** delayed-arm stops: the same grid but the stop only arms after 6h / 12h (avoids cutting the initial mean-reversion window).
- **A7** hash null per policy: exit the SAME fraction of trades early as the real stop, but at a HASH-chosen minute and the real path price there — matches early-exit frequency while destroying the "cut the worst excursion" mechanism.

Metric: realized-PnL **MAR proxy** (per-exit-day book returns → compounded equity →
max drawdown), plus worst-day and drawdown deltas, on BOTH venues. Control (stop=0,
TP12) reproduces the frozen exit: recon vs ledger **0.00002 (bybit) / 0.00000 (binance)**.

Decision rule (from the plan): a candidate must improve MAR vs control AND beat its
hash null on BOTH venues, without a venue split.

## Results (full run, 2026-06-20; control MAR proxy bybit 6.15 / binance 4.32)

A1 — immediate stops (ALL fail both venues):

| stop | bybit Δmar | binance Δmar | beats hash | Δdd |
|------|-----------:|-------------:|:----------:|-----|
| 40% | −2.06 | −1.92 | no | worse |
| 30% | −3.68 | −3.00 | no | worse |
| 25% | −4.66 | −3.00 | no | worse |
| 20% | −4.99 | −2.97 | no | worse |
| 15% | −5.01 | −3.09 | no | worse |

A2 — delayed-arm (less damaging; still no both-venue winner):

| policy | bybit Δmar | binance Δmar | bybit beats hash | binance beats hash |
|--------|-----------:|-------------:|:----------------:|:------------------:|
| stop40_arm12h | −0.12 | −0.31 | yes | no |
| stop20_arm12h | −1.43 | −1.24 | no | no |
| **stop15_arm12h** | **+0.75** | **−0.04** | yes | yes |
| stop15_arm6h | −2.11 | −1.26 | no | no |

**Both-venue winners: NONE.**

## Verdict — falsifier-backed both-venue negative

- **Stops do not help the fade book**, even with proper 1m path fidelity. The fade
  shorts names that popped; the adverse excursion (price continuing up) is exactly
  where mean-reversion to the −12% TP is most likely, so a stop placed there cuts
  the trades that carry the edge. Immediate stops lose 2–5 MAR on both venues and
  do not beat a random-time hash null — i.e. exiting AT the adverse excursion is no
  better (worse) than exiting at a random time.
- **Delaying the stop only helps by making it almost never fire.** As arming is
  pushed to 12h and the stop widened to 40%, the damage shrinks toward zero — i.e.
  the best stop is one that does nothing. The single positive cell, bybit
  `stop15_arm12h` (+0.75 MAR), is **−0.04 on binance**: a venue split, killed by the
  both-venue rule (and +0.75 is within the proxy's noise). This mirrors the F2 TP12
  result — a Bybit-only exit lead is at most an operator-gated venue-policy /
  forward-shadow item, NOT a frozen-object change.
- **Stops mostly worsen realized drawdown** (the stop-losses cluster on bad days),
  so they fail their own stated purpose (tail reduction) on this book.

## Falsifiers applied (all kill the arms)

- Hash null (A7): the few policies that beat it are venue-split or within noise.
- Both-venue: none.
- Drawdown: stops do not reliably reduce realized drawdown.
- Mechanism matches the ledger: damage scales with stop-hit-rate (more stopping →
  more edge destroyed).

## Honest caveats / scope

- This is a per-trade realized-PnL SCREEN: no daily rebalance/hedge re-solve. The
  conclusion is a clean NEGATIVE, which a screen can establish (a survivor would
  have needed full-ledger `build_full_ledger` validation; there is no survivor).
- A2 used the new byte-identical `stop_arm_after_ms` resolver param (unit-tested).
- Bybit max_hold exits carry the documented ~0.05% 1h-root-vs-1m-close nuance;
  immaterial to a both-venue MAR-delta of this size.

## No real-money / promotion claim

`REAL_MONEY` stays false. Book A stop family is closed; the bybit `stop15_arm12h`
near-miss is recorded as an operator-gated venue-policy lead only.
