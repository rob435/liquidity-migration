# R3b Lane-1 evidence card — correlated-cluster caps (2026-07-20)

**Exploratory Lane-1, insurance-graded (item 27).** Declared grid
ρ_min ∈ {0.6, 0.7} × K ∈ {2, 3}; **registered cell ρ≥0.70 / K=3 chosen
before results were computed**; flankers sensitivity-only. Cluster method:
trailing 720 h hourly log-return Pearson ρ vs open positions at entry,
≥240 overlapping bars (un-correlatable young listings counted separately —
`share_uncorrelatable_pairs` = 0.0 on both surfaces). Declared surface:
T-A render gate_on book (this directory). Labelled supplementary stacking
surface: V2 barebones CONT ledger
(`../p13-r3b-cluster-caps-lane1-2026-07-20-barebones/`) — the
disaster-stop study's own book shape. Runner:
`scripts/research_v3/r3b_cluster_caps_lane1.py`.

## Cluster structure

| Surface | Mean open at entry | Entries with ≥1 corr. open (ρ≥0.7) | ≥3 (registered cap binds) |
| --- | --- | --- | --- |
| render gate_on (deployed shape) | **1.11** | 60 / 2,300 (2.6%) | **0** |
| barebones (stacked) | **15.35** | 1,467 / 16,745 (8.8%) | 445 (2.66%) |

## Registered-cell replay (ρ≥0.7, K=3)

- **Deployed-shape book: the cap never binds.** Zero vetoes in 3.26 years —
  at today's admission funnel the correlated-cluster state the cap targets
  essentially does not occur. The layer is dormant and free.
- **Stacked book:** veto rate 2.66% (445 entries) at **≈ zero net premium**
  — forgone upside +0.1441 vs avoided loss −0.1440 (net +0.00004 over
  3.6 y) — while the vetoed entries' losses concentrate exactly where the
  cap aims: −0.0364 summed net on native tail days (68 vetoed entries),
  −0.0205 on the registered V2 common-loss set (61). Era-split: early
  slightly premium-paying (+0.021), late protective (−0.021); the tail-day
  concentration holds in both eras.
- Sensitivity cells behave monotonically (looser ρ / tighter K bind more,
  same zero-ish premium shape); all cells in each surface's `r3b_grid.csv`.

## Read

A correlated-squeeze cap is exactly what its 2026-06-20 recommendation
claimed: **structural insurance with ~zero average premium that sheds
exposure specifically on common-loss days — but only on a book that
stacks.** Today's deployed book does not stack, so the cap deploys as a
dormant guard whose value case is the future the breadth workstream wants
(8–10 bets/day) and any regime that re-crowds the funnel. Registered
accordingly (frozen cell, per-entry hash-parity A/B veto vs shadow-veto,
kill rules incl. an explicit dormancy closure);
`liquidity_migration/cluster_cap.py` is the staged decision layer.

## Non-conclusions / limits

- Lane-1 on seen surfaces; the barebones surface is supplementary and
  labelled as such (added after the declared surface showed dormancy —
  exploration, not cell reselection; the registered cell was fixed first).
- No capacity backfill; vetoed slots stay empty in the counterfactual.
- Correlation is pairwise-to-open-positions, not a full clustering; a
  graph/community structure is a possible refinement, not registered.
- No deployment implication; wiring into entry admission needs an operator
  go with a recorded change point.
