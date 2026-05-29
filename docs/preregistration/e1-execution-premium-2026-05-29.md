# Pre-registration: E1 — quantify the execution premium (fixed_delay vs promoted_quality_squeeze)

**Date:** 2026-05-29
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-pending
**Plan:** [research_plan_selection_execution.md](../research_plan_selection_execution.md) §E1

## What's changing

On the *same* daily liquidity-migration candidate pool and the *same* realistic
baseline (capped stop fills, `max_active=12`, full-PIT, both venues), vary **only**
the entry policy: `fixed_delay` (near-immediate, +1h) vs `promoted_quality_squeeze`
(wait for pop → giveback, "fade the fade"). The B−A delta is the **execution
premium** — how much of the daily strategy's alpha is the *execution* signal
(timing the confirmed fade) rather than the *selection* signal (which names).

This is not a parameter sweep over the daily space (out of scope per the plan). It
is a single, pre-committed two-arm contrast per venue, plus — only if the contrast
shows a premium — a small one-at-a-time knob characterization of B.

## Hypothesis

The liquidity-migration event is a **selection** signal (a candidate pool), not an
entry. The in-migrated flow continues *up* at the extremes before exhausting, so an
**immediate** short (arm A) is run over by the continuation; a **fade-confirmation**
short (arm B) waits that continuation out and enters on the giveback. Mechanism:
B should (a) drop the candidates that never give back (subset selection) and
(b) enter the survivors at a better (higher) short price (timing). If the thesis is
right, **B materially beats A on both venues**. If B ≈ A, the alpha is
**selection-only** and the fade-confirmation framing is not load-bearing — a real
result that re-points E2 toward selection refinement.

## Predicted direction + magnitude

- Per-venue total return: **B > A** on both venues; expect A (immediate entry) to be
  near-zero-to-negative net (it shorts into the continuation), B positive
  (bybit B should land in the ~+15% to +45% range — the existing 45 bps re-baseline
  put `promoted_quality_squeeze` at +37.8% bybit / −4.7% binance gross +16.1%; at the
  honest 15 bps here B should be ≥ those).
- Pooled MAR Δ (B−A): **> +0.1** if the execution premium is real.
- Sharpe Δ: B−A positive both venues.
- Trade count: A > B (B trades the confirmed-fade subset); both ≫ Tier-2 minimums
  (≥30 bybit / ≥20 binance) — re-baseline had 761 by / 477 bn at max3.
- **Failure mode / falsifier:** B ≈ A on either venue (no cross-venue premium), OR
  B's return ≤ 0 on a venue where A is also ≤ 0 (execution doesn't rescue selection),
  OR the premium reverses sign between venues. Any of these ⇒ "selection-only,"
  documented, and E2 pivots to selection refinement (not execution).

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset)
- [x] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (not touched by this backtest)

## Fixed configuration (both arms, both venues — held constant)

- Window: `--start 2023-04-01 --end 2026-05-28` (matches the re-baseline, 1153 days)
- Stop fill: engine default `bar_extreme_capped` (10% cap) — realistic bad-case
- Concentration: `--max-active-symbols 12` (research-validated; NOT the deployed 3)
- Cost: **`--cost-multipliers 1` = 15 bps honest round-trip** (primary)
- Full-PIT universe required (engine aborts on coverage gaps); +1h entry delay
- All selection filters, sizing, exits = the production baseline
  (`scripts/volume_events_cell.sh` defaults: thresholds 0.4, hold 3, stop 0.12,
  TP 0.26, universe rank 31–400, rank-improvement ≥150, turnover ≥6, etc.)

| arm | cell-id | `--entry-policy` | role |
|---|---|---|---|
| A (control) | `00_baseline` | `fixed_delay` | immediate entry at +1h on the full candidate pool |
| B (treatment) | `01_quality_squeeze` | `promoted_quality_squeeze` | enter on confirmed pop→giveback |

Sweep tag (primary, 15 bps): `e1_exec_premium_2026-05-29`.

**Secondary, conditional robustness (45 bps):** only if the primary shows a premium,
re-run the identical two-arm contrast at `--cost-multipliers 3` under sweep tag
`e1_exec_premium_x3cost_2026-05-29` — confirms the premium survives the conservative
cost and anchors B to the existing 45 bps re-baseline. Same hypothesis, harsher cost
(not a new test).

## Decision rule (a priori) — three-tier demo-arbiter, MAR-primary

Apply the Tier-2 demo-candidate rule (STATE.md) to **B with A as the control**, via
`scripts/r1_robustness.py --sweep-tag e1_exec_premium_2026-05-29 --control 00_baseline`:

- **Premium confirmed (→ proceed to E2):** B return positive on **both** venues
  AND pooled MAR Δ (B−A) > +0.1 AND neither venue worse than MAR Δ ≥ −0.5.
- **Selection-only (→ pivot E2 to selection refinement):** B ≈ A on either venue, or
  the delta does not clear the bar cross-venue.
- Fragility diagnostics (bootstrap p5, LOO, sign-consistency, residual Sharpe)
  REPORTED, non-blocking at Tier-2.
- No further loosening to rescue a near-miss. Tier-3 (real money) is untouched here.

## Run command

```bash
# Serial dispatcher (one ~23 GB cell at a time on the 32 GB box); resumable.
PHASE=e1_exec_premium_2026-05-29 COST=1 bash scripts/e1_exec_premium_dispatch.sh
# verdict:
.venv/bin/python scripts/r1_robustness.py --sweep-tag e1_exec_premium_2026-05-29 --control 00_baseline
```

## Post-run results

(filled in after the run; report paths + commit SHA)

## Verdict

(pending)
