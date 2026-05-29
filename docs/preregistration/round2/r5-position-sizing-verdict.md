# R5 — 1/realized-vol position sizing — VERDICT (full-PIT)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R5.
**Run label:** `exploratory` (full-PIT, Tier-1 carry-forward, in-sample; no OOS consumed).
**Tools:** `apply_decision_rule.py --rule investigation --control R5_baseline_dollar_equal` + `r1_robustness.py --sweep-tag r5_position_sizing_2026-05-29 --control R5_baseline_dollar_equal`. Dispatcher `scripts/r5_position_sizing_sweep.py` (committed pre-run, a6c8731).

## Headline

**Every `risk_equal` cell is REJECTED (Tier-1 falsifier, bybit MAR Δ ≤ −1.0).
dollar-equal sizing STAYS → R9 uses dollar-equal sizing** (the pre-committed R5
conditional: "if the winner ≈ dollar-equal, sizing doesn't propagate; R9 uses
legacy sizing"). Frozen promoted profile unchanged.

## Run provenance

- Baseline / control = `R5_baseline_dollar_equal` = `drop_all_4` entries +
  promoted exit + default (dollar-equal) sizing — reproduces r13's
  `00_baseline_drop4` bit-for-bit (bybit 2.95×/−10.6%/MAR 5.14; binance
  0.56×/−20.7%/MAR 0.75). Cells override ONLY sizing → pure sizing effect.
- 8/8 cells `full_pit_universe` (tools returned graded verdicts, not `INVALID`).
  Window 2023-04-01 → 2026-05-28, `SWEEP_MAX_WORKERS=1` full-PIT, 56.2 min.

## Results (engine daily-DD MAR; control dollar-equal)

| cell | bybit ret/DD/MAR (Δ) | binance ret/DD/MAR (Δ) | Tier-1 |
|---|---|---|---|
| dollar-equal (control) | 2.95× / −10.6% / 5.14 | 0.56× / −20.7% / 0.75 | — |
| risk_equal 1.0% | 0.45× / −4.5% / 2.78 (**−2.36**) | 0.17× / −5.8% / 0.90 (+0.14) | **reject** |
| risk_equal 1.5% | 0.65× / −4.6% / 3.73 (**−1.41**) | 0.25× / −6.8% / 1.12 (+0.36) | **reject** |
| risk_equal 2.0% | 0.94× / −6.1% / 3.81 (**−1.33**) | 0.36× / −8.1% / 1.29 (+0.52) | **reject** |

## Findings

- **`risk_equal` under-deploys at the tested target vols.** Per-name realized
  daily vol on this alt universe is ~5–15%; with `weight = target_vol /
  realized_vol`, target vols of 1–2% size positions at a small fraction of
  dollar-equal's ~8.3%/name gross. Return craters (bybit 2.95×→0.45–0.94×) far
  more than DD shrinks (−10.6%→−4.5…−6.1%) → **MAR falls on bybit** despite the
  DD reduction. The pre-registered cells (1/1.5/2%) all fail the bybit MAR-Δ
  ≤ −1.0 falsifier.
- **Venue-divergent (recorded finding):** on **binance**, where dollar-equal
  carries a deep −20.7% DD, `risk_equal`'s DD reduction (−5.8…−8.1%) *does*
  improve MAR (0.75→0.90/1.12/1.29; bootstrap P(MAR Δ>0) 69–80%). So
  vol-targeting helps the venue dominated by tail DD and hurts the venue
  dominated by compounding upside. A single joint sizing rule can't win both.
- This is consistent with the plan's anticipation that `target_vol_per_name`
  needs calibration. A higher target (~5%, matching the universe's realized vol,
  to match dollar-equal gross) might change the bybit result — but that is an
  **off-menu cell**; running it requires a dated R5 amendment, not done here.

## DECISION

- **dollar-equal sizing stays. R9 uses dollar-equal** (`--position-weighting
  equal`), per the pre-committed conditional. `risk_equal` (1/1.5/2%) is
  closed-rejected for the daily Architecture-A strategy on this universe.
- No production/profile change (sizing was already dollar-equal).
- Optional future work (off-menu, needs amendment): R5b testing
  `target_vol_per_name` ≈ realized-vol-matched (~4–6%) to isolate the
  re-weighting effect from the gross-deployment effect; and binance-specific
  vol-targeting given its favorable venue-divergent result.

## Next

R2 (per-feature standalone decile + correlation/PCA) — needs a committed
signal-harness runner (the prior orchestration was removed in the 2026-05-28
cleanup). R4 risk model built in parallel.
