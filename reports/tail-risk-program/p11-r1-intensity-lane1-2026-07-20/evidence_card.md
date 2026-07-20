# R1 Lane-1 evidence card — continuous risk intensity (2026-07-20)

**Exploratory Lane-1.** Declared 7-member grid (see `manifest.json`), all
cells in `r1_grid.csv` (45 rows: members × eras × surfaces), era-split,
costs/funding inside every net figure. MAR not computed anywhere. Data that
shaped: V2 barebones CONT ledger + T-A render books + BTCUSDT klines — all
seen surfaces; the reserved V2 label-level holdout was not read. Runner:
`scripts/research_v3/r1_intensity_lane1.py` (from-origin causal BTC-risk
replay; weights applied at each trade's signal day).

## Claim under test

Replacing the deployed binary BTC gate + discrete 0.35× overlay with one
monotone gross multiplier (T-I's linear member composed with a monotonized
risk ramp) improves the book's tail at an acceptable, explicitly-priced net
premium — the T-I "metric-artifact kill" re-examined under tail metrics.

## Results

**Barebones surface (2021-05→2024-12, negative-net discovery era):** the T-I
claim reproduces exactly under tail metrics. `linear10` vs `binary` at
equal net (−0.1350 vs −0.1348): maxDD −0.308 vs −0.359, ES95 −0.0146 vs
−0.0164, ES99 −0.0255 vs −0.0272, registered-tail losses −0.189 vs −0.276,
native-tail losses −0.935 vs −1.129. Pareto-dominant on this surface. The
composite `linear10_discrete35` is best on net AND tails.

**Render gate_off surface (deployed-shape book, 2023-04→2026-07):** the
domination becomes a priced tradeoff. `linear10_ramp` vs `binary_discrete35`
(deployed shape), full window: net +0.414 vs +0.537 (**premium −12.3pp over
~3.2y ≈ −3.8pp/yr**), ES95 −0.0069 vs −0.0090 (**−23%**), ES99 −0.0136 vs
−0.0168 (**−19%**), native-tail-day losses −0.173 vs −0.259 (**−33%**),
maxDD −0.043 vs −0.043 (unchanged). Era-split: the tail relief is stable in
sign in both halves (ES95 −15%/−30%, ES99 −11%/−26%, tail losses −19%/−44%);
the premium concentrates in the late bull half (−1.4pp early, −10.9pp late)
— strong-but-<10% trend days that binary takes at full size are downsized
and were profitable. Forgone gross next to avoided cost: linear forgoes
+0.437 gross vs binary's +0.246; net intervention delta −0.101 vs +0.022.

**Overlay axis:** `discrete35` vs `ramp` is nearly inert post-2023 (the
expanding-percentile band is rarely visited); the material axis today is
binary→linear on trend. The ramp matters only in high-risk states — exactly
the target regime — and costs nothing when idle.

**Faithfulness check:** ledger-level `gate_off × binary` reproduces the
independently rendered gate-on book to −0.28pp full-window net (0.5374 vs
0.5402) — the weighting proxy is tight on this surface.

## Read

The V4 lesson repeats: a discovery-surface Pareto story attenuates on the
deployed book. What survives, era-stable, is the tail claim: **monotone
intensity buys 19–33% tail relief for ~3.8pp/yr of net premium.** That is a
well-posed insurance candidate under the program's grading — not a free
improvement, and it should never be sold as one. Registered forward as a
shadow A/B (`r1_intensity_v1` config; the commit carrying this card is the
registration) with pre-committed kill criteria on the premium, the tail
relief, and divergence sample; deployment is not proposed today.

## Non-conclusions and limits

- No alpha claim; no promotion; the render surface is Lane-1 (it shaped T-A
  through T-K selection).
- Ledger-level weighting: capacity/admission/hedge interactions not
  re-solved (bounded by the −0.28pp check above on the binary member; the
  linear member's real-render interaction is untested until a true render
  arm exists).
- From-origin score replay differs from the live overlay's own (later-born)
  percentile history; forward scorer uses the same committed replay.
- Sixth-generation read on the T-I family; prior selection discounts apply
  (hypothesis-ledger row recorded at the registration commit).
