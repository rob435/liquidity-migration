# Pre-registration: E1 — quantify the execution premium (fixed_delay vs promoted_quality_squeeze)

**Date:** 2026-05-29
**Author:** quant-researcher (autonomous research loop)
**Stage:** run-complete
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

Run 2026-05-29/30, sweep tag `e1_exec_premium_2026-05-29`, full-PIT both venues,
2023-04-01→2026-05-28, capped10 stops, max_active=12, 15 bps (cost×1).
Reports: `~/SHARED_DATA/{bybit,binance}_full_pit/reports/e1_exec_premium_2026-05-29/{00_baseline,01_quality_squeeze}/`.

| venue | arm | total ret | max DD | MAR (daily-DD) | Sharpe | trades |
|---|---|---:|---:|---:|---:|---:|
| bybit | A `fixed_delay` | **+67.3%** | −24.4% | **+2.76** | +1.07 | 763 |
| bybit | B `quality_squeeze` | **+71.2%** | −24.3% | **+2.93** | +1.10 | 761 |
| binance | A `fixed_delay` | **+8.0%** | −28.9% | +0.28 | +0.26 | 477 |
| binance | B `quality_squeeze` | **+7.3%** | −29.1% | +0.25 | +0.25 | 477 |

**Execution premium (B−A):** bybit +3.8% ret / +0.168 MAR; binance −0.7% ret /
−0.026 MAR. **Sign-flips across venues.** Pooled MAR Δ ≈ +0.01 (r1_robustness,
monthly-DD) to +0.07 (daily-DD) — **below the +0.1 Tier-2 bar.**
`r1_robustness` Tier-2 verdict = **`descriptive`** (not a demo-candidate). bybit
premium is fragile: LOO flips sign (carried by 2026-04), top-3 months = 90% of the
positive Δ, bootstrap P(Δ>0)=72%. binance premium negative, bootstrap P(Δ>0)=12%.

**Paired micro-test (the clean, high-power test — same names, immediate A vs
giveback B, only the genuinely time-divergent trades):** bybit 22 divergent,
Δnet +0.062%/trade, B-better 11/22 (50%), **t=+0.35**; binance 9 divergent,
Δnet −0.069%/trade, B-better 3/9, **t=−1.30**. Both noise; opposite signs. The
giveback-timing signal does **not** carry alpha.

**Why B barely differs from A:** `promoted_quality_squeeze` never filters the pool
(A and B trade the identical 477/≈763 candidates) and only re-times entry on
~9–17% of trades (the ones still strongly popping at h+1); 83% short immediately
just like A. The "fade-confirmation execution" is mostly inactive as deployed.

**Cost/funding integrity:** bybit funding **modeled** (net −6.2% drag for the
short, included in the +67.3%); binance funding **missing** (label: `funding-missing`)
— so binance is if anything optimistic, *widening* the asymmetry. This corrects the
STATE.md note that funding is "a short credit [that] likely understates binance."

**Regime note:** the edge is front-loaded — bybit sub-period thirds +29% / +26% /
+4% (recent third much weaker), echoing the momentum-continuation's recency
([[round3-momentum-null-verdict]]); forward demo (2026-05+) lands in the weak regime.

## Verdict

**SELECTION-DOMINANT — no robust cross-venue execution premium.** Immediate-entry
shorting of the liquidity-migration selection pool is the alpha (bybit +67% / MAR 2.76;
binance +8%, funding-missing). The fade-confirmation execution (`promoted_quality_squeeze`)
adds nothing robust: +0.17 MAR on bybit (LOO-fragile, recent-concentrated) but −0.03 on
binance (sign-flip), pooled below the Tier-2 bar, and the high-power paired test is noise.
This fires the plan's E1 falsifier → **pivot E2 toward SELECTION, not execution.**

**Caveat CLOSED (E1b, run-complete):** the knob-engagement probe
([e1b-knob-engagement-2026-05-30.md](e1b-knob-engagement-2026-05-30.md)) forced every
candidate through the pop→giveback wait (bybit giveback trades 69→267, binance 44→199).
The premium was **unchanged** — bybit still +0.15 MAR (LOO-flips), binance still −0.015,
Tier-2 still `descriptive`. The null is robust to engagement level; the selection-dominant
verdict is **final**. E3 (sniper) is not justified — its gate (entry timing matters) fails.
