# CARRY sleeve kill criteria — registered 2026-07-29

Governs the CARRY sleeve (registered config `lane2_carry_hold_v3`, promoted
2026-07-28; deployed to demo + paper by owner override 2026-07-29 as the
replacement for the retired CONTINUOUS sleeve). These criteria were proposed
in `docs/carry_hold.md` §8 before any forward evidence existed and are armed
at deployment. The forward clock starts at the CARRY deployment change point
(the rollout receipt's commit); no earlier data counts.

Mirrors the discipline of `sleeve_kill_criteria_2026-07-20.md`: criteria are
demotion triggers for the demo/paper sleeve, checked weekly via
`scripts/ops.sh kill-criteria` (exit 3 on any trip). A trip is executed as a
five-line demotion note, a `deploy/sleeves.env` toggle (demo off, paper
unchanged), and a recorded change point. Nothing here creates real-money
authority.

## Criteria

- **K1 (drawdown)**: forward drawdown of the deployed book exceeding **30%**
  of the operational profile's `capital_reference_usdt` × the carry
  `notional_multiplier`, measured from the forward peak on the canonical
  account journal ⇒ demote to research.
- **K2 (dead run)**: **120 consecutive forward days** with the book deployed
  on ≥ 30% of days and cumulative net P&L ≤ 0 ⇒ demote. No epoch-day gate —
  the window slides from the deployment change point.
- **K3 (mechanism break)**: funding received minus price bleed (the
  `docs/carry_hold.md` §2 attribution, recomputed on forward fills and
  settlements) negative over any **90-day** stretch with the book deployed
  ⇒ demote. The payment, not the price, is the thesis; if the crowd fee
  stops covering the bleed, the mechanism is gone regardless of total P&L.
- **K4 (insufficient sample)**: fewer than **25 deployed days in 180
  calendar days** ⇒ no verdict either way; keep accruing, do not promote,
  and do not read quiet as health.

## Executable form

`liquidity_migration/sleeve_kill_criteria.py` registers sleeve group
`carry = ("carry",)`. Where the shared framework cannot express a clause
exactly (K3's funding-vs-price attribution needs the venue funding ledger
beside fills), the executable check covers the expressible subset and the
remainder is checked manually at the weekly cadence; the code comments name
the split explicitly. A manual check that cannot be completed counts as
"unknown", and unknown safety-critical state fails closed.

## Interaction with the research record

The Lane-2 forward scorer (`scripts/score_financed_longs_forward.py`)
continues to grade the registered config on the research panel
independently of this document. Kill criteria grade the LIVE book on the
canonical account journal. The two records may diverge around symbol
delistings and data holes — the registered terminal-day frame caveat
(`configs/lane2_carry_hold_v3.json` `honesty_notes.frame_caveat`); the
runtime trades without the research frame's forward-return dodge. Neither
record substitutes for the other.
