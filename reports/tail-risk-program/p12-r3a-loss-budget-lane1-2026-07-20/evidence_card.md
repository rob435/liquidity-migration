# R3a Lane-1 evidence card — book-level daily loss budget (2026-07-20)

**Exploratory Lane-1, graded AS INSURANCE (item 27).** Declared cells:
X ∈ {−1.0%, −1.5%, −2.0%} with **X = −1.5% registered in advance** from
kill-criteria arithmetic (K1 −5%/epoch); flankers are sensitivity cells and
did not reselect X. All cells in `r3a_grid.csv` (18 rows), per-trigger-day
detail in `r3a_trigger_days.json`. Surfaces (seen data): full V2 barebones
book (LONG+CONTINUOUS — the trigger is book-level) and the T-A deployed-shape
render gate_on book (CONTINUOUS only; no LONG renders exist). Runner:
`scripts/research_v3/r3a_loss_budget_lane1.py`.

## Rule replayed (frozen shape)

Realized book P&L books at each trade's exit; on the first crossing of
X×capital within a UTC day, entries strictly after the breach time that day
are blocked. Entry-side only; existing exposure untouched; reset at UTC
midnight. Realized-at-exit accounting; intraday unrealized excursions are
not part of the trigger by design.

## Registered-cell results (X = −1.5%)

| Surface / era | Triggers (per yr) | False-trip | Forgone upside | Avoided loss | ES95 base→gov | Worst day base→gov |
| --- | --- | --- | --- | --- | --- | --- |
| barebones full (3.58y) | 36 (10.1) | 8.3% | +0.0335 | −0.0691 | −0.0165→−0.0162 | unchanged |
| barebones early | 6 (3.4) | 33% | +0.0070 | −0.0067 | ≈unchanged | unchanged |
| barebones late (bear/chop) | 30 (16.7) | **3.3%** | +0.0264 | **−0.0624** | −0.0196→−0.0192 | −0.0365→−0.0321 |
| render full (3.26y) | 9 (2.8) | 22% | +0.0058 | 0.0 | ≈unchanged | unchanged |
| render early | 3 (1.8) | 0% | 0 | 0 | unchanged | unchanged |
| render late | 6 (3.7) | 33% | +0.0058 | 0.0 | −0.0096→−0.0098 | unchanged |

Sensitivity: X=−1.0% trips 2.5× as often (25/yr barebones, 19% false-trip,
avoided −0.194 vs forgone +0.084); X=−2.0% trips rarely (4.7/yr) with
proportionally less of both. The registered −1.5% keeps the false-trip rate
below 10% on the surface with real tail days while still binding ~10×/yr.

## Read, as insurance

- **Trigger correctness is high where it matters:** in the barebones late
  era (the regime the program exists for) a −1.5% realized day continued
  deeper 96.7% of the time — blocking the rest of the day was right, and
  the blocked entries went on to lose ~2.4× what they forfeited.
- **The premium is small and the layer is quiet when unneeded:** on the
  deployed-shape render book it triggered 2.8×/yr, blocked six entries in
  3.26 years, cost 0.58% of book total (~0.18%/yr), and avoided nothing in
  a bull window — an acceptable standing cost for a common-mode circuit
  breaker, with the honest note that governed ES95 was marginally *worse*
  on render-late (blocked winners made two trigger days slightly redder).
- This is NOT graded as return improvement; the barebones net gain
  (+3.6pp) is reported as context, not as the claim.

## Non-conclusions / limits

- Lane-1 on seen surfaces; activation is an operator decision under the
  frozen A/B design (`docs/preregistration/r3a_loss_budget_experiment_2026-07-20.md`).
- Realized-at-exit convention; a live governor sees venue-time realized
  cash flow (funding settlements included) — the shadow governor measures
  the live analogue before any activation.
- No capacity backfill; blocked-entry counterfactuals remove whole trades.
- Render surface has no LONG sleeve; the book-level claim on the deployed
  era is CONTINUOUS-only there.
